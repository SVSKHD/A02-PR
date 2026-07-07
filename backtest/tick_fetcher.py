"""AUREON backtest — tick fetcher / cache (v3.1.8).

Pulls a calendar month of XAUUSD ticks from MetaTrader5 on the VPS, caches them
to a parquet (or CSV fallback), and returns a tidy DataFrame with columns
[time, bid, ask] where `time` is a tz-aware UTC datetime.

NO MT5 / NO internet here (sandbox): if the import or initialize fails we return
None so back_main can fall back to `synthetic_month_ticks`, which produces a
deterministic, plausible XAUUSD tick stream so the whole pipeline is
demonstrable. On the VPS the real path runs unchanged.

TIME CONVENTION
---------------
MT5 tick 'time' is epoch SECONDS on the BROKER clock (Pepperstone = UTC+3). The
rest of AUREON treats broker time as UTC+3 (see utils.anchor_datetime_utc, which
maps a broker hour H to UTC H-3). To stay consistent we convert broker-epoch ->
true UTC by SUBTRACTING cfg.broker_tz_offset_hours. So a tick stamped broker
02:30 lands at UTC 23:30 the previous day, exactly where anchor_datetime_utc puts
the A1 02:30 anchor. The backtest replay therefore lines up bars and anchors in
one shared UTC frame.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

# Broker is UTC+3. Kept here only as the fetch-time default; the live offset of
# record is cfg.broker_tz_offset_hours and back_main passes the cfg through.
BROKER_TZ_OFFSET_HOURS = 3


# --------------------------------------------------------------------------- #
# cache helpers (parquet preferred, CSV fallback when pyarrow is unavailable)
# --------------------------------------------------------------------------- #
def _cache_paths(ticks_dir: str, year: int, month: int):
    base = os.path.join(ticks_dir, f"ticks_{year:04d}_{month:02d}")
    return base + ".parquet", base + ".csv"


def _load_cache(ticks_dir: str, year: int, month: int):
    parquet_path, csv_path = _cache_paths(ticks_dir, year, month)
    if os.path.exists(parquet_path):
        try:
            df = pd.read_parquet(parquet_path)
            return _coerce(df)
        except Exception as e:  # pyarrow missing / corrupt -> try CSV
            print(f"[fetcher] parquet read failed ({e!r}); trying CSV cache")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        return _coerce(df)
    return None


def _save_cache(df: pd.DataFrame, ticks_dir: str, year: int, month: int):
    os.makedirs(ticks_dir, exist_ok=True)
    parquet_path, csv_path = _cache_paths(ticks_dir, year, month)
    try:
        df.to_parquet(parquet_path, index=False)
        print(f"[fetcher] cached -> {parquet_path}")
        return
    except Exception as e:  # no pyarrow in this sandbox -> CSV fallback
        print(f"[fetcher] parquet write unavailable ({e!r}); using CSV cache")
    df.to_csv(csv_path, index=False)
    print(f"[fetcher] cached -> {csv_path}")


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a loaded frame: ensure tz-aware UTC `time`, float bid/ask."""
    df = df.copy()
    df['time'] = pd.to_datetime(df['time'], utc=True)
    df['bid'] = df['bid'].astype(float)
    df['ask'] = df['ask'].astype(float)
    return df.sort_values('time').reset_index(drop=True)


