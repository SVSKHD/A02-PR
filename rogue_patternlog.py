"""AUREON ROGUE pattern logger — Rogue-ONLY decision/feature capture + dated EOD archive.

Logging / file-IO ONLY. Places NO orders, changes NO decision, and is NEVER on the anchor
(Non-OCO, magic 20260522) path. On every Rogue evaluation the gate hook in rogue.py calls
log_eval() to append ONE row to run_dir/rogue_patterns.csv:

  ENTRY (the model's normalized, PRICE-INVARIANT shape features + the gate decision):
    ts, direction, range_dollars, body_ratio, candle_count, atr, spread, time_bucket,
    confirm_dollars, decision (ENTER/SKIP/FAKEOUT/SKIP_BY_MODEL), model_score
  EXIT (captured later on close, for a FUTURE Phase-3 exit model -- captured now, NOT used
  to decide anything in this task):
    entry_price, max_fav, trail_path_summary, exit_price, held_minutes, outcome_dollars

observe() is a behavior-NEUTRAL close watcher: it reads broker state to detect a closed
Rogue ticket, then backfills the exit columns of the matching ENTER row -- WITHOUT mutating
the Rogue mechanism. archive_day() freezes each broker day's files into logs/archive/{date}/.

Everything is GUARDED: a logging/file error can never reach trading. With rogue_enabled OFF
(raw default) nothing here runs -> byte-identical to master.
"""
from __future__ import annotations

import csv
import logging
import os
import shutil

log = logging.getLogger("AUREON")

ROGUE_MAGIC = 20260626   # anchors (20260522) / warmup (9999998) NEVER appear here.

PATTERNS_CSV = "rogue_patterns.csv"
TRADES_CSV = "rogue_trades.csv"

# entry (shape features + decision) ... then exit (filled on close).
PATTERN_COLUMNS = ['ts', 'direction', 'range_dollars', 'body_ratio', 'candle_count',
                   'atr', 'spread', 'time_bucket', 'confirm_dollars', 'decision',
                   'model_score',
                   'entry_price', 'max_fav', 'trail_path_summary', 'exit_price',
                   'held_minutes', 'outcome_dollars', 'magic']
TRADE_COLUMNS = ['ts', 'event', 'direction', 'entry', 'exit', 'sl',
                 'outcome_dollars', 'ticket', 'magic']

EXIT_COLUMNS = ['max_fav', 'trail_path_summary', 'exit_price', 'held_minutes',
                'outcome_dollars']

# decision labels
ENTER = 'ENTER'
SKIP = 'SKIP'                      # governor blocked (cap / loss-stop / fail-pause)
SKIP_BY_MODEL = 'SKIP_BY_MODEL'   # the trained gate blocked (score < threshold)
FAKEOUT = 'FAKEOUT'               # entered then gave back to the stop


# --- coarse session bucket + numeric code (price-invariant context) ---------------
_BUCKETS = ['asia', 'london', 'london_ny', 'ny', 'off']


def time_of_day_bucket(hour_utc):
    try:
        h = int(hour_utc) % 24
    except Exception:
        return 'unknown'
    if 0 <= h < 7:
        return 'asia'
    if 7 <= h < 12:
        return 'london'
    if 12 <= h < 17:
        return 'london_ny'
    if 17 <= h < 21:
        return 'ny'
    return 'off'


def time_bucket_code(bucket):
    try:
        return _BUCKETS.index(bucket)
    except Exception:
        return -1


# --- pure, price-INVARIANT shape features -----------------------------------------
def normalized_features(bars):
    """Recent M5 bars -> shape features (NO raw price levels): range_dollars (hi-lo $),
    body_ratio (sum|body|/sum|range|, thrust 0..1), candle_count, atr (mean bar range $).
    PURE."""
    cs = [c for c in (bars or []) if c is not None]
    n = len(cs)
    if n == 0:
        return {'range_dollars': 0.0, 'body_ratio': 0.0, 'candle_count': 0, 'atr': 0.0}
    hi = max(float(c['high']) for c in cs)
    lo = min(float(c['low']) for c in cs)
    bodies = sum(abs(float(c['close']) - float(c['open'])) for c in cs)
    ranges = sum(abs(float(c['high']) - float(c['low'])) for c in cs)
    body_ratio = (bodies / ranges) if ranges > 0 else 0.0
    return {'range_dollars': round(hi - lo, 2), 'body_ratio': round(body_ratio, 4),
            'candle_count': n, 'atr': round(ranges / n, 2)}


