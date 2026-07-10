"""AUREON simulator — PER-DAY CONFIG RECONSTRUCTION (baseline = July as TRADED).

The gate is meaningless unless the baseline replays each day with the config that
was LIVE on that day. Today's config.py has features that did not exist (or had
different values) during 2026-07-01..07-10; running it would place trades July
never saw. This reconstructs the config AS IT WAS from ERRORS.md's D-series, as a
timeline of change-points (broker-local time), applied to the running trader when
the active change-point advances -- so the 07-07 14:58 intra-day rogue flip is
honoured to the minute, not smeared across the day.

Sources (ERRORS.md D-series + owner note 2026-07-10):
  D-5   trapped_late_rescue_enabled (F-B)   LIVE 2026-07-03      (off before)
  D-14  FETCHER engine                       LIVE 2026-07-07      (did not exist before)
  D-11  rogue_entry_confirm_redesign 10->5   LIVE 2026-07-07 14:58
  D-13  rogue_init_sl 5->10                   LIVE 2026-07-07 14:58
  D-16/17 rogue/fetcher_daily_loss_stop ->-370  2026-07-08        (was -525 / -700)
  D-28  anchors-only: rogue+fetcher OFF       2026-07-09
  D-29  rescue_entry_enabled -> True          2026-07-09          (off before)
  D-26  seed_break_dollars (PR#101)           merged 07-09 — per owner, NOT in the
  D-27  engine_base_trades_per_anchor         running bot during the window ->
        both DISABLED (=0) for the ENTIRE baseline.

KNOWN SIMPLIFICATION (owner-directed): D-13 also blipped rogue_daily_loss_stop to
-1050 at 07-07 14:58 before D-16/17 cut it to -370 on 07-08. The owner spec folds
that transient ("-525 became -370 on 07-08"), so the baseline uses -525 until
07-08, then -370. The -1050 blip is not modelled (it rarely binds).
"""
from __future__ import annotations

import pandas as pd

BROKER_TZ_OFFSET_HOURS = 3

# Baseline (start of the window). seed_break/base_trades disabled for the WHOLE run.
_BASE = {
    'seed_break_dollars': 0.0,
    'rogue_seed_break_dollars': 0.0,
    'fetcher_seed_break_dollars': 0.0,
    'engine_base_trades_per_anchor': 0,
    'rogue_entry_confirm_redesign': 10.0,
    'rogue_init_sl': 5.0,
    'rogue_daily_loss_stop': -525.0,
    'fetcher_daily_loss_stop': -700.0,
    'rescue_entry_enabled': False,
    'trapped_late_rescue_enabled': False,
}
_BASE_ENGINES = {'anchors': True, 'rogue': True, 'fetcher': False}

# (effective broker-local datetime, cfg overrides, engine overrides, citation)
_TIMELINE = [
    ('2026-07-01 00:00', {}, {}, 'window start'),
    ('2026-07-03 00:00', {'trapped_late_rescue_enabled': True}, {}, 'D-5 F-B live'),
    ('2026-07-07 00:00', {}, {'fetcher': True}, 'D-14 fetcher live'),
    ('2026-07-07 14:58', {'rogue_entry_confirm_redesign': 5.0, 'rogue_init_sl': 10.0}, {},
     'D-11/D-13 rogue confirm 10->5, init_sl 5->10'),
    ('2026-07-08 00:00', {'rogue_daily_loss_stop': -370.0, 'fetcher_daily_loss_stop': -370.0}, {},
     'D-16/17 loss stops -> -370'),
    ('2026-07-09 00:00', {'rescue_entry_enabled': True}, {'rogue': False, 'fetcher': False},
     'D-28 anchors-only (rogue+fetcher off) + D-29 rescue_entry on'),
]

# seed_break / base_trades stay disabled the whole run (owner) -> never in the timeline.
_ALWAYS = {'seed_break_dollars', 'rogue_seed_break_dollars', 'fetcher_seed_break_dollars',
           'engine_base_trades_per_anchor'}


def _cp_time(s):
    return pd.Timestamp(s, tz='UTC') - pd.Timedelta(hours=BROKER_TZ_OFFSET_HOURS)


def active_config(broker_ts):
    """Merged (cfg_overrides, engine_overrides, applied_citations) live at
    `broker_ts` (a tz-aware UTC timestamp of the current tick)."""
    cfg = dict(_BASE)
    eng = dict(_BASE_ENGINES)
    cites = []
    for iso, cover, eover, cite in _TIMELINE:
        if broker_ts >= _cp_time(iso):
            cfg.update(cover)
            eng.update(eover)
            if cover or eover:
                cites.append(cite)
    return cfg, eng, cites


def active_index(broker_ts):
    """Index of the latest change-point active at broker_ts (for cheap 'did it
    change?' checks so we only re-apply on a boundary)."""
    idx = 0
    for i, (iso, _c, _e, _cite) in enumerate(_TIMELINE):
        if broker_ts >= _cp_time(iso):
            idx = i
    return idx


def apply_to_trader(trader, broker_ts):
    """Mutate trader.cfg + trader.engines to the config live at broker_ts. The live
    engines read cfg attributes dynamically, so mutating cfg mid-run takes effect;
    trader.engines[...] gates NEW entries per engine (OFF = manage-only). Returns
    (cfg_overrides, engine_overrides, citations) applied."""
    cfg_over, eng_over, cites = active_config(broker_ts)
    for k, v in cfg_over.items():
        if hasattr(trader.cfg, k):
            setattr(trader.cfg, k, v)
    for e, on in eng_over.items():
        if isinstance(getattr(trader, 'engines', None), dict):
            trader.engines[e] = bool(on)
        if hasattr(trader.cfg, e + '_enabled'):
            setattr(trader.cfg, e + '_enabled', bool(on))
    # anchors switch is non_oco_enabled
    if hasattr(trader.cfg, 'non_oco_enabled'):
        trader.cfg.non_oco_enabled = bool(eng_over.get('anchors', True))
    return cfg_over, eng_over, cites
