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
#   - ST bucket = 34 testfire legs fired BY HAND -> excluded entirely.
#   - /rogueseed + /fetchseed fired by the owner at 14:34 / 14:58 server on
#     2026-07-07 (seed_source=MANUAL). They re-anchored both engines mid-day; the
#     sim seeds from A1 automatically and diverges from that instant, so the WHOLE
#     07-07 ROGUE and FETCH contribution is unreproducible -> excluded. (The
#     pre-14:34 morning is sacrificed with it, conservatively.) MT5 deals carry no
#     seed_source, so the carve is by (engine, day), the only reliable key.
EXCLUDED_BUCKETS = {'ST'}
EXCLUDED_CELLS = {('ROGUE', '2026-07-07'), ('FETCH', '2026-07-07')}
REPRODUCIBLE_BUCKETS = tuple(b for b in BUCKETS if b not in EXCLUDED_BUCKETS)


def _broker_day(d):
    """Broker calendar day 'YYYY-MM-DD' of a deal (time is broker-epoch seconds)."""
    import datetime as _dt
    try:
        return _dt.datetime.utcfromtimestamp(int(getattr(d, 'time', 0))).strftime('%Y-%m-%d')
    except Exception:
        return ''


def bucket_nets_by_cell(out_deals):
    """{(bucket, day): net} over closing deals -- day-aware so (engine, day) cells
    can be excluded."""
    cells = {}
    for d in out_deals:
        b = bucket_of(getattr(d, 'comment', ''), getattr(d, 'magic', 0))
        key = (b, _broker_day(d))
        cells[key] = round(cells.get(key, 0.0) + _deal_realized(d), 2)
    return cells


def reproducible_nets(out_deals):
    """{bucket: net} over the REPRODUCIBLE subset: excluding EXCLUDED_BUCKETS and
    EXCLUDED_CELLS. Returns only REPRODUCIBLE_BUCKETS."""
    cells = bucket_nets_by_cell(out_deals)
    nets = {b: 0.0 for b in REPRODUCIBLE_BUCKETS}
    for (b, day), v in cells.items():
        if b in EXCLUDED_BUCKETS or (b, day) in EXCLUDED_CELLS:
            continue
        nets[b] = round(nets.get(b, 0.0) + v, 2)
    return {b: round(v, 2) for b, v in nets.items()}


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
# deal-export parsing (flexible: MT5 history export CSV)
# --------------------------------------------------------------------------- #
def parse_deal_export(path):
    """Parse an MT5 deal-export CSV into pseudo-deal namespaces (entry==1 OUT rows
    with .comment/.magic/.profit/.swap/.commission). Tolerant of column naming;
    returns [] if the file is absent or unparseable. Only OUT/closing rows count."""
    import csv
    import types
    if not path or not os.path.exists(path):
        return None
    out = []
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None
    if not rows:
        return []
    cols = {c.lower().strip(): c for c in rows[0].keys()}

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
    for r in rows:
        entry_val = (r.get(c_entry, '') if c_entry else '')
        # MT5 export marks the closing deal 'out' / entry==1; if no entry column,
        # count every row that carries realized profit.
        is_out = True
        if c_entry:
            ev = str(entry_val).strip().lower()
            is_out = ('out' in ev) or (ev in ('1', 'close', 'exit'))
        if not is_out:
            continue

        def num(cn):
            try:
                return float(str(r.get(cn, '') or '0').replace(',', ''))
            except (TypeError, ValueError):
                return 0.0
        # broker-epoch time from the export's Time column (MT5 'YYYY.MM.DD HH:MM:SS',
        # broker/server time) -> epoch seconds, so _broker_day() keys exclusion cells.
        t_epoch = 0
        if c_time:
            import pandas as _pd
            try:
                ts = _pd.Timestamp(str(r.get(c_time, '')).replace('.', '-', 2))
                if ts.tzinfo is None:
                    ts = ts.tz_localize('UTC')   # broker wall time -> UTC-based epoch,
                t_epoch = int(ts.timestamp())     # so _broker_day matches the sim's key
            except Exception:
                t_epoch = 0
        out.append(types.SimpleNamespace(
            entry=1, time=t_epoch,
            comment=str(r.get(c_comment, '') if c_comment else ''),
            magic=int(num(c_magic)) if c_magic else 0,
            profit=num(c_profit), swap=num(c_swap) if c_swap else 0.0,
            commission=num(c_comm) if c_comm else 0.0))
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
    sim_out = [d for d in sim_deals if getattr(d, 'entry', None) == 1]
    exp_deals = parse_deal_export(deal_export_path)
    # The reproducible TRUTH is computed from the EXPORT (per-day, minus excluded
    # cells) -- NOT the hardcoded totals (which include the excluded events). Without
    # the export the gate cannot compute it -> HARD REFUSE.
    no_export = not exp_deals
    refused = (bool(refused_days) or (not resolution_all_tick) or bool(build_errors)
               or no_export)

    repro_sim = reproducible_nets(sim_out)
    repro_exp = reproducible_nets(exp_deals) if exp_deals else {}
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
            'excluded_cells': sorted(f"{b}@{d}" for b, d in EXCLUDED_CELLS),
            'sim_total': sim_total, 'export_total': exp_total, 'total_gap': total_gap,
            'all_match': all_match}


def render_gate(result) -> str:
    lines = [sc.gate_header(), "",
             "THE GATE — sim vs MT5 deal export, REPRODUCIBLE SUBSET (2026-07-01..07-10)",
             f"excluded (unreproducible): buckets {result.get('excluded_buckets')} · "
             f"cells {result.get('excluded_cells')}",
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
                         "FROM the export (per-day, minus excluded cells), so it cannot be graded "
                         "without it. Commit backtest/deal_export*.csv.")
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
