"""AUREON FETCHER — the chop-harvesting scalper (SEPARATE engine, magic 20260707).

A fixed-grid scalper that harvests chop. Unlike ROGUE (rides monsters on an adaptive
trail) FETCHER never trails: it seeds a morning anchor (the SAME resolver ROGUE uses --
real A1 when the anchor engine placed, a1_time_snapshot fallback otherwise), then on any
tick where price is >= fetcher_trigger_dollars ($5) off the ACTIVE anchor in EITHER
direction, market-enters in the move direction with a broker-side TP at entry +/- $5 and
SL at entry -/+ $5. NO software trail, NO breakeven, NO ladder. After ANY close it
re-anchors at the CLOSE PRICE and hunts the next $5 move both ways -- high re-entry count.

This module mirrors ROGUE's integration seams EXACTLY (PR-90): a demo-default-ON /
funded-forced-OFF run gate, a per-day governor (cap + loss-stop + fail-stop), the shared
seed resolver (rogue.resolve_seed with fallback_key='fetcher_seed_fallback'), and a live
driver gated by should_run + the runtime /fetcher switch. It NEVER reads or closes an
anchor (20260522) or Rogue (20260626) ticket; it rides its OWN magic (20260707) and lot
READS cfg.lot_size, never mutating it. The PURE cores (governor, entry rule, TP/SL) carry
no IO; the driver does placement + telemetry + close-booking, fully guarded.
"""
from __future__ import annotations

import csv
import logging
import os

log = logging.getLogger("AUREON")

# ROGUE is imported LAZILY (inside the functions that need it) rather than at module top,
# so fetcher.py loads atomically with NO import-time dependency -- it can never be left
# half-initialized by another module's import order. We REUSE (never fork) rogue's
# account_is_demo, _mid, resolve_seed, and _capture_seed_snapshots via _rg().


def _rg():
    """Lazy handle to the ROGUE module (shared helpers we reuse, never fork)."""
    import rogue as _rogue
    return _rogue

# --- tagging (distinct from anchors 20260522 and Rogue 20260626) ------------------
FETCHER_MAGIC = 20260707
FETCHER_LABEL = "FETCH"
FETCHER_LEG_TYPE = "fetcher"
FETCHER_ALERT_PREFIX = "[FETCHER]"
FETCHER_GLYPH = "🪣"                 # a bucket: scoop the chop
TRADES_CSV = "fetcher_trades.csv"
# seed_source is the LAST column (D-8 provenance; appending keeps old files positional-safe)
TRADE_COLUMNS = ['ts', 'event', 'direction', 'anchor', 'entry', 'exit', 'tp', 'sl',
                 'outcome_dollars', 'ticket', 'magic', 'seed_source']


def _persist(trader):
    """After any Fetcher state change, persist the P1 snapshot (guarded; a persistence
    error never reaches the trading path). Mirrors rogue._persist_state."""
    try:
        import p1_state as _p1
        _p1.save(trader)
    except Exception:
        pass


# --- the demo-default-ON / funded-OFF run gate (mirror rogue.py:42/:52) ------------
def funded_default(is_demo, is_funded):
    """The per-account promotion stamp: ON for a demo (non-funded) account, OFF for
    funded. The config boot default is True, but this per-account stamp stays
    authoritative on every boot -- a funded account is always forced OFF. PURE."""
    if is_funded:
        return False
    return bool(is_demo)


def should_run(cfg, is_funded=False):
    """The single effective on/off for the ENTIRE Fetcher mechanism. fetcher_enabled is
    the master switch; a FUNDED account force-disables it (mandatory gate) regardless of
    the flag -- un-proven Fetcher never boots ON on real capital. The runtime /fetcher
    engine switch ANDs on top of this at the drive() call site, it never replaces this
    gate. PURE."""
    if is_funded:
        return False
    return bool(getattr(cfg, 'fetcher_enabled', False))


def promote_on_boot(trader):
    """DEMO default-ON / FUNDED forced-OFF: on every boot the account type sets
    fetcher_enabled -- ON for a demo (non-funded) account, forced OFF for funded.
    Returns the effective fetcher_enabled. Guarded; never raises. Mirrors
    rogue.promote_on_boot (:212)."""
    try:
        _rogue = _rg()
        is_demo = _rogue.account_is_demo(trader)
        is_funded = not is_demo
        if funded_default(is_demo, is_funded):
            trader.cfg.fetcher_enabled = True
            log.info(f"{FETCHER_ALERT_PREFIX} demo account -> fetcher PROMOTED ON (trial).")
        elif is_funded:
            trader.cfg.fetcher_enabled = False
            log.info(f"{FETCHER_ALERT_PREFIX} funded account -> fetcher FORCED OFF (gate).")
        return bool(getattr(trader.cfg, 'fetcher_enabled', False))
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} promote_on_boot non-fatal: {e!r}")
        return bool(getattr(trader.cfg, 'fetcher_enabled', False))


