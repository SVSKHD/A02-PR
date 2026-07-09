"""AUREON boot validator (the watchdog) — proves the WIRING before any trading starts.

Runs on EVERY start, default ON (VALIDATOR_ENABLED). _probe() reads the REAL state of
the live Config object + the feature modules (not a template): every feature flag is
read off the actual cfg, every feature module is imported for real, every LiveTrader
seam is checked as actually bound, and the rogue freeze invariant is evaluated against
the real config. WIRING failures (a missing flag / unbound seam / failed import / broken
invariant) => DO-NOT-START: the bot must not trade. LIVE-pending checks (things only
provable at the first real trade, e.g. a boost filling rc=10009) stay PENDING and do
NOT block boot. A --skip-validator escape exists for explicit manual debugging only.
"""
from __future__ import annotations

import logging

log = logging.getLogger("AUREON")

VALIDATOR_ENABLED = True   # default ON; --skip-validator is the explicit manual override

WIRING = 'wiring'
LIVE = 'live'

# the feature flags the bot relies on (read off the REAL cfg).
_EXPECTED_FLAGS = (
    'override_entry_enabled', 'rescue_entry_enabled', 'override_entry_smooth_confirm',
    'rescue_entry_smooth_confirm', 'override_entry_dynamic_sl',
    'parent_profit_override_enabled', 'rally_pullback_enabled',
    'rogue_enabled', 'rogue_daywatch',
    # v3.6.0 engine switches + rogue seed independence
    'non_oco_enabled', 'rogue_seed_fallback',
    # v3.7.0 FETCHER engine: master switch, grid knobs, governors, seed fallback
    'fetcher_enabled', 'fetcher_trigger_dollars', 'fetcher_tp_dollars',
    'fetcher_sl_dollars', 'fetcher_max_entries_per_day', 'fetcher_daily_loss_stop',
    'fetcher_consecutive_fail_stop', 'fetcher_flatten_at_eod', 'fetcher_seed_fallback',
    # 2026-07-08 daily stops (soft profit-lock + tightened loss halt), both engines
    'rogue_daily_loss_stop', 'rogue_daily_profit_stop', 'fetcher_daily_profit_stop',
    # v3.7.3 ANCHORS-engine daily stops + the (inert) account-level lock
    'anchors_daily_profit_stop', 'anchors_daily_loss_stop', 'account_daily_profit_stop_pct',
    # 2026-07-09 $10-break seed anchor (Rule 1) + earned trade budget (Rule 2), both engines
    'seed_break_dollars', 'engine_base_trades_per_anchor', 'engine_extend_requires_wins',
    'engine_exhausted_gap_sec',
)
# the feature modules that MUST import cleanly for the wired behavior to exist.
_FEATURE_MODULES = ('pullback_entry', 'rally', 'rescue', 'rogue', 'boosts',
                    'boosts_common', 'strategy', 'break_hold', 'fetcher', 'daystops',
                    'seed_budget',
                    # 2026-07-09 P&L pipeline: the single source of truth + the R-8 CSV
                    # self-heal + the reconcile audit must all import cleanly.
                    'pnl_source', 'csv_schema', 'pnl_reconcile')
# the LiveTrader seams that MUST be bound for the per-tick flow to dispatch.
_SEAMS = ('_break_and_hold_ok', '_rescue_entry_ok', '_check_boost_triggers',
          '_resolved_anchor_hm', '_process_anchor_if_due',
          # v3.6.0 engine switches: the shared entries-blocked seams + the runtime read
          '_engine_enabled', '_anchor_entries_blocked', '_rogue_entries_blocked',
          # v3.7.0 FETCHER engine: its shared entries-blocked seam
          '_fetcher_entries_blocked',
          # v3.7.3 ANCHORS daily stops + account lock seams
          '_anchors_daystop_blocked', '_account_locked')


