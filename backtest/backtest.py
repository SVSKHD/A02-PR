"""AUREON backtest — tick-resolution monthly replay engine (v3.1.8).

The WHOLE POINT of this module is backtest == live: every strategy rule is the
LIVE function, imported, never reimplemented. This file only does plumbing —
turn ticks into M1 bars, place the No-OCO straddle, and feed bars to the live
per-bar engine. SL/TP/45m-hold/BE-gate/ladder/boost-trail all live in
strategy.update_position_on_bar; rescue classification lives in
rescue_log._branch_for; the lone-leg rule lives in fills.is_rescue_fill; anchor
geometry/time live in utils + anchors.resolved_anchor_hm.

See LIVE_RULE_SOURCES / rule_sources() for the audited list, asserted identical
by the selftest parity check.
"""
from __future__ import annotations

import os
import sys
from datetime import date as DateType
from typing import Dict, List, Optional

import pandas as pd

# --- make the repo root importable so `import strategy` etc. resolve whether
# this is run as `python backtest/back_main.py` or imported with backtest/ on
# the path. ---
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_REPO_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# === REUSED LIVE FUNCTIONS (imported, NOT reimplemented) ===================
from config import Config
from strategy import Position, update_position_on_bar, realize_pnl_usd
from utils import initial_sl, initial_tp, anchor_datetime_utc, eod_datetime_utc
from anchors import resolved_anchor_hm
from fills import is_rescue_fill
from rescue_log import _branch_for
import boosts  # v3.2.0: the SINGLE canonical lone-leg boost-trigger decision
# v3.3.0: the trail-lock root-cause guards (confirmed-price max_fav, garbage-feed
# filter, lock ladder) live in strategy and are exercised by the SAME imported
# update_position_on_bar -- so the backtest can never drift from the live fix. The
# per-position tracer is imported here too so import-path identity is assertable.
from strategy import update_max_fav, lock_level_for, lock_ladder_prices
from position_telemetry import PositionTracer

# Exposed at module level so a selftest can assert IDENTITY:
#   backtest.update_position_on_bar IS strategy.update_position_on_bar
#   backtest.plan_boost_event       IS boosts.plan_boost_event   (v3.2.0)
#   backtest.PositionTracer         IS position_telemetry.PositionTracer (v3.3.0)
plan_boost_event = boosts.plan_boost_event

__all__ = [
    'run_month', 'rule_sources', 'LIVE_RULE_SOURCES',
    'Position', 'update_position_on_bar', 'realize_pnl_usd',
    'initial_sl', 'initial_tp', 'anchor_datetime_utc', 'eod_datetime_utc',
    'resolved_anchor_hm', 'is_rescue_fill', '_branch_for', 'plan_boost_event',
    'update_max_fav', 'lock_level_for', 'lock_ladder_prices', 'PositionTracer',
]

LIVE_RULE_SOURCES = [
    'strategy.update_position_on_bar',
    'strategy.realize_pnl_usd',
    'strategy.Position',
    'strategy.update_max_fav',
    'strategy.lock_level_for',
    'utils.initial_sl',
    'utils.initial_tp',
    'utils.anchor_datetime_utc',
    'utils.eod_datetime_utc',
    'anchors.resolved_anchor_hm',
    'fills.is_rescue_fill',
    'rescue_log._branch_for',
    'boosts.plan_boost_event',
    'position_telemetry.PositionTracer',
]


def rule_sources() -> List[str]:
    """The live modules every strategy rule is sourced from (audit print +
    selftest parity check)."""
    return list(LIVE_RULE_SOURCES)


