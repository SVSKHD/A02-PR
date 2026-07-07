"""AUREON — per-engine / per-anchor daily P&L report. READ-ONLY / OBSERVER ONLY.

Automates the CSV analysis that drove the A3 cut (ERRORS.md D-1): per-anchor
net / PF / win% / whipsaw, per-leg-class P&L (original vs RALLY boost vs RESCUE
boost vs the F-B trapped-late-rescue hedge), a Rogue section, and a month-to-
date cut/keep roll-up. Fires automatically once per broker day at EOD
(live_trader.py, guarded by `cfg.util_daily_pnl_report`) and on demand via
`python bot.py dailyreport [YYYY-MM-DD|YYYY-MM]`. Never places, modifies, or
closes an order; never reads/writes trading state (`shadow_positions`,
governors, brakes) -- only MT5 HISTORY (closed deals), the local
`rescue_events.csv` / `journal/trades_*.csv` files, and `logs/aureon*.log`.

DATA SOURCES + THE ONE AMBIGUITY THIS MODULE DOES NOT GUESS AROUND
-------------------------------------------------------------------
MT5 history deals (grouped by `position_id`) are the source of truth for every
$ figure. Order comments (`mt5_comment`, <=31 chars) only carry:

    AUR_{A2}_BUY | AUR_{A2}_SELL [_G|_RCV|_CFM] [_R{n}]   -- anchor originals
    AUR_{A2}_{B|S}_B{n}                                    -- ANY boost fleet member
    AUR_ROGUE_{B|S}                                        -- rogue (isolated by
                                                               magic 20260626 too)

The boost comment `AUR_{A2}_{side}_B{n}` is IDENTICAL for a RALLY pyramid, a
RESCUE hedge, and the new F-B TRAPPED_LATE_RESCUE hedge (fills.py / rally.py /
rescue.py / boosts.py all route through the SAME `boosts_common.place_fleet`,
which builds the comment from only anchor+side+sequence -- `boosts.BoostPlan`'s
`kind`/`event_type` is never written to the broker). The ONLY durable, ticket-
keyed record of which is which is `run/rescue_events.csv`'s `event_type`
column (`RALLY_BOOST` / `RESCUE_BOOST` / `TRAPPED_LATE_RESCUE`, written once
the WHOLE fleet event closes -- `rescue_log.py`). This module joins boost
tickets against that file; a boost ticket with NO matching row (the fleet
event hasn't finalized yet, or `rescue_events.csv` doesn't reach back that
far) is reported as `BOOST_UNCLASSIFIED` -- counted and shown, NEVER silently
folded into RALLY or RESCUE. **The minimal fix**, if this gap matters at
scale: a 4th character on the boost comment (`AUR_A1_S_B1R`/`_B1S`/`_B1F` for
RALLY/RESCUE/F-B) -- `mt5_comment`'s 31-char budget has room. Proposed here,
not implemented (this branch is reporting-only; changing the live comment
format is an engine-logic change, out of scope for a READ-ONLY tool).

A second, smaller gap: Rogue's CHAIN re-anchor event (`rogue.py`,
`detect_close`) was only ever sent to Discord/Telegram (`trader.tele.info`),
never to `aureon.log` -- unlike CHASE-REJECT / CHAIN-COOLDOWN / CHAIN-
DISPLACEMENT, which already call `log.info(msg)` alongside the tele call. This
branch adds that ONE missing `log.info(msg)` mirror (`rogue.py`, `detect_close`
+ the close-summary line) so "chain re-anchors today" and "brake events today"
are greppable from the log like their sibling gate-reject lines already are --
a pure additive logging line, not a decision or order-flow change.

Module shape
------------
PURE core (classification, grouping, PF/win%/whipsaw math, month roll-up,
markdown/CSV rendering) needs no MT5 and is fully selftest-covered with fixture
deal-like dicts. The impure readers (bulk MT5 history sweep, `rescue_events.csv`
join, `journal/trades_*.csv` join, `aureon*.log` grep) are isolated at the
bottom of the file and degrade to empty results rather than raising -- a
missing file / broker error never crashes the report, it just reports less.
"""
from __future__ import annotations

import csv
import logging
import os
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("AUREON")

# ---------------------------------------------------------------------------
# Comment / leg classification (PURE)
# ---------------------------------------------------------------------------
ANCHOR_ENGINE = "ANCHOR"
ROGUE_ENGINE = "ROGUE"

ORIGINAL = "ORIGINAL"
BOOST_UNCLASSIFIED = "BOOST_UNCLASSIFIED"
RALLY_BOOST = "RALLY_BOOST"
RESCUE_BOOST = "RESCUE_BOOST"
TRAPPED_LATE_RESCUE = "TRAPPED_LATE_RESCUE"
ROGUE_LEG = "ROGUE"
UNKNOWN = "UNKNOWN"

# From boosts_common.place_fleet's event_type -> our leg_class name (identity
# for the two that already match; TRAPPED_LATE_RESCUE already matches too).
_EVENT_TYPE_TO_LEG_CLASS = {
    "RALLY_BOOST": RALLY_BOOST,
    "RESCUE_BOOST": RESCUE_BOOST,
    "TRAPPED_LATE_RESCUE": TRAPPED_LATE_RESCUE,
}

ROGUE_MAGIC_DEFAULT = 20260626
FETCHER_ENGINE = "FETCHER"
FETCHER_LEG = "FETCHER"
FETCHER_MAGIC_DEFAULT = 20260707

_ANCHOR_ORIG_RE = re.compile(r'^AUR_([A-Z0-9]{2})_(BUY|SELL)(?:_(G|RCV|CFM))?(?:_R\d+)?$')
_ANCHOR_BOOST_RE = re.compile(r'^AUR_([A-Z0-9]{2})_([BS])_B(\d+)$')
_ROGUE_RE = re.compile(r'^AUR_ROGUE_([BS])$')
_FETCH_RE = re.compile(r'^AUR_FETCH_([BS])$')

_SIDE_FROM_CHAR = {'B': 'BUY', 'S': 'SELL'}


