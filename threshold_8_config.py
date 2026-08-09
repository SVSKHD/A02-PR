"""threshold_8 — configuration & named constants.

Every number the rescue / trail / basket / exit logic reads lives here as a named
constant. Logic modules import from this file; they must contain no magic numbers.

Scope note (honest, read this): the wider A02 architecture that this spec assumes
(``replay_engine.py``, ``mt5_client.py``, ``feature_builder`` / ``dataset_builder``)
does not exist in this repository at the time this module was written. threshold_8
is therefore delivered as a *self-contained* subsystem:

  * It never imports MetaTrader5 and never touches a live broker. In production the
    single MT5 boundary would remain ``mt5_client.py``; here the modules are driven
    only by the backtest replay loop.
  * The rescue / trail / basket logic modules CONSUME features (ATR, swing points)
    that are supplied to them as inputs. They never compute a feature themselves.
    The backtest's replay engine plays the ``feature_builder`` role and hands closed
    -M5 ATR / swing values to the detectors — exactly as the production feature
    builder would.
  * There is exactly ONE bar-iteration loop (``threshold_8_replay.ReplayEngine``);
    the backtest drives it and does not re-iterate bars.

INVARIANT 1 (asserted at import): TRAIL_LOCK < TRAIL_ACTIVATE. The inverted form of
this relation historically produced phantom fills and fake +$22k/+$38k/+$79k runs,
so it is enforced the moment this module is imported — an import that violates it
raises immediately and nothing downstream can run.
"""

THRESHOLD_8_VERSION            = "threshold_8"
THRESHOLD_8_SYMBOLS            = ["XAUUSD"]          # others later

# --- entry economics (fixed for this task; entry engine is read-only) -------------
THRESHOLD_8_TRIGGER_DIST       = 20.0                # $ from anchor, existing behaviour
THRESHOLD_8_SL                 = 18.0                # existing per-leg SL, in $
THRESHOLD_8_BASE_LOT           = 0.53

# --- instrument economics ---------------------------------------------------------
# One "point" of quote resolution for gold. The broker SPREAD column is expressed in
# points; spread_$ = SPREAD_points * THRESHOLD_8_POINT.
THRESHOLD_8_POINT              = 0.01
# Account $ produced by a $1.00 price move on a 1.00 lot position. A real MT5 gold
# contract is 100 oz (=> $100 per $1 per lot); this backtest runs on synthetic data
# and uses a smaller, configurable value so the spec's rescue-arm guard and basket
# risk bands (RESCUE_DIST vs BASKET_MAX_RISK, ladder rungs vs BASKET_TP) are mutually
# reachable rather than degenerate. Production overrides this from the symbol spec.
THRESHOLD_8_USD_PER_PRICE_PER_LOT = 10.0

# --- Rescue -----------------------------------------------------------------------
THRESHOLD_8_RESCUE_ENABLED     = True
THRESHOLD_8_RESCUE_DIST        = 10.0    # $ adverse from entry before rescue arms
THRESHOLD_8_RESCUE_LOT_MULT    = 1.20    # 0.53 -> 0.63 ; 1.00 = flat flip
THRESHOLD_8_RESCUE_MAX_PER_SIDE = 1
THRESHOLD_8_RESCUE_MIN_GAP_MIN = 3       # no rescue within N min of the entry fill
THRESHOLD_8_RESCUE_REQUIRE_CLOSE = True  # arm on M1 CLOSE beyond level, not a wick

# --- Dynamic trail (basket-$ ladder, applied to the dominant side) ----------------
THRESHOLD_8_TRAIL_MODE         = "ladder"   # "ladder" | "atr" | "hybrid"
THRESHOLD_8_TRAIL_LADDER       = [(300, 150), (500, 320), (900, 700)]
THRESHOLD_8_TRAIL_ATR_MULT     = 1.5
THRESHOLD_8_TRAIL_ACTIVATE     = 3.0     # $ move before trail engages
THRESHOLD_8_TRAIL_LOCK         = 2.5     # MUST be < ACTIVATE (see invariants)
THRESHOLD_8_TRAIL_STEP         = 0.5
THRESHOLD_8_TRAIL_ANCHOR_SWING = True    # never trail past last opposite swing

# --- Basket -----------------------------------------------------------------------
THRESHOLD_8_BASKET_TP          = 220.0   # net $ target
THRESHOLD_8_BASKET_MAX_RISK    = 300.0   # net $ loss -> close whole basket
THRESHOLD_8_BASKET_MAX_MINUTES = 240
THRESHOLD_8_DAILY_RISK         = 600.0   # incl. floating
THRESHOLD_8_FLATTEN_SERVER_HHMM = "23:30"
THRESHOLD_8_FRIDAY_FLAT        = True
THRESHOLD_8_MAGIC_OFFSET       = 8000

# --- backtest / replay ------------------------------------------------------------
# Broker server timezone is declared explicitly (UTC+3). Timestamps in the M1 CSV are
# interpreted in this zone; the replay engine's weekend-gap sanity check depends on it.
THRESHOLD_8_SERVER_TZ_OFFSET_H = 3
# M5 detectors receive CLOSED bars only; M1 drives event timing + intrabar resolution.
THRESHOLD_8_M5_MINUTES         = 5
THRESHOLD_8_ATR_PERIOD         = 14      # closed-M5 ATR window (consumed as a feature)
THRESHOLD_8_SWING_LOOKBACK     = 3       # closed-M5 fractal swing pivot half-width
# Daily anchor: the reference price is captured at this server-time HH:MM each day and
# the entry engine arms breakout/breakdown stops at anchor +/- TRIGGER_DIST.
THRESHOLD_8_ANCHOR_HHMM        = "01:00"

