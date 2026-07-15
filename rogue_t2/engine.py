"""Rogue T2 Continuation V1 — PURE decision core.

No IO, no clock, no MT5. Every function here is a deterministic transform used by
bot.py (the driver) and exercised directly by the unit tests. The frozen spec
numbers live in config; this module only applies them. Mirror-symmetric for BUY
and SELL by construction (a single `sign` term).

Vocabulary
----------
- "point" == 1.00 in price (XAUUSD), matching the rest of the book.
- A1     : the phase/cycle reference mid, captured at arm time.
- T1     : the first fill (either OCO leg); carries a broker SL at entry ∓ 2.60.
- T2     : the continuation stop, T1 fill ± 12.00 in the fill direction; same SL
           rule; survives T1's exit; cancelled at phase end. No third position.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from .config import RogueT2Config, IST_OFFSET_MINUTES

_IST = timezone(timedelta(minutes=IST_OFFSET_MINUTES))


# --- sizing / pnl -----------------------------------------------------------------
def daily_cap_usd(cfg: RogueT2Config) -> float:
    """(-700/0.40)*LOT — the only place the cap number is formed."""
    return cfg.daily_cap_usd()


def pnl_usd(side: str, entry: float, exit_price: float, lot: float,
            contract_size: float) -> float:
    """Gross USD PnL of a closed leg (before commission/swap). Scales linearly with
    lot and contract_size, sign-correct for BUY/SELL."""
    sgn = 1.0 if side == "BUY" else -1.0
    return sgn * (exit_price - entry) * lot * contract_size


def unrealized_usd(side: str, entry: float, mark: float, lot: float,
                   contract_size: float) -> float:
    """Mark-to-market USD of an open leg."""
    return pnl_usd(side, entry, mark, lot, contract_size)


# --- time / phases ----------------------------------------------------------------
def to_ist(utc_dt: datetime) -> datetime:
    """UTC (aware) -> IST (aware, UTC+5:30)."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(_IST)


def _hm(t: Tuple[int, int]) -> int:
    return t[0] * 60 + t[1]


def phase_start_min(cfg: RogueT2Config, phase_idx: int, weekday: int) -> int:
    """Start-of-phase minutes-of-day in IST, applying the Monday-only 06:00 shift
    to phase 0 (weekday 0 == Monday)."""
    start, _ = cfg.phase_windows[phase_idx]
    if phase_idx == 0 and weekday == 0:
        start = cfg.monday_phase1_start
    return _hm(start)


def resolve_phase(cfg: RogueT2Config, ist_dt: datetime) -> Optional[int]:
    """The active IST phase index (0/1/2) for an IST datetime, or None when outside
    the session. Monday phase 0 does not open until 06:00. Half-open windows
    [start, end): 22:00 exactly is OUT of session (flatten boundary)."""
    minutes = ist_dt.hour * 60 + ist_dt.minute
    wd = ist_dt.weekday()
    for idx, (_start, end) in enumerate(cfg.phase_windows):
        s = phase_start_min(cfg, idx, wd)
        e = _hm(end)
        if s <= minutes < e:
            return idx
    return None


def phase_key(ist_dt: datetime, phase_idx: int) -> str:
    """Stable per-day/per-phase key (IST calendar date + phase) — the persisted
    idempotency namespace. Cycle number + tag complete an order's identity."""
    return f"{ist_dt.strftime('%Y-%m-%d')}#P{phase_idx}"


def idempotency_key(phase_key_str: str, cycle: int, tag: str) -> str:
    """phase + cycle number + tag — persisted so a restart never double-places a
    T1/T2 that already exists at the broker."""
    return f"{phase_key_str}#C{cycle}#{tag}"


# --- entry geometry ---------------------------------------------------------------
@dataclass(frozen=True)
class OcoPlan:
    a1: float
    buy_stop: float
    sell_stop: float
    buy_sl: float
    sell_sl: float


def oco_plan(cfg: RogueT2Config, a1: float) -> OcoPlan:
    """The armed OCO straddle at phase start / re-arm: buy-stop A1+17, sell-stop
    A1-17, each with its broker SL at entry ∓ 2.60."""
    buy = round(a1 + cfg.entry_offset, 2)
    sell = round(a1 - cfg.entry_offset, 2)
    return OcoPlan(
        a1=round(a1, 2),
        buy_stop=buy, sell_stop=sell,
        buy_sl=entry_sl("BUY", buy, cfg),
        sell_sl=entry_sl("SELL", sell, cfg),
    )


def entry_sl(side: str, entry: float, cfg: RogueT2Config) -> float:
    """Broker-side SL from the moment of entry: entry ∓ 2.60."""
    sgn = 1.0 if side == "BUY" else -1.0
    return round(entry - sgn * cfg.sl_offset, 2)


def t2_plan(cfg: RogueT2Config, t1_side: str, t1_fill: float) -> Tuple[str, float, float]:
    """The continuation stop after a T1 fill: SAME direction as T1, T1 fill ± 12.00
    (in the fill direction), same SL rule. Returns (side, trigger_price, sl)."""
    sgn = 1.0 if t1_side == "BUY" else -1.0
    trigger = round(t1_fill + sgn * cfg.t2_offset, 2)
    return t1_side, trigger, entry_sl(t1_side, trigger, cfg)


# --- trailing ---------------------------------------------------------------------
def update_trail(side: str, entry: float, peak: float, current_sl: float,
                 cfg: RogueT2Config) -> float:
    """One-way trailing stop, server-side. Inactive until peak favorable >= 1.50;
    then the stop follows peak by `trail_distance` (2.60), advancing ONLY in 0.50
    ratchet steps and never loosening. Returns the (possibly unchanged) SL.

    `peak` is the best price reached (high for BUY, low for SELL). Mirror-symmetric.
    """
    sgn = 1.0 if side == "BUY" else -1.0
    peak_fav = sgn * (peak - entry)
    if peak_fav < cfg.trail_activation:
        return current_sl                      # not armed yet -> initial SL stands
    candidate = peak - sgn * cfg.trail_distance
    # advance = how far candidate is beyond the current stop, in the favorable dir
    advance = sgn * (candidate - current_sl)
    if advance <= 0:
        return current_sl                      # never loosen
    steps = int(advance / cfg.trail_ratchet)   # whole 0.50 increments only
    if steps < 1:
        return current_sl
    return round(current_sl + sgn * steps * cfg.trail_ratchet, 2)


# --- daily cap --------------------------------------------------------------------
def cap_breached(cfg: RogueT2Config, realized_usd: float, unrealized_usd_: float) -> bool:
    """True when realized (deal history incl. commission+swap, own-magic) plus
    unrealized has reached the daily cap. Cap is negative; breach is <=."""
    return (realized_usd + unrealized_usd_) <= daily_cap_usd(cfg)
