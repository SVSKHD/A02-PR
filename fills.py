"""AUREON — broker reconcile: fills, STRUCTURAL RESCUE (twin-open), boosts, exits.

Split out of live_trader.py in v3.0.0. These are the verbatim LiveTrader
methods (bodies byte-identical, dedented one level); they take `self` and
are bound back onto LiveTrader in live_trader.py. Behavior-frozen (except
the commit-1 fixes already in the fill path).
"""
import csv
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import date as DateType, timedelta, datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from telemetry import telemetry_from_env, Severity, md_escape, anchor_time_block
from mt5_adapter import _MT5_RETCODE_MAP
import discord_cards as dc  # v3.1.0: rich embed cards (pure; safe to import)
import boosts  # v3.2.0: the SINGLE canonical lone-leg boost-trigger decision
import boosts_common  # v3.2.8 Phase 2: shared boost placement / FP-guard / cap
import boosts_dispatch  # v3.2.8 Phase 3: sign-of-leg_fav router (rally vs rescue.fire)
import rally  # v3.2.8 Phase 2: winning-leg pyramid (break-and-hold gate + rally arm)
import rescue  # v3.2.8 Phase 2: losing-leg hedge (rescue arm / free-fire-on-commit)
import soft_restart as _soft  # v3.2.3: restart-reconcile classifier (pure, shared)

log = logging.getLogger("AUREON")


# ============================================================================
# Alert formatters (v3.0.7) — pure, NEVER raise
# ============================================================================
# The silent fill/close regression: building these message strings could throw
# (a missing/None field) and the throw was swallowed, so the alert vanished with
# nothing logged. These formatters NEVER raise: partial enrichment degrades the
# line but a fill/close ALWAYS produces a non-empty message. The timestamp lives
# in each Discord card's footer (ts_header), built centrally — no call site
# hand-formats a timestamp.

def is_rescue_fill(flag_hint, twin_open):
    """v3.1.3: a No-OCO 2nd fill runs as a RESCUE (fires boosts in the breakout
    direction) when EITHER its twin is still open (structural recovery) OR the
    rescue_on_fill flag is set (the first leg filled and the sibling only fills
    after price travels the full $10 spread against it). Dropping the old
    twin-open AND-requirement is the lone-leg hedging change: a leg whose twin
    already closed is still rescued when the breakout runs against it. A genuine
    FIRST fill (no flag, no open twin) is NOT a rescue."""
    return bool(twin_open or flag_hint)


def format_fill_alert(info, ticket, evt_block=""):
    """Build the FILL telegram body. Never raises. `info` is the shadow-pending
    dict (anchor_label/side/entry_price); `evt_block` is the optional scheduled-
    vs-actual time block (already guarded by _anchor_evt_block)."""
    info = info or {}
    try:
        label = info.get('anchor_label', '?')
        side = info.get('side', '?')
        ep = info.get('entry_price')
        ep_txt = f"${float(ep):.2f}" if ep is not None else "$?"
        return (f"🎯 FILL: *{label}* {side} @ {ep_txt} (ticket {ticket})"
                + (evt_block or ""))
    except Exception as e:
        log.warning(f"format_fill_alert degraded ({e!r})")
        return f"🎯 FILL: *{info.get('anchor_label', '?')}* (ticket {ticket})"


def format_close_alert(shadow, outcome, close_price, pnl_usd, daily_pnl,
                       slip_txt="", hold_txt="", nh_txt="", evt_block=""):
    """Build the CLOSE telegram body. Never raises. Enrichment fragments
    (slip_txt / hold_txt / nh_txt / evt_block) may be None or missing -- a None
    close_price or pnl degrades to '$?'/'n/a' but the close ALWAYS alerts."""
    shadow = shadow or {}
    slip_txt = slip_txt or ""
    hold_txt = hold_txt or ""
    nh_txt = nh_txt or ""
    evt_block = evt_block or ""
    try:
        label = shadow.get('anchor_label', '?')
        side = shadow.get('side', '?')
        cp_txt = f"${float(close_price):.2f}" if close_price is not None else "$?"
        pnl_txt = f"${float(pnl_usd):+.2f}" if pnl_usd is not None else "n/a"
        daily_txt = f"${float(daily_pnl):+.2f}" if daily_pnl is not None else "n/a"
        return (f"📤 CLOSE: *{label}* {side} `{outcome}`{slip_txt} @ {cp_txt}\n"
                f"P&L: `{pnl_txt}`  |  Daily total: `{daily_txt}`{hold_txt}{nh_txt}"
                + evt_block)
    except Exception as e:
        log.warning(f"format_close_alert degraded ({e!r})")
        return (f"📤 CLOSE: *{shadow.get('anchor_label', '?')}* "
                f"{shadow.get('side', '?')} `{outcome}`")


def _anchor_evt_block(self, rec, actual_utc=None):
    """v3.0.5: scheduled-vs-actual time block for a fill/close message. `rec` is a
    shadow dict that may carry 'sched_utc' (rides along from placement); falls back
    to resolving the schedule from the anchor label. No LATE tag here (the ⏰ tag is
    placement-specific; a fill/close is naturally minutes after the anchor).
    Returns '' (no extra lines) if the scheduled time can't be resolved."""
    try:
        sched_iso = rec.get('sched_utc')
        sched_utc = (pd.Timestamp(sched_iso) if sched_iso
                     else self._anchor_sched_utc(rec.get('anchor_label')))
        if sched_utc is None:
            return ""
        actual = actual_utc or pd.Timestamp.now(tz='UTC')
        return "\n" + anchor_time_block(sched_utc, actual, ontime_grace_s=float('inf'))
    except Exception:
        return ""


