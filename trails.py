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

    from strategy import update_position_on_bar  # late import

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
        if (getattr(self.cfg, 'tstop_fav', 0.0) > 0
                and self.cfg.freeze_minutes > 0
                and entry_time_for_pos is not None):
            try:
                _elapsed = (bar_time - entry_time_for_pos).total_seconds() / 60.0
            except Exception:
                _elapsed = None
            if _elapsed is not None and _elapsed >= self.cfg.freeze_minutes:
                _fav = (pos.max_fav - pos.entry_price) if shadow['side'] == 'BUY' \
                    else (pos.entry_price - pos.max_fav)
                if _fav < self.cfg.tstop_fav:
                    self.tele.warn(
                        f"\u23f1 TSTOP: {shadow['anchor_label']} {shadow['side']} "
                        f"never reached +${self.cfg.tstop_fav:.2f} fav in "
                        f"{self.cfg.freeze_minutes}m (peak +${max(_fav, 0):.2f}) -- "
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
                    if _through:
                        # Keep the PREVIOUS valid stop if it is still below market
                        # (long) / above market (short); else clamp to the closest
                        # legal level. Either way we re-arm a VALID stop -- never dump.
                        if shadow['side'] == 'BUY':
                            corrected = round(min(old_sl, max_legal), 2)
                        else:
                            corrected = round(max(old_sl, min_legal), 2)
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
                        # Section D #4: ⛔ re-arm alert, rate-limited to 1/60s/ticket.
                        if self._rl_ok(f"stopthru:{ticket}", 60.0):
                            self.tele.warn(
                                f"⛔ STOP-THROUGH re-armed, NOT closed | "
                                f"{shadow['anchor_label']} {shadow['side']} | kept "
                                f"stop ${corrected:.2f} | rides on (computed "
                                f"${intended:.2f} was through bid ${ctk.bid:.2f})")
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
                ok = False
                try:
                    _res = self.adapter.modify_position_sl(ticket, intended)
                    ok = (_res is not None and (
                        getattr(_res, 'retcode', None) == 10009
                        or (isinstance(_res, dict) and _res.get('paper'))))
                except Exception as e:
                    log.warning(f"modify_position_sl raised for {ticket}: {e}")
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
