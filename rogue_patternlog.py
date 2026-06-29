"""AUREON ROGUE pattern logger — Rogue-ONLY decision/feature capture + dated EOD archive.

Logging / file-IO ONLY. This module places NO orders, changes NO decision, and is
NEVER on the anchor (Non-OCO, magic 20260522) path. It is a pure observer of the
Rogue mechanism (magic 20260626): on every Rogue evaluation it appends ONE row of
PRICE-INVARIANT shape features + the decision (ENTER / SKIP / FAKEOUT) to
run_dir/rogue_patterns.csv, so each day becomes a replayable training set of what
Rogue saw and chose -- the negatives (skips / fake-outs) included, because those are
exactly the rows a pattern model needs.

It also keeps run_dir/rogue_trades.csv (actual Rogue fills + closes with realized $),
and archive_day() freezes each broker day's files into logs/archive/{date}/.

Everything is GUARDED: a logging or file error here can never reach the trading path
(observe()/archive_day() swallow and log-continue). With rogue_enabled OFF (the raw
config default) observe() returns immediately -> no rows, byte-identical to master.
"""
from __future__ import annotations

import csv
import logging
import os
import shutil

log = logging.getLogger("AUREON")

# Rogue-only magic. Anchors (20260522) and warmup (9999998) NEVER appear here.
ROGUE_MAGIC = 20260626

PATTERNS_CSV = "rogue_patterns.csv"
TRADES_CSV = "rogue_trades.csv"

# the normalized feature/label schema (price-INVARIANT -- shapes, not levels).
PATTERN_COLUMNS = ['ts', 'direction', 'range_dollars', 'body_ratio', 'candle_count',
                   'atr', 'spread', 'tod_bucket', 'decision', 'outcome_dollars', 'magic']
TRADE_COLUMNS = ['ts', 'event', 'direction', 'entry', 'exit', 'sl',
                 'outcome_dollars', 'ticket', 'magic']

# decision labels
ENTER = 'ENTER'
SKIP = 'SKIP'
FAKEOUT = 'FAKEOUT'


# --- pure feature extraction (no IO, no price levels leak out) --------------------
def time_of_day_bucket(hour_utc):
    """Coarse session bucket from the UTC hour (price-invariant context feature)."""
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


def normalized_features(bars):
    """Reduce recent M5 bars to PRICE-INVARIANT shape features. Returns a dict with
    range_dollars (hi-lo $), body_ratio (sum|body| / sum|range|, the thrust 0..1),
    candle_count, atr (mean bar range $). Raw price LEVELS never leave this function --
    only spans/ratios, so the row is reusable across any price regime. PURE."""
    cs = [c for c in (bars or []) if c is not None]
    n = len(cs)
    if n == 0:
        return {'range_dollars': 0.0, 'body_ratio': 0.0, 'candle_count': 0, 'atr': 0.0}
    hi = max(float(c['high']) for c in cs)
    lo = min(float(c['low']) for c in cs)
    bodies = sum(abs(float(c['close']) - float(c['open'])) for c in cs)
    ranges = sum(abs(float(c['high']) - float(c['low'])) for c in cs)
    body_ratio = (bodies / ranges) if ranges > 0 else 0.0
    atr = ranges / n
    return {'range_dollars': round(hi - lo, 2), 'body_ratio': round(body_ratio, 4),
            'candle_count': n, 'atr': round(atr, 2)}


# --- append-only CSV sinks (header-on-create; never raises to the caller) ----------
def _append_row(path, columns, row):
    new = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=columns)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, '') for k in columns})