def build_features(bars, *, spread=0.0, confirm_dollars=0.0, ts=None):
    """Full normalized feature dict the logger + model share (adds spread, confirm_dollars,
    time_bucket + numeric code). PURE -- no raw price levels."""
    feats = normalized_features(bars)
    bucket = 'unknown'
    if ts is not None:
        try:
            bucket = time_of_day_bucket(str(ts)[11:13])
        except Exception:
            bucket = 'unknown'
    feats['spread'] = round(float(spread or 0.0), 2)
    feats['confirm_dollars'] = round(float(confirm_dollars or 0.0), 2)
    feats['time_bucket'] = bucket
    feats['time_bucket_code'] = time_bucket_code(bucket)
    return feats


# --- append-only CSV sinks (header-on-create; never raise to the caller) -----------
def _append_row(path, columns, row):
    new = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=columns)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, '') for k in columns})


def log_eval(run_dir, *, ts, direction, features, decision, model_score,
             entry_price='', outcome_dollars='', magic=ROGUE_MAGIC):
    """Append ONE Rogue evaluation row (entry shape features + decision + model_score, with
    the exit columns left blank to be backfilled on close). Returns the row dict. Guarded."""
    try:
        f = features or {}
        row = {
            'ts': ts, 'direction': direction,
            'range_dollars': f.get('range_dollars', ''), 'body_ratio': f.get('body_ratio', ''),
            'candle_count': f.get('candle_count', ''), 'atr': f.get('atr', ''),
            'spread': f.get('spread', ''), 'time_bucket': f.get('time_bucket', ''),
            'confirm_dollars': f.get('confirm_dollars', ''), 'decision': decision,
            'model_score': ('' if model_score is None else round(float(model_score), 4)),
            'entry_price': entry_price, 'max_fav': '', 'trail_path_summary': '',
            'exit_price': '', 'held_minutes': '', 'outcome_dollars': outcome_dollars,
            'magic': int(magic),
        }
        _append_row(os.path.join(run_dir, PATTERNS_CSV), PATTERN_COLUMNS, row)
        return row
    except Exception as e:
        log.warning(f"[ROGUE] patternlog eval non-fatal: {e!r}")
        return None


def log_trade(run_dir, *, ts, event, direction, entry, exit_px, sl,
              outcome_dollars='', ticket='', magic=ROGUE_MAGIC):
    """Append one ACTUAL Rogue fill/close row to rogue_trades.csv. Guarded."""
    try:
        row = {'ts': ts, 'event': event, 'direction': direction, 'entry': entry,
               'exit': exit_px, 'sl': sl, 'outcome_dollars': outcome_dollars,
               'ticket': ticket, 'magic': int(magic)}
        _append_row(os.path.join(run_dir, TRADES_CSV), TRADE_COLUMNS, row)
        return row
    except Exception as e:
        log.warning(f"[ROGUE] patternlog trade non-fatal: {e!r}")
        return None


def backfill_exit(run_dir, enter_ts, exit_fields, decision=None):
    """Fill the EXIT columns (and optionally re-label decision) on the ENTER patterns row
    whose ts matches, via atomic temp-then-replace. On ANY error the file is left untouched
    (the realized $ still lives in rogue_trades.csv). Guarded. Returns True on a hit."""
    path = os.path.join(run_dir, PATTERNS_CSV)
    if not os.path.exists(path):
        return False
    try:
        with open(path, newline='') as f:
            rows = list(csv.DictReader(f))
        hit = False
        for r in rows:
            if (not hit and str(r.get('ts')) == str(enter_ts)
                    and str(r.get('decision')) == ENTER
                    and str(r.get('outcome_dollars', '')) == ''):
                for k in EXIT_COLUMNS:
                    if k in exit_fields:
                        r[k] = exit_fields[k]
                if decision is not None:
                    r['decision'] = decision
                hit = True
        if not hit:
            return False
        tmp = path + ".tmp"
        with open(tmp, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=PATTERN_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, '') for k in PATTERN_COLUMNS})
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.warning(f"[ROGUE] patternlog backfill non-fatal: {e!r}")
        return False


# --- behavior-NEUTRAL close watcher (exit capture; never mutates the mechanism) ----
def _held_minutes(enter_ts, close_ts):
    try:
        import pandas as _pd
        return round((_pd.Timestamp(close_ts) - _pd.Timestamp(enter_ts)).total_seconds() / 60.0, 1)
    except Exception:
        return ''