def _reconcile_with_broker(self):
    if self.paper:
        return

    try:
        broker_positions = self.adapter.mt5.positions_get(symbol=self.cfg.symbol) or []
        broker_pendings  = self.adapter.mt5.orders_get(symbol=self.cfg.symbol)    or []
    except Exception as e:
        self.tele.warn(f"MT5 reconcile failed: {e}")
        return

    broker_pos_tickets  = {int(p.ticket) for p in broker_positions}
    broker_pend_tickets = {int(o.ticket) for o in broker_pendings}

    # v3.2.3 SOFT-RESTART RECONCILE (observability; first reconcile after boot):
    # classify every ticket across persisted-state ∪ live-broker as RESUME / ADOPT
    # / FINALIZE and post a one-line summary. A live broker position is NEVER left
    # unmanaged -- a non-zero orphan count trips a loud violation. Pure classifier
    # (soft_restart); this only LOGS, the existing rehydrate/promote/close paths
    # below do the actual work (behavior unchanged).
    if not getattr(self, '_reconcile_logged', False):
        try:
            _state_tk = set(int(k) for k in (self._pending_shadow_rehydrate or {}).keys()) \
                | set(self.shadow_positions.keys())
            _actions, _summary = _soft.reconcile(_state_tk, broker_pos_tickets)
            tr = getattr(self, 'ptrace', None)
            if tr is not None:
                for _tk, _act in _actions.items():
                    tr.reconcile(ticket=_tk, in_state=(_tk in _state_tk),
                                 on_broker=(_tk in broker_pos_tickets), action=_act)
                tr.reconcile_summary(**_summary)
                for _tk in broker_pos_tickets:
                    if _actions.get(_tk) not in (_soft.RESUME, _soft.ADOPT):
                        tr.reconcile_orphan(_tk)  # tripwire (must never fire)
            if _summary['adopted'] or _summary['resumed'] or _summary['finalized']:
                self.tele.info(
                    f"⚡ REHYDRATED | resumed {_summary['resumed']} adopted "
                    f"{_summary['adopted']} finalized {_summary['finalized']} | "
                    f"orphans {_summary['orphans']}")
        except Exception as _re:
            log.warning(f"reconcile telemetry failed (non-fatal): {_re!r}")
        self._reconcile_logged = True

    # v2.5: REHYDRATE from persisted state for any broker position we don't
    # already track in-memory. This handles bot restart mid-trade so we
    # preserve max_fav (= $5 lock state) and fill_time (= freeze gate state).
    if self._pending_shadow_rehydrate:
        for broker_p in broker_positions:
            tk = int(broker_p.ticket)
            if tk in self.shadow_positions:
                continue
            saved = self._pending_shadow_rehydrate.get(str(tk))
            if saved:
                self.shadow_positions[tk] = {
                    'anchor_label': saved.get('anchor_label', 'RECOVERED'),
                    'side':         saved.get('side') or ('BUY' if broker_p.type == 0 else 'SELL'),
                    'entry_price':  float(broker_p.price_open),
                    'current_sl':   float(broker_p.sl),
                    'tp_level':     float(broker_p.tp),
                    # v2.5 critical: restore max_fav from persisted state, not entry price
                    'max_fav':      float(saved.get('max_fav') or broker_p.price_open),
                    'fill_time':    saved.get('fill_time') or pd.Timestamp.now(tz='UTC').isoformat(),
                    'role':         saved.get('role', 'normal'),  # v2.9
                }
                self.tele.info(
                    f"♻️ Rehydrated position {tk} {saved.get('side','?')} "
                    f"entry=${broker_p.price_open:.2f} max_fav=${float(saved.get('max_fav') or broker_p.price_open):.2f} "
                    f"SL=${broker_p.sl:.2f} (lock state preserved)"
                )
        # Clear the rehydration source after first reconcile
        self._pending_shadow_rehydrate = {}

    # v2.9.8: rehydrate PENDING stop orders (restart-safe rescue flag)
    _pend_saved = getattr(self, '_pending_pendings_rehydrate', None)
    if _pend_saved:
        for broker_o in broker_pendings:
            tk = int(broker_o.ticket)
            if tk in self.shadow_pendings:
                continue
            saved = _pend_saved.get(str(tk))
            if saved:
                self.shadow_pendings[tk] = {
                    'anchor_label':   saved.get('anchor_label', 'RECOVERED'),
                    'side':           saved.get('side') or ('BUY' if broker_o.type in (2, 4) else 'SELL'),
                    'sibling_ticket': saved.get('sibling_ticket'),
                    'entry_price':    float(saved.get('entry_price') or broker_o.price_open),
                    'rescue_on_fill': bool(saved.get('rescue_on_fill', False)),
                }
                self.tele.info(
                    f"♻️ Rehydrated pending {tk} {saved.get('side','?')} "
                    f"(rescue_on_fill={bool(saved.get('rescue_on_fill', False))})")
        self._pending_pendings_rehydrate = {}

    # Detect fills (sibling cancel)
    for ticket, info in list(self.shadow_pendings.items()):
        if isinstance(ticket, str): continue
        if ticket not in broker_pend_tickets and ticket in broker_pos_tickets:
            info = self.shadow_pendings.pop(ticket)
            sibling = info['sibling_ticket']
            # v3.2.6 A3-type DOUBLE-FILL log (LOG-ONLY, no gate): both original legs
            # of the same anchor have now filled (price whipsawed through both
            # anchor+/-$5 stops -- the A3 straddle whipsaw). The sibling still runs
            # exactly as before (No-OCO rescue); we only make the event VISIBLE so an
            # A3-type double-fill is never silent. NO behavior change, no gating.
            if sibling is not None and sibling in self.shadow_positions:
                _sib_sh = self.shadow_positions.get(sibling, {}) or {}
                self.tele.warn(
                    f"⚠️ A3 DOUBLE-FILL {info.get('anchor_label')}: both original "
                    f"legs filled ({_sib_sh.get('side')} #{sibling} + "
                    f"{info.get('side')} #{ticket}) — straddle whipsaw; sibling runs "
                    f"as rescue (log-only, no gate)")
                _tr0 = getattr(self, 'ptrace', None)
                if _tr0 is not None:
                    _tr0.double_fill(ticket, info.get('anchor_label'),
                                     side=info.get('side'), sibling_ticket=sibling,
                                     sibling_side=_sib_sh.get('side'),
                                     entry_price=info.get('entry_price'))
            # v3.0.7: build via the never-raising formatter and send important=True
            # so the fill alert can never be dropped by a formatter throw or by
            # INFO rate limiting (a fill often lands seconds after placement).
            # v3.1.0: also attach a rich Discord card, deduped by event_key.
            _evt = self._anchor_evt_block(info)
            self.tele.send(
                format_fill_alert(info, ticket, _evt),
                Severity.INFO, important=True, critical=True,
                card=dc.card_fill(info.get('anchor_label'), info.get('side'),
                                  info.get('entry_price'), ticket,
                                  role=info.get('role', 'normal'),
                                  sl=info.get('current_sl'), tp=info.get('tp_level'),
                                  sched_actual=(_evt.strip() or None)),
                event_key=f"fill:{ticket}",
            )
            # Cancel sibling (OCO) — v2.3: sibling may be None if other side was skipped pre-flight
            # OCO vs No-OCO sibling handling
            if not getattr(self.cfg, 'no_oco', False):
                if sibling is not None and sibling in broker_pend_tickets:
                    try:
                        self.adapter.cancel_order(sibling)
                    except Exception as e:
                        self.tele.warn(f"Could not cancel sibling {sibling}: {e}")
                if sibling is not None:
                    self.shadow_pendings.pop(sibling, None)
            else:
                if sibling is not None and sibling in self.shadow_pendings:
                    self.shadow_pendings[sibling]['sibling_ticket'] = None
                    # v2.9: the sibling, if it ever fills, is a RESCUE leg --
                    # it only fills after price traveled $10 against this leg.
                    self.shadow_pendings[sibling]['rescue_on_fill'] = True
                    self.tele.info(f"No-OCO: sibling {sibling} left live (reversal can fill it; will run as RESCUE)")
            # Promote to managed position
            broker_p = next(p for p in broker_positions if int(p.ticket) == ticket)
            # v2.3: capture broker's actual fill timestamp for freeze logic
            # broker_p.time is Unix seconds (broker convention — use offset-aware decode)
            try:
                fill_unix = int(broker_p.time)
                if self.adapter.tick_time_offset_hours:
                    fill_unix -= self.adapter.tick_time_offset_hours * 3600
                fill_time_utc = pd.Timestamp(fill_unix, unit='s', tz='UTC')
            except Exception:
                fill_time_utc = pd.Timestamp.now(tz='UTC')
            # v2.9.8 STRUCTURAL RESCUE (Jun-12 A1: rescue flag chain silently
            # failed -> 2nd leg ran as 'normal', no boosts fired). In No-OCO a
            # fill for an anchor that ALREADY has an open non-boost position is
            # BY CONSTRUCTION the rescue leg: it can only fill after price
            # traveled the full stop spread against its twin. The flag is now a
            # hint; the structure is the truth.
            # v3.0.0 Fix A (stale rescue flag): a 2nd fill is a genuine
            # RESCUE only if its twin is STILL OPEN at this moment. The
            # rescue_on_fill flag is set when the FIRST leg fills (below)
            # and was never re-checked against the twin later closing --
            # Jun-12 A4: the SELL banked +$477 and closed; an hour later
            # the BUY filled, inherited the stale flag, was tagged RESCUE
            # and fired 2 boosts with no trapped twin to rescue (A2, the
            # identical setup, fired nothing -> nondeterministic). Re-validate
            # structurally: prefer the explicit sibling_ticket, else any
            # non-boost open position of this anchor. shadow_positions holds
            # only OPEN positions (closed ones are popped below), so
            # membership IS the "twin still open" test.
            _flag_hint = bool(info.get('rescue_on_fill'))
            is_rescue = False
            is_lone = False   # v3.2.3: True only for a twin-CLOSED lone leg
            if getattr(self.cfg, 'no_oco', False):
                # The twin must be open in BROKER-confirmed state, not merely
                # tracked in shadow_positions: the "Detect closures" cleanup runs
                # LATER in this same reconcile cycle, so a twin that just closed at
                # the broker can still linger in shadow_positions and falsely read
                # as open (firing phantom boosts -- the exact failure this guard
                # prevents). broker_pos_tickets (built above) is the truth.
                _sib = info.get('sibling_ticket')
                _twin_open = (_sib is not None and _sib in self.shadow_positions
                              and _sib in broker_pos_tickets) or any(
                    tk in broker_pos_tickets
                    and sp.get('anchor_label') == info['anchor_label']
                    and not sp.get('boost')
                    for tk, sp in self.shadow_positions.items())
                is_rescue = _twin_open or _flag_hint
                if _twin_open and not _flag_hint:
                    self.tele.warn(
                        f"⚠️ rescue flag was MISSING for {info['anchor_label']} "
                        f"{info['side']} -- recovered structurally (twin still "
                        f"open). Check log for flag-loss cause.")
                elif _flag_hint and not _twin_open:
                    is_lone = True   # twin closed -> lone leg (rally OR rescue)
                    # v3.1.3 LONE-LEG HEDGING RESCUE: the No-OCO twin already
                    # closed, but this sibling only fills after price traveled the
                    # full $10 spread against the (now-lone) leg -- i.e. a breakout
                    # is underway. Fire the rescue in the breakout direction
                    # (opposite the losing leg) to offset it and catch the trend.
                    # This deliberately reverses the v3.0.0 twin-open guard: the
                    # Jun-17 A4 lone SELL ran to -630 unhedged; a rescue at -10
                    # would have offset it. HEDGING, never martingale.
                    self.tele.info(
                        f"🚑 LONE-LEG RESCUE for {info['anchor_label']} "
                        f"{info['side']} -- twin already closed; hedging the "
                        f"breakout (boosts go opposite the losing leg).")
            self.shadow_positions[ticket] = {
                'anchor_label': info['anchor_label'],
                'side':         info['side'],
                'entry_price':  float(broker_p.price_open),
                'current_sl':   float(broker_p.sl),
                'tp_level':     float(broker_p.tp),
                'max_fav':      float(broker_p.price_open),
                'fill_time':    fill_time_utc.isoformat(),  # v2.3: persisted, restart-safe
                'role':         'rescue' if is_rescue else 'normal',  # v2.9 / v2.9.8 structural
                'sched_utc':    info.get('sched_utc'),  # v3.0.5: for close-msg times
                # v3.2.9: carry the TESTFIRE/SCHEDULED tag through to the journal.
                'trigger_source': info.get('trigger_source', 'SCHEDULED'),
                # v3.2.0: boosts fire from the PER-TICK trigger once price moves a
                # full $10 from THIS fill -- never at fill (the A3 -$900 bug).
                'leg_fill_price': float(broker_p.price_open),
                # v3.2.3 No-OCO STACKING: in No-OCO every straddle leg is boost-
                # eligible so the WINNING side stacks (original + 2 RALLY boosts =
                # 3) while the losing leg rides to its SL. A straddle leg is
                # RALLY-ONLY (a losing leg must not fire a rescue -- it just stops
                # out). A twin-CLOSED LONE leg keeps the full rally-OR-rescue arm.
                # In OCO mode (no_oco off) a normal leg stays non-eligible (unchanged).
                'boost_eligible': bool(is_rescue) or bool(getattr(self.cfg, 'no_oco', False)),
                'boost_rally_only': bool(getattr(self.cfg, 'no_oco', False)) and not is_lone,
                'boost_fired':    False,
                'sibling_ticket': info.get('sibling_ticket'),
            }
            # v3.3.0 FILL + PREDICT telemetry (spec 1.1/1.4): log the realized fill
            # and the full exit-door prediction up front, so a grep on the ticket
            # reconstructs the position from its first instant. Never blocks a fill.
            try:
                tr = getattr(self, 'ptrace', None)
                if tr is not None:
                    _e = float(broker_p.price_open)
                    _sl = float(broker_p.sl); _tp = float(broker_p.tp)
                    _sgn = 1.0 if info['side'] == 'BUY' else -1.0
                    _mult = self.cfg.contract_size * self.cfg.lot_size
                    _max_loss = round(_sgn * (_sl - _e) * _mult, 2)
                    _max_gain = round(_sgn * (_tp - _e) * _mult, 2)
                    tr.fill(ticket, info['anchor_label'], side=info['side'],
                            position_price=_e, stop_price=_sl,
                            bid=_e, ask=_e, max_fav=_e,
                            tp=_tp, max_loss=_max_loss, max_gain=_max_gain,
                            lock_level=0)
                    tr.predict(ticket, info['anchor_label'], info['side'],
                               _e, _sl, _tp, _max_loss, _max_gain,
                               trigger=float(getattr(self.cfg, 'boost_trigger_dollars', 10.0)),
                               breakeven_per_pos=6.0)
            except Exception as _te:
                log.warning(f"ptrace fill/predict failed for {ticket}: {_te!r}")
            if is_rescue:
                # v3.2.0 BUG FIX: the OLD path fired boosts HERE, AT THE LEG'S
                # FILL PRICE, in the sibling's direction, always labelled
                # "RESCUE" -- even when the leg had WON. On A3 (Jun 18) that
                # placed boosts at 4266.30 (= the fill) which died on a reversal
                # (~-$900). RETIRED. Boosts now fire ONLY from the per-tick
                # trigger (_check_boost_triggers) once price moves a full $10
                # from leg_fill_price: RALLY (+$10 same dir, winning) or RESCUE
                # (-$10 opposite, losing) -- NEVER at fill. Marked eligible above.
                self.tele.info(
                    f"\U0001F691 LONE leg armed (ticket {ticket}) — boosts fire "
                    f"on a $10 move from ${float(broker_p.price_open):.2f} "
                    f"(RALLY if it runs, RESCUE if it reverses). No fire-at-fill.")

    # Detect closures
    for ticket in list(self.shadow_positions):
        if ticket in broker_pos_tickets:
            continue
        shadow = self.shadow_positions.pop(ticket)
        try:
            deals = self.adapter.mt5.history_deals_get(position=ticket) or []
            close_deal = next((d for d in deals if d.entry == 1), None)
            if close_deal:
                pnl_usd = float(close_deal.profit) + float(close_deal.swap) + float(close_deal.commission)
                # E-22: OPTIMISTIC increment of the state['daily_pnl'] MIRROR (fast, so the
                # close alert below shows a moved number immediately). It is authoritative for
                # nothing -- the next governor read recomputes from broker deal history and
                # overwrites this. Invalidate the computed-P&L cache so that recompute now
                # includes this close.
                self.state['daily_pnl'] += pnl_usd
                try:
                    import daystops as _ds
                    _ds.invalidate_pnl_cache(self)
                except Exception:
                    pass
                # v3.7.3: this anchor-leg close just moved the anchors realized day P&L --
                # latch the profit lock + fire its one-time alert if it now crosses the
                # target (guarded; never affects the close path).
                try:
                    _ad = getattr(self, '_anchors_daystop', None)
                    if callable(_ad):
                        _ad()
                except Exception:
                    pass
                close_price = float(close_deal.price)
                # v3.0.6 OBSERVER: attribute this close to its fleet event (if any)
                # and finalize the event once all members have closed. Wrapped so a
                # logging error can never affect the close path.
                try:
                    self._rescue_event_on_close(ticket, pnl_usd)
                except Exception as _e:
                    log.warning(f"rescue_event_on_close({ticket}) failed (non-fatal): {_e!r}")
                # v2.9.8 EXIT CLASSIFIER: name the RULE that fired by comparing
                # the close to the bot's own intended stop (current_sl), instead
                # of guessing from distance-to-entry. Jun-12 A1 lesson: a +$10
                # LADDER tier exit was labeled 'Trail' (false FREEZE BREACH) and
                # a BE exit that slipped $2.20 masqueraded as a loss-making trail.
                _sgn = 1.0 if shadow['side'] == 'BUY' else -1.0
                _entry = float(shadow['entry_price'])
                _cur_sl = shadow.get('current_sl')
                slip_txt = ''
                if abs(close_price - (_entry + _sgn * self.cfg.tp_dist)) < 0.05:
                    outcome = 'TP'
                elif _sgn * (close_price - (_entry - _sgn * self.cfg.sl_dist)) <= 0.05:
                    outcome = 'SL'
                else:
                    _locked = _sgn * (float(_cur_sl) - _entry) if _cur_sl is not None else None
                    if _locked is None:
                        outcome = 'Trail'
                    elif abs(_locked) <= 0.10:
                        outcome = 'BE'        # ladder tier 1 (+2.5 -> entry)
                    elif abs(_locked - 4.00) <= 0.10:
                        outcome = 'LOCK4'     # ladder tier 2 (+6 -> +4)
                    elif _locked >= 7.90:
                        outcome = 'TIER'      # ladder tier 3 (+10 -> peak-2, floor +8)
                    else:
                        outcome = 'Trail'     # genuine post-hold trail level
                    if _cur_sl is not None and abs(close_price - float(_cur_sl)) > 0.30:
                        slip_txt = (f" (slip {_sgn * (close_price - float(_cur_sl)):+.2f}"
                                    f" vs stop ${float(_cur_sl):.2f})")
                if shadow.get('tstop'):
                    outcome = 'TSTOP'
                # v2.7: hold-duration audit -- permanent detector for the freeze bug.
                # fill_time is TRUE UTC; close_deal.time is broker epoch seconds, so
                # subtract the offset to compare in the same (UTC) clock.
                hold_min = None
                try:
                    _ft = shadow.get('fill_time')
                    if _ft:
                        _off = getattr(self.adapter, 'tick_time_offset_hours', 0) or 0
                        _close_utc = pd.Timestamp(int(close_deal.time) - _off * 3600,
                                                  unit='s', tz='UTC')
                        hold_min = (_close_utc - pd.Timestamp(_ft)).total_seconds() / 60.0
                except Exception:
                    hold_min = None
                hold_txt = f"  |  held `{hold_min:.1f}m`" if hold_min is not None else ""
                # Freeze-breach alarm: a Trail-class exit before the freeze window
                # elapsed should be impossible. 2026-07-17: profit-lock rungs (BE / +$4 /
                # peak-2) now fire DURING the hold by design, so a Trail exit IN PROFIT
                # inside the hold is EXPECTED (the discrete locks realizing) -- excluded.
                # Only a Trail exit at a LOSS (below entry for BUY / above for SELL) inside
                # the hold is anomalous and still worth the alarm.
                _fb_sgn = 1.0 if shadow['side'] == 'BUY' else -1.0
                _fb_profit = _fb_sgn * (close_price - float(shadow['entry_price']))
                if (hold_min is not None and outcome == 'Trail'
                        and self.cfg.freeze_minutes > 0
                        and hold_min < self.cfg.freeze_minutes - 0.5
                        and _fb_profit < -0.40):
                    self.tele.warn(
                        f"🚨 *FREEZE BREACH* {shadow['anchor_label']} "
                        f"{shadow['side']}: Trail exit after only {hold_min:.1f}m "
                        f"(< freeze {self.cfg.freeze_minutes}m). Trail gate is "
                        f"engaging early -- investigate before next anchor."
                    )
                sev = Severity.SUCCESS if pnl_usd > 0 else Severity.WARN
                # v2.9.8 SHADOW NO-HOLD verdict line (journal feeds the
                # hold-vs-no-hold decision; computed in trail loop)
                nh_txt = ''
                _nh = shadow.get('nh_exit')
                if _nh is not None:
                    _nh_pnl = _sgn * (float(_nh) - _entry) * self.cfg.lot_size * 100
                    nh_txt = f"\nno-hold trail would have exited @ ${float(_nh):.2f} (`${_nh_pnl:+.2f}`)"
                # v3.0.7: never-raising formatter + important=True so a close
                # alert can never be lost to a formatter throw or rate limiting.
                self.tele.send(
                    format_close_alert(
                        shadow, outcome, close_price, pnl_usd,
                        self.state['daily_pnl'],
                        slip_txt=slip_txt, hold_txt=hold_txt, nh_txt=nh_txt,
                        evt_block=self._anchor_evt_block(shadow)),
                    sev, important=True, critical=True,
                    card=dc.card_close(shadow.get('anchor_label'),
                                       shadow.get('side'), outcome,
                                       shadow.get('entry_price'), close_price,
                                       pnl_usd, held_min=hold_min,
                                       day_total=self.state['daily_pnl'],
                                       nh_shadow=(nh_txt.strip() or None)),
                    event_key=f"close:{ticket}",
                )
                # Append to today's trade log
                with open(self.daylog_path, "a", newline="", encoding='utf-8') as f:
                    csv.writer(f).writerow([
                        self.state['last_broker_date'],
                        shadow['anchor_label'], shadow['side'],
                        shadow['entry_price'], close_price,
                        outcome, round(pnl_usd, 2), ticket,
                    ])
                # v3.3.0 EXIT telemetry (spec 1.1/1.5): close the ticket's life
                # story and run the self-consistency assert -- a TRAIL/lock exit
                # with NO preceding TRAIL_ADVANCE writes a TELEMETRY_VIOLATION (the
                # exact silence that hid the A2 bug). Never blocks the close path.
                try:
                    tr = getattr(self, 'ptrace', None)
                    if tr is not None:
                        _trail_class = outcome in ('Trail', 'BE', 'LOCK4', 'TIER')
                        _etype = 'TRAIL' if _trail_class else outcome
                        _isl = shadow.get('current_sl')
                        _slip = (round(close_price - float(_isl), 2)
                                 if _isl is not None else None)
                        tr.exit(ticket, shadow.get('anchor_label'),
                                side=shadow.get('side'),
                                position_price=shadow.get('entry_price'),
                                max_fav=shadow.get('max_fav'),
                                stop_price=_isl, exit_type=_etype,
                                exit_reason=outcome, intended_price=_isl,
                                actual_fill=round(close_price, 2), slip=_slip,
                                pnl=round(pnl_usd, 2),
                                held_minutes=(round(hold_min, 1)
                                              if hold_min is not None else None),
                                nohold_counterfactual=shadow.get('nh_exit'))
                except Exception as _te:
                    log.warning(f"ptrace exit failed for {ticket}: {_te!r}")
                # v2.5.6: rich journal row (one per fill) for strategy evaluation
                try:
                    self._write_journal(shadow, close_deal, close_price, outcome, pnl_usd, ticket)
                except Exception as je:
                    log.warning(f"journal write failed for {ticket}: {je}")
                self._save_state()
            else:
                # v3.0.7: the position is gone from the broker but its closing
                # deal isn't in history yet (timing). Previously this branch did
                # nothing -- the close vanished with NO alert. Always announce a
                # detected close, even degraded, so a close is never silent.
                self.tele.send(
                    format_close_alert(
                        shadow, 'CLOSED', None, None, self.state.get('daily_pnl'),
                        evt_block=self._anchor_evt_block(shadow)),
                    Severity.WARN, important=True, critical=True,
                    card=dc.card_close(shadow.get('anchor_label'),
                                       shadow.get('side'), 'CLOSED',
                                       shadow.get('entry_price'), None, None,
                                       day_total=self.state.get('daily_pnl')),
                    event_key=f"close:{ticket}",
                )
                log.warning(f"close detected for {ticket} but no close deal in "
                            f"history yet -- alerted degraded")
        except Exception as e:
            self.tele.warn(f"Could not fetch close deal for {ticket}: {e}")


