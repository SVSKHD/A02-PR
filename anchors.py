"""AUREON — anchor schedule, defer/retry, gap-mode + in-flight recovery, placement.

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

from telemetry import telemetry_from_env, Severity, anchor_time_block
from mt5_adapter import _MT5_RETCODE_MAP, mt5_comment

log = logging.getLogger("AUREON")


def resolved_anchor_hm(label, broker_date, hour, minute, cfg):
    """Pure (config-only) Monday-A1 cushion resolver — the SINGLE source of truth
    for A1 time resolution, shared by the live anchor loop AND the backtester so
    backtest == live for scheduling. See _resolved_anchor_hm for the rationale."""
    ovr = getattr(cfg, 'monday_a1_override', None)
    if ovr is not None and label.startswith('A1'):
        force = os.environ.get('AUREON_TEST_FORCE_MONDAY_A1', '').strip().lower() \
            in ('1', 'true', 'yes', 'on')
        if force or broker_date.weekday() == 0:
            return int(ovr[0]), int(ovr[1])
    return hour, minute


def _resolved_anchor_hm(self, label, broker_date, hour, minute):
    """Resolve an anchor's (broker_hour, broker_minute), applying the Monday-only
    A1 cold-start cushion. Forex opens Mon 00:00 broker; A1 at 02:30 is only 2.5h
    after week-open (Monday offset re-detect + thin M5 history -> 'no bars' risk),
    so on Mondays A1 fires later (03:30 broker = 6 AM IST) instead. Uses the BROKER
    date's weekday so the Monday test is correct relative to A1's own broker day.
    Other anchors, and A1 on Tue-Fri, are unchanged; cfg.monday_a1_override=None
    disables it. The A1 label is NOT changed (journal/Firebase/aggregation keys
    stay stable). This is the SINGLE source of truth for A1 time resolution --
    both _process_anchor_if_due and the readiness line call it.

    Test hook: AUREON_TEST_FORCE_MONDAY_A1=1 forces the override on ANY weekday so
    the 03:30 resolution can be verified mid-week (combine with the quiet-feed /
    offset-revalidate test toggles to reproduce the full Monday scenario). TEST
    ONLY, defaults OFF, surfaced in the 'TEST MODE ACTIVE' banner line.

    v3.1.8: delegates to the pure module-level resolved_anchor_hm so the backtester
    can reuse the EXACT same resolution without a LiveTrader instance."""
    return resolved_anchor_hm(label, broker_date, hour, minute, self.cfg)

def _anchor_sched_utc(self, label):
    """Resolve the scheduled UTC instant for an anchor label on the current broker
    date (Monday A1 shift applied). Used to print scheduled vs actual times on
    fill/close messages. Best-effort; None if the label isn't found / on error."""
    try:
        bdate = self._broker_date(pd.Timestamp.now(tz='UTC'))
        for lbl, h, m in self.cfg.anchors:
            if lbl == label:
                rh, rm = self._resolved_anchor_hm(lbl, bdate, h, m)
                return self._anchor_datetime_utc(
                    bdate, rh, self.cfg.broker_tz_offset_hours, rm)
    except Exception:
        return None
    return None

def _mark_anchor_placed(self, label):
    """Record an anchor as PLACED (its single fire for the day). Gates all further
    attempts -- guarantees one placement per anchor per day even with late-retry."""
    if label not in self.state['processed_anchors_today']:
        self.state['processed_anchors_today'].append(label)
        self._save_state()

def _anchor_missed(self, label, anchor_utc, utc_now):
    """v3.0.5: the late window elapsed with no successful placement -> give up
    cleanly and LOUDLY. This is the alert that ends the silent misses. Best-effort
    reason from current conditions; fires once per anchor per day."""
    missed = self.state.setdefault('missed_anchors_today', [])
    if label in missed:
        return
    # Best-effort reason (we don't gate on cause; this is just diagnostics).
    reason = "unknown"
    try:
        if not self.paper and not getattr(self, "offset_validated", False):
            reason = "broker time offset never validated"
        else:
            tk = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
            if tk is None or getattr(tk, "time", 0) <= 0:
                reason = "no live tick from broker"
            else:
                off = (getattr(self.adapter, 'tick_time_offset_hours', 0) or 0) * 3600
                age = abs(pd.Timestamp.now(tz='UTC').timestamp() - (tk.time - off))
                if age > getattr(self.cfg, 'stale_tick_threshold_s', 60.0):
                    reason = f"feed stale ({age:.0f}s) through the whole window"
    except Exception:
        pass
    waited_min = max(0, (utc_now - anchor_utc).total_seconds() / 60.0)
    self.tele.error(
        f"❌ *ANCHOR MISSED — {label}*\n"
        f"{anchor_time_block(anchor_utc, utc_now)}\n"
        f"  reason: {reason}\n"
        f"  waited `{waited_min:.0f}m` (late window "
        f"{getattr(self.cfg, 'anchor_late_window_min', 10)}m) — no placement, moving on."
    )
    log.warning(f"{label}: ANCHOR MISSED after {waited_min:.0f}m — reason: {reason}")
    missed.append(label)
    self._save_state()

def _process_anchor_if_due(self, broker_date: DateType, utc_now: pd.Timestamp):
    if self.paused:
        return
    # v3.0.5: bounded LATE-PLACEMENT window. An anchor stays eligible for
    # re-attempt for cfg.anchor_late_window_min after its scheduled time; the
    # original behavior is window_s=120 (anchor_late_window_min=0). Hard stops
    # (kill switch / EOD / weekend / paused) are enforced by the caller BEFORE
    # this runs, so an unplaced anchor is never late-placed through them.
    late_window_min = getattr(self.cfg, 'anchor_late_window_min', 0)
    window_s = max(120.0, late_window_min * 60.0)
    retry_interval = getattr(self, 'ANCHOR_LATE_RETRY_INTERVAL_S', 30)
    grace = getattr(self, 'ANCHOR_ONTIME_GRACE_S', 120)
    for label, hour, minute in self.cfg.anchors:
        # processed_anchors_today is the PLACED set: a placed anchor never re-fires
        # (one placement per anchor per day).
        if label in self.state['processed_anchors_today']:
            continue
        if label in self.state.get('missed_anchors_today', []):
            continue
        # Monday-only A1 shift (cold-start cushion) resolved off the broker date.
        r_hour, r_minute = self._resolved_anchor_hm(label, broker_date, hour, minute)
        anchor_utc = self._anchor_datetime_utc(
            broker_date, r_hour, self.cfg.broker_tz_offset_hours, r_minute)
        delta = (utc_now - anchor_utc).total_seconds()
        if delta < 0:
            continue
        if delta < window_s:
            # On-time OR inside the late window: (re-)attempt placement.
            # Skip if an attempt is already in flight (single deferred slot) so we
            # never double-place; throttle re-attempts to the stale-retry cadence
            # so a persistently-failing cause can't spam Telegram every tick.
            if self._deferred_anchor is not None:
                continue
            last = self._last_anchor_attempt.get(label)
            if last is not None and (utc_now - last).total_seconds() < retry_interval:
                continue
            self._last_anchor_attempt[label] = utc_now
            if delta >= grace:
                log.warning(
                    f"{label}: LATE re-attempt at +{delta/60.0:.1f}m "
                    f"(window {window_s/60.0:.0f}m) — placement not yet completed")
            self._process_anchor(label, anchor_utc)
            self._save_state()
        else:
            # Late window elapsed with no successful placement -> clean MISS.
            self._anchor_missed(label, anchor_utc, utc_now)