def classify_comment(comment, magic=None, rogue_magic=ROGUE_MAGIC_DEFAULT,
                     fetcher_magic=FETCHER_MAGIC_DEFAULT):
    """PURE: classify one MT5 deal's (comment, magic) into a dict:
      {'engine': 'ANCHOR'|'ROGUE'|'FETCHER'|None, 'anchor2': '<2-char code>'|None,
       'side': 'BUY'|'SELL'|None, 'leg_class': ..., 'boost_seq': int|None}
    Magic is checked FIRST (more reliable than string matching per the MT5
    API); the comment is the fallback / cross-check. An unmatched comment
    returns leg_class=UNKNOWN so the caller can COUNT it rather than silently
    drop or mis-bucket it. Never raises."""
    c = str(comment or '')
    if magic == rogue_magic or c.startswith('AUR_ROGUE_'):
        m = _ROGUE_RE.match(c)
        side = _SIDE_FROM_CHAR.get(m.group(1)) if m else None
        return {'engine': ROGUE_ENGINE, 'anchor2': None, 'side': side,
                'leg_class': ROGUE_LEG, 'boost_seq': None}
    if magic == fetcher_magic or c.startswith('AUR_FETCH_'):
        m = _FETCH_RE.match(c)
        side = _SIDE_FROM_CHAR.get(m.group(1)) if m else None
        return {'engine': FETCHER_ENGINE, 'anchor2': None, 'side': side,
                'leg_class': FETCHER_LEG, 'boost_seq': None}
    m = _ANCHOR_BOOST_RE.match(c)
    if m:
        anchor2, sidechar, seq = m.group(1), m.group(2), int(m.group(3))
        return {'engine': ANCHOR_ENGINE, 'anchor2': anchor2,
                'side': _SIDE_FROM_CHAR.get(sidechar),
                'leg_class': BOOST_UNCLASSIFIED, 'boost_seq': seq}
    m = _ANCHOR_ORIG_RE.match(c)
    if m:
        anchor2, side, _tag = m.group(1), m.group(2), m.group(3)
        return {'engine': ANCHOR_ENGINE, 'anchor2': anchor2, 'side': side,
                'leg_class': ORIGINAL, 'boost_seq': None}
    return {'engine': None, 'anchor2': None, 'side': None,
            'leg_class': UNKNOWN, 'boost_seq': None}


def resolve_boost_leg_class(ticket, rescue_event_index):
    """PURE: BOOST_UNCLASSIFIED -> RALLY_BOOST/RESCUE_BOOST/TRAPPED_LATE_RESCUE
    via the {ticket: event_type} index built from rescue_events.csv. No entry
    (event not finalized yet / file doesn't reach back that far) -> stays
    BOOST_UNCLASSIFIED. Never guesses."""
    try:
        et = rescue_event_index.get(int(ticket))
    except (TypeError, ValueError):
        return BOOST_UNCLASSIFIED
    return _EVENT_TYPE_TO_LEG_CLASS.get(et, BOOST_UNCLASSIFIED)


# ---------------------------------------------------------------------------
# Deal grouping -> completed trades (PURE given deal-like records)
# ---------------------------------------------------------------------------
def _dget(d, name, default=None):
    """Read a field off either a plain dict (fixtures/tests) or an MT5 deal
    namedtuple-like object (live). PURE."""
    if isinstance(d, dict):
        return d.get(name, default)
    return getattr(d, name, default)


def group_deals_by_position(deals):
    """PURE: group raw deal records by `position_id`. Returns
    {position_id: {'in': deal|None, 'out': deal|None}}. `entry` semantics
    follow the MT5 convention already used elsewhere in this repo (rogue.py
    `_rogue_close_pnl`): 0 = IN (open), 1 = OUT (close). A position with more
    than one OUT deal (partial closes) collapses to its LAST out deal -- a
    documented simplification (full partial-close reconstruction is out of
    scope for a daily P&L report; the FINAL close still nets to the broker's
    own realized total across all partials, so no P&L is lost, only the
    open/close TIMESTAMP granularity for a partially-closed position)."""
    groups = {}
    for d in (deals or []):
        pid = _dget(d, 'position_id')
        if pid is None:
            continue
        g = groups.setdefault(int(pid), {'in': None, 'out': None})
        entry = _dget(d, 'entry')
        if entry == 0 and g['in'] is None:
            g['in'] = d
        elif entry == 1:
            g['out'] = d
    return groups


def build_trade(position_id, group, rescue_event_index, rogue_magic=ROGUE_MAGIC_DEFAULT):
    """PURE: one completed-trade dict from a {'in':deal,'out':deal} group, or
    None if the position isn't fully closed within the window (no 'in' or no
    'out' deal present -- a still-open or straddling-the-window position is
    excluded from THIS day's completed-trade count, matching "trades closed
    today" as the report's unit, not "trades touched today")."""
    din, dout = group.get('in'), group.get('out')
    if din is None or dout is None:
        return None
    comment = _dget(din, 'comment') or _dget(dout, 'comment')
    magic = _dget(din, 'magic', _dget(dout, 'magic'))
    cls = classify_comment(comment, magic, rogue_magic=rogue_magic)
    leg_class = cls['leg_class']
    ticket = int(position_id)
    if leg_class == BOOST_UNCLASSIFIED:
        leg_class = resolve_boost_leg_class(ticket, rescue_event_index)
    pnl = round(float(_dget(dout, 'profit', 0.0) or 0.0)
                + float(_dget(dout, 'swap', 0.0) or 0.0)
                + float(_dget(dout, 'commission', 0.0) or 0.0), 2)
    return {
        'ticket': ticket, 'symbol': _dget(din, 'symbol'), 'comment': comment,
        'magic': magic, 'engine': cls['engine'], 'anchor2': cls['anchor2'],
        'side': cls['side'], 'leg_class': leg_class,
        'open_time': _dget(din, 'time'), 'close_time': _dget(dout, 'time'),
        'open_price': float(_dget(din, 'price', 0.0) or 0.0),
        'close_price': float(_dget(dout, 'price', 0.0) or 0.0),
        'pnl': pnl,
    }


def build_trades(deals, rescue_event_index=None, rogue_magic=ROGUE_MAGIC_DEFAULT):
    """PURE: raw deals -> list of completed-trade dicts (see build_trade)."""
    rescue_event_index = rescue_event_index or {}
    out = []
    for pid, g in group_deals_by_position(deals).items():
        t = build_trade(pid, g, rescue_event_index, rogue_magic=rogue_magic)
        if t is not None:
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Whipsaw detection (PURE) -- mirrors fills.py's structural `_twin_open` test:
# both straddle legs genuinely LIVE at the broker at the same time.
# ---------------------------------------------------------------------------
def detect_whipsaws(trades):
    """PURE: {anchor2: whipsaw_event_count}. A whipsaw = an opposite-side pair
    of ORIGINAL anchor trades for the same anchor whose [open_time, close_time]
    windows overlap (both legs were open at once at the broker -- the same
    signature fills.py's `_twin_open` checks live)."""
    by_anchor = {}
    for t in trades:
        if t['engine'] == ANCHOR_ENGINE and t['leg_class'] == ORIGINAL:
            by_anchor.setdefault(t['anchor2'], []).append(t)
    counts = {}
    for a, legs in by_anchor.items():
        buys = [t for t in legs if t['side'] == 'BUY']
        sells = [t for t in legs if t['side'] == 'SELL']
        used = set()
        n = 0
        for b in buys:
            for i, s in enumerate(sells):
                if i in used:
                    continue
                if (b['open_time'] is not None and b['close_time'] is not None
                        and s['open_time'] is not None and s['close_time'] is not None
                        and b['open_time'] <= s['close_time']
                        and s['open_time'] <= b['close_time']):
                    n += 1
                    used.add(i)
                    break
        counts[a] = n
    return counts


