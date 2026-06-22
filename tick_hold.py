"""AUREON — tick-hold confirm + A1 sane-tick anchor capture (v3.2.5).

WHY THIS EXISTS
---------------
Two Monday-open / blip failures, both ADDITIVE fixes (nothing in tests 1-73 or the
A2/A3/A4 bar-capture path changes):

  Feature 1 (A1 tick-fallback). At the Monday/post-weekend open the M5 BAR lags
  (not published yet) even though TICKS are live. A1 captured from the bar -> no bar
  -> "NOT placing" -> ANCHOR MISSED. The fix: if A1's M5 bar is still missing after
  the existing retries, FALL BACK to a SANE, SETTLED live tick (passes the jump
  filter AND has HELD >= hold_ticks) and place off that. A1 + open path ONLY.

  Feature 2 (tick-hold on boost/trail). Boost/trail run on tick refresh (~0.3s). A
  +/-$10 cross fires ONLY after it HOLDS >= hold_ticks (~1s); a cross that reverts
  within the window is a blip -> NO fire. A trail lock advances only on a held, sane
  max_fav -- reinforcing the phantom-lock guard (a single spike tick never locks).

This module is PURE: no MT5, no clocks, no state. It is the SINGLE source of the
hold decisions; live (anchors/fills/trails) feeds it ticks, selftest drives it with
fixtures, so both honor the exact same rule (import-path identity).
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

HOLD_TICKS_DEFAULT = 3

# tick-cross states (Feature 2)
IDLE = "IDLE"            # not crossed, nothing pending
CANDIDATE = "CANDIDATE"  # crossed, watching (held < hold_ticks)
CONFIRMED = "CONFIRMED"  # crossed and HELD >= hold_ticks -> may fire
BLIP = "BLIP"            # reverted before hold_ticks -> reject


def hold_ticks(cfg) -> int:
    """Consecutive sane ticks a move must hold before it counts (default 3, ~1s)."""
    return max(1, int(getattr(cfg, "hold_ticks", HOLD_TICKS_DEFAULT)))


def _band(cfg) -> float:
    """Max sane gap between consecutive ticks (reuses the garbage-feed jump filter
    so a settled run can't include a stale/garbage spike)."""
    return float(getattr(cfg, "tick_hold_band", getattr(cfg, "max_tick_jump", 25.0)))


def tick_jump_ok(price: float, ref: Optional[float], cfg) -> bool:
    """A tick is sane if it does not jump more than max_tick_jump from `ref`
    (ref None -> nothing to compare against -> sane)."""
    if ref is None:
        return True
    try:
        return abs(float(price) - float(ref)) <= float(getattr(cfg, "max_tick_jump", 25.0))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Feature 1 — A1 sane-tick anchor capture (open path only)
# ---------------------------------------------------------------------------
def settle_anchor_tick(ticks: Sequence[float], cfg,
                       ref: Optional[float] = None) -> Tuple[bool, Optional[float], int, str]:
    """Pick a SANE, SETTLED anchor price from a sequence of recent live tick prices
    (oldest -> newest). Returns (ok, anchor_price, held_ticks, reason).

    The anchor is taken only when the LATEST run of consecutive ticks that stay
    within `band` of each other has length >= hold_ticks -- i.e. the feed has
    SETTLED, not the first wild reopen spike. A leading spike (gap > band from the
    settled run) is excluded from the run and NEVER becomes the anchor. If a `ref`
    is supplied (e.g. last known price), the settled price must also pass the jump
    filter against it."""
    n = hold_ticks(cfg)
    try:
        ts = [float(t) for t in ticks]
    except (TypeError, ValueError):
        return False, None, 0, "bad_ticks"
    if len(ts) < n:
        return False, None, len(ts), "insufficient_ticks"
    band = _band(cfg)
    # grow a run backwards from the newest tick while consecutive ticks are sane.
    run = [ts[-1]]
    for p in reversed(ts[:-1]):
        if abs(run[-1] - p) <= band:
            run.append(p)
        else:
            break   # a gap (spike) terminates the settled run
    held = len(run)
    if held < n:
        return False, None, held, "not_held"
    price = ts[-1]   # settled price = the newest held tick
    if not tick_jump_ok(price, ref, cfg):
        return False, None, held, "jump_from_ref"
    return True, round(price, 2), held, "held"


# ---------------------------------------------------------------------------
# Feature 2 — tick-hold confirm on boost cross + trail advance
# ---------------------------------------------------------------------------
def step_cross(streak: int, crossed: bool, cfg) -> Tuple[int, str]:
    """Advance a per-leg cross streak by ONE tick.

    `crossed` is True when the leg is currently at/over its +/-$10 trigger THIS tick.
      crossed, streak+1 >= hold_ticks -> (streak+1, CONFIRMED)  # fire
      crossed, streak+1 <  hold_ticks -> (streak+1, CANDIDATE)  # keep watching
      not crossed, streak  > 0        -> (0, BLIP)              # reverted -> reject
      not crossed, streak == 0        -> (0, IDLE)
    """
    n = hold_ticks(cfg)
    if crossed:
        s = int(streak) + 1
        return s, (CONFIRMED if s >= n else CANDIDATE)
    return 0, (BLIP if int(streak) > 0 else IDLE)


def confirm_cross(tick_crosses: Sequence[bool], cfg) -> Tuple[bool, int, str]:
    """Replay a sequence of per-tick `crossed` booleans through step_cross and
    return (fired, final_streak, final_state). `fired` True iff a CONFIRMED state
    was reached without an intervening BLIP that broke the run before it."""
    streak, state, fired = 0, IDLE, False
    for c in tick_crosses:
        streak, state = step_cross(streak, c, cfg)
        if state == CONFIRMED:
            fired = True
            break
    return fired, streak, state


def trail_advance_ok(held_streak: int, cfg) -> bool:
    """A trail lock advances only after the favorable extreme has HELD >= hold_ticks
    consecutive sane ticks. A single spike tick -> streak 1 -> no advance. This
    reinforces the existing phantom-lock guard (lock off a held real move, never a
    ghost), it does NOT replace it."""
    return int(held_streak) >= hold_ticks(cfg)
