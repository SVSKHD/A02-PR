"""AUREON version — single source of truth for __version__ + banner().

The full behavioral changelog moved to aureon_utils/version_changelog.py
(reference only) to keep this file under the line cap. __version__ / CODENAME /
banner() are byte-identical to before the split.
"""


__version__ = "3.8.8"  # D-31: boost_spec_v2 (flag-gated, DEFAULT OFF) — boosts JOIN the winning side outside the band + one-way ratchet (never negative); F-B gated off when ON; freeze=0 (R8) + tstop_after_min. UNVALIDATED on ticks.
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"