# ---------------------------------------------------------------------------
# Per-anchor stats: PF / win% / leg-class split (PURE)
# ---------------------------------------------------------------------------
_RAW_ANCHOR_KEYS = ('trades', 'orig_trades', 'net', 'gross_win', 'gross_loss',
                    'wins', 'losses', 'orig_pnl', 'rally_pnl',
                    'rescue_boost_pnl', 'fb_pnl', 'unclassified_pnl',
                    'unclassified_n', 'whipsaw_count')


_FLOAT_KEYS = {'net', 'gross_win', 'gross_loss', 'orig_pnl', 'rally_pnl',
              'rescue_boost_pnl', 'fb_pnl', 'unclassified_pnl'}


def _blank_anchor_stats():
    return {k: (0.0 if k in _FLOAT_KEYS else 0) for k in _RAW_ANCHOR_KEYS}


def _finalize_ratios(acc):
    """PURE: PF/win% computed ONCE from summed raw numbers (never averaged --
    the classic PF-of-PFs bug). Mutates and returns `acc`."""
    acc['pf'] = (round(acc['gross_win'] / acc['gross_loss'], 2)
                if acc['gross_loss'] > 0
                else (float('inf') if acc['gross_win'] > 0 else 0.0))
    decisive = acc['wins'] + acc['losses']
    acc['win_pct'] = round(100.0 * acc['wins'] / decisive, 1) if decisive else 0.0
    for k in ('net', 'orig_pnl', 'rally_pnl', 'rescue_boost_pnl', 'fb_pnl',
              'unclassified_pnl', 'gross_win', 'gross_loss'):
        acc[k] = round(acc[k], 2)
    return acc


def per_anchor_stats(trades, whipsaw_counts=None):
    """PURE: {anchor2: {trades, net, pf, win_pct, orig_pnl, rally_pnl,
    rescue_boost_pnl, fb_pnl, unclassified_pnl, unclassified_n,
    whipsaw_count, ...}} for every ANCHOR-engine trade. `pf` is `float('inf')`
    for an all-winning anchor with zero losers (rendered specially, see
    `_fmt_pf`) and `0.0` for zero trades / zero winners."""
    whipsaw_counts = whipsaw_counts or {}
    per = {}
    for t in trades:
        if t['engine'] != ANCHOR_ENGINE:
            continue
        a = t['anchor2'] or '??'
        s = per.setdefault(a, _blank_anchor_stats())
        s['trades'] += 1
        pnl = t['pnl']
        s['net'] += pnl
        if t['leg_class'] == ORIGINAL:
            s['orig_pnl'] += pnl
            s['orig_trades'] += 1
        elif t['leg_class'] == RALLY_BOOST:
            s['rally_pnl'] += pnl
        elif t['leg_class'] == RESCUE_BOOST:
            s['rescue_boost_pnl'] += pnl
        elif t['leg_class'] == TRAPPED_LATE_RESCUE:
            s['fb_pnl'] += pnl
        else:
            s['unclassified_pnl'] += pnl
            s['unclassified_n'] += 1
        if pnl > 0:
            s['gross_win'] += pnl
            s['wins'] += 1
        elif pnl < 0:
            s['gross_loss'] += -pnl
            s['losses'] += 1
    for a, s in per.items():
        s['whipsaw_count'] = whipsaw_counts.get(a, 0)
        _finalize_ratios(s)
    return per


def rollup_period(per_anchor_stats_list):
    """PURE: sum RAW fields across several per_anchor_stats() outputs (e.g.
    one per day in a month) and recompute pf/win_pct ONCE at the end -- the
    "cut/keep" month table (mirrors the June A3-cut analysis format: net, PF,
    win% per anchor per month)."""
    out = {}
    for day_stats in per_anchor_stats_list:
        for a, s in day_stats.items():
            acc = out.setdefault(a, {k: 0 for k in _RAW_ANCHOR_KEYS})
            for k in _RAW_ANCHOR_KEYS:
                acc[k] = acc.get(k, 0) + s.get(k, 0)
    for acc in out.values():
        _finalize_ratios(acc)
    return out


def _fmt_pf(pf):
    return "inf" if pf == float('inf') else f"{pf:.2f}"


# ---------------------------------------------------------------------------
# W-2 tracking: avg winner exit vs no-hold shadow delta (needs journal join --
# the modeled no-hold exit is a bot-side computation with no broker analogue)
# ---------------------------------------------------------------------------
def w2_no_hold_delta(trades, journal_index):
    """PURE given an already-loaded journal_index ({ticket: {'exit':, 'nohold':}}
    from load_journal_index): {anchor2: {'n', 'avg_actual_exit',
    'avg_nohold_exit', 'avg_delta'}} for winning ORIGINAL anchor trades that
    have a nohold_trail_exit on file. Anchors/trades with no journal match are
    simply absent (never guessed)."""
    per = {}
    for t in trades:
        if t['engine'] != ANCHOR_ENGINE or t['leg_class'] != ORIGINAL or t['pnl'] <= 0:
            continue
        j = journal_index.get(t['ticket'])
        if not j or j.get('nohold') is None or j.get('exit') is None:
            continue
        a = t['anchor2'] or '??'
        s = per.setdefault(a, {'n': 0, 'sum_actual': 0.0, 'sum_nohold': 0.0})
        s['n'] += 1
        s['sum_actual'] += j['exit']
        s['sum_nohold'] += j['nohold']
    out = {}
    for a, s in per.items():
        n = s['n']
        avg_actual = s['sum_actual'] / n
        avg_nohold = s['sum_nohold'] / n
        out[a] = {'n': n, 'avg_actual_exit': round(avg_actual, 2),
                  'avg_nohold_exit': round(avg_nohold, 2),
                  'avg_delta': round(avg_actual - avg_nohold, 2)}
    return out


