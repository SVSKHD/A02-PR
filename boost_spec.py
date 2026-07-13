"""AUREON — boost_spec_v2 (D-31, 2026-07-10). Flag-gated; DEFAULT OFF.

The inversion of the boost family. Today's F-B (trapped_late_rescue) arms on
ADVERSE DISTANCE ($10 underwater) and hedges the trapped leg OPPOSITE its
direction -- which structurally buys tops and sells bottoms (the 2026-07-10 A1
-$1,695 case: three BUYs at the 4127 top while the trapped SELL sat at 4117; price
fell $18 and killed all three). boost_spec_v2 instead:

  R1  NO boost inside the straddle band (between the two original fills).
  R2  Boost 1 fires spec_break_dollars ($1) PAST the band edge, break direction.
  R3  Boost 2 fires spec_boost2_gap ($4) after boost 1, same direction.
  R4  Boosts JOIN THE WINNING (break) side -- they do NOT hedge the trapped leg.
  R5  Each boost opens with a REAL backstop (spec_boost_sl_dollars $10, validated
      to clear trade_stops_level) and NO TP, then trails a one-way ratchet that
      engages on the tick loop once +spec_boost_min_lock ($1.50) favorable and locks
      that minimum. Once locked a boost can NEVER close negative (the opening
      backstop is the capped worst case before the lock arms).
  R6  The trapped original dies at its SL, capped -- the ONLY permitted loser.
  R7  When the trapped leg hits its SL, close the entire winning side near it.
  R8  freeze_minutes = 0 -- the trail arms on PROFIT (be_trigger 2.50 / arm_buffer
      1.50 remain the guards), never on a clock. (Implemented in strategy.py.)

When ON, trapped_late_rescue (F-B) is GATED OFF (fills.py); the F-B code is kept.
Everything is READ from cfg. The PURE decision functions here are shared by the
live driver and the selftest so they can never drift.

boost_spec_v3 (2026-07-13, flag boost_spec_v3_enabled, DEFAULT ON) layers three
changes on top of the SAME band model, gated so v2's immediate-fire path is
byte-identical when v3 is OFF:
  1. CONFIRM GATE — per-boost-level IDLE->ARMED->FIRE state machine (B1/B2 fully
     independent). A break must DWELL boost_confirm_dwell_s past its level AND
     EXTEND boost_confirm_ext past it before the boost enters at market; a single
     tick back across the level resets that level to IDLE (its siblings untouched).
     Kills the 07-13 fake-break B1 that hugged the edge ~70s and stopped -$350.
  2. RE-ENTRY INVALIDATION — a FILLED boost closes at market the instant price
     crosses back inside the band, not at its $10 SL.
  3. TRAPPED-LEG CUT — on the first confirmed fire per anchor episode the trapped
     opposite anchor leg is cut at market via the existing close path. Additive to
     the -$630 per-engine hard loss stop + account kill switch (never a substitute;
     the broker SL stays in place if the cut rejects). A confirmed cut replaces R7.

PTRACE lines (every decision is greppable): BAND_ESTABLISHED, BREAK_CONFIRMED,
BOOST1_FIRED, BOOST2_FIRED, RATCHET_ARMED, RATCHET_EXIT, R7_CLOSE,
BOOST_SUPPRESSED_IN_BAND (every tick F-B would have fired but R1 blocked it), and
(v3) BOOST_CONFIRM_ARMED, BOOST_CONFIRM_FAILED, BOOST_INVALIDATED_REENTRY,
TRAPPED_CUT. ARMED/FIRE/FAILED and TRAPPED_CUT are mirrored to Discord telemetry.
"""
from __future__ import annotations

import logging

log = logging.getLogger("AUREON")

ANCHORS_MAGIC = 20260522
_SIDE_CHAR = {'BUY': 'B', 'SELL': 'S'}


# ===========================================================================
# VISIBILITY helpers (D-31 banner/status; all values READ from cfg)
# ===========================================================================
def is_active(cfg):
    return bool(getattr(cfg, 'boost_spec_v2', False))


def boost_mode(cfg):
    """'SPEC_V2' when the flag is on, else 'F-B'."""
    return 'SPEC_V2' if is_active(cfg) else 'F-B'


def boost_mode_line(cfg, suppressed_today=None):
    """The one-line boost-mode string for /status + /engines. Shows the
    suppressed-in-band count (the whole point of R1) when SPEC_V2 is active."""
    if is_active(cfg):
        n = 0 if suppressed_today is None else int(suppressed_today)
        return f"Boost mode: SPEC_V2 · suppressed-in-band today: {n}"
    return "Boost mode: F-B (trapped-leg hedge at $10 adverse)"


def startup_card_line(cfg):
    """The boost-mode line appended to the boot startup card (item 3)."""
    if is_active(cfg):
        return "Boost mode: `SPEC_V2` (band-gated, joins winner)"
    return "Boost mode: `F-B` (trapped-leg hedge at $10 adverse)"


