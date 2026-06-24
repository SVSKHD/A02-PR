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

SAFETY (fail-closed; rails 1/2/3/5 mandatory, no override):
  1. DEMO ONLY     — account_info.trade_mode must be ACCOUNT_TRADE_MODE_DEMO.
  2. NOT FP/funded — account_profile must be STANDARD_5PCT (FPZERO_1PCT and any
                     other profile are refused even on demo).
  3. FLAT BOOK     — no broker positions/pendings and no internal shadow state
                     (same flatness guard selftest uses); a real anchor in-flight
                     blocks the test.
  4. NO COLLISION  — no scheduled anchor active or within testfire_collision_min.
                     v3.3.1: this is the ONLY bypassable rail — `--force-window`
                     skips it (loud warning, never silent) so the owner can test
                     off-schedule. Rails 1/2/3/5 are NEVER bypassable.
  5. ONE AT A TIME — refuse if a prior test-fire event is still open.

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
    window to clear. Rails 1/2/3/5 stay HARD and are unaffected — there is no
    override for DEMO-ONLY, NO-FP, FLAT-BOOK, or ONE-AT-A-TIME. When the bypass
    actually fires (a scheduled anchor is within the guard) the returned reason is
    a LOUD warning naming how many minutes the nearest anchor is away; the bypass
    is never silent."""
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

    # Rail 4: never collide with a scheduled anchor (active or within N minutes).
    # This is the ONLY bypassable rail. With --force-window the owner can fire
    # off-schedule even inside the guard; rails 1/2/3/5 above already ran and still
    # refuse their cases. The bypass is LOUD (warning names minutes-away) and safe:
    # the scheduler is SUPPRESSED for the whole testfire session (_testfire_mode
    # gates _process_anchor_if_due), so the test event owns the book — the real
    # scheduled anchor will NOT also place alongside it while the test is live.
    n = int(collision_min if collision_min is not None
            else getattr(cfg, 'testfire_collision_min', 30))
    now = now_utc if now_utc is not None else pd.Timestamp.now(tz='UTC')
    near = minutes_to_nearest_anchor(cfg, now)
    if near is not None and near <= n:
        if force_window:
            return True, (f"⚠️⚠️ CLEARED [rail 4 NO-COLLISION BYPASSED via --force-window]: "
                          f"a scheduled anchor is {near:.0f} min away (<= {n} min guard) — "
                          f"firing OFF-SCHEDULE anyway by owner override. The scheduler is "
                          f"SUPPRESSED for this testfire session, so the real anchor will "
                          f"NOT also fire while the test event is live (the test owns the "
                          f"book). Rails 1/2/3 (DEMO-ONLY, NO-FP, FLAT-BOOK) stay HARD.")
        return False, (f"REFUSED [rail 4 NO-COLLISION]: a scheduled anchor is "
                       f"{near:.0f} min away (<= {n} min guard). Never let a test-fire "
                       f"collide with a real anchor — wait until the window is clear, or "
                       f"pass --force-window to fire off-schedule (rails 1/2/3 still apply).")

    return True, (f"CLEARED: demo account, STANDARD_5PCT profile, flat book, no "
                  f"scheduled anchor within {n} min "
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
