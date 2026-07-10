"""AUREON offline simulator — the FAKE BROKER (Part 1B).

A `FakeMT5` object implementing the RAW MetaTrader5 API surface the live code
calls (via mt5_adapter.MT5Adapter -> self.mt5.*). The REAL MT5Adapter is wrapped
around it (see simulator.py), so NO adapter logic is reimplemented -- only the
broker is simulated. Fills happen on TICK TOUCH at tick+spread; SL/TP fire when a
tick crosses them; the sequence of ticks IS the intrabar order (the whole reason
this is a tick sim, not a bar sim).

!!! GATE-NOT-RUN — baseline never reproduced against MT5 truth.
!!! No number this produces is trustworthy.

Positions and deals carry REAL magics and REAL AUR_* comments (whatever the live
code sent in the order request), so pnl_report's classifier and
pnl_source.magic_day_net work on the simulated deal history UNCHANGED.
"""
from __future__ import annotations

import types

# --- MT5 constant surface (mirrors the values the live code compares against) ---
TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_REMOVE = 2
TRADE_ACTION_MODIFY = 7
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5
ORDER_TIME_GTC = 0
ORDER_TIME_DAY = 1
ORDER_FILLING_IOC = 1
ORDER_FILLING_FOK = 0
ORDER_FILLING_RETURN = 2
TRADE_RETCODE_DONE = 10009
POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
SYMBOL_TRADE_MODE_FULL = 4
ACCOUNT_TRADE_MODE_DEMO = 0
COPY_TICKS_INFO = 1
TIMEFRAME_M1 = 1
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_H1 = 16385
TIMEFRAME_D1 = 16408


class _Obj(types.SimpleNamespace):
    """A namespace that also supports dict-style ['x'] access (some MT5 result
    consumers use _asdict; a few read attributes)."""
    def _asdict(self):
        return dict(self.__dict__)
    def __getitem__(self, k):
        return self.__dict__[k]


