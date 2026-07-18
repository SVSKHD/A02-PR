"""AUREON — ROGUE monster-engine BAR-MODE backtester (parity/acceptance harness).

Drives the SAME live decision core (`rogue_monster.MonsterEngine`) off M1 OHLC
bars and prints per-day + monthly summary tables. This is the parity/acceptance
tool: on the MT5 box, run it over a month of M1 and it reproduces the validated
reference sim's monthly tables (same fills/dates within rounding), because it
runs the identical engine on the identical bar-fill model.

BAR-MODE vs TICK-MODE (read this before comparing to live)
----------------------------------------------------------
This harness fills a resting stop when an M1 bar's high/low crosses its level and
resolves SL/trail on the same bar's extremes — identical to the reference sim.
The LIVE path and the repo TICK simulator (bot.py simulate) resolve fills at tick
resolution and will legitimately DIFFER on any minute where both a stop and an
SL/trail are touched (intrabar wick order — see backtest/tick_cache.py). Bar-mode
is the parity oracle; tick-mode is faithful execution of the same logic. Do not
"fix" a bar/tick divergence by changing the engine.

Usage (repo box):
  python backtest/monster_backtest.py --csv m1_2026-06.csv     # cols: time,open,high,low,close
  python backtest/monster_backtest.py --mt5 --from 2026-06-01 --to 2026-06-30
  python backtest/monster_backtest.py --selftest               # synthetic smoke (off-VPS OK)

Config: uses rogue_monster.MonsterCfg defaults (== live rogue_* defaults). Pass
--lot / --atr-mult / --profit-lock to spot-check; the live engine reads config.py.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

# Load the engine core by absolute path (repo convention: avoid name shadowing).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import rogue_monster as rm  # noqa: E402


# ── rendering (byte-identical to the reference table format) ─────────────────
def print_day(day, trades, events, day_pnl, dark_pct, halted, cfg, verbose):
    print(f"\n=== {day}  |  lot {cfg.lot}  |  dark {dark_pct:.0f}%"
          f"  |  {('HALT: ' + halted) if halted else 'ran full day'} ===")
    if verbose:
        for t, e in events:
            print(f"  {t:%H:%M}  {e}")
    if not trades:
        print("  no fills (dark all day)")
        return 0.0
    hdr = (f"  {'#':<3}{'seq':<4}{'kind':<6}{'side':<6}{'entry@':<16}{'exit@':<16}"
           f"{'pts':>7}{'PnL$':>9}{'peak':>6}{'pullbk':>7}  {'exit':<6} armed-by")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    tot = 0.0
    for i, tr in enumerate(trades, 1):
        tot += tr.pnl(cfg.lot)
        print(f"  {i:<3}{tr.seq:<4}{tr.kind:<6}{tr.side:<6}"
              f"{tr.entry_time:%H:%M} {tr.entry:<9.2f}"
              f"{tr.exit_time:%H:%M} {tr.exit:<9.2f}"
              f"{tr.pts:>7.2f}{tr.pnl(cfg.lot):>9.2f}"
              f"{tr.peak:>6.1f}{tr.mae:>7.1f}  {tr.reason:<6} {tr.arm_reason}")
    print("  " + "-" * (len(hdr) - 2))
    print(f"  day total: {tot:+.2f} $   (governor-checked P/L {day_pnl:+.2f})")
    return tot


def run(m1, cfg, verbose=True, label=""):
    eng = rm.MonsterEngine(cfg)
    days, total = eng.run(m1)
    rows = []
    for day, trades, events, day_pnl, dark, halted, tot in days:
        print_day(day, trades, events, day_pnl, dark, halted, cfg, verbose)
        rows.append((day, len(trades), tot, dark, halted or "-"))
    print(f"\n{'='*72}\n  SUMMARY {label}")
    print(f"  {'day':<12}{'fills':>6}{'PnL$':>12}{'dark%':>8}  halt")
    for d, n, p, dk, h in rows:
        print(f"  {str(d):<12}{n:>6}{p:>12.2f}{dk:>8.0f}  {h}")
    print(f"  {'TOTAL':<12}{'':>6}{total:>12.2f}")
    return total


# ── data loaders ─────────────────────────────────────────────────────────────
def load_csv(path):
    df = pd.read_csv(path, parse_dates=["time"])
    return df.set_index("time")[["open", "high", "low", "close"]]


def load_mt5(d_from, d_to, symbol="XAUUSD", broker_tz_offset_hours=3):
    """Windows/VPS only. Fetches M1 bars and shifts broker epoch -> true UTC so
    the 02:30-server anchor lines up with the live convention."""
    import MetaTrader5 as mt5  # noqa
    if not mt5.initialize():
        sys.exit("MT5 init failed")
    r = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1,
                             datetime.fromisoformat(d_from), datetime.fromisoformat(d_to))
    mt5.shutdown()
    df = pd.DataFrame(r)
    # MT5 stamps broker-epoch seconds (Pepperstone UTC+3); land in true UTC.
    df["time"] = pd.to_datetime(df["time"], unit="s") - pd.Timedelta(hours=broker_tz_offset_hours)
    return df.set_index("time")[["open", "high", "low", "close"]]


def synth():
    """Synthetic day: chop -> monster up -> reversal dump. Smoke test only —
    numbers are NOT market results."""
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-07-17 01:00", "2026-07-17 18:00", freq="1min")
    n = len(idx)
    drift = np.zeros(n)
    a, b = int(n * 0.45), int(n * 0.55)
    drift[a:b] = 30.0 / (b - a)
    c, d = int(n * 0.62), int(n * 0.80)
    drift[c:d] = -38.0 / (d - c)
    close = 3978 + np.cumsum(drift + rng.normal(0, 0.55, n))
    high = close + rng.uniform(0.1, 1.2, n)
    low = close - rng.uniform(0.1, 1.2, n)
    op = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": op, "high": high, "low": low, "close": close}, index=idx)


def _cfg_from_args(args):
    cfg = rm.MonsterCfg()
    if args.lot is not None:
        cfg.lot = args.lot
    if args.atr_mult is not None:
        cfg.atr_mult = args.atr_mult
    if args.profit_lock is not None:
        cfg.profit_lock = args.profit_lock
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="ROGUE monster bar-mode backtester")
    ap.add_argument("--mt5", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--from", dest="dfrom", default="2026-06-01")
    ap.add_argument("--to", dest="dto", default="2026-06-30")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--lot", type=float, default=None)
    ap.add_argument("--atr-mult", dest="atr_mult", type=float, default=None)
    ap.add_argument("--profit-lock", dest="profit_lock", type=float, default=None)
    args = ap.parse_args(argv)

    cfg = _cfg_from_args(args)
    if args.selftest:
        m1 = synth()
        print("SELFTEST (synthetic data — numbers are NOT market results)")
        label = "selftest"
    elif args.csv:
        m1 = load_csv(args.csv)
        label = os.path.basename(args.csv)
    elif args.mt5:
        m1 = load_mt5(args.dfrom, args.dto)
        label = f"{args.dfrom}..{args.dto}"
    else:
        ap.error("choose --mt5, --csv or --selftest")
    return run(m1, cfg, verbose=not args.quiet, label=label)


if __name__ == "__main__":
    main()
