"""AUREON v3.2.8 Phase 2 — rally: the WINNING-leg pyramid.

A leg that runs +arm in its OWN favor pyramids in the SAME direction. Rally owns
the Phase-1 numbers (its OWN keys, NOT the BOOST_* keys rescue depends on):

  - event arm   : rally_arm_fav      = $5  (was the shared $10) -- arms the pyramid
  - trail arm   : rally_lock_floor   = $4  (was $8) -- breath-gap trail goes live
  - lock floor  : rally_lock_floor   = $4  (was $8) -- one-way locked-profit floor
  - trail gap   : rally_trail_gap    = $1.50 (was $3.50) -- kept proportional to $4

plus the break-and-hold gate (do NOT pyramid a fake break) and the fire entrypoint
(pyramid-the-winner) the dispatcher routes a winning leg to. The shared placement /
FP guard / cap / journal live in boosts_common; the pure trigger decision is the
canonical boosts.plan_boost_event; the breath-gap trail engine is
strategy._update_boost_on_bar (which reads the trail_* accessors below for RALLY
boosts). Kept import-light (no top-level boosts_common import) so strategy can pull
the trail accessors without dragging in the order-placement stack.
"""
import logging

log = logging.getLogger("AUREON")

KIND = "RALLY"


# --- the Phase-1 numbers, owned here (read from the dedicated rally_* cfg keys) ---
def event_arm(cfg):
    """The favorable move ($) a winning leg must make before the rally pyramid arms
    (was the shared $10 boost_trigger_dollars; now the dedicated $5)."""
    return float(getattr(cfg, 'rally_arm_fav', 5.0))


def trail_arm(cfg):
    """Peak fav ($) before a rally boost's breath-gap trail goes live (== lock floor;
    $4, was $8). Below it the boost runs on the $10 hard backstop only."""
    return float(getattr(cfg, 'rally_lock_floor', 4.0))


def lock_floor(cfg):
    """Once armed, a rally boost's locked profit ($) never falls below this ($4)."""
    return float(getattr(cfg, 'rally_lock_floor', 4.0))


def trail_gap(cfg):
    """Rally breath-gap trail gap ($1.50) -- proportional to the tighter $4 floor."""
    return float(getattr(cfg, 'rally_trail_gap', 1.50))


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
