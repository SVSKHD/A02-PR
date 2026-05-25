#!/usr/bin/env python3
"""
AUREON v2 — Monthly Analysis Tool with auto-spread detection.

Takes a month, year, and lot size. Auto-detects the actual spread from MT5
historic data (spread column), runs the AUREON strategy, and produces a clean
day-by-day report.

Folder structure (created on demand):
    data/
      XAUUSD/
        XAUUSD_M1_2025_06.csv         ← raw input data, cached per month
        XAUUSD_M1_2025_10.csv
        ...
    results/
      monthly/
        2025_06/
          daily.csv                    ← per-day aggregation
          trades.csv                   ← every trade with spread applied
          summary.json                 ← machine-readable summary
          report.md                    ← human-readable report
        2025_10/
          ...

Usage
-----
    # Auto-detect spread from data, use cached or fetch from MT5
    python monthly_analysis.py --month 6 --year 2025 --lot 0.49

    # Use specific CSV (skip cache/fetch logic)
    python monthly_analysis.py --month 6 --year 2025 --lot 0.49 \
        --csv /path/to/data.csv

    # Override auto-detected spread (e.g. simulate worse broker)
    python monthly_analysis.py --month 6 --year 2025 --lot 0.49 \
        --spread-override 0.30

    # Add fixed slippage on top of broker spread (default $0.05)
    python monthly_analysis.py --month 6 --year 2025 --lot 0.49 \
        --extra-slippage 0.10

    # Different account
    python monthly_analysis.py --month 10 --year 2025 --lot 0.98 \
        --balance 100000
"""

import argparse
import calendar
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd


log = logging.getLogger("MONTHLY")

# XAUUSD point size: 1 point = $0.01 (price has 2 decimal places)
POINT_SIZE = 0.01

# Default folders (clean separation: data on one side, results on the other)
DEFAULT_DATA_DIR    = "./data/XAUUSD"
DEFAULT_RESULTS_DIR = "./results/monthly"


# ============================================================================
# Helpers
# ============================================================================

