#!/usr/bin/env python3
"""
AUREON v2 — Strategy-research data fetcher (multi-symbol, multi-timeframe).

This is separate from the production `fetch_data.py`:

  fetch_data.py    Hardcoded XAUUSD M1 fetcher used by auto_analyze.py daily.
  fetch_lab.py     Flexible: any symbol(s), any timeframe(s), any date range —
                   for developing and testing NEW strategies on different
                   instruments and timeframes.

Examples
--------
    # Single symbol, single timeframe
    python fetch_lab.py --symbol BTCUSD --timeframe M5 --days 365

    # Multiple symbols at one timeframe
    python fetch_lab.py --symbols XAUUSD,EURUSD,NAS100 --timeframe M1 \
        --start 2025-01-01 --end 2026-05-22

    # Multiple symbols × multiple timeframes (Cartesian product)
    python fetch_lab.py --symbols XAUUSD,EURUSD --timeframes M1,M5,H1 --days 365

    # Custom output dir, skip files that already exist
    python fetch_lab.py --symbol XAUUSD --timeframe M1 --days 365 \
        --output-dir ./research_data --skip-existing

Output structure
----------------
    {output_dir}/{symbol}/{symbol}_{timeframe}_{start}_to_{end}.csv

Each file has columns: time, open, high, low, close, tick_volume, spread, real_volume
(compatible with bot.py's run_backtest() and strategy_template.py's run_backtest()).

Prerequisite
------------
The MetaTrader 5 terminal must be running and logged into your broker
account BEFORE running this script. No credentials are passed — mt5.initialize()
inherits the active terminal session.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd


log = logging.getLogger("FETCH-LAB")


# Columns saved, in this exact order
OUTPUT_COLS = ["time", "open", "high", "low", "close",
               "tick_volume", "spread", "real_volume"]

# Map our names to MT5 timeframe constants (resolved lazily after import)
TIMEFRAMES = {
    "M1":  ("TIMEFRAME_M1",  1),
    "M5":  ("TIMEFRAME_M5",  5),
    "M15": ("TIMEFRAME_M15", 15),
    "M30": ("TIMEFRAME_M30", 30),
    "H1":  ("TIMEFRAME_H1",  60),
    "H4":  ("TIMEFRAME_H4",  240),
    "D1":  ("TIMEFRAME_D1",  1440),
    "W1":  ("TIMEFRAME_W1",  10080),
    "MN1": ("TIMEFRAME_MN1", 43200),
}

# Chunk size in days — smaller chunks for higher-frequency data.
# Sized so each chunk returns < ~50k bars (MT5 hard cap ~99k/call).
CHUNK_DAYS = {
    "M1":  30,
    "M5":  90,
    "M15": 180,
    "M30": 365,
    "H1":  730,
    "H4":  3650,
    "D1":  10000,
    "W1":  10000,
    "MN1": 10000,
}


# ============================================================================
# Fetch routine
# ============================================================================

def fetch_symbol_timeframe(mt5, symbol: str, tf_name: str,
                           start: datetime, end: datetime) -> Optional[pd.DataFrame]:
    """Fetch one (symbol, timeframe) pair in chunks. Returns DataFrame or None."""
    tf_attr, _ = TIMEFRAMES[tf_name]
    tf_const = getattr(mt5, tf_attr)
    chunk_days = CHUNK_DAYS[tf_name]

    # Make symbol visible if not already
    info = mt5.symbol_info(symbol)
    if info is None:
        log.error(f"  Symbol {symbol} not found on this broker")
        return None
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            log.error(f"  Could not enable symbol {symbol}")
            return None

    chunks: List[pd.DataFrame] = []
    total = 0
    chunk_start = start
    idx = 0
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
        idx += 1
        bars = mt5.copy_rates_range(symbol, tf_const, chunk_start, chunk_end)
        if bars is None or len(bars) == 0:
            log.warning(f"  Chunk {idx} ({chunk_start.date()}→{chunk_end.date()}): "
                        f"NO BARS  err={mt5.last_error()}")
        else:
            df = pd.DataFrame(bars)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            chunks.append(df)
            total += len(df)
            log.info(f"  Chunk {idx:>2} ({chunk_start.date()}→{chunk_end.date()}): "
                     f"{len(df):>7,} bars  (cum {total:>9,})")
        chunk_start = chunk_end

    if not chunks:
        return None

    full = pd.concat(chunks, ignore_index=True)
    full = full.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    # Ensure all expected columns exist (some symbols may not have spread/real_volume)
    for c in OUTPUT_COLS:
        if c not in full.columns:
            full[c] = 0
    return full[OUTPUT_COLS]


# ============================================================================
# CLI
# ============================================================================

def parse_list(value: Optional[str]) -> List[str]:
    if not value: return []
    return [x.strip() for x in value.split(",") if x.strip()]


def resolve_window(args) -> Tuple[datetime, datetime]:
    if args.days:
        end_dt = datetime.now(timezone.utc).replace(microsecond=0)
        start_dt = end_dt - timedelta(days=args.days)
    elif args.start and args.end:
        start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        end_dt   = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    else:
        log.error("Specify --days N or --start YYYY-MM-DD --end YYYY-MM-DD")
        sys.exit(2)
    return start_dt, end_dt


def main():
    p = argparse.ArgumentParser(description="Multi-symbol multi-timeframe MT5 data fetcher",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sym = p.add_mutually_exclusive_group(required=True)
    sym.add_argument("--symbol",  help="single symbol (e.g. XAUUSD)")
    sym.add_argument("--symbols", help="comma-separated symbols")
    tf = p.add_mutually_exclusive_group(required=True)
    tf.add_argument("--timeframe",  help="single timeframe",
                    choices=list(TIMEFRAMES.keys()))
    tf.add_argument("--timeframes", help="comma-separated timeframes")
    rng = p.add_mutually_exclusive_group(required=True)
    rng.add_argument("--days",  type=int, help="rolling window in days from now")
    rng.add_argument("--start", help="YYYY-MM-DD (use with --end)")
    p.add_argument("--end",     help="YYYY-MM-DD (used with --start)")

    p.add_argument("--output-dir", default="./research_data")
    p.add_argument("--skip-existing", action="store_true",
                   help="skip (symbol,timeframe) pairs whose output file already exists")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    symbols = [args.symbol] if args.symbol else parse_list(args.symbols)
    timeframes = [args.timeframe] if args.timeframe else [t.upper() for t in parse_list(args.timeframes)]
    for tf in timeframes:
        if tf not in TIMEFRAMES:
            log.error(f"Unknown timeframe: {tf}. Valid: {list(TIMEFRAMES.keys())}")
            sys.exit(2)

    start_dt, end_dt = resolve_window(args)

    log.info(f"Window:     {start_dt.date()} → {end_dt.date()}  ({(end_dt-start_dt).days} days)")
    log.info(f"Symbols:    {symbols}")
    log.info(f"Timeframes: {timeframes}")
    log.info(f"Output dir: {args.output_dir}")
    log.info(f"Total pairs to fetch: {len(symbols) * len(timeframes)}")

    try:
        import MetaTrader5 as mt5
    except ImportError:
        log.error("MetaTrader5 package not installed. "
                  "Install with `pip install MetaTrader5` (Windows only).")
        sys.exit(1)

    if not mt5.initialize():
        log.error(f"MT5 init failed: {mt5.last_error()}. "
                  "Make sure the MT5 terminal is running and logged in.")
        sys.exit(1)
    info_acc = mt5.account_info()
    if info_acc is None:
        log.error("MT5 connected but no account is logged in. "
                  "Open the terminal and log into your broker first.")
        mt5.shutdown()
        sys.exit(1)
    log.info(f"MT5 connected: account #{info_acc.login} on {info_acc.server}")

    summary: List[Dict] = []
    for sym in symbols:
        sym_dir = os.path.join(args.output_dir, sym)
        os.makedirs(sym_dir, exist_ok=True)
        for tf in timeframes:
            fname = f"{sym}_{tf}_{start_dt.date()}_to_{end_dt.date()}.csv"
            out_path = os.path.join(sym_dir, fname)
            log.info("")
            if args.skip_existing and os.path.exists(out_path):
                log.info(f"[SKIP] {sym} {tf} — already exists at {out_path}")
                continue
            log.info(f"[FETCH] {sym} {tf}")
            df = fetch_symbol_timeframe(mt5, sym, tf, start_dt, end_dt)
            if df is None or len(df) == 0:
                log.error(f"  No data for {sym} {tf}")
                summary.append({"symbol": sym, "tf": tf, "bars": 0})
                continue
            df.to_csv(out_path, index=False)
            size_mb = os.path.getsize(out_path) / 1e6
            log.info(f"  Saved {len(df):,} bars to {out_path}  ({size_mb:.1f} MB)")
            summary.append({
                "symbol": sym, "tf": tf, "bars": len(df),
                "first": df["time"].iloc[0], "last": df["time"].iloc[-1],
                "size_mb": size_mb, "path": out_path,
            })

    mt5.shutdown()

    log.info("")
    log.info("=" * 78)
    log.info("FETCH SUMMARY")
    log.info("=" * 78)
    log.info(f"  {'Symbol':<10} {'TF':<5} {'Bars':>10} {'MB':>6}  {'First bar':<22} {'Last bar':<22}")
    log.info("  " + "-" * 76)
    for s in summary:
        if s.get("bars", 0) > 0:
            log.info(f"  {s['symbol']:<10} {s['tf']:<5} {s['bars']:>10,} "
                     f"{s['size_mb']:>6.1f}  {str(s['first']):<22} {str(s['last']):<22}")
        else:
            log.info(f"  {s['symbol']:<10} {s['tf']:<5} {'NO DATA':>10}")
    total_files = sum(1 for s in summary if s.get("bars", 0) > 0)
    total_bars  = sum(s.get("bars", 0) for s in summary)
    log.info("  " + "-" * 76)
    log.info(f"  TOTAL: {total_files} files, {total_bars:,} bars")
    log.info("")
    log.info(f"Files saved under: {args.output_dir}/")
    log.info("Next step: feed these CSVs into strategy_template.py or bot.py")


if __name__ == "__main__":
    main()
