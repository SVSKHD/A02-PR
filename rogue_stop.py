"""AUREON — Rogue v2 "stop mode": resting pending-stop entry engine.

WHY
---
On 2026-07-16 the band engine (seed-break + confirm-band) took ZERO entries on a
$65 crash: its tick-confirmation structurally hesitates on fast moves. Resting
pending STOP orders fill broker-side and cannot miss a fast move. This module
replaces detection with resting orders, flag-gated on `rogue_stop_mode` so the
legacy band engine stays selectable (code intact, gated on the flag).

MECHANISM (owner's frozen spec)
-------------------------------
1. Session start (post-A1 capture): rest an OCO pair — buy stop at anchor + 17.00,
   sell stop at anchor − 17.00 — lot from config, magic 20260626, comment "RGS:A1".
2. First fill CANCELS the sibling (OCO).
3. On every fill at price P in direction D: place ONE next chain stop at P ± 12.00
   (same direction), comment "RGS:C<n>". Never two unfilled chain stops at once.
4. Each filled position: init SL $10 + the existing Rogue adaptive trail.
5. Chain runs until the daily governor trips (−370 loss / +400 profit / 10-entry
   cap) or phase/day-end flattens own magic.
6. Reversal: an open position hitting its SL → cancel the pending chain stop and
   re-seed a fresh ±17 OCO at the current price (counts toward the 10-entry
   budget; `rogue_chain_cooldown_sec` gates the re-seed, NOT trend chain fills).

All destructive ops are magic-isolated (20260626). The pure geometry + the
broker-agnostic `RogueStopManager` are exercised directly by the offline,
MT5-mocked tests; the live driver adapts the MT5 adapter to the manager's small
broker interface and is gated on `rogue_stop_mode`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("AUREON")

ROGUE_MAGIC = 20260626
RGS_RE = re.compile(r"RGS:(A1|C\d+)")
RGS_PREFIX = "RGS:"


# --- parameters -------------------------------------------------------------------
@dataclass
class RogueStopParams:
    trigger: float = 17.0            # OCO distance from the anchor
    chain_step: float = 12.0         # each chain stop this far beyond the prior fill
    init_sl: float = 10.0            # init SL ($) on every fill
    lot: float = 0.35
    cooldown_sec: float = 300.0      # re-seed cooldown after an SL (not chain fills)
    trail_arm: float = 5.0
    trail_gap_early: float = 3.0
    trail_gap_deep: float = 6.0
    trail_widen_at: float = 15.0

    @classmethod
    def from_config(cls, cfg) -> "RogueStopParams":
        g = lambda n, d: float(getattr(cfg, n, d))
        return cls(
            trigger=g("rogue_trigger", 17.0),
            chain_step=g("rogue_chain_step", 12.0),
            init_sl=g("rogue_stop_init_sl", 10.0),
            lot=g("lot_size", 0.35),
            cooldown_sec=g("rogue_chain_cooldown_sec", 300.0),
            trail_arm=g("rogue_trail_arm", 5.0),
            trail_gap_early=g("rogue_trail_gap_early", 3.0),
            trail_gap_deep=g("rogue_trail_gap_deep", 6.0),
            trail_widen_at=g("rogue_trail_widen_at", 15.0),
        )


# --- comment tagging --------------------------------------------------------------
def oco_comment() -> str:
    return "RGS:A1"


def chain_comment(n: int) -> str:
    return f"RGS:C{int(n)}"


def parse_rgs(comment) -> Optional[str]:
    """Return 'A1' or 'C<n>' for a Rogue-stop order comment, else None."""
    if not comment:
        return None
    m = RGS_RE.search(str(comment))
    return m.group(1) if m else None


def is_rgs_comment(comment) -> bool:
    return parse_rgs(comment) is not None


def chain_index(comment) -> Optional[int]:
    tag = parse_rgs(comment)
    if tag and tag.startswith("C"):
        try:
            return int(tag[1:])
        except ValueError:
            return None
    return None


# --- pure geometry ----------------------------------------------------------------
def _sgn(side: str) -> float:
    return 1.0 if side == "BUY" else -1.0


def init_sl_price(side: str, entry: float, params: RogueStopParams) -> float:
    return round(entry - _sgn(side) * params.init_sl, 2)


@dataclass(frozen=True)
class StopOrder:
    side: str
    price: float
    sl: float
    comment: str


def oco_plan(anchor: float, params: RogueStopParams) -> List[StopOrder]:
    """The two resting stops at anchor ± trigger, each with its init SL."""
    buy = round(anchor + params.trigger, 2)
    sell = round(anchor - params.trigger, 2)
    return [
        StopOrder("BUY", buy, init_sl_price("BUY", buy, params), oco_comment()),
        StopOrder("SELL", sell, init_sl_price("SELL", sell, params), oco_comment()),
    ]


def chain_next(fill_price: float, direction: str, n: int,
               params: RogueStopParams) -> StopOrder:
    """The next chain stop: chain_step beyond the fill, SAME direction, init SL."""
    price = round(fill_price + _sgn(direction) * params.chain_step, 2)
    return StopOrder(direction, price, init_sl_price(direction, price, params),
                     chain_comment(n))


def update_trail(side: str, entry: float, peak: float, current_sl: float,
                 params: RogueStopParams) -> float:
    """The existing Rogue adaptive trail: inactive until +trail_arm; then follow the
    peak by trail_gap_early ($3) until +trail_widen_at ($15), then trail_gap_deep
    ($6). One-way (never loosens). Mirror-symmetric."""
    sgn = _sgn(side)
    profit = sgn * (peak - entry)
    if profit < params.trail_arm:
        return current_sl
    gap = params.trail_gap_early if profit < params.trail_widen_at else params.trail_gap_deep
    candidate = round(peak - sgn * gap, 2)
    if sgn * (candidate - current_sl) <= 0:
        return current_sl
    return candidate


def widen_for_stops_level(anchor: float, params: RogueStopParams,
                          stops_level_price: float, logger=None) -> float:
    """If the broker min stop distance exceeds `trigger`, widen it (logged). On
    XAUUSD this never triggers; the assert is defensive. Returns the effective
    trigger distance."""
    if stops_level_price and stops_level_price > params.trigger:
        (logger or log).warning(
            f"rogue_stop: broker stops_level ${stops_level_price:.2f} > trigger "
            f"${params.trigger:.2f} — widening OCO distance")
        return float(stops_level_price)
    return params.trigger


# --- broker-agnostic manager ------------------------------------------------------
class RogueStopManager:
    """Drives the OCO + chain + re-seed lifecycle against a small broker interface:

      positions() -> [obj(ticket, side, entry, sl, comment)]   (own magic only)
      pendings()  -> [obj(ticket, side, price, sl, comment)]   (own magic only)
      place_stop(side, price, sl, comment) -> ticket|None
      cancel(ticket) -> bool
      modify_sl(ticket, sl) -> bool
      closed_deal(ticket) -> {pnl, exit_price}|None            (for governor booking)
      cancel_own_pendings() -> int                             (governor halt)
      flatten_own() -> int                                     (governor halt)

    Governor state (`gov`, a rogue.new_day_state() dict) and `cfg` are supplied so
    the SAME can_enter/record_entry brakes gate stop mode. All state is reconciled
    from broker reads each tick (restart-safe); only cooldown + chain index are
    carried in-memory. `anchor_provider()` returns the A1 snapshot for the FIRST
    seed of the session (re-seeds use the current price).
    """

    def __init__(self, broker, params: RogueStopParams, gov: dict, cfg,
                 anchor_provider, logger=None,
                 on_seed=None, on_chain=None, on_reseed=None, on_first_fill=None):
        self.broker = broker
        self.p = params
        self.gov = gov
        self.cfg = cfg
        self.anchor_provider = anchor_provider
        self.log = logger or log
        self.chain_idx = 0
        self.reseed_after = 0.0        # epoch; re-seed only when now >= this
        self.traded = False            # a fill has happened this session
        self._peaks: Dict[int, float] = {}
        self._tracked: Dict[int, dict] = {}
        # Optional card / persistence hooks (default None -> no-op). Prices passed to
        # these are the ACTUAL placed StopOrder values, never recomputed downstream.
        self.on_seed = on_seed          # (anchor, [StopOrder], kind) on initial OCO
        self.on_chain = on_chain        # (StopOrder, fill_price) on each chain stop
        self.on_reseed = on_reseed      # (anchor, [StopOrder]) on a re-seed OCO
        self.on_first_fill = on_first_fill  # () when the FIRST fill of the day lands

    # -- governor ---------------------------------------------------------------
    def _can_place(self) -> Tuple[bool, str]:
        import rogue as _r
        return _r.can_enter(self.gov, self.cfg)

    def _consume_slot(self):
        import rogue as _r
        _r.record_entry(self.gov)

    def _halt_if_loss_stopped(self) -> bool:
        loss_stop = float(getattr(self.cfg, "rogue_daily_loss_stop", -370.0))
        if loss_stop < 0.0 and (self.gov.get("loss_stopped")
                                or float(self.gov.get("day_pnl", 0.0)) <= loss_stop):
            n_p = self.broker.cancel_own_pendings()
            n_f = self.broker.flatten_own()
            self.log.info(f"rogue_stop: LOSS STOP (day_pnl {self.gov.get('day_pnl')}) "
                          f"— cancelled {n_p} pendings, flattened {n_f} positions (own magic)")
            return True
        return False

    # -- one tick ---------------------------------------------------------------
    def on_tick(self, price: float, now: float) -> None:
        try:
            if self._halt_if_loss_stopped():
                return
            self._detect_fills()
            self._trail(price)
            self._detect_closes(now)
            self._seed_or_reseed(price, now)
        except Exception as e:  # never raise onto the live loop
            self.log.warning(f"rogue_stop: on_tick failed ({e!r}) — continuing")

    # -- fills ------------------------------------------------------------------
    def _detect_fills(self) -> None:
        positions = self.broker.positions()
        pendings = self.broker.pendings()
        for p in positions:
            tk = int(p.ticket)
            if tk in self._tracked:
                continue
            tag = parse_rgs(p.comment)
            if tag is None:
                continue
            self._tracked[tk] = {"side": p.side, "entry": p.entry, "comment": p.comment}
            self._peaks[tk] = p.entry
            was_flat = not self.traded
            self.traded = True
            self._consume_slot()   # every FILL counts toward the 10-entry budget
            if was_flat and self.on_first_fill:
                try:
                    self.on_first_fill()   # persist "daily OCO consumed today"
                except Exception:
                    pass
            self.log.info(f"rogue_stop: FILL {p.comment} {p.side} @ {p.entry} "
                          f"(slot {self.gov.get('reanchor_count')})")
            if tag == "A1":
                # OCO: cancel the resting sibling (opposite side, RGS:A1)
                for o in pendings:
                    if parse_rgs(o.comment) == "A1" and o.side != p.side:
                        self.broker.cancel(int(o.ticket))
                        self.log.info(f"rogue_stop: OCO sibling {o.side} @ {o.price} cancelled")
                self.chain_idx = 0
            # place the NEXT chain stop (one at a time), if the governor allows
            self._place_next_chain(p.side, p.entry)

    def _place_next_chain(self, direction: str, fill_price: float) -> None:
        # never stack: cancel any resting chain pending before placing the next
        for o in self.broker.pendings():
            if (parse_rgs(o.comment) or "").startswith("C"):
                self.broker.cancel(int(o.ticket))
        ok, why = self._can_place()
        if not ok:
            self.log.info(f"rogue_stop: chain not placed ({why}) — governor gate")
            return
        self.chain_idx += 1
        c = chain_next(fill_price, direction, self.chain_idx, self.p)
        tk = self.broker.place_stop(c.side, c.price, c.sl, c.comment)
        self.log.info(f"rogue_stop: CHAIN {c.comment} {c.side} stop @ {c.price} "
                      f"SL {c.sl} (beyond fill {fill_price})")
        if self.on_chain:
            try:
                self.on_chain(c, fill_price)   # card from the ACTUAL placed order
            except Exception:
                pass

    # -- trailing ---------------------------------------------------------------
    def _trail(self, price: float) -> None:
        live = {int(p.ticket) for p in self.broker.positions()}
        for tk in list(self._peaks):
            if tk not in live:
                self._peaks.pop(tk, None)
        for p in self.broker.positions():
            tk = int(p.ticket)
            sgn = _sgn(p.side)
            peak = max(self._peaks.get(tk, p.entry), price) if sgn > 0 \
                else min(self._peaks.get(tk, p.entry), price)
            self._peaks[tk] = peak
            new_sl = update_trail(p.side, p.entry, peak, p.sl, self.p)
            if abs(new_sl - p.sl) > 1e-9:
                self.broker.modify_sl(tk, new_sl)

    # -- closes -----------------------------------------------------------------
    def _detect_closes(self, now: float) -> None:
        live = {int(p.ticket) for p in self.broker.positions()}
        for tk in list(self._tracked):
            if tk in live:
                continue
            info = self._tracked.pop(tk)
            self._peaks.pop(tk, None)
            deal = self.broker.closed_deal(tk) if hasattr(self.broker, "closed_deal") else None
            pnl = float(deal.get("pnl", 0.0)) if deal else 0.0
            self.gov["day_pnl"] = float(self.gov.get("day_pnl", 0.0)) + pnl
            if pnl <= 0.0:
                self.gov["consec_fails"] = int(self.gov.get("consec_fails", 0)) + 1
            else:
                self.gov["consec_fails"] = 0
            # any close arms the re-seed cooldown
            self.reseed_after = now + self.p.cooldown_sec
            self.log.info(f"rogue_stop: CLOSE {info['comment']} pnl {pnl:+.2f} "
                          f"(cooldown {self.p.cooldown_sec:.0f}s before re-seed)")

    # -- seed / re-seed ---------------------------------------------------------
    def _seed_or_reseed(self, price: float, now: float) -> None:
        if self.broker.positions():
            return  # not flat — chain/trail is running
        pendings = self.broker.pendings()
        # flat: cancel any leftover chain stop (reversal left it resting)
        chain_pendings = [o for o in pendings if (parse_rgs(o.comment) or "").startswith("C")]
        for o in chain_pendings:
            self.broker.cancel(int(o.ticket))
        oco_resting = any(parse_rgs(o.comment) == "A1" for o in pendings)
        if oco_resting:
            return  # OCO already armed and waiting for the first fill
        if not self.traded:
            # FIRST seed of the session — anchor = the fixed DAILY anchor (never a
            # re-snapshot), no slot consumed. Reconcile-safe: only reached when flat,
            # no OCO resting, and the day's OCO has not been consumed by a fill.
            anchor = self.anchor_provider()
            if anchor is None:
                return
            self._place_oco(anchor, kind="seed")
            return
        # RE-SEED after a close: gated by cooldown + governor, anchor = current price,
        # and it consumes a slot (re-seeds count toward the budget). reseed_after is set
        # ONLY by an observed close this run, so a bare restart (traded reloaded True,
        # reseed_after 0) never spuriously re-seeds — reconcile leaves the day untouched.
        if self.reseed_after <= 0.0 or now < self.reseed_after:
            return
        ok, why = self._can_place()
        if not ok:
            return
        self._consume_slot()
        self.traded = False
        self.chain_idx = 0
        self.log.info(f"rogue_stop: RE-SEED at {price:.2f} "
                      f"(slot {self.gov.get('reanchor_count')})")
        self._place_oco(round(float(price), 2), kind="reseed")

    def _place_oco(self, anchor: float, kind: str = "seed") -> None:
        orders = oco_plan(anchor, self.p)
        for s in orders:
            self.broker.place_stop(s.side, s.price, s.sl, s.comment)
        self.log.info(f"rogue_stop: OCO seeded @ {anchor:.2f} "
                      f"(buy {anchor + self.p.trigger:.2f} / sell {anchor - self.p.trigger:.2f})")
        cb = self.on_reseed if kind == "reseed" else self.on_seed
        if cb:
            try:
                cb(anchor, orders) if kind == "reseed" else cb(anchor, orders, kind)
            except Exception:
                pass


# --- live MT5 shim + driver -------------------------------------------------------
@dataclass
class _O:
    ticket: int
    side: str
    entry: float
    sl: float
    comment: str
    price: float = 0.0


class _StopBroker:
    """Adapts the AUREON MT5 adapter to the manager interface, magic-scoped to
    ROGUE_MAGIC. All reads/writes touch only Rogue's own orders/positions."""

    def __init__(self, trader):
        self.t = trader
        self.mt5 = trader.adapter.mt5
        self.symbol = trader.cfg.symbol
        self.paper = bool(getattr(trader, "paper", True))

    def _ours(self, o):
        return int(getattr(o, "magic", -1) or -1) == ROGUE_MAGIC

    def _pside(self, ty):
        return "BUY" if ty == getattr(self.mt5, "POSITION_TYPE_BUY", 0) else "SELL"

    def _oside(self, ty):
        return "BUY" if ty in (getattr(self.mt5, "ORDER_TYPE_BUY_STOP", 4),
                               getattr(self.mt5, "ORDER_TYPE_BUY_LIMIT", 2)) else "SELL"

    def positions(self):
        raw = self.mt5.positions_get(symbol=self.symbol) or []
        return [_O(int(p.ticket), self._pside(p.type), float(p.price_open),
                   float(getattr(p, "sl", 0.0) or 0.0), str(getattr(p, "comment", "")))
                for p in raw if self._ours(p) and is_rgs_comment(getattr(p, "comment", ""))]

    def pendings(self):
        raw = self.mt5.orders_get(symbol=self.symbol) or []
        return [_O(int(o.ticket), self._oside(o.type), float(o.price_open),
                   float(getattr(o, "sl", 0.0) or 0.0), str(getattr(o, "comment", "")),
                   price=float(o.price_open))
                for o in raw if self._ours(o) and is_rgs_comment(getattr(o, "comment", ""))]

    def place_stop(self, side, price, sl, comment):
        res = self.t.adapter.place_stop_order(self.symbol, side, price,
                                              self.t.cfg.lot_size, sl=sl, tp=0.0,
                                              comment=comment, dry_run=self.paper)
        if getattr(res, "retcode", None) not in (None, 10009) and res is not None:
            log.warning(f"rogue_stop: place_stop {comment} rc={getattr(res,'retcode',None)} "
                        f"— PTRACE retry")
            res = self.t.adapter.place_stop_order(self.symbol, side, price,
                                                  self.t.cfg.lot_size, sl=sl, tp=0.0,
                                                  comment=comment, dry_run=self.paper)
        for a in ("order", "ticket"):
            v = getattr(res, a, None) if res is not None else None
            if v:
                return int(v)
        return None

    def cancel(self, ticket):
        try:
            self.t.adapter.cancel_order(ticket, dry_run=self.paper)
            return True
        except Exception:
            return False

    def modify_sl(self, ticket, new_sl):
        try:
            self.t.adapter.modify_position_sl(ticket, new_sl, dry_run=self.paper)
            return True
        except Exception:
            return False

    def closed_deal(self, ticket):
        return None  # live governor books P&L via rogue.detect_close / pnl_source

    def cancel_own_pendings(self):
        n = 0
        for o in self.pendings():
            if self.cancel(o.ticket):
                n += 1
        return n

    def flatten_own(self):
        n = 0
        for p in self.positions():
            try:
                self.t.adapter.close_position(p.ticket, dry_run=self.paper)
                n += 1
            except Exception:
                pass
        return n