def active_block_lines(cfg):
    """The loud [BOOST-SPEC-V2] ACTIVE block (item 2). EMPTY when the flag is off
    (so boot is byte-identical). All numbers read from cfg."""
    if not is_active(cfg):
        return []
    brk = float(getattr(cfg, 'spec_break_dollars', 1.0))
    gap = float(getattr(cfg, 'spec_boost2_gap', 4.0))
    lock = float(getattr(cfg, 'spec_boost_min_lock', 1.5))
    back = float(getattr(cfg, 'spec_boost_sl_dollars',
                         getattr(cfg, 'boost_sl_dollars', 10.0)))
    tstop = int(getattr(cfg, 'tstop_after_min', 45))
    lines = [
        "[BOOST-SPEC-V2] ACTIVE — boosts join the winning side outside the band.",
        "  band gate: no boost inside the straddle band (R1)",
        f"  boost 1: band edge ±${brk:.2f} · boost 2: +${gap:.2f} after (R2/R3)",
        f"  opening SL: ${back:.2f} backstop (clears trade_stops_level), no TP (R5/R6)",
        f"  ratchet: +${lock:.2f} min lock, one-way, trailing floor from +${lock:.2f} fav (R5)",
        "  F-B (trapped_late_rescue): GATED OFF",
        f"  freeze_minutes: 0 · tstop_after_min: {tstop}",
    ]
    if v3_active(cfg):
        dwell = float(getattr(cfg, 'boost_confirm_dwell_s', 12.0))
        ext = float(getattr(cfg, 'boost_confirm_ext', 1.50))
        lines.append(
            f"  [v3] confirm gate: dwell {dwell:.0f}s + ext ${ext:.2f} before entry · "
            f"re-entry invalidation · trapped-leg cut on 1st fire")
    return lines


# ===========================================================================
# PURE decision functions (shared with the selftest; never hardcode a value)
# ===========================================================================
def band_edges(fill_a, fill_b):
    """The straddle band [lo, hi] from the two ORIGINAL fills."""
    a, b = float(fill_a), float(fill_b)
    return (min(a, b), max(a, b))


def break_direction(mid, band_lo, band_hi, cfg):
    """'DOWN' / 'UP' / None. A break is confirmed only OUTSIDE the band by
    spec_break_dollars (R1: nothing fires inside the band, not even at the edge)."""
    brk = float(getattr(cfg, 'spec_break_dollars', 1.0))
    if mid <= band_lo - brk:
        return 'DOWN'
    if mid >= band_hi + brk:
        return 'UP'
    return None


def boost_side(direction):
    """R4: boosts JOIN the winning side. DOWN break -> SELLs; UP break -> BUYs."""
    return 'SELL' if direction == 'DOWN' else 'BUY'


def boost1_level(band_lo, band_hi, direction, cfg):
    """R2: boost 1 fires spec_break_dollars past the band edge, in the break dir."""
    brk = float(getattr(cfg, 'spec_break_dollars', 1.0))
    return round((band_lo - brk) if direction == 'DOWN' else (band_hi + brk), 2)


def boost2_level(boost1, direction, cfg):
    """R3: boost 2 fires spec_boost2_gap further, same direction."""
    gap = float(getattr(cfg, 'spec_boost2_gap', 4.0))
    return round((boost1 - gap) if direction == 'DOWN' else (boost1 + gap), 2)


def spec_lock_fav(max_fav, cfg):
    """The locked-in favorable $ for a boost given its peak favorable excursion.
    0 until max_fav reaches spec_boost_min_lock; then max(min_lock, max_fav -
    trail_gap) -- a one-way ratchet (max_fav is monotonic) that never drops below
    the +$1.50 floor. R5."""
    min_lock = float(getattr(cfg, 'spec_boost_min_lock', 1.50))
    gap = float(getattr(cfg, 'trail_gap', 2.0))
    if max_fav < min_lock:
        return 0.0
    return round(max(min_lock, max_fav - gap), 2)


def spec_ratchet_sl(side, entry, max_fav, cfg):
    """The boost's protective stop PRICE given its peak favorable excursion.
    Below the lock floor the stop sits at BREAKEVEN (entry) -> the boost can never
    close negative (R5); once locked it sits lock_fav favorable of entry and only
    ratchets further favorable."""
    sgn = 1.0 if side == 'BUY' else -1.0
    lock = spec_lock_fav(max_fav, cfg)
    if lock <= 0.0:
        return round(float(entry), 2)          # breakeven floor
    return round(float(entry) + sgn * lock, 2)


def spec_boost_sl(side, entry, cfg):
    """The OPENING backstop stop PRICE for a spec boost: a REAL protective stop
    spec_boost_sl_dollars beyond entry (below a BUY, above a SELL). This is the boost's
    capped worst case at fill -- NOT the +spec_boost_min_lock ratchet, which is a TRAILING
    floor engaged on the tick loop only once the boost is +spec_boost_min_lock favorable
    (R5/R6). The distance is READ from cfg (default = boost_sl_dollars $10, the rescue
    backstop). Pure; shared with the selftest so live and test can never drift."""
    sgn = 1.0 if side == 'BUY' else -1.0
    dist = float(getattr(cfg, 'spec_boost_sl_dollars',
                         getattr(cfg, 'boost_sl_dollars', 10.0)))
    return round(float(entry) - sgn * dist, 2)


