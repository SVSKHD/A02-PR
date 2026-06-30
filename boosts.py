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


def boost_sl_for(cfg, kind):
    """v3.3.3: the boost hard-SL/backstop ($) for this event KIND -- per-kind, NOT
    one shared value. RALLY -> rally_boost_sl ($13, owner-widened); RESCUE (and any
    unknown/legacy kind) -> boost_sl_dollars ($10, unchanged). Both placement and the
    whipsaw cap read THIS so a rally event uses $13 and a rescue event uses $10."""
    if str(kind).upper() == "RALLY":
        return float(getattr(cfg, "rally_boost_sl", 13.0))
    # v3.5.0 feature 14: rescue_sl_wide widens the RESCUE SL $10 -> $13. Because BOTH the
    # placement and the whipsaw cap read THIS function, the derived cap moves with it
    # (-$700 -> -$910) automatically. DEFAULT OFF -> boost_sl_dollars ($10, byte-identical).
    if bool(getattr(cfg, "rescue_sl_wide", False)):
        return float(getattr(cfg, "rescue_sl_wide_dollars", 13.0))
    return float(getattr(cfg, "boost_sl_dollars", 10.0))


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


def plan_boost_event(leg_side, leg_fill_price, current_price, cfg,
                     allow_rally=True, allow_rescue=True):
    """Decide whether (and how) to fire the leg's boost event RIGHT NOW, given the
    leg's fill price and the CURRENT price. Returns a BoostPlan or None. Pure;
    never raises; never fires at the fill. See module docstring for the rule.

    v3.2.3: `allow_rally`/`allow_rescue` are PER-CALL gates (in addition to the
    global cfg flags). A No-OCO straddle leg passes allow_rescue=False so only the
    WINNING side stacks (RALLY) while the losing leg rides to its SL -- the A3
    mechanic. Lone legs pass both True (rally OR rescue). Defaults keep every
    existing caller unchanged."""
    try:
        leg_fill_price = float(leg_fill_price)
        current_price = float(current_price)
    except (TypeError, ValueError):
        return None
    # v3.2.8 Phase 1: the arm is now ASYMMETRIC by direction. A WINNING leg arms
    # the RALLY pyramid at +rally_arm_fav ($5); a LOSING leg arms the RESCUE hedge
    # at -rescue_arm ($10, the unchanged boost_trigger_dollars). Rescue keeps the
    # v3.2.7 $10 arm exactly -> the RESCUE branch below is byte-identical.
    rescue_arm = _trigger(cfg)                                  # losing-side: $10 (unchanged)
    rally_arm = float(getattr(cfg, 'rally_arm_fav', 5.0))       # winning-side: $5 (Phase 1)
    # v3.2.3: stack_depth (if set) controls the winning-side stack: depth 1 = base
    # (0 boosts => no event), depth 3 = original + 2 boosts. #winners is hard-capped
    # at 3, so n boosts is capped at 2. None => the legacy rescue_boost_count.
    _depth = getattr(cfg, "stack_depth", None)
    if _depth is not None:
        n = max(0, min(3, int(_depth)) - 1)
    else:
        n = int(getattr(cfg, "rescue_boost_count", 2))
    tp_d = float(getattr(cfg, "tp_dist", 30.0))

    # leg excursion in the leg's own favor ($): >0 winning, <0 losing.
    leg_fav = (current_price - leg_fill_price) if leg_side == "BUY" \
        else (leg_fill_price - current_price)

    if leg_fav >= rally_arm - _EPS:
        kind, etype, side = "RALLY", "RALLY_BOOST", leg_side               # winning +$5 -> pyramid
        arm_used = rally_arm
    elif leg_fav <= -(rescue_arm - _EPS):
        kind, etype, side = "RESCUE", "RESCUE_BOOST", _opposite(leg_side)  # losing -$10 -> hedge
        arm_used = rescue_arm
    else:
        return None                                                         # in the dead band: never fire

    # v3.2.2: INDEPENDENT on/off gating. RALLY and RESCUE each have their own
    # toggle; a disabled kind fires ZERO boosts (return None) so the leg runs on
    # its own SL/TP/trail. Defaults True => behavior unchanged. This is the SINGLE
    # source -- live (_check_boost_triggers) and backtest (run_month) both call
    # this fn, so both honor the flags with no separate copy.
    # v3.2.3: per-call allow_* gates layer on top (No-OCO straddle = rally-only).
    if n <= 0:
        return None  # stack_depth=1 (base): no boosts, the leg runs on its own
    if kind == "RALLY" and (not allow_rally
                            or not bool(getattr(cfg, "rally_boosts_enabled", True))):
        return None
    if kind == "RESCUE" and (not allow_rescue
                             or not bool(getattr(cfg, "rescue_boosts_enabled", True))):
        return None

    # HARD GUARD: the boost entry (≈ current market) MUST be >= this kind's arm from
    # the leg fill in the correct direction. This is what makes the A3 fire-at-fill
    # bug structurally impossible -- a near-fill entry is blocked, not placed. The arm
    # is per-kind (RALLY $5 / RESCUE $10) so a valid +$5 rally is not wrongly blocked.
    if abs(current_price - leg_fill_price) < arm_used - _EPS:
        log.error(
            f"BOOST BLOCKED: {kind} entry {current_price:.2f} is only "
            f"${abs(current_price - leg_fill_price):.2f} from leg fill "
            f"{leg_fill_price:.2f} (need >= ${arm_used:.0f}) -- refusing to fire at fill.")
        return None

    # v3.3.3: SL is per-kind -- RALLY $13, RESCUE $10 (boost_sl_for), not shared.
    sl_d = boost_sl_for(cfg, kind)
    return BoostPlan(kind=kind, event_type=etype, boost_side=side,
                     entry_ref=round(current_price, 2), n=n, sl_dollars=sl_d,
                     tp_dollars=tp_d, move_dollars=round(leg_fav, 2))


