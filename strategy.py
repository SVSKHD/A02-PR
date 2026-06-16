"""AUREON — pure strategy core (split from bot.py, v3.0.0).

Position + update_position_on_bar + realize_pnl_usd. NO I/O, byte-identical;
the most precious code in the repo.
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config import Config


@dataclass
class Position:
    """A single open position (one leg from one anchor)."""
    anchor_label: str
    side: str  # 'BUY' or 'SELL'
    entry_price: float
    entry_time: pd.Timestamp
    current_sl: float
    tp_level: float
    max_fav: float
    lot: float
    role: str = 'normal'  # v2.9: 'normal' (1st leg) | 'rescue' (No-OCO 2nd leg)
    closed: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    outcome: Optional[str] = None  # 'SL', 'TP', 'Trail', 'EOD', 'KillSwitch'

    @property
    def pnl_dist(self) -> float:
        """Current/realized price distance favorable to us."""
        ref = self.exit_price if self.closed else self.max_fav
        if self.side == 'BUY':
            return (ref - self.entry_price)
        return (self.entry_price - ref)


def update_position_on_bar(pos: Position, bar: pd.Series, ts: pd.Timestamp,
                           cfg: Config) -> Optional[str]:
    """
    Apply one M1 bar to an open position. Returns the outcome string if closed,
    else None. Mutates pos.
    """
    if pos.closed:
        return pos.outcome

    # 1. PRE-BAR SL CHECK
    if pos.side == 'BUY':
        if bar.low <= pos.current_sl:
            pos.exit_price = pos.current_sl
            pos.exit_time = ts
            pos.outcome = 'SL' if pos.current_sl <= pos.entry_price - cfg.sl_dist + 0.01 else 'Trail'
            pos.closed = True
            return pos.outcome
    else:
        if bar.high >= pos.current_sl:
            pos.exit_price = pos.current_sl
            pos.exit_time = ts
            pos.outcome = 'SL' if pos.current_sl >= pos.entry_price + cfg.sl_dist - 0.01 else 'Trail'
            pos.closed = True
            return pos.outcome

    # 2. UPDATE PEAK FAVORABLE (always, even during freeze — used for reporting & post-freeze trail snap)
    if pos.side == 'BUY':
        if bar.high > pos.max_fav: pos.max_fav = bar.high
        fav = pos.max_fav - pos.entry_price
    else:
        if bar.low < pos.max_fav: pos.max_fav = bar.low
        fav = pos.entry_price - pos.max_fav
    fav = max(fav, 0.0)

    # 3-5. TRAIL UPDATE — gated by freeze window
    # v2.3 FREEZE: for cfg.freeze_minutes after fill, do NOT engage BE-arm/trail.
    # Initial $18 SL stays as the broker-side stop. When freeze expires, normal
    # trail logic engages and will snap to (peak − trail_gap) automatically.
    in_freeze = False
    if cfg.freeze_minutes > 0 and pos.entry_time is not None:
        try:
            elapsed = (ts - pos.entry_time).total_seconds() / 60.0
            in_freeze = elapsed < cfg.freeze_minutes
        except Exception:
            in_freeze = False  # bad timestamp → fall through to normal logic

    # v2.9 ROLE-AWARE PROFIT LADDER -- fires EVEN during the hold. The hold
    # blocks the noise-chasing trail, NOT profit protection. One-way ratchet:
    # locks can only raise the floor, never loosen a stop.
    #
    # NORMAL leg (1st fill -- job: catch the breakout, bank profits):
    #   fav >= $10  -> SL locked at peak - $2 (floor +$8)
    #   fav >= $6   -> SL locked at entry +/- $4  (fires during the hold)
    #   fav >= $5.0 -> SL locked at breakeven, ONLY AFTER the 45m hold
    #                  (v3.0.7: arm was $2.5; raised to $5 AND hold-gated -- the
    #                  BE-to-entry move inside the hold scratched trends to $0)
    # RESCUE leg (No-OCO 2nd fill -- by construction it only fills after price
    # traveled $10 against its twin; its job is to COVER the twin's loss, so it
    # must stay free to run. Early BE-locks scratch it at $0 exactly when the
    # crash it exists for is happening -- the Jun-10 A3 lesson):
    #   fav >= $10 -> SL locked at entry +/- $8   (loss covered; start protecting)
    #   no smaller tiers.
    def _ratchet(level):
        if pos.side == 'BUY':
            if level > pos.current_sl:
                pos.current_sl = level
        else:
            if level < pos.current_sl:
                pos.current_sl = level
    _sgn = 1.0 if pos.side == 'BUY' else -1.0
    if fav >= 10.00:
        # v2.9.1: above +$10 the lock FOLLOWS the peak at $2 distance (ratchet),
        # floor +$8. Captures most of a hold-period spike (peak +$12.8 -> lock
        # +$10.8 = +$540 @0.5) instead of a flat +$8, while $2 of room keeps
        # ordinary noise from tagging it. fav here is peak favorable (max_fav).
        _ratchet(pos.entry_price + _sgn * max(8.00, fav - 2.00))
    elif pos.role != 'rescue':
        if fav >= 6.00:
            _ratchet(pos.entry_price + _sgn * 4.00)
        elif fav >= 5.00 and not in_freeze:
            # v3.0.7 HOLD-GATE: the breakeven-to-entry stop move must NOT engage
            # inside the 45m hold. Live 2026-06-16: A2/A4 hit +$5 fav early, then
            # pulled back and BE-scratched to $0 at 6.2m/2.8m held. Raising the arm
            # to +$5 did not fix this -- the disease is the TIMING. The higher
            # protective locks (+$6->+$4, +$10->peak-2 above) stay active inside
            # the hold; ONLY this entry move waits for hold expiry.
            _ratchet(pos.entry_price)

    if not in_freeze and fav >= cfg.be_trigger:
        if pos.side == 'BUY':
            candidate_sl = max(pos.entry_price, pos.max_fav - cfg.trail_gap)
            if candidate_sl > pos.current_sl + cfg.min_step:
                pos.current_sl = candidate_sl
        else:
            candidate_sl = min(pos.entry_price, pos.max_fav + cfg.trail_gap)
            if candidate_sl < pos.current_sl - cfg.min_step:
                pos.current_sl = candidate_sl

    # v2.6: $5 SECONDARY LOCK REMOVED. It pinned SL to entry+$4 above $5 fav, which is
    # TIGHTER than the peak-1.50 trail and capped runners exactly where you want them to
    # ride. The trail above already ratchets the SL up continuously and never down, so it
    # serves as the moving profit floor. Design: arm at +$2.5, BE lock at +$3, then pure
    # peak-1.50 trail all the way up.

    # 6. TP CHECK
    if pos.side == 'BUY':
        if bar.high >= pos.tp_level:
            pos.exit_price = pos.tp_level
            pos.exit_time = ts
            pos.outcome = 'TP'
            pos.closed = True
            return 'TP'
    else:
        if bar.low <= pos.tp_level:
            pos.exit_price = pos.tp_level
            pos.exit_time = ts
            pos.outcome = 'TP'
            pos.closed = True
            return 'TP'

    return None


def realize_pnl_usd(pos: Position, cfg: Config) -> float:
    """Convert closed position to USD P&L. Returns 0 if not closed."""
    if not pos.closed: return 0.0
    return pos.pnl_dist * cfg.contract_size * pos.lot
