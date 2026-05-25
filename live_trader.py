"""
AUREON v2 — LiveTrader: production-ready live/paper trading loop.

This module implements the runtime that was stubbed in bot.py's run_live().
Imports cleanly into bot.py.

Architecture
------------
Single event loop, wakes every 5 seconds, performs these checks in order:

  1. New broker day?            → reset daily P&L, clear processed-anchors list
  2. Kill switch tripped?       → flatten everything, sleep until next day
  3. EOD time reached?          → close all open positions at market
  4. Anchor time due?           → capture M5, place 2 pending orders
  5. New M1 bar closed?         → for each open position, recompute SL via trail
                                    logic; if it advanced, modify SL on broker
  6. Commands from watchdog?    → process /flatten /pause /resume etc.

Heartbeat, status, commands
---------------------------
  AUREON_RUN_DIR/heartbeat            ← we `touch` it every tick
  AUREON_RUN_DIR/status.json          ← we write current state every 30 seconds
  AUREON_RUN_DIR/commands.json        ← watchdog appends; we consume & remove
  AUREON_RUN_DIR/today_trades.csv     ← we append every closed trade

Telemetry
---------
  Every meaningful event flows through self.tele (a Telemetry instance):
    info  — anchor processed, position opened, SL moved (rate-limited)
    success — trade closed profitably, EOD positive
    warn  — SL hit, anchor missed, broker reconnect
    error — order rejected, MT5 disconnect
    critical — kill switch, account-floor breach
"""

import csv
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import date as DateType, timedelta, datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from telemetry import telemetry_from_env, Severity

log = logging.getLogger("AUREON")


# ============================================================================
# LiveTrader
# ============================================================================

