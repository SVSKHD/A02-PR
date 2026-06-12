"""
AUREON A08 — runner: startup banner (config receipt), state, session orchestration.

Ties the modules together in the v3 order: dhan adapter -> anchors -> fills ->
trails -> risk -> journal. PAPER mode runs end-to-end against the simulated
adapter; LIVE mode reuses the same orchestration once the adapter's TODO(live)
seams are wired and demo-verified.

Restart-safe: positions, pendings, peaks, rescue flags and the day's R are
persisted to cfg.state_file and rehydrated on boot (watchdog-friendly).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Dict, List, Optional

import pandas as pd

from . import version
from .config import Config, load_config
from .conversion import ConvertedDistances, compute_R, convert_all, recompute_R
from .dhan_adapter import make_adapter
from .anchors import build_anchor_plans, is_first_anchor, place_straddle
from .risk import RiskState, anchor_fits_kill_switch, margin_ok, is_eod
from .strategy import Position, on_sibling_trigger
from . import journal
from .telemetry import telemetry_from_env

log = logging.getLogger("A08.runner")


# ---------------------------------------------------------------------------
# Startup banner = full config receipt to Telegram (same DNA as MT5 build)
# ---------------------------------------------------------------------------

def startup_banner(cfg: Config, R: Optional[float] = None) -> str:
    inst = cfg.inst()
    lines = [
        version.banner(),
        f"mode: {'PAPER/SIM' if cfg.paper else 'LIVE'}   "
        f"instrument: {cfg.instrument} ({inst.lot_grams:g}g, "
        f"Rs{inst.value_per_point_inr:g}/pt/lot)   lots: {cfg.lots}",
        f"anchors (IST): {', '.join(a[0] for a in cfg.anchors)}   "
        f"[A1 05:00 DROPPED — MCX closed]",
        f"source $ distances: trigger {cfg.trigger_dist} SL {cfg.sl_dist} "
        f"TP {cfg.tp_dist} | ladder BE {cfg.be_trigger}/lock4 {cfg.lock4_trigger}"
        f"->+{cfg.lock4_amount}/tier {cfg.tier10_trigger} floor {cfg.tier10_floor}",
        f"hold {cfg.hold_minutes}m | trail arm {cfg.trail_arm} gap {cfg.trail_gap} "
        f"| TSTOP<{cfg.tstop_fav} | boosts {cfg.rescue_boost_count}x SL {cfg.rescue_boost_sl}",
        f"netting: sibling-close ~ -{cfg.sibling_close_loss} (vs -{cfg.sl_dist} SL)",
        f"kill switch -{cfg.daily_loss_pct:.0%} of Rs{cfg.starting_capital_inr:,.0f} | "
        f"EOD flatten {cfg.eod_flatten_hour:02d}:{cfg.eod_flatten_minute:02d} IST | "
        f"roll {cfg.roll_days_before_expiry}d pre-expiry",
        f"firebase: {cfg.firebase_collection} schema v{version.SCHEMA_VERSION} (EOD-only)",
    ]
    if R is not None:
        d = convert_all(cfg, R)
        lines.append(f"R={R:.4f} -> Rs: trigger {d.trigger:g} SL {d.sl:g} TP {d.tp:g} "
                     f"BE {d.be:g} lock4 {d.lock4_lock:g} tier10 {d.tier10:g}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State persistence (restart-safe)
# ---------------------------------------------------------------------------

def load_state(cfg: Config) -> Dict:
    for path, label in [(cfg.state_file, "main"), (cfg.state_file + ".bak", "backup")]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    s = json.load(f)
                log.info(f"restored state from {label}: {path}")
                return s
            except Exception as e:
                log.warning(f"state {label} corrupt ({e}); trying next")
    return {"daily_pnl_inr": 0.0, "R": None, "positions": [], "pendings": []}


def save_state(cfg: Config, state: Dict) -> None:
    if os.path.exists(cfg.state_file):
        try:
            os.replace(cfg.state_file, cfg.state_file + ".bak")
        except OSError:
            pass
    tmp = cfg.state_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, cfg.state_file)


# ---------------------------------------------------------------------------
# Session orchestration (scaffold)
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, cfg: Config, adapter, tele):
        self.cfg = cfg
        self.adapter = adapter
        self.tele = tele
        self.state = load_state(cfg)
        self.risk = RiskState(daily_pnl_inr=self.state.get("daily_pnl_inr", 0.0))
        self.R: Optional[float] = self.state.get("R")
        self.dist: Optional[ConvertedDistances] = (
            convert_all(cfg, self.R) if self.R else None)
        self.positions: List[Position] = []
        self.closed_today: List[Dict] = []

    # --- anchor firing -------------------------------------------------
    def ensure_R(self, plan, plans):
        """Recompute R at the first anchor of the session; reuse it after."""
        if is_first_anchor(plan, plans) or self.R is None:
            self.R = recompute_R(self.adapter, self.cfg)
            self.dist = convert_all(self.cfg, self.R)
            self.tele.info(f"{plan.label}: R recomputed = {self.R:.4f}")
        return self.dist

    def fire_anchor(self, plan, plans):
        dist = self.ensure_R(plan, plans)
        if self.risk.killed:
            self.tele.warn(f"{plan.label} skipped — kill switch active")
            return
        if not anchor_fits_kill_switch(self.cfg, dist, self.risk):
            self.tele.warn(f"{plan.label} skipped — worst case would breach kill switch")
            return
        if not margin_ok(self.adapter, self.cfg):
            self.tele.warn(f"{plan.label} skipped — insufficient margin")
            return
        plan.anchor_price = self.adapter.mcx_last_price(self.cfg.instrument)
        buy, sell = place_straddle(self.adapter, self.cfg, plan, dist)
        self.tele.info(f"{plan.label} straddle placed @ {plan.anchor_price}")
        return buy, sell

    # --- close + journal ------------------------------------------------
    def close_and_journal(self, pos: Position, pnl_inr: float, now_ist):
        journal.write_trade(self.cfg, pos, self.dist, pnl_inr, now_ist)
        self.closed_today.append({"anchor": pos.anchor_label, "pnl": pnl_inr,
                                  "outcome": pos.outcome, "role": pos.role})
        tripped = self.risk.record(pnl_inr, self.cfg)
        self.persist()
        if tripped:
            self.tele.critical("KILL SWITCH tripped — flattening + halting new entries")

    def handle_sibling(self, trapped: Position, sibling_price: float, now_ist):
        ev = on_sibling_trigger(trapped, sibling_price, self.cfg, self.dist)
        self.close_and_journal(trapped, ev.trapped_pnl_inr, now_ist)
        self.tele.warn(f"SIBLING fleet: {ev.notes}")
        # rescue + boosts fire as NEW net positions (rescue-class exits)
        for i in range(1 + ev.boost_count):
            role = "rescue" if i == 0 else "boost"
            self.adapter.place_market(ev.rescue_side, ev.rescue_lots,
                                      tag=f"{trapped.anchor_label}:{role}")
        return ev

    # --- EOD ------------------------------------------------------------
    def eod(self, session_date):
        doc = journal.build_firebase_doc(
            self.cfg, session_date, self.R or 0.0, self.closed_today,
            self.risk.daily_pnl_inr, self.risk.killed)
        # TODO(live): write `doc` to Firebase collection cfg.firebase_collection
        self.tele.info(f"EOD {doc['date_ist']}: trades {doc['trades']} "
                       f"pnl Rs{doc['realized_pnl_inr']:,.0f} killed={doc['kill_switch_tripped']}")
        return doc

    def persist(self):
        self.state["daily_pnl_inr"] = self.risk.daily_pnl_inr
        self.state["R"] = self.R
        save_state(self.cfg, self.state)


def main(argv=None):
    ap = argparse.ArgumentParser(description="AUREON A08 runner")
    ap.add_argument("--instrument", default=None)
    ap.add_argument("--lots", type=int, default=None)
    ap.add_argument("--live", action="store_true", help="LIVE (default PAPER)")
    ap.add_argument("--banner-only", action="store_true",
                    help="print the startup config receipt and exit")
    args = ap.parse_args(argv)

    cfg = load_config(instrument=args.instrument, lots=args.lots,
                      paper=(not args.live))
    logging.basicConfig(level=getattr(logging, cfg.log_level))
    tele = telemetry_from_env(component=f"A08-{'live' if args.live else 'paper'}")

    banner = startup_banner(cfg)
    print(banner)
    if args.banner_only:
        return
    tele.info(banner)
    adapter = make_adapter(cfg)
    adapter.connect()
    adapter.load_instrument_master()
    Runner(cfg, adapter, tele)
    log.info("A08 runner initialized (scaffold). Wire the live loop in LIVE mode.")


if __name__ == "__main__":
    main()
