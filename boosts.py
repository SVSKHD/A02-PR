"""AUREON v3.2.0 — lone-leg boost trigger: the SINGLE source of truth.

THE BUG this fixes: lone-leg boosts were fired AT THE LEG'S FILL PRICE the moment
the sibling filled (old fire-at-fill path in fills.py), in the sibling's
direction, always labelled "RESCUE" — even when the leg had WON. On A3 (Jun 18)
that placed boosts at 4266.30 (= the fill) which then died on a reversal (~-$900).

THE RULE (live, backtest, tests ALL call plan_boost_event — never a parallel copy):
  Record the lone leg's fill price. Do NOT fire any boost at/near it. Watch
  (current_price vs fill); once the leg has moved a full trigger ($10):
    leg WINNING by +$trigger  -> RALLY  : 2 boosts SAME direction as the leg,
                                          entered ~fill +trigger (a winning pyramid)
    leg LOSING  by -$trigger  -> RESCUE : 2 boosts OPPOSITE the leg, entered
                                          ~fill -trigger (hedge the breakout)
    neither                   -> None   : no boosts; the leg runs on its own SL/TP
  ONE event per leg (whichever $10 hits first). HARD GUARD: a returned plan's
  entry is always >= ~$trigger from the fill in the correct direction; if that
  can't hold (a near-fill entry) the plan is BLOCKED (None) and an error logged —
  this is the structural defense against the fire-at-fill bug.

Boost geometry (placement) and the breath-gap trail / $10 backstop / $8 floor /
isolation / -$700 cap live in strategy._update_boost_on_bar and the caller; this
module is the pure TRIGGER decision so it is trivially testable and shared.
"""
import logging
from dataclasses import dataclass

log = logging.getLogger("AUREON")

# epsilon so float noise can't let an at-fill entry slip past the >= trigger guard
_EPS = 1e-6


def _trigger(cfg):
    return float(getattr(cfg, "boost_trigger_dollars", 10.0))


def _opposite(side):
    return "SELL" if side == "BUY" else "BUY"


@dataclass
class BoostPlan:
    kind: str          # "RALLY" | "RESCUE"
    event_type: str    # "RALLY_BOOST" | "RESCUE_BOOST"
    boost_side: str    # direction the boosts are placed in
    entry_ref: float   # ~ current price (market) -- always >= trigger from fill
    n: int             # number of boosts
    sl_dollars: float  # boost hard-SL distance ($10 backstop)
    tp_dollars: float  # boost TP distance
    move_dollars: float  # signed leg excursion at trigger (for logging)


def plan_boost_event(leg_side, leg_fill_price, current_price, cfg):
    """Decide whether (and how) to fire the lone leg's boost event RIGHT NOW, given
    the leg's fill price and the CURRENT price. Returns a BoostPlan or None. Pure;
    never raises; never fires at the fill. See module docstring for the rule."""
    try:
        leg_fill_price = float(leg_fill_price)
        current_price = float(current_price)
    except (TypeError, ValueError):
        return None
    trig = _trigger(cfg)
    n = int(getattr(cfg, "rescue_boost_count", 2))
    sl_d = float(getattr(cfg, "boost_sl_dollars", 10.0))
    tp_d = float(getattr(cfg, "tp_dist", 30.0))

    # leg excursion in the leg's own favor ($): >0 winning, <0 losing.
    leg_fav = (current_price - leg_fill_price) if leg_side == "BUY" \
        else (leg_fill_price - current_price)

    if leg_fav >= trig - _EPS:
        kind, etype, side = "RALLY", "RALLY_BOOST", leg_side               # winning -> pyramid
    elif leg_fav <= -(trig - _EPS):
        kind, etype, side = "RESCUE", "RESCUE_BOOST", _opposite(leg_side)  # losing -> hedge
    else:
        return None                                                         # < $10: never fire

    # HARD GUARD: the boost entry (≈ current market) MUST be >= trigger from the
    # leg fill in the correct direction. This is what makes the A3 fire-at-fill
    # bug structurally impossible -- a near-fill entry is blocked, not placed.
    if abs(current_price - leg_fill_price) < trig - _EPS:
        log.error(
            f"BOOST BLOCKED: {kind} entry {current_price:.2f} is only "
            f"${abs(current_price - leg_fill_price):.2f} from leg fill "
            f"{leg_fill_price:.2f} (need >= ${trig:.0f}) -- refusing to fire at fill.")
        return None

    return BoostPlan(kind=kind, event_type=etype, boost_side=side,
                     entry_ref=round(current_price, 2), n=n, sl_dollars=sl_d,
                     tp_dollars=tp_d, move_dollars=round(leg_fav, 2))


def boost_whipsaw_cap(cfg):
    """The hard combined-boost loss cap ($), = n x $sl x lot x contract. A boost
    event whose combined realized+open loss reaches -cap closes remaining boosts."""
    return (int(getattr(cfg, "rescue_boost_count", 2))
            * float(getattr(cfg, "boost_sl_dollars", 10.0))
            * float(getattr(cfg, "lot_size", 0.35))
            * float(getattr(cfg, "contract_size", 100.0)))


def cap_breached(combined_boost_pnl, cfg):
    """True when the boosts' combined P&L has reached/breached the -cap (hard
    close). Clamp-on-breach -- binds even when a single boost slipped past its SL."""
    try:
        return float(combined_boost_pnl) <= -boost_whipsaw_cap(cfg) + _EPS
    except (TypeError, ValueError):
        return False