# --- the day governor: cap + loss-stop + consecutive-fail-stop (mirror rogue) ------
def new_day_state():
    """Fresh per-day Fetcher counters. entries = NEW entries today (the cap counter);
    day_pnl = cumulative Fetcher P&L; consec_fails = consecutive SL strikes.
    profit_locked/override/alerted drive the SOFT daily-profit lock (2026-07-08):
    day_pnl >= fetcher_daily_profit_stop -> manage-only, overridable ONCE/day by /fetchseed
    (profit_override), one-time alert (profit_alerted). Mirrors Rogue."""
    return {'entries': 0, 'day_pnl': 0.0, 'consec_fails': 0,
            'loss_stopped': False, 'fail_paused': False,
            'profit_locked': False, 'profit_override': False, 'profit_alerted': False}


def can_enter(state, cfg):
    """PURE: may Fetcher take a NEW entry now? Returns (ok, reason). Blocks when ANY brake
    is tripped -- the daily loss stop (fetcher_daily_loss_stop, -$700), the consecutive-
    fail pause (fetcher_consecutive_fail_stop, 3), or the cap
    (fetcher_max_entries_per_day, 20). The loss stop (4 strikes) is deeper than the pause
    (3 strikes), so the pause is ALWAYS reachable first. Gates only NEW entries."""
    cap = int(getattr(cfg, 'fetcher_max_entries_per_day', 20))
    loss_stop = float(getattr(cfg, 'fetcher_daily_loss_stop', -700.0))
    profit_stop = float(getattr(cfg, 'fetcher_daily_profit_stop', 0.0))
    fail_stop = int(getattr(cfg, 'fetcher_consecutive_fail_stop', 3))
    if loss_stop < 0.0 and (state.get('loss_stopped')
                            or float(state.get('day_pnl', 0.0)) <= loss_stop):
        return False, 'daily_loss_stop'          # loss_stop == 0 disables the gate
    if (profit_stop > 0.0 and not state.get('profit_override')
            and (state.get('profit_locked')
                 or float(state.get('day_pnl', 0.0)) >= profit_stop)):
        return False, 'daily_profit_stop'         # profit_stop == 0 disables the gate
    if state.get('fail_paused') or int(state.get('consec_fails', 0)) >= fail_stop:
        return False, 'consecutive_fail_pause'
    if int(state.get('entries', 0)) >= cap:
        return False, 'daily_cap'
    return True, 'ok'


def record_entry(state):
    """A NEW Fetcher entry was taken (passed can_enter). Consumes one slot. PURE."""
    state['entries'] = int(state.get('entries', 0)) + 1
    return state


def record_close(state, pnl_dollars, was_fail, cfg):
    """A Fetcher position closed: book its P&L, advance/reset the consecutive-fail streak,
    and latch the loss-stop / fail-pause brakes if tripped. was_fail = the SL was hit; a
    winning TP resets the streak. was_fail=None (P&L unresolved after retries) books the
    P&L but leaves the streak UNCHANGED (an unbooked close can't trip the pause). PURE."""
    fail_stop = int(getattr(cfg, 'fetcher_consecutive_fail_stop', 3))
    loss_stop = float(getattr(cfg, 'fetcher_daily_loss_stop', -700.0))
    profit_stop = float(getattr(cfg, 'fetcher_daily_profit_stop', 0.0))
    state['day_pnl'] = float(state.get('day_pnl', 0.0)) + float(pnl_dollars)
    if was_fail is None:
        pass
    elif was_fail:
        state['consec_fails'] = int(state.get('consec_fails', 0)) + 1
    else:
        state['consec_fails'] = 0
    if loss_stop < 0.0 and state['day_pnl'] <= loss_stop:
        state['loss_stopped'] = True             # loss_stop == 0 disables the gate
    if int(state['consec_fails']) >= fail_stop:
        state['fail_paused'] = True
    # SOFT profit lock: latch once realized day P&L reaches the target (unless already
    # overridden for the day). The one-time alert fires in detect_close.
    if (profit_stop > 0.0 and not state.get('profit_override')
            and state['day_pnl'] >= profit_stop):
        state['profit_locked'] = True
    return state


def maybe_profit_lock_alert(trader, st):
    """Fire the ONE-TIME loud PROFIT-LOCK alert (log + Discord) the first time the soft
    daily-profit lock engages -- 'DAY PROFIT STOP +$X >= $400 — entries locked (reseed to
    override)'. No-op once alerted, or when overridden. Guarded; never raises."""
    try:
        g = st.get('gov') or {}
        if (g.get('profit_locked') and not g.get('profit_override')
                and not g.get('profit_alerted')):
            g['profit_alerted'] = True
            ps = float(getattr(trader.cfg, 'fetcher_daily_profit_stop', 0.0))
            msg = (f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} DAY PROFIT STOP "
                   f"+${float(g.get('day_pnl', 0.0)):.0f} >= ${ps:.0f} — entries locked "
                   f"(reseed to override)")
            log.warning(msg)
            try:
                trader.tele.warn(msg)
            except Exception:
                pass
            _persist(trader)
    except Exception:
        pass


