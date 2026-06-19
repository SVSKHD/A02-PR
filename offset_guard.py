"""AUREON — Monday weekend-wake offset guard (pure, shared).

WHY THIS EXISTS
---------------
On Mondays, at wake-from-weekend-sleep, the broker tick-time offset could be
misdetected (the Jun-8 incident: offset silently fell back to 0h when
MetaQuotes-Demo is UTC+3), so get_m5_close queried the wrong window and A1
(scheduled 5:00 AM IST) drifted. Same disease as phantom-lock / late-fire:
acting on bad data instead of rejecting + retrying.

THE GUARD (the only logic change, surfaced here as ONE shared function so live,
backtest, and selftest call the SAME code — import-path identity):
  1. Do NOT trust a single tick's offset and do NOT fall back to 0h/default.
  2. Re-derive the offset from a fresh valid tick.
  3. Validate the derived offset against the expected broker tz (EXPECTED_OFFSET
     = +3, MetaQuotes-Demo UTC+3). On mismatch, retry up to OFFSET_RETRY_MAX.
  4. Proceed to A1 ONLY once the offset is confirmed sane; if still unresolved
     after the retries, BLOCK placement (never place on a guessed offset).

Pure: no MT5, no network. The live adapter still does the real tick reads; this
module is the decision + the constants, so it is trivially testable.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

# Pinned constants (spec). Do NOT change A1's schedule or monday_a1_override.
WEEKEND_GAP_HOURS = 24      # a first-tick gap larger than this == weekend wake
EXPECTED_OFFSET = 3         # MetaQuotes-Demo is UTC+3
OFFSET_RETRY_MAX = 3        # re-derive attempts before BLOCKING placement
A1_SCHEDULED_IST_MIN = 5 * 60   # A1 = 05:00 AM IST (minutes since IST midnight)
A1_DRIFT_TOL_MIN = 10       # |implied - scheduled| beyond this == drift

CONFIRMED = "CONFIRMED"
RETRY = "RETRY"
BLOCKED = "BLOCKED"


def derive_offset_hours(tick_epoch: float, now_epoch: float) -> int:
    """Whole-hours offset implied by one tick vs the wall clock (the primitive a
    single read yields). Ambiguous on its own -- must be validated, never trusted
    blindly (that is the whole point of the guard)."""
    return round((float(tick_epoch) - float(now_epoch)) / 3600.0)


def weekend_gap_hours(last_tick_epoch: float, first_tick_epoch: float) -> float:
    """Hours between the last pre-sleep tick and the first wake tick."""
    return (float(first_tick_epoch) - float(last_tick_epoch)) / 3600.0


def is_weekend_wake(gap_hours: Optional[float],
                    threshold: float = WEEKEND_GAP_HOURS) -> bool:
    """True when the first-tick gap marks a weekend wake (the path that must
    re-derive + validate the offset). Weekday opens have no such gap."""
    return gap_hours is not None and gap_hours > threshold


def offset_confirmed(derived: Optional[int], expected: int = EXPECTED_OFFSET) -> bool:
    """True iff a derived offset equals the expected broker tz."""
    return derived is not None and int(derived) == int(expected)


def resolve_offset(derived_sequence: Sequence[Optional[int]],
                   expected: int = EXPECTED_OFFSET,
                   retry_max: int = OFFSET_RETRY_MAX
                   ) -> Tuple[Optional[int], str, int]:
    """Re-derive + validate + retry. `derived_sequence` is what each successive
    fresh-tick read derives (the live adapter supplies real reads; tests supply a
    scripted sequence). Returns (offset|None, result, attempts):
      - first read that equals `expected` -> (expected, CONFIRMED, attempt)
      - exhausted without a match         -> (None, BLOCKED, retry_max)
    NEVER returns a guessed/0h offset -- an unresolved offset is BLOCKED so the
    caller refuses to place A1 on bad data."""
    seq: List[Optional[int]] = list(derived_sequence)
    attempts = 0
    for i in range(max(1, int(retry_max))):
        attempts = i + 1
        derived = seq[i] if i < len(seq) else (seq[-1] if seq else None)
        if offset_confirmed(derived, expected):
            return int(expected), CONFIRMED, attempts
    return None, BLOCKED, attempts


def a1_drifted(implied_ist_min: float,
               scheduled_ist_min: float = A1_SCHEDULED_IST_MIN,
               tol_min: float = A1_DRIFT_TOL_MIN) -> bool:
    """True when A1's implied IST fire-time has drifted from its 5:00 schedule by
    more than the tolerance -- the tripwire that fires BEFORE A1 places."""
    return abs(float(implied_ist_min) - float(scheduled_ist_min)) > float(tol_min)


def fmt_hhmm(ist_min: float) -> str:
    """Minutes-since-IST-midnight -> 'HHMM' (e.g. 300 -> '0500')."""
    m = int(round(ist_min)) % (24 * 60)
    return f"{m // 60:02d}{m % 60:02d}"
