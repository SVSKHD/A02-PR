"""AUREON — break-and-hold filter (v3.2.3 Feature D: the profit decider).

WHY THIS EXISTS
---------------
Boosts fired on the FIRST break got chopped up by fake-outs (the 14:30 / 15:30
spike-and-reverse). The decider: do NOT stack on the first break. Stack ONLY if
price (a) clears the range edge by >= break_dist_x, (b) HOLDS hold_candles_n M1
candles past the edge, and (c) retraces less than max_retrace_y of the break
distance. A spike that reverses back through the edge inside the window is a
FAILED break -> fire nothing. After a failed up-spike the caller re-evaluates the
DOWN side and stacks on THAT if it holds.

PURE: no MT5, no state. `evaluate_break` takes the post-break M1 candles and
returns CONFIRMED / FAILED / PENDING. Shared by live + selftest (identity).
"""
from __future__ import annotations

from typing import Dict, List, Sequence

CONFIRMED = "CONFIRMED"   # cleared + held + retrace ok -> boosts may stack
FAILED = "FAILED"         # reversed through the edge or retraced too far -> fire nothing
PENDING = "PENDING"       # not enough candles yet / edge not cleared -> wait


def _sgn(side: str) -> float:
    return 1.0 if side == "BUY" else -1.0


def evaluate_break(side: str, break_level: float,
                   candles: Sequence[Dict], cfg) -> str:
    """Classify a break given the M1 candles AFTER it. `candles` = list of
    {'high','low','close'} in order. Returns CONFIRMED / FAILED / PENDING.

      CONFIRMED: the favorable extreme cleared the edge by >= break_dist_x, at
                 least hold_candles_n candles have printed, no candle's extreme
                 fell back THROUGH the edge during the hold, and the worst pullback
                 from the peak stayed < max_retrace_y of the break distance.
      FAILED:    cleared but then reversed back through the edge within the window,
                 OR retraced >= max_retrace_y (a fake-out).
      PENDING:   edge not yet cleared, or fewer than hold_candles_n candles.
    """
    X = float(getattr(cfg, "break_dist_x", 2.0))
    N = int(getattr(cfg, "hold_candles_n", 2))
    Y = float(getattr(cfg, "max_retrace_y", 0.50))
    s = _sgn(side)
    cs = list(candles)
    if not cs:
        return PENDING

    # favorable extreme of each candle (BUY: high, SELL: low) and the peak so far.
    fav_extremes = [(float(c["high"]) if s > 0 else float(c["low"])) for c in cs]
    peak = max(fav_extremes) if s > 0 else min(fav_extremes)
    cleared = s * (peak - break_level) >= X
    if not cleared:
        # never cleared the edge by X -> still pending until the window is up, then
        # it simply never broke (treat as PENDING -> caller fires nothing either way).
        return PENDING

    if len(cs) < N:
        return PENDING

    # reversal: any candle's ADVERSE extreme fell back through the edge during the
    # hold window -> a fake-out break.
    for c in cs:
        adverse = float(c["low"]) if s > 0 else float(c["high"])
        if s * (adverse - break_level) < 0:
            return FAILED

    # retrace: worst pullback from the peak vs the break distance.
    break_dist = abs(peak - break_level)
    if break_dist <= 0:
        return PENDING
    worst_pullback = 0.0
    for c in cs:
        adverse = float(c["low"]) if s > 0 else float(c["high"])
        worst_pullback = max(worst_pullback, s * (peak - adverse))
    if worst_pullback / break_dist >= Y:
        return FAILED

    return CONFIRMED


def may_stack(side: str, break_level: float, candles: Sequence[Dict], cfg) -> bool:
    """Convenience gate for the boost trigger: True ONLY on a CONFIRMED break (and
    only when the filter is enabled; disabled -> always True, legacy behavior)."""
    if not bool(getattr(cfg, "break_and_hold_enabled", True)):
        return True
    return evaluate_break(side, break_level, candles, cfg) == CONFIRMED
