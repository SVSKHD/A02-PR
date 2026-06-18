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
  3.0.6  RESCUE FLEET-EVENT LOGGER (observer only; rescue/boost mechanics, sizing,
         geometry all FROZEN) + EOD balance fix + requirements. (1) Every $10 fleet
         trigger (leg -$10 + twin open -> RESCUE + 2 BOOSTS) is recorded as one
         event: trigger/rescue legs, both boosts (ticket/fill/rc/≤31-char comment,
         boosts_placed_ok), and on close the fleet net $ + branch CRASH_WIN /
         WHIPSAW_LOSS / SCRATCH (|net|<$50). Rows append to run/rescue_events.csv
         and mirror to Firestore aureon_forex/{date}/rescue_events/{event_id}; a
         📊 FLEET EVENT telegram posts on close with the running crash/whipsaw/
         scratch tally. New `python bot.py rescuestats` prints the tally + per-event
         table (read-only). All hooks in fills.py are pure observation wrapped so a
         logging error can never reach the engine. (2) EOD Firebase docs now store
         close_balance + equity from MT5 account_info (fix-forward; old docs not
         backfilled) so verifyfb shows real balance instead of `bal n/a`. (3)
         requirements firmed: firebase-admin + python-dotenv required so a clean VPS
         rebuild can't silently lose journaling. selftest gains a 12th check.
  3.0.7  READ-ONLY measurement tool, ZERO engine change (no live trading / ladder /
         exit behavior touched; diff is bescratch.py + bot.py subcommand only).
         `python bot.py bescratchscan` quantifies how often the +$2.5->breakeven
         ladder rung scratches a trade flat then the trend continues, and how much
         that costs, BEFORE deciding to loosen it. Reads the live journal
         (trades_*.csv -> Firestore fallback) + the bot's own per-second price_log
         (-> M1 bars; --m1csv fallback); per trade classifies BE-scratch (BE/near-BE
         exit with the +$2.5 rung armed), computes "left on table" over a stated
         lookforward (entry+45m hold +30m), splits continued-in-favor vs reversed
         (BE-saved), per-anchor A1-A4 breakdown, and a counterfactual rung grid
         [+2.5/+3.5/+4/+5] replayed through a faithful mirror of
         strategy.update_position_on_bar (parity-tested at +2.5) reporting net P&L /
         scratches avoided / extra SL hits / runners saved, ending in a data-driven
         verdict. Insufficient price history is marked insufficient_data, never
         guessed. No Firestore writes, no config change, no order placement.
         + FIX silent fill/close Telegram alerts (REGRESSION from the v3.0.4
         ts_header refactor): fills/closes executed on MT5 but their alerts
         vanished with nothing logged. (A) ts_header() now NEVER raises -- on any
         internal error it falls back to a plain UTC stamp and continues, so a bad
         timestamp can't block a message. (B) the telegram send wrapper logs every
         failure at WARNING WITH the message body (no more silent drops), and a
         rate-limit no longer throttles must-see events: fills/closes send with
         important=True (a fill landing seconds after placement used to be dropped
         as a duplicate INFO). (C) fill/close bodies are built by pure, never-
         raising formatters (format_fill_alert / format_close_alert) that degrade
         gracefully when slip/held/open_time/price/pnl are missing; a detected
         close with no history deal yet now alerts degraded instead of vanishing.
         selftest gains 4 checks (fill-alert, close-alert, ts fallback, BE rung).
         + LOOSEN the NORMAL-leg BE ladder rung +$2.5 -> +$5.0 (stop stays at
         entry). The +$2.5 arm scratched trend trades to $0 on ordinary gold noise
         (~5 scratches in 11 days, A2/A4). Single rung change: +$6->+$4, +$10->
         peak-$2 (floor +$8), the RESCUE +$10-only ladder, SL/TP, hold, TSTOP and
         the trail (arm $2.50 / gap $2.00) are all UNCHANGED. Counterfactual
         unmeasurable pre-2026-06-16; judgment-call loosening, re-evaluate vs
         price_log in ~2 weeks. Banner ladder now reads `5>BE | 6>+4 | 10>peak-2`.
         + HOLD-GATE the breakeven-to-entry stop move: it must NOT engage inside
         the 45m hold (live 2026-06-16: A2/A3 hit +$5 fav early, pulled back and
         BE-scratched to $0 at 6.2m/2.8m held -- the disease is the TIMING, not the
         threshold). The BE-to-entry rung now also requires hold expiry; the higher
         protective locks (+$6->+$4, +$10->peak-2) and hard SL/TP stay active
         inside the hold. selftest gains a 17th check (in-hold +$5 stays put, +$6
         lock still fires in-hold, +$5 post-hold engages, +$7 in-hold locks +$4 but
         does NOT move to entry).
  3.0.8  TELEGRAM REACHABILITY (alerting/infra only; NO trading/exit/ladder change).
         The VPS ISP DNS-poisons api.telegram.org (system resolver returns a
         sinkhole IP) so every send/poll timed out, flooded the log and stalled
         cycles. (1) DNS-PIN (telegram_net): all Telegram HTTP (telemetry
         sendMessage + watchdog getUpdates) now connects to a known-good IP past
         the poisoned resolver -- Cloudflare DoH (1.1.1.1 literal) first, then a
         configurable pinned-IP list (default 149.154.166.110), rotating on
         failure and falling back to the system resolver so it self-heals. The
         request URL/SNI/Host stay api.telegram.org so TLS verification is
         UNCHANGED (never verify=False). Toggle telegram_dns_pin_enabled (default
         ON); loud startup line `Telegram DNS-pin ON -> <ip>`. (2) BACKOFF: connect
         timeout cut 10s/35s -> 5s; on a failure streak the poll/retry interval
         grows 30->60->120->cap 300s and resets on first success; log flood
         collapsed to one warning + a periodic summary (FailureStreak). Sends/polls
         stay off the trading path (telemetry worker thread / watchdog process), so
         an unreachable Telegram can never block a fill. selftest gains an 18th
         check (pin resolves a Telegram-range IP; TLS verification stays ON). NOTE:
         if alerts still fail after the pin the ISP is IP-blocking too -> VPS VPN.
  3.0.9  Boost SL tune + Telegram session-rebuild (NO straddle/hold/ladder/normal-
         leg change). (1) BOOST SL $6 -> $10 (config boost_sl_dollars, was the
         hardcoded-ish rescue_boost_sl=6). First live fleet (2026-06-17 A1)
         whipsawed: a $6 stop was tagged by a $6 dip, both boosts died, THEN price
         ran +$8 and the rescue leg rode it alone (-$406.70). A $10 stop sits below
         that dip so the boosts survive and ride too (today's replay ~+$465).
         Per-pair whipsaw cap -$420 -> -$700; rescue alert text updated. Validated
         n=1 only -- $10 dies on dips deeper than $10 and loses more on a true
         crash; a tunable bet pending rescuestats (which still classifies crash vs
         whipsaw + the no-boost counterfactual). ONLY the boost SL changed. (2)
         TELEGRAM SESSION-REBUILD (operator: "works on restart, then jams" = stale
         pinned socket, not necessarily a hard block): rebuild the connection
         (re-resolve DoH + re-pin + fresh socket) automatically on startup, each
         wake, after a failure streak, and every telegram_session_refresh_min (15)
         minutes; a failed send retries ONCE on a brand-new fresh-resolved
         connection. (3) MORNING REFRESH (telegram_morning_refresh, default ON):
         at first readiness each broker day, rebuild + send one compact 🌅 status
         (balance, anchors, pinned IP). (4) CRITICAL QUEUE: fills/closes/rescue/
         boost/EOD are tagged critical; if a send fails the rendered body (original
         ts_header) is queued (cap 50) and flushed newest-first the instant any
         connection succeeds -- the operator never needs MT5 to learn a fill/close
         happened. Steady-state backoff/quiet-summaries unchanged; all of this
         stays off the trading path. selftest gains a 19th check (boost SL =
         boost_sl_dollars). HONEST LIMIT: a HARD ISP IP-block defeats even fresh
         connects -> only a VPS VPN/proxy fixes it; the queue still delivers the
         moment any connection works.
  3.1.0  DISCORD replaces Telegram as the sole alert + command channel (Telegram
         is hard-IP-blocked at the VPS ISP; discord.com is reachable). Trading/
         exit/ladder logic untouched -- alerting/control layer only. (1) RICH EMBED
         CARDS: discord_cards.py builds one color-coded card per event (anchor/
         fill/close/rescue/boost/fleet/EOD/heartbeat/status/connect/intent), with a
         fielded grid + ts_header footer, within Discord's embed limits. Colors:
         green TP/win/CRASH_WIN, red SL/loss/WHIPSAW/kill, amber BE/scratch/locks,
         blue anchor/fill, orange rescue/boost, grey heartbeat/status/banner. (2)
         discord_client.py SENDS via the REST API (requests; works with NO
         discord.py) and optionally runs a gateway (discord.py) for COMMANDS, off
         the trading path. Startup intent self-check warns LOUDLY if Message
         Content Intent is OFF (alerts work, commands won't); connect card on first
         gateway connect. (3) DEDUP: critical events by event key (ticket+type) so
         no double-post across reconnect/flush; general messages by content hash in
         a 60s bucket. (4) CRITICAL QUEUE (cap 50) flushes newest-first on next
         success, dedup-checked. (5) HEARTBEAT card every discord_heartbeat_min
         (60), dedup-aware. (6) COMMANDS port the watchdog set (status/help/
         restart/stop/flatten/pause/resume/today), restricted to DISCORD_CHANNEL_ID
         + optional DISCORD_ALLOWED_USER_IDS. (7) alert_channels default
         ["discord"]; Telegram disabled by default and removed from the selftest
         hard-gate; no Telegram failure can affect Discord. selftest splits alert
         checks: card-build = PASS on correctness, reachability/gateway = WARN on
         network. .env: DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, optional
         DISCORD_ALLOWED_USER_IDS / DISCORD_WEBHOOK_URL.
  3.1.1  FIX selftest fleet-logger stub crash + clean Discord card layout (no
         trading/live-alerting change). (1) The selftest mock tele.send still had
         the old (text, severity) signature and raised TypeError on the v3.1.0
         important=/critical=/card=/event_key= kwargs; the stub now mirrors the
         real signature and swallows any future kwargs via **k (every injected
         stub sender hardened). (2) Discord cards are now clean field-GRID embeds:
         short emoji title (📤 A2 BUY · BE), AUREON · {anchor} author line, inline
         fields (Entry|Exit|P&L / Held|Reason|Day total), P&L in its own field
         with sign + $ and the card color carrying win/loss at a glance, a clean
         one-line ts footer (🕐 12:30 PM IST · server 10:00 · Wed Jun 17), and all
         Telegram MarkdownV2 artifacts stripped. requirements pin discord.py +
         firebase-admin so a clean VPS rebuild can't lose Discord/Firebase.
  3.1.2  Tiered/clean cards for ALL types + two label fixes (cosmetic; NO trading
         change). (1) Status and Startup were dense text blobs routed as generic
         cards; they are now Title + bold-field-name GRIDs like the rest (status:
         account/P&L/positions; startup: lot/kill/hold/ladder/boost/alerts).
         Hierarchy = TITLE (headline) > field NAMES (labels) > field VALUES (data)
         > footer (timestamp), the only "bigger text" Discord allows. (2) FIX
         command source: /restart & /stop said "via Telegram" even from Discord —
         now "via {source}" (Discord gateway -> Discord, Telegram poll ->
         Telegram). (3) FIX anchor underscores eaten ('A1_02h_Asia' -> 'A102hAsia'
         italic): field VALUES + descriptions now backslash-escape markdown
         specials so identifiers render verbatim; the generic-card cleaner no
         longer DELETES underscores (it dropped them in 3.1.1). version -> 3.1.2.
  3.1.3  Three changes, one deploy (alerting + rescue/boost; straddle geometry,
         hold, kill-switch, normal-leg ladder all FROZEN). (1) FULL TELEGRAM RIP:
         Telegram is deleted, not flagged off — telegram_net.py (DNS-pin/DoH/
         session-rebuild) removed, all _send_telegram/critical-queue/TelegramConfig
         and TELEGRAM_*/tg_* config + .env keys gone; Discord is the sole channel
         (FailureStreak backoff moved into discord_client). selftest drops the two
         Telegram steps. (2) LONE-LEG HEDGING RESCUE: the No-OCO twin-open
         precondition is removed so a leg whose twin already closed STILL fires the
         fleet rescue + 2 boosts when price travels the $10 spread against it (the
         sibling fills) -- boosts go in the breakout direction (opposite the losing
         leg), HEDGING not martingale. Jun-17 A4 lone SELL ran to -630 unhedged;
         this offsets it. Trigger -$10, boost SL $10, whipsaw cap -$700, rescue-
         class exits and CRASH_WIN/WHIPSAW_LOSS/SCRATCH logging all UNCHANGED; lone
         events log to rescue_events.csv + Firestore like fleet events. (3) BOOST
         TRAIL HANDOFF: once a boost clears +$8 fav it rides the post-hold trail
         (peak - gap $2.00) with +$8 as a one-way floor, instead of hard-locking at
         +$8 -- a real run (4334->4359) is no longer left on the table. Boosts only
         (new Position.boost flag); normal/rescue-leg trails unchanged. selftest
         adds lone-leg-rescue and boost-trail steps.
  3.1.4  TEST-ONLY: lone-leg rescue BRANCH-RESOLUTION coverage + no-boost
         counterfactual logging. v3.1.3 step 22 proved the lone rescue FIRES with
         the right structure; new step 24 "lone branches" proves the two outcomes
         RESOLVE correctly on dry-run simulated price paths (no real orders): TREND
         -> boosts (real strategy core) ride the trail well past +$8, event nets
         positive and classifies CRASH_WIN (the A4-Jun17 case that motivated the
         feature); WHIPSAW -> each boost is bounded at its $10 SL, the -$700 pair
         cap is never exceeded, classifies WHIPSAW_LOSS (the key safety bound);
         CHOP -> SCRATCH. rescue_log now also computes + logs the NO-BOOST
         counterfactual (rescue/trigger legs alone, boosts excluded) as a new
         rescue_events.csv column for BOTH fleet and lone events, so rescuestats
         can isolate "do the boosts help on lone legs". No live rescue/boost logic
         changed. selftest -> 24 steps.
  3.1.6  BOOST breath-gap trail + $10 backstop + strict boost/original ISOLATION
         (boosts only; straddle/triggers/original-leg & normal/rescue-LEG trails
         FROZEN). (1) The instant a boost fills it gets a tight one-way breath-gap
         trail (config boost_trail_gap_dollars=3.50) armed from entry, WITH its $10
         hard SL as a backstop -- both live, whichever hits first closes the boost.
         Once fav clears +$8 the trail floor never retreats below +$8. So a boost
         that reverses early exits ~-(gap) (was -$10), a boost that gaps THROUGH
         the trail is caught no worse than the $10 backstop, and a runner rides the
         trail past +$8 (4334->4359 no longer left on the table). Managed by a
         dedicated strategy._update_boost_on_bar (boosts early-return; the non-boost
         core is byte-identical to pre-v3.1.3); trails.py market-closes the boost
         on a software-trail hit. (2) ISOLATION: a boost is an additive upside-only
         bet -- its stop logic reads/writes ONLY its own ticket, never the original
         leg; no boost stop event closes/modifies the original (and vice versa);
         the -$700 cap bounds the BOOSTS' combined loss only, never pulls in the
         original; the journal tags each leg's role (normal vs rescue vs boost) so
         P&L is never silently pooled. (NOTE: a future vol-adaptive "smart" gap for
         boosts AND originals is tracked; v3.1.6 ships a fixed tunable gap only.)
         selftest -> 25 steps (breath-trail behaviors + isolation).
"""

__version__ = "3.1.6"
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"