# --------------------------------------------------------------------------- #
# bar building
# --------------------------------------------------------------------------- #
def ticks_to_m1(ticks_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate ticks -> M1 OHLC from MID = (bid+ask)/2, plus per-bar mean
    spread. The live trail loop runs on M1 close, so the replay engine drives
    update_position_on_bar with these same M1 bars."""
    df = ticks_df.copy()
    df['time'] = pd.to_datetime(df['time'], utc=True)
    df = df.set_index('time').sort_index()
    df['mid'] = (df['bid'] + df['ask']) / 2.0
    df['spread'] = (df['ask'] - df['bid'])

    m1 = df['mid'].resample('1min').ohlc()
    m1['spread'] = df['spread'].resample('1min').mean()
    m1 = m1.dropna(subset=['open', 'high', 'low', 'close'])
    return m1


def _close_at_or_before(m1: pd.DataFrame, ts: pd.Timestamp) -> Optional[tuple]:
    """The (index, close) of the last M1 bar at/just before ts; None if none."""
    sub = m1.loc[:ts]
    if len(sub) == 0:
        return None
    return sub.index[-1], float(sub['close'].iloc[-1])


# --------------------------------------------------------------------------- #
# managing one leg with the LIVE per-bar engine
# --------------------------------------------------------------------------- #
def _manage_leg(pos: Position, bars: pd.DataFrame, eod_ts: pd.Timestamp, cfg: Config):
    """Drive a Position bar-by-bar through the LIVE update_position_on_bar until
    it closes; force an EOD flat at the last bar otherwise. bars = the bars
    AFTER the fill bar (the fill bar itself is not re-applied). Returns the
    closing index used."""
    last_ts = None
    last_close = None
    for ts, bar in bars.iterrows():
        if ts > eod_ts:
            break
        last_ts, last_close = ts, float(bar['close'])
        outcome = update_position_on_bar(pos, bar, ts, cfg)
        if outcome:
            return ts
    if not pos.closed:
        # EOD flatten at the last managed bar close (mirror root backtest #9 guard)
        if last_ts is None:
            last_ts = pos.entry_time
            last_close = pos.entry_price
        pos.exit_price = last_close
        pos.exit_time = last_ts
        pos.outcome = 'EOD'
        pos.closed = True
    return last_ts


def _trade_record(pos: Position, cfg: Config, *, date, anchor, label, role,
                  stop_level, is_monday_a1, event_type=None, branch=None) -> Dict:
    usd = realize_pnl_usd(pos, cfg)
    held = None
    try:
        if pos.exit_time is not None and pos.entry_time is not None:
            held = round((pos.exit_time - pos.entry_time).total_seconds() / 60.0, 1)
    except Exception:
        held = None
    return {
        'date': str(date),
        'anchor': anchor,
        'label': label,
        'role': role,
        'side': pos.side,
        'entry': round(pos.entry_price, 2),
        'exit': round(pos.exit_price, 2) if pos.exit_price is not None else None,
        'exit_reason': pos.outcome,
        'pnl_usd': round(usd, 2),
        'held_min': held,
        'slip': round(pos.entry_price - stop_level, 2),
        'is_monday_a1': bool(is_monday_a1),
        'event_type': event_type,
        'branch': branch,
        'boost': bool(pos.boost),
    }, usd


# --------------------------------------------------------------------------- #
# the month replay
# --------------------------------------------------------------------------- #
def run_month(ticks_df: pd.DataFrame, year: int, month: int, cfg: Config) -> Dict:
    """Replay one month of XAUUSD ticks through the LIVE strategy. Returns
    {trades, day_rows, summary}."""
    m1 = ticks_to_m1(ticks_df)

    month_start = pd.Timestamp(year=year, month=month, day=1)
    bdays = pd.date_range(month_start, month_start + pd.offsets.MonthEnd(0), freq='B')

    trades: List[Dict] = []
    day_rows: List[Dict] = []

    equity = cfg.starting_balance
    peak_equity = cfg.starting_balance
    max_dd_usd = 0.0
    max_dd_pct = 0.0

    kill_days: List[Dict] = []
    monday_a1_fired: List[Dict] = []

    # per-anchor accumulators (keyed by LABEL)
    anchor_tot: Dict[str, Dict] = {}
    branch_counts = {'CRASH_WIN': 0, 'WHIPSAW_LOSS': 0, 'SCRATCH': 0}
    # v3.2.0: count boost events by the canonical event_type (RALLY vs RESCUE).
    event_counts = {'RALLY_BOOST': 0, 'RESCUE_BOOST': 0}
    no_boost_counterfactual = 0.0
    raw_net = 0.0
    worst_day = {'date': None, 'net': None}
    half_sp_default = 0.10

    for d in bdays:
        broker_date = d.date()
        eod_ts = eod_datetime_utc(broker_date, cfg)
        daily_pnl = 0.0
        kill_triggered = False
        # per-anchor net for this day's table row
        a_net = {lbl: 0.0 for (lbl, _, _) in cfg.anchors}
        mon_tag = None

        for (label, base_h, base_m) in cfg.anchors:
            if kill_triggered:
                break

            h, m = resolved_anchor_hm(label, broker_date, base_h, base_m, cfg)
            at = anchor_datetime_utc(broker_date, h, cfg.broker_tz_offset_hours, m)
            is_monday_a1 = (label.startswith('A1') and broker_date.weekday() == 0)
            if at >= eod_ts:
                continue

            anchored = _close_at_or_before(m1, at)
            if anchored is None:
                continue
            _, anchor_price = anchored

            if is_monday_a1:
                mon_tag = f"{h:02d}:{m:02d}"
                monday_a1_fired.append({'date': str(broker_date),
                                        'resolved': f"{h:02d}:{m:02d}"})

            buy_stop = round(anchor_price + cfg.trigger_dist, 2)
            sell_stop = round(anchor_price - cfg.trigger_dist, 2)

            window = m1.loc[at:eod_ts]
            if len(window) < 3:
                continue

            # --- FIRST fill scan (No-OCO: both stops rest) ---
            first_side = None
            first_i = None
            for i, (ts, bar) in enumerate(window.iterrows()):
                b_hit = bar.high >= buy_stop
                s_hit = bar.low <= sell_stop
                if b_hit and s_hit:
                    # both in one bar: close>=open => the down-side filled first
                    # => SELL (mirror root backtest.py heuristic)
                    first_side = 'SELL' if bar.close >= bar.open else 'BUY'
                    first_i = i
                    break
                elif b_hit:
                    first_side = 'BUY'; first_i = i; break
                elif s_hit:
                    first_side = 'SELL'; first_i = i; break

            if first_side is None:
                continue

            fill_ts = window.index[first_i]
            half_sp = float(window['spread'].iloc[first_i]) / 2.0
            if not (half_sp == half_sp):  # NaN guard
                half_sp = half_sp_default

            # entry = stop adjusted for spread/slippage (BUY at ask side, SELL bid)
            if first_side == 'BUY':
                first_stop = buy_stop
                first_entry = round(buy_stop + half_sp, 2)
            else:
                first_stop = sell_stop
                first_entry = round(sell_stop - half_sp, 2)

            first_pos = Position(
                anchor_label=label, side=first_side, entry_price=first_entry,
                entry_time=fill_ts, current_sl=initial_sl(first_side, first_entry, cfg),
                tp_level=initial_tp(first_side, first_entry, cfg), max_fav=first_entry,
                lot=cfg.lot_size, role='normal', boost=False)

            # --- watch for the SIBLING (other stop) crossing later (No-OCO) ---
            sib_side = 'SELL' if first_side == 'BUY' else 'BUY'
            sib_stop = sell_stop if sib_side == 'SELL' else buy_stop
            sib_fill_i = None
            after = window.iloc[first_i + 1:]
            for j, (ts, bar) in enumerate(after.iterrows()):
                if sib_side == 'BUY' and bar.high >= buy_stop:
                    sib_fill_i = first_i + 1 + j; break
                if sib_side == 'SELL' and bar.low <= sell_stop:
                    sib_fill_i = first_i + 1 + j; break

            # --- manage the FIRST leg to close (or EOD) ---
            first_bars = window.iloc[first_i + 1:]
            _manage_leg(first_pos, first_bars, eod_ts, cfg)

            event_type = None
            branch = None
            rescue_legs: List[Position] = []
            boost_legs: List[Position] = []

            # --- SIBLING / LONE-LEG handling (v3.2.0 canonical trigger) ---
            # The sibling leg is treated as the lone leg: its fill price is the
            # reference. Boosts NEVER fire at that fill. We walk the post-sibling
            # bars and ask the SAME canonical decision the live path uses
            # (boosts.plan_boost_event) on each bar's high/low; the FIRST bar that
            # returns a plan is the fire point. RALLY (leg +$10, same dir) or
            # RESCUE (leg -$10, opposite). If the sibling never moves $10 -> None,
            # no boosts (event_type stays None).
            if sib_fill_i is not None and cfg.no_oco:
                sib_ts = window.index[sib_fill_i]
                sib_half = float(window['spread'].iloc[sib_fill_i]) / 2.0
                if not (sib_half == sib_half):
                    sib_half = half_sp_default
                if sib_side == 'BUY':
                    sib_entry = round(sib_stop + sib_half, 2)
                else:
                    sib_entry = round(sib_stop - sib_half, 2)

                # The sibling runs as a leg (managed below by _manage_leg). Its
                # role is 'rescue' (a No-OCO 2nd fill) -- recorded as such.
                rescue_pos = Position(
                    anchor_label=label, side=sib_side, entry_price=sib_entry,
                    entry_time=sib_ts,
                    current_sl=initial_sl(sib_side, sib_entry, cfg),
                    tp_level=initial_tp(sib_side, sib_entry, cfg),
                    max_fav=sib_entry, lot=cfg.lot_size, role='rescue', boost=False)
                _manage_leg(rescue_pos, window.iloc[sib_fill_i + 1:], eod_ts, cfg)
                rescue_legs.append(rescue_pos)

                # --- TRIGGER SCAN: walk post-sibling bars; fire on the FIRST $10 ---
                fire_i = None
                plan = None
                if cfg.rescue_boost_enabled:
                    post = window.iloc[sib_fill_i + 1:]
                    for k, (ts_k, bar_k) in enumerate(post.iterrows()):
                        if ts_k > eod_ts:
                            break
                        # test BOTH extremes of the bar (high then low); the first
                        # to clear $10 from the sibling fill wins.
                        for px in (float(bar_k.high), float(bar_k.low)):
                            cand = boosts.plan_boost_event(sib_side, sib_entry, px, cfg)
                            if cand is not None:
                                plan = cand
                                fire_i = sib_fill_i + 1 + k
                                break
                        if plan is not None:
                            break

                if plan is not None:
                    # A boost EVENT fires. event_type from the plan:
                    # RALLY_BOOST (leg winning) / RESCUE_BOOST (leg losing).
                    event_type = plan.event_type
                    b_sgn = 1.0 if plan.boost_side == 'BUY' else -1.0
                    fire_half = float(window['spread'].iloc[fire_i]) / 2.0
                    if not (fire_half == fire_half):
                        fire_half = half_sp_default
                    boost_entry = round(plan.entry_ref + b_sgn * fire_half, 2)
                    boost_bars = window.iloc[fire_i + 1:]
                    for _b in range(int(plan.n)):
                        bpos = Position(
                            anchor_label=label, side=plan.boost_side,
                            entry_price=boost_entry, entry_time=window.index[fire_i],
                            current_sl=round(boost_entry - b_sgn * plan.sl_dollars, 2),
                            tp_level=round(boost_entry + b_sgn * plan.tp_dollars, 2),
                            max_fav=boost_entry, lot=cfg.lot_size,
                            role='rescue', boost=True)
                        _manage_leg(bpos, boost_bars, eod_ts, cfg)
                        boost_legs.append(bpos)

                    # classify the boost EVENT on the boosts' combined P&L
                    orig_pnl = realize_pnl_usd(first_pos, cfg) + \
                        sum(realize_pnl_usd(p, cfg) for p in rescue_legs)
                    boost_pnl = sum(realize_pnl_usd(p, cfg) for p in boost_legs)
                    # -$700 cap (clamp): model the live hard-close of the boosts'
                    # combined loss BEFORE classifying the branch.
                    _cap = boosts.boost_whipsaw_cap(cfg)
                    if boost_pnl < -_cap:
                        boost_pnl = -_cap
                    branch = _branch_for(boost_pnl)
                    branch_counts[branch] += 1
                    event_counts[event_type] += 1
                    # no-boost counterfactual = orig legs alone (rescue+original)
                    no_boost_counterfactual += orig_pnl
                # else: sibling never moved $10 from its fill -> NO boosts; the
                # sibling just runs as a (rescue-role) leg. event_type stays None.

            # --- record every leg ---
            def _push(pos, role):
                rec, usd = _trade_record(
                    pos, cfg, date=broker_date, anchor=label, label=label, role=role,
                    stop_level=(buy_stop if pos.side == 'BUY' else sell_stop),
                    is_monday_a1=is_monday_a1, event_type=event_type, branch=branch)
                trades.append(rec)
                return usd

            anchor_usd = 0.0
            anchor_usd += _push(first_pos, 'normal')
            for rp in rescue_legs:
                anchor_usd += _push(rp, rp.role if rp.role != 'normal' else 'normal')
            for bp in boost_legs:
                anchor_usd += _push(bp, 'rescue-boost')

            daily_pnl += anchor_usd
            a_net[label] += anchor_usd
            raw_net += anchor_usd

            ad = anchor_tot.setdefault(label, {'net': 0.0, 'wins': 0, 'legs': 0})
            ad['net'] += anchor_usd

            # KILL SWITCH
            if daily_pnl <= -cfg.daily_loss_pct * cfg.starting_balance:
                kill_triggered = True
                kill_days.append({'date': str(broker_date),
                                  'loss_at_trip': round(daily_pnl, 2)})
                break

        # --- end anchors for the day ---
        equity += daily_pnl
        peak_equity = max(peak_equity, equity)
        dd_usd = equity - peak_equity
        if dd_usd < max_dd_usd:
            max_dd_usd = dd_usd
            max_dd_pct = 100.0 * dd_usd / cfg.starting_balance

        if worst_day['net'] is None or daily_pnl < worst_day['net']:
            worst_day = {'date': str(broker_date), 'net': round(daily_pnl, 2)}

        day_rows.append({
            'date': str(broker_date),
            'weekday': d.strftime('%a'),
            'is_monday': broker_date.weekday() == 0,
            'mon_a1_time': mon_tag,
            'A1': round(a_net.get('A1_02h_Asia', 0.0), 2),
            'A2': round(a_net.get('A2_10h_London', 0.0), 2),
            'A3': round(a_net.get('A3_1340_Overlap', 0.0), 2),
            'A4': round(a_net.get('A4_1640_NYopen', 0.0), 2),
            'day_net': round(daily_pnl, 2),
            'cumulative': round(equity - cfg.starting_balance, 2),
            'running_max_dd': round(max_dd_usd, 2),
            'kill': kill_triggered,
        })

    # --- per-anchor win rate: a "win" = anchor's net for an anchor-day > 0 ---
    # recompute wins/legs from trades grouped by (date,label)
    if trades:
        tdf = pd.DataFrame(trades)
        for label in [lbl for (lbl, _, _) in cfg.anchors]:
            sub = tdf[tdf['label'] == label]
            if len(sub) == 0:
                anchor_tot.setdefault(label, {'net': 0.0})
                anchor_tot[label].update({'win_rate': 0.0, 'legs': 0, 'anchor_days': 0})
                continue
            grp = sub.groupby('date')['pnl_usd'].sum()
            wins = int((grp > 0).sum())
            anchor_tot.setdefault(label, {'net': float(sub['pnl_usd'].sum())})
            anchor_tot[label]['net'] = round(float(sub['pnl_usd'].sum()), 2)
            anchor_tot[label]['legs'] = int(len(sub))
            anchor_tot[label]['anchor_days'] = int(len(grp))
            anchor_tot[label]['win_rate'] = round(100.0 * wins / len(grp), 1) if len(grp) else 0.0

    # worst anchor by net
    worst_anchor = None
    if anchor_tot:
        wa = min(anchor_tot.items(), key=lambda kv: kv[1].get('net', 0.0))
        worst_anchor = {'label': wa[0], 'net': round(wa[1].get('net', 0.0), 2)}

    summary = {
        'raw_net': round(raw_net, 2),
        'realism_haircut': float(cfg.realism_haircut_dollars),
        'realism_adjusted_net': round(raw_net - cfg.realism_haircut_dollars, 2),
        'max_dd_usd': round(max_dd_usd, 2),
        'max_dd_pct': round(max_dd_pct, 2),
        'worst_day': worst_day,
        'worst_anchor': worst_anchor,
        'kill_days': kill_days,
        'monday_a1_fired': monday_a1_fired,
        'anchor_totals': anchor_tot,
        'branch_counts': branch_counts,
        'event_counts': event_counts,
        'no_boost_counterfactual': round(no_boost_counterfactual, 2),
        'final_equity': round(equity, 2),
        'rule_sources': rule_sources(),
    }

    return {'trades': trades, 'day_rows': day_rows, 'summary': summary}
