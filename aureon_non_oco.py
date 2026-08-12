"""AUREON — `aureon_new_non_oco` engine (observation → confirmation → ladder → chain).

A SEPARATE, self-contained entry/exit mechanism gated behind the single feature flag
`cfg.aureon_new_non_oco` (DEFAULT OFF). Modelled on ROGUE / FETCHER: it has its OWN
magic (`AURNO_MAGIC`), its own per-day state, its own position management, and it
NEVER touches an anchor (20260522) / rogue (20260626) / fetcher leg — nor they it.

    Flag OFF  -> `drive()` is an immediate no-op; the whole bot is byte-identical.
    Flag ON   -> runs IN PARALLEL with the existing engines (additive; it does NOT
                 replace or merge with the blind non-OCO straddle). For each label in
                 `cfg.anc_anchors` (default A2, A5) it:
                   1. captures the anchor price and forms two observation levels at
                      anchor ± `anc_threshold` (15),
                   2. on a TOUCH of either level opens an observation window (no order),
                   3. watches CLOSED M1 candles — `anc_n_candles` (3) consecutive
                      same-direction closes = the signal (3 up -> long, 3 down -> short;
                      a doji breaks the run; direction comes from the candles, NOT the
                      level touched),
                   4. enters at market and rides the exit ladder (SL 18 -> +2.5 at +3 ->
                      +10 secured at +10, then trail 1.5 behind the peak; flat 23:30),
                   5. CHAINS: after a close, observation reopens AT THE EXIT PRICE (no new
                      touch); up to `anc_max_chain` (5) trades/anchor/day, ending on a
                      losing link, with a 200-EMA trend filter on links >= 1 (link 0, the
                      first trade off the touch, is NOT filtered).

The DECISION core (`AnchorDaySession` + the pure helpers) is I/O-free and is driven
byte-for-byte by the backtest, by paper mode, and by the unit tests. The live driver
(`drive`) is the only part that talks to MT5, reusing the existing adapter helpers
(`place_market_order` / `modify_position_sl` / `close_position`) and the existing
`self.tele` notifier — no new broker/notifier/config-loader code.

See `tests/test_aureon_non_oco.py`, `aureon_non_oco_backtest.py`, and the README
section "AUREON NEW NON-OCO" for the mechanism, the tests, and the reference numbers.
"""
import logging
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger("AUREON")

# Distinct from anchor (20260522) / rogue (20260626) / fetcher / warmup (9999998).
AURNO_MAGIC = 20260811
ALERT = "🟪 *NON-OCO*"

# state machine states
WAIT_TOUCH = "WAIT_TOUCH"      # link 0 only: waiting for a level touch to open the window
OBSERVE = "OBSERVE"            # window open: counting consecutive same-dir closed M1 candles
IN_POS = "IN_POS"             # a position is open; riding the ladder
DONE = "DONE"                # anchor finished for the day (expired / capped / stop-on-loss / EOD)


# ---------------------------------------------------------------------------
# pure helpers (no I/O, unit-tested directly)
# ---------------------------------------------------------------------------
def candle_dir(open_, close_):
    """+1 up (close>open), -1 down (close<open), 0 doji (close==open)."""
    if close_ > open_:
        return 1
    if close_ < open_:
        return -1
    return 0


def ladder_sl_offset(peak_fav, p):
    """The TARGET stop offset from entry (profit-positive) for a given peak-favourable
    excursion, per the exit ladder. The caller ratchets it monotonically (max), so it
    can only ever move the stop toward profit, never backwards.

      peak >= target (10)   -> secure at +target, then trail `trail_dist` behind peak
      peak >= lock_at (3)   -> floor at +lock_to (2.5)
      else                  -> the initial stop, -sl (i.e. entry - 18)
    """
    if peak_fav >= p.target:
        return max(p.target, peak_fav - p.trail_dist)
    if peak_fav >= p.lock_at:
        return p.lock_to
    return -p.sl


def ema(closes, period):
    """SMA-seeded EMA over `closes` (oldest first); None if fewer than `period` values.
    The same function backs the live filter (over the last fetched M1 closes) and the
    backtest, so both compute the EMA identically given identical inputs."""
    if closes is None:
        return None
    vals = [float(c) for c in closes]
    if len(vals) < period or period <= 0:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(vals[:period]) / period          # SMA seed
    for c in vals[period:]:
        e = c * k + e * (1.0 - k)
    return e


