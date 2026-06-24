"""AUREON v3.2.8 Phase 3 — the boost dispatcher: route by the sign of leg_fav.

ONE small router. The pure decision (boosts.plan_boost_event) has already classified
the leg by the sign of its favorable excursion:
  - leg_fav > 0 (winning, +rally arm) -> plan.kind == 'RALLY' -> rally.fire()  (pyramid)
  - leg_fav < 0 (losing,  -rescue arm) -> plan.kind == 'RESCUE' -> rescue.fire() (hedge)
Both fire()s call into boosts_common for the shared steps (placement, FP guard, cap,
journal, telemetry). Live behaviour is unchanged from a routing standpoint -- the same
decisions, cleaner files. The per-tick scan + tick-hold + the rally break-and-hold gate
stay in fills._check_boost_triggers; this only routes the confirmed fire.
"""
import logging

import rally
import rescue

log = logging.getLogger("AUREON")


def fire(self, leg_ticket, leg_shadow, plan):
    """Route a CONFIRMED boost plan to its kind's fire(). plan.kind is derived from
    the sign of leg_fav by boosts.plan_boost_event. Returns whatever the kind's
    fire() returns. A None/unknown plan is a no-op (defensive; the scan never calls
    this with None)."""
    if plan is None:
        return None
    if getattr(plan, 'kind', None) == rally.KIND:
        return rally.fire(self, leg_ticket, leg_shadow, plan)   # winning -> pyramid
    return rescue.fire(self, leg_ticket, leg_shadow, plan)      # losing  -> hedge
