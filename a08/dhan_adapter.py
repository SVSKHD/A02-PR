"""
AUREON A08 — DhanHQ adapter.

One seam between the strategy and the broker. PAPER mode is fully functional
(simulated fills against the live/last price, no orders sent); LIVE mode wires
the `dhanhq` SDK -- those calls are marked TODO(live) and must be demo-verified
before any real rupee.

Responsibilities:
  - auth + session
  - instrument master CSV -> security_id for the current-month contract,
    with expiry-roll logic (the source MT5 system never needed this)
  - order placement: SL-M (straddle/ladder stops), MARKET (boosts/TSTOP/flatten),
    optional Dhan "Super Order" (entry+SL+TP bracket)
  - live market feed (websocket) -> last price
  - available margin (SPAN+exposure) query for per-anchor sizing
  - rate-limit guard: <= 1 modify per leg per minute

Order-type mapping (from the handoff):
  SL-M   -> straddle stops + ladder stops
  MARKET -> boosts, TSTOP, EOD flatten
  Super Order can host the bracket; else manage SL/TP server-side in app.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("A08.dhan")


# ---------------------------------------------------------------------------
# Order model (broker-agnostic; the strategy speaks this, the adapter maps it)
# ---------------------------------------------------------------------------

ORDER_SLM = "SL-M"
ORDER_MARKET = "MARKET"

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"


@dataclass
class Order:
    order_id: str
    side: str                 # BUY / SELL
    qty_lots: int
    order_type: str           # SL-M / MARKET
    trigger_price: Optional[float] = None   # for SL-M
    tag: str = ""             # anchor/leg label for the journal
    status: str = "WORKING"   # WORKING / FILLED / CANCELLED / REJECTED
    fill_price: Optional[float] = None
    security_id: Optional[str] = None
    last_modified: float = 0.0


# ---------------------------------------------------------------------------
# Base adapter
# ---------------------------------------------------------------------------

class DhanAdapter:
    """Live DhanHQ adapter. PAPER subclass overrides the I/O methods."""

    def __init__(self, cfg, client_id: str = "", access_token: str = ""):
        self.cfg = cfg
        self.client_id = client_id
        self.access_token = access_token
        self._dhan = None
        self._security_ids: Dict[str, str] = {}     # symbol -> security_id
        self._expiry: Dict[str, str] = {}           # symbol -> expiry date
        self._orders: Dict[str, Order] = {}

    # ---- session --------------------------------------------------------
    def connect(self):
        """TODO(live): from dhanhq import dhanhq; self._dhan = dhanhq(id, token)."""
        from dhanhq import dhanhq  # type: ignore
        self._dhan = dhanhq(self.client_id, self.access_token)
        log.info("DhanHQ session established")

    # ---- instrument master + expiry roll --------------------------------
    def load_instrument_master(self):
        """Download Dhan instrument CSV, resolve current-month security_id.

        TODO(live): fetch the master CSV, filter EXCH=MCX SEGMENT=COMM
        SYMBOL=<instrument>, pick the nearest non-expired contract.
        """
        raise NotImplementedError("wire the Dhan instrument CSV in live mode")

    def current_security_id(self, symbol: str) -> str:
        return self._security_ids[symbol]

    def should_roll(self, symbol: str, today) -> bool:
        """True when within cfg.roll_days_before_expiry of expiry."""
        import pandas as pd
        exp = self._expiry.get(symbol)
        if not exp:
            return False
        days = (pd.Timestamp(exp).normalize() - pd.Timestamp(today).normalize()).days
        return days <= self.cfg.roll_days_before_expiry

    # ---- market data ----------------------------------------------------
    def mcx_last_price(self, symbol: str) -> float:
        """TODO(live): last traded price for the resolved security_id."""
        raise NotImplementedError

    def xau_usd_price(self) -> float:
        """TODO(live): XAUUSD spot (external feed) for the daily R calc."""
        raise NotImplementedError

    # ---- orders ---------------------------------------------------------
    def place_slm(self, side: str, qty_lots: int, trigger_price: float,
                  tag: str = "") -> Order:
        """TODO(live): self._dhan.place_order(... order_type='SL-M' ...)."""
        raise NotImplementedError

    def place_market(self, side: str, qty_lots: int, tag: str = "") -> Order:
        """TODO(live): self._dhan.place_order(... order_type='MARKET' ...)."""
        raise NotImplementedError

    def modify_trigger(self, order: Order, new_trigger: float) -> bool:
        """Rate-limited: <= 1 modify per leg per minute (Dhan throttles)."""
        now = time.time()
        if now - order.last_modified < self.cfg.modify_min_interval_sec:
            log.debug(f"modify suppressed (rate limit) for {order.order_id}")
            return False
        # TODO(live): self._dhan.modify_order(...)
        order.trigger_price = new_trigger
        order.last_modified = now
        return True

    def cancel(self, order: Order) -> bool:
        """TODO(live): self._dhan.cancel_order(order_id)."""
        raise NotImplementedError

    # ---- margin ---------------------------------------------------------
    def available_margin(self) -> float:
        """TODO(live): SPAN+exposure available margin (fund limits API)."""
        raise NotImplementedError

    def required_margin(self, symbol: str, qty_lots: int) -> float:
        """TODO(live): margin calculator API for the order."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Paper adapter -- fully functional simulation, no orders sent