def _process_anchor(self, label: str, anchor_utc: pd.Timestamp):
    # v2.5: account floor check — halt new entries if balance dropped too far
    try:
        ainfo = self.adapter.mt5.account_info()
        if ainfo is not None:
            floor = self.cfg.starting_balance * self.cfg.account_floor_pct
            if ainfo.balance < floor:
                self.tele.warn(
                    f"⛔ *{label} BLOCKED — account floor breached*\n"
                    f"Balance: `${ainfo.balance:,.2f}`\n"
                    f"Floor:   `${floor:,.2f}` ({self.cfg.account_floor_pct*100:.0f}% of starting)\n"
                    f"No new entries until balance recovers."
                )
                return
    except Exception as e:
        log.warning(f"Account floor check failed: {e}")

    # Guard 2 (Monday-wake hardening): never place on an UNVALIDATED broker time
    # offset. A wrong offset queries the wrong M5 window -> "no bars" -> the Jun-8
    # silent A1 miss. Block + alert instead of querying blind (live only; paper/
    # backtest run unguarded). The same offset feeds every anchor, so gating all
    # of them fails CLOSED if the wake validation never passed.
    if not self.paper and not getattr(self, "offset_validated", False):
        self.tele.warn(
            f"⚠️ *{label} skipped — offset not validated*\n"
            f"Broker time offset has not passed wake validation; refusing to "
            f"place on an unvalidated offset (Jun-8 silent-miss guard)."
        )
        log.warning(f"{label}: SKIP — offset_validated is False")
        return

    # Guard 2: retry the anchor M5 fetch before giving up, and NEVER swallow a
    # no-bars result silently (the literal Jun-8 symptom). Loud alert on final
    # failure so a no-bars anchor can never pass unnoticed.
    _fetch_retries = getattr(self, "ANCHOR_FETCH_RETRIES", 3)
    anchor_price = None
    for _attempt in range(1, _fetch_retries + 1):
        anchor_price = self.adapter.get_m5_close(self.cfg.symbol, anchor_utc)
        if anchor_price is not None:
            break
        log.warning(
            f"{label}: get_m5_close returned no bars "
            f"(attempt {_attempt}/{_fetch_retries}) at {anchor_utc}")
        if _attempt < _fetch_retries:
            time.sleep(getattr(self, "ANCHOR_FETCH_RETRY_WAIT_S", 2))
    if anchor_price is None:
        _tr = getattr(self, 'ptrace', None)
        if _tr is not None:
            _tr.a1_bar_missing(label, attempts=_fetch_retries,
                               offset=getattr(self.adapter, 'tick_time_offset_hours', None),
                               anchor_utc=str(anchor_utc))
        # v3.2.5 Feature 1: at the Monday/post-weekend open the M5 bar can lag while
        # ticks are live. A1 (and only A1, only here on the open path) falls back to
        # a SANE, SETTLED live tick so we never lose another Monday A1. A2/A3/A4 and
        # A1-with-a-bar are unaffected (they never reach this branch with no bar).
        if (label.startswith('A1')
                and bool(getattr(self.cfg, 'a1_tick_fallback_enabled', True))
                and not self.paper):
            anchor_price = self._capture_a1_anchor_from_tick(label, anchor_utc)
        if anchor_price is None:
            self.tele.warn(
                f"⚠️ *{label} anchor fetch returned no bars — investigate*\n"
                f"get_m5_close found no M5 bar ending {anchor_utc} after "
                f"{_fetch_retries} attempts "
                f"(offset {getattr(self.adapter, 'tick_time_offset_hours', None)}h). "
                f"NOT placing — no silent miss."
            )
            log.warning(f"⚠️ {label}: anchor fetch returned no bars after retries — skipping")
            return

    # v2.5.2: Per-anchor deferred wait. A2 (London open) and A4 (NY open) need
    # longer than calm sessions for broker comm to stabilize past the volume spike.
    # 2026-05-27 incident: A2 hit rc=-1 with 15s wait on both Pepperstone and
    # MetaQuotes. Bumping A2/A4 to 30s + retry mechanism in _place_orders_for_anchor
    # gives up to 75s total recovery window per anchor.
    defer_seconds = self.DEFER_WAIT_BY_ANCHOR.get(label, self.DEFER_WAIT_DEFAULT)
    defer_until = pd.Timestamp.now(tz='UTC') + pd.Timedelta(seconds=defer_seconds)
    self._deferred_anchor = {
        'label': label,
        'anchor_utc': anchor_utc,
        'anchor_price': anchor_price,
        'defer_until': defer_until,
        'retry_count': 0,                # v2.5.2: retry counter for rc=-1 recovery
        # v2.5.3: gap-mode state preserved across retries (None on first attempt)
        'gap_mode_locked':  False,
        'gap_lot_override': None,
        'gap_sl_override':  None,
        'gap_re_anchor':    None,
    }
    log.info(
        f"{label}: anchor captured @ ${anchor_price:.2f}, deferring placement to "
        f"{defer_until.strftime('%H:%M:%S')} UTC ({defer_seconds}s settle wait — non-blocking)"
    )

def _capture_a1_anchor_from_tick(self, label, anchor_utc):
    """v3.2.5 Feature 1: the M5 bar is still missing at the open -> capture the A1
    anchor from a SANE, SETTLED live tick instead of losing the anchor. Reads up to
    a1_tick_fallback_samples ticks (tick_refresh_s apart), drops stale/garbage ones,
    and hands the run to the SHARED tick_hold.settle_anchor_tick, which requires
    hold_ticks of settled ticks -- so the wild first reopen spike is rejected and the
    anchor is taken only once the feed has settled. Returns the anchor price or None.
    Heartbeat kept alive throughout. Live-only; pure decision lives in tick_hold."""
    import tick_hold as _th
    n_samples = int(getattr(self.cfg, 'a1_tick_fallback_samples', 6))
    poll = float(getattr(self.cfg, 'tick_refresh_s', 0.3))
    thr = float(getattr(self.cfg, 'stale_tick_threshold_s', 60.0))
    prices = []
    for _ in range(max(n_samples, _th.hold_ticks(self.cfg))):
        try:
            tk = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
        except Exception:
            tk = None
        if tk is not None:
            off = (getattr(self.adapter, 'tick_time_offset_hours', 0) or 0) * 3600
            age = abs(pd.Timestamp.now(tz='UTC').timestamp() - (tk.time - off))
            if age <= thr:
                prices.append((float(tk.bid) + float(tk.ask)) / 2.0)
        self._touch_heartbeat()
        time.sleep(poll)
    ok, price, held, reason = _th.settle_anchor_tick(prices, self.cfg)
    tr = getattr(self, 'ptrace', None)
    if tr is not None:
        tr.a1_tick_fallback(label, ok=ok, tick_price=price, held_ticks=held,
                            reason=reason, samples=len(prices), source='tick')
    if not ok:
        log.warning(f"{label}: tick-fallback could not settle a sane tick "
                    f"({reason}, {len(prices)} samples) — not placing from tick")
        return None
    buy_stop = round(price + self.cfg.trigger_dist, 2)
    sell_stop = round(price - self.cfg.trigger_dist, 2)
    if tr is not None:
        tr.a1_placed_from_tick(label, anchor_price=price, buy_stop=buy_stop,
                               sell_stop=sell_stop, held_ticks=held, source='tick')
    log.info(f"{label}: A1 anchor from TICK ${price:.2f} (held {held} ticks, bar lagging)")
    self.tele.success(
        f"🟢 *A1 placed from TICK* (bar lagging at open) | anchor ${price:.2f} "
        f"(held {held} ticks) — buy ${buy_stop:.2f} / sell ${sell_stop:.2f}")
    return price

