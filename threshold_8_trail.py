"""threshold_8 — Module C: Dynamic Trail Engine.

Operates on ``basket.net_pnl`` and the dominant leg's price. It does not close the
basket itself — it computes the trail state (engaged?, locked floor, stop price) and
records whether the trail-exit condition is met. The single exit-precedence function
(Module D) is the only place that actually closes a basket, so precedence order is
never bypassed.

Modes:
  ladder : locked = max(lock for (trigger,lock) in LADDER if peak_net >= trigger).
           Engage when net_pnl >= first ladder trigger. Exit when net_pnl <= locked.
  atr    : stop_price trails the dominant leg by ATR_MULT * ATR(closed M5), monotonic
           in the favourable direction only, stepped by TRAIL_STEP. Engage when
           net_pnl >= TRAIL_ACTIVATE. Exit when price crosses the stop.
  hybrid : locked floor from ladder AND stop_price from atr; whichever binds first.

Guarantees:
  * ``basket.trail_locked`` is monotonic NON-DECREASING for the life of the basket
    (INVARIANT 9). A basket that reached ladder rung K can never close below rung-K
    lock except on a gap bar (checked in the backtest post-pass, C10).
  * With TRAIL_ANCHOR_SWING the atr stop may never be placed beyond the most recent
    opposite swing; it may only tighten.
"""

from threshold_8_basket import BUY, STATE_TRAILING, STATE_RESCUED, STATE_OPEN


class TrailState:
    __slots__ = ("engaged", "locked", "stop_price", "rung", "exit_now", "reason")

    def __init__(self, engaged, locked, stop_price, rung, exit_now, reason):
        self.engaged = engaged
        self.locked = locked
        self.stop_price = stop_price
        self.rung = rung
        self.exit_now = exit_now
        self.reason = reason


def _step_floor(value, step):
    """Round ``value`` DOWN onto the step grid (for a long stop)."""
    return (value // step) * step


def _step_ceil(value, step):
    """Round ``value`` UP onto the step grid (for a short stop)."""
    import math
    return math.ceil(value / step) * step


class Threshold8TrailEngine:
    def __init__(self, params):
        self.p = params

    # --- ladder -------------------------------------------------------------------
    def _ladder_locked_and_rung(self, peak_net):
        p = self.p
        locked = None
        rung = 0
        for i, (trigger, lock) in enumerate(p.trail_ladder, start=1):
            if peak_net >= trigger:
                locked = lock if locked is None else max(locked, lock)
                rung = i
        return locked, rung

    # --- atr ----------------------------------------------------------------------
    def _atr_stop(self, basket, price, atr, swing_price, prev_stop):
        """Compute the trailed stop price for the dominant leg. Monotonic in the
        favourable direction; clamped to the last opposite swing when enabled."""
        p = self.p
        if atr is None or atr <= 0:
            return prev_stop
        gap = p.trail_atr_mult * atr
        dom = basket.dominant_side
        if dom == BUY:
            raw = _step_floor(price - gap, p.trail_step)
            if p.trail_anchor_swing and swing_price is not None:
                # opposite swing for a long is the last swing LOW; never place the
                # stop above it (that would be trailing past the swing).
                raw = min(raw, swing_price)
            if prev_stop is not None:
                raw = max(raw, prev_stop)        # tighten only -> monotonic up
            return raw
        else:
            raw = _step_ceil(price + gap, p.trail_step)
            if p.trail_anchor_swing and swing_price is not None:
                raw = max(raw, swing_price)
            if prev_stop is not None:
                raw = min(raw, prev_stop)        # tighten only -> monotonic down
            return raw

    def _atr_exit(self, basket, price, stop_price):
        if stop_price is None:
            return False
        if basket.dominant_side == BUY:
            return price <= stop_price
        return price >= stop_price

    # --- main ---------------------------------------------------------------------
    def update(self, basket, price, atr, swing_price):
        """Recompute the trail for this bar and stamp it onto the basket.

        Returns a TrailState. ``exit_now`` True means the trail-exit condition is met;
        the exit-precedence function decides whether it actually fires (order 5)."""
        p = self.p
        mode = p.trail_mode

        first_trigger = p.trail_ladder[0][0]
        engage_by_ladder = basket.net_pnl >= first_trigger
        engage_by_atr = basket.net_pnl >= p.trail_activate

        if mode == "ladder":
            engaged = engage_by_ladder
        elif mode == "atr":
            engaged = engage_by_atr
        else:  # hybrid: engaged once either engages
            engaged = engage_by_ladder or engage_by_atr

        if not engaged and not basket.trail_engaged:
            # never engaged yet
            return TrailState(False, basket.trail_locked, basket.trail_stop_price,
                              basket.trail_rung_reached, False, "not_engaged")

        # once engaged, stay engaged for the life of the basket
        basket.trail_engaged = True
        if basket.state in (STATE_OPEN, STATE_RESCUED):
            basket.state = STATE_TRAILING

        exit_now = False
        reasons = []

        # ladder / hybrid: locked floor
        if mode in ("ladder", "hybrid"):
            locked, rung = self._ladder_locked_and_rung(basket.peak_net)
            if locked is not None:
                # monotonic non-decreasing
                if basket.trail_locked is None or locked > basket.trail_locked:
                    basket.trail_locked = locked
                if rung > basket.trail_rung_reached:
                    basket.trail_rung_reached = rung
            if basket.trail_locked is not None and basket.net_pnl <= basket.trail_locked:
                exit_now = True
                reasons.append("ladder_lock")

        # atr / hybrid: stop price
        if mode in ("atr", "hybrid"):
            new_stop = self._atr_stop(basket, price, atr, swing_price,
                                      basket.trail_stop_price)
            basket.trail_stop_price = new_stop
            if self._atr_exit(basket, price, new_stop):
                exit_now = True
                reasons.append("atr_stop")

        basket._trail_exit = exit_now
        return TrailState(True, basket.trail_locked, basket.trail_stop_price,
                          basket.trail_rung_reached, exit_now, "+".join(reasons) or "engaged")