def _probe(cfg):
    """Read the ACTUAL running config + modules and return a flat list of checks:
    {name, kind ('wiring'|'live'), ok, detail}. PURE w.r.t. trading (imports + attribute
    reads only -- never places or modifies an order)."""
    checks = []

    def w(name, ok, detail):
        checks.append({'name': name, 'kind': WIRING, 'ok': bool(ok), 'detail': str(detail)})

    def pending(name, detail):
        checks.append({'name': name, 'kind': LIVE, 'ok': True, 'pending': True,
                       'detail': str(detail)})

    # 1. every expected feature flag exists on the REAL cfg (read its actual value).
    for flag in _EXPECTED_FLAGS:
        has = hasattr(cfg, flag)
        w(f'flag:{flag}', has, (f'{flag}={getattr(cfg, flag)!r}' if has else 'MISSING on cfg'))

    # 2. every feature module imports for real.
    for mod in _FEATURE_MODULES:
        try:
            __import__(mod)
            w(f'import:{mod}', True, 'import ok')
        except Exception as e:
            w(f'import:{mod}', False, f'IMPORT FAILED: {e!r}')

    # 3. every LiveTrader seam is actually bound.
    try:
        import live_trader as _lt
        for seam in _SEAMS:
            w(f'seam:LiveTrader.{seam}', hasattr(_lt.LiveTrader, seam),
              'bound' if hasattr(_lt.LiveTrader, seam) else 'NOT BOUND')
    except Exception as e:
        w('seam:LiveTrader.import', False, f'live_trader import FAILED: {e!r}')

    # 4. ROGUE wiring: distinct magic + the freeze invariant (raw cfg => should_run
    #    matches rogue_enabled; a funded account is force-disabled).
    try:
        import rogue as _r
        w('rogue:magic_distinct', _r.ROGUE_MAGIC not in (20260522, 9999998),
          f'ROGUE_MAGIC={_r.ROGUE_MAGIC}')
        raw_run = _r.should_run(cfg, is_funded=False)
        w('rogue:run_matches_flag', raw_run == bool(getattr(cfg, 'rogue_enabled', False)),
          f'should_run={raw_run} rogue_enabled={getattr(cfg, "rogue_enabled", None)}')
        w('rogue:funded_force_off', _r.should_run(cfg, is_funded=True) is False,
          'funded => forced OFF (mandatory gate)')
    except Exception as e:
        w('rogue:import', False, f'rogue import FAILED: {e!r}')

    # 4b. v3.6.0 rogue seed fallback: the knob must hold a KNOWN mode -- a typo'd
    #     value would silently leave Rogue seedless on an anchors-off morning.
    _seed_mode = str(getattr(cfg, 'rogue_seed_fallback', 'a1_time_snapshot')).lower()
    w('rogue:seed_fallback_valid', _seed_mode in ('a1_time_snapshot', 'market_open'),
      f'rogue_seed_fallback={_seed_mode!r}')

    # 4c. v3.7.0 FETCHER wiring: distinct magic + the same freeze invariant as Rogue
    #     (should_run matches fetcher_enabled; funded forced OFF), the seed-fallback knob
    #     is a known mode, and the PAIRED-governor invariant holds -- the daily loss stop
    #     must be DEEPER than the 3-fail pause (loss_stop <= -fail_stop x one SL strike) so
    #     the pause is always reachable BEFORE the halt (never dead code; the E-5 lesson).
    try:
        import fetcher as _f
        w('fetcher:magic_distinct',
          _f.FETCHER_MAGIC not in (20260522, 20260626, 9999998),
          f'FETCHER_MAGIC={_f.FETCHER_MAGIC}')
        f_raw = _f.should_run(cfg, is_funded=False)
        w('fetcher:run_matches_flag', f_raw == bool(getattr(cfg, 'fetcher_enabled', False)),
          f'should_run={f_raw} fetcher_enabled={getattr(cfg, "fetcher_enabled", None)}')
        w('fetcher:funded_force_off', _f.should_run(cfg, is_funded=True) is False,
          'funded => forced OFF (mandatory gate)')
        # 2026-07-08 DAILY STOPS: the loss stop is a non-positive $ bound (0 disables) and
        # the profit stop is a non-negative $ bound (0 disables). The old D-13 "pause
        # reachable before the loss stop" invariant is SUPERSEDED (owner tightened the loss
        # stop to -$370, so the 3-fail pause is intentionally unreachable at current
        # defaults); the pause code is kept and re-arms if the SL/stop values change.
        _f_loss = float(getattr(cfg, 'fetcher_daily_loss_stop', 0.0))
        _f_profit = float(getattr(cfg, 'fetcher_daily_profit_stop', 0.0))
        w('fetcher:daily_stops_sane', _f_loss <= 0.0 and _f_profit >= 0.0,
          f'loss_stop={_f_loss} (<=0; 0 disables) profit_stop={_f_profit} (>=0; 0 disables)')
        _f_seed = str(getattr(cfg, 'fetcher_seed_fallback', 'a1_time_snapshot')).lower()
        w('fetcher:seed_fallback_valid', _f_seed in ('a1_time_snapshot', 'market_open'),
          f'fetcher_seed_fallback={_f_seed!r}')
    except Exception as e:
        w('fetcher:import', False, f'fetcher import FAILED: {e!r}')

    # 4d. v3.7.3 ANCHORS daily stops + account lock: the daystops module imports, the
    # thresholds are sane bounds (loss <= 0, profit >= 0, account pct >= 0; 0 disables),
    # and the anchors day P&L rebuild (Part 1) exists.
    try:
        import daystops as _ds
        _a_loss = float(getattr(cfg, 'anchors_daily_loss_stop', 0.0))
        _a_profit = float(getattr(cfg, 'anchors_daily_profit_stop', 0.0))
        _acct_pct = float(getattr(cfg, 'account_daily_profit_stop_pct', 0.0))
        w('anchors:daily_stops_sane', _a_loss <= 0.0 and _a_profit >= 0.0 and _acct_pct >= 0.0,
          f'anchors loss={_a_loss} (<=0) profit={_a_profit} (>=0) account_pct={_acct_pct} (>=0)')
        w('anchors:daypnl_rebuild_present', callable(getattr(_ds, 'rebuild_anchors_day_pnl', None)),
          'daystops.rebuild_anchors_day_pnl present (E-20 for anchors)')
    except Exception as e:
        w('daystops:import', False, f'daystops import FAILED: {e!r}')

    # 4e. 2026-07-09 $10-break seed anchor (Rule 1) + earned trade budget (Rule 2): the shared
    # seed_budget cores import and are SANE for the REAL cfg -- break_dollars a non-negative $
    # bound (0 disables), base/gap non-negative (0 disables the budget), and the pure cores
    # actually gate (a $10 break latches; a spent budget with a non-all-win window exhausts).
    try:
        import seed_budget as _sbmod
        _brk = float(getattr(cfg, 'seed_break_dollars', 0.0) or 0.0)
        _base = int(getattr(cfg, 'engine_base_trades_per_anchor', 0) or 0)
        _gap = float(getattr(cfg, 'engine_exhausted_gap_sec', 0.0) or 0.0)
        w('seed_budget:knobs_sane', _brk >= 0.0 and _base >= 0 and _gap >= 0.0,
          f'break=${_brk:g} (>=0; 0 disables) base={_base} (>=0; 0 disables) gap={_gap:g}s')
        _stx = {}
        _latch = (_sbmod.break_seed_anchor(_stx, 4000.0, 4000.0 + max(_brk, 1.0), max(_brk, 1.0))[0]
                  is not None)
        _bx = _sbmod.new_budget(); _bx['trades'] = max(_base, 1); _bx['wl'] = [True, False]
        _exh = (_sbmod.budget_can_trade(_bx, cfg)[0] is False) if _base > 0 else True
        w('seed_budget:cores_gate', _latch and _exh,
          f'break_latches={_latch} budget_exhausts_on_non_allwin={_exh}')
    except Exception as e:
        w('seed_budget:import', False, f'seed_budget import FAILED: {e!r}')

    # 5. derived-cap discipline present (rescue/rally caps resolvable).
    try:
        import boosts as _b
        rescue_cap = _b.boost_whipsaw_cap(cfg, 'RESCUE')
        rally_cap = _b.boost_whipsaw_cap(cfg, 'RALLY')
        w('cap:rescue_derived', rescue_cap > 0, f'RESCUE cap={rescue_cap}')
        w('cap:rally_derived', rally_cap > 0, f'RALLY cap={rally_cap}')
    except Exception as e:
        w('cap:derived', False, f'cap resolve FAILED: {e!r}')

    # 6. LIVE-pending (NOT wiring; never block boot -- provable only at first real trade).
    pending('live:boost_fills_rc10009', 'verified at the first real rescue/boost fill')
    pending('live:rogue_monster_entry', 'verified at the first real monster catch')
    pending('live:anchor_places_at_schedule', 'verified at the first scheduled anchor')

    # 7. ROGUE promotion RULE (report-only). At watchdog time cfg.rogue_enabled is the
    #    RAW value -- promote_on_boot() flips False->True for a demo account later, inside
    #    the LiveTrader. So we report the RULE, not the live state: printing the raw flag
    #    here would falsely read OFF on a demo boot. Always passes; no broker call.
    w('rogue:promotion_rule', True,
      f'rogue_enabled raw={getattr(cfg, "rogue_enabled", False)!r}; '
      f'demo boot promotes ON, funded forces OFF')
    return checks


