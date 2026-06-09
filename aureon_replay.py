#!/usr/bin/env python3
"""
AUREON month backtester  —  tick-resolution, BOT-FAITHFUL.

Goal: replay a FULL MONTH the way the live bot actually trades, so the number
is honest (no look-ahead) and you can (a) see every anchor every day, incl A1,
and (b) understand how a single day printed a big figure.

WHAT MAKES THIS DIFFERENT FROM aureon_replay.py
------------------------------------------------
The old replay had trades typed in by hand and trailed on every tick for 6h.
That is look-ahead and overstates badly. THIS one mirrors the bot:

  ENTRY (per day, per anchor A1/A2/A3/A4):
    - capture anchor price at the anchor minute (M1 close at that time)
    - place BUY stop = anchor+5, SELL stop = anchor-5
    - whichever side price touches FIRST fills (OCO); sibling cancelled
    - if neither touched before EOD, no trade that anchor

  MANAGEMENT (mirrors update_position_on_bar + _manage_trails_on_bar_close):
    - trail decisions ONLY on M1 bar close (once per minute), off bar high/low
    - freeze_minutes after fill: SL stays at initial, no arm/lock
    - BE lock at +$3 (snaps SL to entry)
    - trail arms at be_trigger (your +2.5), SL = peak - trail_gap
    - optional checkpoints
    - stop is checked intrabar on ticks (a tick through SL exits) — this is the
      ONLY tick-level action; everything else is bar-close, like the bot

  EXIT:
    - SL touched (initial / breakeven / trail)
    - TP touched
    - EOD flatten at 23:00 broker (20:00 UTC) — NO multi-hour overhang

DATA
----
  Needs M1 bars (for anchor capture + trail) and ticks (for fill + stop touch)
  for the whole month. Two sources:

    --csv-m1 ticks_or_m1.csv   : an M1 OHLC CSV (time,open,high,low,close)
    --csv-ticks ticks.csv      : a tick CSV (time,bid,ask)   [optional but better]

  If only M1 is given, fills/stops are approximated from bar high/low (coarser).
  If ticks are given, fills/stops use ticks (faithful). Month is sliced from the
  data automatically by --start/--end.

  Caching: parsed per-day windows are cached under ./bt_cache/ so re-runs with
  different RULES (gap, arm, checkpoints) DON'T re-read the big file.

USAGE
-----
  # first run — parses + caches per day
  python aureon_month_bt.py --csv-ticks ticks_june.csv --start 2026-06-01 --end 2026-06-30

  # re-run with different rules — uses cache, fast
  python aureon_month_bt.py --start 2026-06-01 --end 2026-06-30 --trail-gap 0.5
  python aureon_month_bt.py --start 2026-06-01 --end 2026-06-30 --checkpoints 5:3,7:5,9:7

  # inspect ONE day in detail (see how the big day happened, tick by tick decisions)
  python aureon_month_bt.py --start 2026-06-05 --end 2026-06-05 --verbose

OUTPUT
------
  Per-trade table (date, anchor, side, entry, fill_time, exit, exit_time,
  reason, peak, pnl), per-day totals, per-anchor totals (A1..A4 standalone),
  and a month summary with win rate, max DD, best/worst day.

This is SIMULATION ONLY. Nothing is sent to a broker.
"""

import argparse, csv, os, sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date as DateType
from typing import Optional, List, Tuple, Dict

BROKER_OFFSET = 3  # broker = UTC+3
CACHE_DIR = "bt_cache"

# Anchors: (label, broker_hour, broker_minute)
ANCHORS = [
    ("A1_02h_Asia",      6, 40),   # changed to match today's live test time
    ("A2_10h_London",   10,  0),
    ("A3_1340_Overlap", 13, 50),
    ("A4_1640_NYopen",  16, 40),
]

@dataclass
class Rules:
    trigger_dist:   float = 5.00
    sl_dist:        float = 18.00
    tp_dist:        float = 30.00      # set high (e.g. 100) to mimic 'no TP'
    contract_size:  float = 100.0
    lot:            float = 0.54
    be_trigger:     float = 2.50       # trail arms here
    be_lock:        float = 3.00       # SL -> entry once peak crosses this
    trail_gap:      float = 1.50
    freeze_minutes: int   = 15
    min_step:       float = 0.10
    spread:         float = 0.0        # $ spread estimate; bar-extreme peak is haircut by this so the trail rides the EXECUTABLE side, not mid. 0 = old behaviour.
    no_oco:         bool  = False       # False = OCO (sibling cancelled on first fill). True = both stops stay live; sibling can fill as a 2nd independent trade.
    reverse_at:     float = 0.0          # STOP-AND-REVERSE: if >0, OCO fill that goes this many $ underwater (from fill price) is CLOSED and an opposite leg opened at that price, then trailed normally. ONE flip max per anchor. 0 = off. Mutually exclusive with no_oco.
    checkpoints: list = field(default_factory=lambda: [])  # [(peak, locked_profit)]
    eod_broker_hour: int = 23


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def anchor_utc(d: DateType, bh: int, bm: int) -> datetime:
    # broker time -> UTC
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(hours=bh - BROKER_OFFSET, minutes=bm)

