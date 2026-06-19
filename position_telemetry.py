"""AUREON — per-position structured telemetry (the trail-lock-fix overhaul).

WHY THIS EXISTS
---------------
On 2026-06-19 anchor A2_10h_London force-closed a long at a loss because a
lock level advanced off a `max_fav` the market never produced. The log was
SILENT in the middle of the trade -- no `Trail advance`, no `max_fav` -- so
the failure was undiagnosable. This module makes that silence impossible.

CONTRACT (acceptance criteria)
------------------------------
- Every state change of a position emits ONE greppable line, tagged with the
  ticket and anchor, carrying EVERY mandatory field (null when unknown -- a
  field is NEVER omitted).
- Grepping a ticket id returns a gapless life story: PLAN -> PLACE -> FILL ->
  PREDICT -> MAXFAV_UPDATE* -> LOCK_ARM* -> TRAIL_ADVANCE* -> HEARTBEAT* ->
  (STOP_REJECTED*) -> EXIT.
- Runtime self-consistency assertions emit a `TELEMETRY_VIOLATION` line the
  instant an impossible state is seen (e.g. a TRAIL exit with no preceding
  TRAIL_ADVANCE -- exactly today's bug).

PURITY
------
No MT5, no network, no Discord. A `sink` callable receives the finished line
(defaults to logging). This makes the module importable by the live path, the
backtest engine, and the selftest with byte-identical behavior (import-path
identity, asserted by selftest step 27).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

log = logging.getLogger("AUREON.ptrace")

# Mirror telemetry.py's clock derivation locally so this module stays free of
# the heavy telemetry import (which pulls Discord). Broker = UTC+3; IST = UTC+5:30.
_BROKER_UTC_OFFSET = timedelta(hours=3)
_IST_FROM_BROKER = timedelta(hours=2, minutes=30)

# Event types -- the complete set of position state changes (spec Part 1.1).
PLAN = "PLAN"
PLACE = "PLACE"
FILL = "FILL"
PREDICT = "PREDICT"
MAXFAV_UPDATE = "MAXFAV_UPDATE"
LOCK_ARM = "LOCK_ARM"
TRAIL_ADVANCE = "TRAIL_ADVANCE"
STOP_REJECTED = "STOP_REJECTED"
POSITION_HEARTBEAT = "POSITION_HEARTBEAT"
EXIT = "EXIT"
VIOLATION = "TELEMETRY_VIOLATION"

# The fields that MUST appear on every line. A missing field is the failure we
# are eliminating, so the formatter writes `null` rather than dropping it.
MANDATORY_FIELDS = (
    "timestamp_server", "timestamp_ist", "anchor", "ticket", "event_type",
    "current_bid", "current_ask", "position_price", "max_fav", "lock_level",
    "stop_price",
)

# Exit types that, by construction, can only happen AFTER the stop was trailed
# up -- so they MUST have a preceding TRAIL_ADVANCE line for the same ticket.
TRAIL_EXIT_TYPES = ("TRAIL", "STOP_THROUGH")


def _clock_pair(now_utc=None):
    """(server_iso, ist_iso) for one captured instant -- single-source, so the
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
    extras = {k: v for k, v in record.items()
              if k not in MANDATORY_FIELDS}
    extra = " ".join(f"{k}={_fmt(v)}" for k, v in extras.items())
    line = f"{head} {fields}"
    if extra:
        line += f" | {extra}"
    return line