def stops_min_dist(info):
    """The broker's minimum LEGAL stop distance in price $ from a symbol_info object:
    trade_stops_level * point -- the SAME source the anchor straddle / rogue / trails paths
    validate against (mt5_adapter routes 10016 INVALID_STOPS through a recompute-vs-stops_level
    resend; this is that computation, factored so the boost path reuses it, not reinvents it).
    0.0 when unknown/unavailable. Pure."""
    try:
        point = float(getattr(info, 'point', 0.01)) or 0.01
        pts = float(getattr(info, 'trade_stops_level', 0) or 0)
        return max(0.0, pts * point)
    except Exception:
        return 0.0


def clear_stops_level(side, ref_price, sl, min_dist):
    """Ensure a stop `sl` sits at least `min_dist` from `ref_price` on the protective side
    (below a BUY, above a SELL). Returns (sl, widened): a stop INSIDE the broker minimum is
    widened OUT to exactly the minimum (never pulled closer). This is the same clamp trails.py
    applies to the anchor legs' stops -- reused here so a spec boost order can never be sent
    with an INVALID_STOPS geometry (the 2026-07-10 boost-1 reject). Pure."""
    if min_dist <= 0.0:
        return round(float(sl), 2), False
    if side == 'BUY':
        max_legal = round(float(ref_price) - min_dist, 2)   # SL must be <= this (below market)
        if float(sl) > max_legal:
            return max_legal, True
    else:
        min_legal = round(float(ref_price) + min_dist, 2)   # SL must be >= this (above market)
        if float(sl) < min_legal:
            return min_legal, True
    return round(float(sl), 2), False


def favorable(side, entry, price):
    """Favorable excursion in price $ for a leg (>=0 good)."""
    return (price - entry) if side == 'BUY' else (entry - price)


def r7_close_level(trapped_sl):
    """R7: the winning side is closed near where the trapped leg died (its SL)."""
    return round(float(trapped_sl), 2)


# ===========================================================================
# boost_spec_v3 (2026-07-13) — PURE confirm-gate rules (shared with the selftest;
# mirrored for BUY/UP and SELL/DOWN so the two sides can never drift). All values
# READ from cfg. These decide ARM/RESET/EXTENSION/FIRE; the driver below owns the
# per-level state (t0, session extreme) and the order placement.
# ===========================================================================
def v3_active(cfg):
    """True when the boost_spec_v3 layer (confirm gate + re-entry invalidation +
    trapped-leg cut) is enabled. It only has effect when boost_spec_v2 is ALSO on
    (this whole module is dormant otherwise); OFF -> v2's immediate-fire path is
    byte-identical."""
    return bool(getattr(cfg, 'boost_spec_v3_enabled', False))


def boost_confirm_levels(band_lo, band_hi, direction, cfg):
    """The ladder levels each confirm-gate rung watches, keyed by seq {1: lvl1,
    2: lvl2}. Reuses the v2 boost1/boost2 levels VERBATIM so a v3 boost arms and
    fires at the SAME prices the v2 ladder used (band edge ±spec_break_dollars, then
    +spec_boost2_gap), just gated on dwell+extension instead of the first tick."""
    l1 = boost1_level(band_lo, band_hi, direction, cfg)
    l2 = boost2_level(l1, direction, cfg)
    return {1: l1, 2: l2}


def confirm_reached(mid, level, direction):
    """ARM trigger / still-past test: has price reached/held at-or-beyond the level
    in the break direction? UP: mid >= level; DOWN: mid <= level. Pure, mirrored."""
    return (float(mid) >= float(level)) if direction == 'UP' else (float(mid) <= float(level))


def confirm_crossed_back(mid, level, direction):
    """RESET trigger: a single tick back ACROSS the level (to the inside). The exact
    negation of confirm_reached -- UP: mid < level; DOWN: mid > level. Pure."""
    return not confirm_reached(mid, level, direction)


def confirm_extension_ok(session_ext, level, direction, cfg):
    """EXTENSION gate: the running session extreme since arming has cleared
    level ± boost_confirm_ext in the break direction (UP: session_hi >= level+ext;
    DOWN: session_lo <= level-ext). `session_ext` is the FAVORABLE extreme (max for
    UP, min for DOWN). Pure, mirrored."""
    ext = float(getattr(cfg, 'boost_confirm_ext', 1.50))
    if direction == 'UP':
        return float(session_ext) >= float(level) + ext
    return float(session_ext) <= float(level) - ext


def confirm_fire_ok(elapsed_s, session_ext, level, direction, cfg):
    """FIRE gate: the break has dwelled >= boost_confirm_dwell_s AND proven its
    extension (confirm_extension_ok). Both must hold on the SAME tick. Pure."""
    dwell = float(getattr(cfg, 'boost_confirm_dwell_s', 12.0))
    return (float(elapsed_s) >= dwell) and confirm_extension_ok(session_ext, level, direction, cfg)


def session_extreme(session_hi, session_lo, direction):
    """The favorable-most price seen since arming, for the extension/telemetry: the
    running MAX on an UP break, the running MIN on a DOWN break. Pure, mirrored."""
    return float(session_hi) if direction == 'UP' else float(session_lo)


def in_band(mid, band_lo, band_hi):
    """RE-ENTRY test: price is back INSIDE the straddle band [lo, hi] (inclusive).
    A filled boost that re-enters is invalidated at market (v3 change 2). Pure."""
    return float(band_lo) <= float(mid) <= float(band_hi)


