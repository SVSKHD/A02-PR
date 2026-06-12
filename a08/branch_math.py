"""
AUREON A08 — netting-adapted fleet BRANCH MATH (first-task deliverable).

Prints the rupee outcome of each fleet branch at a chosen instrument, lot count,
and R, so the numbers can be confirmed before any code trades. R is the live
ratio (MCX_quote / XAUUSD); pass --R, or --mcx and --xau to compute it.

Branches (netting reality -- positions NET per contract):
  CLEAN     first leg fills, runs to TP                        -> +tp
  CLEAN_SL  first leg fills, stops at initial SL               -> -sl
  CRASH     sibling triggers (trapped closes ~ -sibling_close),
            rescue + boosts ride and all hit TP                -> -sibling_close + fleet*tp
  WHIPSAW   sibling triggers, then rescue + boosts all stop    -> -sibling_close - fleet*boost_sl

Run:
  python -m a08.branch_math --instrument GOLDM --lots 1 --mcx 98000 --xau 3300
"""
from __future__ import annotations

import argparse

from .config import load_config
from .conversion import compute_R, convert_all


def render(cfg, R: float) -> str:
    dist = convert_all(cfg, R)
    inst = cfg.inst()
    L = cfg.lots
    p = lambda d: dist.pnl_inr(d, L)  # noqa: E731  rupee P&L for a rupee distance

    fleet_legs = 1 + (cfg.rescue_boost_count if cfg.rescue_boost_enabled else 0)

    clean_tp = p(dist.tp)
    clean_sl = -p(dist.sl)
    crash = -p(dist.sibling_close) + fleet_legs * p(dist.tp)
    whipsaw = -p(dist.sibling_close) - fleet_legs * p(dist.boost_sl)
    kill = -cfg.daily_loss_pct * cfg.starting_capital_inr

    lines = [
        f"AUREON A08 — netting-adapted fleet branch math",
        f"  instrument   {cfg.instrument} (lot {inst.lot_grams:g}g, "
        f"Rs{inst.value_per_point_inr:g}/point/lot)",
        f"  lots         {L}",
        f"  R            {R:.4f}  (Rs per $ of XAU move, per quote)",
        f"  capital      Rs{cfg.starting_capital_inr:,.0f}   "
        f"kill switch  Rs{kill:,.0f}  (-{cfg.daily_loss_pct:.0%})",
        "",
        f"  distances (Rs): trigger {dist.trigger:g}  SL {dist.sl:g}  TP {dist.tp:g}  "
        f"BE {dist.be:g}  lock4 {dist.lock4_lock:g}  tier10 {dist.tier10:g}",
        f"                  trail_gap {dist.trail_gap:g}  tstop {dist.tstop_fav:g}  "
        f"boost_sl {dist.boost_sl:g}  sibling_close {dist.sibling_close:g}",
        "",
        f"  BRANCH                         net P&L (Rs)",
        f"  CLEAN    (1 leg -> TP)         {clean_tp:>+12,.0f}",
        f"  CLEAN_SL (1 leg -> SL)         {clean_sl:>+12,.0f}",
        f"  CRASH    (trapped + {fleet_legs} -> TP)   {crash:>+12,.0f}",
        f"  WHIPSAW  (trapped + {fleet_legs} stops)   {whipsaw:>+12,.0f}",
        "",
        f"  kill-switch guard: WHIPSAW ({whipsaw:+,.0f}) must be > kill ({kill:,.0f})"
        f"  -> {'OK' if whipsaw > kill else 'BREACH — reduce lots'}",
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="A08 netting fleet branch math")
    ap.add_argument("--instrument", default="GOLDM")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--R", type=float, default=None, help="live ratio MCX/XAU")
    ap.add_argument("--mcx", type=float, default=None, help="MCX quote price")
    ap.add_argument("--xau", type=float, default=None, help="XAUUSD spot")
    args = ap.parse_args(argv)

    cfg = load_config(instrument=args.instrument, lots=args.lots,
                      starting_capital_inr=args.capital)
    if args.R is not None:
        R = args.R
    elif args.mcx is not None and args.xau is not None:
        R = compute_R(args.mcx, args.xau)
    else:
        ap.error("provide --R, or both --mcx and --xau")
        return
    print(render(cfg, R))


if __name__ == "__main__":
    main()
