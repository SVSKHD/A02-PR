"""AUREON ROGUE — the self-anchoring monster-rider (SEPARATE from the clock anchors).

"We are here to ride monsters." Unlike the A1-A5 clock anchors (fixed times), ROGUE
plants its OWN price-anchor where a strong move completes, then hunts the next leg --
reusing the RALLY / RESCUE state machines and the shared trail helper FROM that anchor,
but always tagged [ROGUE] and closed ONLY against its own magic/label (standing rule:
shared HELPERS only, NO merged state, NO generic close-all). A rogue leg never touches
an anchor (Non-OCO) position and vice versa.

This module is PURE (no IO/clock/orders): a detector, an entry rule, an adaptive trail,
a day-governor (cap + loss-stop + fail-stop), and the demo/funded run gate. The live
driver (live_trader._rogue_tick) calls these and is fully gated by rogue_enabled, so with
the flag OFF there is no watching, anchoring, or entering -- byte-identical to master.
"""
from __future__ import annotations

import logging

log = logging.getLogger("AUREON")

# --- tagging (distinct from the anchors) -----------------------------------------
ROGUE_MAGIC = 20260626          # distinct from the anchor magic (20260522) + warmup (9999998)
ROGUE_LABEL = "ROGUE"
ROGUE_LEG_TYPE = "rogue"
ROGUE_ALERT_PREFIX = "[ROGUE]"
ROGUE_GLYPH = "🦏"               # chart glyph distinct from the anchor glyphs


# --- the demo-default-ON / funded-OFF run gate (freeze-safe) ----------------------
def funded_default(is_demo, is_funded):
    """The value the boot promotes rogue_enabled to per account type: ON for a demo
    (non-funded) account, OFF for funded. This is how 'demo default ON' is achieved
    WITHOUT a True raw config default (which would break the all-flags-off==master
    freeze). PURE."""
    if is_funded:
        return False
    return bool(is_demo)


def should_run(cfg, is_funded=False):
    """The single effective on/off for the ENTIRE Rogue mechanism. rogue_enabled is the
    master switch; a FUNDED account force-disables it (mandatory gate) regardless of the
    flag -- un-proven Rogue never boots ON on real capital. With rogue_enabled False (the
    raw config default) this is False -> no watch, no anchor, no entry -> master
    byte-identical. PURE."""
    if is_funded:
        return False
    return bool(getattr(cfg, 'rogue_enabled', False))


# --- the strong-move ("monster") detector ----------------------------------------
def detect_monster(candles, cfg):
    """PURE strong-move detector on recent M5 bars (list of {'open','high','low','close'},
    oldest->newest). A MONSTER = rogue_min_candles consecutive same-direction closes AND
    total range >= rogue_min_range AND a directional thrust (combined body >=
    rogue_body_mult x the average single-bar range -- a real move, not chop). Returns
    (is_monster, move_direction, completion_price): move_direction is the move's own
    direction ('BUY' up / 'SELL' down); completion_price is the far extreme where the
    move completes (the BUY-move high / SELL-move low) -- where Rogue drops its anchor."""
    n_req = int(getattr(cfg, 'rogue_min_candles', 4))
    rng_req = float(getattr(cfg, 'rogue_min_range', 15.0))
    body_mult = float(getattr(cfg, 'rogue_body_mult', 1.5))
    cs = [c for c in (candles or []) if c is not None]
    if len(cs) < n_req:
        return False, None, None
    w = cs[-n_req:]
    ups = sum(1 for c in w if float(c['close']) > float(c['open']))
    downs = sum(1 for c in w if float(c['close']) < float(c['open']))
    if ups == n_req:
        direction = 'BUY'
    elif downs == n_req:
        direction = 'SELL'
    else:
        return False, None, None
    hi = max(float(c['high']) for c in w)
    lo = min(float(c['low']) for c in w)
    if (hi - lo) < rng_req:
        return False, None, None
    bodies = sum(abs(float(c['close']) - float(c['open'])) for c in w)
    avg_range = sum(abs(float(c['high']) - float(c['low'])) for c in w) / len(w)
    if avg_range <= 0 or bodies < body_mult * avg_range:
        return False, None, None
    completion = hi if direction == 'BUY' else lo
    return True, direction, round(completion, 2)


