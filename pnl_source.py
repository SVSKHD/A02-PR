"""AUREON — the SINGLE SOURCE OF TRUTH for realized per-engine day P&L.

GROUND-TRUTH RULE (ERRORS.md): MT5 deal history is the ultimate authority. An engine's
realized day P&L = sum(profit + swap + commission) over its CLOSING deals (entry == 1),
filtered by MAGIC, for the broker day. That is the ONLY correct number; it is what
pnl_ledger.csv is rebuilt from, and it is what every surface must agree with.

This ONE reader is called by:
  - the E-20 governor rebuilds (rogue / fetcher / anchors) that seed the live day P&L,
  - the daily report's per-engine reconciliation,
  - the /status card + /daylock per-engine display,
  - the reconcile audit (pnl_reconcile.py),
so no surface can drift from another -- they are all derived from `magic_day_net`.

NOT a P&L source, ever: rogue_trades.csv / fetcher_trades.csv are DECISION LOGS (their
`outcome_dollars` is a price delta written live, not an account-dollar realized P&L).
Summing their rows is exactly the R-8 corruption the reconcile audit exists to catch.
"""
from __future__ import annotations

import logging

log = logging.getLogger("AUREON")

# One magic per engine (the anchor magic 20260522 covers its boost / rescue / F-B legs too).
ANCHORS_MAGIC = 20260522
ROGUE_MAGIC = 20260626
FETCHER_MAGIC = 20260707
ENGINE_MAGICS = (('anchors', ANCHORS_MAGIC), ('rogue', ROGUE_MAGIC), ('fetcher', FETCHER_MAGIC))


def deal_pnl(d):
    """PURE: realized $ of one MT5 deal = profit + swap + commission. Guarded -> 0.0."""
    return (float(getattr(d, 'profit', 0.0) or 0.0)
            + float(getattr(d, 'swap', 0.0) or 0.0)
            + float(getattr(d, 'commission', 0.0) or 0.0))


def _is_out(d):
    """True iff `d` is a CLOSING deal (MT5 entry == 1). Realized P&L lives on the OUT deal."""
    return getattr(d, 'entry', None) == 1


def _magic(d):
    try:
        return int(getattr(d, 'magic', 0) or 0)
    except Exception:
        return 0


def magic_day_net(deals, magic):
    """PURE, the SINGLE TRUTH: realized net over the CLOSING deals (entry == 1) whose magic
    matches `magic`, in `deals`. Summing ALL out deals (not last-out-per-position) so a
    partial close can never be dropped. Returns a 2dp float. Guarded per-deal."""
    m = int(magic)
    total = 0.0
    for d in (deals or []):
        try:
            if _magic(d) == m and _is_out(d):
                total += deal_pnl(d)
        except Exception:
            continue
    return round(total, 2)


def magic_day_entries(deals, magic):
    """PURE: number of OPENING deals (entry == 0) for `magic` -- the NEW-entry count the
    governors track. Guarded."""
    m = int(magic)
    n = 0
    for d in (deals or []):
        try:
            if _magic(d) == m and getattr(d, 'entry', None) == 0:
                n += 1
        except Exception:
            continue
    return n


def broker_day_range(trader, day=None):
    """(dt_from, dt_to) UTC datetimes bounding a broker day (default: the CURRENT broker day).
    Mirrors rogue/fetcher/daystops._broker_day_range so the window is single-sourced too.
    `day` may be a 'YYYY-MM-DD' string or a date/Timestamp. Guarded -> (None, None)."""
    try:
        import pandas as _pd
        off = float(getattr(trader.cfg, 'broker_tz_offset_hours', 0.0) or 0.0)
        if day is None:
            now_utc = _pd.Timestamp.now(tz='UTC')
            bdate = (now_utc + _pd.Timedelta(hours=off)).normalize()
        else:
            bdate = _pd.Timestamp(str(day)).normalize().tz_localize('UTC')
        dt_from = (bdate - _pd.Timedelta(hours=off)).to_pydatetime()
        dt_to = (bdate + _pd.Timedelta(days=1) - _pd.Timedelta(hours=off)).to_pydatetime()
        return dt_from, dt_to
    except Exception:
        return None, None


def fetch_day_deals(trader, dt_from=None, dt_to=None, day=None):
    """The ONE MT5 history read for a broker day: adapter.mt5.history_deals_get(from, to).
    Returns the raw deal list (possibly []), or None if the query itself fails (so callers
    can distinguish 'no deals' from 'history unavailable'). READ-ONLY; guarded."""
    try:
        if dt_from is None or dt_to is None:
            dt_from, dt_to = broker_day_range(trader, day=day)
        if dt_from is None:
            return None
        return list(trader.adapter.mt5.history_deals_get(dt_from, dt_to) or [])
    except Exception as e:
        log.warning(f"pnl_source: history query failed: {e!r}")
        return None


def engine_day_nets(trader, dt_from=None, dt_to=None, day=None, deals=None):
    """The authoritative per-engine + account realized day net from MT5 history, by magic.
    Returns {'anchors','rogue','fetcher','account'} in dollars, or None if history is
    unavailable (never fabricate a number from a failed query). `deals` may be passed to
    reuse a single fetch. READ-ONLY; guarded."""
    if deals is None:
        deals = fetch_day_deals(trader, dt_from, dt_to, day=day)
    if deals is None:
        return None
    a = magic_day_net(deals, ANCHORS_MAGIC)
    r = magic_day_net(deals, ROGUE_MAGIC)
    f = magic_day_net(deals, FETCHER_MAGIC)
    return {'anchors': a, 'rogue': r, 'fetcher': f, 'account': round(a + r + f, 2)}
