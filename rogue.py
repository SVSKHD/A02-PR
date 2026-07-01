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


# --- Fix 4: A1-ANCHORED REDESIGN — PURE cores (flag-gated; OFF -> never reached) ------
def a1_seed_anchor(last_close_level, a1_price):
    """Fix 4 ANCHOR: the price the next A1-mode entry is measured from. CHAIN to the last
    CLOSED Rogue level when one exists; otherwise seed from the day's A1 anchor price
    (read-only). Returns the anchor price, or None if neither is available (engine waits).
    PURE."""
    if last_close_level is not None:
        return float(last_close_level)
    if a1_price is not None:
        return float(a1_price)
    return None


def a1_entry_decision(anchor_price, current_price, cfg):
    """Fix 4 ENTRY: once price has moved rogue_entry_confirm_redesign ($10) off the anchor,
    ENTER in the MOVE direction (up -> BUY / down -> SELL) with a tight init SL
    (rogue_init_sl on the wrong side). Returns (enter, side, entry_price, init_sl). PURE."""
    confirm = float(getattr(cfg, 'rogue_entry_confirm_redesign', 10.0))
    init_sl = float(getattr(cfg, 'rogue_init_sl', 5.0))
    try:
        a = float(anchor_price)
        p = float(current_price)
    except (TypeError, ValueError):
        return False, None, None, None
    move = p - a
    if move >= confirm:
        return True, 'BUY', round(p, 2), round(p - init_sl, 2)
    if move <= -confirm:
        return True, 'SELL', round(p, 2), round(p + init_sl, 2)
    return False, None, None, None


def a1_reversal_confirmed(entry_price, side, current_price, cfg):
    """Fix 4 REVERSAL: price has crossed the entry AND moved rogue_reversal_dollars ($10)
    PAST entry AGAINST the trade -> the trial is known WRONG -> recover in the new direction.
    Measured in DOLLARS off entry (NOT candles). NOT a two-way hedge. Returns True/False.
    PURE."""
    rev = float(getattr(cfg, 'rogue_reversal_dollars', 10.0))
    try:
        e = float(entry_price)
        p = float(current_price)
    except (TypeError, ValueError):
        return False
    if side == 'BUY':
        return (e - p) >= rev           # dropped >= $10 below a BUY entry
    if side == 'SELL':
        return (p - e) >= rev           # rose >= $10 above a SELL entry
    return False


def a1_soft_lock_met(day_pnl, cfg):
    """Fix 4 TARGET: the soft floor (rogue_daily_soft_lock, $30) is BANKED but is NEVER a
    hard stop -- the engine keeps hunting after it is met. Returns True once met. PURE."""
    try:
        return float(day_pnl) >= float(getattr(cfg, 'rogue_daily_soft_lock', 30.0))
    except (TypeError, ValueError):
        return False


def a1_rescue_cap(cfg):
    """Fix 4 BRAKE (per-rescue): the combined cap ($) on a reversal recovery =
    rescue_boost_count x rogue_rescue_cap_dollars x lot x contract -- bounds the recovery.
    PURE."""
    return (int(getattr(cfg, 'rescue_boost_count', 2))
            * float(getattr(cfg, 'rogue_rescue_cap_dollars', 13.0))
            * float(getattr(cfg, 'lot_size', 0.35))
            * float(getattr(cfg, 'contract_size', 100.0)))


def _a1_anchor_price(trader):
    """READ-ONLY cross-read of the day's A1 anchor price from the anchor engine. Tries the
    A1 shadow position / pending entry price, then a persisted state key. Returns None if
    unavailable (the engine then waits). NEVER mutates or closes an anchor leg. Guarded."""
    try:
        for attr in ('shadow_positions', 'shadow_pendings'):
            book = getattr(trader, attr, None) or {}
            for sh in book.values():
                lbl = str(sh.get('anchor_label', '')) if hasattr(sh, 'get') else ''
                if lbl.startswith('A1'):
                    px = sh.get('leg_fill_price', sh.get('entry_price'))
                    if px is not None:
                        return float(px)
    except Exception:
        pass
    try:
        st = getattr(trader, 'state', {}) or {}
        for k in ('a1_anchor_price', 'A1_anchor_price', 'a1_price'):
            if st.get(k) is not None:
                return float(st[k])
    except Exception:
        pass
    return None


