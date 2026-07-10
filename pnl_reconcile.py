"""AUREON — P&L RECONCILIATION AUDIT (observer only; NEVER on the order path).

For a broker day, compute per-engine + account realized net from FOUR surfaces and prove they
agree with the AUTHORITY (MT5 deal history by magic). Any per-cell delta > $0.01 is a FAILURE.

  (a) AUTHORITY  -- MT5 deal history by magic (pnl_source.engine_day_nets). The truth.
  (b) LEDGER     -- run/reports/pnl_ledger.csv (the MT5-history-rebuilt persisted record).
  (c) REPORT     -- the daily report's engine sections (pnl_report.build_day_report).
  (d) LIVE       -- the day-P&L the stops/status read: the live governors for TODAY, else the
                    E-20 rebuild for a past day (both single-sourced via pnl_source).

CLI:  python bot.py reconcile --date 2026-07-08   (exit non-zero on any mismatch)
EOD:  run_and_alert(trader, date) runs automatically after the daily report; a mismatch posts
      a loud '⚠️ P&L RECONCILE MISMATCH' Discord card. It never silently passes.

Read-only: opens no orders, mutates no engine state. Fully guarded.
"""
from __future__ import annotations

import logging

log = logging.getLogger("AUREON")

TOL = 0.01                                   # any surface delta > 1 cent from authority = FAIL
ENGINES = ('anchors', 'rogue', 'fetcher', 'account')
SURFACES = ('authority', 'ledger', 'report', 'live')


def _f(v):
    """float or None (blank/garbage -> None so a missing surface is 'n/a', not 0)."""
    try:
        if v is None or v == '':
            return None
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


# --- (b) LEDGER: run/reports/pnl_ledger.csv --------------------------------------------
def ledger_nets(run_dir, date_str):
    """Per-engine net from pnl_ledger.csv for `date_str`: anchors = TOTAL.net, rogue =
    ROGUE.rogue_day_pnl, fetcher = FETCHER.fetcher_day_pnl. None (whole dict) if the file is
    absent; per-engine None when a scope row is missing. READ-ONLY; guarded."""
    import csv
    import os
    path = os.path.join(run_dir or '.', 'reports', 'pnl_ledger.csv')
    if not os.path.exists(path):
        return None
    try:
        with open(path, newline='', encoding='utf-8') as fh:
            rows = [r for r in csv.DictReader(fh) if str(r.get('date')) == str(date_str)]
        if not rows:
            return None
        by_scope = {str(r.get('scope')): r for r in rows}
        a = _f((by_scope.get('TOTAL') or {}).get('net'))
        r = _f((by_scope.get('ROGUE') or {}).get('rogue_day_pnl'))
        f = _f((by_scope.get('FETCHER') or {}).get('fetcher_day_pnl'))
        acct = None if None in (a, r, f) else round(a + r + f, 2)
        return {'anchors': a, 'rogue': r, 'fetcher': f, 'account': acct}
    except Exception as e:
        log.warning(f"pnl_reconcile: ledger read failed: {e!r}")
        return None


# --- (c) REPORT: pnl_report.build_day_report engine sections ---------------------------
def report_nets(trader, date_str, run_dir=None):
    """Per-engine net from the daily report: anchors = sum(per_anchor net), rogue/fetcher =
    each section's day_pnl. None on any failure. READ-ONLY; guarded."""
    try:
        import pnl_report as _pr
        # R-14: the report MUST read the SAME broker-day window as the authority
        # (pnl_source.broker_day_range uses trader.cfg.broker_tz_offset_hours), or
        # report vs authority would count different deal sets and never agree.
        off = float(getattr(getattr(trader, 'cfg', None), 'broker_tz_offset_hours',
                            _pr.DEFAULT_BROKER_TZ_OFFSET_HOURS) or 0.0)
        rep = _pr.build_day_report(trader.adapter, str(date_str),
                                   run_dir=run_dir or getattr(trader, 'run_dir', None),
                                   broker_tz_offset_hours=off)
        if not rep:
            return None
        a = round(sum(float(s.get('net', 0.0) or 0.0)
                      for s in (rep.get('per_anchor') or {}).values()), 2)
        r = _f((rep.get('rogue') or {}).get('day_pnl'))
        f = _f((rep.get('fetcher') or {}).get('day_pnl'))
        r = 0.0 if r is None else r
        f = 0.0 if f is None else f
        return {'anchors': a, 'rogue': r, 'fetcher': f, 'account': round(a + r + f, 2)}
    except Exception as e:
        log.warning(f"pnl_reconcile: report build failed: {e!r}")
        return None