def _bv(bar, key):
    """Read an OHLC field from a dict or an object (numpy record / namedtuple / Series)."""
    if isinstance(bar, dict):
        return float(bar[key])
    return float(bar[key]) if hasattr(bar, "__getitem__") and not hasattr(bar, key) \
        else float(getattr(bar, key))


def _minutes(now, start):
    """Whole/fractional minutes between two pandas Timestamps (0 if start is None)."""
    if start is None or now is None:
        return 0.0
    try:
        return (now - start).total_seconds() / 60.0
    except Exception:
        return 0.0


@dataclass
class AncParams:
    threshold: float = 15.0
    n_candles: int = 3
    obs_expiry_min: float = 60.0
    sl: float = 18.0
    lock_at: float = 3.0
    lock_to: float = 2.5
    target: float = 10.0
    trail_dist: float = 1.5
    max_chain: int = 5
    chain_stop_on_loss: bool = True
    chain_trend: str = "ema"          # "none" | "ema"
    ema_period: int = 200
    lot: float = 0.10
    daily_target_pct: float = 0.0
    drift_min: float = 45.0
    flat_broker_hour: float = 23.5

    @classmethod
    def from_cfg(cls, cfg):
        g = lambda k, d: getattr(cfg, k, d)
        return cls(
            threshold=float(g("anc_threshold", 15.0)),
            n_candles=int(g("anc_n_candles", 3)),
            obs_expiry_min=float(g("anc_obs_expiry_min", 60.0)),
            sl=float(g("anc_sl", 18.0)),
            lock_at=float(g("anc_lock_at", 3.0)),
            lock_to=float(g("anc_lock_to", 2.5)),
            target=float(g("anc_target", 10.0)),
            trail_dist=float(g("anc_trail_dist", 1.5)),
            max_chain=int(g("anc_max_chain", 5)),
            chain_stop_on_loss=bool(g("anc_chain_stop_on_loss", True)),
            chain_trend=str(g("anc_chain_trend", "ema")),
            ema_period=int(g("anc_ema_period", 200)),
            lot=float(g("anc_lot", 0.10)),
            daily_target_pct=float(g("anc_daily_target_pct", 0.0)),
            drift_min=float(g("anc_drift_min", 45.0)),
            flat_broker_hour=float(g("anc_flat_broker_hour", 23.5)),
        )


