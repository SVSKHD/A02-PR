"""AUREON version — single source of truth for __version__ + banner().

The full behavioral changelog moved to aureon_utils/version_changelog.py
(reference only) to keep this file under the line cap. __version__ / CODENAME /
banner() are byte-identical to before the split.
"""


__version__ = "3.7.1"  # manual re-seed commands (/rogueseed /fetchseed) with live-testing rails
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"