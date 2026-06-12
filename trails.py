"""
AUREON — per-bar trail management (TrailsMixin).

_manage_trails_on_bar_close: feed the last closed M1 bar through the pure
update_position_on_bar (strategy.py); apply the ladder/trail; TSTOP loser
time-stop; SL assert / drift-heal every bar; the v2.9.8 STOP-THROUGH market
close; and the journal-only no-hold shadow tracker. Behavior FROZEN at v2.9.8.

Method extracted verbatim from live_trader.py (v3.0.0 refactor); FULLY
byte-identical. Its late `from bot import update_position_on_bar` is unchanged --
bot.py re-exports update_position_on_bar from strategy.py, so the line still
resolves to the same pure function.
"""

import logging

import pandas as pd

log = logging.getLogger("AUREON")


class TrailsMixin:
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

        from bot import update_position_on_bar  # late import

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
            )
            old_max_fav = shadow.get('max_fav')
            update_position_on_bar(pos, bar_series, bar_time, self.cfg)
            shadow['current_sl'] = pos.current_sl
            shadow['max_fav'] = pos.max_fav

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
                        # v2.9.8 STOP-THROUGH: if the intended (ladder) stop is
                        # already at/through market, the level was breached intrabar.
                        # Pinning SL to bid/ask (old clamp) is rejection-prone and
                        # fills at noise (Jun-12 A1 BUY: BE pinned -> -$2.20 'Trail').
                        # Close at market and name the rule; one-way ratchet means
                        # this is always the honest outcome.
                        _through = False
                        if shadow['side'] == 'BUY':
                            max_legal = round(ctk.bid - floor, 2)
                            _through = intended > max_legal
                        else:
                            min_legal = round(ctk.ask + floor, 2)
                            _through = intended < min_legal
                        if _through:
                            self.tele.warn(
                                f"⚡ STOP-THROUGH: {shadow['anchor_label']} "
                                f"{shadow['side']} intended stop ${intended:.2f} is "
                                f"through market (bid ${ctk.bid:.2f}/ask ${ctk.ask:.2f}) "
                                f"-- closing at market.")
                            try:
                                self.adapter.close_position(ticket, dry_run=self.paper)
                            except Exception as e:
                                log.warning(f"stop-through close failed {ticket}: {e}")
                            continue
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
                    else:
                        log.warning(
                            f"SL DRIFT ticket={ticket} side={shadow['side']}: broker "
                            f"${broker_sl} != intended ${intended} — re-asserting"
                        )
                    ok = False
                    try:
                        ok = self.adapter.modify_position_sl(ticket, intended)
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
