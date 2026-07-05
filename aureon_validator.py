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
)
# the feature modules that MUST import cleanly for the wired behavior to exist.
_FEATURE_MODULES = ('pullback_entry', 'rally', 'rescue', 'rogue', 'boosts',
                    'boosts_common', 'strategy', 'break_hold')
# the LiveTrader seams that MUST be bound for the per-tick flow to dispatch.
_SEAMS = ('_break_and_hold_ok', '_rescue_entry_ok', '_check_boost_triggers',
          '_resolved_anchor_hm', '_process_anchor_if_due',
          # v3.6.0 engine switches: the shared entries-blocked seams + the runtime read
          '_engine_enabled', '_anchor_entries_blocked', '_rogue_entries_blocked')


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
