"""AUREON v3.2.8 Phase 2 — rescue: the LOSING-leg hedge. UNCHANGED from v3.2.7.

A leg that runs -arm AGAINST itself fires the opposite-direction sibling that becomes
the winner after the whipsaw (the 3-leg model). Rescue keeps EXACTLY its v3.2.7
behaviour -- this module only RELOCATES it; nothing here changes a number or a branch.
Verified working live on A1 2026-06-24 (net -10.85, boost +619.15). Leave it alone.

  - event arm  : boost_trigger_dollars = $10  (the -$10 trigger; UNCHANGED)
  - trail arm  : boost_trail_arm_fav   = $8   (breath-gap trail goes live; UNCHANGED)
  - lock floor : boost_lock_floor      = $8   (one-way locked-profit floor; UNCHANGED)
  - trail gap  : boost_trail_gap_dollars = $3.50 (UNCHANGED)
  - free-fire-on-commit : rescue bypasses the break-and-hold gate (rescue_bypass_
                          break_and_hold, default True) -- a recovery leg is not
                          suppressed by an unconfirmed break.
  - tick-hold >= 3 : the -$10 cross must HOLD hold_ticks ticks before firing (the
                     gate lives in the per-tick scan; tick_hold is the shared engine).

The shared placement / FP guard / cap / journal live in boosts_common; the pure
trigger decision is the canonical boosts.plan_boost_event; the breath-gap trail
engine is strategy._update_boost_on_bar (which reads the trail_* accessors below for
RESCUE boosts -- the default). Kept import-light so strategy can pull the trail
accessors without dragging in the order-placement stack.
"""
import logging

log = logging.getLogger("AUREON")

KIND = "RESCUE"


# --- the v3.2.7 rescue numbers, owned here (read from the unchanged BOOST_* keys) -
def event_arm(cfg):
    """The adverse move ($) a losing leg must make before the rescue hedge arms
    ($10 boost_trigger_dollars; UNCHANGED from v3.2.7)."""
    return float(getattr(cfg, 'boost_trigger_dollars', 10.0))


def trail_arm(cfg):
    """Peak fav ($) before a rescue boost's breath-gap trail goes live ($8,
    boost_trail_arm_fav; UNCHANGED). Below it: the $10 hard backstop only."""
    return float(getattr(cfg, 'boost_trail_arm_fav', 8.0))


def lock_floor(cfg):
    """Once armed, a rescue boost's locked profit ($) never falls below this ($8,
    boost_lock_floor; UNCHANGED)."""
    return float(getattr(cfg, 'boost_lock_floor', 8.0))


def trail_gap(cfg):
    """Rescue breath-gap trail gap ($3.50, boost_trail_gap_dollars; UNCHANGED)."""
    return float(getattr(cfg, 'boost_trail_gap_dollars', 3.50))


def bypass_break_and_hold(cfg):
    """v3.2.7 free-fire-on-commit: True (default) when a rescue fires WITHOUT waiting
    for a confirmed break. False restores the legacy v3.2.6 behaviour (gate both
    kinds on break-and-hold)."""
    return bool(getattr(cfg, 'rescue_bypass_break_and_hold', True))


