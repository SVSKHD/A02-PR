"""AUREON — ROGUE "monster" engine core (magic 20260626).

Framework-independent decision core for the Rogue engine. This is the single
source of trading logic shared by BOTH:
  * the repo bar-mode backtester (backtest/monster_backtest.py) — parity/acceptance
  * the live adapter (rogue.drive) — MT5 order translation

PARITY CONTRACT
---------------
MonsterEngine reproduces the validated reference sim (rp2) BYTE-FOR-BYTE on
identical M1 OHLC input. The reference is bar-based: a resting stop fills when an
M1 bar's high/low crosses its level; SL/trail resolve on the same bar's extremes.
The live/tick path (FakeBroker in the sim, real MT5 live) resolves fills at tick
resolution and will therefore DIVERGE from bar fills on any bar where both a stop
and an SL/trail are touched within the same minute — that intrabar-order accuracy
is by design (see backtest/tick_cache.py), not a port defect. Bar-mode is the
parity oracle; tick-mode is faithful execution of the same logic.

This module is PURE: no MT5, no Discord, no persistence I/O, no logging side
effects. Order translation, cards, decision-log lines and state persistence live
in the adapters. Arithmetic and identifiers deliberately mirror the reference to
keep the port auditable.

Engine mechanics (design ref — user brief 2026-07-18):
  1. Rolling anchor: seed 02:30 server; after a completed sequence the closing
     price becomes the new anchor after a cooldown.
  2. Arming gate: no resting order unless armed — M5 range > atr_mult*ATR(20),
     or M1 velocity >= vel_points in vel_minutes, or break of a box_bars M5 box
     (range <= box_max_range). Disarm after disarm_bars quiet M5 bars.
  3. Bias: H1+M15 momentum -> LONG/SHORT/BOTH; armed side limited to bias.
  4. Entry: stop beyond box edge + edge_offset; fallback anchor +/- fallback_trigger
     when no box. SL cap sl_cap. Chain +chain_step, max_chains. Trail from
     trail_start, gap trail_gap.
  5. Governors: day loss, profit lock, entry cap.
  6. Adaptive guards: consecutive-SL caution, day-profit giveback, red-day carry,
     side fatigue. All adaptive state is exportable for persistence.
  7. Candle module (M5 engulfing / dragonfly): structure-context only, gated by
     cfg.candle_confirm — INERT while False (default).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# $ per 1.00 price point per 1.0 lot (XAUUSD; 0.35 lot -> $35/pt).
POINT_VALUE = 100.0


@dataclass
class MonsterCfg:
    """Engine parameters. Field names map 1:1 to config.py `rogue_*` keys via the
    live adapter (rogue.py). Defaults here mirror the validated reference."""
    lot: float = 0.35
    # gate
    atr_mult: float = 1.5
    atr_period: int = 20
    vel_points: float = 12.0
    vel_minutes: int = 5
    box_bars: int = 12
    box_max_range: float = 8.0
    disarm_bars: int = 6
    # entry / risk
    edge_offset: float = 1.0
    fallback_trigger: float = 17.0
    sl_cap: float = 10.0
    chain_step: float = 12.0
    max_chains: int = 3
    trail_start: float = 10.0
    trail_gap: float = 5.0
    # governors
    day_loss_halt: float = -1000.0
    profit_lock: float = 1000.0
    max_entries: int = 10
    # adaptive guards
    consec_sl_limit: int = 2
    caution_cooldown_min: int = 90
    caution_atr_boost: float = 0.5
    day_profit_trail_start: float = 600.0
    day_profit_giveback: float = 300.0
    redday_atr_step: float = 0.5
    side_fatigue_sl: int = 2
    # anchor
    anchor_hour: int = 2
    anchor_minute: int = 30
    reanchor_cooldown_s: int = 300
    # bias
    bias_m15_lookback: int = 8
    bias_h1_lookback: int = 4
    # candle module — inert while False (structure-context confirmation only)
    candle_confirm: bool = False


@dataclass
class Trade:
    seq: int
    kind: str          # ENTRY / CHAIN
    side: str          # LONG / SHORT
    entry_time: object = None
    entry: float = 0.0
    exit_time: object = None
    exit: float = 0.0
    sl: float = 0.0
    peak: float = 0.0      # max favorable excursion (pts)
    mae: float = 0.0       # max adverse excursion / pullback (pts)
    reason: str = ""
    arm_reason: str = ""

    @property
    def pts(self):
        d = (self.exit - self.entry) if self.side == "LONG" else (self.entry - self.exit)
        return round(d, 2)

    def pnl(self, lot):
        return round(self.pts * POINT_VALUE * lot, 2)


# ── indicator helpers ────────────────────────────────────────────────────────
def resample(m1, rule):
    return m1.resample(rule).agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last")).dropna()


def atr(df, n):
    tr = np.maximum(df.high - df.low,
                    np.maximum((df.high - df.close.shift()).abs(),
                               (df.low - df.close.shift()).abs()))
    return tr.rolling(n).mean()


def bias_of(m15, h1, t, cfg):
    """LONG / SHORT / BOTH from closed M15+H1 momentum."""
    m = m15[m15.index <= t]
    h = h1[h1.index <= t]
    if len(m) < cfg.bias_m15_lookback + 1 or len(h) < cfg.bias_h1_lookback + 1:
        return "BOTH"
    m_mom = m.close.iloc[-1] - m.close.iloc[-1 - cfg.bias_m15_lookback]
    h_mom = h.close.iloc[-1] - h.close.iloc[-1 - cfg.bias_h1_lookback]
    if m_mom > 0 and h_mom >= 0:
        return "LONG"
    if m_mom < 0 and h_mom <= 0:
        return "SHORT"
    return "BOTH"


# ── candle module (inert unless cfg.candle_confirm) ──────────────────────────
def detect_engulfing(prev_bar, cur_bar):
    """M5 engulfing: current real body fully engulfs the prior real body.
    Returns 'LONG' (bullish engulf), 'SHORT' (bearish engulf) or ''."""
    po, pc = float(prev_bar.open), float(prev_bar.close)
    co, cc = float(cur_bar.open), float(cur_bar.close)
    p_lo, p_hi = min(po, pc), max(po, pc)
    c_lo, c_hi = min(co, cc), max(co, cc)
    if c_lo <= p_lo and c_hi >= p_hi and (c_hi - c_lo) > (p_hi - p_lo):
        if cc > co and pc < po:
            return "LONG"
        if cc < co and pc > po:
            return "SHORT"
    return ""


def detect_dragonfly(cur_bar, body_frac=0.25, wick_mult=2.0):
    """Dragonfly doji: tiny body near the high, long lower wick. Bullish reversal
    context. Returns 'LONG' or ''."""
    o, h, l, c = (float(cur_bar.open), float(cur_bar.high),
                  float(cur_bar.low), float(cur_bar.close))
    rng = h - l
    if rng <= 0:
        return ""
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    # tiny body, long lower shadow, negligible upper shadow (all vs range)
    if (body <= body_frac * rng and lower >= wick_mult * body
            and upper <= body_frac * rng and lower >= 0.5 * rng):
        return "LONG"
    return ""


def candle_context(m5_closed, cfg):
    """Structure-context candle read on closed M5 bars. Returns 'LONG'/'SHORT'/''.
    Used only when cfg.candle_confirm is True (inert otherwise)."""
    if len(m5_closed) < 2:
        return ""
    prev, cur = m5_closed.iloc[-2], m5_closed.iloc[-1]
    eng = detect_engulfing(prev, cur)
    if eng:
        return eng
    return detect_dragonfly(cur)


# ── adaptive-guard predicates (pure; single source used by on_bar) ───────────
def caution_active(consec_sl, cfg):
    """Caution engages once consecutive full-SL losses reach the limit."""
    return consec_sl >= cfg.consec_sl_limit


def effective_atr_mult(cfg, extra_atr, caution_on):
    """Gate ATR multiplier including red-day carry and caution tightening."""
    return cfg.atr_mult + extra_atr + (cfg.caution_atr_boost if caution_on else 0.0)


def fatigue_blocks(sl_by_side, side, bias, cfg):
    """A side that has taken side_fatigue_sl SLs today needs real (non-BOTH) bias."""
    return sl_by_side[side] >= cfg.side_fatigue_sl and bias == "BOTH"


def giveback_halt(day_peak_pnl, day_pnl, cfg):
    """Halt the day once P/L retraces `day_profit_giveback` from a peak that
    reached `day_profit_trail_start`."""
    return (day_peak_pnl >= cfg.day_profit_trail_start
            and day_pnl <= day_peak_pnl - cfg.day_profit_giveback)


def redday_carry(day_total, cfg):
    """Red-day carry: the NEXT day starts with a tightened gate after a losing day."""
    return cfg.redday_atr_step if day_total < 0 else 0.0


# ── order-price primitives (pure; shared by the sim loop and the live adapter) ─
def entry_level(side, box, anchor, cfg):
    """Resting-stop trigger price: beyond the box edge (+edge_offset) when a box is
    present, else the anchor +/- fallback_trigger."""
    if box:
        return (box[1] + cfg.edge_offset) if side == "LONG" else (box[0] - cfg.edge_offset)
    return (anchor + cfg.fallback_trigger) if side == "LONG" else (anchor - cfg.fallback_trigger)


def init_sl(side, entry, cfg):
    """Initial stop: sl_cap points behind entry."""
    return entry - cfg.sl_cap if side == "LONG" else entry + cfg.sl_cap


def chain_level(side, entry, cfg):
    """Next chain stop: chain_step points beyond the fill, in the trade direction."""
    return entry + cfg.chain_step if side == "LONG" else entry - cfg.chain_step


def trail_target(side, entry, peak, cfg):
    """Trailed stop price for a position at `peak` favourable points. Only meaningful
    once peak >= trail_start; the caller ratchets it monotonically."""
    return (entry + peak - cfg.trail_gap) if side == "LONG" else (entry - peak + cfg.trail_gap)


# ── arming gate (pure; shared by the sim loop and the live adapter) ──────────
def gate_eval(m5_closed, m5_atr_last, vel_window_m1, px_c, anchor, eff_atr_mult, cfg):
    """Evaluate the arming gate on closed M5 bars. Returns (gate_hit, box):
      gate_hit  – '' or a reason string ('ATRx ..' / 'VEL ..' / 'BOX break ..')
      box       – (lo, hi) of the qualifying M5 box, or None
    `m5_atr_last` is ATR(atr_period) at the current bar (np.nan if unavailable);
    `vel_window_m1` is the M1 slice over the last vel_minutes."""
    gate_hit = ""
    box = None
    if len(m5_closed) >= cfg.box_bars + 1 and anchor is not None:
        last = m5_closed.iloc[-1]
        a = m5_atr_last
        if not np.isnan(a) and (last.high - last.low) > eff_atr_mult * a:
            gate_hit = f"ATRx {(last.high-last.low)/a:.2f}"
        if len(vel_window_m1) >= 2:
            vel = abs(vel_window_m1.close.iloc[-1] - vel_window_m1.close.iloc[0])
            if vel >= cfg.vel_points:
                gate_hit = gate_hit or f"VEL {vel:.1f}p/{cfg.vel_minutes}m"
        bx = m5_closed.iloc[-(cfg.box_bars + 1):-1]
        if len(bx) == cfg.box_bars and (bx.high.max() - bx.low.min()) <= cfg.box_max_range:
            box = (bx.low.min(), bx.high.max())
            if px_c > box[1] or px_c < box[0]:
                gate_hit = gate_hit or f"BOX break {box[0]:.1f}-{box[1]:.1f}"
    return gate_hit, box


def arm_side(gate_hit, px_c, m1_close_upto, m5_closed_len, bias):
    """Pick the side to arm from the gate hit + short-term momentum, limited by bias.
    Returns 'LONG' / 'SHORT' / None."""
    side = None
    if "VEL" in gate_hit or "BOX" in gate_hit or "ATR" in gate_hit:
        mom = px_c - m1_close_upto.iloc[-min(5, m5_closed_len)]
        want = "LONG" if mom > 0 else "SHORT"
        if bias in ("BOTH", want):
            side = want
    return side


class MonsterEngine:
    """Stateful monster engine. Cross-day state (extra_atr red-day carry) lives on
    the instance; per-day state is (re)built by start_day(). The full adaptive
    state is (de)serialisable via export_state()/import_state() so it survives a
    restart the same way the anchor does (PR #121 semantics)."""

    def __init__(self, cfg: MonsterCfg):
        self.cfg = cfg
        self.extra_atr = 0.0          # red-day carry into the NEXT day

    # ---- per-day lifecycle ---------------------------------------------------
    def start_day(self, m1_day, m5, m15, h1):
        c = self.cfg
        self.m1_day = m1_day
        self.m5 = m5
        self.m15 = m15
        self.h1 = h1
        self.trades, self.events = [], []
        self.anchor = None
        self.seq_no = 0
        self.open_pos = []
        self.pend = None
        self.armed_side = None
        self.arm_reason = ""
        self.quiet_m5 = 0
        self.day_pnl = 0.0
        self.day_peak_pnl = 0.0
        self.entries = 0
        self.halted = ""
        self.last_seq_close_t = None
        self.dark_bars = 0
        self.seq_open_reason = ""
        self.consec_sl = 0
        self.caution_until = None
        self.sl_by_side = {"LONG": 0, "SHORT": 0}
        self._extra_atr = self.extra_atr
        if self._extra_atr:
            self.events.append((m1_day.index[0], f"RED-DAY CARRY: atr_mult +{self._extra_atr}"))
        self.m5_day = m5[(m5.index >= m1_day.index[0]) & (m5.index <= m1_day.index[-1])]
        self.m5_atr = atr(m5, c.atr_period)
        self.seed_t = m1_day.index[0].replace(hour=c.anchor_hour, minute=c.anchor_minute)

    def _close_all(self, px, t, reason):
        for p in self.open_pos:
            tr = Trade(self.seq_no, p["kind"], p["side"], p["time"], p["entry"], t, px,
                       p["sl"], round(p["peak"], 2), round(p["mae"], 2), reason,
                       p["arm_reason"])
            self.trades.append(tr)
            self.day_pnl += tr.pnl(self.cfg.lot)
        self.open_pos = []

    def on_bar(self, t, bar):
        """Process one M1 bar. Verbatim port of the reference per-bar loop body,
        returns the events appended this bar (for the decision-grade log)."""
        c = self.cfg
        _ev0 = len(self.events)
        px_h, px_l, px_c = bar.high, bar.low, bar.close

        if self.anchor is None and t >= self.seed_t:
            self.anchor = bar.open
            self.events.append((t, f"ANCHOR seed {self.anchor:.2f}"))
        if self.halted:
            return self.events[_ev0:]

        in_cooldown = self.caution_until is not None and t < self.caution_until
        caution_on = caution_active(self.consec_sl, c)
        eff_atr_mult = effective_atr_mult(c, self._extra_atr, caution_on)
        m5_closed = self.m5_day[self.m5_day.index <= t]
        _atr_slice = self.m5_atr.loc[:t]
        m5_atr_last = _atr_slice.iloc[-1] if len(_atr_slice) else np.nan
        vel_window = self.m1_day[(self.m1_day.index <= t)
                                 & (self.m1_day.index > t - pd.Timedelta(minutes=c.vel_minutes))]
        gate_hit, box = gate_eval(m5_closed, m5_atr_last, vel_window, px_c,
                                  self.anchor, eff_atr_mult, c)

        in_cd = (self.last_seq_close_t is not None
                 and (t - self.last_seq_close_t).total_seconds() < c.reanchor_cooldown_s)

        if gate_hit and not self.open_pos and not in_cd and not in_cooldown and self.anchor is not None:
            b = bias_of(self.m15, self.h1, t, c)
            side = arm_side(gate_hit, px_c,
                            self.m1_day[self.m1_day.index <= t].close, len(m5_closed), b)
            if side and fatigue_blocks(self.sl_by_side, side, b, c):
                self.events.append((t, f"FATIGUE block {side} (SLs {self.sl_by_side[side]}, bias BOTH)"))
                side = None
            if side and caution_on and b == "BOTH":
                self.events.append((t, f"CAUTION block {side} (bias BOTH)"))
                side = None
            # candle module: structure-context confirmation, INERT unless enabled
            if side and c.candle_confirm:
                cc = candle_context(m5_closed, c)
                if cc and cc != side:
                    self.events.append((t, f"CANDLE block {side} (context {cc})"))
                    side = None
            if side and self.armed_side != side:
                self.armed_side = side
                self.arm_reason = f"{gate_hit} | bias {b}"
                self.quiet_m5 = 0
                lvl = entry_level(side, box, self.anchor, c)
                self.pend = {"side": side, "level": lvl, "kind": "ENTRY", "arm_reason": self.arm_reason}
                self.events.append((t, f"ARM {side} @{lvl:.2f} [{self.arm_reason}]"))
        elif not gate_hit and self.armed_side and not self.open_pos:
            self.quiet_m5 += 1
            if self.quiet_m5 >= c.disarm_bars:
                self.events.append((t, f"DISARM ({self.quiet_m5} quiet M5)"))
                self.armed_side, self.pend = None, None
        if not gate_hit and not self.armed_side and not self.open_pos:
            self.dark_bars += 1

        if self.pend and self.entries < c.max_entries:
            hit = (px_h >= self.pend["level"]) if self.pend["side"] == "LONG" else (px_l <= self.pend["level"])
            if hit:
                e = self.pend["level"]
                sl = init_sl(self.pend["side"], e, c)
                if self.pend["kind"] == "ENTRY":
                    self.seq_no += 1
                    self.seq_open_reason = self.pend["arm_reason"]
                self.open_pos.append({"side": self.pend["side"], "entry": e, "sl": sl, "peak": 0.0,
                                      "mae": 0.0, "kind": self.pend["kind"], "time": t,
                                      "arm_reason": self.pend["arm_reason"]})
                self.entries += 1
                self.events.append((t, f"FILL {self.pend['kind']} {self.pend['side']} @{e:.2f} SL {sl:.2f}"))
                nxt = chain_level(self.pend["side"], e, c)
                n_ch = sum(1 for p in self.open_pos if p["kind"] == "CHAIN")
                self.pend = ({"side": self.pend["side"], "level": nxt, "kind": "CHAIN",
                             "arm_reason": self.seq_open_reason}
                             if n_ch < c.max_chains else None)

        closed_now = False
        for p in list(self.open_pos):
            fav = (px_h - p["entry"]) if p["side"] == "LONG" else (p["entry"] - px_l)
            adv = (p["entry"] - px_l) if p["side"] == "LONG" else (px_h - p["entry"])
            p["peak"] = max(p["peak"], fav)
            p["mae"] = max(p["mae"], adv)
            if p["peak"] >= c.trail_start:
                tr_sl = trail_target(p["side"], p["entry"], p["peak"], c)
                p["sl"] = max(p["sl"], tr_sl) if p["side"] == "LONG" else min(p["sl"], tr_sl)
            hit_sl = (px_l <= p["sl"]) if p["side"] == "LONG" else (px_h >= p["sl"])
            if hit_sl:
                reason = "TRAIL" if p["peak"] >= c.trail_start else "SL"
                tr = Trade(self.seq_no, p["kind"], p["side"], p["time"], p["entry"], t,
                           p["sl"], p["sl"], round(p["peak"], 2), round(p["mae"], 2),
                           reason, p["arm_reason"])
                self.trades.append(tr)
                self.day_pnl += tr.pnl(c.lot)
                if reason == "SL":
                    self.consec_sl += 1
                    self.sl_by_side[p["side"]] += 1
                    if self.consec_sl == c.consec_sl_limit:
                        self.caution_until = t + pd.Timedelta(minutes=c.caution_cooldown_min)
                        self.events.append((t, f"CAUTION on: {self.consec_sl} straight SLs, "
                                               f"cooldown {c.caution_cooldown_min}m, "
                                               f"atr_mult +{c.caution_atr_boost}"))
                else:
                    if self.consec_sl >= c.consec_sl_limit:
                        self.events.append((t, "CAUTION off (winner)"))
                    self.consec_sl = 0
                self.open_pos.remove(p)
                closed_now = True

        if closed_now and not self.open_pos:
            self.pend, self.armed_side = None, None
            self.last_seq_close_t = t
            self.anchor = px_c
            self.events.append((t, f"RE-ANCHOR {self.anchor:.2f} (seq {self.seq_no} closed)"))

        self.day_peak_pnl = max(self.day_peak_pnl, self.day_pnl)
        if self.day_pnl <= c.day_loss_halt:
            self._close_all(px_c, t, "GOV-LOSS")
            self.halted = "day-loss halt"
        elif c.profit_lock and self.day_pnl >= c.profit_lock:
            self._close_all(px_c, t, "GOV-LOCK")
            self.halted = "profit lock"
        elif giveback_halt(self.day_peak_pnl, self.day_pnl, c):
            self._close_all(px_c, t, "GOV-GIVEBACK")
            self.halted = f"giveback guard (peak {self.day_peak_pnl:+.0f})"
        elif self.entries >= c.max_entries and not self.open_pos:
            self.halted = "entry cap"
        if self.halted:
            self.events.append((t, f"HALT: {self.halted} (day P/L {self.day_pnl:+.2f})"))
        return self.events[_ev0:]

    def end_day(self):
        if self.open_pos:
            self._close_all(self.m1_day.close.iloc[-1], self.m1_day.index[-1], "EOD")
        dark_pct = 100.0 * self.dark_bars / max(len(self.m1_day), 1)
        day_total = round(sum(t.pnl(self.cfg.lot) for t in self.trades), 2)
        # red-day carry decision for the NEXT day
        self.extra_atr = redday_carry(day_total, self.cfg)
        return self.trades, self.events, self.day_pnl, dark_pct, self.halted

    # ---- batch drivers (parity oracle) --------------------------------------
    def run_day(self, m1_day, m5, m15, h1):
        self.start_day(m1_day, m5, m15, h1)
        for t, bar in m1_day.iterrows():
            self.on_bar(t, bar)
        return self.end_day()

    def run(self, m1):
        m5, m15, h1 = resample(m1, "5min"), resample(m1, "15min"), resample(m1, "1h")
        days, total = [], 0.0
        for day, m1_day in m1.groupby(m1.index.date):
            trades, events, day_pnl, dark, halted = self.run_day(m1_day, m5, m15, h1)
            tot = round(sum(t.pnl(self.cfg.lot) for t in trades), 2)
            total += tot
            days.append((day, trades, events, day_pnl, dark, halted, tot))
        return days, round(total, 2)

    # ---- adaptive-state persistence (survives restart, PR #121 pattern) -----
    def export_state(self):
        """Serialise adaptive-guard state to a JSON-safe dict. Includes the
        cross-day red-day carry plus intra-day streak/caution/side state so a
        mid-day restart resumes exactly where it left off."""
        cu = None
        if getattr(self, "caution_until", None) is not None:
            cu = pd.Timestamp(self.caution_until).isoformat()
        return {
            "extra_atr": float(self.extra_atr),
            "consec_sl": int(getattr(self, "consec_sl", 0)),
            "caution_until": cu,
            "sl_by_side": dict(getattr(self, "sl_by_side", {"LONG": 0, "SHORT": 0})),
            "day_peak_pnl": float(getattr(self, "day_peak_pnl", 0.0)),
        }

    def import_state(self, d):
        """Restore adaptive-guard state from export_state()'s dict. Missing keys
        keep current values (forward-compatible)."""
        if not d:
            return
        if "extra_atr" in d:
            self.extra_atr = float(d["extra_atr"])
            # keep the per-day snapshot in sync if a day is already running
            if hasattr(self, "_extra_atr"):
                self._extra_atr = float(d["extra_atr"])
        if "consec_sl" in d:
            self.consec_sl = int(d["consec_sl"])
        if "caution_until" in d:
            self.caution_until = (pd.Timestamp(d["caution_until"])
                                  if d["caution_until"] else None)
        if "sl_by_side" in d and d["sl_by_side"]:
            self.sl_by_side = {"LONG": int(d["sl_by_side"].get("LONG", 0)),
                               "SHORT": int(d["sl_by_side"].get("SHORT", 0))}
        if "day_peak_pnl" in d:
            self.day_peak_pnl = float(d["day_peak_pnl"])
