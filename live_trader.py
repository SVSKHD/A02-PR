"""
AUREON LiveTrader — production live/paper trading loop. Version: version.py.

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
try:
    from version import __version__ as AUREON_VERSION
except ImportError:
    AUREON_VERSION = '2.9.2'  # fallback if version.py missing

from telemetry import telemetry_from_env, Severity
from mt5_adapter import _MT5_RETCODE_MAP

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

    # Monday-wake + A1 hardening (eliminate the Jun-8 silent-miss)
    OFFSET_VALIDATE_RETRIES   = 10     # wake offset detect attempts
    OFFSET_VALIDATE_WAIT_S    = 30     # spacing between offset detect attempts
    ANCHOR_FETCH_RETRIES      = 3      # get_m5_close attempts before giving up
    ANCHOR_FETCH_RETRY_WAIT_S = 2
    WAKE_FAILSAFE_GRACE_MIN   = 15     # alert if still asleep this long past open
    WAKE_FAILSAFE_REPEAT_S    = 300    # re-alert cadence while still asleep

    def __init__(self, cfg, adapter, paper: bool = True):
        from strategy import Position  # late import
        from utils import anchor_datetime_utc, eod_datetime_utc  # late import

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
        # v2.9.8: pendings rehydrate source (rescue flag survives restarts)
        self._pending_pendings_rehydrate = self.state.get('shadow_pendings_extended', {})

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

        # Monday-wake hardening: no anchor places until the broker time offset is
        # measured fresh on wake and matches cfg.EXPECTED_BROKER_OFFSET_HOURS.
        # Stays False until _validate_offset_on_wake() confirms it (live mode).
        self.offset_validated = False

        # Today's trade log header
        if not os.path.exists(self.daylog_path):
            with open(self.daylog_path, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["date", "anchor", "side", "entry", "exit",
                     "outcome", "pnl_usd", "ticket"])

        self.tele.info(
            f"LiveTrader v{AUREON_VERSION} initialized ({'PAPER' if paper else 'LIVE'}) — "
            f"4-anchor multi-session AUREON, lot {cfg.lot_size}"
        )
        self.tele.info(
            f"Anchors: {[a[0] for a in cfg.anchors]}, "
            f"kill switch: -{cfg.daily_loss_pct*100:.1f}%"
        )
        # TEST MODE banner: surface any active test-scope toggle loudly so a
        # forced code path is never mistaken for production behavior. Defaults OFF.
        if os.environ.get('AUREON_TEST_FORCE_MONDAY_A1', '').strip().lower() \
                in ('1', 'true', 'yes', 'on'):
            self.tele.warn(
                "🧪 TEST MODE ACTIVE — AUREON_TEST_FORCE_MONDAY_A1=1: A1 resolves "
                "via monday_a1_override on ANY weekday (test only, not production)."
            )


    def _broker_date(self, utc_now: pd.Timestamp) -> DateType:
        return (utc_now + pd.Timedelta(hours=self.cfg.broker_tz_offset_hours)).date()

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

    def _write_status(self, broker_date: DateType, sleeping: bool = False):
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
        # Auto-deploy safe-restart signals (INFRA): the watchdog reads these to
        # know when it can restart the bot WITHOUT touching an open trade. flat =
        # no open positions, no pending straddle, no anchor pending placement.
        flat = (len(self.shadow_positions) == 0 and len(self.shadow_pendings) == 0
                and self._deferred_anchor is None)
        status["flat"] = flat
        try:
            status["eod_done"] = bool(
                flat and self._eod_reached(broker_date, pd.Timestamp.now(tz="UTC")))
        except Exception:
            status["eod_done"] = False
        # v3.0.0 follow-up: keep `status` answerable during the weekend
        # deep-sleep and carry last-day + week-to-date stats so the Telegram
        # reply is useful precisely when the human checks in on a closed
        # market. Fail-safe: any error here leaves the normal status intact.
        status["sleeping"] = sleeping
        if sleeping:
            status["next_anchor"] = "A1 02:00 broker"
            try:
                import journal as _jmod
                today = pd.Timestamp.now(tz='Asia/Kolkata').strftime('%Y-%m-%d')
                jpath = os.path.join(self._journal_dir(), f"trades_{today[:7]}.csv")
                last_day, week = _jmod.summarize_recent(jpath, today)
                status["weekend_stats"] = {"last_day": last_day, "week": week}
            except Exception as e:
                status["weekend_stats"] = None
                log.debug(f"weekend stats skipped (non-fatal): {e!r}")
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

    def _eod_reached(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        eod = self._eod_datetime_utc(broker_date, self.cfg)
        return utc_now >= eod

    # ------------------------------------------------------------------------
    # v3.0.0 commit 4: weekend self-sleep + Monday auto-resume
    # ------------------------------------------------------------------------

    def _market_closed_now(self) -> bool:
        """Cheap probe: True if the broker's last tick is >1h old (weekend or a
        holiday). False on any error -- never blocks trading on a probe failure."""
        try:
            server_utc = self.adapter.server_time_utc()
            age = (pd.Timestamp.now(tz='UTC') - server_utc).total_seconds()
            return age > 3600
        except Exception as e:
            log.warning(f"market-closed probe failed: {e}")
            return False

    def _validate_offset_on_wake(self, reason: str = "wake") -> bool:
        """Guard 1 (core fix): on wake/startup, force a fresh broker time-offset
        detect and ASSERT it equals cfg.EXPECTED_BROKER_OFFSET_HOURS before any
        anchor logic. The Jun-8 silent A1 miss was a 0h misdetect -> wrong M5
        window -> no bars -> no trade, silently. Here a mismatch is LOUD and A1
        is BLOCKED (placing on a wrong offset is worse than not placing).

        Sets self.offset_validated and returns it. Heartbeat is kept alive
        between attempts so the watchdog never kills the bot mid-validation.
        Paper/backtest are never blocked (offset still detected for data)."""
        expected = int(getattr(self.cfg, "EXPECTED_BROKER_OFFSET_HOURS",
                               self.cfg.broker_tz_offset_hours))
        if self.paper:
            try:
                self.adapter.ensure_time_offset()
            except Exception as e:
                log.warning(f"paper offset detect (non-blocking) failed: {e}")
            self.offset_validated = True
            return True
        off = None
        for attempt in range(1, self.OFFSET_VALIDATE_RETRIES + 1):
            try:
                ok = self.adapter.ensure_time_offset()
                off = getattr(self.adapter, "tick_time_offset_hours", None)
            except Exception as e:
                ok = False
                log.warning(f"offset detect attempt {attempt} raised: {e}")
            if ok and off is not None and int(off) == expected:
                self.offset_validated = True
                self.tele.success(
                    f"✅ Monday wake: broker offset confirmed +{int(off)}h "
                    f"(attempt {attempt}/{self.OFFSET_VALIDATE_RETRIES}, {reason}).")
                return True
            log.warning(
                f"offset validate attempt {attempt}/{self.OFFSET_VALIDATE_RETRIES}: "
                f"got {off}h, expected {expected}h (ok={ok})")
            if attempt < self.OFFSET_VALIDATE_RETRIES:
                self._touch_heartbeat()
                time.sleep(self.OFFSET_VALIDATE_WAIT_S)
        self.offset_validated = False
        self.tele.critical(
            f"⚠️ offset detect FAILED on wake: got {off}h expected "
            f"{expected}h - A1 BLOCKED, manual check needed. Bot stays up and "
            f"will keep alerting; no anchor will place until the offset validates.")
        return False

    def _post_readiness(self, reason: str = "startup") -> None:
        """Guard 5: one-line Telegram readiness receipt so the human can see at a
        glance the bot is correctly armed for A1. Never raises."""
        try:
            off = getattr(self.adapter, "tick_time_offset_hours", None)
            tag = "validated" if self.offset_validated else "UNVALIDATED"
            try:
                a = self.cfg.anchors[0]
                bdate = self._broker_date(pd.Timestamp.now(tz="UTC"))
                rh, rm = self._resolved_anchor_hm(a[0], bdate, a[1], a[2])
                next_anchor = f"{a[0]} {rh:02d}:{rm:02d}"
            except Exception:
                next_anchor = "A1 02:00"
            state_ok = "ok" if isinstance(self.state, dict) and self.state else "fail"
            self.tele.info(
                f"🔧 Ready: offset {off}h {tag} · next anchor "
                f"{next_anchor} broker · state rehydrated {state_ok} ({reason})")
        except Exception as e:
            log.warning(f"readiness line failed (non-fatal): {e}")

    def _expected_market_open_utc(self, now_utc):
        """Guard 4 helper: the most recent expected weekly market-open instant
        (gold/FX reopen ~Sunday 22:00 UTC = broker Mon 01:00, UTC+3) IF we are
        inside the Mon-Fri trading window, else None (a legitimately-closed
        weekend, where staying asleep is correct and must NOT alarm)."""
        try:
            OPEN_WD, OPEN_HOUR = 6, 22  # Sunday=6 (Mon=0), 22:00 UTC
            days_since = (now_utc.weekday() - OPEN_WD) % 7
            candidate = (now_utc.normalize()
                         - pd.Timedelta(days=days_since)
                         + pd.Timedelta(hours=OPEN_HOUR))
            if candidate > now_utc:
                candidate -= pd.Timedelta(days=7)
            if (now_utc - candidate) > pd.Timedelta(days=5):  # past Fri ~21:00 close
                return None
            return candidate
        except Exception:
            return None

    def wait_until_market_open(self, reason: str = "startup") -> bool:
        """Reusable market-closed deep-sleep. The tick-age>3600s -> 300s sleep
        loop -> resume-when-fresh(<60s) logic is the original startup block,
        factored out so BOTH startup AND the main loop enter the SAME wait --
        the process now stays alive across the weekend and wakes itself Monday.

        Returns True when the market is open (or the probe failed -> proceed),
        False only on the clock-drift abort (caller decides whether to exit).
        On weekend ENTRY: announce once + save state. During sleep: keep the
        heartbeat alive (watchdog must not kill a sleeping bot) and re-check
        every 5 min. On WAKE: force a broker time-offset re-detect BEFORE any
        data call (Jun-8 cold-start fix) and announce the offset + resume."""
        try:
            server_utc = self.adapter.server_time_utc()
            now_utc = pd.Timestamp.now(tz='UTC')
            tick_age_sec = (now_utc - server_utc).total_seconds()
        except Exception as e:
            self.tele.warn(f"Could not verify broker time ({reason}): {e}")
            return True  # don't block; proceed as if open (original on-error path)

        if tick_age_sec > 3600:
            hours = tick_age_sec / 3600
            # ONE Telegram line on ENTERING weekend sleep (announce-once: the
            # while-loop below blocks here until Monday, so this never repeats).
            self.tele.info(
                f"💤 Weekend — market closed, sleeping, will auto-resume Monday. "
                f"Next anchor A1 02:00 broker. "
                f"(last tick {hours:.1f}h old; entered via {reason})")
            # Persist state before the long sleep so a mid-weekend VPS reboot
            # rehydrates cleanly and the relaunched process re-enters this wait.
            try:
                self._save_state()
            except Exception as e:
                log.warning(f"state save before weekend sleep failed: {e}")
            market_open = False
            # Keep `status` answerable while we sleep: the watchdog reads
            # status.json, and on a Sunday cold-start nothing has written it
            # yet (the startup wait runs before the main loop's first
            # _write_status), so `status` would return "No status available".
            sleep_bdate = self.state.get("last_broker_date", "")
            # Sleep the 5-min market re-check in short chunks, touching the
            # heartbeat each chunk. The watchdog restarts a bot whose heartbeat
            # is older than HEARTBEAT_STALE_SECONDS (180s in watchdog.py); a single
            # 300s sleep would let it go stale and trigger a weekend-long restart
            # loop, so touch every 30s while only re-probing the market every 5 min.
            HB_EVERY_S = 30
            RECHECK_EVERY_S = 300
            wake_failsafe_last = None   # Guard 4: throttle the repeated alarm
            while not market_open:
                slept = 0
                while slept < RECHECK_EVERY_S:
                    self._touch_heartbeat()  # keep alive so the watchdog doesn't kill us
                    self._write_status(sleep_bdate, sleeping=True)  # `status` stays fresh while asleep
                    time.sleep(HB_EVERY_S)
                    slept += HB_EVERY_S
                try:
                    server_utc = self.adapter.server_time_utc()
                    now_utc = pd.Timestamp.now(tz='UTC')
                    tick_age_sec = (now_utc - server_utc).total_seconds()
                    if tick_age_sec < 60:
                        market_open = True
                except Exception as e:
                    log.warning(f"Market-open check failed: {e}")
                # Guard 4 - failsafe wake alarm: if the market SHOULD be open
                # (past the weekly open instant + grace) but we are still asleep,
                # a wake failure (VPS down, feed/broker outage) must be LOUD, not
                # silent. Re-alert every WAKE_FAILSAFE_REPEAT_S until we wake.
                if not market_open:
                    nowu = pd.Timestamp.now(tz="UTC")
                    exp_open = self._expected_market_open_utc(nowu)
                    if (exp_open is not None and
                            nowu >= exp_open + pd.Timedelta(minutes=self.WAKE_FAILSAFE_GRACE_MIN)):
                        if (wake_failsafe_last is None or
                                (nowu - wake_failsafe_last).total_seconds() >= self.WAKE_FAILSAFE_REPEAT_S):
                            late_min = (nowu - exp_open).total_seconds() / 60.0
                            self.tele.critical(
                                f"⚠️ WAKE FAILSAFE: market should be open "
                                f"(expected ~{exp_open.strftime('%a %H:%M')} UTC, "
                                f"{late_min:.0f}min ago) but bot still asleep - manual "
                                f"check (VPS/feed/broker). A1 at risk.")
                            wake_failsafe_last = nowu
            # WAKE: re-detect the broker time offset BEFORE any get_m5_close
            # (Jun-8 cold-start: a 0h misdetect made A1 miss). Announce it so a
            # misdetect is visible immediately in Telegram on Monday wake.
            self.tele.success("📈 Market open — resuming. Week starting.")
            # Guard 1: validate the broker offset BEFORE any anchor logic runs on
            # the resumed loop; Guard 5: post the readiness receipt.
            self._validate_offset_on_wake(reason=f"wake/{reason}")
            self._post_readiness(reason=f"wake/{reason}")
            return True
        elif abs(tick_age_sec) > 120:
            # Market open but clock disagrees with broker → config problem
            self.tele.critical(
                f"❌ Broker server time drifts >2min from local UTC "
                f"(broker tick {server_utc} vs local {now_utc}). ABORTING. "
                f"Fix the OS clock (sync NTP) and restart.")
            return False
        # else: tick recent and within tolerance → market is open.
        # Guard 1/5: a market-open startup never entered the wake branch above,
        # so validate the offset and post readiness here before the loop trades.
        self._validate_offset_on_wake(reason=reason)
        self._post_readiness(reason=reason)
        return True

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
            f"🚀 *AUREON v{AUREON_VERSION} {'PAPER' if self.paper else 'LIVE'} starting*\n"
            f"Lot: `{self.cfg.lot_size}` ({auto_lot_label})\n"
            f"Kill switch: `-{self.cfg.daily_loss_pct*100:.1f}%`\n"
            f"Hold: `{self.cfg.freeze_minutes}m` | TSTOP: `fav<${getattr(self.cfg, 'tstop_fav', 0):.2f}` | NoOCO: `{getattr(self.cfg, 'no_oco', False)}`\n"
            f"Ladder: `2.5>BE | 6>+4 | 10>peak-2` | Trail: `gap ${self.cfg.trail_gap:.2f}, arm ${self.cfg.be_trigger:.2f}`\n"
            f"SL/TP: `${self.cfg.sl_dist:.0f}/${self.cfg.tp_dist:.0f}` | Roles: `normal + RESCUE 2nd legs`\n"
            f"Defer waits: A1/A3=15s, A2/A4=30s | rc=-1 retries: {self.MAX_PLACEMENT_RETRIES} (15s, 30s)\n"
            f"v3.0.0: `rescue=twin-open guard` | `boost-diag v2` | `13-module split`\n"
            f"Modules ({len(LOADED_MODULES)}): `{' '.join(LOADED_MODULES)}`"
            + fp_cap_label
        )

        # Broker time check.
        # Note: MT5 Python API only exposes the LAST TICK time, which becomes
        # stale during weekends/holidays. We must distinguish two cases:
        #   - tick is very old (>1h)  → market is closed; sleep until market opens
        #   - tick is recent (<1h) but disagrees with OS clock by >2min → real problem
        # v3.0.0 commit 4: the startup market-closed wait is now wait_until_market_open(),
        # shared with the main loop so weekends are handled wherever first seen.
        # commit 3: on a closed-market (Sunday) STARTUP only, backfill any EOD
        # Firebase write the week missed, before entering the sleep.
        if self._market_closed_now():
            self._firebase_weekly_reconcile()
        if self.wait_until_market_open(reason="startup") is False:
            return  # clock-drift abort (original behavior)

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
        # v3.0.0 commit 4: weekend/holiday self-sleep. If the market has closed
        # while we were running (Friday EOD onward), enter the SAME deep-sleep
        # used at startup and only return Monday when ticks are fresh again.
        # _reset_if_new_day (below) then fires on the Monday tick via broker_date.
        if self._market_closed_now():
            self.wait_until_market_open(reason="weekend")
            return
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
            # v3.0.0 commit 3: Firebase EOD journal -- ONCE per broker day, after
            # the book is flat and the day's P&L is final (never during anchor
            # capture). Guarded so it fires once and never blocks the EOD path.
            if self.state.get('firebase_eod_date') != str(broker_date):
                self._firebase_save_daily(broker_date)
                self.state['firebase_eod_date'] = str(broker_date)
                self._save_state()
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


# ============================================================================
# Live entry point (moved from bot.py in v3.0.0; bot.py main() calls this).
# ============================================================================
def run_live(cfg, paper: bool = True):
    """
    Live or paper trading. Connects to the already-running MT5 terminal
    on this machine (which must be logged into your broker account first).
    Delegates to LiveTrader for the full event loop.
    """
    from mt5_adapter import MT5Adapter  # late import: only the live path needs MT5
    adapter = MT5Adapter(getattr(cfg, 'symbol', 'XAUUSD'),  # hardening #7: probe the configured symbol
                         expected_offset_hours=getattr(cfg, 'EXPECTED_BROKER_OFFSET_HOURS', None))  # Tier-2 consistency
    try:
        trader = LiveTrader(cfg, adapter, paper=paper)
        trader.run()
    finally:
        adapter.shutdown()


# ============================================================================
# v3.0.0 structural split -- method modules bound onto LiveTrader.
# Each function below was moved verbatim (body byte-identical, dedented) out of
# this class into a focused module; it takes `self` as first param and is bound
# here as a method, so every self.x() call site, every state.json key, every
# Telegram string and the 19-col journal schema are unchanged.
# ============================================================================
import state as _state_mod
import risk as _risk_mod
import anchors as _anchors_mod
import fills as _fills_mod
import trails as _trails_mod
import journal as _journal_mod

LiveTrader._load_state              = _state_mod._load_state
LiveTrader._save_state              = _state_mod._save_state
LiveTrader._acquire_pid_lock        = _state_mod._acquire_pid_lock
LiveTrader._release_pid_lock        = _state_mod._release_pid_lock
LiveTrader._compute_safe_lot        = _risk_mod._compute_safe_lot
LiveTrader._check_kill_switch       = _risk_mod._check_kill_switch
LiveTrader._ensure_day_start_equity = _risk_mod._ensure_day_start_equity
LiveTrader._flatten_all             = _risk_mod._flatten_all
LiveTrader._process_anchor_if_due   = _anchors_mod._process_anchor_if_due
LiveTrader._process_anchor          = _anchors_mod._process_anchor
LiveTrader._complete_deferred_anchor= _anchors_mod._complete_deferred_anchor
LiveTrader._place_orders_for_anchor = _anchors_mod._place_orders_for_anchor
LiveTrader._dump_mt5_state          = _anchors_mod._dump_mt5_state
LiveTrader._warmup_trade_channel    = _anchors_mod._warmup_trade_channel
LiveTrader._attempt_mt5_reconnect   = _anchors_mod._attempt_mt5_reconnect
LiveTrader._confirm_a1_placement    = _anchors_mod._confirm_a1_placement
LiveTrader._resolved_anchor_hm      = _anchors_mod._resolved_anchor_hm
LiveTrader._await_fresh_tick_for_placement = _anchors_mod._await_fresh_tick_for_placement
LiveTrader._extract_ticket          = staticmethod(_anchors_mod._extract_ticket)
LiveTrader._reconcile_with_broker   = _fills_mod._reconcile_with_broker
LiveTrader._manage_trails_on_bar_close = _trails_mod._manage_trails_on_bar_close
LiveTrader._write_journal           = _journal_mod._write_journal
LiveTrader._send_daily_summary      = _journal_mod._send_daily_summary
LiveTrader._send_today_summary      = _journal_mod._send_today_summary
LiveTrader._journal_dir             = _journal_mod._journal_dir
LiveTrader._firebase_save_daily     = _journal_mod._firebase_save_daily
LiveTrader._firebase_weekly_reconcile = _journal_mod._firebase_weekly_reconcile

# Module receipt printed in the startup banner (rule #6: deployment drift
# is visible in Telegram).
LOADED_MODULES = ['utils', 'config', 'strategy', 'mt5_adapter', 'backtest',
                  'state', 'risk', 'anchors', 'fills', 'trails', 'journal',
                  'live_trader', 'bot', 'firebase_journal']