def _drive_a1(trader, st):
    """Fix 4 A1-ANCHORED driver (impure; runs ONLY when rogue_a1_anchor_mode is ON). Seeds
    from A1 (read-only) / chains to the last closed Rogue level, enters on a $10 move,
    rides continuation on the existing trail, and on a confirmed reversal closes the wrong-
    way leg + recovers in the new direction (capped). Brake = the live daily loss stop.
    The $30 soft lock is banked but never halts. ROGUE-only (magic 20260626). Guarded."""
    # A1-mode keys live alongside the shared anchor/open keys.
    st.setdefault('a1_last_close', None)   # last CLOSED Rogue level (chain target)
    st.setdefault('a1_reverted', False)    # next entry is a capped recovery leg
    price = _mid(trader)
    if price is None:
        return
    # book a broker-side close FIRST (frees the slot + records the level for chaining).
    o_before = st.get('open')
    if o_before is not None:
        # REVERSAL: the open leg is known wrong -> close it (broker SL likely already did)
        # and chain-recover in the NEW direction next.
        if a1_reversal_confirmed(o_before.get('entry'), o_before.get('side'), price, trader.cfg):
            tk = o_before.get('ticket')
            try:
                if tk is not None:
                    trader.adapter.close_position(int(tk), dry_run=trader.paper)  # ROGUE only
            except Exception:
                pass
            st['a1_last_close'] = float(o_before.get('entry'))
            st['a1_reverted'] = True
            detect_close(trader, st)        # book P&L + clear st['open'] (guarded)
            return
        # CONTINUATION: ride the winner on the proven adaptive trail.
        _manage_rogue_open(trader, st, price)
        # also book a broker close if the trail/SL fired.
        detect_close(trader, st)
        if st.get('open') is not None:
            st['a1_last_close'] = None      # still riding
        return
    # no open: book any pending close, then try a fresh entry off the (chained) anchor.
    detect_close(trader, st)
    anchor = a1_seed_anchor(st.get('a1_last_close'), _a1_anchor_price(trader))
    if anchor is None:
        return
    ok, _why = can_enter(st['gov'], trader.cfg)   # BRAKE: daily loss stop / cap / fail-pause
    if not ok:
        return
    enter, side, epx, sl = a1_entry_decision(anchor, price, trader.cfg)
    if not enter:
        return
    # a reversal-recovery leg uses the wider per-rescue cap as its SL (still bounded).
    if st.get('a1_reverted'):
        cap = float(getattr(trader.cfg, 'rogue_rescue_cap_dollars', 13.0))
        sl = round(epx - cap, 2) if side == 'BUY' else round(epx + cap, 2)
        st['a1_reverted'] = False
    st['leg_dir'] = side
    st['anchor'] = anchor
    _place_rogue_entry(trader, st, epx, sl)
    st['a1_last_close'] = None


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
        # Fix 4: A1-ANCHORED REDESIGN (flag-gated, DEFAULT OFF). ON -> the new engine seeds
        # from the day's A1 anchor (read-only) / chains to the last closed Rogue level and
        # skips monster-detection. OFF (default) -> fall through to the legacy monster
        # pipeline below, byte-identical.
        if bool(getattr(trader.cfg, 'rogue_a1_anchor_mode', False)):
            _drive_a1(trader, st)
            return
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
        # 3. DETECT a broker-side close FIRST (E-2/E-3): book the governor + clear st['open']
        # so the day-stop/fail-pause get real data AND Rogue can re-enter the same day (and
        # the patternlog observe() close branch then runs). If still open, manage the trail.
        if st.get('open') is not None:
            detect_close(trader, st)
        # 4. MANAGE the open winner on the adaptive trail (RIDE-WINNER-UNLIMITED).
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
                         features=feats, decision=decision, model_score=score,
                         entry_price=(round(float(epx), 2) if decision == _pl.ENTER else ''))
            st['rpl_eval_anchor'] = st.get('anchor')
            if decision == _pl.ENTER:
                rpl = getattr(trader, '_rpl', None)
                if rpl is None:
                    rpl = {}
                    trader._rpl = rpl
                rpl['enter_ts'] = ts
                rpl['enter_price'] = round(float(epx), 2)
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


