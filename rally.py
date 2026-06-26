"""AUREON v3.3.0 — rally: the WINNING-leg pyramid (RIDES like the original leg).

A leg that runs +arm in its OWN favor pyramids in the SAME direction. Rally owns
its OWN keys (NOT the BOOST_* keys rescue depends on):

  - event arm   : rally_arm_fav   = $5   -- a winning leg arms the pyramid at +$5
  - trail arm   : rally_arm_fav   = $5   -- the boost's OWN breath-gap trail goes
                                            live once the BOOST peaks +$5 favorable
  - lock floor  : rally_lock_floor = $3  -- break-even+ MINIMUM (= arm - gap); a
                                            FLOOR only, NOT the governing exit
  - trail gap   : rally_trail_gap  = $2.00 -- once armed the boost RIDES at
                                            peak - $2 (one-way ratchet), matching
                                            the original leg's trail, instead of
                                            locking flat at +$4 and bailing.

v3.3.0 fix (test-fire A2 2026-06-24): the v3.2.8 fixed +$4 lock made rally boosts
bail on the first pause while the original leg rode the whole move. Now a rally
boost trails peak - $2 above a +$3 break-even floor -- it rides and exits ~peak-$2.
The +$5 fire trigger is unchanged. RESCUE is byte-identical ($8 arm / $8 lock /
$3.50 gap). The breath-gap trail engine is strategy._update_boost_on_bar (which
reads the trail_* accessors below for RALLY boosts and now floor-clamps an armed
rally exit so it can never close below its ratcheted trail -- no sub-floor clip).
Kept import-light so strategy can pull the trail accessors without the order stack.
"""
import logging

log = logging.getLogger("AUREON")

KIND = "RALLY"


# --- the rally numbers, owned here (read from the dedicated rally_* cfg keys) ---
def event_arm(cfg):
    """The favorable move ($) a winning leg must make before the rally pyramid fires
    (the dedicated $5; was the shared $10 boost_trigger_dollars). UNCHANGED in v3.3.0."""
    return float(getattr(cfg, 'rally_arm_fav', 5.0))


def trail_arm(cfg):
    """v3.3.0: peak fav ($) before a rally boost's breath-gap trail goes live -- now
    +$5 (== rally_arm_fav), up from $4. Below it the boost runs on the $10 hard
    backstop only; at/above it the trail rides peak - gap with a break-even+ floor."""
    return float(getattr(cfg, 'rally_arm_fav', 5.0))


def lock_floor(cfg):
    """v3.3.0: the break-even+ MINIMUM ($3 = arm - gap) an armed rally boost's trailed
    stop may not fall below. A FLOOR only -- the peak-minus-gap trail governs above it
    (the boost rides), so this no longer caps the exit at a flat +$4."""
    return float(getattr(cfg, 'rally_lock_floor', 3.0))


def trail_gap(cfg):
    """v3.3.0: rally breath-gap trail gap ($2.00, was $1.50) -- matches the original
    leg's trail gap so an armed rally boost rides the move and exits ~peak - $2."""
    return float(getattr(cfg, 'rally_trail_gap', 2.00))


# --- the break-and-hold gate (rally only, per the v3.2.7 split) -------------------
def _has_rows(bars):
    """Truthiness for a bars container that may be a python list OR a numpy
    (structured) array. v3.3.3: a numpy array with >1 element raises in a bool
    context ('The truth value of an array ... is ambiguous') -- that exact crash
    (live A2 2026-06-24) made `if bars:` throw, the handler defaulted to ALLOWING,
    and rally SELL boosts fired into a move bottom for -$701. Length-based so it is
    safe for both list and ndarray; never raises."""
    if bars is None:
        return False
    try:
        return len(bars) > 0
    except TypeError:
        return bool(bars)


# --- v3.4.0 RALLY OVERRIDE PULLBACK-ENTRY (flag-gated, DEFAULT OFF) ---------------
# Replaces the override's immediate fire-at-the-extreme with arm -> wait for a pullback
# -> enter on first touch (or skip on timeout). RALLY override ONLY. The state machine
# core is PURE (no IO/clock/orders) so it is exhaustively testable; the wrapper does the
# price/clock read + telemetry. Distinct from the rally_pullback_* EXIT detector.
ARM_HOLD = 'ARM'    # registered / waiting -- do NOT fire this tick
ARM_FIRE = 'FIRE'   # pullback level touched -- fire the boost NOW (at this price)
ARM_SKIP = 'SKIP'   # timeout candles elapsed or parent gone -- cleared, never fire


