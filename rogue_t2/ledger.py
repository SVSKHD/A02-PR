"""Rogue T2 — trade ledger (trades.csv).

One row per booked exit. Schema (documented in README.md):

  ts, side, tag, lot, intended_price, actual_price, slippage,
  pnl, commission, swap, day_pnl, config_hash, git_commit

Append-only, header written once. Slippage = actual_price - intended_price for the
entry (sign in the trade's favor is negative cost). Every row carries the config hash
and git commit so a CSV can be traced back to the exact code + parameters that made it.
"""
from __future__ import annotations

import csv
import os
from typing import Optional

FIELDS = [
    "ts", "side", "tag", "lot", "intended_price", "actual_price", "slippage",
    "pnl", "commission", "swap", "day_pnl", "config_hash", "git_commit",
]


class TradeLedger:
    def __init__(self, path: str, config_hash: str, git_commit: str):
        self.path = path
        self.config_hash = config_hash
        self.git_commit = git_commit
        self._ensure_header()

    def _ensure_header(self) -> None:
        exists = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        if not exists:
            d = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(d, exist_ok=True)
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow(FIELDS)

    def record(self, *, ts, side, tag, lot, intended_price, actual_price,
               pnl, commission=0.0, swap=0.0, day_pnl=0.0,
               slippage: Optional[float] = None) -> None:
        if slippage is None:
            slippage = round(float(actual_price) - float(intended_price), 5)
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow([
                ts, side, tag, lot, intended_price, actual_price, slippage,
                round(float(pnl), 2), round(float(commission), 2), round(float(swap), 2),
                round(float(day_pnl), 2), self.config_hash, self.git_commit,
            ])