# --- INDEPENDENT DAILY ROGUE ANCHOR (Commit 1) ------------------------------------
ROGUE_ANCHOR_LABEL = "ROGUE_02h_Asia"


def rogue_scheduled_utc(cfg, broker_date):
    """The UTC instant of Rogue's OWN daily anchor. Rogue reuses A1's schedule
    (server 02:30, Monday cushion -> 03:30) but captures INDEPENDENTLY — it never
    reads the anchor engine's A1 object, and works with the anchors engine disabled.
    `broker_date` is a `datetime.date` (has weekday()). Returns a pandas UTC Timestamp."""
    import anchors as _a
    import pandas as pd
    label, h, m = cfg.anchors[0]           # ("A1_02h_Asia", 2, 30)
    rh, rm = _a.resolved_anchor_hm(label, broker_date, h, m, cfg)   # Monday -> 03:30
    off = int(getattr(cfg, "broker_tz_offset_hours", 3))
    broker_local = pd.Timestamp(year=broker_date.year, month=broker_date.month,
                                day=broker_date.day, hour=int(rh), minute=int(rm), tz="UTC")
    return broker_local - pd.Timedelta(hours=off)   # broker-local -> real UTC


# anchor-capture decision outcomes
RELOAD = "RELOADED"
CAPTURE_SCHEDULED = "SCHEDULED"
CAPTURE_LATE = "LATE-CAPTURE"
WAIT = "WAIT"