# --- (d) LIVE: the day-P&L the stops/status read ---------------------------------------
def live_nets(trader, date_str):
    """The number the daily stops / status act on. For the CURRENT broker day with live
    governors present, that is the live accumulator (_engine_day_pnls) -- this is what catches
    a live drift from history. For a past day (or no live govs), it is the E-20 rebuild over
    that day's window (single-sourced via pnl_source, so it equals the authority by
    construction -- which PROVES the rebuild path the govs are seeded from). Guarded -> None."""
    try:
        import pnl_source as _ps
        today = str((getattr(trader, 'state', {}) or {}).get('last_broker_date', '') or '')
        is_today = (str(date_str) == today)
        if is_today and callable(getattr(trader, '_engine_day_pnls', None)):
            a, r, f = trader._engine_day_pnls()
            return {'anchors': round(a, 2), 'rogue': round(r, 2), 'fetcher': round(f, 2),
                    'account': round(a + r + f, 2)}
        # past day: rebuild each engine's gov/day-pnl from history for that day's window.
        dt_from, dt_to = _ps.broker_day_range(trader, day=date_str)
        import rogue as _rg
        import fetcher as _ft
        import daystops as _ds
        rg = _rg.rebuild_gov_from_history(trader, dt_from, dt_to)
        ft = _ft.rebuild_gov_from_history(trader, dt_from, dt_to)
        a = _ds.rebuild_anchors_day_pnl(trader, dt_from, dt_to)
        r = None if rg is None else _f(rg.get('day_pnl'))
        f = None if ft is None else _f(ft.get('day_pnl'))
        a = _f(a)
        acct = None if None in (a, r, f) else round(a + r + f, 2)
        return {'anchors': a, 'rogue': r, 'fetcher': f, 'account': acct}
    except Exception as e:
        log.warning(f"pnl_reconcile: live source failed: {e!r}")
        return None


# --- the audit -------------------------------------------------------------------------
def reconcile_day(trader, date_str, run_dir=None):
    """Compute the 4 surfaces for `date_str`, diff each against the AUTHORITY, and return
    {date, table, mismatches, ok, authority_available}. `ok` is True ONLY when the authority
    is available AND every present surface agrees within TOL. READ-ONLY; never raises."""
    run_dir = run_dir or getattr(trader, 'run_dir', './run')
    try:
        import pnl_source as _ps
        authority = _ps.engine_day_nets(trader, day=date_str)
    except Exception as e:
        log.warning(f"pnl_reconcile: authority unavailable: {e!r}")
        authority = None
    ledger = ledger_nets(run_dir, date_str)
    report = report_nets(trader, date_str, run_dir=run_dir)
    live = live_nets(trader, date_str)
    surf_vals = {'authority': authority, 'ledger': ledger, 'report': report, 'live': live}

    table = {}
    mismatches = []
    for eng in ENGINES:
        cell = {s: (surf_vals[s].get(eng) if isinstance(surf_vals[s], dict) else None)
                for s in SURFACES}
        table[eng] = cell
        auth = cell['authority']
        if auth is None:
            continue
        for s in ('ledger', 'report', 'live'):
            v = cell[s]
            if v is not None and abs(v - auth) > TOL:
                mismatches.append({'engine': eng, 'surface': s, 'value': v,
                                   'authority': auth, 'delta': round(v - auth, 2)})
    ok = (authority is not None) and (len(mismatches) == 0)
    return {'date': str(date_str), 'table': table, 'mismatches': mismatches,
            'ok': ok, 'authority_available': authority is not None}


# --- rendering -------------------------------------------------------------------------
def _fmt(v):
    return 'n/a' if v is None else f"{v:+.2f}"


