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


def _boot_watchdog_ok(cfg, args):
    """Boot gate: run the watchdog wiring validator BEFORE any trading starts. Default
    ON; --skip-validator bypasses (manual debug only). Returns True to proceed, False to
    abort (caller exits non-zero). A wiring failure means the bot must NOT trade."""
    try:
        from aureon_validator import run_boot_validation
        return run_boot_validation(cfg, skip=bool(getattr(args, 'skip_validator', False)))
    except Exception as e:
        # the validator itself failing to run is a wiring failure -> DO-NOT-START.
        log.error(f"🛑 watchdog could not run ({e!r}) — DO-NOT-START (the bot will NOT trade).")
        return False


def main():
    # Load .env if present (no-op if not). Must run BEFORE telemetry import
    # reads env vars in submodules.
    from env_loader import load_env
    load_env()
    from live_trader import run_live  # late import: avoids an
    # import cycle (live_trader imports the split modules, not bot)

    parser = argparse.ArgumentParser(description="AUREON v2 bot — XAUUSD multi-anchor")
    parser.add_argument('mode', choices=['backtest', 'paper', 'live', 'selftest',
                                         'testfire', 'testorder', 'verifyfb', 'rescuestats',
                                         'bescratchscan', 'rogueseed', 'fetchseed',
                                         'dailyreport', 'reconcile', 'fetchticks',
                                         'simulate', 'review'])
    parser.add_argument('--i-know-this-is-real', action='store_true',
                        help="testorder: allow running against a non-demo account")
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
    parser.add_argument('--skip-validator', action='store_true',
                        help="boot: bypass the watchdog wiring validator (MANUAL DEBUG "
                             "ONLY). Default is to validate; the bot will NOT trade on a "
                             "wiring failure.")
    parser.add_argument('--anchor', default='A2',
                        help="testfire: anchor label for journal tagging / defer-window "
                             "(price is current market, NOT the scheduled anchor price)")
    parser.add_argument('--force-window', action='store_true',
                        help="testfire: bypass ONLY rail 4 (the 30-min scheduled-anchor "
                             "collision guard) to fire off-schedule. Rails 1/2/3/5 "
                             "(DEMO-only, NO-FP, FLAT-BOOK, ONE-AT-A-TIME) stay HARD. "
                             "Loud warning is printed; scheduler stays suppressed.")
    parser.add_argument('--backfill', metavar='YYYY-MM-DD', default=None,
                        help="verifyfb: re-write ONE day's Firestore doc from the journal CSV")
    parser.add_argument('--m1csv', default=None,
                        help="bescratchscan: M1 price CSV to replay from (else uses run/price_log)")
    parser.add_argument('--horizon', type=int, default=30,
                        help="bescratchscan: post-exit lookforward minutes (default 30)")
    parser.add_argument('--run-dir', default=None,
                        help="bescratchscan: run dir holding journal/ + price_log/ (default $AUREON_RUN_DIR or ./run)")
    parser.add_argument('--date', default=None, metavar='YYYY-MM-DD|YYYY-MM',
                        help="dailyreport: the day (or whole month) to report on; "
                             "reconcile: the broker day to audit. Default: today.")
    parser.add_argument('--from', dest='date_from', default=None, metavar='YYYY-MM-DD',
                        help="fetchticks: first calendar day of the tick-cache range")
    parser.add_argument('--to', dest='date_to', default=None, metavar='YYYY-MM-DD',
                        help="fetchticks: last calendar day of the tick-cache range "
                             "(--force refetches days already on disk)")
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
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2)
        log.info(f"\nWrote {trades_path} and {stats_path}")

    elif args.mode == 'paper':
        if not _boot_watchdog_ok(cfg, args):
            sys.exit(2)
        run_live(cfg, paper=True)

    elif args.mode == 'live':
        if not args.i_understand_the_risks:
            log.error("Live mode requires --i-understand-the-risks flag. Real money at stake. "
                      "Re-read AUREON_V2_SPEC.md §4 (Risk Management) and §5 (Live Adjustments) first.")
            sys.exit(1)
        if not _boot_watchdog_ok(cfg, args):
            sys.exit(2)
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
        ok = run_testfire(cfg, anchor=args.anchor, force_window=args.force_window)
        sys.exit(0 if ok else 1)

    elif args.mode == 'testorder':
        # Risk-free LIVE ORDER-PATH verification (distinct from `testfire`, which
        # fires a real strategy anchor). Places/modifies/cancels a far pending +
        # opens/SL-modifies/closes a 0.01 market position, all magic 20260817 /
        # "TESTORDER" (exempt from stale_leg_sweep + invisible to rescue/rogue).
        # DEMO only (override: --i-know-this-is-real); refuses under a live PID lock.
        # See testorder.py.
        from mt5_adapter import MT5Adapter
        from testorder import run_testorder
        _adapter = MT5Adapter(getattr(cfg, 'symbol', 'XAUUSD'),
                              expected_offset_hours=getattr(cfg, 'EXPECTED_BROKER_OFFSET_HOURS', None))
        try:
            code = run_testorder(cfg, _adapter, allow_real=args.i_know_this_is_real)
        finally:
            try:
                _adapter.shutdown()
            except Exception:
                pass
        sys.exit(code)

    elif args.mode == 'review':
        # Post/print today's decision-grade session-review digest (fills, closes by
        # reason, net by engine, locks armed/fired/fallback, rejects) from
        # logs/review_YYYY-MM-DD.log. No MT5 needed. Also callable from the Discord
        # /review command via review_log.post_review_digest. See review_log.py.
        from review_log import post_review_digest
        try:
            from telemetry import telemetry_from_env
            _tele = telemetry_from_env(component="AUREON-review")
        except Exception:
            _tele = None
        text = post_review_digest(cfg, _tele, day=(args.start if args.start else None))
        print(text)
        try:
            if _tele is not None:
                _tele.stop(timeout=6.0)
        except Exception:
            pass
        sys.exit(0)

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

    elif args.mode == 'dailyreport':
        # READ-ONLY: per-engine/per-anchor daily P&L report from MT5 history deals +
        # rescue_events.csv + the journal CSV + aureon.log. Never touches the broker's
        # order book (opens MT5 for HISTORY reads only). A YYYY-MM-DD date runs one
        # day; YYYY-MM runs the whole month (each day + a month roll-up).
        from pnl_report import run_dailyreport
        sys.exit(run_dailyreport(date_arg=args.date))

    elif args.mode == 'reconcile':
        # READ-ONLY P&L RECONCILE AUDIT: for --date (broker day; default today) compute the
        # per-engine + account net from all four surfaces (MT5 history / pnl_ledger.csv / the
        # daily report / the live stops source) and print a table. Exit 0 iff every surface
        # agrees with MT5 deal history within $0.01; exit 1 on any mismatch. Opens MT5 for
        # HISTORY reads only -- never touches the order book.
        from pnl_reconcile import run_cli
        sys.exit(run_cli(date_arg=args.date))

    elif args.mode == 'fetchticks':
        # Offline-simulator TICK CACHE (Part 1A): cache each calendar day in
        # [--from, --to] to backtest/ticks/<symbol>_<date>.parquet + a manifest
        # recording the resolution ACTUALLY obtained per day (tick vs M1). Reads
        # MT5 for HISTORY ticks only (never the order book); writes ONLY under
        # backtest/ticks/, never run/. Needs a live MT5 terminal (VPS) for real
        # ticks; off-VPS it reports 'unavailable' honestly and writes nothing.
        # Load backtest/tick_cache.py by ABSOLUTE FILE PATH: the repo root has both
        # backtest.py (module) and backtest/ (dir), so `import backtest.tick_cache`
        # is ambiguous -- mirror back_main.py's file-path load.
        import importlib.util as _ilu
        _tc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'backtest', 'tick_cache.py')
        _spec = _ilu.spec_from_file_location('aureon_tick_cache', _tc_path)
        _tc = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_tc)
        sys.exit(_tc.run_cli(args.date_from, args.date_to, symbol=cfg.symbol,
                             broker_tz_offset_hours=cfg.broker_tz_offset_hours,
                             force=args.force))

    elif args.mode == 'simulate':
        # OFFLINE SIMULATOR (Part 1B): replay the cached ticks in [--from, --to]
        # through the REAL LiveTrader tick loop behind a fake broker (MT5
        # disconnected). Writes sim/reports/<run-id>/ and runs THE GATE vs the MT5
        # deal-export truth. Every artifact carries the GATE-NOT-RUN header; the
        # gate cannot pass on synthetic/M1 data. Writes ONLY under sim/, never run/.
        import importlib.util as _ilu
        _sm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'backtest', 'simulator.py')
        _spec = _ilu.spec_from_file_location('aureon_simulator', _sm_path)
        _sm = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_sm)
        sys.exit(_sm.run_cli(args.date_from, args.date_to))

    elif args.mode == 'rogueseed':
        # Manual Rogue A1-mode seed: enqueue a 'rogueseed' command onto the RUNNING bot's
        # command channel so the live loop plants the Rogue anchor at ITS current tick
        # (mid-day restart has no A1 event to seed Fix 4). DEMO-only + rogue_a1_anchor_mode-
        # only + ROGUE-only are enforced when the bot handles it. Adds NO trade logic.
        from rogue import enqueue_seed_command
        sys.exit(enqueue_seed_command(cfg))

    elif args.mode == 'fetchseed':
        # Manual Fetcher re-seed: enqueue a 'fetchseed' command so the live loop plants the
        # Fetcher anchor at ITS current tick (deliberate live testing from a known point).
        # DEMO-only + FETCHER-only + the open-ticket/engine-off/market/kill rails are
        # enforced when the bot handles it. Adds NO trade logic.
        from fetcher import enqueue_seed_command as _fetch_enqueue
        sys.exit(_fetch_enqueue(cfg))


if __name__ == '__main__':
    main()
