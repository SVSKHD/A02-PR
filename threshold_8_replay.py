"""threshold_8 — the replay engine (the ONE bar-iteration loop).

This is the single place that iterates bars. The backtest drives ``ReplayEngine.run``
and never loops bars itself (check C15). Responsibilities:

  * iterate M1 bars (the only ``for bar in bars`` loop in the subsystem)
  * aggregate M1 -> CLOSED M5 bars and attach the ATR + swing features that the
    detectors consume (the feature-builder role; detectors get CLOSED M5 only, C12)
  * resolve fills intrabar on M1, gap-aware, and count UNPLACEABLE events (C2/C3)
  * enforce no-same-bar management: a leg opened on bar N is managed from N+1; only
    its own SL is checked intrabar on N (C4)
  * orchestrate rescue -> trail -> the single exit-precedence function (Module D)
  * record every fill so the backtest can post-verify low <= price <= high (C2)

It touches no broker and imports no MetaTrader5. In production the MT5 boundary stays
in ``mt5_client.py``; here order placement is simulated in-process.
"""

from datetime import timedelta

from threshold_8_config import _parse_hhmm
from threshold_8_basket import (
    Threshold8Basket, Threshold8Leg, BUY, SELL, ROLE_ENTRY, ROLE_RESCUE,
    STATE_RESCUED, side_sign,
)
from threshold_8_entry import Threshold8EntryEngine
from threshold_8_rescue import Threshold8RescueManager
from threshold_8_trail import Threshold8TrailEngine
from threshold_8_exits import evaluate_exit_precedence, EXIT_OPPOSITE_ANCHOR, EXIT_DAILY_RISK


class Bar:
    """One M1 bar. spread is in POINTS (broker convention); spread_$ = spread*point."""
    __slots__ = ("dt", "open", "high", "low", "close", "tickvol", "vol", "spread")

    def __init__(self, dt, o, h, l, c, tickvol, vol, spread):
        self.dt = dt
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.tickvol = tickvol
        self.vol = vol
        self.spread = spread


class FillRecord:
    __slots__ = ("dt", "low", "high", "price", "kind", "basket_id")

    def __init__(self, dt, low, high, price, kind, basket_id):
        self.dt = dt
        self.low = low
        self.high = high
        self.price = price
        self.kind = kind
        self.basket_id = basket_id


class _M5Accum:
    """Accumulates M1 bars into an M5 window."""
    __slots__ = ("start", "open", "high", "low", "close")

    def __init__(self, start, bar):
        self.start = start
        self.open = bar.open
        self.high = bar.high
        self.low = bar.low
        self.close = bar.close

    def add(self, bar):
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.close = bar.close


