"""AUREON backtest — entry point (v3.1.8).

    python backtest/back_main.py 2026-05

Step 1: fetch (cache-first) the month's ticks from MT5. If MT5 is unavailable
        (sandbox), fall back to a deterministic synthetic tick stream and print
        a LOUD warning that the numbers are illustrative.
Step 2: replay the month through the LIVE strategy (backtest.run_month).
Step 3: print the day-by-day / per-anchor / boost / drawdown report and write the
        per-trade audit CSV to backtest/results/results_YYYY-MM.csv.
"""
from __future__ import annotations

import csv
import os
import sys

# --- ensure BOTH backtest/ and the repo root are importable, regardless of cwd
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_REPO_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import Config              # live config
import fetcher

# Import the replay engine by absolute file path so it can never be shadowed by
# the repo-root backtest.py or the backtest/ namespace-package directory (both
# are named "backtest"). This guarantees we load backtest/backtest.py.
import importlib.util as _ilu
_bt_path = os.path.join(_THIS_DIR, 'backtest.py')
_spec = _ilu.spec_from_file_location('aureon_backtest_engine', _bt_path)
bt = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(bt)


TICKS_DIR = os.path.join(_THIS_DIR, 'ticks')
RESULTS_DIR = os.path.join(_THIS_DIR, 'results')


def _parse_month(arg: str):
    try:
        y, m = arg.split('-')
        year, month = int(y), int(m)
        if not (1 <= month <= 12):
            raise ValueError
        return year, month
    except Exception:
        print(f"usage: python backtest/back_main.py YYYY-MM   (got {arg!r})")
        sys.exit(2)


def _money(x):
    return f"${x:,.2f}"


