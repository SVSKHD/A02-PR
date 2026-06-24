#!/usr/bin/env python3
"""
AUREON v2 — Multi-anchor anchor-breakout bot for XAUUSD.

Modes
-----
  backtest : Run on a historical M1 CSV. Outputs per-trade CSV + monthly summary.
  paper    : Live data from MT5, no real orders. Logs intended actions.
  live     : Live data from MT5, real orders placed. Requires --i-understand-the-risks.

Usage
-----
  python bot.py backtest --csv XAUUSD_M1.csv --start 2025-05-08 --end 2026-05-06
  python bot.py paper        # MT5 terminal must be running and logged in
  python bot.py live --i-understand-the-risks

v3.0.0: CLI entry + backtest invocation only. Config / strategy / MT5Adapter /
backtest / utils moved to dedicated modules; run_live moved to live_trader.py.
The names below are re-exported so the unchanged CLI and every existing
`from bot import X` call site (watchdog, analysis scripts) keep working.
"""

import argparse, json, logging, os, sys, time

# v3.0.0 split: re-export the public surface bot.py used to own, so external
# scripts (watchdog: setup_logging; analysis: Config/run_backtest; validate/
# test_place: MT5Adapter) and the CLI keep importing from bot unchanged.
from config import Config
from utils import (setup_logging, initial_sl, initial_tp,
                   anchor_datetime_utc, eod_datetime_utc, m5_close_at)
from strategy import Position, update_position_on_bar, realize_pnl_usd
from mt5_adapter import MT5Adapter, _MT5_RETCODE_MAP
from backtest import run_backtest, summarize_backtest

log = logging.getLogger("AUREON")