def _await_fresh_tick_for_placement(self, label):
    """Fix 1 (2026-06-15 missed-anchor incident): at placement, a tick older than
    cfg.stale_tick_threshold_s is usually a transient MT5/broker blip, not a
    reason to lose the whole anchor (a 76s tick skipped two anchors and a clean
    ~$25 gold move today). Poll every stale_retry_poll_s for up to
    stale_retry_window_s and return (tick, current_price, waited_s) as soon as a
    fresh tick appears; return None if it stays stale the whole window, or a kill
    switch / pause / EOD intervenes (those take priority). The retry only confirms
    the feed is live enough to place -- the anchor price is taken at placement by
    the caller (deployed v2.5.4 current-price anchoring; see REFACTOR_NOTES for the
    spec's fixed-anchor-price request vs the deployed behavior). Heartbeat is kept
    alive throughout so the watchdog never kills the bot mid-wait. ONE Telegram
    line on entry, not per poll."""
    thr    = getattr(self.cfg, 'stale_tick_threshold_s', 60.0)
    window = getattr(self.cfg, 'stale_retry_window_s', 90.0)
    poll   = getattr(self.cfg, 'stale_retry_poll_s', 5.0)

    def _read():
        tk = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
        if tk is None:
            return None, None
        off = (self.adapter.tick_time_offset_hours or 0) * 3600
        age = abs(pd.Timestamp.now(tz='UTC').timestamp() - (tk.time - off))
        return tk, age

    tk, age = _read()
    if tk is not None and age is not None and age <= thr:
        return tk, (tk.ask + tk.bid) / 2, 0.0   # already fresh -> no wait

    self.tele.warn(
        f"⏳ *{label} waiting for fresh tick* — last tick "
        f"{('%.0fs old' % age) if age is not None else 'unavailable'} "
        f"(> {thr:.0f}s). Polling up to {window:.0f}s before skipping; anchor "
        f"held, placing late off the same anchor is correct."
    )
    start = time.monotonic()
    reconnected = False
    while (time.monotonic() - start) < window:
        self._touch_heartbeat()  # this loop blocks the tick loop -- stay alive
        # Priority: kill switch / pause / EOD abort the wait immediately.
        if self.state.get('kill_switch_locked', False) or self.paused:
            log.warning(f"{label}: stale-tick wait aborted (kill_switch/paused).")
            return None
        try:
            now_utc = pd.Timestamp.now(tz='UTC')
            if self._eod_reached(self._broker_date(now_utc), now_utc):
                log.warning(f"{label}: stale-tick wait aborted (EOD reached).")
                return None
        except Exception:
            pass
        # Mid-window, cycle the connection once in case the terminal truly dropped.
        if not reconnected and (time.monotonic() - start) >= (window / 2.0):
            reconnected = True
            try:
                self._attempt_mt5_reconnect(label)
            except Exception as e:
                log.warning(f"{label}: mid-wait reconnect raised: {e}")
        time.sleep(poll)
        tk, age = _read()
        if tk is not None and age is not None and age <= thr:
            waited = time.monotonic() - start
            log.info(f"{label}: fresh tick after {waited:.0f}s stale-tick wait (age {age:.0f}s).")
            self.tele.success(
                f"✅ *{label} placed after {waited:.0f}s stale-tick wait* — feed live again.")
            return tk, (tk.ask + tk.bid) / 2, waited
    return None

def _complete_deferred_anchor(self):
    """v2.5: Called from the tick loop. Completes a deferred anchor placement
    after the settle window. Non-blocking — doesn't stop position management.

    v2.5.2: Plumbs retry_count through to placement so rc=-1 retries
    re-enter via this same path without losing retry state."""
    if self._deferred_anchor is None:
        return
    if pd.Timestamp.now(tz='UTC') < self._deferred_anchor['defer_until']:
        return  # still waiting

    d = self._deferred_anchor
    self._deferred_anchor = None  # consume

    label = d['label']
    anchor_price = d['anchor_price']
    anchor_utc = d['anchor_utc']
    retry_count = d.get('retry_count', 0)   # v2.5.2: pull retry counter
    # v2.5.3: pull preserved gap-mode context
    gap_mode_locked  = d.get('gap_mode_locked',  False)
    gap_lot_override = d.get('gap_lot_override', None)
    gap_sl_override  = d.get('gap_sl_override',  None)
    gap_re_anchor    = d.get('gap_re_anchor',    None)
    if gap_mode_locked and gap_re_anchor is not None:
        anchor_price = gap_re_anchor

    # v2.5: tick freshness check — refuse to use stale market data.
    # Fix 1 (2026-06-15): a transient stale tick (e.g. a 76s blip) must NOT cost
    # the whole anchor. Poll for a fresh tick up to stale_retry_window_s and place
    # as soon as the feed is live again; skip ONLY if it stays stale the whole
    # window. Anchor price is still taken at placement (v2.5.4 current-price
    # anchoring, unchanged) -- see REFACTOR_NOTES for the spec's fixed-anchor-price
    # request vs the deployed behavior.
    current_price = None
    try:
        fresh = self._await_fresh_tick_for_placement(label)
        if fresh is None:
            window = getattr(self.cfg, 'stale_retry_window_s', 90.0)
            thr = getattr(self.cfg, 'stale_tick_threshold_s', 60.0)
            self._dump_mt5_state(label, f"SKIP: tick stale through {window:.0f}s retry window")
            self.tele.warn(
                f"⚠️ *{label} skipped — stale tick* after {window:.0f}s of retries\n"
                f"Tick stayed older than {thr:.0f}s the whole window "
                f"(MT5/broker connection). Anchor lost this cycle."
            )
            return
        tick, current_price, _waited = fresh
        # v2.5.4: ANCHOR ON CURRENT PRICE at the moment of placement (unchanged).
        anchor_price = current_price
    except Exception as e:
        log.warning(f"Could not read fresh tick for {label}: {e}")
        self._dump_mt5_state(label, f"SKIP: tick read raised {e}")
        self.tele.warn(f"⚠️ {label}: tick read failed — skipping")
        return

    # v2.5.4: HARD GUARANTEE of current-price anchoring. If the tick came back
    # None (no exception, just unavailable), current_price is still None and
    # anchor_price would otherwise be the stale M5 close. Refuse to place on it —
    # skip cleanly instead of silently repeating the stale-anchor blunder.
    if current_price is None:
        self._dump_mt5_state(label, "SKIP: no live tick — refusing stale M5 anchor")
        self.tele.warn(
            f"⚠️ *{label} skipped — no live tick for current-price anchor*\n"
            f"symbol_info_tick returned None. Refusing to place on the stale "
            f"M5 anchor. Anchor lost this cycle (no blunder)."
        )
        log.warning(f"{label}: SKIP — current_price None, refusing stale anchor placement")
        return

    # v2.5.3: WARM UP THE TRADE CHANNEL before real placement. If warmup
    # fails AND reconnect also fails, skip cleanly with diagnostic dump.
    if not self._warmup_trade_channel(label):
        self._dump_mt5_state(label, "SKIP: warmup + reconnect both failed")
        self.tele.error(
            f"❌ *{label} skipped — trade channel could not be revived*\n"
            f"Warmup ping returned None and mt5.shutdown()/initialize() also failed.\n"
            f"This anchor is lost. See log for full mt5 state dump."
        )
        return

    # v2.5.2/v2.5.3: pass retry_count and gap state through
    self._place_orders_for_anchor(
        label, anchor_utc, anchor_price, current_price, retry_count,
        gap_mode_locked=gap_mode_locked,
        gap_lot_override=gap_lot_override,
        gap_sl_override=gap_sl_override,
    )

