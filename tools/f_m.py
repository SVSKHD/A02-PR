#!/usr/bin/env python3
"""
AUREON tick fetcher — pull XAUUSD ticks from the running MT5 terminal to a CSV.

Usage
-----
  python f_m.py --start 2026-06-01 --end 2026-06-06
  python f_m.py --start 2026-06-01                # end defaults to NOW
  python f_m.py --start 2026-06-01 --end 2026-06-06 --out ticks_week1.csv

Notes
-----
- --end is automatically clamped to "now" so you can't ask for future ticks
  (which return empty). If you pass --end 2026-06-30 on June 6, it fetches
  only up to June 6.
- Large ranges are fetched WEEK BY WEEK and concatenated, so a big month pull
  can't hit MT5's single copy_ticks_range cap.
- MT5 terminal must be running and logged in before you run this.

Then feed the CSV to the backtester:
  python aureon_month_bt.py --csv-ticks ticks_june.csv --start 2026-06-01 --end 2026-06-06
"""

import argparse
import csv
from datetime import datetime, timedelta, timezone

SYMBOL = "XAUUSD"
BROKER_OFFSET = 3   # broker = UTC+3. Set 0 if your broker sends real UTC.


def parse_date(s: str) -> datetime:
    # accept YYYY-MM-DD (whole day) — time set to 00:00 UTC
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def main():
    import MetaTrader5 as mt5

    ap = argparse.ArgumentParser(description="Fetch XAUUSD ticks from MT5 to CSV")
    ap.add_argument('--start', required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument('--end', default=None,
                    help="YYYY-MM-DD (inclusive end-of-day). Defaults to now; "
                         "always clamped so it never exceeds now.")
    ap.add_argument('--out', default='ticks_june.csv', help="Output CSV path")
    ap.add_argument('--symbol', default=SYMBOL)
    ap.add_argument('--broker-offset', type=float, default=BROKER_OFFSET,
                    help="Hours the broker tick.time is ahead of UTC (UTC+3 -> 3, real-UTC -> 0)")
    args = ap.parse_args()

    start = parse_date(args.start)
    now = datetime.now(timezone.utc)
    if args.end:
        end = parse_date(args.end) + timedelta(hours=23, minutes=59)  # end-of-day
    else:
        end = now
    # CLAMP: never fetch into the future
    if end > now:
        print(f"⚠ --end {end.date()} is in the future; clamping to now ({now:%Y-%m-%d %H:%M} UTC)")
        end = now
    if start >= end:
        raise SystemExit(f"start {start} is not before end {end}")

    if not mt5.initialize():
        raise SystemExit(f"MT5 init failed: {mt5.last_error()} — is the terminal running and logged in?")

    off = args.broker_offset
    print(f"Fetching {args.symbol} ticks {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M} UTC "
          f"(broker offset {off:+.0f}h), weekly chunks...")

    total = 0
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "bid", "ask"])

        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=7), end)
            # broker encodes times as broker-local-unix; add offset to the query window
            send0 = chunk_start + timedelta(hours=off)
            send1 = chunk_end + timedelta(hours=off)
            ticks = mt5.copy_ticks_range(args.symbol, send0, send1, mt5.COPY_TICKS_ALL)
            got = 0 if ticks is None else len(ticks)
            print(f"  {chunk_start:%Y-%m-%d} .. {chunk_end:%Y-%m-%d}: {got:,} raw ticks")
            if ticks is not None:
                for t in ticks:
                    ts = datetime.fromtimestamp(t["time"], tz=timezone.utc) - timedelta(hours=off)
                    bid, ask = float(t["bid"]), float(t["ask"])
                    if bid > 0 and ask > 0:
                        w.writerow([ts.isoformat(), bid, ask]); total += 1
            chunk_start = chunk_end

    mt5.shutdown()

    if total == 0:
        raise SystemExit("No ticks written. Check symbol name, date range, and broker offset.")
    print(f"Wrote {total:,} ticks to {args.out}")
    print(f"Now run: python aureon_month_bt.py --csv-ticks {args.out} "
          f"--start {start:%Y-%m-%d} --end {end:%Y-%m-%d}")


if __name__ == '__main__':
    main()