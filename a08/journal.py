"""
AUREON A08 — journaling.

Two sinks, carrying every v2.9.8 lesson from day one:
  1. Per-trade CSV, the SAME 19-column schema as the MT5 build (one rich row per
     fill), so cross-build analysis tooling reads both. P&L is in rupees here.
  2. Firebase daily doc in the SEPARATE collection `aureon_mcx`, schema_version 2,
     same contract as the forex `aureon_forex` doc. EOD-only write + weekly
     reconcile (the Firebase write itself is wired in the runner; this builds
     the doc payload).

Exit classifier labels (BE/LOCK4/TIER/Trail/SL/TP/TSTOP/SIBLING + slip), held-time
stamps, structural rescue detection, stop-through handling and the no-hold shadow
counterfactual all flow through these rows.
"""
from __future__ import annotations

import csv
import logging
import os
from typing import Dict, List, Optional

import pandas as pd

from .config import Config
from .conversion import ConvertedDistances
from .strategy import Position
from . import version

log = logging.getLogger("A08.journal")

# 19-column schema -- mirrors the MT5 build (USD->INR is the only field rename).
JOURNAL_COLUMNS = [
    "date_ist", "anchor", "anchor_price", "side", "entry_time_ist",
    "entry_price", "lots", "initial_sl", "initial_tp", "max_favorable",
    "exit_time_ist", "actual_exit_price", "modeled_trail_exit", "trail_slip",
    "exit_reason", "realized_pnl_inr", "order_id", "nohold_trail_exit", "role",
]


def journal_path(cfg: Config, now_ist: pd.Timestamp) -> str:
    os.makedirs(cfg.journal_dir, exist_ok=True)
    return os.path.join(cfg.journal_dir, f"trades_{now_ist.strftime('%Y-%m')}.csv")


def write_trade(cfg: Config, pos: Position, dist: ConvertedDistances,
                realized_pnl_inr: float, now_ist: pd.Timestamp,
                nohold_exit: Optional[float] = None) -> None:
    """Append one rich row for a closed leg."""
    sgn = pos.sign
    entry = pos.entry_price
    peak = pos.max_fav
    fav_dist = sgn * (peak - entry)
    modeled_trail = peak - sgn * dist.trail_gap   # peak - gap
    exit_price = pos.exit_price if pos.exit_price is not None else entry

    trail_slip = ""
    if pos.outcome in ("Trail", "BE", "LOCK4", "TIER"):
        trail_slip = round(exit_price - modeled_trail, 3)

    entry_time_ist = (pd.Timestamp(pos.entry_time).tz_convert(cfg.tz).strftime("%H:%M:%S")
                      if pos.entry_time is not None else "")

    row = [
        now_ist.strftime("%Y-%m-%d"),
        pos.anchor_label,
        pos.anchor_price if pos.anchor_price is not None else "",
        pos.side,
        entry_time_ist,
        round(entry, 2),
        pos.lots,
        round(entry - sgn * dist.sl, 2),     # initial_sl
        round(entry + sgn * dist.tp, 2),     # initial_tp
        round(fav_dist, 2),
        now_ist.strftime("%H:%M:%S"),
        round(exit_price, 2),
        round(modeled_trail, 2),
        trail_slip,
        pos.outcome or "",
        round(realized_pnl_inr, 2),
        pos.order_id or "",
        nohold_exit if nohold_exit is not None else "",
        pos.role,
    ]
    path = journal_path(cfg, now_ist)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(JOURNAL_COLUMNS)
        w.writerow(row)
    log.info(f"journal: {pos.anchor_label} {pos.side} {pos.outcome} "
             f"pnl=Rs{realized_pnl_inr:+.0f} slip={trail_slip} role={pos.role}")


def build_firebase_doc(cfg: Config, session_date, R: float,
                       trades: List[Dict], daily_pnl_inr: float,
                       killed: bool) -> Dict:
    """EOD daily doc for the `aureon_mcx` collection (schema_version 2).

    Same contract shape as the forex doc; written once at EOD, reconciled weekly.
    """
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    losses = sum(1 for t in trades if t.get("pnl", 0) < 0)
    return {
        "schema_version": version.SCHEMA_VERSION,
        "collection": cfg.firebase_collection,
        "app_version": version.__version__,
        "source_frozen": version.SOURCE_FROZEN,
        "date_ist": pd.Timestamp(session_date).strftime("%Y-%m-%d"),
        "instrument": cfg.instrument,
        "lots": cfg.lots,
        "R": round(R, 4),
        "anchors": [a[0] for a in cfg.anchors],
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "realized_pnl_inr": round(daily_pnl_inr, 2),
        "kill_switch_tripped": killed,
        "paper": cfg.paper,
    }
