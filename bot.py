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

See AUREON_V2_SPEC.md for the full strategy documentation.
"""

import argparse, json, logging, os, sys, time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date as DateType
from typing import Optional, List, Dict, Tuple
import pandas as pd
from config import Config
from strategy import (Position, initial_sl, initial_tp, update_position_on_bar,
                      realize_pnl_usd, anchor_datetime_utc, eod_datetime_utc,
                      m5_close_at)
from mt5_adapter import MT5Adapter, _MT5_RETCODE_MAP


def setup_logging(level: str = "INFO", log_dir: str = "./logs",
                  app_name: str = "aureon"):
    """Set up logging to BOTH stdout and a daily-rotated file in log_dir.

    File naming: logs/aureon_YYYY-MM-DD.log (rotated daily at UTC midnight,
    keeping 30 days of history). All log levels from app modules go in.

    Format includes timestamp, level, module name, and message. Caller can
    grep for specific anchors, errors, or modules later.
    """
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))
    # Clear any pre-existing handlers so basicConfig calls don't double-log
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler (so terminal still shows everything)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Daily-rotated file handler
    from logging.handlers import TimedRotatingFileHandler
    log_file = os.path.join(log_dir, f"{app_name}.log")
    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', interval=1, backupCount=30, utc=True,
        encoding='utf-8'
    )
    file_handler.setFormatter(fmt)
    file_handler.suffix = "%Y-%m-%d"  # so rotated files become aureon.log.2026-05-25
    root.addHandler(file_handler)

    log = logging.getLogger("AUREON")
    log.info(f"Logging to console + {log_file} (daily rotation, 30-day retention)")
    return log


log = logging.getLogger("AUREON")


def run_backtest(csv_path: str, start: str, end: str, cfg: Config) -> pd.DataFrame:
    log.info(f"Loading M1 from {csv_path}")
    m1 = pd.read_csv(csv_path)
    m1['time'] = pd.to_datetime(m1['time'], utc=True)
    m1 = m1.set_index('time').sort_index()[['open', 'high', 'low', 'close']]
    log.info(f"Loaded {len(m1):,} M1 bars from {m1.index.min()} to {m1.index.max()}")

    m5 = m1.resample('5min', label='right', closed='right').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    log.info(f"Resampled to {len(m5):,} M5 bars")

    days = pd.date_range(start, end, freq='B')
    trades_records: List[Dict] = []
    daily_pnl_running: Dict[DateType, float] = {}
    kill_switch_days: List[DateType] = []

    for d in days:
        broker_date = d.date()
        eod_ts = eod_datetime_utc(broker_date, cfg)
        daily_pnl = 0.0
        kill_triggered = False

        for label, broker_hour, broker_minute in cfg.anchors:
            if kill_triggered: break

            at = anchor_datetime_utc(broker_date, broker_hour, cfg.broker_tz_offset_hours, broker_minute)
            if at >= eod_ts: continue
            anchor_price = m5_close_at(m5, at)
            if anchor_price is None: continue

            buy_stop = round(anchor_price + cfg.trigger_dist, 2)
            sell_stop = round(anchor_price - cfg.trigger_dist, 2)
            window = m1.loc[at:eod_ts]
            if len(window) < 3: continue

            # Single-OCO fill scan
            side, fi = None, None
            for i, (ts, bar) in enumerate(window.iterrows()):
                b_hit = bar.high >= buy_stop
                s_hit = bar.low <= sell_stop
                if b_hit and s_hit:
                    side = 'SELL' if bar.close >= bar.open else 'BUY'
                    fi = i;
                    break
                elif b_hit:
                    side = 'BUY';
                    fi = i;
                    break
                elif s_hit:
                    side = 'SELL';
                    fi = i;
                    break

            if side is None: continue

            entry_price = buy_stop if side == 'BUY' else sell_stop
            entry_time = window.index[fi]

            pos = Position(
                anchor_label=label,
                side=side,
                entry_price=entry_price,
                entry_time=entry_time,
                current_sl=initial_sl(side, entry_price, cfg),
                tp_level=initial_tp(side, entry_price, cfg),
                max_fav=entry_price,
                lot=cfg.lot_size,
            )

            # Walk forward from next bar
            walk = window.iloc[fi + 1:]
            for ts, bar in walk.iterrows():
                outcome = update_position_on_bar(pos, bar, ts, cfg)
                if outcome:
                    break
            if not pos.closed:
                last = walk.iloc[-1]
                pos.exit_price = float(last.close)
                pos.exit_time = walk.index[-1]
                pos.outcome = 'EOD'
                pos.closed = True

            usd = realize_pnl_usd(pos, cfg)
            daily_pnl += usd
            trades_records.append({
                'date': str(broker_date),
                'anchor': pos.anchor_label,
                'side': pos.side,
                'entry_time': str(pos.entry_time),
                'entry': pos.entry_price,
                'exit_time': str(pos.exit_time),
                'exit': pos.exit_price,
                'max_favorable': round(pos.max_fav, 2),
                'outcome': pos.outcome,
                'pnl_dist': round(pos.pnl_dist, 3),
                'pnl_usd': round(usd, 2),
                'lot': pos.lot,
            })

            # Daily kill switch check
            if daily_pnl <= -cfg.daily_loss_pct * cfg.starting_balance:
                log.warning(f"KILL SWITCH triggered on {broker_date}: daily P&L ${daily_pnl:.2f}")
                kill_triggered = True
                kill_switch_days.append(broker_date)
                break

        daily_pnl_running[broker_date] = daily_pnl

    df = pd.DataFrame(trades_records)
    if len(df):
        df['date'] = pd.to_datetime(df['date'])
        log.info(f"Backtest complete: {len(df)} trades, ${df['pnl_usd'].sum():,.2f} P&L, "
                 f"{kill_switch_days and len(kill_switch_days) or 0} kill-switch days")
    return df


def summarize_backtest(df: pd.DataFrame, cfg: Config) -> Dict:
    if len(df) == 0:
        return {'fills': 0, 'total_usd': 0, 'total_pips': 0}

    daily = df.groupby(df['date'].dt.date)['pnl_usd'].sum()
    monthly = df.groupby(df['date'].dt.to_period('M'))['pnl_usd'].sum()
    eq = df['pnl_usd'].cumsum()
    dd = (eq - eq.cummax()).min()

    return {
        'fills': len(df),
        'total_pips': round(df['pnl_dist'].sum(), 2),
        'total_usd': round(df['pnl_usd'].sum(), 2),
        'win_rate': round(100 * (df['pnl_usd'] > 0).mean(), 2),
        'max_dd': round(dd, 2),
        'max_dd_pct': round(100 * dd / cfg.starting_balance, 2),
        'sl_count': int((df['outcome'] == 'SL').sum()),
        'tp_count': int((df['outcome'] == 'TP').sum()),
        'worst_day': round(daily.min(), 2),
        'best_day': round(daily.max(), 2),
        'kill_days': int((daily <= -cfg.daily_loss_pct * cfg.starting_balance).sum()),
        'months': len(monthly),
        'avg_per_month_usd': round(monthly.mean(), 2),
        'avg_per_month_pips': round(df['pnl_dist'].sum() / len(monthly), 2),
        'monthly_pnl': {str(k): round(v, 2) for k, v in monthly.items()},
    }


def run_live(cfg: Config, paper: bool = True):
    """
    Live or paper trading. Connects to the already-running MT5 terminal
    on this machine (which must be logged into your broker account first).
    Delegates to LiveTrader (live_trader.py) for the full event loop.
    """
    from live_trader import LiveTrader
    adapter = MT5Adapter()
    try:
        trader = LiveTrader(cfg, adapter, paper=paper)
        trader.run()
    finally:
        adapter.shutdown()


def main():
    # Load .env if present (no-op if not). Must run BEFORE telemetry import
    # reads env vars in submodules.
    from env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(description="AUREON v2 bot — XAUUSD multi-anchor")
    parser.add_argument('mode', choices=['backtest', 'paper', 'live'])
    parser.add_argument('--csv', help="Path to M1 CSV (backtest mode)")
    parser.add_argument('--start', default='2025-01-01')
    parser.add_argument('--end', default='2026-12-31')
    parser.add_argument('--output-dir', default='./output')
    parser.add_argument('--lot', type=float, default=None)
    parser.add_argument('--balance', type=float, default=None)
    parser.add_argument('--no-oco', action='store_true', help="No-OCO: keep sibling live, reversal can 2nd-fill")

    parser.add_argument('--i-understand-the-risks', action='store_true',
                        help="Required for live mode")
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


if __name__ == '__main__':
    main()
