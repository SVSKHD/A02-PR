"""AUREON — break-and-hold filter (v3.2.4 Feature D: the profit decider).

WHY THIS EXISTS
---------------
Boosts fired on the FIRST/weak break got chopped up by fake-outs (the 14:30 /
15:30 spike-and-reverse) -- the -$700 least case. The decider: do NOT stack on the
first break. A break QUALIFIES to stack only if ALL hold:
  (a) price clears the range edge by >= break_dist_x,
  (b) price HOLDS beyond the edge for >= hold_candles_n M5 candles,
  (c) retrace during the hold < max_retrace_y of the break distance.
A spike that reverses inside the window is a FAILED break -> fire nothing. After a
failed up-spike the caller re-evaluates the DOWN side and stacks on THAT if it
passes the same hold test (continuation).

States:  CANDIDATE (edge cleared, still watching) / CONFIRMED (held -> stack) /
         FAILED (reversed | retrace>Y | hold<N after window).

PURE: no MT5, no state. Shared by live + backtest (import-path identity).
"""
from __future__ import annotations

from typing import Dict, Sequence, Tuple

CONFIRMED = "CONFIRMED"   # cleared + held + retrace ok -> boosts may stack
FAILED = "FAILED"         # reversed through the edge / retraced too far -> fire nothing
CANDIDATE = "CANDIDATE"   # edge cleared but not yet held N candles -> keep watching
# v3.2.3 alias (PENDING) kept so nothing importing the old name breaks.
PENDING = CANDIDATE


def _sgn(side: str) -> float:
    return 1.0 if side == "BUY" else -1.0


def classify(side: str, break_level: float,
             candles: Sequence[Dict], cfg) -> Tuple[str, str]:
    """Classify a break given the M5 candles AFTER it. `candles` = list of
    {'high','low','close'} in order. Returns (state, reason).

      CONFIRMED: favorable extreme cleared the edge by >= break_dist_x, at least
                 hold_candles_n candles printed, no candle fell back THROUGH the
                 edge, and the worst pullback stayed < max_retrace_y of the break.
      FAILED:    reason 'reversed' (fell back through the edge), 'retrace' (pulled
                 back >= max_retrace_y), or 'hold<N' (cleared then faded before N).
      CANDIDATE: edge not yet cleared, or cleared with fewer than N candles so far.
    """
    X = float(getattr(cfg, "break_dist_x", 3.0))
    N = int(getattr(cfg, "hold_candles_n", 2))
    Y = float(getattr(cfg, "max_retrace_y", 0.40))
    s = _sgn(side)
    cs = list(candles)
    if not cs:
        return CANDIDATE, "no_candles"

    fav_extremes = [(float(c["high"]) if s > 0 else float(c["low"])) for c in cs]
    peak = max(fav_extremes) if s > 0 else min(fav_extremes)
    cleared = s * (peak - break_level) >= X
    if not cleared:
        return CANDIDATE, "edge_not_cleared"

    # reversal: any candle's ADVERSE extreme fell back THROUGH the edge -> fake-out.
    for c in cs:
        adverse = float(c["low"]) if s > 0 else float(c["high"])
        if s * (adverse - break_level) < 0:
            return FAILED, "reversed"

    if len(cs) < N:
        return CANDIDATE, "watching"   # cleared, holding, but window not yet up

    # retrace: worst pullback from the peak vs the break distance.
    break_dist = abs(peak - break_level)
    if break_dist <= 0:
        return CANDIDATE, "edge_not_cleared"
    worst_pullback = 0.0
    for c in cs:
        adverse = float(c["low"]) if s > 0 else float(c["high"])
        worst_pullback = max(worst_pullback, s * (peak - adverse))
    if worst_pullback / break_dist >= Y:
        return FAILED, "retrace"

    return CONFIRMED, "held"


def evaluate_break(side: str, break_level: float,
                   candles: Sequence[Dict], cfg) -> str:
    """State only (CANDIDATE / CONFIRMED / FAILED) -- back-compat wrapper."""
    return classify(side, break_level, candles, cfg)[0]


def may_stack(side: str, break_level: float, candles: Sequence[Dict], cfg) -> bool:
    """Boost-trigger gate: True ONLY on a CONFIRMED break (filter disabled ->
    always True = legacy behavior)."""
    if not bool(getattr(cfg, "break_and_hold_enabled", True)):
        return True
    return evaluate_break(side, break_level, candles, cfg) == CONFIRMED
