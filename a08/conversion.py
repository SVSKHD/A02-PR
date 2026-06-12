"""
AUREON A08 — price-distance conversion (XAUUSD $ -> MCX rupees).

Methodology, not hardcoded numbers. Every $ distance scales by the live ratio

    R = MCX_quote_price (Rs per quote_grams) / XAUUSD_price ($ per oz)

so a proportional (same-%) move maps as:  Rs_distance = $_distance x R.

R drifts daily with USDINR + import duty, so it is RECOMPUTED at the first
anchor each session (see recompute_R) and frozen for the rest of the day. All
of: +-$5 trigger, $18 SL, $30 TP, the $2.5/$6/$10 ladder tiers, $2 gap, $1
TSTOP, and the $6 boost SL pass through this same R, then round to the MCX tick.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .config import Config, Instrument


def compute_R(mcx_quote_price: float, xau_usd_price: float) -> float:
    """R = MCX(Rs/quote_grams) / XAUUSD($/oz). Recompute daily at first anchor."""
    if xau_usd_price <= 0:
        raise ValueError("xau_usd_price must be positive to compute R")
    return mcx_quote_price / xau_usd_price


def to_inr(dollar_dist: float, R: float, inst: Instrument) -> float:
    """Convert a $ distance to a rupee distance, rounded to the instrument tick."""
    raw = dollar_dist * R
    return round_to_tick(raw, inst.tick_inr)


def round_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    return round(round(value / tick) * tick, 6)


@dataclass
class ConvertedDistances:
    """All strategy distances in MCX rupees, plus the R they were built from.

    Held per-session: rebuilt once R is recomputed at the first anchor.
    """
    R: float
    trigger: float
    sl: float
    tp: float
    be: float
    lock4: float          # the +$6 trigger, in rupees
    lock4_lock: float     # the +$4 the +$6 tier locks at, in rupees
    tier10: float
    tier10_floor: float
    trail_gap: float
    trail_arm: float
    tstop_fav: float
    boost_sl: float
    boost_tp: float
    sibling_close: float
    value_per_point_inr: float   # P&L per 1-tick move per 1 lot

    def pnl_inr(self, point_dist: float, lots: int) -> float:
        """Rupee P&L for a rupee price distance, given a lot count.

        point_dist is in rupees of quote; value_per_point_inr already encodes
        the contract size, so this is distance/tick * value * lots.
        """
        return point_dist * (self.value_per_point_inr) * lots


def convert_all(cfg: Config, R: float) -> ConvertedDistances:
    """Build the full rupee distance set for a session from the live R."""
    inst = cfg.inst()
    f = lambda d: to_inr(d, R, inst)  # noqa: E731
    return ConvertedDistances(
        R=R,
        trigger=f(cfg.trigger_dist),
        sl=f(cfg.sl_dist),
        tp=f(cfg.tp_dist),
        be=f(cfg.be_trigger),
        lock4=f(cfg.lock4_trigger),
        lock4_lock=f(cfg.lock4_amount),
        tier10=f(cfg.tier10_trigger),
        tier10_floor=f(cfg.tier10_floor),
        trail_gap=f(cfg.trail_gap),
        trail_arm=f(cfg.trail_arm),
        tstop_fav=f(cfg.tstop_fav),
        boost_sl=f(cfg.rescue_boost_sl),
        boost_tp=f(cfg.rescue_boost_tp),
        sibling_close=f(cfg.sibling_close_loss),
        value_per_point_inr=inst.value_per_point_inr,
    )


def recompute_R(adapter, cfg: Config) -> float:
    """Pull live MCX + XAU prices at the first anchor and compute the day's R.

    adapter must expose mcx_last_price(cfg.instrument) and xau_usd_price().
    Kept here (not in the adapter) so the methodology is testable in isolation.
    """
    mcx = adapter.mcx_last_price(cfg.instrument)
    xau = adapter.xau_usd_price()
    R = compute_R(mcx, xau)
    return R
