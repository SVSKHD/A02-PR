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

import seed_budget   # SHARED $10-break seed anchor (Rule 1) + earned trade budget (Rule 2)

log = logging.getLogger("AUREON")

# --- tagging (distinct from the anchors) -----------------------------------------
ROGUE_MAGIC = 20260626          # distinct from the anchor magic (20260522) + warmup (9999998)
ROGUE_LABEL = "ROGUE"
ROGUE_LEG_TYPE = "rogue"
ROGUE_ALERT_PREFIX = "[ROGUE]"


def rogue_impl(cfg) -> str:
    """The SINGLE source of truth for which Rogue implementation `drive()` runs, from
    config alone (config wins on every tick / new-day boot; nothing persisted can
    silently revert it). 'stop' -> the resting pending-stop engine (rogue_stop);
    'band' -> the A1-anchored confirm-band engine; 'legacy' -> monster detection.
    2026-07-17: a live day ran 'band' with 174 BAND_NOT_HELD rejects despite the flag
    — this makes the choice explicit (boot banner + review event) so a silent revert
    can never go unnoticed again, and mirrors drive()'s dispatch order exactly."""
    if bool(getattr(cfg, 'rogue_stop_mode', False)):
        return 'stop'
    if bool(getattr(cfg, 'rogue_a1_anchor_mode', False)):
        return 'band'
    return 'legacy'
ROGUE_GLYPH = "🦏"               # chart glyph distinct from the anchor glyphs


def _persist_state(trader):
    """Fix 5 (E-16) hook: after any Rogue state change, persist the P1 snapshot to
    run/state.json (anchor / a1_last_close / open ticket / governors / latches). Fully
    guarded -- a persistence error never reaches the trading path. No-op if p1_state or
    the trader run_dir is unavailable."""
    try:
        import p1_state as _p1
        _p1.save(trader)
    except Exception:
        pass


# --- the demo-default-ON / funded-OFF run gate (freeze-safe) ----------------------
def funded_default(is_demo, is_funded):
    """The value the boot promotes rogue_enabled to per account type: ON for a demo
    (non-funded) account, OFF for funded. v3.6.0: the config boot default is now
    True, but this per-account promotion stays authoritative on every boot -- a
    funded account is always forced OFF regardless of the config value. PURE."""
    if is_funded:
        return False
    return bool(is_demo)


def should_run(cfg, is_funded=False):
    """The single effective on/off for the ENTIRE Rogue mechanism. rogue_enabled is the
    master switch; a FUNDED account force-disables it (mandatory gate) regardless of the
    flag -- un-proven Rogue never boots ON on real capital. With rogue_enabled explicitly
    False this is False -> no watch, no anchor, no entry. (v3.6.0: the config boot
    default is True; the runtime /rogue engine switch ANDs on top of this at the
    drive() call site, it never replaces this gate.) PURE."""
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
    counter); day_pnl = cumulative Rogue P&L; consec_fails = consecutive init-SL hits.
    profit_locked/override/alerted drive the SOFT daily-profit lock (2026-07-08):
    day_pnl >= rogue_daily_profit_stop -> manage-only, overridable ONCE/day by a manual
    reseed (profit_override), one-time alert (profit_alerted)."""
    return {'reanchor_count': 0, 'day_pnl': 0.0, 'consec_fails': 0,
            'loss_stopped': False, 'fail_paused': False,
            'profit_locked': False, 'profit_override': False, 'profit_alerted': False}


def can_enter(state, cfg):
    """PURE: may Rogue take a NEW entry now? Returns (ok, reason). Blocks when ANY brake
    is tripped -- the daily loss stop (rogue_daily_loss_stop; HARD, never overridable), the
    SOFT daily profit lock (rogue_daily_profit_stop; manage-only, cleared by profit_override
    from a manual reseed), the consecutive-fail pause (rogue_consecutive_fail_stop), or the
    cap (rogue_max_reentries_per_day). RIDE-WINNER-UNLIMITED: gates only NEW entries, never
    the trailing of an already-open winner. profit_stop / loss_stop == 0 disable that gate."""
    cap = int(getattr(cfg, 'rogue_max_reentries_per_day', 10))
    loss_stop = float(getattr(cfg, 'rogue_daily_loss_stop', -150.0))
    profit_stop = float(getattr(cfg, 'rogue_daily_profit_stop', 0.0))
    fail_stop = int(getattr(cfg, 'rogue_consecutive_fail_stop', 3))
    if loss_stop < 0.0 and (state.get('loss_stopped')
                            or float(state.get('day_pnl', 0.0)) <= loss_stop):
        return False, 'daily_loss_stop'          # loss_stop == 0 disables the gate
    if (profit_stop > 0.0 and not state.get('profit_override')
            and (state.get('profit_locked')
                 or float(state.get('day_pnl', 0.0)) >= profit_stop)):
        return False, 'daily_profit_stop'         # profit_stop == 0 disables the gate
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
    hit (a fake-out); a winner resets the fail streak. Fix 2 (E-14): was_fail=None means
    the P&L was UNRESOLVED after retries -> book the P&L but leave the fail streak UNCHANGED
    (neither increment nor reset), so an unbooked close can't trip the fail-pause. PURE."""
    fail_stop = int(getattr(cfg, 'rogue_consecutive_fail_stop', 3))
    loss_stop = float(getattr(cfg, 'rogue_daily_loss_stop', -150.0))
    profit_stop = float(getattr(cfg, 'rogue_daily_profit_stop', 0.0))
    state['day_pnl'] = float(state.get('day_pnl', 0.0)) + float(pnl_dollars)
    if was_fail is None:
        pass                                       # E-14: pnl-unresolved -> streak untouched
    elif was_fail:
        state['consec_fails'] = int(state.get('consec_fails', 0)) + 1
    else:
        state['consec_fails'] = 0
    if loss_stop < 0.0 and state['day_pnl'] <= loss_stop:
        state['loss_stopped'] = True             # loss_stop == 0 disables the gate
    if int(state['consec_fails']) >= fail_stop:
        state['fail_paused'] = True
    # SOFT profit lock: latch once realized day P&L reaches the target (unless a manual
    # reseed already overrode it for the day). The one-time alert fires in detect_close.
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
            ps = float(getattr(trader.cfg, 'rogue_daily_profit_stop', 0.0))
            msg = (f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} DAY PROFIT STOP "
                   f"+${float(g.get('day_pnl', 0.0)):.0f} >= ${ps:.0f} — entries locked "
                   f"(reseed to override)")
            log.warning(msg)
            try:
                trader.tele.warn(msg)
            except Exception:
                pass
            _persist_state(trader)
    except Exception:
        pass


# --- E-20: restart-recovery gov rebuild from BROKER deal history (mirror fetcher) ------
def rebuild_gov_from_history(trader, dt_from=None, dt_to=None):
    """E-20 LESSON (Rogue): on a SAME-DAY restart the day governor (day_pnl / reanchor_count
    / consec_fails) must NOT reset to zero -- a restart mid-day would otherwise re-arm the
    full cap and forget a tripped brake. REBUILD from BROKER truth: every magic-20260626
    deal in the current broker day. reanchor_count (= NEW entries) = count of entry-IN deals;
    day_pnl = sum(profit+swap+commission) over entry-OUT deals; consec_fails = the trailing
    run of losing closes (time-ordered). Latches loss_stopped / fail_paused / profit_locked
    per the cfg thresholds. profit_override / profit_alerted are RUNTIME decisions that can't
    be derived from history -- the caller overlays them from the persisted snapshot. Returns
    a rebuilt gov dict, or None if history is unavailable (caller keeps the snapshot).
    READ-ONLY; guarded. Mirrors fetcher.rebuild_gov_from_history exactly."""
    try:
        if dt_from is None or dt_to is None:
            dt_from, dt_to = _broker_day_range(trader)
        deals = trader.adapter.mt5.history_deals_get(dt_from, dt_to) or []
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} rebuild history query failed: {e!r}")
        return None
    try:
        import pnl_source as _ps
        ours = [d for d in deals if int(getattr(d, 'magic', 0) or 0) == ROGUE_MAGIC]
        ins = [d for d in ours if getattr(d, 'entry', None) == 0]
        outs = [d for d in ours if getattr(d, 'entry', None) == 1]
        outs.sort(key=lambda d: getattr(d, 'time', 0) or 0)
        _pnl = _ps.deal_pnl                            # single source: profit+swap+commission
        gov = new_day_state()
        gov['reanchor_count'] = len(ins)
        # SINGLE SOURCE OF TRUTH: realized day P&L = pnl_source.magic_day_net over the SAME
        # deal sweep, by magic (identical to what the report + reconcile + /status read).
        gov['day_pnl'] = _ps.magic_day_net(deals, ROGUE_MAGIC)
        fails = 0
        for d in reversed(outs):
            if _pnl(d) <= 0.0:
                fails += 1
            else:
                break
        gov['consec_fails'] = fails
        fail_stop = int(getattr(trader.cfg, 'rogue_consecutive_fail_stop', 3))
        loss_stop = float(getattr(trader.cfg, 'rogue_daily_loss_stop', -150.0))
        profit_stop = float(getattr(trader.cfg, 'rogue_daily_profit_stop', 0.0))
        gov['loss_stopped'] = bool(loss_stop < 0.0 and gov['day_pnl'] <= loss_stop)
        gov['fail_paused'] = bool(gov['consec_fails'] >= fail_stop)
        gov['profit_locked'] = bool(profit_stop > 0.0 and gov['day_pnl'] >= profit_stop)
        log.info(f"{ROGUE_ALERT_PREFIX} gov rebuilt from history: entries={gov['reanchor_count']} "
                 f"day_pnl=${gov['day_pnl']:+.2f} consec_fails={gov['consec_fails']} "
                 f"loss_stopped={gov['loss_stopped']} fail_paused={gov['fail_paused']} "
                 f"profit_locked={gov['profit_locked']}")
        return gov
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} rebuild parse failed: {e!r}")
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


