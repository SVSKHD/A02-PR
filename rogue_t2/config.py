"""Rogue T2 Continuation V1 — configuration.

LOT is a config parameter, never hardcoded. Everything lot-dependent (daily cap,
PnL, margin) derives from LOT so the strategy scales without touching frozen spec
numbers. The frozen spec numbers below are backtest-validated and MUST NOT be
tuned in this integration (the work is execution hardening, not strategy).

SAFETY: trading_unlocked defaults False -> the broker's default order path is the
inert _simulated_send(); no real order leaves the process until an operator flips
this per UNLOCK.md.
"""
from dataclasses import dataclass, field
from typing import List, Tuple

# Dedicated magic, namespaced to THIS bot. Distinct from every other magic on the
# shared account: 20260522 (Aureon anchors), 20260626 (Aureon rogue rider),
# 20260707 (fetcher), 9999998 (warmup). ALL destructive ops filter on this.
ROGUE_T2_MAGIC = 20260815

# IST phase windows (hour, minute). Flatten own-magic at every boundary and 22:00.
PHASE_WINDOWS: List[Tuple[Tuple[int, int], Tuple[int, int]]] = [
    ((5, 0), (12, 30)),
    ((12, 30), (17, 0)),
    ((17, 0), (22, 0)),
]
# Monday cold-start: phase 1 begins 06:00 IST instead of 05:00.
MONDAY_PHASE1_START: Tuple[int, int] = (6, 0)
IST_OFFSET_MINUTES = 5 * 60 + 30  # IST = UTC+5:30


@dataclass
class RogueT2Config:
    # --- identity ---
    symbol: str = "XAUUSD"
    magic: int = ROGUE_T2_MAGIC
    contract_size: float = 100.0     # oz per 1.0 lot (XAUUSD)

    # --- LOT (the ONLY sizing knob; launch value 0.35) ---
    lot: float = 0.35

    # --- FROZEN SPEC (do not tune) ---
    entry_offset: float = 17.00      # A1 ± 17.00 -> OCO buy-stop / sell-stop
    sl_offset: float = 2.60          # broker-side SL at entry ∓ 2.60
    t2_offset: float = 12.00         # T2 pending at T1 fill ± 12.00 (continuation)
    trail_activation: float = 1.50   # trail arms after +1.50 favorable
    trail_distance: float = 2.60     # trailing stop distance once active
    trail_ratchet: float = 0.50      # SL ratchets only in 0.50 increments

    # --- daily cap: (-700 / 0.40) * LOT ---
    cap_base_loss: float = -700.0
    cap_base_lot: float = 0.40

    # --- guards ---
    max_spread: float = 0.60         # skip entry when spread > 0.60
    max_tick_age_s: float = 10.0     # skip entry when last tick older than 10s

    # --- phases (IST) ---
    phase_windows: List[Tuple[Tuple[int, int], Tuple[int, int]]] = field(
        default_factory=lambda: list(PHASE_WINDOWS))
    monday_phase1_start: Tuple[int, int] = MONDAY_PHASE1_START

    # --- SAFETY (AO8 defaults — never flip in code) ---
    trading_unlocked: bool = False   # False => _simulated_send is the order path

    # ------------------------------------------------------------------
    def daily_cap_usd(self) -> float:
        """Per-day loss cap in USD, scaled from the frozen base by LOT.
        (-700/0.40)*LOT — at 0.35 => -612.50, at 0.15 => -262.50, at 0.40 => -700."""
        return (self.cap_base_loss / self.cap_base_lot) * self.lot

    def validate(self) -> None:
        """Cheap invariants; raises ValueError on a nonsensical config."""
        if self.lot <= 0:
            raise ValueError(f"lot must be > 0, got {self.lot}")
        if self.cap_base_lot == 0:
            raise ValueError("cap_base_lot must be non-zero")
        if len(self.phase_windows) != 3:
            raise ValueError("Rogue T2 spec requires exactly 3 IST phases")
