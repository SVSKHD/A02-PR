"""AUREON — backtest engine (split from bot.py, v3.0.0). Byte-identical."""
import json
import logging
import os
from datetime import date as DateType
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import Config
from strategy import Position, update_position_on_bar, realize_pnl_usd
from utils import initial_sl, initial_tp, anchor_datetime_utc, eod_datetime_utc, m5_close_at

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