def render_text(result):
    """A fixed-width table of all four surfaces + deltas for the console / log. PURE."""
    lines = [f"P&L RECONCILE — {result['date']}  "
             f"({'PASS' if result['ok'] else 'FAIL'})",
             f"{'engine':<9} {'authority':>11} {'ledger':>11} {'report':>11} {'live':>11}"]
    for eng in ENGINES:
        c = result['table'].get(eng, {})
        lines.append(f"{eng:<9} {_fmt(c.get('authority')):>11} {_fmt(c.get('ledger')):>11} "
                     f"{_fmt(c.get('report')):>11} {_fmt(c.get('live')):>11}")
    if not result['authority_available']:
        lines.append("  ! AUTHORITY (MT5 history) UNAVAILABLE — cannot reconcile.")
    for m in result['mismatches']:
        lines.append(f"  ! {m['surface'].upper()} {m['engine']} {m['value']:+.2f} vs "
                     f"authority {m['authority']:+.2f}  (Δ {m['delta']:+.2f})")
    return "\n".join(lines)


def card(result):
    """A Discord embed for a reconcile result -- a loud RED mismatch card, or a quiet GREEN
    receipt when clean. Guarded (import discord_cards lazily). Returns a dict or None."""
    try:
        import discord_cards as dc
        clean = result['ok']
        title = ("✅ P&L reconciled — all surfaces agree" if clean
                 else "⚠️ P&L RECONCILE MISMATCH")
        color = dc.GREEN if clean else dc.RED
        fields = []
        for eng in ENGINES:
            c = result['table'].get(eng, {})
            fields.append((eng.capitalize(),
                           f"auth {_fmt(c.get('authority'))} · ledger {_fmt(c.get('ledger'))} "
                           f"· report {_fmt(c.get('report'))} · live {_fmt(c.get('live'))}",
                           False))
        desc = None
        if not clean:
            if not result['authority_available']:
                desc = "MT5 deal history (the authority) was unavailable — cannot reconcile."
            else:
                desc = " · ".join(
                    f"{m['surface']} {m['engine']} Δ{m['delta']:+.2f}"
                    for m in result['mismatches'][:8])
        return dc.build_embed(f"{title} — {result['date']}", color,
                              fields=fields, description=desc)
    except Exception:
        return None


# --- backfill: reconcile a range + a corrections table ---------------------------------
def reconcile_range(trader, dates, run_dir=None):
    """Reconcile every broker day in `dates` (a list of 'YYYY-MM-DD' strings). Returns the
    list of per-day result dicts (skipping days with no authority/deal history). READ-ONLY."""
    out = []
    for d in dates:
        res = reconcile_day(trader, d, run_dir=run_dir)
        # keep only days that actually had trading history (authority present + any surface)
        if res.get('authority_available'):
            auth = res['table'].get('account', {}).get('authority')
            has_trades = any(v not in (None, 0.0)
                             for c in res['table'].values() for v in c.values())
            if auth is not None and (has_trades or res['mismatches']):
                out.append(res)
    return out


def corrections_table(results):
    """PURE: from a list of reconcile results, every (day, engine, surface) where a surface
    the operator saw (report / ledger / live) DIFFERED from the MT5 authority, with the
    corrected figure. This is the record the ledger should be updated to -- historical reports
    are NOT rewritten. Empty list == every surface already agreed with MT5 on every day."""
    rows = []
    for res in results:
        for eng in ENGINES:
            c = res['table'].get(eng, {})
            auth = c.get('authority')
            if auth is None:
                continue
            for surf in ('report', 'ledger', 'live'):
                v = c.get(surf)
                if v is not None and abs(v - auth) > TOL:
                    rows.append({'date': res['date'], 'engine': eng, 'surface': surf,
                                 'claimed': v, 'corrected': auth, 'delta': round(v - auth, 2)})
    return rows


def render_corrections(results):
    """A human table of the corrections (claimed vs corrected per day/engine/surface). PURE."""
    rows = corrections_table(results)
    if not results:
        return "P&L BACKFILL — no days with trading history in range (nothing to reconcile)."
    head = [f"P&L BACKFILL CORRECTIONS — {len(results)} day(s) reconciled",
            f"{'date':<12}{'engine':<9}{'surface':<8}{'claimed':>11}{'corrected':>11}{'delta':>11}"]
    if not rows:
        head.append("  ALL SURFACES AGREE WITH MT5 ON EVERY DAY — no corrections needed.")
        return "\n".join(head)
    for r in rows:
        head.append(f"{r['date']:<12}{r['engine']:<9}{r['surface']:<8}"
                    f"{r['claimed']:>+11.2f}{r['corrected']:>+11.2f}{r['delta']:>+11.2f}")
    return "\n".join(head)


