"""AUREON v3.2.8 Phase 2 — boosts_common: the steps RALLY and RESCUE SHARE.

Mapped ONCE here so both kinds go through identical plumbing (the A3 fire-at-fill
bug, the comment<=31 guard, the -$700 cap, the rescuestats journal write, the
gapless telemetry trace -- all in one place). The kind-specific decisions live in
rally.py (winning-side pyramid: $5 arm / $3 floor / $2.00 gap + break-and-hold gate)
and rescue.py (losing-side hedge: $10 arm, free-fire-on-commit, tick-hold>=3); the
dispatcher (boosts_dispatch.py) routes by the sign of leg_fav. This module owns:

  - place_fleet()  : the MARKET-order placement loop + per-boost shadow registration
                     (SL-drift re-assert is the one-way breath-gap ratchet in
                     strategy._update_boost_on_bar, the single SL authority), the
                     fill/stack alerts, the BOOST_FIRE telemetry emit, and the
                     rescuestats event-journal write.
  - fp_guard_ok()  : the FP-rule + 5-long stack-cap pre-trade guard call.
  - enforce_cap()  : the combined -$700 whipsaw cap hard-close.

Functions take the LiveTrader as `self` (the repo's bound-method idiom) so the
bodies are byte-identical to the v3.2.7 fills.py originals they were lifted from --
behaviour-frozen; RESCUE output is unchanged.
"""
import logging

import pandas as pd

from telemetry import Severity, md_escape
from mt5_adapter import _MT5_RETCODE_MAP
import discord_cards as dc  # v3.1.0: rich embed cards (pure; safe to import)
import boosts  # v3.2.0: the SINGLE canonical lone-leg boost-trigger decision

log = logging.getLogger("AUREON")