# rogue.py promote_on_boot
def promote_on_boot(trader):
    """DEMO default-ON / FUNDED forced-OFF, UNLESS the owner set an explicit
    bool in config. None = auto-promote by account type (legacy behavior).
    True/False = owner's explicit choice; funded still forces OFF regardless
    (the mandatory gate is never overridable upward)."""
    try:
        is_demo = account_is_demo(trader)
        is_funded = not is_demo
        explicit = getattr(trader.cfg, 'rogue_enabled', None)

        if is_funded:                       # mandatory gate — always wins
            trader.cfg.rogue_enabled = False
            log.info(f"{ROGUE_ALERT_PREFIX} funded account -> rogue FORCED OFF (gate).")
        elif explicit is None:              # no owner opinion -> auto-promote
            trader.cfg.rogue_enabled = True
            log.info(f"{ROGUE_ALERT_PREFIX} demo account -> rogue PROMOTED ON (trial).")
        else:                               # owner said so, respect it
            trader.cfg.rogue_enabled = bool(explicit)
            log.info(f"{ROGUE_ALERT_PREFIX} demo account -> rogue {'ON' if explicit else 'OFF'} "
                     f"(explicit config override; promotion skipped).")
        # 2026-07-17 hardening: make the running implementation EXPLICIT at boot so a
        # silent band-mode revert (174 BAND_NOT_HELD rejects that live day) can never go
        # unnoticed. Config wins on every boot; the persisted value is audit-only.
        impl = rogue_impl(trader.cfg)
        log.info(f"{ROGUE_ALERT_PREFIX} ROGUE IMPL: {impl} "
                 f"(rogue_stop_mode={bool(getattr(trader.cfg, 'rogue_stop_mode', False))}, "
                 f"rogue_a1_anchor_mode={bool(getattr(trader.cfg, 'rogue_a1_anchor_mode', False))})")
        try:
            trader.state['rogue_impl'] = impl
        except Exception:
            pass
        try:
            import review_log as _rv
            _rv.get_review_logger(trader.cfg).governor('ROGUE', 'engine_impl', detail=impl)
        except Exception:
            pass
        return bool(trader.cfg.rogue_enabled)
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
    (rogue_init_sl on the wrong side). Returns (enter, side, entry_price, init_sl). PURE.

    P3 GATE 1 (E-17 CHASE CAP): the entry band is confirm <= |move| <=
    rogue_chase_cap_dollars ($10..$20 default). Beyond the cap the move is EXHAUSTED --
    entering there is chasing (live 2026-07-02 trade 3: chain re-anchored mid-trend,
    bought the $24 extension, -$178.50). Mirrors the anchor engine's catchable-zone cap
    on in-flight breakout recovery (anchors.py ~:807). NO latch: this re-evaluates per
    tick, so if price pulls back inside the band later the entry is allowed again.
    Cap <= 0 disables (old unbounded behavior). Applies to every A1-mode entry,
    including the reversal-recovery leg (the cooldown exemption is separate)."""
    confirm = float(getattr(cfg, 'rogue_entry_confirm_redesign', 10.0))
    init_sl = float(getattr(cfg, 'rogue_init_sl', 5.0))
    cap = float(getattr(cfg, 'rogue_chase_cap_dollars', 0.0) or 0.0)
    try:
        a = float(anchor_price)
        p = float(current_price)
    except (TypeError, ValueError):
        return False, None, None, None
    move = p - a
    if cap > 0 and abs(move) > cap:
        return False, None, None, None       # GATE 1: exhausted move -> no chase
    if move >= confirm:
        return True, 'BUY', round(p, 2), round(p - init_sl, 2)
    if move <= -confirm:
        return True, 'SELL', round(p, 2), round(p + init_sl, 2)
    return False, None, None, None


def runaway_should_reanchor(st, anchor_price, current_price, cfg):
    """2026-07-16 RUNAWAY detector (PURE, no mutation): has price RUN >= rogue_runaway_trigger
    past the ACTIVE anchor in one direction? Returns (trigger, direction) with direction
    'UP'/'DN' (the continuation side). The CALLER additionally gates on "no open position AND
    no close preceded this anchor" (a1_last_close is None -> the seed anchor, OR runaway_active
    -> an existing runaway chain); this core only decides the distance + loop guards:

      * disabled (flag off or trigger <= 0) -> never,
      * < 3 re-anchors used today (rogue caps the loop at 3/day), AND
      * >= rogue_runaway_trigger from the PREVIOUS runaway price (spacing guard) so a single
        fast leg can't plant a stack of re-anchors on top of each other."""
    if not bool(getattr(cfg, 'rogue_runaway_reanchor_enabled', False)):
        return False, None
    trig = float(getattr(cfg, 'rogue_runaway_trigger', 0.0) or 0.0)
    if trig <= 0.0:
        return False, None
    try:
        a = float(anchor_price)
        p = float(current_price)
    except (TypeError, ValueError):
        return False, None
    move = p - a
    if abs(move) < trig:
        return False, None
    if int(st.get('runaway_count', 0)) >= 3:
        return False, None                    # loop guard: max 3 runaway re-anchors per day
    last = st.get('runaway_last_px')
    if last is not None:
        try:
            if abs(p - float(last)) < trig:
                return False, None            # each re-anchor must be >= trigger from the last
        except (TypeError, ValueError):
            pass
    return True, ('UP' if move > 0 else 'DN')


def runaway_entry_decision(anchor_price, current_price, runaway_dir, cfg):
    """2026-07-16 RUNAWAY continuation entry (PURE). Off a runaway re-anchor, ENTER only in
    the SAME direction as the runaway once price has displaced rogue_runaway_confirm ($) from
    the new anchor -- CONTINUATION ONLY. A counter-trend move (opposite the runaway) is
    REFUSED (never fade a runaway re-anchor). The chase cap (rogue_chase_cap_dollars) still
    bounds the far side and the init SL is the normal rogue_init_sl. Returns
    (enter, side, entry_price, init_sl). Mirrors a1_entry_decision's shape/SL geometry, with a
    smaller confirm and the direction lock."""
    confirm = float(getattr(cfg, 'rogue_runaway_confirm', 8.0))
    init_sl = float(getattr(cfg, 'rogue_init_sl', 10.0))
    cap = float(getattr(cfg, 'rogue_chase_cap_dollars', 0.0) or 0.0)
    try:
        a = float(anchor_price)
        p = float(current_price)
    except (TypeError, ValueError):
        return False, None, None, None
    move = p - a
    if cap > 0 and abs(move) > cap:
        return False, None, None, None        # exhausted move -> no chase (same as A1 entry)
    if runaway_dir == 'DN':
        if move <= -confirm:
            return True, 'SELL', round(p, 2), round(p + init_sl, 2)
        return False, None, None, None        # above the anchor = counter-trend / not yet
    if runaway_dir == 'UP':
        if move >= confirm:
            return True, 'BUY', round(p, 2), round(p - init_sl, 2)
        return False, None, None, None        # below the anchor = counter-trend / not yet
    return False, None, None, None


def chase_rejected(anchor_price, current_price, cfg):
    """P3 GATE 1 (E-17) telemetry helper: is the current tick a CHASE reject -- |move| off
    the anchor beyond rogue_chase_cap_dollars? Returns (rejected, move) where move is
    SIGNED (+ above the anchor / - below). Cap <= 0 -> never rejected. PURE (the decision
    itself lives inside a1_entry_decision; this exists so the driver can log the reject
    without re-deriving the band)."""
    cap = float(getattr(cfg, 'rogue_chase_cap_dollars', 0.0) or 0.0)
    if cap <= 0:
        return False, 0.0
    try:
        move = float(current_price) - float(anchor_price)
    except (TypeError, ValueError):
        return False, 0.0
    return (abs(move) > cap), move


