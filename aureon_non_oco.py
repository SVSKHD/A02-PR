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
        # Transient telemetry breadcrumbs for the live NOTIFIER only (touch / ema
        # block / observation expiry). Pure annotations — the live driver drains
        # them to post NEW NON-OCO cards. They NEVER feed a decision, so the
        # backtest/paper/unit-test behaviour is byte-identical whether or not
        # anyone reads them.
        self.notices = []

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
                touched = self.upper if _bv(bar, "high") >= self.upper else self.lower
                self.notices.append(("touch", touched))     # telemetry only
                self._open_observation(now)   # touch opens the window; count from next candle
            return None

        if self.state == OBSERVE:
            if _minutes(now, self.obs_start) > self.p.obs_expiry_min:
                self.notices.append(("expiry", None))        # telemetry only
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
                        self.notices.append(                # telemetry only
                            ("ema_block", side, float(ema_value), close_,
                             self.trades_done))
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
# NEW NON-OCO message identity ------------------------------------------------
# Every event this engine posts wears its OWN embed (🔷 NEW NON-OCO, teal/amber/
# red), NEVER the straddle's blue "AUREON INFO", and every body line carries the
# greppable `[NNO]` prefix + the anchor label + the chain link index. The title,
# emoji and colours come from cfg.nno_* so the operator can retune them without a
# code change; the straddle / ROGUE / FETCHER cards are untouched. Bursts inside a
# single drive() tick are BATCHED into one card per tone so a fast chain can never
# flood #bot_update — and the batched card keeps the same NEW NON-OCO title/colour.

def _nno_line(cfg, label, link, text):
    """Build one `[NNO]`-prefixed body line carrying the anchor label + link index."""
    prefix = str(getattr(cfg, "nno_notify_prefix", "[NNO]"))
    head = str(label) if label else ""
    if link is not None and head:
        head = f"{head} link {link}"
    return " ".join(x for x in (prefix, head, str(text)) if x).strip()


def _nno_colour(cfg, tone):
    if tone == "warn":
        return int(getattr(cfg, "nno_embed_colour_warn", 0xE8A33D))
    if tone == "bad":
        return int(getattr(cfg, "nno_embed_colour_bad", 0xE24B4A))
    return int(getattr(cfg, "nno_embed_colour", 0x2ECC9B))


def _nno_sev(tone):
    try:
        from telemetry import Severity
        return {"warn": Severity.WARN, "bad": Severity.ERROR}.get(tone, Severity.INFO)
    except Exception:
        return {"warn": 30, "bad": 40}.get(tone, 20)   # INFO/WARN/ERROR ints


def _send_nno(trader, tone, line):
    """Post ONE NEW NON-OCO card immediately (single event, or a command reply)."""
    cfg = trader.cfg
    try:
        import discord_cards as _dc
        card = _dc.card_nno(str(getattr(cfg, "nno_embed_title", "NEW NON-OCO")),
                            str(getattr(cfg, "nno_embed_emoji", "\U0001F537")),
                            _nno_colour(cfg, tone), line)
    except Exception:
        card = None
    try:
        trader.tele.send(line, _nno_sev(tone), card=card, important=True)
    except Exception:
        pass


def _emit(trader, tone, label, link, text):
    """Emit a NEW NON-OCO event. Inside a drive() tick it is BUFFERED (flushed as a
    batched card at the end of the tick); outside one (command replies) it posts
    immediately. `tone` in 'normal' | 'warn' | 'bad'. Never raises."""
    line = _nno_line(trader.cfg, label, link, text)
    buf = getattr(trader, "_aurno_batch", None)
    if buf is not None:
        buf.append((tone, line))
    else:
        _send_nno(trader, tone, line)