def place_fleet(self, leg_ticket, leg_shadow, plan):
    """Place the lone leg's boost event at MARKET in plan.boost_side (RALLY same
    dir / RESCUE opposite) -- only ever called once price is >= the kind's arm from
    the leg's fill. Opens the rescue event (event_type RALLY_BOOST/RESCUE_BOOST) for
    rescuestats. Reuses the proven place_market_order loop. SHARED by rally.fire +
    rescue.fire (v3.2.8); body byte-identical to the v3.2.7 _fire_boost_event."""
    anchor = leg_shadow.get('anchor_label', '?')
    side = plan.boost_side
    sgn = 1.0 if side == 'BUY' else -1.0
    n = int(plan.n)
    sl_d = float(plan.sl_dollars)
    # v3.5.0: an adaptive pullback entry (rally/rescue) stashes a DYNAMIC SL distance
    # (entry -> beyond the retrace extreme) on the leg shadow; consume it so the boost
    # stop sits below the dip low / above the bounce high. Absent (flag OFF / smooth
    # entry) -> plan.sl_dollars (fixed) -> byte-identical to pre-v3.5.0.
    try:
        _sl_override = leg_shadow.pop('_boost_entry_sl_dollars_override', None) \
            if hasattr(leg_shadow, 'pop') else None
        if _sl_override is not None:
            sl_d = float(_sl_override)
    except Exception:
        pass
    tp_d = float(plan.tp_dollars)
    ref = float(plan.entry_ref)
    leg_fill = float(leg_shadow.get('leg_fill_price', ref))
    event_id = f"{self.state.get('last_broker_date', '?')}_{anchor[:2]}_{leg_ticket}"
    _tr = getattr(self, 'ptrace', None)
    # v3.2.8 Phase 1: the per-kind arm ($5 RALLY / $10 RESCUE) is the trigger this
    # fire honored. Report it to telemetry so a legit +$5 rally fire is NOT flagged
    # boost_fire_below_trigger (which would fire if we reported the rescue $10 arm).
    trig_used = (float(getattr(self.cfg, 'rally_arm_fav', 5.0)) if plan.kind == 'RALLY'
                 else float(getattr(self.cfg, 'boost_trigger_dollars', 10.0)))
    # the fav at which THIS kind's breath-gap trail arms (RALLY $4 / RESCUE $8) --
    # for the STACK-COMPLETE alert text. RESCUE keeps "+$8" (byte-identical).
    arm_disp = (float(getattr(self.cfg, 'rally_lock_floor', 4.0)) if plan.kind == 'RALLY'
                else float(getattr(self.cfg, 'boost_trail_arm_fav', 8.0)))
    stack_target = 1 + n  # parent leg + n boosts
    # v3.2.3 Section D #1: proactive BOOST FIRED alert in the spec format (fires
    # the moment the event arms, both RALLY and RESCUE, lone AND No-OCO stack).
    self.tele.send(
        f"🚀 BOOST FIRED [{plan.kind}] | {anchor} | {side} {self.cfg.lot_size} "
        f"@~${ref:.2f} | parent {leg_ticket} | stack now {stack_target}/3 | "
        f"move ${plan.move_dollars:+.0f} from fill ${leg_fill:.2f}",
        Severity.WARN, important=True, critical=True,
        card=dc.card_rescue(anchor,
                            trapped_leg=f"parent {leg_shadow.get('side')} (ticket {leg_ticket})",
                            rescue_leg=f"{plan.kind} {side} @ ~${ref:.2f}", twin_pnl=None),
        event_key=f"{plan.event_type}:{leg_ticket}")
    fleet = []
    for bi in range(n):
        b_sl = round(ref - sgn * sl_d, 2)
        b_tp = round(ref + sgn * tp_d, 2)
        cmt = f"AUR_{anchor[:2]}_{side[0]}_B{bi + 1}"
        try:
            res = self.adapter.place_market_order(
                self.cfg.symbol, side, self.cfg.lot_size,
                sl=b_sl, tp=b_tp, comment=cmt, dry_run=self.paper)
        except Exception as e:
            self.tele.error(f"❌ {plan.kind} BOOST{bi + 1} EXCEPTION: {md_escape(repr(e))}")
            fleet.append({'ticket': None, 'fill': None, 'rc': None, 'comment': cmt})
            continue
        rc = getattr(res, 'retcode', None) if res is not None else None
        rcn = _MT5_RETCODE_MAP.get(rc, f"UNKNOWN_{rc}")
        if rc == 10009:
            b_tk = getattr(res, 'order', None) or getattr(res, 'deal', None)
            b_fp = float(getattr(res, 'price', ref) or ref)
            if b_tk:
                self.shadow_positions[int(b_tk)] = {
                    'anchor_label': anchor, 'side': side, 'entry_price': b_fp,
                    'current_sl': round(b_fp - sgn * sl_d, 2),
                    'tp_level': round(b_fp + sgn * tp_d, 2), 'max_fav': b_fp,
                    'fill_time': pd.Timestamp.now(tz='UTC').isoformat(),
                    'role': 'rescue', 'boost': True, 'boost_event': event_id,
                    'boost_kind': plan.kind,  # v3.2.8: RALLY -> tighter trail; RESCUE default
                    # E-6: the parent leg this boost pyramided off -- trails resolves it
                    # READ-ONLY to ride the boost with the parent's stop (RALLY only).
                    'parent_ticket': int(leg_ticket),
                }
            self.tele.send(
                f"✅⚡ {plan.kind} BOOST{bi + 1} {side} FILLED @ ${b_fp} (ticket {b_tk})",
                Severity.SUCCESS, important=True, critical=True,
                card=dc.card_boost(bi + 1, side, b_fp, round(b_fp - sgn * sl_d, 2),
                                   round(b_fp + sgn * tp_d, 2), f"{rc}"),
                event_key=f"boost:{b_tk}")
            # v3.2.3 BOOST_FIRE telemetry (per placed boost) -- the gapless trace
            # of the stack. parent_ticket links it to the leg that triggered it.
            if _tr is not None:
                _stack_now = 1 + len([1 for b in fleet if b.get('ticket')]) + 1
                _tr.boost_fire(int(b_tk) if b_tk else None, anchor,
                               parent_ticket=leg_ticket, side=side,
                               position_price=b_fp, boost_kind=plan.kind,
                               stack_size=_stack_now, move_dollars=plan.move_dollars,
                               trigger=trig_used,
                               stop_price=round(b_fp - sgn * sl_d, 2))
            fleet.append({'ticket': int(b_tk) if b_tk else None, 'fill': b_fp,
                          'rc': rc, 'comment': cmt})
            # feature 9 (boost_ledger.csv): the ledger's only prior writers were the
            # optional pullback-entry paths in rally.py/rescue.py -- the actual fleet
            # fire (this loop, SHARED by both kinds) never logged a row, so every
            # immediate/non-pullback boost fill was silently absent from the ledger.
            try:
                import boost_metrics as _bm
                # R-3/D-6 Branch 2 (2c): a trapped late-rescue hedge (F-B) routes
                # through this SAME shared placement loop as a normal RESCUE
                # (plan.kind is always "RESCUE" for F-B -- see boosts.py's
                # plan_trapped_late_rescue). Tag it distinctly as kind=FB so the
                # ledger (and the daily Rogue/report readers of it) can tell an
                # F-B hedge apart from an ordinary RESCUE boost instead of both
                # silently landing as "RESCUE".
                _ledger_kind = 'FB' if plan.event_type == 'TRAPPED_LATE_RESCUE' else plan.kind
                _bm.append_ledger(self, {
                    'ts': pd.Timestamp.now(tz='UTC').isoformat(), 'anchor': anchor,
                    'kind': _ledger_kind, 'event': 'enter', 'arm_px': round(ref, 2),
                    'entry_px': round(b_fp, 2)})
            except Exception as e:
                # 2d: a swallowed ledger-write failure here is exactly how R-3(d)'s
                # real fills went missing for weeks with zero trace. Never let it
                # vanish silently again -- log loud and alert, even though the
                # write itself still fails soft (never blocks placement/order flow).
                log.error(f"boost_ledger.csv write FAILED for {plan.kind} "
                         f"(ticket {b_tk}): {e!r}")
                try:
                    self.tele.error(
                        f"⚠️ boost_ledger.csv write failed for {plan.kind} @ {anchor} "
                        f"(ticket {b_tk}): {md_escape(repr(e))}")
                except Exception:
                    pass
        else:
            self.tele.error(f"❌ {plan.kind} BOOST{bi + 1} rejected rc={rc} ({md_escape(rcn)})")
            fleet.append({'ticket': None, 'fill': None, 'rc': rc, 'comment': cmt})
    # v3.2.3 Section D #2: STACK COMPLETE alert when the full 3-position stack
    # filled (parent + 2 boosts). Names the break-even truth + when the trail arms.
    _filled = len([1 for b in fleet if b.get('ticket')])
    if (1 + _filled) >= stack_target and stack_target >= 3:
        # break-even = the one losing straddle leg's SL = $18 * lot * contract
        # (= $630 @ 0.35) the 3-position winning stack must clear (~+$6/position).
        _be = round(float(getattr(self.cfg, 'sl_dist', 18.0))
                    * float(self.cfg.lot_size)
                    * float(getattr(self.cfg, 'contract_size', 100.0)), 0)
        self.tele.send(
            f"📦 STACK 3/3 {side} | combined breakeven +${_be:.0f} | "
            f"trail arms at +${arm_disp:.0f}",
            Severity.WARN, important=True, critical=True,
            event_key=f"stack:{event_id}")
    # Open the rescue event (RALLY_BOOST / RESCUE_BOOST) so it logs to rescuestats.
    try:
        members = {int(leg_ticket)} | {int(b['ticket']) for b in fleet if b.get('ticket')}
        self._rescue_event_open({
            'event_id': event_id, 'event_type': plan.event_type,
            'date_ist': self.state.get('last_broker_date'),
            'anchor': anchor, 'sched_iso': leg_shadow.get('sched_utc'),
            'open_iso': pd.Timestamp.now(tz='UTC').isoformat(),
            'trigger': {'ticket': None, 'side': None, 'trigger_pnl': None},
            'rescue': {'ticket': int(leg_ticket), 'side': leg_shadow.get('side'),
                       'fill': leg_fill},
            'boosts': fleet,
            'boosts_placed_ok': bool(fleet) and all(b.get('rc') == 10009 for b in fleet),
            'members': members,
        })
    except Exception as e:
        log.warning(f"rescue_event_open ({plan.event_type}) failed: {e!r}")


