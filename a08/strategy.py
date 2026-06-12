"""
AUREON A08 — core strategy engine (ported from MT5 v2.9.8, adapted for netting).

This is the frozen v3 behavior expressed in MCX rupees:
  - first fill starts a 45-min HOLD (no trail exits inside; SL/TP/ladder live)
  - one-way ratchet ladder:
        NORMAL leg: +be -> BE | +lock4 -> lock +Rs4($) | +tier10 -> trail peak-$2 (floor +$8)
        RESCUE leg: only the +tier10 tier (stay free to cover the twin)
  - TSTOP at hold expiry if peak favorable < tstop_fav
  - post-hold trail: arm at trail_arm, gap trail_gap (never > gap behind peak)

STRUCTURAL DIFFERENCE #1 -- netting. Indian futures NET per contract, so the
MT5 coexisting fleet (trapped leg + live rescue + boosts) is impossible. Adapted:
  - straddle = two pending SL-M; first fill = position; sibling stays working.
  - if the sibling triggers (price traveled the full spread), it CLOSES the
    trapped leg at ~ -(sibling_close x R)  [better than riding to the $18 SL],
    and on that event the RESCUE leg + boost_count market BOOSTS fire as NEW net
    positions in the rescue direction (rescue-class exits, tight boost SL).

All distances arrive pre-converted to rupees in a ConvertedDistances (dist).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from .conversion import ConvertedDistances

log = logging.getLogger("A08.strategy")

ROLE_NORMAL = "normal"
ROLE_RESCUE = "rescue"
ROLE_BOOST = "boost"


@dataclass
class Position:
    """One open net position (one leg)."""
    anchor_label: str
    side: str                 # BUY / SELL
    entry_price: float
    entry_time: pd.Timestamp
    current_sl: float
    tp_level: float
    max_fav: float            # peak favorable PRICE reached
    lots: int
    role: str = ROLE_NORMAL
    order_id: Optional[str] = None
    closed: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    outcome: Optional[str] = None   # SL/TP/BE/LOCK4/TIER/Trail/TSTOP/SIBLING/EOD/Kill
    anchor_price: Optional[float] = None

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "BUY" else -1.0

    def fav_dist(self, price: float) -> float:
        """Favorable rupee distance of `price` from entry."""
        return self.sign * (price - self.entry_price)

    @property
    def peak_fav_dist(self) -> float:
        return self.sign * (self.max_fav - self.entry_price)


def open_position(anchor_label, side, entry_price, entry_time, lots,
                  dist: ConvertedDistances, role=ROLE_NORMAL,
                  order_id=None, anchor_price=None) -> Position:
    sgn = 1.0 if side == "BUY" else -1.0
    return Position(
        anchor_label=anchor_label, side=side, entry_price=entry_price,
        entry_time=entry_time, lots=lots, role=role, order_id=order_id,
        anchor_price=anchor_price, max_fav=entry_price,
        current_sl=entry_price - sgn * dist.sl,
        tp_level=entry_price + sgn * dist.tp,
    )


def in_hold(pos: Position, now: pd.Timestamp, hold_minutes: int) -> bool:
    if pos.entry_time is None:
        return False
    elapsed = (now - pos.entry_time).total_seconds() / 60.0
    return 0 <= elapsed < hold_minutes


def _ratchet(pos: Position, level: float):
    """One-way: a stop can only move in the favorable direction."""
    if pos.side == "BUY":
        if level > pos.current_sl:
            pos.current_sl = level
    else:
        if level < pos.current_sl:
            pos.current_sl = level


def update_on_bar(pos: Position, high: float, low: float, close: float,
                  now: pd.Timestamp, dist: ConvertedDistances,
                  hold_minutes: int, trail_arm: float, trail_gap: float,
                  min_step: float) -> Optional[str]:
    """Apply one bar to an open position. Mutates pos; returns outcome if closed.

    Mirrors bot.update_position_on_bar, in rupees, role-aware.
    """
    if pos.closed:
        return pos.outcome
    sgn = pos.sign

    # 1. SL check (stop-through -> classify by where the stop sat)
    sl_hit = (low <= pos.current_sl) if pos.side == "BUY" else (high >= pos.current_sl)
    if sl_hit:
        pos.exit_price = pos.current_sl
        pos.exit_time = now
        at_initial = abs(pos.current_sl - (pos.entry_price - sgn * dist.sl)) <= dist.R * 0.01 + 0.5
        pos.outcome = "SL" if at_initial else _classify_stop(pos, dist)
        pos.closed = True
        return pos.outcome

    # 2. peak favorable (always, even during hold)
    cand_peak = high if pos.side == "BUY" else low
    if pos.fav_dist(cand_peak) > pos.peak_fav_dist:
        pos.max_fav = cand_peak
    fav = pos.peak_fav_dist

    held = in_hold(pos, now, hold_minutes)

    # 3. role-aware ladder (one-way ratchet; fires EVEN during hold)
    if fav >= dist.tier10:
        # +$10 tier: trail peak-$2, floor +$8
        _ratchet(pos, pos.entry_price + sgn * max(dist.tier10_floor, fav - dist.trail_gap))
    elif pos.role != ROLE_RESCUE:
        if fav >= dist.lock4:
            _ratchet(pos, pos.entry_price + sgn * dist.lock4_lock)
        elif fav >= dist.be:
            _ratchet(pos, pos.entry_price)   # breakeven

    # 4. post-hold trail (arm above trail_arm; never > gap behind peak)
    if not held and fav >= trail_arm:
        candidate = pos.max_fav - sgn * trail_gap
        if pos.side == "BUY":
            candidate = max(pos.entry_price, candidate)
            if candidate > pos.current_sl + min_step:
                pos.current_sl = candidate
        else:
            candidate = min(pos.entry_price, candidate)
            if candidate < pos.current_sl - min_step:
                pos.current_sl = candidate

    # 5. TP check
    tp_hit = (high >= pos.tp_level) if pos.side == "BUY" else (low <= pos.tp_level)
    if tp_hit:
        pos.exit_price = pos.tp_level
        pos.exit_time = now
        pos.outcome = "TP"
        pos.closed = True
        return "TP"

    return None


def _classify_stop(pos: Position, dist: ConvertedDistances) -> str:
    """Name the rule the stop represents (BE/LOCK4/TIER/Trail) from current_sl."""
    locked = pos.sign * (pos.current_sl - pos.entry_price)
    if abs(locked) <= dist.R * 0.10:
        return "BE"
    if abs(locked - dist.lock4_lock) <= dist.R * 0.10:
        return "LOCK4"
    if locked >= dist.tier10_floor - dist.R * 0.10:
        return "TIER"
    return "Trail"


def check_tstop(pos: Position, now: pd.Timestamp, hold_minutes: int,
                dist: ConvertedDistances) -> bool:
    """At hold expiry, True if this leg never reached tstop_fav (cut at market)."""
    if pos.closed or pos.entry_time is None:
        return False
    elapsed = (now - pos.entry_time).total_seconds() / 60.0
    if elapsed < hold_minutes:
        return False
    return pos.peak_fav_dist < dist.tstop_fav


# ---------------------------------------------------------------------------
# Netting-adapted fleet on a SIBLING trigger
# ---------------------------------------------------------------------------

@dataclass
class FleetEvent:
    """Result of a sibling-trigger event: what the engine wants the adapter to do."""
    trapped_closed_at: float          # price the trapped leg closed at (~ -sibling_close)
    trapped_pnl_inr: float
    rescue_side: str
    rescue_lots: int
    boost_count: int
    notes: str = ""


def on_sibling_trigger(trapped: Position, sibling_fill_price: float,
                       cfg, dist: ConvertedDistances) -> FleetEvent:
    """The full-spread travel triggered the sibling stop.

    Netting reality: the sibling order, filled opposite to the trapped leg in the
    SAME contract, squares the trapped leg off. It realizes ~ -(sibling_close)
    -- better than riding the trapped leg to its -$18 SL. We then fire the rescue
    leg + boosts as NEW net positions in the rescue (sibling) direction.
    """
    trapped.exit_price = sibling_fill_price
    trapped.outcome = "SIBLING"
    trapped.closed = True
    loss_dist = -dist.sibling_close
    trapped.exit_time = None
    trapped_pnl = dist.pnl_inr(loss_dist, trapped.lots)

    rescue_side = "SELL" if trapped.side == "BUY" else "BUY"
    boost_n = cfg.rescue_boost_count if cfg.rescue_boost_enabled else 0
    return FleetEvent(
        trapped_closed_at=sibling_fill_price,
        trapped_pnl_inr=trapped_pnl,
        rescue_side=rescue_side,
        rescue_lots=cfg.lots,
        boost_count=boost_n,
        notes=f"trapped {trapped.side} closed ~ -Rs{dist.sibling_close} "
              f"(={trapped_pnl:.0f}); rescue {rescue_side} +{boost_n} boosts",
    )