# ---------------------------------------------------------------------------

@dataclass
class _PaperBook:
    last_price: float = 0.0
    xau_price: float = 0.0
    margin: float = 1_000_000.0


class PaperDhanAdapter(DhanAdapter):
    """Simulated broker. Feed it ticks via set_price(); fills resolve on tick.

    SL-M orders fill when price crosses the trigger; MARKET fills at last price.
    This is the surface the demo/paper record is built on -- the MT5 record does
    NOT transfer across the netting change, so this is where A08 earns its own.
    """

    def __init__(self, cfg, start_price: float = 0.0, xau_price: float = 0.0):
        super().__init__(cfg)
        self.book = _PaperBook(last_price=start_price, xau_price=xau_price,
                               margin=cfg.starting_capital_inr * 5)  # ~5x leverage
        self._working: Dict[str, Order] = {}

    # session / master are no-ops in paper
    def connect(self):
        log.info("PAPER adapter: no broker session")

    def load_instrument_master(self):
        self._security_ids[self.cfg.instrument] = "PAPER-" + self.cfg.instrument
        self._expiry[self.cfg.instrument] = "2099-12-31"

    # market data
    def set_price(self, mcx_price: float, xau_price: Optional[float] = None):
        self.book.last_price = mcx_price
        if xau_price is not None:
            self.book.xau_price = xau_price

    def mcx_last_price(self, symbol: str) -> float:
        return self.book.last_price

    def xau_usd_price(self) -> float:
        return self.book.xau_price

    # orders
    def _new(self, side, qty, otype, trigger, tag) -> Order:
        oid = "P-" + uuid.uuid4().hex[:8]
        o = Order(order_id=oid, side=side, qty_lots=qty, order_type=otype,
                  trigger_price=trigger, tag=tag,
                  security_id=self._security_ids.get(symbol_or(self.cfg)))
        self._orders[oid] = o
        return o

    def place_slm(self, side, qty_lots, trigger_price, tag="") -> Order:
        o = self._new(side, qty_lots, ORDER_SLM, trigger_price, tag)
        self._working[o.order_id] = o
        log.info(f"PAPER SL-M {side} {qty_lots}@trig {trigger_price} [{tag}]")
        return o

    def place_market(self, side, qty_lots, tag="") -> Order:
        o = self._new(side, qty_lots, ORDER_MARKET, None, tag)
        o.status = "FILLED"
        o.fill_price = self.book.last_price
        log.info(f"PAPER MKT {side} {qty_lots}@{o.fill_price} [{tag}]")
        return o

    def modify_trigger(self, order, new_trigger) -> bool:
        ok = super().modify_trigger(order, new_trigger)
        return ok

    def cancel(self, order) -> bool:
        order.status = "CANCELLED"
        self._working.pop(order.order_id, None)
        return True

    def available_margin(self) -> float:
        return self.book.margin

    def required_margin(self, symbol, qty_lots) -> float:
        # rough SPAN+exposure proxy: ~10% of contract notional per lot
        inst = self.cfg.inst()
        notional = self.book.last_price * (inst.lot_grams / inst.quote_grams)
        return 0.10 * notional * qty_lots

    # simulation tick: resolve any SL-M whose trigger is crossed
    def on_tick(self, price: float) -> List[Order]:
        self.book.last_price = price
        filled = []
        for oid, o in list(self._working.items()):
            if o.status != "WORKING":
                continue
            crossed = (o.side == SIDE_BUY and price >= o.trigger_price) or \
                      (o.side == SIDE_SELL and price <= o.trigger_price)
            if crossed:
                o.status = "FILLED"
                o.fill_price = o.trigger_price
                self._working.pop(oid, None)
                filled.append(o)
        return filled


def symbol_or(cfg) -> str:
    return cfg.instrument


def make_adapter(cfg, client_id="", access_token="",
                 start_price=0.0, xau_price=0.0) -> DhanAdapter:
    """Factory: PAPER unless cfg.paper is False."""
    if cfg.paper:
        return PaperDhanAdapter(cfg, start_price=start_price, xau_price=xau_price)
    return DhanAdapter(cfg, client_id=client_id, access_token=access_token)