def anchor_decision(now_utc, sched_utc, has_stored_today: bool,
                    grace_min: float = 10.0) -> str:
    """PURE: what to do this tick for the daily anchor.
      - a stored anchor for today exists -> RELOAD (never re-snapshot);
      - before the scheduled time and nothing stored -> WAIT;
      - at/just after the schedule (within grace) -> CAPTURE_SCHEDULED;
      - well past the schedule with nothing stored (a late first boot) -> CAPTURE_LATE.
    """
    if has_stored_today:
        return RELOAD
    if now_utc < sched_utc:
        return WAIT
    late = (now_utc - sched_utc).total_seconds() > float(grace_min) * 60.0
    return CAPTURE_LATE if late else CAPTURE_SCHEDULED


def _capture_price(trader, sched_utc, scheduled: bool):
    """Capture the anchor price. SCHEDULED -> the M5 close ending at the scheduled
    time (with the shared tick-fallback); LATE -> a sane settled current tick. Returns
    a float or None. Reuses the SAME capture discipline A1 uses."""
    import rogue as _r
    try:
        if scheduled:
            px = trader.adapter.get_m5_close(trader.cfg.symbol, sched_utc)
            if px is not None:
                return round(float(px), 2)
        # late boot (or the M5 bar is missing): settle a sane current tick
        return _r.seed_tick_price(trader)
    except Exception as e:
        log.warning(f"rogue_stop: anchor capture failed ({e!r})")
        return None


