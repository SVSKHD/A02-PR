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


# ============================================================================
# Weekend-status stats (v3.0.0 follow-up). PURE READ of the local trades
# journal CSV -- no side effects, no AUREON-runtime imports. Used by the
# weekend `status` reply and reusable by a future standalone `stats` command
# or the frontend later.
# ============================================================================

def summarize_recent(csv_path, today=None):
    """Return (last_day, week) realized-P&L summaries from a trades journal CSV.

    last_day = {} when there is no data, else
        {'date': 'YYYY-MM-DD',
         'anchors': {'A1': pnl, 'A2': pnl, ...},   # sum realized_pnl_usd / anchor
         'total': float}
    week = {} when there is no data, else
        {'days': [('YYYY-MM-DD', day_total), ...],  # chronological
         'total': float, 'n': int}

    "Last trading day" = the most recent date_ist present in the CSV. "Week to
    date" = the trading days present in the Mon-Fri week containing `today` (a
    'YYYY-MM-DD' str/date; default = the last trading day). All numbers come
    from the realized_pnl_usd column. If `csv_path` is named trades_<YYYY-MM>.csv
    a sibling previous-month file (same dir) is merged in, so a Mon-Fri week that
    straddles a month boundary is still complete. Pure read; a malformed row is
    skipped rather than raised, so the caller always gets whatever parsed.
    """
    def _parse_day(s):
        try:
            return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return None

    # Candidate files: the given CSV + (if monthly-named) the prior month, so a
    # week spanning month-end is whole even though trades live in monthly files.
    paths = [csv_path]
    base = os.path.basename(csv_path)
    if base.startswith('trades_') and base.endswith('.csv'):
        ym = base[len('trades_'):-len('.csv')]
        try:
            y, m = int(ym[:4]), int(ym[5:7])
            pm = (y - 1, 12) if m == 1 else (y, m - 1)
            paths.append(os.path.join(os.path.dirname(csv_path),
                                      f"trades_{pm[0]:04d}-{pm[1]:02d}.csv"))
        except (ValueError, IndexError):
            pass

    per_anchor = {}   # date -> {anchor -> pnl}
    per_day = {}      # date -> total pnl
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        try:
            with open(p, newline="") as f:
                for r in csv.DictReader(f):
                    d = _parse_day(r.get('date_ist'))
                    if d is None:
                        continue
                    try:
                        pnl = float(r.get('realized_pnl_usd') or 0.0)
                    except (TypeError, ValueError):
                        continue
                    label = (r.get('anchor') or '?').strip() or '?'
                    bucket = per_anchor.setdefault(d, {})
                    bucket[label] = round(bucket.get(label, 0.0) + pnl, 2)
                    per_day[d] = round(per_day.get(d, 0.0) + pnl, 2)
        except (OSError, csv.Error):
            continue

    if not per_day:
        return {}, {}

    last = max(per_day)
    last_day = {
        'date': last.isoformat(),
        'anchors': dict(per_anchor.get(last, {})),
        'total': round(per_day[last], 2),
    }

    # Week = Mon-Fri of the week containing `today` (else the last trading day);
    # weekend/holiday days map onto the same Mon-Fri window as that week's trades.
    anchor_day = _parse_day(today) or last
    monday = anchor_day - timedelta(days=anchor_day.weekday())
    friday = monday + timedelta(days=4)
    wdays = sorted(d for d in per_day if monday <= d <= friday)
    week = {
        'days': [(d.isoformat(), round(per_day[d], 2)) for d in wdays],
        'total': round(sum(per_day[d] for d in wdays), 2),
        'n': len(wdays),
    }
    return last_day, week


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
    self.tele.send(msg, sev, important=True, critical=True)  # v3.0.9: queue if unreachable


def _send_today_summary(self):
    day_str = self.state.get("last_broker_date", "?")
    pnl = self.state.get("daily_pnl", 0.0)
    self._send_daily_summary(day_str, pnl)


