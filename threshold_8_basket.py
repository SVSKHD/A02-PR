"""threshold_8 — Module A: Basket & Exposure state.

State only, NO decisions. This module records legs and recomputes exposure. Every
decision (rescue / trail / exit) is made elsewhere and reads ``basket.net_pnl`` — no
leg is ever evaluated on its own P/L for a basket-level decision. (A leg's OWN stop
loss is a separate, leg-local concern handled by the replay engine; it never closes
the basket.)

P/L convention: a leg's dollar P/L is
    (mark - entry_price) * side_sign * lot * USD_PER_PRICE_PER_LOT
where side_sign is +1 for BUY, -1 for SELL. For an OPEN leg ``mark`` is the current
bar price and ``leg.pnl`` holds that floating value; for a CLOSED leg ``leg.pnl`` is
frozen at its exit. INVARIANT 5 holds by construction and is asserted every update:
    sum(leg.pnl for all legs) == basket.realized_pnl + basket.floating_pnl  (tol 1e-6)
"""

from threshold_8_config import (
    THRESHOLD_8_USD_PER_PRICE_PER_LOT,
)

BUY = "BUY"
SELL = "SELL"

ROLE_ENTRY = "ENTRY"
ROLE_RESCUE = "RESCUE"

# basket states
STATE_OPEN = "OPEN"
STATE_RESCUED = "RESCUED"
STATE_RECOVERING = "RECOVERING"
STATE_TRAILING = "TRAILING"
STATE_CLOSED = "CLOSED"


def side_sign(side):
    return 1.0 if side == BUY else -1.0


def opposite(side):
    return SELL if side == BUY else BUY


def leg_pnl(side, entry_price, mark, lot):
    """Signed account $ of a leg marked at ``mark``. Pure function."""
    return (mark - entry_price) * side_sign(side) * lot * THRESHOLD_8_USD_PER_PRICE_PER_LOT


class Threshold8Leg:
    __slots__ = (
        "ticket", "side", "lot", "entry_price", "sl", "role",
        "opened_at", "opened_bar_index", "closed_at", "exit_price",
        "exit_reason", "pnl", "is_open", "_sl_at_fill", "_entry_spread_price",
    )

    def __init__(self, ticket, side, lot, entry_price, sl, role,
                 opened_at, opened_bar_index):
        self.ticket = ticket
        self.side = side
        self.lot = lot
        self.entry_price = entry_price
        self.sl = sl
        self.role = role
        self.opened_at = opened_at
        self.opened_bar_index = opened_bar_index
        self.closed_at = None
        self.exit_price = None
        self.exit_reason = None
        self.pnl = 0.0
        self.is_open = True
        # INVARIANT 8 support: record the SL the leg was born with. The ENTRY leg's
        # SL must equal this at basket close (never widened / moved / cancelled).
        self._sl_at_fill = sl
        self._entry_spread_price = 0.0

    def mark(self, price):
        """Update floating pnl for an open leg."""
        if self.is_open:
            self.pnl = leg_pnl(self.side, self.entry_price, price, self.lot)
        return self.pnl

    def close(self, price, when, reason):
        if not self.is_open:
            return
        self.pnl = leg_pnl(self.side, self.entry_price, price, self.lot)
        self.exit_price = price
        self.closed_at = when
        self.exit_reason = reason
        self.is_open = False