def main(argv):
    if len(argv) < 2:
        print("usage: python backtest/back_main.py YYYY-MM [--no-rally] [--no-rescue]")
        return 2
    year, month = _parse_month(argv[1])
    cfg = Config()

    # v3.2.2: optional run-time overrides for the independent RALLY/RESCUE boost
    # toggles so configs can be compared WITHOUT editing config.py. These set the
    # SAME flags the live path reads; backtest.run_month honors them via the
    # shared boosts.plan_boost_event (no separate copy).
    flags = set(argv[2:])
    if '--no-rally' in flags:
        cfg.rally_boosts_enabled = False
        flags.discard('--no-rally')
    if '--no-rescue' in flags:
        cfg.rescue_boosts_enabled = False
        flags.discard('--no-rescue')
    # v3.2.3 Section F: --stack-depth N controls the winning-side stack size
    # (1 = base/no boosts, 3 = full stack = original + 2 boosts). Honored by the
    # SHARED boosts.plan_boost_event, so live + backtest stack identically.
    for f in list(flags):
        if f.startswith('--stack-depth'):
            try:
                cfg.stack_depth = int(f.split('=', 1)[1]) if '=' in f \
                    else int(argv[argv.index(f) + 1])
            except (ValueError, IndexError):
                print("usage: --stack-depth N  (1=base, 3=full stack)")
                return 2
            flags.discard(f)
            flags.discard(str(cfg.stack_depth))
    if flags:
        print(f"unknown option(s): {' '.join(sorted(flags))}")
        print("usage: python backtest/back_main.py YYYY-MM "
              "[--no-rally] [--no-rescue] [--stack-depth N]")
        return 2

    # ----- Step 1: ticks (cache-first; synthetic fallback) -----
    ticks = fetcher.fetch_month_ticks(
        cfg.symbol, year, month, TICKS_DIR,
        broker_tz_offset_hours=cfg.broker_tz_offset_hours)
    synthetic = False
    if ticks is None:
        synthetic = True
        print("\n" + "!" * 78)
        print("⚠  using SYNTHETIC ticks (no MT5 in this environment) — numbers are")
        print("   illustrative, run on the VPS for real data")
        print("!" * 78 + "\n")
        ticks = fetcher.synthetic_month_ticks(
            year, month, broker_tz_offset_hours=cfg.broker_tz_offset_hours)

    # ----- Step 2: replay -----
    result = bt.run_month(ticks, year, month, cfg)
    trades = result['trades']
    day_rows = result['day_rows']
    s = result['summary']

    # ----- Step 3: output -----
    lines = []

    def out(line=""):
        print(line)
        lines.append(line)

    out(f"rules sourced from live modules: [{', '.join(bt.rule_sources())}]")
    out()
    tag = "  (SYNTHETIC ticks — illustrative)" if synthetic else "  (real MT5 ticks)"
    out(f"AUREON backtest  {year:04d}-{month:02d}{tag}")
    # v3.2.2: print the ACTIVE boost config so every result is unambiguous about
    # which mode produced it (compare --no-rally / --no-rescue runs directly).
    _r_on = "on" if getattr(cfg, 'rally_boosts_enabled', True) else "off"
    _s_on = "on" if getattr(cfg, 'rescue_boosts_enabled', True) else "off"
    out(f"boosts: RALLY={_r_on} RESCUE={_s_on}")
    out("=" * 92)

    # DAY-BY-DAY table
    hdr = f"{'date':<12} {'A1':>10} {'A2':>10} {'A3':>10} {'A4':>10} " \
          f"{'day_net':>11} {'cumulative':>12} {'max_DD':>11}"
    out(hdr)
    out("-" * 92)
    for r in day_rows:
        note = ""
        if r['is_monday'] and r['mon_a1_time'] == '03:30':
            note = "  Mon-A1@03:30"
        elif r['kill']:
            note = "  KILL"
        out(f"{r['date']:<12} {r['A1']:>10.2f} {r['A2']:>10.2f} {r['A3']:>10.2f} "
            f"{r['A4']:>10.2f} {r['day_net']:>11.2f} {r['cumulative']:>12.2f} "
            f"{r['running_max_dd']:>11.2f}{note}")
    out("-" * 92)

    # Per-anchor monthly totals + win rate
    out()
    out("PER-ANCHOR monthly totals:")
    for label in [lbl for (lbl, _, _) in cfg.anchors]:
        a = s['anchor_totals'].get(label, {})
        out(f"  {label:<18} net {_money(a.get('net', 0.0)):>14}   "
            f"win_rate {a.get('win_rate', 0.0):>5.1f}%   "
            f"legs {a.get('legs', 0):>3}   anchor-days {a.get('anchor_days', 0):>3}")

    # Boost summary (v3.2.0: canonical event_type counts + branch + counterfactual)
    out()
    bc = s['branch_counts']
    ec = s.get('event_counts', {'RALLY_BOOST': 0, 'RESCUE_BOOST': 0})
    out(f"BOOST summary:  RALLY_BOOST {ec.get('RALLY_BOOST', 0)}   "
        f"RESCUE_BOOST {ec.get('RESCUE_BOOST', 0)}")
    out(f"  branch:  CRASH_WIN {bc['CRASH_WIN']}   "
        f"WHIPSAW_LOSS {bc['WHIPSAW_LOSS']}   SCRATCH {bc['SCRATCH']}")
    out(f"  no-boost counterfactual (orig+rescue legs alone): "
        f"{_money(s['no_boost_counterfactual'])}")

    # Month totals
    out()
    out(f"MONTH RAW net: {_money(s['raw_net'])}")
    out(f"REALISM-ADJUSTED net: {_money(s['realism_adjusted_net'])}  "
        f"(= RAW - ${int(s['realism_haircut'])} realism_haircut; approximates live "
        f"drag not modeled in backtest)")
    out(f"MAX DRAWDOWN: {_money(s['max_dd_usd'])} ({s['max_dd_pct']:.2f}%)  — note: "
        f"live DD likely deeper (late-fire tax); use raw DD + margin for the funded "
        f"5% check")

    # Kill switch days
    out()
    if s['kill_days']:
        kd = "; ".join(f"{k['date']} (loss-at-trip {_money(k['loss_at_trip'])})"
                       for k in s['kill_days'])
        out(f"KILL-SWITCH days: {kd}")
    else:
        out("KILL-SWITCH days: none")

    # Worst day / anchor
    wd = s['worst_day']
    wa = s['worst_anchor']
    wd_txt = f"{wd['date']} ({_money(wd['net'])})" if wd['date'] else "n/a"
    wa_txt = f"{wa['label']} ({_money(wa['net'])})" if wa else "n/a"
    out(f"Worst day: {wd_txt} / Worst anchor: {wa_txt}")

    # Monday A1 confirm
    mon = s['monday_a1_fired']
    mon_at_330 = sorted({m['date'] for m in mon if m['resolved'] == '03:30'})
    out(f"Monday A1 fired @ 03:30 on: {mon_at_330 if mon_at_330 else '[]'}")

    # ----- write per-trade audit CSV -----
    os.makedirs(RESULTS_DIR, exist_ok=True)
    csv_path = os.path.join(RESULTS_DIR, f"results_{year:04d}-{month:02d}.csv")
    cols = ['date', 'anchor', 'label', 'role', 'side', 'entry', 'exit',
            'exit_reason', 'pnl_usd', 'held_min', 'slip', 'is_monday_a1',
            'event_type', 'branch', 'boost']
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for t in trades:
            w.writerow({c: t.get(c) for c in cols})
    print(f"\n[back_main] wrote {len(trades)} trade rows -> {csv_path}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