# ============================================================================
# v3.2.0 — lone-leg boost firing (PER-TICK; never at fill). Bound on LiveTrader.
# The DECISION is boosts.plan_boost_event (shared with tests + backtest); only the
# live order PLACEMENT lives here. ISOLATION: boosts are their own tickets and
# never close/modify/net against the original leg.
# ============================================================================
def _fire_boost_event(self, leg_ticket, leg_shadow, plan):
    """v3.2.8 Phase 3 seam: route the CONFIRMED boost plan by the sign of leg_fav
    (plan.kind) to rally.fire (winning -> pyramid) or rescue.fire (losing -> hedge),
    both of which call boosts_common.place_fleet for the shared placement. Kept as a
    bound LiveTrader method so the live scan AND the selftest stubs hit the same seam.
    The placement body now lives in boosts_common.place_fleet (byte-identical)."""
    return boosts_dispatch.fire(self, leg_ticket, leg_shadow, plan)


def _enforce_boost_cap(self, mid):
    """v3.2.8 Phase 2 seam -> boosts_common.enforce_cap: the shared -$700 combined-
    boost whipsaw cap hard-close (body byte-identical to the v3.2.7 original)."""
    return boosts_common.enforce_cap(self, mid)


def _check_boost_triggers(self):
    """v3.2.0 PER-TICK trigger: fire a lone leg's boost event the instant price is
    >= $10 from its fill (RALLY winning / RESCUE losing, via the canonical
    boosts.plan_boost_event) -- NEVER at fill -- then enforce the -$700 cap. Live
    only; never raises onto the tick loop."""
    if self.paper:
        return
    try:
        tk = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
        mid = (float(tk.bid) + float(tk.ask)) / 2.0
    except Exception:
        return
    # v3.4.0: stash the current tick price so the RALLY override pullback-entry gate
    # (rally._override_entry_decision) can read it without a signature change. Harmless
    # when override_entry_enabled is OFF (the gate never reads it).
    self._last_boost_mid = mid
    # v3.6.0 ENGINE SWITCH (anchors OFF = MANAGE-ONLY): the whole boost family
    # (RALLY / RESCUE / F-B trapped late-rescue) opens NEW tickets off an anchor
    # leg, so it is entry-side and switched off with the engine -- including on
    # legs restored from state.json (every candidate leg lives in shadow_positions,
    # exactly what the loop below iterates). The whipsaw-cap enforcement still
    # runs: hard-closing a breaching boost protects an OPEN position, it is not a
    # new entry. GUARDED read (a stub trader without the runtime dict reads
    # ENABLED -- see live_trader._engine_enabled).
    # v3.7.3: the anchors engine takes NO new boost risk when it is switched OFF OR its
    # daily stop is active (loss halt / profit lock / account lock) -- the boost family is
    # entry-side. The whipsaw-cap enforcement still runs (protective close of a breaching
    # boost). GUARDED: a stub without either seam reads ENABLED.
    _eng = getattr(self, 'engines', None)
    _dsb = getattr(self, '_anchors_daystop_blocked', None)
    _daystop_blocked = bool(callable(_dsb) and _dsb())
    if (isinstance(_eng, dict) and not bool(_eng.get('anchors', True))) or _daystop_blocked:
        try:
            self._enforce_boost_cap(mid)
        except Exception as e:
            log.warning(f"_enforce_boost_cap failed: {e!r}")
        return
    # D-31: boost_spec_v2 REPLACES the F-B / RALLY / RESCUE trigger below entirely
    # (band-outside boosts that JOIN the winning side + a one-way ratchet), and
    # thereby GATES trapped_late_rescue (F-B) OFF -- the F-B block is never reached.
    # Flag OFF (default) -> this is skipped and the path below is byte-identical.
    if bool(getattr(self.cfg, 'boost_spec_v2', False)):
        try:
            import boost_spec as _bs
            _bs.boost_spec_tick(self, mid)
        except Exception as e:
            log.warning(f"boost_spec_tick failed (non-fatal): {e!r}")
        return
    import tick_hold as _th
    # v3.2.8 Phase 1: ASYMMETRIC arm. A WINNING leg arms RALLY at +rally_arm_fav ($5);
    # a LOSING leg arms RESCUE at -rescue_arm ($10, unchanged boost_trigger_dollars).
    rescue_arm = rescue.event_arm(self.cfg)   # $10 losing-side arm (unchanged)
    rally_arm = rally.event_arm(self.cfg)     # $5 winning-side arm (Phase 1)
    tr = getattr(self, 'ptrace', None)
    for ticket, shadow in list(self.shadow_positions.items()):
        if shadow.get('boost') or shadow.get('boost_fired') \
                or not shadow.get('boost_eligible'):
            continue
        side = shadow['side']
        fill_px = float(shadow.get('leg_fill_price', shadow['entry_price']))
        rally_only = bool(shadow.get('boost_rally_only', False))
        leg_fav = (mid - fill_px) if side == 'BUY' else (fill_px - mid)
        # F-B (flag-gated, DEFAULT OFF): a TRAPPED No-OCO losing straddle leg (rally_only ->
        # rescue suppressed, normally rides naked to its -$18 SL) may arm a CAPPED late-
        # rescue hedge (OPPOSITE dir, OWN $13 SL + per-event cap) once it is
        # trapped_rescue_arm_dollars adverse. Flag OFF -> plan_trapped_late_rescue returns
        # None -> this block is a no-op (byte-identical). Anchor-side only: Rogue legs
        # (magic 20260626) are not in shadow_positions and are never boost_rally_only, so
        # this can never touch a Rogue ticket. Fires once per trapped leg.
        if (rally_only and not shadow.get('trapped_rescue_fired')
                and bool(getattr(self.cfg, 'trapped_late_rescue_enabled', False))):
            try:
                _lr = boosts.plan_trapped_late_rescue(side, fill_px, mid, self.cfg)
            except Exception:
                _lr = None
            if _lr is not None:
                shadow['trapped_rescue_fired'] = True   # one late-rescue per trapped leg
                # V-3/D-6 Branch 2: this path fires THROUGH the break-and-hold gate
                # (by design, see the comment above) with no distinct log line -- the
                # ONLY prior evidence was the generic "BOOST FIRED" alert inside
                # place_fleet, indistinguishable from a normal RALLY/RESCUE fire.
                # Logging only: emits before the fire, never gates it.
                try:
                    self.tele.info(
                        f"🛟 F-B TRAPPED RESCUE FIRED | parent {ticket} | "
                        f"{_lr.boost_side} x{_lr.n} | SL ${_lr.sl_dollars:.2f}")
                except Exception:
                    pass
                if tr is not None:
                    try:
                        tr.break_eval(shadow.get('anchor_label'), side=_lr.boost_side,
                                      kind=_lr.kind, result='BYPASS_TRAPPED_RESCUE',
                                      reason='fb_trapped_late_rescue_fires_through_gate')
                    except Exception:
                        pass
                self._fire_boost_event(ticket, shadow, _lr)
                continue
        # winning side arms at the rally arm ($5); losing side at the rescue arm ($10).
        crossed = (leg_fav >= rally_arm) or (leg_fav <= -rescue_arm)
        try:
            plan = boosts.plan_boost_event(
                side, fill_px, mid, self.cfg, allow_rescue=not rally_only)
        except Exception:
            plan = None
        if plan is None:
            # v3.2.5 tick-hold: a cross that reverts BACK inside +/-$10 before it
            # held hold_ticks is a blip -- reset the streak (logged, not fired).
            if not crossed and int(shadow.get('boost_cross_streak', 0)) > 0:
                if tr is not None:
                    tr.tick_blip_rejected(ticket, shadow.get('anchor_label'),
                                          side=side, reverted_from=shadow['boost_cross_streak'],
                                          move_dollars=round(leg_fav, 2))
                shadow['boost_cross_streak'] = 0
            # v3.2.3 MISSED_BOOST watchdog: a fire was EXPECTED (the threshold was
            # crossed AND that kind is enabled here) but none was planned -- the
            # logic failed to detect a valid trigger. A silent no-fire is a
            # failure, not a no-op (the lone-leg analogue of A2's silent miss).
            rally_exp = (leg_fav >= rally_arm
                         and bool(getattr(self.cfg, 'rally_boosts_enabled', True)))
            rescue_exp = (leg_fav <= -rescue_arm and not rally_only
                          and bool(getattr(self.cfg, 'rescue_boosts_enabled', True)))
            if (rally_exp or rescue_exp) and tr is not None:
                _missed_trig = rally_arm if rally_exp else rescue_arm
                tr.missed_boost(ticket, shadow.get('anchor_label'), side=side,
                                position_price=fill_px, move_dollars=round(leg_fav, 2),
                                trigger=_missed_trig, rally_only=rally_only)
            continue
        # v3.2.5 tick-hold confirm: the cross must HOLD >= hold_ticks consecutive
        # ticks before it fires; a blip that reverts within the window never gets
        # here (reset above). Levels/stack/cap unchanged -- this only gates WHEN.
        streak, tstate = _th.step_cross(
            int(shadow.get('boost_cross_streak', 0)), True, self.cfg)
        shadow['boost_cross_streak'] = streak
        # the arm this plan honored (RALLY $5 / RESCUE $10) -- for honest telemetry.
        plan_arm = rally_arm if plan.kind == 'RALLY' else rescue_arm
        if tstate != _th.CONFIRMED:
            if tr is not None:
                tr.tick_cross_candidate(ticket, shadow.get('anchor_label'),
                                        side=plan.boost_side, held_ticks=streak,
                                        hold_ticks=_th.hold_ticks(self.cfg),
                                        move_dollars=plan.move_dollars, trigger=plan_arm)
            continue   # crossed but not yet held -> keep watching
        if tr is not None:
            tr.tick_hold_confirmed(ticket, shadow.get('anchor_label'),
                                   side=plan.boost_side, held_ticks=streak,
                                   move_dollars=plan.move_dollars)
        # stack after this event: parent leg (1) + n boosts.
        stack_after = 1 + int(plan.n)
        # v3.2.3 Feature D — BREAK-AND-HOLD gate: do NOT stack on a fake break.
        # Only stack on a CONFIRMED break (cleared edge + held N candles + retrace
        # < Y). Guarded: any error / disabled -> allow (legacy). Live-only data.
        # v3.2.7: gate RALLY boosts ONLY. A RESCUE boost (the opposite-side sibling
        # that becomes the winner after a whipsaw) fires FREELY on direction commit
        # -- gating it on a confirmed break wrongly suppressed winning-side recovery
        # legs. RESCUE is still bounded by the +/-$10 trigger, tick-hold >=3 (above)
        # and the FP guard (below); ONLY break-and-hold is bypassed. Toggle:
        # cfg.rescue_bypass_break_and_hold (default True) -> False restores gating both.
        _gate_this = (plan.kind == 'RALLY'
                      or not bool(getattr(self.cfg, 'rescue_bypass_break_and_hold', True)))
        if _gate_this:
            if not self._break_and_hold_ok(shadow, plan):
                continue
        elif plan.kind == 'RESCUE' and bool(getattr(self.cfg, 'rescue_entry_enabled', False)):
            # v3.5.0 RESCUE adaptive pullback entry (flag ON): arm-then-wait for a
            # bounce-rollover / smooth-confirm instead of the immediate bypass-fire.
            # Flag OFF -> this branch is skipped and rescue free-fires exactly as today.
            if not self._rescue_entry_ok(shadow, plan):
                continue
        elif tr is not None:
            tr.break_eval(shadow.get('anchor_label'), side=plan.boost_side,
                          kind=plan.kind, result='BYPASS_RESCUE',
                          reason='rescue_fires_free_on_commit')
        # v3.2.3 Feature E — FP GUARD: block/reduce a stack that would breach the
        # account's FP rule at the chosen lot BEFORE placing it.
        if not self._fp_guard_ok(shadow, stack_after):
            continue
        if tr is not None:
            tr.boost_arm(ticket, shadow.get('anchor_label'), parent_ticket=ticket,
                         side=plan.boost_side, position_price=fill_px,
                         boost_kind=plan.kind, stack_size=stack_after,
                         stack_cap=boosts.stack_cap(self.cfg),
                         move_dollars=plan.move_dollars, trigger=plan_arm)
        shadow['boost_fired'] = True   # one event per leg (set before placing)
        try:
            self._fire_boost_event(ticket, shadow, plan)
        except Exception as e:
            log.warning(f"_fire_boost_event({ticket}) failed: {e!r}")
            # v3.2.3 BOOST_ARM_ORPHANED: armed (trigger detected) but execution
            # dropped -- channel error, retcode fail, order rejected.
            if tr is not None:
                tr.boost_arm_orphaned(ticket, shadow.get('anchor_label'),
                                      side=plan.boost_side, error=repr(e))
    try:
        self._enforce_boost_cap(mid)
    except Exception as e:
        log.warning(f"_enforce_boost_cap failed: {e!r}")


def _break_and_hold_ok(self, shadow, plan):
    """v3.2.8 Phase 2 seam -> rally.break_and_hold_ok: the RALLY-only break-and-hold
    gate (it stays on rally, per the v3.2.7 split). Kept as a bound LiveTrader method
    so the scan + selftest stubs hit it; body byte-identical, relocated to rally.py."""
    return rally.break_and_hold_ok(self, shadow, plan)


def _rescue_entry_ok(self, shadow, plan):
    """v3.5.0 seam -> rescue.entry_gate_ok: the RESCUE-only adaptive pullback-entry
    gate (separate call site from the rally gate, per the standing rally/rescue split).
    Only reached when rescue_entry_enabled is ON."""
    import rescue
    return rescue.entry_gate_ok(self, shadow, plan)


def _fp_guard_ok(self, shadow, stack_after):
    """v3.2.8 Phase 2 seam -> boosts_common.fp_guard_ok: the shared FP-rule + 5-long
    stack-cap pre-trade guard (body byte-identical to the v3.2.7 original)."""
    return boosts_common.fp_guard_ok(self, shadow, stack_after)