# ---------------------------------------------------------------------------
# Rogue stats (PURE given trades + pre-counted log events)
# ---------------------------------------------------------------------------
def rogue_stats(trades, log_counts=None):
    """PURE: {entries, wins, fails, day_pnl, biggest_win, chain_reanchors,
    chase_rejects, cooldown_rejects, displacement_rejects, brake_events,
    loss_stop_events, fail_pause_events} from ROGUE-engine trades + the
    (already log-parsed) event counts."""
    log_counts = log_counts or {}
    rg = [t for t in trades if t['engine'] == ROGUE_ENGINE]
    wins = sum(1 for t in rg if t['pnl'] > 0)
    fails = sum(1 for t in rg if t['pnl'] <= 0)
    day_pnl = round(sum(t['pnl'] for t in rg), 2)
    biggest_win = round(max((t['pnl'] for t in rg), default=0.0), 2)
    out = {'entries': len(rg), 'wins': wins, 'fails': fails, 'day_pnl': day_pnl,
           'biggest_win': biggest_win}
    for k in ('chain_reanchors', 'chase_rejects', 'cooldown_rejects',
              'displacement_rejects', 'brake_events', 'loss_stop_events',
              'fail_pause_events'):
        out[k] = int(log_counts.get(k, 0))
    return out


def fetcher_stats(trades, log_counts=None):
    """PURE: {entries, wins, fails, day_pnl, biggest_win, reanchors, brake_events,
    loss_stop_events, fail_pause_events} from FETCHER-engine trades + the (already
    log-parsed) event counts. Entries/wins/fails/day_pnl are UNIQUE broker trades
    (one per closed position), never log lines (R-7)."""
    log_counts = log_counts or {}
    fg = [t for t in trades if t['engine'] == FETCHER_ENGINE]
    wins = sum(1 for t in fg if t['pnl'] > 0)
    fails = sum(1 for t in fg if t['pnl'] <= 0)
    day_pnl = round(sum(t['pnl'] for t in fg), 2)
    biggest_win = round(max((t['pnl'] for t in fg), default=0.0), 2)
    out = {'entries': len(fg), 'wins': wins, 'fails': fails, 'day_pnl': day_pnl,
           'biggest_win': biggest_win}
    for k in ('reanchors', 'brake_events', 'loss_stop_events', 'fail_pause_events'):
        out[k] = int(log_counts.get(k, 0))
    return out


# ---------------------------------------------------------------------------
# aureon*.log parsing (PURE given lines; impure file read isolated below)
# ---------------------------------------------------------------------------
_CHASE_RE = re.compile(r'\[ROGUE\][^\n]*CHASE-REJECT')
_COOLDOWN_RE = re.compile(r'\[ROGUE\][^\n]*CHAIN-COOLDOWN')
_DISPLACEMENT_RE = re.compile(r'\[ROGUE\][^\n]*CHAIN-DISPLACEMENT')
_REANCHOR_RE = re.compile(r'\[ROGUE\][^\n]*CHAIN re-anchor')
_CLOSE_BRAKE_RE = re.compile(r'\[ROGUE\][^\n]*\bCLOSE\b.*\|\s*(LOSS-STOP|FAIL-PAUSE|live)\s*$')


def count_rogue_log_events(log_lines):
    """PURE: count Rogue gate-reject / chain / brake lines from an iterable of
    aureon.log text lines. NOTE (episode throttling): CHASE-REJECT / CHAIN-
    COOLDOWN / CHAIN-DISPLACEMENT are logged ONCE PER EPISODE at the source
    (rogue.py `_log_chase_reject` / `_log_chain_block`), not once per tick --
    a returned count is EPISODES, not raw tick-level rejects. Never raises on
    a malformed/partial line."""
    chase = cooldown = displacement = reanchor = 0
    loss_stop = fail_pause = 0
    for line in log_lines:
        if _CHASE_RE.search(line):
            chase += 1
        if _COOLDOWN_RE.search(line):
            cooldown += 1
        if _DISPLACEMENT_RE.search(line):
            displacement += 1
        if _REANCHOR_RE.search(line):
            reanchor += 1
        m = _CLOSE_BRAKE_RE.search(line)
        if m:
            if m.group(1) == 'LOSS-STOP':
                loss_stop += 1
            elif m.group(1) == 'FAIL-PAUSE':
                fail_pause += 1
    return {'chase_rejects': chase, 'cooldown_rejects': cooldown,
            'displacement_rejects': displacement, 'chain_reanchors': reanchor,
            'loss_stop_events': loss_stop, 'fail_pause_events': fail_pause,
            'brake_events': loss_stop + fail_pause}


_FETCH_REANCHOR_RE = re.compile(r'\[FETCHER\][^\n]*re-anchor @')
_FETCH_CLOSE_BRAKE_RE = re.compile(
    r'\[FETCHER\][^\n]*\bCLOSE\b.*\|\s*(LOSS-STOP|FAIL-PAUSE|live)\s*\|')


def count_fetcher_log_events(log_lines):
    """PURE: count Fetcher re-anchor + brake lines from aureon.log text lines. Each
    Fetcher CLOSE logs ONE line carrying both the brake tag and the re-anchor level, so a
    close contributes at most one re-anchor and one brake tag -- UNIQUE events, not raw
    duplicated log/telemetry lines (R-7). Never raises on a malformed line."""
    reanchor = loss_stop = fail_pause = 0
    for line in log_lines:
        if _FETCH_REANCHOR_RE.search(line):
            reanchor += 1
        m = _FETCH_CLOSE_BRAKE_RE.search(line)
        if m:
            if m.group(1) == 'LOSS-STOP':
                loss_stop += 1
            elif m.group(1) == 'FAIL-PAUSE':
                fail_pause += 1
    return {'reanchors': reanchor, 'loss_stop_events': loss_stop,
            'fail_pause_events': fail_pause, 'brake_events': loss_stop + fail_pause}


