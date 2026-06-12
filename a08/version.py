"""
AUREON A08 (MCX port) — single source of truth for the version.

Bump __version__ HERE and nowhere else. Every A08 module imports it; the
Telegram startup banner, logs, and the journal/Firebase docs all read from
this file, so the version you see in Telegram is by construction the version
that is running.

This is the India-market port of the FROZEN AUREON v2.9.8/v3 strategy onto
the DhanHQ API, trading MCX gold futures. The MT5/cTrader builds (repo root)
stay independent; this package is versioned on its own track (3.x = MCX).

History (one line per behavioral change):
  3.0.0  scaffold: dhan adapter -> anchors -> fills -> trails -> risk ->
         journal, mirroring the v3 module structure. Source strategy frozen
         at MT5 v2.9.8 behavior. STRUCTURAL DIFFERENCE #1 (Indian futures
         NET per contract) handled: the No-OCO coexisting fleet is replaced
         by a netting-adapted fleet (trapped leg closes at ~-($10xR) when the
         sibling stop triggers, then rescue + 2 boosts fire as NEW net
         positions). All $ distances scale through the live ratio R recomputed
         daily at first anchor -- NOTHING hardcoded in rupees. A1 dropped
         (MCX closed 05:00 IST); A2/A3/A4 live. PAPER/SIM ONLY -- the MT5
         forward record does NOT carry across the netting change; A08 builds
         its own demo record before any real rupee.
"""

__version__ = "3.0.0"
CODENAME = "Garuda"          # India port; A08 line
SOURCE_FROZEN = "2.9.8"      # MT5 reference behavior this port mirrors
SCHEMA_VERSION = 2           # Firebase aureon_mcx doc schema


def banner() -> str:
    return f"AUREON A08 v{__version__} ({CODENAME}) [MCX port of v{SOURCE_FROZEN}]"
