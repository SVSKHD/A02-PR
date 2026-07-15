"""AUREON — rescue boost (v2): pre-SL counter-direction recovery for straddle legs.

DISTINCT from the existing market-order rescue/boost fleet (rescue.py /
boosts_common). This mechanism rests TWO pending stop orders in the OPPOSITE
direction the instant a straddle leg fills, so that on a confirmed reversal the
boosts recover the leg's hard-SL loss and profit on continuation. Mirror-symmetric
for BUY and SELL originals.

Spec (launch values; frozen — see config.py rescue_boost_v2_* overrides):
  BOOST_LOT        = 0.45
  BOOST_1_OFFSET   = 15.0   # boost #1 at original entry ∓ 15 (adverse)
  BOOST_2_OFFSET   = 25.0   # boost #2 at original entry ∓ 25 (adverse)
  TRAIL_ACTIVATION = 10.0   # a boost trails after +10 in its favor
  TRAIL_GAP        = 5.0    # trailing-stop distance once active
  MAX_BOOSTS       = 2

Resolved rules (the truncated part of the spec, confirmed with the operator):
  - Original position keeps its hard SL UNCHANGED at entry ∓ SL_POINTS; boosts
    never move or remove it, and the original rides to its own SL/TP untouched.
  - Each boost's INITIAL hard SL sits at the ORIGINAL ENTRY (=> 15 pts risk for
    boost #1, 25 pts for boost #2). Once +10 favorable, the $5 trail takes over.
  - Unfilled boosts are cancelled when their PARENT position closes.

Tagging: each boost order comment is "RB1:<parent_ticket>" / "RB2:<parent_ticket>"
so the parent link survives restarts (the manager reconciles purely from broker
state) and so stale_leg_sweep can recognise and exempt a live rescue boost.

This module is pure + broker-agnostic: the manager talks to a small duck-typed
broker interface (positions/pendings/place_pending/cancel/modify_sl), exercised
directly by the unit tests with a fake. The live hook in live_trader adapts the
MT5 adapter to that interface, flag-gated on cfg.rescue_boost_v2_enabled (OFF by
default — this adds real counter orders on the live path).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

log = logging.getLogger("AUREON")

BOOST_TAG_RE = re.compile(r"RB([12]):(\d+)")


# --- parameters -------------------------------------------------------------------
@dataclass
class RescueBoostParams:
    boost_lot: float = 0.45
    boost_1_offset: float = 15.0
    boost_2_offset: float = 25.0
    trail_activation: float = 10.0
    trail_gap: float = 5.0
    max_boosts: int = 2

    @classmethod
    def from_config(cls, cfg) -> "RescueBoostParams":
        g = lambda name, d: float(getattr(cfg, name, d))
        return cls(
            boost_lot=g("rescue_boost_v2_lot", 0.45),
            boost_1_offset=g("rescue_boost_v2_offset_1", 15.0),
            boost_2_offset=g("rescue_boost_v2_offset_2", 25.0),
            trail_activation=g("rescue_boost_v2_trail_activation", 10.0),
            trail_gap=g("rescue_boost_v2_trail_gap", 5.0),
            max_boosts=int(getattr(cfg, "rescue_boost_v2_max_boosts", 2)),
        )

    def offsets(self) -> List[float]:
        return [self.boost_1_offset, self.boost_2_offset][: max(0, self.max_boosts)]


# --- comment tagging --------------------------------------------------------------
def boost_comment(idx: int, parent_ticket) -> str:
    """"RB1:<ticket>" / "RB2:<ticket>" — restart-safe parent link."""
    return f"RB{int(idx)}:{int(parent_ticket)}"


def parse_boost_comment(comment) -> Optional[Tuple[int, int]]:
    """(boost_idx, parent_ticket) from an "RB<idx>:<parent>" comment, else None."""
    if not comment:
        return None
    m = BOOST_TAG_RE.search(str(comment))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def is_boost_comment(comment) -> bool:
    return parse_boost_comment(comment) is not None


# --- pure geometry ----------------------------------------------------------------
def opposite(side: str) -> str:
    return "SELL" if side == "BUY" else "BUY"


@dataclass(frozen=True)
class BoostOrder:
    idx: int
    side: str
    price: float
    sl: float
    lot: float
    comment: str


def boost_plan(parent_side: str, parent_entry: float, parent_ticket,
               params: RescueBoostParams) -> List[BoostOrder]:
    """The pending boost stop orders to rest when a straddle leg fills.

    OPPOSITE direction, at parent_entry ∓ offset (adverse to the parent), lot
    boost_lot, SL at the ORIGINAL ENTRY. Mirror-symmetric: for a BUY parent the
    boosts are SELL stops BELOW entry; for a SELL parent they are BUY stops ABOVE.
    """
    b_side = opposite(parent_side)
    # adverse-to-parent direction: below a BUY entry, above a SELL entry
    adverse = -1.0 if parent_side == "BUY" else 1.0
    sl = round(float(parent_entry), 2)   # boost SL = original entry (per spec)
    plan: List[BoostOrder] = []
    for i, off in enumerate(params.offsets(), start=1):
        price = round(parent_entry + adverse * off, 2)
        plan.append(BoostOrder(idx=i, side=b_side, price=price, sl=sl,
                               lot=params.boost_lot,
                               comment=boost_comment(i, parent_ticket)))
    return plan


def _sgn(side: str) -> float:
    return 1.0 if side == "BUY" else -1.0


def update_boost_trail(boost_side: str, boost_entry: float, peak: float,
                       current_sl: float, params: RescueBoostParams) -> float:
    """One-way trailing stop for a filled boost. Inactive until the boost is at
    least +trail_activation (10) favorable at its PEAK; then the stop follows the
    peak by trail_gap (5), never loosening. `peak` is the best price reached (high
    for a BUY boost, low for a SELL boost). Mirror-symmetric.

    Before activation the boost keeps its initial SL (the original entry), returned
    unchanged here.
    """
    sgn = _sgn(boost_side)
    peak_fav = sgn * (peak - boost_entry)
    if peak_fav < params.trail_activation:
        return current_sl
    candidate = round(peak - sgn * params.trail_gap, 2)
    # advance only in the favorable direction (never loosen the stop)
    if sgn * (candidate - current_sl) <= 0:
        return current_sl
    return candidate


# --- broker-agnostic manager ------------------------------------------------------
class RescueBoostManager:
    """Places boosts on new straddle fills, trails filled boosts, and cancels
    unfilled boosts when their parent closes. Broker is a small duck-typed object:

      positions() -> [obj(ticket, side, entry, sl, lot, comment)]
      pendings()  -> [obj(ticket, side, price, sl, lot, comment)]
      place_pending(side, price, sl, lot, comment) -> ticket|None
      cancel(ticket) -> bool
      modify_sl(ticket, new_sl) -> bool

    All state is reconciled from broker reads each tick, so the manager is
    restart-safe with no persistence: a parent already carrying RB orders is never
    re-boosted (idempotent), and a boost adopted after restart trails normally.
    `is_parent` decides which positions are boostable (default: any non-RB position);
    the live hook narrows this to the straddle magic.
    """

    def __init__(self, broker, params: RescueBoostParams, logger=None, is_parent=None):
        self.broker = broker
        self.params = params
        self.log = logger or log
        self._is_parent = is_parent or (lambda p: not is_boost_comment(getattr(p, "comment", "")))
        self._peaks = {}  # boost ticket -> best price seen (for the trail)

    # -- placement --------------------------------------------------------------
    def _parents_with_boosts(self):
        """Set of parent tickets that already have any RB order (pending or open)."""
        have = set()
        for o in list(self.broker.pendings()) + list(self.broker.positions()):
            pc = parse_boost_comment(getattr(o, "comment", ""))
            if pc:
                have.add(pc[1])
        return have

    def place_boosts_for_new_fills(self) -> List[dict]:
        placed = []
        have = self._parents_with_boosts()
        for p in self.broker.positions():
            if not self._is_parent(p):
                continue
            ticket = int(getattr(p, "ticket"))
            if ticket in have:
                continue  # idempotent: already boosted (survives restart)
            plan = boost_plan(p.side, p.entry, ticket, self.params)
            for b in plan:
                tk = self.broker.place_pending(b.side, b.price, b.sl, b.lot, b.comment)
                placed.append({"parent": ticket, "idx": b.idx, "ticket": tk,
                               "side": b.side, "price": b.price, "sl": b.sl})
                self.log.info(
                    f"rescue_boost: placed {b.comment} {b.side} {b.lot} @ {b.price} "
                    f"SL {b.sl} (parent {ticket} {p.side} @ {p.entry})")
            have.add(ticket)
        return placed

    # -- trailing ---------------------------------------------------------------
    def trail_open_boosts(self, current_price: float) -> None:
        open_boosts = [p for p in self.broker.positions()
                       if is_boost_comment(getattr(p, "comment", ""))]
        live = {int(p.ticket) for p in open_boosts}
        for tk in list(self._peaks):
            if tk not in live:
                self._peaks.pop(tk, None)
        for p in open_boosts:
            tk = int(p.ticket)
            sgn = _sgn(p.side)
            prev = self._peaks.get(tk, p.entry)
            peak = max(prev, current_price) if sgn > 0 else min(prev, current_price)
            self._peaks[tk] = peak
            new_sl = update_boost_trail(p.side, p.entry, peak, p.sl, self.params)
            if abs(new_sl - p.sl) > 1e-9:
                if self.broker.modify_sl(tk, new_sl):
                    self.log.info(f"rescue_boost: trail {getattr(p,'comment','')} "
                                  f"SL {p.sl} -> {new_sl} (peak {peak})")

    # -- cancellation -----------------------------------------------------------
    def cancel_orphaned_boosts(self) -> List[int]:
        """Cancel unfilled boost pendings whose parent position has closed. A boost
        is orphaned when its parent ticket is not among the open non-RB positions.
        (Open boost positions are left to their own trail/SL.)"""
        parents_open = {int(p.ticket) for p in self.broker.positions()
                        if not is_boost_comment(getattr(p, "comment", ""))}
        cancelled = []
        for o in self.broker.pendings():
            pc = parse_boost_comment(getattr(o, "comment", ""))
            if not pc:
                continue
            _idx, parent = pc
            if parent not in parents_open:
                if self.broker.cancel(int(o.ticket)):
                    cancelled.append(int(o.ticket))
                    self.log.info(
                        f"rescue_boost: cancelled orphan {getattr(o,'comment','')} "
                        f"ticket={o.ticket} (parent {parent} closed)")
        return cancelled

    # -- one step ---------------------------------------------------------------
    def on_tick(self, current_price: float) -> None:
        """Full per-tick management: cancel orphans, place boosts on new fills,
        trail open boosts. Guarded — never raises onto the caller's loop."""
        try:
            self.cancel_orphaned_boosts()
            self.place_boosts_for_new_fills()
            self.trail_open_boosts(current_price)
        except Exception as e:  # pragma: no cover
            self.log.warning(f"rescue_boost: on_tick failed ({e!r}) — continuing")


