"""
AUREON — single source of truth for the version.

Bump __version__ HERE and nowhere else. Every module imports it; the Telegram
startup banner, logs, and journal all read from this file, so the version you
see in Telegram is by construction the version that is running.

History (one line per behavioral change):
  2.5.x  legacy: trail from bar one (freeze silently dead -- tz bug)
  2.7    FIX fill_time/bar_time clock skew -> 45m hold actually works;
         fallback inversion fix; hold-duration audit + FREEZE BREACH alarm;
         config: freeze 45, gap 1.0, arm 1.5, no_oco default ON
  2.7.1  TSTOP: cut legs that never reached +$1, at hold expiry, at market
  2.8    profit ladder during hold (3 -> BE, 10 -> +8)
  2.9    role-aware exits: No-OCO 2nd legs run as RESCUE (no small locks)
  2.9.1  top tier follows peak - $2 (floor +$8)
  2.9.2  BE tier 3.00 -> 2.50; trail gap 1.00 -> 2.00 (one rule: in profit,
         never more than $2 behind the peak)
  2.9.3  A1/A2 anchors disabled -- honest replay shows them net -$7.1k/29d
         and their losses kill-switch-block A3/A4 (the only green anchors)
  2.9.4  all 4 anchors re-enabled for live forward trial on demo; verdict
         per anchor from the live journal after 2 weeks (backtest set aside)
  2.9.5  SL-RESCUE BOOST (Hithesh): on rescue fill, +2 market trades in the
         rescue direction, tight $6 SL each -- covers the twins remaining
         $8-to-SL on crash days (~+$560), capped -$420 on whipsaw days
  2.9.6  trail arm 1.50 -> 2.50: sub-$2.5 peaks keep full SL post-hold
         (kills the $0 scratch-at-entry exits; Jun-11 A3 lesson)
  2.9.7  trail gap actually set 1.00 -> 2.00 (v2.9.2 manual edit was never
         applied; caught via the live banner config receipt on Jun-11 A4)
  2.9.8  Jun-12 A1 forensics: (1) RESCUE now detected STRUCTURALLY (2nd fill
         of a live anchor), flag is only a hint -- flag-loss no longer kills
         the fleet; pendings + rescue flag persisted/rehydrated across
         restarts; (2) STOP-THROUGH: ladder stop breached intrabar -> close
         at market (old clamp pinned SL to bid: the BUY -$2.20 'Trail');
         (3) exit classifier names the rule (BE/LOCK4/TIER/Trail/SL/TP +
         slip) from the bot's own current_sl -- kills false FREEZE BREACH on
         ladder exits; (4) boost orders: exceptions Telegram-visible, rc=None
         shows last_error, 10030 retries FOK (Jun-11 silent-boost mystery);
         (5) journal-only no-hold trail counterfactual per leg -- decides
         hold-vs-no-hold from live data
  2.9.9  FIX A stale rescue flag: a 2nd fill is a genuine RESCUE only if its
         twin is STILL OPEN at the moment of the 2nd fill (Jun-12 A4: SELL
         banked +$477 and closed, BUY filled an hour later, inherited the
         stale rescue_on_fill flag and fired 2 boosts with no twin to rescue;
         A2 same setup fired nothing -> nondeterministic). Twin-open is tested
         structurally (sibling_ticket or any non-boost open leg of the anchor);
         a stale flag with a closed twin runs as a normal breakout leg, no
         boosts. FIX B boost diagnostics: every exit of the boost path now
         self-reports (attempting / exception / result=None+last_error /
         rejected rc+name+comment / filled price+ticket) -- the 0-for-6 silent
         boosts get a full trace on the next live event (no param change)
  3.0.0  STRUCTURAL SPLIT (behavior-frozen): live_trader.py (~2.4k) and bot.py
         (~1k) broken into 13 modules -- utils, config, strategy (pure Position
         + update_position_on_bar), mt5_adapter (sole MetaTrader5 importer),
         backtest, state, risk, anchors, fills, trails, journal -- plus the slim
         LiveTrader orchestrator and a CLI/backtest-only bot.py (run_live moved
         to live_trader). Moved code is byte-identical (proof in REFACTOR_NOTES);
         methods are bound onto LiveTrader so every call site, state.json key,
         Telegram string and 19-col journal schema are unchanged. Startup banner
         prints a MODULE RECEIPT (rule #6). Firebase EOD journal wired in
         (journal.py, fail-safe). Weekend self-sleep + Monday auto-resume:
         wait_until_market_open() factored from startup into the main loop, so
         the process deep-sleeps over the weekend and wakes itself Monday (offset
         re-detect on wake, heartbeat kept alive, state saved before sleeping).
         Weekend `status` (follow-up): status.json is now refreshed inside the
         weekend sleep loop, and the sleep-state reply carries last-trading-day
         per-anchor P&L + week-to-date totals read from the local
         trades_<YYYY-MM>.csv (journal.summarize_recent, fail-safe -> "stats
         unavailable"). No trading-behavior change; version held at 3.0.0.
         Monday-wake + A1 hardening (defense in depth, no strategy change): the
         broker offset is VALIDATED on wake/startup against
         cfg.EXPECTED_BROKER_OFFSET_HOURS and A1 is BLOCKED (loud alert) if it
         mismatches after retries -- eliminates the Jun-8 silent miss (0h
         misdetect -> wrong M5 window -> no bars -> no trade, silently). Anchors
         refuse to place on an unvalidated offset; the M5 anchor fetch retries and
         a no-bars result is alerted, never swallowed; A1's resting stops are
         confirmed at the broker (missing leg re-placed once, else INCOMPLETE
         alert); a WAKE FAILSAFE alarm fires if still asleep past the expected
         weekly open; a "Ready" receipt posts on every startup/wake. 3.0.0 held.
         Monday-only A1 shift (cold-start cushion, no strategy change): on Mondays
         A1 fires at 03:00 broker (05:30 IST) instead of 02:30 -- ~3h after the
         week's open, by which point the offset is settled and M5 history exists,
         so the Monday cold-start "no bars" miss can't happen. Tue-Fri A1 and
         A2/A3/A4 unchanged; label "A1_02h_Asia" stable; cfg.monday_a1_override
         (default (3,0), None disables). Complements the wake guards, not a
         replacement. 3.0.0 held.
         Offset detect: stale-tick consistency fallback for the quiet Monday wake
         (no schedule change to the detector's contract). The live-feed detector
         needs ADVANCING ticks, which gold lacks pre-session Monday -> every quiet
         wake timed out -> A1 blocked. New Tier 2 validates a single stale tick
         against the constant cfg.EXPECTED_BROKER_OFFSET_HOURS (must round to
         expected AND be within STALE_TOL_S of utc+expected) -> confirms +3h when
         the feed is quiet, yet still REJECTS a wrong offset (Jun-8 0h stays
         blocked). Belt-and-suspenders with the Monday A1 shift above. 3.0.0 held.
         Fix 1 (2026-06-15 missed-anchor incident): stale-tick RETRY at placement.
         A 76s tick (16s over threshold) skipped two anchors today incl. a clean
         ~$25 gold move. Placement now polls for a fresh tick (stale_retry_poll_s)
         up to stale_retry_window_s before skipping, nudging/reconnecting the feed
         mid-window; kill switch / pause / EOD abort the wait. Skips only if stale
         the whole window. No anchor-timing change. 3.0.0 held.
         Auto-deploy (INFRA, default OFF, AUTODEPLOY_ENABLED): the watchdog polls
         master, pulls + validates new code off-tree (py_compile + import), and
         restarts the bot ONLY when the book is flat or at EOD (never mid-trade);
         ff-only merge keeps git-ignored .env/state/firebase_key/logs intact. The
         bot now publishes flat/eod_done in status.json for the gate. 3.0.0 held.
"""

__version__ = "3.0.0"
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"