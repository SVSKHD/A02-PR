"""
AUREON — trade journal (JournalMixin): the 19-column monthly CSV + daily
summaries, PLUS the new v3.0.0 Firebase EOD/weekly wiring.

_write_journal writes one rich row per fill (schema UNCHANGED — 19 columns incl.
nohold_trail_exit, role). The Firebase helpers are NEW and fail-safe: they read
today's closed trades from the run-dir CSVs and push an end-of-day journal to
Firestore, and reconcile the week from the monthly trade CSVs on a market-closed
startup. Any Firebase failure is swallowed and never blocks trading or the flatten.

_write_journal / _send_daily_summary / _send_today_summary extracted verbatim
from live_trader.py (v3.0.0 refactor). Byte-identical.
"""

import csv
import logging
import os

import pandas as pd

from telemetry import Severity

import firebase_journal  # registered at import so the module receipt surfaces it

log = logging.getLogger("AUREON")


class JournalMixin:
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

    # ------------------------------------------------------------------------
    # v3.0.0 NEW — Firebase EOD journal wiring (fail-safe; never blocks trading)
    # ------------------------------------------------------------------------
    def _collect_today_trade_records(self, broker_date):
        """Build per-trade record dicts for `broker_date` from the run-dir CSVs.

        today_trades.csv carries the basic close (date, anchor, side, entry, exit,
        outcome, pnl_usd, ticket); the monthly journal CSV (trades_YYYY-MM.csv)
        carries the rich columns (max_favorable, trail_slip, nohold_trail_exit,
        role, lot, entry/exit time). We join on ticket and hand the merged dicts
        to firebase_journal.make_trade_record. Best-effort; returns [] on any error.
        """
        import firebase_journal
        records = []
        day = str(broker_date)
        # rich rows by ticket from the monthly journals (scan all months present)
        rich = {}
        jdir = os.path.join(self.run_dir, "journal")
        try:
            if os.path.isdir(jdir):
                for fn in os.listdir(jdir):
                    if not (fn.startswith("trades_") and fn.endswith(".csv")):
                        continue
                    with open(os.path.join(jdir, fn), newline="") as f:
                        for r in csv.DictReader(f):
                            tk = str(r.get("ticket", "")).strip()
                            if tk:
                                rich[tk] = r
        except Exception as e:
            log.warning(f"firebase: rich-journal scan failed: {e}")
        try:
            with open(self.daylog_path, newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            log.warning(f"firebase: today_trades read failed: {e}")
            return []
        for r in rows:
            if str(r.get("date", "")).strip() != day:
                continue
            rr = rich.get(str(r.get("ticket", "")).strip(), {})
            try:
                rec = firebase_journal.make_trade_record(self.cfg, r, rr)
            except Exception as e:
                log.warning(f"firebase: make_trade_record failed for {r.get('ticket')}: {e}")
                continue
            if rec is not None:
                records.append(rec)
        return records

    def _firebase_eod_save(self, broker_date):
        """Push the end-of-day journal to Firebase. Fail-safe wrapper."""
        try:
            import firebase_journal
            records = self._collect_today_trade_records(broker_date)
            firebase_journal.save_daily_journal(
                day=str(broker_date),
                daily_pnl=float(self.state.get("daily_pnl", 0.0)),
                trades=records,
                paper=self.paper,
            )
            log.info(f"firebase: EOD journal saved for {broker_date} "
                     f"({len(records)} trades)")
        except Exception as e:
            log.warning(f"firebase EOD save failed (non-fatal): {e}")

    def _firebase_weekly_reconcile(self):
        """On a market-closed (e.g. Sunday) startup, reconcile the week from the
        monthly trade CSVs. Fail-safe wrapper — never raises."""
        try:
            import firebase_journal
            firebase_journal.weekly_reconcile(
                journal_dir=os.path.join(self.run_dir, "journal"),
                paper=self.paper,
            )
            log.info("firebase: weekly reconcile complete")
        except Exception as e:
            log.warning(f"firebase weekly reconcile failed (non-fatal): {e}")
