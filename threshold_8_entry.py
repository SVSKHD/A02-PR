"""threshold_8 — read-only entry signal generator (anchor / threshold straddle).

The production entry engine (``anchors.py``) is READ-ONLY per the repo rules and is
tied to the live MT5 stack, so it cannot be driven from a bare M1 CSV. This module is
a faithful, minimal reproduction of the *anchor + threshold-crossing* rule that the
rescue / trail / basket subsystem consumes, so the backtest has a deterministic entry
source. It does NOT modify anchors.py and it invents no new entry behaviour:

  * Once per day at ANCHOR_HHMM (server time) an anchor price A is captured (the OPEN
    of the anchor M5 bar).
  * A breakout straddle is armed: a BUY stop at A + TRIGGER_DIST and a SELL stop at
    A - TRIGGER_DIST, one-cancels-the-other.
  * Detection uses CLOSED M5 bars only; fills resolve intrabar on M1 (see replay).
  * The opposite (un-filled) level becomes the "opposite anchor" — crossing it later
    is exit-precedence rule 7 and re-arms a fresh straddle in the new direction.

INVARIANT 3 support: if the anchor M5 bar's own range already ran through a level
BEFORE the straddle could be placed (we only learn A when that bar closes), that level
is UNPLACEABLE — reported, never filled. The replay engine does the counting; this
module exposes the placeability test.
"""

from threshold_8_basket import BUY, SELL


# straddle side states
ARMED = "ARMED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
UNPLACEABLE = "UNPLACEABLE"


class EntrySignal:
    """A resolved entry the replay engine should turn into an ENTRY leg."""
    __slots__ = ("side", "level", "anchor_id", "anchor_time", "kind")

    def __init__(self, side, level, anchor_id, anchor_time, kind):
        self.side = side
        self.level = level
        self.anchor_id = anchor_id
        self.anchor_time = anchor_time
        self.kind = kind          # "breakout" | "reentry"


class Threshold8EntryEngine:
    """Deterministic anchor straddle. One active straddle per symbol.

    The replay engine calls:
      * ``arm_anchor(anchor_bar, ...)`` once per day when the anchor M5 bar closes
      * ``check_fill(m1_bar)`` every M1 bar -> EntrySignal or None
      * ``opposite_triggered(m1_bar)`` every M1 bar while a basket is open
    """

    def __init__(self, params):
        self.p = params
        self.reset()

    def reset(self):
        self.anchor_id = None
        self.anchor_time = None
        self.anchor_price = None
        self.buy_level = None
        self.sell_level = None
        self.buy_state = None
        self.sell_state = None
        self.filled_side = None
        self._anchor_seq = 0

    # --- arming --------------------------------------------------------------------
    def arm_anchor(self, anchor_open, anchor_time):
        """Arm a fresh straddle around anchor price ``anchor_open`` (the open of the
        anchor M5 bar). Returns (buy_level, sell_level)."""
        self._anchor_seq += 1
        self.anchor_id = "%s#%d" % (anchor_time.strftime("%Y%m%d"), self._anchor_seq)
        self.anchor_time = anchor_time
        self.anchor_price = anchor_open
        self.buy_level = anchor_open + self.p.trigger_dist
        self.sell_level = anchor_open - self.p.trigger_dist
        self.buy_state = ARMED
        self.sell_state = ARMED
        self.filled_side = None
        return self.buy_level, self.sell_level

    def mark_unplaceable_from_anchor_bar(self, anchor_high, anchor_low):
        """If the anchor bar's OWN range already crossed a level before the straddle
        could be placed, that side is UNPLACEABLE. Returns the list of sides that were
        unplaceable (for counting)."""
        unplaceable = []
        if self.buy_state == ARMED and anchor_high >= self.buy_level:
            self.buy_state = UNPLACEABLE
            unplaceable.append(BUY)
        if self.sell_state == ARMED and anchor_low <= self.sell_level:
            self.sell_state = UNPLACEABLE
            unplaceable.append(SELL)
        return unplaceable

    def has_live_straddle(self):
        return self.buy_state == ARMED or self.sell_state == ARMED

    # --- fills ---------------------------------------------------------------------
    def check_fill(self, m1_bar):
        """If an armed level is touched by this M1 bar, return the EntrySignal and
        cancel the OCO sibling. Buy fills when high >= buy_level; sell when low <=
        sell_level. Fill price is chosen by the replay engine (level or gap open)."""
        # If both would fill on the same bar, resolve by which the open is nearer /
        # which side the bar opened beyond; default to the breakout the open is closest
        # to. Deterministic tie-break: prefer the side the bar's open already sits past.
        buy_hit = self.buy_state == ARMED and m1_bar.high >= self.buy_level
        sell_hit = self.sell_state == ARMED and m1_bar.low <= self.sell_level
        if buy_hit and sell_hit:
            # ambiguous same-bar double touch: take the side the OPEN is beyond, else
            # the nearer level to the open.
            if m1_bar.open >= self.buy_level:
                sell_hit = False
            elif m1_bar.open <= self.sell_level:
                buy_hit = False
            elif (m1_bar.open - self.sell_level) <= (self.buy_level - m1_bar.open):
                buy_hit = False
            else:
                sell_hit = False
        if buy_hit:
            self.buy_state = FILLED
            self.sell_state = CANCELLED
            self.filled_side = BUY
            return EntrySignal(BUY, self.buy_level, self.anchor_id, self.anchor_time,
                               "breakout")
        if sell_hit:
            self.sell_state = FILLED
            self.buy_state = CANCELLED
            self.filled_side = SELL
            return EntrySignal(SELL, self.sell_level, self.anchor_id, self.anchor_time,
                               "breakout")
        return None

    # --- opposite anchor (exit precedence rule 7) ----------------------------------
    def opposite_level(self):
        if self.filled_side == BUY:
            return self.sell_level
        if self.filled_side == SELL:
            return self.buy_level
        return None

    def opposite_triggered(self, m1_bar):
        """True when the opposite (un-filled) anchor level is crossed while a basket
        is open. This is exit-precedence rule 7."""
        lvl = self.opposite_level()
        if lvl is None:
            return False
        if self.filled_side == BUY:
            return m1_bar.low <= lvl
        return m1_bar.high >= lvl

    def reentry_signal(self):
        """After an opposite-anchor exit, re-arm a fresh straddle flipped into the new
        direction and report the re-entry. The new anchor is the crossed level."""
        if self.filled_side == BUY:
            new_side = SELL
            level = self.sell_level
        elif self.filled_side == SELL:
            new_side = BUY
            level = self.buy_level
        else:
            return None
        # fresh straddle centred on the crossed level, flipped direction filled
        anchor_time = self.anchor_time
        self.arm_anchor(level, anchor_time)
        if new_side == BUY:
            self.buy_state = FILLED
            self.sell_state = CANCELLED
            self.filled_side = BUY
        else:
            self.sell_state = FILLED
            self.buy_state = CANCELLED
            self.filled_side = SELL
        return EntrySignal(new_side, level, self.anchor_id, anchor_time, "reentry")
