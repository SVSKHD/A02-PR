"""AUREON offline simulator — REPORTS (Part 1C).

Same renderer as live (pnl_report.render_markdown / PNL_LEDGER_COLUMNS), written
to a SEPARATE tree (sim/reports/<run-id>/), every artifact carrying the mandatory
GATE-NOT-RUN header. Plus a per-engine summary (trades, net, PF, win%, max
drawdown, worst day, worst 3-day streak). NEVER writes under run/.

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
import pnl_source as ps
import sim_common as sc


# --------------------------------------------------------------------------- #
# per-day / per-engine aggregation from the simulated deal history
# --------------------------------------------------------------------------- #
def _day_deals(deals, cfg, date_str):
    """Deals whose OUT falls in `date_str`'s broker day (same window the report
    uses). Simple SimpleNamespace trader carrying cfg for broker_day_range."""
    import types
    tr = types.SimpleNamespace(cfg=cfg)
    f, t = ps.broker_day_range(tr, day=date_str)
    fe, te = f.timestamp(), t.timestamp()
    return [d for d in deals if fe <= d.time < te]


def day_report(deals, cfg, date_str, rescue_idx=None):
    """Build one day's report dict from sim deals (pure pnl_report functions,
    authority-sourced totals via reconcile_anchor_total)."""
    dd = _day_deals(deals, cfg, date_str)
    trades = pr.build_trades(dd, rescue_idx or {})
    whip = pr.detect_whipsaws(trades)
    per_anchor = pr.per_anchor_stats(trades, whip)
    per_anchor = pr.reconcile_anchor_total(per_anchor, dd)
    rogue = pr.rogue_stats(trades)
    rogue['day_pnl'] = ps.magic_day_net(dd, pr.ROGUE_MAGIC_DEFAULT)
    fetcher = pr.fetcher_stats(trades)
    fetcher['day_pnl'] = ps.magic_day_net(dd, pr.FETCHER_MAGIC_DEFAULT)
    return {'date': date_str, 'trades': trades, 'per_anchor': per_anchor,
            'whipsaw_counts': whip, 'rogue': rogue, 'fetcher': fetcher,
            'w2': {}, 'unclassified': [t for t in trades
                                       if t['leg_class'] in (pr.UNKNOWN, pr.BOOST_UNCLASSIFIED)],
            'anchors_net': ps.magic_day_net(dd, ps.ANCHORS_MAGIC)}


# --------------------------------------------------------------------------- #
# per-engine summary metrics
# --------------------------------------------------------------------------- #
def _drawdown(daily_nets):
    """Max peak-to-trough drawdown of the cumulative equity curve (<= 0)."""
    cum = 0.0; peak = 0.0; mdd = 0.0
    for v in daily_nets:
        cum += v
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return round(mdd, 2)


def _worst_streak(daily_pairs, k=3):
    """Worst rolling k-day sum (date_start, sum). daily_pairs = [(date, net), ...]."""
    if not daily_pairs:
        return None
    worst = None
    for i in range(len(daily_pairs)):
        window = daily_pairs[i:i + k]
        s = round(sum(v for _, v in window), 2)
        if worst is None or s < worst[1]:
            worst = (window[0][0], s)
    return worst


def engine_summary(reports, engine):
    """Per-engine roll-up across the day reports. engine in {'anchors','rogue','fetcher'}."""
    daily = []
    trades = 0; wins = 0; losses = 0; gross_win = 0.0; gross_loss = 0.0
    for rep in reports:
        if engine == 'anchors':
            net = round(sum(s['net'] for s in rep['per_anchor'].values()), 2)
            for s in rep['per_anchor'].values():
                trades += s['trades']; wins += s['wins']; losses += s['losses']
                gross_win += s['gross_win']; gross_loss += s['gross_loss']
        else:
            e = rep[engine]
            net = round(e['day_pnl'], 2)
            trades += e['entries']; wins += e['wins']; losses += e['fails']
        daily.append((rep['date'], net))
    nets = [v for _, v in daily]
    pf = (round(gross_win / gross_loss, 2) if gross_loss > 0
          else (float('inf') if gross_win > 0 else 0.0))
    decisive = wins + losses
    return {
        'engine': engine, 'net': round(sum(nets), 2), 'trades': trades,
        'pf': pf if engine == 'anchors' else None,
        'win_pct': round(100.0 * wins / decisive, 1) if decisive else 0.0,
        'max_drawdown': _drawdown(nets),
        'worst_day': min(daily, key=lambda kv: kv[1]) if daily else None,
        'worst_3day': _worst_streak(daily, 3),
        'daily': daily,
    }


# --------------------------------------------------------------------------- #
# writers (every file carries the GATE header)
# --------------------------------------------------------------------------- #
def write_reports(run_id, deals, cfg, day_list):
    """Write sim/reports/<run-id>/daily_<date>.md + pnl_ledger.csv + summary.md.
    Returns the output dir. NEVER writes under run/ (sim_common guard)."""
    out_dir = sc.run_output_dir(run_id)
    reports = [day_report(deals, cfg, d) for d in day_list]

    # per-day markdown + ledger
    import csv as _csv
    ledger_path = os.path.join(out_dir, 'pnl_ledger.csv')
    all_rows = []
    for rep in reports:
        md = pr.render_markdown(rep['date'], rep['per_anchor'], rep['rogue'],
                                whipsaw_counts=rep['whipsaw_counts'], w2=rep['w2'],
                                fetcher=rep['fetcher'])
        md_path = os.path.join(out_dir, f"daily_{rep['date']}.md")
        with sc.open_sim_file(md_path, 'w') as f:
            f.write(sc.gate_banner_md() + "\n" + md + "\n")
        all_rows += pr.ledger_rows(rep['date'], rep['per_anchor'], rep['rogue'],
                                   fetcher=rep['fetcher'])
    with sc.open_sim_file(ledger_path, 'w') as f:
        f.write(sc.gate_header("# ") + "\n")
        w = _csv.DictWriter(f, fieldnames=pr.PNL_LEDGER_COLUMNS)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k, '') for k in pr.PNL_LEDGER_COLUMNS})

    # per-engine summary
    summ = {e: engine_summary(reports, e) for e in ('anchors', 'rogue', 'fetcher')}
    summary_md = render_summary_md(run_id, summ, reports, cfg)
    with sc.open_sim_file(os.path.join(out_dir, 'summary.md'), 'w') as f:
        f.write(summary_md)
    return out_dir, reports, summ


def render_summary_md(run_id, summ, reports, cfg):
    lines = [sc.gate_banner_md(),
             f"# AUREON SIM per-engine summary — run {run_id}", "",
             "| engine | trades | net | PF | win% | max DD | worst day | worst 3-day |",
             "|---|---:|---:|---:|---:|---:|---|---|"]
    for e in ('anchors', 'rogue', 'fetcher'):
        s = summ[e]
        pf = '—' if s['pf'] is None else pr._fmt_pf(s['pf'])
        wd = f"{s['worst_day'][0]} ({s['worst_day'][1]:+.2f})" if s['worst_day'] else 'n/a'
        w3 = f"{s['worst_3day'][0]} ({s['worst_3day'][1]:+.2f})" if s['worst_3day'] else 'n/a'
        lines.append(f"| {e} | {s['trades']} | {s['net']:+.2f} | {pf} | {s['win_pct']:.1f}% | "
                     f"{s['max_drawdown']:+.2f} | {wd} | {w3} |")
    total = round(sum(summ[e]['net'] for e in summ), 2)
    lines += ["", f"**Account net (all engines): {total:+.2f}**", "",
              "_Per-day anchors net = pnl_source.magic_day_net over the same broker-day "
              "window as live; rogue/fetcher = their magic's net. Attribution best-effort._"]
    return "\n".join(lines) + "\n"
