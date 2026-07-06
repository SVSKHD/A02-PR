"""feat/symbol-profiles — per-symbol Config profiles.

A PROFILE is a plain dict of Config dataclass-field overrides. GOLD's profile
is EMPTY ({}) because the Config defaults ARE gold — `build_config('gold')`
is exactly `Config()`, guaranteeing byte-identical gold behavior (regression-
guarded in the selftest). SILVER (config_profiles/silver.py) overrides the
measured XAGUSD scale.

Loader rules (validator-grade, fail LOUDLY):
  * profile name must be one of PROFILES;
  * every PROFILE key must be a real Config dataclass field — an unknown key
    aborts with the full list of offenders (a typo'd override silently doing
    nothing is exactly the class of bug this exists to prevent).
"""
import dataclasses

from config import Config

PROFILES = ('gold', 'silver')


def load_profile(name):
    """Return a COPY of the PROFILE dict for `name`. Unknown name raises."""
    key = str(name or 'gold').strip().lower()
    if key == 'gold':
        from config_profiles.gold import PROFILE
    elif key == 'silver':
        from config_profiles.silver import PROFILE
    else:
        raise ValueError(
            f"unknown profile {name!r} — expected one of {PROFILES}")
    return dict(PROFILE)


def build_config(profile='gold'):
    """cfg = Config(**PROFILE) for the named profile. Rejects unknown keys
    loudly (validator rule): every override must be a real Config field."""
    overrides = load_profile(profile)
    known = {f.name for f in dataclasses.fields(Config)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise ValueError(
            f"profile {profile!r} has {len(unknown)} unknown Config key(s): "
            f"{unknown} — fix the profile (or add the field to config.Config); "
            f"a silently-ignored override is forbidden.")
    return Config(**overrides)