# --- PURE entry + TP/SL cores -----------------------------------------------------
def entry_decision(anchor, price, cfg):
    """PURE: a $5 move off the active anchor in EITHER direction fires a market entry in
    the move direction. Returns (enter, side, entry_px). At exactly the trigger it fires;
    below it holds. entry_px is the current (market) price."""
    trig = float(getattr(cfg, 'fetcher_trigger_dollars', 5.0))
    d = float(price) - float(anchor)
    if d >= trig:
        return True, 'BUY', round(float(price), 2)
    if d <= -trig:
        return True, 'SELL', round(float(price), 2)
    return False, None, None


def tp_sl_for(side, entry_px, cfg):
    """PURE: broker-side TP at entry +/- fetcher_tp_dollars and SL at entry -/+
    fetcher_sl_dollars (direction-signed). NO trail -- these are the ONLY exits."""
    tp_d = float(getattr(cfg, 'fetcher_tp_dollars', 5.0))
    sl_d = float(getattr(cfg, 'fetcher_sl_dollars', 5.0))
    if side == 'BUY':
        return round(entry_px + tp_d, 2), round(entry_px - sl_d, 2)
    return round(entry_px - tp_d, 2), round(entry_px + sl_d, 2)


# --- closure isolation: a fetcher close only ever closes fetcher legs --------------
def closes(position, scope):
    """PURE label-scoped closure check. A 'FETCHER'-scoped close closes a position ONLY if
    it belongs to Fetcher (magic 20260707 / leg_type). It never closes an anchor or Rogue
    leg and vice versa -- there is NO generic close-all."""
    pos_magic = position.get('magic') if hasattr(position, 'get') else getattr(position, 'magic', None)
    pos_type = position.get('leg_type') if hasattr(position, 'get') else getattr(position, 'leg_type', None)
    is_fetcher = (pos_magic == FETCHER_MAGIC) or (pos_type == FETCHER_LEG_TYPE)
    if str(scope).upper() == 'FETCHER':
        return bool(is_fetcher)
    return not bool(is_fetcher)


# --- CSV sink (header-on-create; seed_source LAST; never raises to the caller) ------
def _log_trade(trader, *, event, direction, anchor, entry, exit_px, tp, sl,
               outcome_dollars='', ticket='', seed_source=''):
    try:
        run_dir = getattr(trader, 'run_dir', '.') or '.'
        path = os.path.join(run_dir, TRADES_CSV)
        import pandas as _pd
        row = {'ts': _pd.Timestamp.now(tz='UTC').isoformat(), 'event': event,
               'direction': direction or '', 'anchor': anchor, 'entry': entry,
               'exit': exit_px, 'tp': tp, 'sl': sl, 'outcome_dollars': outcome_dollars,
               'ticket': ticket, 'magic': FETCHER_MAGIC, 'seed_source': seed_source or ''}
        new = not os.path.exists(path)
        with open(path, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, '') for k in TRADE_COLUMNS})
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} trade-log non-fatal: {e!r}")


# --- seed record (FETCHER-tagged; latch mirrors rogue._record_seed) ----------------
def _record_seed(trader, st, seed_px, seed_source):
    """Log 'FETCH SEED via <SOURCE> @ price' ONCE per (source, price) and stamp
    st['seed_source'] so every trade row carries it. A FALLBACK seed additionally LATCHES
    (st['seed_px']); the live A1_ANCHOR read is not latched. Guarded; never raises."""
    try:
        if seed_px is None:
            return
        _rogue = _rg()
        key = f"{seed_source}:{round(float(seed_px), 2)}"
        st['seed_source'] = seed_source
        st['seed_recorded_px'] = round(float(seed_px), 2)
        if seed_source in (_rogue.SEED_A1_TIME_SNAPSHOT, _rogue.SEED_MARKET_OPEN):
            st['seed_px'] = round(float(seed_px), 2)
        if st.get('_seed_log_key') == key:
            return
        st['_seed_log_key'] = key
        msg = (f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} FETCH SEED via {seed_source} @ "
               f"{round(float(seed_px), 2)}")
        log.info(msg)
        try:
            trader.tele.info(msg)
        except Exception:
            pass
        _persist(trader)
    except Exception:
        pass


# --- broker close-P&L resolution (mirror rogue; READ-ONLY) -------------------------
def _close_pnl(trader, ticket):
    """Realized $ of a CLOSED Fetcher position from its broker close deal (entry==1):
    profit + swap + commission. None if the close deal isn't in history yet. READ-ONLY."""
    try:
        deals = trader.adapter.mt5.history_deals_get(position=int(ticket)) or []
        cd = next((d for d in deals if getattr(d, 'entry', None) == 1), None)
        if cd is None:
            return None
        return float(cd.profit) + float(cd.swap) + float(cd.commission)
    except Exception:
        return None


def _close_price(trader, ticket):
    """Exit PRICE of a CLOSED Fetcher position (the re-anchor level). None if the close
    deal isn't in history yet. READ-ONLY."""
    try:
        deals = trader.adapter.mt5.history_deals_get(position=int(ticket)) or []
        cd = next((d for d in deals if getattr(d, 'entry', None) == 1), None)
        if cd is None:
            return None
        return float(cd.price)
    except Exception:
        return None


