"""AUREON — Config dataclass (split from bot.py, v3.0.0). Byte-identical."""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Config:
    # Strategy
    symbol: str = "XAUUSD"
    contract_size: float = 100.0  # oz per 1.0 lot
    trigger_dist: float = 5.00
    tp_dist: float = 30.00  # was 20.00 — let winners run longer
    sl_dist: float = 18.00  # was 20.00 — slightly tighter (saves $118 per SL)
    lot_size: float = 0.35  # v2.7: pinned to the backtested lot. Two full SLs = -$1,260,
    # which survives the 3% daily kill switch (~$1,490 @ $49.6k). At 0.50+, two SLs
    # (-$1,800+) trip the switch and end the day early.
    be_trigger: float = 2.50  # v2.9.6 trail ARM (was 1.50). Jun-11 A3: a +$2.00-peak
    # sell got its post-hold stop snapped to peak-2 = its own ENTRY and scratched $0
    # minutes before its move came. Arm 2.50 means: peaks under +$2.50 get NO trail --
    # full SL stays, trade keeps waiting. Cost: dead-chop days pay -$630 instead of -$25.
    # arm 1.5 marginally better at every freeze level once the hold rule works.
    # The 0.30 arm let the trail chase price within seconds of fill,
    # parking the SL near entry so the first pullback closed the trade
    # at ~breakeven (the Jun-5 A2/A3/A4 losses). At +$2.5 the trade has
    # proven direction before the SL starts following.
    trail_gap: float = 2.00  # v2.9.7: was 1.00 (manual edit from v2.9.2 was never
    # applied -- caught live via the Jun-11 A4 banner). One rule everywhere: in
    # profit, never more than $2 behind the peak (matches the ladder's peak-2).
    # job, a tighter post-hold trail keeps more of the move (gap 1.0 best at every
    # freeze level in the grid). The hold protects the runner; the gap banks it.
    min_step: float = 0.10  # v2.5.5: back to 0.10 to match the 0.30 trail gap
    # --- trail-lock root-cause guards (2026-06-19 A2 incident) -----------------
    # A lock level advanced off a max_fav the market never produced, parking an
    # invalid stop ABOVE a long's market and force-closing a trade that was only
    # in normal drawdown. These three knobs make that impossible:
    arm_buffer: float = 1.50  # price must clear entry by AT LEAST this (>= spread +
    # noise band) before lock_1 / the trail may arm. Stops the trail engaging
    # during entry chop. All existing arm tiers (2.5/5/6/10) already exceed this,
    # so the default is a belt-and-suspenders floor -- no behavior change.
    max_tick_jump: float = 25.0  # garbage-feed filter: a bar's favorable extreme
    # that jumps more than this beyond the running max_fav is rejected (stale/
    # garbage tick), so a phantom spike can never inflate max_fav and arm a lock
    # off a price that never traded. $25 in one M1 bar is far beyond any normal
    # XAUUSD move; legitimate fast markets stay well under it. 0 disables.
    lock_confirm: bool = True  # master switch for the confirmed-price lock gate:
    # a lock level N may advance ONLY when max_fav has truly reached level_N's
    # price. Never on a timer, tick count, loop iteration, or default value.
    # --- v3.2.4 Feature D: break-and-hold filter (the profit decider) -----------
    # Do NOT fire boosts on the FIRST/weak break. Stack ONLY if price clears the
    # range edge by >= break_dist_x AND holds hold_candles_n M5 candles AND retraces
    # less than max_retrace_y of the break distance. A spike that reverses inside the
    # window is a FAILED break -> fire nothing (kills the 14:30/15:30 fake-outs).
    break_and_hold_enabled: bool = True
    # v3.2.7: break-and-hold gates RALLY boosts only (must confirm the break before
    # pyramiding the winner). RESCUE boosts fire FREELY on direction commit -- a
    # rescue is the opposite-side sibling that becomes the winner after a whipsaw;
    # gating it on a confirmed break wrongly suppressed winning-side recovery legs
    # (the 3-leg model). RESCUE is still subject to the +/-$10 trigger, tick-hold
    # >=3, and the FP guard -- ONLY break-and-hold is bypassed. Set False to fall
    # back to gating BOTH kinds (legacy v3.2.6 behavior) without a rebuild.
    rescue_bypass_break_and_hold: bool = True
    break_timeframe: str = "M5"   # hold is measured on M5 candles (v3.2.4)
    break_dist_x: float = 3.0     # must clear the edge by >= this ($)
    hold_candles_n: int = 2       # must hold this many M5 candles past the edge
    max_retrace_y: float = 0.40   # retrace must stay < this fraction of break dist
    # --- v3.2.4 Feature E: lot config + FP-rule guard --------------------------
    # Account profile gates the pre-trade worst-case-stack check. STANDARD_5PCT =
    # 5% daily ($2,500 @ $50k); FPZERO_1PCT = 1% floating ($500). A stack whose
    # worst-case floating loss breaches the limit at the chosen lot is reduced or
    # blocked. lot_size (above) is the chosen lot: 0.35 demo / 0.15 FP-safe / 0.27 Zero.
    # fp_spread_buffer widens the per-leg adverse distance (18 SL + ~0.6 spread =
    # 18.6 effective) so the worst-case matches live floating: 5x0.35 -> -$3,255,
    # 5x0.15 -> -$1,395. FPZERO caps the 5-long back to 3 (no 5-stack on a 1% rule).
    account_profile: str = "STANDARD_5PCT"   # or "FPZERO_1PCT"
    fp_standard_pct: float = 0.05
    fp_zero_pct: float = 0.01
    fp_spread_buffer: float = 0.60
    # --- v3.2.4 Feature C: 5-long No-OCO stack (DEFAULT ON, disableable) --------
    # When True (default) the winning side caps at 5 (original + 2 RALLY + 2 RESCUE-
    # converts once the losing leg SLs out), FP-gated by the guard above and only
    # after a BREAK_CONFIRMED. Set False to fall back to the proven 3-cap. On the
    # FPZERO_1PCT profile the 5-long is disallowed regardless (capped to 3).
    allow_5_long: bool = True
    # NOTE: the 5-long co-close reuses the EXISTING tuned trail_gap (line ~26),
    # it does NOT redefine it. Only the arm threshold is new here.
    trail_arm_profit: float = 8.0  # a long arms its trail once +$8 in profit
    # --- v3.2.5 Feature 1: A1 tick-fallback anchor capture (open path ONLY) ----
    # At the Monday/post-weekend open the M5 bar can lag (not yet published) while
    # ticks are live. If A1's get_m5_close finds NO bar after the existing retries,
    # fall back to a SANE, SETTLED live tick (passes max_tick_jump AND held >=
    # hold_ticks) and place off it -- A1 only, open path only. A2/A3/A4 and A1 on a
    # normal day with a present bar are UNCHANGED (always bar-capture). False = the
    # old behavior (skip on no-bar). a1_tick_fallback_samples = how many recent ticks
    # to read when settling.
    a1_tick_fallback_enabled: bool = True
    a1_tick_fallback_samples: int = 6
    # --- v3.2.5 Feature 2: tick-hold confirm on boost + trail ------------------
    # Boost/trail run on tick refresh (~tick_refresh_s). A +/-$10 cross fires ONLY
    # after it HOLDS >= hold_ticks consecutive sane ticks (~1s); a cross that reverts
    # within the window is a blip -> NO fire. A trail lock advances only on a held,
    # sane max_fav (reinforces the phantom-lock guard). Tightens WHEN a boost/lock
    # acts; does NOT change the +/-$10 levels, the stack rules, the cap, or any
    # existing boost/trail logic. tick_hold_band reuses max_tick_jump by default.
    hold_ticks: int = 3
    tick_refresh_s: float = 0.3
    freeze_minutes: int = 45  # v2.7: was 15 (and functionally DEAD until the v2.7 timezone
    # fix in live_trader._manage_trails_on_bar_close -- see comment there). 45m = risk-
    # adjusted sweet spot of the tick grid: +$26.7k vs +$23.0k @30m, same maxDD (-$2,520),
    # mid-plateau (30/45/60 all similar -- not a lucky number). During the hold only the
    # $18 SL, $30 TP and +$3 BASE LOCK may close a trade.
    no_oco: bool = True  # v2.7 default ON: grid shows nooco > oco by ~2x at every freeze
    # level (2nd legs net +$6k standalone). --no-oco launch flag no longer required.
    rescue_boost_enabled: bool = True  # v2.9.5 Hithesh's SL-RESCUE BOOST: when the
    # sibling fills as RESCUE (= first leg is -$10), open extra market trades in
    # the rescue direction so the remaining $8 to the first leg's SL is covered:
    # 2 x 0.35 x $8 = +$560 at the moment the trapped leg stops out. Each boost
    # carries a TIGHT $6 SL so the whipsaw day costs -$420 extra (vs -$1,260
    # with full $18 SLs -- which would breach the daily kill switch in ONE
    # anchor; measured Jun-11 A3). Boosts run as rescue legs: no small locks,
    # $10 tier, post-hold trail, TSTOP at 45m.
    rescue_boost_count: int = 2
    stack_depth: Optional[int] = None  # v3.2.3: winning-side stack size (No-OCO +
    # lone). None => use rescue_boost_count (2 boosts => stack of 3). 1 = base (NO
    # boosts; leg runs alone). 3 = full stack (original + 2). Honored by the SINGLE
    # source boosts.plan_boost_event so live + backtest stack identically. The hard
    # cap is 3 winners; values above 3 are clamped (telemetry flags > 3 as a violation).
    boost_sl_dollars: float = 10.0  # v3.0.9: boost SL $6 -> $10. First live fleet
    # (2026-06-17 A1) whipsawed: a $6 stop was tagged by a $6 dip then price ran
    # +$8. The $10 stop is now the boost's HARD BACKSTOP only -- v3.1.6 adds a
    # tight breath-gap TRAIL on top (below), so a reversing boost exits at ~-(gap)
    # not -$10. This is the RESCUE boost SL (UNCHANGED). Per-pair RESCUE whipsaw
    # worst case stays 2 x $10 x 0.35 x 100 = -$700.
    rally_boost_sl: float = 13.0  # v3.3.3 (owner choice): the RALLY boost hard
    # SL/backstop, widened 10 -> 13 (was 12 pre-v3.3.3) so a rally pyramid is not
    # tagged out by a shallow pullback before the move resumes. RALLY ONLY -- the
    # RESCUE backstop stays $10 (boost_sl_dollars). The rally whipsaw cap scales
    # with it: 2 x $13 x 0.35 x 100 = -$910 (vs the rescue cap -$700). The cap reads
    # the per-event kind's SL, never one shared value (boosts.boost_sl_for).
    boost_trigger_dollars: float = 10.0  # v3.2.0: a lone leg must move this
    # far from its FILL before any boost fires -- +$10 => RALLY (same dir,
    # winning), -$10 => RESCUE (opposite, losing). Boosts NEVER fire at fill
    # (the A3 -$900 bug). The single source boosts.plan_boost_event enforces it.
    rally_boosts_enabled: bool = True  # v3.2.2: independent on/off for the RALLY
    # branch (lone leg +$10 -> same-dir pyramid). When False, a +$10 move fires
    # ZERO rally boosts and the leg runs on its own SL/TP/trail. Independent of
    # rescue_boosts_enabled. Default True => current behavior unchanged. Gated in
    # the SINGLE source boosts.plan_boost_event so live + backtest honor it.
    rescue_boosts_enabled: bool = True  # v3.2.2: independent on/off for the
    # RESCUE branch (lone leg -$10 -> opposite-dir hedge). When False, a -$10 move
    # fires ZERO rescue boosts and the leg runs on its own SL/TP/trail. Independent
    # of rally_boosts_enabled. Default True => current behavior unchanged. Gated in
    # boosts.plan_boost_event. NOTE: distinct from the pre-existing master
    # rescue_boost_enabled (No-OCO sibling-as-rescue scan switch) above.
    boost_trail_gap_dollars: float = 3.50  # v3.1.6: boost-ONLY breath-gap trail,
    # armed from the instant the boost fills, alongside the $10 hard SL backstop
    # (both live; whichever hits first closes the boost). One-way ratchet; once a
    # boost clears +$8 the trail floor never retreats below +$8. A reversing boost
    # exits ~-(gap); a gap THROUGH the trail is caught no worse than the $10 SL; a
    # runner rides past +$8. Boosts are upside-only -- isolated from the original
    # leg (the original runs to its OWN exit; boosts never close/modify it). NOTE:
    # a future "smart" adaptive gap (vol-scaled, for boosts AND originals) is a
    # tracked item; v3.1.6 ships this as a tunable fixed gap only.
    # --- v3.2.6 BOOST breath-gap +$8 ARM GATE (incident 2026-06-23 fix) --------
    # The 2026-06-23 SELL boosts (#56860793855/#...813) entered ~4185.92 and were
    # CUT underwater at ~4191.32 (-188.65 each) by the breath-gap software stop that
    # was armed at fav=0; price then dropped ~$35. ROOT CAUSE: the breath trail was
    # live below profit, sitting only $gap adverse of entry. FIX: the breath-gap
    # software trail is INACTIVE until the boost has been at least +boost_trail_arm_fav
    # favorable (peak). Below the arm, ONLY the $10 hard backstop protects -> a
    # reversing boost rides to the backstop (or recovers) instead of being cut at
    # ~-gap. AT the arm, a one-way LOCK FLOOR at +boost_lock_floor engages (locked
    # profit never falls below it); ABOVE it the $gap trail follows the favorable peak,
    # floor never retreating. Boost-path ONLY; original-leg ladder/BE/trail untouched.
    boost_trail_arm_fav: float = 8.0  # BOOST_TRAIL_ARM_FAV: peak fav ($) before the
    #   breath-gap trail/lock arms; below it the boost runs on the $10 backstop only.
    boost_lock_floor: float = 8.0     # BOOST_LOCK_FLOOR: once armed, locked profit ($)
    #   never falls below this (one-way ratchet). == arm by default.
    max_boost_stack: int = 5          # MAX_BOOST_STACK: hard cap on winning-side stack
    #   when allow_5_long (orig + 2 RALLY + 2 RESCUE); 3 when allow_5_long is False.
    # --- v3.2.8 Phase 1 RALLY-ONLY breath-gap tightening -----------------------
    # The RALLY path (winning leg -> same-direction pyramid) gets its OWN tighter
    # arm/lock/gap so a winning leg arms sooner and locks profit earlier, while the
    # RESCUE path (losing leg -> opposite-direction hedge) is LEFT EXACTLY as v3.2.7
    # (arm $10 via boost_trigger_dollars, lock/arm $8 via boost_trail_arm_fav /
    # boost_lock_floor, gap $3.50 via boost_trail_gap_dollars). Rally gets DEDICATED
    # keys -- it does NOT reuse the BOOST_* keys rescue depends on. The trail gap is
    # kept PROPORTIONAL: $3.50 under an $8 floor scales to $1.50 under a $4 floor so
    # the one-way ratchet keeps the same shape, just tighter. Used only when a boost
    # position's kind is RALLY (Position.boost_kind == 'RALLY'); RESCUE boosts and
    # every existing caller are byte-identical.
    rally_arm_fav: float = 5.0        # RALLY_ARM_FAV: a WINNING leg arms the rally
    #   pyramid once it is +$5 favorable (was +$10, the shared boost_trigger_dollars).
    #   Rescue's losing-side -$10 arm (boost_trigger_dollars) is untouched. v3.3.0:
    #   this is ALSO the rally boost's trail-arm peak -- the breath-gap trail goes
    #   live once the boost's OWN peak reaches +$5.
    rally_lock_floor: float = 3.00    # RALLY_LOCK_FLOOR: v3.3.0 the break-even+ MINIMUM
    #   an armed rally boost's trailed stop may not fall below (= arm - gap = 5 - 2 =
    #   $3). It is a FLOOR only, NOT the governing exit: above it the boost RIDES at
    #   peak - rally_trail_gap (one-way), exactly like the original leg, instead of
    #   locking flat at +$4 and bailing on the first pause (v3.2.8 defect; test-fire
    #   A2 2026-06-24: original +$425 rode 4069->4081 while boosts clipped at ~4078).
    rally_trail_gap: float = 2.00     # RALLY_TRAIL_GAP: v3.3.0 the rally boost trails
    #   at peak - $2.00 (was $1.50), matching the original leg's trail gap (trail_gap
    #   / banner "gap $2.00") so a rally boost rides the move and exits ~peak-$2.
    # --- v3.3.4 RALLY PULLBACK DETECTOR (rally boosts only) -----------------------
    # An entry-relative early-cut that sits ABOVE the $13 hard backstop (rally_boost_sl).
    # A rally boost that pulls back AGAINST ITS ENTRY is HELD while the adverse
    # excursion stays within T dollars (a pullback); crossing T cuts it early (a
    # reversal); B minutes adverse without returning to entry also cuts it (a slow
    # reversal). Returning to ENTRY ends the pullback and normal trail/backstop resume.
    # The $13 backstop stays underneath as the hard gap floor. RESCUE never enters here.
    # NUMBERS ARE TBD FROM LIVE DATA -- these are starting defaults, fully tunable.
    # v3.3.4 ships the mechanism flag-gated and DEFAULT OFF: the code is inert (live
    # rally-boost exits are UNCHANGED) until the owner flips rally_pullback_enabled on
    # after validating T/B against live data. Flip to True to activate.
    rally_pullback_enabled: bool = False  # v3.3.4: DEFAULT OFF (opt-in until measured).
    rally_pullback_tol_dollars: float = 7.50  # T: adverse $ vs entry (candidate $7-8 ->
    #   $7.50). Must stay < the $13 hard backstop (rally_boost_sl); clamped to it. Within
    #   T = hold (pullback); cross T = cut early at the threshold (reversal).
    rally_pullback_time_bound_min: float = 30.0  # B: minutes adverse (below/above entry)
    #   without returning to entry before a slow-reversal cut. 0 disables the time bound.
    tstop_fav: float = 1.00  # v2.7.1 loser time-stop: at hold expiry, market-close any
    # leg whose best favorable excursion never reached this ($1). Grid verdict: +$2.0k
    # funded net, 6 fewer full SLs, identical maxDD (-$2,520), best half-balance of all
    # 72 combos. fav<$2 or <$3 tested WORSE -- only truly dead legs get cut. 0 disables.
    # Auto-sizing: read balance from MT5 at startup, compute the largest safe lot
    auto_lot: bool = False  # if True, override lot_size from live balance
    lot_conservatism: float = 0.99  # was 0.92 — produces lot 0.54 at $50k (1.94% per trade, safe buffer to 4% daily rule)
    risk_pct_under_50k: float = 0.03  # Funding Pips: 3% per-trade on <$50k accounts
    risk_pct_over_50k: float = 0.02  # Funding Pips: 2% per-trade on ≥$50k accounts
    slippage_buffer: float = 0.98  # keep lot's worst-case loss to this fraction of the rule cap

    # Anchors — (label, broker_hour, broker_minute). Broker = UTC+3.
    # v2.5.6: A3/A4 shifted 20 min EARLIER (13:40 / 16:40) so the position is
    # opened and its freeze-lock established BEFORE the 10:00-ET news block,
    # instead of entering into the news spike. A1/A2 unchanged (no US news).
    anchors: List[Tuple[str, int, int]] = field(default_factory=lambda: [
        # v2.9.4: ALL anchors re-enabled for LIVE forward evaluation (user
        # decision: backtest evidence set aside; only forward demo performance
        # counts). Each anchor is judged on its own live record after 2 demo
        # weeks -- persistent losers get cut based on the journal, not sims.
        ("A1_02h_Asia", 2, 30),
        ("A2_10h_London", 10, 0),
        ("A3_1340_Overlap", 13, 50),
        ("A4_1640_NYopen", 16, 40),
    ])
    # Monday cold-start cushion. Forex opens Mon 00:00 broker; A1 at 02:30 is only
    # 2.5h after week-open, when the Monday offset re-detect + still-thin M5 history
    # can make get_m5_close land on an empty/forming window -> "no bars" -> a silent
    # A1 miss. On MONDAYS ONLY, fire A1 later: 03:30 broker (6:00 AM IST) -- ~3.5h
    # after open, by which point the feed is reliably live and M5 history exists, so
    # the quiet-feed cold-wake miss can't happen. (broker_hour, broker_minute);
    # None disables the shift (pure 02:30 every day). A2/A3/A4 and A1 on Tue-Fri are
    # unaffected; the A1 label "A1_02h_Asia" is unchanged. v3.0.3: 03:00 -> 03:30.
    monday_a1_override: Optional[Tuple[int, int]] = (3, 30)
    broker_tz_offset_hours: int = 3  # UTC+3
    # Monday-wake hardening: the broker offset the bot MUST measure on wake before
    # it will place any anchor. Pepperstone = UTC+3. A mismatch (e.g. the Jun-8
    # 0h misdetect) blocks A1 loudly instead of querying the wrong M5 window.
    EXPECTED_BROKER_OFFSET_HOURS: int = 3
    eod_broker_hour: int = 23  # close all at 23:00 broker

    # Risk
    starting_balance: float = 50000.0
    # --- v3.2.9 manual TESTFIRE collision guard ---------------------------------
    # `python bot.py testfire` fires ONE real anchor entry at current market, on
    # demand. Rail #4: refuse if a SCHEDULED anchor is active or within this many
    # minutes, so a manual test can never collide with a real anchor. Fail-closed.
    testfire_collision_min: int = 30
    daily_loss_pct: float = 0.03  # 3% kill switch (Funding Pips Zero has 5% trailing DD — 3% daily gives a 2% multi-day buffer)
    weekly_loss_pct: float = 0.08
    account_floor_pct: float = 0.85  # halt new entries below this multiple of starting
    # Fix 1 (2026-06-15 missed-anchor incident): stale-tick RETRY at placement
    # instead of an immediate skip. A tick older than the threshold triggers a
    # poll loop (every poll_s, up to window_s) for a fresh tick before giving up
    # -- a transient MT5/broker blip must not cost a whole anchor.
    stale_tick_threshold_s: float = 60.0   # tick age that counts as 'stale'
    stale_retry_window_s: float = 90.0     # NEW: total poll window before skip
    stale_retry_poll_s: float = 5.0        # NEW: poll cadence within the window

    # v3.0.5: anchor LATE-PLACEMENT window. If an anchor did not PLACE by its
    # scheduled time (any cause: quiet feed, stale tick, wake, warmup fail,
    # transient rc, ...), keep re-attempting on the stale-retry cadence for this
    # many minutes after the scheduled time, then give up with a loud MISS alert.
    # Geometry is unchanged -- the late straddle just re-captures the anchor price
    # at the moment it actually places. Hard stops (kill switch / EOD / weekend /
    # window-elapsed) are never overridden. 0 disables late-retry (original 120s
    # window behavior). One placement per anchor per day regardless.
    anchor_late_window_min: int = 10

    # Operational
    log_level: str = "INFO"
    state_file: str = "aureon_v2_state.json"
    # v3.1.8: month-level realism haircut for the backtester's REALISM-ADJUSTED
    # net (RAW - this). Approximates live drag not modeled in backtest (late
    # fills, partial fills, requote slippage, weekend gaps). Month-level only.
    realism_haircut_dollars: float = 1000.0

    # Alerting/control channel (v3.1.0). Discord (discord.com) is the sole alert +
    # command channel, using rich embed CARDS. Override with env
    # AUREON_ALERT_CHANNELS (csv). Discord is enabled by DISCORD_BOT_TOKEN +
    # DISCORD_CHANNEL_ID (.env); commands need the gateway (discord.py) +
    # Message Content Intent ON in the Developer Portal.
    alert_channels: List[str] = field(default_factory=lambda: ["discord"])
    discord_heartbeat_min: int = 60   # heartbeat card cadence (0 disables)
