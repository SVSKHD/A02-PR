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
  3.1.7  FIX the LIVE lone-leg rescue logging gap (logging/observer only; no
         trading change). A real lone rescue (2026-06-18 A1, +$2,079) fired but
         rescuestats showed 0 -- the event OPENED but never FINALIZED/wrote.
         Root cause: in-flight rescue events were in-memory ONLY (unlike shadow
         positions/pendings, which are persisted exactly because a restart between
         placement and fill used to orphan them); a restart between the rescue
         OPEN and its members CLOSING dropped the event so nothing was written.
         FIX: (1) persist _rescue_events + _rescue_event_by_ticket to state on
         every open/finalize and REHYDRATE on startup -- an event opened before a
         restart now finalizes + writes when its members close after. (2) The CSV/
         Firestore row now carries event_type (FLEET vs LONE_RESCUE) and SEPARATE
         orig_pnl + boost_pnl fields (isolation -- never pooled) alongside the
         existing no_boost_net counterfactual. selftest gains a LIVE-PATH-PARITY
         step (26) that drives the SAME bound open/close/finalize + persist/
         rehydrate methods the live path uses, asserts an opened lone event that
         closes ALWAYS writes a row, SURVIVES a restart (persist->rehydrate->close
         ->write), has event_type + orig/boost fields, and leaves no orphan -- so a
         future sim/live divergence is caught. selftest -> 26 steps. NOTE: the
         specific 2026-06-18 event's in-flight state was lost (pre-fix, in-memory)
         so it cannot be auto-recovered; backfill needs the operator's MT5/journal.
  3.1.8  TICK-RESOLUTION MONTHLY BACKTESTER in backtest/ (no live-behavior change;
         read-only against live logic). backtest/backtest.py REUSES the live rules
         by IMPORT -- strategy.update_position_on_bar / realize_pnl_usd / Position
         (45m hold, gated BE, ladder, breath-gap $3.50 boost trail + $10 backstop +
         $8 floor, isolation), utils.initial_sl/tp + anchor/eod time, anchors.
         resolved_anchor_hm (Monday A1 @ 03:30 cushion -- newly EXPOSED as a pure
         function the live method now delegates to), fills.is_rescue_fill (lone-leg
         rescue rule), rescue_log._branch_for (CRASH_WIN/WHIPSAW_LOSS/SCRATCH). It
         replays cached MT5 ticks (backtest/fetcher.py, copy_ticks_range chunked +
         parquet cache) through these functions with realistic spread/fills/
         slippage/stop-through, modeling No-OCO straddle, lone-leg rescue + boosts,
         -3% kill switch, EOD flatten. `python backtest/back_main.py YYYY-MM` prints
         a day-by-day table (Monday A1 tagged @03:30), per-anchor + boost-branch
         summaries, RAW vs REALISM-ADJUSTED net (RAW - realism_haircut_dollars,
         default $1000), max DD, kill-switch days. selftest gains a "backtest
         parity" step asserting the backtester IMPORTS the live functions (identity,
         not copies) so a reimplementation that could drift is caught.
         STANDING RULE: every new strategy feature must land in BOTH live modules
         AND be exercised by the backtest -- a feature is not "done" until backtest
         reflects it. backtest == live.
  3.2.0  CRITICAL boost-trigger fix — lone-leg boosts NO LONGER fire at the leg's
         fill price. The A3 bug (Jun 18): the old fire-at-fill path placed 2
         boosts at 4266.30 (= the lone leg's fill), in the sibling's direction,
         always labelled "RESCUE" even when the leg had WON; a reversal then
         killed them (~-$900). FIX: one canonical TRIGGER decision,
         boosts.plan_boost_event(leg_side, leg_fill_price, current_price, cfg) ->
         BoostPlan|None, is the SINGLE source of truth. The rule: boosts never
         fire at/near the fill -- only once price moves a full $10 from the fill.
         Leg WINNING by +$10 -> RALLY (2 boosts SAME direction, event_type
         RALLY_BOOST, a winning pyramid); leg LOSING by -$10 -> RESCUE (2 boosts
         OPPOSITE, RESCUE_BOOST, hedging the breakout); <$10 -> None (no boosts).
         A HARD GUARD blocks any returned plan whose entry is < $10 from the fill
         (>= $10 entry guard) -- the fire-at-fill bug is now structurally
         impossible. The SAME function is called by LIVE (fills.py per-tick in
         _check_boost_triggers, never at fill), by the BACKTEST (backtest.py walks
         post-sibling bars and fires on the first $10 move; old sibling-fire-at-
         fill logic retired), AND by the SELFTEST (import-path parity asserts
         fills.boosts.plan_boost_event IS boosts.plan_boost_event IS
         backtest.plan_boost_event -- a future test/live divergence is caught).
         The -$700 combined-boost loss cap hard-closes the boosts (clamp on
         breach; isolation -- never pulls in the original leg). rescuestats logs
         the canonical event_type (RALLY_BOOST / RESCUE_BOOST) instead of FLEET/
         LONE_RESCUE for boost events. backtest boost-summary now reports
         RALLY_BOOST/RESCUE_BOOST counts + branch (CRASH_WIN/WHIPSAW_LOSS/SCRATCH)
         + the no-boost counterfactual. selftest gains step 28 "boost trigger"
         (no-fire-at-fill, >=$10 entry, RALLY/RESCUE direction, cap clamp, import-
         path parity) and extends "backtest parity" with the boost parity check ->
         28 steps.
  3.2.1  FIX selftest silent early-exit + rescue_events.csv startup-ensure (no
         trading change). v3.2.0 selftest printed "SELF-TEST starting" then exited
         with no steps/error/traceback: an early return from a failed _preflight()
         (e.g. broker-read error or positions present) returned False WITHOUT
         draining the async telemetry worker -- the abort reason was enqueued but
         the daemon worker was killed at process exit before printing it, and
         nothing printed synchronously. FIX: run() now prints the start line AND
         the abort reason SYNCHRONOUSLY (stdout), wraps the whole run in a
         full-traceback catch to stderr (never a silent crash), drains telemetry in
         a finally on EVERY exit path, and run_selftest catches adapter-build/
         construction failures with a traceback too; _preflight prints its abort
         reason + traceback synchronously. So selftest now ALWAYS ends with RESULT:
         ... or a clear ABORTED/CRASHED reason. Also: rescue_log.ensure_rescue_
         events_csv creates run/rescue_events.csv (header only) at startup so
         rescuestats always reads a valid file and a path/permission problem
         surfaces at startup, not silently at the first finalize (finalize already
         create-with-header on first write -- this is belt-and-suspenders).
  3.2.2  INDEPENDENT RALLY / RESCUE boost toggles (no other behavior change).
         Two new config flags -- rally_boosts_enabled / rescue_boosts_enabled
         (both default True => current behavior unchanged) -- gate the RALLY
         (lone leg +$10, same-dir pyramid) and RESCUE (lone leg -$10, opposite
         hedge) branches INDEPENDENTLY. The gating lives in the SINGLE canonical
         boosts.plan_boost_event, so LIVE (fills._check_boost_triggers per-tick)
         and BACKTEST (backtest.run_month) honor the SAME flags by import -- no
         separate copy. A disabled branch fires ZERO boosts; the leg runs on its
         own SL/TP/trail. Trigger threshold, >=$10 entry guard, $3.50 breath-trail,
         $10 backstop, -$700 cap, isolation and logging are all UNCHANGED.
         back_main.py adds run-time overrides (--no-rally / --no-rescue) so configs
         compare without editing config, and prints the active mode in the header
         ("boosts: RALLY=on RESCUE=off"); the boost summary (RALLY/RESCUE counts,
         CRASH_WIN/WHIPSAW_LOSS/SCRATCH, no-boost counterfactual) is unchanged.
         selftest gains step 29 "boost toggles": rally-off => zero rally on +$10,
         rescue-off => zero rescue on -$10, independence (the other still fires),
         live/backtest import-path parity, and defaults reproduce current behavior.
         selftest -> 29 steps.
  3.2.3  TELEMETRY OVERHAUL + No-OCO BOOST STACKING + DISCORD ALERTS. (1) Per-
         position structured trace (position_telemetry.PositionTracer): one
         greppable line per state change (PLAN..FILL..MAXFAV_UPDATE..LOCK_ARM..
         TRAIL_ADVANCE..STOP_THROUGH_REARM..BOOST_ARM..BOOST_FIRE..HEARTBEAT..EXIT)
         carrying all mandatory fields (null explicit, never omitted) + runtime
         TELEMETRY_VIOLATION asserts (trail-exit-without-advance, lock-above-max_fav,
         long-stop>=bid, lock-skip, boost-below-trigger, stack>3, MISSED_BOOST,
         BOOST_ARM_ORPHANED). (2) Trail-lock root-cause fix: confirmed-price lock
         ladder, max_fav floored at entry + garbage-tick filter (max_tick_jump),
         arm-buffer; stop-through RE-ARMS (never market-closes). (3) No-OCO winning-
         side STACKING (default ON): every straddle leg is RALLY-only boost-eligible
         so the winner stacks to 3 while the loser rides to SL; stack hard-cap 3;
         break-even economics CODED in boosts (per-position +$6 line). (4) Proactive
         Discord on every event: 🚀 BOOST FIRED / 📦 STACK 3/3 / ⛔ STOP-THROUGH /
         🔒 TRAIL / 🚨 VIOLATION. (5) --stack-depth backtest flag (shared source).
         selftest -> 38 steps (Groups 1-5: trail/lock, lone boost, No-OCO stack,
         telemetry/Discord, parity); import-path identity extended to the guards +
         tracer + economics.
         (phantom-lock follow-up) EXPLICIT phantom-lock guard surfaced as a single
         shared check (strategy.lock_trigger_reached): a lock level may apply ONLY
         if max_fav genuinely reached its trigger price. Additive only -- lock
         formulas / step size / rung thresholds UNCHANGED. New observability:
         LOCK_CHECK (every evaluation, PASS|FAIL), LOCK_REJECTED_PHANTOM (a blocked
         phantom is now countable, never silent), and a phantom_lock_applied
         tripwire VIOLATION. Discord: 👻 PHANTOM LOCK BLOCKED (rate-limited 1/60s)
         + 🔒 LOCK on real arm. selftest -> 40 steps (PL1-PL7); import-path identity
         covers lock_trigger_reached. Banner stays v3.2.3.
         (monday-offset follow-up) Weekend-wake offset guard surfaced as ONE shared
         module (offset_guard): re-derive the broker offset from a fresh tick on a
         weekend wake, validate against EXPECTED_OFFSET=+3, retry up to
         OFFSET_RETRY_MAX=3, and BLOCK A1 if unresolved -- never place on a guessed
         0h offset (the Jun-8 silent miss). Additive only: A1 schedule /
         monday_a1_override / trade logic UNCHANGED (backtest RAW net identical).
         New observability: WEEKEND_WAKE, OFFSET_DETECT (CONFIRMED|RETRY|BLOCKED),
         OFFSET_MISMATCH, ANCHOR_TIME_RESOLVED + offset_mismatch / monday_a1_drift
         violations. Discord: 🌅 WEEKEND WAKE, ⛔ OFFSET MISMATCH A1 (before A1
         places), 🟢 A1 RESOLVED (Monday all-clear). selftest -> 47 steps (41-47:
         monday wake / badoffset / drift-trip / weekday-unaffected / trace / Jun-8
         replay / offset parity); import-path identity covers offset_guard.resolve_offset.
         (soft-restart follow-up) SOFT self-update + restart-reconcile. Positions
         live on the BROKER, so a restart never closes them; the risk is returning
         BLIND. Pure shared decision module (soft_restart): the auto-pull gate
         (an open position does NOT defer; only mid-anchor/mid-fill defers), the
         deploy gate (selftest must ALL-PASS or abort + keep old build), and the
         RESUME/ADOPT/FINALIZE reconcile classifier (a live broker position is
         NEVER orphaned). Constants SOFT_RESTART, SOFT_RESTART_MAX_GAP_S=10,
         PERSIST_OPEN_POSITIONS, RECONCILE_ON_BOOT, NEVER_FLATTEN_ON_UPDATE. The
         soft restart persists full state + hands off to the watchdog WITHOUT
         touching any position/pending. New observability: SOFT_RESTART_SNAPSHOT /
         _EXIT / _REHYDRATE, RECONCILE (per ticket) + RECONCILE_SUMMARY + the
         reconcile_orphan / AUTOPULL_ABORTED tripwires; Discord 💾/⚡/🚨/🚀.
         Additive only -- trade logic UNCHANGED (backtest RAW net identical).
         selftest -> 54 steps (soft-restart 48-54: autopull-soft / abort / no-flatten
         / rehydrate-resume / reconcile-adopt / -finalize / quick-gap).
         (build-list follow-up) Feature D break-and-hold filter (break_hold): do NOT
         stack on the first break -- only on a CONFIRMED break (clears edge by
         break_dist_x, holds hold_candles_n candles, retrace < max_retrace_y); a
         spike that reverses = FAILED -> fire nothing (kills 14:30/15:30 fake-outs).
         Feature E lot config + FP guard (fp_guard): pre-trade worst-case-stack
         check vs the account FP rule (STANDARD_5PCT $2,500 / FPZERO_1PCT $500) at
         the chosen lot -> OK / REDUCE / BLOCK. Feature C 5-long No-OCO stack behind
         allow_5_long (DEFAULT OFF; cap stays 3 = test-36 unchanged): when on, the
         winning side caps at 5 (original + 2 RALLY + 2 RESCUE-converts), FP-gated.
         Both gates wired as additive preconditions in the live boost trigger;
         BREAK_EVAL / FP_GUARD telemetry + Discord 🚦/🛡️. Trade core UNCHANGED
         (backtest RAW net identical). selftest -> 68 steps (D 55-59, E 60-63,
         C 64-68); import-path identity covers break_hold + fp_guard. Banner v3.2.3.
  3.2.4  BREAK-AND-HOLD + FP GUARD + 5-LONG hardening (evolves the v3.2.3 build).
         Break-and-hold now measures the hold on M5 candles, clears >= $3, retrace
         < 40%, with states CANDIDATE/CONFIRMED/FAILED(reason) and events
         BREAK_CANDIDATE / BREAK_CONFIRMED / BREAK_FAILED / CONTINUATION_STACK
         (📈/🚫 Discord). FP guard worst-case uses SL + spread buffer (18.6 eff):
         5x0.35 -> -$3,255, 5x0.15 -> -$1,395; FPZERO_1PCT caps the 5-long to 3
         (profile_stack_cap); FP_GUARD_EVAL event + 🛡️ Discord. 5-long is now
         DEFAULT ON (allow_5_long=True, still disableable): test-36 cap 3->5 (the
         only sanctioned existing-test change), test-37 pins the 3-profile to keep
         its 3-stack economics. New: stack_trail_exits (armed longs co-close at
         max_fav - trail_gap; unarmed -> own $10 SL) + STACK_COMPLETE/LEG_SL/
         TRAIL_LOCK/STACK_CLOSE events; P&L fixtures (0.15: +15/+315/+915; 0.35
         modest +735). selftest -> 73 steps (new 69-73: trail co-close / P&L 0.15 /
         P&L 0.35 / FPZERO profile cap / 5-long default-on). Banner v3.2.4.
  3.2.5  A1 TICK-FALLBACK + TICK-HOLD CONFIRM (Mon Jun-22 A1 miss). ADDITIVE, scoped
         -- tests 1-73 and A2/A3/A4 bar-capture UNCHANGED. (1) A1 open-path tick
         fallback: when get_m5_close still finds NO M5 bar after the existing retries
         (the bar lags at the Monday/post-weekend open while ticks are live), A1
         falls back to a SANE, SETTLED live tick (passes max_tick_jump AND held >=
         hold_ticks via the shared tick_hold.settle_anchor_tick) and PLACES off it
         instead of missing. A1 only, open path only; A2/A3/A4 and A1-with-a-bar
         untouched. Events A1_BAR_MISSING / A1_TICK_FALLBACK / A1_PLACED_FROM_TICK +
         🟢 Discord. (2) Tick-hold confirm: a +/-$10 boost cross fires ONLY after it
         HOLDS >= hold_ticks (default 3, ~1s) consecutive ticks; a cross that reverts
         within the window is a blip -> no fire (TICK_CROSS_CANDIDATE /
         TICK_HOLD_CONFIRMED / TICK_BLIP_REJECTED, blips to file not Discord). A trail
         lock advances only on a held max_fav (tick_hold.trail_advance_ok), reinforcing
         the existing phantom-lock guard. Levels / stack / cap / existing boost+trail
         logic UNCHANGED -- only WHEN a boost/lock acts. New module tick_hold.py (pure,
         import-path identity). selftest -> 78 steps (new 74-78: A1 places-from-tick /
         A1 rejects-spike / tick-hold fires / blip-rejected / trail-advance). Banner
         v3.2.5.
  3.2.6  BOOST breath-gap +$8 ARM GATE (incident 2026-06-23). The breath-gap software
         trail was armed at fav=0, sitting only $gap ($3.50) adverse of entry, so two
         SELL boosts (#56860793855/#...813) were CUT underwater at +$5.4 adverse
         (-188.65 each) right before price dropped ~$35. FIX (boost-path only,
         strategy.py _update_boost_on_bar): the breath-gap trail is now INACTIVE until
         the boost has peaked >= +boost_trail_arm_fav ($8). Below the arm ONLY the $10
         hard backstop protects (a reversing boost rides to the backstop or recovers).
         At +arm a one-way LOCK FLOOR engages at +boost_lock_floor ($8); above it the
         $gap trail follows the favorable peak, floor never retreating. New config
         knobs boost_trail_arm_fav / boost_lock_floor / max_boost_stack (no hard-codes).
         Original-leg ladder/BE/trail UNTOUCHED. Added A3-type DOUBLE_FILL log (both
         original legs filled -> log-only, no gate). Re-spec'd selftests 23/24/25 to the
         arm-gate behaviour (reverse<8 -> backstop; reach+8 -> lock floor; run past +8
         -> $gap trail) + NEW incident regression test 79 (SELL 4185.92 -> adverse
         4191.32 NOT cut -> $35 drop -> held/profit). selftest -> 79 steps. Tradeoff:
         a failing boost now rides to -$10 (vs old -$3.50) -- intentional, bounded by
         break-and-hold + the -$700 pair cap. Banner v3.2.6.
  3.2.7  RALLY-ONLY break-and-hold gate (rescue fires free). Audit found break-and-hold
         (fills.py:760) gated ALL boosts incl. RESCUE -- it never branched on plan.kind,
         so a rescue boost (the opposite-side sibling that becomes the winner after a
         whipsaw) was suppressed when the break wasn't confirmed, losing winning-side
         recovery legs (the 3-leg model). FIX (additive): gate break-and-hold on
         plan.kind == 'RALLY' only; a RESCUE plan bypasses it and fires on direction
         commit -- still bounded by the +/-$10 trigger, tick-hold >=3, and the FP guard
         (ONLY break-and-hold is bypassed). New toggle cfg.rescue_bypass_break_and_hold
         (default True; False restores gating both). RALLY still requires a CONFIRMED
         break. The v3.2.6 backstop/+8-arm/lock/trail and the FP guard are UNCHANGED for
         both kinds. selftest -> 80 steps (new 80: rally gated / rescue fires free /
         rescue still FP-blocked / toggle-off re-gates / rally-confirmed fires). Banner
         v3.2.7.
  3.2.8  RALLY tightened + boost file split (rescue UNTOUCHED). Phase 1: the WINNING-
         side rally pyramid gets its OWN dedicated keys -- arm $10->$5 (rally_arm_fav),
         lock floor $8->$4 (rally_lock_floor, == its breath-trail arm), trail gap
         $3.50->$1.50 (rally_trail_gap, kept proportional to the $4 floor: 3.50/8 ->
         1.50/4, same one-way-ratchet shape, just tighter). It does NOT reuse the
         BOOST_* keys rescue depends on. RESCUE keeps its $10 arm / $8 lock / $3.50 gap
         EXACTLY (byte-identical; verified live A1 2026-06-24). boosts.plan_boost_event
         arms asymmetrically (RALLY +$5 / RESCUE -$10) with a per-kind hard guard;
         Position.boost_kind selects the trail in strategy._update_boost_on_bar.
         Phase 2/3: the tangled boost logic (boosts.py + the rally/rescue branches in
         fills.py) splits into rally.py (winning pyramid + break-and-hold gate + the
         Phase-1 numbers), rescue.py (losing hedge; UNCHANGED), boosts_common.py
         (shared placement / FP-guard / -$700 cap / journal / telemetry, mapped ONCE),
         and boosts_dispatch.py (routes by the sign of leg_fav -> rally.fire /
         rescue.fire). The fills LiveTrader methods become thin seams onto these. Pure
         refactor beyond the rally numbers; rescue output byte-identical. selftest ->
         83 steps (new 81 rally arm +5, 82 rally trail 4/1.5 + kind isolation, 83 split
         isolation + dispatcher routing + rescue-relocated byte-identity). Banner v3.2.8.
  3.2.9  Manual TESTFIRE — `python bot.py testfire [--anchor A2]` fires ONE real anchor
         entry at the CURRENT market price, off-schedule, so fills (straddle, boosts,
         rally/rescue) can be watched on demand. REUSES the live placement path (does
         NOT fork): arm_testfire drops a deferred anchor (defer_until=now) and the SAME
         run() loop calls _complete_deferred_anchor -> _place_orders_for_anchor (which
         already re-anchors to current price), so the straddle is current_mid +/-$5,
         $18 SL / $30 TP, No-OCO, rally(+5)/rescue(-10) boosts — identical to a
         scheduled anchor; only the trigger source + timestamp differ. Fail-closed
         safety rails (testfire.py): (1) DEMO-only, no --force; (2) refuse FP/funded
         profile even on demo; (3) flat book (broker + internal shadow); (4) no
         scheduled-anchor collision within testfire_collision_min (default 30);
         (5) one test-fire at a time. Scheduled anchors are SUPPRESSED during the
         session (_testfire_mode gates _process_anchor_if_due). The trade is real and
         COUNTS toward validation; journal tags trigger_source='TESTFIRE' (new column).
         NO v3.2.8 boost number changed; rally + rescue byte-identical. selftest -> 88
         steps (new 84 demo-only, 85 FP-refuse, 86 flat/in-flight, 87 anchor-window,
         88 same-placement call-identity). Banner v3.2.9.
  3.3.0  RALLY boosts now RIDE like the original leg (rally path ONLY). v3.2.8's fixed
         +$4 lock made rally boosts bail on the first pause while the original leg rode
         the whole move (test-fire A2 2026-06-24: original +$425 ran 4069->4081, boosts
         only +$135/+$131 clipped ~4078). FIX: drop the flat lock; once armed at +$5
         (peak) the rally boost trails at peak - rally_trail_gap ($2.00, was $1.50),
         one-way, above a break-even+ MINIMUM floor of +$3 (= arm - gap). It rides and
         exits ~peak-$2 (a +$10 peak banks ~+$8) instead of locking +$4. The +$5 FIRE
         trigger (rally_arm_fav) is UNCHANGED. Config: rally_trail_gap 1.50->2.00,
         rally_lock_floor 4.0->3.0; rally.trail_arm now reads rally_arm_fav ($5).
         KNOWN-DEFECT fix: an armed rally boost (a) emits LOCK_ARM/TRAIL_ADVANCE via a
         threaded tracer so its trail exit is never flagged exit_trail_without_trail_
         advance (the test-fire PTRACE clip), and (b) NEVER closes below its ratcheted
         trail floor -- a bar that gaps THROUGH the trail is clamped to the floor, no
         sub-floor clip (RALLY only; RESCUE keeps the v3.2.7 backstop-floored gap fill).
         RESCUE is BYTE-IDENTICAL ($10 arm / $8 lock / $3.50 gap, free-fire on commit) --
         9 rescue/shared selftest outputs diffed vs v3.2.9 -> identical. selftest -> 90
         steps (82 rewritten to the ride model; new 89 rides-not-bails, 90 no-subfloor-
         clip + trail-advance traced). Banner v3.3.0.
  3.3.1  TESTFIRE --force-window: bypass ONLY rail 4 (the 30-min scheduled-anchor
         collision guard) so the owner can test off-schedule without waiting for the
         window to clear. `python bot.py testfire --anchor A2 --force-window` skips
         rail 4's in-window refusal and prints a LOUD warning (never silent) naming
         how many minutes the nearest scheduled anchor is away and confirming the
         scheduler stays SUPPRESSED for the session (_testfire_mode gates _process_
         anchor_if_due) -- the test event owns the book, so no real anchor places
         alongside it while the test is live. Rails 1/2/3/5 (DEMO-ONLY, NO-FP,
         FLAT-BOOK, ONE-AT-A-TIME) STAY HARD and have NO override, even with
         --force-window. No boost/rally/rescue number changed. selftest -> 90 steps
         (87 rewritten: default still refuses within 30 min, --force-window clears the
         rail-4 refusal while rails 1/2/3 still refuse their cases). Banner v3.3.1.
  3.3.3  TWO fixes. (1 CRITICAL) break-and-hold gate crash + FAIL CLOSED. Live A2
         2026-06-24: rally SELL boosts fired at the +$5 trigger at a move BOTTOM,
         price reversed up, both hit SL for -$701. ROOT CAUSE: the break-and-hold
         gate (rally.break_and_hold_ok) crashed on a numpy ambiguous-truth ValueError
         ("truth value of an array with more than one element is ambiguous") because
         the M5 bars container was tested with `if bars:` / `not bars` -- and the
         handler DEFAULTED TO ALLOWING ("non-fatal, allowing"). FIX 1A: bars
         truthiness now goes through _has_rows (length-based, safe for list AND numpy
         array) so the gate evaluates without raising -- a rally boost fires ONLY on a
         CONFIRMED break (edge cleared by break_dist_x + held hold_candles_n M5
         candles + retrace < max_retrace_y), not on an exhausted spike. FIX 1B: the
         exception handler now FAILS CLOSED -- any gate error BLOCKS the fire (no
         boost) and logs loudly as 'RALLY BOOST BLOCKED', never 'allowing'. RALLY
         only; RESCUE still bypasses break-and-hold by design (rescue_bypass_break_
         and_hold). (2 owner choice) RALLY boost hard SL/backstop 10 -> 13
         (rally_boost_sl=13.0; RALLY ONLY). RESCUE SL stays $10 (boost_sl_dollars
         unchanged). The whipsaw cap is now PER-KIND (boosts.boost_sl_for): RALLY
         2 x $13 x 0.35 x 100 = -$910, RESCUE 2 x $10 x 0.35 x 100 = -$700 -- the cap
         reads the firing event's kind, never one shared value. Everything else on
         rally unchanged: fire +$5, trail peak-$2, entry+$3 floor, one-way ratchet --
         SL width only. RESCUE DO NOT TOUCH: arm $8 / lock $8 / gap $3.50 / SL $10 /
         free-fire on commit / cap -$700 -- asserted unchanged. selftest -> new gate
         array-input (no raise), gate-exception fail-closed (BLOCKED), exhausted-move
         no-fire vs confirmed-break fires; rally SL $13 / backstop +/-$13 / cap -$910;
         rescue SL $10 / cap -$700 unchanged. Banner v3.3.3.
  3.3.4  RALLY PULLBACK DETECTOR (rally boosts only) -- an entry-relative early-cut
         that sits ABOVE the $13 hard backstop (strategy._update_boost_on_bar). A rally
         boost that pulls back AGAINST ITS ENTRY is HELD while the adverse excursion
         stays within T dollars (a pullback); crossing T cuts it early (a reversal);
         B minutes adverse without returning to entry cuts it (a slow reversal).
         Returning to ENTRY ends the pullback and the normal trail/backstop resume.
         The $13 backstop stays underneath as the hard gap floor; a bar that gaps
         THROUGH T fills no better than the backstop. RESCUE is never governed by the
         detector (rally-only; boost_kind gate). Shipped flag-gated and DEFAULT OFF --
         the code is INERT (live rally-boost exits UNCHANGED) until the owner flips it
         on after validating T/B against live data. Config knobs (NUMBERS TBD FROM LIVE
         DATA -- starting defaults, fully tunable): rally_pullback_enabled=False (opt-in),
         rally_pullback_tol_dollars=7.50 (T; candidate $7-8 -> $7.50, must stay < the
         $13 backstop, clamped to it), rally_pullback_time_bound_min=30.0 (B; 0 disables).
         Position gains a pullback_since field (when the current pullback began; None
         outside one). No rally trail number changed (fire +$5, trail peak-$2, +$3 floor,
         one-way) and RESCUE is byte-identical. selftest -> 95 steps (new 94 pullback
         band: hold-within-T / cut-beyond-T / gap-floored-at-backstop / rescue-unaffected,
         proven with an enabled+tol=$8 override; 95 recover/time: recovery resets, B-min
         slow-reversal cut, ships-default-OFF inert). Banner v3.3.4.
  3.3.5  CASE 2 FIX -- parent-profit override for the break-and-hold gate (RALLY only;
         rally.break_and_hold_ok). The gate cannot tell Case 1 (fresh fake spike off a
         flat fill -> reverses -> MUST block, the -$701 loss) from Case 2 (strong crash
         the parent is already riding -> continues -> SHOULD fire): both look violent in
         the first candles. Live A2 2026-06-24: parent SELL rode +$892 on a ~$32 plunge
         but break-and-hold returned BREAK_FAILED (reversed/retrace) the whole way down,
         so NO boost fired. The one reliable distinguisher: in Case 2 the PARENT is
         already DEEPLY favorable in the SAME direction the boost fires. So on the
         would-block path, IF the move is same-direction as the parent AND the parent's
         favorable excursion (max_fav vs entry, $) >= parent_established_dollars, the
         break is treated as CONFIRMED (a proven continuation) and the boost FIRES,
         logging a loud BREAK_OVERRIDE_PARENT_ESTABLISHED PTRACE line (parent_max_fav,
         threshold, move_dollars) for the trial. The override ONLY loosens: below the
         threshold the strict candle gate is fully in force (Case 1 still blocks), and
         an opposite-direction move never qualifies. Config: parent_profit_override_
         enabled=True, parent_established_dollars=20.0 (TRIAL-CALIBRATED, NOT FINAL --
         tunable without a rebuild). RESCUE untouched (bypasses break-and-hold; 10/8/8/
         3.50, cap -$700). $13 rally SL / -$910 cap / fail-closed handler all unchanged.
         selftest -> 98 steps (new 96 case2 override fires + logs; 97 case1 fresh spike
         still blocks incl. $19.99 boundary; 98 opposite-dir no-override + rescue bypass/
         SL/cap unchanged). Banner v3.3.5.
"""

__version__ = "3.3.5"
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"