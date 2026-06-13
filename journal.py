"""AUREON — trade journal: _write_journal (19-col) + daily/today summaries.

Split out of live_trader.py in v3.0.0. These are the verbatim LiveTrader
methods (bodies byte-identical, dedented one level); they take `self` and
are bound back onto LiveTrader in live_trader.py. Behavior-frozen (except
the commit-1 fixes already in the fill path).
"""
import csv
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import date as DateType, timedelta, datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from telemetry import telemetry_from_env, Severity
from mt5_adapter import _MT5_RETCODE_MAP

log = logging.getLogger("AUREON")


def _write_journal(self, shadow, close_deal, close_price, outcome, pnl_usd, ticket):
    import os as _os
    jdir = _os.path.join(self.run_dir, "journal")
    _os.makedirs(jdir, exist_ok=True)
    now_ist = pd.Timestamp.now(tz='Asia/Kolkata')
    month = now_ist.strftime('%Y-%m')
    jpath = _os.path.join(jdir, f"trades_{month}.csv")

    side = shadow['side']
    entry = float(shadow['entry_price'])
    max_fav = float(shadow.get('max_fav', entry))
    # favorable excursion in price terms
    if side == 'BUY':
        fav_dist = max_fav - entry
        modeled_trail = entry + fav_dist - self.cfg.trail_gap  # peak - 0.30
    else:
        fav_dist = entry - max_fav
        modeled_trail = entry - fav_dist + self.cfg.trail_gap
    # refine outcome into the lock tiers when it was a 'Trail'-class exit
    # (v2.9.8: classifier already names BE/LOCK4/TIER; this only refines
    # legacy 'Trail' labels)
    refined = outcome
    if outcome == 'Trail':
        if abs(fav_dist) < 3.0:
            refined = 'SL_be'         # closed near BE before $3 lock
        elif fav_dist < 5.0:
            refined = 'SL_lock_3'     # $3 BE lock region
        elif fav_dist < (self.cfg.trail_gap + 5.0):
            refined = 'SL_lock_5'     # $5->+4 lock region
        else:
            refined = 'SL_trail'      # genuine trailing exit
    # slippage of the actual fill vs the modeled trail level (only meaningful for trail exits)
    trail_slip = ''
    if refined in ('SL_trail', 'SL_lock_5', 'SL_lock_3', 'SL_be'):
        trail_slip = round(close_price - modeled_trail, 3)

    entry_time = shadow.get('entry_time')
    entry_time_ist = (pd.Timestamp(entry_time).tz_convert('Asia/Kolkata').strftime('%H:%M:%S')
                      if entry_time is not None else '')
    row = [
        now_ist.strftime('%Y-%m-%d'),                # date_ist
        shadow.get('anchor_label', ''),              # anchor
        shadow.get('anchor_price', ''),              # anchor_price
        side,                                        # side
        entry_time_ist,                              # entry_time_ist
        round(entry, 3),                             # entry_price
        shadow.get('lot', self.cfg.lot_size),        # lot
        round(entry - self.cfg.sl_dist, 3) if side=='BUY' else round(entry + self.cfg.sl_dist, 3),  # initial_sl
        round(entry + self.cfg.tp_dist, 3) if side=='BUY' else round(entry - self.cfg.tp_dist, 3),  # initial_tp
        round(fav_dist, 3),                          # max_favorable ($ price)
        now_ist.strftime('%H:%M:%S'),                # exit_time_ist
        round(close_price, 3),                       # actual_exit_price
        round(modeled_trail, 3),                     # modeled_trail_exit (peak-0.30)
        trail_slip,                                  # actual - modeled (THE validation number)
        refined,                                     # exit_reason
        round(pnl_usd, 2),                           # realized_pnl_usd
        ticket,                                      # ticket
        shadow.get('nh_exit', ''),                   # v2.9.8 no-hold trail exit
        shadow.get('role', 'normal'),                # v2.9.8 role
    ]
    new_file = not _os.path.exists(jpath)
    with open(jpath, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(['date_ist','anchor','anchor_price','side','entry_time_ist',
                        'entry_price','lot','initial_sl','initial_tp','max_favorable',
                        'exit_time_ist','actual_exit_price','modeled_trail_exit',
                        'trail_slip','exit_reason','realized_pnl_usd','ticket',
                        'nohold_trail_exit','role'])
        w.writerow(row)
    log.info(f"journal: {shadow.get('anchor_label')} {side} {refined} "
             f"pnl=${pnl_usd:+.2f} trail_slip={trail_slip}")


def _send_daily_summary(self, day_str: str, pnl: float):
    emoji = "✅" if pnl > 0 else ("➖" if pnl == 0 else "📉")
    # Try to read today_trades.csv for richer detail
    n_trades = 0; wins = 0; sls = 0
    try:
        with open(self.daylog_path) as f:
            rows = list(csv.DictReader(f))
        n_trades = len(rows)
        wins = sum(1 for r in rows if float(r["pnl_usd"]) > 0)
        sls  = sum(1 for r in rows if r["outcome"] == "SL")
    except Exception:
        pass
    msg = (f"{emoji} *Daily summary {day_str}*\n"
           f"P&L: `${pnl:+,.2f}`\n"
           f"Trades: `{n_trades}` (wins `{wins}`, SLs `{sls}`)")
    sev = Severity.SUCCESS if pnl > 0 else Severity.WARN
    self.tele.send(msg, sev)


def _send_today_summary(self):
    day_str = self.state.get("last_broker_date", "?")
    pnl = self.state.get("daily_pnl", 0.0)
    self._send_daily_summary(day_str, pnl)