# ===========================================================================
# telemetry (every decision greppable via ptrace + a util_pullback_log feed)
# ===========================================================================
def _pt(trader, event, anchor, **kw):
    # PositionTracer.emit(event_type, ticket=None, anchor=None, *, ...) takes ticket as
    # its 2nd POSITIONAL; a `ticket=` field in kw would collide with a positional None
    # ("multiple values for argument 'ticket'") and silently drop the whole line to the
    # log fallback. Forward it positionally so ticketed events (BOOSTn_FIRED,
    # RATCHET_ARMED, TRAPPED_CUT, BOOST_INVALIDATED_REENTRY) emit as structured PTRACE.
    tr = getattr(trader, 'ptrace', None)
    ticket = kw.pop('ticket', None)
    if tr is not None:
        try:
            tr.emit(event, ticket, anchor, **kw)
            return
        except Exception:
            pass
    log.info(f"[BOOST_SPEC] {event} anchor={anchor} ticket={ticket} " +
             " ".join(f"{k}={v}" for k, v in kw.items()))


def _pullback_log(trader, event, anchor, **kw):
    """Feed BOOST_SUPPRESSED_IN_BAND (and friends) to util_pullback_log's daily
    JSON when enabled, so the count of 'times the old behavior would have fired'
    is queryable. Best-effort; never raises."""
    _pt(trader, event, anchor, **kw)
    if not bool(getattr(trader.cfg, 'util_pullback_log', False)):
        return
    fn = getattr(trader, '_pullback_log_append', None) or getattr(trader, '_log_pullback', None)
    if callable(fn):
        try:
            fn({'event': event, 'anchor': anchor, **kw})
        except Exception:
            pass


def _tele(trader, msg, sev='info'):
    """Mirror a boost_spec_v3 decision to the operator's Discord/telemetry channel in
    the existing one-line style (rally.py's `self.tele.info('📈 BREAK CONFIRMED …')`).
    Best-effort and fully guarded -- telemetry must NEVER crash the tick loop, and a
    stub trader without .tele (selftest) is a silent no-op."""
    tele = getattr(trader, 'tele', None)
    if tele is None:
        return
    fn = getattr(tele, sev, None) or getattr(tele, 'info', None)
    if not callable(fn):
        return
    try:
        fn(msg)
    except Exception:
        pass


def _leg_pnl(trader, side, entry, price):
    """Realized $ P&L of a leg closed at `price` (side/entry from its shadow). Uses
    lot_size * contract_size, the SAME product the fill reconcile / selftest broker
    book with. For the TRAPPED_CUT / BOOST_INVALIDATED_REENTRY telemetry only --
    never a trade decision. Pure-ish (reads cfg); never raises."""
    try:
        lot = float(getattr(trader.cfg, 'lot_size', 0.0))
        contract = float(getattr(trader.cfg, 'contract_size', 100.0))
        return round(favorable(side, float(entry), float(price)) * lot * contract, 2)
    except Exception:
        return None


# ===========================================================================
# the per-tick driver (live; called from fills._check_boost_triggers when ON)
# ===========================================================================
def _anchor_originals(trader, anchor):
    """Open ORIGINAL (non-boost) legs of `anchor` in shadow_positions."""
    out = []
    for tk, sh in trader.shadow_positions.items():
        if sh.get('anchor_label') == anchor and not sh.get('boost') and not sh.get('spec_boost'):
            out.append((tk, sh))
    return out


def _live_min_dist(trader):
    """Live broker minimum LEGAL stop distance ($) for the traded symbol. Best-effort;
    0.0 when the adapter/mt5/symbol_info is unavailable (paper stubs, test brokers)."""
    try:
        info = trader.adapter.mt5.symbol_info(trader.cfg.symbol)
    except Exception:
        info = None
    return stops_min_dist(info)


