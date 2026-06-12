"""
AUREON tick exporter — pulls 1 year of XAUUSD ticks (bid/ask/spread) from MT5.
Run on the Windows VPS where the MT5 terminal is logged in.

Output: XAUUSD_ticks_1y.csv with columns:
    time_msc, time, bid, ask, spread, last, volume

WHY week-chunked: a year of gold ticks = tens of millions of rows. One big
copy_ticks_range call will run out of memory or time out. We pull 7 days at a
time and append to the CSV so memory stays flat and a failure mid-run only
loses the current week, not everything.

Usage:
    python export_ticks.py                 # defaults to XAUUSD
    python export_ticks.py XAUUSD          # explicit
    python export_ticks.py XAGUSD          # silver
    python export_ticks.py XAUUSD.r        # if your broker uses a suffix

Output filename is derived from the symbol automatically:
    XAUUSD   -> XAUUSD_ticks_1y.csv
    XAGUSD   -> XAGUSD_ticks_1y.csv
    XAUUSD.r -> XAUUSD_r_ticks_1y.csv   (dots replaced so it's a valid filename)
"""

import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone
import pandas as pd
import os, sys, time

# ---- config ----
SYMBOL    = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"   # pass symbol as first arg
START     = datetime(2025, 6, 1, tzinfo=timezone.utc)
END       = datetime(2026, 6, 1, tzinfo=timezone.utc)
# output name derived from the symbol; sanitize chars that aren't filename-safe
_safe     = SYMBOL.replace(".", "_").replace("/", "_").replace("\\", "_")
OUT       = f"{_safe}_ticks_1y.csv"
CHUNK_DAYS = 7                  # pull one week at a time
# ----------------

def main():
    print(f"Exporting ticks: symbol={SYMBOL}  range={START.date()} -> {END.date()}  out={OUT}")
    if not mt5.initialize():
        print("initialize() FAILED:", mt5.last_error())
        print("Make sure the MT5 terminal is OPEN and logged in to the demo account.")
        sys.exit(1)

    # confirm the symbol exists and is selected (must be in Market Watch to pull ticks)
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        print(f"Symbol {SYMBOL} not found. Available gold symbols:")
        for s in mt5.symbols_get("*XAU*"):
            print("   ", s.name)
        mt5.shutdown(); sys.exit(1)
    if not info.visible:
        mt5.symbol_select(SYMBOL, True)
        time.sleep(1)

    # fresh file with header
    if os.path.exists(OUT):
        os.remove(OUT)
    header_written = False

    total_rows = 0
    cur = START
    while cur < END:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS), END)
        # COPY_TICKS_ALL = every tick (bid AND ask changes). time_msc = ms precision.
        ticks = mt5.copy_ticks_range(SYMBOL, cur, chunk_end, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            print(f"  {cur.date()} -> {chunk_end.date()}: 0 ticks ({mt5.last_error()})")
            cur = chunk_end
            continue

        df = pd.DataFrame(ticks)
        # millisecond timestamp -> readable time (UTC)
        df["time"] = pd.to_datetime(df["time_msc"], unit="ms", utc=True)
        # spread in PRICE terms = ask - bid (gold: e.g. 0.11). Round to avoid float noise.
        df["spread"] = (df["ask"] - df["bid"]).round(3)
        # keep only the columns we need for the backtest
        cols = ["time_msc", "time", "bid", "ask", "spread", "last", "volume"]
        cols = [c for c in cols if c in df.columns]
        df = df[cols]

        df.to_csv(OUT, mode="a", header=not header_written, index=False)
        header_written = True
        total_rows += len(df)
        print(f"  {cur.date()} -> {chunk_end.date()}: {len(df):,} ticks  (running total {total_rows:,})")
        cur = chunk_end

    mt5.shutdown()
    size_mb = os.path.getsize(OUT) / 1e6
    print(f"\nDONE. {total_rows:,} ticks -> {OUT} ({size_mb:.0f} MB)")
    print("Upload that CSV back to the chat for a real tick-level backtest.")
    if size_mb > 400:
        print("NOTE: file is large. If upload is hard, zip it first, or send 1 month at a time.")

if __name__ == "__main__":
    main()