# ---------------------------------------------------------------------------
# Markdown + CSV ledger rendering (PURE)
# ---------------------------------------------------------------------------
PNL_LEDGER_COLUMNS = (
    'date', 'scope', 'trades', 'net', 'gross_win', 'gross_loss', 'pf',
    'win_pct', 'orig_pnl', 'rally_pnl', 'rescue_boost_pnl', 'fb_pnl',
    'unclassified_pnl', 'unclassified_n', 'whipsaw_count',
    'rogue_entries', 'rogue_wins', 'rogue_fails', 'rogue_day_pnl',
    'rogue_biggest_win', 'rogue_chain_reanchors', 'rogue_chase_rejects',
    'rogue_cooldown_rejects', 'rogue_displacement_rejects',
    'rogue_brake_events',
    # v3.7.0 Fetcher engine columns (appended LAST -> old ledger files stay
    # positional-safe; a FETCHER scope row fills these, other rows leave them '').
    'fetcher_entries', 'fetcher_wins', 'fetcher_fails', 'fetcher_day_pnl',
    'fetcher_biggest_win', 'fetcher_reanchors', 'fetcher_brake_events',
)


def ledger_rows(date_str, per_anchor, rogue, fetcher=None):
    """PURE: one PNL_LEDGER_COLUMNS row per anchor scope + one 'ROGUE' row +
    (when `fetcher` is supplied) one 'FETCHER' row + one 'TOTAL' row (anchors
    summed; Rogue and Fetcher kept separate per the isolation rule -- TOTAL never
    mixes engines). Long/stable schema: adding or cutting an anchor never changes
    the column set."""
    rows = []
    total = {k: 0 for k in _RAW_ANCHOR_KEYS}
    for a in sorted(per_anchor):
        s = per_anchor[a]
        for k in _RAW_ANCHOR_KEYS:
            total[k] = total.get(k, 0) + s.get(k, 0)
        rows.append(_anchor_row(date_str, a, s))
    _finalize_ratios(total)
    rows.append(_anchor_row(date_str, 'TOTAL', total))
    rows.append({
        'date': date_str, 'scope': 'ROGUE', 'trades': rogue['entries'],
        'net': '', 'gross_win': '', 'gross_loss': '', 'pf': '', 'win_pct': '',
        'orig_pnl': '', 'rally_pnl': '', 'rescue_boost_pnl': '', 'fb_pnl': '',
        'unclassified_pnl': '', 'unclassified_n': '', 'whipsaw_count': '',
        'rogue_entries': rogue['entries'], 'rogue_wins': rogue['wins'],
        'rogue_fails': rogue['fails'], 'rogue_day_pnl': rogue['day_pnl'],
        'rogue_biggest_win': rogue['biggest_win'],
        'rogue_chain_reanchors': rogue['chain_reanchors'],
        'rogue_chase_rejects': rogue['chase_rejects'],
        'rogue_cooldown_rejects': rogue['cooldown_rejects'],
        'rogue_displacement_rejects': rogue['displacement_rejects'],
        'rogue_brake_events': rogue['brake_events'],
    })
    if fetcher is not None:
        rows.append({
            'date': date_str, 'scope': 'FETCHER', 'trades': fetcher['entries'],
            'net': '', 'gross_win': '', 'gross_loss': '', 'pf': '', 'win_pct': '',
            'orig_pnl': '', 'rally_pnl': '', 'rescue_boost_pnl': '', 'fb_pnl': '',
            'unclassified_pnl': '', 'unclassified_n': '', 'whipsaw_count': '',
            'fetcher_entries': fetcher['entries'], 'fetcher_wins': fetcher['wins'],
            'fetcher_fails': fetcher['fails'], 'fetcher_day_pnl': fetcher['day_pnl'],
            'fetcher_biggest_win': fetcher['biggest_win'],
            'fetcher_reanchors': fetcher['reanchors'],
            'fetcher_brake_events': fetcher['brake_events'],
        })
    # stable schema: every row carries the FULL PNL_LEDGER_COLUMNS set (a scope that
    # doesn't populate a column leaves it '') so adding an engine's columns never makes
    # an older scope's row miss keys.
    rows = [{k: r.get(k, '') for k in PNL_LEDGER_COLUMNS} for r in rows]
    return rows


def _anchor_row(date_str, scope, s):
    return {
        'date': date_str, 'scope': scope, 'trades': s['trades'],
        'net': s['net'], 'gross_win': s['gross_win'],
        'gross_loss': s['gross_loss'], 'pf': _fmt_pf(s['pf']),
        'win_pct': s['win_pct'], 'orig_pnl': s['orig_pnl'],
        'rally_pnl': s['rally_pnl'], 'rescue_boost_pnl': s['rescue_boost_pnl'],
        'fb_pnl': s['fb_pnl'], 'unclassified_pnl': s['unclassified_pnl'],
        'unclassified_n': s['unclassified_n'],
        'whipsaw_count': s['whipsaw_count'],
        'rogue_entries': '', 'rogue_wins': '', 'rogue_fails': '',
        'rogue_day_pnl': '', 'rogue_biggest_win': '',
        'rogue_chain_reanchors': '', 'rogue_chase_rejects': '',
        'rogue_cooldown_rejects': '', 'rogue_displacement_rejects': '',
        'rogue_brake_events': '',
    }