def get_month_window(year: int, month: int):
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def ensure_data(year: int, month: int, csv_override=None,
                data_dir=DEFAULT_DATA_DIR) -> str:
    """
    Locate the M1 CSV for the given month.
    Order: --csv override > cached file > fetch from MT5
    """
    first, last = get_month_window(year, month)

    if csv_override:
        if not os.path.exists(csv_override):
            log.error(f"CSV not found: {csv_override}")
            sys.exit(1)
        log.info(f"Using provided CSV: {csv_override}")
        return csv_override

    os.makedirs(data_dir, exist_ok=True)
    cache_path = os.path.join(data_dir, f"XAUUSD_M1_{first.year}_{first.month:02d}.csv")
    if os.path.exists(cache_path):
        log.info(f"Using cached: {cache_path}")
        return cache_path

    log.info(f"No cache for {first.strftime('%B %Y')} → fetching from MT5...")
    try:
        from fetch_data import fetch_m1
    except ImportError:
        log.error("Could not import fetch_data.py")
        sys.exit(1)

    try:
        fetch_start = datetime.combine(
            first - timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
        fetch_end = datetime.combine(
            last + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
        fetch_m1(symbol="XAUUSD",
                 start=fetch_start, end=fetch_end,
                 output_path=cache_path)
    except Exception as e:
        log.error(f"MT5 fetch failed: {e}")
        log.error("If MT5 is not available, pass --csv /path/to/your_data.csv")
        sys.exit(1)

    return cache_path


def auto_detect_spread(csv_path: str, year: int, month: int,
                       trim_pct: float = 0.05) -> tuple:
    """
    Read the spread column from the CSV and compute representative spread.
    Returns (median_dollars, mean_dollars, percentiles, count) for THIS month.

    Filters: only bars in the requested month, only spread > 0,
    and trims top/bottom trim_pct to ignore news-spike outliers.
    """
    first, last = get_month_window(year, month)

    df = pd.read_csv(csv_path, usecols=["time", "spread"])
    df["time"] = pd.to_datetime(df["time"], utc=True)

    # Filter to month
    mask = (df["time"].dt.date >= first) & (df["time"].dt.date <= last)
    month_df = df.loc[mask & (df["spread"] > 0)].copy()

    if len(month_df) == 0:
        log.warning("No spread data for this month, falling back to $0.25")
        return 0.25, 0.25, {}, 0

    # Trim outliers
    lo = month_df["spread"].quantile(trim_pct)
    hi = month_df["spread"].quantile(1 - trim_pct)
    trimmed = month_df[(month_df["spread"] >= lo) & (month_df["spread"] <= hi)]

    median_points = float(trimmed["spread"].median())
    mean_points   = float(trimmed["spread"].mean())

    percentiles = {
        "p10":  float(month_df["spread"].quantile(0.10)),
        "p25":  float(month_df["spread"].quantile(0.25)),
        "p50":  float(month_df["spread"].quantile(0.50)),
        "p75":  float(month_df["spread"].quantile(0.75)),
        "p90":  float(month_df["spread"].quantile(0.90)),
        "max":  float(month_df["spread"].max()),
    }

    return (round(median_points * POINT_SIZE, 4),
            round(mean_points   * POINT_SIZE, 4),
            percentiles,
            len(month_df))


def apply_per_trade_spread(df_trades: pd.DataFrame, spread_dollars: float,
                           extra_slip_dollars: float, lot: float) -> pd.DataFrame:
    """
    Subtract spread + slippage from each trade.
    Returns a new DataFrame with gross/net columns.
    """
    if len(df_trades) == 0:
        return df_trades
    df = df_trades.copy()
    total_cost = spread_dollars + extra_slip_dollars
    df["gross_pnl_dist"] = df["pnl_dist"]
    df["spread_cost"]    = spread_dollars
    df["slippage_cost"]  = extra_slip_dollars
    df["pnl_dist"]       = df["pnl_dist"] - total_cost
    df["pnl_usd"]        = (df["pnl_dist"] * 100 * lot).round(2)
    return df


def write_report_md(path: str, *, args, daily, df, totals, dd_info, spread_info):
    """Write human-readable markdown report for the month."""
    first, _ = get_month_window(args.year, args.month)
    month_name = first.strftime("%B %Y")

    lines = []
    P = lines.append
    P(f"# AUREON v2 — {month_name} Report")
    P(f"")
    P(f"*Generated {datetime.now(timezone.utc).isoformat()}*")
    P("")
    P("## Configuration")
    P("")
    P(f"| Parameter | Value |")
    P(f"|-----------|------:|")
    P(f"| Month | {month_name} |")
    P(f"| Lot size | {args.lot} |")
    P(f"| Starting balance | ${args.balance:,.0f} |")
    P(f"| Daily kill switch | -{args.daily_kill_pct*100:.1f}% (-${args.balance * args.daily_kill_pct:,.0f}) |")
    P(f"| Per-trade SL | ${20.0 * 100 * args.lot:,.0f} ({100*20.0*100*args.lot/args.balance:.2f}% of balance) |")
    P(f"| Spread (auto-detected) | ${spread_info['used']:.3f} ({spread_info['source']}) |")
    P(f"| Extra slippage | ${args.extra_slippage:.3f} |")
    P(f"| Total cost per trade | ${spread_info['used'] + args.extra_slippage:.3f} (price) = ${(spread_info['used'] + args.extra_slippage) * 100 * args.lot:.2f} USD |")
    P("")
    P("## Spread Detection (from broker data this month)")
    P("")
    if spread_info.get("count", 0):
        pct = spread_info["percentiles"]
        P(f"| Statistic | Points | USD |")
        P(f"|-----------|------:|----:|")
        P(f"| Median (used) | {spread_info['median_points']:.1f} | ${spread_info['median_points'] * POINT_SIZE:.3f} |")
        P(f"| Mean (trimmed) | {spread_info['mean_points']:.1f} | ${spread_info['mean_points'] * POINT_SIZE:.3f} |")
        P(f"| P10 | {pct['p10']:.0f} | ${pct['p10'] * POINT_SIZE:.3f} |")
        P(f"| P75 | {pct['p75']:.0f} | ${pct['p75'] * POINT_SIZE:.3f} |")
        P(f"| P90 | {pct['p90']:.0f} | ${pct['p90'] * POINT_SIZE:.3f} |")
        P(f"| Max (news spike) | {pct['max']:.0f} | ${pct['max'] * POINT_SIZE:.3f} |")
        P(f"| Bars analyzed | {spread_info['count']:,} | |")
        P("")
        P("*Used median (5%-trimmed) as representative round-trip cost per trade.*")
    P("")
    P("## Day-by-Day P&L")
    P("")
    P(f"| Date | DOW | Trades | Wins | TPs | SLs | Gross pips | Net pips | Net USD | Cumulative |")
    P(f"|------|-----|------:|----:|----:|----:|----------:|--------:|--------:|-----------:|")
    for _, r in daily.iterrows():
        marker = ""
        if r["net_usd"] <= -args.balance * args.daily_kill_pct: marker = " 🚨"
        elif r["sls"] > 0: marker = " ⚠"
        elif r["net_usd"] > 200: marker = " ✅"
        P(f"| {r['date_only']} | {r['dow']} | {int(r['trades'])} | "
          f"{int(r['wins'])} | {int(r['tps'])} | {int(r['sls'])} | "
          f"{r['gross_pips']:+.2f} | {r['net_pips']:+.2f} | "
          f"${r['net_usd']:+,.2f} | ${r['cum_usd']:+,.2f} |{marker}")
    P(f"| **TOTAL** | | **{int(totals['trades'])}** | **{int(totals['wins'])}** | "
      f"**{int(totals['tps'])}** | **{int(totals['sls'])}** | "
      f"**{totals['gross_pips']:+.2f}** | **{totals['net_pips']:+.2f}** | "
      f"**${totals['net_usd']:+,.2f}** | |")
    P("")
    P("## Summary")
    P("")
    P(f"| Metric | Value |")
    P(f"|--------|------:|")
    P(f"| Trading days | {totals['days']} ({totals['profitable']} profitable, {totals['losing']} losing) |")
    P(f"| Total trades | {int(totals['trades'])} |")
    P(f"| Win rate | {totals['win_rate']:.2f}% |")
    P(f"| Gross pips | {totals['gross_pips']:+.2f} |")
    P(f"| Spread cost | -{totals['trades'] * (spread_info['used'] + args.extra_slippage):.2f} pips ({int(totals['trades'])} trades × ${spread_info['used'] + args.extra_slippage:.3f}) |")
    P(f"| Net pips | {totals['net_pips']:+.2f} |")
    P(f"| **Net P&L @ {args.lot} lot** | **${totals['net_usd']:+,.2f}** |")
    P(f"| Best day | ${totals['best_day']:+,.2f} |")
    P(f"| Worst day | ${totals['worst_day']:+,.2f} |")
    P(f"| Max drawdown | ${dd_info['dd_usd']:+,.2f} ({dd_info['dd_pct']:+.2f}%) |")
    P(f"| Kill switch days | {dd_info['kill_days']} |")
    P(f"| Avg per trading day | ${totals['avg_day']:+,.2f} |")
    P(f"| Avg per trade | ${totals['avg_trade']:+,.2f} |")

    with open(path, "w") as f:
        f.write("\n".join(lines))


# ============================================================================
# CLI
# ============================================================================

def main():
    try:
        from env_loader import load_env
        load_env()
    except ImportError:
        pass

    p = argparse.ArgumentParser(
        description="AUREON v2 monthly analyzer (auto-spread, clean folders)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--month", type=int, required=True, help="1-12")
    p.add_argument("--year",  type=int, required=True, help="e.g. 2025")
    p.add_argument("--lot",   type=float, required=True, help="e.g. 0.49")
    p.add_argument("--csv", help="Use this CSV (bypasses cache and MT5 fetch)")
    p.add_argument("--spread-override", type=float, default=None,
                   help="Force a specific spread in $ (otherwise auto-detected)")
    p.add_argument("--extra-slippage", type=float, default=0.05,
                   help="Extra slippage per trade in $ (default $0.05)")
    p.add_argument("--balance", type=float, default=50000)
    p.add_argument("--daily-kill-pct", type=float, default=0.03)
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                   help=f"Where to cache/find raw data (default: {DEFAULT_DATA_DIR})")
    p.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR,
                   help=f"Where to write results (default: {DEFAULT_RESULTS_DIR}/YYYY_MM/)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    if not 1 <= args.month <= 12:   log.error("--month 1-12");  sys.exit(2)
    if not 2020 <= args.year <= 2050: log.error("--year invalid"); sys.exit(2)
    if not 0.01 <= args.lot <= 10:  log.error("--lot 0.01-10"); sys.exit(2)

    first, last = get_month_window(args.year, args.month)
    month_name = first.strftime("%B %Y")
    month_tag  = f"{args.year}_{args.month:02d}"

    # Output goes into a per-month subfolder
    out_dir = os.path.join(args.results_dir, month_tag)
    os.makedirs(out_dir, exist_ok=True)

    # Header banner
    sl_dollar   = 20.0 * 100 * args.lot
    pct_balance = 100 * sl_dollar / args.balance
    kill_dollar = args.balance * args.daily_kill_pct

    print()
    print("=" * 100)
    print(f"  AUREON v2 Monthly Analysis — {month_name}")
    print(f"  Lot: {args.lot}  •  Balance: ${args.balance:,.0f}  •  "
          f"Kill: ${kill_dollar:,.0f} (-{args.daily_kill_pct*100:.1f}%)")
    print(f"  Per-trade SL: ${sl_dollar:,.0f}  ({pct_balance:.2f}% of balance)")
    print(f"  Data dir:    {args.data_dir}/")
    print(f"  Results dir: {out_dir}/")
    print("=" * 100)
    print()

    # Get data
    csv_path = ensure_data(args.year, args.month, args.csv, args.data_dir)

    # Detect spread from data
    if args.spread_override is not None:
        spread_used = args.spread_override
        spread_info = {"used": spread_used, "source": f"manual override --spread-override {spread_used}"}
        log.info(f"Using overridden spread: ${spread_used}")
    else:
        median_dollars, mean_dollars, percentiles, n = auto_detect_spread(
            csv_path, args.year, args.month)
        spread_used = median_dollars
        spread_info = {
            "used": spread_used,
            "median_points": round(median_dollars / POINT_SIZE, 1),
            "mean_points":   round(mean_dollars   / POINT_SIZE, 1),
            "percentiles":   {k: round(v, 1) for k, v in percentiles.items()},
            "count":         n,
            "source":        f"auto-detected median from {n:,} bars in {month_name}",
        }
        log.info(f"Spread auto-detected: median ${median_dollars:.3f} "
                 f"(mean ${mean_dollars:.3f}, {n:,} bars sampled)")

    total_cost_per_trade = spread_used + args.extra_slippage
    log.info(f"Total cost per trade: ${total_cost_per_trade:.3f} "
             f"(spread ${spread_used:.3f} + slippage ${args.extra_slippage:.3f}) "
             f"= ${total_cost_per_trade * 100 * args.lot:.2f} USD @ {args.lot} lot")

    # Run strategy
    try:
        from bot import Config, run_backtest
    except ImportError:
        log.error("Could not import from bot.py"); sys.exit(1)

    cfg = Config()
    cfg.lot_size         = args.lot
    cfg.starting_balance = args.balance
    cfg.daily_loss_pct   = args.daily_kill_pct
    cfg.min_step         = 0.0
    cfg.auto_lot         = False

    log.info(f"Running AUREON strategy {first} → {last}")
    trades = run_backtest(csv_path, str(first), str(last), cfg)

    if len(trades) == 0:
        log.warning("No trades produced. Check data and date range.")
        sys.exit(0)

    trades = apply_per_trade_spread(trades, spread_used, args.extra_slippage, args.lot)

    # Day-by-day
    trades["date_only"] = pd.to_datetime(trades["date"]).dt.date
    daily = trades.groupby("date_only").agg(
        trades=("pnl_usd",  "count"),
        wins=("pnl_usd",    lambda x: (x > 0).sum()),
        sls=("outcome",     lambda x: (x == "SL").sum()),
        tps=("outcome",     lambda x: (x == "TP").sum()),
        gross_pips=("gross_pnl_dist", "sum"),
        net_pips=("pnl_dist", "sum"),
        net_usd=("pnl_usd",   "sum"),
    ).reset_index()
    daily["dow"] = pd.to_datetime(daily["date_only"]).dt.day_name().str[:3]
    daily["cum_usd"] = daily["net_usd"].cumsum()

    # Print daily table
    print(f"{'Date':<12} {'DOW':<4} {'Trd':>4} {'W':>3} {'TP':>3} {'SL':>3} "
          f"{'Gross':>8} {'Net pips':>9} {'Net USD':>11} {'Cum USD':>11}")
    print("-" * 80)
    for _, r in daily.iterrows():
        marker = ""
        if r["net_usd"] <= -kill_dollar:      marker = " 🚨"
        elif r["sls"] > 0:                     marker = " ⚠"
        elif r["net_usd"] > 200:               marker = " ✅"
        print(f"{str(r['date_only']):<12} {r['dow']:<4} "
              f"{int(r['trades']):>4} {int(r['wins']):>3} {int(r['tps']):>3} {int(r['sls']):>3} "
              f"{r['gross_pips']:>+8.2f} "
              f"{r['net_pips']:>+9.2f} ${r['net_usd']:>+10,.2f} ${r['cum_usd']:>+10,.2f}{marker}")
    print("-" * 80)

    # Totals
    totals = {
        "trades":     int(daily["trades"].sum()),
        "wins":       int(daily["wins"].sum()),
        "sls":        int(daily["sls"].sum()),
        "tps":        int(daily["tps"].sum()),
        "gross_pips": float(daily["gross_pips"].sum()),
        "net_pips":   float(daily["net_pips"].sum()),
        "net_usd":    float(daily["net_usd"].sum()),
        "days":       len(daily),
        "profitable": int((daily["net_usd"] > 0).sum()),
        "losing":     int((daily["net_usd"] < 0).sum()),
        "best_day":   float(daily["net_usd"].max()),
        "worst_day":  float(daily["net_usd"].min()),
    }
    totals["win_rate"]  = 100 * totals["wins"] / totals["trades"] if totals["trades"] else 0
    totals["avg_day"]   = totals["net_usd"] / totals["days"] if totals["days"] else 0
    totals["avg_trade"] = totals["net_usd"] / totals["trades"] if totals["trades"] else 0

    print(f"{'TOTAL':<17} {totals['trades']:>4} {totals['wins']:>3} {totals['tps']:>3} {totals['sls']:>3} "
          f"{totals['gross_pips']:>+8.2f} {totals['net_pips']:>+9.2f} ${totals['net_usd']:>+10,.2f}")
    print()

    # DD
    eq = daily["net_usd"].cumsum()
    dd_info = {
        "dd_usd":    float((eq - eq.cummax()).min()),
        "dd_pct":    100 * float((eq - eq.cummax()).min()) / args.balance,
        "kill_days": int((daily["net_usd"] <= -kill_dollar).sum()),
    }

    # Summary box
    print(f"┌─ SUMMARY {month_name} " + "─" * (60 - len(month_name)) + "┐")
    print(f"│  Spread        ${spread_used:.3f} ({spread_info['source'][:50]})")
    print(f"│  + Slippage    ${args.extra_slippage:.3f}")
    print(f"│  = Cost/trade  ${total_cost_per_trade:.3f} price = ${total_cost_per_trade * 100 * args.lot:.2f} USD")
    print(f"│  Trading days  {totals['days']} ({totals['profitable']} ✅  {totals['losing']} ⚠)")
    print(f"│  Trades        {totals['trades']}  ({totals['wins']} W / {totals['sls']} SL / {totals['tps']} TP)")
    print(f"│  Win rate      {totals['win_rate']:.2f}%")
    print(f"│  Gross pips    {totals['gross_pips']:+.2f}")
    print(f"│  Spread cost   {-totals['trades'] * total_cost_per_trade:+.2f} pips")
    print(f"│  Net pips      {totals['net_pips']:+.2f}")
    print(f"│  Net P&L       ${totals['net_usd']:+,.2f}  @ {args.lot} lot")
    print(f"│  Best day      ${totals['best_day']:+,.2f}")
    print(f"│  Worst day     ${totals['worst_day']:+,.2f}")
    print(f"│  Max DD        ${dd_info['dd_usd']:+,.2f}  ({dd_info['dd_pct']:+.2f}%)")
    print(f"│  Kill days     {dd_info['kill_days']}")
    print(f"│  Avg / day     ${totals['avg_day']:+,.2f}")
    print(f"│  Avg / trade   ${totals['avg_trade']:+,.2f}")
    print("└" + "─" * 79 + "┘")
    print()

    # Save outputs into per-month folder
    daily_path   = os.path.join(out_dir, "daily.csv")
    trades_path  = os.path.join(out_dir, "trades.csv")
    summary_path = os.path.join(out_dir, "summary.json")
    report_path  = os.path.join(out_dir, "report.md")

    daily.to_csv(daily_path, index=False)
    trades.to_csv(trades_path, index=False)

    summary = {
        "month":              str(first),
        "month_name":         month_name,
        "lot":                args.lot,
        "spread_used_dollars":      spread_used,
        "extra_slippage_dollars":   args.extra_slippage,
        "total_cost_per_trade":     round(total_cost_per_trade, 4),
        "spread_detection":   spread_info,
        "starting_balance":   args.balance,
        "daily_kill_pct":     args.daily_kill_pct,
        **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in totals.items()},
        "max_dd_usd":         round(dd_info["dd_usd"], 2),
        "max_dd_pct":         round(dd_info["dd_pct"], 2),
        "kill_switch_days":   dd_info["kill_days"],
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    write_report_md(report_path,
                    args=args, daily=daily, df=trades,
                    totals=totals, dd_info=dd_info, spread_info=spread_info)

    print(f"📁 All outputs in: {out_dir}/")
    print(f"   daily.csv      ({len(daily)} rows)")
    print(f"   trades.csv     ({len(trades)} trades)")
    print(f"   summary.json   (machine-readable)")
    print(f"   report.md      (human-readable)")
    print()


if __name__ == "__main__":
    main()