def _resolve_close_pnl(trader, ticket, tries=3, delay=1.0):
    """Bounded retry on the close-deal P&L (the deal often lands a beat after the position
    leaves the book). First non-None value, else None after every try. READ-ONLY."""
    import time as _time
    for i in range(max(1, int(tries))):
        pnl = _close_pnl(trader, ticket)
        if pnl is not None:
            return pnl
        if i < int(tries) - 1:
            try:
                _time.sleep(float(delay))
            except Exception:
                pass
    return None


# --- placement (mirror rogue's brick-safe path; own magic) -------------------------
def _mark_open(trader, st, entry_px, tp, sl, tk, rc):
    """Set st['open'] on a REAL fill + consume ONE governor slot + log the enter. Called
    ONLY once a placement is confirmed (rc==10009 with a ticket, or the paper path)."""
    side = st['leg_dir']
    st['open'] = {'ticket': tk, 'side': side, 'entry': entry_px, 'tp': tp, 'sl': sl,
                  'magic': FETCHER_MAGIC, 'leg_type': FETCHER_LEG_TYPE}
    record_entry(st['gov'])
    _log_trade(trader, event='enter', direction=side, anchor=st.get('anchor'),
               entry=entry_px, exit_px='', tp=tp, sl=sl, ticket=tk,
               seed_source=st.get('seed_source'))
    try:
        trader.tele.info(f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} ENTER {side} @ {entry_px} "
                         f"TP {tp} SL {sl} (slot {st['gov']['entries']}/"
                         f"{int(getattr(trader.cfg, 'fetcher_max_entries_per_day', 20))}) rc={rc}")
    except Exception:
        pass
    _persist(trader)


def _place_entry(trader, st, entry_px):
    """Place ONE FETCHER-tagged market entry with fixed broker TP/SL (own magic). LIVE
    placements go through the shared place_with_retry wrapper; lot is NEVER resized. BRICK
    FIX: st['open'] is set (and ONE slot consumed) ONLY on a real fill -- on final failure
    the state stays clean (no phantom open, no slot consumed, engine stays alive)."""
    side = st['leg_dir']
    tp, sl = tp_sl_for(side, entry_px, trader.cfg)
    if trader.paper:
        try:
            res = trader.adapter.place_market_order(
                trader.cfg.symbol, side, trader.cfg.lot_size, sl=sl, tp=tp,
                magic=FETCHER_MAGIC, comment=f"AUR_FETCH_{side[0]}", dry_run=True)
            rc = getattr(res, 'retcode', None) if res is not None else None
            tk = getattr(res, 'order', None) or getattr(res, 'deal', None)
            _mark_open(trader, st, entry_px, tp, sl, tk, rc)
        except Exception as e:
            log.warning(f"{FETCHER_ALERT_PREFIX} place entry (paper) non-fatal: {e!r}")
        return

    def _send(attempt, recompute_stops):
        return trader.adapter.place_market_order(
            trader.cfg.symbol, side, trader.cfg.lot_size, sl=sl, tp=tp,
            magic=FETCHER_MAGIC, comment=f"AUR_FETCH_{side[0]}", dry_run=False)
    describe = {'label': f'FETCH {side}', 'side': side, 'symbol': trader.cfg.symbol,
                'lot': trader.cfg.lot_size, 'price': 'mkt', 'sl': sl, 'tp': tp,
                'magic': FETCHER_MAGIC}
    try:
        res = trader.adapter.place_with_retry(
            _send, describe=describe, tele=getattr(trader, 'tele', None))
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} place entry non-fatal: {e!r}")
        res = None
    rc = getattr(res, 'retcode', None) if res is not None else None
    tk = (getattr(res, 'order', None) or getattr(res, 'deal', None)) if res is not None else None
    if rc == 10009 and tk:
        _mark_open(trader, st, entry_px, tp, sl, tk, rc)
    else:
        st['open'] = None
        try:
            trader.tele.warn(
                f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} ENTER {side} @ {entry_px} FAILED "
                f"rc={rc} — no position, slot preserved; engine live for next signal")
        except Exception:
            pass


