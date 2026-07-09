"""AUREON version — single source of truth for __version__ + banner().

The full behavioral changelog moved to aureon_utils/version_changelog.py
(reference only) to keep this file under the line cap. __version__ / CODENAME /
banner() are byte-identical to before the split.
"""


__version__ = "3.8.4"  # selftest truth: config-drift tests assert behavior-vs-cfg (not values), utf-8 on every open(), emoji-safe stdout under redirect
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"