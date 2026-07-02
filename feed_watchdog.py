"""AUREON — feed-death watchdog (E-12): re-subscribe + throttled FEED-DOWN alert.

WHY THIS EXISTS
---------------
2026-06-30 ~06:00-10:08 the XAUUSD subscription dropped. `_market_closed_now`'s probe
raised "symbol_info_tick returned None -- symbol not subscribed?" ~13,833 times. The bot
logged a warning EVERY tick, NEVER re-subscribed, and NEVER alerted -- it went fully blind
through the morning monster move (no trail, no Rogue, no anchors). This module is the fix.

PURE state machine (no MT5, no clock, no Discord): the live probe-failure path
(live_trader._market_closed_now) feeds it each consecutive probe outcome plus a monotonic
timestamp and PERFORMS the side effects it returns (warn / resubscribe / alert); selftest
drives the same FeedWatchdog with fixtures, so live and tests honor one rule (import-path
identity, the repo idiom shared by tick_hold / boosts / rogue).

DEFAULT-SAFE
------------
`feed_watchdog_enabled=False` -> on_failure returns warn=True every call and NEVER
resubscribe/alert. The caller then emits the exact pre-watchdog warning line, so behavior
is BYTE-IDENTICAL to today. Enabled (default) -> the warning is throttled (one episode-start
line + a periodic count every feed_recover_after_fails failures), a re-subscribe is attempted
on every feed_recover_after_fails-th consecutive failure (the backoff cadence), and after
feed_recover_max_tries failed attempts a single FEED DOWN alert fires, repeating only after
feed_alert_cooldown_min.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FeedAction:
    """What the caller must DO for one failed probe. All side effects are the caller's;
    this object only decides which fire (so the decision is testable without MT5/Discord).

    Fix 4 (E-12) escalation ladder: L1 re-subscribe -> L2 full MT5 reinit -> L3 controlled
    self-restart. Exactly one of resubscribe / reinit / self_restart fires per failure."""
    warn: bool = False          # emit a (throttled) warning line this failure
    resubscribe: bool = False   # L1: attempt adapter.resubscribe() now
    alert: bool = False         # fire the hard FEED DOWN Discord alert now
    reinit: bool = False        # L2: full in-process MT5 reinit now
    self_restart: bool = False  # L3: persist state + sys.exit(42) (launcher relaunches)
    fails: int = 0              # consecutive failure count (for the log/alert text)
    blind_s: float = 0.0        # seconds since this feed-death episode began
    attempt: int = 0            # L1 re-subscribe attempt number (set when resubscribe True)
    reinit_attempt: int = 0     # L2 reinit attempt number (set when reinit True)


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "feed_watchdog_enabled", True))


def recover_after_fails(cfg) -> int:
    """Consecutive 'not subscribed' failures before a re-subscribe is attempted (>=1)."""
    return max(1, int(getattr(cfg, "feed_recover_after_fails", 30)))


def recover_max_tries(cfg) -> int:
    """Re-subscribe attempts that must fail before the first FEED DOWN alert (>=1)."""
    return max(1, int(getattr(cfg, "feed_recover_max_tries", 5)))


def alert_cooldown_s(cfg) -> float:
    """Minimum seconds between FEED DOWN alerts once the max-tries threshold is passed."""
    return max(0.0, float(getattr(cfg, "feed_alert_cooldown_min", 5.0)) * 60.0)


def reinit_blind_s(cfg) -> float:
    """L2 trigger (E-12): seconds blind after which a full MT5 reinit is escalated even if
    the re-subscribe attempts have not all been spent yet ('blind > 3 min')."""
    return max(0.0, float(getattr(cfg, "feed_reinit_blind_min", 3.0)) * 60.0)


def reinit_max_tries(cfg) -> int:
    """L2 reinit attempts that must fail before the L3 self-restart is escalated (>=1)."""
    return max(1, int(getattr(cfg, "feed_reinit_max_tries", 2)))


class FeedWatchdog:
    """Per-LiveTrader feed-death tracker. A single instance lives on the trader and is fed
    one outcome per market-closed probe: on_success() when the probe read a tick, on_failure()
    when it raised 'not subscribed'. One-way within an episode; on_success ends the episode."""

    def __init__(self):
        self.fails = 0                # consecutive failures in the CURRENT episode
        self.attempts = 0             # L1 re-subscribe attempts in the current episode
        self.reinit_attempts = 0      # L2 full-reinit attempts in the current episode
        self.restarted = False        # L3 self-restart requested (one-shot latch)
        self.episode_start_s = None   # monotonic seconds at the first failure of the episode
        self.last_alert_s = None      # monotonic seconds of the last FEED DOWN alert

    def on_success(self) -> bool:
        """A probe SUCCEEDED -- the feed is alive. Reset the episode. Returns True iff this
        ended an in-progress feed-death episode (so the caller can post a one-shot RECOVERED
        line). Idempotent: a success with no episode in flight returns False and no-ops."""
        recovered = self.fails > 0
        self.fails = 0
        self.attempts = 0
        self.reinit_attempts = 0
        self.restarted = False
        self.episode_start_s = None
        self.last_alert_s = None
        return recovered

    def on_failure(self, cfg, now_s: float) -> FeedAction:
        """A probe FAILED ('not subscribed'). Advance the episode and return the side effects
        the caller must perform. `now_s` is a MONOTONIC seconds clock (live: time.monotonic();
        test: a synthetic counter) so the cooldown is testable without a real clock.

        Fix 4 (E-12) escalation ladder, one step per n-th ('cadence') failure:
          L1 RE-SUBSCRIBE -- up to recover_max_tries attempts. The counter STOPS at the cap
             (bug fix: it used to run to 'attempt 6/5'); one FEED DOWN alert fires at the cap.
          L2 FULL REINIT  -- once the L1 attempts are spent OR blind >= feed_reinit_blind_min,
             up to feed_reinit_max_tries full MT5 reinits.
          L3 SELF-RESTART -- once the L2 reinits are spent, request one controlled restart."""
        self.fails += 1
        if self.episode_start_s is None:
            self.episode_start_s = now_s
        try:
            blind_s = max(0.0, float(now_s) - float(self.episode_start_s))
        except (TypeError, ValueError):
            blind_s = 0.0

        # DISABLED -> warn every failure, never act: byte-identical to the pre-watchdog path.
        if not enabled(cfg):
            return FeedAction(warn=True, fails=self.fails, blind_s=blind_s)

        n = recover_after_fails(cfg)
        # Throttle: episode-start line + a periodic count every n failures (so 1000 failures
        # produce ~1 + floor(1000/n) lines, never 1000).
        warn = (self.fails == 1) or (self.fails % n == 0)
        act = FeedAction(warn=warn, fails=self.fails, blind_s=blind_s)

        on_cadence = (self.fails >= n and self.fails % n == 0)
        max_tries = recover_max_tries(cfg)
        resub_exhausted = self.attempts >= max_tries
        blind_reinit = blind_s >= reinit_blind_s(cfg)

        # LEVEL 1 -- bounded re-subscribe. STOP at max_tries (the 'attempt 6/5' bug fix); skip
        # straight to L2 once blind has already crossed the reinit threshold.
        if not resub_exhausted and not blind_reinit:
            if on_cadence:
                self.attempts += 1
                act.resubscribe = True
                act.attempt = self.attempts
                if self.attempts >= max_tries:            # at the cap: one FEED DOWN alert
                    cd = alert_cooldown_s(cfg)
                    if self.last_alert_s is None or (float(now_s) - float(self.last_alert_s)) >= cd:
                        act.alert = True
                        self.last_alert_s = now_s
            return act

        # LEVEL 2 -- full in-process MT5 reinit (bounded) after L1 spent OR blind > threshold.
        if self.reinit_attempts < reinit_max_tries(cfg):
            if on_cadence:
                self.reinit_attempts += 1
                act.reinit = True
                act.reinit_attempt = self.reinit_attempts
            return act

        # LEVEL 3 -- controlled self-restart, one-shot (the caller gates on the market-closed
        # guard + feed_selfrestart_enabled before actually exiting).
        if not self.restarted:
            if on_cadence:
                self.restarted = True
                act.self_restart = True
        return act
