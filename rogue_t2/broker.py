"""Rogue T2 — execution layer (the hardened part).

Wraps an injected MT5 handle. ALL destructive operations filter strictly by this
bot's magic, so on the shared XAUUSD account it can NEVER touch the Aureon straddle,
the rogue rider, the fetcher, or a manual trade. When trading_unlocked is False (the
default), every order routes through the inert _simulated_send(): nothing leaves the
process. Tests inject a FakeMT5 to exercise both the magic isolation and the
simulated-send gate without a terminal.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional

log = logging.getLogger("ROGUE_T2")


@dataclass
class Tick:
    bid: float
    ask: float
    time_msc: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class OwnPosition:
    ticket: int
    side: str      # 'BUY' / 'SELL'
    entry: float
    sl: float
    lot: float
    tag: str
    magic: int


@dataclass
class OwnPending:
    ticket: int
    side: str
    price: float
    sl: float
    lot: float
    tag: str
    magic: int


class MT5Broker:
    def __init__(self, mt5, cfg, notifier=None):
        self.mt5 = mt5
        self.cfg = cfg
        self.magic = cfg.magic
        self.symbol = cfg.symbol
        self.notify = notifier
        self._sim_ticket = 900_000_000    # synthetic tickets for simulated sends
        self.last_tick_msc = 0

    # --- startup assertions ------------------------------------------------------
    def startup_assertions(self) -> None:
        """Refuse to run unless margin mode is RETAIL_HEDGING, expert trading is
        allowed, and the symbol is visible. Raises RuntimeError otherwise."""
        mt5 = self.mt5
        acct = mt5.account_info()
        if acct is None:
            raise RuntimeError("startup: account_info() is None (not logged in)")
        hedging = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        if getattr(acct, "margin_mode", None) != hedging:
            raise RuntimeError(
                f"startup: margin_mode {getattr(acct, 'margin_mode', None)} != "
                f"RETAIL_HEDGING ({hedging}) — refusing to run")
        if not getattr(acct, "trade_expert", False):
            raise RuntimeError("startup: expert trading not allowed on this account")
        term = mt5.terminal_info()
        if term is not None and not getattr(term, "trade_allowed", True):
            raise RuntimeError("startup: terminal trade_allowed is False")
        info = mt5.symbol_info(self.symbol)
        if info is None or not getattr(info, "visible", False):
            # try to make it visible once
            if not mt5.symbol_select(self.symbol, True):
                raise RuntimeError(f"startup: symbol {self.symbol} not visible")
        return None

    # --- tick consumption --------------------------------------------------------
    def consume_ticks(self, last_msc: Optional[int] = None) -> List[Tick]:
        """Consume EVERY tick since last processed (not just the latest) via
        copy_ticks_from. Advances self.last_tick_msc. Returns chronological ticks."""
        mt5 = self.mt5
        since = self.last_tick_msc if last_msc is None else last_msc
        flags = getattr(mt5, "COPY_TICKS_ALL", 0)
        from_time = max(0, since // 1000)  # copy_ticks_from wants seconds
        raw = mt5.copy_ticks_from(self.symbol, from_time, 10_000, flags)
        out: List[Tick] = []
        for r in (raw or []):
            tmsc = int(r["time_msc"] if isinstance(r, dict) else r["time_msc"])
            if tmsc <= since:
                continue
            bid = float(r["bid"] if isinstance(r, dict) else r["bid"])
            ask = float(r["ask"] if isinstance(r, dict) else r["ask"])
            out.append(Tick(bid=bid, ask=ask, time_msc=tmsc))
        out.sort(key=lambda t: t.time_msc)
        if out:
            self.last_tick_msc = out[-1].time_msc
        return out

    def latest_tick(self) -> Optional[Tick]:
        t = self.mt5.symbol_info_tick(self.symbol)
        if t is None:
            return None
        return Tick(bid=float(t.bid), ask=float(t.ask),
                    time_msc=int(getattr(t, "time_msc", int(time.time() * 1000))))

    # --- own-magic reads (isolation by construction) -----------------------------
    def _is_ours(self, o) -> bool:
        return int(getattr(o, "magic", -1)) == self.magic

    @staticmethod
    def _side_from_pos_type(mt5, t) -> str:
        return "BUY" if t == getattr(mt5, "POSITION_TYPE_BUY", 0) else "SELL"

    @staticmethod
    def _side_from_order_type(mt5, t) -> str:
        return "BUY" if t in (getattr(mt5, "ORDER_TYPE_BUY_STOP", 4),
                              getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2),
                              getattr(mt5, "ORDER_TYPE_BUY", 0)) else "SELL"

    def own_positions(self) -> List[OwnPosition]:
        mt5 = self.mt5
        raw = mt5.positions_get(symbol=self.symbol) or []
        return [OwnPosition(
            ticket=int(p.ticket), side=self._side_from_pos_type(mt5, p.type),
            entry=float(p.price_open), sl=float(getattr(p, "sl", 0.0) or 0.0),
            lot=float(p.volume), tag=str(getattr(p, "comment", "")), magic=int(p.magic))
            for p in raw if self._is_ours(p)]

    def own_pendings(self) -> List[OwnPending]:
        mt5 = self.mt5
        raw = mt5.orders_get(symbol=self.symbol) or []
        return [OwnPending(
            ticket=int(o.ticket), side=self._side_from_order_type(mt5, o.type),
            price=float(o.price_open), sl=float(getattr(o, "sl", 0.0) or 0.0),
            lot=float(getattr(o, "volume_current", getattr(o, "volume_initial", 0.0))),
            tag=str(getattr(o, "comment", "")), magic=int(o.magic))
            for o in raw if self._is_ours(o)]

    # --- the DEFAULT (inert) order path ------------------------------------------
    def _simulated_send(self, request: dict):
        """Default order path while trading_unlocked is False. Places NOTHING at the
        broker; returns a synthetic DONE result with a fake ticket so the loop and
        state machine run end-to-end in dry mode."""
        self._sim_ticket += 1
        log.info(f"[SIMULATED] order_send {request.get('_tag','?')} "
                 f"{request.get('type')} @ {request.get('price')} (no real order)")

        class _SimResult:
            pass
        r = _SimResult()
        r.retcode = getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
        r.order = self._sim_ticket
        r.deal = 0
        r.comment = "SIMULATED"
        return r

    def _send(self, request: dict):
        """Route through the simulated path unless trading is explicitly unlocked."""
        if not getattr(self.cfg, "trading_unlocked", False):
            return self._simulated_send(request)
        return self.mt5.order_send(request)

    # --- placement ---------------------------------------------------------------
    def place_pending(self, side: str, price: float, sl: float, tag: str) -> Optional[int]:
        mt5 = self.mt5
        otype = (getattr(mt5, "ORDER_TYPE_BUY_STOP", 4) if side == "BUY"
                 else getattr(mt5, "ORDER_TYPE_SELL_STOP", 5))
        req = {
            "action": getattr(mt5, "TRADE_ACTION_PENDING", 5),
            "symbol": self.symbol, "volume": float(self.cfg.lot), "type": otype,
            "price": float(price), "sl": float(sl),
            "magic": self.magic, "comment": tag[:31],
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1),
            "_tag": tag,
        }
        res = self._send(req)
        rc = getattr(res, "retcode", None)
        if rc != getattr(mt5, "TRADE_RETCODE_DONE", 10009):
            log.error(f"place_pending {tag} rejected rc={rc}")
            return None
        return int(getattr(res, "order", 0)) or None

    def modify_sl(self, ticket: int, new_sl: float) -> bool:
        mt5 = self.mt5
        req = {"action": getattr(mt5, "TRADE_ACTION_SLTP", 6),
               "position": int(ticket), "sl": float(new_sl), "_tag": "TRAIL"}
        res = self._send(req)
        return getattr(res, "retcode", None) == getattr(mt5, "TRADE_RETCODE_DONE", 10009)

    def cancel(self, ticket: int) -> bool:
        mt5 = self.mt5
        req = {"action": getattr(mt5, "TRADE_ACTION_REMOVE", 2),
               "order": int(ticket), "_tag": "CANCEL"}
        res = self._send(req)
        return getattr(res, "retcode", None) == getattr(mt5, "TRADE_RETCODE_DONE", 10009)

    def close_position(self, ticket: int) -> bool:
        mt5 = self.mt5
        req = {"action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
               "position": int(ticket), "symbol": self.symbol, "_tag": "CLOSE"}
        res = self._send(req)
        return getattr(res, "retcode", None) == getattr(mt5, "TRADE_RETCODE_DONE", 10009)

    # --- destructive, MAGIC-FILTERED cleanup -------------------------------------
    def cancel_own_pendings(self) -> int:
        n = 0
        for o in self.own_pendings():   # already magic-filtered
            if self.cancel(o.ticket):
                n += 1
        return n

    def flatten_own(self) -> int:
        n = 0
        for p in self.own_positions():  # already magic-filtered
            if self.close_position(p.ticket):
                n += 1
        return n

    # --- realized PnL from broker deal history (own magic, incl fees) ------------
    def day_realized_usd(self, from_ts, to_ts) -> float:
        mt5 = self.mt5
        deals = mt5.history_deals_get(from_ts, to_ts) or []
        total = 0.0
        for d in deals:
            if int(getattr(d, "magic", -1)) != self.magic:
                continue
            total += (float(getattr(d, "profit", 0.0))
                      + float(getattr(d, "commission", 0.0))
                      + float(getattr(d, "swap", 0.0)))
        return total
