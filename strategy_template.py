#!/usr/bin/env python3
"""
AUREON v2 — Strategy research template.

Scaffold for developing a NEW strategy on data fetched via fetch_lab.py.

This is intentionally simple and unopinionated — write your entry/exit logic
in the `Strategy` class below, run it against any CSV from fetch_lab.py,
get a per-trade record and summary stats.

Workflow
--------
    1. Fetch data:
       python fetch_lab.py --symbol XAUUSD --timeframe M5 --days 365

    2. Edit the `Strategy` class below to express your idea.
       Two callbacks:
         on_bar(bar, ts, pos)         — when flat: decide whether to OPEN_BUY / OPEN_SELL
         manage_position(pos, bar, ts) — when in trade: update SL/TP, return exit reason or None

    3. Run:
       python strategy_template.py \
           --csv research_data/XAUUSD/XAUUSD_M5_2025-05-22_to_2026-05-22.csv

    4. Iterate — tweak params in `Strategy`, re-run, look at stats.

Output
------
    {output_dir}/trades.csv  — every trade
    {output_dir}/stats.json  — summary stats

The skeleton example: open BUY every 1000 bars, manage with fixed $20 SL/TP.
Replace with your actual logic.
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import pandas as pd


log = logging.getLogger("STRATEGY")


# ============================================================================
# Action + Position primitives
# ============================================================================

class Action(Enum):
    STAY      = "STAY"
    OPEN_BUY  = "OPEN_BUY"
    OPEN_SELL = "OPEN_SELL"


@dataclass
class Position:
    side: str             # 'BUY' or 'SELL'
    entry: float
    entry_time: pd.Timestamp
    sl: float
    tp: float
    max_fav: float        # peak favorable price (for trail logic)

    def pnl(self, exit_price: float) -> float:
        return (exit_price - self.entry) if self.side == "BUY" else (self.entry - exit_price)


# ============================================================================
# ============ YOUR STRATEGY GOES HERE — EDIT THE CLASS BELOW ===============
# ============================================================================

@dataclass
class Strategy:
    """
    Edit these two methods to express your strategy.
    Use self.state (a plain dict) to persist anything across bars
    (running averages, last anchor price, signal counters, etc.).
    """

    # Tunable parameters — edit or accept via CLI
    sl_dist: float = 20.0
    tp_dist: float = 20.0
    trail_gap: float = 0.30          # tighten SL behind peak (set 0 to disable trail)
    be_trigger: float = 0.30         # arm trail when fav move exceeds this

    # Mutable state (do not edit — used by your callbacks)
    state: Dict = field(default_factory=dict)

    # ------------------------------------------------------------------------
    # Entry logic — called every bar while FLAT
    # ------------------------------------------------------------------------
    def on_bar(self, bar: pd.Series, ts: pd.Timestamp) -> Action:
        """
        Return Action.OPEN_BUY / OPEN_SELL / STAY.
        EXAMPLE: enter long every 1000 bars. REPLACE with your real logic.

        Bar fields available: bar.open, bar.high, bar.low, bar.close,
        bar.tick_volume, bar.spread, bar.real_volume
        """
        n = self.state.get("bar_count", 0) + 1
        self.state["bar_count"] = n
        if n % 1000 == 0:
            return Action.OPEN_BUY
        return Action.STAY

    # ------------------------------------------------------------------------
    # Exit logic — called every bar while POSITION OPEN
    # ------------------------------------------------------------------------
    def manage_position(self, pos: Position, bar: pd.Series, ts: pd.Timestamp) -> Optional[str]:
        """
        Update pos.sl in-place to trail.
        Return 'SL', 'TP', 'Trail', or None to keep going.

        DEFAULT IMPLEMENTATION: AUREON-style continuous trail behind peak,
        pessimistic pre-bar SL check (matches the AUREON v2 backtest engine).
        Replace if your strategy uses different exit logic.
        """
        # 1. PRE-BAR SL CHECK (pessimistic — assume intrabar worst order)
        if pos.side == "BUY":
            if bar.low <= pos.sl:
                # was SL? Check if it ever moved off the initial level
                is_initial = abs(pos.sl - (pos.entry - self.sl_dist)) < 0.01
                return "SL" if is_initial else "Trail"
        else:
            if bar.high >= pos.sl:
                is_initial = abs(pos.sl - (pos.entry + self.sl_dist)) < 0.01
                return "SL" if is_initial else "Trail"

        # 2. UPDATE PEAK FAVORABLE
        if pos.side == "BUY":
            if bar.high > pos.max_fav: pos.max_fav = bar.high
            fav = pos.max_fav - pos.entry
        else:
            if bar.low < pos.max_fav: pos.max_fav = bar.low
            fav = pos.entry - pos.max_fav
        fav = max(fav, 0.0)

        # 3. TRAIL — once fav crosses be_trigger, ratchet SL behind peak
        if self.trail_gap > 0 and fav >= self.be_trigger:
            if pos.side == "BUY":
                candidate = max(pos.entry, pos.max_fav - self.trail_gap)
                if candidate > pos.sl:
                    pos.sl = candidate
            else:
                candidate = min(pos.entry, pos.max_fav + self.trail_gap)
                if candidate < pos.sl:
                    pos.sl = candidate

        # 4. TP check
        if pos.side == "BUY":
            if bar.high >= pos.tp: return "TP"
        else:
            if bar.low <= pos.tp: return "TP"

        return None


# ============================================================================
# Generic backtest engine — usually no need to edit
# ============================================================================

def run_backtest(df: pd.DataFrame, strategy: Strategy,
                 lot_size: float = 0.5,
                 contract_size: float = 100) -> Tuple[List[Dict], Dict]:
    """
    Walk a DataFrame of bars through the strategy. Returns (trades, stats).
    """
    trades: List[Dict] = []
    pos: Optional[Position] = None

    for ts, bar in df.iterrows():
        if pos is None:
            action = strategy.on_bar(bar, ts)
            if action == Action.OPEN_BUY:
                entry = float(bar.close)
                pos = Position(side="BUY", entry=entry, entry_time=ts,
                               sl=entry - strategy.sl_dist,
                               tp=entry + strategy.tp_dist,
                               max_fav=entry)
            elif action == Action.OPEN_SELL:
                entry = float(bar.close)
                pos = Position(side="SELL", entry=entry, entry_time=ts,
                               sl=entry + strategy.sl_dist,
                               tp=entry - strategy.tp_dist,
                               max_fav=entry)
        else:
            outcome = strategy.manage_position(pos, bar, ts)
            if outcome is not None:
                # Determine exit price
                if outcome == "SL" or outcome == "Trail":
                    exit_price = pos.sl
                elif outcome == "TP":
                    exit_price = pos.tp
                else:
                    exit_price = float(bar.close)
                pnl = pos.pnl(exit_price)
                trades.append({
                    "entry_time":   pos.entry_time,
                    "exit_time":    ts,
                    "side":         pos.side,
                    "entry":        round(pos.entry, 2),
                    "exit":         round(exit_price, 2),
                    "max_fav":      round(pos.max_fav, 2),
                    "outcome":      outcome,
                    "pnl_dist":     round(pnl, 3),
                    "pnl_usd":      round(pnl * contract_size * lot_size, 2),
                    "bars_held":    int((ts - pos.entry_time).total_seconds() / 60),
                })
                pos = None

    # ------- Stats -------
    tdf = pd.DataFrame(trades)
    if len(tdf) == 0:
        return trades, {"trades": 0}

    wins = tdf[tdf["pnl_usd"] > 0]
    losses = tdf[tdf["pnl_usd"] < 0]
    eq = tdf["pnl_usd"].cumsum()
    dd = (eq - eq.cummax()).min()

    stats = {
        "trades":            len(tdf),
        "wins":              int(len(wins)),
        "losses":            int(len(losses)),
        "win_rate_pct":      round(100 * len(wins) / len(tdf), 2),
        "total_pnl_dist":    round(tdf["pnl_dist"].sum(), 2),
        "total_pnl_usd":     round(tdf["pnl_usd"].sum(), 2),
        "avg_win_usd":       round(wins["pnl_usd"].mean(), 2) if len(wins) else 0.0,
        "avg_loss_usd":      round(losses["pnl_usd"].mean(), 2) if len(losses) else 0.0,
        "max_win_usd":       round(tdf["pnl_usd"].max(), 2),
        "max_loss_usd":      round(tdf["pnl_usd"].min(), 2),
        "max_drawdown_usd":  round(dd, 2),
        "avg_bars_held":     round(tdf["bars_held"].mean(), 1),
        "outcomes":          tdf["outcome"].value_counts().to_dict(),
    }
    return trades, stats


def monthly_summary(trades: List[Dict]) -> pd.DataFrame:
    df = pd.DataFrame(trades)
    if len(df) == 0: return df
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["month"] = df["entry_time"].dt.to_period("M").astype(str)
    return df.groupby("month").agg(
        trades=("pnl_usd", "count"),
        wins=("pnl_usd", lambda x: (x > 0).sum()),
        pnl_usd=("pnl_usd", "sum"),
        pnl_dist=("pnl_dist", "sum"),
    )


# ============================================================================
# CLI
# ============================================================================

def main():
    p = argparse.ArgumentParser(description="Strategy research backtester")
    p.add_argument("--csv", required=True,
                   help="Path to OHLC CSV (from fetch_lab.py)")
    p.add_argument("--lot", type=float, default=0.5)
    p.add_argument("--contract-size", type=float, default=100,
                   help="oz per 1.0 lot (100 for gold, 100000 for FX, 1 for crypto)")
    p.add_argument("--sl",        type=float, default=20.0)
    p.add_argument("--tp",        type=float, default=20.0)
    p.add_argument("--trail",     type=float, default=0.30)
    p.add_argument("--be",        type=float, default=0.30)
    p.add_argument("--output-dir", default="./strategy_output")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    log.info(f"Loading {args.csv}")
    df = pd.read_csv(args.csv)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    log.info(f"Loaded {len(df):,} bars from {df.index.min()} to {df.index.max()}")

    strategy = Strategy(
        sl_dist=args.sl, tp_dist=args.tp,
        trail_gap=args.trail, be_trigger=args.be,
    )
    log.info(f"Strategy params: SL=${strategy.sl_dist} TP=${strategy.tp_dist} "
             f"trail=${strategy.trail_gap} be_trigger=${strategy.be_trigger}")

    trades, stats = run_backtest(df, strategy,
                                 lot_size=args.lot,
                                 contract_size=args.contract_size)

    log.info("=" * 60)
    log.info("BACKTEST SUMMARY")
    log.info("=" * 60)
    for k, v in stats.items():
        if k == "outcomes":
            log.info(f"  {k:<22} {v}")
        else:
            log.info(f"  {k:<22} {v}")

    monthly = monthly_summary(trades)
    if len(monthly):
        log.info("")
        log.info("MONTHLY")
        log.info("-" * 50)
        for m, r in monthly.iterrows():
            log.info(f"  {m}  trades={int(r['trades']):>3}  wins={int(r['wins']):>3}  "
                     f"USD=${r['pnl_usd']:>+9,.2f}  pips={r['pnl_dist']:>+8.2f}")

    os.makedirs(args.output_dir, exist_ok=True)
    pd.DataFrame(trades).to_csv(os.path.join(args.output_dir, "trades.csv"), index=False)
    with open(os.path.join(args.output_dir, "stats.json"), "w") as f:
        json.dump(stats, f, indent=2, default=str)
    log.info(f"\nWrote {args.output_dir}/trades.csv and stats.json")


if __name__ == "__main__":
    main()
