"""Rogue T2 Continuation V1 — the driver loop.

Ties the pure engine (decisions) to the broker (execution), statestore (durable
idempotency + halt), and notifier. Designed to be stepped one tick at a time so the
tests can drive it deterministically. The broker is an injected dependency: MT5Broker
in production, a price-driven SimBroker in tests — both expose the same interface.

Cycle model (frozen spec):
  arm  -> OCO buy-stop/sell-stop at A1±17 (each SL entry∓2.60)
  T1   -> first OCO fill; cancel the sibling; T1 trails
  T2   -> continuation stop at T1 fill ±12; survives T1's exit; cancelled at phase end
  re-arm when flat AND no resting pending (unlimited per phase); never a 3rd position
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from . import engine as E
from .config import RogueT2Config

log = logging.getLogger("ROGUE_T2")

TAG_BUY = "A1B"
TAG_SELL = "A1S"
TAG_T2 = "T2"


def _role(tag: str) -> Optional[str]:
    for r in (TAG_T2, TAG_BUY, TAG_SELL):
        if f"#{r}" in tag or tag.endswith(r):
            return r
    return None


@dataclass
class CycleState:
    cycle: int = 0
    phase_key: str = ""
    a1: float = 0.0
    t1_side: Optional[str] = None
    t1_entry: float = 0.0
    t1_filled: bool = False
    t2_placed: bool = False
    peaks: Dict[int, float] = field(default_factory=dict)  # ticket -> peak price
    tracked: Dict[int, dict] = field(default_factory=dict)  # ticket -> {side,entry,tag}


class RogueT2Bot:
    def __init__(self, cfg: RogueT2Config, broker, store, notifier,
                 history_window):
        cfg.validate()
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.notify = notifier
        # history_window(ist_day) -> (from_ts, to_ts) for realized-PnL queries
        self._history_window = history_window
        self.cs = CycleState()
        self.last_phase: Optional[int] = self.store.get("last_phase", None)
        self.max_spread_seen = 0.0
        self._guard_muted_until_msc = 0

    # --- startup -----------------------------------------------------------------
    def startup(self) -> None:
        self.broker.startup_assertions()
        self.reconcile()

    def reconcile(self) -> None:
        """Adopt own-magic broker state after a (re)start. Rebuild the live cycle
        from resting pendings + open positions so we never duplicate a T1/T2 that
        already exists, and restore t1_filled/t2_placed from persisted state."""
        self.cs.cycle = int(self.store.get("cur_cycle", 0))
        self.cs.phase_key = self.store.get("cur_phase_key", "")
        self.cs.t1_filled = bool(self.store.get("t1_filled", False))
        self.cs.t1_side = self.store.get("t1_side", None)
        self.cs.t1_entry = float(self.store.get("t1_entry", 0.0) or 0.0)
        self.cs.t2_placed = bool(self.store.get("t2_placed", False))
        for p in self.broker.own_positions():
            self.cs.tracked[p.ticket] = {"side": p.side, "entry": p.entry, "tag": p.tag}
            self.cs.peaks[p.ticket] = p.entry
            r = _role(p.tag)
            if r in (TAG_BUY, TAG_SELL):
                self.cs.t1_filled = True
                self.cs.t1_side = p.side
                self.cs.t1_entry = p.entry
            if self.notify:
                self.notify.reconcile("position", p.ticket)
        for o in self.broker.own_pendings():
            if _role(o.tag) == TAG_T2:
                self.cs.t2_placed = True
            if self.notify:
                self.notify.reconcile("pending", o.ticket)
        self._persist_cycle()

    # --- persistence -------------------------------------------------------------
    def _persist_cycle(self) -> None:
        self.store.set("cur_cycle", self.cs.cycle)
        self.store.set("cur_phase_key", self.cs.phase_key)
        self.store.set("t1_filled", self.cs.t1_filled)
        self.store.set("t1_side", self.cs.t1_side)
        self.store.set("t1_entry", self.cs.t1_entry)
        self.store.set("t2_placed", self.cs.t2_placed)
        self.store.set("last_phase", self.last_phase)

    # --- main step ---------------------------------------------------------------
    def on_tick(self, tick, ist_now: datetime) -> None:
        cfg = self.cfg
        ist_day = ist_now.strftime("%Y-%m-%d")
        self.store.clear_halt_if_new_day(ist_day)

        phase = E.resolve_phase(cfg, ist_now)

        # Phase boundary (including session close): flatten + cancel own, reset cycle.
        if phase != self.last_phase:
            self._on_phase_boundary(phase, ist_now)
            self.last_phase = phase
            self._persist_cycle()

        if phase is None:
            return  # outside session — stay flat

        if self.store.is_halted(ist_day):
            return  # halted for the day (survives restart)

        # book any broker-side exits (SL/trail closes) before cap + management
        self._reconcile_exits(ist_now)

        # daily cap: realized (own-magic deal history incl fees) + unrealized
        realized = self._realized_today(ist_day)
        unreal = self._unrealized(tick)
        if E.cap_breached(cfg, realized, unreal):
            self._halt(ist_day, realized + unreal)
            return

        # guards affect ENTRY only; management still runs
        entry_ok, guard_reason = self._entry_guards(tick)

        # detect OCO fill, cancel sibling, arm T2, trail
        self._manage(tick, phase, ist_now)

        # (re-)arm when flat and nothing resting
        if entry_ok:
            self._maybe_arm(tick, phase, ist_now)
        elif guard_reason:
            self._guard_trip(guard_reason, tick)

        self._persist_cycle()

    # --- phase boundary ----------------------------------------------------------
    def _on_phase_boundary(self, new_phase: Optional[int], ist_now: datetime) -> None:
        # flatten + cancel are MAGIC-FILTERED inside the broker
        self.broker.cancel_own_pendings()
        self.broker.flatten_own()
        # daily summary at the 22:00 close (leaving the session)
        if new_phase is None and self.last_phase is not None:
            self._daily_summary(ist_now)
        self.cs = CycleState()
        if new_phase is not None and self.notify:
            self.notify.phase_start(new_phase, a1="pending")

    # --- fills / management ------------------------------------------------------
    def _manage(self, tick, phase: int, ist_now: datetime) -> None:
        positions = {p.ticket: p for p in self.broker.own_positions()}
        pendings = self.broker.own_pendings()

        # track new positions; detect T1 fill
        for tk, p in positions.items():
            if tk not in self.cs.tracked:
                self.cs.tracked[tk] = {"side": p.side, "entry": p.entry, "tag": p.tag}
                self.cs.peaks[tk] = p.entry
                r = _role(p.tag)
                if r in (TAG_BUY, TAG_SELL):
                    self.cs.t1_filled = True
                    self.cs.t1_side = p.side
                    self.cs.t1_entry = p.entry
                if self.notify:
                    self.notify.fill(r or "?", p.side, p.entry, p.lot)

        # OCO: once T1 has filled, cancel any resting A1 sibling
        if self.cs.t1_filled:
            for o in pendings:
                if _role(o.tag) in (TAG_BUY, TAG_SELL):
                    self.broker.cancel(o.ticket)

        # arm T2 exactly once per cycle after T1 fills (survives T1 exit)
        if self.cs.t1_filled and not self.cs.t2_placed:
            key = E.idempotency_key(self.cs.phase_key, self.cs.cycle, TAG_T2)
            if not self.store.was_placed(key):
                side, trigger, sl = E.t2_plan(self.cfg, self.cs.t1_side, self.cs.t1_entry)
                tk = self.broker.place_pending(side, trigger, sl, self._tag(TAG_T2))
                if tk is not None:
                    self.store.mark_placed(key, tk)
            self.cs.t2_placed = True

        # trailing (server-side SL modifications) on every open position
        mid = tick.mid
        for tk, p in positions.items():
            side = p.side
            peak = self.cs.peaks.get(tk, p.entry)
            extreme = max(peak, mid) if side == "BUY" else min(peak, mid)
            self.cs.peaks[tk] = extreme
            new_sl = E.update_trail(side, p.entry, extreme, p.sl, self.cfg)
            if abs(new_sl - p.sl) > 1e-9:
                self.broker.modify_sl(tk, new_sl)

    def _maybe_arm(self, tick, phase: int, ist_now: datetime) -> None:
        if self.broker.own_positions():
            return  # not flat
        if self.broker.own_pendings():
            return  # something resting (A1 OCO or T2) — do not re-arm
        # flat & nothing resting -> fresh cycle
        self.cs = CycleState()
        self.cs.cycle = int(self.store.get("cur_cycle", 0)) + 1
        self.cs.phase_key = E.phase_key(ist_now, phase)
        a1 = tick.mid
        self.cs.a1 = a1
        plan = E.oco_plan(self.cfg, a1)
        kb = E.idempotency_key(self.cs.phase_key, self.cs.cycle, TAG_BUY)
        ks = E.idempotency_key(self.cs.phase_key, self.cs.cycle, TAG_SELL)
        tb = self.broker.place_pending("BUY", plan.buy_stop, plan.buy_sl, self._tag(TAG_BUY))
        ts = self.broker.place_pending("SELL", plan.sell_stop, plan.sell_sl, self._tag(TAG_SELL))
        if tb is not None:
            self.store.mark_placed(kb, tb)
        if ts is not None:
            self.store.mark_placed(ks, ts)
        if self.notify:
            self.notify.phase_start(phase, round(a1, 2))

    def _tag(self, role: str) -> str:
        return f"{self.cs.phase_key}#C{self.cs.cycle}#{role}"

    # --- exits / pnl -------------------------------------------------------------
    def _reconcile_exits(self, ist_now: datetime) -> None:
        live = {p.ticket for p in self.broker.own_positions()}
        for tk in list(self.cs.tracked.keys()):
            if tk in live:
                continue
            info = self.cs.tracked.pop(tk)
            self.cs.peaks.pop(tk, None)
            deal = None
            if hasattr(self.broker, "closed_deal"):
                deal = self.broker.closed_deal(tk)
            if deal and self.notify:
                self.notify.exit(_role(info["tag"]) or "?", info["side"],
                                 deal.get("exit_price", 0.0),
                                 deal.get("slippage", 0.0), deal.get("pnl", 0.0))

    def _realized_today(self, ist_day: str) -> float:
        frm, to = self._history_window(ist_day)
        try:
            return self.broker.day_realized_usd(frm, to)
        except Exception as e:
            log.warning(f"realized pnl query failed: {e!r}")
            return 0.0

    def _unrealized(self, tick) -> float:
        total = 0.0
        for p in self.broker.own_positions():
            total += E.unrealized_usd(p.side, p.entry, tick.mid, p.lot, self.cfg.contract_size)
        return total

    # --- guards / halt / summary -------------------------------------------------
    def _entry_guards(self, tick):
        self.max_spread_seen = max(self.max_spread_seen, tick.spread)
        if tick.spread > self.cfg.max_spread:
            return False, f"spread {tick.spread:.2f} > {self.cfg.max_spread}"
        # tick age is enforced by the driver that supplies fresh ticks; a stale
        # tick surfaces as age via the broker layer. Here we trust the supplied tick.
        return True, None

    def _guard_trip(self, reason: str, tick) -> None:
        if tick.time_msc < self._guard_muted_until_msc:
            return  # throttle repeated guard alerts
        self._guard_muted_until_msc = tick.time_msc + 30_000
        log.warning(f"guard trip: {reason}")
        if self.notify:
            self.notify.guard("entry_blocked", reason)

    def _halt(self, ist_day: str, day_pnl: float) -> None:
        self.broker.cancel_own_pendings()
        self.broker.flatten_own()
        self.store.set_halt(ist_day, f"daily_cap {self.cfg.daily_cap_usd():.2f}")
        self.cs = CycleState()
        if self.notify:
            self.notify.halt("daily_cap", day_pnl)

    def _daily_summary(self, ist_now: datetime) -> None:
        ist_day = ist_now.strftime("%Y-%m-%d")
        day_pnl = self._realized_today(ist_day)
        stats = self.store.get("day_stats", {"trades": 0, "wins": 0})
        trades = int(stats.get("trades", 0))
        wins = int(stats.get("wins", 0))
        win_rate = (wins / trades) if trades else 0.0
        if self.notify:
            self.notify.daily_summary(trades, win_rate, day_pnl, self.max_spread_seen)
        self.max_spread_seen = 0.0