def eod_utc(d: DateType, rules: Rules) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(hours=rules.eod_broker_hour - BROKER_OFFSET)


# ---------------------------------------------------------------------------
# DATA LOADING  (streaming + per-day cache)
# ---------------------------------------------------------------------------

def _parse_ts(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    try:
        if raw.replace('.', '', 1).isdigit():
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        ts = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def load_m1(path: str, t0: datetime, t1: datetime):
    """Yield (ts, o,h,l,c) M1 bars in window. Auto-detect columns."""
    with open(path, newline='') as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            return
        c = {x.lower(): x for x in r.fieldnames}
        tc = next((c[k] for k in ('time','timestamp','utc','datetime') if k in c), None)
        oc, hc, lc, cc = c.get('open'), c.get('high'), c.get('low'), c.get('close')
        if not (tc and oc and hc and lc and cc):
            sys.exit(f"M1 CSV needs time,open,high,low,close. Found {r.fieldnames}")
        for row in r:
            ts = _parse_ts(row[tc])
            if ts is None or ts < t0 or ts > t1:
                continue
            try:
                yield ts, float(row[oc]), float(row[hc]), float(row[lc]), float(row[cc])
            except Exception:
                continue

def load_ticks(path: str, t0: datetime, t1: datetime):
    """Yield (ts, bid, ask) ticks in window."""
    with open(path, newline='') as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            return
        c = {x.lower(): x for x in r.fieldnames}
        tc = next((c[k] for k in ('time','timestamp','utc','datetime') if k in c), None)
        bc, ac = c.get('bid'), c.get('ask')
        if not (tc and bc and ac):
            sys.exit(f"Tick CSV needs time,bid,ask. Found {r.fieldnames}")
        for row in r:
            ts = _parse_ts(row[tc])
            if ts is None or ts < t0 or ts > t1:
                continue
            try:
                yield ts, float(row[bc]), float(row[ac])
            except Exception:
                continue

def ticks_to_m1(ticks):
    """Build M1 OHLC from ticks (mid price) when no M1 file is given."""
    bars = {}
    for ts, bid, ask in ticks:
        mid = (bid + ask) / 2
        key = ts.replace(second=0, microsecond=0)
        if key not in bars:
            bars[key] = [mid, mid, mid, mid]
        else:
            b = bars[key]
            b[1] = max(b[1], mid); b[2] = min(b[2], mid); b[3] = mid
    return [(k, v[0], v[1], v[2], v[3]) for k, v in sorted(bars.items())]


# ---------------------------------------------------------------------------
# SINGLE-TRADE MANAGEMENT  (mirrors the bot)
# ---------------------------------------------------------------------------

def manage_trade(side, entry, fill_time, m1_bars, ticks_after, rules, eod, allow_reverse=False):
    """
    m1_bars: list of (ts,o,h,l,c) AFTER fill, ascending, up to EOD.
    ticks_after: list of (ts,bid,ask) AFTER fill, ascending, up to EOD (for stop touch).
    Returns (exit_price, exit_reason, exit_time, peak_fav, timeline).

    Trail decisions happen on M1 close (like _manage_trails_on_bar_close).
    Stop touches are checked on ticks between bar closes (like the broker stop).

    allow_reverse: if True and rules.reverse_at>0, a leg that goes reverse_at $
    underwater (from fill) BEFORE going favorable is closed early with reason
    'REVERSE' at the -reverse_at price, so the caller can open the opposite leg.
    The reversed leg itself is managed with allow_reverse=False (one flip max).
    """
    sl = entry - rules.sl_dist if side == 'BUY' else entry + rules.sl_dist
    tp = entry + rules.tp_dist if side == 'BUY' else entry - rules.tp_dist
    peak = 0.0
    timeline = []   # (ts, unrealized_fav_price) sampled at each bar close — for intraday equity curve

    # interleave: walk ticks; at each M1 boundary, run the bar-close trail update
    bar_idx = 0
    nbars = len(m1_bars)

    def fav_of(price):
        return (price - entry) if side == 'BUY' else (entry - price)

    def done(exit_price, reason, exit_time):
        # final realized point on the timeline
        timeline.append((exit_time, fav_of(exit_price)))
        return exit_price, reason, exit_time, peak, timeline

    for ts, bid, ask in ticks_after:
        if ts > eod:
            break
        mark = bid if side == 'BUY' else ask
        # stop touch (broker-side, intrabar)
        if (side == 'BUY' and bid <= sl) or (side == 'SELL' and ask >= sl):
            if side == 'BUY':
                is_init = sl <= entry - rules.sl_dist + 0.01
            else:
                is_init = sl >= entry + rules.sl_dist - 0.01
            reason = 'SL_initial' if is_init else ('SL_be' if abs(sl-entry) < 0.05 else 'SL_trail')
            return done(sl, reason, ts)
        # STOP-AND-REVERSE trigger: if enabled, and this leg has gone reverse_at $
        # underwater while never having armed (peak still below be_trigger), close
        # here and signal a flip. Guard on peak<be_trigger so a trade that already
        # ran favorable and is now pulling back is handled by the trail, not flipped.
        if allow_reverse and rules.reverse_at > 0 and peak < rules.be_trigger:
            adverse = (entry - bid) if side == 'BUY' else (ask - entry)
            if adverse >= rules.reverse_at:
                rev_price = entry - rules.reverse_at if side == 'BUY' else entry + rules.reverse_at
                return done(rev_price, 'REVERSE', ts)
        # TP touch
        if (side == 'BUY' and ask >= tp) or (side == 'SELL' and bid <= tp):
            return done(tp, 'TP', ts)
        # track peak
        f = fav_of(mark)
        if f > peak:
            peak = f
        # run bar-close trail updates for any bars that have now closed
        while bar_idx < nbars and m1_bars[bar_idx][0] <= ts:
            _, bo, bh, bl, bc = m1_bars[bar_idx]
            bar_idx += 1
            # sample unrealized P&L at this bar close (mark-to-market on close) for
            # the intraday equity curve. Use the bar CLOSE (executable-ish).
            timeline.append((m1_bars[bar_idx-1][0], fav_of(bc)))
            # peak from bar extreme (matches update_position_on_bar), but
            # haircut by spread: the bar high/low is a mid/raw extreme, whereas
            # we could only EXIT on the near side (bid for BUY, ask for SELL).
            # Subtracting the spread makes the trailed SL ride an executable
            # level instead of an optimistic mid. spread=0 reproduces old behaviour.
            bar_extreme = (bh - rules.spread) if side == 'BUY' else (bl + rules.spread)
            bar_fav = fav_of(bar_extreme)
            if bar_fav > peak:
                peak = bar_fav
            in_freeze = (m1_bars[bar_idx-1][0] - fill_time).total_seconds()/60.0 < rules.freeze_minutes
            if in_freeze:
                continue
            # BE lock at +3
            if peak >= rules.be_lock:
                sl = max(sl, entry) if side == 'BUY' else min(sl, entry)
            # trail arm at be_trigger
            if peak >= rules.be_trigger:
                if side == 'BUY':
                    cand = (entry + peak) - rules.trail_gap
                    if cand > sl + rules.min_step:
                        sl = cand
                else:
                    cand = (entry - peak) + rules.trail_gap
                    if cand < sl - rules.min_step:
                        sl = cand
            # checkpoints
            for thr, lock in rules.checkpoints:
                if peak >= thr:
                    sl = max(sl, entry + lock) if side == 'BUY' else min(sl, entry - lock)

    # EOD flatten — close at last tick mark
    last_mark = mark if ticks_after else entry
    return done(last_mark, 'EOD', eod)


# ---------------------------------------------------------------------------
# ONE DAY  (mirrors bot anchor processing + OCO fill)
# ---------------------------------------------------------------------------

def run_day(d: DateType, m1_day, ticks_day, rules, verbose=False):
    """m1_day, ticks_day: ascending lists for the whole broker day."""
    eod = eod_utc(d, rules)
    results = []
    m1_by_min = {b[0]: b for b in m1_day}

    for label, bh, bm in ANCHORS:
        a_utc = anchor_utc(d, bh, bm)
        if a_utc >= eod:
            continue
        # anchor price = M1 close at the anchor minute (nearest within 5 min)
        anchor_price = None
        for off in range(0, 6):
            k = a_utc - timedelta(minutes=off)
            if k in m1_by_min:
                anchor_price = m1_by_min[k][4]
                break
        if anchor_price is None:
            continue
        buy_stop = round(anchor_price + rules.trigger_dist, 2)
        sell_stop = round(anchor_price - rules.trigger_dist, 2)

        # ---- FILL DETECTION ----
        # OCO (default): whichever side touches FIRST fills; sibling cancelled.
        # No-OCO (rules.no_oco): both stops stay live. The first side fills; if
        #   the OTHER side is later touched (price reversed through it), it fills
        #   too as a SECOND independent trade. One anchor can yield TWO trades.
        legs = []  # list of (side, entry, fill_time)

        first_side = first_entry = first_fill = None
        sib_side = sib_stop = None
        for ts, bid, ask in ticks_day:
            if ts < a_utc or ts > eod:
                continue
            if ask >= buy_stop:
                first_side, first_entry, first_fill = 'BUY', buy_stop, ts
                sib_side, sib_stop = 'SELL', sell_stop
                break
            if bid <= sell_stop:
                first_side, first_entry, first_fill = 'SELL', sell_stop, ts
                sib_side, sib_stop = 'BUY', buy_stop
                break
        if first_side is None:
            if verbose:
                print(f"  {label}: no fill (anchor {anchor_price:.2f}, stops {buy_stop}/{sell_stop})")
            continue
        legs.append((first_side, first_entry, first_fill))

        # No-OCO: scan AFTER the first fill for the sibling triggering
        if rules.no_oco:
            for ts, bid, ask in ticks_day:
                if ts <= first_fill or ts > eod:
                    continue
                if sib_side == 'BUY' and ask >= sib_stop:
                    legs.append(('BUY', sib_stop, ts)); break
                if sib_side == 'SELL' and bid <= sib_stop:
                    legs.append(('SELL', sib_stop, ts)); break

        # ---- MANAGE EACH LEG ----
        # legs is a queue; the SAR flip can append one opposite leg dynamically.
        leg_i = 0
        queue = list(legs)
        while leg_i < len(queue):
            side, entry, fill_time = queue[leg_i]
            m1_after = [b for b in m1_day if b[0] >= fill_time]
            ticks_after = [t for t in ticks_day if t[0] >= fill_time]
            # allow_reverse only on the FIRST leg of an OCO anchor, and only if the
            # reverse feature is on (and No-OCO is off — they're mutually exclusive).
            allow_rev = (rules.reverse_at > 0 and not rules.no_oco and leg_i == 0)
            exit_price, reason, exit_time, peak, timeline = manage_trade(
                side, entry, fill_time, m1_after, ticks_after, rules, eod,
                allow_reverse=allow_rev)
            pnl = ((exit_price - entry) if side == 'BUY' else (entry - exit_price)) * rules.contract_size * rules.lot
            if leg_i == 0:
                leg_tag = label
            elif reason == 'REVERSE' or (queue[leg_i-1][0] != side and rules.reverse_at > 0 and not rules.no_oco):
                leg_tag = f"{label}~"   # ~ marks the stop-and-reverse flipped leg
            else:
                leg_tag = f"{label}*"   # * marks the No-OCO 2nd leg
            usd_samples = [(ts, fav * rules.contract_size * rules.lot) for ts, fav in timeline]
            results.append({
                'date': str(d), 'anchor': leg_tag, 'side': side, 'entry': round(entry,2),
                'fill_time': fill_time.strftime('%H:%M:%S'), 'exit': round(exit_price,2),
                'exit_time': exit_time.strftime('%H:%M:%S'), 'reason': reason,
                'peak': round(peak,2), 'pnl': round(pnl,2),
                'samples': usd_samples,
            })
            if verbose:
                tag = " (REVERSE flip)" if leg_tag.endswith('~') else (" (2nd leg)" if leg_i else "")
                print(f"  {leg_tag} {side} entry {entry:.2f} @ {fill_time:%H:%M}{tag} "
                      f"peak +${peak:.2f} -> exit {exit_price:.2f} ({reason}) @ {exit_time:%H:%M}  ${pnl:+.2f}")
            # If this leg ended on a REVERSE signal, open ONE opposite leg at the
            # reverse price and queue it (managed with allow_reverse=False → one flip max).
            if reason == 'REVERSE':
                flip_side = 'SELL' if side == 'BUY' else 'BUY'
                queue.append((flip_side, exit_price, exit_time))
            leg_i += 1
    return results


# ---------------------------------------------------------------------------
# METRICS  (incl. intraday equity drawdown — the number FP actually checks)
# ---------------------------------------------------------------------------

def intraday_equity_dd(all_rows):
    """True intraday equity drawdown.

    End-of-day netting hides the worst moment when multiple legs are open and
    BOTH under water at once (the No-OCO risk). Here we build a per-day equity
    curve from every leg's per-bar unrealized samples PLUS realized P&L of legs
    already closed, find the lowest point each day, and take the worst across
    the month. This is closer to how Funding Pips measures the 5% rule.
    """
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in all_rows:
        by_day[r['date']].append(r)

    worst_intraday = 0.0
    worst_day = None
    for d, rows in by_day.items():
        # collect all timestamped equity-change events for the day
        # event at ts = sum of (unrealized of open legs) + (realized of closed legs)
        # Simplify: build a merged set of sample timestamps; at each, sum each
        # leg's contribution = its last sample at-or-before ts if still open,
        # or its final realized pnl if it has closed by ts.
        samples_per_leg = []
        for r in rows:
            s = r.get('samples') or []
            # parse fill/exit as same-day times for ordering
            samples_per_leg.append((r, s))
        # gather all timestamps
        all_ts = sorted({ts for _, s in samples_per_leg for ts, _ in s})
        if not all_ts:
            continue
        day_low = 0.0
        for t in all_ts:
            eq = 0.0
            for r, s in samples_per_leg:
                # find this leg's unrealized value at time t (last sample <= t)
                val = None
                for ts, usd in s:
                    if ts <= t:
                        val = usd
                    else:
                        break
                if val is not None:
                    eq += val
            if eq < day_low:
                day_low = eq
        if day_low < worst_intraday:
            worst_intraday = day_low
            worst_day = d
    return worst_intraday, worst_day


def summarize(all_rows, label):
    """Return a one-line-ish summary dict for a row set (one OCO mode)."""
    if not all_rows:
        return None
    total = sum(r['pnl'] for r in all_rows)
    wins = sum(1 for r in all_rows if r['pnl'] > 0)
    n = len(all_rows)
    per_day = {}
    for r in all_rows:
        per_day[r['date']] = per_day.get(r['date'], 0.0) + r['pnl']
    eq=0.0; pk=0.0; eod_dd=0.0
    for d in sorted(per_day):
        eq += per_day[d]; pk=max(pk,eq); eod_dd=min(eod_dd, eq-pk)
    intr_dd, intr_day = intraday_equity_dd(all_rows)
    n_full_stops = sum(1 for r in all_rows if r['reason'] == 'SL_initial')
    return {
        'label': label, 'trades': n, 'wins': wins, 'wr': 100*wins/n,
        'total': total, 'eod_dd': eod_dd, 'intr_dd': intr_dd, 'intr_day': intr_day,
        'best_day': max(per_day.values()), 'worst_day': min(per_day.values()),
        'full_stops': n_full_stops,
    }

def daterange(start: DateType, end: DateType):
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            yield d
        d += timedelta(days=1)

def main():
    ap = argparse.ArgumentParser(description="AUREON month backtester (bot-faithful, tick resolution)")
    ap.add_argument('--csv-ticks', help="Tick CSV (time,bid,ask) for the month")
    ap.add_argument('--csv-m1', help="M1 OHLC CSV (time,open,high,low,close). If omitted, built from ticks.")
    ap.add_argument('--start', required=True, help="YYYY-MM-DD")
    ap.add_argument('--end', required=True, help="YYYY-MM-DD")
    ap.add_argument('--trail-gap', type=float, default=1.5)
    ap.add_argument('--trail-arm', type=float, default=2.5)
    ap.add_argument('--be-lock', type=float, default=3.0)
    ap.add_argument('--freeze', type=int, default=15)
    ap.add_argument('--tp', type=float, default=30.0, help="TP distance; set 100 to mimic no-TP")
    ap.add_argument('--lot', type=float, default=0.54)
    ap.add_argument('--spread', type=float, default=0.0,
                    help="$ spread haircut on the trail's bar-extreme peak (e.g. 0.20 for gold). "
                         "0 = mid (optimistic). Fills/stops already use bid/ask regardless.")
    ap.add_argument('--checkpoints', default='')
    ap.add_argument('--no-oco', action='store_true',
                    help="Disable OCO: keep both stops live; the sibling can fill as a "
                         "2nd independent trade (marked anchor*). Doubles worst-case exposure.")
    ap.add_argument('--reverse-at', type=float, default=0.0,
                    help="STOP-AND-REVERSE: $ adverse move (from fill) that triggers a flip. "
                         "OCO leg that goes this far underwater (before arming) is closed and an "
                         "opposite leg opened at that price, trailed normally. ONE flip max per "
                         "anchor (flipped leg marked anchor~). 0 = off. Ignored if --no-oco set.")
    ap.add_argument('--compare', action='store_true',
                    help="Run BOTH OCO and No-OCO over the same data and print a side-by-side "
                         "summary incl. intraday-equity drawdown. Suppresses the per-trade table.")
    ap.add_argument('--verbose', action='store_true', help="print per-trade detail per day")
    args = ap.parse_args()

    if not args.csv_ticks and not args.csv_m1:
        sys.exit("Provide --csv-ticks (preferred) and/or --csv-m1")

    rules = Rules(trail_gap=args.trail_gap, be_trigger=args.trail_arm, be_lock=args.be_lock,
                  freeze_minutes=args.freeze, tp_dist=args.tp, lot=args.lot, spread=args.spread,
                  no_oco=args.no_oco, reverse_at=args.reverse_at)
    if args.checkpoints.strip():
        rules.checkpoints = [(float(a), float(b)) for a,b in (p.split(':') for p in args.checkpoints.split(','))]

    start = datetime.fromisoformat(args.start).date()
    end = datetime.fromisoformat(args.end).date()

    print(f"\nAUREON MONTH BACKTEST  {start} .. {end}")
    print(f"RULES  arm=+${rules.be_trigger}  BE=+${rules.be_lock}  gap=${rules.trail_gap}  "
          f"freeze={rules.freeze_minutes}m  SL=${rules.sl_dist}  TP=${rules.tp_dist}  lot={rules.lot}  "
          f"spread=${rules.spread}  ckpt={rules.checkpoints or 'off'}  "
          f"reverse_at={('$'+str(rules.reverse_at)) if rules.reverse_at>0 else 'off'}")
    print(f"       bot-faithful: trail on M1 close, stop on tick, EOD flatten 23:00 broker")
    print(f"       OCO: {'OFF — both stops live, sibling can 2nd-fill (marked *)' if rules.no_oco else 'ON — first fill cancels sibling'}\n")

    # ---- SINGLE-PASS LOAD: read the big file ONCE, bucket every row by broker-day ----
    # (was: re-reading the whole CSV once per day = ~26x over 2M+ rows = very slow)
    from collections import defaultdict
    def broker_day_of(ts):
        # which broker-calendar day this UTC tick belongs to
        return (ts + timedelta(hours=BROKER_OFFSET)).date()

    wanted_days = set(daterange(start, end))
    ticks_by_day = defaultdict(list)
    m1_by_day = defaultdict(list)

    if args.csv_ticks:
        print("Loading ticks (single pass)...")
        n = 0
        # widen window once: from start-day open to end-day EOD
        win0 = datetime(start.year,start.month,start.day,tzinfo=timezone.utc) - timedelta(hours=BROKER_OFFSET)
        win1 = eod_utc(end, rules) + timedelta(minutes=1)
        for ts, bid, ask in load_ticks(args.csv_ticks, win0, win1):
            bd = broker_day_of(ts)
            if bd in wanted_days:
                ticks_by_day[bd].append((ts, bid, ask)); n += 1
        print(f"  bucketed {n:,} ticks into {len(ticks_by_day)} days")
    if args.csv_m1:
        win0 = datetime(start.year,start.month,start.day,tzinfo=timezone.utc) - timedelta(hours=BROKER_OFFSET)
        win1 = eod_utc(end, rules) + timedelta(minutes=1)
        for bar in load_m1(args.csv_m1, win0, win1):
            bd = broker_day_of(bar[0])
            if bd in wanted_days:
                m1_by_day[bd].append(bar)

    # ---- COMPARE MODE: run BOTH OCO and No-OCO over the same bucketed data ----
    if args.compare:
        def run_all(no_oco_flag):
            r2 = Rules(trail_gap=args.trail_gap, be_trigger=args.trail_arm, be_lock=args.be_lock,
                       freeze_minutes=args.freeze, tp_dist=args.tp, lot=args.lot,
                       spread=args.spread, no_oco=no_oco_flag)
            if args.checkpoints.strip():
                r2.checkpoints = [(float(a), float(b)) for a,b in (p.split(':') for p in args.checkpoints.split(','))]
            rows = []
            for d in daterange(start, end):
                td = ticks_by_day.get(d, [])
                md = m1_by_day.get(d, []) if args.csv_m1 else ticks_to_m1(td)
                if not td and not md:
                    continue
                if not td:
                    td = [(b[0], b[4], b[4]) for b in md]
                rows.extend(run_day(d, md, td, r2, verbose=False))
            return rows

        oco_rows = run_all(False)
        noco_rows = run_all(True)
        oco = summarize(oco_rows, "OCO (sibling cancelled)")
        noco = summarize(noco_rows,  "No-OCO (sibling 2nd-fills)")

        FP_LIMIT = 0.05 * 50000  # $2,500 trailing on a $50k account
        def fp_flag(dd):
            return "FAILS FP" if abs(dd) > FP_LIMIT else "within FP"

        print(f"\n{'='*70}\nOCO vs No-OCO — lot {args.lot}, spread ${args.spread}\n{'='*70}")
        hdr = f"{'metric':<22}{'OCO':>22}{'No-OCO':>22}"
        print(hdr); print('-'*66)
        def row(name, a, b, money=True):
            fa = f"${a:+,.2f}" if money else f"{a}"
            fb = f"${b:+,.2f}" if money else f"{b}"
            print(f"{name:<22}{fa:>22}{fb:>22}")
        row("Total P&L",        oco['total'], noco['total'])
        row("Trades",           oco['trades'], noco['trades'], money=False)
        row("Win rate",         f"{oco['wr']:.1f}%", f"{noco['wr']:.1f}%", money=False)
        row("Full -stops (SL_initial)", oco['full_stops'], noco['full_stops'], money=False)
        row("Best day",         oco['best_day'], noco['best_day'])
        row("Worst day",        oco['worst_day'], noco['worst_day'])
        row("MaxDD (EOD-netted)", oco['eod_dd'], noco['eod_dd'])
        row("MaxDD (INTRADAY)*", oco['intr_dd'], noco['intr_dd'])
        print('-'*66)
        print(f"{'FP $50k (5%=$2.5k):':<22}{fp_flag(oco['intr_dd']):>22}{fp_flag(noco['intr_dd']):>22}")
        print(f"\n* INTRADAY drawdown = worst point with all open legs marked-to-market")
        print(f"  on the bar close, summed across concurrent legs. This is the number")
        print(f"  Funding Pips actually measures — NOT the EOD-netted figure. For")
        print(f"  No-OCO it is the real test, since two open legs can both be underwater.")
        print(f"  (OCO worst intraday day: {oco['intr_day']}; No-OCO: {noco['intr_day']})")

        # ---- WEEKLY BREAKDOWN ----
        # Group rows by ISO (year, week). Shows whether the No-OCO edge is steady
        # or carried by one or two big reversal weeks. A 3x edge driven by a single
        # week is fragile; a steady edge across every week is real.
        def iso_week(date_str):
            y, m, dd = (int(x) for x in date_str.split('-'))
            iy, iw, _ = DateType(y, m, dd).isocalendar()
            return (iy, iw)

        def weekly_rollup(rows):
            wk = {}
            for r in rows:
                k = iso_week(r['date'])
                wk.setdefault(k, []).append(r)
            out = {}
            for k, rr in wk.items():
                tot = sum(x['pnl'] for x in rr)
                w = sum(1 for x in rr if x['pnl'] > 0)
                idd, _ = intraday_equity_dd(rr)
                out[k] = (tot, len(rr), 100*w/len(rr) if rr else 0, idd)
            return out

        oco_wk = weekly_rollup(oco_rows)
        noco_wk = weekly_rollup(noco_rows)
        all_weeks = sorted(set(oco_wk) | set(noco_wk))

        print(f"\n{'='*70}\nWEEKLY BREAKDOWN — is the No-OCO edge steady or one big week?\n{'='*70}")
        print(f"{'week':<12}{'OCO P&L':>14}{'No-OCO P&L':>14}{'NoOCO intraDD':>16}{'NoOCO WR':>12}")
        print('-'*68)
        for wk in all_weeks:
            o = oco_wk.get(wk, (0,0,0,0))
            nq = noco_wk.get(wk, (0,0,0,0))
            label = f"{wk[0]}-W{wk[1]:02d}"
            print(f"{label:<12}{o[0]:>+14,.2f}{nq[0]:>+14,.2f}{nq[3]:>+16,.2f}{nq[2]:>11.1f}%")
        print('-'*68)
        # how concentrated is the No-OCO total?
        noco_weeks_sorted = sorted(noco_wk.values(), key=lambda v: v[0], reverse=True)
        top_week = noco_weeks_sorted[0][0] if noco_weeks_sorted else 0
        share = (top_week / noco['total'] * 100) if noco['total'] else 0
        print(f"No-OCO best single week = ${top_week:+,.2f} = {share:.0f}% of the month's total.")
        if share > 50:
            print("⚠ Over half the No-OCO edge is ONE week — fragile, not a steady edge.")
        else:
            print("Edge is spread across multiple weeks (more robust than a one-week fluke).")

        print(f"\nNOTE: both numbers assume the sibling/2nd leg fills exactly on tick touch.")
        print(f"That resting-order fill is NOT live-validated — demo first.\n")
        return

    all_rows = []
    for d in daterange(start, end):
        ticks_day = ticks_by_day.get(d, [])
        if args.csv_m1:
            m1_day = m1_by_day.get(d, [])
        else:
            m1_day = ticks_to_m1(ticks_day)
        if not ticks_day and not m1_day:
            continue
        if not ticks_day:
            ticks_day = [(b[0], b[4], b[4]) for b in m1_day]
        if args.verbose:
            print(f"{d}  (m1 bars={len(m1_day)}, ticks={len(ticks_day)})")
        rows = run_day(d, m1_day, ticks_day, rules, verbose=args.verbose)
        all_rows.extend(rows)

    if not all_rows:
        print("No trades. Check date range and that the CSV covers it.")
        return

    # ---- report ----
    print(f"\n{'date':<12}{'anchor':<18}{'side':<5}{'entry':>9}{'fill':>9}"
          f"{'exit':>9}{'out':>9}{'peak':>7}{'pnl':>10}")
    print('-'*88)
    for r in all_rows:
        print(f"{r['date']:<12}{r['anchor']:<18}{r['side']:<5}{r['entry']:>9.2f}{r['fill_time']:>9}"
              f"{r['exit']:>9.2f}{r['reason']:>9}{r['peak']:>7.2f}{r['pnl']:>10.2f}")

    total = sum(r['pnl'] for r in all_rows)
    wins = sum(1 for r in all_rows if r['pnl'] > 0)
    # per anchor
    # per anchor (No-OCO 2nd legs marked 'A3*' group under base 'A3')
    def base_anchor(a):
        return a[:-1] if a.endswith('*') else a
    per_anchor = {}
    for r in all_rows:
        ba = base_anchor(r['anchor'])
        per_anchor.setdefault(ba, [0,0.0])
        per_anchor[ba][0]+=1; per_anchor[ba][1]+=r['pnl']
    # per day + DD
    per_day = {}
    for r in all_rows:
        per_day[r['date']] = per_day.get(r['date'],0.0)+r['pnl']
    eq=0.0; peak_eq=0.0; maxdd=0.0
    for d in sorted(per_day):
        eq += per_day[d]; peak_eq=max(peak_eq,eq); maxdd=min(maxdd, eq-peak_eq)

    intr_dd, intr_day = intraday_equity_dd(all_rows)
    print('-'*88)
    print(f"\nTRADES {len(all_rows)}  | WINS {wins} ({100*wins/len(all_rows):.1f}%)  | TOTAL ${total:+,.2f}")
    print(f"Best day ${max(per_day.values()):+,.2f}  Worst day ${min(per_day.values()):+,.2f}  "
          f"MaxDD(EOD) ${maxdd:,.2f}")
    print(f"MaxDD(INTRADAY) ${intr_dd:,.2f}  (worst day {intr_day}) "
          f"— FP $50k 5%: {'FAILS' if abs(intr_dd) > 2500 else 'within'}")
    print("\nPer anchor (standalone — this is how you decide if A1 earns its place):")
    for a in sorted(per_anchor):
        n,p = per_anchor[a]
        print(f"  {a:<18} n={n:<3} net=${p:+,.2f}")

    # ---- ANCHOR-REMOVAL ANALYSIS ----
    # For each anchor, recompute the WHOLE-PORTFOLIO total AND max drawdown as if
    # that anchor were switched off. This answers "what if we drop A3?" correctly —
    # not just by subtracting A3's net (which ignores how it changes the daily
    # equity path and therefore the drawdown).
    def metrics_excluding(exclude):
        pd_ = {}
        for r in all_rows:
            if base_anchor(r['anchor']) == exclude:
                continue
            pd_[r['date']] = pd_.get(r['date'], 0.0) + r['pnl']
        if not pd_:
            return 0.0, 0.0, 0, 0
        e=0.0; pk=0.0; dd=0.0; w=0; nt=0
        for r in all_rows:
            if base_anchor(r['anchor']) != exclude:
                nt += 1
                if r['pnl'] > 0: w += 1
        for dd_day in sorted(pd_):
            e += pd_[dd_day]; pk=max(pk,e); dd=min(dd, e-pk)
        return e, dd, w, nt

    print("\nAnchor-removal — month TOTAL and MaxDD if that anchor is switched OFF:")
    print(f"  {'(keep all)':<18} total=${total:+,.2f}   MaxDD=${maxdd:,.2f}   "
          f"WR={100*wins/len(all_rows):.1f}%")
    for a in sorted(per_anchor):
        t_ex, dd_ex, w_ex, n_ex = metrics_excluding(a)
        delta = t_ex - total
        wr_ex = f"{100*w_ex/n_ex:.1f}%" if n_ex else "n/a"
        print(f"  drop {a:<13} total=${t_ex:+,.2f}   MaxDD=${dd_ex:,.2f}   "
              f"WR={wr_ex}   (Δtotal ${delta:+,.2f})")
    print()


if __name__ == '__main__':
    main()