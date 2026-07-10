"""AUREON simulator — PER-DAY tick cache + manifest (Part 1A).

`python bot.py fetchticks --from 2026-07-01 --to 2026-07-10`
  -> backtest/ticks/XAUUSD_YYYY-MM-DD.parquet  (one file per calendar day)
  -> backtest/ticks/manifest.json              (resolution ACTUALLY obtained per day)

WHY PER-DAY (not the month cache in tick_fetcher.py): the offline simulator
(Part 1B) replays a date RANGE and must know, per day, whether it is driving on
real TICKS or only M1 BARS -- a bar day cannot resolve intrabar wick ORDER, which
decides whether a trapped leg's SL fires before the winning side's trail locks.
The manifest records that resolution so every downstream report can state it and
never silently present a bar day as tick-accurate.

DATA / ENVIRONMENT REALITY
--------------------------
The real fetch needs a running MT5 terminal (Windows/VPS). In a sandbox with no
MetaTrader5 module the day fetch returns ('unavailable') and NOTHING is written --
the command reports honestly that no ticks were obtained rather than fabricating
data. The pure cache/manifest plumbing (idempotency, per-day split, resolution
tagging) is fully unit-tested with injected synthetic day-frames, so it is proven
here; only the live MT5 read cannot run off-VPS.

TIME CONVENTION: identical to tick_fetcher.py -- MT5 stamps ticks in broker-epoch
seconds (Pepperstone = UTC+3); we subtract cfg.broker_tz_offset_hours to land in
the SAME true-UTC frame as utils.anchor_datetime_utc, so anchors line up.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

BROKER_TZ_OFFSET_HOURS = 3

# manifest resolution values
RES_TICK = "tick"          # real quote ticks (COPY_TICKS_INFO) -- intrabar order known
RES_M1 = "M1"              # only M1 bars available -- intrabar order UNKNOWN (warn)
RES_UNAVAILABLE = "unavailable"  # no MT5 / no data -> nothing written for the day
RES_CACHE = "cache"        # already on disk (idempotent hit) -- resolution carried from before


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def day_cache_paths(ticks_dir: str, symbol: str, day: str):
    """(parquet, csv) paths for one day's cache file (csv is the no-pyarrow fallback)."""
    base = os.path.join(ticks_dir, f"{symbol}_{day}")
    return base + ".parquet", base + ".csv"


def manifest_path(ticks_dir: str) -> str:
    return os.path.join(ticks_dir, "manifest.json")


def _daterange(d_from: str, d_to: str):
    """Inclusive list of 'YYYY-MM-DD' calendar days from d_from..d_to."""
    a = datetime.strptime(d_from, "%Y-%m-%d").date()
    b = datetime.strptime(d_to, "%Y-%m-%d").date()
    if b < a:
        return []
    out = []
    cur = a
    while cur <= b:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# manifest (JSON: {date -> {resolution, rows, source, path, fetched_at}})
