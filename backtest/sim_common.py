"""AUREON offline simulator — shared scaffolding (Part 1B).

The mandatory GATE-NOT-RUN header, the sim/ output tree, and a HARD guard that
nothing the simulator writes ever lands under run/. Imported by simulator.py and
sim_report.py.

!!! GATE-NOT-RUN — the simulator's baseline has NOT been reproduced against the
!!! MT5 deal-export truth. No number it emits is trustworthy until the gate
!!! passes on REAL ticks. See backtest/SIMULATOR_STATUS.md.
"""
from __future__ import annotations

import os

# Every artifact (report, CSV, chart, console banner) MUST carry this header --
# not just the filename. Removed ONLY when the gate passes against MT5 truth.
GATE_NOT_RUN_HEADER_LINES = (
    "!!! GATE-NOT-RUN — baseline never reproduced against MT5 truth.",
    "!!! No number in this file is trustworthy.",
)


def gate_header(comment_prefix: str = "") -> str:
    """The two-line header, each line optionally prefixed (e.g. '# ' for CSV,
    '' for markdown/console)."""
    return "\n".join(f"{comment_prefix}{ln}" for ln in GATE_NOT_RUN_HEADER_LINES)


def gate_banner_md() -> str:
    """Markdown blockquote banner for the top of a report."""
    return "> " + "\n> ".join(GATE_NOT_RUN_HEADER_LINES) + "\n"


# --------------------------------------------------------------------------- #
# output tree: sim/reports/<run-id>/... — NEVER run/
# --------------------------------------------------------------------------- #
def sim_root() -> str:
    """Repo-root/sim (a SEPARATE tree from run/). Overridable via AUREON_SIM_DIR
    for tests."""
    env = os.environ.get("AUREON_SIM_DIR")
    if env:
        return env
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "sim")


def run_output_dir(run_id: str) -> str:
    return os.path.join(sim_root(), "reports", run_id)


def assert_not_run_dir(path: str) -> None:
    """HARD guard: raise if `path` resolves anywhere under a run/ directory. The
    simulator must never write engine state / ledgers into the live run tree.
    Called on every sim file open."""
    ap = os.path.abspath(path)
    parts = ap.split(os.sep)
    # reject any path whose components include a 'run' dir (…/run/… or trailing /run)
    if "run" in parts:
        raise AssertionError(
            f"simulator refused to write under a run/ directory: {ap} "
            "(the sim writes ONLY under sim/; run/ is the LIVE tree)")


def open_sim_file(path: str, mode: str = "w", **kw):
    """open() wrapper that (1) refuses any run/ path and (2) makes the parent dir.
    Every simulator write goes through here."""
    assert_not_run_dir(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    kw.setdefault("encoding", "utf-8")
    return open(path, mode, **kw)