# ---------------------------------------------------------------------------
# the decision core — one anchor, one day
# ---------------------------------------------------------------------------
class AnchorDaySession:
    """Pure state machine for a single anchor on a single day. It owns the whole
    observation -> confirmation -> ladder -> chain lifecycle and emits nothing but
    decisions; it never talks to a broker. Two ways to drive it, sharing the SAME
    internal primitives so there is exactly one behavioural truth:

      * BACKTEST / PAPER: `on_m1_close(bar, now, ema_value)` — the session simulates
        fills against the bar OHLC (entry at the NEXT bar's open, stop-outs against the
        stop that was in force at the start of the bar) and advances the chain itself.

      * LIVE: `poll_setup()` -> `enter_live()` -> `manage_live()` -> `on_exit()` — the
        broker owns the fill/stop; the driver feeds the realised exit back in.
    """

    def __init__(self, anchor_label, anchor_price, params, flat_ts=None):
        self.label = anchor_label
        self.anchor_price = float(anchor_price)
        self.p = params
        self.flat_ts = flat_ts
        self.upper = round(self.anchor_price + params.threshold, 2)
        self.lower = round(self.anchor_price - params.threshold, 2)

        self.state = WAIT_TOUCH
        self.obs_start = None
        self.run_dir = 0
        self.run_len = 0
        self.reopen_price = None          # exit price a chained window reopened at

        self.trades_done = 0              # entries taken so far (link index = this at entry)
        self.chain_ended = False
        self.done = False

        self.pos = None                   # open position dict, or None
        self.pending_side = None          # backtest: confirmed, enter at next bar's open
        self.open_ticket = None           # live: broker ticket of the open position
        self.trades = []                  # closed-trade records (dicts)

    # ---- shared primitives -------------------------------------------------
    def _open_observation(self, now, reopen_price=None):
        self.state = OBSERVE
        self.obs_start = now
        self.run_dir = 0
        self.run_len = 0
        self.reopen_price = reopen_price

    def _advance_setup(self, bar, now, ema_value):
        """WAIT_TOUCH + OBSERVE. Returns a confirmed side ('BUY'/'SELL') or None.
        May flip the session to DONE on observation-window expiry."""
        if self.state == WAIT_TOUCH:
            if _bv(bar, "high") >= self.upper or _bv(bar, "low") <= self.lower:
                self._open_observation(now)   # touch opens the window; count from next candle
            return None

        if self.state == OBSERVE:
            if _minutes(now, self.obs_start) > self.p.obs_expiry_min:
                self.chain_ended = True
                self.state = DONE
                self.done = True
                return None
            d = candle_dir(_bv(bar, "open"), _bv(bar, "close"))
            if d == 0:                        # doji breaks the run
                self.run_dir = 0
                self.run_len = 0
                return None
            if d == self.run_dir:
                self.run_len += 1
            else:
                self.run_dir = d
                self.run_len = 1
            if self.run_len >= self.p.n_candles:
                side = "BUY" if self.run_dir > 0 else "SELL"
                # EMA trend filter — chained links (>=1) only; link 0 is NEVER filtered.
                if (self.trades_done >= 1 and self.p.chain_trend == "ema"
                        and ema_value is not None):
                    close_ = _bv(bar, "close")
                    aligned = ((side == "BUY" and close_ > ema_value)
                               or (side == "SELL" and close_ < ema_value))
                    if not aligned:
                        self.run_dir = 0      # reject against-trend; need a fresh run
                        self.run_len = 0
                        return None
                self.run_dir = 0
                self.run_len = 0
                return side
        return None

    def _start_position(self, side, entry, now):
        entry = round(float(entry), 2)
        sl_off = -self.p.sl
        sl_price = entry + sl_off if side == "BUY" else entry - sl_off
        self.pos = {
            "side": side, "entry": entry, "entry_time": now,
            "peak_fav": 0.0, "sl_off": sl_off, "sl_price": round(sl_price, 2),
            "link": self.trades_done,
        }
        self.trades_done += 1
        self.state = IN_POS
        return self.pos

    def _sl_hit(self, bar):
        if self.pos["side"] == "BUY":
            return _bv(bar, "low") <= self.pos["sl_price"]
        return _bv(bar, "high") >= self.pos["sl_price"]

    def _ladder(self, bar):
        """Update the peak from this bar and ratchet the stop. Returns the new stop
        price if it advanced (toward profit), else None. Never moves backwards."""
        pos = self.pos
        entry = pos["entry"]
        if pos["side"] == "BUY":
            fav = _bv(bar, "high") - entry
        else:
            fav = entry - _bv(bar, "low")
        pos["peak_fav"] = max(pos["peak_fav"], fav)
        new_off = max(pos["sl_off"], ladder_sl_offset(pos["peak_fav"], self.p))
        if new_off > pos["sl_off"] + 1e-9:
            pos["sl_off"] = new_off
            pos["sl_price"] = round(entry + new_off if pos["side"] == "BUY"
                                    else entry - new_off, 2)
            return pos["sl_price"]
        return None

    def _classify(self, reason):
        """Map an exit to one of the four report buckets: stop / lock / target / EOD."""
        if reason == "EOD":
            return "EOD"
        off = self.pos["sl_off"]
        if off >= self.p.target - 1e-9:
            return "target"
        if off >= self.p.lock_to - 1e-9:
            return "lock"
        return "stop"

    def _close_and_chain(self, exit_price, now, reason, force_done=False):
        pos = self.pos
        entry = pos["entry"]
        exit_price = round(float(exit_price), 2)
        pnl = (exit_price - entry) if pos["side"] == "BUY" else (entry - exit_price)
        loss = pnl < -1e-9
        rec = {
            "kind": "EXIT",
            "label": self.label, "side": pos["side"], "link": pos["link"],
            "entry": entry, "exit": exit_price, "pnl_price": round(pnl, 4),
            "peak_fav": round(pos["peak_fav"], 4), "reason": self._classify(reason),
            "entry_time": pos["entry_time"], "exit_time": now,
        }
        self.trades.append(rec)
        self.pos = None
        self.open_ticket = None
        if force_done or self.trades_done >= self.p.max_chain:
            self.state = DONE
            self.done = True
        elif loss and self.p.chain_stop_on_loss:
            self.chain_ended = True
            self.state = DONE
            self.done = True
        else:
            self._open_observation(now, reopen_price=exit_price)  # chain: reopen at exit price
        return rec

    # ---- BACKTEST / PAPER driver ------------------------------------------
    def on_m1_close(self, bar, now, ema_value=None):
        """Drive the session with one CLOSED M1 candle. Returns a list of event dicts
        (ENTER / MODIFY_SL / EXIT) for logging. Simulates fills internally."""
        events = []
        if self.done:
            return events

        # Flat everything at the EOD cutoff (23:30 server).
        if self.flat_ts is not None and now >= self.flat_ts:
            if self.pos is not None:
                events.append(self._close_and_chain(_bv(bar, "close"), now, "EOD",
                                                    force_done=True))
            self.state = DONE
            self.done = True
            return events

        # Flat: take a pending (confirmed) entry at THIS bar's open, else advance setup.
        if self.pos is None:
            if self.pending_side is not None:
                side = self.pending_side
                self.pending_side = None
                self._start_position(side, _bv(bar, "open"), now)
                events.append({"kind": "ENTER", "side": side,
                               "price": self.pos["entry"], "sl": self.pos["sl_price"],
                               "link": self.pos["link"], "label": self.label})
                # fall through to manage this same (entry) bar
            else:
                side = self._advance_setup(bar, now, ema_value)
                if side is not None:
                    self.pending_side = side   # enter on the NEXT bar's open
                return events

        # Manage the open position. Test the stop that was in force at bar start FIRST
        # (conservative: the ladder cannot trail up within the same spike that stops us).
        if self.pos is not None:
            if self._sl_hit(bar):
                events.append(self._close_and_chain(self.pos["sl_price"], now, "SL"))
                return events
            new_sl = self._ladder(bar)
            if new_sl is not None:
                events.append({"kind": "MODIFY_SL", "sl": new_sl, "label": self.label})
        return events

    # ---- LIVE driver primitives -------------------------------------------
    def poll_setup(self, bar, now, ema_value=None):
        """LIVE: advance WAIT_TOUCH/OBSERVE on a closed bar; return a confirmed side to
        enter at market NOW, or None. No pending/next-open (live enters immediately)."""
        if self.done or self.pos is not None:
            return None
        if self.flat_ts is not None and now >= self.flat_ts:
            self.state = DONE
            self.done = True
            return None
        return self._advance_setup(bar, now, ema_value)

    def enter_live(self, side, fill_price, now):
        return self._start_position(side, fill_price, now)

    def manage_live(self, bar, now):
        """LIVE: ratchet the ladder on a closed bar; return a new stop price to push to
        the broker, or None. The broker owns the actual stop-out."""
        if self.pos is None:
            return None
        return self._ladder(bar)

    def on_exit(self, exit_price, now, reason="SL"):
        """LIVE: the broker closed the position — record it and advance the chain."""
        if self.pos is None:
            return None
        return self._close_and_chain(exit_price, now, reason)