# --------------------------------------------------------------------------- #
# real fetch (VPS only)
# --------------------------------------------------------------------------- #
def fetch_month_ticks(symbol: str, year: int, month: int, ticks_dir: str,
                      broker_tz_offset_hours: int = BROKER_TZ_OFFSET_HOURS):
    """Return a DataFrame [time(UTC), bid, ask] for the given month.

    Cache-first: if ticks_dir/ticks_YYYY_MM.(parquet|csv) exists, load and return
    (prints "cache hit"). Otherwise fetch from MT5 chunked by day, convert
    broker-epoch -> UTC, cache, return. If MT5 is unavailable (sandbox), print a
    warning and return None so the caller falls back to synthetic ticks.
    """
    cached = _load_cache(ticks_dir, year, month)
    if cached is not None:
        print(f"[fetcher] cache hit ({year:04d}-{month:02d}, {len(cached):,} ticks)")
        return cached

    try:
        import MetaTrader5 as mt5
    except Exception as e:
        print(f"[fetcher] ⚠ MetaTrader5 import failed ({e!r}) — no MT5 in this "
              f"environment; returning None (caller falls back to synthetic).")
        return None

    if not mt5.initialize():
        print(f"[fetcher] ⚠ mt5.initialize() failed ({mt5.last_error()}) — "
              f"returning None (caller falls back to synthetic).")
        return None

    try:
        # Month span on the BROKER clock (the clock MT5 stamps ticks with).
        month_start = datetime(year, month, 1)
        if month == 12:
            month_end = datetime(year + 1, 1, 1)
        else:
            month_end = datetime(year, month + 1, 1)

        frames = []
        day = month_start
        while day < month_end:
            nxt = day + timedelta(days=1)
            # COPY_TICKS_INFO = bid/ask quote ticks; chunk DAY-by-DAY to respect
            # MT5's per-call tick ceiling.
            ticks = mt5.copy_ticks_range(symbol, day, nxt, mt5.COPY_TICKS_INFO)
            if ticks is not None and len(ticks):
                frames.append(pd.DataFrame(ticks))
            day = nxt

        if not frames:
            print(f"[fetcher] ⚠ MT5 returned no ticks for {year:04d}-{month:02d}; "
                  f"returning None.")
            return None

        raw = pd.concat(frames, ignore_index=True)
        # 'time' is broker-epoch seconds. broker = UTC+offset, so true UTC =
        # broker - offset (matches utils.anchor_datetime_utc). Build the UTC ts
        # from epoch then shift back by the offset.
        broker_dt = pd.to_datetime(raw['time'], unit='s', utc=True)
        utc_time = broker_dt - pd.Timedelta(hours=broker_tz_offset_hours)
        out = pd.DataFrame({
            'time': utc_time,
            'bid': raw['bid'].astype(float),
            'ask': raw['ask'].astype(float),
        })
        out = _coerce(out)
        _save_cache(out, ticks_dir, year, month)
        print(f"[fetcher] fetched {len(out):,} ticks for {year:04d}-{month:02d}")
        return out
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# synthetic fallback (deterministic) — sandbox / demo
# --------------------------------------------------------------------------- #
def synthetic_month_ticks(year: int, month: int,
                          broker_tz_offset_hours: int = BROKER_TZ_OFFSET_HOURS) -> pd.DataFrame:
    """Generate a deterministic, plausible XAUUSD tick stream for the business
    days of `month`. ~1 tick/sec across broker 00:00-23:00, a mean-reverting
    random walk around ~4300 with ~$0.20 spread and enough intraday movement that
    anchors fill and siblings occasionally fill too (so rescues/boosts occur).

    Deterministic: seeded by year*100+month so output is reproducible. Returns
    columns [time(UTC), bid, ask] in the SAME UTC frame as the real fetch (broker
    timestamps shifted back by broker_tz_offset_hours).
    """
    rng = np.random.default_rng(year * 100 + month)

    # business days of the month (broker calendar)
    month_start = pd.Timestamp(year=year, month=month, day=1)
    bdays = pd.date_range(month_start, month_start + pd.offsets.MonthEnd(0), freq='B')

    SEC_PER_DAY = 23 * 3600          # broker 00:00..23:00
    STRIDE = 1                       # ~1 tick/sec
    mid_level = 4300.0               # anchor price level
    HALF_SPREAD = 0.10               # ~$0.20 spread

    times = []
    bids = []
    asks = []

    for d in bdays:
        broker_date = d.normalize()
        # per-day vol regime: most days quiet, some trend, occasional whipsaw —
        # tuned so a meaningful fraction of anchors fill (move > $5 trigger) and
        # a slice see BOTH stops cross (siblings -> rescues/boosts).
        regime = rng.random()
        if regime < 0.45:
            sigma = 0.020      # quiet
            drift = rng.normal(0, 0.0008)
        elif regime < 0.80:
            sigma = 0.055      # trend
            drift = rng.normal(0, 0.0030)
        else:
            sigma = 0.090      # whipsaw / news
            drift = rng.normal(0, 0.0010)

        n = SEC_PER_DAY // STRIDE
        steps = rng.normal(drift, sigma, n)
        # gentle mean reversion to keep the level near ~4300 across the day
        mid = mid_level + np.cumsum(steps)
        mid = mid - np.linspace(0, (mid[-1] - mid_level) if n else 0.0, n) * 0.15

        # occasional intraday shock so some days swing both ways (whipsaw -> both
        # straddle legs cross -> sibling rescue + boosts)
        if regime >= 0.80 and n > 3 * 3600:
            shock_at = int(rng.integers(2 * 3600, n - 3600))
            direction = 1.0 if rng.random() < 0.5 else -1.0
            mag = rng.uniform(8.0, 16.0)
            mid[shock_at:shock_at + 1800] += direction * mag * np.linspace(0, 1, 1800)
            mid[shock_at + 1800:] -= direction * mag * 0.9  # snap back -> whipsaw

        # broker-clock timestamps, then shift to UTC (broker = UTC+offset)
        secs = np.arange(0, n * STRIDE, STRIDE)
        broker_ts = broker_date + pd.to_timedelta(secs, unit='s')
        utc_ts = broker_ts - pd.Timedelta(hours=broker_tz_offset_hours)

        spread_jitter = rng.uniform(0.0, 0.05, n)
        times.append(utc_ts)
        bids.append(mid - HALF_SPREAD - spread_jitter)
        asks.append(mid + HALF_SPREAD + spread_jitter)

    if not times:
        return pd.DataFrame(columns=['time', 'bid', 'ask'])

    out = pd.DataFrame({
        'time': pd.DatetimeIndex(np.concatenate([t.values for t in times])).tz_localize('UTC'),
        'bid': np.round(np.concatenate(bids), 2),
        'ask': np.round(np.concatenate(asks), 2),
    })
    return _coerce(out)