def chain_entry_allowed(chain_time, now_epoch, disp_in_dir, cfg):
    """P3 GATE 2 (E-17 CHAIN COOLDOWN + DISPLACEMENT): may an entry fire off a CHAINED
    anchor (a detect_close re-anchor)? Requires BOTH (a) rogue_chain_cooldown_sec elapsed
    since the close that planted it, AND (b) the observed displacement from the re-anchor
    price, in the entry direction, to have reached rogue_chain_min_displacement at some
    point since planting -- the $10 confirm must build from FRESH movement, not the tail
    of the move that just closed. Returns (ok, reason, cooldown_remaining_sec); reason is
    'ok' / 'cooldown' / 'displacement'. Each check independently disabled by <= 0. NOT
    applied to the A1 morning seed, a manual rogueseed, or a reversal-recovery leg (the
    driver decides what is chained). PURE."""
    cool = float(getattr(cfg, 'rogue_chain_cooldown_sec', 0.0) or 0.0)
    disp_req = float(getattr(cfg, 'rogue_chain_min_displacement', 0.0) or 0.0)
    if cool > 0 and chain_time is not None:
        try:
            remaining = cool - (float(now_epoch) - float(chain_time))
        except (TypeError, ValueError):
            remaining = 0.0
        if remaining > 0:
            return False, 'cooldown', remaining
    if disp_req > 0:
        try:
            if float(disp_in_dir) < disp_req:
                return False, 'displacement', 0.0
        except (TypeError, ValueError):
            return False, 'displacement', 0.0
    return True, 'ok', 0.0


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


# --- v3.6.0 ROGUE SEED INDEPENDENCE (anchors-off must NOT stop Rogue) --------------
# Seed-source labels: every seed logs "ROGUE SEED via <SOURCE> @ price" and stamps
# st['seed_source'] so ledger/pattern-log rows stay segmentable per source (D-8).
SEED_A1_ANCHOR = 'A1_ANCHOR'               # the real A1 anchor read (master behavior)
SEED_A1_TIME_SNAPSHOT = 'A1_TIME_SNAPSHOT' # tick price captured AT A1's scheduled time
SEED_MARKET_OPEN = 'MARKET_OPEN'           # first live tick price of the broker day
SEED_MANUAL = 'MANUAL'                     # the rogueseed command (manual_seed)


def _anchors_engine_on(trader):
    """Runtime state of the ANCHOR engine switch (live_trader.engines['anchors'],
    /anchors on|off). GUARDED: a trader without the runtime dict (stubs, old
    snapshots) reads ON -> the A1_ANCHOR read, i.e. master behavior."""
    eng = getattr(trader, 'engines', None)
    if not isinstance(eng, dict):
        return True
    return bool(eng.get('anchors', True))


def _a1_gave_up(trader):
    """True iff A1 is recorded MISSED for today (its late window elapsed with no
    placement) -- the 'A1 otherwise doesn't place' branch of the seed fallback.
    Guarded; False on any doubt (master behavior: keep waiting for A1)."""
    try:
        missed = (getattr(trader, 'state', {}) or {}).get('missed_anchors_today', []) or []
        return any(str(lbl).startswith('A1') for lbl in missed)
    except Exception:
        return False


def _a1_sched_reached(trader):
    """True once broker wall-clock has reached A1's scheduled time today -- resolved
    via the SAME resolver the anchor engine uses (_resolved_anchor_hm, Monday cushion
    included), so the a1_time_snapshot capture has IDENTICAL timing to a real A1.
    Guarded: any missing seam / error -> False (never snapshot early)."""
    try:
        import pandas as _pd
        label, hour, minute = trader.cfg.anchors[0]
        utc_now = _pd.Timestamp.now(tz='UTC')
        bdate = (utc_now + _pd.Timedelta(hours=trader.cfg.broker_tz_offset_hours)).date()
        rh, rm = trader._resolved_anchor_hm(label, bdate, hour, minute)
        sched = trader._anchor_datetime_utc(bdate, rh,
                                            trader.cfg.broker_tz_offset_hours, rm)
        return bool(utc_now >= sched)
    except Exception:
        return False


def _capture_seed_snapshots(trader, st, price):
    """PASSIVE per-tick capture of the two fallback seed candidates (NO orders):
    day_open_px = the first live tick price of the broker trading day this driver
    saw; a1_snap_px = the tick price at A1's scheduled clock time. Both are captured
    regardless of the current switch state (capturing a price is free), so a mid-day
    /rogue on can still seed with A1 timing; resolve_seed picks AT SEED TIME which
    one (if either) is used. Persisted with the governors (p1_state). Guarded."""
    try:
        if price is None:
            return
        changed = False
        if st.get('day_open_px') is None:
            st['day_open_px'] = round(float(price), 2)
            changed = True
        if st.get('a1_snap_px') is None and _a1_sched_reached(trader):
            st['a1_snap_px'] = round(float(price), 2)
            log.info(f"{ROGUE_ALERT_PREFIX} A1-time snapshot captured @ "
                     f"{st['a1_snap_px']} (fallback seed candidate; no order placed)")
            changed = True
        if changed:
            _persist_state(trader)
    except Exception:
        pass


def resolve_seed(trader, st, fallback_key='rogue_seed_fallback'):
    """(seed_px, seed_source) for the A1-mode engine when NO chain target exists.
    Resolution happens AT SEED TIME from the CURRENT switch state:

      1. a fallback seed already LATCHED today -> reuse it (a mid-day toggle must
         never double-seed or orphan the day's chain);
      2. anchor engine ON and A1 not given up -> the REAL A1 anchor read, per tick,
         exactly as master (byte-identical when non_oco_enabled=True);
      3. else cfg.<fallback_key>: 'a1_time_snapshot' (DEFAULT -- the price
         captured at A1's scheduled time) or 'market_open' (first tick of the
         broker day).

    fallback_key selects WHICH config knob names the fallback mode so a second
    engine (Fetcher) can REUSE this resolver verbatim with its own
    'fetcher_seed_fallback' knob -- the logic is shared, never forked. Default
    'rogue_seed_fallback' keeps every existing caller byte-identical.

    Returns (None, source) while the chosen source has no price yet -- the engine
    WAITS, exactly like master waits for A1 to place. Guarded reads only."""
    if st.get('seed_px') is not None:
        return float(st['seed_px']), st.get('seed_source')
    if _anchors_engine_on(trader) and not _a1_gave_up(trader):
        return _a1_anchor_price(trader), SEED_A1_ANCHOR
    # already seeded TODAY via the real A1 read, and the switch was toggled off
    # (or A1 later gave up) mid-day -> LATCH the recorded A1 seed instead of
    # re-seeding via the fallback: one seed per day, never a double-seed.
    if (st.get('seed_source') == SEED_A1_ANCHOR
            and st.get('seed_recorded_px') is not None):
        st['seed_px'] = float(st['seed_recorded_px'])
        return st['seed_px'], SEED_A1_ANCHOR
    mode = str(getattr(trader.cfg, fallback_key, 'a1_time_snapshot')).lower()
    if mode == 'market_open':
        return st.get('day_open_px'), SEED_MARKET_OPEN
    return st.get('a1_snap_px'), SEED_A1_TIME_SNAPSHOT


def _record_seed(trader, st, seed_px, seed_source):
    """Log 'ROGUE SEED via <SOURCE> @ price' ONCE per (source, price) episode and
    stamp st['seed_source'] so every subsequent ledger/pattern row carries it.
    A FALLBACK seed additionally LATCHES (st['seed_px']): once seeded today,
    toggling the anchor engine does nothing to the seed/chain. The A1_ANCHOR read
    is deliberately NOT latched -- it stays the live per-tick read master does.
    Guarded; never raises onto the driver."""
    try:
        if seed_px is None:
            return
        key = f"{seed_source}:{round(float(seed_px), 2)}"
        st['seed_source'] = seed_source
        # remember the last recorded price: the A1_ANCHOR read stays live per tick
        # (master), but if the switch is later toggled off mid-day, resolve_seed
        # latches THIS price rather than re-seeding via the fallback.
        st['seed_recorded_px'] = round(float(seed_px), 2)
        # A1_BREAK latches like the fallback sources: once the $10-break anchor is planted it
        # is the day's fixed seed (resolve_seed reuses it; the opposite side never re-seeds).
        if seed_source in (SEED_A1_TIME_SNAPSHOT, SEED_MARKET_OPEN, seed_budget.SEED_A1_BREAK):
            st['seed_px'] = round(float(seed_px), 2)
        if st.get('_seed_log_key') == key:
            return
        st['_seed_log_key'] = key
        msg = (f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} ROGUE SEED via {seed_source} @ "
               f"{round(float(seed_px), 2)}")
        log.info(msg)
        try:
            trader.tele.info(msg)
        except Exception:
            pass
        _persist_state(trader)
    except Exception:
        pass