# ---------------------------------------------------------------------------
# LIVE driver — the only part that touches MT5 / telemetry
# ---------------------------------------------------------------------------
def _tele(trader, level, msg):
    try:
        getattr(trader.tele, level)(f"{ALERT} {msg}")
    except Exception:
        pass


def _mid(trader):
    try:
        tk = trader.adapter.mt5.symbol_info_tick(trader.cfg.symbol)
        return (float(tk.bid) + float(tk.ask)) / 2.0
    except Exception:
        return None


def _flat_ts(trader, bdate, p):
    fh = p.flat_broker_hour
    hh = int(fh)
    mm = int(round((fh - hh) * 60))
    try:
        return trader._anchor_datetime_utc(bdate, hh, trader.cfg.broker_tz_offset_hours, mm)
    except Exception:
        return None


def _live_ema(trader, p):
    if p.chain_trend != "ema":
        return None
    try:
        bars = trader.adapter.get_latest_m1(trader.cfg.symbol, p.ema_period * 3 + 1)
        if bars is None or len(bars) < p.ema_period + 1:
            return None
        closes = [float(b["close"]) for b in bars[:-1]]   # closed bars only
        return ema(closes, p.ema_period)
    except Exception:
        return None


def _closed_m1(trader):
    try:
        bars = trader.adapter.get_latest_m1(trader.cfg.symbol, 2)
        if bars is None or len(bars) < 2:
            return None
        b = bars[-2]
        return {"open": float(b["open"]), "high": float(b["high"]),
                "low": float(b["low"]), "close": float(b["close"])}
    except Exception:
        return None