def plan_trapped_late_rescue(leg_side, leg_fill_price, current_price, cfg):
    """F-B (PURE, flag-gated DEFAULT OFF): a TRAPPED No-OCO losing straddle leg may arm a
    CAPPED late-rescue hedge instead of riding naked to its full -$18 SL. Returns a
    BoostPlan (kind RESCUE, OPPOSITE the trapped leg, with its OWN hard SL
    trapped_rescue_sl_dollars) ONCE the leg is >= trapped_rescue_arm_dollars ADVERSE from
    its fill -- else None. With trapped_late_rescue_enabled OFF (default) this ALWAYS
    returns None, so the caller path is byte-identical (the loser rides to its SL).
    Never raises. Anchor-side decision only -- the caller guarantees no Rogue ticket."""
    if not bool(getattr(cfg, "trapped_late_rescue_enabled", False)):
        return None
    try:
        leg_fill_price = float(leg_fill_price)
        current_price = float(current_price)
    except (TypeError, ValueError):
        return None
    arm = float(getattr(cfg, "trapped_rescue_arm_dollars", 10.0))
    # leg excursion in the leg's OWN favor ($): < 0 means losing (trapped).
    leg_fav = (current_price - leg_fill_price) if leg_side == "BUY" \
        else (leg_fill_price - current_price)
    if leg_fav > -(arm - _EPS):
        return None  # not yet trapped by the arm distance -> do not fire
    _depth = getattr(cfg, "stack_depth", None)
    if _depth is not None:
        n = max(0, min(3, int(_depth)) - 1)
    else:
        n = int(getattr(cfg, "rescue_boost_count", 2))
    if n <= 0:
        return None
    side = _opposite(leg_side)              # hedge OPPOSITE the trapped leg
    sl_d = float(getattr(cfg, "trapped_rescue_sl_dollars", 13.0))  # the hedge's OWN cap
    tp_d = float(getattr(cfg, "tp_dist", 30.0))
    return BoostPlan(kind="RESCUE", event_type="TRAPPED_LATE_RESCUE", boost_side=side,
                     entry_ref=round(current_price, 2), n=n, sl_dollars=sl_d,
                     tp_dollars=tp_d, move_dollars=round(leg_fav, 2))


def trapped_rescue_cap(cfg):
    """F-B: the hard combined-loss cap ($) for a trapped late-rescue fleet =
    n x trapped_rescue_sl_dollars x lot x contract. This is what BOUNDS a reverse-whipsaw
    (the hedge can never lose more than this) -- a naked late hedge would be unbounded."""
    _depth = getattr(cfg, "stack_depth", None)
    n = (max(0, min(3, int(_depth)) - 1) if _depth is not None
         else int(getattr(cfg, "rescue_boost_count", 2)))
    return (n * float(getattr(cfg, "trapped_rescue_sl_dollars", 13.0))
            * float(getattr(cfg, "lot_size", 0.35))
            * float(getattr(cfg, "contract_size", 100.0)))


def boost_whipsaw_cap(cfg, kind="RESCUE"):
    """The hard combined-boost loss cap ($), = n x $sl x lot x contract, for this
    event KIND. v3.3.3: the SL is per-kind (boost_sl_for) so RALLY caps at
    2 x $13 x 0.35 x 100 = -$910 and RESCUE stays 2 x $10 x 0.35 x 100 = -$700 --
    NOT one shared value. Default kind='RESCUE' keeps the historical $700 for any
    legacy caller that doesn't pass a kind. A boost event whose combined
    realized+open loss reaches -cap closes the remaining boosts."""
    return (int(getattr(cfg, "rescue_boost_count", 2))
            * boost_sl_for(cfg, kind)
            * float(getattr(cfg, "lot_size", 0.35))
            * float(getattr(cfg, "contract_size", 100.0)))


def cap_breached(combined_boost_pnl, cfg, kind="RESCUE"):
    """True when the boosts' combined P&L has reached/breached the -cap (hard
    close) for this KIND. Clamp-on-breach -- binds even when a single boost slipped
    past its SL. v3.3.3: reads the per-kind cap (RALLY -$910 / RESCUE -$700)."""
    try:
        return float(combined_boost_pnl) <= -boost_whipsaw_cap(cfg, kind) + _EPS
    except (TypeError, ValueError):
        return False