class Threshold8Basket:
    def __init__(self, basket_id, symbol, anchor_id, anchor_time, magic, opened_at):
        self.basket_id = basket_id
        self.symbol = symbol
        self.anchor_id = anchor_id
        self.anchor_time = anchor_time
        self.magic = magic
        self.legs = []
        self.opened_at = opened_at
        self.realized_pnl = 0.0
        self.floating_pnl = 0.0
        self.net_pnl = 0.0
        self.peak_net = 0.0
        self.trough_net = 0.0
        self.buy_lots = 0.0
        self.sell_lots = 0.0
        self.net_lots = 0.0
        self.dominant_side = None
        self.state = STATE_OPEN
        # trail bookkeeping (owned by the trail engine, stored on the basket so the
        # exit function and the CSV can read it). locked is monotonic non-decreasing.
        self.trail_locked = None
        self.trail_stop_price = None
        self.trail_rung_reached = 0
        self.trail_engaged = False
        # diagnostics for the per-basket CSV
        self.max_floating_loss = 0.0
        self.max_floating_profit = 0.0

    # --- leg management ------------------------------------------------------------
    def add_leg(self, leg):
        self.legs.append(leg)

    def entry_legs(self):
        return [l for l in self.legs if l.role == ROLE_ENTRY]

    def rescue_legs(self):
        return [l for l in self.legs if l.role == ROLE_RESCUE]

    def open_legs(self):
        return [l for l in self.legs if l.is_open]

    def entry_leg(self):
        legs = self.entry_legs()
        return legs[0] if legs else None

    def rescue_legs_on_side(self, side):
        return [l for l in self.rescue_legs() if l.side == side]

    # --- exposure ------------------------------------------------------------------
    def update(self, price):
        """Recompute exposure at ``price`` (M1 close in backtest, tick live).

        No leg is evaluated on its own P/L here — this only aggregates. Returns the
        basket net_pnl. Asserts INVARIANT 5 (leg-sum P/L identity)."""
        realized = 0.0
        floating = 0.0
        buy_lots = 0.0
        sell_lots = 0.0
        leg_sum = 0.0
        for leg in self.legs:
            if leg.is_open:
                leg.mark(price)
                floating += leg.pnl
                if leg.side == BUY:
                    buy_lots += leg.lot
                else:
                    sell_lots += leg.lot
            else:
                realized += leg.pnl
            leg_sum += leg.pnl

        self.realized_pnl = realized
        self.floating_pnl = floating
        self.net_pnl = realized + floating
        self.buy_lots = round(buy_lots, 10)
        self.sell_lots = round(sell_lots, 10)
        self.net_lots = round(buy_lots - sell_lots, 10)
        if self.net_lots > 0:
            self.dominant_side = BUY
        elif self.net_lots < 0:
            self.dominant_side = SELL
        else:
            # equal lots: dominant is the side of the ENTRY leg (the basket's origin)
            e = self.entry_leg()
            self.dominant_side = e.side if e else None

        if self.net_pnl > self.peak_net:
            self.peak_net = self.net_pnl
        if self.net_pnl < self.trough_net:
            self.trough_net = self.net_pnl
        if self.floating_pnl < self.max_floating_loss:
            self.max_floating_loss = self.floating_pnl
        if self.floating_pnl > self.max_floating_profit:
            self.max_floating_profit = self.floating_pnl

        # INVARIANT 5 — P/L identity, every bar, to 1e-6.
        assert abs(leg_sum - self.net_pnl) < 1e-6, (
            "threshold_8 INVARIANT 5 violated: sum(leg.pnl)=%r != realized+floating=%r"
            % (leg_sum, self.net_pnl)
        )
        return self.net_pnl

    def dominant_leg(self):
        """The open leg carrying the basket's net directional exposure. The trail
        engine trails this leg's price."""
        if self.dominant_side is None:
            return None
        opens = [l for l in self.open_legs() if l.side == self.dominant_side]
        if not opens:
            return None
        # largest lot on the dominant side
        return max(opens, key=lambda l: l.lot)

    def is_closed(self):
        return self.state == STATE_CLOSED

    def close_all(self, price, when, reason):
        """Close every open leg at ``price`` with ``reason`` and mark basket CLOSED.
        Used by the exit engine. Recomputes exposure once more so realized is final."""
        for leg in self.open_legs():
            leg.close(price, when, reason)
        self.update(price)
        self.state = STATE_CLOSED
        self.close_reason = reason
        self.closed_at = when
        return self.net_pnl

    def duration_min(self, now):
        end = getattr(self, "closed_at", None) or now
        return (end - self.opened_at).total_seconds() / 60.0
