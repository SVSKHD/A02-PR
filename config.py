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
    # --- v3.2.3 Feature D: break-and-hold filter (the profit decider) -----------
    # Do NOT fire boosts on the FIRST break; stack ONLY if price clears the range
    # edge by >= break_dist_x AND holds hold_candles_n M1 candles AND retraces
    # less than max_retrace_y of the break distance. A spike that reverses inside
    # the window is a FAILED break -> fire nothing (kills the 14:30/15:30 fake-outs).
    break_and_hold_enabled: bool = True
    break_dist_x: float = 2.0     # must clear the edge by >= this ($)
    hold_candles_n: int = 2       # must hold this many M1 candles past the edge
    max_retrace_y: float = 0.50   # retrace must stay < this fraction of break dist
    # --- v3.2.3 Feature E: lot config + FP-rule guard --------------------------
    # Account profile gates the pre-trade worst-case-stack check. STANDARD_5PCT =
    # 5% daily ($2,500 @ $50k); FPZERO_1PCT = 1% floating ($500). A stack whose
    # worst-case floating loss breaches the limit at the chosen lot is reduced or
    # blocked. lot_size (above) is the chosen lot: 0.35 demo / 0.15 FP-safe / 0.27 Zero.
    account_profile: str = "STANDARD_5PCT"   # or "FPZERO_1PCT"
    fp_standard_pct: float = 0.05
    fp_zero_pct: float = 0.01
    # --- v3.2.3 Feature C: 5-long No-OCO stack (DEFAULT OFF) --------------------
    # When False the winning side hard-caps at 3 (original + 2 RALLY) -- unchanged,
    # test-36 cap-at-3 stays the invariant. When True the cap rises to 5 (original
    # + 2 RALLY + 2 RESCUE-converts once the losing leg SLs out), FP-gated by the
    # guard above. Flip ONLY after backtesting the higher exposure on the VPS.
    allow_5_long: bool = False
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
    # not -$10. Per-pair whipsaw worst case stays 2 x $10 x 0.35 x 100 = -$700.
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
