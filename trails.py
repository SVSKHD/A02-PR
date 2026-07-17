"""AUREON — trail management on bar close: ladder/trail, TSTOP, SL heal, STOP-THROUGH.

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

from telemetry import telemetry_from_env, Severity
from mt5_adapter import _MT5_RETCODE_MAP

log = logging.getLogger("AUREON")

# --- SL-modify reject handling (2026-07-17: base_lock modify REJECTED rc=10016 with
# no retry/fallback -> a +7.77 peak rode back to -10 open). PURE helpers so the
# retry/broker-min-adjust/fallback-close decision is unit-testable offline. -----------
STOPS_REJECT_RCS = frozenset({10016, 10013})   # INVALID_STOPS / INVALID -> adjust + retry


def is_stops_reject(rc) -> bool:
    """True for a stops-class SL-modify reject (10016 INVALID_STOPS / 10013 INVALID)
    that warrants a broker-min-adjusted retry then a would-have-fired fallback."""
    return rc in STOPS_REJECT_RCS


def broker_min_sl(side: str, bid: float, ask: float, floor: float) -> float:
    """The closest LEGAL stop to market for a re-try: `floor` below the bid (BUY) or
    above the ask (SELL). `floor` is the broker's min stop distance (>= a small probe
    minimum). Never sends an illegal (through-market) value again."""
    return round(bid - float(floor), 2) if side == "BUY" else round(ask + float(floor), 2)


def lock_would_fire(side: str, intended: float, bid: float, ask: float) -> bool:
    """True when price is AT-OR-BEYOND the intended lock level — i.e. the lock 'would
    have fired' as an SL. For a BUY the stop sits below: fired when bid <= intended;
    for a SELL it sits above: fired when ask >= intended. Only then does a
    still-rejected modify escalate to a market close (never scratch a live winner)."""
    return (bid <= float(intended)) if side == "BUY" else (ask >= float(intended))


def _modify_ok(res) -> bool:
    return res is not None and (
        getattr(res, "retcode", None) == 10009
        or (isinstance(res, dict) and res.get("paper")))


def modify_sl_with_fallback(modify_fn, close_fn, side, intended, bid, ask, floor):
    """Never abandon a rejected profit lock silently. `modify_fn(sl)` sends an SL
    modify (returns the MT5 result); `close_fn()` market-closes. Flow:
      1. modify at `intended`; DONE if accepted;
      2. a stops-class reject (10016/10013) -> retry ONCE at the broker-min-adjusted
         level (broker_min_sl); RETRY_OK if accepted;
      3. still rejected AND the lock would have fired (price at/through it) ->
         market-close (FALLBACK_CLOSE);
      4. otherwise keep the old stop and retry next bar (KEEP).
    Returns {'ok', 'outcome', 'sl', 'rc'}. Pure orchestration (no IO of its own) so
    the whole decision is unit-testable with fake callables."""
    r1 = modify_fn(intended)
    if _modify_ok(r1):
        return {"ok": True, "outcome": "DONE", "sl": intended, "rc": getattr(r1, "retcode", None)}
    rc = getattr(r1, "retcode", None) if r1 is not None else None
    if not is_stops_reject(rc):
        return {"ok": False, "outcome": "REJECT", "sl": intended, "rc": rc}
    adj = broker_min_sl(side, bid, ask, floor)
    r2 = modify_fn(adj)
    if _modify_ok(r2):
        return {"ok": True, "outcome": "RETRY_OK", "sl": adj, "rc": rc}
    if lock_would_fire(side, intended, bid, ask):
        close_fn()
        return {"ok": True, "outcome": "FALLBACK_CLOSE", "sl": intended, "rc": rc}
    return {"ok": False, "outcome": "KEEP", "sl": intended, "rc": rc}


def _resolve_parent_sl(self, shadow):
    """E-6: READ-ONLY resolve a boost's PARENT anchor-leg current trailing stop, from
    shadow['parent_ticket'] -> self.shadow_positions[parent]['current_sl']. Returns None
    unless ride-with-parent is ON, this is a boost, the parent_ticket is present, AND the
    parent is STILL OPEN (membership in shadow_positions = open). A closed/missing parent
    -> None -> the boost runs its own trail (edge cases #2/#3). A rescue-leg parent is still
    read (its current_sl is a valid trailing stop). NEVER closes or mutates the parent
    (isolation: read-only). Logs once (rate-limited) when parent_ticket is missing so a
    boost that can't ride is visible, never silent. Any error -> None (own trail)."""
    try:
        if not bool(getattr(self.cfg, 'boost_ride_with_parent', False)):
            return None
        if not shadow.get('boost'):
            return None
        ptk = shadow.get('parent_ticket')
        if ptk is None:
            if self._rl_ok(f"e6_noparent:{shadow.get('boost_event')}", 300.0):
                log.info(f"E-6: boost {shadow.get('anchor_label')} has no parent_ticket "
                         f"-- running own trail (no ride-with-parent)")
            return None
        parent = self.shadow_positions.get(int(ptk))
        if not parent:                 # parent already closed -> own trail (edge case #2)
            return None
        psl = parent.get('current_sl')
        return float(psl) if psl is not None else None
    except Exception as e:
        log.warning(f"E-6 _resolve_parent_sl failed (own trail): {e!r}")
        return None


def _manage_trails_on_bar_close(self):
    if not self.shadow_positions:
        return
    bars = self.adapter.get_latest_m1(self.cfg.symbol, 2)
    if bars is None or len(bars) < 2:
        return
    closed_bar = bars[-2]
    bar_series = pd.Series({
        'open':  float(closed_bar['open']),
        'high':  float(closed_bar['high']),
        'low':   float(closed_bar['low']),
        'close': float(closed_bar['close']),
    })
    bar_time = pd.Timestamp(closed_bar['time'], unit='s', tz='UTC')

    from strategy import update_position_on_bar, lock_level_for  # late import

    for ticket, shadow in list(self.shadow_positions.items()):
        old_sl = shadow['current_sl']
        # v2.3: pull stored fill_time so the freeze window is anchored to the
        # actual broker fill timestamp (restart-safe). Fallback to bar_time only
        # if state predates the patch (legacy positions opened before v2.3 deploy).
        fill_time_iso = shadow.get('fill_time')
        if fill_time_iso:
            try:
                entry_time_for_pos = pd.Timestamp(fill_time_iso)
                if entry_time_for_pos.tzinfo is None:
                    entry_time_for_pos = entry_time_for_pos.tz_localize('UTC')
                # v2.7 FIX (CRITICAL): fill_time is stored in TRUE UTC (the broker
                # offset is subtracted at capture), but bar_time is broker-clock-
                # LABELED-as-UTC (MT5 convention, no offset applied). Comparing them
                # inflated elapsed by +offset hours (+3h), so the freeze window was
                # ALWAYS already expired and the trail engaged from bar one on every
                # position (the $63-on-a-$1,500-move exits). Shift fill_time back
                # into the broker-clock convention so both sides use the same clock.
                _off = getattr(self.adapter, 'tick_time_offset_hours', 0) or 0
                entry_time_for_pos = entry_time_for_pos + pd.Timedelta(hours=_off)
            except Exception:
                entry_time_for_pos = None  # unknown fill time -> no freeze, normal trail
        else:
            # v2.7 FIX: None = no freeze (bot.py gates on `entry_time is not None`).
            # The old `= bar_time` fallback made elapsed ~= 0 on EVERY bar, freezing
            # the trail FOREVER -- the opposite of the original comment's intent.
            entry_time_for_pos = None

        pos = self._Position(
            anchor_label=shadow['anchor_label'],
            side=shadow['side'],
            entry_price=shadow['entry_price'],
            entry_time=entry_time_for_pos,
            current_sl=shadow['current_sl'],
            tp_level=shadow['tp_level'],
            max_fav=shadow['max_fav'],
            lot=self.cfg.lot_size,
            role=shadow.get('role', 'normal'),  # v2.9 role-aware ladder
            boost=bool(shadow.get('boost', False)),  # v3.1.3 boost trail-after-+8
            # v3.2.8 Phase 1: thread the boost kind so a RALLY boost trails on the
            # tighter rally arm/lock/gap; defaults to RESCUE (byte-identical v3.2.7).
            boost_kind=shadow.get('boost_kind', 'RESCUE'),
            # E-6: resolve this boost's PARENT anchor leg current stop READ-ONLY so a
            # RALLY boost can ride with the parent (strategy._ride_with_parent_stop). Only
            # for an open boost with a known parent_ticket still in shadow_positions; a
            # closed/missing parent -> None -> the boost runs its own trail (unchanged).
            # ISOLATION: this only READS the parent shadow; it never closes/mutates it.
            parent_sl=_resolve_parent_sl(self, shadow),
        )
        old_max_fav = shadow.get('max_fav')
        # v3.3.0: pass the per-position tracer so MAXFAV_UPDATE / LOCK_ARM /
        # TRAIL_ADVANCE are logged for THIS ticket -- the lines A2 was missing.
        update_position_on_bar(pos, bar_series, bar_time, self.cfg,
                               tracer=getattr(self, 'ptrace', None), ticket=ticket)
        shadow['current_sl'] = pos.current_sl
        shadow['max_fav'] = pos.max_fav

        # v3.1.6: a BOOST manages its OWN breath-gap trail + $10 backstop in
        # strategy (_update_boost_on_bar). When that returns a close, close THIS
        # boost ticket at market -- both stops live, whichever hit first. ISOLATION:
        # this only ever closes the boost's own ticket; it never reads, modifies,
        # or closes the original leg (a different shadow position managed in its own
        # iteration). Non-boost legs are unaffected (their pos.closed is ignored
        # here; the broker SL still governs them, exactly as before).
        if pos.closed and shadow.get('boost'):
            self.tele.send(
                f"⚡ BOOST exit {shadow['anchor_label']} {shadow['side']} "
                f"@ ~${float(pos.exit_price):.2f} ({pos.outcome}) -- breath-gap "
                f"trail/$10 backstop; original leg unaffected",
                Severity.INFO, important=True)
            try:
                self.adapter.close_position(ticket, dry_run=self.paper)
            except Exception as e:
                log.warning(f"boost trail close failed for {ticket}: {e}")
            if pos.current_sl != old_sl or pos.max_fav != old_max_fav:
                self._save_state()
            continue

        # v2.9.8 SHADOW NO-HOLD LOG (journal-only, zero behavior change):
        # where would a trail with NO 45m hold (arm $2.50, gap $2.00, from
        # fill) have exited this leg? Hithesh's hold-vs-no-hold question
        # gets answered from live data instead of a one-day sample.
        if 'nh_exit' not in shadow:
            _e = float(shadow['entry_price'])
            _s = 1.0 if shadow['side'] == 'BUY' else -1.0
            _pk = float(shadow.get('nh_peak', _e))
            _hi = float(bar_series['high']); _lo = float(bar_series['low'])
            _pk = max(_pk, _hi) if _s > 0 else min(_pk, _lo)
            shadow['nh_peak'] = _pk
            if _s * (_pk - _e) >= self.cfg.be_trigger:
                if _s > 0:
                    _stop = max(_e, _pk - self.cfg.trail_gap)
                    if _lo <= _stop:
                        shadow['nh_exit'] = round(_stop, 2)
                else:
                    _stop = min(_e, _pk + self.cfg.trail_gap)
                    if _hi >= _stop:
                        shadow['nh_exit'] = round(_stop, 2)

        # v2.7.1 TSTOP -- loser time-stop (grid-validated). At hold expiry, a leg
        # whose best favorable excursion never reached +$tstop_fav is a trapped
        # fake-out; close at market (~ -$5..-$12) instead of riding to the full SL.
        # One-shot by construction: max_fav is monotonic, so once fav >= threshold
        # at expiry this can never fire later.
        # D-31: with boost_spec_v2 ON the effective freeze is 0, so "hold expiry" no
        # longer exists; the loser time-stop instead fires at tstop_after_min (default
        # 45 = today's window, so today's timing is preserved). NEVER at t=0 (bound > 0
        # required; 0 disables). Flag OFF -> the freeze_minutes hold-expiry path, unchanged.
        _tstop_bound = (float(getattr(self.cfg, 'tstop_after_min', 45))
                        if bool(getattr(self.cfg, 'boost_spec_v2', False))
                        else float(self.cfg.freeze_minutes))
        if (getattr(self.cfg, 'tstop_fav', 0.0) > 0
                and _tstop_bound > 0
                and entry_time_for_pos is not None):
            try:
                _elapsed = (bar_time - entry_time_for_pos).total_seconds() / 60.0
            except Exception:
                _elapsed = None
            if _elapsed is not None and _elapsed >= _tstop_bound:
                _fav = (pos.max_fav - pos.entry_price) if shadow['side'] == 'BUY' \
                    else (pos.entry_price - pos.max_fav)
                if _fav < self.cfg.tstop_fav:
                    self.tele.warn(
                        f"\u23f1 TSTOP: {shadow['anchor_label']} {shadow['side']} "
                        f"never reached +${self.cfg.tstop_fav:.2f} fav in "
                        f"{_tstop_bound:.0f}m (peak +${max(_fav, 0):.2f}) -- "
                        f"closing at market instead of riding to SL."
                    )
                    shadow['tstop'] = True
                    try:
                        self.adapter.close_position(ticket, dry_run=self.paper)
                    except Exception as e:
                        log.warning(f"TSTOP close failed for {ticket}: {e}")
                    continue

        if not self.paper:
            # v2.5.7: read broker's ACTUAL sl and re-assert if it doesn't match
            # the bot's intended sl — EVERY bar, not only on advance. A silently
            # dropped/rejected modify (the A2 -990 bug) self-heals next bar
            # instead of leaving the original stop live for hours.
            intended = round(pos.current_sl, 2)

            # v2.5.8: clamp SL to the broker's minimum LEGAL distance from market.
            # This broker reports stops_level=0 but rejects stops within ~$0.20 of
            # price (INVALID_STOPS / 10013); probe confirmed $0.30 is safely accepted.
            # Pull the SL to the closest legal level rather than send an illegal value
            # that gets rejected (which left the OLD stop live — the A2 -990 bug).
            # NOTE: this only governs HOW CLOSE the stop may sit to market; the
            # $3 BE and $5->+4 locks (in update_position_on_bar) still guarantee
            # minimum locked profit regardless of this clamp.
            MIN_SL_DIST = 0.00
            try:
                ctk = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
                csi = self.adapter.mt5.symbol_info(self.cfg.symbol)
                if ctk is not None:
                    floor = MIN_SL_DIST
                    if csi is not None and csi.trade_stops_level > 0:
                        floor = max(floor, csi.trade_stops_level * csi.point)
                    # v3.3.0 STOP-THROUGH -> RE-PLACE, do NOT market-close (spec
                    # Part 2 #3 + #4). A computed stop at/above a long's bid (mirror
                    # for shorts) is INVALID: it reads as "through market" and the
                    # old code dumped at market (the A2 -5.38 slip on a trade that
                    # was merely in normal drawdown). The correct action is to KEEP
                    # the previous valid stop / re-derive one safely below price and
                    # re-arm it. Market-close is reserved for genuine SL/TP/kill/EOD.
                    _through = False
                    if shadow['side'] == 'BUY':
                        max_legal = round(ctk.bid - floor, 2)
                        _through = intended > max_legal
                    else:
                        min_legal = round(ctk.ask + floor, 2)
                        _through = intended < min_legal
                    if not _through:
                        # episode over -- the next through-event gets a fresh warning.
                        shadow.pop('_stopthru_episode_warned', None)
                    if _through:
                        # E-18 (2026-07-03): a losing leg with NO genuinely armed lock
                        # (lock_level_for == 0 -- either never armed, or parked at exact
                        # breakeven, the "no small locks" role-2.9 case) has nothing above
                        # its resting SL worth protecting. The pre-fix code chased the
                        # market with an "advancing" corrected stop every bar it stayed
                        # through -- 27x/26min on a 2026-07-03 trapped A1 SELL, $14
                        # underwater, computed stop 4123.10 vs bid ~4137. Root cause: the
                        # STOP-THROUGH re-arm didn't distinguish a real profit-lock (worth
                        # re-arming near market) from a phantom/breakeven one (worth
                        # nothing) -- it advanced BOTH. Fix: a no-armed-lock leg computes
                        # NO stop advance at all; the resting original SL is left exactly
                        # as-is (guard already correctly refuses to SEND the invalid
                        # value, this stops it being RECOMPUTED every bar).
                        _lock_now = 0
                        try:
                            _lock_now = lock_level_for(pos, self.cfg)
                        except Exception:
                            _lock_now = 0
                        if _lock_now == 0:
                            if not shadow.get('_stopthru_episode_warned'):
                                shadow['_stopthru_episode_warned'] = True
                                try:
                                    if getattr(self, 'ptrace', None) is not None:
                                        self.ptrace.stop_through_rearm(
                                            ticket, shadow['anchor_label'],
                                            side=shadow['side'], bid=ctk.bid,
                                            ask=ctk.ask,
                                            position_price=shadow.get('entry_price'),
                                            max_fav=shadow.get('max_fav'),
                                            rejected_stop=round(intended, 2),
                                            stop_price=round(old_sl, 2),
                                            reason="stop_through_no_armed_lock_no_advance")
                                except Exception:
                                    pass
                                self.tele.warn(
                                    f"⛔ STOP-THROUGH (no armed lock), NO advance | "
                                    f"{shadow['anchor_label']} {shadow['side']} | "
                                    f"kept stop ${old_sl:.2f} unchanged (computed "
                                    f"${intended:.2f} was through bid/ask) | further "
                                    f"repeats this episode are suppressed")
                            # NO advance: current_sl/shadow stay exactly as they were.
                            intended = round(old_sl, 2)
                            pos.current_sl = old_sl
                        else:
                            # A genuinely armed lock (tier 2/3) IS worth protecting --
                            # keep the PREVIOUS valid stop if it is still below market
                            # (long) / above market (short); else clamp to the closest
                            # legal level. Either way we re-arm a VALID stop -- never dump.
                            if shadow['side'] == 'BUY':
                                corrected = round(min(old_sl, max_legal), 2)
                            else:
                                corrected = round(max(old_sl, min_legal), 2)
                            if not shadow.get('_stopthru_episode_warned'):
                                shadow['_stopthru_episode_warned'] = True
                                try:
                                    if getattr(self, 'ptrace', None) is not None:
                                        self.ptrace.stop_through_rearm(
                                            ticket, shadow['anchor_label'],
                                            side=shadow['side'], bid=ctk.bid,
                                            ask=ctk.ask,
                                            position_price=shadow.get('entry_price'),
                                            max_fav=shadow.get('max_fav'),
                                            rejected_stop=round(intended, 2),
                                            stop_price=corrected,
                                            reason="stop_through_replaced_not_closed")
                                except Exception:
                                    pass
                                # Section D #4 re-arm alert -- throttled to once/episode
                                # (was rate-limited 1/60s, which still spammed a slow
                                # multi-minute through-episode; E-18 hardens it further).
                                self.tele.warn(
                                    f"⛔ STOP-THROUGH re-armed, NOT closed | "
                                    f"{shadow['anchor_label']} {shadow['side']} | kept "
                                    f"stop ${corrected:.2f} | rides on (computed "
                                    f"${intended:.2f} was through bid ${ctk.bid:.2f}) | "
                                    f"further repeats this episode are suppressed")
                            # Re-arm the corrected (valid) stop and continue managing
                            # this leg normally on the next bars.
                            pos.current_sl = corrected
                            shadow['current_sl'] = corrected
                            intended = corrected
            except Exception as e:
                log.warning(f"SL clamp check failed for {ticket}: {e}")

            try:
                bp = self.adapter.mt5.positions_get(ticket=ticket)
                broker_sl = float(bp[0].sl) if bp else None
            except Exception as e:
                broker_sl = None
                log.warning(f"Could not read broker SL for {ticket}: {e}")

            needs_assert = (broker_sl is None) or (abs(broker_sl - intended) > 0.05)
            if needs_assert:
                if pos.current_sl != old_sl:
                    log.info(
                        f"Trail advance ticket={ticket} side={shadow['side']} "
                        f"SL ${old_sl:.2f} → ${intended:.2f} (max_fav=${pos.max_fav:.2f})"
                    )
                    # Section D #5: 🔒 TRAIL LOCK ADVANCE alert (rate-limited 1/60s).
                    _sgn = 1.0 if shadow['side'] == 'BUY' else -1.0
                    _locked = _sgn * (intended - float(shadow['entry_price']))
                    if self._rl_ok(f"trail:{ticket}", 60.0):
                        self.tele.info(
                            f"🔒 TRAIL {ticket} stop ${old_sl:.2f}→${intended:.2f} "
                            f"| max_fav ${pos.max_fav:.2f} | locked ${_locked:+.2f}")
                else:
                    log.warning(
                        f"SL DRIFT ticket={ticket} side={shadow['side']}: broker "
                        f"${broker_sl} != intended ${intended} — re-asserting"
                    )
                # Hardening #5: modify_position_sl returns the MT5 result object
                # (truthy) on BOTH success and broker rejection -- treating it as
                # a boolean masked failed re-asserts. Confirm retcode == 10009
                # (DONE); otherwise the SL really didn't move and we must warn.
                # 2026-07-17: a rejected profit-lock modify must NEVER be abandoned
                # silently (base_lock rc=10016 with no retry let a +7.77 peak ride back
                # to -10 open). Retry once broker-min-adjusted; if still rejected AND the
                # lock would have fired, market-close it (LOCK_FALLBACK_CLOSE).
                _bid = _ask = None
                _floor = 0.30
                try:
                    _tk = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
                    _si = self.adapter.mt5.symbol_info(self.cfg.symbol)
                    if _tk is not None:
                        _bid, _ask = float(_tk.bid), float(_tk.ask)
                    if _si is not None and getattr(_si, 'trade_stops_level', 0) > 0:
                        _floor = max(_floor, _si.trade_stops_level * _si.point)
                except Exception:
                    pass
                ok = False
                try:
                    _plan = modify_sl_with_fallback(
                        lambda sl: self.adapter.modify_position_sl(ticket, sl),
                        lambda: self.adapter.close_position(ticket, dry_run=self.paper),
                        shadow['side'], intended,
                        _bid if _bid is not None else intended,
                        _ask if _ask is not None else intended, _floor)
                    ok = _plan['ok']
                    if _plan['outcome'] == 'RETRY_OK':
                        pos.current_sl = _plan['sl']
                        shadow['current_sl'] = _plan['sl']
                        log.info(f"SL modify RETRY OK ticket={ticket} {shadow['side']} "
                                 f"broker-min adjusted ${intended}→${_plan['sl']} (rc={_plan['rc']})")
                    elif _plan['outcome'] == 'FALLBACK_CLOSE':
                        shadow['lock_fallback_close'] = True
                        _msg = (f"🔒 *LOCK_FALLBACK_CLOSE* {shadow['anchor_label']} "
                                f"{shadow['side']} @ market — lock `${intended}` rejected "
                                f"(rc={_plan['rc']}) twice AND price through it "
                                f"(bid ${_bid} / ask ${_ask}). Realized rather than abandoned.")
                        log.warning(_msg)
                        try:
                            import discord_cards as _dc
                            self.tele.send(_msg, Severity.WARN,
                                           card=_dc.card_lock_fallback_close(
                                               shadow['anchor_label'], shadow['side'],
                                               intended, _bid, _ask, _plan['rc']))
                        except Exception:
                            self.tele.warn(_msg)
                except Exception as e:
                    log.warning(f"modify_position_sl / fallback raised for {ticket}: {e}")
                if not ok:
                    self.tele.warn(
                        f"⚠️ *SL modify FAILED* ticket={ticket} {shadow['side']}\n"
                        f"Intended `${intended}`, broker still `${broker_sl}`.\n"
                        f"Trade is on its PREVIOUS stop. Re-attempting next bar."
                    )
        else:
            if pos.current_sl != old_sl:
                log.info(
                    f"[PAPER] Trail advance ticket={ticket} SL ${old_sl:.2f} → "
                    f"${pos.current_sl:.2f} (max_fav=${pos.max_fav:.2f})"
                )
        # v2.5.5: persist whenever SL moved OR max_fav advanced. Without this,
        # max_fav/current_sl live only in RAM between saves; a Windows sleep or
        # crash mid-trade would restore a STALE max_fav (often == entry) and the
        # trail would "forget" the peak it had already reached. Saving here makes
        # the trail restart-safe — the dominant cause of "trail doesn't work right".
        if pos.current_sl != old_sl or pos.max_fav != old_max_fav:
            self._save_state()
