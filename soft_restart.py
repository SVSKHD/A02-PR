"""AUREON — soft self-update + restart-reconcile (pure decisions, shared).

CORE PRINCIPLE
--------------
Positions live on the BROKER (server-side), not in the bot process. A restart
does NOT close trades -- they keep existing on MT5. The only risk is the bot
returning BLIND: not knowing a position exists, losing its max_fav / lock /
stack / boost state, then double-managing or orphaning it.

SOFT RESTART therefore = (a) never flatten, (b) persist full state before +
rehydrate + reconcile after, (c) be fast enough that no trail/boost decision is
missed. This module is the PURE decision layer (no MT5, no git, no subprocess):
the auto-pull gate, the deploy gate, and the RESUME/ADOPT/FINALIZE reconcile
classifier -- so every branch is trivially testable and shared by live + tests.

Builds on the already-proven rehydrate mechanism (selftest 26: persist=True
rehydrate=True survived_restart=True no_orphan=True); extends it to ALL live
state including open positions and stacks.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

# Pinned constants (spec).
SOFT_RESTART = True
SOFT_RESTART_MAX_GAP_S = 10
PERSIST_OPEN_POSITIONS = True
RECONCILE_ON_BOOT = True
NEVER_FLATTEN_ON_UPDATE = True

# Reconcile actions.
RESUME = "RESUME"      # on broker AND in state -> resume managing (restore state)
ADOPT = "ADOPT"        # on broker, NOT in state -> reconstruct conservative state
FINALIZE = "FINALIZE"  # in state, NOT on broker -> closed during gap -> journal


def should_soft_restart(update_available: bool, mid_anchor: bool,
                        mid_fill: bool, position_open: bool) -> Tuple[bool, str]:
    """The auto-pull gate. An OPEN POSITION does NOT block a soft restart (the
    position lives on the broker and is reconciled on boot). We defer ONLY while
    mid-anchor / mid-fill, where a few seconds of blindness could miss a fill.
    Returns (proceed, reason)."""
    if not update_available:
        return False, "no_update"
    if mid_anchor or mid_fill:
        return False, "defer_mid_anchor"
    return True, "soft_restart"


def should_deploy(selftest_passed: bool) -> Tuple[bool, str]:
    """The deploy gate: a pulled build deploys ONLY if selftest ALL-PASSes;
    otherwise abort and keep the old build (positions untouched either way)."""
    if selftest_passed:
        return True, "selftest_pass"
    return False, "selftest_fail"


def reconcile_action(in_state: bool, on_broker: bool) -> Optional[str]:
    """Classify ONE ticket. A live broker position is NEVER ignored: if it isn't
    in state we ADOPT it. neither -> None (nothing to do)."""
    if on_broker and in_state:
        return RESUME
    if on_broker and not in_state:
        return ADOPT
    if in_state and not on_broker:
        return FINALIZE
    return None


def reconcile(state_tickets: Iterable, broker_tickets: Iterable) -> Tuple[Dict, Dict]:
    """Classify every ticket across state ∪ broker. Returns (actions, summary):
      actions  = {ticket: RESUME|ADOPT|FINALIZE}
      summary  = {resumed, adopted, finalized, orphans}
    orphans MUST be 0 by construction (every on-broker ticket is RESUME or ADOPT);
    a non-zero orphan count is the tripwire (a live position left unmanaged)."""
    s: Set = set(state_tickets)
    b: Set = set(broker_tickets)
    actions: Dict = {}
    for tk in (s | b):
        a = reconcile_action(tk in s, tk in b)
        if a is not None:
            actions[tk] = a
    summary = {
        "resumed": sum(1 for a in actions.values() if a == RESUME),
        "adopted": sum(1 for a in actions.values() if a == ADOPT),
        "finalized": sum(1 for a in actions.values() if a == FINALIZE),
        # an orphan = a live broker ticket that got no managing action.
        "orphans": sum(1 for tk in b if actions.get(tk) not in (RESUME, ADOPT)),
    }
    return actions, summary


def adopt_shadow(broker_pos: Dict) -> Dict:
    """Reconstruct CONSERVATIVE shadow state for an adopted (untracked) live
    position: max_fav = entry (never a ghost peak -> the phantom-lock guard then
    blocks any lock until price genuinely re-reaches a level), lock_level 0, no
    boost/stack assumptions. Never ignores the position."""
    entry = float(broker_pos.get("entry_price", broker_pos.get("price_open", 0.0)))
    return {
        "anchor_label": broker_pos.get("anchor_label", "ADOPTED"),
        "side": broker_pos.get("side", "BUY"),
        "entry_price": entry,
        "current_sl": float(broker_pos.get("sl", broker_pos.get("current_sl", entry))),
        "tp_level": float(broker_pos.get("tp", broker_pos.get("tp_level", entry))),
        "max_fav": entry,            # conservative: no ghost peak
        "lock_level": 0,
        "role": "normal",
        "adopted": True,
    }


def soft_exit_plan(open_tickets: Iterable) -> Dict:
    """The exit plan for a soft restart: NOTHING is closed or modified; every open
    position is simply LEFT OPEN on the broker. Encodes NEVER_FLATTEN_ON_UPDATE."""
    left = list(open_tickets)
    return {"closed": [], "modified": [], "left_open": left}


def gap_seconds(exit_epoch: float, boot_epoch: float) -> float:
    """Downtime between the clean exit and the next boot."""
    return float(boot_epoch) - float(exit_epoch)


def gap_ok(gap_s: float, max_gap_s: float = SOFT_RESTART_MAX_GAP_S) -> bool:
    """True when the restart was quick enough that no decision was missed."""
    return 0.0 <= float(gap_s) < float(max_gap_s)


def snapshot_summary(positions: Dict, pendings: Optional[Dict] = None,
                     boost_events: Optional[Iterable] = None,
                     state_bytes: Optional[int] = None) -> Dict:
    """The pre-restart snapshot receipt (counts only; the full state is persisted
    by the caller's _save_state)."""
    return {
        "n_positions": len(positions or {}),
        "n_pending": len(pendings or {}),
        "n_boost_events": len(list(boost_events or [])),
        "state_bytes": state_bytes,
    }
