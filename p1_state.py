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
            'boost_trails': {},
            'engines': None}
    try:
        # v3.6.0 ENGINE SWITCHES: persist the runtime flags so a same-day restart
        # restores them (persisted state WINS over the config boot defaults; the
        # override-vs-default alert fires in recover_on_boot).
        eng = getattr(trader, 'engines', None)
        if isinstance(eng, dict):
            snap['engines'] = {'anchors': bool(eng.get('anchors', True)),
                               'rogue': bool(eng.get('rogue', True)),
                               'fetcher': bool(eng.get('fetcher', True))}
    except Exception:
        pass
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
                # v3.6.0 seed independence: the latched seed + the passive fallback
                # captures survive a same-day restart, so a restart never re-seeds
                # at a different price/source.
                'seed_px': r.get('seed_px'),
                'seed_source': r.get('seed_source'),
                'seed_recorded_px': r.get('seed_recorded_px'),
                'a1_snap_px': r.get('a1_snap_px'),
                'day_open_px': r.get('day_open_px'),
                'open': ({'ticket': o.get('ticket'), 'side': o.get('side'),
                          'entry': o.get('entry'), 'sl': o.get('sl'), 'peak': o.get('peak'),
                          'magic': o.get('magic'), 'leg_type': o.get('leg_type')}
                         if o else None),
                'gov': {'reanchor_count': int(gov.get('reanchor_count', 0)),
                        'day_pnl': float(gov.get('day_pnl', 0.0)),
                        'consec_fails': int(gov.get('consec_fails', 0)),
                        'loss_stopped': bool(gov.get('loss_stopped', False)),
                        'fail_paused': bool(gov.get('fail_paused', False)),
                        # 2026-07-08 soft daily-profit lock: the override + alerted flags are
                        # RUNTIME decisions that a same-day restart must preserve (rebuilt
                        # numbers alone can't recover a mid-day manual override).
                        'profit_locked': bool(gov.get('profit_locked', False)),
                        'profit_override': bool(gov.get('profit_override', False)),
                        'profit_alerted': bool(gov.get('profit_alerted', False))},
            }
    except Exception:
        pass
    try:
        # v3.7.0 FETCHER runtime state (mirror the Rogue block): the switch + anchor +
        # seed provenance + the open ticket + the day governor survive a same-day restart.
        # The governor is ADDITIONALLY rebuilt from broker deal history on recover (E-20),
        # but persisting it here keeps the snapshot self-describing + is the fallback if
        # the history query fails.
        fr = getattr(trader, '_fetcher', None)
        if fr is not None:
            fgov = fr.get('gov', {}) or {}
            fo = fr.get('open') or None
            snap['fetcher'] = {
                'day': fr.get('day'),
                'anchor': fr.get('anchor'),
                'leg_dir': fr.get('leg_dir'),
                'seed_px': fr.get('seed_px'),
                'seed_source': fr.get('seed_source'),
                'seed_recorded_px': fr.get('seed_recorded_px'),
                'a1_snap_px': fr.get('a1_snap_px'),
                'day_open_px': fr.get('day_open_px'),
                'open': ({'ticket': fo.get('ticket'), 'side': fo.get('side'),
                          'entry': fo.get('entry'), 'tp': fo.get('tp'), 'sl': fo.get('sl'),
                          'magic': fo.get('magic'), 'leg_type': fo.get('leg_type')}
                         if fo else None),
                'gov': {'entries': int(fgov.get('entries', 0)),
                        'day_pnl': float(fgov.get('day_pnl', 0.0)),
                        'consec_fails': int(fgov.get('consec_fails', 0)),
                        'loss_stopped': bool(fgov.get('loss_stopped', False)),
                        'fail_paused': bool(fgov.get('fail_paused', False)),
                        'profit_locked': bool(fgov.get('profit_locked', False)),
                        'profit_override': bool(fgov.get('profit_override', False)),
                        'profit_alerted': bool(fgov.get('profit_alerted', False))},
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
        # --- v3.6.0 restore the ENGINE SWITCHES (persisted runtime state WINS over
        # the config boot defaults). Any restored value that differs from the boot
        # default is LOUD: "⚠️ ENGINE STATE OVERRIDE" log + Discord alert naming
        # BOTH values, so a forgotten mid-day /anchors off can never silently
        # carry into a restart unnoticed. ---
        eng_saved = data.get('engines')
        if isinstance(eng_saved, dict) and isinstance(getattr(trader, 'engines', None), dict):
            defaults = getattr(trader, '_engine_boot_defaults', None) or {
                'anchors': bool(getattr(trader.cfg, 'non_oco_enabled', True)),
                'rogue': bool(getattr(trader.cfg, 'rogue_enabled', True)),
                'fetcher': bool(getattr(trader.cfg, 'fetcher_enabled', True))}
            for name in ('anchors', 'rogue', 'fetcher'):
                if name not in eng_saved:
                    continue
                restored = bool(eng_saved[name])
                trader.engines[name] = restored          # persisted state WINS
                boot_default = bool(defaults.get(name, True))
                if restored != boot_default:
                    msg = (f"⚠️ ENGINE STATE OVERRIDE — {name} engine restored "
                           f"{'ON' if restored else 'OFF'} from run/state.json, but the "
                           f"config boot default is {'ON' if boot_default else 'OFF'}. "
                           f"Persisted state wins; use /{name} "
                           f"{'off' if restored else 'on'} to revert.")
                    log.warning(msg)
                    try:
                        trader.tele.warn(msg)
                    except Exception:
                        pass
            summary['engines'] = dict(trader.engines)
        # --- restore ROGUE governors + chain anchor + open ticket ---
        r = data.get('rogue')
        if isinstance(r, dict):
            import rogue as _rogue
            gov_s = r.get('gov', {}) or {}
            gov = _rogue.new_day_state()
            # persisted snapshot as the BASE (fallback if the history query fails)...
            gov.update({'reanchor_count': int(gov_s.get('reanchor_count', 0)),
                        'day_pnl': float(gov_s.get('day_pnl', 0.0)),
                        'consec_fails': int(gov_s.get('consec_fails', 0)),
                        'loss_stopped': bool(gov_s.get('loss_stopped', False)),
                        'fail_paused': bool(gov_s.get('fail_paused', False)),
                        'profit_locked': bool(gov_s.get('profit_locked', False)),
                        'profit_override': bool(gov_s.get('profit_override', False)),
                        'profit_alerted': bool(gov_s.get('profit_alerted', False))})
            # ...then PART 1 (E-20): REBUILD day_pnl / reanchor_count / consec_fails and the
            # loss/fail/profit LATCHES from broker deal history for magic 20260626 (current
            # broker day). A same-day restart must NEVER reset the governor to zero. Broker
            # truth wins over the snapshot; the RUNTIME override/alerted flags (not derivable
            # from deals) are overlaid FROM the snapshot so a mid-day manual override + its
            # one-time alert survive the restart. Query failure -> keep the persisted gov.
            try:
                _rb = _rogue.rebuild_gov_from_history(trader)
                if _rb is not None:
                    _rb['profit_override'] = bool(gov_s.get('profit_override', False))
                    _rb['profit_alerted'] = bool(gov_s.get('profit_alerted', False))
                    gov = _rb
            except Exception as e:
                log.warning(f"rogue gov rebuild non-fatal: {e!r}")
            st = {'day': today, 'gov': gov, 'anchor': r.get('anchor'),
                  'leg_dir': r.get('leg_dir'), 'open': None,
                  'a1_last_close': r.get('a1_last_close'),
                  'a1_reverted': bool(r.get('a1_reverted', False)),
                  # P3 (E-17): restore the chain-gate meta so the cooldown survives
                  # a same-day restart (absent in an older file -> None = not chained).
                  'chain_time': r.get('chain_time'),
                  'chain_anchor': r.get('chain_anchor'),
                  'chain_disp_up': float(r.get('chain_disp_up', 0.0) or 0.0),
                  'chain_disp_dn': float(r.get('chain_disp_dn', 0.0) or 0.0),
                  # v3.6.0 seed independence: restore the latched seed + fallback
                  # captures (a same-day restart must never re-seed differently).
                  'seed_px': r.get('seed_px'),
                  'seed_source': r.get('seed_source'),
                  'seed_recorded_px': r.get('seed_recorded_px'),
                  'a1_snap_px': r.get('a1_snap_px'),
                  'day_open_px': r.get('day_open_px')}
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
        # --- restore FETCHER switch state + runtime + E-20 gov rebuild from history ---
        fr = data.get('fetcher')
        if isinstance(fr, dict):
            import fetcher as _fetcher
            fgov_s = fr.get('gov', {}) or {}
            fgov = _fetcher.new_day_state()
            # persisted snapshot as the BASE (the fallback if the history query fails)...
            fgov.update({'entries': int(fgov_s.get('entries', 0)),
                         'day_pnl': float(fgov_s.get('day_pnl', 0.0)),
                         'consec_fails': int(fgov_s.get('consec_fails', 0)),
                         'loss_stopped': bool(fgov_s.get('loss_stopped', False)),
                         'fail_paused': bool(fgov_s.get('fail_paused', False)),
                         'profit_locked': bool(fgov_s.get('profit_locked', False)),
                         'profit_override': bool(fgov_s.get('profit_override', False)),
                         'profit_alerted': bool(fgov_s.get('profit_alerted', False))})
            # ...then E-20: REBUILD the gov from broker deal history for the current broker
            # day (magic 20260707). A same-day restart must NEVER reset day_pnl/entries/fails
            # to zero (that would re-arm the full cap and forget a tripped brake). Broker
            # truth wins over the snapshot; the RUNTIME profit override/alerted flags (not
            # derivable from deals) are overlaid from the snapshot; query failure keeps gov.
            try:
                rebuilt = _fetcher.rebuild_gov_from_history(trader)
                if rebuilt is not None:
                    rebuilt['profit_override'] = bool(fgov_s.get('profit_override', False))
                    rebuilt['profit_alerted'] = bool(fgov_s.get('profit_alerted', False))
                    fgov = rebuilt
            except Exception as e:
                log.warning(f"fetcher gov rebuild non-fatal: {e!r}")
            fst = {'day': today, 'gov': fgov,
                   'anchor': fr.get('anchor'), 'leg_dir': fr.get('leg_dir'), 'open': None,
                   'seed_px': fr.get('seed_px'), 'seed_source': fr.get('seed_source'),
                   'seed_recorded_px': fr.get('seed_recorded_px'),
                   'a1_snap_px': fr.get('a1_snap_px'),
                   'day_open_px': fr.get('day_open_px')}
            fo = fr.get('open')
            if fo and fo.get('ticket') is not None and _position_open_at_broker(trader, fo.get('ticket')):
                # ADOPT the already-open Fetcher position instead of ignoring it.
                fst['open'] = {'ticket': fo.get('ticket'), 'side': fo.get('side'),
                               'entry': fo.get('entry'), 'tp': fo.get('tp'),
                               'sl': fo.get('sl'),
                               'magic': fo.get('magic', _fetcher.FETCHER_MAGIC),
                               'leg_type': fo.get('leg_type', _fetcher.FETCHER_LEG_TYPE)}
            trader._fetcher = fst
            summary['fetcher'] = True
        # --- v3.7.3 PART 1 (E-20 for anchors): rebuild the ANCHORS realized day P&L
        # (state['daily_pnl'], magic 20260522) from broker deal history so a same-day
        # restart never trusts a stale persisted value. Broker truth wins; a query failure
        # keeps the persisted value. The anchors/account lock override + alert flags live in
        # state.json (loaded by _load_state) and are preserved. ---
        try:
            import daystops as _ds
            _rebuilt_anchors = _ds.rebuild_anchors_day_pnl(trader)
            if _rebuilt_anchors is not None and isinstance(getattr(trader, 'state', None), dict):
                trader.state['daily_pnl'] = float(_rebuilt_anchors)
                summary['anchors_day_pnl'] = float(_rebuilt_anchors)
        except Exception as e:
            log.warning(f"anchors day-pnl rebuild non-fatal: {e!r}")
        summary['boosts'] = len(data.get('boost_trails', {}) or {})
        summary['anchors'] = len(data.get('processed_anchors_today', []) or [])
        summary['recovered'] = True
        rg = _rogue_summary(trader)
        _anchors_dp = float((getattr(trader, 'state', {}) or {}).get('daily_pnl', 0.0) or 0.0)
        try:
            log.info(f"RESTART-RECOVERY OK | day {today} | anchors_placed={summary['anchors']} "
                     f"(skipped on re-fire) | anchors_day_pnl=${_anchors_dp:+.2f} "
                     f"| {rg} | boost_trails={summary['boosts']}")
            trader.tele.info(f"♻️ *RESTART-RECOVERY OK* — day {today}; "
                             f"{summary['anchors']} anchor(s) already placed (skipped); "
                             f"{rg}; {summary['boosts']} boost trail(s) resumed.")
        except Exception:
            pass
    except Exception as e:
        log.warning(f"p1_state.recover_on_boot non-fatal: {e!r}")
        summary['reason'] = f'error:{e!r}'
    return summary
