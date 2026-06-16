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
  3.0.1  FIX the 0-for-7 boost root cause: MetaTrader5 silently rejects an order
         `comment` longer than 31 chars with (-2, 'Invalid "comment" argument')
         (Jun-15 A3: AUREONv2_A3_1340_Overlap_SELL_BOOST1 = 34). All order comments
         now route through mt5_comment() (hard <=31) and use a compact scheme
         (AUR_A3_S_B1, AUR_A3_BUY). Boost Telegram messages escape dynamic values
         (md_escape) + a plain-text failover so a Markdown 400 never drops a message;
         the LiveTrader init banner reads version.__version__ (was a stale hardcoded
         2.5.3). Unblocks the crash-branch boost upside.
  3.0.3  ON-DEMAND SELF-TEST harness (selftest.py + `python bot.py selftest`):
         exercises the ENTIRE placement + rescue/boost path against the live demo
         broker with vol_min throwaway orders and reports PASS/FAIL per step to
         console + Telegram -- connection, tick freshness, comment<=31 guard, real
         stop placement (cancelled), the MARKET/boost path (the 0-for-7 call,
         closed), SL/TP modify, rescue classification (twin-open=rescue /
         twin-closed=normal, pure logic), full rescue dry-run (real boosts -> 10009
         -> closed), and Telegram parse-safety (no unclosed-entity 400). Hard
         safety: runs ONLY via the CLI (never the live loop / a timer), refuses if
         any position/pending is open ("run when flat"), demo-account guard on the
         market steps (--force overrides), and a try/finally that closes/cancels
         every throwaway order even if a step raises. Proves the boost fleet places
         at 10009 in ~2 minutes instead of waiting for a real live rescue.
         Monday-only A1 -> 03:30 broker (6 AM IST), was 03:00: the quiet-feed
         cold-wake risk is worst in the first hours of the week, so a later Monday
         A1 lands when the feed is reliably live. ONE source of truth confirmed --
         _resolved_anchor_hm (used by both the anchor-due check and the readiness
         line); no rogue override existed (the live "03:00" was the prior
         (3,0) value resolving correctly on a Monday). cfg.monday_a1_override
         (3,30); None disables (pure 02:30). A2/A3/A4 + A1 Tue-Fri unchanged; label
         "A1_02h_Asia" stable; weekday tested in BROKER-date terms. Test hook
         AUREON_TEST_FORCE_MONDAY_A1=1 forces the override on any weekday (TEST
         ONLY, default OFF, shown in a 'TEST MODE ACTIVE' banner line).
  3.0.4  Two independent, no-strategy-change features.
         (1) Firebase backfill VERIFIER (verify_firebase.py + `python bot.py
         verifyfb`): read-only by default -- lists every aureon_forex doc with a
         one-line summary (net / trade count / balance), cross-checks the local
         journal CSVs and names MISSING trading days, and suggests the backfill
         command. `--backfill <YYYY-MM-DD>` re-writes ONE day idempotently (clean
         .set() overwrite from the CSV); it NEVER auto-writes. Fail-safe: an
         unreachable Firestore warns and exits 0, so it can never touch trading.
         A 10th selftest check covers ts_header. (2) TIMESTAMPED Telegram: one
         helper ts_header() in telemetry is the SINGLE source for every outbound
         message's timestamp, prepended in _send_telegram so ALL alert types
         (anchor/fill/close/rescue/boost/TSTOP/EOD/verifyfb) inherit it; format
         '🕐 5:00 AM IST (server 02:30 · IST 05:00) — Tue Jun 16', server (UTC+3)
         and IST (broker+2:30) derived from ONE captured instant so they can't
         drift. No trading/spec behavior changed.
  3.0.5  ANCHOR LATE-RETRY + clear anchor timestamps (recovery + telemetry only;
         straddle geometry / ladder / rescue / hold / kill switch / schedule all
         frozen). (1) If an anchor did not PLACE by its scheduled time -- for ANY
         reason (quiet feed, stale tick, wake, warmup fail, transient rc) -- it is
         re-attempted on the stale-retry cadence for cfg.anchor_late_window_min
         (=10) minutes, then gives up with a loud ❌ ANCHOR MISSED (scheduled time,
         reason if known, minutes waited) -- the alert that ends the silent misses.
         The late straddle RE-CAPTURES the anchor price at the moment it places
         (existing current-price anchoring), geometry unchanged. processed_anchors_
         today is now the PLACED set (success gate) so one placement per anchor per
         day is guaranteed even with retries; hard stops (kill switch / EOD /
         weekend / paused / window-elapsed) are enforced by the tick loop BEFORE
         re-attempt and never overridden. A late fire posts ⏰ LATE ANCHOR (WARN).
         (2) Every anchor message (placement / LATE / MISSED / fill / close) shows
         BOTH scheduled and actual times (server + IST) via the v3.0.4 ts_header
         derivation (new telemetry.anchor_time_block; single source, no hand-
         formatting); on-time anchors show matching times and no LATE tag. selftest
         gains an 11th check (mocked clock: late placement fires within the window
         with a re-captured price; clean give-up after the window).
"""

__version__ = "3.0.5"
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"