def _place_spec_boost(trader, anchor, side, seq, band_state):
    """Place ONE spec boost at market on `side`. The OPENING stop is a REAL backstop
    (spec_boost_sl_dollars beyond the reference price), VALIDATED to clear the broker's
    trade_stops_level -- NOT the +spec_boost_min_lock ratchet (that is a TRAILING lock the
    tick loop engages once the boost is +spec_boost_min_lock favorable; see _ratchet_boost).
    NO take-profit is sent to the broker (tp=0.0): the exit is governed by the ratchet / R7,
    never a placeholder target. The shadow keeps a WIDE STRUCTURAL tp_level only so the
    bar-close trail manager never reads it as a hit. Registers a shadow with spec markers.
    Returns ticket or None."""
    a2 = str(anchor)[:2]
    sgn = 1.0 if side == 'BUY' else -1.0
    comment = f"AUR_{a2}_{_SIDE_CHAR[side]}_B{seq}"
    mid = float(band_state['last_mid'])
    # OPENING backstop, clamped to the broker's minimum legal stop distance (reuse the same
    # trade_stops_level validation the anchor/rogue/trails paths use). The 2026-07-10 reject
    # was a breakeven-at-mid stop ($0.24 from a BUY entry) landing inside trade_stops_level.
    min_dist = _live_min_dist(trader)
    sl0 = spec_boost_sl(side, mid, trader.cfg)
    sl0, widened = clear_stops_level(side, mid, sl0, min_dist)
    if widened:
        log.warning(f"boost_spec: {side} B{seq} opening SL inside trade_stops_level "
                    f"(min ${min_dist:.2f} from {mid}) -- widened to {sl0}")
    # WIDE STRUCTURAL TP for the local shadow ONLY (keeps the bar-close trail's TP check
    # inert); the broker order carries NO TP (tp=0.0), not the old entry+$1000 placeholder.
    structural_tp = round(mid + sgn * 1000.0, 2)
    try:
        res = trader.adapter.place_market_order(
            trader.cfg.symbol, side, trader.cfg.lot_size, sl=sl0, tp=0.0,
            comment=comment, dry_run=trader.paper, magic=ANCHORS_MAGIC)
    except Exception as e:
        log.warning(f"boost_spec: place {side} B{seq} failed: {e!r}")
        return None
    tk = getattr(res, 'order', None) or getattr(res, 'deal', None)
    rc = getattr(res, 'retcode', None)
    fill = float(getattr(res, 'price', mid) or mid)
    if not tk or (rc is not None and rc != 10009):
        log.error(f"boost_spec: {side} B{seq} not filled (rc={rc}) -- no shadow registered")
        return None
    # re-derive the backstop from the ACTUAL fill so the shadow's stop matches what protects
    # the live position (validated the same way).
    sl_fill = spec_boost_sl(side, fill, trader.cfg)
    sl_fill, _w = clear_stops_level(side, fill, sl_fill, min_dist)
    trader.shadow_positions[int(tk)] = {
        'anchor_label': anchor, 'side': side, 'entry_price': fill,
        'current_sl': sl_fill, 'tp_level': structural_tp, 'max_fav': fill,
        'spec_open_sl': sl_fill,
        'leg_fill_price': fill, 'role': 'rescue', 'boost': True, 'spec_boost': True,
        'spec_seq': seq, 'spec_anchor': anchor, 'boost_eligible': False,
        'boost_fired': True, 'boost_rally_only': False,
    }
    return int(tk)


def _ratchet_boost(trader, tk, sh, mid):
    """R5: the boost's one-way TRAILING ratchet. It ENGAGES only once the boost is
    +spec_boost_min_lock favorable -- until then the OPENING backstop set at placement
    stands (the ratchet is NOT the opening SL: that was the 2026-07-10 defect). Once armed
    it locks >= +spec_boost_min_lock and only ever advances further favorable, emitting
    RATCHET_ARMED the first time. Every stop it sends is clamped to clear trade_stops_level
    so a lock near market can never reject. The broker SL is moved via modify_position_sl
    (live) / the shadow current_sl (paper)."""
    side = sh['side']; entry = float(sh['entry_price'])
    fav = favorable(side, entry, mid)
    peak = max(float(sh.get('spec_max_fav', 0.0)), fav)
    sh['spec_max_fav'] = peak
    # NOT yet +spec_boost_min_lock favorable -> the opening backstop holds; the ratchet
    # (the +$1.50 lock) is a TRAILING floor engaged on the tick loop, never the initial SL.
    if spec_lock_fav(peak, trader.cfg) <= 0.0:
        return
    new_sl = spec_ratchet_sl(side, entry, peak, trader.cfg)
    # never send a stop inside the broker's minimum distance from the live market
    new_sl, _w = clear_stops_level(side, mid, new_sl, _live_min_dist(trader))
    cur = float(sh.get('current_sl', entry))
    # one-way: only ever move the stop in the favorable direction
    improves = (new_sl > cur + 1e-9) if side == 'BUY' else (new_sl < cur - 1e-9)
    if improves:
        sh['current_sl'] = new_sl
        try:
            if not trader.paper:
                trader.adapter.modify_position_sl(int(tk), new_sl)
        except Exception:
            pass
        if not sh.get('spec_ratchet_armed'):
            sh['spec_ratchet_armed'] = True
            _pt(trader, 'RATCHET_ARMED', sh.get('anchor_label'), ticket=tk, side=side,
                lock_level=new_sl, max_fav=round(peak, 2))


def _now_seconds(now):
    """Epoch-seconds 'now' for the v3 confirm-gate dwell timer. Accepts a float (epoch
    seconds -- the selftest), a pandas Timestamp/datetime, or None -> pd.Timestamp.now().
    In the offline simulator pandas.Timestamp.now is monkeypatched to the tick time, so
    the SAME call yields sim-time in the sim and wall-time live; only DIFFERENCES are
    used (episode-local elapsed), so the naive-local vs UTC base cancels. Never raises."""
    try:
        if now is None:
            import pandas as _pd
            return float(_pd.Timestamp.now().timestamp())
        if isinstance(now, (int, float)):
            return float(now)
        return float(now.timestamp())
    except Exception:
        try:
            import time as _t
            return float(_t.time())
        except Exception:
            return 0.0


def _suppression_tick(trader, anchor, origs, state, mid):
    """R1 suppressed-in-band telemetry + counter (unchanged from v2): count each tick the
    OLD F-B would have fired a boost INSIDE the band (a leg >= trapped_rescue_arm adverse).
    Best-effort; never raises."""
    arm = float(getattr(trader.cfg, 'trapped_rescue_arm_dollars', 10.0))
    for _t, s in origs:
        if favorable(s['side'], float(s['leg_fill_price']), mid) <= -arm:
            trader._spec_suppressed_today = int(
                getattr(trader, '_spec_suppressed_today', 0)) + 1
            _pullback_log(trader, 'BOOST_SUPPRESSED_IN_BAND', anchor,
                          side=s['side'], adverse=round(-favorable(
                              s['side'], float(s['leg_fill_price']), mid), 2),
                          band_lo=state['band_lo'], band_hi=state['band_hi'])
            break