def validate(cfg):
    """Aggregate _probe into a verdict. SAFE-TO-START iff ZERO wiring failures; any wiring
    failure => DO-NOT-START. LIVE-pending checks are reported but never gate the verdict."""
    checks = _probe(cfg)
    wiring_failures = [c for c in checks if c['kind'] == WIRING and not c['ok']]
    wiring_ok = [c for c in checks if c['kind'] == WIRING and c['ok']]
    pending = [c for c in checks if c['kind'] == LIVE]
    verdict = 'SAFE-TO-START' if not wiring_failures else 'DO-NOT-START'
    return {'verdict': verdict, 'wiring_failures': wiring_failures,
            'wiring_ok': wiring_ok, 'pending': pending, 'checks': checks}


def run_boot_validation(cfg, tele=None, skip=False):
    """The boot gate. Returns True to PROCEED, False to ABORT (caller exits non-zero).
    Default ON. skip=True (only via --skip-validator) bypasses with a loud warning. On
    DO-NOT-START it logs every wiring failure and best-effort alerts Discord/Telegram;
    the bot must NOT trade. LIVE-pending checks are logged as PENDING, not blocking."""
    if skip or not VALIDATOR_ENABLED:
        log.warning("🟡 watchdog SKIPPED (--skip-validator / VALIDATOR_ENABLED off) — "
                    "manual debug only; the bot is trading UNVALIDATED.")
        return True
    rep = validate(cfg)
    log.info(f"🛫 watchdog: {len(rep['wiring_ok'])} wiring OK, "
             f"{len(rep['wiring_failures'])} wiring FAIL, {len(rep['pending'])} live-pending")
    for c in rep['pending']:
        log.info(f"   · PENDING {c['name']}: {c['detail']}")
    if rep['verdict'] == 'SAFE-TO-START':
        log.info("✅ watchdog: SAFE-TO-START (0 wiring failures) — proceeding to run.")
        return True
    lines = [f" - {c['name']}: {c['detail']}" for c in rep['wiring_failures']]
    msg = ("🛑 AUREON watchdog: DO-NOT-START — the bot will NOT trade. "
           f"{len(rep['wiring_failures'])} WIRING FAILURE(S):\n" + "\n".join(lines))
    log.error(msg)
    _tele = tele
    if _tele is None:
        try:
            from telemetry import telemetry_from_env
            _tele = telemetry_from_env(component="AUREON-watchdog")
        except Exception:
            _tele = None
    if _tele is not None:
        try:
            _tele.error(msg)
        except Exception:
            pass
    return False