# --------------------------------------------------------------------------- #
def read_manifest(ticks_dir: str) -> dict:
    p = manifest_path(ticks_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("days", data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_manifest(ticks_dir: str, days: dict) -> str:
    os.makedirs(ticks_dir, exist_ok=True)
    p = manifest_path(ticks_dir)
    doc = {"symbol_dir": ticks_dir, "days": dict(sorted(days.items()))}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return p


# --------------------------------------------------------------------------- #
# writing / loading one day
# --------------------------------------------------------------------------- #
def _write_day(df: pd.DataFrame, ticks_dir: str, symbol: str, day: str) -> str:
    os.makedirs(ticks_dir, exist_ok=True)
    parquet_path, csv_path = day_cache_paths(ticks_dir, symbol, day)
    try:
        df.to_parquet(parquet_path, index=False)
        return parquet_path
    except Exception:
        df.to_csv(csv_path, index=False)
        return csv_path


def load_day(ticks_dir: str, symbol: str, day: str):
    """Return the cached day frame (parquet or csv) or None if absent."""
    parquet_path, csv_path = day_cache_paths(ticks_dir, symbol, day)
    for path, reader in ((parquet_path, pd.read_parquet), (csv_path, pd.read_csv)):
        if os.path.exists(path):
            try:
                df = reader(path)
                if "time" in df.columns:
                    df["time"] = pd.to_datetime(df["time"], utc=True)
                return df
            except Exception:
                continue
    return None


def day_on_disk(ticks_dir: str, symbol: str, day: str) -> bool:
    pq, cs = day_cache_paths(ticks_dir, symbol, day)
    return os.path.exists(pq) or os.path.exists(cs)


# --------------------------------------------------------------------------- #
# the impure per-day MT5 fetch (VPS only) -- ticks first, M1 fallback
# --------------------------------------------------------------------------- #
def mt5_day_fetch(symbol: str, day: str, broker_tz_offset_hours: int = BROKER_TZ_OFFSET_HOURS):
    """(df|None, resolution, source) for ONE broker day.

    Tries real quote ticks first (RES_TICK); falls back to M1 bars (RES_M1) so the
    day is at least replayable with a LOUD resolution flag; returns (None,
    RES_UNAVAILABLE, ...) when MT5 is absent or the day has no data. Never raises."""
    try:
        import MetaTrader5 as mt5
    except Exception as e:
        return None, RES_UNAVAILABLE, f"no-mt5 ({e!r})"
    started = False
    try:
        if not mt5.initialize():
            return None, RES_UNAVAILABLE, f"init-failed ({mt5.last_error()})"
        started = True
        d0 = datetime.strptime(day, "%Y-%m-%d")
        d1 = d0 + timedelta(days=1)
        off = pd.Timedelta(hours=broker_tz_offset_hours)
        ticks = mt5.copy_ticks_range(symbol, d0, d1, mt5.COPY_TICKS_INFO)
        if ticks is not None and len(ticks):
            raw = pd.DataFrame(ticks)
            utc = pd.to_datetime(raw["time"], unit="s", utc=True) - off
            df = pd.DataFrame({"time": utc, "bid": raw["bid"].astype(float),
                               "ask": raw["ask"].astype(float)}).sort_values("time")
            return df.reset_index(drop=True), RES_TICK, "mt5-ticks"
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, d0, d1)
        if rates is not None and len(rates):
            raw = pd.DataFrame(rates)
            utc = pd.to_datetime(raw["time"], unit="s", utc=True) - off
            df = pd.DataFrame({"time": utc, "open": raw["open"].astype(float),
                               "high": raw["high"].astype(float),
                               "low": raw["low"].astype(float),
                               "close": raw["close"].astype(float)}).sort_values("time")
            return df.reset_index(drop=True), RES_M1, "mt5-m1-fallback"
        return None, RES_UNAVAILABLE, "no-data"
    except Exception as e:
        return None, RES_UNAVAILABLE, f"error ({e!r})"
    finally:
        if started:
            try:
                mt5.shutdown()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# the range fetch (idempotent) -- PURE plumbing given an injectable day fetch
# --------------------------------------------------------------------------- #
def fetch_range(symbol, d_from, d_to, ticks_dir, broker_tz_offset_hours=BROKER_TZ_OFFSET_HOURS,
                day_fetch_fn=None, force=False, now_iso=None):
    """Cache every calendar day in [d_from, d_to] to its own file and (re)write the
    manifest. IDEMPOTENT: a day already on disk is not refetched unless force=True
    (its manifest row is preserved / marked RES_CACHE). `day_fetch_fn(symbol, day,
    offset) -> (df|None, resolution, source)` defaults to the MT5 reader; the
    selftest injects a synthetic one so the plumbing is provable without MT5.
    Returns {'days': {...manifest...}, 'summary': {...counts...}}. Never raises."""
    day_fetch_fn = day_fetch_fn or mt5_day_fetch
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    days = read_manifest(ticks_dir)
    counts = {RES_TICK: 0, RES_M1: 0, RES_UNAVAILABLE: 0, RES_CACHE: 0}

    for day in _daterange(d_from, d_to):
        if not force and day_on_disk(ticks_dir, symbol, day):
            cached = load_day(ticks_dir, symbol, day)
            rows = 0 if cached is None else int(len(cached))
            prev = days.get(day, {})
            # keep the original resolution if the manifest already recorded it
            res = prev.get("resolution", RES_CACHE)
            if res == RES_UNAVAILABLE:
                res = RES_CACHE
            pq, cs = day_cache_paths(ticks_dir, symbol, day)
            days[day] = {"resolution": res, "rows": rows,
                         "source": prev.get("source", "cache"),
                         "path": os.path.basename(pq if os.path.exists(pq) else cs),
                         "fetched_at": prev.get("fetched_at", now_iso)}
            counts[RES_CACHE] += 1
            continue

        df, resolution, source = day_fetch_fn(symbol, day, broker_tz_offset_hours)
        if df is None or len(df) == 0:
            days[day] = {"resolution": RES_UNAVAILABLE, "rows": 0, "source": source,
                         "path": None, "fetched_at": now_iso}
            counts[RES_UNAVAILABLE] += 1
            continue
        written = _write_day(df, ticks_dir, symbol, day)
        days[day] = {"resolution": resolution, "rows": int(len(df)), "source": source,
                     "path": os.path.basename(written), "fetched_at": now_iso}
        counts[resolution] = counts.get(resolution, 0) + 1

    write_manifest(ticks_dir, days)
    return {"days": days, "summary": counts}


def render_manifest_table(result) -> str:
    """PURE: a human table of the per-day resolution manifest for the CLI."""
    days = result.get("days", {})
    c = result.get("summary", {})
    lines = [f"tick cache manifest — {len(days)} day(s)",
             f"{'date':<12}{'resolution':<13}{'rows':>10}  source",
             "-" * 52]
    for day in sorted(days):
        e = days[day]
        lines.append(f"{day:<12}{e.get('resolution',''):<13}{e.get('rows',0):>10}  "
                     f"{e.get('source','')}")
    lines.append("-" * 52)
    lines.append(f"tick {c.get('tick',0)} · M1 {c.get('M1',0)} · "
                 f"cache {c.get('cache',0)} · unavailable {c.get('unavailable',0)}")
    if c.get(RES_M1):
        lines.append("⚠  M1 day(s) present — intrabar wick ORDER is UNKNOWN on those days; "
                     "the simulator must flag them, not present them as tick-accurate.")
    if c.get(RES_UNAVAILABLE):
        lines.append("⚠  unavailable day(s) — no MT5 / no data. Run on the VPS with a live "
                     "MT5 terminal to obtain real ticks.")
    return "\n".join(lines)


def run_cli(d_from, d_to, symbol="XAUUSD", ticks_dir=None, broker_tz_offset_hours=BROKER_TZ_OFFSET_HOURS,
            force=False):
    """`python bot.py fetchticks --from D1 --to D2`. Read-only w.r.t. run/. Writes
    ONLY under backtest/ticks/. Returns an exit code (0 = at least one day obtained
    real ticks; 1 = nothing real obtained, e.g. no MT5 in this environment)."""
    if ticks_dir is None:
        ticks_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticks")
    if not (d_from and d_to):
        print("fetchticks: --from and --to (YYYY-MM-DD) are required")
        return 2
    try:
        result = fetch_range(symbol, d_from, d_to, ticks_dir,
                             broker_tz_offset_hours=broker_tz_offset_hours, force=force)
    except Exception as e:
        print(f"fetchticks: error: {e!r}")
        return 2
    print(render_manifest_table(result))
    got_real = result["summary"].get(RES_TICK, 0) + result["summary"].get(RES_CACHE, 0)
    return 0 if got_real else 1