def _plant_fresh_anchor_a1(trader, st, price):
    """RULE 2: the exhaustion gap has elapsed -> plant a FRESH anchor at the current tick (NOT
    the stale close that fed the chop) with a fresh trade budget. Mirrors manual_seed's core
    (chain target = the tick; chain meta cleared so the first entry off it is unconstrained).
    Guarded; never raises onto the driver."""
    try:
        px = round(float(price), 2)
        st['a1_last_close'] = px
        st['a1_reverted'] = False
        st['chain_time'] = None
        st['chain_anchor'] = None
        seed_budget.budget_reset(st.setdefault('budget', seed_budget.new_budget()))
        msg = (f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} FRESH ANCHOR @ {px:.2f} — budget reset "
               f"(hunting again after the exhaustion gap)")
        log.warning(msg)
        try:
            trader.tele.info(msg)
        except Exception:
            pass
        _persist_state(trader)
    except Exception:
        pass


def _plant_runaway_reanchor(trader, st, old_anchor, direction):
    """2026-07-16 RUNAWAY: plant a FRESH chained continuation anchor at the current SETTLED
    tick (the same sane/held discipline A1's tick fallback uses -- passes max_tick_jump +
    hold_ticks via seed_tick_price). This is a CHAINED anchor with the chain meta CLEARED
    (chain_anchor/chain_time = None), exactly like _plant_fresh_anchor_a1: the driver's
    `chained` test is therefore False, so Gate 2's chain cooldown is not applied -- correct,
    because NO close preceded this re-anchor. The CONTINUATION-only direction lock lives
    downstream (runaway_entry_decision). Increments the 3/day counter and records the price
    for the spacing guard. Guarded; never raises onto the driver."""
    try:
        px = seed_tick_price(trader)
        if px is None:
            return False
        px = round(float(px), 2)
        st['a1_last_close'] = px
        st['a1_reverted'] = False
        st['chain_time'] = None
        st['chain_anchor'] = None
        st['runaway_active'] = True
        st['runaway_dir'] = direction
        st['runaway_anchor_px'] = px
        st['runaway_count'] = int(st.get('runaway_count', 0)) + 1
        st['runaway_last_px'] = px
        try:
            moved = abs(px - float(old_anchor))
        except (TypeError, ValueError):
            moved = 0.0
        confirm = float(getattr(trader.cfg, 'rogue_runaway_confirm', 8.0))
        msg = (f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} ROGUE RUNAWAY REANCHOR @ {px:.2f} "
               f"(moved {moved:.2f} off old anchor {float(old_anchor):.2f}) "
               f"dir={direction} #{st['runaway_count']}/3 — continuation ${confirm:g} only")
        log.warning(msg)
        try:
            trader.tele.info(msg)
        except Exception:
            pass
        _persist_state(trader)
        return True
    except Exception:
        return False


def _budget_exhausted_alert(trader, st, b, gap_sec):
    """RULE 2: the anchor's trade budget is spent without an earned extension -> loud one-time
    log + Discord (n trades, last two not both wins, gap length). Fires ONCE per exhaustion
    episode (the gap latch guarantees single fire). Guarded; never raises."""
    try:
        n = int(b.get('trades', 0))
        last2 = seed_budget.wl_tag(b, int(getattr(trader.cfg, 'engine_extend_requires_wins', 2) or 2))
        mins = float(gap_sec) / 60.0
        msg = (f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} ANCHOR EXHAUSTED ({n} trades, last two "
               f"[{last2}] not both wins) — {mins:g}m gap, fresh anchor after")
        log.warning(msg)
        try:
            trader.tele.warn(msg)
        except Exception:
            pass
        _persist_state(trader)
    except Exception:
        pass