class LiveTrader:
    """
    Production live/paper trading loop. Pass paper=True to log actions
    without sending real orders.
    """

    HEARTBEAT_EVERY_TICKS = 1         # touch heartbeat every tick (5s)
    STATUS_EVERY_TICKS    = 6         # write status.json every 30s
    COMMAND_POLL_EVERY    = 1         # poll commands every tick

    def __init__(self, cfg, adapter, paper: bool = True):
        from bot import Position, anchor_datetime_utc, eod_datetime_utc  # late import

        self.cfg = cfg
        self.adapter = adapter
        self.paper = paper
        self._Position = Position
        self._anchor_datetime_utc = anchor_datetime_utc
        self._eod_datetime_utc = eod_datetime_utc

        # Telemetry
        component = f"AUREON-{'paper' if paper else 'live'}"
        self.tele = telemetry_from_env(component=component)

        # Run dir for IPC files (heartbeat / status / commands)
        run_dir = os.environ.get("AUREON_RUN_DIR", "./run")
        os.makedirs(run_dir, exist_ok=True)
        self.run_dir = run_dir
        self.heartbeat_path = os.path.join(run_dir, "heartbeat")
        self.status_path    = os.path.join(run_dir, "status.json")
        self.commands_path  = os.path.join(run_dir, "commands.json")
        self.daylog_path    = os.path.join(run_dir, "today_trades.csv")

        # Persistent state
        self.state_path = cfg.state_file
        self.state = self._load_state()

        # In-memory shadow of broker state. Maps ticket -> dict.
        self.shadow_positions: Dict = {}
        self.shadow_pendings: Dict = {}

        # Bar-close tracking
        self._last_managed_minute: Optional[pd.Timestamp] = None
        self._tick_counter = 0

        # Pause flag (set via /pause command)
        self.paused = False

        # Today's trade log header
        if not os.path.exists(self.daylog_path):
            with open(self.daylog_path, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["date", "anchor", "side", "entry", "exit",
                     "outcome", "pnl_usd", "ticket"])

        self.tele.info(
            f"LiveTrader initialized ({'PAPER' if paper else 'LIVE'}) — "
            f"4-anchor multi-session AUREON, lot {cfg.lot_size}"
        )
        self.tele.info(
            f"Anchors: {[a[0] for a in cfg.anchors]}, "
            f"kill switch: -{cfg.daily_loss_pct*100:.1f}%"
        )

    # ------------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------------

    def _load_state(self) -> Dict:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    s = json.load(f)
                log.info(f"Restored state from {self.state_path}")
                return s
            except Exception as e:
                log.warning(f"Could not load state ({e}); starting fresh")
        return {
            'daily_pnl': 0.0,
            'last_broker_date': None,
            'processed_anchors_today': [],
            'kill_switch_locked': False,
        }

    def _save_state(self):
        if self.paper:
            return
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.state, f, indent=2, default=str)
        os.replace(tmp, self.state_path)

    def _broker_date(self, utc_now: pd.Timestamp) -> DateType:
        return (utc_now + pd.Timedelta(hours=self.cfg.broker_tz_offset_hours)).date()

    # ------------------------------------------------------------------------
    # Auto-sizing from live account balance
    # ------------------------------------------------------------------------

    def _compute_safe_lot(self, balance: float) -> float:
        """
        Return the largest safe lot under Funding Pips per-trade risk rules.
        3% on accounts <$50k, 2% on ≥$50k. Apply slippage buffer + conservatism.
        Rounds DOWN to broker's 0.01 lot precision so we never breach.
        """
        risk_pct = (self.cfg.risk_pct_over_50k if balance >= 50_000
                    else self.cfg.risk_pct_under_50k)
        max_loss = balance * risk_pct * self.cfg.slippage_buffer
        # SL distance × oz per lot (contract_size assumed 100 for XAUUSD)
        max_lot = max_loss / (self.cfg.sl_dist * 100)
        # Apply user conservatism multiplier
        effective_lot = max_lot * self.cfg.lot_conservatism
        # Floor to 0.01 precision (round DOWN, never UP)
        effective_lot = max(0.01, int(effective_lot * 100) / 100)
        return effective_lot

    def _refresh_from_broker(self, reason: str = "startup"):
        """Pull balance/equity from MT5, optionally re-compute lot. Returns dict or {}."""
        if self.paper:
            return {}
        info = self.adapter.get_account_info()
        if not info:
            self.tele.error("Could not read account info from MT5")
            return {}
        old_balance = self.cfg.starting_balance
        new_balance = info['balance']
        self.cfg.starting_balance = new_balance

        if self.cfg.auto_lot:
            old_lot = self.cfg.lot_size
            new_lot = self._compute_safe_lot(new_balance)
            if abs(new_lot - old_lot) > 0.001:
                self.cfg.lot_size = new_lot
                self.tele.success(
                    f"📊 *Auto-lot updated ({reason})*\n"
                    f"Account #{info['login']}  on `{info['server']}`\n"
                    f"Balance: `${new_balance:,.2f}`  Equity: `${info['equity']:,.2f}`\n"
                    f"Lot: `{old_lot}` → `{new_lot}`\n"
                    f"Max risk/trade: `${new_lot * self.cfg.sl_dist * 100:,.0f}` "
                    f"(`{100 * new_lot * self.cfg.sl_dist * 100 / new_balance:.2f}%` of balance)\n"
                    f"Daily kill switch: `-${new_balance * self.cfg.daily_loss_pct:,.0f}` "
                    f"(`{self.cfg.daily_loss_pct*100:.1f}%`)"
                )
            else:
                self.tele.info(
                    f"📊 *Account refresh ({reason})*\n"
                    f"Balance: `${new_balance:,.2f}`  Equity: `${info['equity']:,.2f}`\n"
                    f"Lot: `{new_lot}` (unchanged)"
                )
        else:
            self.tele.info(
                f"📊 *Account refresh ({reason})*\n"
                f"Balance: `${new_balance:,.2f}`  Equity: `${info['equity']:,.2f}`\n"
                f"Lot: `{self.cfg.lot_size}` (manual, auto_lot=False)"
            )
        return info

    def _live_equity(self) -> Optional[float]:
        """Get current equity from MT5 (includes unrealized P&L). None on failure."""
        if self.paper:
            return None
        info = self.adapter.get_account_info()
        return info.get('equity') if info else None

    def _reset_if_new_day(self, broker_date: DateType):
        if str(broker_date) != self.state.get('last_broker_date'):
            if self.state.get('last_broker_date'):
                # Send daily summary for the day that just ended
                self._send_daily_summary(self.state.get('last_broker_date'),
                                          self.state.get('daily_pnl', 0.0))
            self.tele.info(f"📅 New broker day: {broker_date}")
            self.state['daily_pnl'] = 0.0
            self.state['last_broker_date'] = str(broker_date)
            self.state['processed_anchors_today'] = []
            self.state['kill_switch_locked'] = False
            self._save_state()
            # Reset today's trade log
            with open(self.daylog_path, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["date", "anchor", "side", "entry", "exit",
                     "outcome", "pnl_usd", "ticket"])
            # Refresh balance/equity and recompute lot for the new day
            self._refresh_from_broker(reason=f"new day {broker_date}")

    # ------------------------------------------------------------------------
    # Heartbeat, status, commands
    # ------------------------------------------------------------------------

    def _touch_heartbeat(self):
        with open(self.heartbeat_path, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())

    def _write_status(self, broker_date: DateType):
        # Try to fetch live broker state (live mode only)
        broker_info = {}
        if not self.paper:
            try:
                broker_info = self.adapter.get_account_info()
            except Exception:
                pass
        status = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "broker_date": str(broker_date),
            "daily_pnl_realized": self.state.get("daily_pnl", 0.0),
            "open_positions": len(self.shadow_positions),
            "pending_orders": len(self.shadow_pendings),
            "anchors_processed_today": self.state.get("processed_anchors_today", []),
            "kill_switch_locked": self.state.get("kill_switch_locked", False),
            "paused": self.paused,
            "mode": "paper" if self.paper else "live",
            "lot_size": self.cfg.lot_size,
            "starting_balance": self.cfg.starting_balance,
            "broker_balance": broker_info.get("balance"),
            "broker_equity":  broker_info.get("equity"),
            "broker_login":   broker_info.get("login"),
            "broker_server":  broker_info.get("server"),
            "daily_loss_pct": self.cfg.daily_loss_pct,
            "kill_threshold_usd": self.cfg.daily_loss_pct * self.cfg.starting_balance,
        }
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=2, default=str)
        os.replace(tmp, self.status_path)

    def _consume_commands(self) -> List[Dict]:
        if not os.path.exists(self.commands_path):
            return []
        try:
            with open(self.commands_path) as f:
                cmds = json.load(f)
        except Exception:
            return []
        # Clear the file by overwriting with []
        try:
            with open(self.commands_path, "w") as f:
                json.dump([], f)
        except Exception:
            pass
        return cmds

    def _handle_commands(self):
        cmds = self._consume_commands()
        for c in cmds:
            cmd = c.get("cmd", "").lower()
            if cmd == "flatten":
                self.tele.warn("🚨 /flatten received — closing all positions")
                self._flatten_all(reason="ManualFlatten")
            elif cmd == "pause":
                self.paused = True
                self.tele.info("⏸ Paused — no new anchor orders until /resume")
            elif cmd == "resume":
                self.paused = False
                self.tele.info("▶️ Resumed — anchor processing back on")
            elif cmd == "today_summary":
                self._send_today_summary()

    # ------------------------------------------------------------------------
    # Time-driven actions
    # ------------------------------------------------------------------------

    def _check_kill_switch(self) -> bool:
        """
        Daily kill switch — fires if losses exceed cfg.daily_loss_pct of starting balance.
        In LIVE mode: uses live equity (includes unrealized P&L from open positions),
                      which matches how Funding Pips actually measures the daily loss rule.
        In PAPER mode: falls back to internal daily_pnl (realized only).
        """
        threshold = self.cfg.daily_loss_pct * self.cfg.starting_balance
        equity = self._live_equity()
        if equity is not None:
            live_daily_loss = self.cfg.starting_balance - equity
            return live_daily_loss >= threshold
        # Fallback (paper or MT5 query failure)
        return self.state['daily_pnl'] <= -threshold

    def _process_anchor_if_due(self, broker_date: DateType, utc_now: pd.Timestamp):
        if self.paused:
            return
        for label, hour in self.cfg.anchors:
            if label in self.state['processed_anchors_today']:
                continue
            anchor_utc = self._anchor_datetime_utc(
                broker_date, hour, self.cfg.broker_tz_offset_hours)
            delta = (utc_now - anchor_utc).total_seconds()
            # Window: 0 to 120 seconds after the anchor minute
            if 0 <= delta < 120:
                self._process_anchor(label, anchor_utc)
                self.state['processed_anchors_today'].append(label)
                self._save_state()

    def _process_anchor(self, label: str, anchor_utc: pd.Timestamp):
        anchor_price = self.adapter.get_m5_close(self.cfg.symbol, anchor_utc)
        if anchor_price is None:
            self.tele.warn(f"⚠️ Could not fetch M5 close at {anchor_utc} — skipping {label}")
            return

        buy_stop  = round(anchor_price + self.cfg.trigger_dist, 2)
        sell_stop = round(anchor_price - self.cfg.trigger_dist, 2)
        self.tele.info(
            f"⚓ *{label}* anchor=${anchor_price:.2f}\n"
            f"  BUY  stop @ ${buy_stop}  (SL ${buy_stop-self.cfg.sl_dist:.2f})\n"
            f"  SELL stop @ ${sell_stop} (SL ${sell_stop+self.cfg.sl_dist:.2f})"
        )

        buy_res = self.adapter.place_stop_order(
            self.cfg.symbol, 'BUY', buy_stop, self.cfg.lot_size,
            sl=round(buy_stop - self.cfg.sl_dist, 2),
            tp=round(buy_stop + self.cfg.tp_dist, 2),
            comment=f"AUREONv2_{label}_BUY", dry_run=self.paper)
        sell_res = self.adapter.place_stop_order(
            self.cfg.symbol, 'SELL', sell_stop, self.cfg.lot_size,
            sl=round(sell_stop + self.cfg.sl_dist, 2),
            tp=round(sell_stop - self.cfg.tp_dist, 2),
            comment=f"AUREONv2_{label}_SELL", dry_run=self.paper)

        buy_ticket  = self._extract_ticket(buy_res,  f"paper_{label}_BUY")
        sell_ticket = self._extract_ticket(sell_res, f"paper_{label}_SELL")
        if buy_ticket is not None and sell_ticket is not None:
            self.shadow_pendings[buy_ticket] = {
                'anchor_label': label, 'side': 'BUY',
                'sibling_ticket': sell_ticket,
                'entry_price': buy_stop,
            }
            self.shadow_pendings[sell_ticket] = {
                'anchor_label': label, 'side': 'SELL',
                'sibling_ticket': buy_ticket,
                'entry_price': sell_stop,
            }
        else:
            self.tele.error(f"❌ Failed to place pending orders for {label}")

    @staticmethod
    def _extract_ticket(result, fallback: str):
        if result is None: return None
        if isinstance(result, dict) and result.get('paper'):
            return fallback
        ticket = getattr(result, 'order', None)
        if ticket:
            return int(ticket)
        return None

    def _eod_reached(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        eod = self._eod_datetime_utc(broker_date, self.cfg)
        return utc_now >= eod

    # ------------------------------------------------------------------------
    # OCO emulation and fill detection
    # ------------------------------------------------------------------------

    def _reconcile_with_broker(self):
        if self.paper:
            return

        try:
            broker_positions = self.adapter.mt5.positions_get(symbol=self.cfg.symbol) or []
            broker_pendings  = self.adapter.mt5.orders_get(symbol=self.cfg.symbol)    or []
        except Exception as e:
            self.tele.warn(f"MT5 reconcile failed: {e}")
            return

        broker_pos_tickets  = {int(p.ticket) for p in broker_positions}
        broker_pend_tickets = {int(o.ticket) for o in broker_pendings}

        # Detect fills (sibling cancel)
        for ticket, info in list(self.shadow_pendings.items()):
            if isinstance(ticket, str): continue
            if ticket not in broker_pend_tickets and ticket in broker_pos_tickets:
                info = self.shadow_pendings.pop(ticket)
                sibling = info['sibling_ticket']
                self.tele.info(
                    f"🎯 FILL: *{info['anchor_label']}* {info['side']} "
                    f"@ ${info['entry_price']:.2f} (ticket {ticket})"
                )
                # Cancel sibling (OCO)
                if sibling in broker_pend_tickets:
                    try:
                        self.adapter.cancel_order(sibling)
                    except Exception as e:
                        self.tele.warn(f"Could not cancel sibling {sibling}: {e}")
                self.shadow_pendings.pop(sibling, None)
                # Promote to managed position
                broker_p = next(p for p in broker_positions if int(p.ticket) == ticket)
                self.shadow_positions[ticket] = {
                    'anchor_label': info['anchor_label'],
                    'side':         info['side'],
                    'entry_price':  float(broker_p.price_open),
                    'current_sl':   float(broker_p.sl),
                    'tp_level':     float(broker_p.tp),
                    'max_fav':      float(broker_p.price_open),
                }

        # Detect closures
        for ticket in list(self.shadow_positions):
            if ticket in broker_pos_tickets:
                continue
            shadow = self.shadow_positions.pop(ticket)
            try:
                deals = self.adapter.mt5.history_deals_get(position=ticket) or []
                close_deal = next((d for d in deals if d.entry == 1), None)
                if close_deal:
                    pnl_usd = float(close_deal.profit) + float(close_deal.swap) + float(close_deal.commission)
                    self.state['daily_pnl'] += pnl_usd
                    close_price = float(close_deal.price)
                    # Determine outcome label
                    if shadow['side'] == 'BUY':
                        if abs(close_price - (shadow['entry_price'] + self.cfg.tp_dist)) < 0.05:
                            outcome = 'TP'
                        elif close_price <= shadow['entry_price'] - self.cfg.sl_dist + 0.05:
                            outcome = 'SL'
                        else:
                            outcome = 'Trail'
                    else:
                        if abs(close_price - (shadow['entry_price'] - self.cfg.tp_dist)) < 0.05:
                            outcome = 'TP'
                        elif close_price >= shadow['entry_price'] + self.cfg.sl_dist - 0.05:
                            outcome = 'SL'
                        else:
                            outcome = 'Trail'
                    sev = Severity.SUCCESS if pnl_usd > 0 else Severity.WARN
                    self.tele.send(
                        f"📤 CLOSE: *{shadow['anchor_label']}* {shadow['side']} "
                        f"`{outcome}` @ ${close_price:.2f}\n"
                        f"P&L: `${pnl_usd:+.2f}`  |  Daily total: `${self.state['daily_pnl']:+.2f}`",
                        sev
                    )
                    # Append to today's trade log
                    with open(self.daylog_path, "a", newline="") as f:
                        csv.writer(f).writerow([
                            self.state['last_broker_date'],
                            shadow['anchor_label'], shadow['side'],
                            shadow['entry_price'], close_price,
                            outcome, round(pnl_usd, 2), ticket,
                        ])
                    self._save_state()
            except Exception as e:
                self.tele.warn(f"Could not fetch close deal for {ticket}: {e}")

    # ------------------------------------------------------------------------
    # Trail management on M1 bar close
    # ------------------------------------------------------------------------

    def _manage_trails_on_bar_close(self):
        if not self.shadow_positions:
            return
        bars = self.adapter.get_latest_m1(self.cfg.symbol, 2)
        if bars is None or len(bars) < 2:
            return
        closed_bar = bars[-2]
        bar_series = pd.Series({
            'open':  float(closed_bar['open']),
            'high':  float(closed_bar['high']),
            'low':   float(closed_bar['low']),
            'close': float(closed_bar['close']),
        })
        bar_time = pd.Timestamp(closed_bar['time'], unit='s', tz='UTC')

        from bot import update_position_on_bar  # late import

        for ticket, shadow in list(self.shadow_positions.items()):
            old_sl = shadow['current_sl']
            pos = self._Position(
                anchor_label=shadow['anchor_label'],
                side=shadow['side'],
                entry_price=shadow['entry_price'],
                entry_time=bar_time,
                current_sl=shadow['current_sl'],
                tp_level=shadow['tp_level'],
                max_fav=shadow['max_fav'],
                lot=self.cfg.lot_size,
            )
            update_position_on_bar(pos, bar_series, bar_time, self.cfg)
            shadow['current_sl'] = pos.current_sl
            shadow['max_fav']    = pos.max_fav

            if pos.current_sl != old_sl:
                # Log only — too noisy for Telegram
                log.info(
                    f"Trail advance ticket={ticket} side={shadow['side']} "
                    f"SL ${old_sl:.2f} → ${pos.current_sl:.2f} "
                    f"(max_fav=${pos.max_fav:.2f})"
                )
                if not self.paper:
                    try:
                        self.adapter.modify_position_sl(ticket, round(pos.current_sl, 2))
                    except Exception as e:
                        self.tele.warn(f"Could not modify SL on {ticket}: {e}")

    # ------------------------------------------------------------------------
    # Bulk operations & summaries
    # ------------------------------------------------------------------------

    def _flatten_all(self, reason: str = "Manual"):
        self.tele.warn(f"FLATTEN ({reason}) — closing {len(self.shadow_positions)} positions, "
                       f"cancelling {len(self.shadow_pendings)} pendings")
        for ticket in list(self.shadow_positions.keys()):
            try:
                self.adapter.close_position(ticket, dry_run=self.paper)
            except Exception as e:
                self.tele.error(f"Failed to close {ticket}: {e}")
            self.shadow_positions.pop(ticket, None)
        for ticket in list(self.shadow_pendings.keys()):
            try:
                self.adapter.cancel_order(ticket, dry_run=self.paper)
            except Exception as e:
                self.tele.error(f"Failed to cancel {ticket}: {e}")
            self.shadow_pendings.pop(ticket, None)

    def _send_daily_summary(self, day_str: str, pnl: float):
        emoji = "✅" if pnl > 0 else ("➖" if pnl == 0 else "📉")
        # Try to read today_trades.csv for richer detail
        n_trades = 0; wins = 0; sls = 0
        try:
            with open(self.daylog_path) as f:
                rows = list(csv.DictReader(f))
            n_trades = len(rows)
            wins = sum(1 for r in rows if float(r["pnl_usd"]) > 0)
            sls  = sum(1 for r in rows if r["outcome"] == "SL")
        except Exception:
            pass
        msg = (f"{emoji} *Daily summary {day_str}*\n"
               f"P&L: `${pnl:+,.2f}`\n"
               f"Trades: `{n_trades}` (wins `{wins}`, SLs `{sls}`)")
        sev = Severity.SUCCESS if pnl > 0 else Severity.WARN
        self.tele.send(msg, sev)

    def _send_today_summary(self):
        day_str = self.state.get("last_broker_date", "?")
        pnl = self.state.get("daily_pnl", 0.0)
        self._send_daily_summary(day_str, pnl)

    # ------------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------------

    def run(self):
        self.tele.success(
            f"🚀 *AUREON v2 {'PAPER' if self.paper else 'LIVE'} starting*\n"
            f"Lot: `{self.cfg.lot_size}` (auto_lot={'on' if self.cfg.auto_lot else 'off'})\n"
            f"Kill switch: `-{self.cfg.daily_loss_pct*100:.1f}%`"
        )

        # Broker time check.
        # Note: MT5 Python API only exposes the LAST TICK time, which becomes
        # stale during weekends/holidays. We must distinguish two cases:
        #   - tick is very old (>1h)  → market is closed; sleep until market opens
        #   - tick is recent (<1h) but disagrees with OS clock by >2min → real problem
        market_open = True
        try:
            server_utc = self.adapter.server_time_utc()
            now_utc = pd.Timestamp.now(tz='UTC')
            tick_age_sec = (now_utc - server_utc).total_seconds()
            if tick_age_sec > 3600:
                # Last tick is more than an hour old — market is closed.
                # SLEEP and re-check every 5 minutes instead of exiting.
                hours = tick_age_sec / 3600
                self.tele.info(
                    f"📅 Market closed (last tick {hours:.1f}h old). "
                    f"Bot will sleep and re-check every 5 min until market opens. "
                    f"First anchor of week is 02:00 broker (Mon Asia)."
                )
                market_open = False
                # Sleep loop: wait for market to reopen
                while not market_open:
                    time.sleep(300)  # 5 minutes
                    try:
                        server_utc = self.adapter.server_time_utc()
                        now_utc = pd.Timestamp.now(tz='UTC')
                        tick_age_sec = (now_utc - server_utc).total_seconds()
                        if tick_age_sec < 60:
                            self.tele.success(
                                f"📈 Market open detected — broker tick is fresh "
                                f"({tick_age_sec:.0f}s old). Starting trader loop."
                            )
                            market_open = True
                    except Exception as e:
                        log.warning(f"Market-open check failed: {e}")
            elif abs(tick_age_sec) > 120:
                # Market is open but clock disagrees with broker → config problem
                self.tele.critical(
                    f"❌ Broker server time drifts >2min from local UTC "
                    f"(broker tick {server_utc} vs local {now_utc}). ABORTING. "
                    f"Fix the OS clock (sync NTP) and restart.")
                return
            # else: tick recent and within tolerance → all good
        except Exception as e:
            self.tele.warn(f"Could not verify broker time: {e}")

        # Initial balance + lot autodetect (live mode only)
        if not self.paper:
            self._refresh_from_broker(reason="startup")

        try:
            while True:
                try:
                    self._tick()
                except Exception as e:
                    self.tele.error(f"Tick failed: {e}")
                    log.exception("Tick exception")
                time.sleep(5)
        except KeyboardInterrupt:
            self.tele.warn("Manual interrupt received — flattening positions")
            self._flatten_all(reason="ManualInterrupt")
        finally:
            self.tele.stop()

    def _tick(self):
        self._tick_counter += 1
        utc_now = pd.Timestamp.now(tz='UTC')
        broker_date = self._broker_date(utc_now)

        # 1. Heartbeat (every tick)
        self._touch_heartbeat()

        # 2. New broker day?
        self._reset_if_new_day(broker_date)

        # 3. Reconcile broker state
        self._reconcile_with_broker()

        # 4. Handle inbound commands
        self._handle_commands()

        # 5. Kill switch?
        if self._check_kill_switch() and not self.state['kill_switch_locked']:
            self.tele.critical(
                f"🚨 *KILL SWITCH TRIGGERED*\n"
                f"Daily P&L: `${self.state['daily_pnl']:.2f}` "
                f"(limit `${-self.cfg.daily_loss_pct * self.cfg.starting_balance:.0f}`)\n"
                f"Flattening everything, no more trades today."
            )
            self._flatten_all(reason="KillSwitch")
            self.state['kill_switch_locked'] = True
            self._save_state()

        if self.state['kill_switch_locked']:
            # Still emit status periodically but don't open new trades
            if self._tick_counter % self.STATUS_EVERY_TICKS == 0:
                self._write_status(broker_date)
            return

        # 6. EOD?
        if self._eod_reached(broker_date, utc_now):
            if self.shadow_positions or self.shadow_pendings:
                self._flatten_all(reason="EOD")
            if self._tick_counter % self.STATUS_EVERY_TICKS == 0:
                self._write_status(broker_date)
            return

        # 7. Anchor due?
        self._process_anchor_if_due(broker_date, utc_now)

        # 8. M1 bar close → trails
        current_minute = utc_now.floor('1min')
        if current_minute != self._last_managed_minute:
            seconds_into = utc_now.second
            if seconds_into >= 3:
                self._manage_trails_on_bar_close()
                self._last_managed_minute = current_minute

        # 9. Status snapshot
        if self._tick_counter % self.STATUS_EVERY_TICKS == 0:
            self._write_status(broker_date)