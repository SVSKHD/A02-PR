#!/usr/bin/env python3
"""
AUREON v2 — Historic data fetcher.

Pulls XAUUSD M1 bars from MetaTrader 5 over an arbitrary date range, in
chunks (MT5 caps each call at ~99,999 bars), then saves a single CSV with
columns matching what bot.py's backtest engine expects:
    time, open, high, low, close, tick_volume, spread, real_volume

CLI usage
---------
    python fetch_data.py --days 365 --output data/XAUUSD_M1_last_year.csv

    python fetch_data.py \
        --start 2025-05-22 --end 2026-05-22 \
        --output data/XAUUSD_M1_2025-05-22_to_2026-05-22.csv

Library usage
-------------
    from fetch_data import fetch_m1
    df = fetch_m1(symbol='XAUUSD', days_back=365,
                  output_path='data/XAUUSD_M1.csv')

Prerequisite
------------
The MetaTrader 5 terminal must be running and logged into your broker
account BEFORE running this script. No credentials are passed — `mt5.initialize()`
inherits the active terminal session.

Notes
-----
- The fetcher is CHUNKED (30 days per call by default) because MT5 limits
  the number of bars per request. Chunks are deduplicated by timestamp before
  saving, so safe to overlap.
- Each chunk is logged to stderr with bar count for visibility.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd


log = logging.getLogger("AUREON-fetch")


# Columns we save, in this order
OUTPUT_COLS = ["time", "open", "high", "low", "close",
               "tick_volume", "spread", "real_volume"]

# Default chunk size — 30 days of M1 is ~43,200 bars, well under MT5's cap
CHUNK_DAYS = 30


def fetch_m1(symbol: str = "XAUUSD",
             days_back: Optional[int] = None,
             start: Optional[datetime] = None,
             end: Optional[datetime] = None,
             output_path: Optional[str] = None,
             chunk_days: int = CHUNK_DAYS) -> pd.DataFrame:
    """
    Fetch M1 bars from MT5 in chunked calls. Returns a DataFrame.
    If output_path is given, also writes the CSV.

    Exactly one of:
      - days_back  (e.g. 365)
      - (start, end)  pair of datetimes (UTC; naive treated as UTC)

    Connects to the already-running MT5 terminal (no credentials).
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise RuntimeError(
            "MetaTrader5 package not installed. "
            "Install with `pip install MetaTrader5` (Windows only)."
        )

    # Resolve date range
    if days_back is not None:
        end_utc = datetime.now(timezone.utc).replace(microsecond=0)
        start_utc = end_utc - timedelta(days=days_back)
    elif start is not None and end is not None:
        start_utc = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end_utc   = end   if end.tzinfo   else end.replace(tzinfo=timezone.utc)
    else:
        raise ValueError("Provide either days_back or (start, end)")

    log.info(f"Fetching {symbol} M1 from {start_utc} to {end_utc} "
             f"({(end_utc-start_utc).days} days)")

    # Connect to running MT5 terminal (no credentials)
    if not mt5.initialize():
        err = mt5.last_error()
        raise RuntimeError(
            f"MT5 init failed: {err}. "
            "Make sure the MT5 terminal is running and logged in."
        )
    info_acc = mt5.account_info()
    if info_acc is None:
        mt5.shutdown()
        raise RuntimeError(
            "MT5 connected but no account is logged in. "
            "Open the terminal and log in first."
        )
    log.info(f"MT5 connected: account #{info_acc.login} on {info_acc.server}")

    # Verify symbol
    info = mt5.symbol_info(symbol)
    if info is None:
        mt5.shutdown()
        raise RuntimeError(f"Symbol {symbol} not found on this broker")
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            mt5.shutdown()
            raise RuntimeError(f"Could not enable symbol {symbol}")

    # Chunked fetch
    chunks = []
    cum = 0
    chunk_start = start_utc
    idx = 0
    while chunk_start < end_utc:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end_utc)
        idx += 1
        bars = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, chunk_start, chunk_end)
        if bars is None or len(bars) == 0:
            log.warning(f"  Chunk {idx} ({chunk_start.date()}→{chunk_end.date()}): "
                        f"NO BARS  err={mt5.last_error()}")
        else:
            df = pd.DataFrame(bars)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            chunks.append(df)
            cum += len(df)
            log.info(f"  Chunk {idx:>2} ({chunk_start.date()}→{chunk_end.date()}): "
                     f"{len(df):>7,} bars  (cum {cum:>9,})")
        chunk_start = chunk_end

    mt5.shutdown()

    if not chunks:
        raise RuntimeError("No bars fetched")

    full = pd.concat(chunks, ignore_index=True)
    full = full.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    for c in OUTPUT_COLS:
        if c not in full.columns:
            full[c] = 0
    full = full[OUTPUT_COLS]

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        full.to_csv(output_path, index=False)
        size_mb = os.path.getsize(output_path) / 1e6
        log.info(f"Wrote {len(full):,} bars to {output_path} ({size_mb:.1f} MB)")

    return full


def main():
    # Load .env if present
    from env_loader import load_env
    load_env()

    p = argparse.ArgumentParser(description="Fetch XAUUSD M1 historic bars from MT5")
    rng = p.add_mutually_exclusive_group(required=True)
    rng.add_argument("--days", type=int, help="rolling window in days from now")
    rng.add_argument("--start", help="YYYY-MM-DD (use with --end)")
    p.add_argument("--end", help="YYYY-MM-DD (used with --start)")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--output", required=True, help="output CSV path")
    p.add_argument("--chunk-days", type=int, default=CHUNK_DAYS)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.days:
        kwargs = {"days_back": args.days}
    elif args.start and args.end:
        kwargs = {
            "start": datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
            "end":   datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
        }
    else:
        log.error("Must specify --days N or --start YYYY-MM-DD --end YYYY-MM-DD")
        sys.exit(2)

    try:
        fetch_m1(symbol=args.symbol,
                 output_path=args.output,
                 chunk_days=args.chunk_days,
                 **kwargs)
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
