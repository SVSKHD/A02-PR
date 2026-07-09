"""AUREON — per-engine DAILY STOPS for the ANCHORS engine + the (inert-by-default)
account-level lock.

Rogue and Fetcher carry their own governor stops inside their engine modules; this module
holds the ANCHORS-engine daily brakes (realized day P&L = state['daily_pnl'], magic
20260522, which already EXCLUDES Rogue/Fetcher) and the combined ACCOUNT-level lock.

PURE threshold cores (no IO); the LiveTrader binds thin methods that read state + cfg and
call these. Independent latches -- one engine locking/halting NEVER affects the others.
Everything resets at the broker day roll (live_trader._reset_if_new_day). The LOSS stop is
HARD (never overridable); the PROFIT lock is SOFT (cleared by /daylock; no same-day
re-lock). A 0 threshold disables that gate.
"""
from __future__ import annotations

import logging

log = logging.getLogger("AUREON")

ANCHORS_MAGIC = 20260522
ANCHORS_ALERT_PREFIX = "[ANCHORS]"
ANCHORS_GLYPH = "⚓"

# state keys (persisted via p1_state; reset at day roll)
K_ANCHORS_LOCKED = 'anchors_profit_locked'
K_ANCHORS_OVERRIDE = 'anchors_profit_override'
K_ANCHORS_ALERTED = 'anchors_profit_alerted'
K_ACCOUNT_LOCKED = 'account_profit_locked'
K_ACCOUNT_OVERRIDE = 'account_profit_override'
K_ACCOUNT_ALERTED = 'account_profit_alerted'
# daily 2% target (repurposed account lock) -- persisted via state.json, reset at day roll
K_TARGET_SECURED = 'account_target_secured'          # latched: post-A4 net >= min_target
K_TARGET_GIVEBACK_LOCKED = 'account_target_giveback_locked'  # latched: peak>=min then retreat
K_TARGET_OVERRIDE = 'account_target_override'         # /daylock off clears any target lock
K_TARGET_PEAK = 'account_target_peak_net'             # running peak of combined net (float)
_DAY_KEYS = (K_ANCHORS_LOCKED, K_ANCHORS_OVERRIDE, K_ANCHORS_ALERTED,
             K_ACCOUNT_LOCKED, K_ACCOUNT_OVERRIDE, K_ACCOUNT_ALERTED,
             K_TARGET_SECURED, K_TARGET_GIVEBACK_LOCKED, K_TARGET_OVERRIDE)


def reset_day_state(state):
    """Clear ALL anchors + account lock/override/alert flags at the broker day roll --
    no carryover of locks, overrides or alerts. (day P&L is reset separately.) PURE."""
    for k in _DAY_KEYS:
        state[k] = False
    state[K_TARGET_PEAK] = 0.0     # running peak resets to flat, not False
    return state


# --- DAILY 2% TARGET with the A4 decision gate (repurposed account lock) -----------------

def target_levels(day_start_equity, cfg):
    """PURE: (full_target, min_target) in dollars. full = account_target_pct x day-start
    equity; min = account_target_min_pct x full. account_target_pct <= 0 disables -> (0,0)."""
    pct = float(getattr(cfg, 'account_target_pct', 0.0) or 0.0)
    if pct <= 0.0:
        return 0.0, 0.0
    full = pct * float(day_start_equity or 0.0)
    minp = float(getattr(cfg, 'account_target_min_pct', 0.0) or 0.0)
    return round(full, 2), round(minp * full, 2)


def update_peak(combined_net, state):
    """Track the running peak of COMBINED NET realized day P&L. Returns the (new) peak.
    Mutates only state[K_TARGET_PEAK]. PURE w.r.t. IO."""
    peak = float(state.get(K_TARGET_PEAK, 0.0) or 0.0)
    net = float(combined_net or 0.0)
    if net > peak:
        peak = round(net, 2)
        state[K_TARGET_PEAK] = peak
    return peak


