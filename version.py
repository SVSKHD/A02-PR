"""AUREON version — single source of truth for __version__ + banner().

The full behavioral changelog moved to aureon_utils/version_changelog.py
(reference only) to keep this file under the line cap. __version__ / CODENAME /
banner() are byte-identical to before the split.
"""


__version__ = "3.8.7"  # R-14: pnl_report drifted from MT5 authority — report anchor net now == pnl_source.magic_day_net over the SAME broker-day window; no straddling/partial/unattributable realized close is dropped
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"