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
  R5  Each boost trails from ENTRY, one-way ratchet, locks spec_boost_min_lock
      ($1.50) minimum once reached. A boost can NEVER close negative.
  R6  The trapped original dies at its SL, capped -- the ONLY permitted loser.
  R7  When the trapped leg hits its SL, close the entire winning side near it.
  R8  freeze_minutes = 0 -- the trail arms on PROFIT (be_trigger 2.50 / arm_buffer
      1.50 remain the guards), never on a clock. (Implemented in strategy.py.)

When ON, trapped_late_rescue (F-B) is GATED OFF (fills.py); the F-B code is kept.
Everything is READ from cfg. The PURE decision functions here are shared by the
live driver and the selftest so they can never drift.

PTRACE lines (every decision is greppable): BAND_ESTABLISHED, BREAK_CONFIRMED,
BOOST1_FIRED, BOOST2_FIRED, RATCHET_ARMED, RATCHET_EXIT, R7_CLOSE, and
BOOST_SUPPRESSED_IN_BAND (every tick F-B would have fired but R1 blocked it).
"""
from __future__ import annotations

import logging

log = logging.getLogger("AUREON")

ANCHORS_MAGIC = 20260522
_SIDE_CHAR = {'BUY': 'B', 'SELL': 'S'}


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


def favorable(side, entry, price):
    """Favorable excursion in price $ for a leg (>=0 good)."""
    return (price - entry) if side == 'BUY' else (entry - price)


def r7_close_level(trapped_sl):
    """R7: the winning side is closed near where the trapped leg died (its SL)."""
    return round(float(trapped_sl), 2)


# ===========================================================================
# telemetry (every decision greppable via ptrace + a util_pullback_log feed)
# ===========================================================================
def _pt(trader, event, anchor, **kw):
    tr = getattr(trader, 'ptrace', None)
    if tr is not None:
        try:
            tr.emit(event, None, anchor, **kw)
            return
        except Exception:
            pass
    log.info(f"[BOOST_SPEC] {event} anchor={anchor} " +
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


def _place_spec_boost(trader, anchor, side, seq, band_state):
    """Place ONE spec boost at market on `side`, breakeven stop, no TP (rides the
    ratchet / R7). Registers a shadow with spec markers. Returns ticket or None."""
    a2 = str(anchor)[:2]
    sgn = 1.0 if side == 'BUY' else -1.0
    comment = f"AUR_{a2}_{_SIDE_CHAR[side]}_B{seq}"
    # far TP so only the ratchet / R7 close the boost; breakeven SL at the current
    # mid (corrected to the real fill by the first ratchet pass).
    mid = float(band_state['last_mid'])
    far_tp = round(mid + sgn * 1000.0, 2)
    try:
        res = trader.adapter.place_market_order(
            trader.cfg.symbol, side, trader.cfg.lot_size, sl=round(mid, 2), tp=far_tp,
            comment=comment, dry_run=trader.paper, magic=ANCHORS_MAGIC)
    except Exception as e:
        log.warning(f"boost_spec: place {side} B{seq} failed: {e!r}")
        return None
    tk = getattr(res, 'order', None) or getattr(res, 'deal', None)
    fill = float(getattr(res, 'price', mid) or mid)
    if not tk:
        return None
    trader.shadow_positions[int(tk)] = {
        'anchor_label': anchor, 'side': side, 'entry_price': fill,
        'current_sl': round(fill, 2), 'tp_level': far_tp, 'max_fav': fill,
        'leg_fill_price': fill, 'role': 'rescue', 'boost': True, 'spec_boost': True,
        'spec_seq': seq, 'spec_anchor': anchor, 'boost_eligible': False,
        'boost_fired': True, 'boost_rally_only': False,
    }
    return int(tk)


def _ratchet_boost(trader, tk, sh, mid):
    """R5: advance the boost's one-way ratchet stop from its peak favorable
    excursion; emit RATCHET_ARMED the first time it locks. The broker SL is moved
    via modify_position_sl (live) / the shadow current_sl (paper)."""
    side = sh['side']; entry = float(sh['entry_price'])
    fav = favorable(side, entry, mid)
    peak = max(float(sh.get('spec_max_fav', 0.0)), fav)
    sh['spec_max_fav'] = peak
    new_sl = spec_ratchet_sl(side, entry, peak, trader.cfg)
    cur = float(sh.get('current_sl', entry))
    sgn = 1.0 if side == 'BUY' else -1.0
    # one-way: only ever move the stop in the favorable direction
    improves = (new_sl > cur + 1e-9) if side == 'BUY' else (new_sl < cur - 1e-9)
    if improves:
        sh['current_sl'] = new_sl
        try:
            if not trader.paper:
                trader.adapter.modify_position_sl(int(tk), new_sl)
        except Exception:
            pass
        if not sh.get('spec_ratchet_armed') and spec_lock_fav(peak, trader.cfg) > 0.0:
            sh['spec_ratchet_armed'] = True
            _pt(trader, 'RATCHET_ARMED', sh.get('anchor_label'), ticket=tk, side=side,
                lock_level=new_sl, max_fav=round(peak, 2))


def boost_spec_tick(trader, mid):
    """The per-tick boost_spec_v2 handler. Establishes each anchor's band, fires
    boost1/boost2 on a confirmed break OUTSIDE the band (R1-R4), ratchets the
    boosts (R5), and closes the winning side when the trapped original dies (R7).
    Called from fills._check_boost_triggers ONLY when cfg.boost_spec_v2 is ON, so
    it fully replaces the F-B / RALLY / RESCUE trigger path. Never raises."""
    st = getattr(trader, '_spec_state', None)
    if st is None:
        st = trader._spec_state = {}
    cfg = trader.cfg

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
                                      'trapped': None, 'r7_done': False, 'last_mid': mid}
                _pt(trader, 'BAND_ESTABLISHED', anchor, band_lo=lo, band_hi=hi)
            else:
                continue
        state['last_mid'] = mid

        # --- WATCHING: R1 (nothing inside band) + break detection ---
        if state['status'] == 'watching':
            d = break_direction(mid, state['band_lo'], state['band_hi'], cfg)
            if d is None:
                # R1 suppression telemetry: would the OLD F-B have fired? (a leg
                # >= trapped_rescue_arm_dollars adverse while inside the band)
                arm = float(getattr(cfg, 'trapped_rescue_arm_dollars', 10.0))
                for _t, s in origs:
                    if favorable(s['side'], float(s['leg_fill_price']), mid) <= -arm:
                        _pullback_log(trader, 'BOOST_SUPPRESSED_IN_BAND', anchor,
                                      side=s['side'], adverse=round(-favorable(
                                          s['side'], float(s['leg_fill_price']), mid), 2),
                                      band_lo=state['band_lo'], band_hi=state['band_hi'])
                        break
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

        # --- BROKEN: boost2 when price runs spec_boost2_gap past boost1 ---
        if state['status'] == 'broken' and state['boost1'] and not state['boost2']:
            d = state['dir']; bs = boost_side(d)
            lvl1 = boost1_level(state['band_lo'], state['band_hi'], d, cfg)
            lvl2 = boost2_level(lvl1, d, cfg)
            if (d == 'DOWN' and mid <= lvl2) or (d == 'UP' and mid >= lvl2):
                tk2 = _place_spec_boost(trader, anchor, bs, 2, state)
                if tk2:
                    state['boost2'] = tk2
                    _pt(trader, 'BOOST2_FIRED', anchor, ticket=tk2, side=bs, level=lvl2)

        # --- RATCHET the spec boosts (R5) ---
        for tk, sh in grp['boosts']:
            if int(tk) in trader.shadow_positions:
                _ratchet_boost(trader, tk, sh, mid)

        # --- R7: trapped original died -> close the winning side, once ---
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