def _flush_nno(trader, buf):
    """Flush a drive() tick's buffered events. 0/1 -> nothing / one card. A burst
    -> ONE batched card per tone (keeping the NEW NON-OCO title + colour), so a
    fast chain coalesces instead of flooding the channel. Never raises."""
    try:
        if not buf:
            return
        if len(buf) == 1:
            _send_nno(trader, buf[0][0], buf[0][1])
            return
        cfg = trader.cfg
        import discord_cards as _dc
        title = str(getattr(cfg, "nno_embed_title", "NEW NON-OCO"))
        emoji = str(getattr(cfg, "nno_embed_emoji", "\U0001F537"))
        for tone in ("normal", "warn", "bad"):     # one card per non-empty tone
            lines = [ln for t, ln in buf if t == tone]
            if not lines:
                continue
            if len(lines) == 1:
                _send_nno(trader, tone, lines[0])
                continue
            try:
                card = _dc.card_nno_batch(title, emoji, _nno_colour(cfg, tone), lines)
            except Exception:
                card = None
            try:
                trader.tele.send("\n".join(lines), _nno_sev(tone),
                                 card=card, important=True)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"{ALERT} notify flush non-fatal: {e!r}")


def notify(trader, tone, label, link, text):
    """Public one-shot NEW NON-OCO card (used by the live_trader `!nno` command
    replies, which run outside a drive() tick). Posts immediately."""
    _send_nno(trader, tone, _nno_line(trader.cfg, label, link, text))


def _drain_notices(trader, sess):
    """Turn the session's pure telemetry breadcrumbs (touch / ema block / expiry)
    into NEW NON-OCO cards, then clear them. Decision-free."""
    notices, sess.notices = sess.notices, []
    for n in notices:
        kind = n[0]
        if kind == "touch":
            _emit(trader, "normal", sess.label, None,
                  f"touch {n[1]:.2f} — observing (no order)")
        elif kind == "ema_block":
            _side, ema_v, _close, link = n[1], n[2], n[3], n[4]
            _emit(trader, "warn", sess.label, link,
                  f"short blocked — ema200 {ema_v:.2f}" if _side == "SELL"
                  else f"long blocked — ema200 {ema_v:.2f}")
        elif kind == "expiry":
            _emit(trader, "warn", sess.label, None,
                  "observation expired, no confirmation")


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
            return                       # flag OFF -> byte-identical no-op
        trader._aurno_batch = []         # events this tick coalesce into it
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
                _emit(trader, "warn", label, None,
                      f"skipped — {delta/60:.0f}m past anchor (> {p.drift_min:.0f}m drift)")
                continue
            if not allow_new_entries:
                continue
            mid = _mid(trader)
            if mid is None:
                continue
            sess = AnchorDaySession(label, mid, p, flat_ts=_flat_ts(trader, bdate, p))
            st["sessions"][label] = sess
            _emit(trader, "normal", label, None,
                  f"anchor {mid:.2f} — levels {sess.upper:.2f} / {sess.lower:.2f}")

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
    finally:
        # Coalesce this tick's events into batched NEW NON-OCO cards, then detach
        # the buffer (so command-reply notifies post immediately, not buffered).
        # Only runs when the flag is ON (the buffer is created past that gate).
        batch = getattr(trader, "_aurno_batch", None)
        if batch is not None:
            trader._aurno_batch = None
            _flush_nno(trader, batch)


def _exit_tone(rec):
    """Losing exit -> red; profitable / break-even -> teal."""
    return "bad" if float(rec.get("pnl_price", 0.0)) < -1e-9 else "normal"


def _emit_ladder(trader, sess, new_sl):
    """A stop advanced this bar — post the ladder milestone (lock / target-secured)
    the FIRST time each threshold is crossed, so the operator sees the ratchet act.
    Reads the pos it already ratcheted; sets one-shot flags on the (live-only) pos
    dict so a re-touch of the same rung doesn't re-post. Decision-free."""
    pos = sess.pos
    if pos is None:
        return
    off = float(pos.get("sl_off", 0.0))
    p = sess.p
    if off >= p.target - 1e-9 and not pos.get("_secure_notified"):
        pos["_secure_notified"] = True
        pos["_lock_notified"] = True
        _emit(trader, "normal", sess.label, pos["link"],
              f"secured +{p.target:.0f}, trailing {p.trail_dist:.1f}")
    elif off >= p.lock_to - 1e-9 and not pos.get("_lock_notified"):
        pos["_lock_notified"] = True
        _emit(trader, "normal", sess.label, pos["link"], f"locked +{p.lock_to:.1f}")