def latch_target(combined_net, day_start_equity, post_a4, cfg, state):
    """Latch SECURED / GIVE-BACK once their condition first holds (unless overridden). Returns
    (just_secured, just_giveback) so the caller fires each one-time alert. Assumes update_peak
    already ran this tick. PURE w.r.t. IO.
      GIVE-BACK: peak banked >= min_target AND net retreated >= giveback from peak (applies
                 pre- OR post-A4 -- round-trip protection for an already-good day).
      SECURED:   post_a4 AND net >= min_target (the day's minimum acceptable close is booked)."""
    pct = float(getattr(cfg, 'account_target_pct', 0.0) or 0.0)
    if pct <= 0.0 or state.get(K_TARGET_OVERRIDE):
        return False, False
    _full, min_t = target_levels(day_start_equity, cfg)
    net = float(combined_net or 0.0)
    peak = float(state.get(K_TARGET_PEAK, 0.0) or 0.0)
    giveback = float(getattr(cfg, 'account_target_giveback_dollars', 0.0) or 0.0)
    just_secured = just_giveback = False
    if (min_t > 0.0 and peak >= min_t and giveback > 0.0 and (peak - net) >= giveback
            and not state.get(K_TARGET_GIVEBACK_LOCKED)):
        state[K_TARGET_GIVEBACK_LOCKED] = True
        just_giveback = True
    if (post_a4 and min_t > 0.0 and net >= min_t and not state.get(K_TARGET_SECURED)):
        state[K_TARGET_SECURED] = True
        just_secured = True
    return just_secured, just_giveback


def target_daystop(combined_net, day_start_equity, post_a4, cfg, state):
    """PURE: (blocked, reason, kind) for the daily 2% target lock. kind in
    ('secured','giveback',''). Disabled when account_target_pct <= 0; never blocks while
    K_TARGET_OVERRIDE is set. GIVE-BACK ranks above SECURED (both stop NEW entries only;
    the per-engine loss stops, kill switch and EOD/Friday flatten all outrank this soft lock)."""
    pct = float(getattr(cfg, 'account_target_pct', 0.0) or 0.0)
    if pct <= 0.0 or state.get(K_TARGET_OVERRIDE):
        return False, 'ok', ''
    _full, min_t = target_levels(day_start_equity, cfg)
    net = float(combined_net or 0.0)
    peak = float(state.get(K_TARGET_PEAK, 0.0) or 0.0)
    giveback = float(getattr(cfg, 'account_target_giveback_dollars', 0.0) or 0.0)
    if (state.get(K_TARGET_GIVEBACK_LOCKED)
            or (min_t > 0.0 and peak >= min_t and giveback > 0.0 and (peak - net) >= giveback)):
        return True, 'account_target_giveback', 'giveback'
    if (state.get(K_TARGET_SECURED)
            or (post_a4 and min_t > 0.0 and net >= min_t)):
        return True, 'account_target_secured', 'secured'
    return False, 'ok', ''


def target_status_lines(combined_net, day_start_equity, post_a4, cfg, state):
    """PURE: the /daylock + /status lines for the daily 2% target -- day-start equity,
    full/min targets, combined net, pre/post-A4 state, secured flag, A5-skip flag, and the
    give-back distance. Returns [] when the target is disabled (account_target_pct <= 0)."""
    pct = float(getattr(cfg, 'account_target_pct', 0.0) or 0.0)
    if pct <= 0.0:
        return []
    full, min_t = target_levels(day_start_equity, cfg)
    net = float(combined_net or 0.0)
    peak = float(state.get(K_TARGET_PEAK, 0.0) or 0.0)
    giveback = float(getattr(cfg, 'account_target_giveback_dollars', 0.0) or 0.0)
    blocked, _reason, kind = target_daystop(net, day_start_equity, post_a4, cfg, state)
    if state.get(K_TARGET_OVERRIDE):
        state_str = '🟠 OVERRIDDEN (no re-lock today)'
    elif kind == 'secured':
        state_str = '✅ SECURED'
    elif kind == 'giveback':
        state_str = '🛟 GIVE-BACK LOCK'
    else:
        state_str = '🟢 building'
    gb_dist = max(0.0, peak - net)
    a5 = 'skip' if bool(getattr(cfg, 'account_target_skip_a5_when_met', True)) else 'keep'
    return [
        f"🎯 Daily target: net ${net:+,.0f} / min ${min_t:,.0f} / full ${full:,.0f} "
        f"({pct*100:g}% of ${float(day_start_equity or 0.0):,.0f}) -> {state_str}",
        f"   gate: {'POST-A4' if post_a4 else 'PRE-A4 (building)'} · peak ${peak:+,.0f} · "
        f"give-back ${gb_dist:,.0f}/{giveback:,.0f} · A5={a5}",
    ]