# --- broker-side close detection + re-anchor at close price ------------------------
def detect_close(trader, st):
    """Detect a BROKER-side close (TP or SL fired) of the open Fetcher position and book it
    ONCE -- record_close the governor + clear st['open'] -- then RE-ANCHOR at the CLOSE
    PRICE so the next $5 move is measured from where Fetcher actually got out. ISOLATION:
    only ever inspects st['open']'s OWN ticket and issues NO close (the broker already
    did); it can NEVER touch an anchor (20260522) or Rogue (20260626) ticket. Returns True
    if a close was booked. Guarded; never raises onto the tick."""
    o = st.get('open')
    if not o or o.get('ticket') is None:
        return False
    tk = int(o['ticket'])
    try:
        still = trader.adapter.mt5.positions_get(ticket=tk)
    except Exception:
        return False
    if still:
        return False
    pnl = _resolve_close_pnl(trader, tk)
    unresolved = (pnl is None)
    if unresolved:
        pnl = 0.0
        record_close(st['gov'], 0.0, None, trader.cfg)
    else:
        was_fail = float(pnl) <= 0.0        # a non-winning close = SL strike (a TP win resets)
        record_close(st['gov'], pnl, was_fail, trader.cfg)
    exit_px = _close_price(trader, tk)
    if exit_px is None:
        exit_px = o.get('sl')               # the stop that fired ~= the exit level
    _log_trade(trader, event='exit', direction=o.get('side'), anchor=st.get('anchor'),
               entry=o.get('entry'), exit_px=exit_px, tp=o.get('tp'), sl=o.get('sl'),
               outcome_dollars=round(float(pnl), 2), ticket=tk,
               seed_source=st.get('seed_source'))
    st['open'] = None
    # RE-ANCHOR at the close price: the next entry needs a fresh $5 move off HERE.
    if exit_px is not None:
        st['anchor'] = round(float(exit_px), 2)
    _persist(trader)
    maybe_profit_lock_alert(trader, st)  # one-time PROFIT-LOCK alert if this close engaged it
    if unresolved:
        try:
            log.warning(f"{FETCHER_ALERT_PREFIX} WARN pnl-unresolved ticket #{tk}")
            trader.tele.warn(f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} WARN pnl-unresolved "
                             f"ticket #{tk} — booked $0, fail-streak untouched")
        except Exception:
            pass
    try:
        g = st['gov']
        brake = ('LOSS-STOP' if g.get('loss_stopped')
                 else ('FAIL-PAUSE' if g.get('fail_paused') else 'live'))
        msg = (f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} CLOSE {o.get('side')} #{tk} "
               f"P&L ${float(pnl):+.2f} | day ${float(g.get('day_pnl', 0.0)):+.2f} | "
               f"fails {int(g.get('consec_fails', 0))} | {brake} | "
               f"re-anchor @ {st.get('anchor')}")
        log.info(msg)
        trader.tele.info(msg)
    except Exception:
        pass
    return True


# --- live driver (impure; SEPARATE call-site, fully gated by should_run) -----------
def drive(trader, allow_new_entries=True):
    """The per-tick Fetcher driver. Runs ONLY when should_run (fetcher_enabled AND not
    funded). Pipeline: book any broker close (+ re-anchor at the close price) -> if flat
    and entries allowed, seed the morning anchor (shared resolver) -> governor gate -> a
    $5 move off the anchor either direction fires a fixed-TP/SL market entry. All decisions
    come from the PURE cores; this only does IO + telemetry + placement (FETCHER-tagged).

    allow_new_entries=False (the manage-only call: /fetcher off, post-EOD, or kill-locked)
    still books an existing position's broker close but takes NO new entry -- the switch
    never orphans a leg. Fully guarded -- never raises onto _tick."""
    try:
        _rogue = _rg()
        is_funded = not _rogue.account_is_demo(trader)
        if not should_run(trader.cfg, is_funded=is_funded):
            return
        try:
            today = str(trader.state.get('last_broker_date', ''))
        except Exception:
            today = ''
        st = getattr(trader, '_fetcher', None)
        if st is None or st.get('day') != today:
            st = {'day': today, 'gov': new_day_state(),
                  'anchor': None, 'leg_dir': None, 'open': None}
            trader._fetcher = st
        price = _rogue._mid(trader)
        if price is None:
            return
        # passively capture the fallback seed candidates every tick (shared with Rogue;
        # capturing a price is free -- resolve_seed picks AT SEED TIME which one is used).
        _rogue._capture_seed_snapshots(trader, st, price)
        # 1. book a broker-side close FIRST (frees the slot + re-anchors at the exit).
        if st.get('open') is not None:
            detect_close(trader, st)
        # 2. if still open -> manage-only (broker TP/SL do the work; nothing to trail).
        if st.get('open') is not None:
            return
        if not allow_new_entries:
            return                          # no NEW entries when manage-only / post-EOD
        # 3. seed the morning anchor ONCE (shared resolver; fetcher's own fallback knob).
        if st.get('anchor') is None:
            seed_px, seed_source = _rogue.resolve_seed(
                trader, st, fallback_key='fetcher_seed_fallback')
            if seed_px is None:
                return                      # WAIT for the seed source (A1 not placed yet)
            st['anchor'] = round(float(seed_px), 2)
            _record_seed(trader, st, seed_px, seed_source)
        # 4. governor gate (loss stop / fail pause / cap).
        ok, _why = can_enter(st['gov'], trader.cfg)
        if not ok:
            return
        # 5. a $5 move off the anchor either direction -> enter in the move direction.
        enter, side, epx = entry_decision(st['anchor'], price, trader.cfg)
        if not enter:
            return
        st['leg_dir'] = side
        _place_entry(trader, st, epx)
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} drive non-fatal: {e!r}")


