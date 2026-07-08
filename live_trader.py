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
import threading
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
import discord_cards as dc  # v3.1.2: startup banner as a field-grid card
import offset_guard  # v3.2.3 Monday weekend-wake offset guard (shared, identity)
import soft_restart  # v3.2.3 soft self-update / restart-reconcile (shared, identity)
import break_hold    # v3.2.3 Feature D: break-and-hold filter (shared, identity)
import fp_guard      # v3.2.3 Feature E: FP exposure guard (shared, identity)
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
        'A3_1430_Overlap': 15,   # STALE (harmless): A3 cut from cfg.anchors 2026-07-02 per per-anchor P&L (Jun -$2,255 PF 0.68, Jul -$385) -- unreferenced lookup key, kept for a possible restore
        'A4_1640_NYopen': 30,
        'A5_1930_LateUS': 30,   # v3.3.8: 22:00 IST US-session anchor (like A4)
    }
    DEFER_WAIT_DEFAULT = 15

    # v3.0.5: anchor LATE-PLACEMENT recovery. ANCHOR_ONTIME_GRACE_S is the cutoff
    # (seconds after the scheduled time) beyond which a placement is tagged LATE;
    # normal defer + settle completes well under it, so on-time anchors never read
    # LATE. Within the cfg.anchor_late_window_min window, an unplaced anchor is
    # re-attempted at most every ANCHOR_LATE_RETRY_INTERVAL_S (matches the
    # stale-retry cadence; prevents per-tick re-attempt spam).
    ANCHOR_ONTIME_GRACE_S = 120
    ANCHOR_LATE_RETRY_INTERVAL_S = 30

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
    # v3.2.3 pinned weekend-wake guard constants (shared with offset_guard; these
    # are the spec-named knobs surfaced for observability -- the existing live
    # retry CADENCE above is unchanged, behavior-preserving).
    WEEKEND_GAP_HOURS = 24             # first-tick gap that marks a weekend wake
    EXPECTED_OFFSET   = 3              # MetaQuotes-Demo UTC+3
    OFFSET_RETRY_MAX  = 3              # spec retry budget (offset_guard default)
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

        # v3.3.0 per-position structured trace (the trail-lock-fix overhaul). One
        # greppable line per state change so a ticket's whole life is gapless and
        # the middle can never be silent again. Pure; the sink mirrors to the bot
        # log AND to telemetry at INFO so it reaches the operator's channel too.
        from position_telemetry import PositionTracer
        self.ptrace = PositionTracer(sink=self._ptrace_sink)
        # throttle for POSITION_HEARTBEAT (spec 1.3): per-ticket last-emit epoch.
        self._ptrace_hb_last: Dict = {}
        # v3.2.3: per-key Discord rate-limit clock (stop-through/trail alerts).
        self._discord_rl: Dict = {}
        # v3.2.3: post the RESUME/ADOPT/FINALIZE reconcile summary once after boot.
        self._reconcile_logged: bool = False

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
        # v3.0.5: per-anchor late-retry throttle (label -> last attempt UTC ts)
        self._last_anchor_attempt: Dict = {}
        # v3.0.6: in-flight rescue FLEET/LONE events (observer-only).
        # v3.1.7: now PERSISTED across restarts (rehydrated below) so an event
        # opened before a restart still finalizes + writes when its members close.
        self._rescue_events: Dict = {}            # event_id -> event record
        self._rescue_event_by_ticket: Dict = {}   # member ticket -> event_id
        self._rehydrate_rescue_events()
        # v3.2.1: ensure rescue_events.csv exists (header) so rescuestats always
        # reads a valid file and a path/permission issue surfaces at startup.
        try:
            _rescue_mod.ensure_rescue_events_csv(self.run_dir)
        except Exception as _e:
            log.warning(f"ensure_rescue_events_csv failed (non-fatal): {_e!r}")

        # Pause flag (set via /pause command)
        self.paused = False

        # v3.6.0 ENGINE SWITCHES — runtime per-engine flags, owned HERE (the config
        # keys are boot defaults only). Toggled live via Discord (/anchors on|off,
        # /rogue on|off; effective next tick, no restart) and PERSISTED in
        # run/state.json like the Rogue governors (p1_state snapshot/recover): on a
        # SAME-day restart the persisted state WINS, and a restored value that
        # differs from the boot default emits the ENGINE STATE OVERRIDE alert.
        # OFF = MANAGE-ONLY (no NEW entries for that engine; trails/exits/SL/EOD/
        # Friday-flatten/kill-switch continue on all open positions of both magics
        # -- OFF never orphans a leg).
        self.engines = {'anchors': bool(getattr(cfg, 'non_oco_enabled', True)),
                        'rogue': bool(getattr(cfg, 'rogue_enabled', True)),
                        'fetcher': bool(getattr(cfg, 'fetcher_enabled', True))}
        # the boot defaults, frozen at init, for the restore-override comparison
        # (cfg.rogue_enabled itself is mutated later by rogue.promote_on_boot).
        self._engine_boot_defaults = dict(self.engines)

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
        # v3.0.4 module receipt: confirm the new surfaces are loaded at startup.
        self.tele.info(
            "v3.0.4: timestamped alerts (ts_header, single source) + Firebase "
            "verifier `python bot.py verifyfb` online."
        )
        # v3.0.5 module receipt: anchor late-retry window + scheduled/actual times.
        self.tele.info(
            f"v3.0.5: anchor late-retry online — anchor_late_window_min="
            f"`{getattr(cfg, 'anchor_late_window_min', 0)}` "
            f"(missed anchors re-fire within the window; loud MISS after)."
        )
        # v3.0.6 module receipt: rescue fleet-event logger (observer only) + EOD
        # balance capture. No change to rescue/boost mechanics.
        self.tele.info(
            "v3.1.7: rescue event logger online — FLEET + LONE-leg, "
            "restart-persisted (no orphaned events), event_type + separate "
            "orig/boost P&L logged (rescue_events.csv + Firestore; "
            "`python bot.py rescuestats`)."
        )
        # v3.1.8 module receipt: tick-resolution backtester reuses these LIVE
        # rules by import (backtest == live). No live-behavior change.
        self.tele.info(
            "v3.1.8: tick backtester online — `python backtest/back_main.py "
            "YYYY-MM` (imports live strategy/anchors/rescue rules). STANDING RULE: "
            "every new feature must land in BOTH live AND backtest."
        )
        # v3.2.0 module receipt: lone-leg boosts NEVER fire at fill — per-tick
        # trigger fires RALLY (+$10 same dir) / RESCUE (-$10 opposite) via the
        # single canonical boosts.plan_boost_event; -$700 hard cap.
        self.tele.info(
            "v3.2.0: boost trigger FIXED — boosts fire only on a $10 move from the "
            "leg fill (RALLY/RESCUE), never at fill; per-tick; $10 SL + $3.50 trail; "
            "-$700 hard cap. One function for live/backtest/tests."
        )
        # v3.3.6: the AUREON_TEST_FORCE_MONDAY_A1 hook was REMOVED (it forced the
        # Monday cushion on ANY weekday in the SHARED resolver -> a leaked env var
        # would have placed weekday A1 an hour late). If it is still set in the
        # environment, announce loudly that it is now IGNORED so the operator knows
        # weekday A1 resolves correctly (02:30 broker) regardless.
        if os.environ.get('AUREON_TEST_FORCE_MONDAY_A1', '').strip().lower() \
                in ('1', 'true', 'yes', 'on'):
            self.tele.warn(
                "🧪 AUREON_TEST_FORCE_MONDAY_A1 is set but IGNORED (removed in "
                "v3.3.6) — the Monday A1 cushion now applies on Mondays ONLY; "
                "weekday A1 resolves at 02:30 broker / 05:00 IST."
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
            self.state['missed_anchors_today'] = []   # v3.0.5: reset late-window give-ups
            self._last_anchor_attempt = {}            # v3.0.5: clear late-retry throttle
            self.state['kill_switch_locked'] = False
            # v3.7.3: EVERYTHING resets at the broker day roll -- the anchors + account
            # profit lock / override / alert flags carry NOTHING into the new day (the day
            # P&L above is already zeroed; Rogue/Fetcher govs reset in their own new-day path).
            try:
                import daystops as _ds
                _ds.reset_day_state(self.state)
            except Exception:
                pass
            self.state['friday_flatten_done'] = False  # Friday weekend-hold-ban gate, daily reset
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

    def _start_discord_heartbeat(self):
        """v3.1.0: post a 💓 heartbeat CARD every discord_heartbeat_min minutes on
        a daemon thread (off the trading path). Dedup-aware: if nothing changed
        since the last beat it is skipped. Never raises; low priority."""
        dc_client = getattr(self.tele, 'discord', None)
        period_min = int(getattr(self.cfg, 'discord_heartbeat_min', 60))
        if dc_client is None or period_min <= 0:
            return
        import discord_cards as _dc

        def _loop():
            while not getattr(self, '_stop', False):
                time.sleep(period_min * 60)
                try:
                    eq = self._live_equity()
                    bal = self.state.get('day_start_equity')
                    open_n = len(getattr(self, 'shadow_positions', {}) or {})
                    pend_n = len(getattr(self, 'shadow_pendings', {}) or {})
                    anchors = " ".join(self.state.get('processed_anchors_today', []) or []) or "—"
                    last = f"day P&L ${self.state.get('daily_pnl', 0.0):+.2f}"
                    sig = (open_n, pend_n, anchors, round(float(eq or 0), 0))
                    dc_client.heartbeat(
                        _dc.card_heartbeat(bal, eq, open_n, pend_n, anchors, last),
                        signature=sig)
                except Exception as e:
                    log.warning(f"discord heartbeat failed (non-fatal): {e!r}")

        threading.Thread(target=_loop, name="discord-heartbeat", daemon=True).start()

    # ------------------------------------------------------------------------
    # Heartbeat, status, commands
    # ------------------------------------------------------------------------

    def _touch_heartbeat(self):
        with open(self.heartbeat_path, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())

    def _rl_ok(self, key: str, period_s: float = 60.0) -> bool:
        """True if `key` hasn't fired within period_s (then arms it). Used to
        rate-limit repetitive Discord alerts (stop-through re-arm, trail advance)
        so a per-bar event can't flood the channel. Never raises."""
        try:
            now = time.time()
            if now - self._discord_rl.get(key, 0.0) < period_s:
                return False
            self._discord_rl[key] = now
            return True
        except Exception:
            return True

    def _ptrace_sink(self, line: str):
        """Sink for the per-position structured trace: ALWAYS to the bot log (every
        line, every occurrence -- the file record is complete). Loud lines also go
        to Discord: a TELEMETRY_VIOLATION immediately/unrate-limited; a blocked
        phantom lock (👻) rate-limited to 1/60s/ticket so a stuck phantom can't
        flood. Must NEVER raise -- telemetry can't touch the trading loop."""
        try:
            log.info(line)
            if line.startswith("PTRACE TELEMETRY_VIOLATION"):
                self.tele.warn(f"🚨 {line}")
            elif line.startswith("PTRACE LOCK_REJECTED_PHANTOM"):
                tk = self._line_ticket(line)
                if self._rl_ok(f"phantom:{tk}", 60.0):
                    self.tele.warn(
                        f"👻 PHANTOM LOCK BLOCKED {tk} | {line.split('PTRACE ', 1)[-1]}")
            elif line.startswith("PTRACE LOCK_ARM"):
                tk = self._line_ticket(line)
                if self._rl_ok(f"lockarm:{tk}", 60.0):
                    self.tele.info(f"🔒 {line.split('PTRACE ', 1)[-1]}")
        except Exception:
            pass

    @staticmethod
    def _line_ticket(line: str) -> str:
        """Pull `ticket=<id>` out of a PTRACE line for per-ticket rate-limiting."""
        for tok in line.split():
            if tok.startswith("ticket="):
                return tok.split("=", 1)[1]
        return "?"

    def _maybe_position_heartbeat(self):
        """v3.3.0 spec 1.3: while any position is live, emit a low-frequency
        POSITION_HEARTBEAT (~60s) so 'what was happening between fill and exit' is
        always answerable even when no state changed. Reads the live tick once."""
        if not self.shadow_positions:
            return
        now = time.time()
        bid = ask = None
        try:
            tk = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
            if tk is not None:
                bid, ask = float(tk.bid), float(tk.ask)
        except Exception:
            bid = ask = None
        for ticket, sh in list(self.shadow_positions.items()):
            if now - self._ptrace_hb_last.get(ticket, 0.0) < 60.0:
                continue
            self._ptrace_hb_last[ticket] = now
            entry = float(sh.get('entry_price')) if sh.get('entry_price') is not None else None
            mf = sh.get('max_fav')
            floating = None
            if bid is not None and entry is not None:
                sgn = 1.0 if sh.get('side') == 'BUY' else -1.0
                ref = bid if sh.get('side') == 'BUY' else ask
                if ref is not None:
                    floating = round(sgn * (ref - entry) * self.cfg.contract_size
                                     * self.cfg.lot_size, 2)
            anc = sh.get('anchor_label')
            stack_size = sum(1 for s in self.shadow_positions.values()
                             if s.get('anchor_label') == anc)
            try:
                self.ptrace.heartbeat(
                    ticket, anc, side=sh.get('side'),
                    bid=bid, ask=ask, position_price=entry,
                    max_fav=mf, stop_price=sh.get('current_sl'),
                    boost_kind=('boost' if sh.get('boost') else None),
                    stack_size=stack_size, floating_pnl=floating,
                    active_lock_level=None)
            except Exception:
                pass

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
            # v3.6.0 engine switches (runtime state; config keys are boot defaults)
            "engines": {"anchors": self._engine_enabled('anchors'),
                        "rogue": self._engine_enabled('rogue')},
            # v3.7.4 per-engine realized day P&L + thresholds + lock state (display-only;
            # SAME source the daily stops read -- _engine_day_pnls / govs / daystops).
            "day_pnl_by_engine": self._day_pnl_by_engine_payload(),
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
            # v3.3.6: derive the next A1 (upcoming Monday -> 03:30 broker / 06:00 IST)
            # from the resolver instead of the stale hardcoded "A1 02:00 broker".
            try:
                status["next_anchor"] = self._next_a1_display()
            except Exception:
                status["next_anchor"] = "A1 (time unresolved)"
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
            args = c.get("args") or {}
            if cmd == "engine":
                # v3.6.0 /anchors on|off · /rogue on|off (queued by the watchdog).
                # Effective NEXT TICK (this consume runs at tick step 0); the bot
                # posts the confirm embed itself (engines state + counts per magic).
                try:
                    self._set_engine(str(args.get("engine", "")).lower(),
                                     str(args.get("action", "")).lower() == "on")
                except Exception as e:
                    log.warning(f"engine toggle command failed (non-fatal): {e!r}")
            elif cmd == "engines_status":
                # v3.6.0 /engines status · /anchors status · /rogue status
                try:
                    self._post_engines_status()
                except Exception as e:
                    log.warning(f"engines status command failed (non-fatal): {e!r}")
            elif cmd in ("anchors_flatten", "rogue_flatten", "fetcher_flatten"):
                # v3.6.0 confirm-gated per-magic flatten (see _handle_engine_flatten).
                try:
                    self._handle_engine_flatten(cmd.split("_", 1)[0],
                                                bool(args.get("confirm", False)))
                except Exception as e:
                    log.warning(f"{cmd} command failed (non-fatal): {e!r}")
            elif cmd == "flatten":
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
            elif cmd == "rogueseed":
                # Manual Rogue A1-mode seed at the CURRENT live tick (mid-day restart has no
                # A1 event to seed Fix 4). DEMO-only + rogue_a1_anchor_mode-only + ROGUE-only
                # (never an anchor 20260522 ticket) -- all enforced inside rogue.manual_seed
                # (+ the shared open-ticket / engine-off / market / kill rails). Fully
                # guarded: a seed error never breaks the tick loop.
                try:
                    import rogue as _rogue
                    _rogue.manual_seed(self, _rogue.seed_tick_price(self))
                except Exception:
                    pass
            elif cmd == "fetchseed":
                # Manual FETCHER re-seed at the CURRENT live tick: plant the anchor here so
                # trigger -> entry -> close -> re-anchor can be observed from a known point.
                # DEMO-only + FETCHER-only (never anchor 20260522 / Rogue 20260626), + the
                # SAME rails as rogueseed -- all enforced inside fetcher.manual_seed. Reuses
                # rogue.seed_tick_price (the shared sane/settled-tick read). Fully guarded.
                try:
                    import fetcher as _fetcher, rogue as _rogue
                    _fetcher.manual_seed(self, _rogue.seed_tick_price(self))
                except Exception:
                    pass
            elif cmd == "daylock_status":
                # v3.7.3 /daylock status -> per-engine day P&L vs both thresholds + lock
                # state for all three engines (+ the disabled-by-default account lock).
                try:
                    self._post_daylock_status()
                except Exception as e:
                    log.warning(f"daylock status failed (non-fatal): {e!r}")
            elif cmd == "daylock_override":
                # v3.7.3 /daylock anchors off | off -> clear the anchors profit lock OR the
                # account lock for the rest of the broker day (no same-day re-lock). The HARD
                # loss stop is NOT affected. Fully guarded.
                try:
                    self._daylock_override(str(args.get("which", "")).lower())
                except Exception as e:
                    log.warning(f"daylock override failed (non-fatal): {e!r}")

    def _eod_reached(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        eod = self._eod_datetime_utc(broker_date, self.cfg)
        return utc_now >= eod

    def _friday_flatten_reached(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        """Friday-only weekend-hold-ban cutoff (see config.py `friday_flatten_*`):
        True once `utc_now` reaches `cfg.friday_flatten_broker_hour` (a decimal
        broker hour, e.g. 22.5 = 22:30) on a Friday `broker_date`. False on any
        other weekday, or when `friday_flatten_enabled` is off (restores plain
        EOD-only behavior). The decimal hour is split into (hour, minute) and
        run through the SAME `anchor_datetime_utc` conversion `_eod_reached`
        uses, so this is not a new time-comparison idiom -- just a new cutoff
        instant on the existing one."""
        if not bool(getattr(self.cfg, 'friday_flatten_enabled', True)):
            return False
        if broker_date.weekday() != 4:  # Monday=0 .. Friday=4
            return False
        hour_f = float(getattr(self.cfg, 'friday_flatten_broker_hour', 22.5))
        hour = int(hour_f)
        minute = round((hour_f - hour) * 60)
        threshold = self._anchor_datetime_utc(
            broker_date, hour, self.cfg.broker_tz_offset_hours, minute)
        return utc_now >= threshold

    # anchor engine's magic (mt5_adapter.py place_market_order/place_stop_order
    # default `magic=20260522`); Rogue's is rogue.ROGUE_MAGIC (20260626).
    _FRIDAY_ANCHOR_MAGIC = 20260522

    def _friday_query_flat(self):
        """D-6: broker-verified flat check for the Friday weekend-hold-ban poll --
        queries MT5 DIRECTLY for this symbol's open positions + pending orders
        (never trusts shadow_positions/shadow_pendings, which is exactly the
        state a dead/reconnecting feed can desync from the broker). Returns
        (flat: bool, counts: dict) so a failed pass's alert can name what's
        still open. A query failure is NEVER treated as flat."""
        import rogue as _rogue
        try:
            positions = self.adapter.mt5.positions_get(symbol=self.cfg.symbol) or []
            pendings = self.adapter.mt5.orders_get(symbol=self.cfg.symbol) or []
        except Exception as e:
            return False, {'query_error': repr(e)}

        def _magic(o):
            try:
                return int(getattr(o, 'magic', -1))
            except (TypeError, ValueError):
                return -1
        import fetcher as _fetcher
        anchor_m, rogue_m = self._FRIDAY_ANCHOR_MAGIC, _rogue.ROGUE_MAGIC
        fetch_m = _fetcher.FETCHER_MAGIC
        _known = (anchor_m, rogue_m, fetch_m)
        counts = {
            'anchor_positions': sum(1 for p in positions if _magic(p) == anchor_m),
            'rogue_positions': sum(1 for p in positions if _magic(p) == rogue_m),
            'fetcher_positions': sum(1 for p in positions if _magic(p) == fetch_m),
            'other_positions': sum(1 for p in positions if _magic(p) not in _known),
            'anchor_pendings': sum(1 for o in pendings if _magic(o) == anchor_m),
            'rogue_pendings': sum(1 for o in pendings if _magic(o) == rogue_m),
            'fetcher_pendings': sum(1 for o in pendings if _magic(o) == fetch_m),
            'other_pendings': sum(1 for o in pendings if _magic(o) not in _known),
        }
        return (len(positions) == 0 and len(pendings) == 0), counts

    def _friday_resync_shadow_from_broker(self):
        """D-6: before each poll-flatten pass, make sure risk._flatten_all's
        per-ticket retry loop has something to iterate for EVERY broker-open
        anchor position/pending on this symbol -- not just whatever is still in
        shadow_positions/shadow_pendings. A minimal synthesized entry is enough
        (only the ticket + side/prices _flatten_all's close/cancel calls need).
        Never overwrites an already-tracked ticket. Rogue positions are
        explicitly EXCLUDED here -- Rogue's own open ticket is tracked
        separately (self._rogue['open']) and closed via
        rogue.force_close_open(), never via shadow_positions/_flatten_all;
        adopting a Rogue-magic ticket here would race a double-close attempt
        against force_close_open on the SAME ticket."""
        import rogue as _rogue
        try:
            positions = self.adapter.mt5.positions_get(symbol=self.cfg.symbol) or []
            pendings = self.adapter.mt5.orders_get(symbol=self.cfg.symbol) or []
        except Exception as e:
            log.warning(f"friday flatten resync: broker query failed: {e!r}")
            return
        for p in positions:
            tk = int(p.ticket)
            if tk in self.shadow_positions:
                continue
            if int(getattr(p, 'magic', -1)) == _rogue.ROGUE_MAGIC:
                continue
            side = 'BUY' if getattr(p, 'type', 0) == 0 else 'SELL'
            price_open = float(getattr(p, 'price_open', 0.0))
            self.shadow_positions[tk] = {
                'anchor_label': 'FRIDAY_FLATTEN_RESYNC', 'side': side,
                'entry_price': price_open, 'current_sl': float(getattr(p, 'sl', 0.0)),
                'tp_level': float(getattr(p, 'tp', 0.0)), 'max_fav': price_open,
                'fill_time': pd.Timestamp.now(tz='UTC').isoformat(), 'role': 'normal',
            }
        for o in pendings:
            tk = int(o.ticket)
            if tk in self.shadow_pendings:
                continue
            self.shadow_pendings[tk] = {
                'anchor_label': 'FRIDAY_FLATTEN_RESYNC',
                'side': 'BUY' if getattr(o, 'type', 0) in (2, 4) else 'SELL',
                'sibling_ticket': None,
                'entry_price': float(getattr(o, 'price_open', 0.0)),
                'rescue_on_fill': False,
            }

    def _friday_poll_flatten(self, broker_date: DateType, utc_now: pd.Timestamp):
        """D-6: poll-until-flat Friday weekend-hold-ban flatten. Called every
        tick once the Friday cutoff is reached and not yet broker-verified flat;
        internally rate-limited to at most one actual flatten+verify pass per
        cfg.friday_flatten_poll_seconds (default 30s) of BROKER WALL-CLOCK
        (utc_now), not tick/price freshness -- so a frozen/dead feed can never
        stall the poll (mirrors wait_until_market_open's same wall-clock-driven
        probe). NEVER single-shot: every pass re-syncs shadow bookkeeping from
        the broker, re-invokes _flatten_all (keeping its own bounded per-ticket
        retry) for the anchor engine + force-closes any open Rogue ticket, then
        VERIFIES flat by broker query across BOTH magics. friday_flatten_done
        (and the Discord confirm) only ever set on a broker-verified flat pass;
        a failed pass alerts and leaves state untouched so the next poll retries."""
        import rogue as _rogue
        poll_s = float(getattr(self.cfg, 'friday_flatten_poll_seconds', 30.0))
        last = self.state.get('friday_flatten_last_poll_utc')
        if last:
            try:
                elapsed = (utc_now - pd.Timestamp(last)).total_seconds()
            except Exception:
                elapsed = poll_s  # malformed timestamp -> don't get stuck; poll now
            if elapsed < poll_s:
                return
        self.state['friday_flatten_last_poll_utc'] = utc_now.isoformat()
        self._save_state()

        self._friday_resync_shadow_from_broker()
        if self.shadow_positions or self.shadow_pendings:
            # != "EOD" -> risk._flatten_all ALSO force-closes any open Rogue
            # ticket (see risk.py:176); the daily EOD flatten still passes
            # reason="EOD" deliberately, unchanged, to let Rogue ride per
            # rogue_flatten_at_eod.
            self._flatten_all(reason="FRIDAY_FLATTEN")
        try:
            _rogue.force_close_open(self, reason="FRIDAY_FLATTEN")
        except Exception:
            pass

        flat, counts = self._friday_query_flat()
        if flat:
            self.state['friday_flatten_done'] = True
            self._save_state()
            log.warning(
                f"FRIDAY FLATTEN — broker-verified FLAT (both magics) at "
                f"{self.cfg.friday_flatten_broker_hour} broker hour; no new "
                f"entries (anchor or Rogue) until Monday.")
            self.tele.warn(
                f"🗓️✅ *Friday flatten CONFIRMED flat* — 0 positions, 0 pendings "
                f"(anchor {self._FRIDAY_ANCHOR_MAGIC} + Rogue {_rogue.ROGUE_MAGIC}), "
                f"broker-verified. No new entries either engine until Monday.")
        else:
            log.error(f"FRIDAY FLATTEN pass FAILED — still not flat: {counts}; "
                     f"retrying in <= {poll_s:.0f}s.")
            self.tele.error(
                f"⚠️ *Friday flatten pass FAILED* — still open: {counts}. "
                f"Retrying every {poll_s:.0f}s until broker-verified flat.")

    def _friday_entries_blocked(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        """D-6: True from the instant the Friday flatten window opens until the
        next broker day (Monday) -- gates NEW entries for BOTH engines (anchor +
        Rogue), independent of _friday_poll_flatten's verify/latch progress. A
        thin, separately-testable wrapper around _friday_flatten_reached so the
        two _tick() call sites (anchor-due, Rogue drive()) can't drift apart."""
        return self._friday_flatten_reached(broker_date, utc_now)

    # ------------------------------------------------------------------------
    # v3.6.0 ENGINE SWITCHES — runtime read/toggle + the shared entries-blocked
    # seams (effective_block = friday_window OR engine_disabled, per engine).
    # ------------------------------------------------------------------------

    def _engine_enabled(self, engine: str) -> bool:
        """Runtime engine-switch read ('anchors' | 'rogue' | 'fetcher'). GUARDED: a
        trader without the engines dict (selftest stubs, old snapshots) reads ENABLED --
        the switches can only ever REMOVE behavior, never invent it."""
        eng = getattr(self, 'engines', None)
        if not isinstance(eng, dict):
            return True
        return bool(eng.get(engine, True))

    def _set_engine(self, engine: str, on: bool, source: str = "Discord"):
        """Flip a runtime engine switch (effective next tick, no restart), persist it
        to run/state.json (p1_state, like the governors), and post the confirm embed
        (both engines' state + open-position count per magic). Guarded."""
        if engine not in ('anchors', 'rogue', 'fetcher'):
            return
        prev = self._engine_enabled(engine)
        self.engines[engine] = bool(on)
        mode = ('entries live' if on else
                'MANAGE-ONLY: no new entries; trails/exits/SL/EOD/kill-switch '
                'continue on open positions')
        log.warning(f"ENGINE SWITCH: {engine} {'ON' if on else 'OFF'} "
                    f"(was {'ON' if prev else 'OFF'}, via {source}) — {mode}")
        try:
            import p1_state as _p1
            _p1.save(self, force=True)   # a toggle must survive a restart (paper too)
        except Exception:
            pass
        _glyph = {'anchors': '⚓', 'rogue': '🦏', 'fetcher': '🪣'}.get(engine, '⚙️')
        self._post_engines_status(
            note=f"{_glyph} "
                 f"`/{engine} {'on' if on else 'off'}` applied — effective next tick"
                 + ("" if on else " (manage-only: existing positions keep trailing)"))

    def _anchor_entries_blocked(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        """v3.6.0 shared entries-blocked seam for the ANCHOR engine (the PR-89 D-6
        seam, extended): effective_block = friday_window OR engine_disabled. Gates
        NEW straddle placement only -- management of open positions never routes
        through here (OFF never orphans a leg)."""
        return (self._friday_entries_blocked(broker_date, utc_now)
                or not self._engine_enabled('anchors'))

    def _rogue_entries_blocked(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        """v3.6.0 shared entries-blocked seam for the ROGUE engine: effective_block =
        friday_window OR engine_disabled. Feeds drive(allow_new_entries=...) -- with
        it blocked, drive() still trail-manages and books closes on an existing open
        Rogue position (manage-only), it just takes no seed/chain/reversal entry.
        v3.7.3: the (inert-by-default) account-level day lock also blocks new Rogue risk."""
        return (self._friday_entries_blocked(broker_date, utc_now)
                or not self._engine_enabled('rogue')
                or bool(getattr(self, '_account_locked', lambda: False)()))

    def _fetcher_entries_blocked(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        """v3.7.0 shared entries-blocked seam for the FETCHER engine: effective_block =
        friday_window OR engine_disabled. Feeds fetcher.drive(allow_new_entries=...) -- with
        it blocked, drive() still books a close on an existing open Fetcher position
        (manage-only), it just takes no new $5-move entry. Mirrors _rogue_entries_blocked.
        v3.7.3: the (inert-by-default) account-level day lock also blocks new Fetcher risk."""
        return (self._friday_entries_blocked(broker_date, utc_now)
                or not self._engine_enabled('fetcher')
                or bool(getattr(self, '_account_locked', lambda: False)()))

    # ------------------------------------------------------------------------
    # v3.7.3 ANCHORS-engine daily stops + the (inert) account-level lock
    # ------------------------------------------------------------------------
    def _engine_day_pnls(self):
        """(anchors, rogue, fetcher) realized day P&L. Anchors = state['daily_pnl'] (magic
        20260522, already EXCLUDES Rogue/Fetcher); Rogue/Fetcher from their governors.
        Guarded -- a missing piece reads $0."""
        st = getattr(self, 'state', {}) or {}
        anchors = float(st.get('daily_pnl', 0.0) or 0.0)
        rogue = float(((getattr(self, '_rogue', None) or {}).get('gov') or {}).get('day_pnl', 0.0) or 0.0)
        fetcher = float(((getattr(self, '_fetcher', None) or {}).get('gov') or {}).get('day_pnl', 0.0) or 0.0)
        return anchors, rogue, fetcher

    def _anchors_daystop(self):
        """(blocked, reason, kind) for the ANCHORS engine day stop -- LOSS halt (hard) or
        PROFIT lock (soft, /daylock-overridable). Latches the profit lock + fires the
        one-time alert when it first engages. Guarded -> (False,'ok','')."""
        try:
            import daystops as _ds
            dp = float((self.state or {}).get('daily_pnl', 0.0) or 0.0)
            if _ds.latch_profit(dp, self.cfg, self.state):
                self._anchors_profit_alert(dp)
            return _ds.anchors_daystop(dp, self.cfg, self.state)
        except Exception:
            return False, 'ok', ''

    def _anchors_daystop_blocked(self) -> bool:
        """True iff the ANCHORS engine may take NO new risk today -- its own loss halt /
        profit lock OR the account lock. Consulted by the straddle-skip + the boost gate.
        GUARDED: a stub without state reads NOT blocked. Manage-only never routes here."""
        try:
            blocked, _r, _k = self._anchors_daystop()
            return bool(blocked) or self._account_locked()
        except Exception:
            return False

    def _anchors_profit_alert(self, day_pnl):
        """One-time loud PROFIT-LOCK alert for the anchors engine (log + Discord). Guarded."""
        try:
            if (self.state or {}).get('anchors_profit_alerted'):
                return
            self.state['anchors_profit_alerted'] = True
            ps = float(getattr(self.cfg, 'anchors_daily_profit_stop', 0.0))
            msg = (f"⚓ [ANCHORS] DAY PROFIT STOP +${float(day_pnl):.0f} >= ${ps:.0f} — "
                   f"entries locked (/daylock anchors off to override)")
            log.warning(msg)
            try:
                self.tele.warn(msg)
            except Exception:
                pass
            try:
                import p1_state as _p1
                _p1.save(self, force=True)
            except Exception:
                pass
        except Exception:
            pass

    def _account_locked(self) -> bool:
        """True iff the (inert-by-default) account-level lock is engaged: combined realized
        P&L across all magics >= account_daily_profit_stop_pct x day-start equity. pct == 0
        -> always False (owner default). Latches + one-time alert on engage. Guarded."""
        try:
            import daystops as _ds
            a, r, f = self._engine_day_pnls()
            combined = a + r + f
            dse = float((self.state or {}).get('day_start_equity')
                        or getattr(self.cfg, 'starting_balance', 0.0))
            blocked, _reason = _ds.account_daystop(combined, dse, self.cfg, self.state)
            if blocked and not self.state.get('account_profit_locked'):
                self.state['account_profit_locked'] = True
                if not self.state.get('account_profit_alerted'):
                    self.state['account_profit_alerted'] = True
                    pct = float(getattr(self.cfg, 'account_daily_profit_stop_pct', 0.0))
                    msg = (f"💰 ACCOUNT DAY LOCK — combined +${combined:.0f} >= {pct*100:g}% "
                           f"of day-start equity — all engines manage-only "
                           f"(/daylock off to override)")
                    log.warning(msg)
                    try:
                        self.tele.warn(msg)
                    except Exception:
                        pass
            return bool(blocked)
        except Exception:
            return False

    def _anchors_daystop_skip(self, label, anchor_utc, utc_now):
        """A scheduled anchor came DUE while the anchors engine is halted/locked: mark it
        missed (skip ONCE per anchor per day, no late-window retry) with a loud one-time
        message. Manage-only -- open legs untouched. Guarded."""
        try:
            missed = self.state.setdefault('missed_anchors_today', [])
            if label in missed:
                return
            missed.append(label)
            _b, _reason, kind = self._anchors_daystop()
            if not _b and self._account_locked():
                kind = 'account'
            a, _r, _f = self._engine_day_pnls()
            names = {'loss': 'loss halt', 'profit': 'profit lock', 'account': 'account lock'}
            msg = (f"⚓ {label} skipped: anchors {names.get(kind, kind or 'day stop')} "
                   f"(day ${a:+.0f} vs profit "
                   f"${float(getattr(self.cfg, 'anchors_daily_profit_stop', 0.0)):.0f} / "
                   f"loss ${float(getattr(self.cfg, 'anchors_daily_loss_stop', 0.0)):.0f}) "
                   f"— no new straddle today")
            log.warning(msg)
            try:
                self.tele.warn(msg)
            except Exception:
                pass
            self._save_state()
        except Exception as e:
            log.warning(f"anchors daystop skip failed (non-fatal): {e!r}")

    def _daylock_lines(self):
        """The /daylock status lines (also reused in the /engines embed). Guarded -> []."""
        try:
            import daystops as _ds
            a, r, f = self._engine_day_pnls()
            dse = float((self.state or {}).get('day_start_equity')
                        or getattr(self.cfg, 'starting_balance', 0.0))
            return _ds.render_status(a, r, f, a + r + f, dse, self.cfg, self.state)
        except Exception:
            return []

    @staticmethod
    def _gov_lock_label(gov):
        """PURE: a Rogue/Fetcher governor's lock state string for the /status display --
        'LOSS-HALTED' | 'override' | 'PROFIT-LOCKED' | 'active'. Read from the persisted
        latches, NOT recomputed. Loss ranks first (mirrors can_enter)."""
        gov = gov or {}
        if gov.get('loss_stopped'):
            return 'LOSS-HALTED'
        if gov.get('profit_override'):
            return 'override'
        if gov.get('profit_locked'):
            return 'PROFIT-LOCKED'
        return 'active'

    def _day_pnl_by_engine_payload(self):
        """DISPLAY-ONLY structured per-engine realized day P&L + thresholds + lock state for
        the /status card. SINGLE SOURCE OF TRUTH: reuses _engine_day_pnls() -- the SAME
        numbers the daily stops act on -- and reads each lock state from the govs /
        _anchors_daystop (never recomputed). Read-only; guarded -> {} on any error."""
        try:
            a, r, f = self._engine_day_pnls()
            cfg = self.cfg
            # anchors lock state via the daystop reader (loss ranks first); override maps
            # to 'override' since the reader reports NOT blocked once overridden.
            _blk, _reason, akind = self._anchors_daystop()
            if akind == 'loss':
                anchors_lock = 'LOSS-HALTED'
            elif (self.state or {}).get('anchors_profit_override'):
                anchors_lock = 'override'
            elif akind == 'profit':
                anchors_lock = 'PROFIT-LOCKED'
            else:
                anchors_lock = 'active'
            rgov = (getattr(self, '_rogue', None) or {}).get('gov') or {}
            fgov = (getattr(self, '_fetcher', None) or {}).get('gov') or {}
            dse = float((self.state or {}).get('day_start_equity')
                        or getattr(cfg, 'starting_balance', 0.0))
            kill_th = round(float(getattr(cfg, 'daily_loss_pct', 0.0)) * dse, 2)
            return {
                'anchors': {'pnl': round(a, 2),
                            'profit': float(getattr(cfg, 'anchors_daily_profit_stop', 0.0)),
                            'loss': float(getattr(cfg, 'anchors_daily_loss_stop', 0.0)),
                            'lock': anchors_lock},
                'rogue': {'pnl': round(r, 2),
                          'profit': float(getattr(cfg, 'rogue_daily_profit_stop', 0.0)),
                          'loss': float(getattr(cfg, 'rogue_daily_loss_stop', 0.0)),
                          'lock': self._gov_lock_label(rgov)},
                'fetcher': {'pnl': round(f, 2),
                            'profit': float(getattr(cfg, 'fetcher_daily_profit_stop', 0.0)),
                            'loss': float(getattr(cfg, 'fetcher_daily_loss_stop', 0.0)),
                            'lock': self._gov_lock_label(fgov)},
                'account': {'pnl': round(a + r + f, 2), 'kill_threshold': kill_th,
                            'kill_pct': round(float(getattr(cfg, 'daily_loss_pct', 0.0)) * 100.0, 1)},
            }
        except Exception:
            return {}

    def _post_daylock_status(self, note: str = ""):
        """The /daylock status embed: each engine's realized day P&L vs BOTH thresholds +
        lock/halt state, plus the (disabled-by-default) account lock. Guarded."""
        try:
            lines = ([note] if note else []) + self._daylock_lines()
            self.tele.send("🔒 *DAY LOCKS*\n" + "\n".join(lines), Severity.INFO)
        except Exception as e:
            log.warning(f"daylock status post failed (non-fatal): {e!r}")

    def _daylock_override(self, which: str):
        """Clear the ANCHORS profit lock (which='anchors') or the ACCOUNT lock
        (which='account') for the rest of the broker day -- no same-day re-lock. The HARD
        loss stop is NEVER cleared. Loud one-time alert. Guarded."""
        import daystops as _ds
        if which == 'anchors':
            _b, _reason, kind = self._anchors_daystop()
            if kind == 'loss':
                self.tele.warn("⚓ /daylock anchors off IGNORED — anchors is in a LOSS HALT "
                               "(not overridable), not a profit lock.")
                return
            self.state[_ds.K_ANCHORS_OVERRIDE] = True
            a, _r, _f = self._engine_day_pnls()
            msg = (f"⚓ [ANCHORS] PROFIT STOP OVERRIDDEN BY /daylock anchors off "
                   f"(day ${a:+.0f}) — entries re-enabled for the day (no re-lock)")
        elif which == 'account':
            self.state[_ds.K_ACCOUNT_OVERRIDE] = True
            a, r, f = self._engine_day_pnls()
            msg = (f"💰 ACCOUNT LOCK OVERRIDDEN BY /daylock off (combined ${a + r + f:+.0f}) "
                   f"— all engines re-enabled for the day (no re-lock)")
        else:
            self.tele.info("Usage: `/daylock anchors off` or `/daylock off`")
            return
        log.warning(msg)
        try:
            self.tele.warn(msg)
        except Exception:
            pass
        try:
            import p1_state as _p1
            _p1.save(self, force=True)
        except Exception:
            pass
        self._post_daylock_status()

    def _open_counts_per_magic(self):
        """Broker-truth open-position/pending counts per magic (anchor 20260522 /
        Rogue 20260626 / other) for the engines confirm embed. Reuses the D-6
        broker query. Paper mode / query failure degrade to shadow-book counts."""
        try:
            flat, counts = self._friday_query_flat()
            if 'query_error' not in counts:
                return counts
        except Exception:
            pass
        return {'anchor_positions': len(getattr(self, 'shadow_positions', {}) or {}),
                'rogue_positions': (1 if (getattr(self, '_rogue', None) or {}).get('open')
                                    else 0),
                'fetcher_positions': (1 if (getattr(self, '_fetcher', None) or {}).get('open')
                                      else 0),
                'anchor_pendings': len(getattr(self, 'shadow_pendings', {}) or {})}

    def _post_engines_status(self, note: str = ""):
        """The /engines status (and toggle-confirm) embed: BOTH engines' runtime
        state + open-position count per magic. Guarded; never raises onto the tick."""
        try:
            import rogue as _rogue
            import fetcher as _fetcher
            counts = self._open_counts_per_magic()
            snap = {
                "Anchors engine": ("🟢 ON" if self._engine_enabled('anchors')
                                   else "🔴 OFF (manage-only)"),
                "Rogue engine": ("🟢 ON" if self._engine_enabled('rogue')
                                 else "🔴 OFF (manage-only)"),
                "Fetcher engine": ("🟢 ON" if self._engine_enabled('fetcher')
                                   else "🔴 OFF (manage-only)"),
                f"Open {self._FRIDAY_ANCHOR_MAGIC} (anchor)":
                    f"{counts.get('anchor_positions', 0)} pos / "
                    f"{counts.get('anchor_pendings', 0)} pend",
                f"Open {_rogue.ROGUE_MAGIC} (Rogue)":
                    f"{counts.get('rogue_positions', 0)} pos / "
                    f"{counts.get('rogue_pendings', 0)} pend",
                f"Open {_fetcher.FETCHER_MAGIC} (Fetcher)":
                    f"{counts.get('fetcher_positions', 0)} pos / "
                    f"{counts.get('fetcher_pendings', 0)} pend",
                "Seed fallback": str(getattr(self.cfg, 'rogue_seed_fallback',
                                             'a1_time_snapshot')),
            }
            # v3.7.3: fold the per-engine day-lock states into the engines embed.
            try:
                for _ln in self._daylock_lines():
                    _k, _, _v = str(_ln).partition(': ')
                    if _v:
                        snap[f"Day {_k}"] = _v
            except Exception:
                pass
            lines = ([note] if note else []) + [
                f"{k}: {v}" for k, v in snap.items()]
            self.tele.send("⚙️ *ENGINES*\n" + "\n".join(lines), Severity.INFO,
                           card=dc.card_status(snap))
        except Exception as e:
            log.warning(f"engines status post failed (non-fatal): {e!r}")

    def _handle_engine_flatten(self, engine: str, confirm: bool):
        """Confirm-gated per-magic flatten. Bare `/anchors|/rogue flatten` replies
        with the live position count and asks for the confirm; `... flatten confirm`
        executes -- ANCHORS: risk._flatten_all scoped to the anchor engine (magic
        20260522 book; the Rogue force-close inside _flatten_all is skipped);
        ROGUE: rogue.force_close_open + rogue.cancel_pendings (magic 20260626 only).
        Neither touches the other engine's tickets."""
        import rogue as _rogue
        counts = self._open_counts_per_magic()
        if engine == 'anchors':
            n = counts.get('anchor_positions', 0)
            n_pend = counts.get('anchor_pendings', 0)
            if not confirm:
                self.tele.warn(
                    f"⚓ */anchors flatten* — {n} open position(s), {n_pend} pending(s) "
                    f"on magic {self._FRIDAY_ANCHOR_MAGIC}. NOT closed. "
                    f"Send `/anchors flatten confirm` to execute.")
                return
            self.tele.warn(f"⚓🚨 /anchors flatten CONFIRMED — closing {n} position(s) "
                           f"+ {n_pend} pending(s) on magic {self._FRIDAY_ANCHOR_MAGIC} "
                           f"(Rogue untouched)")
            self._flatten_all(reason="AnchorsFlatten", scope="ANCHORS")
        elif engine == 'rogue':
            n = counts.get('rogue_positions', 0)
            n_pend = counts.get('rogue_pendings', 0)
            if not confirm:
                self.tele.warn(
                    f"🦏 */rogue flatten* — {n} open position(s), {n_pend} pending(s) "
                    f"on magic {_rogue.ROGUE_MAGIC}. NOT closed. "
                    f"Send `/rogue flatten confirm` to execute.")
                return
            self.tele.warn(f"🦏🚨 /rogue flatten CONFIRMED — closing {n} position(s) "
                           f"+ cancelling {n_pend} pending(s) on magic "
                           f"{_rogue.ROGUE_MAGIC} (anchors untouched)")
            try:
                _rogue.force_close_open(self, reason="ManualFlatten")
            except Exception as e:
                log.warning(f"rogue flatten close failed (non-fatal): {e!r}")
            try:
                _rogue.cancel_pendings(self)
            except Exception as e:
                log.warning(f"rogue flatten pending-cancel failed (non-fatal): {e!r}")
        elif engine == 'fetcher':
            import fetcher as _fetcher
            n = counts.get('fetcher_positions', 0)
            n_pend = counts.get('fetcher_pendings', 0)
            if not confirm:
                self.tele.warn(
                    f"🪣 */fetcher flatten* — {n} open position(s), {n_pend} pending(s) "
                    f"on magic {_fetcher.FETCHER_MAGIC}. NOT closed. "
                    f"Send `/fetcher flatten confirm` to execute.")
                return
            self.tele.warn(f"🪣🚨 /fetcher flatten CONFIRMED — closing {n} position(s) "
                           f"+ cancelling {n_pend} pending(s) on magic "
                           f"{_fetcher.FETCHER_MAGIC} (anchors + Rogue untouched)")
            # scoped flatten (magic 20260707 ONLY) via risk._flatten_all's FETCHER scope.
            self._flatten_all(reason="FetcherFlatten", scope="FETCHER")

    # ------------------------------------------------------------------------
    # v3.0.0 commit 4: weekend self-sleep + Monday auto-resume
    # ------------------------------------------------------------------------

    def _market_closed_now(self) -> bool:
        """Cheap probe: True if the broker's last tick is >1h old (weekend or a
        holiday). False on any error -- never blocks trading on a probe failure.

        E-12: a probe failure is usually the feed/subscription dropping (the
        2026-06-30 'symbol not subscribed' storm), not a closed market. The
        feed watchdog (re-subscribe + throttled FEED DOWN alert) is driven from
        here; a successful probe ends any in-flight feed-death episode."""
        try:
            server_utc = self.adapter.server_time_utc()
            age = (pd.Timestamp.now(tz='UTC') - server_utc).total_seconds()
            self._feed_watchdog_ok()
            return age > 3600
        except Exception as e:
            self._feed_watchdog_fail(e)
            return False

    def _feed_watchdog_ok(self):
        """A market-closed probe SUCCEEDED -- the feed is alive. End any feed-death
        episode and (once) announce recovery. No-op until a failure has created the
        watchdog, and silent unless the watchdog is enabled."""
        wd = getattr(self, '_feed_wd', None)
        if wd is None:
            return
        recovered = wd.on_success()
        if recovered and bool(getattr(self.cfg, 'feed_watchdog_enabled', True)):
            log.warning("FEED RECOVERED — market-closed probe read a tick again.")
            try:
                self.tele.info("✅ *FEED RECOVERED* — ticks live again; resuming "
                               "normal operation.")
            except Exception:
                pass

    def _feed_watchdog_fail(self, err):
        """A market-closed probe FAILED ('not subscribed'). Drive the watchdog: throttle
        the warning, attempt a re-subscribe on the backoff cadence, and fire ONE FEED DOWN
        alert after feed_recover_max_tries failed attempts. When the watchdog is DISABLED
        this emits the exact pre-fix warning line every call (byte-identical)."""
        import time as _time
        import feed_watchdog as _fw
        wd = getattr(self, '_feed_wd', None)
        if wd is None:
            wd = _fw.FeedWatchdog()
            self._feed_wd = wd
        act = wd.on_failure(self.cfg, _time.monotonic())
        if not bool(getattr(self.cfg, 'feed_watchdog_enabled', True)):
            # Pre-watchdog behavior, unchanged.
            log.warning(f"market-closed probe failed: {err}")
            return
        if act.warn:
            log.warning(
                f"market-closed probe failed ({act.fails} consecutive, blind "
                f"~{act.blind_s / 60.0:.1f}m): {err}")
        if act.resubscribe:
            ok = False
            try:
                ok = bool(self.adapter.resubscribe())
            except Exception as e:
                log.warning(f"feed re-subscribe raised: {e!r}")
            log.warning(
                f"FEED re-subscribe attempt {act.attempt}/"
                f"{int(getattr(self.cfg, 'feed_recover_max_tries', 5))} for "
                f"{self.cfg.symbol} -> {'select OK' if ok else 'FAILED'}")
        if act.alert:
            try:
                self.tele.critical(
                    f"🚨 *FEED DOWN — bot blind*\n"
                    f"No ticks for ~{act.blind_s / 60.0:.0f} min "
                    f"({act.fails} failed probes, {act.attempt} re-subscribe attempts).\n"
                    f"Symbol `{self.cfg.symbol}` not subscribed — bot cannot trade or "
                    f"trail until the feed returns. Check the MT5 terminal / Market Watch.")
            except Exception:
                pass
        # Fix 4 (E-12) LEVEL 2: re-subscribe exhausted (or blind > threshold) -> full MT5
        # reinit in-process. A fresh tick ends the episode on the next probe (on_success).
        if act.reinit:
            self._feed_reinit(act)
        # Fix 4 (E-12) LEVEL 3: reinit exhausted -> controlled self-restart (persist + exit 42).
        # Gated on feed_selfrestart_enabled AND the market-closed guard inside _feed_self_restart.
        if act.self_restart:
            self._feed_self_restart(act)

    def _feed_reinit(self, act):
        """Fix 4 Level 2 (E-12): drive a full in-process MT5 reinit and announce it. A True
        return means a fresh tick was confirmed; the next market-closed probe then reads a
        tick and _feed_watchdog_ok posts FEED RECOVERED. Guarded."""
        n_max = int(getattr(self.cfg, 'feed_reinit_max_tries', 2))
        try:
            self.tele.critical(
                f"🔧 *FEED REINIT attempt {act.reinit_attempt}/{n_max}* — re-subscribe "
                f"exhausted / blind ~{act.blind_s / 60.0:.0f} min. Full MT5 "
                f"shutdown→initialize→select→verify-tick now.")
        except Exception:
            pass
        ok = False
        try:
            ok = bool(self.adapter.reinit())
        except Exception as e:
            log.error(f"feed reinit raised: {e!r}")
        log.warning(f"FEED REINIT attempt {act.reinit_attempt}/{n_max} -> "
                    f"{'FRESH TICK' if ok else 'still dead'}")
        if ok:
            try:
                self.tele.success("✅ *FEED REINIT OK* — MT5 reconnected and a fresh tick "
                                  "is flowing. Resuming normal operation.")
            except Exception:
                pass

    def _feed_self_restart(self, act):
        """Fix 4 Level 3 (E-12): controlled self-restart when the feed is dead and both MT5
        reinits failed. NEVER restarts when the market is closed (the weekend probe owns that
        case) or when feed_selfrestart_enabled is False. Persists the P1 state, posts a
        Discord notice, releases the PID lock, and exits with code 42 so run_aureon.bat /
        Task Scheduler relaunches. Open positions stay protected by their broker SL."""
        if not bool(getattr(self.cfg, 'feed_selfrestart_enabled', True)):
            log.warning("FEED self-restart requested but feed_selfrestart_enabled=False — "
                        "staying up; feed stays escalated at Level 2.")
            return
        if self._weekend_by_clock():
            log.warning("FEED self-restart suppressed — market looks closed by clock "
                        "(weekend). The weekend deep-sleep will handle the wake.")
            return
        try:
            import p1_state as _p1
            _p1.save(self, force=True)
        except Exception:
            pass
        try:
            self.tele.critical(
                f"🔁 *SELF-RESTART: feed dead* — MT5 reinit failed after ~"
                f"{act.blind_s / 60.0:.0f} min blind. Persisting state and exiting (42) so "
                f"the launcher relaunches. Open positions stay protected by broker SL.")
        except Exception:
            pass
        try:
            self._release_pid_lock()
        except Exception:
            pass
        try:
            self.tele.stop()
        except Exception:
            pass
        import sys as _sys
        log.error("SELF-RESTART (feed dead): exiting with code 42 for the relaunch loop.")
        _sys.exit(42)

    def _weekend_by_clock(self, utc_now=None) -> bool:
        """Coarse weekend guard for the Level-3 self-restart (used when the feed can't be
        read so a tick-age probe is impossible): XAUUSD is closed Fri ~21:00 UTC → Sun
        ~21:00 UTC. Returns True inside that window so a feed-death self-restart is
        suppressed when the market is simply closed. Clock-only; guarded."""
        try:
            u = utc_now or pd.Timestamp.now(tz='UTC')
            dow = int(u.dayofweek)       # Mon=0 .. Sat=5, Sun=6
            h = int(u.hour)
            if dow == 5:                 # Saturday: closed all day
                return True
            if dow == 6 and h < 21:      # Sunday before ~21:00 UTC open
                return True
            if dow == 4 and h >= 21:     # Friday after ~21:00 UTC close
                return True
            return False
        except Exception:
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
            # v3.2.3 additive telemetry: every detection is logged + visible.
            confirmed = bool(ok) and off is not None and int(off) == expected
            try:
                self.ptrace.offset_detect(
                    derived_offset=(int(off) if off is not None else None),
                    expected_offset=expected,
                    result=('CONFIRMED' if confirmed else
                            ('BLOCKED' if attempt >= self.OFFSET_VALIDATE_RETRIES else 'RETRY')),
                    attempt=attempt, gap_since_last_tick=None)
            except Exception:
                pass
            if confirmed:
                self.offset_validated = True
                # v3.3.6: derive A1's resolved time from the resolver so this Monday
                # all-clear reports the TRUE scheduled time (06:00 IST via the Monday
                # 03:30 broker cushion) instead of the stale hardcoded '0500'/5:00.
                try:
                    _a = self.cfg.anchors[0]
                    _bd = self._broker_date(pd.Timestamp.now(tz="UTC"))
                    _rh, _rm, _ih, _im = self._resolved_anchor_ist_hm(_a[0], _bd, _a[1], _a[2])
                    _a1_ist_hhmm = f"{_ih:02d}{_im:02d}"
                    _a1_ist_disp = f"{_ih:02d}:{_im:02d} IST ({_rh:02d}:{_rm:02d} broker)"
                except Exception:
                    _a1_ist_hhmm, _a1_ist_disp = '0500', '05:00 IST'
                self.tele.success(
                    f"✅ Monday wake: broker offset confirmed +{int(off)}h "
                    f"(attempt {attempt}/{self.OFFSET_VALIDATE_RETRIES}, {reason}).")
                # A1 RESOLVED -- Monday-morning all-clear (positive confirmation).
                try:
                    self.ptrace.anchor_time_resolved(
                        scheduled_ist=_a1_ist_hhmm, offset_used=int(off),
                        result='CONFIRMED')
                except Exception:
                    pass
                self.tele.success(
                    f"🟢 A1 RESOLVED | scheduled {_a1_ist_disp} | offset +{int(off)}h "
                    f"({reason}).")
                return True
            # mismatch / not-yet-confirmed: loud BEFORE A1 places.
            try:
                self.ptrace.offset_mismatch(
                    derived=(int(off) if off is not None else None),
                    expected=expected, retry_count=attempt)
                self.ptrace.violation(None, 'A1', 'offset_mismatch',
                                      derived=(int(off) if off is not None else None),
                                      expected=expected, retry_count=attempt)
            except Exception:
                pass
            self.tele.warn(
                f"⛔ OFFSET MISMATCH A1 | derived {off} vs expected +{expected} "
                f"| retry {attempt}/{self.OFFSET_VALIDATE_RETRIES}")
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

    def _soft_update_available(self) -> bool:
        """True iff origin has commits ahead of HEAD (ff-only). Guarded subprocess;
        any failure (no git, offline) -> False (never self-restart on uncertainty).
        Pure read -- does NOT pull. The relaunch path validates before deploying."""
        try:
            import subprocess
            root = os.path.dirname(os.path.abspath(__file__))
            subprocess.run(["git", "fetch", "--quiet", "origin"], cwd=root,
                           timeout=30, check=False)
            local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                   capture_output=True, text=True, timeout=10)
            remote = subprocess.run(["git", "rev-parse", "@{u}"], cwd=root,
                                    capture_output=True, text=True, timeout=10)
            return (local.returncode == 0 and remote.returncode == 0
                    and local.stdout.strip() != remote.stdout.strip())
        except Exception as e:
            log.warning(f"soft-update check failed (non-fatal): {e!r}")
            return False

    def _perform_soft_restart(self, reason: str = "update") -> bool:
        """SOFT restart: persist full state + hand off to the watchdog WITHOUT
        touching any position. Positions live on the broker and are reconciled on
        the next boot (RESUME/ADOPT/FINALIZE). NEVER flattens (spec
        NEVER_FLATTEN_ON_UPDATE). Writes a restart-request the watchdog relaunches
        from; returns True once the clean handoff is recorded. Does NOT itself kill
        the process (the watchdog owns relaunch -> measured downtime < 10s)."""
        import soft_restart as _soft
        try:
            n_pos = len(self.shadow_positions)
            n_pend = len(self.shadow_pendings)
            snap = _soft.snapshot_summary(self.shadow_positions, self.shadow_pendings,
                                          self._rescue_events)
            try:
                self.ptrace.soft_restart_snapshot(**snap)
            except Exception:
                pass
            self.tele.info(
                f"💾 SOFT RESTART | {n_pos} positions persisted | NOT flattening "
                f"({reason})")
            # Persist EVERYTHING before handoff (positions stay open on the broker).
            self._save_state()
            plan = _soft.soft_exit_plan(self.shadow_positions.keys())
            try:
                self.ptrace.soft_restart_exit(clean=True,
                                              positions_left_open=len(plan["left_open"]),
                                              gap_start_ts=time.time())
            except Exception:
                pass
            # Hand off: a restart-request file the watchdog acts on (relaunch via the
            # venv python). We deliberately do NOT close/modify any position/pending.
            try:
                with open(os.path.join(self.run_dir, "restart.request"), "w") as f:
                    f.write(json.dumps({"reason": reason, "ts": time.time(),
                                        "positions_left_open": len(plan["left_open"])}))
            except Exception as e:
                log.warning(f"restart.request write failed: {e!r}")
            return True
        except Exception as e:
            log.warning(f"soft restart handoff failed (non-fatal): {e!r}")
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
                # v3.3.6: derive A1's time (broker + IST) from the resolver so the
                # readiness line shows the TRUE resolved time -- 06:00 IST on Mondays
                # (03:30 broker cushion), 05:00 IST on weekdays -- not a stale string.
                rh, rm, ih, im = self._resolved_anchor_ist_hm(a[0], bdate, a[1], a[2])
                next_anchor = f"{a[0]} {rh:02d}:{rm:02d} broker / {ih:02d}:{im:02d} IST"
            except Exception:
                next_anchor = "A1 (time unresolved)"
            state_ok = "ok" if isinstance(self.state, dict) and self.state else "fail"
            self.tele.info(
                f"🔧 Ready: offset {off}h {tag} · next anchor "
                f"{next_anchor} · state rehydrated {state_ok} ({reason})")
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

        # E-19: server_time_utc() decodes tick.time with `tick_time_offset_hours or 0`
        # (mt5_adapter.py), so while the offset is UNDETECTED (None -- a boot into a
        # closed/quiet market where neither Tier 1 live-feed nor Tier 2 stale-consistency
        # could confirm it) `tick_age_sec` is off by up to the real broker offset (3h =
        # 10800s) and can land in the 120-3600s window below by pure arithmetic accident,
        # even though the true cause is "closed market", not "clock drift". Live proof
        # (2026-07-03 23:02): Tier 2 REJECT -> offset stayed None -> this miscalculation
        # put a genuine Friday-night closed-market boot into the ABORT branch -> clean
        # exit(0) -> watchdog never relaunches (exit-42-only) -> the bot silently never
        # came back. An unconfirmed offset makes tick_age_sec UNRELIABLE by construction,
        # so it can never safely justify the clock-drift abort below -- route it into the
        # SAME sleep-probe loop as a confirmed closed market instead. The loop's own
        # wake-detection re-validates the offset (Tier 1, now against LIVE ticks) before
        # ever returning, so this can't mask a real drift once the market actually opens.
        offset_unconfirmed = self.adapter.tick_time_offset_hours is None

        if tick_age_sec > 3600 or offset_unconfirmed:
            hours = tick_age_sec / 3600
            # ONE Telegram line on ENTERING weekend sleep (announce-once: the
            # while-loop below blocks here until Monday, so this never repeats).
            try:
                _next_a1 = self._next_a1_display()   # v3.3.6: resolver-derived (Mon 06:00 IST)
            except Exception:
                _next_a1 = "A1 (time unresolved)"
            _age_note = (f"last tick {hours:.1f}h old" if not offset_unconfirmed
                         else "broker offset UNDETECTED -- age unreliable, waiting for a "
                              "live/consistent tick to re-detect it")
            self.tele.info(
                f"💤 Weekend — market closed, sleeping, will auto-resume Monday. "
                f"Next anchor {_next_a1}. "
                f"({_age_note}; entered via {reason})")
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
                    if self.adapter.tick_time_offset_hours is None:
                        # E-19: the offset was never confirmed (the boot-time Tier 1/2
                        # detect failed too), so server_time_utc()'s age is unreliable --
                        # do NOT trust a raw tick_age_sec<60 read here (it can read
                        # "fresh" hours before the market genuinely reopens, per the
                        # constant-bias math above). The only trustworthy wake signal
                        # while unconfirmed is a detection actually succeeding: Tier 1
                        # requires a LIVE advancing feed and Tier 2 requires the tick be
                        # within 10min of true now -- neither can fire early.
                        if self.adapter.ensure_time_offset(max_wait_s=5.0):
                            market_open = True
                    else:
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
            # v3.2.3 WEEKEND_WAKE: log + announce the first-tick-after-gap BEFORE
            # the offset is re-derived, so a Monday wake is always visible and the
            # gap that triggered the re-derive is on the record.
            try:
                import offset_guard as _og
                _gap_h = round(tick_age_sec / 3600.0, 1)
                self.ptrace.weekend_wake(
                    gap_hours=_gap_h, last_tick=None, first_tick=None,
                    is_weekend=_og.is_weekend_wake(_gap_h, self.WEEKEND_GAP_HOURS))
                self.tele.info(
                    f"🌅 WEEKEND WAKE | gap {_gap_h}h | re-deriving offset "
                    f"(expected +{self.EXPECTED_OFFSET}) | validating before A1")
            except Exception:
                pass
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
        # v3.5.0 feature 11: boot preflight self-check (alert-only; never gates here --
        # the adapter offset guard is the real block). Fully guarded.
        try:
            import boost_metrics as _bm
            _bm.run_preflight(self)
        except Exception:
            pass
        # ROGUE: demo default-ON promotion / funded force-OFF gate (guarded; raw config
        # default is OFF so this is the only place rogue can switch ON, demo-only).
        try:
            import rogue as _rogue
            _rogue.promote_on_boot(self)
        except Exception:
            pass
        # FETCHER: demo default-ON promotion / funded force-OFF gate (same shape as Rogue).
        try:
            import fetcher as _fetcher
            _fetcher.promote_on_boot(self)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------------

    def run(self):
        _boost_sl = float(getattr(self.cfg, 'boost_sl_dollars', 10.0))
        _boost_n = int(getattr(self.cfg, 'rescue_boost_count', 2))
        _whip_cap = _boost_n * _boost_sl * self.cfg.lot_size * 100
        _boost_gap = float(getattr(self.cfg, 'boost_trail_gap_dollars', 3.50))
        # v3.3.6 banner-truth: RALLY boosts run their OWN $13 SL / -$910 cap / $2 trail
        # (rally_* keys), NOT the rescue $10/$3.50 the banner used to print. Show both.
        _rally_sl = float(getattr(self.cfg, 'rally_boost_sl', 13.0))
        _rally_gap = float(getattr(self.cfg, 'rally_trail_gap', 2.00))
        _rally_cap = _boost_n * _rally_sl * self.cfg.lot_size * 100
        # v3.1.0: alert-channel banner line (Discord, embed cards).
        _hb = int(getattr(self.cfg, 'discord_heartbeat_min', 60))
        _alert_line = (f"Alerts: Discord (embed cards) — commands ON, "
                       f"heartbeat {_hb}m")
        # escape underscores so Markdown doesn't italicize
        auto_lot_label = "auto\\_lot=on" if self.cfg.auto_lot else "auto\\_lot=off"
        fp_cap_label = (f"\nFP\\_ZERO\\_MAX\\_LOT: `{self.FP_ZERO_MAX_LOT}` ⚠ CAP ACTIVE"
                        if self.FP_ZERO_MAX_LOT is not None
                        else "\nFP\\_ZERO\\_MAX\\_LOT: `None` (Pepperstone demo — no cap)")
        _mode = 'PAPER' if self.paper else 'LIVE'
        _alerts_val = f"Discord cards · heartbeat {_hb}m"
        self.tele.send(
            f"🚀 *AUREON v{AUREON_VERSION} {_mode} starting*\n"
            f"Lot: `{self.cfg.lot_size}` ({auto_lot_label})\n"
            f"Kill switch: `-{self.cfg.daily_loss_pct*100:.1f}%`\n"
            f"Hold: `{self.cfg.freeze_minutes}m` | TSTOP: `fav<${getattr(self.cfg, 'tstop_fav', 0):.2f}` | NoOCO: `{getattr(self.cfg, 'no_oco', False)}`\n"
            f"Ladder: `5>BE | 6>+4 | 10>peak-2` | Trail: `gap ${self.cfg.trail_gap:.2f}, arm ${self.cfg.be_trigger:.2f}`\n"
            f"SL/TP: `${self.cfg.sl_dist:.0f}/${self.cfg.tp_dist:.0f}` | Roles: `normal + RESCUE 2nd legs`\n"
            f"Boost RALLY: `{_boost_n}x SL ${_rally_sl:.0f}` | trail `${_rally_gap:.2f}` | cap `-${_rally_cap:.0f}` · RESCUE: `SL ${_boost_sl:.0f}` | gap `${_boost_gap:.2f}` | cap `-${_whip_cap:.0f}` | isolated\n"
            f"{_alert_line}\n"
            f"Defer waits: A1/A3=15s, A2/A4/A5=30s | rc=-1 retries: {self.MAX_PLACEMENT_RETRIES} (15s, 30s)\n"
            f"v3.0.0: `rescue=twin-open guard` | `boost-diag v2` | `13-module split`\n"
            f"Modules ({len(LOADED_MODULES)}): `{' '.join(LOADED_MODULES)}`"
            + fp_cap_label,
            Severity.SUCCESS, important=True,
            card=dc.card_startup(
                f"v{AUREON_VERSION}", _mode,
                f"{self.cfg.lot_size} ({'auto' if self.cfg.auto_lot else 'manual'})",
                f"-{self.cfg.daily_loss_pct*100:.1f}%",
                f"{self.cfg.freeze_minutes}m / fav<${getattr(self.cfg, 'tstop_fav', 0):.2f}",
                "5>BE | 6>+4 | 10>peak-2",
                f"RALLY ${_rally_sl:.0f}/cap-${_rally_cap:.0f} · RESCUE ${_boost_sl:.0f}/cap-${_whip_cap:.0f} (isolated)",
                _alerts_val),
        )
        # v3.1.0: start the Discord heartbeat (no-op if Discord disabled).
        self._start_discord_heartbeat()

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
        # 0. Inbound commands FIRST, every loop. Manual commands (flatten / pause / resume /
        # today_summary / rogueseed) MUST be consumed even when the market-closed guard below
        # early-returns -- otherwise a queued command (e.g. rogueseed after a mid-day restart,
        # while the feed is briefly stale) sits UNREAD in commands.json and never fires. This
        # is idempotent: _consume_commands clears the file, so a command runs exactly once.
        self._handle_commands()
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

        # 2b. Fix 5 (E-16): ONE-SHOT boot recovery. Runs after the first new-day reset so the
        # trading-date comparison is correct: a SAME-day restart restores the Rogue governors
        # + chain anchor + adopts an open Rogue position; a NEW day ignores the stale file.
        if not getattr(self, '_p1_recovered', False):
            try:
                import p1_state as _p1
                _p1.recover_on_boot(self)
            except Exception:
                pass
            self._p1_recovered = True

        # 3. Reconcile broker state
        self._reconcile_with_broker()

        # 3b. v3.2.0 PER-TICK boost trigger: fire RALLY/RESCUE boosts only
        # once price is >= $10 from a lone leg's fill (never at fill); cap.
        self._check_boost_triggers()

        # 3c. ROGUE per-tick driver — MOVED (Fix 3 / E-15): the drive() call that can take
        # NEW entries now runs BELOW the kill-switch lock gate AND the EOD check (see step
        # 6b), so a new Rogue entry can never open on a kill-locked day or after EOD. An
        # EXISTING open Rogue position is still trail-managed post-EOD when
        # rogue_flatten_at_eod is False (the trail-only drive in the EOD branch).

        # 3d. ROGUE close watcher (Rogue-ONLY; logging/file-IO only, behavior-neutral --
        # reads broker state to record a closed Rogue ticket's realized $, never mutates
        # the mechanism). The eval rows themselves are logged by the gate hook in rogue.py.
        # Fully guarded -- never touches trading.
        try:
            import rogue_patternlog as _rpl
            _rpl.observe(self)
        except Exception:
            pass

        # 4. (inbound commands now handled at step 0, before the market-closed guard, so a
        #     queued command is never stranded unread when the feed looks stale.)

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

        # 5.5 D-6 Friday weekend-hold ban: from cfg.friday_flatten_broker_hour
        # (default 22:30 broker, 30min ahead of the normal 23:00 EOD) onward,
        # POLL until a broker query confirms BOTH magics (anchor 20260522, Rogue
        # 20260626) are flat -- single-shot is forbidden (07-03: the feed froze
        # ~20:00 broker before the one timed attempt fired, and it was never
        # retried). New entries for BOTH engines are blocked from the INSTANT
        # this window opens, independent of flatten/verify progress (see the
        # anchor-due gate + the Rogue drive() call below, both now gated
        # directly on _friday_flatten_reached rather than on the done-latch).
        # friday_flatten_done only latches once broker-verified flat.
        if self._friday_flatten_reached(broker_date, utc_now) and not self.state.get('friday_flatten_done'):
            self._friday_poll_flatten(broker_date, utc_now)

        # 6. EOD?
        if self._eod_reached(broker_date, utc_now):
            if self.shadow_positions or self.shadow_pendings:
                self._flatten_all(reason="EOD")
            # E-4: rogue EOD flatten (flag rogue_flatten_at_eod, default OFF -> rides).
            # Rogue-scoped (closes ONLY the Rogue ticket); guarded so it never blocks EOD.
            try:
                import rogue as _rogue
                _rogue.eod_flatten(self)
                # Fix 3 (E-15): if Rogue is NOT flattened at EOD (default), keep trailing
                # any EXISTING open Rogue position post-EOD — but with NEW entries hard-
                # blocked (allow_new_entries=False). The kill-switch/EOD returns above mean
                # the normal entry-taking drive() never runs here, so this is the only
                # post-EOD Rogue call and it can only manage/close, never open.
                if not bool(getattr(self.cfg, 'rogue_flatten_at_eod', False)):
                    _rogue.drive(self, allow_new_entries=False)
            except Exception:
                pass
            # FETCHER EOD flatten (flag fetcher_flatten_at_eod, default ON -> closes).
            # Fetcher-scoped (closes ONLY the Fetcher ticket); guarded so it never blocks
            # EOD. If NOT flattened at EOD, keep booking an existing position's broker
            # close post-EOD with NEW entries hard-blocked (allow_new_entries=False).
            try:
                import fetcher as _fetcher
                _fetcher.eod_flatten(self)
                if not bool(getattr(self.cfg, 'fetcher_flatten_at_eod', True)):
                    _fetcher.drive(self, allow_new_entries=False)
            except Exception:
                pass
            # v3.0.0 commit 3: Firebase EOD journal -- ONCE per broker day, after
            # the book is flat and the day's P&L is final (never during anchor
            # capture). Guarded so it fires once and never blocks the EOD path.
            if self.state.get('firebase_eod_date') != str(broker_date):
                self._firebase_save_daily(broker_date)
                # v3.5.0 feature 10: per-anchor markdown report (read-only; guarded).
                try:
                    import boost_metrics as _bm
                    # R-1: pass the same broker_date pnl_report gets below, so both
                    # EOD reports key off one authoritative day instead of _bm
                    # defaulting to IST wall-clock now() (which can already be
                    # tomorrow relative to the broker day this EOD branch is for).
                    _bm.run_daily_report(self, date_str=str(broker_date))
                except Exception:
                    pass
                # P4: full per-engine/per-anchor daily P&L report (markdown + CSV
                # ledger + Discord card). Read-only on MT5 HISTORY only; guarded by
                # util_daily_pnl_report + its own try/except so it can never block
                # the EOD path (same once-per-broker-day gate as the block above).
                try:
                    import pnl_report as _pnl
                    _pnl.run_eod_report(self, broker_date)
                except Exception:
                    pass
                # ROGUE dated EOD archive: freeze this day's rogue_patterns.csv +
                # rogue_trades.csv (+ today_trades / price_log) into
                # logs/archive/{broker_date}/ (copy, not move). Logging/file-IO only.
                try:
                    import rogue_patternlog as _rpl
                    _rpl.archive_day(self.run_dir, broker_date=broker_date,
                                     price_log_dir=self.price_log_dir,
                                     daylog_path=self.daylog_path)
                except Exception:
                    pass
                # ROGUE EOD champion/challenger auto-train (Rogue-ONLY; AFTER the archive).
                # Trains a challenger, promotes ONLY if it beats the champion (fail-safe:
                # champion can only improve). EOD only, never per-trade. Fully guarded --
                # an ML error never affects trading or the next boot.
                try:
                    import rogue as _rogue
                    if _rogue.should_run(self.cfg,
                                         is_funded=not _rogue.account_is_demo(self)):
                        import rogue_autotrain as _rat
                        _rat.run(self.run_dir, archive_dir="./logs/archive")
                except Exception:
                    pass
                self.state['firebase_eod_date'] = str(broker_date)
                self._save_state()
            if self._tick_counter % self.STATUS_EVERY_TICKS == 0:
                self._write_status(broker_date)
            return

        # 6b. ROGUE per-tick driver (Fix 3 / E-15): runs here — AFTER the kill-switch lock
        # gate AND the EOD check, both of which `return` above — so a NEW Rogue entry can
        # only open on a live, non-killed, pre-EOD tick. No-op unless rogue is ON (demo-
        # promoted, never funded). Fully guarded (the _tick except below also catches it).
        # D-6: new Rogue entries are ALSO blocked once the Friday flatten window opens
        # (gated directly on _friday_flatten_reached, not the done-latch, so this takes
        # effect the instant the cutoff hits -- independent of poll/verify progress).
        # v3.6.0: the gate is now the shared per-engine seam _rogue_entries_blocked
        # (= friday_window OR rogue engine switch OFF). With entries blocked, drive()
        # still trail-manages / books closes on an existing open Rogue position
        # (manage-only) -- the switch never orphans a leg.
        try:
            import rogue as _rogue
            _rogue.drive(self, allow_new_entries=not self._rogue_entries_blocked(broker_date, utc_now))
        except Exception:
            pass

        # 6c. FETCHER per-tick driver: mirrors 6b exactly (runs AFTER the kill-switch and
        # EOD returns, so a NEW Fetcher entry only opens on a live, non-killed, pre-EOD
        # tick). No-op unless fetcher is ON (demo-promoted, never funded). Gated on the
        # shared per-engine seam _fetcher_entries_blocked (= friday_window OR fetcher
        # switch OFF); with entries blocked, drive() still books a close on an existing
        # open Fetcher position (manage-only) -- the switch never orphans a leg.
        try:
            import fetcher as _fetcher
            _fetcher.drive(self, allow_new_entries=not self._fetcher_entries_blocked(broker_date, utc_now))
        except Exception:
            pass

        # 7. Anchor due? Blocked from the instant the Friday flatten window opens
        # (5.5 above) -- no NEW anchor entry may open between the Friday cutoff and
        # Monday. v3.6.0: gated on the shared per-engine seam _anchor_entries_blocked
        # (= _friday_entries_blocked OR anchors engine switch OFF), so /anchors off
        # blocks new straddles at the SAME seam the Friday window uses. Trails (step
        # 8), exits, EOD (step 6) and the kill switch (step 5) run regardless.
        if not self._anchor_entries_blocked(broker_date, utc_now):
            self._process_anchor_if_due(broker_date, utc_now)

        # 7b. v2.5: Complete any deferred anchor placement (settle window or retry)
        self._complete_deferred_anchor()

        # 7c. v3.3.0 POSITION_HEARTBEAT: keep the ticket trace gapless between
        # state changes while any position is open (throttled ~60s internally).
        self._maybe_position_heartbeat()

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
        # ROGUE promotion — GUARANTEED on every LIVE boot. The other call site
        # lives inside wait_until_market_open(), which RETURNS EARLY on the
        # weekend/sleep->wake path (it never reaches the promotion line), leaving
        # rogue dormant. Promote here too: this runs on every live start
        # regardless of weekday/weekend/wake, AFTER the adapter is connected (so
        # account_is_demo reads the real account) and BEFORE trading starts.
        # LIVE only -- paper must never promote. Idempotent (just stamps the flag
        # per account type); fully guarded so it can never abort the boot.
        # v3.6.0: the config boot default is True, but this per-account stamp
        # (demo ON / funded forced OFF) stays authoritative on every live boot.
        if not paper:
            try:
                import rogue as _rogue
                _rogue.promote_on_boot(trader)
            except Exception:
                pass
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
import rescue_log as _rescue_mod

LiveTrader._rescue_event_open       = _rescue_mod._rescue_event_open
LiveTrader._rescue_event_on_close   = _rescue_mod._rescue_event_on_close
LiveTrader._rescue_event_finalize   = _rescue_mod._rescue_event_finalize
LiveTrader._persist_rescue_events   = _rescue_mod._persist_rescue_events
LiveTrader._rehydrate_rescue_events = _rescue_mod._rehydrate_rescue_events
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
LiveTrader._anchor_sched_utc        = _anchors_mod._anchor_sched_utc
LiveTrader._mark_anchor_placed      = _anchors_mod._mark_anchor_placed
LiveTrader._anchor_missed           = _anchors_mod._anchor_missed
LiveTrader._dump_mt5_state          = _anchors_mod._dump_mt5_state
LiveTrader._warmup_trade_channel    = _anchors_mod._warmup_trade_channel
LiveTrader._attempt_mt5_reconnect   = _anchors_mod._attempt_mt5_reconnect
LiveTrader._confirm_a1_placement    = _anchors_mod._confirm_a1_placement
LiveTrader._resolved_anchor_hm      = _anchors_mod._resolved_anchor_hm
LiveTrader._anchor_skipped_today_friday = _anchors_mod._anchor_skipped_today_friday
LiveTrader._resolved_anchor_ist_hm  = _anchors_mod._resolved_anchor_ist_hm  # v3.3.6 display
LiveTrader._next_a1_display         = _anchors_mod._next_a1_display          # v3.3.6 display
LiveTrader._await_fresh_tick_for_placement = _anchors_mod._await_fresh_tick_for_placement
LiveTrader._capture_a1_anchor_from_tick = _anchors_mod._capture_a1_anchor_from_tick
LiveTrader._extract_ticket          = staticmethod(_anchors_mod._extract_ticket)
LiveTrader._reconcile_with_broker   = _fills_mod._reconcile_with_broker
LiveTrader._check_boost_triggers    = _fills_mod._check_boost_triggers
LiveTrader._fire_boost_event        = _fills_mod._fire_boost_event
LiveTrader._enforce_boost_cap       = _fills_mod._enforce_boost_cap
LiveTrader._break_and_hold_ok       = _fills_mod._break_and_hold_ok
LiveTrader._rescue_entry_ok         = _fills_mod._rescue_entry_ok   # v3.5.0 rescue gate seam
LiveTrader._fp_guard_ok             = _fills_mod._fp_guard_ok
LiveTrader._anchor_evt_block        = _fills_mod._anchor_evt_block
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
                  'live_trader', 'bot', 'firebase_journal', 'position_telemetry',
                  'offset_guard', 'soft_restart', 'break_hold', 'fp_guard']
