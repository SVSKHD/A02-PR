"""AUREON version — single source of truth for __version__ + banner().

The full behavioral changelog moved to aureon_utils/version_changelog.py
(reference only) to keep this file under the line cap. __version__ / CODENAME /
banner() are byte-identical to before the split.
"""


__version__ = "3.8.3"  # E-23 testfire loss-halt rail 6 + R-8 rescue/journal self-heal + R-10 engine-surface unification + comment/ERRORS.md truth
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"