"""AUREON v3.2.9 — manual TESTFIRE: one real anchor entry, on demand, at market.

`python bot.py testfire [--anchor A2]` fires ONE straddle at the CURRENT market
price, off-schedule, so the operator can watch real fills (straddle, boosts,
rally/rescue resolution) without waiting for a scheduled anchor.

It REUSES the live placement path — it does NOT fork a parallel copy:
  arm_testfire() drops a deferred anchor (defer_until = now) onto the trader, and
  the SAME LiveTrader.run() loop then calls _complete_deferred_anchor() ->
  _place_orders_for_anchor() (the exact scheduled-anchor entry). _complete_deferred_
  anchor already re-anchors to the current price at placement, so the straddle is
  current_mid +/- trigger_dist ($5), $18 SL / $30 TP, No-OCO, and the rally(+5)/
  rescue(-10) boost logic — all identical to a scheduled anchor. The ONLY
  differences are the trigger source (manual) and the timestamp.

SAFETY (fail-closed; rails 1/2/3/5/6 mandatory, no override):
  1. DEMO ONLY     — account_info.trade_mode must be ACCOUNT_TRADE_MODE_DEMO.
  2. NOT FP/funded — account_profile must be STANDARD_5PCT (FPZERO_1PCT and any
                     other profile are refused even on demo).
  3. FLAT BOOK     — no broker positions/pendings and no internal shadow state
                     (same flatness guard selftest uses); a real anchor in-flight
                     blocks the test.
  4. NO COLLISION  — no scheduled anchor active or within testfire_collision_min.
                     v3.3.1: this is the ONLY bypassable rail — `--force-window`
                     skips it (loud warning, never silent) so the owner can test
                     off-schedule. Rails 1/2/3/5/6 are NEVER bypassable.
  5. ONE AT A TIME — refuse if a prior test-fire event is still open.
  6. ANCHORS BRAKE — (E-23, v3.8.3) refuse while the anchors engine may take NO
                     new risk today: the daily LOSS halt (hard) / PROFIT lock /
                     account lock, OR the Friday weekend-hold window / anchors
                     engine switch OFF. A test-fire is NEW anchor risk and obeys
                     the exact same brake as a scheduled anchor. The day P&L comes
                     from the COMPUTED source (pnl_source.magic_day_net), never the
                     state['daily_pnl'] mirror; the rebuild is run BEFORE preflight
                     so the governor knows the day's truth (the 07-09 defect was
                     preflight clearing 4s before the rebuild landed). `--force-
                     window` does NOT bypass this — it only skips rail 4.

The trade IS real, so it counts toward the 30-trade validation; it is tagged in
the journal with trigger_source='TESTFIRE' so it is auditable and distinguishable
from a clock-scheduled anchor (NOT excluded from the count).
"""
import logging

import pandas as pd

log = logging.getLogger("AUREON")


