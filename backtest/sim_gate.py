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


def bucket_nets(out_deals):
    """{bucket: net} over closing (entry==1) deals, keyed by AUR_* comment bucket."""
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
        out.append(types.SimpleNamespace(
            entry=1, comment=str(r.get(c_comment, '') if c_comment else ''),
            magic=int(num(c_magic)) if c_magic else 0,
            profit=num(c_profit), swap=num(c_swap) if c_swap else 0.0,
            commission=num(c_comm) if c_comm else 0.0))
    return out


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def run_gate(sim_deals, *, deal_export_path=None, resolution_all_tick=False):
    """Compare sim bucket nets to TRUTH (and, if provided, to the deal export).
    Returns {passed, resolution_ok, rows, sim, export, total_gap}. `passed` is True
    only when resolution is all-tick AND every bucket matches TRUTH within TOL."""
    sim = sim_bucket_nets(sim_deals)
    export = None
    exp_deals = parse_deal_export(deal_export_path)
    if exp_deals:
        export = bucket_nets(exp_deals)
    rows = []
    all_match = True
    for b in BUCKETS:
        s = sim.get(b, 0.0)
        truth = TRUTH[b]
        gap = round(s - truth, 2)
        if abs(gap) > TOL:
            all_match = False
        rows.append({'bucket': b, 'sim': s, 'truth': truth, 'gap': gap,
                     'export': (export.get(b) if export else None)})
    sim_total = round(sum(sim.values()), 2)
    total_gap = round(sim_total - TRUTH_TOTAL, 2)
    passed = bool(resolution_all_tick and all_match)
    return {'passed': passed, 'resolution_ok': resolution_all_tick, 'rows': rows,
            'sim': sim, 'export': export, 'sim_total': sim_total,
            'truth_total': TRUTH_TOTAL, 'total_gap': total_gap}


def render_gate(result) -> str:
    lines = [sc.gate_header(), "",
             "THE GATE — sim vs MT5 deal-export truth (2026-07-01..07-10)",
             f"{'bucket':<8}{'sim':>12}{'truth':>12}{'gap':>12}{'export':>12}",
             "-" * 56]
    for r in result['rows']:
        exp = '—' if r['export'] is None else f"{r['export']:+.2f}"
        lines.append(f"{r['bucket']:<8}{r['sim']:>+12.2f}{r['truth']:>+12.2f}"
                     f"{r['gap']:>+12.2f}{exp:>12}")
    lines.append("-" * 56)
    lines.append(f"{'TOTAL':<8}{result['sim_total']:>+12.2f}{result['truth_total']:>+12.2f}"
                 f"{result['total_gap']:>+12.2f}")
    lines.append("")
    if result['passed']:
        lines.append("GATE PASSED — sim reproduces MT5 truth to the cent on all-tick data.")
    else:
        why = []
        if not result['resolution_ok']:
            why.append("resolution is NOT all-tick (some days are M1/synthetic — intrabar "
                       "order unknown)")
        if any(abs(r['gap']) > TOL for r in result['rows']):
            why.append("bucket(s) differ from truth")
        lines.append("GATE **NOT PASSED** — " + "; ".join(why) + ".")
        lines.append("Per the spec: the gap is NOT to be tuned away. On synthetic/illustrative "
                     "ticks a gap is EXPECTED (the sim is replaying invented prices, not July's "
                     "real market). Re-run on the committed real-tick cache to make this meaningful.")
    return "\n".join(lines)
