"""AUREON — P1 state persistence + boot recovery (E-16; supersedes restart-dormancy).

Persists a compact snapshot to `run/state.json` on every state change and restores it on a
SAME trading-day restart, so the bot is never blind or bricked across a crash / a Level-3
feed self-restart / a watchdog relaunch:

  - `processed_anchors_today` + per-anchor placed markers -> anchors already placed today
    are SKIPPED on re-fire (the anchor scheduler's own PLACED gate reads this set).
  - Rogue governors + chain anchor + open ticket: `a1_last_close` / `anchor` / `leg_dir` /
    `open` / `day_pnl` / `consec_fails` / `reanchor_count` (entries_today) / latches
    (`loss_stopped`, `fail_paused`, `a1_reverted`). Restoring `a1_last_close` is what the
    old, never-WRITTEN `a1_anchor_price` state keys (rogue.py `_a1_anchor_price`) were meant
    to do -- the Fix 4 engine now recovers its chain anchor across a restart.
  - boost trail states (armed flag + peak) derived from the open boost legs so a mid-ride
    boost resumes from its recorded peak (the underlying max_fav is also rehydrated by the
    main state file; this mirror keeps the P1 snapshot self-describing).
  - `trading_date` -- a NEW broker day IGNORES the stale file and starts fresh.

PURE serialization + guarded IO. A persistence error NEVER reaches the trading path.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("AUREON")

STATE_FILENAME = "state.json"


def _run_dir(trader):
    return getattr(trader, 'run_dir', None) or os.environ.get("AUREON_RUN_DIR", "./run")


def _path(trader):
    return os.path.join(_run_dir(trader), STATE_FILENAME)


def _trading_date(trader):
    try:
        return str((getattr(trader, 'state', {}) or {}).get('last_broker_date') or '')
    except Exception:
        return ''


def _boost_trails_from_shadows(trader):
    """Derive {ticket: {armed, peak, role}} for the open BOOST legs from shadow_positions.
    The boost 'peak' is the recorded max favorable excursion (max_fav); 'armed' is True once
    the trail arm has been passed (max_fav present). Read-only, guarded."""
    out = {}
    try:
        for tk, sh in (getattr(trader, 'shadow_positions', {}) or {}).items():
            role = (sh.get('role') if hasattr(sh, 'get') else None) or 'normal'
            if str(role).lower() in ('normal',):
                continue                          # only boost/rescue legs carry a boost trail
            peak = sh.get('max_fav') if hasattr(sh, 'get') else None
            out[str(tk)] = {'role': role, 'peak': peak, 'armed': peak is not None}
    except Exception:
        pass
    return out


def snapshot(trader):
    """Build the P1 snapshot dict from live trader state. Read-only on the trader; guarded
    field-by-field so a missing attribute never aborts the snapshot."""
    snap = {'trading_date': _trading_date(trader),
            'processed_anchors_today': [],
            'anchor_placed_markers': {},
            'rogue': None,
            'boost_trails': {}}
    try:
        st = getattr(trader, 'state', {}) or {}
        snap['processed_anchors_today'] = list(st.get('processed_anchors_today', []) or [])
        snap['anchor_placed_markers'] = {
            str(t): {'anchor_label': (sh.get('anchor_label') if hasattr(sh, 'get') else None),
                     'side': (sh.get('side') if hasattr(sh, 'get') else None)}
            for t, sh in (getattr(trader, 'shadow_positions', {}) or {}).items()}
    except Exception:
        pass
    try:
        r = getattr(trader, '_rogue', None)
        if r is not None:
            gov = r.get('gov', {}) or {}
            o = r.get('open') or None
            snap['rogue'] = {
                'day': r.get('day'),
                'anchor': r.get('anchor'),
                'leg_dir': r.get('leg_dir'),
                'a1_last_close': r.get('a1_last_close'),
                'a1_reverted': bool(r.get('a1_reverted', False)),
                # P3 (E-17): chain-gate meta -- a restart mid-cooldown must NOT bypass
                # the chain cooldown/displacement gate on the chained anchor.
                'chain_time': r.get('chain_time'),
                'chain_anchor': r.get('chain_anchor'),
                'chain_disp_up': float(r.get('chain_disp_up', 0.0) or 0.0),
                'chain_disp_dn': float(r.get('chain_disp_dn', 0.0) or 0.0),
                'open': ({'ticket': o.get('ticket'), 'side': o.get('side'),
                          'entry': o.get('entry'), 'sl': o.get('sl'), 'peak': o.get('peak'),
                          'magic': o.get('magic'), 'leg_type': o.get('leg_type')}
                         if o else None),
                'gov': {'reanchor_count': int(gov.get('reanchor_count', 0)),
                        'day_pnl': float(gov.get('day_pnl', 0.0)),
                        'consec_fails': int(gov.get('consec_fails', 0)),
                        'loss_stopped': bool(gov.get('loss_stopped', False)),
                        'fail_paused': bool(gov.get('fail_paused', False))},
            }
    except Exception:
        pass
    try:
        snap['boost_trails'] = _boost_trails_from_shadows(trader)
    except Exception:
        pass
    return snap


def save(trader, force=False):
    """Persist the P1 snapshot to run/state.json (atomic tmp+replace). No-op in paper unless
    `force` (the Level-3 exit forces a final write). Only writes when the snapshot CHANGED
    (unless force), so per-tick calls are cheap. Returns True iff it wrote. Guarded."""
    try:
        if getattr(trader, 'paper', False) and not force:
            return False
        snap = snapshot(trader)
        blob = json.dumps(snap, indent=2, default=str, sort_keys=True)
        if blob == getattr(trader, '_p1_last_blob', None) and not force:
            return False
        path = _path(trader)
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(blob)
        os.replace(tmp, path)
        trader._p1_last_blob = blob
        return True
    except Exception as e:
        log.warning(f"p1_state.save non-fatal: {e!r}")
        return False


def load(run_dir):
    """Load run/state.json -> dict, or {} if missing/corrupt. Guarded."""
    try:
        path = os.path.join(run_dir or "./run", STATE_FILENAME)
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f) or {}
    except Exception as e:
        log.warning(f"p1_state.load non-fatal: {e!r}")
        return {}


def _position_open_at_broker(trader, ticket):
    try:
        pos = trader.adapter.mt5.positions_get(ticket=int(ticket))
        return bool(pos)
    except Exception:
        return False


def _rogue_summary(trader):
    try:
        r = getattr(trader, '_rogue', None) or {}
        gov = r.get('gov', {}) or {}
        openx = ('open#%s' % (r.get('open') or {}).get('ticket')) if r.get('open') else 'no-open'
        anchor = r.get('a1_last_close') if r.get('a1_last_close') is not None else r.get('anchor')
        return (f"rogue[anchor={anchor} entries={gov.get('reanchor_count', 0)} "
                f"day_pnl={float(gov.get('day_pnl', 0.0)):+.2f} "
                f"fails={gov.get('consec_fails', 0)} {openx}]")
    except Exception:
        return 'rogue[?]'


def recover_on_boot(trader):
    """Fix 5 (E-16) boot recovery — call ONCE after the first new-day reset. On a SAME
    trading-day restart: restore the Rogue governors + chain anchor + open ticket (adopting
    an already-open Rogue position IFF it is still open at the broker), and leave
    processed_anchors_today intact so anchors already placed today are SKIPPED. A NEW trading
    day ignores the stale file (fresh start). Logs 'RESTART-RECOVERY OK ...'. Returns a
    summary dict. Guarded -- never raises onto boot."""
    summary = {'recovered': False, 'reason': '', 'rogue': False, 'anchors': 0, 'boosts': 0}
    try:
        data = load(_run_dir(trader))
        if not data:
            summary['reason'] = 'no-file'
            return summary
        today = _trading_date(trader)
        saved_day = str(data.get('trading_date') or '')
        if not today or saved_day != today:
            summary['reason'] = f'new-day(saved={saved_day},today={today})'
            log.info(f"RESTART-RECOVERY: stale/new-day file ignored ({summary['reason']}).")
            return summary
        # --- restore ROGUE governors + chain anchor + open ticket ---
        r = data.get('rogue')
        if isinstance(r, dict):
            import rogue as _rogue
            gov_s = r.get('gov', {}) or {}
            gov = _rogue.new_day_state()
            gov.update({'reanchor_count': int(gov_s.get('reanchor_count', 0)),
                        'day_pnl': float(gov_s.get('day_pnl', 0.0)),
                        'consec_fails': int(gov_s.get('consec_fails', 0)),
                        'loss_stopped': bool(gov_s.get('loss_stopped', False)),
                        'fail_paused': bool(gov_s.get('fail_paused', False))})
            st = {'day': today, 'gov': gov, 'anchor': r.get('anchor'),
                  'leg_dir': r.get('leg_dir'), 'open': None,
                  'a1_last_close': r.get('a1_last_close'),
                  'a1_reverted': bool(r.get('a1_reverted', False)),
                  # P3 (E-17): restore the chain-gate meta so the cooldown survives
                  # a same-day restart (absent in an older file -> None = not chained).
                  'chain_time': r.get('chain_time'),
                  'chain_anchor': r.get('chain_anchor'),
                  'chain_disp_up': float(r.get('chain_disp_up', 0.0) or 0.0),
                  'chain_disp_dn': float(r.get('chain_disp_dn', 0.0) or 0.0)}
            o = r.get('open')
            if o and o.get('ticket') is not None and _position_open_at_broker(trader, o.get('ticket')):
                # ADOPT the already-open Rogue position instead of ignoring it.
                st['open'] = {'ticket': o.get('ticket'), 'side': o.get('side'),
                              'entry': o.get('entry'), 'sl': o.get('sl'),
                              'peak': o.get('peak', o.get('entry')),
                              'magic': o.get('magic', _rogue.ROGUE_MAGIC),
                              'leg_type': o.get('leg_type', _rogue.ROGUE_LEG_TYPE)}
            trader._rogue = st
            summary['rogue'] = True
        summary['boosts'] = len(data.get('boost_trails', {}) or {})
        summary['anchors'] = len(data.get('processed_anchors_today', []) or [])
        summary['recovered'] = True
        rg = _rogue_summary(trader)
        try:
            log.info(f"RESTART-RECOVERY OK | day {today} | anchors_placed={summary['anchors']} "
                     f"(skipped on re-fire) | {rg} | boost_trails={summary['boosts']}")
            trader.tele.info(f"♻️ *RESTART-RECOVERY OK* — day {today}; "
                             f"{summary['anchors']} anchor(s) already placed (skipped); "
                             f"{rg}; {summary['boosts']} boost trail(s) resumed.")
        except Exception:
            pass
    except Exception as e:
        log.warning(f"p1_state.recover_on_boot non-fatal: {e!r}")
        summary['reason'] = f'error:{e!r}'
    return summary
