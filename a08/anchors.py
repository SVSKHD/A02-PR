"""
AUREON A08 — anchor scheduling (IST).

A1 (05:00 IST) is DROPPED -- MCX is closed, the trade is not placeable. A2/A3/A4
are live. The first anchor of the session is where R is recomputed (it drifts
daily with USDINR + duty); the rest of the day reuses that frozen R.

Anchor placement = the netting-adapted straddle: two pending SL-M orders,
buy stop +(trigger) and sell stop -(trigger) from the anchor price. First fill
becomes the position; the sibling stop stays working (see strategy.py for what
happens when the sibling triggers).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pandas as pd

from .config import Config
from .conversion import ConvertedDistances
from .dhan_adapter import DhanAdapter, SIDE_BUY, SIDE_SELL

log = logging.getLogger("A08.anchors")


@dataclass
class AnchorPlan:
    label: str
    fire_at: pd.Timestamp        # tz-aware IST
    anchor_price: Optional[float] = None
    buy_trigger: Optional[float] = None
    sell_trigger: Optional[float] = None
    fired: bool = False


def anchor_datetime(session_date, hour: int, minute: int, tz: str) -> pd.Timestamp:
    """A session-local anchor time as a tz-aware timestamp."""
    naive = pd.Timestamp(session_date) + pd.Timedelta(hours=hour, minutes=minute)
    return naive.tz_localize(tz)


def build_anchor_plans(cfg: Config, session_date) -> List[AnchorPlan]:
    plans = []
    for label, h, m in cfg.anchors:
        plans.append(AnchorPlan(label=label,
                                fire_at=anchor_datetime(session_date, h, m, cfg.tz)))
    return sorted(plans, key=lambda p: p.fire_at)


def is_first_anchor(plan: AnchorPlan, plans: List[AnchorPlan]) -> bool:
    return plan.label == plans[0].label


def place_straddle(adapter: DhanAdapter, cfg: Config, plan: AnchorPlan,
                   dist: ConvertedDistances) -> Tuple[object, object]:
    """Place the two pending SL-M legs around the anchor price.

    Returns (buy_order, sell_order). The anchor_price must already be set on the
    plan (snapped at fire time from the live feed).
    """
    ap = plan.anchor_price
    if ap is None:
        raise ValueError(f"{plan.label}: anchor_price not snapped before placement")
    plan.buy_trigger = ap + dist.trigger
    plan.sell_trigger = ap - dist.trigger
    buy = adapter.place_slm(SIDE_BUY, cfg.lots, plan.buy_trigger,
                            tag=f"{plan.label}:BUY")
    sell = adapter.place_slm(SIDE_SELL, cfg.lots, plan.sell_trigger,
                             tag=f"{plan.label}:SELL")
    plan.fired = True
    log.info(f"{plan.label} straddle: anchor {ap} buy@{plan.buy_trigger} "
             f"sell@{plan.sell_trigger} (trigger Rs{dist.trigger}, R={dist.R:.3f})")
    return buy, sell