def render_markdown(date_str, per_anchor, rogue, whipsaw_counts=None,
                    w2=None, month_rollup_stats=None, month_str=None,
                    unclassified_note=None, fetcher=None):
    """PURE: the full markdown report body for one day (+ optional month
    roll-up table when `month_rollup_stats` is supplied). `fetcher` (when supplied)
    adds a Fetcher engine section."""
    whipsaw_counts = whipsaw_counts or {}
    w2 = w2 or {}
    lines = [f"# AUREON daily P&L report — {date_str}", ""]

    lines += ["## Per anchor", "",
             "| anchor | trades | net | PF | win% | whipsaws | orig | rally | "
             "rescue-boost | F-B | unclass |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    day_net = 0.0
    for a in sorted(per_anchor):
        s = per_anchor[a]
        day_net += s['net']
        lines.append(
            f"| {a} | {s['trades']} | ${s['net']:+.2f} | {_fmt_pf(s['pf'])} | "
            f"{s['win_pct']:.1f}% | {s['whipsaw_count']} | ${s['orig_pnl']:+.2f} | "
            f"${s['rally_pnl']:+.2f} | ${s['rescue_boost_pnl']:+.2f} | "
            f"${s['fb_pnl']:+.2f} | ${s['unclassified_pnl']:+.2f} "
            f"({s['unclassified_n']}) |")
    lines += ["", f"**Anchor net: ${day_net:+.2f}**", ""]

    if w2:
        lines += ["## W-2 tracking — avg winner exit vs no-hold shadow", "",
                 "| anchor | n | avg actual exit | avg no-hold exit | avg delta |",
                 "|---|---:|---:|---:|---:|"]
        for a in sorted(w2):
            r = w2[a]
            lines.append(f"| {a} | {r['n']} | {r['avg_actual_exit']:.2f} | "
                        f"{r['avg_nohold_exit']:.2f} | {r['avg_delta']:+.2f} |")
        lines.append("")

    lines += ["## Rogue", "",
             f"- Entries: {rogue['entries']} (wins {rogue['wins']} / "
             f"fails {rogue['fails']})",
             f"- Day P&L: ${rogue['day_pnl']:+.2f}",
             f"- Biggest win: ${rogue['biggest_win']:+.2f}",
             f"- Chain re-anchors: {rogue['chain_reanchors']}",
             f"- Gate rejects (episodes, not raw ticks): chase "
             f"{rogue['chase_rejects']} · cooldown {rogue['cooldown_rejects']} · "
             f"displacement {rogue['displacement_rejects']}",
             f"- Brake events: {rogue['brake_events']} "
             f"(loss-stop {rogue['loss_stop_events']} / "
             f"fail-pause {rogue['fail_pause_events']})",
             ""]

    if fetcher is not None:
        lines += ["## Fetcher", "",
                 f"- Entries: {fetcher['entries']} (wins {fetcher['wins']} / "
                 f"fails {fetcher['fails']})",
                 f"- Day P&L: ${fetcher['day_pnl']:+.2f}",
                 f"- Biggest win: ${fetcher['biggest_win']:+.2f}",
                 f"- Re-anchors: {fetcher['reanchors']}",
                 f"- Brake events: {fetcher['brake_events']} "
                 f"(loss-stop {fetcher['loss_stop_events']} / "
                 f"fail-pause {fetcher['fail_pause_events']})",
                 ""]

    if month_rollup_stats:
        lines += [f"## Month-to-date ({month_str or date_str[:7]}) — cut/keep table",
                 "", "| anchor | trades | net | PF | win% | whipsaws |",
                 "|---|---:|---:|---:|---:|---:|"]
        for a in sorted(month_rollup_stats):
            s = month_rollup_stats[a]
            lines.append(f"| {a} | {s['trades']} | ${s['net']:+.2f} | "
                        f"{_fmt_pf(s['pf'])} | {s['win_pct']:.1f}% | "
                        f"{s['whipsaw_count']} |")
        lines.append("")

    if unclassified_note:
        lines += ["## Data-quality notes", "", unclassified_note, ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Impure readers -- MT5 history sweep, rescue_events.csv join, journal join,
# log file read. Each degrades to empty/None on any error; never raises.
# ---------------------------------------------------------------------------
def ist_day_window_utc(date_str):
    """(dt_from, dt_to) UTC datetimes spanning ONE IST calendar day [00:00,
    24:00) -- matches journal.py's `date_ist` convention (the existing
    per-trade day-key everywhere else in this codebase), so a dailyreport for
    date_str lines up with the SAME day's rows in trades_<month>.csv. MT5
    deal.time is always a true UTC epoch second (a different, more reliable
    convention than the tick-timestamp broker-offset ambiguity documented
    elsewhere in this repo -- deals are historical broker-server records, not
    live ticks)."""
    d = datetime.strptime(date_str, '%Y-%m-%d')
    ist_offset = timedelta(hours=5, minutes=30)
    dt_from = (d - ist_offset).replace(tzinfo=timezone.utc)
    dt_to = (d + timedelta(days=1) - ist_offset).replace(tzinfo=timezone.utc)
    return dt_from, dt_to


def fetch_deals_for_range(adapter, dt_from, dt_to):
    """Impure: adapter.mt5.history_deals_get(dt_from, dt_to) -- a bulk
    date-ranged sweep (a NEW access pattern for this codebase; existing code
    only reads history per-ticket via `history_deals_get(position=ticket)`).
    Never raises; a broker/adapter error returns [] so the report degrades to
    an empty day instead of crashing."""
    try:
        deals = adapter.mt5.history_deals_get(dt_from, dt_to)
        return list(deals) if deals else []
    except Exception as e:
        log.warning(f"pnl_report: history_deals_get({dt_from},{dt_to}) failed: {e!r}")
        return []


def _rescue_events_path(run_dir):
    return os.path.join(run_dir, 'rescue_events.csv')


def load_rescue_event_index(run_dir):
    """Impure: {ticket:int -> event_type:str} from run/rescue_events.csv's
    boost1_ticket/boost2_ticket columns. Missing file / parse error -> {}."""
    path = _rescue_events_path(run_dir)
    idx = {}
    if not os.path.exists(path):
        return idx
    try:
        with open(path, newline='') as f:
            for r in csv.DictReader(f):
                et = (r.get('event_type') or '').strip()
                if not et:
                    continue
                for col in ('boost1_ticket', 'boost2_ticket'):
                    tk = r.get(col)
                    if tk:
                        try:
                            idx[int(tk)] = et
                        except (TypeError, ValueError):
                            pass
    except (OSError, csv.Error) as e:
        log.warning(f"pnl_report: rescue_events.csv read failed: {e!r}")
        return {}
    return idx


def load_journal_index(run_dir, date_str):
    """Impure: {ticket:int -> {'exit': float|None, 'nohold': float|None}} for
    rows in run/journal/trades_<YYYY-MM>.csv matching date_ist==date_str.
    Missing file / column / parse error -> {} (the W-2 section is simply
    omitted, never guessed)."""
    month = date_str[:7]
    path = os.path.join(run_dir, 'journal', f'trades_{month}.csv')
    idx = {}
    if not os.path.exists(path):
        return idx
    try:
        with open(path, newline='') as f:
            for r in csv.DictReader(f):
                if r.get('date_ist') != date_str:
                    continue
                tk = r.get('ticket')
                if not tk:
                    continue
                try:
                    tk = int(tk)
                except (TypeError, ValueError):
                    continue
                nh = r.get('nohold_trail_exit')
                idx[tk] = {
                    'exit': _safe_float(r.get('actual_exit_price')),
                    'nohold': _safe_float(nh) if nh not in (None, '') else None,
                }
    except (OSError, csv.Error) as e:
        log.warning(f"pnl_report: journal CSV read failed: {e!r}")
        return {}
    return idx


def _safe_float(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _logs_dir():
    return os.environ.get("AUREON_LOG_DIR", "./logs")


def read_log_lines_for_date(logs_dir, date_str, today_str=None):
    """Impure: lines of the aureon log file covering `date_str` (YYYY-MM-DD).
    Today -> logs/aureon.log; any other day -> the daily-rotated backup
    logs/aureon.log.<date_str> (utils.setup_logging's TimedRotatingFileHandler
    naming, 30-day retention). Missing / rotated-out file -> [] (never
    raises)."""
    today_str = today_str or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    fname = 'aureon.log' if date_str == today_str else f'aureon.log.{date_str}'
    path = os.path.join(logs_dir, fname)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.readlines()
    except OSError as e:
        log.warning(f"pnl_report: log read failed ({path}): {e!r}")
        return []


def run_dir_default():
    return os.environ.get("AUREON_RUN_DIR", "./run")


# ---------------------------------------------------------------------------
# Orchestration for one day
# ---------------------------------------------------------------------------
def build_day_report(adapter, date_str, run_dir=None, logs_dir=None,
                     rogue_magic=ROGUE_MAGIC_DEFAULT):
    """Impure orchestrator: pull MT5 history for `date_str` (IST calendar
    day), join rescue_events.csv + the journal CSV + the day's log file, and
    return {'trades', 'per_anchor', 'whipsaw_counts', 'rogue', 'w2',
    'unclassified'} -- everything render_markdown/ledger_rows need. Never
    raises (every reader below degrades independently)."""
    run_dir = run_dir or run_dir_default()
    logs_dir = logs_dir or _logs_dir()
    dt_from, dt_to = ist_day_window_utc(date_str)
    deals = fetch_deals_for_range(adapter, dt_from, dt_to)
    rescue_idx = load_rescue_event_index(run_dir)
    trades = build_trades(deals, rescue_idx, rogue_magic=rogue_magic)
    whipsaw_counts = detect_whipsaws(trades)
    per_anchor = per_anchor_stats(trades, whipsaw_counts)
    journal_idx = load_journal_index(run_dir, date_str)
    w2 = w2_no_hold_delta(trades, journal_idx)
    log_lines = read_log_lines_for_date(logs_dir, date_str)
    log_counts = count_rogue_log_events(log_lines)
    rogue = rogue_stats(trades, log_counts)
    fetcher = fetcher_stats(trades, count_fetcher_log_events(log_lines))
    unclassified = [t for t in trades if t['leg_class'] in (UNKNOWN, BOOST_UNCLASSIFIED)]
    return {'date': date_str, 'trades': trades, 'per_anchor': per_anchor,
            'whipsaw_counts': whipsaw_counts, 'rogue': rogue, 'fetcher': fetcher,
            'w2': w2, 'unclassified': unclassified}


def _unclassified_note(unclassified):
    if not unclassified:
        return None
    n_unknown = sum(1 for t in unclassified if t['leg_class'] == UNKNOWN)
    n_boost = sum(1 for t in unclassified if t['leg_class'] == BOOST_UNCLASSIFIED)
    parts = []
    if n_boost:
        parts.append(f"{n_boost} boost ticket(s) had no matching rescue_events.csv "
                     f"row (fleet event not finalized yet, or the file doesn't reach "
                     f"back this far) -- kept OUT of the RALLY/RESCUE/F-B split, "
                     f"counted in 'unclass' instead of guessed.")
    if n_unknown:
        parts.append(f"{n_unknown} deal(s) had a comment that matched none of the "
                     f"known AUR_* patterns -- shown, not dropped.")
    return " ".join(parts)


def write_report_files(day_report, run_dir=None, month_rollup_stats=None,
                       month_str=None):
    """Write run/reports/daily_<date>.md and append the day's rows to
    run/reports/pnl_ledger.csv. Returns (md_path, csv_path). Creates
    run/reports/ if missing. Never raises past a best-effort try (caller
    decides whether a write failure should be visible)."""
    run_dir = run_dir or run_dir_default()
    out_dir = os.path.join(run_dir, 'reports')
    os.makedirs(out_dir, exist_ok=True)
    date_str = day_report['date']
    note = _unclassified_note(day_report['unclassified'])
    md = render_markdown(date_str, day_report['per_anchor'], day_report['rogue'],
                         whipsaw_counts=day_report['whipsaw_counts'],
                         w2=day_report['w2'], month_rollup_stats=month_rollup_stats,
                         month_str=month_str, unclassified_note=note,
                         fetcher=day_report.get('fetcher'))
    md_path = os.path.join(out_dir, f"daily_{date_str}.md")
    with open(md_path, 'w') as f:
        f.write(md)
    csv_path = os.path.join(out_dir, 'pnl_ledger.csv')
    rows = ledger_rows(date_str, day_report['per_anchor'], day_report['rogue'],
                       fetcher=day_report.get('fetcher'))
    upsert_ledger_rows(csv_path, date_str, rows)
    return md_path, csv_path


def upsert_ledger_rows(csv_path, date_str, new_rows):
    """Idempotent write: replace any existing rows for `date_str` (keyed on
    the (date, scope) pair) with `new_rows`, keeping every other date's rows
    untouched. Re-running a report for the same day (a manual CLI re-run, or
    the EOD hook somehow firing twice) must never duplicate ledger rows. A
    missing/corrupt existing file is treated as empty, never raised."""
    existing = []
    if os.path.exists(csv_path):
        try:
            with open(csv_path, newline='') as f:
                existing = [r for r in csv.DictReader(f) if r.get('date') != date_str]
        except (OSError, csv.Error) as e:
            log.warning(f"pnl_report: pnl_ledger.csv read failed (rewriting fresh): {e!r}")
            existing = []
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=PNL_LEDGER_COLUMNS)
        w.writeheader()
        for r in existing:
            w.writerow(r)
        for r in new_rows:
            w.writerow(r)


def build_discord_card(day_report):
    """Impure-safe (no network): build the Discord embed dict for one day's
    report via discord_cards. Returns None if discord_cards import fails
    (never raises onto the caller)."""
    try:
        import discord_cards as dc
    except Exception:
        return None
    per_anchor = day_report['per_anchor']
    rogue = day_report['rogue']
    date_str = day_report['date']
    day_net = round(sum(s['net'] for s in per_anchor.values()), 2)
    fields = []
    for a in sorted(per_anchor):
        s = per_anchor[a]
        fields.append((a, f"${s['net']:+.2f} | PF {_fmt_pf(s['pf'])} | "
                          f"{s['win_pct']:.0f}% | {s['trades']}t | "
                          f"{s['whipsaw_count']}ws"))
    fields.append(("ROGUE", f"${rogue['day_pnl']:+.2f} | {rogue['entries']}e "
                            f"({rogue['wins']}W/{rogue['fails']}F) | "
                            f"chain {rogue['chain_reanchors']}"))
    fields.append(("Anchor net", f"${day_net:+.2f}"))
    return dc.build_embed(f"📈 AUREON daily P&L — {date_str}", dc.GREEN if day_net >= 0 else dc.RED,
                          fields=fields)


# ---------------------------------------------------------------------------
# Live EOD hook (bound-method idiom used throughout this codebase)
# ---------------------------------------------------------------------------
def run_eod_report(self, broker_date):
    """Live hook: called once per broker day from live_trader.py's EOD branch
    (guarded by cfg.util_daily_pnl_report + the same firebase_eod_date-style
    once-per-day gate the caller already checks). READ-ONLY: opens no new MT5
    session (reuses self.adapter), places no orders, never touches
    shadow_positions/governors. Guarded top-to-bottom; a failure here can
    never block the EOD path -- caller wraps this in try/except too, but this
    also self-guards for direct/CLI reuse."""
    try:
        if not bool(getattr(self.cfg, 'util_daily_pnl_report', True)):
            return None
        date_str = str(broker_date)
        rep = build_day_report(self.adapter, date_str, run_dir=self.run_dir)
        month_stats = None
        try:
            month_stats = month_to_date_rollup(self.adapter, date_str, run_dir=self.run_dir)
        except Exception as e:
            log.warning(f"pnl_report: month rollup failed (day report unaffected): {e!r}")
        md_path, csv_path = write_report_files(rep, run_dir=self.run_dir,
                                               month_rollup_stats=month_stats,
                                               month_str=date_str[:7])
        card = build_discord_card(rep)
        day_net = round(sum(s['net'] for s in rep['per_anchor'].values()), 2)
        from telemetry import Severity
        self.tele.send(
            f"📈 Daily P&L report {date_str}: anchor net ${day_net:+.2f}, "
            f"Rogue ${rep['rogue']['day_pnl']:+.2f} -> {md_path}",
            Severity.INFO, important=True,
            card=card, event_key=f"dailypnl:{date_str}")
        return md_path, csv_path
    except Exception as e:
        log.warning(f"pnl_report: run_eod_report non-fatal: {e!r}")
        return None


def month_to_date_rollup(adapter, date_str, run_dir=None):
    """Build the month-to-date per-anchor roll-up (1st of the month through
    date_str inclusive) by running build_day_report for each day and summing
    via rollup_period. Bounded to <=31 iterations; a day with no deals just
    contributes zeros."""
    run_dir = run_dir or run_dir_default()
    d = datetime.strptime(date_str, '%Y-%m-%d')
    first = d.replace(day=1)
    day_stats = []
    cur = first
    while cur <= d:
        cur_str = cur.strftime('%Y-%m-%d')
        rep = build_day_report(adapter, cur_str, run_dir=run_dir)
        day_stats.append(rep['per_anchor'])
        cur += timedelta(days=1)
    return rollup_period(day_stats)


# ---------------------------------------------------------------------------
# CLI entrypoint: python bot.py dailyreport [YYYY-MM-DD|YYYY-MM]
# ---------------------------------------------------------------------------
def run_dailyreport(date_arg=None):
    """CLI (python bot.py dailyreport [YYYY-MM-DD|YYYY-MM]). Read-only: opens
    its own MT5Adapter for history reads only, never touches the broker's
    order book. A bare YYYY-MM-DD runs ONE day; YYYY-MM runs the whole month
    (each day + the month roll-up). Prints a console summary, writes the
    markdown + CSV ledger files, and returns an exit code (0 success, 1 on a
    hard error such as an unparseable date or a failed MT5 connect)."""
    import sys as _sys
    run_dir = run_dir_default()
    if not date_arg:
        date_arg = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    date_arg = str(date_arg).strip()
    is_month = re.fullmatch(r'\d{4}-\d{2}', date_arg) is not None
    is_day = re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_arg) is not None
    if not (is_month or is_day):
        print(f"dailyreport: unrecognized date '{date_arg}' -- use YYYY-MM-DD or YYYY-MM")
        return 1
    try:
        from mt5_adapter import MT5Adapter
        from config import Config
        adapter = MT5Adapter(Config().symbol)
    except Exception as e:
        print(f"dailyreport: could not connect to MT5: {e!r}")
        return 1
    try:
        if is_day:
            rep = build_day_report(adapter, date_arg, run_dir=run_dir)
            month_stats = month_to_date_rollup(adapter, date_arg, run_dir=run_dir)
            md_path, csv_path = write_report_files(
                rep, run_dir=run_dir, month_rollup_stats=month_stats,
                month_str=date_arg[:7])
            _print_day_summary(rep)
            print(f"\nWrote {md_path}\nAppended {csv_path}")
        else:
            year, month = int(date_arg[:4]), int(date_arg[5:7])
            first = datetime(year, month, 1)
            last_day = (datetime(year + (month == 12), (month % 12) + 1, 1)
                       - timedelta(days=1))
            today = datetime.now(timezone.utc).replace(tzinfo=None)
            end = min(last_day, today)
            cur = first
            per_day_anchor_stats = []
            last_rep = None
            while cur <= end:
                cur_str = cur.strftime('%Y-%m-%d')
                rep = build_day_report(adapter, cur_str, run_dir=run_dir)
                per_day_anchor_stats.append(rep['per_anchor'])
                write_report_files(rep, run_dir=run_dir)
                _print_day_summary(rep)
                last_rep = rep
                cur += timedelta(days=1)
            month_stats = rollup_period(per_day_anchor_stats)
            print(f"\n## Month {date_arg} roll-up")
            for a in sorted(month_stats):
                s = month_stats[a]
                print(f"  {a}: trades={s['trades']} net=${s['net']:+.2f} "
                     f"PF={_fmt_pf(s['pf'])} win%={s['win_pct']:.1f} "
                     f"whipsaws={s['whipsaw_count']}")
        return 0
    finally:
        try:
            adapter.shutdown()
        except Exception:
            pass


def _print_day_summary(rep):
    print(f"\n=== {rep['date']} ===")
    for a in sorted(rep['per_anchor']):
        s = rep['per_anchor'][a]
        print(f"  {a}: trades={s['trades']} net=${s['net']:+.2f} "
             f"PF={_fmt_pf(s['pf'])} win%={s['win_pct']:.1f} "
             f"whipsaws={s['whipsaw_count']}")
    r = rep['rogue']
    print(f"  ROGUE: entries={r['entries']} ({r['wins']}W/{r['fails']}F) "
         f"day_pnl=${r['day_pnl']:+.2f} chain={r['chain_reanchors']}")