# --- EOD / kill-switch / manual flatten (mirror rogue; FETCHER-ONLY) ---------------
def eod_flatten(trader):
    """E-4 analogue (flag fetcher_flatten_at_eod, DEFAULT ON): at EOD close an OPEN Fetcher
    position so it does not ride overnight on its own TP/SL. OFF -> no-op (rides).
    FETCHER-ONLY: closes ONLY st['open']'s own ticket (never an anchor/Rogue ticket), books
    it via the governor + clears st['open']. Guarded; never raises."""
    try:
        if not bool(getattr(trader.cfg, 'fetcher_flatten_at_eod', True)):
            return False
        st = getattr(trader, '_fetcher', None)
        if not st or not st.get('open') or st['open'].get('ticket') is None:
            return False
        tk = int(st['open']['ticket'])
        o = st['open']
        trader.adapter.close_position(tk, dry_run=trader.paper)   # FETCHER ticket ONLY
        pnl = _close_pnl(trader, tk)
        if pnl is None:
            pnl = 0.0
        record_close(st['gov'], pnl, float(pnl) <= 0.0, trader.cfg)
        _log_trade(trader, event='eod_flatten', direction=o.get('side'),
                   anchor=st.get('anchor'), entry=o.get('entry'), exit_px='',
                   tp=o.get('tp'), sl=o.get('sl'), outcome_dollars=round(float(pnl), 2),
                   ticket=tk, seed_source=st.get('seed_source'))
        st['open'] = None
        _persist(trader)
        try:
            trader.tele.info(f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} EOD flatten -> closed "
                             f"#{tk} P&L ${float(pnl):+.2f}")
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} eod_flatten non-fatal: {e!r}")
        return False


def force_close_open(trader, reason="flatten"):
    """Close an OPEN Fetcher ticket (magic 20260707) UNCONDITIONALLY -- the kill-switch /
    manual-flatten path. Fetcher rides its OWN magic, so the anchor flatten loop never
    touches it; this closes st['open']'s own ticket, books it, and clears st['open'].
    IGNORES fetcher_flatten_at_eod (that governs the EOD ride, not a kill). FETCHER-ONLY.
    Returns True if it closed one. Guarded; never raises."""
    try:
        st = getattr(trader, '_fetcher', None)
        if not st or not st.get('open') or st['open'].get('ticket') is None:
            return False
        tk = int(st['open']['ticket'])
        o = st['open']
        trader.adapter.close_position(tk, dry_run=trader.paper)   # FETCHER ticket ONLY
        pnl = _close_pnl(trader, tk)
        if pnl is None:
            pnl = 0.0
        record_close(st['gov'], pnl, float(pnl) <= 0.0, trader.cfg)
        _log_trade(trader, event='force_flatten', direction=o.get('side'),
                   anchor=st.get('anchor'), entry=o.get('entry'), exit_px='',
                   tp=o.get('tp'), sl=o.get('sl'), outcome_dollars=round(float(pnl), 2),
                   ticket=tk, seed_source=st.get('seed_source'))
        st['open'] = None
        _persist(trader)
        try:
            trader.tele.info(f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} {reason} flatten -> "
                             f"closed #{tk} P&L ${float(pnl):+.2f}")
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} force_close_open non-fatal: {e!r}")
        return False


def cancel_pendings(trader, reason="flatten"):
    """Cancel any resting FETCHER pending orders (magic 20260707). Fetcher enters at
    MARKET so there are normally none, but the /fetcher flatten path mirrors Rogue's for
    symmetry + safety. FETCHER-ONLY (never an anchor/Rogue order). Guarded; never raises."""
    n = 0
    try:
        orders = trader.adapter.mt5.orders_get() or []
        for o in orders:
            try:
                if int(getattr(o, 'magic', 0)) != FETCHER_MAGIC:
                    continue
                trader.adapter.cancel_order(int(o.ticket), dry_run=trader.paper)
                n += 1
            except Exception:
                continue
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} cancel_pendings non-fatal: {e!r}")
    return n


# --- manual current-tick re-seed (/fetchseed): deliberate live testing -------------
def manual_seed_ok(cfg, is_demo):
    """PURE gate for /fetchseed. Funded refuses (fail-closed, same mandatory gate as
    fetcher promotion). Fetcher has no a1-mode flag (unlike Rogue), so DEMO is the only
    config-level gate; the runtime engine switch + open-ticket + market/kill rails are the
    shared manual_seed_rails_blocked. Returns (ok, reason). No side effects."""
    if not bool(is_demo):
        return False, 'funded'
    return True, 'ok'