def anchors_daystop(day_pnl, cfg, state):
    """PURE: (blocked, reason, kind) for the ANCHORS engine given realized day P&L +
    the persisted lock state. kind in ('loss','profit',''). LOSS is HARD (day_pnl <=
    anchors_daily_loss_stop; never overridable). PROFIT is SOFT (day_pnl >=
    anchors_daily_profit_stop; suppressed by anchors_profit_override). Loss ranks above
    profit. loss_stop == 0 / profit_stop == 0 disable their gate."""
    loss_stop = float(getattr(cfg, 'anchors_daily_loss_stop', 0.0))
    profit_stop = float(getattr(cfg, 'anchors_daily_profit_stop', 0.0))
    dp = float(day_pnl or 0.0)
    if loss_stop < 0.0 and dp <= loss_stop:
        return True, 'daily_loss_stop', 'loss'
    if (profit_stop > 0.0 and not state.get(K_ANCHORS_OVERRIDE)
            and (state.get(K_ANCHORS_LOCKED) or dp >= profit_stop)):
        return True, 'daily_profit_stop', 'profit'
    return False, 'ok', ''


def account_daystop(combined_pnl, day_start_equity, cfg, state):
    """PURE: (blocked, reason) for the combined ACCOUNT-level lock. INERT when
    account_daily_profit_stop_pct == 0 (the owner default). When armed (pct > 0): combined
    realized P&L across all magics >= pct x day-start equity -> locked (suppressed by
    account_profit_override). Profit-style / soft (overridable via /daylock off)."""
    pct = float(getattr(cfg, 'account_daily_profit_stop_pct', 0.0) or 0.0)
    if pct <= 0.0:
        return False, 'ok'
    try:
        thresh = pct * float(day_start_equity or 0.0)
    except Exception:
        return False, 'ok'
    if (not state.get(K_ACCOUNT_OVERRIDE)
            and (state.get(K_ACCOUNT_LOCKED) or float(combined_pnl or 0.0) >= thresh)):
        return True, 'account_profit_stop'
    return False, 'ok'


def latch_profit(day_pnl, cfg, state):
    """Latch state[anchors_profit_locked] once realized day P&L reaches the profit target
    (unless already overridden for the day). Returns True iff it JUST latched (the caller
    fires the one-time alert). PURE w.r.t. IO."""
    profit_stop = float(getattr(cfg, 'anchors_daily_profit_stop', 0.0))
    if (profit_stop > 0.0 and not state.get(K_ANCHORS_OVERRIDE)
            and float(day_pnl or 0.0) >= profit_stop and not state.get(K_ANCHORS_LOCKED)):
        state[K_ANCHORS_LOCKED] = True
        return True
    return False