def _m5_start(dt, m5_minutes):
    minute = (dt.minute // m5_minutes) * m5_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


class ReplayEngine:
    def __init__(self, params, symbol, base_magic=0):
        from threshold_8_config import threshold_8_magic
        self.p = params
        self.symbol = symbol
        self.magic = threshold_8_magic(symbol, base_magic)
        self.entry = Threshold8EntryEngine(params)
        self.rescue = Threshold8RescueManager(params)
        self.trail = Threshold8TrailEngine(params)

        self.baskets = []          # every basket ever opened
        self.active_basket = None
        self._ticket_seq = 0
        self._basket_seq = 0

        # features (closed M5)
        self.m5_closed = []        # list of dicts: start, o,h,l,c
        self.m5_atr = []           # ATR aligned to m5_closed
        self._last_swing_high = None
        self._last_swing_low = None

        # per-day state
        self._cur_date = None
        self._day_baskets = []
        self._day_halted = False

        # diagnostics
        self.fills = []
        self.unplaceable_count = 0
        self.unplaceable_detail = []
        self.exit_reason_counts = {}
        self.spread_prices_used = []
        self.days_hit_daily_risk = set()
        # C10: count any bar where a TRAILING basket sits below its ladder floor without
        # exiting (structurally impossible — every bar is evaluated). Must stay 0.
        self.trail_floor_violations = 0
        self._m5_accum = None
        self._m5_key = None

        fh, fm = _parse_hhmm(params.flatten_server_hhmm)
        self._flat_h, self._flat_m = fh, fm
        self._anchor_h, self._anchor_m = _parse_hhmm(params.anchor_hhmm)

    # --- ids -----------------------------------------------------------------------
    def _next_ticket(self):
        self._ticket_seq += 1
        return self.magic * 1_000_000 + self._ticket_seq

    def _next_basket_id(self):
        self._basket_seq += 1
        return "%s-%s-%d" % (self.p.version, self.symbol, self._basket_seq)

    # --- features (feature-builder role; CLOSED M5 only) ---------------------------
    def _close_m5(self, accum):
        rec = {"start": accum.start, "open": accum.open, "high": accum.high,
               "low": accum.low, "close": accum.close}
        self.m5_closed.append(rec)
        self._update_atr()
        self._update_swings()

    def _update_atr(self):
        n = self.p.atr_period
        bars = self.m5_closed
        if len(bars) < 2:
            self.m5_atr.append(None)
            return
        trs = []
        for i in range(max(1, len(bars) - n), len(bars)):
            h = bars[i]["high"]
            l = bars[i]["low"]
            pc = bars[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < 1:
            self.m5_atr.append(None)
        else:
            self.m5_atr.append(sum(trs) / len(trs))

    def _update_swings(self):
        """Causal fractal swing confirmation. A pivot at index i is confirmed once
        SWING_LOOKBACK bars exist on both sides. We only ever confirm past pivots, so
        this is look-ahead free."""
        k = self.p.swing_lookback
        bars = self.m5_closed
        i = len(bars) - 1 - k          # candidate pivot index
        if i - k < 0:
            return
        window_hi = bars[i]["high"]
        window_lo = bars[i]["low"]
        is_high = all(bars[j]["high"] <= window_hi for j in range(i - k, i + k + 1) if j != i)
        is_low = all(bars[j]["low"] >= window_lo for j in range(i - k, i + k + 1) if j != i)
        if is_high:
            self._last_swing_high = window_hi
        if is_low:
            self._last_swing_low = window_lo

    def _latest_atr(self):
        for a in reversed(self.m5_atr):
            if a is not None:
                return a
        return None

    def _opposite_swing(self, dominant_side):
        # opposite swing for a long is the last swing low; for a short, last swing high
        if dominant_side == BUY:
            return self._last_swing_low
        return self._last_swing_high

    # --- fill helpers (gap-aware; INVARIANT 2) -------------------------------------
    @staticmethod
    def _fill_buy_stop(level, bar):
        if bar.high < level:
            return None
        return bar.open if bar.open > level else level

    @staticmethod
    def _fill_sell_stop(level, bar):
        if bar.low > level:
            return None
        return bar.open if bar.open < level else level

    @staticmethod
    def _fill_buy_sl(sl, bar):
        # long leg stop is below; hit when low <= sl
        if bar.low > sl:
            return None
        return bar.open if bar.open < sl else sl

    @staticmethod
    def _fill_sell_sl(sl, bar):
        # short leg stop is above; hit when high >= sl
        if bar.high < sl:
            return None
        return bar.open if bar.open > sl else sl

    def _record_fill(self, bar, price, kind, basket_id):
        self.fills.append(FillRecord(bar.dt, bar.low, bar.high, price, kind, basket_id))

    def _spread_price(self, bar):
        sp = bar.spread * self.p.point
        self.spread_prices_used.append(sp)
        return sp

    # --- leg lifecycle -------------------------------------------------------------
    def _open_leg(self, basket, side, lot, fill_price, role, bar, bar_index):
        sl = fill_price - self.p.sl if side == BUY else fill_price + self.p.sl
        leg = Threshold8Leg(self._next_ticket(), side, lot, fill_price, sl, role,
                            bar.dt, bar_index)
        leg._entry_spread_price = self._spread_price(bar)
        basket.add_leg(leg)
        self._record_fill(bar, fill_price, "OPEN_%s_%s" % (role, side), basket.basket_id)
        return leg

    def _close_leg(self, leg, price, bar, reason):
        """Close a leg, folding a full round-turn spread cost into the exit price so
        INVARIANT 5's identity stays exact while costs are real (never spread=0)."""
        sp = getattr(leg, "_entry_spread_price", 0.0)
        eff = price - sp if leg.side == BUY else price + sp
        leg.close(eff, bar.dt, reason)
        self._record_fill(bar, price, "CLOSE_%s" % reason, None)

    # --- per-bar leg SL (intrabar, allowed on the open bar N) -----------------------
    def _check_leg_sls(self, basket, bar):
        for leg in list(basket.open_legs()):
            if leg.side == BUY:
                fp = self._fill_buy_sl(leg.sl, bar)
            else:
                fp = self._fill_sell_sl(leg.sl, bar)
            if fp is not None:
                self._close_leg(leg, fp, bar, "LEG_SL")

    # --- main loop -----------------------------------------------------------------
    def run(self, bars):
        """Drive the whole subsystem across ``bars`` (an iterable of M1 Bar). THE only
        bar loop. Returns the list of basket records (dicts) for the CSV."""
        bar_index = -1
        last_bar = None
        for bar in bars:                       # <-- single bar-iteration loop
            bar_index += 1
            last_bar = bar
            self._on_new_day_if_needed(bar)
            self._maybe_close_m5(bar)

            # 1) leg-local SL, intrabar, every open basket (allowed on open bar N)
            if self.active_basket is not None:
                self._check_leg_sls(self.active_basket, bar)

            # 2) entry straddle fills (only when flat and the day isn't halted)
            if self.active_basket is None and not self._day_halted \
                    and self.entry.has_live_straddle():
                sig = self.entry.check_fill(bar)
                if sig is not None:
                    self._open_basket_from_signal(sig, bar, bar_index)
                    # INVARIANT 4 exception: a leg opened on bar N still has its OWN SL
                    # checked intrabar on bar N using the bar extremes.
                    self._check_leg_sls(self.active_basket, bar)

            # 3) mark exposure for the active basket at this bar's close
            b = self.active_basket
            if b is not None and not b.is_closed():
                b.update(bar.close)

                # 4) basket-level management only from bar N+1 (INVARIANT 4)
                if bar_index > b.opened_bar_index:
                    self._manage_basket(b, bar, bar_index)

        # final flatten of anything still open at end of data (marked to last close)
        if last_bar is not None and self.active_basket is not None \
                and not self.active_basket.is_closed():
            self._flatten_basket(self.active_basket, last_bar, "END_OF_DATA")
            self.active_basket = None
        return self._basket_records()

    # --- day / session -------------------------------------------------------------
    def _on_new_day_if_needed(self, bar):
        d = bar.dt.date()
        if d != self._cur_date:
            self._cur_date = d
            self._day_baskets = []
            self._day_halted = False
            self.entry.reset()

    def _session_flatten(self, bar):
        return (bar.dt.hour, bar.dt.minute) >= (self._flat_h, self._flat_m)

    def _is_friday(self, bar):
        return bar.dt.weekday() == 4

    # --- M5 rollover + anchor arming ------------------------------------------------
    def _maybe_close_m5(self, bar):
        self._last_close = bar.close
        key = _m5_start(bar.dt, self.p.m5_minutes)
        if self._m5_key is None:
            self._m5_accum = _M5Accum(key, bar)
            self._m5_key = key
            return
        if key != self._m5_key:
            # previous M5 window is now CLOSED
            closed = self._m5_accum
            self._close_m5(closed)
            self._maybe_arm_anchor(closed)
            # start new window
            self._m5_accum = _M5Accum(key, bar)
            self._m5_key = key
        else:
            self._m5_accum.add(bar)

    def _maybe_arm_anchor(self, closed_m5):
        if (closed_m5.start.hour, closed_m5.start.minute) == (self._anchor_h, self._anchor_m):
            self.entry.arm_anchor(closed_m5.open, closed_m5.start)
            unp = self.entry.mark_unplaceable_from_anchor_bar(closed_m5.high, closed_m5.low)
            for side in unp:
                self.unplaceable_count += 1
                self.unplaceable_detail.append(
                    {"dt": closed_m5.start, "side": side, "kind": "ANCHOR_STOP",
                     "level": self.entry.buy_level if side == BUY else self.entry.sell_level})

    # --- basket open ---------------------------------------------------------------
    def _open_basket_from_signal(self, sig, bar, bar_index):
        # gap-aware fill at the level
        if sig.side == BUY:
            fp = self._fill_buy_stop(sig.level, bar)
        else:
            fp = self._fill_sell_stop(sig.level, bar)
        if fp is None:
            # for a re-entry we force the level (it was crossed this bar); guard anyway
            fp = sig.level
        basket = Threshold8Basket(self._next_basket_id(), self.symbol, sig.anchor_id,
                                  sig.anchor_time, self.magic, bar.dt)
        basket.opened_bar_index = bar_index
        basket.entry_price_recorded = fp
        basket.entry_side = sig.side
        self._open_leg(basket, sig.side, self.p.base_lot, fp, ROLE_ENTRY, bar, bar_index)
        basket.update(bar.close)
        self.baskets.append(basket)
        self._day_baskets.append(basket)
        self.active_basket = basket

    # --- basket management (from bar N+1) ------------------------------------------
    def _manage_basket(self, basket, bar, bar_index):
        p = self.p

        # a) rescue (evaluated on the ENTRY leg, per M1 bar)
        if basket.entry_leg() is not None and basket.entry_leg().is_open:
            dec = self.rescue.evaluate(basket, bar, bar.dt)
            if dec.armed:
                leg = self._open_leg(basket, dec.order.side, dec.order.lot, bar.close,
                                     ROLE_RESCUE, bar, bar_index)
                basket.rescue_price = bar.close
                basket.rescue_lot = dec.order.lot
                if basket.state not in ("TRAILING",):
                    basket.state = STATE_RESCUED
                # INVARIANT 4 exception: the rescue leg opened this bar still has its
                # own SL checked intrabar on this bar using the bar extremes.
                self._check_leg_sls(basket, bar)
                basket.update(bar.close)

        # b) features for the trail (consumed, not computed here)
        atr = self._latest_atr()
        swing = self._opposite_swing(basket.dominant_side)
        self.trail.update(basket, bar.close, atr, swing)

        # C10 guard: a basket that reached a ladder rung can never sit below that rung's
        # lock without the trail-exit firing on the same bar (gap in net or not).
        if basket.trail_locked is not None \
                and basket.net_pnl < basket.trail_locked - 1e-9 \
                and not getattr(basket, "_trail_exit", False):
            self.trail_floor_violations += 1

        # c) opposite-anchor detection (exit precedence rule 7)
        opp = self.entry.opposite_triggered(bar)

        # d) day net incl. floating
        day_net = sum(x.net_pnl for x in self._day_baskets)
        session_flat = self._session_flatten(bar)
        dur = basket.duration_min(bar.dt)

        decision = evaluate_exit_precedence(
            basket, p, bar.dt, day_net=day_net, session_flatten=session_flat,
            is_friday=self._is_friday(bar), opposite_anchor_triggered=opp,
            duration_min=dur,
        )
        if decision is None:
            return

        self.exit_reason_counts[decision.reason] = \
            self.exit_reason_counts.get(decision.reason, 0) + 1

        if decision.scope == "day":
            self.days_hit_daily_risk.add(bar.dt.date())
            # flatten every open basket today, halt the day
            for x in list(self._day_baskets):
                if not x.is_closed():
                    self._flatten_basket(x, bar, decision.reason)
            self._day_halted = True
            self.active_basket = None
            return

        # basket scope
        if decision.reason == EXIT_OPPOSITE_ANCHOR:
            level = self.entry.opposite_level()
            self._flatten_basket(basket, bar, decision.reason, at_price=level)
            self.active_basket = None
            # entry engine may open a new one (flipped)
            re = self.entry.reentry_signal()
            if re is not None and not self._day_halted:
                self._open_basket_from_signal(re, bar, bar_index)
        else:
            self._flatten_basket(basket, bar, decision.reason)
            self.active_basket = None

    def _flatten_basket(self, basket, bar, reason, at_price=None):
        px = bar.close if at_price is None else at_price
        # fold spread into each leg via _close_leg, then finalize
        for leg in list(basket.open_legs()):
            self._close_leg(leg, px, bar, reason)
        basket.update(px)
        from threshold_8_basket import STATE_CLOSED
        basket.state = STATE_CLOSED
        basket.close_reason = reason
        basket.closed_at = bar.dt

    # --- output --------------------------------------------------------------------
    def _basket_records(self):
        rows = []
        for b in self.baskets:
            entry = b.entry_leg()
            rescued = len(b.rescue_legs()) > 0
            rows.append({
                "basket_id": b.basket_id,
                "anchor_id": b.anchor_id,
                "anchor_time": b.anchor_time.isoformat(),
                "symbol": b.symbol,
                "trigger_dist": self.p.trigger_dist,
                "entry_side": getattr(b, "entry_side", entry.side if entry else ""),
                "entry_price": round(getattr(b, "entry_price_recorded",
                                             entry.entry_price if entry else 0.0), 5),
                "rescued": rescued,
                "rescue_price": round(getattr(b, "rescue_price", 0.0) or 0.0, 5),
                "rescue_lot": getattr(b, "rescue_lot", 0.0) or 0.0,
                "max_floating_loss": round(b.max_floating_loss, 2),
                "max_floating_profit": round(b.max_floating_profit, 2),
                "peak_net": round(b.peak_net, 2),
                "trail_engaged": b.trail_engaged,
                "trail_rung_reached": b.trail_rung_reached,
                "exit_reason": getattr(b, "close_reason", "OPEN_AT_END"),
                "net_pnl": round(b.net_pnl, 2),
                "duration_min": round(b.duration_min(getattr(b, "closed_at", b.opened_at)), 2),
                "unplaceable": False,
            })
        return rows