def _v3_confirm_step(trader, anchor, grp, state, mid, now_s):
    """boost_spec_v3 CONFIRM GATE (change 1): per-boost-level IDLE -> ARMED -> FIRE, in
    place of v2's fire-on-the-first-tick. Each rung (B1, B2) is FULLY INDEPENDENT -- a
    reset on one never touches a sibling's state. A rung ARMS the first tick it reaches
    its level (recording t0 and the running session extreme since arming), FAILS back to
    IDLE on a single tick back across the level (BOOST_CONFIRM_FAILED,
    reason=re_entered|no_extension), and FIRES a market boost (exactly as v2 --
    _place_spec_boost, same lot/SL/magic/comment) only once it has dwelled
    boost_confirm_dwell_s AND extended boost_confirm_ext past the level. On the FIRST fire
    of the episode the trapped opposite anchor leg is cut (change 3). Never raises."""
    cfg = trader.cfg
    origs = grp['orig']
    lo, hi = state['band_lo'], state['band_hi']

    # Commit the break direction on the FIRST tick outside the band (the same $1 edge as
    # v2's boost1 level == B1's ARM level). Until then: R1 (nothing inside the band) plus
    # the suppressed-in-band counter, exactly as v2's watching state.
    if state.get('dir') is None:
        d = break_direction(mid, lo, hi, cfg)
        if d is None:
            _suppression_tick(trader, anchor, origs, state, mid)
            return
        state['dir'] = d
        state['status'] = 'broken'
        bs = boost_side(d)
        trapped = None
        for _t, s in origs:
            if s['side'] != bs:                 # opposite the break dir = trapped
                trapped = _t
        state['trapped'] = trapped
        state.setdefault('rungs', {})
        _pt(trader, 'BREAK_CONFIRMED', anchor, direction=d,
            level=boost1_level(lo, hi, d, cfg), trapped=trapped)

    d = state['dir']; bs = boost_side(d)
    dwell = float(getattr(cfg, 'boost_confirm_dwell_s', 12.0))
    rungs = state.setdefault('rungs', {})
    for seq, level in boost_confirm_levels(lo, hi, d, cfg).items():
        r = rungs.get(seq)
        st_ = (r or {}).get('state', 'IDLE')
        if st_ == 'FIRED':
            continue
        if st_ == 'IDLE':
            if confirm_reached(mid, level, d):
                rungs[seq] = {'state': 'ARMED', 't0': now_s, 'level': level,
                              'session_hi': float(mid), 'session_lo': float(mid),
                              'ext_ok': confirm_extension_ok(mid, level, d, cfg)}
                _pt(trader, 'BOOST_CONFIRM_ARMED', anchor, level=level, side=bs,
                    t0=round(now_s, 3), seq=seq)
                _tele(trader, f"🕒 BOOST CONFIRM ARMED {bs} {str(anchor)[:2]} B{seq} "
                              f"@ {level:.2f} — need dwell {dwell:.0f}s + ext "
                              f"${float(getattr(cfg,'boost_confirm_ext',1.50)):.2f}")
            continue
        # ARMED: fold in this tick's running session extremes, then RESET or FIRE.
        r['session_hi'] = max(r['session_hi'], float(mid))
        r['session_lo'] = min(r['session_lo'], float(mid))
        ext = session_extreme(r['session_hi'], r['session_lo'], d)
        if confirm_extension_ok(ext, level, d, cfg):
            r['ext_ok'] = True
        if confirm_crossed_back(mid, level, d):
            elapsed = max(0.0, now_s - float(r['t0']))
            reason = 're_entered' if r.get('ext_ok') else 'no_extension'
            _pt(trader, 'BOOST_CONFIRM_FAILED', anchor, reason=reason,
                elapsed=round(elapsed, 1), hi=round(ext, 2), seq=seq, side=bs, level=level)
            _tele(trader, f"⚠️ BOOST CONFIRM FAILED {bs} {str(anchor)[:2]} B{seq} "
                          f"reason={reason} elapsed={elapsed:.0f}s hi={ext:.2f}", sev='warn')
            rungs[seq] = {'state': 'IDLE'}           # per-level reset; siblings untouched
            continue
        elapsed = max(0.0, now_s - float(r['t0']))
        if confirm_fire_ok(elapsed, ext, level, d, cfg):
            tk = _place_spec_boost(trader, anchor, bs, seq, state)
            if tk:
                r['state'] = 'FIRED'; r['ticket'] = tk
                state['boost%d' % seq] = tk           # keep v2 fields for R7 continuity
                _pt(trader, 'BOOST%d_FIRED' % seq, anchor, ticket=tk, side=bs, level=level)
                _tele(trader, f"🚀 BOOST FIRED {bs} {str(anchor)[:2]} B{seq} @ {level:.2f} "
                              f"— confirmed (dwell {elapsed:.0f}s, ext ${float(getattr(cfg,'boost_confirm_ext',1.50)):.2f})")
                _v3_trapped_cut(trader, anchor, state, mid)