def rebuild_anchors_day_pnl(trader, dt_from=None, dt_to=None):
    """E-20 (anchors, Part 1): rebuild realized ANCHORS day P&L (state['daily_pnl']) from
    BROKER deal history for magic 20260522 for the current broker day -- a same-day restart
    must NOT trust a possibly-stale persisted value. day_pnl = sum(profit+swap+commission)
    over entry-OUT deals. Returns the float, or None if history is unavailable (the caller
    keeps the persisted value). READ-ONLY; guarded. Mirrors rogue/fetcher's rebuild."""
    try:
        if dt_from is None or dt_to is None:
            dt_from, dt_to = _broker_day_range(trader)
        deals = trader.adapter.mt5.history_deals_get(dt_from, dt_to) or []
    except Exception as e:
        log.warning(f"{ANCHORS_ALERT_PREFIX} day-pnl rebuild query failed: {e!r}")
        return None
    try:
        import pnl_source as _ps
        # SINGLE SOURCE OF TRUTH: anchors realized day P&L via pnl_source.magic_day_net
        # (the SAME function the report / reconcile / rogue+fetcher rebuilds use, by magic).
        day_pnl = _ps.magic_day_net(deals, ANCHORS_MAGIC)
        n_out = sum(1 for d in deals if int(getattr(d, 'magic', 0) or 0) == ANCHORS_MAGIC
                    and getattr(d, 'entry', None) == 1)
        log.info(f"{ANCHORS_ALERT_PREFIX} day P&L rebuilt from history: ${day_pnl:+.2f} "
                 f"({n_out} closes, magic {ANCHORS_MAGIC})")
        return day_pnl
    except Exception as e:
        log.warning(f"{ANCHORS_ALERT_PREFIX} day-pnl rebuild parse failed: {e!r}")
        return None


def _broker_day_range(trader):
    """(dt_from, dt_to) UTC datetimes bounding the CURRENT broker day. Mirrors
    fetcher._broker_day_range. Guarded; (None, None) on any error."""
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


def render_status(anchors_pnl, rogue_pnl, fetcher_pnl, combined_pnl,
                  day_start_equity, cfg, state):
    """PURE: the /daylock status lines -- each engine's realized day P&L vs BOTH thresholds
    with its lock/halt state, plus the (disabled-by-default) account lock. Returns a list of
    'label: value' pairs for the embed/text."""
    ab, areason, akind = anchors_daystop(anchors_pnl, cfg, state)
    acct_b, _ = account_daystop(combined_pnl, day_start_equity, cfg, state)

    def _eng_line(name, pnl, profit_stop, loss_stop, blocked, kind, extra=''):
        state_str = ('🔴 LOSS-HALT' if kind == 'loss'
                     else ('🟡 PROFIT-LOCK' if kind == 'profit'
                           else ('🟠 ' + kind.upper() if kind else '🟢 live')))
        return (f"{name}: ${float(pnl or 0.0):+.0f} "
                f"(profit {profit_stop:g} / loss {loss_stop:g}) -> {state_str}{extra}")

    lines = [
        _eng_line('Anchors', anchors_pnl,
                  float(getattr(cfg, 'anchors_daily_profit_stop', 0.0)),
                  float(getattr(cfg, 'anchors_daily_loss_stop', 0.0)),
                  ab, akind,
                  extra=(' (overridden)' if state.get(K_ANCHORS_OVERRIDE) else '')),
        _eng_line('Rogue', rogue_pnl,
                  float(getattr(cfg, 'rogue_daily_profit_stop', 0.0)),
                  float(getattr(cfg, 'rogue_daily_loss_stop', 0.0)), False, ''),
        _eng_line('Fetcher', fetcher_pnl,
                  float(getattr(cfg, 'fetcher_daily_profit_stop', 0.0)),
                  float(getattr(cfg, 'fetcher_daily_loss_stop', 0.0)), False, ''),
    ]
    pct = float(getattr(cfg, 'account_daily_profit_stop_pct', 0.0) or 0.0)
    if pct <= 0.0:
        lines.append(f"Account: ${float(combined_pnl or 0.0):+.0f} combined "
                     f"— lock DISABLED (account_daily_profit_stop_pct=0)")
    else:
        thresh = pct * float(day_start_equity or 0.0)
        lines.append(f"Account: ${float(combined_pnl or 0.0):+.0f} / ${thresh:,.0f} "
                     f"({pct*100:g}% of day-start) -> "
                     f"{'🔒 LOCKED' if acct_b else '🟢 live'}"
                     + (' (overridden)' if state.get(K_ACCOUNT_OVERRIDE) else ''))
    return lines