# --- early entry off the rogue anchor (NOT chasing the obvious top) ---------------
def entry_decision(anchor_price, leg_direction, current_price, cfg):
    """PURE early-entry rule for the next leg off the Rogue anchor. ENTER once price has
    moved rogue_entry_confirm ($) in leg_direction from the anchor -- early, on
    confirmation, ~$20 in -- NEVER chasing the full move. Returns (enter, entry_price,
    init_sl): init_sl is rogue_init_sl ($5) on the wrong side of the entry (tight: a
    fake-out is a small capped loss)."""
    confirm = float(getattr(cfg, 'rogue_entry_confirm', 20.0))
    init_sl = float(getattr(cfg, 'rogue_init_sl', 5.0))
    p = float(current_price)
    a = float(anchor_price)
    if leg_direction == 'BUY':
        if (p - a) >= confirm:
            return True, round(p, 2), round(p - init_sl, 2)
    elif leg_direction == 'SELL':
        if (a - p) >= confirm:
            return True, round(p, 2), round(p + init_sl, 2)
    return False, None, None


# --- adaptive trail: tight early, wider once the monster proves itself ------------
def trail_gap(profit_dollars, cfg):
    """PURE adaptive trail gap ($). Tight (rogue_trail_gap_early, $3) until profit reaches
    rogue_trail_widen_at ($15), then WIDE (rogue_trail_gap_deep, $6) so a proven monster
    is not shaken out by small wiggles on the way up. The wider deep gap gives back more
    at the top -- correct for monster-riding (ride further > exit early)."""
    early = float(getattr(cfg, 'rogue_trail_gap_early', 3.0))
    deep = float(getattr(cfg, 'rogue_trail_gap_deep', 6.0))
    widen_at = float(getattr(cfg, 'rogue_trail_widen_at', 15.0))
    return deep if float(profit_dollars) >= widen_at else early


# --- the day governor: cap + loss-stop + consecutive-fail-stop --------------------
def new_day_state():
    """Fresh per-day Rogue counters. reanchor_count = NEW entries today (the own
    counter); day_pnl = cumulative Rogue P&L; consec_fails = consecutive init-SL hits."""
    return {'reanchor_count': 0, 'day_pnl': 0.0, 'consec_fails': 0,
            'loss_stopped': False, 'fail_paused': False}


def can_enter(state, cfg):
    """PURE: may Rogue take a NEW entry now? Returns (ok, reason). Blocks when ANY brake
    is tripped -- the cap (rogue_max_reentries_per_day, 10), the daily loss stop
    (rogue_daily_loss_stop, -$150), or the consecutive-fail pause
    (rogue_consecutive_fail_stop, 3). RIDE-WINNER-UNLIMITED: this gates only NEW entries,
    never the trailing of an already-open winner."""
    cap = int(getattr(cfg, 'rogue_max_reentries_per_day', 10))
    loss_stop = float(getattr(cfg, 'rogue_daily_loss_stop', -150.0))
    fail_stop = int(getattr(cfg, 'rogue_consecutive_fail_stop', 3))
    if state.get('loss_stopped') or float(state.get('day_pnl', 0.0)) <= loss_stop:
        return False, 'daily_loss_stop'
    if state.get('fail_paused') or int(state.get('consec_fails', 0)) >= fail_stop:
        return False, 'consecutive_fail_pause'
    if int(state.get('reanchor_count', 0)) >= cap:
        return False, 'daily_cap'
    return True, 'ok'


def record_entry(state):
    """A NEW Rogue entry was taken (passed the setup gate AND can_enter). Consumes one
    re-entry slot. PURE."""
    state['reanchor_count'] = int(state.get('reanchor_count', 0)) + 1
    return state