def _emit_chain(trader, sess, rec):
    """After an exit, report where the chain went: reopened at the exit price (next
    link), or ended (losing link / cap). Reads the post-exit session state."""
    _emit(trader, _exit_tone(rec), sess.label, rec["link"],
          f"closed {_money(rec['pnl_price'])} ({rec['reason']})")
    if sess.done:
        if sess.chain_ended:
            _emit(trader, "warn", sess.label, None, "chain ended (losing link)")
        else:
            _emit(trader, "warn", sess.label, None,
                  f"chain ended (cap {sess.p.max_chain})")
    elif sess.reopen_price is not None:
        _emit(trader, "normal", sess.label, None,
              f"observing again from {sess.reopen_price:.2f} (link {sess.trades_done})")


def _money(v):
    try:
        f = float(v)
        return f"{'+' if f >= 0 else '-'}${abs(f):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _manage_session_live(trader, sess, bar, now, ema_val, allow_new_entries, p):
    cfg = trader.cfg

    # PAPER: reuse the deterministic simulate brain so the chain runs end-to-end.
    if trader.paper:
        for ev in sess.on_m1_close(bar, now, ema_val):
            k = ev.get("kind")
            if k == "ENTER":
                _emit(trader, "normal", sess.label, ev["link"],
                      f"[PAPER] {ev['side']} {p.lot:.2f} @ {ev['price']:.2f} "
                      f"sl {ev['sl']:.2f}")
            elif k == "MODIFY_SL":
                _emit_ladder(trader, sess, ev["sl"])
            elif k == "EXIT":
                _emit_chain(trader, sess, ev)
        _drain_notices(trader, sess)
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
                _emit_chain(trader, sess, rec)
            _drain_notices(trader, sess)
            return
        if sess.flat_ts is not None and now >= sess.flat_ts:
            trader.adapter.close_position(tk, dry_run=False)
            rec = sess.on_exit(_mid(trader) or sess.pos["entry"], now, "EOD")
            if rec:
                _emit(trader, "normal", sess.label, rec["link"],
                      f"flat @ 23:30 (EOD) {_money(rec['pnl_price'])}")
            return
        new_sl = sess.manage_live(bar, now)
        if new_sl is not None:
            trader.adapter.modify_position_sl(tk, new_sl, dry_run=False)
            _emit_ladder(trader, sess, new_sl)
        return

    # Entries blocked (paused / friday window / account lock): do NOT advance the
    # setup state machine — leave it exactly as it was so a confirmation is never
    # consumed-and-discarded while blocked (byte-identical to the pre-control-surface
    # behaviour). Open positions above still ladder/exit; this gate is new-risk only.
    if not allow_new_entries:
        return
    side = sess.poll_setup(bar, now, ema_val)
    _drain_notices(trader, sess)     # touch / ema-block / expiry surfaced on this bar
    if side is None:
        return
    mid = _mid(trader) or bar["close"]
    sl = round(mid - p.sl, 2) if side == "BUY" else round(mid + p.sl, 2)
    res = trader.adapter.place_market_order(
        cfg.symbol, side, p.lot, sl=sl, tp=0.0,
        comment=f"AURNO_{sess.label[:2]}_{side[0]}", dry_run=False, magic=AURNO_MAGIC)
    rc = getattr(res, "retcode", None) if res is not None else None
    if rc not in (10009,) and not isinstance(res, dict):
        _emit(trader, "bad", sess.label, sess.trades_done,
              f"{side} market rejected (rc={rc})")
        return
    fill = _fill_price(res) or mid
    sess.enter_live(side, fill, now)
    sess.open_ticket = _ticket(res)
    _emit(trader, "normal", sess.label, sess.pos["link"],
          f"{side} {p.lot:.2f} @ {fill:.2f} sl {sl:.2f}")


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