def _drive_a1(trader, st, allow_new_entries=True):
    """Fix 4 A1-ANCHORED driver (impure; runs ONLY when rogue_a1_anchor_mode is ON). Seeds
    from A1 (read-only) / chains to the last closed Rogue level, enters on a $10 move,
    rides continuation on the existing trail, and on a confirmed reversal closes the wrong-
    way leg + recovers in the new direction (capped). Brake = the live daily loss stop.
    The $30 soft lock is banked but never halts. ROGUE-only (magic 20260626). Guarded.

    Fix 3 (E-15): with allow_new_entries=False (the post-EOD trail-only call) an EXISTING
    open position still rides the adaptive trail and still books its broker close, but no
    reversal-recovery leg and no fresh $10-move entry are taken -- NEW entries are hard-
    blocked after EOD (and, at the call site, on kill-locked days)."""
    # A1-mode keys live alongside the shared anchor/open keys.
    st.setdefault('a1_last_close', None)   # last CLOSED Rogue level (chain target)
    st.setdefault('a1_reverted', False)    # next entry is a capped recovery leg
    price = _mid(trader)
    if price is None:
        return
    # v3.6.0 SEED INDEPENDENCE: passively capture the fallback seed candidates
    # (first-tick-of-day + A1-scheduled-time snapshot) every tick, whether or not
    # they end up used -- resolve_seed decides AT SEED TIME. No orders placed.
    _capture_seed_snapshots(trader, st, price)
    # book a broker-side close FIRST (frees the slot + records the level for chaining).
    o_before = st.get('open')
    if o_before is not None:
        # REVERSAL: the open leg is known wrong -> close it (broker SL likely already did)
        # and chain-recover in the NEW direction next. A reversal-recovery is a NEW entry,
        # so post-EOD (allow_new_entries=False) we skip it and let the broker SL / trail
        # handle the position instead.
        if (allow_new_entries
                and a1_reversal_confirmed(o_before.get('entry'), o_before.get('side'),
                                          price, trader.cfg)):
            tk = o_before.get('ticket')
            try:
                if tk is not None:
                    trader.adapter.close_position(int(tk), dry_run=trader.paper)  # ROGUE only
            except Exception:
                pass
            st['a1_last_close'] = float(o_before.get('entry'))
            st['a1_reverted'] = True
            # P3 (E-17): a reversal-recovery anchor is NOT a chained anchor -- the recovery
            # is time-critical by design, so the chain cooldown must not delay it (the
            # chase cap still applies inside a1_entry_decision). Clear the chain meta so
            # the gate can't mistake the entry-based anchor for a re-anchor.
            st['chain_time'] = None
            st['chain_anchor'] = None
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
    if not allow_new_entries:
        return                              # Fix 3: no NEW entries post-EOD / kill-locked
    # v3.6.0 SEED INDEPENDENCE: with no chain target, the seed source is resolved
    # AT SEED TIME from the current switch state -- the real A1 anchor when the
    # anchor engine placed (master, byte-identical), else the configured fallback
    # (A1-time snapshot / market open). Every seed logs its source once and stamps
    # st['seed_source'] for the ledger/pattern-log rows.
    seed_px, seed_source = resolve_seed(trader, st)
    # RULE 1 ($10-break seed anchor): A1 is only the seed REFERENCE. The INITIAL seed is
    # withheld until price first travels seed_break_dollars from A1 in either direction; the
    # $-point (A1 +/- break) then latches as the day's anchor (seed_source=A1_BREAK). Chain
    # re-anchors (a1_last_close set) and a manual/latched seed bypass it; seed_break_dollars
    # <= 0 plants at A1 directly (today's behavior). No break yet -> no anchor, no trade.
    brk = float(getattr(trader.cfg, 'seed_break_dollars', 0.0) or 0.0)
    if (brk > 0.0 and st.get('a1_last_close') is None and st.get('seed_px') is None
            and seed_source != SEED_MANUAL):
        b_anchor, b_latched = seed_budget.break_seed_anchor(st, seed_px, price, brk)
        if b_anchor is None:
            # NO_ANCHOR: the $10 break has not latched yet. anchor= the A1 ref so the line
            # shows the running travel (e.g. move=-9.67 is the 2026-07-16 upside near-miss).
            _ptrace_reject(trader, st, 'NO_ANCHOR', price, seed_px)
            return                              # price has not travelled $10 from A1 yet
        seed_px, seed_source = b_anchor, seed_budget.SEED_A1_BREAK
        if b_latched:
            _bmsg = (f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} SEED via {seed_budget.SEED_A1_BREAK} "
                     f"@ {b_anchor:.2f} (A1 {float(st.get('break_a1_ref')):.2f}, "
                     f"{'+' if b_anchor >= float(st.get('break_a1_ref')) else '-'}${brk:g} break)")
            log.info(_bmsg)
            try:
                trader.tele.info(_bmsg)
            except Exception:
                pass
    anchor = a1_seed_anchor(st.get('a1_last_close'), seed_px)
    if anchor is None:
        _ptrace_reject(trader, st, 'NO_ANCHOR', price, seed_px)
        return
    if st.get('a1_last_close') is None:
        _record_seed(trader, st, seed_px, seed_source)
    # P3 (E-17): this anchor is CHAINED iff it is the re-anchor detect_close planted
    # after a close (chain_anchor matches the active chain target). The A1 morning seed,
    # a manual rogueseed, and a reversal-recovery anchor all leave chain_anchor unset ->
    # not chained -> Gate 2 never touches them (first trade of the day is unaffected).
    chained = (st.get('chain_anchor') is not None
               and st.get('a1_last_close') is not None
               and abs(float(st['chain_anchor']) - float(anchor)) < 1e-9)
    if chained:
        # Track the running per-direction displacement off the re-anchor since planting
        # ("at some point since planting" -- a spike that later pulls back still counts).
        d = float(price) - float(anchor)
        st['chain_disp_up'] = max(float(st.get('chain_disp_up', 0.0)), d)
        st['chain_disp_dn'] = max(float(st.get('chain_disp_dn', 0.0)), -d)
    ok, _why = can_enter(st['gov'], trader.cfg)   # BRAKE: daily loss stop / cap / fail-pause
    if not ok:
        _ptrace_reject(trader, st, 'GOVERNOR', price, anchor)
        return
    # RULE 2 (earned trade budget): SUBORDINATE to the loss stop above (can_enter ranks first;
    # a loss-stopped day is terminal -- no fresh anchor resurrects it). During the exhaustion
    # gap: manage-only (the open leg was already handled upstream). Gap elapsed: plant a FRESH
    # anchor at the current tick with a fresh budget. Budget spent without an earned extension:
    # latch the gap + one-time alert. Disabled (base<=0) -> byte-neutral.
    if not seed_budget.budget_off(trader.cfg):
        b = st.setdefault('budget', seed_budget.new_budget())
        _now = _epoch()
        if seed_budget.budget_in_gap(b, _now):
            _ptrace_reject(trader, st, 'BUDGET', price, anchor)
            return
        if seed_budget.budget_gap_ready(b, _now):
            _plant_fresh_anchor_a1(trader, st, price)
            return
        ok_b, _why_b = seed_budget.budget_can_trade(b, trader.cfg)
        if not ok_b:
            _gap = seed_budget.budget_start_gap(b, trader.cfg, _now)
            _budget_exhausted_alert(trader, st, b, _gap)
            _ptrace_reject(trader, st, 'BUDGET', price, anchor)
            return
    # 2026-07-16 RUNAWAY: on a runaway re-anchor the entry is CONTINUATION-only off the new
    # anchor at the smaller rogue_runaway_confirm; otherwise the normal A1 entry decision.
    on_runaway = (bool(st.get('runaway_active'))
                  and st.get('a1_last_close') is not None
                  and st.get('runaway_anchor_px') is not None
                  and abs(float(st['a1_last_close']) - float(st['runaway_anchor_px'])) < 1e-9)
    if on_runaway:
        enter, side, epx, sl = runaway_entry_decision(
            anchor, price, st.get('runaway_dir'), trader.cfg)
    else:
        enter, side, epx, sl = a1_entry_decision(anchor, price, trader.cfg)
    if not enter:
        # 2026-07-16 RUNAWAY RE-ANCHOR (band-overshoot recovery): if price has RUN past the
        # active anchor with no position and no close-owed cooldown (the seed anchor, or an
        # existing runaway chain), plant a fresh continuation anchor at the settled tick and
        # hunt from there next tick. Gated by can_enter/budget above (already passed) so a
        # loss-stopped / capped day never re-anchors; the plant itself takes NO slot -- the
        # ENTRY off it consumes the governor slot as usual.
        reanchor_eligible = (st.get('a1_last_close') is None or bool(st.get('runaway_active')))
        if reanchor_eligible:
            _trig, _rdir = runaway_should_reanchor(st, anchor, price, trader.cfg)
            if _trig and _plant_runaway_reanchor(trader, st, anchor, _rdir):
                return
        # P3 GATE 1 (E-17): if this no-enter is a CHASE reject (|move| > cap), say so --
        # once per episode. NO slot is consumed (slots are only consumed on a real fill
        # in _mark_rogue_open) and NO latch is set: the anchor stays planted and the gate
        # re-evaluates per tick, so a pullback inside the band allows entry again.
        _log_chase_reject(trader, st, anchor, price)
        # PTRACE: CHASE_CAP when past the cap, else BAND_NOT_HELD (move below confirm / no
        # tick landed inside the entry band -- the 2026-07-16 band-overshoot signature).
        _rej, _mv = chase_rejected(anchor, price, trader.cfg)
        _ptrace_reject(trader, st, 'CHASE_CAP' if _rej else 'BAND_NOT_HELD', price, anchor)
        return
    # P3 GATE 2 (E-17): a CHAINED anchor additionally needs the cooldown elapsed AND the
    # $6 fresh displacement in the entry direction. The reversal-recovery leg is exempt
    # (never chained: the reversal path cleared the chain meta above); rejection consumes
    # no slot and the gate re-evaluates per tick.
    if chained:
        disp = float(st.get('chain_disp_up', 0.0)) if side == 'BUY' \
            else float(st.get('chain_disp_dn', 0.0))
        allowed, why2, remaining = chain_entry_allowed(
            st.get('chain_time'), _epoch(), disp, trader.cfg)
        if not allowed:
            _log_chain_block(trader, st, why2, remaining)
            _ptrace_reject(trader, st, 'COOLDOWN', price, anchor)
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
    # P3 (E-17): the chained anchor is consumed by this entry -- clear the chain meta so
    # a later, unrelated anchor can never inherit a stale cooldown/displacement record.
    st['chain_time'] = None
    st['chain_anchor'] = None


def drive(trader, allow_new_entries=True):
    """The per-tick Rogue driver. Runs ONLY when should_run (rogue_enabled AND not
    funded) and rogue_daywatch -- otherwise an immediate no-op (no watch/anchor/entry).
    Pipeline: watch recent M5 -> detect monster -> drop anchor -> (governor + cap + gate)
    -> early entry on confirmation -> manage the open winner on the adaptive trail. All
    decisions come from the PURE cores above; this only does the IO + telemetry +
    placement (ROGUE-tagged via ROGUE_MAGIC). Fully guarded -- never raises onto _tick.

    Fix 3 (E-15): the live loop now calls drive() only AFTER the kill-switch and EOD
    gates, so NEW entries are hard-blocked on kill-locked days and post-EOD. `allow_new_
    entries=False` (the post-EOD trail-only call, when rogue_flatten_at_eod is False) lets
    an EXISTING open Rogue position keep trailing / booking its close while refusing any
    new entry or reversal-recovery leg."""
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
        # Rogue v2 STOP MODE: resting pending-stop engine (OCO ±rogue_trigger + chain).
        # Gated on the flag so the legacy band engine below stays byte-identical when
        # OFF. In stop mode the seed-break / confirm-band / runaway / hold_ticks paths
        # are inert (never reached) -- the stop engine owns entries end to end.
        if bool(getattr(trader.cfg, 'rogue_stop_mode', False)):
            import rogue_stop as _rs
            _rs.drive_stop(trader, st, allow_new_entries=allow_new_entries)
            return
        # Fix 4: A1-ANCHORED REDESIGN (flag-gated, DEFAULT OFF). ON -> the new engine seeds
        # from the day's A1 anchor (read-only) / chains to the last closed Rogue level and
        # skips monster-detection. OFF (default) -> fall through to the legacy monster
        # pipeline below, byte-identical.
        if bool(getattr(trader.cfg, 'rogue_a1_anchor_mode', False)):
            # Keep the default (entry-taking) call 2-arg so a test/monkeypatch that binds a
            # 2-arg _drive_a1 stays valid; only the trail-only path passes the kwarg.
            if allow_new_entries:
                _drive_a1(trader, st)
            else:
                _drive_a1(trader, st, allow_new_entries=False)
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
        # 2. ENTRY (gated): only on a NEW slot AND the setup gate (a live anchor). Fix 3:
        # NEW entries are refused when allow_new_entries is False (post-EOD trail-only).
        price = _mid(trader)
        if (allow_new_entries and st.get('open') is None
                and st.get('anchor') is not None and price is not None):
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


def _epoch():
    """Wall clock in epoch seconds for the P3 chain cooldown. A separate seam (not inline
    time.time()) so tests can pin/patch it. Guarded; 0.0 on any error."""
    try:
        import time as _t
        return float(_t.time())
    except Exception:
        return 0.0