def record_close(state, pnl_dollars, was_fail, cfg):
    """A Rogue position closed: book its P&L, advance/reset the consecutive-fail streak,
    and latch the loss-stop / fail-pause brakes if tripped. was_fail = the init-SL was
    hit (a fake-out). A winner resets the fail streak. PURE."""
    fail_stop = int(getattr(cfg, 'rogue_consecutive_fail_stop', 3))
    loss_stop = float(getattr(cfg, 'rogue_daily_loss_stop', -150.0))
    state['day_pnl'] = float(state.get('day_pnl', 0.0)) + float(pnl_dollars)
    if was_fail:
        state['consec_fails'] = int(state.get('consec_fails', 0)) + 1
    else:
        state['consec_fails'] = 0
    if state['day_pnl'] <= loss_stop:
        state['loss_stopped'] = True
    if int(state['consec_fails']) >= fail_stop:
        state['fail_paused'] = True
    return state


# --- closure isolation: a rogue close only ever closes rogue legs -----------------
def closes(position, scope):
    """PURE label-scoped closure check. A close issued for `scope` ('ROGUE' or 'ANCHOR')
    closes a position ONLY if the position belongs to that scope (by magic/leg_type).
    A ROGUE close never closes an anchor leg and vice versa -- there is NO generic
    close-all. `position` is a dict/obj with 'magic' (and/or 'leg_type')."""
    pos_magic = position.get('magic') if hasattr(position, 'get') else getattr(position, 'magic', None)
    pos_type = position.get('leg_type') if hasattr(position, 'get') else getattr(position, 'leg_type', None)
    is_rogue = (pos_magic == ROGUE_MAGIC) or (pos_type == ROGUE_LEG_TYPE)
    if str(scope).upper() == 'ROGUE':
        return bool(is_rogue)
    return not bool(is_rogue)   # ANCHOR scope closes only NON-rogue legs


# --- live driver (impure; SEPARATE call-site, fully gated by should_run) ----------
def account_is_demo(trader):
    """True when the broker account is a DEMO account (mirrors testfire's check)."""
    try:
        mt5 = trader.adapter.mt5
        ai = mt5.account_info()
        return int(getattr(ai, 'trade_mode', -1)) == int(getattr(mt5, 'ACCOUNT_TRADE_MODE_DEMO', 0))
    except Exception:
        return False


def promote_on_boot(trader):
    """DEMO default-ON: on a demo (non-funded) account the boot promotes rogue_enabled ON
    (the raw config default is OFF for funded-safety + the freeze). A funded account is
    NEVER promoted. Returns the effective rogue_enabled. Guarded; never raises."""
    try:
        is_demo = account_is_demo(trader)
        is_funded = not is_demo
        if funded_default(is_demo, is_funded):
            trader.cfg.rogue_enabled = True
            log.info(f"{ROGUE_ALERT_PREFIX} demo account -> rogue PROMOTED ON (trial).")
        elif is_funded:
            trader.cfg.rogue_enabled = False
            log.info(f"{ROGUE_ALERT_PREFIX} funded account -> rogue FORCED OFF (gate).")
        return bool(getattr(trader.cfg, 'rogue_enabled', False))
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} promote_on_boot non-fatal: {e!r}")
        return bool(getattr(trader.cfg, 'rogue_enabled', False))


