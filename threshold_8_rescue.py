"""threshold_8 — Module B: Rescue Manager.

Deterministic, pure logic. Evaluated per M1 bar on the basket's ENTRY leg. When the
entry runs adverse by RESCUE_DIST it arms ONE opposite counter-leg. That is the whole
job. It never places a second leg on the entry side (no averaging), never scales lot
with loss (no martingale), never arms more than once per side, and never touches the
entry leg's stop.

This module makes a DECISION and describes the order. It does not touch a broker or a
CSV — the replay engine (the single order-placement boundary) executes the returned
``RescueOrder`` by creating the leg. That keeps order placement in one place and keeps
this module trivially unit-testable.

Forbidden (asserted in tests):
  * no averaging      — the counter-leg is always the OPPOSITE side of ENTRY
  * no martingale     — lot = round(BASE_LOT * RESCUE_LOT_MULT, 2), nothing else
  * one RESCUE/side   — guarded by rescue_max_per_side
  * entry SL is sacred — this module never reads or writes entry.sl
"""

from threshold_8_basket import ROLE_RESCUE, opposite, BUY


class RescueOrder:
    """A fully-specified counter-leg the engine should open at market. The lot is
    already resolved as a pure function of config; the engine only chooses the fill
    price (current market) and assigns a ticket."""
    __slots__ = ("side", "lot", "sl_dist", "role")

    def __init__(self, side, lot, sl_dist):
        self.side = side
        self.lot = lot
        self.sl_dist = sl_dist
        self.role = ROLE_RESCUE


class RescueDecision:
    __slots__ = ("armed", "reason", "order", "adverse_move")

    def __init__(self, armed, reason, order=None, adverse_move=0.0):
        self.armed = armed
        self.reason = reason
        self.order = order
        self.adverse_move = adverse_move


def _adverse_move(entry_side, entry_price, m1_bar, require_close):
    """$ the ENTRY leg is under water at this bar.

    require_close=True  -> measure at the M1 CLOSE (a wick beyond the level does not
                           arm; INVARIANT / check: rescue never arms on a wick).
    require_close=False -> measure at the adverse extreme of the bar.
    """
    if require_close:
        px = m1_bar.close
    else:
        # adverse extreme: for a BUY entry the pain is price falling -> use the low;
        # for a SELL entry the pain is price rising -> use the high.
        px = m1_bar.low if entry_side == BUY else m1_bar.high
    if entry_side == BUY:
        return entry_price - px
    return px - entry_price


class Threshold8RescueManager:
    def __init__(self, params):
        self.p = params

    def evaluate(self, basket, m1_bar, now):
        """Return a RescueDecision for this M1 bar. ``armed`` True means the engine
        should open ``decision.order``.

        Arm condition (all must hold):
          RESCUE_ENABLED
          adverse_move >= RESCUE_DIST                     (M1 close if REQUIRE_CLOSE)
          rescue legs on the opposite side < MAX_PER_SIDE
          now - entry.opened_at >= RESCUE_MIN_GAP_MIN
          basket.net_pnl > -BASKET_MAX_RISK
        """
        p = self.p
        if not p.rescue_enabled:
            return RescueDecision(False, "disabled")

        entry = basket.entry_leg()
        if entry is None or not entry.is_open:
            # Nothing to rescue if the entry leg never filled or already stopped out.
            return RescueDecision(False, "no_open_entry")

        adverse = _adverse_move(entry.side, entry.entry_price, m1_bar,
                                p.rescue_require_close)

        if adverse < p.rescue_dist:
            return RescueDecision(False, "not_adverse_enough", adverse_move=adverse)

        rescue_side = opposite(entry.side)
        if len(basket.rescue_legs_on_side(rescue_side)) >= p.rescue_max_per_side:
            return RescueDecision(False, "max_per_side", adverse_move=adverse)

        gap_min = (now - entry.opened_at).total_seconds() / 60.0
        if gap_min < p.rescue_min_gap_min:
            return RescueDecision(False, "min_gap", adverse_move=adverse)

        if basket.net_pnl <= -p.basket_max_risk:
            # basket is already at/through max risk; the exit engine owns it now.
            return RescueDecision(False, "past_max_risk", adverse_move=adverse)

        # Arm. Lot is a pure function of BASE_LOT and RESCUE_LOT_MULT.
        order = RescueOrder(side=rescue_side, lot=p.rescue_lot(), sl_dist=p.sl)
        return RescueDecision(True, "armed", order=order, adverse_move=adverse)
