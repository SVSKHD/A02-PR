"""
AUREON A08 — risk: kill switch, margin sizing, EOD flatten.

Two independent constraints, both must hold (handoff: "kill-switch sizing !=
margin sizing; both constraints apply"):
  1. Kill switch: cumulative daily P&L <= -daily_loss_pct x capital  -> halt + flatten.
     And nothing may let ONE anchor's worst case breach the switch.
  2. Margin: futures are SPAN+exposure leveraged -- per anchor, the order's
     required margin must fit within margin_buffer x available margin.
EOD: flatten everything before the MCX close buffer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .config import Config
from .conversion import ConvertedDistances

log = logging.getLogger("A08.risk")


@dataclass
class RiskState:
    daily_pnl_inr: float = 0.0
    killed: bool = False

    def kill_threshold(self, cfg: Config) -> float:
        return -cfg.daily_loss_pct * cfg.starting_capital_inr

    def record(self, pnl_inr: float, cfg: Config) -> bool:
        """Add a realized P&L; return True if the kill switch just tripped."""
        self.daily_pnl_inr += pnl_inr
        if not self.killed and self.daily_pnl_inr <= self.kill_threshold(cfg):
            self.killed = True
            log.warning(f"KILL SWITCH: daily {self.daily_pnl_inr:.0f} <= "
                        f"{self.kill_threshold(cfg):.0f}")
            return True
        return False


def anchor_worst_case_inr(cfg: Config, dist: ConvertedDistances) -> float:
    """Worst realistic loss a single anchor can take, for the kill-switch guard.

    Netting whipsaw branch: trapped leg realizes -sibling_close, then the rescue
    leg + boosts all stop at the boost SL. That bounded shape is the design point
    of the boost SL (vs full -$18 SLs which would breach the switch in one anchor).
    """
    trapped = dist.pnl_inr(dist.sibling_close, cfg.lots)
    fleet_legs = 1 + (cfg.rescue_boost_count if cfg.rescue_boost_enabled else 0)
    fleet = dist.pnl_inr(dist.boost_sl, cfg.lots) * fleet_legs
    return trapped + fleet


def anchor_fits_kill_switch(cfg: Config, dist: ConvertedDistances,
                            rstate: RiskState) -> bool:
    """No anchor may, by itself, push the day past the kill threshold."""
    projected = rstate.daily_pnl_inr - anchor_worst_case_inr(cfg, dist)
    return projected > rstate.kill_threshold(cfg)


def margin_ok(adapter, cfg: Config) -> bool:
    """Per-anchor margin gate: required <= buffer x available."""
    if not cfg.margin_check_enabled:
        return True
    needed = adapter.required_margin(cfg.instrument, cfg.lots)
    avail = adapter.available_margin()
    ok = needed <= cfg.margin_buffer * avail
    if not ok:
        log.warning(f"margin gate: need {needed:.0f} > {cfg.margin_buffer:.0%} "
                    f"of {avail:.0f}")
    return ok


def is_eod(now: pd.Timestamp, cfg: Config) -> bool:
    return (now.hour, now.minute) >= (cfg.eod_flatten_hour, cfg.eod_flatten_minute)