def drive(trader):
    """The per-tick Rogue driver. Runs ONLY when should_run (rogue_enabled AND not
    funded) and rogue_daywatch -- otherwise an immediate no-op (no watch/anchor/entry).
    Pipeline: watch recent M5 -> detect monster -> drop anchor -> (governor + cap + gate)
    -> early entry on confirmation -> manage the open winner on the adaptive trail. All
    decisions come from the PURE cores above; this only does the IO + telemetry +
    placement (ROGUE-tagged via ROGUE_MAGIC). Fully guarded -- never raises onto _tick."""
    try:
        is_funded = not account_is_demo(trader)
        if not should_run(trader.cfg, is_funded=is_funded):
            return
        if not bool(getattr(trader.cfg, 'rogue_daywatch', True)):
            return
        today = ''
        try:
            today = str(trader.state.get('last_broker_date', ''))
        except Exception:
            today = ''
        st = getattr(trader, '_rogue', None)
        if st is None or st.get('day') != today:
            st = {'day': today, 'gov': new_day_state(),
                  'anchor': None, 'leg_dir': None, 'open': None}
            trader._rogue = st
        bars = _recent_m5(trader)
        if not bars:
            return
        # 1. WATCH / DETECT -> drop a fresh anchor at the move-completion price.
        is_monster, mdir, completion = detect_monster(bars, trader.cfg)
        if is_monster and st.get('open') is None:
            st['anchor'] = completion
            # the next leg Rogue hunts is the REVERSAL off the completed extreme: a SELL
            # move (low) -> hunt the BUY bounce; a BUY move (high) -> hunt the SELL fade.
            st['leg_dir'] = 'BUY' if mdir == 'SELL' else 'SELL'
            log.info(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} monster {mdir} -> anchor @ "
                     f"{completion}, hunting next leg {st['leg_dir']}")
        # 2. ENTRY (gated): only on a NEW slot AND the setup gate (a live anchor).
        price = _mid(trader)
        if (st.get('open') is None and st.get('anchor') is not None and price is not None):
            ok, _why = can_enter(st['gov'], trader.cfg)
            enter, epx, sl = entry_decision(st['anchor'], st['leg_dir'], price, trader.cfg)
            if enter:
                # MODEL GATE (pass-through by default). Computes + logs a confidence score
                # for EVERY confirmed setup; only BLOCKS when rogue_model_gate_enabled AND
                # the score is below threshold. With the gate disabled (default) this is
                # byte-neutral to the order path: placement still happens iff (ok and enter),
                # exactly as before -- the score is logged but never blocks. An untrained
                # model and any predict() error both score 1.0 (fail OPEN), so the model
                # can never silently kill Rogue. One eval logged per anchor (no flooding).
                if not _model_gate(trader, st, price, epx, sl, ok):
                    pass   # gated: SKIP_BY_MODEL already logged; do NOT enter
                elif ok:
                    _place_rogue_entry(trader, st, epx, sl)
        # 3. MANAGE the open winner on the adaptive trail (RIDE-WINNER-UNLIMITED).
        if st.get('open') is not None and price is not None:
            _manage_rogue_open(trader, st, price)
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} drive non-fatal: {e!r}")


def _recent_m5(trader):
    try:
        n = max(int(getattr(trader.cfg, 'rogue_min_candles', 4)) + 2, 6)
        for fn in ('get_latest_m5', 'get_latest_bars'):
            getter = getattr(trader.adapter, fn, None)
            if getter is None:
                continue
            bars = getter(trader.cfg.symbol, n)
            if bars is not None and len(bars) >= int(getattr(trader.cfg, 'rogue_min_candles', 4)):
                return [{'open': float(b['open']), 'high': float(b['high']),
                         'low': float(b['low']), 'close': float(b['close'])} for b in bars]
    except Exception:
        return None
    return None


def _mid(trader):
    try:
        tk = trader.adapter.mt5.symbol_info_tick(trader.cfg.symbol)
        return (float(tk.bid) + float(tk.ask)) / 2.0
    except Exception:
        return getattr(trader, '_last_boost_mid', None)


def _now_ts():
    try:
        import pandas as _pd
        return _pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def _spread(trader):
    try:
        tk = trader.adapter.mt5.symbol_info_tick(trader.cfg.symbol)
        return round(float(tk.ask) - float(tk.bid), 2)
    except Exception:
        return 0.0