def _ptrace_reject(trader, st, reason, price, anchor):
    """DIAGNOSTIC (2026-07-16): emit ONE structured PTRACE line each time Rogue evaluates a
    tick and does NOT take an entry, tagged with the blocking reason. This exists because
    the 2026-07-16 zero-entry day was AMBIGUOUS from the logs -- a fast crash blew through
    the $8-wide entry band and no single line said "why no fill". reason in
    (NO_ANCHOR / BAND_NOT_HELD / CHASE_CAP / COOLDOWN / BUDGET / GOVERNOR). Throttled to ONE
    line per reason per minute (a persistent block can't spam). PURE TELEMETRY -- never
    changes a trading decision; log-only (no Discord). Guarded; never raises onto the
    driver."""
    try:
        now = _epoch()
        seen = st.setdefault('_ptrace_log', {})
        last = seen.get(reason)
        if last is not None and (now - float(last)) < 60.0:
            return
        seen[reason] = now
        try:
            a = float(anchor)
            atxt = f"{a:.2f}"
            mtxt = f"{float(price) - a:+.2f}"
        except (TypeError, ValueError):
            atxt, mtxt = "none", "n/a"
        try:
            ptxt = f"{float(price):.2f}"
        except (TypeError, ValueError):
            ptxt = "none"
        log.info(f"{ROGUE_ALERT_PREFIX} PTRACE reject={reason} px={ptxt} "
                 f"anchor={atxt} move={mtxt}")
    except Exception:
        pass


def _log_chase_reject(trader, st, anchor, price):
    """P3 GATE 1 (E-17) telemetry: log a CHASE-REJECT once per episode -- an episode ends
    when the tick is no longer beyond the cap (pullback inside the band / below confirm),
    which re-arms the log. The GATE itself has no latch (a1_entry_decision re-evaluates
    per tick); only this LOG is throttled so a persistent extension can't spam. Guarded."""
    try:
        rejected, move = chase_rejected(anchor, price, trader.cfg)
        if not rejected:
            if st.get('_chase_log_key') is not None:
                st['_chase_log_key'] = None      # back inside the band -> re-arm the log
            return
        key = f"{round(float(anchor), 2)}:{'UP' if move > 0 else 'DN'}"
        if st.get('_chase_log_key') == key:
            return                               # same episode -> already logged
        st['_chase_log_key'] = key
        cap = float(getattr(trader.cfg, 'rogue_chase_cap_dollars', 0.0) or 0.0)
        msg = (f"{ROGUE_ALERT_PREFIX} CHASE-REJECT move ${abs(move):.2f} > cap ${cap:.0f} "
               f"(anchor {float(anchor):.2f})")
        log.info(msg)
        trader.tele.info(msg)
    except Exception:
        pass


def _log_chain_block(trader, st, reason, remaining):
    """P3 GATE 2 (E-17) telemetry: log a chain-gate block ONCE per (reason, re-anchor) --
    the gate itself re-evaluates every tick; only the log is throttled. Guarded."""
    try:
        key = f"{reason}:{st.get('chain_time')}"
        if st.get('_chain_log_key') == key:
            return
        st['_chain_log_key'] = key
        if reason == 'cooldown':
            msg = (f"{ROGUE_ALERT_PREFIX} CHAIN-COOLDOWN {max(0.0, float(remaining)):.0f}s "
                   f"remaining (re-anchor {float(st.get('chain_anchor') or 0):.2f})")
        else:
            need = float(getattr(trader.cfg, 'rogue_chain_min_displacement', 0.0) or 0.0)
            msg = (f"{ROGUE_ALERT_PREFIX} CHAIN-DISPLACEMENT < ${need:.0f} fresh off "
                   f"re-anchor {float(st.get('chain_anchor') or 0):.2f} -- waiting for a "
                   f"NEW move, not the closed one's tail")
        log.info(msg)
        trader.tele.info(msg)
    except Exception:
        pass


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
                         entry_price=(round(float(epx), 2) if decision == _pl.ENTER else ''),
                         seed_source=str(st.get('seed_source') or ''))
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


def _rogue_recompute_sl(trader, side, sl):
    """Fix 1 (E-13): on a 10016 INVALID_STOPS resend, push the init SL just BEYOND the
    broker's minimum stops_level from the CURRENT market so the retry is accepted --
    WITHOUT ever touching lot size. READ-ONLY on the market; returns the input SL
    unchanged on any error. This only widens the stop distance; the position is untouched."""
    try:
        mt5 = trader.adapter.mt5
        info = mt5.symbol_info(trader.cfg.symbol)
        tk = mt5.symbol_info_tick(trader.cfg.symbol)
        point = float(getattr(info, 'point', 0.01)) or 0.01
        stops_pts = float(getattr(info, 'trade_stops_level', 0) or 0)
        min_dist = stops_pts * point
        if min_dist <= 0:
            return sl
        px = float(tk.ask) if side == 'BUY' else float(tk.bid)
        pad = min_dist + 2.0 * point
        return round(px - pad, 2) if side == 'BUY' else round(px + pad, 2)
    except Exception:
        return sl


def _mark_rogue_open(trader, st, entry_px, sl, tk, rc):
    """Set st['open'] on a REAL fill + consume ONE governor slot + fire the enter side
    effects. Called ONLY once a placement is confirmed (rc==10009 with a ticket, or the
    paper shadow path). Never called on a failed live placement (the brick fix)."""
    side = st['leg_dir']
    st['open'] = {'ticket': tk, 'side': side, 'entry': entry_px, 'sl': sl,
                  'peak': entry_px, 'magic': ROGUE_MAGIC, 'leg_type': ROGUE_LEG_TYPE}
    # 2026-07-16: this entry consumed the runaway continuation anchor -- clear the active
    # markers so a later post-close chained anchor (cooldown OWED) is never mistaken for a
    # runaway continuation. runaway_count / runaway_last_px persist (the daily loop + spacing
    # guards span the whole day).
    st['runaway_active'] = False
    st['runaway_dir'] = None
    st['runaway_anchor_px'] = None
    record_entry(st['gov'])
    # RULE 2: this entry consumes one of the anchor session's budget attempts.
    seed_budget.budget_record_entry(st.setdefault('budget', seed_budget.new_budget()))
    try:
        import boost_metrics as _bm
        import pandas as _pd
        _bm.append_ledger(trader, {'ts': _pd.Timestamp.now(tz='UTC').isoformat(),
                                   'anchor': ROGUE_LABEL, 'kind': 'ROGUE',
                                   'event': 'enter', 'arm_px': st.get('anchor'),
                                   'entry_px': entry_px,
                                   'seed_source': st.get('seed_source')})
    except Exception:
        pass
    try:
        trader.tele.info(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} ENTER {side} @ {entry_px} "
                         f"SL {sl} (slot {st['gov']['reanchor_count']}/"
                         f"{int(getattr(trader.cfg, 'rogue_max_reentries_per_day', 10))}) rc={rc}")
    except Exception:
        pass
    try:  # decision-grade review line (one per rogue detection-mode fill)
        import review_log as _rv
        _rv.get_review_logger(getattr(trader, 'cfg', None)).fill(
            'ROGUE', side, float(getattr(trader.cfg, 'rogue_lot', getattr(trader.cfg, 'lot_size', 0.0)) or 0.0),
            float(entry_px), tag=ROGUE_LABEL)
    except Exception:
        pass
    _persist_state(trader)