def enforce_cap(self, mid):
    """v3.2.0: hard-close a boost EVENT's boosts once their COMBINED open P&L
    breaches the cap. Boosts only -- the original leg is outside the cap (isolation).
    v3.3.3: the cap is PER-EVENT-KIND -- RALLY events use the rally SL ($13 ->
    -$910), RESCUE events use the rescue SL ($10 -> -$700), never one shared value.
    The event's kind is taken from its boosts' boost_kind shadow field."""
    by_event = {}
    kind_of = {}
    for tk, sp in self.shadow_positions.items():
        if not sp.get('boost'):
            continue
        sgn = 1.0 if sp.get('side') == 'BUY' else -1.0
        pnl = sgn * (mid - float(sp.get('entry_price', mid))) * self.cfg.lot_size \
            * float(getattr(self.cfg, 'contract_size', 100.0))
        eid = sp.get('boost_event')
        by_event.setdefault(eid, []).append((tk, pnl))
        kind_of.setdefault(eid, sp.get('boost_kind', 'RESCUE'))
    for eid, legs in by_event.items():
        try:
            cap = boosts.boost_whipsaw_cap(self.cfg, kind_of.get(eid, 'RESCUE'))
        except Exception:
            continue
        if sum(p for _, p in legs) <= -cap + 1e-6:
            for tk, _ in legs:
                try:
                    self.adapter.close_position(tk, dry_run=self.paper)
                    self.tele.warn(f"🛑 BOOST CAP -${cap:.0f} breached on {eid} — "
                                   f"closing boost {tk} at market.")
                except Exception as e:
                    log.warning(f"boost cap close {tk} failed: {e!r}")


