"""AUREON v3.2.8 Phase 2 — rescue: the LOSING-leg hedge. UNCHANGED from v3.2.7.

A leg that runs -arm AGAINST itself fires the opposite-direction sibling that becomes
the winner after the whipsaw (the 3-leg model). Rescue keeps EXACTLY its v3.2.7
behaviour -- this module only RELOCATES it; nothing here changes a number or a branch.
Verified working live on A1 2026-06-24 (net -10.85, boost +619.15). Leave it alone.

  - event arm  : boost_trigger_dollars = $10  (the -$10 trigger; UNCHANGED)
  - trail arm  : boost_trail_arm_fav   = $8   (breath-gap trail goes live; UNCHANGED)
  - lock floor : boost_lock_floor      = $8   (one-way locked-profit floor; UNCHANGED)
  - trail gap  : boost_trail_gap_dollars = $3.50 (UNCHANGED)
  - free-fire-on-commit : rescue bypasses the break-and-hold gate (rescue_bypass_
                          break_and_hold, default True) -- a recovery leg is not
                          suppressed by an unconfirmed break.
  - tick-hold >= 3 : the -$10 cross must HOLD hold_ticks ticks before firing (the
                     gate lives in the per-tick scan; tick_hold is the shared engine).

The shared placement / FP guard / cap / journal live in boosts_common; the pure
trigger decision is the canonical boosts.plan_boost_event; the breath-gap trail
engine is strategy._update_boost_on_bar (which reads the trail_* accessors below for
RESCUE boosts -- the default). Kept import-light so strategy can pull the trail
accessors without dragging in the order-placement stack.
"""
import logging

log = logging.getLogger("AUREON")

KIND = "RESCUE"


# --- the v3.2.7 rescue numbers, owned here (read from the unchanged BOOST_* keys) -
def event_arm(cfg):
    """The adverse move ($) a losing leg must make before the rescue hedge arms
    ($10 boost_trigger_dollars; UNCHANGED from v3.2.7)."""
    return float(getattr(cfg, 'boost_trigger_dollars', 10.0))


def trail_arm(cfg):
    """Peak fav ($) before a rescue boost's breath-gap trail goes live ($8,
    boost_trail_arm_fav; UNCHANGED). Below it: the $10 hard backstop only."""
    return float(getattr(cfg, 'boost_trail_arm_fav', 8.0))


def lock_floor(cfg):
    """Once armed, a rescue boost's locked profit ($) never falls below this ($8,
    boost_lock_floor; UNCHANGED)."""
    return float(getattr(cfg, 'boost_lock_floor', 8.0))


def trail_gap(cfg):
    """Rescue breath-gap trail gap ($3.50, boost_trail_gap_dollars; UNCHANGED)."""
    return float(getattr(cfg, 'boost_trail_gap_dollars', 3.50))


def bypass_break_and_hold(cfg):
    """v3.2.7 free-fire-on-commit: True (default) when a rescue fires WITHOUT waiting
    for a confirmed break. False restores the legacy v3.2.6 behaviour (gate both
    kinds on break-and-hold)."""
    return bool(getattr(cfg, 'rescue_bypass_break_and_hold', True))


# --- the fire entrypoint the dispatcher routes a LOSING leg to --------------------
def fire(self, leg_ticket, leg_shadow, plan):
    """Hedge the loser: place the RESCUE boost fleet (OPPOSITE the leg) via the shared
    placement. Routed here by the dispatcher when leg_fav < 0. Byte-identical to
    v3.2.7 (the placement loop is the same shared code rally uses)."""
    import boosts_common
    return boosts_common.place_fleet(self, leg_ticket, leg_shadow, plan)