class PositionTracer:
    """Emits one structured line per position state change and runs the runtime
    self-consistency assertions. Stateful per ticket so it can prove, at EXIT,
    that the life story has no gaps (e.g. a TRAIL exit had a TRAIL_ADVANCE)."""

    def __init__(self, sink: Optional[Callable[[str], None]] = None):
        # Default sink: AUREON.ptrace logger at INFO (violations escalate to
        # WARNING via the dedicated path below).
        self._sink = sink or (lambda line: log.info(line))
        self._history: Dict[object, List[Dict]] = {}
        self._violations: List[str] = []

    # ---- core emit -------------------------------------------------------
    def emit(self, event_type: str, ticket=None, anchor=None, *,
             now_utc=None, current_bid=None, current_ask=None,
             position_price=None, max_fav=None, lock_level=None,
             stop_price=None, **extra) -> Dict:
        server, ist = _clock_pair(now_utc)
        record = {
            "timestamp_server": server,
            "timestamp_ist": ist,
            "anchor": anchor,
            "ticket": ticket,
            "event_type": event_type,
            "current_bid": current_bid,
            "current_ask": current_ask,
            "position_price": position_price,
            "max_fav": max_fav,
            "lock_level": lock_level,
            "stop_price": stop_price,
        }
        record.update(extra)
        self._history.setdefault(ticket, []).append(record)
        try:
            self._sink(format_event_line(record))
        except Exception as e:  # telemetry must NEVER crash trading
            log.warning(f"ptrace sink failed (non-fatal): {e!r}")
        self._check_invariants(record)
        return record

    # ---- typed helpers (spec Part 1.1) -----------------------------------
    def plan(self, ticket, anchor, **kw):    return self.emit(PLAN, ticket, anchor, **kw)
    def place(self, ticket, anchor, **kw):   return self.emit(PLACE, ticket, anchor, **kw)
    def fill(self, ticket, anchor, **kw):    return self.emit(FILL, ticket, anchor, **kw)
    def maxfav_update(self, ticket, anchor, **kw): return self.emit(MAXFAV_UPDATE, ticket, anchor, **kw)
    def lock_arm(self, ticket, anchor, **kw):     return self.emit(LOCK_ARM, ticket, anchor, **kw)
    def trail_advance(self, ticket, anchor, **kw): return self.emit(TRAIL_ADVANCE, ticket, anchor, **kw)
    def stop_rejected(self, ticket, anchor, **kw): return self.emit(STOP_REJECTED, ticket, anchor, **kw)
    def heartbeat(self, ticket, anchor, **kw):    return self.emit(POSITION_HEARTBEAT, ticket, anchor, **kw)
    def exit(self, ticket, anchor, **kw):         return self.emit(EXIT, ticket, anchor, **kw)

    def predict(self, ticket, anchor, side, entry, sl, tp, max_loss, max_gain,
                lock_levels, **kw):
        """The fill-time prediction line (spec 1.4): one line that names every
        exit door up front so the outcome space is known before anything moves.
        `lock_levels` is a list of (level_no, price_threshold)."""
        ladder = ";".join(f"lock_{n}>={_fmt(p)}" for n, p in lock_levels)
        return self.emit(
            PREDICT, ticket, anchor, position_price=entry, stop_price=sl,
            side=side, tp=tp, max_loss=max_loss, max_gain=max_gain,
            lock_ladder=ladder,
            rule="lock_N fires ONLY if max_fav actually reaches its level", **kw)

    # ---- self-consistency assertions (spec 1.5) --------------------------
    def violation(self, ticket, anchor, reason, **kw):
        """Write a TELEMETRY_VIOLATION line (and record it) -- fail LOUD. These
        are the impossible states that hid today's bug."""
        rec = {
            "event_type": VIOLATION, "ticket": ticket, "anchor": anchor,
            "reason": reason,
        }
        rec.update(kw)
        line = (f"PTRACE {VIOLATION} ticket={_fmt(ticket)} anchor={_fmt(anchor)} "
                f"reason={reason} "
                + " ".join(f"{k}={_fmt(v)}" for k, v in kw.items()))
        self._violations.append(line)
        try:
            self._sink(line)
        except Exception:
            pass
        log.warning(line)
        return rec

    @property
    def violations(self) -> List[str]:
        return list(self._violations)

    def _events_for(self, ticket, event_type) -> List[Dict]:
        return [e for e in self._history.get(ticket, [])
                if e.get("event_type") == event_type]

    def _check_invariants(self, record: Dict):
        """Runtime asserts. Each writes a TELEMETRY_VIOLATION the instant an
        impossible state is observed -- the four cases from spec 1.5."""
        et = record.get("event_type")
        ticket = record.get("ticket")
        anchor = record.get("anchor")
        side = record.get("side")
        hist = self._history.get(ticket, [])

        # (1) An EXIT via a trail/stop-through with NO preceding TRAIL_ADVANCE.
        #     This is exactly today's failure -- a lock-ladder exit that the
        #     trail never justified.
        if et == EXIT:
            xt = str(record.get("exit_type", "")).upper()
            if any(t in xt for t in TRAIL_EXIT_TYPES) or xt.startswith("SL_LOCK"):
                if not self._events_for(ticket, TRAIL_ADVANCE):
                    self.violation(ticket, anchor,
                                   "exit_trail_without_trail_advance",
                                   exit_type=record.get("exit_type"))

        # (2) A profit-lock armed while max_fav is below entry (a lock off a
        #     price the market never reached).
        if et == LOCK_ARM:
            mf = record.get("max_fav")
            entry = record.get("position_price")
            if mf is not None and entry is not None:
                _s = -1.0 if str(side).upper() == "SELL" else 1.0
                if _s * (mf - entry) < 0:
                    self.violation(ticket, anchor, "lock_armed_below_entry",
                                   max_fav=mf, entry=entry, side=side)

        # (3) A long stop at/above bid (mirror: short stop at/below ask) when a
        #     stop is set (PLACE / TRAIL_ADVANCE).
        if et in (PLACE, TRAIL_ADVANCE):
            stop = record.get("stop_price")
            bid = record.get("current_bid")
            ask = record.get("current_ask")
            if stop is not None:
                if str(side).upper() == "SELL" and ask is not None and stop <= ask:
                    self.violation(ticket, anchor, "short_stop_at_or_below_ask",
                                   stop=stop, ask=ask)
                elif str(side).upper() != "SELL" and bid is not None and stop >= bid:
                    self.violation(ticket, anchor, "long_stop_at_or_above_bid",
                                   stop=stop, bid=bid)

        # (4) lock_level jumped by more than one step between consecutive lines
        #     AND the jump is NOT justified by a max_fav that actually reached the
        #     new rung's price. A genuine fast move legitimately skips rungs (the
        #     ladder picks the HIGHEST applicable tier), so only an UNJUSTIFIED
        #     skip -- a lock level that ran ahead of the price -- is corruption.
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

    # Dollar fav each lock rung REQUIRES (mirror strategy.lock_ladder_prices).
    _RUNG_FAV = {1: 5.0, 2: 6.0, 3: 10.0}

    def _level_justified(self, record: Dict, level: int) -> bool:
        """True if max_fav actually reached `level`'s required price. Unknown data
        (missing max_fav/entry) -> treated as justified so we never FALSE-flag a
        legitimate fast move; the below-entry lock assert (#2) covers corruption."""
        mf = record.get("max_fav")
        entry = record.get("position_price")
        if mf is None or entry is None:
            return True
        need = self._RUNG_FAV.get(int(level), self._RUNG_FAV[3])
        _s = -1.0 if str(record.get("side")).upper() == "SELL" else 1.0
        return _s * (mf - entry) >= need - 0.011