def minutes_to_nearest_anchor(cfg, now_utc):
    """Minutes (>=0) from now_utc to the NEAREST scheduled anchor, scanning
    yesterday/today/tomorrow so the day-wrap near midnight is handled. Anchors are
    (label, broker_hour, broker_minute) in broker time = UTC + broker_tz_offset_hours.
    Returns None if no anchors are configured."""
    off = int(getattr(cfg, 'broker_tz_offset_hours', 3))
    best = None
    for item in getattr(cfg, 'anchors', []) or []:
        try:
            _label, bh, bm = item[0], int(item[1]), int(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        for day_delta in (-1, 0, 1):
            base = (now_utc + pd.Timedelta(days=day_delta)).normalize()  # midnight UTC
            sched = base + pd.Timedelta(hours=bh - off, minutes=bm)      # anchor in UTC
            diff = abs((now_utc - sched).total_seconds()) / 60.0
            if best is None or diff < best:
                best = diff
    return best


def testfire_preflight(trader, now_utc=None, collision_min=None, force_window=False):
    """Fail-closed safety gate. Returns (ok: bool, reason: str). ANY error reading
    the broker is treated as a refusal (never assume safe). The reason string is the
    block reason to print on refusal, or the clear-to-fire summary on pass.

    force_window (v3.3.1): bypasses ONLY rail 4 (the 30-min scheduled-anchor
    collision guard) so the owner can test off-schedule without waiting for the
    window to clear. Rails 1/2/3/5/6 stay HARD and are unaffected — there is no
    override for DEMO-ONLY, NO-FP, FLAT-BOOK, ONE-AT-A-TIME, or the ANCHORS daily
    BRAKE (E-23). When the bypass actually fires (a scheduled anchor is within the
    guard) the returned reason is a LOUD warning naming how many minutes the
    nearest anchor is away; the bypass is never silent."""
    cfg = trader.cfg
    sym = getattr(cfg, 'symbol', 'XAUUSD')
    try:
        mt5 = trader.adapter.mt5
    except Exception as e:
        return False, f"REFUSED: no broker adapter ({e!r}) — fail-closed"

    # Rail 1: DEMO ONLY (no --force override for testfire).
    try:
        ai = mt5.account_info()
    except Exception as e:
        return False, f"REFUSED: cannot read account_info ({e!r}) — fail-closed"
    if ai is None:
        return False, "REFUSED: account_info() is None — cannot confirm DEMO; fail-closed"
    try:
        demo = int(getattr(ai, 'trade_mode', -1)) == int(getattr(mt5, 'ACCOUNT_TRADE_MODE_DEMO', 0))
    except Exception:
        demo = False
    if not demo:
        return False, ("REFUSED [rail 1 DEMO-ONLY]: account trade_mode is NOT "
                       "ACCOUNT_TRADE_MODE_DEMO. testfire places REAL orders and runs "
                       "on the demo terminal only — no --force override.")

    # Rail 2: refuse any FP/funded profile (even on demo).
    profile = str(getattr(cfg, 'account_profile', 'STANDARD_5PCT'))
    if profile != 'STANDARD_5PCT':
        return False, (f"REFUSED [rail 2 NO-FP]: account_profile={profile} is an "
                       f"FP/funded profile. testfire is for the Pepperstone/MetaQuotes "
                       f"demo (STANDARD_5PCT) only.")

    # Rail 3: flat book — broker AND internal shadow state (same guard selftest uses).
    try:
        pos = mt5.positions_get(symbol=sym) or []
        pend = mt5.orders_get(symbol=sym) or []
    except Exception as e:
        return False, f"REFUSED: cannot read broker positions/orders ({e!r}) — fail-closed"
    if pos or pend:
        return False, (f"REFUSED [rail 3 FLAT]: book not flat ({len(pos)} open, "
                       f"{len(pend)} pending). A real anchor may be in-flight — cannot "
                       f"isolate the test.")
    if getattr(trader, 'shadow_positions', None) or getattr(trader, 'shadow_pendings', None):
        return False, ("REFUSED [rail 3 FLAT]: internal shadow positions/pendings "
                       "present — a prior anchor or test-fire is still open.")

    # Rail 5: one test-fire at a time.
    if getattr(trader, '_testfire_event_open', False):
        return False, ("REFUSED [rail 5 ONE-AT-A-TIME]: a prior test-fire event is "
                       "still open. Wait for it to resolve before firing another.")

    now = now_utc if now_utc is not None else pd.Timestamp.now(tz='UTC')

    # Rail 6 (E-23): obey the ANCHORS daily brake. A test-fire is NEW anchor risk, so it must
    # refuse under the SAME conditions a scheduled anchor does. Evaluated BEFORE rail 4 so
    # --force-window (which bypasses ONLY rail 4) can NEVER skip it -- the 07-09 defect fires
    # off-schedule, which is exactly the --force-window path. TWO seams are checked (NOTE:
    # _anchor_entries_blocked alone does NOT cover the daily stop -- it is only the Friday-
    # window / engine-switch seam -- so the daystop is read explicitly):
    #   (a) the anchors daily LOSS halt (hard) / PROFIT lock, via the PURE daystops.anchors_
    #       daystop over the COMPUTED day P&L (_anchors_day_pnl_computed -> magic_day_net,
    #       NEVER the state['daily_pnl'] mirror). PURE, not the LATCHING _anchors_daystop_
    #       blocked -- a preflight must never latch the profit lock or fire its one-time alert.
    #       This is the exact 07-09 defect: the anchors book was LOSS-HALTED at -$821 vs its
    #       -$630 stop, yet testfire placed a real A2 straddle without consulting the governor.
    #   (b) _anchor_entries_blocked(broker_date, now) -- the Friday weekend-hold window and the
    #       anchors engine switch (OFF = manage-only): a test-fire is a NEW straddle.
    # Evaluated AFTER the anchors day-P&L rebuild (run_testfire primes it before preflight).
    # FAIL-CLOSED: the brake methods are called DIRECTLY (a missing/renamed method raises ->
    # the except refuses), never guarded into a silent pass. NOT bypassable by --force-window.
    try:
        import daystops as _ds
        broker_date = trader._broker_date(now)
        dp = trader._anchors_day_pnl_computed()
        daystop_blocked = bool(_ds.anchors_daystop(dp, trader.cfg, trader.state)[0])
        entries_blocked = bool(trader._anchor_entries_blocked(broker_date, now))
    except Exception as e:
        return False, (f"REFUSED [rail 6 ANCHORS-BRAKE]: cannot evaluate the anchors daily "
                       f"brake ({e!r}) — fail-closed. A test-fire never places while the "
                       f"governor's state is unknown.")
    if daystop_blocked or entries_blocked:
        return False, ("REFUSED [rail 6 ANCHORS-BRAKE]: anchors entries blocked — daily loss "
                       "halt / profit lock active (or the Friday weekend-hold window / anchors "
                       "engine switched OFF). A test-fire is NEW anchor risk and obeys the same "
                       "brake as a scheduled anchor. --force-window does NOT override this — it "
                       "skips only rail 4 (the scheduled-anchor collision guard).")

    # Rail 4: never collide with a scheduled anchor (active or within N minutes).
    # This is the ONLY bypassable rail. With --force-window the owner can fire
    # off-schedule even inside the guard; rails 1/2/3/5/6 above already ran and still
    # refuse their cases. The bypass is LOUD (warning names minutes-away) and safe:
    # the scheduler is SUPPRESSED for the whole testfire session (_testfire_mode
    # gates _process_anchor_if_due), so the test event owns the book — the real
    # scheduled anchor will NOT also place alongside it while the test is live.
    n = int(collision_min if collision_min is not None
            else getattr(cfg, 'testfire_collision_min', 30))
    near = minutes_to_nearest_anchor(cfg, now)
    if near is not None and near <= n:
        if force_window:
            return True, (f"⚠️⚠️ CLEARED [rail 4 NO-COLLISION BYPASSED via --force-window]: "
                          f"a scheduled anchor is {near:.0f} min away (<= {n} min guard) — "
                          f"firing OFF-SCHEDULE anyway by owner override. The scheduler is "
                          f"SUPPRESSED for this testfire session, so the real anchor will "
                          f"NOT also fire while the test event is live (the test owns the "
                          f"book). Rails 1/2/3/5/6 (DEMO-ONLY, NO-FP, FLAT-BOOK, ONE-AT-A-"
                          f"TIME, ANCHORS-BRAKE) stay HARD.")
        return False, (f"REFUSED [rail 4 NO-COLLISION]: a scheduled anchor is "
                       f"{near:.0f} min away (<= {n} min guard). Never let a test-fire "
                       f"collide with a real anchor — wait until the window is clear, or "
                       f"pass --force-window to fire off-schedule (rails 1/2/3/5/6 still apply).")

    return True, (f"CLEARED: demo account, STANDARD_5PCT profile, flat book, anchors brake "
                  f"clear, no scheduled anchor within {n} min "
                  f"(nearest {('%.0f min' % near) if near is not None else 'n/a'}).")


def arm_testfire(trader, label='A2', now_utc=None):
    """Drop a deferred anchor (defer_until = now) so the SAME LiveTrader.run() loop
    places it on the next tick via _complete_deferred_anchor -> _place_orders_for_anchor
    (the exact scheduled path; current-price anchoring re-takes the mid at placement).
    Also sets the journal tag + disables scheduled-anchor placement for the session.
    Returns the deferred-anchor dict. NO broker orders are placed here."""
    now = now_utc if now_utc is not None else pd.Timestamp.now(tz='UTC')
    mid = None
    try:
        tk = trader.adapter.mt5.symbol_info_tick(trader.cfg.symbol)
        if tk is not None:
            mid = (float(tk.bid) + float(tk.ask)) / 2.0
    except Exception:
        mid = None  # placement re-takes current price anyway; None is safe (skips if no tick)

    # journal tag + scheduler lockout + one-at-a-time latch (rail 4/5 enforcement).
    trader._trigger_source = 'TESTFIRE'
    trader._testfire_mode = True
    trader._testfire_event_open = True
    trader._deferred_anchor = {
        'label': label,
        'anchor_utc': now,           # manual timestamp (tagged TESTFIRE in the journal)
        'anchor_price': mid,         # re-taken as current price at placement
        'defer_until': now,          # no settle wait — fire on the next tick
        'retry_count': 0,
        'gap_mode_locked': False,
        'gap_lot_override': None,
        'gap_sl_override': None,
        'gap_re_anchor': None,
    }
    log.info(f"TESTFIRE armed [{label}] @ ~${mid if mid is not None else float('nan'):.2f} "
             f"(current-mid straddle, +/-${getattr(trader.cfg, 'trigger_dist', 5.0):.0f})")
    return trader._deferred_anchor


# ============================================================================
# IN-PROCESS TESTFIRE (Discord /testfire) — runs inside the RUNNING live process
# ----------------------------------------------------------------------------
# Unlike run_testfire() (a standalone process that OWNS the book and SUPPRESSES
# the scheduler via _testfire_mode), the in-process path is FULLY ISOLATED from
# the real anchor schedule:
#   * the test straddle gets its OWN anchor identity "TF_<HHMMSS>" — never A1..A5,
#     never a real anchor slot, never in state['processed_anchors_today'];
#   * it uses a SEPARATE deferred slot (trader._testfire_deferred) so it can never
#     delay or consume a real scheduled-anchor placement;
#   * it NEVER sets trader._testfire_mode, so the real scheduler keeps running
#     completely unaware the test exists.
# TF-ness is carried by the label prefix (TF_) end-to-end, so anchors, sweep,
# review-log, journal and the fleet tally all recognise + isolate a test event.
# ============================================================================
TESTFIRE_LABEL_PREFIX = "TF_"


def is_testfire_label(label) -> bool:
    """True iff an anchor label is a TESTFIRE test-anchor identity (TF_<...>)."""
    return str(label or "").startswith(TESTFIRE_LABEL_PREFIX)


def make_testfire_label(now_utc) -> str:
    """The INDIVIDUAL identity for one test straddle: TF_<HHMMSS> (broker-agnostic
    UTC stamp). Never collides with A1..A5 and never reuses a real anchor slot."""
    try:
        stamp = pd.Timestamp(now_utc).strftime("%H%M%S")
    except Exception:
        stamp = "000000"
    return f"{TESTFIRE_LABEL_PREFIX}{stamp}"


def _active_real_anchor(trader, now_utc):
    """The label of a scheduled anchor whose PLACEMENT WINDOW is currently ACTIVE
    (its time has passed and the late window has not elapsed, and it is not already
    placed), else None. Mirrors anchors._process_anchor_if_due's `0 <= delta <
    window_s` eligibility EXACTLY (via the trader's own Monday-shift + datetime
    resolution) so a test-fire never races a real straddle that is placing THIS
    minute — while a test at 09:58 (A2 still 2 min in the FUTURE) is allowed, since
    A2's window is not yet active. Guarded -> None (fail-open on read error; the
    other rails still gate)."""
    try:
        cfg = trader.cfg
        off = int(getattr(cfg, 'broker_tz_offset_hours', 3))
        late = getattr(cfg, 'anchor_late_window_min', 0)
        window_s = max(120.0, late * 60.0)
        now = now_utc if now_utc is not None else pd.Timestamp.now(tz='UTC')
        broker_date = trader._broker_date(now)
        processed = set((getattr(trader, 'state', {}) or {}).get('processed_anchors_today', set()) or set())
        for label, hour, minute in getattr(cfg, 'anchors', []) or []:
            if label in processed:
                continue
            r_hour, r_minute = trader._resolved_anchor_hm(label, broker_date, hour, minute)
            anchor_utc = trader._anchor_datetime_utc(broker_date, r_hour, off, r_minute)
            delta = (now - anchor_utc).total_seconds()
            if 0.0 <= delta < window_s:
                return label
    except Exception:
        return None
    return None


def testfire_preflight_inproc(trader, now_utc=None):
    """Fail-closed gate for the IN-PROCESS /testfire (concurrent with the live bot).
    Returns (ok, reason). Reuses the CLI rails EXCEPT flat-book (rail 3) — the live
    book is NOT expected to be flat (real anchors may be open); isolation, not
    flatness, keeps the test clean. Applied rails: DEMO-only (1), NO-FP (2),
    ONE-AT-A-TIME (5), ACTIVE-ANCHOR-WINDOW (a narrower rail 4), ANCHORS-BRAKE (6).
    The 10-minute rate limit is enforced by the caller."""
    cfg = trader.cfg
    now = now_utc if now_utc is not None else pd.Timestamp.now(tz='UTC')
    try:
        mt5 = trader.adapter.mt5
    except Exception as e:
        return False, f"REFUSED: no broker adapter ({e!r}) — fail-closed"

    # Rail 1: DEMO ONLY.
    try:
        ai = mt5.account_info()
    except Exception as e:
        return False, f"REFUSED: cannot read account_info ({e!r}) — fail-closed"
    if ai is None:
        return False, "REFUSED: account_info() is None — cannot confirm DEMO; fail-closed"
    try:
        demo = int(getattr(ai, 'trade_mode', -1)) == int(getattr(mt5, 'ACCOUNT_TRADE_MODE_DEMO', 0))
    except Exception:
        demo = False
    if not demo:
        return False, ("REFUSED [rail 1 DEMO-ONLY]: account trade_mode is NOT "
                       "ACCOUNT_TRADE_MODE_DEMO. /testfire places REAL orders and runs on "
                       "the demo terminal only.")

    # Rail 2: refuse any FP/funded profile (even on demo).
    profile = str(getattr(cfg, 'account_profile', 'STANDARD_5PCT'))
    if profile != 'STANDARD_5PCT':
        return False, (f"REFUSED [rail 2 NO-FP]: account_profile={profile} is an FP/funded "
                       f"profile. /testfire is for the demo (STANDARD_5PCT) only.")

    # Rail 5: one test-fire at a time.
    if getattr(trader, '_testfire_event_open', False):
        return False, ("REFUSED [rail 5 ONE-AT-A-TIME]: a prior /testfire is still in "
                       "flight. Wait for it to resolve (see /testfire status).")

    # Rail 4 (narrowed): refuse only while a real anchor's placement window is ACTIVE
    # this minute — never for merely being NEAR a future anchor (the test must run
    # independently; the real anchor still fires on schedule).
    active = _active_real_anchor(trader, now)
    if active is not None:
        return False, (f"REFUSED [rail 4 ACTIVE-WINDOW]: scheduled anchor {active} is in its "
                       f"placement window right now. /testfire never races a real anchor that "
                       f"is placing — retry once {active} has placed.")

    # Rail 6 (E-23): obey the ANCHORS daily brake (loss halt / profit lock / account
    # lock / Friday hold / engine OFF). A test-fire is NEW anchor risk. Fail-closed.
    try:
        import daystops as _ds
        broker_date = trader._broker_date(now)
        dp = trader._anchors_day_pnl_computed()
        daystop_blocked = bool(_ds.anchors_daystop(dp, trader.cfg, trader.state)[0])
        entries_blocked = bool(trader._anchor_entries_blocked(broker_date, now))
    except Exception as e:
        return False, (f"REFUSED [rail 6 ANCHORS-BRAKE]: cannot evaluate the anchors daily "
                       f"brake ({e!r}) — fail-closed.")
    if daystop_blocked or entries_blocked:
        return False, ("REFUSED [rail 6 ANCHORS-BRAKE]: anchors entries blocked — daily loss "
                       "halt / profit lock / account lock (or Friday hold / anchors engine "
                       "OFF). A test-fire is NEW anchor risk and obeys the same brake.")

    return True, ("CLEARED: demo, STANDARD_5PCT, one-at-a-time, no active anchor window, "
                  "anchors brake clear — firing an isolated TF_ straddle.")


def arm_testfire_inproc(trader, now_utc=None):
    """Arm ONE isolated test straddle inside the RUNNING process. Drops a deferred
    anchor onto trader._testfire_deferred (a SEPARATE slot from the real
    _deferred_anchor) with a TF_<HHMMSS> identity; the live loop's
    _complete_testfire_anchor places it on the next tick via the SAME
    _place_orders_for_anchor path (current-mid straddle). Does NOT set
    _testfire_mode, so the real scheduler is entirely unaffected. Returns the label.
    NO broker orders are placed here."""
    now = now_utc if now_utc is not None else pd.Timestamp.now(tz='UTC')
    label = make_testfire_label(now)
    mid = None
    try:
        tk = trader.adapter.mt5.symbol_info_tick(trader.cfg.symbol)
        if tk is not None:
            mid = (float(tk.bid) + float(tk.ask)) / 2.0
    except Exception:
        mid = None
    trader._testfire_event_open = True
    trader._testfire_deferred = {
        'label': label,
        'anchor_utc': now,
        'anchor_price': mid,      # re-taken as current price at placement
        'defer_until': now,       # fire on the next tick
        'retry_count': 0,
        'gap_mode_locked': False,
        'gap_lot_override': None,
        'gap_sl_override': None,
        'gap_re_anchor': None,
    }
    # result record (surfaced by /testfire status + the placement/summary cards)
    try:
        st = trader.state.setdefault('testfire', {})
        st.update({'label': label, 'armed_iso': now.isoformat(), 'in_flight': True,
                   'result': 'ARMED', 'legs': {}})
    except Exception:
        pass
    log.info(f"TESTFIRE (in-process) armed [{label}] @ ~"
             f"${mid if mid is not None else float('nan'):.2f} — isolated TF_ straddle, "
             f"scheduler untouched.")
    return label


def _tf_positions_or_pendings_open(trader) -> bool:
    """True iff any TF_ test order/position (or a pending TF placement) is still live."""
    try:
        for coll in (getattr(trader, 'shadow_positions', {}) or {},
                     getattr(trader, 'shadow_pendings', {}) or {}):
            for info in coll.values():
                if is_testfire_label((info or {}).get('anchor_label')):
                    return True
        if getattr(trader, '_testfire_deferred', None) is not None:
            return True
    except Exception:
        return True   # fail-safe: if unsure, treat as still open (never re-fire early)
    return False


def record_testfire_placement(trader, label, latency_ms):
    """Record the /testfire placement outcome (per-leg retcode + ticket + latency) into
    state['testfire'] and post the PASS/FAIL table to Discord. If NO leg placed, tear the
    event down immediately (nothing to manage). Guarded — never raises onto the loop."""
    try:
        cap = getattr(trader, '_testfire_leg_capture', None) or {}
        trader._testfire_leg_capture = None
        legs = {}
        placed = 0
        for side in ('BUY', 'SELL'):
            c = cap.get(side) or {}
            if c.get('skipped'):
                legs[side] = {'status': 'SKIPPED', 'rc': c.get('rc'), 'ticket': c.get('ticket')}
            elif c.get('ticket') is not None:
                placed += 1
                legs[side] = {'status': 'PLACED', 'rc': c.get('rc'), 'ticket': c.get('ticket')}
            else:
                legs[side] = {'status': 'REJECTED', 'rc': c.get('rc'), 'ticket': None}
        result = 'PLACED' if placed >= 1 else 'REJECTED'
        st = trader.state.setdefault('testfire', {})
        st.update({'label': label, 'result': result, 'legs': legs,
                   'latency_ms': round(float(latency_ms), 1),
                   'placed_iso': pd.Timestamp.now(tz='UTC').isoformat()})
        # PASS/FAIL table (same shape as the CLI verification tables): per-leg row + total.
        rows = "\n".join(
            f"  {side:<4} {legs[side]['status']:<8} rc={legs[side]['rc']} "
            f"ticket={legs[side]['ticket']}" for side in ('BUY', 'SELL'))
        verdict = "✅ PASS" if placed >= 1 else "❌ FAIL"
        body = (f"🧪🔥 *TESTFIRE {label}* — {verdict}\n"
                f"```\n{rows}\n  placement latency: {latency_ms:.0f} ms\n```")
        try:
            (trader.tele.success if placed >= 1 else trader.tele.error)(body)
        except Exception:
            pass
        if placed == 0:
            # nothing resting/open -> the event is finished; release the one-at-a-time latch.
            st['in_flight'] = False
            trader._testfire_event_open = False
        log.info(f"TESTFIRE placement {label}: {result} legs={legs} latency={latency_ms:.0f}ms")
    except Exception as e:
        log.warning(f"record_testfire_placement failed (non-fatal): {e!r}")


def testfire_maybe_teardown(trader):
    """Called from the close-detection path: once a /testfire event's LAST TF_ order/
    position has resolved, release the one-at-a-time latch and post the final summary so
    the next /testfire may run. No-op unless an event is open and fully resolved. Guarded."""
    try:
        if not getattr(trader, '_testfire_event_open', False):
            return
        if _tf_positions_or_pendings_open(trader):
            return
        trader._testfire_event_open = False
        st = trader.state.setdefault('testfire', {})
        st['in_flight'] = False
        st['result'] = 'RESOLVED'
        st['resolved_iso'] = pd.Timestamp.now(tz='UTC').isoformat()
        try:
            trader.tele.info(f"🧪🔥 *TESTFIRE {st.get('label', '')} resolved* — all test legs "
                             f"closed; /testfire is clear to run again.")
        except Exception:
            pass
        log.info(f"TESTFIRE event {st.get('label','')} resolved — one-at-a-time latch released.")
    except Exception as e:
        log.warning(f"testfire_maybe_teardown failed (non-fatal): {e!r}")


def handle_testfire_command(trader, now_utc=None):
    """Discord /testfire (in-process): rate-limit -> preflight -> arm ONE isolated TF_
    straddle. The live loop places + manages it; results post as it runs. Guarded."""
    now = now_utc if now_utc is not None else pd.Timestamp.now(tz='UTC')
    rate_sec = float(getattr(trader.cfg, 'testfire_rate_limit_sec', 600.0))
    st = (getattr(trader, 'state', {}) or {}).get('testfire', {}) or {}
    last = st.get('last_run_epoch')
    if last is not None:
        try:
            elapsed = now.timestamp() - float(last)
            if elapsed < rate_sec:
                wait = (rate_sec - elapsed) / 60.0
                trader.tele.warn(f"🧪🔥 /testfire REFUSED [rate-limit]: last run "
                                 f"{elapsed/60.0:.1f} min ago — one run per "
                                 f"{rate_sec/60.0:.0f} min. Try again in {wait:.1f} min.")
                return False
        except Exception:
            pass
    ok, reason = testfire_preflight_inproc(trader, now)
    if not ok:
        try:
            trader.tele.error(f"🧪🔥 /testfire {reason}")
        except Exception:
            pass
        return False
    trader.state.setdefault('testfire', {})['last_run_epoch'] = now.timestamp()
    label = arm_testfire_inproc(trader, now)
    try:
        trader.tele.warn(
            f"🧪🔥 *TESTFIRE armed* [{label}] — placing ONE isolated straddle at current "
            f"mid (+/-${getattr(trader.cfg, 'trigger_dist', 5.0):.0f}, $"
            f"{getattr(trader.cfg, 'sl_dist', 18.0):.0f} SL / ${getattr(trader.cfg, 'tp_dist', 30.0):.0f} "
            f"TP, No-OCO). Real anchor schedule is UNAFFECTED; results post as it runs "
            f"(/testfire status).")
    except Exception:
        pass
    return True


def handle_testfire_status(trader):
    """Discord /testfire status: last run time, result, whether one is in flight. Guarded."""
    try:
        st = (getattr(trader, 'state', {}) or {}).get('testfire', {}) or {}
        if not st:
            trader.tele.info("🧪🔥 /testfire status: no test-fire has run this session.")
            return
        inflight = "YES" if (st.get('in_flight') or getattr(trader, '_testfire_event_open', False)) else "no"
        legs = st.get('legs') or {}
        legs_txt = ", ".join(f"{s}:{(legs.get(s) or {}).get('status','?')}" for s in ('BUY', 'SELL')) or "—"
        when = st.get('placed_iso') or st.get('armed_iso') or "—"
        trader.tele.info(
            f"🧪🔥 *TESTFIRE status*\n"
            f"  last: {st.get('label','—')} @ {when}\n"
            f"  result: {st.get('result','—')}  ({legs_txt})\n"
            f"  in flight: {inflight}")
    except Exception as e:
        log.warning(f"handle_testfire_status failed (non-fatal): {e!r}")


def _prime_anchors_daypnl(trader):
    """E-23: rebuild the anchors realized day P&L from broker deal history BEFORE the
    preflight so rail 6 evaluates the anchors daily brake against the day's real number.
    The 07-09 defect was preflight clearing 4s BEFORE the rebuild landed, so the governor
    did not yet know the day was -$821 against its -$630 stop and a real straddle placed.
    READ-ONLY on the broker; idempotent; fully guarded (a failure leaves rail 6 to the live
    computed source, which -- post E-22 -- already reads magic_day_net directly from history).
    Also drops the per-tick computed-P&L cache so the first rail-6 read recomputes fresh."""
    try:
        import daystops as _ds
        dp = _ds.rebuild_anchors_day_pnl(trader)
        if dp is not None and isinstance(getattr(trader, 'state', None), dict):
            trader.state['daily_pnl'] = float(dp)
        _ds.invalidate_pnl_cache(trader)
        log.info(f"TESTFIRE preflight priming: anchors day P&L rebuilt "
                 f"${(dp if dp is not None else float('nan')):+.2f} (magic 20260522) "
                 f"before rail-6 evaluation.")
    except Exception as e:
        log.warning(f"testfire day-pnl rebuild before preflight failed (non-fatal): {e!r}")


def run_testfire(cfg, anchor='A2', force_window=False):
    """Build the live adapter + trader (same as run_live), run the fail-closed
    preflight, arm ONE manual entry, and hand off to the SAME live management loop.
    Returns True if the test-fire was armed and the loop ran; False (exit non-zero)
    if any safety rail refused. Mirrors run_selftest's adapter lifecycle.

    force_window (v3.3.1): bypasses ONLY rail 4 (the 30-min scheduled-anchor
    collision guard); rails 1/2/3/5 stay HARD. The bypass is announced loudly."""
    import sys as _sys, traceback as _tb
    adapter = None
    try:
        from mt5_adapter import MT5Adapter
        adapter = MT5Adapter(
            getattr(cfg, 'symbol', 'XAUUSD'),
            expected_offset_hours=getattr(cfg, 'EXPECTED_BROKER_OFFSET_HOURS', None))
        from live_trader import LiveTrader
        trader = LiveTrader(cfg, adapter, paper=False)

        # E-23: establish the day's anchors P&L truth BEFORE preflight so rail 6 (the
        # anchors daily-brake gate) sees the real number, not a cold un-rebuilt state.
        _prime_anchors_daypnl(trader)

        ok, reason = testfire_preflight(trader, force_window=force_window)
        print(f"🧪🔥 TESTFIRE preflight: {reason}", flush=True)
        # The rail-4 bypass is never silent: re-surface it as a standalone loud
        # banner (the reason string carries 'BYPASSED' only when it actually fired).
        if ok and force_window and 'BYPASSED' in reason:
            banner = ("⚠️⚠️ TESTFIRE --force-window: rail 4 (scheduled-anchor collision "
                      "guard) was BYPASSED by owner override. Rails 1/2/3 stayed HARD. "
                      "Scheduler is SUPPRESSED this session — no real anchor fires "
                      "alongside the test.")
            print(banner, flush=True)
            log.warning(banner)
            try:
                trader.tele.warn(banner)
            except Exception:
                pass
        try:
            (trader.tele.success if ok else trader.tele.error)(f"🧪🔥 TESTFIRE {reason}")
        except Exception:
            pass
        if not ok:
            return False

        # Disable scheduled-anchor placement for this session (rail 4 isolation) and
        # arm the manual entry; the run() loop completes it via the scheduled path.
        arm_testfire(trader, anchor)
        try:
            trader.tele.warn(
                f"🧪🔥 *TESTFIRE armed* [{anchor}] — placing ONE straddle at current "
                f"mid (+/-${getattr(cfg, 'trigger_dist', 5.0):.0f}, $"
                f"{getattr(cfg, 'sl_dist', 18.0):.0f} SL / ${getattr(cfg, 'tp_dist', 30.0):.0f} "
                f"TP, No-OCO). Scheduled anchors are SUPPRESSED this session. "
                f"Ctrl-C flattens. Tagged trigger_source=TESTFIRE in the journal.")
        except Exception:
            pass
        # SAME live management loop: places the deferred anchor, then trails/boosts/
        # freeze/telemetry manage it to a natural resolution. Scheduler is a no-op
        # because _testfire_mode is set (see anchors._process_anchor_if_due).
        trader.run()
        return True
    except BaseException:
        tb = _tb.format_exc()
        print("🧪🔥 TESTFIRE could not start — full traceback:\n" + tb,
              file=_sys.stderr, flush=True)
        log.error("run_testfire crashed:\n%s", tb)
        return False
    finally:
        if adapter is not None:
            try:
                adapter.shutdown()
            except Exception:
                pass
