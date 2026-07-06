"""AUREON — pure strategy core (split from bot.py, v3.0.0).

Position + update_position_on_bar + realize_pnl_usd. NO I/O, byte-identical;
the most precious code in the repo.
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config import Config

def _pdig(cfg):
    """feat/symbol-profiles: price decimal places (cfg.price_digits; gold 2, silver 3)."""
    return int(getattr(cfg, 'price_digits', 2))



@dataclass
class Position:
    """A single open position (one leg from one anchor)."""
    anchor_label: str
    side: str  # 'BUY' or 'SELL'
    entry_price: float
    entry_time: pd.Timestamp
    current_sl: float
    tp_level: float
    max_fav: float
    lot: float
    role: str = 'normal'  # v2.9: 'normal' (1st leg) | 'rescue' (No-OCO 2nd leg)
    boost: bool = False   # v3.1.3: SL-rescue BOOST leg (trail-after-+8 handoff)
    boost_kind: str = 'RESCUE'  # v3.2.8: 'RALLY' | 'RESCUE'. ONLY consulted when
    # boost is True; selects the breath-gap trail's arm/lock/gap. Defaults to
    # 'RESCUE' so every existing boost Position (and the v3.2.7 rescue path) keeps
    # the $8 arm / $8 lock / $3.50 gap byte-identical; a RALLY boost uses the
    # tighter v3.3.0 rally_arm_fav ($5) / rally_lock_floor ($3) / rally_trail_gap ($2.00).
    parent_sl: Optional[float] = None  # E-6: the PARENT anchor leg's current trailing
    # stop, resolved READ-ONLY by the caller (trails) from this boost's parent_ticket.
    # Consulted ONLY for a RALLY boost when cfg.boost_ride_with_parent is ON, to hold the
    # boost's exit no tighter than the parent (ride-with-parent). None -> no parent
    # resolved (parent closed / missing) -> the boost runs its own trail, unchanged.
    closed: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    outcome: Optional[str] = None  # 'SL', 'TP', 'Trail', 'EOD', 'KillSwitch'
    pullback_since: Optional[pd.Timestamp] = None  # v3.3.4: when this RALLY boost
    # first went adverse vs its entry in the CURRENT pullback (None = not in a
    # pullback). Drives the rally pullback detector's B-minute slow-reversal cut;
    # reset to None when price returns to entry. Ignored for non-rally / non-boost.

    @property
    def pnl_dist(self) -> float:
        """Current/realized price distance favorable to us."""
        ref = self.exit_price if self.closed else self.max_fav
        if self.side == 'BUY':
            return (ref - self.entry_price)
        return (self.entry_price - ref)


def _close_boost(pos, ts, fill, backstop):
    pos.exit_price = fill
    pos.exit_time = ts
    pos.closed = True
    if pos.side == 'BUY':
        pos.outcome = 'BoostSL' if fill <= backstop + 0.01 else 'BoostTrail'
    else:
        pos.outcome = 'BoostSL' if fill >= backstop - 0.01 else 'BoostTrail'
    return pos.outcome


def _ride_with_parent_stop(pos, cfg, stop, is_rally):
    """E-6 (RALLY-only, flag-gated DEFAULT OFF): return a boost stop held no TIGHTER than
    the parent anchor leg's current trailing stop, so an ARMED rally boost rides at least
    as long as the parent on the same move instead of bailing on its own tight floor (the
    2026-06-30 +$105-vs-+$491 gap). `pos.parent_sl` is the parent's current_sl, resolved
    READ-ONLY by trails -- this NEVER closes or mutates the parent (isolation intact).

    'No tighter' is sign-correct: a SELL trailing stop sits ABOVE price, so the HIGHER of
    (own, parent) survives a bounce longer (max); a BUY stop sits BELOW price, so the LOWER
    survives longer (min). Bounded at the boost's OWN entry (breakeven) so riding-with-parent
    can never drag the boost into a loss, even if the parent's stop is barely in profit.
    Returns `stop` unchanged when the flag is OFF, the leg isn't RALLY, or no parent_sl was
    resolved -> byte-identical to today."""
    if not is_rally or not bool(getattr(cfg, 'boost_ride_with_parent', False)):
        return stop
    psl = getattr(pos, 'parent_sl', None)
    if psl is None:
        return stop
    e = pos.entry_price
    if pos.side == 'SELL':
        return min(max(stop, float(psl)), e)   # ride longer (higher), but never above BE
    return max(min(stop, float(psl)), e)        # BUY: ride longer (lower), never below BE


def _update_boost_on_bar(pos: Position, bar: pd.Series, ts: pd.Timestamp,
                         cfg: Config, tracer=None, ticket=None) -> Optional[str]:
    """v3.2.6 BOOST stop management — boosts ONLY, fully ISOLATED from the original
    leg (this reads/writes ONLY `pos`, never the original's ticket/stop).

    v3.3.0: `tracer` (optional) records the boost's LOCK_ARM / TRAIL_ADVANCE so a
    boost trail exit is never flagged exit_trail_without_trail_advance (the test-fire
    A2 PTRACE defect) -- the boost's life-story now has the same gapless trace the
    original leg has. Default None keeps every existing caller byte-identical. For a
    RALLY boost the ARMED trailed stop is the HARD MINIMUM exit: it can never close
    below its ratcheted trail floor (no sub-floor clip). RESCUE is byte-identical.

    v3.2.6 +$8 ARM GATE (incident 2026-06-23 fix). The breath-gap software trail is
    INACTIVE until the boost has been at least +arm (boost_trail_arm_fav, default $8)
    favorable at its PEAK. The three regimes:
      below +arm  -> trail OFF; protected ONLY by the $10 hard backstop. A reversing
                     boost rides to the backstop (or recovers) -- it is NOT cut at
                     ~-gap underwater (that was the bug: a SELL boost cut at +$5.4
                     adverse right before price dropped ~$35).
      at +arm     -> a one-way LOCK FLOOR engages at +floor (boost_lock_floor); locked
                     profit never falls below it.
      above +arm  -> the $gap (boost_trail_gap_dollars) breath trail follows the
                     favorable peak, its floor never retreating below +floor.
    The original leg's exit is untouched by all this (boost-path only).
    """
    if pos.closed:
        return pos.outcome
    sgn = 1.0 if pos.side == 'BUY' else -1.0
    # v3.3.0 (corrected): RALLY boosts run a tighter breath-gap (arm $5 / floor $3,
    # gap $2.00) off their OWN dedicated keys; RESCUE boosts (and every legacy boost
    # Position, which defaults boost_kind='RESCUE') keep the v3.2.7 $8 arm / $8 lock /
    # $3.50 gap byte-identical. v3.3.3: the HARD backstop is now per-kind too -- RALLY $13
    # (rally_boost_sl, owner-widened), RESCUE $10 (boost_sl_dollars, unchanged).
    # Each kind OWNS its trail numbers (rally.py / rescue.py). RALLY -> $5 arm / $3 floor,
    # $2.00 gap; RESCUE -> $8 arm/lock, $3.50 gap. Lazy import keeps this precious
    # module free of the order-placement stack.
    import rally as _rally, rescue as _rescue
    is_rally = getattr(pos, 'boost_kind', 'RESCUE') == 'RALLY'
    _bk = _rally if is_rally else _rescue
    hard = float(getattr(cfg, 'rally_boost_sl', 13.0)) if is_rally \
        else float(getattr(cfg, 'boost_sl_dollars', 10.0))
    gap = _bk.trail_gap(cfg)
    arm = _bk.trail_arm(cfg)
    floor = _bk.lock_floor(cfg)
    backstop = pos.entry_price - sgn * hard          # the per-kind hard SL backstop

    def trail_for(fav):
        # ARMED trail price only: lock at +floor, then trail by gap once fav-gap
        # clears the floor. One-way (fav is the monotonic peak). Never < +floor.
        d = max(floor, fav - gap)
        return pos.entry_price + sgn * d

    # ARM GATE: the breath trail is live ONLY once the PEAK fav has reached +arm.
    peak_fav = sgn * (pos.max_fav - pos.entry_price)
    armed = peak_fav >= arm
    _open = bar.get('open') if hasattr(bar, 'get') else getattr(bar, 'open', None)

    # E-6: once ARMED, ride the boost's STOP with the parent. Clamp the incoming broker
    # stop up-front so BOTH this bar's hard-backstop check (section 2, which reads
    # current_sl) and the broker SL re-assert ride no tighter than the parent. ARMED-only:
    # below +arm the boost keeps its full $13/$10 backstop (the helper's BE bound would
    # otherwise wrongly tighten an unarmed backstop). RALLY+flag only -> else unchanged.
    if armed:
        pos.current_sl = _ride_with_parent_stop(pos, cfg, pos.current_sl, is_rally)

    # (1) ARMED breath-gap trail EXIT (off entirely below +arm).
    if armed:
        breath_sl = trail_for(peak_fav)
        # E-6: hold the software exit no tighter than the parent's current stop (RALLY
        # only, flag-gated). No-op when OFF/non-rally/no parent -> byte-identical.
        breath_sl = _ride_with_parent_stop(pos, cfg, breath_sl, is_rally)
        if sgn > 0:
            if bar.low <= breath_sl:                      # trail hit
                fill = breath_sl
                if _open is not None and _open < breath_sl:   # gapped THROUGH it
                    # v3.3.0: a RALLY boost's ratcheted trail is the HARD MINIMUM --
                    # never clip below it (the test-fire sub-floor exit). RESCUE keeps
                    # the v3.2.7 backstop-floored gap fill (byte-identical).
                    fill = breath_sl if is_rally else max(backstop, _open)
                return _close_boost(pos, ts, fill, backstop)
        else:
            if bar.high >= breath_sl:
                fill = breath_sl
                if _open is not None and _open > breath_sl:
                    fill = breath_sl if is_rally else min(backstop, _open)
                return _close_boost(pos, ts, fill, backstop)

    # (1b) v3.3.4 RALLY PULLBACK DETECTOR (rally boosts only) — sits ABOVE the $13
    # hard backstop. A rally boost that pulls back AGAINST ENTRY is HELD while the
    # adverse excursion stays within T dollars; crossing T cuts early (reversal), and
    # B minutes adverse without returning to entry cuts (slow reversal). Returning to
    # ENTRY ends the pullback and the normal trail/backstop resume. The $13 backstop
    # (section 2) is untouched underneath. RESCUE never enters here. Numbers (T, B) are
    # config knobs (TBD from live data) and the whole block is DEFAULT OFF (opt-in);
    # tol is clamped to the backstop so it can never exceed the $13 hard floor.
    if is_rally and bool(getattr(cfg, 'rally_pullback_enabled', False)):
        tol = float(getattr(cfg, 'rally_pullback_tol_dollars', 7.50))
        tol = min(max(tol, 0.0), hard)            # never wider than the hard backstop
        bmin = float(getattr(cfg, 'rally_pullback_time_bound_min', 30.0))
        cut_level = pos.entry_price - sgn * tol   # entry -/+ T (>= backstop since tol<=hard)
        adverse_extreme = bar.low if sgn > 0 else bar.high
        crossed_T = adverse_extreme <= cut_level if sgn > 0 else adverse_extreme >= cut_level
        recovered = bar.high >= pos.entry_price if sgn > 0 else bar.low <= pos.entry_price
        in_pullback = adverse_extreme < pos.entry_price if sgn > 0 else adverse_extreme > pos.entry_price
        if crossed_T:
            # reversal: cut at the T threshold; a bar that GAPS THROUGH it fills at the
            # open, floored by the $13 backstop (never worse than the hard SL).
            fill = cut_level
            if _open is not None and ((sgn > 0 and _open < cut_level)
                                      or (sgn < 0 and _open > cut_level)):
                fill = max(backstop, _open) if sgn > 0 else min(backstop, _open)
            pos.pullback_since = None
            return _close_boost(pos, ts, fill, backstop)
        if recovered:
            pos.pullback_since = None             # back to entry -> resume normal trail
        elif in_pullback:
            if getattr(pos, 'pullback_since', None) is None:
                pos.pullback_since = ts           # pullback starts now
            elapsed_min = (ts - pos.pullback_since).total_seconds() / 60.0
            if bmin > 0 and elapsed_min >= bmin:
                # slow reversal: cut at market (bar close), floored by the $13 backstop.
                close_px = bar.get('close') if hasattr(bar, 'get') else getattr(bar, 'close', None)
                if close_px is None:
                    close_px = adverse_extreme
                fill = max(backstop, close_px) if sgn > 0 else min(backstop, close_px)
                pos.pullback_since = None
                return _close_boost(pos, ts, fill, backstop)

    # (2) $10 HARD BACKSTOP — ALWAYS live (armed or not). current_sl == backstop
    # below +arm; == the ratcheted lock/trail once armed.
    if sgn > 0:
        if bar.low <= pos.current_sl:
            return _close_boost(pos, ts, pos.current_sl, backstop)
    else:
        if bar.high >= pos.current_sl:
            return _close_boost(pos, ts, pos.current_sl, backstop)

    # TP
    if (sgn > 0 and bar.high >= pos.tp_level) or (sgn < 0 and bar.low <= pos.tp_level):
        pos.exit_price = pos.tp_level
        pos.exit_time = ts
        pos.outcome = 'TP'
        pos.closed = True
        return 'TP'

    # Update peak, then ratchet the broker SL one-way: the $10 backstop below +arm;
    # the lock floor / breath trail (>= +floor) once armed. (The trail itself is the
    # software close above; current_sl is the broker-side hard stop.)
    px = bar.high if sgn > 0 else bar.low
    if sgn * (px - pos.entry_price) > sgn * (pos.max_fav - pos.entry_price):
        pos.max_fav = px
    fav = sgn * (pos.max_fav - pos.entry_price)
    new_sl = trail_for(fav) if fav >= arm else backstop
    _prev_sl = pos.current_sl
    if sgn > 0:
        pos.current_sl = max(pos.current_sl, new_sl)
    else:
        pos.current_sl = min(pos.current_sl, new_sl)
    # E-6: re-apply the parent ride to the ratcheted stop (the ratchet above re-tightens
    # from the boost's own peak) so the PERSISTED / broker-re-asserted stop rides with the
    # parent. ARMED-only (fav >= arm); below +arm the $13/$10 backstop is untouched.
    # RALLY-only, flag-OFF -> unchanged. Bounded at the boost's own BE (never into a loss).
    if fav >= arm:
        pos.current_sl = _ride_with_parent_stop(pos, cfg, pos.current_sl, is_rally)
    # v3.3.0: trace the boost's armed trail so its EXIT is never flagged
    # exit_trail_without_trail_advance (the test-fire PTRACE defect). The FIRST
    # armed advance is the LOCK_ARM (the stop leaves the $10 backstop and engages
    # the ratchet); each subsequent advance is a TRAIL_ADVANCE. Emission only when a
    # tracer is supplied (live path) -- selftest/backtest callers pass None and stay
    # byte-identical. The numbers (current_sl) are unchanged; this is observability.
    if (tracer is not None and bool(getattr(cfg, 'fix_boost_telemetry', True))
            and fav >= arm and sgn * (pos.current_sl - _prev_sl) > 1e-9):  # v3.5.0 feature 15
        _was_backstop = sgn * (_prev_sl - backstop) <= 1e-9
        _kw = dict(side=pos.side, position_price=round(pos.entry_price, _pdig(cfg)),
                   max_fav=round(pos.max_fav, _pdig(cfg)), stop_price=round(pos.current_sl, _pdig(cfg)),
                   boost_kind=getattr(pos, 'boost_kind', 'RESCUE'))
        try:
            anchor = getattr(pos, 'anchor_label', None)
            if _was_backstop:
                tracer.lock_arm(ticket, anchor, **_kw)
            else:
                tracer.trail_advance(ticket, anchor, **_kw)
        except Exception:
            pass  # telemetry must never break stop management
    return None


def update_position_on_bar(pos: Position, bar: pd.Series, ts: pd.Timestamp,
                           cfg: Config, tracer=None, ticket=None) -> Optional[str]:
    """
    Apply one M1 bar to an open position. Returns the outcome string if closed,
    else None. Mutates pos.

    v3.3.0 (trail-lock root-cause fix): `tracer` (a position_telemetry.PositionTracer
    or None) records MAXFAV_UPDATE / LOCK_ARM / TRAIL_ADVANCE so the middle of a
    position's life can never be silent again. The default None keeps every
    existing caller (and import-path identity) byte-identical. The lock ladder now
    advances ONLY on a CONFIRMED max_fav that truly reached the rung's price, and
    nothing arms until price clears entry by cfg.arm_buffer.
    """
    if pos.closed:
        return pos.outcome

    # v3.1.6: BOOST legs have their own breath-gap trail + $10 backstop, managed in
    # full isolation (see _update_boost_on_bar). The normal/rescue-leg path below
    # is byte-identical to pre-boost behavior and never sees boost state.
    if getattr(pos, 'boost', False):
        return _update_boost_on_bar(pos, bar, ts, cfg, tracer=tracer, ticket=ticket)

    # 1. PRE-BAR SL CHECK
    if pos.side == 'BUY':
        if bar.low <= pos.current_sl:
            pos.exit_price = pos.current_sl
            pos.exit_time = ts
            pos.outcome = 'SL' if pos.current_sl <= pos.entry_price - cfg.sl_dist + 0.01 else 'Trail'
            pos.closed = True
            return pos.outcome
    else:
        if bar.high >= pos.current_sl:
            pos.exit_price = pos.current_sl
            pos.exit_time = ts
            pos.outcome = 'SL' if pos.current_sl >= pos.entry_price + cfg.sl_dist - 0.01 else 'Trail'
            pos.closed = True
            return pos.outcome

    # 2. UPDATE PEAK FAVORABLE (always, even during freeze — used for reporting &
    # post-freeze trail snap). v3.3.0: advanced ONLY from a confirmed bar extreme,
    # with a floor at entry and a garbage-feed jump filter, so a phantom spike can
    # never inflate max_fav and arm a lock off a price that never traded (the A2
    # root cause). EXIT detection above still uses the raw bar — a real move is
    # never missed; only the lock-arming reference is filtered.
    update_max_fav(pos, bar, cfg, tracer=tracer, ticket=ticket, ts=ts)
    if pos.side == 'BUY':
        fav = pos.max_fav - pos.entry_price
    else:
        fav = pos.entry_price - pos.max_fav
    fav = max(fav, 0.0)

    # 3-5. TRAIL UPDATE — gated by freeze window
    # v2.3 FREEZE: for cfg.freeze_minutes after fill, do NOT engage BE-arm/trail.
    # Initial $18 SL stays as the broker-side stop. When freeze expires, normal
    # trail logic engages and will snap to (peak − trail_gap) automatically.
    in_freeze = False
    if cfg.freeze_minutes > 0 and pos.entry_time is not None:
        try:
            elapsed = (ts - pos.entry_time).total_seconds() / 60.0
            in_freeze = elapsed < cfg.freeze_minutes
        except Exception:
            in_freeze = False  # bad timestamp → fall through to normal logic

    # v2.9 ROLE-AWARE PROFIT LADDER -- fires EVEN during the hold. The hold
    # blocks the noise-chasing trail, NOT profit protection. One-way ratchet:
    # locks can only raise the floor, never loosen a stop.
    #
    # NORMAL leg (1st fill -- job: catch the breakout, bank profits):
    #   fav >= $10  -> SL locked at peak - $2 (floor +$8)
    #   fav >= $6   -> SL locked at entry +/- $4  (fires during the hold)
    #   fav >= $5.0 -> SL locked at breakeven, ONLY AFTER the 45m hold
    #                  (v3.0.7: arm was $2.5; raised to $5 AND hold-gated -- the
    #                  BE-to-entry move inside the hold scratched trends to $0)
    # RESCUE leg (No-OCO 2nd fill -- by construction it only fills after price
    # traveled $10 against its twin; its job is to COVER the twin's loss, so it
    # must stay free to run. Early BE-locks scratch it at $0 exactly when the
    # crash it exists for is happening -- the Jun-10 A3 lesson):
    #   fav >= $10 -> SL locked at entry +/- $8   (loss covered; start protecting)
    #   no smaller tiers.
    def _ratchet(level):
        if pos.side == 'BUY':
            if level > pos.current_sl:
                pos.current_sl = level
        else:
            if level < pos.current_sl:
                pos.current_sl = level
    _sgn = 1.0 if pos.side == 'BUY' else -1.0

    def _apply_lock(level, lock_price):
        # v3.2.3 PHANTOM-LOCK GUARD (the ONE permitted logic change): a lock level
        # may apply ONLY if max_fav has GENUINELY reached that level's trigger price
        # (long: max_fav >= trigger; short: max_fav <= trigger). The lock-price
        # formulas, step size, and rung thresholds are UNCHANGED -- this only adds
        # the "max_fav reached?" guard + makes every evaluation visible. A blocked
        # phantom leaves a LOCK_REJECTED_PHANTOM line (countable, never silent).
        trigger = lock_trigger_price(pos.side, pos.entry_price, level, cfg)
        reached = lock_trigger_reached(pos.side, pos.entry_price, pos.max_fav, level, cfg)
        if tracer is not None:
            try:
                tracer.lock_check(
                    ticket, pos.anchor_label, now_utc=ts, side=pos.side,
                    position_price=pos.entry_price, max_fav=pos.max_fav,
                    lock_level=level, lock_trigger_price=trigger,
                    guard_result=('PASS' if reached else 'FAIL'))
            except Exception:
                pass
        if not reached:
            if tracer is not None:
                try:
                    tracer.lock_rejected_phantom(
                        ticket, pos.anchor_label, now_utc=ts, side=pos.side,
                        position_price=pos.entry_price, max_fav=pos.max_fav,
                        lock_level=level, attempted_lock_price=round(lock_price, _pdig(cfg)),
                        reason="max_fav_not_reached")
                except Exception:
                    pass
            return
        # TRIPWIRE: a lock must never lock in more profit than max_fav supports.
        # Impossible once the guard above holds; this is the loud last line.
        if _sgn * (lock_price - pos.entry_price) > _sgn * (pos.max_fav - pos.entry_price) + 0.05:
            if tracer is not None:
                try:
                    tracer.violation(ticket, pos.anchor_label, "phantom_lock_applied",
                                     attempted_lock_price=round(lock_price, _pdig(cfg)),
                                     max_fav=pos.max_fav)
                except Exception:
                    pass
            return
        _ratchet(lock_price)

    # v3.3.0 ARM-DELAY (spec Part 2.5): nothing arms until price has CLEARED entry
    # by at least cfg.arm_buffer (>= spread + noise band). Stops the trail/lock
    # engaging during entry chop. fav is peak favorable (from the confirmed,
    # filtered max_fav), so this is a confirmed-price gate: a lock can fire ONLY
    # if max_fav truly reached its level. All real arm tiers (2.5/5/6/10) exceed
    # the $1.50 default, so behavior is unchanged when the feed is clean.
    _sl_before = pos.current_sl
    _level_before = lock_level_for(pos, cfg)
    _arm_buffer = float(getattr(cfg, 'arm_buffer', 0.0) or 0.0)
    # The whole ladder + trail is GATED on fav >= arm_buffer: nothing arms until
    # price clears entry by the buffer. (TP check below always runs.)
    if fav >= _arm_buffer:
        if fav >= 10.00:
            # v2.9.1: above +$10 the lock FOLLOWS the peak at $2 distance (ratchet),
            # floor +$8. Captures most of a hold-period spike (peak +$12.8 -> lock
            # +$10.8 = +$540 @0.5) instead of a flat +$8, while $2 of room keeps
            # ordinary noise from tagging it. fav here is peak favorable (max_fav).
            _apply_lock(3, pos.entry_price + _sgn * max(8.00, fav - 2.00))
        elif pos.role != 'rescue':
            if fav >= 6.00:
                _apply_lock(2, pos.entry_price + _sgn * 4.00)
            elif fav >= 5.00 and not in_freeze:
                # v3.0.7 HOLD-GATE: the breakeven-to-entry stop move must NOT engage
                # inside the 45m hold. Live 2026-06-16: A2/A4 hit +$5 fav early, then
                # pulled back and BE-scratched to $0 at 6.2m/2.8m held. Raising the arm
                # to +$5 did not fix this -- the disease is the TIMING. The higher
                # protective locks (+$6->+$4, +$10->peak-2 above) stay active inside
                # the hold; ONLY this entry move waits for hold expiry.
                _apply_lock(1, pos.entry_price)

        if not in_freeze and fav >= cfg.be_trigger:
            if pos.side == 'BUY':
                candidate_sl = max(pos.entry_price, pos.max_fav - cfg.trail_gap)
                if candidate_sl > pos.current_sl + cfg.min_step:
                    pos.current_sl = candidate_sl
            else:
                candidate_sl = min(pos.entry_price, pos.max_fav + cfg.trail_gap)
                if candidate_sl < pos.current_sl - cfg.min_step:
                    pos.current_sl = candidate_sl

    # v3.3.0 TELEMETRY: a lock rung that armed and/or the stop that advanced this
    # bar -- the TRAIL_ADVANCE line A2 was missing. Pure record; never alters the
    # stop. lock_level is the confirmed rung now realized by current_sl.
    if tracer is not None and pos.current_sl != _sl_before:
        _level_now = lock_level_for(pos, cfg)
        try:
            if _level_now > _level_before:
                tracer.lock_arm(
                    ticket, pos.anchor_label, now_utc=ts, side=pos.side,
                    position_price=pos.entry_price, max_fav=pos.max_fav,
                    lock_level=_level_now, stop_price=round(pos.current_sl, _pdig(cfg)),
                    required=lock_ladder_prices(pos, cfg)[min(_level_now, 3) - 1][1])
            tracer.trail_advance(
                ticket, pos.anchor_label, now_utc=ts, side=pos.side,
                position_price=pos.entry_price, max_fav=pos.max_fav,
                lock_level=_level_now, stop_price=round(pos.current_sl, _pdig(cfg)),
                old_stop=round(_sl_before, _pdig(cfg)), new_stop=round(pos.current_sl, _pdig(cfg)))
        except Exception:
            pass

    # v2.6: $5 SECONDARY LOCK REMOVED. It pinned SL to entry+$4 above $5 fav, which is
    # TIGHTER than the peak-1.50 trail and capped runners exactly where you want them to
    # ride. The trail above already ratchets the SL up continuously and never down, so it
    # serves as the moving profit floor. Design: arm at +$2.5, BE lock at +$3, then pure
    # peak-1.50 trail all the way up.

    # 6. TP CHECK
    if pos.side == 'BUY':
        if bar.high >= pos.tp_level:
            pos.exit_price = pos.tp_level
            pos.exit_time = ts
            pos.outcome = 'TP'
            pos.closed = True
            return 'TP'
    else:
        if bar.low <= pos.tp_level:
            pos.exit_price = pos.tp_level
            pos.exit_time = ts
            pos.outcome = 'TP'
            pos.closed = True
            return 'TP'

    return None


def realize_pnl_usd(pos: Position, cfg: Config) -> float:
    """Convert closed position to USD P&L. Returns 0 if not closed."""
    if not pos.closed: return 0.0
    return pos.pnl_dist * cfg.contract_size * pos.lot


# ============================================================================
# Trail-lock root-cause helpers (2026-06-19 A2 incident). Pure; shared by the
# live path, the backtest engine, and the selftest (import-path identity).
# ============================================================================
def _sgn_of(side: str) -> float:
    return 1.0 if side == 'BUY' else -1.0


def update_max_fav(pos: Position, bar: pd.Series, cfg: Config,
                   tracer=None, ticket=None, ts=None) -> bool:
    """Advance pos.max_fav from a CONFIRMED bar extreme only.

    Two guards make a lock off a phantom price impossible (the A2 root cause):
      1. FLOOR: max_fav is never worse than entry (a corrupted restore that left
         max_fav below entry can no longer arm a profit-lock off a value the
         market never produced).
      2. GARBAGE FILTER: a favorable extreme that jumps more than cfg.max_tick_jump
         beyond the running max_fav is rejected as stale/garbage feed -- a single
         spurious tick can no longer inflate max_fav and arm a lock off a price
         that never traded. EXIT detection (SL/TP) still uses the raw bar, so a
         real move is never missed; only the lock-arming reference is filtered.

    Returns True if max_fav advanced. Emits MAXFAV_UPDATE (accepted) or records
    the rejection through `tracer` (best-effort; telemetry never alters logic)."""
    sgn = _sgn_of(pos.side)
    # FLOOR at entry (sign-aware).
    if sgn * (pos.max_fav - pos.entry_price) < 0:
        pos.max_fav = pos.entry_price
    cand = float(bar.high) if pos.side == 'BUY' else float(bar.low)
    advances = sgn * (cand - pos.max_fav) > 0
    if not advances:
        return False
    jump = sgn * (cand - pos.max_fav)
    if cfg.max_tick_jump and jump > cfg.max_tick_jump:
        # Garbage/stale extreme: do NOT let it touch max_fav.
        if tracer is not None:
            try:
                tracer.maxfav_update(
                    ticket, pos.anchor_label, now_utc=ts,
                    position_price=pos.entry_price, max_fav=pos.max_fav,
                    rejected_extreme=round(cand, _pdig(cfg)), jump=round(jump, _pdig(cfg)),
                    reason="tick_jump_exceeds_max", accepted=False)
            except Exception:
                pass
        return False
    old = pos.max_fav
    pos.max_fav = cand
    if tracer is not None:
        try:
            tracer.maxfav_update(
                ticket, pos.anchor_label, now_utc=ts,
                position_price=pos.entry_price, max_fav=pos.max_fav,
                old_max_fav=round(old, _pdig(cfg)), tick_price=round(cand, _pdig(cfg)),
                accepted=True)
        except Exception:
            pass
    return True


def lock_level_for(pos: Position, cfg: Config) -> int:
    """The lock LADDER rung currently realized by pos.current_sl (0..3), named
    the same way fills.py classifies a close (BE / LOCK4 / TIER):
      0 = no lock (initial SL still live)
      1 = breakeven lock (SL at entry)
      2 = +$4 lock
      3 = +$8.. peak-2 tier
    Used for telemetry lock_level and the confirmed-price gate."""
    sgn = _sgn_of(pos.side)
    locked = sgn * (pos.current_sl - pos.entry_price)
    # Bucket by the PROFIT the stop locks in (a continuous post-hold trail floor
    # maps to the nearest rung; a +$2 floor is rung 1, NOT rung 3 -- the bug that
    # false-flagged a legitimate trail as a phantom tier-3 lock).
    if locked >= 7.90:
        return 3
    if locked >= 3.90:
        return 2
    if locked > 0.10:
        return 1
    return 0


def lock_ladder_prices(pos: Position, cfg: Config):
    """The price each lock rung REQUIRES max_fav to reach before it may arm --
    the fill-time prediction ladder (spec 1.4) and the confirmed-price gate
    (spec Part 2.1). A rung fires ONLY if max_fav actually reaches its level."""
    e = pos.entry_price
    return [(L, lock_trigger_price(pos.side, e, L, cfg)) for L in (1, 2, 3)]


# v3.2.3 PHANTOM-LOCK GUARD -- the SINGLE source of the "max_fav reached?" check,
# imported (identity) by live, backtest, and selftest (spec PL7). The dollar fav
# each rung requires; the lock may activate ONLY if max_fav genuinely reached it.
_LOCK_RUNG_FAV = {1: 5.00, 2: 6.00, 3: 10.00}


def lock_trigger_price(side: str, entry: float, level: int, cfg=None) -> float:
    """The price max_fav must reach for lock `level` to be allowed to arm
    (long: entry + fav; short: entry - fav). `cfg` (optional) supplies
    price_digits; omitted -> gold 2dp, byte-identical."""
    return round(entry + _sgn_of(side) * _LOCK_RUNG_FAV[int(level)], _pdig(cfg))


def lock_trigger_reached(side: str, entry: float, max_fav: float, level: int,
                         cfg=None) -> bool:
    """THE GUARD: True iff max_fav has GENUINELY reached lock `level`'s trigger
    price (long: max_fav >= trigger; short: max_fav <= trigger). A lock may apply
    ONLY when this is True -- a phantom (lock priced off a high/low that never
    happened) is blocked here. Pure; shared everywhere."""
    return _sgn_of(side) * (max_fav - lock_trigger_price(side, entry, level, cfg)) >= -1e-9