def _ticket(res):
    for a in ("order", "deal", "ticket"):
        v = getattr(res, a, None)
        if v:
            return int(v)
    if isinstance(res, dict):
        return res.get("ticket")
    return None


def _fill_price(res):
    v = getattr(res, "price", None)
    if v:
        return float(v)
    if isinstance(res, dict):
        return res.get("price") or None
    return None


def _position_open(trader, ticket):
    if ticket is None or isinstance(ticket, str):
        return False
    try:
        pos = trader.adapter.mt5.positions_get(ticket=int(ticket))
        return bool(pos)
    except Exception:
        return True     # fail-safe: assume open (never fabricate a close)


def _last_exit_price(trader, ticket):
    try:
        import pandas as pd
        deals = trader.adapter.mt5.history_deals_get(
            (pd.Timestamp.utcnow() - pd.Timedelta(hours=48)).to_pydatetime(),
            (pd.Timestamp.utcnow() + pd.Timedelta(minutes=5)).to_pydatetime())
        if not deals:
            return None
        outs = [d for d in deals if int(getattr(d, "position_id", 0)) == int(ticket)
                and int(getattr(d, "entry", 0)) == 1]   # DEAL_ENTRY_OUT
        if outs:
            return float(outs[-1].price)
    except Exception:
        return None
    return None


def drive(trader, allow_new_entries=True):
    """Per-tick driver for the `aureon_new_non_oco` engine. Immediate no-op unless the
    master flag is on. Fully guarded — never raises onto `_tick`. In PAPER mode the
    session is driven by the same simulate path the backtest uses (so the chain is
    exercised deterministically); in LIVE mode it drives the broker order lifecycle."""
    try:
        cfg = trader.cfg
        if not bool(getattr(cfg, "aureon_new_non_oco", False)):
            return
        import pandas as pd
        p = AncParams.from_cfg(cfg)
        utc_now = pd.Timestamp.now(tz="UTC")
        bdate = trader._broker_date(utc_now)
        today = str(bdate)

        st = getattr(trader, "_aurno", None)
        if st is None or st.get("day") != today:
            st = {"day": today, "sessions": {}, "skipped": {}, "last_min": None}
            trader._aurno = st

        want = set(str(x) for x in getattr(cfg, "anc_anchors", ["A2", "A5"]))

        # Arm sessions as their anchors come due (within the drift window).
        for label, hour, minute in cfg.anchors:
            if label[:2] not in want and label not in want:
                continue
            if label in st["sessions"] or label in st["skipped"]:
                continue
            try:
                if trader._anchor_skipped_today_friday(label, bdate):
                    continue
            except Exception:
                pass
            rh, rm = trader._resolved_anchor_hm(label, bdate, hour, minute)
            a_utc = trader._anchor_datetime_utc(bdate, rh, cfg.broker_tz_offset_hours, rm)
            delta = (utc_now - a_utc).total_seconds()
            if delta < 0:
                continue
            if delta > p.drift_min * 60.0:
                st["skipped"][label] = True
                _tele(trader, "warn",
                      f"{label} skipped — {delta/60:.0f}m past anchor (> {p.drift_min:.0f}m drift)")
                continue
            if not allow_new_entries:
                continue
            mid = _mid(trader)
            if mid is None:
                continue
            sess = AnchorDaySession(label, mid, p, flat_ts=_flat_ts(trader, bdate, p))
            st["sessions"][label] = sess
            _tele(trader, "info",
                  f"{label} armed @ ${mid:.2f} — levels ${sess.upper:.2f} / ${sess.lower:.2f}")

        # Manage once per newly-closed M1 bar.
        cur_min = utc_now.floor("1min")
        if st.get("last_min") == cur_min:
            return
        bar = _closed_m1(trader)
        if bar is None:
            return
        st["last_min"] = cur_min
        ema_val = _live_ema(trader, p)
        for label, sess in list(st["sessions"].items()):
            if sess.done:
                continue
            try:
                _manage_session_live(trader, sess, bar, utc_now, ema_val,
                                     allow_new_entries, p)
            except Exception as e:
                log.warning(f"{ALERT} {label} manage non-fatal: {e!r}")
    except Exception as e:
        log.warning(f"{ALERT} drive non-fatal: {e!r}")