class FakeBroker:
    """Holds simulated broker state: open positions, pending orders, realized deal
    history, the current tick, and the account balance. Advanced one tick at a
    time by the simulator; fills/SLs/TPs are evaluated on each advance."""

    def __init__(self, symbol, cfg, starting_balance=None, spread=0.20,
                 slippage=0.0, broker_tz_offset_hours=3.0):
        self.symbol = symbol
        self.cfg = cfg
        self.contract_size = float(getattr(cfg, 'contract_size', 100.0))
        self.spread = float(spread)
        self.slippage = float(slippage)
        self.offset = float(broker_tz_offset_hours)
        self.balance = float(starting_balance if starting_balance is not None
                             else getattr(cfg, 'starting_balance', 100000.0))
        self._ticket = 700000
        self.positions = {}          # ticket -> _Obj position
        self.pendings = {}           # ticket -> _Obj pending order
        self.deals = []              # list[_Obj] realized deal history (IN + OUT)
        self.cur = None              # current tick _Obj(time, bid, ask)
        self._bars = None            # optional M1 frame for copy_rates (set by simulator)

    # -- ticket allocator -------------------------------------------------------
    def _next(self):
        self._ticket += 1
        return self._ticket

    # -- broker-epoch time of the current tick (UTC + offset) -------------------
    def _broker_epoch(self):
        import pandas as pd
        t = pd.Timestamp(self.cur.time_utc)
        return int((t + pd.Timedelta(hours=self.offset)).timestamp())

    def _dir(self, side):
        return 1.0 if side in ('BUY', ORDER_TYPE_BUY) else -1.0

    def _pos_value(self, p):
        """Unrealized $ for an open position at the current bid/ask."""
        px = self.cur.bid if p.type == POSITION_TYPE_BUY else self.cur.ask
        sgn = 1.0 if p.type == POSITION_TYPE_BUY else -1.0
        return round(sgn * (px - p.price_open) * p.volume * self.contract_size, 2)

    # -- deal writers -----------------------------------------------------------
    def _mk_deal(self, *, position_id, order, entry, ptype, volume, price, magic,
                 comment, profit=0.0):
        d = _Obj(ticket=self._next(), order=order, position_id=position_id,
                 time=self._broker_epoch(), time_msc=self._broker_epoch() * 1000,
                 type=ptype, entry=entry, magic=int(magic), volume=float(volume),
                 price=float(price), commission=0.0, swap=0.0, profit=round(float(profit), 2),
                 symbol=self.symbol, comment=str(comment or ''), reason=0, fee=0.0)
        self.deals.append(d)
        return d

    # -- open / close -----------------------------------------------------------
    def _open_position(self, side, volume, price, sl, tp, magic, comment):
        tk = self._next()
        ptype = POSITION_TYPE_BUY if side in ('BUY', ORDER_TYPE_BUY) else POSITION_TYPE_SELL
        p = _Obj(ticket=tk, symbol=self.symbol, type=ptype, volume=float(volume),
                 price_open=float(price), sl=float(sl or 0.0), tp=float(tp or 0.0),
                 price_current=float(price), profit=0.0, swap=0.0, commission=0.0,
                 magic=int(magic), comment=str(comment or ''), time=self._broker_epoch(),
                 time_msc=self._broker_epoch() * 1000, identifier=tk, reason=0)
        self.positions[tk] = p
        self._mk_deal(position_id=tk, order=tk, entry=DEAL_ENTRY_IN, ptype=ptype,
                      volume=volume, price=price, magic=magic, comment=comment, profit=0.0)
        return tk

    def _close_position(self, tk, price, reason='close'):
        p = self.positions.pop(tk, None)
        if p is None:
            return None
        sgn = 1.0 if p.type == POSITION_TYPE_BUY else -1.0
        profit = round(sgn * (float(price) - p.price_open) * p.volume * self.contract_size, 2)
        self.balance = round(self.balance + profit, 2)
        # the OUT deal carries the SAME comment/magic as the position -> classifier works
        self._mk_deal(position_id=tk, order=tk, entry=DEAL_ENTRY_OUT, ptype=p.type,
                      volume=p.volume, price=price, magic=p.magic, comment=p.comment,
                      profit=profit)
        return profit

    # -- per-tick advance: evaluate pending fills, then SL/TP touches ------------
    def advance(self, tick):
        """Set the current tick and resolve any pending-order fills + SL/TP hits.
        `tick` = object with .time_utc (tz-aware UTC ts), .bid, .ask."""
        self.cur = tick
        # 1) pending STOP orders: BUY_STOP fills when ask >= price; SELL_STOP when bid <= price
        for tk in list(self.pendings):
            o = self.pendings[tk]
            fill = None
            if o.type == ORDER_TYPE_BUY_STOP and self.cur.ask >= o.price_open:
                fill = self.cur.ask + self.slippage
            elif o.type == ORDER_TYPE_SELL_STOP and self.cur.bid <= o.price_open:
                fill = self.cur.bid - self.slippage
            if fill is not None:
                self.pendings.pop(tk, None)
                side = 'BUY' if o.type == ORDER_TYPE_BUY_STOP else 'SELL'
                self._open_position(side, o.volume, round(fill, 2), o.sl, o.tp,
                                    o.magic, o.comment)
        # 2) open positions: SL/TP on tick touch (SL checked first = worst-case)
        for tk in list(self.positions):
            p = self.positions.get(tk)
            if p is None:
                continue
            p.price_current = self.cur.bid if p.type == POSITION_TYPE_BUY else self.cur.ask
            p.profit = self._pos_value(p)
            if p.type == POSITION_TYPE_BUY:
                if p.sl and self.cur.bid <= p.sl:
                    self._close_position(tk, p.sl, 'sl')
                elif p.tp and self.cur.bid >= p.tp:
                    self._close_position(tk, p.tp, 'tp')
            else:
                if p.sl and self.cur.ask >= p.sl:
                    self._close_position(tk, p.sl, 'sl')
                elif p.tp and self.cur.ask <= p.tp:
                    self._close_position(tk, p.tp, 'tp')

    # -- the ONE mutation entrypoint (mirrors what mt5.order_send accepts) -------
    def order_send(self, req):
        """Simulate MetaTrader5.order_send. Handles the four request shapes the
        live adapter builds: PENDING (stop order), DEAL (market open OR position
        close), REMOVE (cancel pending), SLTP/MODIFY (adjust SL/TP). Always acks
        rc=10009 with .order/.deal set, so the adapter's rc=-1 reconciliation
        (with its time.sleep) never triggers."""
        action = req.get("action")
        if action == TRADE_ACTION_PENDING:
            tk = self._next()
            o = _Obj(ticket=tk, symbol=req.get("symbol", self.symbol),
                     type=req["type"], volume=float(req["volume"]),
                     price_open=float(req["price"]), sl=float(req.get("sl", 0.0) or 0.0),
                     tp=float(req.get("tp", 0.0) or 0.0), magic=int(req.get("magic", 0)),
                     comment=str(req.get("comment", "")), volume_current=float(req["volume"]),
                     time_setup=self._broker_epoch())
            self.pendings[tk] = o
            return _Obj(retcode=TRADE_RETCODE_DONE, order=tk, deal=0,
                        price=float(req["price"]), comment="sim", request_id=tk)
        if action == TRADE_ACTION_DEAL:
            if req.get("position"):          # CLOSE an open position
                tk = int(req["position"])
                price = float(req.get("price") or (self.cur.bid if self.cur else 0.0))
                self._close_position(tk, price, 'close')
                return _Obj(retcode=TRADE_RETCODE_DONE, order=tk, deal=tk,
                            price=price, comment="sim-close", request_id=tk)
            # MARKET open
            side = 'BUY' if req["type"] == ORDER_TYPE_BUY else 'SELL'
            price = float(req.get("price") or (self.cur.ask if side == 'BUY' else self.cur.bid))
            price += self.slippage * (1 if side == 'BUY' else -1)
            tk = self._open_position(side, req["volume"], round(price, 2),
                                     req.get("sl", 0.0), req.get("tp", 0.0),
                                     req.get("magic", 0), req.get("comment", ""))
            return _Obj(retcode=TRADE_RETCODE_DONE, order=tk, deal=tk,
                        price=round(price, 2), comment="sim-mkt", request_id=tk)
        if action == TRADE_ACTION_REMOVE:
            tk = int(req.get("order"))
            self.pendings.pop(tk, None)
            return _Obj(retcode=TRADE_RETCODE_DONE, order=tk, deal=0, comment="sim-cancel")
        if action in (TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY):
            tk = int(req.get("position") or req.get("order") or 0)
            p = self.positions.get(tk)
            if p is not None:
                if "sl" in req:
                    p.sl = float(req["sl"] or 0.0)
                if "tp" in req:
                    p.tp = float(req["tp"] or 0.0)
            return _Obj(retcode=TRADE_RETCODE_DONE, order=tk, deal=0, comment="sim-sltp")
        return _Obj(retcode=TRADE_RETCODE_DONE, order=0, deal=0, comment="sim-noop")

    # -- bar/tick history from the loaded day frame (for get_m5_close etc.) ------
    def _rates(self, timeframe, dt_from, dt_to):
        """M1/M5 OHLC bars from the day's tick frame, in broker-epoch time (the
        adapter shifts UTC->broker before calling, so we compare in broker time)."""
        import numpy as np
        import pandas as pd
        if self._bars is None or len(self._bars) == 0:
            return np.array([])
        freq = '5min' if int(timeframe) == TIMEFRAME_M5 else '1min'
        df = self._bars
        # index by tz-aware UTC time so resample works (raw frame has a RangeIndex)
        idx = pd.to_datetime(df['time'], utc=True)
        mid = pd.Series(((df['bid'].values + df['ask'].values) / 2.0), index=idx)
        # bar index is UTC; the adapter passes BROKER-local naive datetimes -> shift back
        f = pd.Timestamp(dt_from); t = pd.Timestamp(dt_to)
        if f.tzinfo is None:
            f = f.tz_localize('UTC') - pd.Timedelta(hours=self.offset)
        if t.tzinfo is None:
            t = t.tz_localize('UTC') - pd.Timedelta(hours=self.offset)
        o = mid.resample(freq).ohlc().dropna()
        o = o.loc[(o.index >= f) & (o.index <= t)]
        rows = []
        for ts, r in o.iterrows():
            bt = int((ts + pd.Timedelta(hours=self.offset)).timestamp())
            rows.append((bt, r['open'], r['high'], r['low'], r['close'], 0, 0, 0))
        return np.array(rows, dtype=[('time', 'i8'), ('open', 'f8'), ('high', 'f8'),
                                     ('low', 'f8'), ('close', 'f8'), ('tick_volume', 'i8'),
                                     ('spread', 'i8'), ('real_volume', 'i8')])

    def _rates_from_pos(self, timeframe, count):
        import pandas as pd
        if self._bars is None or self.cur is None:
            return self._rates(timeframe, pd.Timestamp('1970-01-01'), pd.Timestamp('1970-01-01'))
        end = pd.Timestamp(self.cur.time_utc)
        start = end - pd.Timedelta(minutes=count + 2)
        allbars = self._rates(timeframe, (start + pd.Timedelta(hours=self.offset)).tz_localize(None),
                              (end + pd.Timedelta(hours=self.offset)).tz_localize(None))
        return allbars[-count:] if len(allbars) else allbars

    def _ticks_range(self, dt_from, dt_to):
        import numpy as np
        return np.array([])

    # -- unrealized total (for equity) ------------------------------------------
    def unrealized(self):
        return round(sum(self._pos_value(p) for p in self.positions.values()), 2)