# --- log tag ----------------------------------------------------------------------
THRESHOLD_8_LOG_TAG            = "threshold_8"


class Threshold8Params:
    """Runtime snapshot of the tunable constants. Defaults come straight from the
    named constants above (no magic numbers); the backtest sweep constructs variants
    by overriding individual fields. Logic modules read a params instance so a sweep
    cell never mutates module globals.

    INVARIANT 1 is re-checked in __init__ so an overridden (LOCK, ACTIVATE) pair that
    inverts the relation fails loudly at construction, not just at import.
    """

    def __init__(self, **overrides):
        self.version = THRESHOLD_8_VERSION
        self.trigger_dist = THRESHOLD_8_TRIGGER_DIST
        self.sl = THRESHOLD_8_SL
        self.base_lot = THRESHOLD_8_BASE_LOT
        self.point = THRESHOLD_8_POINT
        self.usd_per_price_per_lot = THRESHOLD_8_USD_PER_PRICE_PER_LOT

        self.rescue_enabled = THRESHOLD_8_RESCUE_ENABLED
        self.rescue_dist = THRESHOLD_8_RESCUE_DIST
        self.rescue_lot_mult = THRESHOLD_8_RESCUE_LOT_MULT
        self.rescue_max_per_side = THRESHOLD_8_RESCUE_MAX_PER_SIDE
        self.rescue_min_gap_min = THRESHOLD_8_RESCUE_MIN_GAP_MIN
        self.rescue_require_close = THRESHOLD_8_RESCUE_REQUIRE_CLOSE

        self.trail_mode = THRESHOLD_8_TRAIL_MODE
        self.trail_ladder = list(THRESHOLD_8_TRAIL_LADDER)
        self.trail_atr_mult = THRESHOLD_8_TRAIL_ATR_MULT
        self.trail_activate = THRESHOLD_8_TRAIL_ACTIVATE
        self.trail_lock = THRESHOLD_8_TRAIL_LOCK
        self.trail_step = THRESHOLD_8_TRAIL_STEP
        self.trail_anchor_swing = THRESHOLD_8_TRAIL_ANCHOR_SWING

        self.basket_tp = THRESHOLD_8_BASKET_TP
        self.basket_max_risk = THRESHOLD_8_BASKET_MAX_RISK
        self.basket_max_minutes = THRESHOLD_8_BASKET_MAX_MINUTES
        self.daily_risk = THRESHOLD_8_DAILY_RISK
        self.flatten_server_hhmm = THRESHOLD_8_FLATTEN_SERVER_HHMM
        self.friday_flat = THRESHOLD_8_FRIDAY_FLAT
        self.magic_offset = THRESHOLD_8_MAGIC_OFFSET

        self.server_tz_offset_h = THRESHOLD_8_SERVER_TZ_OFFSET_H
        self.m5_minutes = THRESHOLD_8_M5_MINUTES
        self.atr_period = THRESHOLD_8_ATR_PERIOD
        self.swing_lookback = THRESHOLD_8_SWING_LOOKBACK
        self.anchor_hhmm = THRESHOLD_8_ANCHOR_HHMM

        for k, v in overrides.items():
            if not hasattr(self, k):
                raise KeyError("Threshold8Params has no field %r" % k)
            setattr(self, k, v)

        # INVARIANT 1 re-checked for any overridden pair.
        assert self.trail_lock < self.trail_activate, (
            "threshold_8 INVARIANT 1 violated (params): trail_lock (%r) must be < "
            "trail_activate (%r)" % (self.trail_lock, self.trail_activate)
        )

    def rescue_lot(self):
        """Rescue lot is a PURE function of base_lot and rescue_lot_mult — never of
        loss size or loss count (INVARIANT 6 / check C6)."""
        return round(self.base_lot * self.rescue_lot_mult, 2)

    def label(self):
        return ("rescue=%s dist=%s mult=%s trail=%s"
                % (self.rescue_enabled, self.rescue_dist, self.rescue_lot_mult,
                   self.trail_mode))


def threshold_8_magic(symbol, base_magic=0):
    """MAGIC_OFFSET + existing per-symbol magic, so threshold_8 positions are
    separable from every other system on the account. ``base_magic`` is whatever
    magic the rest of the stack already assigns the symbol (0 in the backtest)."""
    return THRESHOLD_8_MAGIC_OFFSET + int(base_magic)


def _parse_hhmm(hhmm):
    h, m = hhmm.split(":")
    return int(h), int(m)


# INVARIANT 1 — enforced at import time. Do not remove; do not weaken to <=.
assert THRESHOLD_8_TRAIL_LOCK < THRESHOLD_8_TRAIL_ACTIVATE, (
    "threshold_8 INVARIANT 1 violated: TRAIL_LOCK (%r) must be strictly < "
    "TRAIL_ACTIVATE (%r). The inverted relation produces phantom fills and fake "
    "equity curves." % (THRESHOLD_8_TRAIL_LOCK, THRESHOLD_8_TRAIL_ACTIVATE)
)
