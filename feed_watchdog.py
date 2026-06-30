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
    this object only decides which fire (so the decision is testable without MT5/Discord)."""
    warn: bool = False          # emit a (throttled) warning line this failure
    resubscribe: bool = False   # attempt adapter.resubscribe() now
    alert: bool = False         # fire the hard FEED DOWN Discord alert now
    fails: int = 0              # consecutive failure count (for the log/alert text)
    blind_s: float = 0.0        # seconds since this feed-death episode began
    attempt: int = 0            # re-subscribe attempt number (set when resubscribe True)


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


class FeedWatchdog:
    """Per-LiveTrader feed-death tracker. A single instance lives on the trader and is fed
    one outcome per market-closed probe: on_success() when the probe read a tick, on_failure()
    when it raised 'not subscribed'. One-way within an episode; on_success ends the episode."""

    def __init__(self):
        self.fails = 0                # consecutive failures in the CURRENT episode
        self.attempts = 0             # re-subscribe attempts in the current episode
        self.episode_start_s = None   # monotonic seconds at the first failure of the episode
        self.last_alert_s = None      # monotonic seconds of the last FEED DOWN alert

    def on_success(self) -> bool:
        """A probe SUCCEEDED -- the feed is alive. Reset the episode. Returns True iff this
        ended an in-progress feed-death episode (so the caller can post a one-shot RECOVERED
        line). Idempotent: a success with no episode in flight returns False and no-ops."""
        recovered = self.fails > 0
        self.fails = 0
        self.attempts = 0
        self.episode_start_s = None
        self.last_alert_s = None
        return recovered

    def on_failure(self, cfg, now_s: float) -> FeedAction:
        """A probe FAILED ('not subscribed'). Advance the episode and return the side effects
        the caller must perform. `now_s` is a MONOTONIC seconds clock (live: time.monotonic();
        test: a synthetic counter) so the cooldown is testable without a real clock."""
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
        # Re-subscribe on every n-th consecutive failure (the backoff cadence). The FIRST
        # attempt lands at exactly n failures (T-F1: after n, not before).
        if self.fails >= n and self.fails % n == 0:
            self.attempts += 1
            act.resubscribe = True
            act.attempt = self.attempts
            # After max_tries failed attempts: one alert, then cooldown-gated repeats.
            if self.attempts >= recover_max_tries(cfg):
                cd = alert_cooldown_s(cfg)
                if self.last_alert_s is None or (float(now_s) - float(self.last_alert_s)) >= cd:
                    act.alert = True
                    self.last_alert_s = now_s
        return act