def _post_anchor_card(trader, source: str, actual_ts: str, anchor: float,
                      params: RogueStopParams) -> None:
    """Post the Rogue daily-anchor Discord card. Level prices come from oco_plan (the
    exact StopOrder values that WILL be placed) — never recomputed in the card layer."""
    try:
        import discord_cards as _dc
        plan = {o.side: o for o in oco_plan(anchor, params)}
        card = _dc.card_rogue_anchor(
            ROGUE_ANCHOR_LABEL, source, actual_ts, anchor, params, plan["BUY"], plan["SELL"])
        from telemetry import Severity
        trader.tele.send(
            f"🗡️ {ROGUE_ANCHOR_LABEL} anchor ${anchor:.2f} ({source})",
            Severity.INFO, card=card, important=True)
    except Exception as e:
        log.warning(f"rogue_stop: anchor card post failed ({e!r})")


def _ensure_daily_anchor(trader, st, now_utc, mgr) -> None:
    """Establish/reload the FIXED daily Rogue anchor and hand it to the manager. This
    is the fix for the 2026-07-16/17 restart re-snapshot: the anchor is captured ONCE
    on schedule, persisted, and RELOADED on every restart — never re-snapshotted."""
    import rogue as _r
    cfg = trader.cfg
    params = mgr.p
    try:
        broker_date = trader._broker_date(now_utc)
    except Exception:
        broker_date = now_utc.date()
    date_str = str(broker_date)
    stored = st.get("rogue_daily")
    has_today = bool(stored and stored.get("date") == date_str)
    sched = rogue_scheduled_utc(cfg, broker_date)
    grace = float(getattr(cfg, "rogue_anchor_grace_min", 10.0))
    decision = anchor_decision(now_utc, sched, has_today, grace)

    if decision == WAIT:
        mgr._daily_anchor = None
        return
    if decision == RELOAD:
        mgr._daily_anchor = float(stored["anchor"])
        mgr.traded = bool(stored.get("oco_consumed", False))   # consumed -> don't re-place OCO
        if not st.get("_rogue_anchor_announced"):
            st["_rogue_anchor_announced"] = True
            log.info(f"ROGUE ANCHOR RELOADED @ {stored['anchor']} "
                     f"(captured {stored.get('ts')}, source {stored.get('source')})")
            _post_anchor_card(trader, RELOAD, str(stored.get("ts")),
                              float(stored["anchor"]), params)
        return

    # CAPTURE (scheduled or late)
    scheduled = decision == CAPTURE_SCHEDULED
    px = _capture_price(trader, sched, scheduled)
    if px is None:
        mgr._daily_anchor = None
        return
    ts = now_utc.isoformat()
    st["rogue_daily"] = {"date": date_str, "anchor": float(px), "ts": ts,
                         "source": decision, "oco_consumed": False}
    st["_rogue_anchor_announced"] = True
    mgr._daily_anchor = float(px)
    mgr.traded = False
    if decision == CAPTURE_LATE:
        log.warning(f"ROGUE ANCHOR LATE-CAPTURE @ {px} (first boot after "
                    f"{sched.strftime('%H:%M')} UTC with no stored anchor)")
    else:
        log.info(f"ROGUE ANCHOR SCHEDULED @ {px} (captured {ts})")
    _post_anchor_card(trader, decision, ts, float(px), params)
    _r._persist_state(trader)