def observe(trader):
    """Detect a CLOSED Rogue position and record its EXIT features -- WITHOUT touching the
    Rogue mechanism's state (pure observation). Wired after rogue.drive(). Rogue-only;
    returns immediately unless should_run. Guarded."""
    try:
        import rogue as _r
        if not _r.should_run(trader.cfg, is_funded=not _r.account_is_demo(trader)):
            return
        st = getattr(trader, '_rogue', None)
        if not st:
            return
        rpl = getattr(trader, '_rpl', None)
        if rpl is None:
            rpl = {'open_ticket': None, 'open_snap': None, 'enter_ts': None,
                   'enter_price': None, 'closed': set()}
            trader._rpl = rpl
        open_ = st.get('open')
        run_dir = getattr(trader, 'run_dir', '.')
        try:
            import pandas as _pd
            ts = _pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ts = ''
        # while open: remember the live snapshot (peak = max favorable excursion).
        if open_ is not None and open_.get('ticket') is not None:
            rpl['open_ticket'] = open_.get('ticket')
            rpl['open_snap'] = dict(open_)
            return
        # open gone from state but we had a ticket -> the position closed.
        tk = rpl.get('open_ticket')
        snap = rpl.get('open_snap')
        if tk is None or snap is None or tk in rpl.get('closed', set()):
            return
        if not _ticket_closed(trader, tk):
            return
        side = snap.get('side')
        sgn = 1.0 if side == 'BUY' else -1.0
        try:
            entry = float(snap.get('entry'))
            peak = float(snap.get('peak', entry))
            exit_px = float(snap.get('sl'))   # closed on the trailing/init stop
            outcome = round(sgn * (exit_px - entry), 2)
            max_fav = round(sgn * (peak - entry), 2)
        except Exception:
            entry = peak = exit_px = ''
            outcome = max_fav = ''
        enter_ts = rpl.get('enter_ts')
        held = _held_minutes(enter_ts, ts) if enter_ts else ''
        trail_path = (f"{entry}->{peak}->{exit_px}"
                      if '' not in (entry, peak, exit_px) else '')
        log_trade(run_dir, ts=ts, event='close', direction=side,
                  entry=entry, exit_px=exit_px, sl=exit_px,
                  outcome_dollars=outcome, ticket=tk)
        if enter_ts and outcome != '':
            backfill_exit(run_dir, enter_ts,
                          {'max_fav': max_fav, 'trail_path_summary': trail_path,
                           'exit_price': exit_px, 'held_minutes': held,
                           'outcome_dollars': outcome},
                          decision=(FAKEOUT if outcome <= 0 else None))
        rpl.setdefault('closed', set()).add(tk)
        rpl['open_ticket'] = None
        rpl['open_snap'] = None
    except Exception as e:
        log.warning(f"[ROGUE] patternlog observe non-fatal: {e!r}")


def _ticket_closed(trader, ticket):
    """True if `ticket` is no longer an open broker position. Conservative: any read error
    -> False (never claim a close we cannot confirm)."""
    try:
        pos = trader.adapter.mt5.positions_get(ticket=int(ticket))
        return not pos
    except Exception:
        return False


# --- dated EOD archive (copy, not move -- live files keep rolling) ------------------
def archive_day(run_dir, *, broker_date, price_log_dir=None, daylog_path=None,
                base_log_dir="./logs"):
    """Freeze the day's files into base_log_dir/archive/{broker_date}/. COPIES
    rogue_patterns.csv, rogue_trades.csv, today_trades.csv, price_{date}.csv (never moves).
    Returns archived basenames. Guarded."""
    archived = []
    try:
        dest = os.path.join(base_log_dir, "archive", str(broker_date))
        os.makedirs(dest, exist_ok=True)
        srcs = [os.path.join(run_dir, PATTERNS_CSV), os.path.join(run_dir, TRADES_CSV)]
        if daylog_path:
            srcs.append(daylog_path)
        if price_log_dir:
            srcs.append(os.path.join(price_log_dir, f"price_{broker_date}.csv"))
        for src in srcs:
            try:
                if src and os.path.exists(src):
                    shutil.copy2(src, os.path.join(dest, os.path.basename(src)))
                    archived.append(os.path.basename(src))
            except Exception as e:
                log.warning(f"[ROGUE] archive copy {src} non-fatal: {e!r}")
        log.info(f"[ROGUE] archived {len(archived)} file(s) -> {dest}")
    except Exception as e:
        log.warning(f"[ROGUE] archive_day non-fatal: {e!r}")
    return archived
