"""AUREON — FP-rule exposure guard (v3.2.3 Feature E: lot config + FP guard).

WHY THIS EXISTS
---------------
A 5-long stack at 0.35 floats far more than the prop-firm daily limit. Before any
stack is added we must check the WORST-CASE floating loss of the open stack at the
chosen lot against the account's FP rule, and BLOCK or REDUCE if it would breach.

Profiles:
  STANDARD_5PCT  -> 5% daily  ($2,500 @ $50k)
  FPZERO_1PCT    -> 1% floating ($500 @ $50k)

Lot is operator-chosen via cfg.lot_size: 0.35 (demo) / 0.15 (FP-safe) / 0.27 (Zero).

PURE: no MT5. Shared by live (pre-trade) + selftest (identity).
"""
from __future__ import annotations

from typing import Tuple

PROFILES = {"STANDARD_5PCT": 0.05, "FPZERO_1PCT": 0.01}

OK = "OK"
REDUCE = "REDUCE"
BLOCK = "BLOCK"


def fp_pct(profile: str) -> float:
    """The fraction-of-balance limit for a profile (defaults to 5% if unknown)."""
    return PROFILES.get(str(profile), 0.05)


def fp_limit_usd(profile: str, balance: float) -> float:
    """The $ floating-loss limit for a profile at the given balance."""
    return fp_pct(profile) * float(balance)


def per_leg_loss_usd(lot: float, sl_dist: float, contract: float = 100.0) -> float:
    """One leg's worst-case loss = lot * sl_dist * contract ($630 @ 0.35/$18)."""
    return float(lot) * float(sl_dist) * float(contract)


def worst_case_stack_usd(n_positions: int, lot: float, sl_dist: float,
                         contract: float = 100.0) -> float:
    """Worst-case floating loss of an n-position stack all the way to SL."""
    return int(n_positions) * per_leg_loss_usd(lot, sl_dist, contract)


def fp_guard(n_positions: int, lot: float, sl_dist: float, profile: str,
             balance: float, contract: float = 100.0) -> Tuple[str, float, float, int]:
    """Pre-trade gate. Returns (action, worst_case_usd, limit_usd, allowed_n):
      OK     -> worst-case within the limit; allowed_n == n_positions
      REDUCE -> too big; allowed_n = the largest stack that fits (>=1)
      BLOCK  -> even ONE leg breaches; allowed_n == 0
    Never lets a stack breach the FP rule at the chosen lot."""
    wc = worst_case_stack_usd(n_positions, lot, sl_dist, contract)
    lim = fp_limit_usd(profile, balance)
    if wc <= lim:
        return OK, round(wc, 2), round(lim, 2), int(n_positions)
    per = per_leg_loss_usd(lot, sl_dist, contract)
    max_n = int(lim // per) if per > 0 else 0
    if max_n >= 1:
        return REDUCE, round(wc, 2), round(lim, 2), max_n
    return BLOCK, round(wc, 2), round(lim, 2), 0


def profile_stack_cap(profile: str, base_cap: int) -> int:
    """The 5-long is disallowed on a 1% floating rule -- FPZERO_1PCT caps the stack
    back to 3 regardless of the configured cap. STANDARD_5PCT keeps the base cap."""
    if str(profile) == "FPZERO_1PCT":
        return min(int(base_cap), 3)
    return int(base_cap)


def effective_sl_dist(cfg) -> float:
    """Adverse distance used for worst-case floating = SL + spread buffer (18 + 0.6
    = 18.6 effective), so the guard matches live floating, not the bare SL."""
    return float(getattr(cfg, "sl_dist", 18.0)) + float(getattr(cfg, "fp_spread_buffer", 0.60))


def guard_cfg(n_positions: int, cfg, balance: float) -> Tuple[str, float, float, int]:
    """fp_guard wired from cfg (lot_size, effective SL, account_profile, contract)
    so the SAME lot/profile drive every call -- 'lot config applies everywhere'.
    Worst-case uses SL + fp_spread_buffer: 5x0.35 -> -$3,255, 5x0.15 -> -$1,395."""
    return fp_guard(
        n_positions,
        float(getattr(cfg, "lot_size", 0.35)),
        effective_sl_dist(cfg),
        str(getattr(cfg, "account_profile", "STANDARD_5PCT")),
        float(balance),
        float(getattr(cfg, "contract_size", 100.0)),
    )