def _place_rogue_entry(trader, st, entry_px, sl):
    """Place ONE ROGUE-tagged market entry (own magic). LIVE placements go through the
    SHARED place_with_retry wrapper (Fix 1 / E-13): bounded retry on requote/price/stops,
    abort+alert on volume/no-money/closed/disabled -- the lot is NEVER resized.

    BRICK FIX (E-13): st['open'] is set (and ONE governor slot consumed) ONLY on a real
    fill -- rc==10009 with a ticket. On final failure the state stays clean (no phantom
    open), NO slot is consumed, an abort alert has already fired, and the engine stays
    alive for the next signal -- it does not brick."""
    side = st['leg_dir']
    tp = round(entry_px + (200.0 if side == 'BUY' else -200.0), 2)  # far TP; the trail governs
    if trader.paper:
        # PAPER / dry-run: no real broker + no brick risk -- keep the prior shadow-entry
        # behavior (single send; selftest stub adapters return a success object + ticket).
        try:
            res = trader.adapter.place_market_order(
                trader.cfg.symbol, side, trader.cfg.lot_size, sl=sl, tp=tp,
                magic=ROGUE_MAGIC, comment=f"AUR_ROGUE_{side[0]}", dry_run=True)
            rc = getattr(res, 'retcode', None) if res is not None else None
            tk = getattr(res, 'order', None) or getattr(res, 'deal', None)
            _mark_rogue_open(trader, st, entry_px, sl, tk, rc)
        except Exception as e:
            log.warning(f"{ROGUE_ALERT_PREFIX} place entry (paper) non-fatal: {e!r}")
        return
    # LIVE: bounded-retry via the shared wrapper. sender re-reads the tick inside
    # place_market_order every attempt; on 10016 it recomputes the SL vs stops_level.
    def _send(attempt, recompute_stops):
        _sl = _rogue_recompute_sl(trader, side, sl) if recompute_stops else sl
        return trader.adapter.place_market_order(
            trader.cfg.symbol, side, trader.cfg.lot_size, sl=_sl, tp=tp,
            magic=ROGUE_MAGIC, comment=f"AUR_ROGUE_{side[0]}", dry_run=False)
    describe = {'label': f'ROGUE {side}', 'side': side, 'symbol': trader.cfg.symbol,
                'lot': trader.cfg.lot_size, 'price': 'mkt', 'sl': sl, 'tp': tp,
                'magic': ROGUE_MAGIC}
    try:
        res = trader.adapter.place_with_retry(
            _send, describe=describe, tele=getattr(trader, 'tele', None))
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} place entry non-fatal: {e!r}")
        res = None
    rc = getattr(res, 'retcode', None) if res is not None else None
    tk = (getattr(res, 'order', None) or getattr(res, 'deal', None)) if res is not None else None
    if rc == 10009 and tk:
        _mark_rogue_open(trader, st, entry_px, sl, tk, rc)
    else:
        # FINAL FAILURE: no phantom open, NO governor slot consumed, engine stays alive.
        st['open'] = None
        try:
            trader.tele.warn(
                f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} ENTER {side} @ {entry_px} FAILED "
                f"rc={rc} — no position, slot preserved "
                f"({st['gov']['reanchor_count']}/"
                f"{int(getattr(trader.cfg, 'rogue_max_reentries_per_day', 10))}); "
                f"engine live for next signal")
        except Exception:
            pass


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


def _resolve_close_pnl(trader, ticket, tries=3, delay=1.0):
    """Fix 2 (E-14): resolve a CLOSED Rogue position's realized $ with a BOUNDED retry --
    the close deal often lands in history a beat after the position leaves the book. Tries
    _rogue_close_pnl up to `tries` times, `delay`s apart, and returns the first non-None
    value; returns None only if it is STILL unavailable after every try. READ-ONLY."""
    import time as _time
    for i in range(max(1, int(tries))):
        pnl = _rogue_close_pnl(trader, ticket)
        if pnl is not None:
            return pnl
        if i < int(tries) - 1:
            try:
                _time.sleep(float(delay))
            except Exception:
                pass
    return None


def _rogue_close_price(trader, ticket):
    """Exit PRICE of a CLOSED Rogue position from its broker close deal (entry==1). Used to
    re-anchor the A1 chain at the level Rogue actually got out. Returns None if the close deal
    isn't in history yet (the caller then falls back to the last stop). READ-ONLY."""
    try:
        deals = trader.adapter.mt5.history_deals_get(position=int(ticket)) or []
        cd = next((d for d in deals if getattr(d, 'entry', None) == 1), None)
        if cd is None:
            return None
        return float(cd.price)
    except Exception:
        return None


