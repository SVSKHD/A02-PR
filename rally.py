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
def break_and_hold_ok(self, shadow, plan):
    """v3.2.4 Feature D gate (live): stack ONLY on a CONFIRMED break (cleared edge +
    held N M5 candles + retrace < Y), via the shared break_hold.classify on the
    recent M5 bars. Disabled / no data / any error -> True (legacy: don't block).
    Emits BREAK_CONFIRMED / BREAK_CANDIDATE / BREAK_FAILED(reason). The decision is
    the pure shared fn; this only feeds it live candles. (v3.2.8: lifted verbatim
    from fills._break_and_hold_ok -- break-and-hold stays on RALLY.)"""
    import break_hold as _bh
    if not bool(getattr(self.cfg, 'break_and_hold_enabled', True)):
        return True
    try:
        n = int(getattr(self.cfg, 'hold_candles_n', 2))
        tf = str(getattr(self.cfg, 'break_timeframe', 'M5'))
        bars = None
        for fn in ('get_latest_m5', 'get_latest_bars', 'get_latest_m1'):
            getter = getattr(self.adapter, fn, None)
            if getter is not None:
                try:
                    bars = getter(self.cfg.symbol, n + 2)
                    if bars:
                        break
                except TypeError:
                    bars = getter(self.cfg.symbol, n + 2, tf)
                    if bars:
                        break
        if not bars or len(bars) < n:
            return True   # not enough data -> don't block (legacy)
        candles = [{'high': float(b['high']), 'low': float(b['low']),
                    'close': float(b['close'])} for b in bars]
        edge = float(shadow.get('leg_fill_price', shadow['entry_price']))
        result, reason = _bh.classify(plan.boost_side, edge, candles, self.cfg)
        tr = getattr(self, 'ptrace', None)
        anchor = shadow.get('anchor_label')
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
        if result == _bh.CONFIRMED:
            self.tele.info(f"📈 BREAK CONFIRMED {plan.boost_side} {anchor} "
                           f"@edge ${edge:.2f} — stacking")
            return True
        self.tele.info(f"🚫 BREAK {result} ({reason}) {anchor} {plan.boost_side} "
                       f"@edge ${edge:.2f} — no fire")
        return False
    except Exception as e:
        log.warning(f"break-and-hold check failed (non-fatal, allowing): {e!r}")
        return True


# --- the fire entrypoint the dispatcher routes a WINNING leg to -------------------
def fire(self, leg_ticket, leg_shadow, plan):
    """Pyramid the winner: place the RALLY boost fleet (SAME direction as the leg)
    via the shared placement. Routed here by the dispatcher when leg_fav > 0."""
    import boosts_common
    return boosts_common.place_fleet(self, leg_ticket, leg_shadow, plan)