def _v3_trapped_cut(trader, anchor, state, mid):
    """boost_spec_v3 TRAPPED-LEG CUT (change 3): on the FIRST confirmed fire per anchor
    episode, close the trapped opposite anchor leg at market via the EXISTING close path
    (adapter.close_position -- the same call _flatten_all and R7 use). SAFETY (E-22/E-23
    class): additive to the -$630 per-engine hard loss stop and the account kill switch,
    NEVER a substitute -- neither governor is read or written here, so both stay armed and
    un-bypassed. The broker SL on the trapped leg is left untouched, so if the cut order
    rejects/raises the leg is STILL protected and R7 stays armed as the fallback; only a
    CONFIRMED cut sets r7_done (which is why a clean episode shows no R7_CLOSE). Never
    raises."""
    if state.get('trapped_cut_done'):
        return
    trapped = state.get('trapped')
    if trapped is None or int(trapped) not in trader.shadow_positions:
        return
    sh = trader.shadow_positions.get(int(trapped), {})
    pnl = _leg_pnl(trader, sh.get('side'),
                   sh.get('entry_price', sh.get('leg_fill_price')), mid)
    try:
        res = trader.adapter.close_position(int(trapped), dry_run=trader.paper)
    except Exception as e:
        log.warning(f"boost_spec_v3: trapped-leg cut {trapped} raised: {e!r} -- broker "
                    f"SL still protects it; R7 fallback armed")
        return
    rc = getattr(res, 'retcode', None)
    if not (bool(res) and (rc is None or int(rc) == 10009)):
        log.warning(f"boost_spec_v3: trapped-leg cut {trapped} rejected (rc={rc}) -- broker "
                    f"SL still protects it; R7 fallback armed")
        return
    state['trapped_cut_done'] = True
    state['r7_done'] = True            # the cut REPLACES R7 -> no R7_CLOSE this episode
    _pt(trader, 'TRAPPED_CUT', anchor, ticket=int(trapped), pnl=pnl)
    _tele(trader, f"✂️ TRAPPED CUT {str(anchor)[:2]} #{int(trapped)} pnl={pnl} "
                  f"(hard loss stop + kill switch remain armed)", sev='warn')


def _v3_reentry_invalidation(trader, anchor, state, grp, mid):
    """boost_spec_v3 RE-ENTRY INVALIDATION (change 2): a FILLED boost closes at market the
    instant price crosses back INSIDE the band [band_lo, band_hi] -- it does NOT wait for
    its $10 SL. Emits BOOST_INVALIDATED_REENTRY (ticket, pnl). Routed through the existing
    close path; guarded so a reject just leaves the boost on its broker SL. Never raises."""
    if not in_band(mid, state['band_lo'], state['band_hi']):
        return
    for tk, sh in list(grp['boosts']):
        if int(tk) not in trader.shadow_positions or sh.get('spec_invalidated'):
            continue
        pnl = _leg_pnl(trader, sh.get('side'), sh.get('entry_price'), mid)
        try:
            trader.adapter.close_position(int(tk), dry_run=trader.paper)
        except Exception as e:
            log.warning(f"boost_spec_v3: re-entry invalidation close {tk} raised: {e!r}")
            continue
        sh['spec_invalidated'] = True
        _pt(trader, 'BOOST_INVALIDATED_REENTRY', anchor, ticket=int(tk), pnl=pnl)
        _tele(trader, f"↩️ BOOST INVALIDATED (re-entry) {str(anchor)[:2]} #{int(tk)} "
                      f"pnl={pnl} — closed at market inside band")