def _model_gate(trader, st, price, epx, sl, ok):
    """The ML confidence gate at the Rogue entry point. Returns True to PROCEED, False to
    BLOCK (SKIP_BY_MODEL). It ALWAYS computes + logs a model_score for the confirmed setup
    (one row per anchor), but only BLOCKS when rogue_model_gate_enabled AND score < the
    threshold. PASS-THROUGH by default (gate disabled -> never blocks -> order path
    byte-identical; only a log row is added). Untrained model and any predict() error both
    score 1.0 (FAIL OPEN). Fully guarded -- a gate error never blocks Rogue."""
    try:
        import rogue_patternlog as _pl
        import rogue_model as _rm
        ts = _now_ts()
        confirm = abs(float(price) - float(st.get('anchor')))
        feats = _pl.build_features(_recent_m5(trader), spread=_spread(trader),
                                   confirm_dollars=confirm, ts=ts)
        score = _rm.get_model(getattr(trader.cfg, 'rogue_model_path', None)).predict(feats)
        gate_on = bool(getattr(trader.cfg, 'rogue_model_gate_enabled', False))
        thr = float(getattr(trader.cfg, 'rogue_model_threshold', 0.5))
        blocked = gate_on and (float(score) < thr)
        decision = _pl.SKIP_BY_MODEL if blocked else (_pl.ENTER if ok else _pl.SKIP)
        if st.get('rpl_eval_anchor') != st.get('anchor'):   # one eval per setup (no flood)
            _pl.log_eval(getattr(trader, 'run_dir', '.'), ts=ts, direction=st.get('leg_dir'),
                         features=feats, decision=decision, model_score=score)
            st['rpl_eval_anchor'] = st.get('anchor')
            if decision == _pl.ENTER:
                rpl = getattr(trader, '_rpl', None)
                if rpl is None:
                    rpl = {}
                    trader._rpl = rpl
                rpl['enter_ts'] = ts
        if blocked:
            trader.tele.info(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} SKIP_BY_MODEL "
                             f"score={round(float(score), 3)} < thr {thr}")
        return (not blocked)
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} model gate non-fatal: {e!r}")
        return True   # FAIL OPEN: a gate error must never block Rogue


def _place_rogue_entry(trader, st, entry_px, sl):
    """Place ONE ROGUE-tagged market entry (own magic). Consumes a re-entry slot. The
    rally/rescue reuse + the adaptive trail then manage it from the Rogue anchor."""
    side = st['leg_dir']
    tp = round(entry_px + (200.0 if side == 'BUY' else -200.0), 2)  # far TP; the trail governs
    try:
        res = trader.adapter.place_market_order(
            trader.cfg.symbol, side, trader.cfg.lot_size, sl=sl, tp=tp,
            magic=ROGUE_MAGIC, comment=f"AUR_ROGUE_{side[0]}", dry_run=trader.paper)
        rc = getattr(res, 'retcode', None) if res is not None else None
        tk = getattr(res, 'order', None) or getattr(res, 'deal', None)
        st['open'] = {'ticket': tk, 'side': side, 'entry': entry_px, 'sl': sl,
                      'peak': entry_px, 'magic': ROGUE_MAGIC, 'leg_type': ROGUE_LEG_TYPE}
        record_entry(st['gov'])
        try:
            import boost_metrics as _bm
            _bm.append_ledger(trader, {'ts': '', 'anchor': ROGUE_LABEL, 'kind': 'ROGUE',
                                       'event': 'enter', 'arm_px': st.get('anchor'),
                                       'entry_px': entry_px})
        except Exception:
            pass
        trader.tele.info(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} ENTER {side} @ {entry_px} "
                         f"SL {sl} (slot {st['gov']['reanchor_count']}/"
                         f"{int(getattr(trader.cfg, 'rogue_max_reentries_per_day', 10))}) rc={rc}")
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} place entry non-fatal: {e!r}")


def _manage_rogue_open(trader, st, price):
    """Trail the open Rogue winner with the adaptive gap (tight early, wide once proven).
    RIDE-WINNER-UNLIMITED: no $20-30 ceiling -- the trail rides until it closes."""
    o = st['open']
    side = o['side']
    sgn = 1.0 if side == 'BUY' else -1.0
    o['peak'] = max(o['peak'], price) if side == 'BUY' else min(o['peak'], price)
    profit = sgn * (o['peak'] - o['entry'])
    if profit < float(getattr(trader.cfg, 'rogue_trail_arm', 5.0)):
        return                      # trail not armed yet (still on the init SL)
    gap = trail_gap(profit, trader.cfg)
    new_sl = round(o['peak'] - sgn * gap, 2)
    # one-way ratchet
    if (side == 'BUY' and new_sl > o['sl']) or (side == 'SELL' and new_sl < o['sl']):
        o['sl'] = new_sl
        try:
            if o.get('ticket'):
                trader.adapter.modify_position_sl(int(o['ticket']), new_sl)
        except Exception:
            pass
