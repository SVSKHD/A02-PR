"""AUREON — per-position structured telemetry (v3.2.3 telemetry overhaul).

WHY THIS EXISTS
---------------
The goal of v3.2.x: never leave a failure as "unknown" again. On 2026-06-19 A2
force-closed a long at a loss because a lock advanced off a `max_fav` the market
never produced, and the log was SILENT in the middle of the trade. This module
makes that silence impossible, for the trail AND for the boost stack.

CONTRACT
--------
- Every position state change emits ONE greppable line carrying EVERY mandatory
  field (null when unknown -- a field is NEVER omitted), tagged ticket + anchor.
- Grepping a ticket id returns a gapless life story: PLAN -> PLACE -> FILL ->
  PREDICT -> MAXFAV_UPDATE* -> LOCK_ARM* -> TRAIL_ADVANCE* -> BOOST_ARM* ->
  BOOST_FIRE* -> STOP_THROUGH_REARM* -> HEARTBEAT* -> EXIT.
- Runtime self-consistency asserts write a loud `TELEMETRY_VIOLATION` the instant
  an impossible state is seen -- including a trigger that was met but never fired
  (MISSED_BOOST) or armed but never executed (BOOST_ARM_ORPHANED).

PURITY
------
No MT5, no network. A `sink` callable receives each finished line (defaults to
logging). Importable by live, backtest, and selftest with identical behavior
(import-path identity, asserted by the selftest parity step).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

log = logging.getLogger("AUREON.ptrace")

# Mirror telemetry.py's clock derivation locally so this module stays free of the
# heavy telemetry import (which pulls Discord). Broker = UTC+3; IST = UTC+5:30.
_BROKER_UTC_OFFSET = timedelta(hours=3)
_IST_FROM_BROKER = timedelta(hours=2, minutes=30)

# Event types -- the complete set of position state changes (spec Part B1).
PLAN = "PLAN"
PLACE = "PLACE"
FILL = "FILL"
PREDICT = "PREDICT"
MAXFAV_UPDATE = "MAXFAV_UPDATE"
LOCK_ARM = "LOCK_ARM"
LOCK_CHECK = "LOCK_CHECK"
LOCK_REJECTED_PHANTOM = "LOCK_REJECTED_PHANTOM"
TRAIL_ADVANCE = "TRAIL_ADVANCE"
STOP_REJECTED = "STOP_REJECTED"
STOP_THROUGH_REARM = "STOP_THROUGH_REARM"
BOOST_ARM = "BOOST_ARM"
BOOST_FIRE = "BOOST_FIRE"
POSITION_HEARTBEAT = "POSITION_HEARTBEAT"
EXIT = "EXIT"
# v3.2.3 Monday weekend-wake offset guard (system-level events; ticket may be null)
WEEKEND_WAKE = "WEEKEND_WAKE"
OFFSET_DETECT = "OFFSET_DETECT"
OFFSET_MISMATCH = "OFFSET_MISMATCH"
ANCHOR_TIME_RESOLVED = "ANCHOR_TIME_RESOLVED"
# v3.2.3 soft self-update / restart-reconcile (system-level; ticket may be null)
SOFT_RESTART_SNAPSHOT = "SOFT_RESTART_SNAPSHOT"
SOFT_RESTART_EXIT = "SOFT_RESTART_EXIT"
SOFT_RESTART_REHYDRATE = "SOFT_RESTART_REHYDRATE"
RECONCILE = "RECONCILE"
RECONCILE_SUMMARY = "RECONCILE_SUMMARY"
# v3.2.3 Feature D/E: break-and-hold filter + FP exposure guard
BREAK_EVAL = "BREAK_EVAL"
# v3.2.4 break-and-hold + 5-long stack lifecycle
BREAK_CANDIDATE = "BREAK_CANDIDATE"
BREAK_CONFIRMED = "BREAK_CONFIRMED"
BREAK_FAILED = "BREAK_FAILED"
CONTINUATION_STACK = "CONTINUATION_STACK"
FP_GUARD = "FP_GUARD"
FP_GUARD_EVAL = "FP_GUARD_EVAL"
STACK_COMPLETE = "STACK_COMPLETE"
LEG_SL = "LEG_SL"
TRAIL_LOCK = "TRAIL_LOCK"
STACK_CLOSE = "STACK_CLOSE"
# v3.2.5 A1 tick-fallback + tick-hold confirm
A1_BAR_MISSING = "A1_BAR_MISSING"
A1_TICK_FALLBACK = "A1_TICK_FALLBACK"
A1_PLACED_FROM_TICK = "A1_PLACED_FROM_TICK"
TICK_CROSS_CANDIDATE = "TICK_CROSS_CANDIDATE"
TICK_HOLD_CONFIRMED = "TICK_HOLD_CONFIRMED"
TICK_BLIP_REJECTED = "TICK_BLIP_REJECTED"
VIOLATION = "TELEMETRY_VIOLATION"

# Mandatory fields on EVERY line (spec B2). A missing field is the failure we are
# eliminating, so the formatter writes `null` rather than dropping it.
MANDATORY_FIELDS = (
    "ts_server", "ts_ist", "anchor", "ticket", "parent_ticket", "event_type",
    "side", "bid", "ask", "position_price", "max_fav", "lock_level",
    "stop_price", "boost_kind", "stack_size", "floating_pnl",
)

# Exit types that, by construction, can only happen AFTER the stop was trailed up
# -- so they MUST have a preceding TRAIL_ADVANCE line for the same ticket.
TRAIL_EXIT_TYPES = ("TRAIL", "STOP_THROUGH")

# Dollar fav each lock rung REQUIRES (mirror strategy.lock_ladder_prices).
_RUNG_FAV = {1: 5.0, 2: 6.0, 3: 10.0}


def _clock_pair(now_utc=None):
    """(ts_server, ts_ist) for one captured instant -- single-source, so the
    server and IST stamps can never drift apart. `now_utc` is for testing."""
    base = now_utc or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base = base.astimezone(timezone.utc)
    server = base + _BROKER_UTC_OFFSET
    ist = server + _IST_FROM_BROKER
    return server.strftime("%Y-%m-%d %H:%M:%S"), ist.strftime("%Y-%m-%d %H:%M:%S")


def _fmt(v):
    """Render a value for a log line. None -> the literal `null` (never omitted);
    floats to 2dp; everything else str()."""
    if v is None:
        return "null"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def format_event_line(record: Dict) -> str:
    """One greppable line carrying every mandatory field plus any extras. The
    ticket and event_type lead so `grep ticket=<id>` reconstructs the whole life
    of the position in order."""
    head = (f"PTRACE {record.get('event_type', 'null')} "
            f"ticket={_fmt(record.get('ticket'))} "
            f"anchor={_fmt(record.get('anchor'))}")
    fields = " ".join(f"{k}={_fmt(record.get(k))}" for k in MANDATORY_FIELDS
                      if k not in ("event_type", "ticket", "anchor"))
    extras = {k: v for k, v in record.items() if k not in MANDATORY_FIELDS}
    extra = " ".join(f"{k}={_fmt(v)}" for k, v in extras.items())
    line = f"{head} {fields}"
    if extra:
        line += f" | {extra}"
    return line


class PositionTracer:
    """Emits one structured line per position state change and runs the runtime
    self-consistency assertions. Stateful per ticket so it can prove, at EXIT,
    that the life story has no gaps (a trail exit had a TRAIL_ADVANCE; an armed
    boost actually fired)."""

    def __init__(self, sink: Optional[Callable[[str], None]] = None):
        self._sink = sink or (lambda line: log.info(line))
        self._history: Dict[object, List[Dict]] = {}
        self._violations: List[str] = []

    # ---- core emit -------------------------------------------------------
    def emit(self, event_type: str, ticket=None, anchor=None, *,
             now_utc=None, parent_ticket=None, side=None, bid=None, ask=None,
             position_price=None, max_fav=None, lock_level=None, stop_price=None,
             boost_kind=None, stack_size=None, floating_pnl=None, **extra) -> Dict:
        server, ist = _clock_pair(now_utc)
        record = {
            "ts_server": server, "ts_ist": ist, "anchor": anchor,
            "ticket": ticket, "parent_ticket": parent_ticket,
            "event_type": event_type, "side": side, "bid": bid, "ask": ask,
            "position_price": position_price, "max_fav": max_fav,
            "lock_level": lock_level, "stop_price": stop_price,
            "boost_kind": boost_kind, "stack_size": stack_size,
            "floating_pnl": floating_pnl,
        }
        record.update(extra)
        self._history.setdefault(ticket, []).append(record)
        try:
            self._sink(format_event_line(record))
        except Exception as e:  # telemetry must NEVER crash trading
            log.warning(f"ptrace sink failed (non-fatal): {e!r}")
        self._check_invariants(record)
        return record

    # ---- typed helpers (spec Part B1) ------------------------------------
    def plan(self, ticket, anchor, **kw):    return self.emit(PLAN, ticket, anchor, **kw)
    def place(self, ticket, anchor, **kw):   return self.emit(PLACE, ticket, anchor, **kw)
    def fill(self, ticket, anchor, **kw):    return self.emit(FILL, ticket, anchor, **kw)
    def maxfav_update(self, ticket, anchor, **kw): return self.emit(MAXFAV_UPDATE, ticket, anchor, **kw)
    def lock_arm(self, ticket, anchor, **kw):     return self.emit(LOCK_ARM, ticket, anchor, **kw)
    def lock_check(self, ticket, anchor, **kw):   return self.emit(LOCK_CHECK, ticket, anchor, **kw)
    def lock_rejected_phantom(self, ticket, anchor, **kw): return self.emit(LOCK_REJECTED_PHANTOM, ticket, anchor, **kw)
    def trail_advance(self, ticket, anchor, **kw): return self.emit(TRAIL_ADVANCE, ticket, anchor, **kw)
    def stop_rejected(self, ticket, anchor, **kw): return self.emit(STOP_REJECTED, ticket, anchor, **kw)
    def stop_through_rearm(self, ticket, anchor, **kw): return self.emit(STOP_THROUGH_REARM, ticket, anchor, **kw)
    def boost_arm(self, ticket, anchor, **kw):    return self.emit(BOOST_ARM, ticket, anchor, **kw)
    def boost_fire(self, ticket, anchor, **kw):   return self.emit(BOOST_FIRE, ticket, anchor, **kw)
    def heartbeat(self, ticket, anchor, **kw):    return self.emit(POSITION_HEARTBEAT, ticket, anchor, **kw)
    def exit(self, ticket, anchor, **kw):         return self.emit(EXIT, ticket, anchor, **kw)
    # v3.2.3 Monday weekend-wake offset guard (ticket=None; anchor='A1').
    def weekend_wake(self, anchor="A1", **kw):    return self.emit(WEEKEND_WAKE, None, anchor, **kw)
    def offset_detect(self, anchor="A1", **kw):   return self.emit(OFFSET_DETECT, None, anchor, **kw)
    def offset_mismatch(self, anchor="A1", **kw): return self.emit(OFFSET_MISMATCH, None, anchor, **kw)
    def anchor_time_resolved(self, anchor="A1", **kw): return self.emit(ANCHOR_TIME_RESOLVED, None, anchor, **kw)
    # v3.2.3 soft self-update / restart-reconcile.
    def soft_restart_snapshot(self, **kw): return self.emit(SOFT_RESTART_SNAPSHOT, None, "SYS", **kw)
    def soft_restart_exit(self, **kw):     return self.emit(SOFT_RESTART_EXIT, None, "SYS", **kw)
    def soft_restart_rehydrate(self, **kw): return self.emit(SOFT_RESTART_REHYDRATE, None, "SYS", **kw)
    def reconcile(self, ticket=None, **kw): return self.emit(RECONCILE, ticket, "SYS", **kw)
    def reconcile_summary(self, **kw):     return self.emit(RECONCILE_SUMMARY, None, "SYS", **kw)
    def break_eval(self, anchor, **kw):    return self.emit(BREAK_EVAL, None, anchor, **kw)
    def break_candidate(self, anchor, **kw): return self.emit(BREAK_CANDIDATE, None, anchor, **kw)
    def break_confirmed(self, anchor, **kw): return self.emit(BREAK_CONFIRMED, None, anchor, **kw)
    def break_failed(self, anchor, **kw):  return self.emit(BREAK_FAILED, None, anchor, **kw)
    def continuation_stack(self, anchor, **kw): return self.emit(CONTINUATION_STACK, None, anchor, **kw)
    def fp_guard(self, anchor="SYS", **kw): return self.emit(FP_GUARD_EVAL, None, anchor, **kw)
    def stack_complete(self, ticket, anchor, **kw): return self.emit(STACK_COMPLETE, ticket, anchor, **kw)
    def leg_sl(self, ticket, anchor, **kw): return self.emit(LEG_SL, ticket, anchor, **kw)
    def trail_lock(self, ticket, anchor, **kw): return self.emit(TRAIL_LOCK, ticket, anchor, **kw)
    def stack_close(self, anchor, **kw):   return self.emit(STACK_CLOSE, None, anchor, **kw)
    def a1_bar_missing(self, anchor, **kw): return self.emit(A1_BAR_MISSING, None, anchor, **kw)
    def a1_tick_fallback(self, anchor, **kw): return self.emit(A1_TICK_FALLBACK, None, anchor, **kw)
    def a1_placed_from_tick(self, anchor, **kw): return self.emit(A1_PLACED_FROM_TICK, None, anchor, **kw)
    def tick_cross_candidate(self, ticket, anchor, **kw): return self.emit(TICK_CROSS_CANDIDATE, ticket, anchor, **kw)
    def tick_hold_confirmed(self, ticket, anchor, **kw): return self.emit(TICK_HOLD_CONFIRMED, ticket, anchor, **kw)
    def tick_blip_rejected(self, ticket, anchor, **kw): return self.emit(TICK_BLIP_REJECTED, ticket, anchor, **kw)
    def reconcile_orphan(self, ticket, **kw):
        return self.violation(ticket, "SYS", "reconcile_orphan", **kw)
    def autopull_aborted(self, reason="selftest_fail", **kw):
        return self.violation(None, "SYS", "AUTOPULL_ABORTED", abort_reason=reason, **kw)

    def predict(self, ticket, anchor, side, entry, sl, tp, max_loss, max_gain,
                trigger=10.0, breakeven_per_pos=6.0, **kw):
        """The fill-time prediction line (spec B4): one line that names every exit
        door + the boost arm prices + the break-even truth, up front."""
        _s = -1.0 if str(side).upper() == "SELL" else 1.0
        rally_at = round(entry + _s * trigger, 2)
        rescue_at = round(entry - _s * trigger, 2)
        return self.emit(
            PREDICT, ticket, anchor, side=side, position_price=entry,
            stop_price=sl, tp=tp, max_loss=max_loss, max_gain=max_gain,
            rally_arms_at=rally_at, rescue_arms_at=rescue_at,
            breakeven_per_pos=breakeven_per_pos,
            rule="lock_N fires ONLY if max_fav reaches its level; boost only >= "
                 "$%.0f from fill" % trigger, **kw)

    # ---- self-consistency assertions (spec B5) ---------------------------
    def violation(self, ticket, anchor, reason, **kw):
        """Write a TELEMETRY_VIOLATION line (and record it) -- fail LOUD."""
        line = (f"PTRACE {VIOLATION} ticket={_fmt(ticket)} anchor={_fmt(anchor)} "
                f"reason={reason} "
                + " ".join(f"{k}={_fmt(v)}" for k, v in kw.items()))
        self._violations.append(line)
        try:
            self._sink(line)
        except Exception:
            pass
        log.warning(line)
        return line

    @property
    def violations(self) -> List[str]:
        return list(self._violations)

    def _events_for(self, ticket, event_type) -> List[Dict]:
        return [e for e in self._history.get(ticket, [])
                if e.get("event_type") == event_type]

    def missed_boost(self, ticket, anchor, **kw):
        """The trigger condition was met (>=$trigger crossed and held) but NO
        BOOST_ARM/BOOST_FIRE happened -- the logic failed to even detect a valid
        trigger (the lone-leg equivalent of A2's silent trail-miss)."""
        return self.violation(ticket, anchor, "MISSED_BOOST", **kw)

    def boost_arm_orphaned(self, ticket, anchor, **kw):
        """A BOOST_ARM was logged (trigger detected) but no BOOST_FIRE followed --
        execution dropped (channel error, retcode fail, order rejected)."""
        return self.violation(ticket, anchor, "BOOST_ARM_ORPHANED", **kw)

    def check_orphan_arms(self, ticket) -> bool:
        """At EXIT / EOD: if this ticket armed a boost but no BOOST_FIRE followed,
        that is a BOOST_ARM_ORPHANED. A fire is keyed by the BOOST's own ticket and
        carries parent_ticket=<this ticket>, so resolve fires by parent (or same
        ticket). Returns True if an orphan was found."""
        arms = self._events_for(ticket, BOOST_ARM)
        if not arms:
            return False
        fired = bool(self._events_for(ticket, BOOST_FIRE))
        if not fired:
            for evs in self._history.values():
                if any(e.get("event_type") == BOOST_FIRE
                       and e.get("parent_ticket") == ticket for e in evs):
                    fired = True
                    break
        if not fired:
            self.boost_arm_orphaned(ticket, arms[-1].get("anchor"))
            return True
        return False

    def _level_justified(self, record: Dict, level: int) -> bool:
        """True if max_fav actually reached `level`'s required price. Unknown data
        -> treated as justified so a legit fast move is never FALSE-flagged."""
        mf = record.get("max_fav")
        entry = record.get("position_price")
        if mf is None or entry is None:
            return True
        need = _RUNG_FAV.get(int(level), _RUNG_FAV[3])
        _s = -1.0 if str(record.get("side")).upper() == "SELL" else 1.0
        return _s * (mf - entry) >= need - 0.011

    def _check_invariants(self, record: Dict):
        """Runtime asserts -- each writes a TELEMETRY_VIOLATION the instant an
        impossible state is observed (spec B5)."""
        et = record.get("event_type")
        ticket = record.get("ticket")
        anchor = record.get("anchor")
        side = record.get("side")
        hist = self._history.get(ticket, [])

        # (1) An EXIT via trail/stop-through/lock with NO preceding TRAIL_ADVANCE.
        if et == EXIT:
            xt = str(record.get("exit_type", "")).upper()
            if any(t in xt for t in TRAIL_EXIT_TYPES) or xt.startswith("SL_LOCK"):
                if not self._events_for(ticket, TRAIL_ADVANCE):
                    self.violation(ticket, anchor,
                                   "exit_trail_without_trail_advance",
                                   exit_type=record.get("exit_type"))
            # at close, surface an armed-but-never-fired boost.
            self.check_orphan_arms(ticket)

        # (2) A profit-lock armed below entry, OR locking in MORE profit than
        #     max_fav ever supported (a lock off a price the market never produced
        #     -- the A2 root cause). The latter is the exact, continuous-trail-safe
        #     invariant: the stop's locked profit can never exceed peak favorable.
        if et == LOCK_ARM:
            mf = record.get("max_fav")
            entry = record.get("position_price")
            stop = record.get("stop_price")
            if mf is not None and entry is not None:
                _s = -1.0 if str(side).upper() == "SELL" else 1.0
                if _s * (mf - entry) < 0:
                    self.violation(ticket, anchor, "lock_armed_below_entry",
                                   max_fav=mf, entry=entry, side=side)
                elif stop is not None and \
                        _s * (stop - entry) > _s * (mf - entry) + 0.05:
                    self.violation(ticket, anchor, "lock_armed_above_max_fav",
                                   max_fav=mf, entry=entry, stop=stop, side=side)

        # (3) Long stop at/above bid (mirror: short stop at/below ask).
        if et in (PLACE, TRAIL_ADVANCE):
            stop = record.get("stop_price")
            bid = record.get("bid")
            ask = record.get("ask")
            if stop is not None:
                if str(side).upper() == "SELL" and ask is not None and stop <= ask:
                    self.violation(ticket, anchor, "short_stop_at_or_below_ask",
                                   stop=stop, ask=ask)
                elif str(side).upper() != "SELL" and bid is not None and stop >= bid:
                    self.violation(ticket, anchor, "long_stop_at_or_above_bid",
                                   stop=stop, bid=bid)

        # (4) lock_level jumped > 1 step UNLESS max_fav justifies the new rung.
        prev_levels = [e.get("lock_level") for e in hist[:-1]
                       if e.get("lock_level") is not None]
        cur = record.get("lock_level")
        if cur is not None and prev_levels:
            try:
                if int(cur) - int(prev_levels[-1]) > 1 and \
                        not self._level_justified(record, int(cur)):
                    self.violation(ticket, anchor, "lock_level_skipped_unjustified",
                                   prev=prev_levels[-1], now=cur,
                                   max_fav=record.get("max_fav"))
            except (TypeError, ValueError):
                pass

        # (5) BOOST_FIRE with move < trigger from fill (the fire-at-fill bug).
        if et == BOOST_FIRE:
            move = record.get("move_dollars")
            trig = record.get("trigger", 10.0)
            if move is not None and abs(float(move)) < float(trig) - 1e-6:
                self.violation(ticket, anchor, "boost_fire_below_trigger",
                               move=move, trigger=trig)

        # (6) stack_size beyond the hard cap (3 by default; 5 only when the 5-long
        #     feature is enabled, passed as stack_cap on the record). A record
        #     without an explicit cap uses 3 -- so test-36's cap-at-3 is unchanged.
        ss = record.get("stack_size")
        if ss is not None:
            try:
                cap = int(record.get("stack_cap") or 3)
                if int(ss) > cap:
                    self.violation(ticket, anchor, "stack_size_exceeds_cap",
                                   stack_size=ss, stack_cap=cap)
            except (TypeError, ValueError):
                pass