def _manage_session_live(trader, sess, bar, now, ema_val, allow_new_entries, p):
    cfg = trader.cfg

    # PAPER: reuse the deterministic simulate brain so the chain runs end-to-end.
    if trader.paper:
        for ev in sess.on_m1_close(bar, now, ema_val):
            if ev.get("kind") == "ENTER":
                _tele(trader, "info",
                      f"{sess.label} [PAPER] {ev['side']} link{ev['link']} @ ${ev['price']:.2f} "
                      f"SL ${ev['sl']:.2f}")
            elif ev.get("kind") == "EXIT":
                _tele(trader, "success",
                      f"{sess.label} [PAPER] EXIT {ev['reason']} @ ${ev['exit']:.2f} "
                      f"({ev['pnl_price']:+.2f})")
        return

    # LIVE: broker owns the fill + stop.
    if sess.pos is not None:
        tk = sess.open_ticket
        if not _position_open(trader, tk):
            exit_px = _last_exit_price(trader, tk)
            if exit_px is None:
                exit_px = sess.pos["sl_price"]
            rec = sess.on_exit(exit_px, now, "SL")
            if rec:
                _tele(trader, "success",
                      f"{sess.label} EXIT {rec['reason']} @ ${rec['exit']:.2f} "
                      f"({rec['pnl_price']:+.2f}) — link{rec['link']}")
            return
        if sess.flat_ts is not None and now >= sess.flat_ts:
            trader.adapter.close_position(tk, dry_run=False)
            sess.on_exit(_mid(trader) or sess.pos["entry"], now, "EOD")
            _tele(trader, "info", f"{sess.label} flat @ 23:30 (EOD)")
            return
        new_sl = sess.manage_live(bar, now)
        if new_sl is not None:
            trader.adapter.modify_position_sl(tk, new_sl, dry_run=False)
        return

    if not allow_new_entries:
        return
    side = sess.poll_setup(bar, now, ema_val)
    if side is None:
        return
    mid = _mid(trader) or bar["close"]
    sl = round(mid - p.sl, 2) if side == "BUY" else round(mid + p.sl, 2)
    res = trader.adapter.place_market_order(
        cfg.symbol, side, p.lot, sl=sl, tp=0.0,
        comment=f"AURNO_{sess.label[:2]}_{side[0]}", dry_run=False, magic=AURNO_MAGIC)
    rc = getattr(res, "retcode", None) if res is not None else None
    if rc not in (10009,) and not isinstance(res, dict):
        _tele(trader, "error", f"{sess.label} {side} market rejected (rc={rc})")
        return
    fill = _fill_price(res) or mid
    sess.enter_live(side, fill, now)
    sess.open_ticket = _ticket(res)
    _tele(trader, "success",
          f"{sess.label} {side} link{sess.pos['link']} @ ${fill:.2f} SL ${sl:.2f} (lot {p.lot})")


def close_all(trader, dry_run=False):
    """Close every open AURNO_MAGIC position + cancel any AURNO pending. Used by the
    engine's own flatten path; never touches anchor/rogue/fetcher legs (magic-scoped)."""
    closed = 0
    try:
        positions = trader.adapter.mt5.positions_get(symbol=trader.cfg.symbol) or []
        for pos in positions:
            if int(getattr(pos, "magic", -1)) != AURNO_MAGIC:
                continue
            trader.adapter.close_position(int(pos.ticket), dry_run=dry_run)
            closed += 1
    except Exception as e:
        log.warning(f"{ALERT} close_all non-fatal: {e!r}")
    return closed