class FakeMT5:
    """The raw MetaTrader5-handle shim. Every method the live code reaches through
    MT5Adapter.self.mt5 lives here, delegating to a FakeBroker. Constants are set
    as attributes below so `mt5.ORDER_TYPE_BUY_STOP` etc. resolve."""

    def __init__(self, broker: FakeBroker):
        self.b = broker
        # expose the constant surface as attributes
        for k, v in globals().items():
            if k.isupper() and isinstance(v, int):
                setattr(self, k, v)

    # lifecycle
    def initialize(self, *a, **k):
        return True
    def shutdown(self, *a, **k):
        return True
    def last_error(self):
        return (0, "ok")

    # account
    def account_info(self):
        b = self.b
        eq = round(b.balance + b.unrealized(), 2)
        return _Obj(login=999999, balance=round(b.balance, 2), equity=eq, margin=0.0,
                    margin_free=eq, currency="USD", leverage=100, server="SimServer-Demo",
                    trade_mode=ACCOUNT_TRADE_MODE_DEMO)

    # symbol / tick
    def symbol_info(self, symbol=None):
        return _Obj(name=symbol or self.b.symbol, digits=2, point=0.01,
                    trade_stops_level=0, trade_freeze_level=0, volume_min=0.01,
                    volume_max=100.0, volume_step=0.01, bid=self.b.cur.bid if self.b.cur else 0.0,
                    ask=self.b.cur.ask if self.b.cur else 0.0, visible=True, filling_mode=2,
                    trade_mode=SYMBOL_TRADE_MODE_FULL, spread=int(self.b.spread * 100))

    def symbol_select(self, symbol, enable=True):
        return True

    def terminal_info(self):
        return _Obj(connected=True, trade_allowed=True, community_account=False)

    def symbol_info_tick(self, symbol=None):
        c = self.b.cur
        if c is None:
            return None
        return _Obj(time=self.b._broker_epoch(), time_msc=self.b._broker_epoch() * 1000,
                    bid=round(c.bid, 2), ask=round(c.ask, 2), last=round((c.bid + c.ask) / 2, 2),
                    volume=1, flags=6, volume_real=1.0)

    # positions / orders / history
    def positions_get(self, ticket=None, symbol=None, group=None):
        vals = list(self.b.positions.values())
        if ticket is not None:
            vals = [p for p in vals if p.ticket == int(ticket)]
        if symbol is not None:
            vals = [p for p in vals if p.symbol == symbol]
        return tuple(vals)

    def positions_total(self):
        return len(self.b.positions)

    def orders_get(self, ticket=None, symbol=None, group=None):
        vals = list(self.b.pendings.values())
        if ticket is not None:
            vals = [o for o in vals if o.ticket == int(ticket)]
        if symbol is not None:
            vals = [o for o in vals if o.symbol == symbol]
        return tuple(vals)

    def orders_total(self):
        return len(self.b.pendings)

    def history_deals_get(self, *a, position=None, **k):
        if position is not None:
            return tuple(d for d in self.b.deals if d.position_id == int(position))
        if len(a) >= 2:
            import pandas as pd
            def ep(x):
                if isinstance(x, (int, float)):
                    return float(x)
                return pd.Timestamp(x).timestamp()
            f, t = ep(a[0]), ep(a[1])
            return tuple(d for d in self.b.deals if f <= d.time < t)
        return tuple(self.b.deals)

    def history_orders_get(self, *a, **k):
        return tuple()

    # M1/M5 bars from the day's tick frame (for get_m5_close anchor pricing)
    def copy_rates_range(self, symbol, timeframe, dt_from, dt_to):
        return self.b._rates(timeframe, dt_from, dt_to)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        return self.b._rates_from_pos(timeframe, count)

    def copy_ticks_range(self, symbol, dt_from, dt_to, flags):
        return self.b._ticks_range(dt_from, dt_to)

    # the ONE mutation entrypoint the adapter uses
    def order_send(self, req):
        return self.b.order_send(req)