def _month_days(month_str):
    """['YYYY-MM-DD', ...] for a 'YYYY-MM' string, or [month_str] if it's already a day."""
    try:
        import calendar
        parts = str(month_str).split('-')
        if len(parts) == 2:
            y, m = int(parts[0]), int(parts[1])
            n = calendar.monthrange(y, m)[1]
            return [f"{y:04d}-{m:02d}-{d:02d}" for d in range(1, n + 1)]
    except Exception:
        pass
    return [str(month_str)]


# --- EOD hook + CLI --------------------------------------------------------------------
def run_and_alert(trader, date_str):
    """Run the audit at EOD (after the daily report) and POST a Discord card iff it MISMATCHES
    (loud '⚠️ P&L RECONCILE MISMATCH', naming each surface + delta). A clean day logs a quiet
    line and posts nothing (no channel spam). Returns the result dict. Guarded; never raises
    onto the EOD path."""
    result = {'ok': True, 'mismatches': [], 'date': str(date_str)}
    try:
        result = reconcile_day(trader, date_str)
        log.info(render_text(result))
        if not result['ok']:
            try:
                c = card(result)
                if c is not None:
                    from telemetry import Severity
                    trader.tele.send("⚠️ P&L RECONCILE MISMATCH — a P&L surface disagrees with "
                                     "MT5 deal history; see the card.", Severity.ERROR,
                                     card=c, important=True,
                                     event_key=f"reconcile:{result['date']}")
                else:
                    trader.tele.warn("⚠️ P&L RECONCILE MISMATCH on " + str(date_str))
            except Exception:
                pass
    except Exception as e:
        log.warning(f"pnl_reconcile.run_and_alert non-fatal: {e!r}")
    return result


def run_cli(date_arg=None):
    """`python bot.py reconcile --date YYYY-MM-DD`. Builds its own MT5 adapter (mirrors
    pnl_report.run_dailyreport), prints the table, and returns an EXIT CODE: 0 = all agree,
    1 = a mismatch (or the authority was unavailable). Guarded -> 2 on a setup error."""
    try:
        import types
        from config import Config
        from mt5_adapter import MT5Adapter
        cfg = Config()
        try:
            # R-12 (2026-07-09): MT5Adapter takes the SYMBOL STRING and connects in __init__ --
            # there is NO .connect() method. The prior `MT5Adapter(cfg)` + `adapter.connect()`
            # raised AttributeError on every run, so this CLI (whose whole job is to prove the
            # P&L surfaces agree) had NEVER executed. Canonical idiom: pnl_report.run_dailyreport.
            adapter = MT5Adapter(getattr(cfg, 'symbol', 'XAUUSD'))
        except Exception as e:
            print(f"reconcile: could not connect MT5 adapter: {e!r}")
            return 2
        date_str = date_arg
        if not date_str:
            import pandas as _pd
            off = float(getattr(cfg, 'broker_tz_offset_hours', 0.0) or 0.0)
            date_str = str((_pd.Timestamp.now(tz='UTC') + _pd.Timedelta(hours=off)).date())
        import os
        # The namespace carries adapter + cfg, which is ALL the `live` surface's past-day
        # rebuilds need (rogue/fetcher rebuild_gov_from_history + daystops.rebuild_anchors_day_pnl
        # + pnl_source.broker_day_range each read only trader.adapter.mt5.history_deals_get and
        # trader.cfg). last_broker_date='' forces live_nets down the past-day HISTORY-rebuild path
        # for every date (the CLI has no running bot's in-memory govs), so `live` == authority by
        # construction and returns n/a (never a silent 0.0) only if the history query itself fails.
        trader = types.SimpleNamespace(
            adapter=adapter, cfg=cfg, run_dir=os.environ.get('AUREON_RUN_DIR', './run'),
            state={'last_broker_date': ''})
        # A 'YYYY-MM' arg runs the whole month as a BACKFILL (corrections table); a single
        # 'YYYY-MM-DD' runs one day's full four-surface table.
        if len(str(date_str).split('-')) == 2:
            results = reconcile_range(trader, _month_days(date_str))
            print(render_corrections(results))
            ok = all(r['ok'] for r in results)
        else:
            result = reconcile_day(trader, date_str)
            print(render_text(result))
            ok = result['ok']
        try:
            adapter.shutdown()
        except Exception:
            pass
        return 0 if ok else 1
    except Exception as e:
        print(f"reconcile: setup error: {e!r}")
        return 2