def main():
    # Load .env if present (no-op if not). Must run BEFORE telemetry import
    # reads env vars in submodules.
    from env_loader import load_env
    load_env()
    from live_trader import run_live  # late import: avoids an
    # import cycle (live_trader imports the split modules, not bot)

    parser = argparse.ArgumentParser(description="AUREON v2 bot — XAUUSD multi-anchor")
    parser.add_argument('mode', choices=['backtest', 'paper', 'live', 'selftest',
                                         'testfire', 'verifyfb', 'rescuestats',
                                         'bescratchscan'])
    parser.add_argument('--csv', help="Path to M1 CSV (backtest mode)")
    parser.add_argument('--start', default='2025-01-01')
    parser.add_argument('--end', default='2026-12-31')
    parser.add_argument('--output-dir', default='./output')
    parser.add_argument('--lot', type=float, default=None)
    parser.add_argument('--balance', type=float, default=None)
    parser.add_argument('--no-oco', action='store_true', help="No-OCO: keep sibling live, reversal can 2nd-fill")

    parser.add_argument('--i-understand-the-risks', action='store_true',
                        help="Required for live mode")
    parser.add_argument('--force', action='store_true',
                        help="selftest: allow market-order steps on a non-demo account")
    parser.add_argument('--anchor', default='A2',
                        help="testfire: anchor label for journal tagging / defer-window "
                             "(price is current market, NOT the scheduled anchor price)")
    parser.add_argument('--backfill', metavar='YYYY-MM-DD', default=None,
                        help="verifyfb: re-write ONE day's Firestore doc from the journal CSV")
    parser.add_argument('--m1csv', default=None,
                        help="bescratchscan: M1 price CSV to replay from (else uses run/price_log)")
    parser.add_argument('--horizon', type=int, default=30,
                        help="bescratchscan: post-exit lookforward minutes (default 30)")
    parser.add_argument('--run-dir', default=None,
                        help="bescratchscan: run dir holding journal/ + price_log/ (default $AUREON_RUN_DIR or ./run)")
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args()

    global log
    log = setup_logging(args.log_level)

    cfg = Config()
    if args.no_oco: cfg.no_oco = True

    if args.lot is not None: cfg.lot_size = args.lot
    if args.balance is not None: cfg.starting_balance = args.balance

    if args.mode == 'backtest':
        if not args.csv:
            log.error("Backtest mode requires --csv");
            sys.exit(1)
        cfg.min_step = 0.0  # clean math in backtest
        os.makedirs(args.output_dir, exist_ok=True)
        df = run_backtest(args.csv, args.start, args.end, cfg)
        if len(df) == 0:
            log.warning("No trades produced. Check CSV and date range.")
            return
        stats = summarize_backtest(df, cfg)
        log.info(f"\n{'=' * 60}\nBACKTEST SUMMARY\n{'=' * 60}")
        for k, v in stats.items():
            if k == 'monthly_pnl': continue
            log.info(f"  {k:20s} = {v}")
        log.info("\nMonthly P&L:")
        for m, p in stats['monthly_pnl'].items():
            log.info(f"  {m}  ${p:>10,.2f}")
        # Save outputs
        trades_path = os.path.join(args.output_dir, 'trades.csv')
        stats_path = os.path.join(args.output_dir, 'stats.json')
        df.to_csv(trades_path, index=False)
        with open(stats_path, 'w') as f:
            json.dump(stats, f, indent=2)
        log.info(f"\nWrote {trades_path} and {stats_path}")

    elif args.mode == 'paper':
        run_live(cfg, paper=True)

    elif args.mode == 'live':
        if not args.i_understand_the_risks:
            log.error("Live mode requires --i-understand-the-risks flag. Real money at stake. "
                      "Re-read AUREON_V2_SPEC.md §4 (Risk Management) and §5 (Live Adjustments) first.")
            sys.exit(1)
        run_live(cfg, paper=False)

    elif args.mode == 'selftest':
        # On-demand placement + rescue/boost self-test against the connected MT5
        # demo terminal. Runs ONLY here (never from the live loop / a timer);
        # refuses to run unless the book is flat. Proves the boost path places at
        # rc=10009 in ~2 minutes instead of waiting for a real live rescue.
        from selftest import run_selftest
        ok = run_selftest(cfg, force=args.force)
        sys.exit(0 if ok else 1)

    elif args.mode == 'testfire':
        # v3.2.9: manual ONE-anchor entry at current market, on demand. Fires the
        # EXACT scheduled placement path (straddle off current mid, $18 SL / $30 TP,
        # No-OCO, rally(+5)/rescue(-10) boosts) then hands off to the live management
        # loop. Fail-closed safety rails (DEMO only, no FP profile, flat book, no
        # scheduled-anchor collision, one at a time) — see testfire.py. Real orders.
        from testfire import run_testfire
        ok = run_testfire(cfg, anchor=args.anchor)
        sys.exit(0 if ok else 1)

    elif args.mode == 'verifyfb':
        # Firebase backfill verifier. Read-only by default (lists docs, names
        # MISSING trading days vs the local journal CSVs); --backfill <date>
        # re-writes ONE day idempotently. Fail-safe: unreachable Firestore -> exit
        # 0, never touches trading. Safe to run live while flat (read-only path).
        from verify_firebase import run_verifyfb
        sys.exit(run_verifyfb(backfill=args.backfill))

    elif args.mode == 'rescuestats':
        # Read-only: print the running crash-vs-whipsaw tally + per-event table
        # from rescue_events.csv. Never touches the broker or trading.
        from rescue_log import run_rescuestats
        sys.exit(run_rescuestats())

    elif args.mode == 'bescratchscan':
        # READ-ONLY measurement: quantify the +$2.5->BE rung's "left on table"
        # cost and replay looser rungs over recorded trades. No live change.
        from bescratch import run_bescratchscan
        sys.exit(run_bescratchscan(
            start=args.start, end=args.end, run_dir=args.run_dir,
            m1csv=args.m1csv, horizon_min=args.horizon))


if __name__ == '__main__':
    main()