def _place_orders_for_anchor(self, label, anchor_utc, anchor_price, current_price,
                              retry_count=0,
                              gap_mode_locked=False,
                              gap_lot_override=None,
                              gap_sl_override=None):
    # All the original gap detection + pre-flight + placement logic.
    # v2.5.2: retry_count parameter added — used in the rc=-1 recovery block below.
    # v2.5.3: gap_mode_locked + overrides — if a previous attempt resolved
    #         gap mode, retries inherit verbatim instead of re-evaluating
    #         (re-eval would fall to normal mode → 2× lot + wider SL).

    # v2.5.3: if gap mode was locked in a prior attempt, honor it
    if gap_mode_locked:
        gap_mode    = True
        gap_lot     = gap_lot_override or round(self.cfg.lot_size / 2, 2)
        gap_sl_dist = gap_sl_override  or 10.0
        gap_tp_dist = self.cfg.tp_dist
        log.info(
            f"{label}: gap mode preserved across retry — "
            f"lot={gap_lot}, SL=${gap_sl_dist}, anchor=${anchor_price:.2f}"
        )
        self.tele.info(
            f"♻️ *{label} retry inheriting gap mode* — "
            f"lot `{gap_lot}` SL `${gap_sl_dist}` (locked from initial)"
        )
    else:
        # ADAPTIVE RE-ANCHOR ON GAP DAYS
        # If the captured anchor is too far from current market, BOTH stops
        # would be on the same side of price → one is mechanically invalid.
        # Instead of skipping (passive), we re-anchor to current M5 close and
        # trade the breakout from there with REDUCED RISK (half-lot, tight SL).
        gap_mode = False
        gap_lot = self.cfg.lot_size
        gap_sl_dist = self.cfg.sl_dist
        gap_tp_dist = self.cfg.tp_dist
        if current_price is not None:
            gap = abs(current_price - anchor_price)
            if gap > self.cfg.trigger_dist + 0.1:  # v2.3: was 0.5, now 0.1 — catches edge cases where market crept 10¢+ past trigger
                # Try to use the most recent M5 close as the new anchor.
                # We fetch the M5 bar just before NOW (not the scheduled anchor time).
                try:
                    now_utc = pd.Timestamp.now(tz='UTC')
                    # Round DOWN to nearest 5 min boundary, then go one bar back
                    minute = now_utc.minute - (now_utc.minute % 5)
                    last_m5_end = now_utc.replace(minute=minute, second=0, microsecond=0)
                    new_anchor = self.adapter.get_m5_close(self.cfg.symbol, last_m5_end)
                    if new_anchor is None or abs(new_anchor - current_price) > self.cfg.trigger_dist:
                        # Couldn't get fresh M5 OR fresh M5 also far from market
                        # → use current price as anchor directly
                        new_anchor = round(current_price, 2)
                except Exception as e:
                    log.warning(f"Re-anchor M5 fetch failed: {e}")
                    new_anchor = round(current_price, 2)

                gap_mode = True
                gap_lot = round(self.cfg.lot_size / 2, 2)  # half-size
                gap_sl_dist = 10.0    # tight SL: $10 instead of $18
                gap_tp_dist = self.cfg.tp_dist  # keep normal TP
                retry_tag = f" (retry {retry_count})" if retry_count > 0 else ""    # v2.5.2
                self.tele.warn(
                    f"⚠️ *{label} GAP DETECTED{retry_tag}*\n"
                    f"Original anchor: `${anchor_price:.2f}`\n"
                    f"Current market:  `${current_price:.2f}`\n"
                    f"Gap: `${gap:.2f}` (> ${self.cfg.trigger_dist + 0.1:.2f} threshold)\n"
                    f"→ Re-anchoring to current M5 close `${new_anchor:.2f}`\n"
                    f"→ Half-lot `{gap_lot}` with tight SL `${gap_sl_dist:.0f}` "
                    f"(reduced risk for gap-day breakout)"
                )
                anchor_price = new_anchor

    buy_stop  = round(anchor_price + self.cfg.trigger_dist, 2)
    sell_stop = round(anchor_price - self.cfg.trigger_dist, 2)
    sl_buy    = round(buy_stop  - gap_sl_dist, 2)
    sl_sell   = round(sell_stop + gap_sl_dist, 2)
    tp_buy    = round(buy_stop  + gap_tp_dist, 2)
    tp_sell   = round(sell_stop - gap_tp_dist, 2)

    # FINAL SAFETY CHECK — after re-anchor, both stops should be on opposite
    # sides of current price. If only ONE is invalid, place the valid side
    # alone (v2.3 fix — was skipping both, leaving valid trades on the table).
    skip_buy = False
    skip_sell = False
    if current_price is not None:
        buy_invalid  = buy_stop  < current_price
        sell_invalid = sell_stop > current_price
        if buy_invalid and sell_invalid:
            self.tele.error(
                f"❌ *{label} skipped — BOTH sides invalid after re-anchor*\n"
                f"Anchor ${anchor_price:.2f}, market ${current_price:.2f}\n"
                f"BUY ${buy_stop} below market, SELL ${sell_stop} above market.\n"
                f"Refusing to place orders that would be rejected."
            )
            return
        elif buy_invalid:
            skip_buy = True
            self.tele.warn(
                f"⚠️ *{label} — BUY invalid, placing SELL alone*\n"
                f"BUY ${buy_stop} would be below market ${current_price:.2f} (skip).\n"
                f"SELL ${sell_stop} valid — proceeding with one-sided entry."
            )
        elif sell_invalid:
            skip_sell = True
            self.tele.warn(
                f"⚠️ *{label} — SELL invalid, placing BUY alone*\n"
                f"SELL ${sell_stop} would be above market ${current_price:.2f} (skip).\n"
                f"BUY ${buy_stop} valid — proceeding with one-sided entry."
            )

    mode_tag = " [GAP MODE: half-lot, $10 SL]" if gap_mode else ""
    retry_tag = f" [RETRY {retry_count}]" if retry_count > 0 else ""     # v2.5.2
    # v3.0.5: scheduled-vs-actual times on every placement; if this fire is past
    # the on-time grace it is a LATE ANCHOR (loud WARN). anchor_price is the price
    # RE-CAPTURED at this placement moment (current-price anchoring), not the stale
    # scheduled-time price -- geometry (±$5 / SL / TP) is unchanged.
    now_utc = pd.Timestamp.now(tz='UTC')
    secs_late = (now_utc - anchor_utc).total_seconds()
    is_late = secs_late >= getattr(self, 'ANCHOR_ONTIME_GRACE_S', 120)
    header = (f"⏰ *LATE ANCHOR {label}*{retry_tag}" if is_late
              else f"⚓ *{label}*{retry_tag}")
    placement_msg = (
        f"{header}\n"
        f"{anchor_time_block(anchor_utc, now_utc)}\n"
        f"  anchor=${anchor_price:.2f} (re-captured){mode_tag}\n"
        f"  BUY  stop @ ${buy_stop}  (SL ${sl_buy}, TP ${tp_buy})\n"
        f"  SELL stop @ ${sell_stop} (SL ${sl_sell}, TP ${tp_sell})\n"
        f"  Lot: `{gap_lot}`"
    )
    (self.tele.warn if is_late else self.tele.info)(placement_msg)

    # PRE-FLIGHT VALIDATION — don't send orders that will be rejected.
    # v2.3: if only ONE side is invalid, place the valid side alone.
    if current_price is not None:
        buy_invalid  = buy_stop  <= current_price
        sell_invalid = sell_stop >= current_price
        if buy_invalid and sell_invalid:
            self.tele.warn(
                f"⚠️ *{label} skipped — BOTH sides invalid in pre-flight*\n"
                f"Anchor ${anchor_price:.2f}, market ${current_price:.2f}\n"
                f"BUY ${buy_stop} ≤ market, SELL ${sell_stop} ≥ market. Not sending."
            )
            return
        elif buy_invalid and not skip_buy:
            skip_buy = True
            self.tele.warn(
                f"⚠️ *{label} pre-flight — placing SELL alone*\n"
                f"BUY ${buy_stop} ≤ market ${current_price:.2f}; SELL ${sell_stop} valid."
            )
        elif sell_invalid and not skip_sell:
            skip_sell = True
            self.tele.warn(
                f"⚠️ *{label} pre-flight — placing BUY alone*\n"
                f"SELL ${sell_stop} ≥ market ${current_price:.2f}; BUY ${buy_stop} valid."
            )

    # v2.3: only place the sides that passed pre-flight
    # v2.5.2: append retry tag to comment for MT5 audit trail
    # v2.5.3: capture mt5.last_error() IMMEDIATELY after each call so we
    #         have forensic data on every rc=-1 (adapter swallows it
    #         internally during its built-in rc=-1 reconcile retry)
    retry_comment = f"_R{retry_count}" if retry_count > 0 else ""
    buy_res = None
    sell_res = None
    buy_err = None
    sell_err = None
    if not skip_buy:
        buy_res = self.adapter.place_stop_order(
            self.cfg.symbol, 'BUY', buy_stop, gap_lot,
            sl=sl_buy, tp=tp_buy,
            comment=f"AUR_{label[:2]}_BUY{'_G' if gap_mode else ''}{retry_comment}",
            dry_run=self.paper)
        if not self.paper:
            try:
                buy_err = self.adapter.mt5.last_error()
            except Exception:
                buy_err = ('?', 'last_error read failed')
    if not skip_sell:
        sell_res = self.adapter.place_stop_order(
            self.cfg.symbol, 'SELL', sell_stop, gap_lot,
            sl=sl_sell, tp=tp_sell,
            comment=f"AUR_{label[:2]}_SELL{'_G' if gap_mode else ''}{retry_comment}",
            dry_run=self.paper)
        if not self.paper:
            try:
                sell_err = self.adapter.mt5.last_error()
            except Exception:
                sell_err = ('?', 'last_error read failed')

    # v2.5.3: surface mt5.last_error() in logs immediately when placement
    # returns None (otherwise this info is lost forever)
    if buy_res is None and not skip_buy:
        log.error(
            f"{label} BUY order_send returned None. mt5.last_error={buy_err}. "
            f"Price=${buy_stop} SL=${sl_buy} TP=${tp_buy} lot={gap_lot} "
            f"gap_mode={gap_mode}"
        )
    if sell_res is None and not skip_sell:
        log.error(
            f"{label} SELL order_send returned None. mt5.last_error={sell_err}. "
            f"Price=${sell_stop} SL=${sl_sell} TP=${tp_sell} lot={gap_lot} "
            f"gap_mode={gap_mode}"
        )

    buy_ticket  = self._extract_ticket(buy_res,  f"paper_{label}_BUY")  if buy_res  is not None else None
    sell_ticket = self._extract_ticket(sell_res, f"paper_{label}_SELL") if sell_res is not None else None

    # v2.3: success path includes single-side placement
    buy_ok  = (buy_ticket  is not None) if not skip_buy  else True   # treat skipped-by-design as "no problem"
    sell_ok = (sell_ticket is not None) if not skip_sell else True

    if buy_ok and sell_ok:
        # v3.0.5: this anchor has PLACED -> mark it (gates any further/late
        # attempts; one placement per anchor per day). sched_utc rides along on
        # the shadow pendings so fill/close can print scheduled vs actual times.
        self._mark_anchor_placed(label)
        sched_iso = anchor_utc.isoformat()
        if buy_ticket is not None:
            self.shadow_pendings[buy_ticket] = {
                'anchor_label': label, 'side': 'BUY',
                'sibling_ticket': sell_ticket,  # None when SELL was skipped — fill handler tolerates None
                'entry_price': buy_stop,
                'sched_utc': sched_iso,
            }
        if sell_ticket is not None:
            self.shadow_pendings[sell_ticket] = {
                'anchor_label': label, 'side': 'SELL',
                'sibling_ticket': buy_ticket,  # None when BUY was skipped
                'entry_price': sell_stop,
                'sched_utc': sched_iso,
            }
        # Hot polling window
        self._hot_poll_until = pd.Timestamp.now(tz='UTC') + pd.Timedelta(seconds=30)
        # v2.5.2: surface retry success
        if retry_count > 0:
            self.tele.success(f"✅ *{label} placement succeeded on retry {retry_count}*")
        # Guard 3 (Monday-wake hardening): confirm A1's resting stops actually
        # exist at the broker. A "successful" send that left no resting order is a
        # silent A1 no-show; re-place the missing leg once, else alert loudly.
        if not self.paper and label.startswith("A1"):
            self._confirm_a1_placement(
                label, gap_lot,
                None if skip_buy  else (buy_stop,  sl_buy,  tp_buy,  buy_ticket),
                None if skip_sell else (sell_stop, sl_sell, tp_sell, sell_ticket))
        return

    # If we got here, pre-flight passed but the broker STILL rejected
    # one or both (slippage between check and send, or other broker issue).
    # Clean up: cancel anything that did place, log honestly, move on.
    def _rcname(res):
        rc = getattr(res, 'retcode', None) if res is not None else None
        return f"{rc} ({_MT5_RETCODE_MAP.get(rc, '?')})" if rc else "no_response"

    buy_rc  = getattr(buy_res,  'retcode', None) if buy_res  is not None else None
    sell_rc = getattr(sell_res, 'retcode', None) if sell_res is not None else None

    # Cancel any orphan FIRST before deciding recovery
    for orphan in (buy_ticket, sell_ticket):
        if orphan is not None and not str(orphan).startswith("paper_"):
            try:
                self.adapter.cancel_order(orphan, dry_run=self.paper)
                self.tele.info(f"Cancelled orphan ticket {orphan}")
            except Exception as e:
                self.tele.error(f"Failed to cancel orphan {orphan}: {e}")

    # ----- IN-FLIGHT BREAKOUT RECOVERY (rc=10015 INVALID_PRICE only) -----
    # When pre-flight passed but broker rejected with INVALID_PRICE on one
    # side, it means price moved past our threshold WHILE the order was in
    # flight (sub-second timing). This is a real breakout we just missed
    # by milliseconds. Catchable if slip is small.
    #
    # Only activates when ALL of these are true:
    #   1. One side rejected with INVALID_PRICE (10015)
    #   2. The OTHER side either filled or also rejected (not a partial OK)
    #   3. Re-read market confirms direction (price IS past the threshold)
    #   4. Slip is in catchable zone: $0.50 to $15
    #
    # Outside that zone we skip cleanly. Gap mode at top of function handles
    # huge anchor staleness; this handles the in-flight millisecond gap.
    try:
        tick = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
        recovery_price = (tick.ask + tick.bid) / 2 if tick else None
    except Exception:
        recovery_price = None

    breakout_side = None
    slip = 0.0
    if recovery_price is not None:
        if buy_rc == 10015 and recovery_price >= buy_stop:
            breakout_side = 'BUY'
            slip = recovery_price - buy_stop
        elif sell_rc == 10015 and recovery_price <= sell_stop:
            breakout_side = 'SELL'
            slip = sell_stop - recovery_price

    # Catchable zone check
    if breakout_side is not None and 0.5 <= slip <= 15.0 and recovery_price is not None:
        # Half the gap_lot (already half if in gap mode), tight $10 SL,
        # normal $30 TP. Recovery trades tagged "_RCV" in MT5 comment.
        rcv_lot = round(max(gap_lot / 2 if gap_mode else gap_lot * 0.5, 0.01), 2)
        rcv_sl_dist = 10.0
        if breakout_side == 'BUY':
            rcv_sl = round(recovery_price - rcv_sl_dist, 2)
            rcv_tp = round(recovery_price + gap_tp_dist, 2)
        else:
            rcv_sl = round(recovery_price + rcv_sl_dist, 2)
            rcv_tp = round(recovery_price - gap_tp_dist, 2)

        self.tele.warn(
            f"🎯 *{label} IN-FLIGHT BREAKOUT — recovering {breakout_side}*\n"
            f"Threshold ${buy_stop if breakout_side=='BUY' else sell_stop} was "
            f"${slip:.2f} behind market ${recovery_price:.2f} (catchable zone).\n"
            f"Market {breakout_side} • Lot `{rcv_lot}` • SL `${rcv_sl}` ($10 tight) • TP `${rcv_tp}`"
        )
        mkt_res = self.adapter.place_market_order(
            self.cfg.symbol, breakout_side, rcv_lot,
            sl=rcv_sl, tp=rcv_tp,
            comment=f"AUR_{label[:2]}_{breakout_side[0]}_RCV",
            dry_run=self.paper)
        mkt_rc = getattr(mkt_res, 'retcode', None) if mkt_res is not None else None
        if mkt_rc == 10009:
            actual_ticket = getattr(mkt_res, 'order', None) or getattr(mkt_res, 'deal', None)
            fill_price = getattr(mkt_res, 'price', recovery_price)
            if actual_ticket:
                self.shadow_positions[int(actual_ticket)] = {
                    'anchor_label': label, 'side': breakout_side,
                    'entry_price': float(fill_price),
                    'current_sl': rcv_sl,
                    'tp_level': rcv_tp,
                    'max_fav': float(fill_price),
                    'recovery': True,
                    'fill_time': pd.Timestamp.now(tz='UTC').isoformat(),  # v2.3
                    'sched_utc': anchor_utc.isoformat(),  # v3.0.5
                }
            # v3.0.5: an in-flight recovery fill IS this anchor's placement.
            self._mark_anchor_placed(label)
            self.tele.success(
                f"✅ *{label} recovery {breakout_side} filled @ ${fill_price}*"
            )
        else:
            self.tele.error(
                f"❌ *{label} recovery market order also rejected*\n"
                f"retcode={mkt_rc} ({_MT5_RETCODE_MAP.get(mkt_rc, '?')})"
            )
        return

    # ----- v2.5.2: rc=-1 / no_response RETRY -----
    # If broker simply didn't respond (most likely VPS↔broker network spike
    # at session open), re-schedule placement via the deferred-anchor
    # mechanism instead of giving up. Tick loop continues managing existing
    # positions during the wait. Backoff: 15s, 30s. Max 2 retries.
    # v2.5.3: PRESERVE gap-mode state so retries don't fall back to normal
    #         mode (and double the lot + widen the SL).
    both_no_response_now = (buy_rc in (None, -1)) and (sell_rc in (None, -1))
    if both_no_response_now and retry_count < self.MAX_PLACEMENT_RETRIES:
        # v2.5.3: dump full mt5 state on rc=-1 — this is the diagnostic
        # gold the user wants. If anything fails tomorrow, this log line
        # tells us exactly why.
        self._dump_mt5_state(
            label,
            f"rc=-1 RETRY scheduled (attempt {retry_count + 1}/{self.MAX_PLACEMENT_RETRIES})"
        )
        retry_delay = self.RETRY_BACKOFF_BASE_SEC * (1 + retry_count)  # 15s, then 30s
        next_defer = pd.Timestamp.now(tz='UTC') + pd.Timedelta(seconds=retry_delay)
        self._deferred_anchor = {
            'label': label,
            'anchor_utc': anchor_utc,
            'anchor_price': anchor_price,    # use the *current* anchor
                                              # (already re-anchored if gap mode)
            'defer_until': next_defer,
            'retry_count': retry_count + 1,
            # v2.5.3: lock gap state across retries
            'gap_mode_locked':  gap_mode,
            'gap_lot_override': gap_lot     if gap_mode else None,
            'gap_sl_override':  gap_sl_dist if gap_mode else None,
            'gap_re_anchor':    anchor_price if gap_mode else None,
        }
        err_detail = ""
        if not self.paper and (buy_err or sell_err):
            err_detail = (f"\nBUY  mt5.last\\_error: `{buy_err}`"
                          f"\nSELL mt5.last\\_error: `{sell_err}`")
        self.tele.warn(
            f"🔁 *{label} retry {retry_count + 1}/{self.MAX_PLACEMENT_RETRIES} scheduled*\n"
            f"Both sides returned rc=-1 (broker/network comm failure).\n"
            f"Re-attempting in `{retry_delay}s` at `{next_defer.strftime('%H:%M:%S')}` UTC.\n"
            f"Position management on existing trades continues uninterrupted."
            + err_detail
        )
        return  # tick loop will pick this up via _complete_deferred_anchor

    # Out of catchable zone OR no breakout direction confirmed — skip cleanly
    # v2.3: distinguish "order placement failed" (rc=-1 etc) from "genuine no-breakout"
    # v2.5.2: append retry-exhausted suffix to skip message
    # v2.5.3: dump full mt5 state on final skip so we have FULL forensics
    both_no_response = (buy_rc in (None, -1)) and (sell_rc in (None, -1))
    if both_no_response:
        retry_suffix = f" — gave up after {retry_count} retries" if retry_count > 0 else ""
        skip_reason = f"ORDER PLACEMENT FAILED — broker returned no response on both sides{retry_suffix}"
        # v2.5.3: full diagnostic dump on final failure
        self._dump_mt5_state(label, f"FINAL SKIP: {skip_reason}")
    elif breakout_side is not None and slip > 15.0:
        skip_reason = f"slip ${slip:.2f} > $15 (move exhausted, would chase top/bottom)"
    elif breakout_side is not None and slip < 0.5:
        skip_reason = f"slip ${slip:.2f} < $0.50 (price didn't actually break, broker quirk)"
    else:
        skip_reason = "no breakout confirmed"
    err_detail_skip = ""
    if not self.paper and (buy_err or sell_err):
        err_detail_skip = (f"\nBUY  mt5.last\\_error: `{buy_err}`"
                           f"\nSELL mt5.last\\_error: `{sell_err}`")
    self.tele.error(
        f"❌ *{label} skipped — {skip_reason}*\n"
        f"BUY  stop @ ${buy_stop}: rc={_rcname(buy_res)}\n"
        f"SELL stop @ ${sell_stop}: rc={_rcname(sell_res)}\n"
        f"Current market: ${recovery_price if recovery_price else '?'}"
        + err_detail_skip
    )