# ===========================================================================
# `!nno` control surface — magic 20260811 ONLY
# ===========================================================================
# Runs on the TRADING thread (via live_trader._handle_commands), so every broker
# read/write here is serialised with the engine's own order lifecycle — the
# Discord thread never calls MT5. EVERY command replies with a NEW NON-OCO card
# so the control surface shares the engine's distinct identity. EVERY broker
# touch is filtered by AURNO_MAGIC; no command can read, modify, or close an
# anchor / ROGUE / FETCHER / RB / RGS / TF_ leg. With the master flag OFF (or the
# command surface disabled) `handle_command` is an immediate no-op.
import time as _time


def _now_wall():
    return _time.time()


def _sessions(trader):
    st = getattr(trader, "_aurno", None)
    return (st.get("sessions", {}) or {}) if isinstance(st, dict) else {}


def _nno_paused(trader):
    return bool((getattr(trader, "state", None) or {}).get("nno_paused", False))


def _open_positions(trader):
    """Broker-truth OPEN positions owned by THIS engine (magic 20260811 ONLY).
    Every other magic is filtered out here, so nothing downstream can act on a
    foreign leg."""
    out = []
    try:
        positions = trader.adapter.mt5.positions_get(symbol=trader.cfg.symbol) or []
    except Exception:
        positions = []
    for pos in positions:
        try:
            if int(getattr(pos, "magic", -1)) == AURNO_MAGIC:
                out.append(pos)
        except Exception:
            continue
    return out


def _ticket_link_map(trader):
    """ticket -> (anchor label, link index) for open engine positions, from the
    live session state (the broker position carries no link index)."""
    m = {}
    for label, sess in _sessions(trader).items():
        tk = getattr(sess, "open_ticket", None)
        pos = getattr(sess, "pos", None)
        if tk is not None and pos:
            try:
                m[int(tk)] = (label, pos.get("link"))
            except Exception:
                continue
    return m


def _link_usd(cfg, pnl_price):
    """Price-point P&L -> USD at the engine's fixed per-link lot (anc_lot) and the
    account contract size. Mirrors the straddle's contract_size convention."""
    cs = float(getattr(cfg, "contract_size", 100.0))
    lot = float(getattr(cfg, "anc_lot", 0.10))
    return float(pnl_price) * cs * lot


def _realized_today(trader):
    """(n_links, usd) realized by the engine today, from the session trade records
    (magic 20260811 only — the sessions dict is this engine's alone)."""
    recs = []
    for sess in _sessions(trader).values():
        recs.extend(getattr(sess, "trades", []) or [])
    usd = sum(_link_usd(trader.cfg, r.get("pnl_price", 0.0)) for r in recs)
    return len(recs), usd


def _anchor_state_str(sess):
    parts = [f"anc {sess.anchor_price:.2f}", f"{sess.lower:.2f}/{sess.upper:.2f}"]
    if sess.done:
        parts.append("dead (losing link)" if sess.chain_ended else "done")
    elif sess.state == WAIT_TOUCH:
        parts.append("waiting touch")
    elif sess.state == OBSERVE:
        parts.append(f"observing (run {sess.run_len}/{sess.p.n_candles})")
    elif sess.state == IN_POS:
        parts.append(f"in pos link {sess.pos['link'] if sess.pos else '?'}")
    parts.append(f"link {sess.trades_done}")
    return " · ".join(parts)