def log_evaluation(run_dir, *, ts, direction, bars, decision, spread='',
                   outcome_dollars='', magic=ROGUE_MAGIC, tod_bucket=None, atr=None):
    """Append ONE Rogue evaluation row (features + decision) to rogue_patterns.csv.
    Returns the written row dict (so a caller can remember its ts for outcome backfill).
    Guarded -- returns None on any IO error, never raises."""
    try:
        feats = normalized_features(bars)
        if tod_bucket is None:
            try:
                tod_bucket = time_of_day_bucket(str(ts)[11:13])
            except Exception:
                tod_bucket = 'unknown'
        row = {
            'ts': ts, 'direction': direction,
            'range_dollars': feats['range_dollars'], 'body_ratio': feats['body_ratio'],
            'candle_count': feats['candle_count'],
            'atr': feats['atr'] if atr is None else round(float(atr), 2),
            'spread': spread, 'tod_bucket': tod_bucket, 'decision': decision,
            'outcome_dollars': outcome_dollars, 'magic': int(magic),
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


def _backfill_outcome(run_dir, ts, outcome_dollars, decision=None):
    """Fill outcome_dollars (and optionally re-label decision) on the patterns row whose
    ts matches the ENTER, by atomic temp-then-replace. Append-only safe: on ANY error the
    original file is untouched and the outcome simply stays blank (the realized $ is still
    in rogue_trades.csv). Guarded."""
    path = os.path.join(run_dir, PATTERNS_CSV)
    if not os.path.exists(path):
        return False
    try:
        with open(path, newline='') as f:
            rows = list(csv.DictReader(f))
        hit = False
        for r in rows:
            if (not hit and str(r.get('ts')) == str(ts)
                    and str(r.get('decision')) == ENTER
                    and str(r.get('outcome_dollars', '')) == ''):
                r['outcome_dollars'] = outcome_dollars
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
        os.replace(tmp, path)   # atomic
        return True
    except Exception as e:
        log.warning(f"[ROGUE] patternlog backfill non-fatal: {e!r}")
        return False


# --- the live observer hook (called from live_trader AFTER rogue.drive) -----------
def observe(trader):
    """Per-tick Rogue observer. Diffs trader._rogue (managed by rogue.drive) against a
    private trader._rpl snapshot and logs the decision transitions:
      * a monster setup replaced WITHOUT an entry -> SKIP row (a negative example);
      * a new Rogue open                          -> ENTER row + a 'enter' trade row;
      * a Rogue open that closed                  -> 'close' trade row with realized $,
                                                     backfilled onto the ENTER row
                                                     (FAKEOUT if it gave back to the SL).
    Rogue-ONLY: returns immediately unless should_run (rogue_enabled AND demo). Fully
    guarded -- never raises onto the tick."""
    try:
        import rogue as _r
        is_funded = not _r.account_is_demo(trader)
        if not _r.should_run(trader.cfg, is_funded=is_funded):
            return
        st = getattr(trader, '_rogue', None)
        if not st:
            return
        rpl = getattr(trader, '_rpl', None)
        if rpl is None:
            rpl = {'anchor': None, 'leg_dir': None, 'bars': None, 'entered': False,
                   'enter_ts': None, 'open_ticket': None, 'open_snap': None}
            trader._rpl = rpl
        run_dir = getattr(trader, 'run_dir', '.')
        try:
            import pandas as _pd
            ts = _pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ts = ''
        spread = _spread(trader)
        bars = None
        try:
            bars = _r._recent_m5(trader)
        except Exception:
            bars = None

        anchor = st.get('anchor')
        open_ = st.get('open')

        # 1. NEW SETUP: a fresh monster anchor appeared. If the PREVIOUS setup was never
        #    entered, it was abandoned -> log it as SKIP (the negative example).
        if anchor is not None and anchor != rpl.get('anchor'):
            if rpl.get('anchor') is not None and not rpl.get('entered'):
                log_evaluation(run_dir, ts=ts, direction=rpl.get('leg_dir'),
                               bars=rpl.get('bars'), decision=SKIP, spread=spread,
                               outcome_dollars=0)
            rpl.update(anchor=anchor, leg_dir=st.get('leg_dir'), bars=bars,
                       entered=False, enter_ts=None)

        # 2. ENTRY: a new Rogue open -> ENTER pattern row + a fill trade row.
        if open_ is not None and open_.get('ticket') != rpl.get('open_ticket'):
            log_evaluation(run_dir, ts=ts, direction=open_.get('side'),
                           bars=rpl.get('bars') or bars, decision=ENTER, spread=spread,
                           outcome_dollars='')
            log_trade(run_dir, ts=ts, event='enter', direction=open_.get('side'),
                      entry=open_.get('entry'), exit_px='', sl=open_.get('sl'),
                      ticket=open_.get('ticket'))
            rpl.update(entered=True, enter_ts=ts, open_ticket=open_.get('ticket'),
                       open_snap=dict(open_))
        if open_ is not None:
            rpl['open_snap'] = dict(open_)   # keep the latest peak/sl snapshot

        # 3. CLOSE: the open went away -> realized $ from the last snapshot (it closed on
        #    its trailing SL). Backfill the ENTER row; a give-back to the SL is a FAKEOUT.
        if open_ is None and rpl.get('open_snap') is not None:
            snap = rpl['open_snap']
            sgn = 1.0 if snap.get('side') == 'BUY' else -1.0
            try:
                outcome = round(sgn * (float(snap.get('sl')) - float(snap.get('entry'))), 2)
            except Exception:
                outcome = ''
            decision = FAKEOUT if (outcome != '' and outcome <= 0) else ENTER
            log_trade(run_dir, ts=ts, event='close', direction=snap.get('side'),
                      entry=snap.get('entry'), exit_px=snap.get('sl'), sl=snap.get('sl'),
                      outcome_dollars=outcome, ticket=snap.get('ticket'))
            if rpl.get('enter_ts') is not None and outcome != '':
                _backfill_outcome(run_dir, rpl['enter_ts'], outcome,
                                  decision=(FAKEOUT if decision == FAKEOUT else None))
            rpl.update(open_snap=None, open_ticket=None)
    except Exception as e:
        log.warning(f"[ROGUE] patternlog observe non-fatal: {e!r}")


def _spread(trader):
    try:
        tk = trader.adapter.mt5.symbol_info_tick(trader.cfg.symbol)
        return round(float(tk.ask) - float(tk.bid), 2)
    except Exception:
        return ''


# --- dated EOD archive (copy, don't move -- the live files keep rolling) -----------
def archive_day(run_dir, *, broker_date, price_log_dir=None, daylog_path=None,
                base_log_dir="./logs"):
    """Freeze the day's Rogue + trade files into base_log_dir/archive/{broker_date}/ as a
    replayable snapshot. COPIES (never moves) rogue_patterns.csv, rogue_trades.csv,
    today_trades.csv, and the day's price_{broker_date}.csv, so the live files keep
    rolling. Returns the list of archived basenames. Guarded -- never raises."""
    archived = []
    try:
        dest = os.path.join(base_log_dir, "archive", str(broker_date))
        os.makedirs(dest, exist_ok=True)
        srcs = [os.path.join(run_dir, PATTERNS_CSV),
                os.path.join(run_dir, TRADES_CSV)]
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