def _confirm_a1_placement(self, label, lot, buy_leg, sell_leg):
    """Guard 3: assert A1's resting stop orders exist at the broker after a
    "successful" placement; re-place a confirmed-missing leg ONCE, else fire a
    loud INCOMPLETE alert. buy_leg/sell_leg are (price, sl, tp, ticket) or None
    (skipped by design). Live only. Never raises (a guard must not break the
    loop). Re-placement triggers only on TWO consecutive broker reads that both
    miss the leg, to avoid duplicating an order on a transient empty read."""
    try:
        def _present_once(side, price, ticket):
            try:
                orders = self.adapter.mt5.orders_get(symbol=self.cfg.symbol) or []
            except Exception as e:
                log.warning(f"{label}: orders_get raised during A1 confirm: {e}")
                return None
            want = (self.adapter.mt5.ORDER_TYPE_BUY_STOP if side == "BUY"
                    else self.adapter.mt5.ORDER_TYPE_SELL_STOP)
            for o in orders:
                if ticket is not None and getattr(o, "ticket", None) == ticket:
                    return True
                if (getattr(o, "type", None) == want and
                        abs(getattr(o, "price_open", 0.0) - price) <= 0.05):
                    return True
            return False

        def _present(side, price, ticket):
            r1 = _present_once(side, price, ticket)
            if r1 is None or r1:
                return True            # unknown or present -> never re-place
            time.sleep(1)
            r2 = _present_once(side, price, ticket)
            return (r2 is None) or bool(r2)  # absent only on two confirmed misses

        legs = []
        if buy_leg is not None:
            legs.append(("BUY",) + tuple(buy_leg))
        if sell_leg is not None:
            legs.append(("SELL",) + tuple(sell_leg))

        for side, price, sl, tp, ticket in legs:
            if _present(side, price, ticket):
                continue
            self.tele.warn(
                f"⚠️ *{label} {side} stop missing at broker — re-placing once*\n"
                f"Sent OK but no resting {side} stop @ ${price} found.")
            res = self.adapter.place_stop_order(
                self.cfg.symbol, side, price, lot, sl=sl, tp=tp,
                comment=f"AUR_{label[:2]}_{side[0]}_CFM", dry_run=self.paper)
            new_ticket = self._extract_ticket(res, f"paper_{label}_{side}")
            time.sleep(1)
            if new_ticket is not None and _present(side, price, new_ticket):
                self.shadow_pendings[int(new_ticket)] = {
                    "anchor_label": label, "side": side,
                    "sibling_ticket": None, "entry_price": price,
                }
                self.tele.success(
                    f"✅ *{label} {side} stop re-placed and confirmed* (ticket {new_ticket})")
            else:
                self.tele.warn(
                    f"⚠️ *{label} placement INCOMPLETE — {side} leg missing*\n"
                    f"{side} stop @ ${price} not present at broker after re-place "
                    f"(rc={getattr(res, 'retcode', None)}). Manual check needed.")
    except Exception as e:
        log.warning(f"{label}: A1 placement confirm raised (non-fatal): {e}")


