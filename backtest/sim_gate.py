"""AUREON simulator — THE GATE (reconcile sim vs the MT5 DEAL-EXPORT truth).

The gate validates the simulator against the MT5 deal export's COMMENT column --
NOT against pnl_report (a different source; this week proved they disagree: the
report claimed anchors +$1,728 for July while the deal comments say +$605.61 /
+$587.41 account). Bucketing is by each OUT deal's AUR_* comment, reusing
pnl_report.classify_comment so sim and export are bucketed identically.

The gate PASSES only when the simulator reproduces the truth within tolerance on
REAL ticks. Until then every artifact carries GATE-NOT-RUN and NO number is
trustworthy. DO NOT tune the simulator to match -- explain the gap.

!!! GATE-NOT-RUN — baseline never reproduced against MT5 truth.
!!! No number in this file is trustworthy.
"""
from __future__ import annotations

import os
import sys

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
for _p in (_ROOT, _THIS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pnl_report as pr
import sim_common as sc

# The MT5 deal-export truth: 200 comment-labelled positions, 2026-07-01..07-10.
TRUTH = {
    'A1': 1075.57, 'A2': -685.65, 'A3': -385.35, 'A4': 682.15, 'A5': -75.60,
    'ST': -5.51, 'ROGUE': 168.00, 'FETCH': -186.20,
}
TRUTH_TOTAL = 587.41
BUCKETS = ('A1', 'A2', 'A3', 'A4', 'A5', 'ST', 'ROGUE', 'FETCH')
TOL = 0.01


def bucket_of(comment, magic):
    """Which truth bucket an OUT deal belongs to, from its comment+magic (reusing
    the live classifier). anchors-magic legs that don't resolve to A1..A5 -> ST."""
    c = pr.classify_comment(comment, magic)
    if c['engine'] == pr.ROGUE_ENGINE:
        return 'ROGUE'
    if c['engine'] == pr.FETCHER_ENGINE:
        return 'FETCH'
    a = c['anchor2']
    if a in ('A1', 'A2', 'A3', 'A4', 'A5'):
        return a
    return 'ST'   # anchors-magic seed / testfire / scalp / unattributable


def _deal_realized(d):
    return (float(getattr(d, 'profit', 0.0) or 0.0)
            + float(getattr(d, 'swap', 0.0) or 0.0)
            + float(getattr(d, 'commission', 0.0) or 0.0))


# --- UNREPRODUCIBLE-EVENT EXCLUSION (owner decision 2026-07-10) -----------------
# These events cannot be reproduced by an automatic replay, so they are EXCLUDED
# from the gate and the reproducible subset is reconciled instead (NOT absorbed
# into a tolerance):
#   - ST bucket = 34 testfire legs fired BY HAND (`python bot.py testfire`; no rule
#     generates them) -> excluded entirely.
#   - /rogueseed + /fetchseed fired by the owner at 14:34 server on 2026-07-07
#     (again ~14:58 after a restart), seed_source=MANUAL. They re-anchored both
#     engines at the current tick; only legs OPENED at/after 14:34 chain off the
#     manual seed and diverge from the automatic A1 seed. Rogue & Fetcher traded
#     CLEANLY all morning from the A1 seed, so that morning is REPRODUCIBLE and must
#     stay IN -- excluding the whole day discarded ~5 hours of good trades for no
#     reason. The carve is therefore by ENTRY TIMESTAMP, not by day.
#
# The 14:34:00 boundary is the owner's manual-seed time, read from the live seed
# log (run/rogue_trades.csv / run/fetcher_trades.csv, seed_source==MANUAL). Those
# CSVs are gitignored (VPS-only; .gitignore run/ + rogue_trades.csv/fetcher_trades.csv),
# so the value lives here as a documented constant -- it is NOT grepped from a file
# committed to the repo. On the VPS, verify it against the MANUAL rows before a run.
EXCLUDED_BUCKETS = {'ST'}
MANUAL_SEED_ENGINES = {'ROGUE', 'FETCH'}
MANUAL_SEED_CUTOFF_BROKER = '2026-07-07 14:34:00'   # server/broker wall time
REPRODUCIBLE_BUCKETS = tuple(b for b in BUCKETS if b not in EXCLUDED_BUCKETS)


def _manual_seed_cutoff_epoch():
    """The 14:34 cutoff as a broker-epoch int, matching how deal `time`/`entry_time`
    are stored (broker wall-clock interpreted as a UTC epoch -- the sim's
    _broker_epoch and the export parser agree on this convention)."""
    import pandas as pd
    return int(pd.Timestamp(MANUAL_SEED_CUTOFF_BROKER, tz='UTC').timestamp())


def _entry_epoch_map(all_deals):
    """position_id -> earliest deal epoch = the IN (open) leg's time. MT5 (and the
    fake broker) write an IN deal and an OUT deal sharing one position_id; the IN
    leg is opened first, so its time is the position's entry timestamp."""
    m = {}
    for d in all_deals:
        pid = getattr(d, 'position_id', None)
        if pid is None:
            continue
        t = int(getattr(d, 'time', 0) or 0)
        if not t:
            continue
        m[pid] = min(m[pid], t) if pid in m else t
    return m


def attach_entry_times(all_deals):
    """Return the OUT (entry==1) deals, each annotated with `.entry_time` (broker
    epoch of the position's OPEN), paired from the IN leg via position_id. If a deal
    already carries `.entry_time` (the export parser sets it) it is kept; if no IN
    leg is available the deal's own close time is used (conservative fallback)."""
    emap = _entry_epoch_map(all_deals)
    outs = []
    for d in all_deals:
        if getattr(d, 'entry', None) != 1:
            continue
        if getattr(d, 'entry_time', None) in (None, 0):
            pid = getattr(d, 'position_id', None)
            d.entry_time = emap.get(pid) or int(getattr(d, 'time', 0) or 0)
        outs.append(d)
    return outs


def _leg_entry_epoch(d):
    return int(getattr(d, 'entry_time', 0) or getattr(d, 'time', 0) or 0)


def is_excluded_leg(d):
    """True iff this closing leg is UNREPRODUCIBLE: the ST bucket, or a ROGUE/FETCH
    leg OPENED at/after the 14:34 manual-seed cutoff on 2026-07-07."""
    b = bucket_of(getattr(d, 'comment', ''), getattr(d, 'magic', 0))
    if b in EXCLUDED_BUCKETS:
        return True
    if b in MANUAL_SEED_ENGINES:
        et = _leg_entry_epoch(d)
        if et and et >= _manual_seed_cutoff_epoch():
            return True
    return False


def reproducible_nets(out_deals):
    """{bucket: net} over the REPRODUCIBLE subset: excluding the ST bucket and any
    ROGUE/FETCH leg opened at/after the 14:34 manual-seed cutoff. Callers must have
    annotated `.entry_time` first (attach_entry_times / the export parser). Returns
    only REPRODUCIBLE_BUCKETS."""
    nets = {b: 0.0 for b in REPRODUCIBLE_BUCKETS}
    for d in out_deals:
        if is_excluded_leg(d):
            continue
        b = bucket_of(getattr(d, 'comment', ''), getattr(d, 'magic', 0))
        if b not in nets:
            continue
        nets[b] = round(nets.get(b, 0.0) + _deal_realized(d), 2)
    return {b: round(v, 2) for b, v in nets.items()}


def excluded_breakdown(out_deals):
    """{'ST': net, 'MANUAL_SEED': net, 'total': net} over the EXCLUDED legs, so the
    gate can STATE the excluded total on every run (owner requirement). Callers must
    have annotated `.entry_time` first."""
    st = ms = 0.0
    cutoff = _manual_seed_cutoff_epoch()
    for d in out_deals:
        b = bucket_of(getattr(d, 'comment', ''), getattr(d, 'magic', 0))
        if b in EXCLUDED_BUCKETS:
            st = round(st + _deal_realized(d), 2)
        elif b in MANUAL_SEED_ENGINES and _leg_entry_epoch(d) >= cutoff and _leg_entry_epoch(d):
            ms = round(ms + _deal_realized(d), 2)
    return {'ST': round(st, 2), 'MANUAL_SEED': round(ms, 2), 'total': round(st + ms, 2)}


def bucket_nets(out_deals):
    """{bucket: net} over ALL closing deals (every bucket) -- reference/reporting."""
    nets = {b: 0.0 for b in BUCKETS}
    for d in out_deals:
        b = bucket_of(getattr(d, 'comment', ''), getattr(d, 'magic', 0))
        nets[b] = round(nets.get(b, 0.0) + _deal_realized(d), 2)
    return {b: round(v, 2) for b, v in nets.items()}


def sim_bucket_nets(deals):
    return bucket_nets([d for d in deals if getattr(d, 'entry', None) == 1])


# --------------------------------------------------------------------------- #
# deal-export parsing (flexible: MT5 history export CSV or XLSX)
# --------------------------------------------------------------------------- #
def _epoch_broker(s):
    """Parse an MT5 timestamp ('YYYY.MM.DD HH:MM:SS', broker/server wall time) into
    a broker epoch int (wall time read as UTC), matching the sim's _broker_epoch
    convention. Returns 0 on failure."""
    import pandas as _pd
    txt = str(s or '').strip()
    if not txt:
        return 0
    try:
        ts = _pd.Timestamp(txt.replace('.', '-', 2))
        if ts.tzinfo is None:
            ts = ts.tz_localize('UTC')
        return int(ts.timestamp())
    except Exception:
        return 0


def _read_export_rows(path):
    """[dict] rows from a deal export -- .xlsx (the committed fixture,
    backtest/fixtures/deal_export_2026-07.xlsx) or .csv. Returns None if the file is
    absent/unreadable, [] if empty."""
    if not path or not os.path.exists(path):
        return None
    low = path.lower()
    if low.endswith(('.xlsx', '.xls')):
        try:
            import pandas as _pd
            df = _pd.read_excel(path)      # needs openpyxl for .xlsx
            return df.to_dict('records')
        except Exception:
            return None
    import csv
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            return list(csv.DictReader(f))
    except Exception:
        return None


def parse_deal_export(path):
    """Parse an MT5 deal export (CSV or XLSX) into pseudo-deal namespaces: entry==1
    OUT rows carrying .comment/.magic/.profit/.swap/.commission, .time (close epoch),
    .position_id and .entry_time (OPEN epoch). The entry timestamp is what the 14:34
    manual-seed carve keys on, taken (in order of reliability) from an explicit
    open-time column, else by pairing the IN leg via position_id, else the row's own
    close time. Tolerant of column naming; returns None if absent/unparseable."""
    import types
    rows = _read_export_rows(path)
    if rows is None:
        return None
    if not rows:
        return []
    cols = {str(c).lower().strip(): c for c in rows[0].keys()}

    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None
    c_comment = col('comment', 'comments')
    c_profit = col('profit', 'pnl', 'net')
    c_swap = col('swap')
    c_comm = col('commission', 'fee', 'commissions')
    c_magic = col('magic')
    c_entry = col('entry', 'direction')
    c_time = col('time', 'close_time', 'time_close', 'closetime', 'date')
    c_open = col('open_time', 'time_open', 'opentime', 'entry_time', 'time_entry',
                 'open', 'entry_ts_server')
    c_pos = col('position', 'position_id', 'positionid', 'position id', 'deal_position')

    def num(r, cn):
        try:
            return float(str(r.get(cn, '') or '0').replace(',', ''))
        except (TypeError, ValueError):
            return 0.0

    def is_out_row(r):
        # MT5 marks the closing deal 'out' / entry==1; with no entry column every
        # row is a closing row (a positions export is one row per closed position).
        if not c_entry:
            return True
        ev = str(r.get(c_entry, '')).strip().lower()
        return ('out' in ev) or (ev in ('1', 'close', 'exit', '1.0'))

    # first pass: per-position earliest epoch across ALL rows (the IN leg = entry).
    pos_entry = {}
    for r in rows:
        pid = str(r.get(c_pos, '')).strip() if c_pos else ''
        t_any = _epoch_broker(r.get(c_open, '')) if c_open else 0
        if not t_any and c_time:
            t_any = _epoch_broker(r.get(c_time, ''))
        if pid and t_any:
            pos_entry[pid] = min(pos_entry[pid], t_any) if pid in pos_entry else t_any

    out = []
    for r in rows:
        if not is_out_row(r):
            continue
        t_close = _epoch_broker(r.get(c_time, '')) if c_time else 0
        pid = str(r.get(c_pos, '')).strip() if c_pos else ''
        t_open = _epoch_broker(r.get(c_open, '')) if c_open else 0
        entry_epoch = t_open or (pos_entry.get(pid, 0) if pid else 0) or t_close
        out.append(types.SimpleNamespace(
            entry=1, time=t_close, entry_time=entry_epoch, position_id=(pid or None),
            comment=str(r.get(c_comment, '') if c_comment else ''),
            magic=int(num(r, c_magic)) if c_magic else 0,
            profit=num(r, c_profit), swap=num(r, c_swap) if c_swap else 0.0,
            commission=num(r, c_comm) if c_comm else 0.0))
    return out


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def run_gate(sim_deals, *, deal_export_path=None, resolution_all_tick=False,
             refused_days=None, build_errors=None):
    """Compare sim bucket nets to TRUTH (and, if provided, to the deal export).

    §4 HARD REFUSAL: the gate does not merely 'fail' on bad inputs, it REFUSES to
    render a verdict at all when (a) any day did not resolve to TICK (M1/synthetic
    -> intrabar wick order is invented; 9 of 15 July whipsaws turn on it by cents),
    or (b) the sim emitted a leg whose comment does not classify (a BUILD ERROR).
    `passed` is True ONLY when NOT refused AND every engine bucket matches TRUTH to
    the cent -- "off by $X because feature Y isn't wired" is the gate failing, not
    an explanation."""
    refused_days = list(refused_days or [])
    build_errors = list(build_errors or [])
    # Annotate each OUT leg with its ENTRY timestamp (paired from the IN leg via
    # position_id) so the 14:34 manual-seed carve keys on entry, not close, time.
    sim_out = attach_entry_times(list(sim_deals))
    exp_deals = parse_deal_export(deal_export_path)
    # The reproducible TRUTH is computed from the EXPORT (per-leg, minus the excluded
    # ST bucket and the >=14:34 manual-seed legs) -- NOT the hardcoded totals (which
    # include the excluded events). Without the export the gate cannot compute it
    # -> HARD REFUSE.
    no_export = not exp_deals
    refused = (bool(refused_days) or (not resolution_all_tick) or bool(build_errors)
               or no_export)

    repro_sim = reproducible_nets(sim_out)
    repro_exp = reproducible_nets(exp_deals) if exp_deals else {}
    # STATE the excluded total on every run (owner requirement): from the export
    # (truth) when present, else from the sim so the line is never blank.
    excluded = excluded_breakdown(exp_deals if exp_deals else sim_out)
    excluded_source = 'export' if exp_deals else 'sim'
    rows = []
    all_match = True
    for b in REPRODUCIBLE_BUCKETS:
        s = repro_sim.get(b, 0.0)
        truth = repro_exp.get(b) if exp_deals else None
        gap = None if truth is None else round(s - truth, 2)
        if gap is None or abs(gap) > TOL:
            all_match = False
        rows.append({'bucket': b, 'sim': s, 'truth': truth, 'gap': gap,
                     'ref_truth': TRUTH.get(b)})
    sim_total = round(sum(repro_sim.values()), 2)
    exp_total = round(sum(repro_exp.values()), 2) if exp_deals else None
    total_gap = None if exp_total is None else round(sim_total - exp_total, 2)
    passed = bool((not refused) and all_match)
    return {'passed': passed, 'refused': refused, 'resolution_ok': resolution_all_tick,
            'refused_days': refused_days, 'build_errors': build_errors, 'no_export': no_export,
            'rows': rows, 'repro_sim': repro_sim, 'repro_export': repro_exp,
            'excluded_buckets': sorted(EXCLUDED_BUCKETS),
            'manual_seed_cutoff': MANUAL_SEED_CUTOFF_BROKER,
            'manual_seed_engines': sorted(MANUAL_SEED_ENGINES),
            'excluded': excluded, 'excluded_source': excluded_source,
            'sim_total': sim_total, 'export_total': exp_total, 'total_gap': total_gap,
            'all_match': all_match}


def render_gate(result) -> str:
    exc = result.get('excluded') or {}
    exc_src = result.get('excluded_source', 'export')
    lines = [sc.gate_header(), "",
             "THE GATE — sim vs MT5 deal export, REPRODUCIBLE SUBSET (2026-07-01..07-10)",
             f"excluded (unreproducible): ST bucket (testfire, by hand) + "
             f"{'/'.join(result.get('manual_seed_engines', []))} legs OPENED >= "
             f"{result.get('manual_seed_cutoff')} server (manual /rogueseed //fetchseed)",
             f"excluded total ({exc_src}): {exc.get('total', 0.0):+.2f}  "
             f"[ST {exc.get('ST', 0.0):+.2f} · manual-seed {exc.get('MANUAL_SEED', 0.0):+.2f}]",
             "",
             f"{'bucket':<8}{'sim':>13}{'export':>13}{'gap':>12}{'(full truth)':>14}",
             "-" * 60]
    for r in result['rows']:
        exp = '   n/a' if r['truth'] is None else f"{r['truth']:+.2f}"
        gap = '   n/a' if r['gap'] is None else f"{r['gap']:+.2f}"
        ref = '' if r.get('ref_truth') is None else f"{r['ref_truth']:+.2f}"
        lines.append(f"{r['bucket']:<8}{r['sim']:>+13.2f}{exp:>13}{gap:>12}{ref:>14}")
    lines.append("-" * 60)
    et = '   n/a' if result['export_total'] is None else f"{result['export_total']:+.2f}"
    tg = '   n/a' if result['total_gap'] is None else f"{result['total_gap']:+.2f}"
    lines.append(f"{'TOTAL':<8}{result['sim_total']:>+13.2f}{et:>13}{tg:>12}")
    lines.append("")
    if result['passed']:
        lines.append("GATE PASSED — sim reproduces the MT5 export to the cent on the "
                     "reproducible subset, all-tick, all features wired.")
    elif result.get('refused'):
        lines.append("GATE **HARD-REFUSED** — the inputs are not gradeable; no verdict is rendered.")
        if result.get('no_export'):
            lines.append("  · the MT5 deal export is missing — the reproducible truth is computed "
                         "FROM the export (per-leg, minus the ST bucket and >=14:34 manual-seed "
                         "legs), so it cannot be graded without it. Commit "
                         "backtest/fixtures/deal_export_2026-07.xlsx (or a deal_export*.csv).")
        if result.get('refused_days'):
            lines.append(f"  · non-tick day(s) refused: {', '.join(result['refused_days'])} "
                         "(M1/synthetic — intrabar wick order is INVENTED; 9 of 15 July whipsaws "
                         "turn on it by cents, so a bar day cannot decide them).")
        if not result['resolution_ok'] and not result.get('refused_days'):
            lines.append("  · resolution is not all-tick for the range.")
        if result.get('build_errors'):
            lines.append(f"  · BUILD ERROR — {len(result['build_errors'])} leg(s) emitted a "
                         f"comment that does not classify: {result['build_errors'][:5]}. A "
                         "non-AUR_* comment is a simulator bug, not an 'unknown' bucket.")
        lines.append("  Fix the inputs; the gap is never tuned away.")
    else:
        lines.append("GATE **NOT PASSED** — reproducible bucket(s) differ from the export. "
                     "This is the gate failing (a missing/incorrect feature), NOT an "
                     "explanation. Report and fix the mechanism; do not tune to match.")
    return "\n".join(lines)