# --- E-2/E-3 close-detection + governor wiring (Rogue-ONLY, never closes anything) -----
def _rogue_close_pnl(trader, ticket):
    """Realized $ of a CLOSED Rogue position from its broker close deal (entry==1):
    profit + swap + commission (same convention as the anchor fill path). Returns None if
    the close deal isn't in history yet. READ-ONLY."""
    try:
        deals = trader.adapter.mt5.history_deals_get(position=int(ticket)) or []
        cd = next((d for d in deals if getattr(d, 'entry', None) == 1), None)
        if cd is None:
            return None
        return float(cd.profit) + float(cd.swap) + float(cd.commission)
    except Exception:
        return None


def detect_close(trader, st):
    """E-2/E-3: detect a BROKER-side close of the open Rogue position and book it ONCE --
    update the day-governor via record_close (day_pnl / consec_fails / loss_stopped /
    fail_paused) and clear st['open'] so Rogue can re-enter the same day AND the patternlog
    observe() close branch runs. Returns True if a close was booked.

    ISOLATION: only ever inspects st['open']'s OWN ticket and issues NO close (the broker
    SL/TP already closed it) -- it can NEVER touch an anchor (20260522) ticket. Rogue P&L
    stays in the governor; it is NOT mixed into the anchor state['daily_pnl'] (the global
    kill switch still sees Rogue via live equity). Guarded; never raises onto the tick."""
    o = st.get('open')
    if not o or o.get('ticket') is None:
        return False
    tk = int(o['ticket'])
    try:
        still = trader.adapter.mt5.positions_get(ticket=tk)
    except Exception:
        return False
    if still:
        return False                     # still open at the broker -> nothing to book
    pnl = _rogue_close_pnl(trader, tk)
    if pnl is None:
        pnl = 0.0                        # close deal not in history yet -> book 0, still clear
    was_fail = float(pnl) <= 0.0         # a non-winning close = init-SL fake-out (winner resets)
    record_close(st['gov'], pnl, was_fail, trader.cfg)
    st['open'] = None
    try:
        g = st['gov']
        brake = ('LOSS-STOP' if g.get('loss_stopped')
                 else ('FAIL-PAUSE' if g.get('fail_paused') else 'live'))
        trader.tele.info(
            f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} CLOSE {o.get('side')} #{tk} "
            f"P&L ${float(pnl):+.2f} | day ${float(g.get('day_pnl', 0.0)):+.2f} | "
            f"fails {int(g.get('consec_fails', 0))} | {brake}")
    except Exception:
        pass
    return True


def eod_flatten(trader):
    """E-4 (flag rogue_flatten_at_eod, DEFAULT OFF): at EOD close an OPEN Rogue position so
    it does not ride overnight on its own SL/TP. DEFAULT OFF -> no-op (rides, current
    behavior). ROGUE-ONLY: closes ONLY st['open']'s own ticket (never an anchor 20260522
    ticket), then books it via the governor + clears st['open']. Guarded; never raises."""
    try:
        if not bool(getattr(trader.cfg, 'rogue_flatten_at_eod', False)):
            return False
        st = getattr(trader, '_rogue', None)
        if not st or not st.get('open') or st['open'].get('ticket') is None:
            return False
        tk = int(st['open']['ticket'])
        trader.adapter.close_position(tk, dry_run=trader.paper)   # ROGUE ticket ONLY
        pnl = _rogue_close_pnl(trader, tk)
        if pnl is None:
            pnl = 0.0
        record_close(st['gov'], pnl, float(pnl) <= 0.0, trader.cfg)
        st['open'] = None
        try:
            trader.tele.info(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} EOD flatten -> closed "
                             f"#{tk} P&L ${float(pnl):+.2f}")
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} eod_flatten non-fatal: {e!r}")
        return False


# --- manual current-tick seed (mid-day restart: no A1 event to seed the A1-mode engine) --
def manual_seed_ok(cfg, is_demo):
    """PURE gate for `rogueseed`. Returns (ok, reason). Valid ONLY when rogue_a1_anchor_mode
    is ON (else 'disabled' -- tell the user to enable it) AND the account is DEMO (funded
    refuses, FAIL-CLOSED -- the same gate as rogue promotion). No side effects."""
    if not bool(getattr(cfg, 'rogue_a1_anchor_mode', False)):
        return False, 'disabled'
    if not bool(is_demo):
        return False, 'funded'
    return True, 'ok'