# --- live broker shim + LiveTrader hook -------------------------------------------
@dataclass
class _P:
    ticket: int
    side: str
    entry: float
    sl: float
    lot: float
    comment: str


class _AdapterBoostBroker:
    """Adapts the AUREON MT5 adapter to the manager's tiny broker interface. Scoped
    to the straddle magic; `is_parent` restricts boosting to genuine anchor straddle
    legs (those carrying an "A:<price>" origin tag), so a rescue-boost position, a
    market-order boost, or a foreign-magic trade is never itself boosted."""

    STRADDLE_MAGIC = 20260522

    def __init__(self, adapter, cfg, paper: bool):
        self.adapter = adapter
        self.mt5 = adapter.mt5
        self.cfg = cfg
        self.symbol = getattr(cfg, "symbol", "XAUUSD")
        self.paper = paper
        self.magic = int(getattr(cfg, "rescue_boost_v2_magic", self.STRADDLE_MAGIC))

    def _ours(self, o):
        return int(getattr(o, "magic", -1) or -1) == self.magic

    def _pside(self, t):
        return "BUY" if t == getattr(self.mt5, "POSITION_TYPE_BUY", 0) else "SELL"

    def _oside(self, t):
        return "BUY" if t in (getattr(self.mt5, "ORDER_TYPE_BUY_STOP", 4),
                              getattr(self.mt5, "ORDER_TYPE_BUY_LIMIT", 2)) else "SELL"

    def positions(self):
        raw = self.mt5.positions_get(symbol=self.symbol) or []
        return [_P(int(p.ticket), self._pside(p.type), float(p.price_open),
                   float(getattr(p, "sl", 0.0) or 0.0), float(p.volume),
                   str(getattr(p, "comment", ""))) for p in raw if self._ours(p)]

    def pendings(self):
        raw = self.mt5.orders_get(symbol=self.symbol) or []
        return [_P(int(o.ticket), self._oside(o.type), float(o.price_open),
                   float(getattr(o, "sl", 0.0) or 0.0),
                   float(getattr(o, "volume_current", getattr(o, "volume_initial", 0.0))),
                   str(getattr(o, "comment", ""))) for o in raw if self._ours(o)]

    def place_pending(self, side, price, sl, lot, comment):
        res = self.adapter.place_stop_order(self.symbol, side, price, lot,
                                            sl=sl, tp=0.0, comment=comment,
                                            dry_run=self.paper)
        if res is None:
            return None
        for attr in ("order", "ticket"):
            v = getattr(res, attr, None)
            if v:
                return int(v)
        return -1  # placed (paper/reconciled) but no ticket surfaced

    def cancel(self, ticket):
        try:
            self.adapter.cancel_order(ticket, dry_run=self.paper)
            return True
        except Exception:
            return False

    def modify_sl(self, ticket, new_sl):
        try:
            self.adapter.modify_position_sl(ticket, new_sl, dry_run=self.paper)
            return True
        except Exception:
            return False

    def is_parent(self, p) -> bool:
        c = getattr(p, "comment", "") or ""
        # genuine anchor straddle leg: has an "A:<price>" origin tag and is not a boost
        return ("A:" in c) and (not is_boost_comment(c))


def _rescue_boost_v2_tick(self) -> None:
    """LiveTrader method: drive the rescue-boost manager once per tick. Flag-gated on
    cfg.rescue_boost_v2_enabled (default OFF) and fully guarded so it never disturbs
    the main loop. Reads the current mid itself so it needs no loop locals."""
    cfg = getattr(self, "cfg", None)
    if cfg is None or not bool(getattr(cfg, "rescue_boost_v2_enabled", False)):
        return
    try:
        tk = self.adapter.mt5.symbol_info_tick(getattr(cfg, "symbol", "XAUUSD"))
        if tk is None:
            return
        mid = (float(tk.bid) + float(tk.ask)) / 2.0
        broker = _AdapterBoostBroker(self.adapter, cfg, bool(getattr(self, "paper", True)))
        mgr = getattr(self, "_rescue_boost_mgr", None)
        if mgr is None:
            mgr = RescueBoostManager(broker, RescueBoostParams.from_config(cfg),
                                     logger=log, is_parent=broker.is_parent)
            self._rescue_boost_mgr = mgr
        else:
            mgr.broker = broker  # refresh the broker view each tick
        mgr.on_tick(mid)
    except Exception as e:
        log.warning(f"rescue_boost: tick hook failed ({e!r}) — continuing")