def fp_guard_ok(self, shadow, stack_after):
    """v3.2.3 Feature E gate (live): block/reduce a stack that would breach the
    account FP rule at the chosen lot. Returns True if the full stack fits; False
    (suppress this event) if it would breach. Guarded -> True on any error. SHARED by
    both kinds; body byte-identical to the v3.2.7 _fp_guard_ok."""
    import fp_guard as _fp, boosts as _b
    try:
        bal = None
        try:
            ai = self.adapter.get_account_info() or {}
            bal = ai.get('balance') or ai.get('equity')
        except Exception:
            bal = None
        bal = float(bal or getattr(self.cfg, 'starting_balance', 50000.0))
        profile = getattr(self.cfg, 'account_profile', 'STANDARD_5PCT')
        lot = getattr(self.cfg, 'lot_size', 0.35)
        # FPZERO disallows the 5-long entirely -> profile caps the stack to 3.
        prof_cap = _fp.profile_stack_cap(profile, _b.stack_cap(self.cfg))
        action, wc, lim, allowed = _fp.guard_cfg(stack_after, self.cfg, bal)
        if stack_after > prof_cap and action == _fp.OK:
            action, allowed = _fp.REDUCE, prof_cap
        tr = getattr(self, 'ptrace', None)
        anchor = shadow.get('anchor_label')
        if tr is not None:
            tr.fp_guard(anchor, action=action, worst_case=wc, limit=lim,
                        allowed_n=allowed, requested_n=stack_after,
                        profile=profile, lot=lot, profile_cap=prof_cap)
        if action != _fp.OK or stack_after > prof_cap:
            self.tele.warn(
                f"🛡️ FP GUARD | {lot} x {stack_after} = worst -${wc:.0f} vs "
                f"limit ${lim:.0f} ({profile}) — {action} to {allowed}, not stacking")
            return False
        return True
    except Exception as e:
        log.warning(f"fp guard check failed (non-fatal, allowing): {e!r}")
        return True