def manual_seed(trader, price):
    """Plant the Rogue A1-mode anchor at `price` (the current live tick) ON DEMAND, so a
    mid-day restart (no A1 event) can seed the Fix 4 engine without waiting for A1. Sets the
    engine's chain target (st['a1_last_close']) to the seed; from there the EXISTING _drive_a1
    takes over UNCHANGED -- enters on a $10 move off the seed, reversal at $10 past entry,
    the -$525 brake, the $13 rescue cap. Adds NO new trade logic. DEMO-only + a1-mode-only
    (manual_seed_ok); ROGUE-only (never touches an anchor 20260522 ticket). Returns
    (ok, reason, price). Guarded; never raises."""
    try:
        is_demo = account_is_demo(trader)
        ok, reason = manual_seed_ok(trader.cfg, is_demo)
        if not ok:
            msg = ("enable rogue_a1_anchor_mode first" if reason == 'disabled'
                   else "DEMO-only (funded refused)")
            log.warning(f"{ROGUE_ALERT_PREFIX} MANUAL SEED refused ({reason}): {msg}")
            try:
                trader.tele.warn(f"{ROGUE_ALERT_PREFIX} 🌱 manual seed refused — {msg}")
            except Exception:
                pass
            return False, reason, None
        if price is None:
            log.warning(f"{ROGUE_ALERT_PREFIX} MANUAL SEED refused (no_tick): no sane tick")
            return False, 'no_tick', None
        price = round(float(price), 2)
        # ensure the per-day Rogue state exists (mirrors drive()'s init on a fresh restart).
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
        # plant the seed as the chain target the Fix 4 engine anchors from.
        st['a1_last_close'] = price
        st['a1_reverted'] = False
        confirm = float(getattr(trader.cfg, 'rogue_entry_confirm_redesign', 10.0))
        log.info(f"{ROGUE_ALERT_PREFIX} MANUAL SEED @ {price} (current tick) -> hunting "
                 f"${confirm:.0f} move both directions")
        try:
            trader.tele.info(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} MANUAL SEED @ {price} "
                             f"(current tick) — hunting ${confirm:.0f} move both directions")
        except Exception:
            pass
        return True, 'ok', price
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} manual_seed non-fatal: {e!r}")
        return False, 'error', None


def seed_tick_price(trader):
    """Read a SANE current tick to seed at -- the same sane/held tick discipline A1's
    tick-fallback uses: sample a few ticks and settle via tick_hold; fall back to the current
    mid if it can't settle. Returns a price or None. Guarded, READ-ONLY."""
    try:
        import tick_hold as _th
        prices = []
        for _ in range(int(getattr(trader.cfg, 'a1_tick_fallback_samples', 6))):
            try:
                tk = trader.adapter.mt5.symbol_info_tick(trader.cfg.symbol)
                prices.append((float(tk.bid) + float(tk.ask)) / 2.0)
            except Exception:
                pass
        if prices:
            try:
                ok, price, _held, _reason = _th.settle_anchor_tick(prices, trader.cfg)
                if ok and price is not None:
                    return round(float(price), 2)
            except Exception:
                pass
    except Exception:
        pass
    return _mid(trader)   # fallback: the current mid (already guarded)


def enqueue_seed_command(cfg):
    """CLI `python bot.py rogueseed`: enqueue a 'rogueseed' command onto the RUNNING bot's
    command channel (AUREON_RUN_DIR/commands.json) so the live loop plants the seed at ITS
    current tick (where the live _rogue state + adapter live). Returns 0 on enqueue, 2 on
    error. The DEMO-only / a1-mode gate is enforced by manual_seed when the bot handles it."""
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
        cmds.append({"cmd": "rogueseed"})
        with open(path, "w") as f:
            _json.dump(cmds, f)
        # Log the ABSOLUTE path so a run_dir mismatch (the launcher and the running bot
        # resolving AUREON_RUN_DIR / cwd differently) is immediately visible: this path MUST
        # match the running bot's run_dir/commands.json, or the bot will never see it.
        abspath = _os.path.abspath(path)
        log.info(f"{ROGUE_ALERT_PREFIX} rogueseed queued -> {abspath} "
                 f"(AUREON_RUN_DIR={_os.environ.get('AUREON_RUN_DIR', '<unset:./run>')}). "
                 f"The running bot consumes this each tick and plants the Rogue anchor at its "
                 f"current tick (DEMO-only; funded refuses; requires rogue_a1_anchor_mode ON). "
                 f"If nothing happens, confirm this path matches the bot's run dir.")
        return 0
    except Exception as e:
        log.error(f"{ROGUE_ALERT_PREFIX} rogueseed enqueue failed: {e!r}")
        return 2