# ------------------------------------------------------------------------
# v2.5.3: Trade channel warmup, MT5 reconnect, and DIAGNOSTIC DUMPS
# ------------------------------------------------------------------------

def _dump_mt5_state(self, label: str, context: str) -> None:
    """v2.5.3: CLEAR FAILURE LOGGING. Captures the full MT5 state at the
    moment of any failure into a single multi-line log entry. If anything
    fails tomorrow, ONE log block has the complete story.

    Always logs at ERROR level (visible in default log filter). Also
    sends a compact telegram so failures are visible on the phone.

    Captures: terminal_info, account_info, symbol_info trade params,
    latest tick, and mt5.last_error(). Designed to never raise.
    """
    try:
        if self.paper:
            log.error(f"[{label}] {context} — PAPER mode, no MT5 state")
            return

        mt5 = self.adapter.mt5
        lines = [
            f"╔══ MT5 DIAGNOSTIC DUMP — {label} ══",
            f"║ Context : {context}",
            f"║ UTC time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        ]

        # 1. Terminal state
        try:
            ti = mt5.terminal_info()
            if ti:
                lines.append(
                    f"║ Terminal: connected={ti.connected}  "
                    f"trade_allowed={ti.trade_allowed}  "
                    f"dlls_allowed={ti.dlls_allowed}  "
                    f"build={ti.build}  ping={ti.ping_last/1000:.0f}ms"
                )
            else:
                lines.append("║ Terminal: terminal_info() returned None ⚠")
        except Exception as e:
            lines.append(f"║ Terminal: read raised {type(e).__name__}: {e}")

        # 2. Account state — balance, equity, margin
        try:
            ai = mt5.account_info()
            if ai:
                lines.append(
                    f"║ Account : #{ai.login} on `{ai.server}`  "
                    f"balance=${ai.balance:.2f}  equity=${ai.equity:.2f}  "
                    f"margin=${ai.margin:.2f}  free=${ai.margin_free:.2f}  "
                    f"trade_mode={ai.trade_mode}"
                )
            else:
                lines.append("║ Account : account_info() returned None ⚠")
        except Exception as e:
            lines.append(f"║ Account : read raised {type(e).__name__}: {e}")

        # 3. Symbol trading state — stops/freeze/filling/etc
        try:
            si = mt5.symbol_info(self.cfg.symbol)
            if si:
                lines.append(
                    f"║ Symbol  : {self.cfg.symbol}  "
                    f"trade_mode={si.trade_mode} "
                    f"(0=disabled,1=long_only,2=short_only,3=close_only,4=full)"
                )
                lines.append(
                    f"║         : stops_level={si.trade_stops_level}pts "
                    f"= ${si.trade_stops_level * si.point:.2f} | "
                    f"freeze_level={si.trade_freeze_level}pts "
                    f"= ${si.trade_freeze_level * si.point:.2f}"
                )
                lines.append(
                    f"║         : volume_step={si.volume_step}  "
                    f"vol_min={si.volume_min}  vol_max={si.volume_max}  "
                    f"filling_mode={si.filling_mode} "
                    f"(1=FOK,2=IOC,3=both,4=RETURN)"
                )
            else:
                lines.append(f"║ Symbol  : symbol_info({self.cfg.symbol}) returned None ⚠")
        except Exception as e:
            lines.append(f"║ Symbol  : read raised {type(e).__name__}: {e}")

        # 4. Latest tick — how old? mid-price?
        try:
            tk = mt5.symbol_info_tick(self.cfg.symbol)
            if tk:
                broker_offset = self.adapter.tick_time_offset_hours * 3600
                tick_utc_unix = tk.time - broker_offset
                now_unix = pd.Timestamp.now(tz='UTC').timestamp()
                age = abs(now_unix - tick_utc_unix)
                lines.append(
                    f"║ Tick    : bid=${tk.bid:.2f}  ask=${tk.ask:.2f}  "
                    f"spread=${(tk.ask-tk.bid):.2f}  age={age:.1f}s  "
                    f"volume={tk.volume}"
                )
            else:
                lines.append("║ Tick    : symbol_info_tick() returned None ⚠")
        except Exception as e:
            lines.append(f"║ Tick    : read raised {type(e).__name__}: {e}")

        # 5. THE BIG ONE — last_error
        try:
            err = mt5.last_error()
            lines.append(f"║ last_err: {err}  ← THE ROOT CAUSE")
        except Exception as e:
            lines.append(f"║ last_err: read raised {type(e).__name__}: {e}")

        # 6. Bot state context
        try:
            positions_now = len(mt5.positions_get(symbol=self.cfg.symbol) or [])
            pendings_now  = len(mt5.orders_get(symbol=self.cfg.symbol) or [])
            lines.append(
                f"║ Bot     : daily_pnl=${self.state.get('daily_pnl', 0):+.2f}  "
                f"positions={positions_now}  pendings={pendings_now}  "
                f"shadow_pos={len(self.shadow_positions)}  "
                f"shadow_pend={len(self.shadow_pendings)}"
            )
        except Exception as e:
            lines.append(f"║ Bot     : state read raised {e}")

        lines.append("╚════════════════════════════════════════")
        dump = "\n".join(lines)
        log.error(dump)
    except Exception as outer:
        # Diagnostic dump must NEVER raise — fall back to bare log
        log.error(f"[{label}] {context} — dump raised: {outer}")

def _warmup_trade_channel(self, label: str) -> bool:
    """v2.5.3: Send a tiny throwaway pending ($100 from market) to wake
    the MT5 trade channel before real placement.

    The tick loop hammers READ calls every second, but doesn't WRITE
    between anchors. Hours of read-only activity → SDK's write path goes
    cold → order_send returns None instantly (rc=-1). Confirmed root
    cause for A2/A3/A4 failures on 2026-05-27.

    Returns True if channel is healthy (or paper mode). False if both
    the warmup ping AND mt5 reconnect failed.
    """
    if self.paper:
        return True

    try:
        tick = self.adapter.mt5.symbol_info_tick(self.cfg.symbol)
        if tick is None:
            log.warning(f"{label}: warmup — tick read returned None")
            self._dump_mt5_state(label, "WARMUP: tick read returned None")
            return self._attempt_mt5_reconnect(label)

        ping_price = round(tick.ask + self.WARMUP_DISTANCE, 2)
        ping_req = {
            "action":       self.adapter.mt5.TRADE_ACTION_PENDING,
            "symbol":       self.cfg.symbol,
            "volume":       self.WARMUP_LOT,
            "type":         self.adapter.mt5.ORDER_TYPE_BUY_STOP,
            "price":        ping_price,
            "sl":           round(ping_price - 20.0, 2),
            "tp":           round(ping_price + 50.0, 2),
            "deviation":    20,
            "magic":        self.WARMUP_MAGIC,
            "comment":      mt5_comment(self.WARMUP_COMMENT),
            "type_filling": self.adapter.mt5.ORDER_FILLING_IOC,
            "type_time":    self.adapter.mt5.ORDER_TIME_DAY,  # matches bot's convention
        }
        ping_res = self.adapter.mt5.order_send(ping_req)
        ping_err = self.adapter.mt5.last_error()

        if ping_res is None:
            log.warning(
                f"{label}: WARMUP PING returned None. last_error={ping_err}. "
                f"Attempting MT5 reconnect..."
            )
            self._dump_mt5_state(label, "WARMUP PING returned None — channel cold")
            self.tele.warn(
                f"⚠️ *{label}: trade channel cold (warmup failed)*\n"
                f"last\\_error: `{ping_err}`\n"
                f"Cycling MT5 connection via shutdown+initialize..."
            )
            return self._attempt_mt5_reconnect(label)

        if ping_res.retcode != 10009:
            rc_name = _MT5_RETCODE_MAP.get(ping_res.retcode, f"UNKNOWN_{ping_res.retcode}")
            log.warning(
                f"{label}: WARMUP PING rejected retcode={ping_res.retcode} ({rc_name}) "
                f"comment={ping_res.comment} last_error={ping_err}"
            )
            self._dump_mt5_state(
                label,
                f"WARMUP PING rejected rc={ping_res.retcode} ({rc_name})"
            )
            self.tele.warn(
                f"⚠️ *{label}: warmup ping rejected*\n"
                f"retcode `{ping_res.retcode}` ({rc_name}) — `{ping_res.comment}`\n"
                f"last\\_error: `{ping_err}`\n"
                f"Cycling MT5 connection..."
            )
            # Cancel partial ping if a ticket was issued
            try:
                if ping_res.order:
                    self.adapter.mt5.order_send({
                        "action": self.adapter.mt5.TRADE_ACTION_REMOVE,
                        "order": ping_res.order,
                    })
            except Exception:
                pass
            return self._attempt_mt5_reconnect(label)

        # Success — cancel the ping
        try:
            cancel_res = self.adapter.mt5.order_send({
                "action": self.adapter.mt5.TRADE_ACTION_REMOVE,
                "order":  ping_res.order,
            })
            if cancel_res is None or cancel_res.retcode != 10009:
                log.warning(
                    f"{label}: warmup ping placed (ticket {ping_res.order}) "
                    f"but cancel failed (rc={getattr(cancel_res,'retcode',None)}). "
                    f"Ping is $100 from market — will not fill."
                )
        except Exception as e:
            log.warning(f"{label}: ping cancel raised: {e}")

        log.info(
            f"{label}: ✅ trade channel warmup OK (ping ticket {ping_res.order})"
        )
        return True

    except Exception as e:
        log.error(f"{label}: warmup raised {type(e).__name__}: {e}")
        self._dump_mt5_state(label, f"WARMUP raised {type(e).__name__}: {e}")
        self.tele.warn(f"⚠️ {label}: warmup raised exception — attempting reconnect")
        return self._attempt_mt5_reconnect(label)

def _attempt_mt5_reconnect(self, label: str) -> bool:
    """v2.5.3: Force-cycle the MT5 connection: shutdown + initialize + verify.

    Called when warmup ping fails. Recovers a cold trade channel by tearing
    down and re-establishing the SDK. Returns True if reconnect succeeded
    and verified healthy state, False otherwise.

    shadow_positions/shadow_pendings are unaffected — reconcile loop will
    rebuild them from broker state on the next tick.
    """
    log.warning(f"{label}: cycling MT5 connection (shutdown + initialize)")
    try:
        self.adapter.mt5.shutdown()
    except Exception as e:
        log.warning(f"{label}: mt5.shutdown() raised: {e}")

    # Tiny pause to let the OS release sockets cleanly
    time.sleep(0.5)

    try:
        init_ok = self.adapter.mt5.initialize()
        if not init_ok:
            err = self.adapter.mt5.last_error()
            self._dump_mt5_state(
                label, f"RECONNECT: mt5.initialize() returned False, last_error={err}"
            )
            self.tele.error(
                f"❌ *{label}: mt5.initialize() failed after shutdown*\n"
                f"last\\_error: `{err}`\n"
                f"Anchor will be skipped. Watchdog may need to restart bot."
            )
            return False
    except Exception as e:
        self._dump_mt5_state(label, f"RECONNECT: mt5.initialize() raised: {e}")
        self.tele.error(f"❌ *{label}: mt5.initialize() raised:* `{e}`")
        return False

    # Verify reconnect actually worked
    try:
        ti = self.adapter.mt5.terminal_info()
        ai = self.adapter.mt5.account_info()
        if ti is None or not ti.connected or not ti.trade_allowed:
            self._dump_mt5_state(label, "RECONNECT: post-reconnect terminal unhealthy")
            self.tele.error(
                f"❌ *{label}: post-reconnect terminal unhealthy*\n"
                f"connected=`{getattr(ti,'connected',None)}`  "
                f"trade\\_allowed=`{getattr(ti,'trade_allowed',None)}`"
            )
            return False
        if ai is None:
            self._dump_mt5_state(label, "RECONNECT: account_info() is None after reconnect")
            self.tele.error(f"❌ *{label}: post-reconnect account_info is None*")
            return False
        log.info(
            f"{label}: ✅ MT5 reconnected — account #{ai.login} on {ai.server}, "
            f"balance ${ai.balance:.2f}"
        )
        self.tele.warn(
            f"♻️ *{label}: MT5 trade channel cycled (recovery)*\n"
            f"Account `#{ai.login}` on `{ai.server}` — proceeding with placement."
        )
        return True
    except Exception as e:
        self._dump_mt5_state(label, f"RECONNECT: post-reconnect verify raised: {e}")
        self.tele.error(f"❌ *{label}: post-reconnect verification raised:* `{e}`")
        return False

@staticmethod
def _extract_ticket(result, fallback: str):
    if result is None: return None
    if isinstance(result, dict) and result.get('paper'):
        return fallback
    # Real MT5 result — only consider it a real ticket if retcode == DONE (10009)
    retcode = getattr(result, 'retcode', None)
    if retcode != 10009:
        return None
    ticket = getattr(result, 'order', None)
    if ticket:
        return int(ticket)
    return None