# ============================================================================
# v3.2.3 No-OCO STACK ECONOMICS — the break-even truth, CODED (spec A3/A4, N2/N4/N6).
# Pure; shared by live, backtest, and selftest so the +$6/position gating line is
# enforced, never assumed.
# ============================================================================
def _lot(cfg):
    return float(getattr(cfg, "lot_size", 0.35))


def _contract(cfg):
    return float(getattr(cfg, "contract_size", 100.0))


def stack_breakeven_usd(cfg):
    """The combined $ the winning stack must clear to cover the ONE losing
    straddle leg riding to its SL: sl_dist * lot * contract (= $630 @ $18/0.35)."""
    return float(getattr(cfg, "sl_dist", 18.0)) * _lot(cfg) * _contract(cfg)


def stack_cap(cfg):
    """The hard cap on winners on the winning side: max_boost_stack (default 5) when
    cfg.allow_5_long (original + 2 RALLY + 2 RESCUE-converts once the losing leg SLs
    out), else the proven 3-cap (test-36). v3.2.6: the 5-value is the config knob
    cfg.max_boost_stack so the cap is tunable, not hard-coded."""
    return int(getattr(cfg, "max_boost_stack", 5)) if bool(getattr(cfg, "allow_5_long", False)) else 3


def stack_winners(cfg):
    """Positions on the winning side at full stack (== the hard cap)."""
    return stack_cap(cfg)


def per_position_breakeven_usd(cfg):
    """Break-even $ EACH winner must clear (= total / #winners, ~$210 @ 3)."""
    return stack_breakeven_usd(cfg) / max(1, stack_winners(cfg))


def per_position_breakeven_move(cfg):
    """Break-even MOVE ($ of price) each winner must clear (~$6 @ 0.35/3)."""
    return per_position_breakeven_usd(cfg) / (_lot(cfg) * _contract(cfg))


def stack_net_usd(winner_move_each, cfg, n_winners=None, loser_loss_usd=None):
    """Net of the full No-OCO event = (winners' P&L) - (losing leg SL). `winner_move_each`
    is the $ price move banked by EACH winner. Positive net <=> winners cleared the
    break-even line; negative <=> whipsaw that compounds the loss (logged honestly)."""
    n = stack_winners(cfg) if n_winners is None else int(n_winners)
    loss = stack_breakeven_usd(cfg) if loser_loss_usd is None else float(loser_loss_usd)
    win = float(winner_move_each) * _lot(cfg) * _contract(cfg) * n
    return round(win - loss, 2)


def stack_peak_exposure(cfg):
    """Peak live exposure at full stack: N winners + 1 open losing leg.
    Returns (lots_live, usd_per_dollar). At 5x0.35 -> 6 legs = 2.10 lot = $210/$1."""
    legs = stack_winners(cfg) + 1
    lots = legs * _lot(cfg)
    return round(lots, 2), round(lots * _contract(cfg), 2)


def stack_trail_exits(longs, max_fav, cfg):
    """v3.2.4 trail-lock co-close (the expected Wednesday behaviour). Each ARMED
    long (reached +trail_arm_profit) closes TOGETHER on the reversal at the SHARED
    high-water mark minus the gap (max_fav - trail_gap). A long that never armed
    falls to its OWN $10 boost SL (entry - boost_trigger), not the trail. max_fav
    must be the REAL peak (the phantom-lock guard from tests 39/40 still applies).

    `longs` = list of {'entry': px, 'armed': bool|None}. armed=None -> inferred from
    whether max_fav put it at least +trail_arm_profit in profit. Returns
    (co_close_price, [ {entry, armed, exit, pnl} ... ]) for BUY longs."""
    gap = float(getattr(cfg, "trail_gap", 1.50))
    arm = float(getattr(cfg, "trail_arm_profit", 8.0))
    boost_sl = float(getattr(cfg, "boost_trigger_dollars", 10.0))
    lot = _lot(cfg); contract = _contract(cfg)
    co_close = round(float(max_fav) - gap, 2)
    rows = []
    for lg in longs:
        entry = float(lg["entry"])
        armed = lg.get("armed")
        if armed is None:
            armed = (float(max_fav) - entry) >= arm
        exit_px = co_close if armed else round(entry - boost_sl, 2)
        rows.append({
            "entry": round(entry, 2), "armed": bool(armed),
            "exit": round(exit_px, 2),
            "pnl": round((exit_px - entry) * lot * contract, 2),
        })
    return co_close, rows


def stack_scenario_net(longs_profit_usd, loser_loss_usd):
    """Net of a No-OCO 5-long event = winners' aggregate P&L - the losing leg's SL.
    Fixtures (0.15, loser -$270): least +285 -> +$15, modest +585 -> +$315, bigger
    +1185 -> +$915. (0.35, loser -$630): modest +1365 -> +$735."""
    return round(float(longs_profit_usd) - float(loser_loss_usd), 2)
