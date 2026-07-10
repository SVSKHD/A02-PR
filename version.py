"""AUREON version — single source of truth for __version__ + banner().

The full behavioral changelog moved to aureon_utils/version_changelog.py
(reference only) to keep this file under the line cap. __version__ / CODENAME /
banner() are byte-identical to before the split.
"""


__version__ = "3.9.0"  # D-31 boost order geometry FIX: the spec boost opening SL is now a REAL backstop (spec_boost_sl_dollars $10) validated to clear symbol_info.trade_stops_level (widened to the broker minimum if inside), NOT breakeven-at-mid; NO placeholder TP is sent (tp=0.0), never entry+$1000; the +$1.50 ratchet arms on the tick loop at +spec_boost_min_lock favorable, not at fill. Fixes the 2026-07-10 boost-1 retcode=10016 (INVALID_STOPS) reject. selftest -> 305 (new 305 order-geometry). Flag OFF byte-identical.
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"