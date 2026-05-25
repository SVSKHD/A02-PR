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
from bot import _MT5_RETCODE_MAP

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
        self.price_log_dir  = os.path.join(run_dir, "price_log")  # daily-rotated CSVs
        os.makedirs(self.price_log_dir, exist_ok=True)

        # Persistent state
        self.state_path = cfg.state_file
        self.state = self._load_state()

        # In-memory shadow of broker state. Maps ticket -> dict.
        self.shadow_positions: Dict = {}
        self.shadow_pendings: Dict = {}

        # Bar-close tracking
        self._last_managed_minute: Optional[pd.Timestamp] = None
        self._tick_counter = 0
        # Hot polling window: for 30s after firing an anchor we tick at 0.2s
        # to catch fills fast. After that, back to normal 1.0s cadence.
        self._hot_poll_until: Optional[pd.Timestamp] = None

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

    def _log_price(self, utc_now: pd.Timestamp):
        """Write a per-tick row to today's price log. CSV per broker-date.
        Captures: timestamp_utc, broker_time, bid, ask, mid, spread, last_m1_close.
        At 1-sec polling, ~86k rows/day ≈ 5MB/day. Auto-rotates daily."""
        if self.paper:
            return  # no live tick data in paper mode
        try:
            tick = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
            if tick is None:
                return
            bid, ask = float(tick.bid), float(tick.ask)
            spread_dollars = round(ask - bid, 4)
            mid = round((bid + ask) / 2, 4)
            broker_now = utc_now + pd.Timedelta(hours=self.cfg.broker_tz_offset_hours)
            broker_date = broker_now.date()
            # M1 close (last completed bar) — quick check, not critical
            m1_close = ""
            try:
                m1_bars = self.adapter.get_latest_m1(self.cfg.symbol, 1)
                if m1_bars is not None and len(m1_bars) > 0:
                    m1_close = float(m1_bars[0]['close'])
            except Exception:
                pass

            csv_path = os.path.join(self.price_log_dir, f"price_{broker_date}.csv")
            need_header = not os.path.exists(csv_path)
            with open(csv_path, "a", newline="") as f:
                w = csv.writer(f)
                if need_header:
                    w.writerow(["utc", "broker_time", "bid", "ask", "mid", "spread", "m1_close"])
                w.writerow([
                    utc_now.isoformat(timespec='seconds'),
                    broker_now.isoformat(timespec='seconds'),
                    bid, ask, mid, spread_dollars, m1_close,
                ])
        except Exception as e:
            log.debug(f"price log write failed: {e}")

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

        # Get current market price BEFORE attempting any orders.
        # We need this to detect gap-anchors that would produce invalid stops.
        current_price = None
        try:
            tick = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
            if tick:
                current_price = (tick.ask + tick.bid) / 2
        except Exception as e:
            log.warning(f"Could not read current tick for {label}: {e}")

        # ADAPTIVE RE-ANCHOR ON GAP DAYS
        # If the captured anchor is too far from current market, BOTH stops
        # would be on the same side of price → one is mechanically invalid.
        # Instead of skipping (passive), we re-anchor to current M5 close and
        # trade the breakout from there with REDUCED RISK (half-lot, tight SL).
        gap_mode = False
        gap_lot = self.cfg.lot_size
        gap_sl_dist = self.cfg.sl_dist
        gap_tp_dist = self.cfg.tp_dist
        if current_price is not None:
            gap = abs(current_price - anchor_price)
            if gap > self.cfg.trigger_dist + 0.5:  # 50¢ buffer past the trigger
                # Try to use the most recent M5 close as the new anchor.
                # We fetch the M5 bar just before NOW (not the scheduled anchor time).
                try:
                    now_utc = pd.Timestamp.now(tz='UTC')
                    # Round DOWN to nearest 5 min boundary, then go one bar back
                    minute = now_utc.minute - (now_utc.minute % 5)
                    last_m5_end = now_utc.replace(minute=minute, second=0, microsecond=0)
                    new_anchor = self.adapter.get_m5_close(self.cfg.symbol, last_m5_end)
                    if new_anchor is None or abs(new_anchor - current_price) > self.cfg.trigger_dist:
                        # Couldn't get fresh M5 OR fresh M5 also far from market
                        # → use current price as anchor directly
                        new_anchor = round(current_price, 2)
                except Exception as e:
                    log.warning(f"Re-anchor M5 fetch failed: {e}")
                    new_anchor = round(current_price, 2)

                gap_mode = True
                gap_lot = round(self.cfg.lot_size / 2, 2)  # half-size
                gap_sl_dist = 10.0    # tight SL: $10 instead of $18
                gap_tp_dist = self.cfg.tp_dist  # keep normal TP
                self.tele.warn(
                    f"⚠️ *{label} GAP DETECTED*\n"
                    f"Original anchor: `${anchor_price:.2f}`\n"
                    f"Current market:  `${current_price:.2f}`\n"
                    f"Gap: `${gap:.2f}` (> ${self.cfg.trigger_dist + 0.5:.2f} threshold)\n"
                    f"→ Re-anchoring to current M5 close `${new_anchor:.2f}`\n"
                    f"→ Half-lot `{gap_lot}` with tight SL `${gap_sl_dist:.0f}` "
                    f"(reduced risk for gap-day breakout)"
                )
                anchor_price = new_anchor

        buy_stop  = round(anchor_price + self.cfg.trigger_dist, 2)
        sell_stop = round(anchor_price - self.cfg.trigger_dist, 2)
        sl_buy    = round(buy_stop  - gap_sl_dist, 2)
        sl_sell   = round(sell_stop + gap_sl_dist, 2)
        tp_buy    = round(buy_stop  + gap_tp_dist, 2)
        tp_sell   = round(sell_stop - gap_tp_dist, 2)

        # FINAL SAFETY CHECK — after re-anchor, both stops should be on opposite
        # sides of current price. If not, something is still wrong → skip.
        if current_price is not None:
            buy_invalid  = buy_stop  < current_price
            sell_invalid = sell_stop > current_price
            if buy_invalid or sell_invalid:
                self.tele.error(
                    f"❌ *{label} skipped after re-anchor*\n"
                    f"Anchor ${anchor_price:.2f}, market ${current_price:.2f}\n"
                    f"BUY stop ${buy_stop} {'INVALID (below market)' if buy_invalid else 'ok'}\n"
                    f"SELL stop ${sell_stop} {'INVALID (above market)' if sell_invalid else 'ok'}\n"
                    f"Refusing to place orders that would be rejected."
                )
                return

        mode_tag = " [GAP MODE: half-lot, $10 SL]" if gap_mode else ""
        self.tele.info(
            f"⚓ *{label}* anchor=${anchor_price:.2f}{mode_tag}\n"
            f"  BUY  stop @ ${buy_stop}  (SL ${sl_buy}, TP ${tp_buy})\n"
            f"  SELL stop @ ${sell_stop} (SL ${sl_sell}, TP ${tp_sell})\n"
            f"  Lot: `{gap_lot}`"
        )

        # PRE-FLIGHT VALIDATION — don't send orders that will be rejected.
        # MT5 has no native OCO; these are two independent pending stops.
        # A BUY_STOP must be ABOVE current ask; a SELL_STOP must be BELOW current bid.
        # If our anchor + $5 trigger is on the wrong side of current market,
        # the broker will return INVALID_PRICE. Better to detect locally and skip.
        if current_price is not None:
            buy_invalid  = buy_stop  <= current_price
            sell_invalid = sell_stop >= current_price
            if buy_invalid or sell_invalid:
                self.tele.warn(
                    f"⚠️ *{label} skipped — pre-flight rejected*\n"
                    f"Anchor ${anchor_price:.2f}, market ${current_price:.2f}\n"
                    f"BUY  stop ${buy_stop}: "
                    f"{'❌ would be BELOW market (invalid)' if buy_invalid else '✅ ok'}\n"
                    f"SELL stop ${sell_stop}: "
                    f"{'❌ would be ABOVE market (invalid)' if sell_invalid else '✅ ok'}\n"
                    f"Not sending. {label} marked processed."
                )
                return

        buy_res = self.adapter.place_stop_order(
            self.cfg.symbol, 'BUY', buy_stop, gap_lot,
            sl=sl_buy, tp=tp_buy,
            comment=f"AUREONv2_{label}_BUY{'_GAP' if gap_mode else ''}",
            dry_run=self.paper)
        sell_res = self.adapter.place_stop_order(
            self.cfg.symbol, 'SELL', sell_stop, gap_lot,
            sl=sl_sell, tp=tp_sell,
            comment=f"AUREONv2_{label}_SELL{'_GAP' if gap_mode else ''}",
            dry_run=self.paper)

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
            # Mark a "hot polling" window — for the next 30 sec, _tick() runs
            # at 0.2s cadence instead of 1s so we catch fills fast.
            self._hot_poll_until = pd.Timestamp.now(tz='UTC') + pd.Timedelta(seconds=30)
            return

        # If we got here, pre-flight passed but the broker STILL rejected
        # one or both (slippage between check and send, or other broker issue).
        # Clean up: cancel anything that did place, log honestly, move on.
        def _rcname(res):
            rc = getattr(res, 'retcode', None) if res is not None else None
            return f"{rc} ({_MT5_RETCODE_MAP.get(rc, '?')})" if rc else "no_response"

        buy_rc  = getattr(buy_res,  'retcode', None) if buy_res  is not None else None
        sell_rc = getattr(sell_res, 'retcode', None) if sell_res is not None else None

        # Cancel any orphan FIRST before deciding recovery
        for orphan in (buy_ticket, sell_ticket):
            if orphan is not None and not str(orphan).startswith("paper_"):
                try:
                    self.adapter.cancel_order(orphan, dry_run=self.paper)
                    self.tele.info(f"Cancelled orphan ticket {orphan}")
                except Exception as e:
                    self.tele.error(f"Failed to cancel orphan {orphan}: {e}")

        # ----- CLEVER RECOVERY (narrow scope) -----
        # When pre-flight passed but broker rejected with INVALID_PRICE on one
        # side, it means price moved past our threshold WHILE the order was in
        # flight (sub-second timing). This is a real breakout we just missed
        # by milliseconds. Catchable if slip is small.
        #
        # Only activates when ALL of these are true:
        #   1. One side rejected with INVALID_PRICE (10015)
        #   2. The OTHER side either filled or also rejected (not a partial OK)
        #   3. Re-read market confirms direction (price IS past the threshold)
        #   4. Slip is in catchable zone: $0.50 to $15
        #
        # Outside that zone we skip cleanly. Gap mode at top of function handles
        # huge anchor staleness; this handles the in-flight millisecond gap.
        try:
            tick = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
            recovery_price = (tick.ask + tick.bid) / 2 if tick else None
        except Exception:
            recovery_price = None

        breakout_side = None
        slip = 0.0
        if recovery_price is not None:
            if buy_rc == 10015 and recovery_price >= buy_stop:
                breakout_side = 'BUY'
                slip = recovery_price - buy_stop
            elif sell_rc == 10015 and recovery_price <= sell_stop:
                breakout_side = 'SELL'
                slip = sell_stop - recovery_price

        # Catchable zone check
        if breakout_side is not None and 0.5 <= slip <= 15.0 and recovery_price is not None:
            # Half the gap_lot (already half if in gap mode), tight $10 SL,
            # normal $30 TP. Recovery trades tagged "_RCV" in MT5 comment.
            rcv_lot = round(max(gap_lot / 2 if gap_mode else gap_lot * 0.5, 0.01), 2)
            rcv_sl_dist = 10.0
            if breakout_side == 'BUY':
                rcv_sl = round(recovery_price - rcv_sl_dist, 2)
                rcv_tp = round(recovery_price + gap_tp_dist, 2)
            else:
                rcv_sl = round(recovery_price + rcv_sl_dist, 2)
                rcv_tp = round(recovery_price - gap_tp_dist, 2)

            self.tele.warn(
                f"🎯 *{label} IN-FLIGHT BREAKOUT — recovering {breakout_side}*\n"
                f"Threshold ${buy_stop if breakout_side=='BUY' else sell_stop} was "
                f"${slip:.2f} behind market ${recovery_price:.2f} (catchable zone).\n"
                f"Market {breakout_side} • Lot `{rcv_lot}` • SL `${rcv_sl}` ($10 tight) • TP `${rcv_tp}`"
            )
            mkt_res = self.adapter.place_market_order(
                self.cfg.symbol, breakout_side, rcv_lot,
                sl=rcv_sl, tp=rcv_tp,
                comment=f"AUREONv2_{label}_{breakout_side}_RCV",
                dry_run=self.paper)
            mkt_rc = getattr(mkt_res, 'retcode', None) if mkt_res is not None else None
            if mkt_rc == 10009:
                actual_ticket = getattr(mkt_res, 'order', None) or getattr(mkt_res, 'deal', None)
                fill_price = getattr(mkt_res, 'price', recovery_price)
                if actual_ticket:
                    self.shadow_positions[int(actual_ticket)] = {
                        'anchor_label': label, 'side': breakout_side,
                        'entry_price': float(fill_price),
                        'current_sl': rcv_sl,
                        'tp_level': rcv_tp,
                        'max_fav': float(fill_price),
                        'recovery': True,
                        'fill_time': pd.Timestamp.now(tz='UTC').isoformat(),  # v2.3
                    }
                self.tele.success(
                    f"✅ *{label} recovery {breakout_side} filled @ ${fill_price}*"
                )
            else:
                self.tele.error(
                    f"❌ *{label} recovery market order also rejected*\n"
                    f"retcode={mkt_rc} ({_MT5_RETCODE_MAP.get(mkt_rc, '?')})"
                )
            return

        # Out of catchable zone OR no breakout direction confirmed — skip cleanly
        # v2.3: distinguish "order placement failed" (rc=-1 etc) from "genuine no-breakout"
        both_no_response = (buy_rc in (None, -1)) and (sell_rc in (None, -1))
        if both_no_response:
            skip_reason = "ORDER PLACEMENT FAILED — broker returned no response on both sides"
        elif breakout_side is not None and slip > 15.0:
            skip_reason = f"slip ${slip:.2f} > $15 (move exhausted, would chase top/bottom)"
        elif breakout_side is not None and slip < 0.5:
            skip_reason = f"slip ${slip:.2f} < $0.50 (price didn't actually break, broker quirk)"
        else:
            skip_reason = "no breakout confirmed"
        self.tele.error(
            f"❌ *{label} skipped — {skip_reason}*\n"
            f"BUY  stop @ ${buy_stop}: rc={_rcname(buy_res)}\n"
            f"SELL stop @ ${sell_stop}: rc={_rcname(sell_res)}\n"
            f"Current market: ${recovery_price if recovery_price else '?'}"
        )

    @staticmethod
    def _extract_ticket(result, fallback: str):
        if result is None: return None
        if isinstance(result, dict) and result.get('paper'):
            return fallback
        # Real MT5 result — only consider it a real ticket if retcode == DONE (10009)
        retcode = getattr(result, 'retcode', None)
        if retcode != 10009:
            return None
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
                # v2.3: capture broker's actual fill timestamp for freeze logic
                # broker_p.time is Unix seconds (broker convention — use offset-aware decode)
                try:
                    fill_unix = int(broker_p.time)
                    if self.adapter.tick_time_offset_hours:
                        fill_unix -= self.adapter.tick_time_offset_hours * 3600
                    fill_time_utc = pd.Timestamp(fill_unix, unit='s', tz='UTC')
                except Exception:
                    fill_time_utc = pd.Timestamp.now(tz='UTC')
                self.shadow_positions[ticket] = {
                    'anchor_label': info['anchor_label'],
                    'side':         info['side'],
                    'entry_price':  float(broker_p.price_open),
                    'current_sl':   float(broker_p.sl),
                    'tp_level':     float(broker_p.tp),
                    'max_fav':      float(broker_p.price_open),
                    'fill_time':    fill_time_utc.isoformat(),  # v2.3: persisted, restart-safe
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
            # v2.3: pull stored fill_time so the freeze window is anchored to the
            # actual broker fill timestamp (restart-safe). Fallback to bar_time only
            # if state predates the patch (legacy positions opened before v2.3 deploy).
            fill_time_iso = shadow.get('fill_time')
            if fill_time_iso:
                try:
                    entry_time_for_pos = pd.Timestamp(fill_time_iso)
                    if entry_time_for_pos.tzinfo is None:
                        entry_time_for_pos = entry_time_for_pos.tz_localize('UTC')
                except Exception:
                    entry_time_for_pos = bar_time
            else:
                entry_time_for_pos = bar_time  # legacy fallback — freeze won't apply, normal trail

            pos = self._Position(
                anchor_label=shadow['anchor_label'],
                side=shadow['side'],
                entry_price=shadow['entry_price'],
                entry_time=entry_time_for_pos,
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
                tick_start = time.time()
                utc_now = pd.Timestamp.now(tz='UTC')
                try:
                    self._tick()
                except Exception as e:
                    self.tele.error(f"Tick failed: {e}")
                    log.exception("Tick exception")
                # Per-second price snapshot for forensic log
                try:
                    self._log_price(utc_now)
                except Exception as e:
                    log.debug(f"price log error: {e}")
                # Adaptive cadence: 0.2s during hot window (just after anchor
                # fired and pendings are fresh), 1.0s otherwise.
                in_hot = (self._hot_poll_until is not None
                          and utc_now < self._hot_poll_until)
                target_interval = 0.2 if in_hot else 1.0
                elapsed = time.time() - tick_start
                time.sleep(max(0.0, target_interval - elapsed))
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