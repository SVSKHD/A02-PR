"""
AUREON v2.5.3 — LiveTrader: production-ready live/paper trading loop.

This module implements the runtime that was stubbed in bot.py's run_live().
Imports cleanly into bot.py.

v2.5.3 changes (2026-05-27 evening, post-A4 failure analysis)
-------------------------------------------------------------
Root cause confirmed today: after long idle periods between anchors (the bot
does only read-only mt5 calls in its tick loop), the MT5 Python SDK's *write*
channel goes cold. order_send returns None instantly (rc=-1) while every read
operation keeps working. Verified by elimination — diag scripts placed 10/10
on the same VPS while the bot itself was hitting rc=-1 on every anchor.

Fixes in this version:
1. Trade channel WARMUP — before every anchor placement, send-and-cancel a
   tiny pending order at $100 from market. This wakes the trade channel.
2. AUTO-RECONNECT — if warmup ping fails, attempt mt5.shutdown() +
   mt5.initialize() recovery. If that also fails, skip cleanly.
3. mt5.last_error() CAPTURE in placement path. Each failed order_send leaves
   a forensic trail in the telegram error message.
4. PRESERVE gap_mode across retries — previously the retry re-evaluated gap
   against fresh current_price (after re-anchor → zero gap → normal mode →
   lot doubles and SL widens). Now gap state locks once resolved.
5. FP_ZERO_MAX_LOT class constant — set to 0.27 before FP Zero deployment.
6. MARKDOWN ESCAPE in startup banner — fixes the v2.5.2 Telegram 400 error
   on `auto_lot` underscore parsing.
7. ★ COMPREHENSIVE FAILURE DUMPS ★ — every failure path now emits a full
   diagnostic via _dump_mt5_state() capturing terminal_info, account_info,
   symbol_info, tick, and last_error in one structured block. If anything
   fails tomorrow, the log says WHY in one place. No more guessing.

v2.5.2 changes (2026-05-27, post-A2 failure analysis)
----------------------------------------------------
1. Per-anchor deferred wait — A2 (London) and A4 (NY) now wait 30s before
   placement (was 15s globally). A1 and A3 remain at 15s.
2. Retry-on-rc=-1 — if both pending stops return rc=-1 / no_response (the
   May 27 A2 failure mode), placement is re-scheduled via the existing
   deferred-anchor mechanism rather than being abandoned. Up to 2 retries
   with 15s, then 30s backoff. Position management on existing trades
   continues uninterrupted during the wait.

Max total recovery window per anchor:
   A1/A3:  15s defer + 15s retry-1 + 30s retry-2 = 60s
   A2/A4:  30s defer + 15s retry-1 + 30s retry-2 = 75s

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

    # v2.5.2: per-anchor deferred wait (seconds). Session opens (A2 London,
    # A4 NY) get more time for broker comm to stabilize past the volume spike.
    DEFER_WAIT_BY_ANCHOR = {
        'A1_02h_Asia': 15,
        'A2_10h_London': 30,
        'A3_1340_Overlap': 15,
        'A4_1640_NYopen': 30,
    }
    DEFER_WAIT_DEFAULT = 15

    # v2.5.2: retry on rc=-1 / no_response from broker
    MAX_PLACEMENT_RETRIES = 2          # initial + 2 retries = 3 attempts total
    RETRY_BACKOFF_BASE_SEC = 15        # delays: 15s, 30s

    # v2.5.3: Trade channel warmup constants. The send-and-cancel ping wakes
    # the MT5 SDK's write channel which goes cold after hours of read-only
    # tick activity. Ping is placed at $100 from market — far enough to NEVER
    # accidentally fill on a $30/sec move.
    WARMUP_LOT = 0.01
    WARMUP_DISTANCE = 100.0    # $100 from market
    WARMUP_MAGIC = 9999998     # distinct from main magic (20260522)
    WARMUP_COMMENT = "WARMUP"

    # v2.5.3: Hard lot cap for FP Zero 1% floating-loss compliance.
    # None = disabled (Pepperstone demo). 0.27 caps $50k FP Zero at <$500 SL.
    FP_ZERO_MAX_LOT = None     # set to 0.27 before FP Zero $50k buy

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

        # v2.5: PID lock — prevent multiple bot instances running simultaneously
        # against the same account. Multiple instances would share magic 20260522
        # and conflict on shadow_position tracking + OCO cancels.
        self.pid_lock_path = os.path.join(run_dir, "aureon.pid")
        self._acquire_pid_lock()

        # Persistent state
        self.state_path = cfg.state_file
        self.state = self._load_state()

        # In-memory shadow of broker state. Maps ticket -> dict.
        self.shadow_positions: Dict = {}
        self.shadow_pendings: Dict = {}

        # v2.5: rehydrate shadow_positions max_fav/fill_time from persisted state
        # so a mid-trade restart doesn't lose the $5 lock or freeze gate state.
        self._pending_shadow_rehydrate = self.state.get('shadow_positions_extended', {})

        # Bar-close tracking
        self._last_managed_minute: Optional[pd.Timestamp] = None
        self._tick_counter = 0
        # Hot polling window: for 30s after firing an anchor we tick at 0.2s
        # to catch fills fast. After that, back to normal 1.0s cadence.
        self._hot_poll_until: Optional[pd.Timestamp] = None
        # v2.5: deferred anchor placement (non-blocking 5s settle wait)
        # v2.5.2: now carries retry_count for rc=-1 recovery
        self._deferred_anchor: Optional[Dict] = None

        # Pause flag (set via /pause command)
        self.paused = False

        # Today's trade log header
        if not os.path.exists(self.daylog_path):
            with open(self.daylog_path, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["date", "anchor", "side", "entry", "exit",
                     "outcome", "pnl_usd", "ticket"])

        self.tele.info(
            f"LiveTrader v2.5.3 initialized ({'PAPER' if paper else 'LIVE'}) — "
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
        # v2.5: try main state, then .bak fallback, then fresh
        for path, label in [(self.state_path, "main"),
                            (self.state_path + ".bak", "backup")]:
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        s = json.load(f)
                    log.info(f"Restored state from {label}: {path}")
                    return s
                except Exception as e:
                    log.warning(f"State {label} corrupt ({e}); trying next source")
        log.warning("No usable state file; starting fresh")
        return {
            'daily_pnl': 0.0,
            'last_broker_date': None,
            'processed_anchors_today': [],
            'kill_switch_locked': False,
            'day_start_equity': None,  # v2.5.4: today's opening equity — kill baseline
            'shadow_positions_extended': {},  # v2.5: persisted max_fav/fill_time per ticket
        }

    def _save_state(self):
        if self.paper:
            return
        # v2.5: atomic write + rolling .bak backup
        tmp = self.state_path + '.tmp'
        bak = self.state_path + '.bak'
        # Mirror in-memory shadow lock state into the dict before writing
        try:
            self.state['shadow_positions_extended'] = {
                str(ticket): {
                    'max_fav':   shadow.get('max_fav'),
                    'fill_time': shadow.get('fill_time'),
                    'current_sl': shadow.get('current_sl'),
                    'side':       shadow.get('side'),
                    'entry_price': shadow.get('entry_price'),
                    'anchor_label': shadow.get('anchor_label'),
                }
                for ticket, shadow in self.shadow_positions.items()
            }
        except Exception as e:
            log.warning(f"Could not snapshot shadow_positions to state: {e}")
        # Copy current main → .bak before overwriting
        if os.path.exists(self.state_path):
            try:
                import shutil
                shutil.copyfile(self.state_path, bak)
            except Exception:
                pass  # backup failure is not fatal
        with open(tmp, 'w') as f:
            json.dump(self.state, f, indent=2, default=str)
        os.replace(tmp, self.state_path)

    def _acquire_pid_lock(self):
        """v2.5: Refuse to start if another bot instance is already running."""
        import psutil
        if os.path.exists(self.pid_lock_path):
            try:
                with open(self.pid_lock_path) as f:
                    other_pid = int(f.read().strip())
                if psutil.pid_exists(other_pid):
                    # Verify it's actually a python process running this bot
                    try:
                        p = psutil.Process(other_pid)
                        cmdline = " ".join(p.cmdline()).lower()
                        if "aureon" in cmdline or "live_trader" in cmdline or "bot.py" in cmdline:
                            raise RuntimeError(
                                f"Another AUREON bot is already running (PID {other_pid}). "
                                f"Refusing to start a second instance — they would conflict on "
                                f"magic number 20260522 and OCO sibling tracking. "
                                f"Kill the other instance first: taskkill /F /PID {other_pid}"
                            )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass  # stale lock, safe to take it
                # else: stale lock, fall through and take it
            except (ValueError, OSError):
                pass  # malformed lock, take it
        with open(self.pid_lock_path, 'w') as f:
            f.write(str(os.getpid()))
        log.info(f"PID lock acquired: {self.pid_lock_path} = {os.getpid()}")

    def _release_pid_lock(self):
        try:
            if os.path.exists(self.pid_lock_path):
                with open(self.pid_lock_path) as f:
                    locked_pid = int(f.read().strip())
                if locked_pid == os.getpid():
                    os.remove(self.pid_lock_path)
                    log.info("PID lock released")
        except Exception as e:
            log.warning(f"Could not release PID lock: {e}")

    def _broker_date(self, utc_now: pd.Timestamp) -> DateType:
        return (utc_now + pd.Timedelta(hours=self.cfg.broker_tz_offset_hours)).date()

    # ------------------------------------------------------------------------
    # Auto-sizing from live account balance
    # ------------------------------------------------------------------------

    def _compute_safe_lot(self, balance: float) -> float:
        """
        Return the largest safe lot under Funding Pips per-trade risk rules.
        3% on accounts <$50k, 2% on ≥$50k. Apply slippage buffer + conservatism.
        Rounds DOWN to broker's actual volume_step precision (v2.5: validated).
        """
        risk_pct = (self.cfg.risk_pct_over_50k if balance >= 50_000
                    else self.cfg.risk_pct_under_50k)
        max_loss = balance * risk_pct * self.cfg.slippage_buffer
        # SL distance × oz per lot (contract_size assumed 100 for XAUUSD)
        max_lot = max_loss / (self.cfg.sl_dist * 100)
        # Apply user conservatism multiplier
        effective_lot = max_lot * self.cfg.lot_conservatism

        # v2.5: validate against broker's actual volume_step/min/max
        try:
            si = self.adapter.mt5.symbol_info(self.cfg.symbol)
            if si is not None:
                step = si.volume_step if si.volume_step > 0 else 0.01
                vmin = si.volume_min if si.volume_min > 0 else 0.01
                vmax = si.volume_max if si.volume_max > 0 else 100.0
                # Floor to broker step
                steps = int(effective_lot / step)
                effective_lot = max(vmin, min(vmax, steps * step))
                # Round to step decimal precision for cleanness
                effective_lot = round(effective_lot, 2)
            else:
                effective_lot = max(0.01, int(effective_lot * 100) / 100)
        except Exception as e:
            log.warning(f"Lot validation against broker volume_step failed: {e}")
            effective_lot = max(0.01, int(effective_lot * 100) / 100)

        # v2.5.3: Hard lot cap for FP Zero 1% floating-loss compliance.
        # No-op when FP_ZERO_MAX_LOT is None (Pepperstone demo testing).
        if self.FP_ZERO_MAX_LOT is not None and effective_lot > self.FP_ZERO_MAX_LOT:
            log.info(
                f"Lot capped {effective_lot} → {self.FP_ZERO_MAX_LOT} "
                f"(FP_ZERO_MAX_LOT compliance)"
            )
            effective_lot = self.FP_ZERO_MAX_LOT
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
            # v2.5.4: re-baseline the daily kill switch to TODAY's opening equity.
            # Prevents prior-day losses (and the start-of-day gap from a fixed
            # starting_balance) from bleeding into today's daily-loss budget.
            # This also matches how Funding Pips measures the daily-loss rule
            # (from start-of-day equity, not the initial deposit).
            day_start = self._live_equity()
            self.state['day_start_equity'] = day_start if day_start is not None \
                else self.cfg.starting_balance
            log.info(
                f"Daily kill baseline re-set to opening equity "
                f"${self.state['day_start_equity']:,.2f} for {broker_date}"
            )
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
            "kill_threshold_usd": self.cfg.daily_loss_pct * (
                self.state.get('day_start_equity') or self.cfg.starting_balance),
            # v2.5.2: surface retry state in status for watchdog/dashboard visibility
            "deferred_anchor": (
                {
                    'label': self._deferred_anchor['label'],
                    'retry_count': self._deferred_anchor.get('retry_count', 0),
                    'defer_until': str(self._deferred_anchor['defer_until']),
                } if self._deferred_anchor else None
            ),
        }
        tmp = self.status_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(status, f, indent=2, default=str)
            os.replace(tmp, self.status_path)
        except (PermissionError, OSError) as e:
            log.debug(f"status.json write skipped (locked): {e}")

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
        Daily kill switch — fires if losses exceed cfg.daily_loss_pct of the
        DAY'S OPENING equity (v2.5.4). Previously measured from cfg.starting_balance,
        a fixed baseline, which double-counted prior-day losses and the start-of-day
        gap — that's what gated A2/A3/A4 off after A1's loss on 2026-05-28.
        In LIVE mode: uses live equity (includes unrealized P&L), which matches how
                      Funding Pips actually measures the daily loss rule.
        In PAPER mode: falls back to internal daily_pnl (realized only).
        """
        self._ensure_day_start_equity()
        base = self.state.get('day_start_equity') or self.cfg.starting_balance
        threshold = self.cfg.daily_loss_pct * base
        equity = self._live_equity()
        if equity is not None:
            live_daily_loss = base - equity
            return live_daily_loss >= threshold
        # Fallback (paper or MT5 query failure)
        return self.state['daily_pnl'] <= -threshold

    def _ensure_day_start_equity(self):
        """v2.5.4: lazily backfill today's opening equity if missing (e.g. the bot
        is restarted mid-day before any new-broker-day reset has run). Reconstructs
        opening equity = current balance - realized daily P&L, so the kill baseline
        is correct immediately on restart instead of falling back to starting_balance."""
        if self.state.get('day_start_equity') is not None:
            return
        base = self.cfg.starting_balance
        try:
            info = self.adapter.get_account_info() if self.adapter else None
            if info and info.get('balance') is not None:
                base = info['balance'] - self.state.get('daily_pnl', 0.0)
        except Exception as e:
            log.warning(f"_ensure_day_start_equity: account read failed ({e}); "
                        f"using starting_balance ${base:,.2f}")
        self.state['day_start_equity'] = base
        self._save_state()
        log.info(f"Daily kill baseline backfilled to opening equity ${base:,.2f}")

    def _process_anchor_if_due(self, broker_date: DateType, utc_now: pd.Timestamp):
        if self.paused:
            return
        for label, hour, minute in self.cfg.anchors:
            if label in self.state['processed_anchors_today']:
                continue
            anchor_utc = self._anchor_datetime_utc(
                broker_date, hour, self.cfg.broker_tz_offset_hours, minute)
            delta = (utc_now - anchor_utc).total_seconds()
            # Window: 0 to 120 seconds after the anchor minute
            if 0 <= delta < 120:
                self._process_anchor(label, anchor_utc)
                self.state['processed_anchors_today'].append(label)
                self._save_state()

    def _process_anchor(self, label: str, anchor_utc: pd.Timestamp):
        # v2.5: account floor check — halt new entries if balance dropped too far
        try:
            ainfo = self.adapter.mt5.account_info()
            if ainfo is not None:
                floor = self.cfg.starting_balance * self.cfg.account_floor_pct
                if ainfo.balance < floor:
                    self.tele.warn(
                        f"⛔ *{label} BLOCKED — account floor breached*\n"
                        f"Balance: `${ainfo.balance:,.2f}`\n"
                        f"Floor:   `${floor:,.2f}` ({self.cfg.account_floor_pct*100:.0f}% of starting)\n"
                        f"No new entries until balance recovers."
                    )
                    return
        except Exception as e:
            log.warning(f"Account floor check failed: {e}")

        anchor_price = self.adapter.get_m5_close(self.cfg.symbol, anchor_utc)
        if anchor_price is None:
            self.tele.warn(f"⚠️ Could not fetch M5 close at {anchor_utc} — skipping {label}")
            return

        # v2.5.2: Per-anchor deferred wait. A2 (London open) and A4 (NY open) need
        # longer than calm sessions for broker comm to stabilize past the volume spike.
        # 2026-05-27 incident: A2 hit rc=-1 with 15s wait on both Pepperstone and
        # MetaQuotes. Bumping A2/A4 to 30s + retry mechanism in _place_orders_for_anchor
        # gives up to 75s total recovery window per anchor.
        defer_seconds = self.DEFER_WAIT_BY_ANCHOR.get(label, self.DEFER_WAIT_DEFAULT)
        defer_until = pd.Timestamp.now(tz='UTC') + pd.Timedelta(seconds=defer_seconds)
        self._deferred_anchor = {
            'label': label,
            'anchor_utc': anchor_utc,
            'anchor_price': anchor_price,
            'defer_until': defer_until,
            'retry_count': 0,                # v2.5.2: retry counter for rc=-1 recovery
            # v2.5.3: gap-mode state preserved across retries (None on first attempt)
            'gap_mode_locked':  False,
            'gap_lot_override': None,
            'gap_sl_override':  None,
            'gap_re_anchor':    None,
        }
        log.info(
            f"{label}: anchor captured @ ${anchor_price:.2f}, deferring placement to "
            f"{defer_until.strftime('%H:%M:%S')} UTC ({defer_seconds}s settle wait — non-blocking)"
        )

    def _complete_deferred_anchor(self):
        """v2.5: Called from the tick loop. Completes a deferred anchor placement
        after the settle window. Non-blocking — doesn't stop position management.

        v2.5.2: Plumbs retry_count through to placement so rc=-1 retries
        re-enter via this same path without losing retry state."""
        if self._deferred_anchor is None:
            return
        if pd.Timestamp.now(tz='UTC') < self._deferred_anchor['defer_until']:
            return  # still waiting

        d = self._deferred_anchor
        self._deferred_anchor = None  # consume

        label = d['label']
        anchor_price = d['anchor_price']
        anchor_utc = d['anchor_utc']
        retry_count = d.get('retry_count', 0)   # v2.5.2: pull retry counter
        # v2.5.3: pull preserved gap-mode context
        gap_mode_locked  = d.get('gap_mode_locked',  False)
        gap_lot_override = d.get('gap_lot_override', None)
        gap_sl_override  = d.get('gap_sl_override',  None)
        gap_re_anchor    = d.get('gap_re_anchor',    None)
        if gap_mode_locked and gap_re_anchor is not None:
            anchor_price = gap_re_anchor

        # v2.5: tick freshness check — refuse to use stale market data
        current_price = None
        try:
            tick = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
            if tick is not None:
                # tick.time is broker-time as unix; subtract broker offset to get UTC unix
                broker_offset = self.adapter.tick_time_offset_hours * 3600
                tick_utc_unix = tick.time - broker_offset
                now_unix = pd.Timestamp.now(tz='UTC').timestamp()
                tick_age_s = abs(now_unix - tick_utc_unix)
                if tick_age_s > 60:
                    self._dump_mt5_state(label, f"SKIP: tick stale ({tick_age_s:.0f}s old)")
                    self.tele.warn(
                        f"⚠️ *{label} skipped — stale tick*\n"
                        f"Tick age: {tick_age_s:.0f}s (> 60s threshold)\n"
                        f"MT5 terminal may have lost connection. Skipping placement."
                    )
                    return
                current_price = (tick.ask + tick.bid) / 2
                # v2.5.4: ANCHOR ON CURRENT PRICE at the moment of placement.
                # Replaces the M5-close anchor captured ~30s earlier in
                # _process_anchor. Because placement uses anchor_price, both stops
                # are now symmetric around live price -> gap mode self-disables
                # (anchor-vs-current diff = 0). On rc=-1 retries this re-runs and
                # re-fetches the tick, so retries re-anchor to fresh price too —
                # fixing the 2026-05-27 A4 flaw (RETRY reused a dead anchor).
                anchor_price = current_price
        except Exception as e:
            log.warning(f"Could not read fresh tick for {label}: {e}")
            self._dump_mt5_state(label, f"SKIP: tick read raised {e}")
            self.tele.warn(f"⚠️ {label}: tick read failed — skipping")
            return

        # v2.5.4: HARD GUARANTEE of current-price anchoring. If the tick came back
        # None (no exception, just unavailable), current_price is still None and
        # anchor_price would otherwise be the stale M5 close. Refuse to place on it —
        # skip cleanly instead of silently repeating the stale-anchor blunder.
        if current_price is None:
            self._dump_mt5_state(label, "SKIP: no live tick — refusing stale M5 anchor")
            self.tele.warn(
                f"⚠️ *{label} skipped — no live tick for current-price anchor*\n"
                f"symbol_info_tick returned None. Refusing to place on the stale "
                f"M5 anchor. Anchor lost this cycle (no blunder)."
            )
            log.warning(f"{label}: SKIP — current_price None, refusing stale anchor placement")
            return

        # v2.5.3: WARM UP THE TRADE CHANNEL before real placement. If warmup
        # fails AND reconnect also fails, skip cleanly with diagnostic dump.
        if not self._warmup_trade_channel(label):
            self._dump_mt5_state(label, "SKIP: warmup + reconnect both failed")
            self.tele.error(
                f"❌ *{label} skipped — trade channel could not be revived*\n"
                f"Warmup ping returned None and mt5.shutdown()/initialize() also failed.\n"
                f"This anchor is lost. See log for full mt5 state dump."
            )
            return

        # v2.5.2/v2.5.3: pass retry_count and gap state through
        self._place_orders_for_anchor(
            label, anchor_utc, anchor_price, current_price, retry_count,
            gap_mode_locked=gap_mode_locked,
            gap_lot_override=gap_lot_override,
            gap_sl_override=gap_sl_override,
        )

    def _place_orders_for_anchor(self, label, anchor_utc, anchor_price, current_price,
                                  retry_count=0,
                                  gap_mode_locked=False,
                                  gap_lot_override=None,
                                  gap_sl_override=None):
        # All the original gap detection + pre-flight + placement logic.
        # v2.5.2: retry_count parameter added — used in the rc=-1 recovery block below.
        # v2.5.3: gap_mode_locked + overrides — if a previous attempt resolved
        #         gap mode, retries inherit verbatim instead of re-evaluating
        #         (re-eval would fall to normal mode → 2× lot + wider SL).

        # v2.5.3: if gap mode was locked in a prior attempt, honor it
        if gap_mode_locked:
            gap_mode    = True
            gap_lot     = gap_lot_override or round(self.cfg.lot_size / 2, 2)
            gap_sl_dist = gap_sl_override  or 10.0
            gap_tp_dist = self.cfg.tp_dist
            log.info(
                f"{label}: gap mode preserved across retry — "
                f"lot={gap_lot}, SL=${gap_sl_dist}, anchor=${anchor_price:.2f}"
            )
            self.tele.info(
                f"♻️ *{label} retry inheriting gap mode* — "
                f"lot `{gap_lot}` SL `${gap_sl_dist}` (locked from initial)"
            )
        else:
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
                if gap > self.cfg.trigger_dist + 0.1:  # v2.3: was 0.5, now 0.1 — catches edge cases where market crept 10¢+ past trigger
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
                    retry_tag = f" (retry {retry_count})" if retry_count > 0 else ""    # v2.5.2
                    self.tele.warn(
                        f"⚠️ *{label} GAP DETECTED{retry_tag}*\n"
                        f"Original anchor: `${anchor_price:.2f}`\n"
                        f"Current market:  `${current_price:.2f}`\n"
                        f"Gap: `${gap:.2f}` (> ${self.cfg.trigger_dist + 0.1:.2f} threshold)\n"
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
        # sides of current price. If only ONE is invalid, place the valid side
        # alone (v2.3 fix — was skipping both, leaving valid trades on the table).
        skip_buy = False
        skip_sell = False
        if current_price is not None:
            buy_invalid  = buy_stop  < current_price
            sell_invalid = sell_stop > current_price
            if buy_invalid and sell_invalid:
                self.tele.error(
                    f"❌ *{label} skipped — BOTH sides invalid after re-anchor*\n"
                    f"Anchor ${anchor_price:.2f}, market ${current_price:.2f}\n"
                    f"BUY ${buy_stop} below market, SELL ${sell_stop} above market.\n"
                    f"Refusing to place orders that would be rejected."
                )
                return
            elif buy_invalid:
                skip_buy = True
                self.tele.warn(
                    f"⚠️ *{label} — BUY invalid, placing SELL alone*\n"
                    f"BUY ${buy_stop} would be below market ${current_price:.2f} (skip).\n"
                    f"SELL ${sell_stop} valid — proceeding with one-sided entry."
                )
            elif sell_invalid:
                skip_sell = True
                self.tele.warn(
                    f"⚠️ *{label} — SELL invalid, placing BUY alone*\n"
                    f"SELL ${sell_stop} would be above market ${current_price:.2f} (skip).\n"
                    f"BUY ${buy_stop} valid — proceeding with one-sided entry."
                )

        mode_tag = " [GAP MODE: half-lot, $10 SL]" if gap_mode else ""
        retry_tag = f" [RETRY {retry_count}]" if retry_count > 0 else ""     # v2.5.2
        self.tele.info(
            f"⚓ *{label}*{retry_tag} anchor=${anchor_price:.2f}{mode_tag}\n"
            f"  BUY  stop @ ${buy_stop}  (SL ${sl_buy}, TP ${tp_buy})\n"
            f"  SELL stop @ ${sell_stop} (SL ${sl_sell}, TP ${tp_sell})\n"
            f"  Lot: `{gap_lot}`"
        )

        # PRE-FLIGHT VALIDATION — don't send orders that will be rejected.
        # v2.3: if only ONE side is invalid, place the valid side alone.
        if current_price is not None:
            buy_invalid  = buy_stop  <= current_price
            sell_invalid = sell_stop >= current_price
            if buy_invalid and sell_invalid:
                self.tele.warn(
                    f"⚠️ *{label} skipped — BOTH sides invalid in pre-flight*\n"
                    f"Anchor ${anchor_price:.2f}, market ${current_price:.2f}\n"
                    f"BUY ${buy_stop} ≤ market, SELL ${sell_stop} ≥ market. Not sending."
                )
                return
            elif buy_invalid and not skip_buy:
                skip_buy = True
                self.tele.warn(
                    f"⚠️ *{label} pre-flight — placing SELL alone*\n"
                    f"BUY ${buy_stop} ≤ market ${current_price:.2f}; SELL ${sell_stop} valid."
                )
            elif sell_invalid and not skip_sell:
                skip_sell = True
                self.tele.warn(
                    f"⚠️ *{label} pre-flight — placing BUY alone*\n"
                    f"SELL ${sell_stop} ≥ market ${current_price:.2f}; BUY ${buy_stop} valid."
                )

        # v2.3: only place the sides that passed pre-flight
        # v2.5.2: append retry tag to comment for MT5 audit trail
        # v2.5.3: capture mt5.last_error() IMMEDIATELY after each call so we
        #         have forensic data on every rc=-1 (adapter swallows it
        #         internally during its built-in rc=-1 reconcile retry)
        retry_comment = f"_R{retry_count}" if retry_count > 0 else ""
        buy_res = None
        sell_res = None
        buy_err = None
        sell_err = None
        if not skip_buy:
            buy_res = self.adapter.place_stop_order(
                self.cfg.symbol, 'BUY', buy_stop, gap_lot,
                sl=sl_buy, tp=tp_buy,
                comment=f"AUREONv2_{label}_BUY{'_GAP' if gap_mode else ''}{retry_comment}",
                dry_run=self.paper)
            if not self.paper:
                try:
                    buy_err = self.adapter.mt5.last_error()
                except Exception:
                    buy_err = ('?', 'last_error read failed')
        if not skip_sell:
            sell_res = self.adapter.place_stop_order(
                self.cfg.symbol, 'SELL', sell_stop, gap_lot,
                sl=sl_sell, tp=tp_sell,
                comment=f"AUREONv2_{label}_SELL{'_GAP' if gap_mode else ''}{retry_comment}",
                dry_run=self.paper)
            if not self.paper:
                try:
                    sell_err = self.adapter.mt5.last_error()
                except Exception:
                    sell_err = ('?', 'last_error read failed')

        # v2.5.3: surface mt5.last_error() in logs immediately when placement
        # returns None (otherwise this info is lost forever)
        if buy_res is None and not skip_buy:
            log.error(
                f"{label} BUY order_send returned None. mt5.last_error={buy_err}. "
                f"Price=${buy_stop} SL=${sl_buy} TP=${tp_buy} lot={gap_lot} "
                f"gap_mode={gap_mode}"
            )
        if sell_res is None and not skip_sell:
            log.error(
                f"{label} SELL order_send returned None. mt5.last_error={sell_err}. "
                f"Price=${sell_stop} SL=${sl_sell} TP=${tp_sell} lot={gap_lot} "
                f"gap_mode={gap_mode}"
            )

        buy_ticket  = self._extract_ticket(buy_res,  f"paper_{label}_BUY")  if buy_res  is not None else None
        sell_ticket = self._extract_ticket(sell_res, f"paper_{label}_SELL") if sell_res is not None else None

        # v2.3: success path includes single-side placement
        buy_ok  = (buy_ticket  is not None) if not skip_buy  else True   # treat skipped-by-design as "no problem"
        sell_ok = (sell_ticket is not None) if not skip_sell else True

        if buy_ok and sell_ok:
            if buy_ticket is not None:
                self.shadow_pendings[buy_ticket] = {
                    'anchor_label': label, 'side': 'BUY',
                    'sibling_ticket': sell_ticket,  # None when SELL was skipped — fill handler tolerates None
                    'entry_price': buy_stop,
                }
            if sell_ticket is not None:
                self.shadow_pendings[sell_ticket] = {
                    'anchor_label': label, 'side': 'SELL',
                    'sibling_ticket': buy_ticket,  # None when BUY was skipped
                    'entry_price': sell_stop,
                }
            # Hot polling window
            self._hot_poll_until = pd.Timestamp.now(tz='UTC') + pd.Timedelta(seconds=30)
            # v2.5.2: surface retry success
            if retry_count > 0:
                self.tele.success(f"✅ *{label} placement succeeded on retry {retry_count}*")
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

        # ----- IN-FLIGHT BREAKOUT RECOVERY (rc=10015 INVALID_PRICE only) -----
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

        # ----- v2.5.2: rc=-1 / no_response RETRY -----
        # If broker simply didn't respond (most likely VPS↔broker network spike
        # at session open), re-schedule placement via the deferred-anchor
        # mechanism instead of giving up. Tick loop continues managing existing
        # positions during the wait. Backoff: 15s, 30s. Max 2 retries.
        # v2.5.3: PRESERVE gap-mode state so retries don't fall back to normal
        #         mode (and double the lot + widen the SL).
        both_no_response_now = (buy_rc in (None, -1)) and (sell_rc in (None, -1))
        if both_no_response_now and retry_count < self.MAX_PLACEMENT_RETRIES:
            # v2.5.3: dump full mt5 state on rc=-1 — this is the diagnostic
            # gold the user wants. If anything fails tomorrow, this log line
            # tells us exactly why.
            self._dump_mt5_state(
                label,
                f"rc=-1 RETRY scheduled (attempt {retry_count + 1}/{self.MAX_PLACEMENT_RETRIES})"
            )
            retry_delay = self.RETRY_BACKOFF_BASE_SEC * (1 + retry_count)  # 15s, then 30s
            next_defer = pd.Timestamp.now(tz='UTC') + pd.Timedelta(seconds=retry_delay)
            self._deferred_anchor = {
                'label': label,
                'anchor_utc': anchor_utc,
                'anchor_price': anchor_price,    # use the *current* anchor
                                                  # (already re-anchored if gap mode)
                'defer_until': next_defer,
                'retry_count': retry_count + 1,
                # v2.5.3: lock gap state across retries
                'gap_mode_locked':  gap_mode,
                'gap_lot_override': gap_lot     if gap_mode else None,
                'gap_sl_override':  gap_sl_dist if gap_mode else None,
                'gap_re_anchor':    anchor_price if gap_mode else None,
            }
            err_detail = ""
            if not self.paper and (buy_err or sell_err):
                err_detail = (f"\nBUY  mt5.last\\_error: `{buy_err}`"
                              f"\nSELL mt5.last\\_error: `{sell_err}`")
            self.tele.warn(
                f"🔁 *{label} retry {retry_count + 1}/{self.MAX_PLACEMENT_RETRIES} scheduled*\n"
                f"Both sides returned rc=-1 (broker/network comm failure).\n"
                f"Re-attempting in `{retry_delay}s` at `{next_defer.strftime('%H:%M:%S')}` UTC.\n"
                f"Position management on existing trades continues uninterrupted."
                + err_detail
            )
            return  # tick loop will pick this up via _complete_deferred_anchor

        # Out of catchable zone OR no breakout direction confirmed — skip cleanly
        # v2.3: distinguish "order placement failed" (rc=-1 etc) from "genuine no-breakout"
        # v2.5.2: append retry-exhausted suffix to skip message
        # v2.5.3: dump full mt5 state on final skip so we have FULL forensics
        both_no_response = (buy_rc in (None, -1)) and (sell_rc in (None, -1))
        if both_no_response:
            retry_suffix = f" — gave up after {retry_count} retries" if retry_count > 0 else ""
            skip_reason = f"ORDER PLACEMENT FAILED — broker returned no response on both sides{retry_suffix}"
            # v2.5.3: full diagnostic dump on final failure
            self._dump_mt5_state(label, f"FINAL SKIP: {skip_reason}")
        elif breakout_side is not None and slip > 15.0:
            skip_reason = f"slip ${slip:.2f} > $15 (move exhausted, would chase top/bottom)"
        elif breakout_side is not None and slip < 0.5:
            skip_reason = f"slip ${slip:.2f} < $0.50 (price didn't actually break, broker quirk)"
        else:
            skip_reason = "no breakout confirmed"
        err_detail_skip = ""
        if not self.paper and (buy_err or sell_err):
            err_detail_skip = (f"\nBUY  mt5.last\\_error: `{buy_err}`"
                               f"\nSELL mt5.last\\_error: `{sell_err}`")
        self.tele.error(
            f"❌ *{label} skipped — {skip_reason}*\n"
            f"BUY  stop @ ${buy_stop}: rc={_rcname(buy_res)}\n"
            f"SELL stop @ ${sell_stop}: rc={_rcname(sell_res)}\n"
            f"Current market: ${recovery_price if recovery_price else '?'}"
            + err_detail_skip
        )

    # ------------------------------------------------------------------------
    # v2.5.3: Trade channel warmup, MT5 reconnect, and DIAGNOSTIC DUMPS
    # ------------------------------------------------------------------------

    def _dump_mt5_state(self, label: str, context: str) -> None:
        """v2.5.3: CLEAR FAILURE LOGGING. Captures the full MT5 state at the
        moment of any failure into a single multi-line log entry. If anything
        fails tomorrow, ONE log block has the complete story.

        Always logs at ERROR level (visible in default log filter). Also
        sends a compact telegram so failures are visible on the phone.

        Captures: terminal_info, account_info, symbol_info trade params,
        latest tick, and mt5.last_error(). Designed to never raise.
        """
        try:
            if self.paper:
                log.error(f"[{label}] {context} — PAPER mode, no MT5 state")
                return

            mt5 = self.adapter.mt5
            lines = [
                f"╔══ MT5 DIAGNOSTIC DUMP — {label} ══",
                f"║ Context : {context}",
                f"║ UTC time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            ]

            # 1. Terminal state
            try:
                ti = mt5.terminal_info()
                if ti:
                    lines.append(
                        f"║ Terminal: connected={ti.connected}  "
                        f"trade_allowed={ti.trade_allowed}  "
                        f"dlls_allowed={ti.dlls_allowed}  "
                        f"build={ti.build}  ping={ti.ping_last/1000:.0f}ms"
                    )
                else:
                    lines.append("║ Terminal: terminal_info() returned None ⚠")
            except Exception as e:
                lines.append(f"║ Terminal: read raised {type(e).__name__}: {e}")

            # 2. Account state — balance, equity, margin
            try:
                ai = mt5.account_info()
                if ai:
                    lines.append(
                        f"║ Account : #{ai.login} on `{ai.server}`  "
                        f"balance=${ai.balance:.2f}  equity=${ai.equity:.2f}  "
                        f"margin=${ai.margin:.2f}  free=${ai.margin_free:.2f}  "
                        f"trade_mode={ai.trade_mode}"
                    )
                else:
                    lines.append("║ Account : account_info() returned None ⚠")
            except Exception as e:
                lines.append(f"║ Account : read raised {type(e).__name__}: {e}")

            # 3. Symbol trading state — stops/freeze/filling/etc
            try:
                si = mt5.symbol_info(self.cfg.symbol)
                if si:
                    lines.append(
                        f"║ Symbol  : {self.cfg.symbol}  "
                        f"trade_mode={si.trade_mode} "
                        f"(0=disabled,1=long_only,2=short_only,3=close_only,4=full)"
                    )
                    lines.append(
                        f"║         : stops_level={si.trade_stops_level}pts "
                        f"= ${si.trade_stops_level * si.point:.2f} | "
                        f"freeze_level={si.trade_freeze_level}pts "
                        f"= ${si.trade_freeze_level * si.point:.2f}"
                    )
                    lines.append(
                        f"║         : volume_step={si.volume_step}  "
                        f"vol_min={si.volume_min}  vol_max={si.volume_max}  "
                        f"filling_mode={si.filling_mode} "
                        f"(1=FOK,2=IOC,3=both,4=RETURN)"
                    )
                else:
                    lines.append(f"║ Symbol  : symbol_info({self.cfg.symbol}) returned None ⚠")
            except Exception as e:
                lines.append(f"║ Symbol  : read raised {type(e).__name__}: {e}")

            # 4. Latest tick — how old? mid-price?
            try:
                tk = mt5.symbol_info_tick(self.cfg.symbol)
                if tk:
                    broker_offset = self.adapter.tick_time_offset_hours * 3600
                    tick_utc_unix = tk.time - broker_offset
                    now_unix = pd.Timestamp.now(tz='UTC').timestamp()
                    age = abs(now_unix - tick_utc_unix)
                    lines.append(
                        f"║ Tick    : bid=${tk.bid:.2f}  ask=${tk.ask:.2f}  "
                        f"spread=${(tk.ask-tk.bid):.2f}  age={age:.1f}s  "
                        f"volume={tk.volume}"
                    )
                else:
                    lines.append("║ Tick    : symbol_info_tick() returned None ⚠")
            except Exception as e:
                lines.append(f"║ Tick    : read raised {type(e).__name__}: {e}")

            # 5. THE BIG ONE — last_error
            try:
                err = mt5.last_error()
                lines.append(f"║ last_err: {err}  ← THE ROOT CAUSE")
            except Exception as e:
                lines.append(f"║ last_err: read raised {type(e).__name__}: {e}")

            # 6. Bot state context
            try:
                positions_now = len(mt5.positions_get(symbol=self.cfg.symbol) or [])
                pendings_now  = len(mt5.orders_get(symbol=self.cfg.symbol) or [])
                lines.append(
                    f"║ Bot     : daily_pnl=${self.state.get('daily_pnl', 0):+.2f}  "
                    f"positions={positions_now}  pendings={pendings_now}  "
                    f"shadow_pos={len(self.shadow_positions)}  "
                    f"shadow_pend={len(self.shadow_pendings)}"
                )
            except Exception as e:
                lines.append(f"║ Bot     : state read raised {e}")

            lines.append("╚════════════════════════════════════════")
            dump = "\n".join(lines)
            log.error(dump)
        except Exception as outer:
            # Diagnostic dump must NEVER raise — fall back to bare log
            log.error(f"[{label}] {context} — dump raised: {outer}")

    def _warmup_trade_channel(self, label: str) -> bool:
        """v2.5.3: Send a tiny throwaway pending ($100 from market) to wake
        the MT5 trade channel before real placement.

        The tick loop hammers READ calls every second, but doesn't WRITE
        between anchors. Hours of read-only activity → SDK's write path goes
        cold → order_send returns None instantly (rc=-1). Confirmed root
        cause for A2/A3/A4 failures on 2026-05-27.

        Returns True if channel is healthy (or paper mode). False if both
        the warmup ping AND mt5 reconnect failed.
        """
        if self.paper:
            return True

        try:
            tick = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
            if tick is None:
                log.warning(f"{label}: warmup — tick read returned None")
                self._dump_mt5_state(label, "WARMUP: tick read returned None")
                return self._attempt_mt5_reconnect(label)

            ping_price = round(tick.ask + self.WARMUP_DISTANCE, 2)
            ping_req = {
                "action":       self.adapter.mt5.TRADE_ACTION_PENDING,
                "symbol":       self.cfg.symbol,
                "volume":       self.WARMUP_LOT,
                "type":         self.adapter.mt5.ORDER_TYPE_BUY_STOP,
                "price":        ping_price,
                "sl":           round(ping_price - 20.0, 2),
                "tp":           round(ping_price + 50.0, 2),
                "deviation":    20,
                "magic":        self.WARMUP_MAGIC,
                "comment":      self.WARMUP_COMMENT,
                "type_filling": self.adapter.mt5.ORDER_FILLING_IOC,
                "type_time":    self.adapter.mt5.ORDER_TIME_DAY,  # matches bot's convention
            }
            ping_res = self.adapter.mt5.order_send(ping_req)
            ping_err = self.adapter.mt5.last_error()

            if ping_res is None:
                log.warning(
                    f"{label}: WARMUP PING returned None. last_error={ping_err}. "
                    f"Attempting MT5 reconnect..."
                )
                self._dump_mt5_state(label, "WARMUP PING returned None — channel cold")
                self.tele.warn(
                    f"⚠️ *{label}: trade channel cold (warmup failed)*\n"
                    f"last\\_error: `{ping_err}`\n"
                    f"Cycling MT5 connection via shutdown+initialize..."
                )
                return self._attempt_mt5_reconnect(label)

            if ping_res.retcode != 10009:
                rc_name = _MT5_RETCODE_MAP.get(ping_res.retcode, f"UNKNOWN_{ping_res.retcode}")
                log.warning(
                    f"{label}: WARMUP PING rejected retcode={ping_res.retcode} ({rc_name}) "
                    f"comment={ping_res.comment} last_error={ping_err}"
                )
                self._dump_mt5_state(
                    label,
                    f"WARMUP PING rejected rc={ping_res.retcode} ({rc_name})"
                )
                self.tele.warn(
                    f"⚠️ *{label}: warmup ping rejected*\n"
                    f"retcode `{ping_res.retcode}` ({rc_name}) — `{ping_res.comment}`\n"
                    f"last\\_error: `{ping_err}`\n"
                    f"Cycling MT5 connection..."
                )
                # Cancel partial ping if a ticket was issued
                try:
                    if ping_res.order:
                        self.adapter.mt5.order_send({
                            "action": self.adapter.mt5.TRADE_ACTION_REMOVE,
                            "order": ping_res.order,
                        })
                except Exception:
                    pass
                return self._attempt_mt5_reconnect(label)

            # Success — cancel the ping
            try:
                cancel_res = self.adapter.mt5.order_send({
                    "action": self.adapter.mt5.TRADE_ACTION_REMOVE,
                    "order":  ping_res.order,
                })
                if cancel_res is None or cancel_res.retcode != 10009:
                    log.warning(
                        f"{label}: warmup ping placed (ticket {ping_res.order}) "
                        f"but cancel failed (rc={getattr(cancel_res,'retcode',None)}). "
                        f"Ping is $100 from market — will not fill."
                    )
            except Exception as e:
                log.warning(f"{label}: ping cancel raised: {e}")

            log.info(
                f"{label}: ✅ trade channel warmup OK (ping ticket {ping_res.order})"
            )
            return True

        except Exception as e:
            log.error(f"{label}: warmup raised {type(e).__name__}: {e}")
            self._dump_mt5_state(label, f"WARMUP raised {type(e).__name__}: {e}")
            self.tele.warn(f"⚠️ {label}: warmup raised exception — attempting reconnect")
            return self._attempt_mt5_reconnect(label)

    def _attempt_mt5_reconnect(self, label: str) -> bool:
        """v2.5.3: Force-cycle the MT5 connection: shutdown + initialize + verify.

        Called when warmup ping fails. Recovers a cold trade channel by tearing
        down and re-establishing the SDK. Returns True if reconnect succeeded
        and verified healthy state, False otherwise.

        shadow_positions/shadow_pendings are unaffected — reconcile loop will
        rebuild them from broker state on the next tick.
        """
        log.warning(f"{label}: cycling MT5 connection (shutdown + initialize)")
        try:
            self.adapter.mt5.shutdown()
        except Exception as e:
            log.warning(f"{label}: mt5.shutdown() raised: {e}")

        # Tiny pause to let the OS release sockets cleanly
        time.sleep(0.5)

        try:
            init_ok = self.adapter.mt5.initialize()
            if not init_ok:
                err = self.adapter.mt5.last_error()
                self._dump_mt5_state(
                    label, f"RECONNECT: mt5.initialize() returned False, last_error={err}"
                )
                self.tele.error(
                    f"❌ *{label}: mt5.initialize() failed after shutdown*\n"
                    f"last\\_error: `{err}`\n"
                    f"Anchor will be skipped. Watchdog may need to restart bot."
                )
                return False
        except Exception as e:
            self._dump_mt5_state(label, f"RECONNECT: mt5.initialize() raised: {e}")
            self.tele.error(f"❌ *{label}: mt5.initialize() raised:* `{e}`")
            return False

        # Verify reconnect actually worked
        try:
            ti = self.adapter.mt5.terminal_info()
            ai = self.adapter.mt5.account_info()
            if ti is None or not ti.connected or not ti.trade_allowed:
                self._dump_mt5_state(label, "RECONNECT: post-reconnect terminal unhealthy")
                self.tele.error(
                    f"❌ *{label}: post-reconnect terminal unhealthy*\n"
                    f"connected=`{getattr(ti,'connected',None)}`  "
                    f"trade\\_allowed=`{getattr(ti,'trade_allowed',None)}`"
                )
                return False
            if ai is None:
                self._dump_mt5_state(label, "RECONNECT: account_info() is None after reconnect")
                self.tele.error(f"❌ *{label}: post-reconnect account_info is None*")
                return False
            log.info(
                f"{label}: ✅ MT5 reconnected — account #{ai.login} on {ai.server}, "
                f"balance ${ai.balance:.2f}"
            )
            self.tele.warn(
                f"♻️ *{label}: MT5 trade channel cycled (recovery)*\n"
                f"Account `#{ai.login}` on `{ai.server}` — proceeding with placement."
            )
            return True
        except Exception as e:
            self._dump_mt5_state(label, f"RECONNECT: post-reconnect verify raised: {e}")
            self.tele.error(f"❌ *{label}: post-reconnect verification raised:* `{e}`")
            return False

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

        # v2.5: REHYDRATE from persisted state for any broker position we don't
        # already track in-memory. This handles bot restart mid-trade so we
        # preserve max_fav (= $5 lock state) and fill_time (= freeze gate state).
        if self._pending_shadow_rehydrate:
            for broker_p in broker_positions:
                tk = int(broker_p.ticket)
                if tk in self.shadow_positions:
                    continue
                saved = self._pending_shadow_rehydrate.get(str(tk))
                if saved:
                    self.shadow_positions[tk] = {
                        'anchor_label': saved.get('anchor_label', 'RECOVERED'),
                        'side':         saved.get('side') or ('BUY' if broker_p.type == 0 else 'SELL'),
                        'entry_price':  float(broker_p.price_open),
                        'current_sl':   float(broker_p.sl),
                        'tp_level':     float(broker_p.tp),
                        # v2.5 critical: restore max_fav from persisted state, not entry price
                        'max_fav':      float(saved.get('max_fav') or broker_p.price_open),
                        'fill_time':    saved.get('fill_time') or pd.Timestamp.now(tz='UTC').isoformat(),
                    }
                    self.tele.info(
                        f"♻️ Rehydrated position {tk} {saved.get('side','?')} "
                        f"entry=${broker_p.price_open:.2f} max_fav=${float(saved.get('max_fav') or broker_p.price_open):.2f} "
                        f"SL=${broker_p.sl:.2f} (lock state preserved)"
                    )
            # Clear the rehydration source after first reconcile
            self._pending_shadow_rehydrate = {}

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
                # Cancel sibling (OCO) — v2.3: sibling may be None if other side was skipped pre-flight
                # OCO vs No-OCO sibling handling
                if not getattr(self.cfg, 'no_oco', False):
                    if sibling is not None and sibling in broker_pend_tickets:
                        try:
                            self.adapter.cancel_order(sibling)
                        except Exception as e:
                            self.tele.warn(f"Could not cancel sibling {sibling}: {e}")
                    if sibling is not None:
                        self.shadow_pendings.pop(sibling, None)
                else:
                    if sibling is not None and sibling in self.shadow_pendings:
                        self.shadow_pendings[sibling]['sibling_ticket'] = None
                        self.tele.info(f"No-OCO: sibling {sibling} left live (reversal can fill it)")
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
                    if shadow.get('tstop'):
                        outcome = 'TSTOP'
                    # v2.7: hold-duration audit -- permanent detector for the freeze bug.
                    # fill_time is TRUE UTC; close_deal.time is broker epoch seconds, so
                    # subtract the offset to compare in the same (UTC) clock.
                    hold_min = None
                    try:
                        _ft = shadow.get('fill_time')
                        if _ft:
                            _off = getattr(self.adapter, 'tick_time_offset_hours', 0) or 0
                            _close_utc = pd.Timestamp(int(close_deal.time) - _off * 3600,
                                                      unit='s', tz='UTC')
                            hold_min = (_close_utc - pd.Timestamp(_ft)).total_seconds() / 60.0
                    except Exception:
                        hold_min = None
                    hold_txt = f"  |  held `{hold_min:.1f}m`" if hold_min is not None else ""
                    # Freeze-breach alarm: a Trail-class exit before the freeze window
                    # elapsed should be impossible. Exits AT entry (+/- $0.40) are the
                    # +$3 BASE LOCK firing, which IS allowed during freeze -- excluded.
                    if (hold_min is not None and outcome == 'Trail'
                            and self.cfg.freeze_minutes > 0
                            and hold_min < self.cfg.freeze_minutes - 0.5
                            and abs(close_price - float(shadow['entry_price'])) > 0.40):
                        self.tele.warn(
                            f"🚨 *FREEZE BREACH* {shadow['anchor_label']} "
                            f"{shadow['side']}: Trail exit after only {hold_min:.1f}m "
                            f"(< freeze {self.cfg.freeze_minutes}m). Trail gate is "
                            f"engaging early -- investigate before next anchor."
                        )
                    sev = Severity.SUCCESS if pnl_usd > 0 else Severity.WARN
                    self.tele.send(
                        f"📤 CLOSE: *{shadow['anchor_label']}* {shadow['side']} "
                        f"`{outcome}` @ ${close_price:.2f}\n"
                        f"P&L: `${pnl_usd:+.2f}`  |  Daily total: `${self.state['daily_pnl']:+.2f}`{hold_txt}",
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
                    # v2.5.6: rich journal row (one per fill) for strategy evaluation
                    try:
                        self._write_journal(shadow, close_deal, close_price, outcome, pnl_usd, ticket)
                    except Exception as je:
                        log.warning(f"journal write failed for {ticket}: {je}")
                    self._save_state()
            except Exception as e:
                self.tele.warn(f"Could not fetch close deal for {ticket}: {e}")

    # ------------------------------------------------------------------------
    # v2.5.6: Trade journal — one rich row per fill, monthly CSV.
    # The decisive column is modeled_trail_exit vs actual_exit: for trail exits
    # it shows whether the live fill matched the backtest assumption (peak-0.30).
    # That comparison is what validates (or kills) the backtest's edge.
    # ------------------------------------------------------------------------
    def _write_journal(self, shadow, close_deal, close_price, outcome, pnl_usd, ticket):
        import os as _os
        jdir = _os.path.join(self.run_dir, "journal")
        _os.makedirs(jdir, exist_ok=True)
        now_ist = pd.Timestamp.now(tz='Asia/Kolkata')
        month = now_ist.strftime('%Y-%m')
        jpath = _os.path.join(jdir, f"trades_{month}.csv")

        side = shadow['side']
        entry = float(shadow['entry_price'])
        max_fav = float(shadow.get('max_fav', entry))
        # favorable excursion in price terms
        if side == 'BUY':
            fav_dist = max_fav - entry
            modeled_trail = entry + fav_dist - self.cfg.trail_gap  # peak - 0.30
        else:
            fav_dist = entry - max_fav
            modeled_trail = entry - fav_dist + self.cfg.trail_gap
        # refine outcome into the lock tiers when it was a 'Trail'-class exit
        refined = outcome
        if outcome == 'Trail':
            if abs(fav_dist) < 3.0:
                refined = 'SL_be'         # closed near BE before $3 lock
            elif fav_dist < 5.0:
                refined = 'SL_lock_3'     # $3 BE lock region
            elif fav_dist < (self.cfg.trail_gap + 5.0):
                refined = 'SL_lock_5'     # $5->+4 lock region
            else:
                refined = 'SL_trail'      # genuine trailing exit
        # slippage of the actual fill vs the modeled trail level (only meaningful for trail exits)
        trail_slip = ''
        if refined in ('SL_trail', 'SL_lock_5', 'SL_lock_3', 'SL_be'):
            trail_slip = round(close_price - modeled_trail, 3)

        entry_time = shadow.get('entry_time')
        entry_time_ist = (pd.Timestamp(entry_time).tz_convert('Asia/Kolkata').strftime('%H:%M:%S')
                          if entry_time is not None else '')
        row = [
            now_ist.strftime('%Y-%m-%d'),                # date_ist
            shadow.get('anchor_label', ''),              # anchor
            shadow.get('anchor_price', ''),              # anchor_price
            side,                                        # side
            entry_time_ist,                              # entry_time_ist
            round(entry, 3),                             # entry_price
            shadow.get('lot', self.cfg.lot_size),        # lot
            round(entry - self.cfg.sl_dist, 3) if side=='BUY' else round(entry + self.cfg.sl_dist, 3),  # initial_sl
            round(entry + self.cfg.tp_dist, 3) if side=='BUY' else round(entry - self.cfg.tp_dist, 3),  # initial_tp
            round(fav_dist, 3),                          # max_favorable ($ price)
            now_ist.strftime('%H:%M:%S'),                # exit_time_ist
            round(close_price, 3),                       # actual_exit_price
            round(modeled_trail, 3),                     # modeled_trail_exit (peak-0.30)
            trail_slip,                                  # actual - modeled (THE validation number)
            refined,                                     # exit_reason
            round(pnl_usd, 2),                           # realized_pnl_usd
            ticket,                                      # ticket
        ]
        new_file = not _os.path.exists(jpath)
        with open(jpath, "a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(['date_ist','anchor','anchor_price','side','entry_time_ist',
                            'entry_price','lot','initial_sl','initial_tp','max_favorable',
                            'exit_time_ist','actual_exit_price','modeled_trail_exit',
                            'trail_slip','exit_reason','realized_pnl_usd','ticket'])
            w.writerow(row)
        log.info(f"journal: {shadow.get('anchor_label')} {side} {refined} "
                 f"pnl=${pnl_usd:+.2f} trail_slip={trail_slip}")

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
                    # v2.7 FIX (CRITICAL): fill_time is stored in TRUE UTC (the broker
                    # offset is subtracted at capture), but bar_time is broker-clock-
                    # LABELED-as-UTC (MT5 convention, no offset applied). Comparing them
                    # inflated elapsed by +offset hours (+3h), so the freeze window was
                    # ALWAYS already expired and the trail engaged from bar one on every
                    # position (the $63-on-a-$1,500-move exits). Shift fill_time back
                    # into the broker-clock convention so both sides use the same clock.
                    _off = getattr(self.adapter, 'tick_time_offset_hours', 0) or 0
                    entry_time_for_pos = entry_time_for_pos + pd.Timedelta(hours=_off)
                except Exception:
                    entry_time_for_pos = None  # unknown fill time -> no freeze, normal trail
            else:
                # v2.7 FIX: None = no freeze (bot.py gates on `entry_time is not None`).
                # The old `= bar_time` fallback made elapsed ~= 0 on EVERY bar, freezing
                # the trail FOREVER -- the opposite of the original comment's intent.
                entry_time_for_pos = None

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
            old_max_fav = shadow.get('max_fav')
            update_position_on_bar(pos, bar_series, bar_time, self.cfg)
            shadow['current_sl'] = pos.current_sl
            shadow['max_fav'] = pos.max_fav

            # v2.7.1 TSTOP -- loser time-stop (grid-validated). At hold expiry, a leg
            # whose best favorable excursion never reached +$tstop_fav is a trapped
            # fake-out; close at market (~ -$5..-$12) instead of riding to the full SL.
            # One-shot by construction: max_fav is monotonic, so once fav >= threshold
            # at expiry this can never fire later.
            if (getattr(self.cfg, 'tstop_fav', 0.0) > 0
                    and self.cfg.freeze_minutes > 0
                    and entry_time_for_pos is not None):
                try:
                    _elapsed = (bar_time - entry_time_for_pos).total_seconds() / 60.0
                except Exception:
                    _elapsed = None
                if _elapsed is not None and _elapsed >= self.cfg.freeze_minutes:
                    _fav = (pos.max_fav - pos.entry_price) if shadow['side'] == 'BUY' \
                        else (pos.entry_price - pos.max_fav)
                    if _fav < self.cfg.tstop_fav:
                        self.tele.warn(
                            f"\u23f1 TSTOP: {shadow['anchor_label']} {shadow['side']} "
                            f"never reached +${self.cfg.tstop_fav:.2f} fav in "
                            f"{self.cfg.freeze_minutes}m (peak +${max(_fav, 0):.2f}) -- "
                            f"closing at market instead of riding to SL."
                        )
                        shadow['tstop'] = True
                        try:
                            self.adapter.close_position(ticket, dry_run=self.paper)
                        except Exception as e:
                            log.warning(f"TSTOP close failed for {ticket}: {e}")
                        continue

            if not self.paper:
                # v2.5.7: read broker's ACTUAL sl and re-assert if it doesn't match
                # the bot's intended sl — EVERY bar, not only on advance. A silently
                # dropped/rejected modify (the A2 -990 bug) self-heals next bar
                # instead of leaving the original stop live for hours.
                intended = round(pos.current_sl, 2)

                # v2.5.8: clamp SL to the broker's minimum LEGAL distance from market.
                # This broker reports stops_level=0 but rejects stops within ~$0.20 of
                # price (INVALID_STOPS / 10013); probe confirmed $0.30 is safely accepted.
                # Pull the SL to the closest legal level rather than send an illegal value
                # that gets rejected (which left the OLD stop live — the A2 -990 bug).
                # NOTE: this only governs HOW CLOSE the stop may sit to market; the
                # $3 BE and $5->+4 locks (in update_position_on_bar) still guarantee
                # minimum locked profit regardless of this clamp.
                MIN_SL_DIST = 0.00
                try:
                    ctk = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
                    csi = self.adapter.mt5.symbol_info(self.cfg.symbol)
                    if ctk is not None:
                        floor = MIN_SL_DIST
                        if csi is not None and csi.trade_stops_level > 0:
                            floor = max(floor, csi.trade_stops_level * csi.point)
                        if shadow['side'] == 'BUY':
                            max_legal = round(ctk.bid - floor, 2)
                            if intended > max_legal:
                                log.info(f"SL clamp ticket={ticket} BUY: ${intended} "
                                         f"too close to bid ${ctk.bid:.2f} → ${max_legal}")
                                intended = max_legal
                        else:
                            min_legal = round(ctk.ask + floor, 2)
                            if intended < min_legal:
                                log.info(f"SL clamp ticket={ticket} SELL: ${intended} "
                                         f"too close to ask ${ctk.ask:.2f} → ${min_legal}")
                                intended = min_legal
                except Exception as e:
                    log.warning(f"SL clamp check failed for {ticket}: {e}")

                try:
                    bp = self.adapter.mt5.positions_get(ticket=ticket)
                    broker_sl = float(bp[0].sl) if bp else None
                except Exception as e:
                    broker_sl = None
                    log.warning(f"Could not read broker SL for {ticket}: {e}")

                needs_assert = (broker_sl is None) or (abs(broker_sl - intended) > 0.05)
                if needs_assert:
                    if pos.current_sl != old_sl:
                        log.info(
                            f"Trail advance ticket={ticket} side={shadow['side']} "
                            f"SL ${old_sl:.2f} → ${intended:.2f} (max_fav=${pos.max_fav:.2f})"
                        )
                    else:
                        log.warning(
                            f"SL DRIFT ticket={ticket} side={shadow['side']}: broker "
                            f"${broker_sl} != intended ${intended} — re-asserting"
                        )
                    ok = False
                    try:
                        ok = self.adapter.modify_position_sl(ticket, intended)
                    except Exception as e:
                        log.warning(f"modify_position_sl raised for {ticket}: {e}")
                    if not ok:
                        self.tele.warn(
                            f"⚠️ *SL modify FAILED* ticket={ticket} {shadow['side']}\n"
                            f"Intended `${intended}`, broker still `${broker_sl}`.\n"
                            f"Trade is on its PREVIOUS stop. Re-attempting next bar."
                        )
            else:
                if pos.current_sl != old_sl:
                    log.info(
                        f"[PAPER] Trail advance ticket={ticket} SL ${old_sl:.2f} → "
                        f"${pos.current_sl:.2f} (max_fav=${pos.max_fav:.2f})"
                    )
            # v2.5.5: persist whenever SL moved OR max_fav advanced. Without this,
            # max_fav/current_sl live only in RAM between saves; a Windows sleep or
            # crash mid-trade would restore a STALE max_fav (often == entry) and the
            # trail would "forget" the peak it had already reached. Saving here makes
            # the trail restart-safe — the dominant cause of "trail doesn't work right".
            if pos.current_sl != old_sl or pos.max_fav != old_max_fav:
                self._save_state()

    # ------------------------------------------------------------------------
    # Bulk operations & summaries
    # ------------------------------------------------------------------------

    def _flatten_all(self, reason: str = "Manual"):
        """v2.5: hardened EOD flatten — retries up to 3x on rc=-1, verifies via broker query.
        Critical: if a position fails to close at EOD, it stays open OVERNIGHT
        with bracket SL, and the bot loses tracking. Worth fighting for the close."""
        self.tele.warn(f"FLATTEN ({reason}) — closing {len(self.shadow_positions)} positions, "
                       f"cancelling {len(self.shadow_pendings)} pendings")

        import time as _time

        # Close positions with retry+verify
        failed_closes = []
        for ticket in list(self.shadow_positions.keys()):
            closed = False
            for attempt in range(3):
                try:
                    result = self.adapter.close_position(ticket, dry_run=self.paper)
                    if self.paper:
                        closed = True
                        break
                    # Verify by querying broker
                    _time.sleep(0.3)
                    still_open = self.adapter.mt5.positions_get(ticket=ticket)
                    if not still_open:
                        closed = True
                        log.info(f"Position {ticket} verified closed (attempt {attempt+1})")
                        break
                    else:
                        log.warning(f"Position {ticket} still open after close attempt {attempt+1} — retrying")
                except Exception as e:
                    log.warning(f"Close {ticket} attempt {attempt+1} raised: {e}")
                _time.sleep(0.5)
            if not closed:
                failed_closes.append(ticket)
            self.shadow_positions.pop(ticket, None)

        # Cancel pendings with retry+verify
        failed_cancels = []
        for ticket in list(self.shadow_pendings.keys()):
            cancelled = False
            for attempt in range(3):
                try:
                    self.adapter.cancel_order(ticket, dry_run=self.paper)
                    if self.paper:
                        cancelled = True
                        break
                    _time.sleep(0.2)
                    still_pending = self.adapter.mt5.orders_get(ticket=int(ticket))
                    if not still_pending:
                        cancelled = True
                        break
                except Exception as e:
                    log.warning(f"Cancel {ticket} attempt {attempt+1} raised: {e}")
                _time.sleep(0.3)
            if not cancelled:
                failed_cancels.append(ticket)
            self.shadow_pendings.pop(ticket, None)

        # v2.5.2: if a deferred anchor was queued, drop it on flatten (consistent state)
        if self._deferred_anchor is not None:
            log.info(f"Flatten dropped deferred anchor: {self._deferred_anchor.get('label')}")
            self._deferred_anchor = None

        # Critical alert if anything failed to close — these are real money exposure
        if failed_closes or failed_cancels:
            self.tele.critical(
                f"🚨 *FLATTEN INCOMPLETE — manual intervention needed*\n"
                f"Failed to close {len(failed_closes)} positions: `{failed_closes}`\n"
                f"Failed to cancel {len(failed_cancels)} pendings: `{failed_cancels}`\n"
                f"These remain at broker with bracket SL but no bot tracking.\n"
                f"Check MT5 terminal manually."
            )

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
        # v2.5.3: escape underscores so Telegram Markdown doesn't italicize
        auto_lot_label = "auto\\_lot=on" if self.cfg.auto_lot else "auto\\_lot=off"
        fp_cap_label = (f"\nFP\\_ZERO\\_MAX\\_LOT: `{self.FP_ZERO_MAX_LOT}` ⚠ CAP ACTIVE"
                        if self.FP_ZERO_MAX_LOT is not None
                        else "\nFP\\_ZERO\\_MAX\\_LOT: `None` (Pepperstone demo — no cap)")
        self.tele.success(
            f"🚀 *AUREON v2.5.3 {'PAPER' if self.paper else 'LIVE'} starting*\n"
            f"Lot: `{self.cfg.lot_size}` ({auto_lot_label})\n"
            f"Kill switch: `-{self.cfg.daily_loss_pct*100:.1f}%`\n"
            f"Defer waits: A1/A3=15s, A2/A4=30s | rc=-1 retries: {self.MAX_PLACEMENT_RETRIES} (15s, 30s)\n"
            f"Trade channel warmup: ON ($100 ping + mt5 reconnect on cold)\n"
            f"Diagnostic dumps: ON (full MT5 state on any failure)"
            + fp_cap_label
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

        # v2.5.6: sleep-gap detector. The bot CANNOT run while the OS is suspended
        # (the process is frozen by the kernel). What we CAN do is notice on wake
        # that a large gap occurred, alert, and force an immediate broker reconcile
        # so we re-sync state before doing anything. _last_loop_wall is wall-clock
        # time of the previous loop iteration; a gap far larger than the loop
        # interval means the OS slept/locked.
        self._last_loop_wall = time.time()
        SLEEP_GAP_THRESHOLD = 60.0  # loop runs every 0.2-1.0s; >60s gap = suspension

        try:
            while True:
                tick_start = time.time()
                # --- sleep-gap detection ---
                gap = tick_start - self._last_loop_wall
                self._last_loop_wall = tick_start
                if gap > SLEEP_GAP_THRESHOLD:
                    mins = gap / 60.0
                    log.warning(f"SLEEP GAP DETECTED: {gap:.0f}s ({mins:.1f} min) "
                                f"since last loop — OS was likely suspended/locked.")
                    try:
                        self.tele.warn(
                            f"⏰ *Bot was asleep* ~{mins:.0f} min "
                            f"(OS suspended/locked). Forcing broker reconcile now — "
                            f"CHECK any open trades, they were unmanaged during this gap.")
                    except Exception:
                        pass
                    # force immediate re-sync with broker before normal processing
                    try:
                        self._reconcile_with_broker()
                    except Exception as e:
                        log.warning(f"post-wake reconcile failed: {e}")
                # --- end sleep-gap detection ---
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
        except Exception as e:
            # v2.5: capture unexpected exceptions, alert, then exit cleanly
            import traceback
            tb = traceback.format_exc()
            self.tele.critical(
                f"🚨 *AUREON CRASHED — unhandled exception*\n"
                f"`{type(e).__name__}: {e}`\n"
                f"Watchdog will restart. Open positions stay protected by broker SL.\n"
                f"Traceback in logs."
            )
            log.error(f"Unhandled exception in run loop:\n{tb}")
            # Re-raise so watchdog can restart cleanly
            raise
        finally:
            # v2.5: always release PID lock so watchdog restart isn't blocked
            self._release_pid_lock()
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
            kill_base = self.state.get('day_start_equity') or self.cfg.starting_balance
            kill_limit = self.cfg.daily_loss_pct * kill_base
            # v2.5.4: persist to file log, not just Telegram, so any future gating
            # of anchors is always reconstructable on disk.
            log.warning(
                f"KILL SWITCH TRIGGERED — daily_pnl=${self.state['daily_pnl']:.2f} "
                f"limit=-${kill_limit:.0f} (base day_start_equity=${kill_base:,.2f}). "
                f"Flattening; no new anchors today."
            )
            self.tele.critical(
                f"🚨 *KILL SWITCH TRIGGERED*\n"
                f"Daily P&L: `${self.state['daily_pnl']:.2f}` "
                f"(limit `-${kill_limit:.0f}`, from day-open `${kill_base:,.0f}`)\n"
                f"Flattening everything, no more trades today."
            )
            self._flatten_all(reason="KillSwitch")
            self.state['kill_switch_locked'] = True
            self._save_state()

        if self.state['kill_switch_locked']:
            # v2.5.4: leave a periodic on-disk record of WHY anchors are gated,
            # so a silent "no trades today" is never a mystery in the log again.
            if self._tick_counter % self.STATUS_EVERY_TICKS == 0:
                log.warning(
                    "Anchor processing GATED: kill switch locked for the day "
                    f"(daily_pnl=${self.state['daily_pnl']:.2f}). Resets next broker day."
                )
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

        # 7b. v2.5: Complete any deferred anchor placement (settle window or retry)
        self._complete_deferred_anchor()

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