def _post_nno_card(trader, tone, header, fields):
    """Post a multi-field NEW NON-OCO card (status/anchors/positions/today/config/
    help). Header is a plain `[NNO]`-prefixed line; fields is a list of (name,
    value) tuples. Never raises."""
    cfg = trader.cfg
    line = _nno_line(cfg, None, None, header)
    try:
        import discord_cards as _dc
        card = _dc.card_nno(str(getattr(cfg, "nno_embed_title", "NEW NON-OCO")),
                            str(getattr(cfg, "nno_embed_emoji", "\U0001F537")),
                            _nno_colour(cfg, tone), line, fields=fields)
    except Exception:
        card = None
    try:
        text = line + "\n" + "\n".join(f"{n}: {v}" for n, v, *_ in fields)
        trader.tele.send(text, _nno_sev(tone), card=card, important=True)
    except Exception:
        pass


def _cmd_status(trader):
    cfg = trader.cfg
    positions = _open_positions(trader)
    open_usd = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in positions)
    n_done, realized = _realized_today(trader)
    fields = [
        ("Engine", "🟢 ON" if bool(getattr(cfg, "aureon_new_non_oco", False)) else "🔴 OFF"),
        ("Paused", "⏸ yes" if _nno_paused(trader) else "▶️ no"),
        ("Realized today", f"{_money(realized)} · {n_done} link(s)"),
        ("Open positions", f"{len(positions)} · {_money(open_usd)} live"),
    ]
    for label, sess in _sessions(trader).items():
        fields.append((label, _anchor_state_str(sess)))
    _post_nno_card(trader, "normal", "status (magic 20260811)", fields)


def _cmd_anchors(trader):
    sessions = _sessions(trader)
    if not sessions:
        notify(trader, "normal", None, None, "anchors — none armed yet today")
        return
    fields = [(label, _anchor_state_str(sess)) for label, sess in sessions.items()]
    _post_nno_card(trader, "normal", "anchors", fields)


def _cmd_positions(trader):
    positions = _open_positions(trader)
    if not positions:
        notify(trader, "normal", None, None, "positions — none open (magic 20260811)")
        return
    linkmap = _ticket_link_map(trader)
    fields = []
    for p in positions:
        tk = int(getattr(p, "ticket", 0))
        side = "BUY" if int(getattr(p, "type", 0)) == 0 else "SELL"
        entry = float(getattr(p, "price_open", 0.0) or 0.0)
        cur = float(getattr(p, "price_current", 0.0) or 0.0)
        sl = float(getattr(p, "sl", 0.0) or 0.0)
        prof = float(getattr(p, "profit", 0.0) or 0.0)
        label, link = linkmap.get(tk, ("?", "?"))
        fields.append((f"{tk} {side}",
                       f"{label} link {link} · in {entry:.2f} now {cur:.2f} "
                       f"sl {sl:.2f} · {_money(prof)}"))
    _post_nno_card(trader, "normal", "positions (magic 20260811)", fields)


def _cmd_today(trader):
    recs = []
    for label, sess in _sessions(trader).items():
        for r in (getattr(sess, "trades", []) or []):
            recs.append((label, r))
    if not recs:
        notify(trader, "normal", None, None, "today — no closed links yet")
        return
    fields, total = [], 0.0
    for label, r in recs:
        usd = _link_usd(trader.cfg, r.get("pnl_price", 0.0))
        total += usd
        t = r.get("exit_time")
        tstr = t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)
        fields.append((f"{tstr} {label} link {r.get('link')}",
                       f"{r.get('reason')} · {_money(usd)}"))
    fields.append(("TOTAL", _money(total)))
    _post_nno_card(trader, "normal" if total >= 0 else "bad", "today", fields)


def _cmd_config(trader):
    cfg = trader.cfg
    keys = ["anc_anchors", "anc_threshold", "anc_n_candles", "anc_obs_expiry_min",
            "anc_sl", "anc_lock_at", "anc_lock_to", "anc_target", "anc_trail_dist",
            "anc_max_chain", "anc_chain_stop_on_loss", "anc_chain_trend",
            "anc_ema_period", "anc_lot", "anc_drift_min", "anc_flat_broker_hour"]
    fields = [(k, str(getattr(cfg, k, "?"))) for k in keys]
    _post_nno_card(trader, "normal", "config (live anc_*)", fields)