def override_pullback_step(state, cfg, parent_side, current_price, m5_bucket,
                           parent_alive):
    """PURE arm-then-pullback-entry state machine for the RALLY override (v3.4.0).

    `state` is a per-parent mutable dict (lives in the parent shadow as
    shadow['override_arm']); called once per tick while the parent is override-grade.
    Returns ARM_HOLD (registered/holding, do NOT fire), ARM_FIRE (pullback level
    touched -> fire NOW at current_price), or ARM_SKIP (timeout elapsed or parent gone
    -> cleared, no fire; latched so the event never re-arms). Mutates `state` in place.
    NO IO, NO clock, NO order placement -- m5_bucket (a 5-min bucket id) is supplied by
    the caller so the timeout is countable without an M5-close hook."""
    pull = float(getattr(cfg, 'override_entry_pullback_dollars', 13.0))
    timeout = int(getattr(cfg, 'override_entry_arm_timeout_candles', 4))
    # SKIP latches: once skipped (timeout/parent-exit) the event never re-arms.
    if state.get('skipped'):
        return ARM_SKIP
    if not parent_alive:
        state['skipped'] = True
        return ARM_SKIP
    if not state.get('armed'):
        # ARM: register and seed the tracked extreme at the current price.
        state['armed'] = True
        state['side'] = parent_side
        state['extreme'] = float(current_price)
        state['m5_bucket'] = m5_bucket
        state['arm_m5_count'] = 0
        return ARM_HOLD
    # advance the M5 timeout counter once per new 5-min bucket (an M5 close).
    if m5_bucket != state.get('m5_bucket'):
        state['arm_m5_count'] = int(state.get('arm_m5_count', 0)) + 1
        state['m5_bucket'] = m5_bucket
    # track the running extreme; entry triggers on a `pull`-dollar retrace from it.
    if parent_side == 'BUY':
        state['extreme'] = max(float(state['extreme']), float(current_price))
        level = float(state['extreme']) - pull
        touched = float(current_price) <= level
    else:
        state['extreme'] = min(float(state['extreme']), float(current_price))
        level = float(state['extreme']) + pull
        touched = float(current_price) >= level
    # touch wins over a same-tick timeout (we got the pullback in time).
    if touched:
        state['fire_level'] = round(level, 2)
        return ARM_FIRE
    if timeout > 0 and int(state.get('arm_m5_count', 0)) >= timeout:
        state['skipped'] = True
        return ARM_SKIP
    return ARM_HOLD


def _override_grade(cfg, shadow, plan):
    """PURE: is this parent OVERRIDE-GRADE (same direction as the boost AND already
    >= parent_established_dollars favorable)? Returns (is_grade, parent_fav, threshold,
    parent_side). Mirrors the inline computation in the legacy override branch so both
    the flag-ON and flag-OFF paths read the same signal."""
    if not bool(getattr(cfg, 'parent_profit_override_enabled', True)):
        return False, 0.0, 0.0, None
    parent_side = shadow.get('side') if hasattr(shadow, 'get') else None
    parent_entry = float(shadow.get('entry_price'))
    parent_maxfav_price = float(shadow.get('max_fav', parent_entry))
    if parent_side == 'BUY':
        parent_fav = parent_maxfav_price - parent_entry
    elif parent_side == 'SELL':
        parent_fav = parent_entry - parent_maxfav_price
    else:
        parent_fav = 0.0
    threshold = float(getattr(cfg, 'parent_established_dollars', 20.0))
    same_dir = (parent_side == plan.boost_side)
    return (same_dir and parent_fav >= threshold), parent_fav, threshold, parent_side