# ============================================================================
# v3.0.0 commit 3 — Firebase EOD journal wiring.
# firebase_journal.py is internally fail-safe; we double-guard at every call
# site so a Firebase error can NEVER block trading, the EOD flatten, or startup.
# ============================================================================

def _journal_dir(self):
    return os.path.join(self.run_dir, "journal")


def _firebase_save_daily(self, broker_date):
    """ONE Firestore write per trading day, after the EOD flatten when the day's
    P&L is final. Builds one record per closed trade from today's journal CSV via
    make_trade_record/build_anchor, then a single save_daily_journal() call.
    Never raises -- a Firebase failure must not touch the EOD path."""
    try:
        import firebase_journal
        # Key the export off the BROKER trading date passed in by the EOD logic --
        # NOT the IST wall clock. EOD fires at broker 23:00 = ~01:30 IST, so
        # now_ist would be the NEXT day and the date_ist filter would miss the
        # whole broker day. Anchor closes are intraday, so a trade's journal
        # date_ist equals the broker calendar date; match on that.
        day_str = str(broker_date)
        jpath = os.path.join(self._journal_dir(), f"trades_{day_str[:7]}.csv")
        if not os.path.exists(jpath):
            log.info(f"firebase EOD: no journal CSV for {day_str}; nothing to save")
            return
        rows = []
        with open(jpath, newline="") as f:
            for r in csv.DictReader(f):
                if r.get('date_ist') == day_str:
                    rows.append(r)
        if not rows:
            log.info(f"firebase EOD: 0 trades for {day_str}; skipping daily journal")
            return
        grouped = {}
        total = 0.0
        for r in rows:
            rec = firebase_journal.make_trade_record(
                ticket=r.get('ticket'), side=r.get('side'), lot=r.get('lot'),
                entry_price=r.get('entry_price'), exit_price=r.get('actual_exit_price'),
                pnl=r.get('realized_pnl_usd'), role=r.get('role'),
                open_time=r.get('entry_time_ist'), close_time=r.get('exit_time_ist'),
                exit_reason=r.get('exit_reason'), slip=r.get('trail_slip'),
                max_favorable=r.get('max_favorable'),
                nohold_trail_exit=r.get('nohold_trail_exit'), anchor=r.get('anchor'))
            try:
                total += float(r.get('realized_pnl_usd') or 0.0)
            except (TypeError, ValueError):
                pass
            label = r.get('anchor') or '?'
            grouped.setdefault(label, {'price': r.get('anchor_price'), 'trades': []})
            grouped[label]['trades'].append(rec)
        anchors = [firebase_journal.build_anchor(label, g['price'], g['trades'])
                   for label, g in grouped.items()]
        # v3.0.6: capture the day's CLOSE balance + equity from MT5 so the doc no
        # longer reads `bal n/a` (fix-forward; old docs are not backfilled).
        close_balance = equity = None
        try:
            ainfo = self.adapter.get_account_info() if self.adapter else {}
            if ainfo:
                close_balance = ainfo.get('balance')
                equity = ainfo.get('equity')
        except Exception as be:
            log.warning(f"firebase EOD: could not read account balance: {be!r}")
        firebase_journal.save_daily_journal(
            day_str, anchors=anchors, total_pnl=round(total, 2),
            meta={'source': 'eod', 'broker_date': str(broker_date)},
            close_balance=close_balance, equity=equity)
    except Exception as e:
        log.warning(f"firebase EOD journal skipped (non-fatal): {e!r}")


def _firebase_weekly_reconcile(self):
    """On closed-market (Sunday) startup, backfill any day the EOD write missed
    by reconciling the monthly trades CSVs against Firestore. Never raises."""
    try:
        import firebase_journal
        n = firebase_journal.weekly_reconcile(self._journal_dir())
        if n:
            self.tele.info(f"📒 Firebase weekly reconcile backfilled {n} day(s).")
    except Exception as e:
        log.warning(f"firebase weekly reconcile skipped (non-fatal): {e!r}")
