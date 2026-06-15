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

from telemetry import telemetry_from_env, Severity, md_escape
from mt5_adapter import _MT5_RETCODE_MAP

log = logging.getLogger("AUREON")


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
            self.tele.info(
                f"🎯 FILL: *{info['anchor_label']}* {info['side']} "
                f"@ ${info['entry_price']:.2f} (ticket {ticket})"
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
                is_rescue = _twin_open
                if _twin_open and not _flag_hint:
                    self.tele.warn(
                        f"⚠️ rescue flag was MISSING for {info['anchor_label']} "
                        f"{info['side']} -- recovered structurally (twin still "
                        f"open). Check log for flag-loss cause.")
                elif _flag_hint and not _twin_open:
                    self.tele.warn(
                        f"ℹ️ stale rescue flag IGNORED for {info['anchor_label']} "
                        f"{info['side']} -- twin already closed; running as a "
                        f"normal breakout leg (no boosts).")
            self.shadow_positions[ticket] = {
                'anchor_label': info['anchor_label'],
                'side':         info['side'],
                'entry_price':  float(broker_p.price_open),
                'current_sl':   float(broker_p.sl),
                'tp_level':     float(broker_p.tp),
                'max_fav':      float(broker_p.price_open),
                'fill_time':    fill_time_utc.isoformat(),  # v2.3: persisted, restart-safe
                'role':         'rescue' if is_rescue else 'normal',  # v2.9 / v2.9.8 structural
            }
            if is_rescue:
                self.tele.info(f"\U0001F691 RESCUE leg active (ticket {ticket}): no early locks, "
                               f"free to run until +$10 covers the twin's loss.")
                # v2.9.5 SL-RESCUE BOOST (Hithesh): at this exact moment the
                # first leg is -$10; open extra trades in the rescue
                # direction with a tight $6 SL each, so the remaining $8 to
                # the first leg's SL is harvested (~+$560 @ 2x0.35).
                if getattr(self.cfg, 'rescue_boost_enabled', False):
                    b_side = info['side']
                    b_n = int(getattr(self.cfg, 'rescue_boost_count', 2))
                    b_sld = float(getattr(self.cfg, 'rescue_boost_sl', 6.0))
                    b_ep = float(info['entry_price'])
                    sgn = 1.0 if b_side == 'BUY' else -1.0
                    b_sl = round(b_ep - sgn * b_sld, 2)
                    b_tp = round(b_ep + sgn * self.cfg.tp_dist, 2)
                    self.tele.warn(
                        f"\u26A1 SL-RESCUE BOOST: opening {b_n}x{self.cfg.lot_size} "
                        f"{b_side} @ market | SL ${b_sl} (tight ${b_sld:.0f}) | TP ${b_tp}\n"
                        f"Goal: +${b_n * 8 * self.cfg.lot_size * 100:.0f} covers the twin "
                        f"if its SL hits; capped -${b_n * b_sld * self.cfg.lot_size * 100:.0f} on whipsaw."
                    )
                    for bi in range(b_n):
                        # v3.0.0 Fix B (boost-fill diagnostics): boosts are
                        # 0-for-6 lifetime -- announced (⚡) but NO success/
                        # reject follow-up ever appears and no boost shows in
                        # the broker; the failure bypasses all current logging.
                        # We do NOT yet know the cause, so instrument EVERY
                        # possible exit of this path: no boost is announced
                        # without a subsequent Telegram line naming its fate.
                        self.tele.info(
                            f"… attempting BOOST{bi+1} {b_side} {self.cfg.lot_size} "
                            f"@ market | SL ${b_sl} TP ${b_tp}")
                        try:
                            b_res = self.adapter.place_market_order(
                                self.cfg.symbol, b_side, self.cfg.lot_size,
                                sl=b_sl, tp=b_tp,
                                comment=f"AUR_{info['anchor_label'][:2]}_{b_side[0]}_B{bi+1}",
                                dry_run=self.paper)
                        except Exception as e:
                            log.warning(f"BOOST{bi+1} order error: {e!r}")
                            # v2.9.8: Jun-11 A4 mystery -- exceptions here were
                            # log-only, so Telegram showed the announce and then
                            # NOTHING. Every boost now reports fate to Telegram.
                            self.tele.error(f"❌ BOOST{bi+1} EXCEPTION: {md_escape(repr(e))} -- order NOT placed")
                            continue
                        if b_res is None:
                            # v3.0.0: broker returned no result object at all --
                            # surface mt5.last_error() so the next event tells us why.
                            _le = ''
                            try:
                                _le = f" last_error={md_escape(self.adapter.mt5.last_error())}"
                            except Exception:
                                pass
                            self.tele.error(
                                f"❌ BOOST{bi+1} result=None -- order NOT placed{_le}")
                            continue
                        b_rc = getattr(b_res, 'retcode', None)
                        b_rc_name = _MT5_RETCODE_MAP.get(b_rc, f"UNKNOWN_{b_rc}")
                        b_cmt = getattr(b_res, 'comment', '') or ''
                        if b_rc == 10009:
                            b_tk = getattr(b_res, 'order', None) or getattr(b_res, 'deal', None)
                            b_fp = float(getattr(b_res, 'price', b_ep) or b_ep)
                            if b_tk:
                                self.shadow_positions[int(b_tk)] = {
                                    'anchor_label': info['anchor_label'],
                                    'side':         b_side,
                                    'entry_price':  b_fp,
                                    'current_sl':   b_sl,
                                    'tp_level':     b_tp,
                                    'max_fav':      b_fp,
                                    'fill_time':    pd.Timestamp.now(tz='UTC').isoformat(),
                                    'role':         'rescue',
                                    'boost':        True,
                                }
                            self.tele.success(
                                f"\u2705\u26A1 BOOST{bi+1} {b_side} FILLED @ ${b_fp} "
                                f"(ticket {b_tk}) rc={b_rc} ({b_rc_name})")
                        else:
                            # v3.0.0: non-success retcode -- name it + show comment
                            self.tele.error(
                                f"\u274C BOOST{bi+1} rejected rc={b_rc} ({md_escape(b_rc_name)}) "
                                f"comment={md_escape(repr(b_cmt))}")

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
                self.state['daily_pnl'] += pnl_usd
                close_price = float(close_deal.price)
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
                # elapsed should be impossible. Exits AT entry (+/- $0.40) are the
                # +$3 BASE LOCK firing, which IS allowed during freeze -- excluded.
                if (hold_min is not None and outcome == 'Trail'
                        and self.cfg.freeze_minutes > 0
                        and hold_min < self.cfg.freeze_minutes - 0.5
                        and abs(close_price - float(shadow['entry_price'])) > 0.40):
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
                self.tele.send(
                    f"📤 CLOSE: *{shadow['anchor_label']}* {shadow['side']} "
                    f"`{outcome}`{slip_txt} @ ${close_price:.2f}\n"
                    f"P&L: `${pnl_usd:+.2f}`  |  Daily total: `${self.state['daily_pnl']:+.2f}`{hold_txt}{nh_txt}",
                    sev
                )
                # Append to today's trade log
                with open(self.daylog_path, "a", newline="") as f:
                    csv.writer(f).writerow([
                        self.state['last_broker_date'],
                        shadow['anchor_label'], shadow['side'],
                        shadow['entry_price'], close_price,
                        outcome, round(pnl_usd, 2), ticket,
                    ])
                # v2.5.6: rich journal row (one per fill) for strategy evaluation
                try:
                    self._write_journal(shadow, close_deal, close_price, outcome, pnl_usd, ticket)
                except Exception as je:
                    log.warning(f"journal write failed for {ticket}: {je}")
                self._save_state()
        except Exception as e:
            self.tele.warn(f"Could not fetch close deal for {ticket}: {e}")