def _override_entry_decision(self, shadow, plan, anchor, tf, edge, result, reason,
                             parent_fav, threshold, parent_side):
    """v3.4.0 wrapper around override_pullback_step: reads the current tick price
    (self._last_boost_mid, stashed by the scan) + a 5-min bucket id (wall clock), runs
    the PURE state machine on shadow['override_arm'], emits telemetry on the ARM / SKIP
    transitions and on FIRE, and returns the bool the gate hands back (True = fire now,
    False = hold/skip). Never raises onto the gate (the gate's own except is below)."""
    import time as _time
    price = getattr(self, '_last_boost_mid', None)
    if price is None:
        return False   # no price this tick -> hold, never fire blind
    state = shadow.setdefault('override_arm', {})
    pre_armed = bool(state.get('armed'))
    pre_skipped = bool(state.get('skipped'))
    m5_bucket = int(_time.time() // 300)   # 5-min wall-clock bucket (M5-close proxy)
    decision = override_pullback_step(state, self.cfg, parent_side, float(price),
                                      m5_bucket, parent_alive=True)
    tr = getattr(self, 'ptrace', None)
    if decision == ARM_FIRE:
        msg = (f"🟢 OVERRIDE PULLBACK ENTRY — armed +${parent_fav:.2f} parent, entered "
               f"on ${getattr(self.cfg, 'override_entry_pullback_dollars', 13.0):.2f} "
               f"retrace {plan.boost_side} {anchor} @ ${price:.2f} "
               f"(extreme ${float(state.get('extreme', price)):.2f})")
        log.info(msg)
        try:
            self.tele.info(msg)
        except Exception:
            pass
        if tr is not None:
            try:
                tr.break_override_parent_established(
                    anchor, side=plan.boost_side, break_level=round(edge, 2),
                    reason=f'pullback_entry/{reason}', parent_max_fav=round(parent_fav, 2),
                    threshold=round(threshold, 2),
                    move_dollars=round(float(state.get('fire_level', price)), 2),
                    entry_mode='pullback_first_touch',
                    extreme=round(float(state.get('extreme', price)), 2),
                    arm_m5_count=int(state.get('arm_m5_count', 0)))
            except Exception:
                pass
        return True
    if decision == ARM_SKIP:
        if tr is not None and not pre_skipped:
            try:
                tr.override_entry_skipped(
                    anchor, side=plan.boost_side, parent_max_fav=round(parent_fav, 2),
                    arm_m5_count=int(state.get('arm_m5_count', 0)),
                    reason='arm_timeout_no_pullback')
            except Exception:
                pass
        return False
    # ARM_HOLD: emit the ARMED line once (first registration), then hold silently.
    if tr is not None and not pre_armed:
        try:
            tr.override_entry_armed(
                anchor, side=plan.boost_side, parent_max_fav=round(parent_fav, 2),
                threshold=round(threshold, 2), position_price=round(float(price), 2),
                pullback_needed=float(getattr(self.cfg, 'override_entry_pullback_dollars', 13.0)))
        except Exception:
            pass
    return False


def break_and_hold_ok(self, shadow, plan):
    """v3.2.4 Feature D gate (live): stack ONLY on a CONFIRMED break (cleared edge +
    held N M5 candles + retrace < Y), via the shared break_hold.classify on the
    recent M5 bars. Disabled / not enough data -> True (legacy: don't block).
    Emits BREAK_CONFIRMED / BREAK_CANDIDATE / BREAK_FAILED(reason).

    v3.3.3 FIX 1B -- FAIL CLOSED: if the gate raises for ANY reason it now BLOCKS the
    fire (returns False) and logs loudly, instead of the old 'non-fatal, allowing'
    default that let an unconfirmed/exhausted break stack. The gate exists to stop
    rally boosts firing into a fake break; a gate that cannot evaluate must not fire.
    RALLY only -- RESCUE bypasses this gate entirely (rescue_bypass_break_and_hold).
    v3.3.3 FIX 1A: bars truthiness goes through _has_rows so a numpy array can't
    raise the ambiguous-truth ValueError that triggered the bug."""
    import break_hold as _bh
    if not bool(getattr(self.cfg, 'break_and_hold_enabled', True)):
        return True
    anchor = None
    try:
        anchor = shadow.get('anchor_label') if hasattr(shadow, 'get') else None
        n = int(getattr(self.cfg, 'hold_candles_n', 2))
        tf = str(getattr(self.cfg, 'break_timeframe', 'M5'))
        bars = None
        for fn in ('get_latest_m5', 'get_latest_bars', 'get_latest_m1'):
            getter = getattr(self.adapter, fn, None)
            if getter is None:
                continue
            try:
                bars = getter(self.cfg.symbol, n + 2)
            except TypeError:
                bars = getter(self.cfg.symbol, n + 2, tf)
            if _has_rows(bars):
                break
        if not _has_rows(bars) or len(bars) < n:
            return True   # not enough data -> don't block (legacy)
        candles = [{'high': float(b['high']), 'low': float(b['low']),
                    'close': float(b['close'])} for b in bars]
        edge = float(shadow.get('leg_fill_price', shadow['entry_price']))
        result, reason = _bh.classify(plan.boost_side, edge, candles, self.cfg)
        tr = getattr(self, 'ptrace', None)
        if tr is not None:
            if result == _bh.CONFIRMED:
                tr.break_confirmed(anchor, side=plan.boost_side,
                                   break_level=round(edge, 2), reason=reason,
                                   n_candles=len(candles), timeframe=tf)
            elif result == _bh.FAILED:
                tr.break_failed(anchor, side=plan.boost_side,
                                break_level=round(edge, 2), reason=reason,
                                n_candles=len(candles), timeframe=tf)
            else:
                tr.break_candidate(anchor, side=plan.boost_side,
                                   break_level=round(edge, 2), reason=reason,
                                   n_candles=len(candles), timeframe=tf)
        # v3.4.0 RALLY OVERRIDE PULLBACK-ENTRY (flag-gated, DEFAULT OFF). When the flag
        # is ON and this parent is OVERRIDE-GRADE, the arm-then-pullback state machine
        # GOVERNS the fire decision (arm at +$20, enter on the retrace, skip on timeout)
        # -- superseding both the immediate CONFIRMED fire and the legacy override below,
        # because the whole point is NOT firing at the extreme. With the flag OFF this
        # entire block is skipped and the original v3.3.8 logic runs verbatim
        # (byte-identical). RESCUE never reaches here; the +$5 arm path is upstream.
        if bool(getattr(self.cfg, 'override_entry_enabled', False)):
            _og, _pfav, _thr, _pside = _override_grade(self.cfg, shadow, plan)
            if _og:
                return _override_entry_decision(
                    self, shadow, plan, anchor, tf, edge, result, reason,
                    _pfav, _thr, _pside)
        if result == _bh.CONFIRMED:
            self.tele.info(f"📈 BREAK CONFIRMED {plan.boost_side} {anchor} "
                           f"@edge ${edge:.2f} — stacking")
            return True
        # v3.3.5 CASE 2 override (RALLY only): the candle-structure gate would BLOCK
        # here (FAILED/CANDIDATE), but if this move is in the SAME direction as the
        # parent leg AND the parent is already deeply favorable (max_fav vs its entry
        # >= parent_established_dollars), the break is a PROVEN continuation, not a
        # fake spike -- fire anyway and log it loudly. The override ONLY loosens: a
        # parent that is NOT established (< threshold) leaves the strict gate fully in
        # force, so a fresh spike off a flat fill (Case 1, the -$701 loss) STILL
        # BLOCKS. RESCUE never reaches here (it bypasses break-and-hold entirely).
        if bool(getattr(self.cfg, 'parent_profit_override_enabled', True)):
            parent_side = shadow.get('side') if hasattr(shadow, 'get') else None
            parent_entry = float(shadow.get('entry_price'))
            parent_maxfav_price = float(shadow.get('max_fav', parent_entry))
            if parent_side == 'BUY':
                parent_fav = parent_maxfav_price - parent_entry
            elif parent_side == 'SELL':
                parent_fav = parent_entry - parent_maxfav_price
            else:
                parent_fav = 0.0
            threshold = float(getattr(self.cfg, 'parent_established_dollars', 20.0))
            same_dir = (parent_side == plan.boost_side)
            if same_dir and parent_fav >= threshold:
                if plan.boost_side == 'SELL':
                    move_dollars = edge - min(c['low'] for c in candles)
                else:
                    move_dollars = max(c['high'] for c in candles) - edge
                msg = (f"🟢 BREAK OVERRIDE — parent established (+${parent_fav:.2f} "
                       f">= ${threshold:.2f}) {plan.boost_side} {anchor} @edge "
                       f"${edge:.2f}: candle gate said {result} ({reason}) but a "
                       f"deep same-direction parent is a proven continuation — "
                       f"FIRING (move ${move_dollars:.2f})")
                log.info(msg)
                try:
                    self.tele.info(msg)
                except Exception:
                    pass
                if tr is not None:
                    try:
                        tr.break_override_parent_established(
                            anchor, side=plan.boost_side, break_level=round(edge, 2),
                            reason=reason, parent_max_fav=round(parent_fav, 2),
                            threshold=round(threshold, 2),
                            move_dollars=round(move_dollars, 2),
                            n_candles=len(candles), timeframe=tf)
                    except Exception:
                        pass
                return True
        self.tele.info(f"🚫 BREAK {result} ({reason}) {anchor} {plan.boost_side} "
                       f"@edge ${edge:.2f} — no fire")
        return False
    except Exception as e:
        # v3.3.3 FIX 1B: FAIL CLOSED. Do NOT fire the rally boost when the gate
        # raises (the old default allowed it -> the -$701 A2 loss). Log loudly as a
        # BLOCKED fire and trace it, then refuse.
        side = getattr(plan, 'boost_side', '?')
        msg = (f"🛑 RALLY BOOST BLOCKED — break-and-hold gate raised, FAILING CLOSED "
               f"(no fire) {anchor} {side}: {e!r}")
        log.error(msg)
        tr = getattr(self, 'ptrace', None)
        if tr is not None:
            try:
                tr.break_failed(anchor, side=side, break_level=None,
                                reason='gate_exception_fail_closed', n_candles=0,
                                timeframe=str(getattr(self.cfg, 'break_timeframe', 'M5')))
            except Exception:
                pass
        try:
            self.tele.error(msg)
        except Exception:
            pass
        return False


# --- the fire entrypoint the dispatcher routes a WINNING leg to -------------------
def fire(self, leg_ticket, leg_shadow, plan):
    """Pyramid the winner: place the RALLY boost fleet (SAME direction as the leg)
    via the shared placement. Routed here by the dispatcher when leg_fav > 0."""
    import boosts_common
    return boosts_common.place_fleet(self, leg_ticket, leg_shadow, plan)
