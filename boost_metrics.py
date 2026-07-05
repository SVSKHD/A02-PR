"""AUREON v3.5.0 — boost measurement utilities (features 8-11).

READ-ONLY / ALERT-ONLY. None of this touches order flow: the pure builders below
construct artifacts (counts JSON, ledger rows, a markdown report, a preflight summary)
and the thin live writers are each guarded by their own flag + try/except so a telemetry
failure can never break trading. These are the keystone measurement for the keep-vs-delete
decision: flip the CORE features on, let 8-11 collect, judge each at month-end.

  8  util_pullback_log  -> per-anchor armed/pulled-back/entered/skipped (daily JSON)
  9  util_boost_ledger  -> every boost event (arm/fire/skip px, P&L) appended to a CSV
 10  util_daily_report  -> per-anchor markdown report from the trades CSV (read-only)
 11  util_preflight     -> boot self-check (offset detected / anchors / flags / market)
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("AUREON")

# ---- 8: pullback-frequency counts (PURE) ----------------------------------------
PULLBACK_EVENTS = ('armed', 'pulled_back', 'entered', 'skipped')


def pullback_bump(counts: dict, anchor, kind, event) -> dict:
    """Increment the per-(anchor, kind) counter for one event. PURE: mutates and
    returns `counts`. Unknown events are ignored (no crash)."""
    key = f"{anchor}:{kind}"
    c = counts.setdefault(key, {e: 0 for e in PULLBACK_EVENTS})
    if event in c:
        c[event] += 1
    return counts


def pullback_json(counts: dict, date_str) -> str:
    """Serialize the counts to a stable, sorted JSON string (the daily log body)."""
    return json.dumps({'date': date_str, 'counts': counts}, sort_keys=True, indent=2)


# ---- 9: boost ledger (PURE) -----------------------------------------------------
# v3.6.0: seed_source appended LAST (ROGUE rows carry A1_ANCHOR | A1_TIME_SNAPSHOT |
# MARKET_OPEN | MANUAL; anchor-engine boost rows leave it '') so the July evidence
# stays segmentable per seed source (D-8). Appending keeps an existing ledger file's
# positional columns valid -- an old header simply lacks the tail column.
LEDGER_COLUMNS = ('ts', 'anchor', 'kind', 'event', 'arm_px', 'entry_px',
                  'exit_px', 'pnl_usd', 'seed_source')


def ledger_row(event: dict) -> list:
    """One ledger CSV row from an event dict, in LEDGER_COLUMNS order. Missing keys
    render as '' (never raises). PURE."""
    def _v(k):
        v = event.get(k)
        return '' if v is None else v
    return [_v(k) for k in LEDGER_COLUMNS]


# ---- 10: daily analysis report (PURE) -------------------------------------------
def daily_report_md(trades_rows, date_str, rogue_rows=None, open_anchors=None) -> str:
    """Build a per-anchor markdown report from trades rows (dicts with 'anchor' and a
    numeric 'pnl'/'realized_pnl_usd'/'max_favorable'... we read 'anchor' + 'pnl').
    `rogue_rows` (optional): closed Rogue events (dicts with a numeric 'pnl') rendered
    as their own "Rogue" section, since Rogue is a separate engine never mixed into
    the per-anchor table. `open_anchors` (optional): anchor codes with a position
    still open at report time -- listed as "pending-open" so a zero-leg anchor can't
    be misread as "no trades" when it simply hasn't closed yet. Read-only: it only
    formats what it is given. PURE."""
    per = {}
    for r in (trades_rows or []):
        a = str(r.get('anchor', '?'))[:2] or '?'
        try:
            pnl = float(r.get('pnl', r.get('realized_pnl_usd', 0.0)) or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        agg = per.setdefault(a, {'legs': 0, 'net': 0.0})
        agg['legs'] += 1
        agg['net'] += pnl
    lines = [f"# AUREON daily report — {date_str}", "", "| anchor | legs | net |",
             "|---|---:|---:|"]
    day_net = 0.0
    for a in sorted(per):
        agg = per[a]
        day_net += agg['net']
        lines.append(f"| {a} | {agg['legs']} | ${agg['net']:+.2f} |")
    for a in sorted(set(open_anchors or ()) - set(per)):
        lines.append(f"| {a} | pending-open | — |")
    lines += ["", f"**Day net: ${day_net:+.2f}**  ({sum(v['legs'] for v in per.values())} legs)"]

    rogue_rows = rogue_rows or []
    rg_net = 0.0
    for r in rogue_rows:
        try:
            rg_net += float(r.get('pnl', 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
    lines += ["", "## Rogue", "",
              f"**Rogue: {len(rogue_rows)} closes ${rg_net:+.2f}**"]

    if open_anchors:
        lines += ["", f"Open (not yet closed): {', '.join(sorted(open_anchors))}"]
    return "\n".join(lines)


# ---- 11: pre-flight self-check (PURE) -------------------------------------------
def preflight_lines(offset, anchors_today, flags: dict, market_open) -> tuple:
    """Build the boot self-check report. Returns (ok, lines). NOT ok (abort-with-alert)
    when the broker offset is undetected (None) -- never trade on a guessed offset.
    PURE: the caller gathers offset/anchors/flags/market and renders/aborts."""
    ok = True
    lines = ["🛫 AUREON preflight:"]
    if offset is None:
        ok = False
        lines.append("  ❌ broker offset UNDETECTED (None) — ABORT (never trade on a 0h guess)")
    else:
        lines.append(f"  ✅ broker offset +{int(offset)}h detected")
    lines.append(f"  ✅ anchors scheduled today: {len(anchors_today or [])} "
                 f"({', '.join(str(a) for a in (anchors_today or []))})")
    if not market_open:
        lines.append("  ⏸ market CLOSED (will sleep until open)")
    else:
        lines.append("  ✅ market open")
    on = sorted(k for k, v in (flags or {}).items() if v)
    off = sorted(k for k, v in (flags or {}).items() if not v)
    lines.append(f"  flags ON:  {', '.join(on) if on else '(none)'}")
    lines.append(f"  flags OFF: {', '.join(off) if off else '(none)'}")
    return ok, lines


# ---- thin LIVE writers (each flag-guarded; never raise onto the caller) ----------
def _safe_dir(trader):
    try:
        return trader._journal_dir()
    except Exception:
        return os.getcwd()


def record_pullback_event(trader, anchor, kind, event):
    """feature 8 live hook: accumulate counts on the trader and (re)write the daily
    JSON. Guarded by util_pullback_log; never raises onto the order path."""
    try:
        if not bool(getattr(trader.cfg, 'util_pullback_log', True)):
            return
        counts = getattr(trader, '_pullback_counts', None)
        if counts is None:
            counts = {}
            trader._pullback_counts = counts
        pullback_bump(counts, anchor, kind, event)
        import pandas as _pd
        day = _pd.Timestamp.now(tz='Asia/Kolkata').strftime('%Y-%m-%d')
        path = os.path.join(_safe_dir(trader), f"pullback_log_{day}.json")
        with open(path, 'w') as f:
            f.write(pullback_json(counts, day))
    except Exception as e:
        log.warning(f"util_pullback_log non-fatal: {e!r}")


PREFLIGHT_FLAG_KEYS = (
    'override_entry_enabled', 'rescue_entry_enabled', 'entry_confirm_candle',
    'entry_adaptive_depth', 'rescue_sl_wide', 'util_pullback_log', 'util_boost_ledger',
    'util_daily_report', 'util_preflight', 'fix_boost_telemetry', 'fix_a1_offset')


def run_preflight(trader):
    """feature 11 live hook: gather offset / anchors / flags / market on boot and emit
    the preflight report. ALERT-ONLY -- it surfaces an undetected offset loudly but does
    NOT itself gate placement (the pre-existing adapter offset guard is the real block,
    so this utility never touches order flow). Guarded by util_preflight; returns the
    (ok, lines) it reported, or (True, []) when disabled / on error."""
    try:
        if not bool(getattr(trader.cfg, 'util_preflight', True)):
            return True, []
        offset = getattr(getattr(trader, 'adapter', None), 'tick_time_offset_hours', None)
        anchors = [a[0] for a in getattr(trader.cfg, 'anchors', [])]
        flags = {k: bool(getattr(trader.cfg, k)) for k in PREFLIGHT_FLAG_KEYS
                 if hasattr(trader.cfg, k)}
        try:
            market_open = not trader._market_closed_now()
        except Exception:
            market_open = True
        ok, lines = preflight_lines(offset, anchors, flags, market_open)
        for ln in lines:
            log.info(ln)
        try:
            trader.tele.info("\n".join(lines))
        except Exception:
            pass
        return ok, lines
    except Exception as e:
        log.warning(f"util_preflight non-fatal: {e!r}")
        return True, []


def run_daily_report(trader, date_str=None):
    """feature 10 live hook: read the day's rows out of the month's trades CSV and the
    boost ledger, and write a per-anchor + Rogue markdown report. Read-only on the
    trades/ledger data; guarded by util_daily_report; never raises."""
    try:
        if not bool(getattr(trader.cfg, 'util_daily_report', True)):
            return None
        import csv as _csv
        import pandas as _pd
        if date_str is None:
            date_str = _pd.Timestamp.now(tz='Asia/Kolkata').strftime('%Y-%m-%d')
        jdir = _safe_dir(trader)
        src = os.path.join(jdir, f"trades_{date_str[:7]}.csv")
        rows = []
        if os.path.exists(src):
            with open(src) as f:
                for r in _csv.DictReader(f):
                    if r.get('date_ist') != date_str:
                        continue
                    rows.append({'anchor': r.get('anchor'),
                                 'pnl': r.get('realized_pnl_usd', r.get('pnl', 0))})
        rogue_rows = load_rogue_closes(jdir, date_str)
        open_anchors = open_position_anchors(trader)
        md = daily_report_md(rows, date_str, rogue_rows=rogue_rows,
                             open_anchors=open_anchors)
        out = os.path.join(jdir, f"daily_report_{date_str}.md")
        with open(out, 'w') as f:
            f.write(md)
        return out
    except Exception as e:
        log.warning(f"util_daily_report non-fatal: {e!r}")
        return None


def load_rogue_closes(jdir, date_str):
    """Impure: closed Rogue events (kind=ROGUE, event=exit) for `date_str` out of
    boost_ledger.csv, as [{'pnl': float}, ...]. `ts` is an ISO-8601 UTC timestamp
    (see append_ledger callers), but `date_str` is an IST broker/calendar day (same
    convention as journal.py's date_ist) -- so `ts` is converted to Asia/Kolkata
    before its date is compared, not sliced as a raw UTC prefix. A close at, say,
    18:45 UTC lands on the NEXT IST calendar day and must bucket there.
    Missing file / rows -> []. Never raises."""
    import csv as _csv
    import pandas as _pd
    path = os.path.join(jdir, "boost_ledger.csv")
    out = []
    if not os.path.exists(path):
        return out
    try:
        with open(path) as f:
            for r in _csv.DictReader(f):
                if r.get('kind') != 'ROGUE' or r.get('event') != 'exit':
                    continue
                ts = r.get('ts') or ''
                if not ts:
                    continue
                try:
                    tsp = _pd.Timestamp(ts)
                    if tsp.tzinfo is None:
                        tsp = tsp.tz_localize('UTC')
                    ts_date_ist = tsp.tz_convert('Asia/Kolkata').strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    continue
                if ts_date_ist != date_str:
                    continue
                try:
                    pnl = float(r.get('pnl_usd') or 0.0)
                except (TypeError, ValueError):
                    pnl = 0.0
                out.append({'pnl': pnl})
    except (OSError, _csv.Error) as e:
        log.warning(f"util_daily_report: boost_ledger.csv read failed: {e!r}")
        return []
    return out


def open_position_anchors(trader):
    """Impure: 2-char anchor codes with a position still open on `trader` right now
    (e.g. A4/A5 late-day anchors that haven't closed by report time) -- so the report
    can mark them "pending-open" instead of silently showing zero legs. Reads
    `trader.shadow_positions` only; never raises."""
    try:
        return sorted({str(sp.get('anchor_label', ''))[:2]
                       for sp in getattr(trader, 'shadow_positions', {}).values()
                       if sp.get('anchor_label')})
    except Exception:
        return []


def append_ledger(trader, event: dict):
    """feature 9 live hook: append one boost event to ledger.csv. Guarded by
    util_boost_ledger; never raises onto the order path."""
    try:
        if not bool(getattr(trader.cfg, 'util_boost_ledger', True)):
            return
        import csv
        path = os.path.join(_safe_dir(trader), "boost_ledger.csv")
        new = not os.path.exists(path)
        with open(path, 'a', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(LEDGER_COLUMNS)
            w.writerow(ledger_row(event))
    except Exception as e:
        log.warning(f"util_boost_ledger non-fatal: {e!r}")
