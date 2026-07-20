"""AUREON version — single source of truth for __version__ + banner().

The full behavioral changelog moved to aureon_utils/version_changelog.py
(reference only) to keep this file under the line cap. __version__ / CODENAME /
banner() are byte-identical to before the split.
"""


__version__ = "3.10.1"  # v3.10.1 pending-stop rejection handling (10015 INVALID_PRICE flood fix): preflight (side + broker stops-level) before every stop send; Rogue ENTRY/CHAIN chase-to-market within pending_chase_cap_pts (3.0) else drop the arm; max 3 retries 0.5/1/2s backoff then abandon + one deduped Discord card; anchor/RB legs skip (never chase). Bumped forward from 3.9.1 (absorbs the monster engine + A+C line intended as 3.1.0/3.10.0). Prior 3.9.1: boost_spec_v3 (2026-07-13, flag boost_spec_v3_enabled DEFAULT ON, layered on boost_spec_v2): (1) per-boost-level CONFIRM GATE (IDLE->ARMED->FIRE, B1/B2 independent) -- a break must dwell boost_confirm_dwell_s ($12s) AND extend boost_confirm_ext ($1.50) past its level before entry, a single tick back across the level resets that level only (kills the 07-13 fake-break B1 that stopped -$350); (2) RE-ENTRY INVALIDATION -- a filled boost closes at market the instant price re-enters the band, not at its $10 SL; (3) TRAPPED-LEG CUT -- the first confirmed fire cuts the trapped opposite anchor leg via the existing close path (additive to the -$630 hard loss stop + kill switch, never a substitute; broker SL stays put if the cut rejects; a confirmed cut replaces R7). New PTRACE: BOOST_CONFIRM_ARMED/FAILED, BOOST_INVALIDATED_REENTRY, TRAPPED_CUT (ARMED/FIRE/FAILED/TRAPPED_CUT mirrored to Discord). selftest -> 308 (new 306-308). v3 flag OFF -> v2 immediate-fire byte-identical. (NB: task requested "v3.8.9" but that shipped earlier and HEAD was already 3.9.0 -- bumped forward to 3.9.1.)
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"