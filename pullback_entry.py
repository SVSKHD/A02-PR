"""AUREON v3.5.0 — adaptive pullback-entry state machine (PURE, the shared HELPER).

This is the ONE generic piece RALLY and RESCUE are allowed to share (standing rule):
pure logic, fully parameterized, with NO rally/rescue-specific state and NO merged
code path. Each mechanism calls step() with its OWN config values, its OWN per-parent
`state` dict, and its OWN call site -- a rescue call can never mutate rally behavior.

Boost direction D = the direction the boost ENTERS ('BUY' for a rally pyramid, 'SELL'
for a rescue hedge). Both mechanisms stop firing at the EDGE (the extreme of the move);
they arm, then pick the best entry:

  PULLBACK : a counter-move AGAINST D of >= pullback_depth (a dip for a BUY boost / a
             bounce for a SELL boost), then a TURN back toward D -> ENTER at the turn.
             SL is placed BEYOND the retrace extreme (dynamic) so the natural retrace
             cannot stop the position (BUY: below the dip low / SELL: above the bounce
             high), offset by fixed_sl.
  SMOOTH   : no qualifying counter-move, but break-and-hold CONFIRMS the continuation
             in D (smooth_confirm, supplied by the caller -- the same proven mechanism
             the $5 rally arm uses) -> ENTER on the confirm. SL fixed (entry -/+ fixed_sl).
  SKIP     : neither within timeout_candles M5 closes -> no entry (parent runs alone).

PURE: no IO, no clock, no order placement. The 5-min `m5_bucket` id is supplied by the
caller so the timeout is countable without an M5-close hook.
"""
from __future__ import annotations

ARM = 'ARM'      # registered / waiting -- do NOT fire this tick
ENTER = 'ENTER'  # entry condition met -- fire the boost NOW at the returned price/sl
SKIP = 'SKIP'    # timeout elapsed or parent gone -- cleared, never fire (latched)

# Minimal reversal ($) off the retrace extreme that confirms the TURN (a real reversal,
# not a still-extending knife). Conservative; trial-tunable. NOT a per-kind config key
# -- the same turn-confirmation applies to both mechanisms via the shared helper.
TURN_CONFIRM_DOLLARS = 1.5


def _sgn(direction: str) -> float:
    return 1.0 if str(direction) == 'BUY' else -1.0


def _result(action, price=None, sl=None, mode=None):
    return {'action': action, 'price': price, 'sl': sl, 'mode': mode}


def effective_depth(cfg, fixed_depth, atr):
    """v3.5.0 feature 13: the pullback/bounce depth in $. When entry_adaptive_depth is
    ON and a positive ATR is available, the depth tracks recent volatility
    (atr_mult * ATR); OFF (default) -> the fixed per-kind depth ($13 rally / $6 rescue).
    PURE -- the caller supplies the ATR from recent M5 bars."""
    if bool(getattr(cfg, 'entry_adaptive_depth', False)) and atr and float(atr) > 0:
        return float(getattr(cfg, 'atr_mult', 1.0)) * float(atr)
    return float(fixed_depth)


def atr_from_candles(candles):
    """v3.5.0 feature 13 helper: a simple ATR ($) = mean true range over the supplied
    M5 candles (high-low per bar; gaps ignored -- adequate for depth scaling). Returns
    0.0 if there are no candles. PURE."""
    cs = [c for c in (candles or []) if c is not None]
    if not cs:
        return 0.0
    trs = [abs(float(c['high']) - float(c['low'])) for c in cs]
    return sum(trs) / len(trs) if trs else 0.0


def step(state, *, direction, pullback_depth, fixed_sl, timeout_candles,
         current_price, m5_bucket, parent_alive, smooth_confirm, allow_smooth,
         dynamic_sl, confirm_candle=False):
    """One tick of the adaptive entry machine. `state` is a per-parent mutable dict
    (lives in the parent shadow under the mechanism's OWN key). Returns
    {'action', 'price', 'sl', 'mode'}: ARM (hold), ENTER (fire at price, stop at sl,
    mode 'pullback'|'smooth'), or SKIP. Mutates `state` in place. PURE."""
    sgn = _sgn(direction)
    p = float(current_price)
    # ENTER / SKIP latch -- the event resolves exactly once and never re-arms.
    if state.get('done'):
        return _result(state['done'], state.get('price'), state.get('sl'),
                       state.get('mode'))
    if not parent_alive:
        state['done'] = SKIP
        return _result(SKIP)
    if not state.get('armed'):
        state['armed'] = True
        state['cont_ext'] = p          # continuation extreme (high for BUY / low for SELL)
        state['m5_bucket'] = m5_bucket
        state['arm_m5'] = 0
        state['phase'] = 'watch'
        return _result(ARM)
    # advance the M5 timeout counter once per new 5-min bucket (an M5 close).
    if m5_bucket != state.get('m5_bucket'):
        state['arm_m5'] = int(state.get('arm_m5', 0)) + 1
        state['m5_bucket'] = m5_bucket
    # extend the continuation extreme in D's favor (higher for BUY, lower for SELL).
    state['cont_ext'] = (max(float(state['cont_ext']), p) if sgn > 0
                         else min(float(state['cont_ext']), p))
    # retrace magnitude AGAINST D: > 0 once price moves counter to the continuation.
    retrace = sgn * (float(state['cont_ext']) - p)   # BUY: high-p ; SELL: p-low
    if state.get('phase') == 'watch':
        if retrace >= pullback_depth:
            state['phase'] = 'pullback'
            state['retr_ext'] = p                    # dip low / bounce high seed
        elif allow_smooth and smooth_confirm:
            entry = p                                # SMOOTH branch: confirmed continuation
            sl = round(entry - sgn * fixed_sl, 2)    # fixed SL from entry
            state.update(done=ENTER, price=entry, sl=sl, mode='smooth')
            return _result(ENTER, entry, sl, 'smooth')
    if state.get('phase') == 'pullback':
        # track the retrace extreme (further against D: lower for BUY / higher for SELL).
        state['retr_ext'] = (min(float(state['retr_ext']), p) if sgn > 0
                             else max(float(state['retr_ext']), p))
        # TURN: price reverses from the retrace extreme back toward D by >= the confirm.
        turn = sgn * (p - float(state['retr_ext']))  # BUY: p-dip_low ; SELL: bounce_high-p
        if turn < TURN_CONFIRM_DOLLARS:
            state.pop('turn_bucket', None)   # turn not (yet) valid -> drop any pending confirm
        else:
            # v3.5.0 feature 12: when entry_confirm_candle is ON, the turn must be held
            # to an M5 CLOSE in the entry direction before filling (replaces first-touch):
            # arm the confirm on the first qualifying turn, enter only once the 5-min
            # bucket advances with price still in-direction. OFF -> first-touch (enter now).
            if confirm_candle:
                tb = state.get('turn_bucket')
                if tb is None:
                    state['turn_bucket'] = m5_bucket
                    return _result(ARM)          # wait for the candle to close
                if m5_bucket == tb:
                    return _result(ARM)          # same M5 candle -> not yet closed
            entry = p
            if dynamic_sl:
                sl = round(float(state['retr_ext']) - sgn * fixed_sl, 2)  # beyond the extreme
            else:
                sl = round(entry - sgn * fixed_sl, 2)
            state.update(done=ENTER, price=entry, sl=sl, mode='pullback')
            return _result(ENTER, entry, sl, 'pullback')
    if timeout_candles > 0 and int(state.get('arm_m5', 0)) >= timeout_candles:
        state['done'] = SKIP
        return _result(SKIP)
    return _result(ARM)