def boost_spec_tick(trader, mid, now=None):
    """The per-tick boost_spec handler. Establishes each anchor's band, then either
    (v2) fires boost1/boost2 on the first tick of a confirmed break OUTSIDE the band, or
    (v3, boost_spec_v3_enabled) runs the per-boost-level CONFIRM GATE + re-entry
    invalidation + trapped-leg cut on top of the SAME band model. Ratchets the boosts
    (R5) and, when the trapped original dies without an intervening cut, closes the
    winning side (R7). Called from fills._check_boost_triggers ONLY when cfg.boost_spec_v2
    is ON, so it fully replaces the F-B / RALLY / RESCUE trigger path. `now` (epoch s /
    Timestamp) is the confirm-gate clock; None -> pd.Timestamp.now() (sim-time in the
    simulator, wall-time live). Never raises."""
    st = getattr(trader, '_spec_state', None)
    if st is None:
        st = trader._spec_state = {}
    cfg = trader.cfg
    _v3 = v3_active(cfg)
    now_s = _now_seconds(now) if _v3 else 0.0

    # item 5 (state-machine visibility): log ONCE per anchor when its straddle is
    # pending (both legs resting, not yet filled) so a no-fill day is NOT silent /
    # indistinguishable from "the flag never loaded".
    armed = getattr(trader, '_spec_armed_logged', None)
    if armed is None:
        armed = trader._spec_armed_logged = set()
    pend = {}
    for _tk, _sh in list(getattr(trader, 'shadow_pendings', {}).items()):
        a = _sh.get('anchor_label')
        pend[a] = pend.get(a, 0) + 1
    for a, n in pend.items():
        if n >= 2 and a not in st and a not in armed:
            armed.add(a)
            log.info(f"[BOOST-SPEC-V2] armed for {str(a)[:2]} — awaiting fills to "
                     f"establish band")

    # group open originals by anchor
    anchors = {}
    for tk, sh in list(trader.shadow_positions.items()):
        a = sh.get('anchor_label')
        if a is None:
            continue
        anchors.setdefault(a, {'orig': [], 'boosts': []})
        (anchors[a]['boosts'] if sh.get('spec_boost') else
         (anchors[a]['orig'] if not sh.get('boost') else anchors[a].setdefault('_x', []))).append((tk, sh))

    for anchor, grp in anchors.items():
        origs = grp['orig']
        state = st.get(anchor)
        # --- BAND: needs BOTH original straddle legs open ---
        if state is None:
            if len(origs) >= 2:
                fills = sorted(float(s['leg_fill_price']) for _t, s in origs)
                lo, hi = band_edges(fills[0], fills[-1])
                state = st[anchor] = {'band_lo': lo, 'band_hi': hi, 'status': 'watching',
                                      'boost1': None, 'boost2': None, 'dir': None,
                                      'trapped': None, 'r7_done': False, 'last_mid': mid,
                                      'rungs': {}, 'trapped_cut_done': False}
                _pt(trader, 'BAND_ESTABLISHED', anchor, band_lo=lo, band_hi=hi)
                log.info(f"[BOOST-SPEC-V2] BAND_ESTABLISHED {str(anchor)[:2]} "
                         f"lo={lo} hi={hi}")
            else:
                continue
        state['last_mid'] = mid

        if _v3:
            # v3: confirm gate (arms/fires the rungs, cuts the trapped leg on 1st fire)
            _v3_confirm_step(trader, anchor, grp, state, mid, now_s)
        else:
            # --- v2 WATCHING: R1 (nothing inside band) + break detection ---
            if state['status'] == 'watching':
                d = break_direction(mid, state['band_lo'], state['band_hi'], cfg)
                if d is None:
                    _suppression_tick(trader, anchor, origs, state, mid)
                    continue
                # BREAK confirmed
                state['dir'] = d
                state['status'] = 'broken'
                bs = boost_side(d)
                # trapped original = the leg on the LOSING side of the break
                trapped = None
                for _t, s in origs:
                    if s['side'] != bs:            # opposite the break dir = trapped
                        trapped = _t
                state['trapped'] = trapped
                lvl1 = boost1_level(state['band_lo'], state['band_hi'], d, cfg)
                _pt(trader, 'BREAK_CONFIRMED', anchor, **{'direction': d, 'level': lvl1,
                                                          'trapped': trapped})
                tk1 = _place_spec_boost(trader, anchor, bs, 1, state)
                if tk1:
                    state['boost1'] = tk1
                    _pt(trader, 'BOOST1_FIRED', anchor, ticket=tk1, side=bs, level=lvl1)

            # --- v2 BROKEN: boost2 when price runs spec_boost2_gap past boost1 ---
            if state['status'] == 'broken' and state['boost1'] and not state['boost2']:
                d = state['dir']; bs = boost_side(d)
                lvl1 = boost1_level(state['band_lo'], state['band_hi'], d, cfg)
                lvl2 = boost2_level(lvl1, d, cfg)
                if (d == 'DOWN' and mid <= lvl2) or (d == 'UP' and mid >= lvl2):
                    tk2 = _place_spec_boost(trader, anchor, bs, 2, state)
                    if tk2:
                        state['boost2'] = tk2
                        _pt(trader, 'BOOST2_FIRED', anchor, ticket=tk2, side=bs, level=lvl2)

        # --- RE-ENTRY INVALIDATION (v3 change 2): a filled boost back inside the band
        #     closes at market now, not at its $10 SL ---
        if _v3:
            _v3_reentry_invalidation(trader, anchor, state, grp, mid)

        # --- RATCHET the spec boosts (R5) — shared by v2 and v3 ---
        for tk, sh in grp['boosts']:
            if int(tk) in trader.shadow_positions and not sh.get('spec_invalidated'):
                _ratchet_boost(trader, tk, sh, mid)

        # --- R7: trapped original died -> close the winning side, once. In v3 a
        #     CONFIRMED trapped-leg cut sets r7_done, so R7 is the FALLBACK only (a cut
        #     that rejected and let the leg ride to its broker SL) — a clean v3 episode
        #     emits no R7_CLOSE. ---
        if state.get('trapped') is not None and not state['r7_done']:
            if int(state['trapped']) not in trader.shadow_positions:
                # the trapped leg is gone (hit its SL). Close every winning-side leg
                # (winning original + spec boosts) near that level.
                bs = boost_side(state['dir'])
                closed = []
                for tk, sh in list(trader.shadow_positions.items()):
                    if sh.get('anchor_label') == anchor and sh.get('side') == bs \
                            and (sh.get('spec_boost') or not sh.get('boost')):
                        try:
                            trader.adapter.close_position(int(tk), dry_run=trader.paper)
                        except Exception:
                            pass
                        closed.append(int(tk))
                state['r7_done'] = True
                _pt(trader, 'R7_CLOSE', anchor, legs=closed,
                    trapped=state['trapped'], winning_side=bs)