def manual_seed(trader, price):
    """Plant the Fetcher anchor at `price` (the current live tick) ON DEMAND so trigger ->
    entry -> close -> re-anchor can be observed from a known point instead of a stale
    anchor. Sets st['anchor'] = seed; the EXISTING drive() then enters on a
    fetcher_trigger_dollars move off it, exactly as the morning seed. seed_source=MANUAL
    (propagates to fetcher_trades.csv + the close re-anchor). Mirrors rogue.manual_seed:
    DEMO-only + the shared rails (open ticket / engine off / market closed / kill switch);
    FETCHER-only (never an anchor 20260522 / Rogue 20260626 ticket). Does NOT reset the day
    governors -- a manual seed is a new ANCHOR, not a new day. Returns (ok, reason, price).
    Guarded; never raises."""
    try:
        _rogue = _rg()
        is_demo = _rogue.account_is_demo(trader)
        ok, reason = manual_seed_ok(trader.cfg, is_demo)
        if not ok:
            log.warning(f"{FETCHER_ALERT_PREFIX} MANUAL SEED refused ({reason}): "
                        f"DEMO-only (funded refused)")
            try:
                trader.tele.warn(f"{FETCHER_ALERT_PREFIX} 🌱 manual seed refused — "
                                 f"DEMO-only (funded refused)")
            except Exception:
                pass
            return False, reason, None
        # RAILS (shared with Rogue): never re-anchor under a live position / with the
        # engine switched off / market closed / kill-switch active. Guarded for old stubs.
        st_now = getattr(trader, '_fetcher', None) or {}
        blocked, rreason = _rogue.manual_seed_rails_blocked(
            trader, 'fetcher', bool(st_now.get('open')))
        if blocked:
            log.warning(f"{FETCHER_ALERT_PREFIX} MANUAL SEED refused (rail): {rreason}")
            try:
                trader.tele.warn(f"{FETCHER_ALERT_PREFIX} 🌱 manual seed refused — {rreason}")
            except Exception:
                pass
            return False, 'rail', None
        if price is None:
            log.warning(f"{FETCHER_ALERT_PREFIX} MANUAL SEED refused (no_tick): no sane tick")
            try:
                trader.tele.warn(f"{FETCHER_ALERT_PREFIX} 🌱 manual seed refused — "
                                 f"no sane settled tick (stale/garbage feed)")
            except Exception:
                pass
            return False, 'no_tick', None
        # DAILY-STOP interaction (2026-07-08): a manual reseed is the SOFT override for the
        # PROFIT lock, but the HARD loss stop is NEVER overridable -> refuse while it is
        # active. Read the LIVE gov (before any re-init); a same-day reseed reuses this gov.
        gov = (st_now.get('gov') or {}) if isinstance(st_now, dict) else {}
        loss_stop = float(getattr(trader.cfg, 'fetcher_daily_loss_stop', 0.0))
        if gov.get('loss_stopped') or (loss_stop < 0.0
                                       and float(gov.get('day_pnl', 0.0)) <= loss_stop):
            log.warning(f"{FETCHER_ALERT_PREFIX} MANUAL SEED refused (loss_stop): "
                        f"daily loss stop active (not overridable)")
            try:
                trader.tele.warn(f"{FETCHER_ALERT_PREFIX} 🌱 manual seed refused — daily loss "
                                 f"stop active (not overridable)")
            except Exception:
                pass
            return False, 'loss_stop', None
        overrode = False
        profit_stop = float(getattr(trader.cfg, 'fetcher_daily_profit_stop', 0.0))
        if (profit_stop > 0.0 and not gov.get('profit_override')
                and (gov.get('profit_locked')
                     or float(gov.get('day_pnl', 0.0)) >= profit_stop)):
            gov['profit_override'] = True   # clears the lock for the REST of the broker day
            overrode = True
        price = round(float(price), 2)
        # ensure per-day state (mirrors drive()). A SAME-day re-seed reuses it, so the day
        # governors (entries / day_pnl / fail streak) keep counting -- new anchor, not new day.
        today = ''
        try:
            today = str(trader.state.get('last_broker_date', ''))
        except Exception:
            today = ''
        st = getattr(trader, '_fetcher', None)
        if st is None or st.get('day') != today:
            st = {'day': today, 'gov': new_day_state(),
                  'anchor': None, 'leg_dir': None, 'open': None}
            trader._fetcher = st
        st['anchor'] = price                       # the level the next $5 move is measured off
        st['seed_source'] = _rogue.SEED_MANUAL     # provenance -> every fetcher_trades row
        _persist(trader)
        trig = float(getattr(trader.cfg, 'fetcher_trigger_dollars', 5.0))
        if overrode:
            omsg = (f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} PROFIT STOP OVERRIDDEN BY MANUAL "
                    f"RESEED @ {price} — entries re-enabled for the day (no re-lock)")
            log.warning(omsg)
            try:
                trader.tele.warn(omsg)
            except Exception:
                pass
        log.info(f"{FETCHER_ALERT_PREFIX} FETCH SEED via MANUAL @ {price} (current tick) -> "
                 f"hunting ${trig:.0f} move both directions")
        try:
            trader.tele.info(f"{FETCHER_ALERT_PREFIX} {FETCHER_GLYPH} FETCH SEED via MANUAL @ "
                             f"{price} (current tick) — hunting ${trig:.0f} move both "
                             f"directions")
        except Exception:
            pass
        return True, 'ok', price
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} manual_seed non-fatal: {e!r}")
        return False, 'error', None