def _mark_oco_consumed(trader, st):
    d = st.get("rogue_daily")
    if isinstance(d, dict) and not d.get("oco_consumed"):
        d["oco_consumed"] = True
        try:
            import rogue as _r
            _r._persist_state(trader)
        except Exception:
            pass


def drive_stop(trader, st, allow_new_entries: bool = True) -> None:
    """Live per-tick Rogue STOP-MODE driver. Gated by the caller on
    cfg.rogue_stop_mode. Establishes the FIXED daily anchor (Commit 1), builds the
    shim + manager (persisted on the trader), and steps it once. Fully guarded."""
    import rogue as _r
    try:
        price = _r._mid(trader)
        if price is None:
            return
        now = _r._epoch()
        now_utc = _rogue_now_utc()
        mgr = getattr(trader, "_rogue_stop_mgr", None)
        if mgr is None:
            def _post_chain(order, fill_px):
                _post_chain_card(trader, order, fill_px)
            def _post_reseed(anchor, orders):
                _post_reseed_card(trader, anchor, orders)
            mgr = RogueStopManager(
                _StopBroker(trader), RogueStopParams.from_config(trader.cfg),
                st.setdefault("gov", _r.new_day_state()), trader.cfg,
                anchor_provider=lambda: getattr(mgr, "_daily_anchor", None), logger=log,
                on_chain=_post_chain, on_reseed=_post_reseed,
                on_first_fill=lambda: _mark_oco_consumed(trader, st))
            mgr._daily_anchor = None
            trader._rogue_stop_mgr = mgr
        else:
            mgr.broker = _StopBroker(trader)
            mgr.gov = st.setdefault("gov", _r.new_day_state())
        # FIXED daily anchor first (capture-on-schedule / reload-on-restart)
        if now_utc is not None:
            _ensure_daily_anchor(trader, st, now_utc, mgr)
        if not allow_new_entries:
            mgr._trail(price)   # post-EOD / kill-locked: manage + trail only
            return
        mgr.on_tick(price, now)
    except Exception as e:
        log.warning(f"rogue_stop: drive_stop non-fatal: {e!r}")


def _rogue_now_utc():
    try:
        import pandas as pd
        return pd.Timestamp.now(tz="UTC")
    except Exception:
        return None


def _post_chain_card(trader, order, fill_px):
    try:
        import discord_cards as _dc
        from telemetry import Severity
        card = _dc.card_rogue_chain(order, fill_px)
        trader.tele.send(f"🗡️ ROGUE CHAIN {order.comment} {order.side} @ ${order.price:.2f}",
                         Severity.INFO, card=card)
    except Exception as e:
        log.warning(f"rogue_stop: chain card failed ({e!r})")


def _post_reseed_card(trader, anchor, orders):
    try:
        import discord_cards as _dc
        from telemetry import Severity
        plan = {o.side: o for o in orders}
        card = _dc.card_rogue_reseed(anchor, plan["BUY"], plan["SELL"])
        trader.tele.send(f"🗡️ ROGUE RESEED anchor ${anchor:.2f}", Severity.INFO, card=card)
    except Exception as e:
        log.warning(f"rogue_stop: reseed card failed ({e!r})")