def _cmd_help(trader):
    fields = [
        ("!nno status", "on/off, paused, today P&L, open positions, per-anchor state"),
        ("!nno anchors", "per-anchor: price, levels, touched, observing, link, dead"),
        ("!nno positions", "open magic-20260811: ticket, side, entry, current, SL, P&L, link"),
        ("!nno today", "today's closed links: time, link, reason, P&L, total"),
        ("!nno pause / resume", "stop / allow NEW entries + chain links (open positions keep laddering)"),
        ("!nno flat [confirm]", "close ONLY magic 20260811 (two-step confirm)"),
        ("!nno config", "the live anc_* values"),
    ]
    _post_nno_card(trader, "normal", "commands", fields)


def _cmd_pause(trader):
    st = getattr(trader, "state", None)
    if isinstance(st, dict):
        st["nno_paused"] = True
    try:
        trader._save_state()
    except Exception:
        pass
    notify(trader, "warn", None, None,
           "paused — no NEW entries or chain links (open positions keep laddering)")


def _cmd_resume(trader):
    st = getattr(trader, "state", None)
    if isinstance(st, dict):
        st["nno_paused"] = False
    try:
        trader._save_state()
    except Exception:
        pass
    notify(trader, "normal", None, None, "resumed — new entries allowed")


def _cmd_flat(trader, confirm):
    """Two-step, magic-20260811-only flatten. Bare `!nno flat` reports the count +
    combined P&L that WILL close and arms a confirm window; `!nno flat confirm`
    within `nno_discord_flat_confirm_sec` executes close_all (magic-scoped)."""
    cfg = trader.cfg
    win = float(getattr(cfg, "nno_discord_flat_confirm_sec", 60.0))
    if not confirm:
        positions = _open_positions(trader)
        n = len(positions)
        usd = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in positions)
        trader._nno_flat_pending_ts = _now_wall()
        if n == 0:
            notify(trader, "normal", None, None,
                   "flat — no open magic-20260811 positions")
            return
        notify(trader, "warn", None, None,
               f"flat WILL close {n} position(s), combined {_money(usd)} — send "
               f"`!nno flat confirm` within {win:.0f}s (anchor/ROGUE/FETCHER untouched)")
        return
    ts = getattr(trader, "_nno_flat_pending_ts", None)
    if ts is None or (_now_wall() - float(ts)) > win:
        trader._nno_flat_pending_ts = None
        notify(trader, "warn", None, None,
               "flat confirm expired or none pending — send `!nno flat` first")
        return
    trader._nno_flat_pending_ts = None
    closed = close_all(trader, dry_run=False)
    notify(trader, "bad", None, None,
           f"FLAT — closed {closed} magic-20260811 position(s) "
           f"(anchor/ROGUE/FETCHER untouched)")


_NNO_COMMANDS = {
    "status": _cmd_status, "anchors": _cmd_anchors, "positions": _cmd_positions,
    "today": _cmd_today, "config": _cmd_config, "help": _cmd_help,
    "pause": _cmd_pause, "resume": _cmd_resume,
}


def handle_command(trader, sub, confirm=False):
    """Entry point for a queued `!nno <sub>` command, called on the trading thread.
    No-op — touching no broker, no notifier, no state — when the command surface is
    disabled OR the master flag is OFF (so the flag-off guarantee holds). Guarded:
    a command error never breaks the tick loop."""
    cfg = trader.cfg
    if not bool(getattr(cfg, "nno_discord_commands_enabled", True)):
        return
    if not bool(getattr(cfg, "aureon_new_non_oco", False)):
        return
    sub = str(sub or "").lower()
    try:
        if sub == "flat":
            _cmd_flat(trader, bool(confirm))
        else:
            _NNO_COMMANDS.get(sub, lambda _t: None)(trader)
    except Exception as e:
        log.warning(f"{ALERT} !nno {sub} non-fatal: {e!r}")