# --- v3.5.0 RESCUE adaptive pullback-entry gate (flag-gated, DEFAULT OFF) ---------
def entry_gate_ok(self, shadow, plan):
    """v3.5.0 RESCUE adaptive pullback entry. Called from the scan ONLY when
    rescue_entry_enabled is ON (otherwise rescue keeps today's immediate bypass-fire).
    The rescue KEEPS the losing parent, ARMS at the -$10 trigger, and waits via the
    SHARED pullback_entry.step helper (SELL direction): enter on a BOUNCE-then-ROLLOVER
    (SL ABOVE the bounce high, dynamic) or on a SMOOTH down-move that break-and-hold
    CONFIRMS (SL entry+$10, fixed), else SKIP (parent takes its SL alone). Returns True
    = fire now / False = hold or skip. RESCUE ONLY -- its OWN keys + OWN state dict
    (shadow['rescue_entry_arm']); shares only the pure helper, never touches rally.
    Fail-safe: any error -> False (hold; never fire blind)."""
    import time as _time
    import pullback_entry as _pe
    import break_hold as _bh
    import rally as _rally   # pure _has_rows util only (no rally behavior/state)
    try:
        price = getattr(self, '_last_boost_mid', None)
        if price is None:
            return False
        anchor = shadow.get('anchor_label') if hasattr(shadow, 'get') else None
        # SMOOTH-confirm: break-and-hold on the rescue (SELL) direction (same mechanism
        # as the $5 arm). Any failure -> not confirmed (conservative).
        smooth_ok = False
        if bool(getattr(self.cfg, 'rescue_entry_smooth_confirm', True)):
            try:
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
                    if _rally._has_rows(bars):
                        break
                if _rally._has_rows(bars) and len(bars) >= n:
                    candles = [{'high': float(b['high']), 'low': float(b['low']),
                                'close': float(b['close'])} for b in bars]
                    edge = float(shadow.get('leg_fill_price', shadow['entry_price']))
                    res, _rsn = _bh.classify(plan.boost_side, edge, candles, self.cfg)
                    smooth_ok = (res == _bh.CONFIRMED)
            except Exception:
                smooth_ok = False
        state = shadow.setdefault('rescue_entry_arm', {})
        pre_armed = bool(state.get('armed'))
        pre_done = state.get('done')
        m5_bucket = int(_time.time() // 300)
        d = _pe.step(
            state, direction=plan.boost_side,
            pullback_depth=float(getattr(self.cfg, 'rescue_entry_bounce_dollars', 6.0)),
            fixed_sl=float(getattr(self.cfg, 'boost_sl_dollars', 10.0)),
            timeout_candles=int(getattr(self.cfg, 'rescue_entry_arm_timeout_candles', 4)),
            current_price=float(price), m5_bucket=m5_bucket, parent_alive=True,
            smooth_confirm=smooth_ok,
            allow_smooth=bool(getattr(self.cfg, 'rescue_entry_smooth_confirm', True)),
            dynamic_sl=True)   # rescue pullback SL is ALWAYS above the bounce high
        tr = getattr(self, 'ptrace', None)
        if d['action'] == _pe.ENTER:
            entry, sl, mode = float(d['price']), float(d['sl']), d['mode']
            if mode == 'pullback':
                shadow['_boost_entry_sl_dollars_override'] = round(abs(entry - sl), 2)
            msg = (f"🟢 RESCUE {mode.upper()} ENTRY — {plan.boost_side} {anchor} "
                   f"@ ${entry:.2f} SL ${sl:.2f}")
            log.info(msg)
            try:
                self.tele.info(msg)
            except Exception:
                pass
            if tr is not None:
                try:
                    tr.rescue_entry_fired(anchor, side=plan.boost_side,
                                          position_price=round(entry, 2),
                                          stop_price=round(sl, 2), entry_mode=mode,
                                          arm_m5_count=int(state.get('arm_m5', 0)))
                except Exception:
                    pass
            return True
        if d['action'] == _pe.SKIP:
            if tr is not None and not pre_done:
                try:
                    tr.rescue_entry_skipped(anchor, side=plan.boost_side,
                                            arm_m5_count=int(state.get('arm_m5', 0)),
                                            reason='arm_timeout_no_entry')
                except Exception:
                    pass
            return False
        if tr is not None and not pre_armed:
            try:
                tr.rescue_entry_armed(anchor, side=plan.boost_side,
                                      position_price=round(float(price), 2),
                                      bounce_needed=float(getattr(self.cfg, 'rescue_entry_bounce_dollars', 6.0)))
            except Exception:
                pass
        return False
    except Exception as e:
        log.warning(f"rescue.entry_gate_ok failed (hold, no fire): {e!r}")
        return False


# --- the fire entrypoint the dispatcher routes a LOSING leg to --------------------
def fire(self, leg_ticket, leg_shadow, plan):
    """Hedge the loser: place the RESCUE boost fleet (OPPOSITE the leg) via the shared
    placement. Routed here by the dispatcher when leg_fav < 0. Byte-identical to
    v3.2.7 (the placement loop is the same shared code rally uses)."""
    import boosts_common
    return boosts_common.place_fleet(self, leg_ticket, leg_shadow, plan)