def enqueue_seed_command(cfg):
    """CLI `python bot.py fetchseed`: enqueue a 'fetchseed' command onto the RUNNING bot's
    command channel (AUREON_RUN_DIR/commands.json) so the live loop plants the Fetcher
    anchor at ITS current tick. Returns 0 on enqueue, 2 on error. The DEMO gate + rails are
    enforced by manual_seed when the bot handles it. Mirrors rogue.enqueue_seed_command."""
    import json as _json
    import os as _os
    try:
        run_dir = _os.environ.get("AUREON_RUN_DIR", "./run")
        _os.makedirs(run_dir, exist_ok=True)
        path = _os.path.join(run_dir, "commands.json")
        cmds = []
        if _os.path.exists(path):
            try:
                with open(path) as f:
                    cmds = _json.load(f) or []
            except Exception:
                cmds = []
        cmds.append({"cmd": "fetchseed"})
        with open(path, "w") as f:
            _json.dump(cmds, f)
        abspath = _os.path.abspath(path)
        log.info(f"{FETCHER_ALERT_PREFIX} fetchseed queued -> {abspath} "
                 f"(AUREON_RUN_DIR={_os.environ.get('AUREON_RUN_DIR', '<unset:./run>')}). "
                 f"The running bot consumes this each tick and plants the Fetcher anchor at "
                 f"its current tick (DEMO-only; funded refuses). If nothing happens, confirm "
                 f"this path matches the bot's run dir.")
        return 0
    except Exception as e:
        log.error(f"{FETCHER_ALERT_PREFIX} fetchseed enqueue failed: {e!r}")
        return 2


# --- E-20: restart-recovery gov rebuild from BROKER deal history -------------------
def rebuild_gov_from_history(trader, dt_from=None, dt_to=None):
    """E-20 LESSON: on a SAME-DAY restart the governor counters (day_pnl / entries /
    consec_fails) must NOT reset to zero -- a restart mid-day would otherwise re-arm the
    full 20-entry cap and forget a tripped brake. REBUILD them from BROKER truth: every
    magic-20260707 deal in the current broker day. entries = count of entry-IN deals;
    day_pnl = sum(profit+swap+commission) over entry-OUT deals; consec_fails = the trailing
    run of losing closes (time-ordered). Latches loss_stopped / fail_paused per the cfg
    thresholds. Returns a rebuilt gov dict, or None if history is unavailable (the caller
    then keeps the persisted snapshot). READ-ONLY; guarded."""
    try:
        if dt_from is None or dt_to is None:
            dt_from, dt_to = _broker_day_range(trader)
        deals = trader.adapter.mt5.history_deals_get(dt_from, dt_to) or []
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} rebuild history query failed: {e!r}")
        return None
    try:
        ours = [d for d in deals if int(getattr(d, 'magic', 0) or 0) == FETCHER_MAGIC]
        ins = [d for d in ours if getattr(d, 'entry', None) == 0]
        outs = [d for d in ours if getattr(d, 'entry', None) == 1]
        outs.sort(key=lambda d: getattr(d, 'time', 0) or 0)
        gov = new_day_state()
        gov['entries'] = len(ins)
        gov['day_pnl'] = round(sum(
            float(getattr(d, 'profit', 0.0) or 0.0)
            + float(getattr(d, 'swap', 0.0) or 0.0)
            + float(getattr(d, 'commission', 0.0) or 0.0) for d in outs), 2)
        # trailing consecutive losing closes (a win breaks the streak)
        fails = 0
        for d in reversed(outs):
            pnl = (float(getattr(d, 'profit', 0.0) or 0.0)
                   + float(getattr(d, 'swap', 0.0) or 0.0)
                   + float(getattr(d, 'commission', 0.0) or 0.0))
            if pnl <= 0.0:
                fails += 1
            else:
                break
        gov['consec_fails'] = fails
        fail_stop = int(getattr(trader.cfg, 'fetcher_consecutive_fail_stop', 3))
        loss_stop = float(getattr(trader.cfg, 'fetcher_daily_loss_stop', -700.0))
        profit_stop = float(getattr(trader.cfg, 'fetcher_daily_profit_stop', 0.0))
        gov['loss_stopped'] = bool(loss_stop < 0.0 and gov['day_pnl'] <= loss_stop)
        gov['fail_paused'] = bool(gov['consec_fails'] >= fail_stop)
        gov['profit_locked'] = bool(profit_stop > 0.0 and gov['day_pnl'] >= profit_stop)
        log.info(f"{FETCHER_ALERT_PREFIX} gov rebuilt from history: entries={gov['entries']} "
                 f"day_pnl=${gov['day_pnl']:+.2f} consec_fails={gov['consec_fails']} "
                 f"loss_stopped={gov['loss_stopped']} fail_paused={gov['fail_paused']} "
                 f"profit_locked={gov['profit_locked']}")
        return gov
    except Exception as e:
        log.warning(f"{FETCHER_ALERT_PREFIX} rebuild parse failed: {e!r}")
        return None


def _broker_day_range(trader):
    """(dt_from, dt_to) UTC datetimes bounding the CURRENT broker day, for the history
    rebuild. Mirrors pnl_report's IST-day window. Guarded; (None, None) on any error."""
    try:
        import pandas as _pd
        off = float(getattr(trader.cfg, 'broker_tz_offset_hours', 0.0) or 0.0)
        now_utc = _pd.Timestamp.now(tz='UTC')
        bdate = (now_utc + _pd.Timedelta(hours=off)).normalize()
        dt_from = (bdate - _pd.Timedelta(hours=off)).to_pydatetime()
        dt_to = (bdate + _pd.Timedelta(days=1) - _pd.Timedelta(hours=off)).to_pydatetime()
        return dt_from, dt_to
    except Exception:
        return None, None
