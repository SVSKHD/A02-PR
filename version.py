"""AUREON version — single source of truth for __version__ + banner().

The full behavioral changelog moved to aureon_utils/version_changelog.py
(reference only) to keep this file under the line cap. __version__ / CODENAME /
banner() are byte-identical to before the split.
"""


__version__ = "3.8.9"  # D-31 visibility: dynamic preflight flag list (every Config bool), boot ACTIVE block + startup "Boost mode" line, /status + /engines boost mode + suppressed-in-band count, state-machine armed/BAND_ESTABLISHED logs. Display-only; flag OFF byte-identical.
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"