def detect_close(trader, st):
    """E-2/E-3: detect a BROKER-side close of the open Rogue position and book it ONCE --
    update the day-governor via record_close (day_pnl / consec_fails / loss_stopped /
    fail_paused) and clear st['open'] so Rogue can re-enter the same day AND the patternlog
    observe() close branch runs. Returns True if a close was booked.

    E-3 CHAIN: the moment a close is booked, re-anchor the A1 redesign at the EXIT price
    (st['a1_last_close']) so Rogue keeps hunting the next $10 move BOTH directions after ANY
    close (SL / TP / trailing) instead of going dormant after one. A reversal-recovery leg
    (a1_reverted) keeps its own entry-based anchor; the legacy monster path (a1 mode OFF) is
    untouched. Gated on the existing brakes (can_enter) at the next-entry site.

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
    # Fix 2 (E-14): the close-deal P&L can lag the position leaving the book. Retry the
    # history fetch (3 tries, 1s apart) before booking so a real close is not mis-booked as
    # $0 (which the old code then counted as an init-SL fail, tripping the fail-pause brake).
    pnl = _resolve_close_pnl(trader, tk)
    unresolved = (pnl is None)
    if unresolved:
        pnl = 0.0
        # STILL None after retries: book $0 but do NOT increment consec_fails (was_fail=None
        # leaves the fail streak intact) -- an unresolvable P&L must not pause the engine.
        record_close(st['gov'], 0.0, None, trader.cfg)
    else:
        was_fail = float(pnl) <= 0.0     # a non-winning close = init-SL fake-out (winner resets)
        record_close(st['gov'], pnl, was_fail, trader.cfg)
        # RULE 2: append this close's outcome to the anchor session's trailing win/loss
        # window (a win = pnl > 0). Unresolved P&L (was_fail=None) leaves the window intact.
        seed_budget.budget_record_close(
            st.setdefault('budget', seed_budget.new_budget()), float(pnl) > 0.0)
    try:
        import boost_metrics as _bm
        import pandas as _pd
        _bm.append_ledger(trader, {'ts': _pd.Timestamp.now(tz='UTC').isoformat(),
                                   'anchor': ROGUE_LABEL, 'kind': 'ROGUE',
                                   'event': 'exit', 'entry_px': o.get('entry'),
                                   'exit_px': _rogue_close_price(trader, tk),
                                   'pnl_usd': round(float(pnl), 2),
                                   'seed_source': st.get('seed_source')})
    except Exception:
        pass
    st['open'] = None
    _persist_state(trader)               # Fix 5 (E-16): governors changed -> persist
    maybe_profit_lock_alert(trader, st)  # one-time PROFIT-LOCK alert if this close engaged it
    if unresolved:
        try:
            log.warning(f"{ROGUE_ALERT_PREFIX} WARN pnl-unresolved ticket #{tk}")
            trader.tele.warn(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} WARN pnl-unresolved "
                             f"ticket #{tk} — booked $0, fail-streak untouched")
        except Exception:
            pass
    # E-3 CHAIN re-anchor (A1 redesign only): plant the exit as the next chain target so the
    # engine re-anchors there and hunts the next $10 move both ways. A reversal recovery keeps
    # its own entry anchor (a1_reverted); legacy monster mode (flag OFF) is unaffected. Guarded.
    try:
        if bool(getattr(trader.cfg, 'rogue_a1_anchor_mode', False)) and not st.get('a1_reverted'):
            exit_px = _rogue_close_price(trader, tk)
            if exit_px is None:
                exit_px = o.get('sl')          # trailing/init stop that fired ~= the exit
            if exit_px is not None:
                st['a1_last_close'] = float(exit_px)
                # P3 (E-17) GATE 2: this re-anchor is a CHAINED anchor -- stamp when and
                # where it was planted and reset the per-direction displacement record.
                # The next entry off it must wait out rogue_chain_cooldown_sec AND show
                # rogue_chain_min_displacement of fresh movement (chain_entry_allowed).
                st['chain_time'] = _epoch()
                st['chain_anchor'] = float(exit_px)
                st['chain_disp_up'] = 0.0
                st['chain_disp_dn'] = 0.0
                _reanchor_msg = (f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} CHAIN re-anchor @ "
                                f"{float(exit_px):.2f} -> hunting $10 both dirs")
                # P4 (daily P&L report): mirror to aureon.log like the sibling
                # CHASE-REJECT/CHAIN-COOLDOWN/CHAIN-DISPLACEMENT lines already do
                # (rogue._log_chase_reject / _log_chain_block) -- this line was
                # Discord/Telegram-only before, so "chain re-anchors today" was
                # NOT recoverable from the log for a historical report. Logging
                # only; no decision changes.
                log.info(_reanchor_msg)
                trader.tele.info(_reanchor_msg)
    except Exception:
        pass
    try:
        g = st['gov']
        brake = ('LOSS-STOP' if g.get('loss_stopped')
                 else ('FAIL-PAUSE' if g.get('fail_paused') else 'live'))
        _close_msg = (
            f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} CLOSE {o.get('side')} #{tk} "
            f"P&L ${float(pnl):+.2f} | day ${float(g.get('day_pnl', 0.0)):+.2f} | "
            f"fails {int(g.get('consec_fails', 0))} | {brake}")
        # P4 (daily P&L report): same mirror -- the brake tag (LOSS-STOP/FAIL-
        # PAUSE/live) was Discord/Telegram-only, so "brake events today" was not
        # greppable from aureon.log. Logging only.
        log.info(_close_msg)
        trader.tele.info(_close_msg)
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


def force_close_open(trader, reason="flatten"):
    """Fix 3 (E-15): close an OPEN Rogue ticket (magic 20260626) UNCONDITIONALLY -- the
    kill-switch / manual-flatten path. Rogue rides its OWN magic, so the anchor flatten
    loop never touches it; this closes st['open']'s own ticket, books it via the governor,
    and clears st['open'] so no phantom open survives a kill. IGNORES rogue_flatten_at_eod
    (that flag governs the EOD *ride* decision, not a kill). ROGUE-ONLY (never an anchor
    20260522 ticket). Returns True if it closed one. Guarded; never raises."""
    try:
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
        _persist_state(trader)
        try:
            trader.tele.info(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} {reason} flatten -> closed "
                             f"#{tk} P&L ${float(pnl):+.2f}")
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} force_close_open non-fatal: {e!r}")
        return False


def cancel_pendings(trader, reason="flatten"):
    """v3.6.0 /rogue flatten confirm: cancel any PENDING order carrying ROGUE_MAGIC
    (20260626). Rogue currently places market entries only, so this is normally a
    no-op -- it exists so a scoped Rogue flatten provably leaves nothing resting at
    the broker. ROGUE-ONLY: an anchor (20260522) or warmup (9999998) pending is
    never touched. Returns the number cancelled. Guarded; never raises."""
    n = 0
    try:
        pendings = trader.adapter.mt5.orders_get(symbol=trader.cfg.symbol) or []
        for o in pendings:
            try:
                if int(getattr(o, 'magic', -1)) != ROGUE_MAGIC:
                    continue
                trader.adapter.cancel_order(int(o.ticket), dry_run=trader.paper)
                n += 1
            except Exception as e:
                log.warning(f"{ROGUE_ALERT_PREFIX} cancel pending "
                            f"{getattr(o, 'ticket', '?')} non-fatal: {e!r}")
        if n:
            log.info(f"{ROGUE_ALERT_PREFIX} cancelled {n} Rogue pending(s) ({reason})")
    except Exception as e:
        log.warning(f"{ROGUE_ALERT_PREFIX} cancel_pendings non-fatal: {e!r}")
    return n


# --- manual current-tick seed (mid-day restart: no A1 event to seed the A1-mode engine) --
def manual_seed_rails_blocked(trader, engine, has_open):
    """SHARED live-testing rails for /rogueseed + /fetchseed. Refuse a deliberate manual
    re-seed when: the engine has an OPEN ticket (never re-anchor under a live position),
    the runtime engine switch is OFF (/rogue|/fetcher off), the market is closed, or the
    kill switch is active. Returns (blocked, reason) -- reason is a short human string for
    the Discord refusal. EVERY check is GUARDED so a stub/old trader lacking the runtime
    dict / state / market probe reads NOT blocked (switches only ever REMOVE behavior, never
    invent it -- same philosophy as live_trader._engine_enabled). READ-ONLY; never raises."""
    if has_open:
        return True, 'open position — flatten it before re-seeding'
    try:
        eng = getattr(trader, 'engines', None)
        if isinstance(eng, dict) and not bool(eng.get(engine, True)):
            return True, f'{engine} engine switch is OFF (/{engine} on to re-enable)'
    except Exception:
        pass
    try:
        if (getattr(trader, 'state', {}) or {}).get('kill_switch_locked'):
            return True, 'kill switch active'
    except Exception:
        pass
    try:
        probe = getattr(trader, '_market_closed_now', None)
        if callable(probe) and probe():
            return True, 'market closed'
    except Exception:
        pass
    return False, 'ok'


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
        # RAILS: never re-anchor under a live position / with the engine switched off /
        # market closed / kill-switch active (deliberate live testing must be SAFE). Guarded
        # so an old stub trader (no runtime dict) is unaffected.
        st_now = getattr(trader, '_rogue', None) or {}
        blocked, rreason = manual_seed_rails_blocked(
            trader, 'rogue', bool(st_now.get('open')))
        if blocked:
            log.warning(f"{ROGUE_ALERT_PREFIX} MANUAL SEED refused (rail): {rreason}")
            try:
                trader.tele.warn(f"{ROGUE_ALERT_PREFIX} 🌱 manual seed refused — {rreason}")
            except Exception:
                pass
            return False, 'rail', None
        if price is None:
            log.warning(f"{ROGUE_ALERT_PREFIX} MANUAL SEED refused (no_tick): no sane tick")
            try:
                trader.tele.warn(f"{ROGUE_ALERT_PREFIX} 🌱 manual seed refused — "
                                 f"no sane settled tick (stale/garbage feed)")
            except Exception:
                pass
            return False, 'no_tick', None
        # DAILY-STOP interaction (2026-07-08): a manual reseed is the SOFT override for the
        # PROFIT lock, but the HARD loss stop is NEVER overridable -> refuse while it is
        # active. Read the LIVE gov (before any re-init); a same-day reseed reuses this gov.
        gov = (st_now.get('gov') or {}) if isinstance(st_now, dict) else {}
        loss_stop = float(getattr(trader.cfg, 'rogue_daily_loss_stop', 0.0))
        if gov.get('loss_stopped') or (loss_stop < 0.0
                                       and float(gov.get('day_pnl', 0.0)) <= loss_stop):
            log.warning(f"{ROGUE_ALERT_PREFIX} MANUAL SEED refused (loss_stop): "
                        f"daily loss stop active (not overridable)")
            try:
                trader.tele.warn(f"{ROGUE_ALERT_PREFIX} 🌱 manual seed refused — daily loss "
                                 f"stop active (not overridable)")
            except Exception:
                pass
            return False, 'loss_stop', None
        # RULE 2: refuse a manual reseed while the exhaustion gap is still running -- the gap
        # is a deliberate cool-off; a manual seed must wait it out (subordinate, like the loss
        # stop). Read the LIVE budget from the pre-init state.
        b_now = (st_now.get('budget') or {}) if isinstance(st_now, dict) else {}
        if seed_budget.budget_in_gap(b_now, _epoch()):
            log.warning(f"{ROGUE_ALERT_PREFIX} MANUAL SEED refused (anchor_gap): "
                        f"exhaustion gap still running")
            try:
                trader.tele.warn(f"{ROGUE_ALERT_PREFIX} 🌱 manual seed refused — anchor "
                                 f"exhaustion gap still running (wait it out)")
            except Exception:
                pass
            return False, 'anchor_gap', None
        overrode = False
        profit_stop = float(getattr(trader.cfg, 'rogue_daily_profit_stop', 0.0))
        if (profit_stop > 0.0 and not gov.get('profit_override')
                and (gov.get('profit_locked')
                     or float(gov.get('day_pnl', 0.0)) >= profit_stop)):
            gov['profit_override'] = True   # clears the lock for the REST of the broker day
            overrode = True
        price = round(float(price), 2)
        # ensure the per-day Rogue state exists (mirrors drive()'s init on a fresh restart).
        # A SAME-day re-seed reuses the existing state -> the day governors (entries count,
        # day_pnl, fail streak) keep counting: a manual seed is a NEW ANCHOR, not a new day.
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
        # v3.6.0: a manual seed is its own provenance -- ledger/pattern rows carry it.
        st['seed_source'] = SEED_MANUAL
        # P3 (E-17): a manual seed is NOT a chained anchor -- no cooldown/displacement
        # gate on the first entry off it (same exemption as the A1 morning seed).
        st['chain_time'] = None
        st['chain_anchor'] = None
        # RULE 1/2: a manual seed OVERRIDES the $10-break (plants at the tick directly, above)
        # and starts a FRESH anchor session -- reset the trade budget (new anchor, not new day).
        seed_budget.budget_reset(st.setdefault('budget', seed_budget.new_budget()))
        _persist_state(trader)             # Fix 5 (E-16): seed changed -> persist
        confirm = float(getattr(trader.cfg, 'rogue_entry_confirm_redesign', 10.0))
        if overrode:
            omsg = (f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} PROFIT STOP OVERRIDDEN BY MANUAL "
                    f"RESEED @ {price} — entries re-enabled for the day (no re-lock)")
            log.warning(omsg)
            try:
                trader.tele.warn(omsg)
            except Exception:
                pass
        log.info(f"{ROGUE_ALERT_PREFIX} ROGUE SEED via MANUAL @ {price} (current tick) -> "
                 f"hunting ${confirm:.0f} move both directions")
        try:
            trader.tele.info(f"{ROGUE_ALERT_PREFIX} {ROGUE_GLYPH} ROGUE SEED via MANUAL @ "
                             f"{price} (current tick) — hunting ${confirm:.0f} move both "
                             f"directions")
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
                with open(path, encoding='utf-8') as f:
                    cmds = _json.load(f) or []
            except Exception:
                cmds = []
        cmds.append({"cmd": "rogueseed"})
        with open(path, "w", encoding='utf-8') as f:
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
