"""
AUREON A08 — configuration (single source of strategy truth for the MCX port).

Design rule that makes this port correct: NOTHING price-distance is hardcoded
in rupees. Every distance is held in the FROZEN source units ($ on XAUUSD) and
converted to rupees at runtime through the live ratio R (see conversion.py),
which is recomputed daily at the first anchor. Change a tier here in dollars
and the whole ladder follows in rupees automatically.

The $ numbers below are the v2.9.8 reference, frozen. Do not "tune" them for
MCX -- the adaptation is the netting fleet (strategy.py) and R (conversion.py),
not the strategy constants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Instrument table -- MCX gold contracts.
#   quote_grams : the metal mass the quote is "per" (GOLDM/GOLD = 10g, PETAL = 1g)
#   lot_grams   : contract size (the metal you actually hold per 1 lot)
#   tick_inr    : minimum price increment, in rupees of the quote
#   value_per_point_inr = tick_inr * (lot_grams / quote_grams)  -> P&L per tick per lot
# These let conversion.py turn every $ distance into rupees AND into P&L without
# a single magic number.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Instrument:
    name: str
    quote_grams: float      # quote is rupees per this many grams
    lot_grams: float        # contract size in grams
    tick_inr: float = 1.0   # MCX gold tick = Re 1

    @property
    def value_per_point_inr(self) -> float:
        """Rupees of P&L for a 1-tick move, per 1 lot."""
        return self.tick_inr * (self.lot_grams / self.quote_grams)


INSTRUMENTS: Dict[str, Instrument] = {
    # GOLDM (100g mini) -- recommended for sizing granularity. Re1/10g tick,
    # value Rs.10 per point per lot.
    "GOLDM":      Instrument("GOLDM", quote_grams=10.0, lot_grams=100.0, tick_inr=1.0),
    # GOLDPETAL (1g) -- micro-validation. Quoted Rs/1g, Re1 tick, value Re1/point/lot.
    "GOLDPETAL":  Instrument("GOLDPETAL", quote_grams=1.0, lot_grams=1.0, tick_inr=1.0),
    # GOLD (1kg) -- later, when the demo record justifies the margin.
    "GOLD":       Instrument("GOLD", quote_grams=10.0, lot_grams=1000.0, tick_inr=1.0),
}


@dataclass
class Config:
    # ---- Instrument selection -------------------------------------------
    # GOLDM recommended for validation (granular sizing); GOLDPETAL for the
    # micro dry-run. Switchable here -- the rest of the app is instrument-blind.
    instrument: str = "GOLDM"
    lots: int = 1                       # number of contracts per leg (PAPER: 1)

    # ---- FROZEN source distances (DOLLARS on XAUUSD; never rupees) -------
    # Converted to MCX rupees at runtime by conversion.convert_all(R).
    trigger_dist: float = 5.00         # buy stop +$5 / sell stop -$5 from anchor
    sl_dist: float = 18.00             # initial stop
    tp_dist: float = 30.00             # take profit
    be_trigger: float = 2.50           # ladder: +$2.5 -> breakeven (NORMAL)
    lock4_trigger: float = 6.00        # ladder: +$6 -> lock +$4 (NORMAL)
    lock4_amount: float = 4.00         # the +$4 the +$6 tier locks the SL at
    tier10_trigger: float = 10.00      # ladder: +$10 -> trail peak-$2 floor +$8
    tier10_floor: float = 8.00         # floor for the +$10 tier
    trail_gap: float = 2.00            # post-hold trail: never > $2 behind peak
    trail_arm: float = 2.50            # post-hold trail arms only above +$2.5 peak
    min_step: float = 0.10             # min SL improvement to bother modifying
    tstop_fav: float = 1.00            # TSTOP at hold expiry if peak fav < $1

    # ---- Netting-adapted fleet (STRUCTURAL DIFFERENCE #1) ----------------
    # Indian futures NET per contract: long+short in one contract square off, so
    # the MT5 coexisting fleet is impossible. Instead: the sibling SL-M stays
    # working; if price travels the full spread it closes the trapped leg at
    # ~-($10xR) (better than riding to the $18 SL), and the rescue + boosts fire
    # as NEW net positions in the rescue direction.
    rescue_boost_enabled: bool = True
    rescue_boost_count: int = 2        # +2 market boosts on rescue
    rescue_boost_sl: float = 6.00      # tight $6 SL on each boost
    rescue_boost_tp: float = 30.00     # $30 TP on each boost
    sibling_close_loss: float = 10.00  # trapped leg realizes ~ -$10 (not -$18)

    # ---- Hold / timing ---------------------------------------------------
    hold_minutes: int = 45             # first fill starts a 45-min hold
    # Assert ladder stops on bar close (mirrors source) -- and respects Dhan
    # rate limits: at most 1 modify per leg per minute.
    modify_min_interval_sec: int = 60

    # ---- Anchors (IST). A1 05:00 DROPPED -- MCX closed. --------------------
    # (label, hour, minute) in Asia/Kolkata. A4 sits in MCX's most liquid
    # evening (COMEX overlap) -- the ~2x-range session in the source data.
    anchors: List[Tuple[str, int, int]] = field(default_factory=lambda: [
        ("A2_1230_India", 12, 30),
        ("A3_1620_Overlap", 16, 20),
        ("A4_1910_COMEX", 19, 10),
        # Optional NEW late-evening anchor: decided after live data, not at launch.
    ])
    tz: str = "Asia/Kolkata"

    # ---- EOD / kill switch ----------------------------------------------
    # MCX closes ~23:30/23:55 IST; flatten before close with a buffer.
    eod_flatten_hour: int = 23
    eod_flatten_minute: int = 15       # ~23:15 IST buffer before MCX close
    daily_loss_pct: float = 0.03       # -3% daily kill switch (same rule)
    starting_capital_inr: float = 200000.0  # A08 capital; set to real demo value

    # ---- Margin (futures are leveraged; both constraints apply) ----------
    # kill-switch sizing != margin sizing. Per anchor, check available SPAN+
    # exposure margin AND that no single anchor can breach the kill switch.
    margin_check_enabled: bool = True
    margin_buffer: float = 0.80        # use at most 80% of available margin

    # ---- Expiry roll (source system never needed this) -------------------
    roll_days_before_expiry: int = 3   # roll to next-month before this many days

    # ---- Operational -----------------------------------------------------
    paper: bool = True                 # PAPER/SIM ONLY until a green demo record
    log_level: str = "INFO"
    state_file: str = "a08_mcx_state.json"
    journal_dir: str = "run/journal"
    run_dir: str = "run"

    # Firebase: separate collection, schema_version 2, same contract as forex.
    firebase_collection: str = "aureon_mcx"

    def inst(self) -> Instrument:
        return INSTRUMENTS[self.instrument]


def load_config(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    if cfg.instrument not in INSTRUMENTS:
        raise ValueError(f"unknown instrument {cfg.instrument!r}; "
                         f"choose from {list(INSTRUMENTS)}")
    return cfg
