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
    # --- v3.3.5 CASE 2 fix: parent-profit override (RALLY only) -----------------
    # The candle-structure gate above cannot tell a fresh fake spike (Case 1: spike
    # off a flat fill, reverses -> MUST block, the -$701 loss) from a violent but
    # GENUINE continuation (Case 2: a strong crash the parent leg is already riding
    # deep in profit -> SHOULD fire). Both look violent in the first candles. The one
    # reliable distinguisher: in Case 2 the PARENT is already deeply favorable in the
    # SAME direction the boost fires. So: if the move is same-direction as the parent
    # AND the parent's favorable excursion (max_fav vs entry, $) >= the threshold
    # below, treat the break as CONFIRMED even if candle-structure says reversed --
    # a proven continuation, not a fake spike. Below the threshold the strict gate is
    # fully in force (Case 1 still blocks). The override ONLY loosens; it never makes
    # the gate more permissive on a fresh spike. RESCUE is unaffected (it bypasses
    # break-and-hold entirely). Live A2 2026-06-24: parent rode +$892 on a ~$32 plunge
    # while the gate returned BREAK_FAILED the whole way down -> no boost fired.
    parent_profit_override_enabled: bool = True
    parent_established_dollars: float = 20.0  # TRIAL-CALIBRATED, NOT FINAL. Parent must
    #   be >= +$20 favorable (max_fav vs entry, same units as the $5/$3/$13 knobs) for
    #   the override to apply. Tunable WITHOUT a rebuild -- calibrate from trial data
    #   (the BREAK_OVERRIDE_PARENT_ESTABLISHED PTRACE lines show every time it fired).
    # --- v3.4.0 RALLY OVERRIDE PULLBACK-ENTRY (flag-gated, DEFAULT OFF) ----------
    # The override (above) fires the instant the parent is +$20 same-direction -- i.e.
    # at the EXTREME of the move, which got knifed by the natural breath (Jun 25 A3:
    # fired the top, pulled back $13, -$905). This OPT-IN gate, when enabled, instead
    # ARMS at +$20 and waits for price to retrace override_entry_pullback_dollars from
    # the tracked extreme, then enters on first touch (SL still $13 from THAT entry).
    # If no pullback appears within override_entry_arm_timeout_candles M5 candles -> SKIP
    # the boost (a skip is free; a bad entry costs $905). RALLY override ONLY -- RESCUE
    # and the +$5 rally arm are untouched. DISTINCT from the rally_pullback_* EXIT
    # detector below (this is an ENTRY gate). DEFAULT OFF: with the flag off, the
    # override fires immediately exactly as v3.3.8 (byte-identical). NUMBERS ARE TRIAL-
    # TUNABLE -- band depth + timeout are first guesses, and a high skip rate is a valid
    # verdict (it may mean DELETE the override, not ship this).
    override_entry_enabled: bool = False  # v3.4.0 MASTER FLAG, DEFAULT OFF (freeze-safe).
    override_entry_pullback_dollars: float = 13.0  # retrace $ from the tracked extreme
    #   that arms-then-enters the boost on first touch (entry = extreme -/+ this).
    override_entry_arm_timeout_candles: int = 4  # M5 candles (~20 min) to wait for the
    #   pullback before SKIPPING the boost. Owner's suggested default; trial-tunable.
    override_entry_first_touch: bool = True  # v1 = enter on first touch of the level.
    #   RESERVED: False (a confirm-candle close) is a later refinement, NOT implemented
    #   in v3.4.0 -- the gate uses first-touch regardless of this flag for now.
    # --- v3.5.0 ADAPTIVE PULLBACK ENTRY (extends the override_entry_* path) ------
    # When override_entry_enabled is ON, v3.5.0 upgrades the rally override entry from
    # v3.4.0 first-touch to the ADAPTIVE rule (pullback turn / smooth break-and-hold
    # confirm / timeout-skip, via the shared pullback_entry.step helper). These two
    # toggles refine that path; both DEFAULT ON but are inert while override_entry_
    # enabled is OFF (the whole block is skipped -> v3.4.0/v3.3.8 behavior preserved).
    override_entry_smooth_confirm: bool = True  # allow the SMOOTH branch: if no pullback
    #   appears but break-and-hold CONFIRMS the up-move (same mechanism as the $5 arm),
    #   enter on the confirm. False -> pullback-or-skip only (no smooth entry).
    override_entry_dynamic_sl: bool = True  # pullback entry: SL BEYOND the dip low
    #   (dip_low - rally_boost_sl) so the retrace can't stop it. False -> fixed $13 from
    #   entry. Smooth entries always use the fixed SL (no retrace extreme to anchor to).
    # --- v3.5.0 RESCUE ADAPTIVE PULLBACK ENTRY (NEW mechanism, flag-gated OFF) ---
    # MIRROR of the rally override entry for the RESCUE hedge. RESCUE today FIRES
    # IMMEDIATELY at the -$10 arm (bypassing break-and-hold) and gets knifed by the
    # bounce (Jun 25 A5: -$1,330). When rescue_entry_enabled is ON the rescue KEEPS the
    # losing parent, ARMS at -$10, and waits: enter SELL on a BOUNCE-then-ROLLOVER (SL
    # ABOVE the bounce high), or on a SMOOTH down-move that break-and-hold CONFIRMS (SL
    # entry + $10), or SKIP if neither within the timeout (parent takes its SL alone).
    # SEPARATE keys / flag / call-site from rally (standing rule); shares ONLY the pure
    # pullback_entry.step helper. DEFAULT OFF -> today's immediate bypass-fire preserved
    # (byte-identical). Rescue SL stays $10 (boost_sl_dollars) and the cap stays -$700;
    # the $10->$13 question is a SEPARATE month-end decision (it would move the cap).
    rescue_entry_enabled: bool = False  # v3.5.0 MASTER FLAG, DEFAULT OFF (freeze-safe).
    rescue_entry_bounce_dollars: float = 6.0  # bounce $ UP toward the parent fill that
    #   qualifies the retrace before the SELL entry on the rollover. Trial-tunable.
    rescue_entry_arm_timeout_candles: int = 4  # M5 candles before SKIP (no hedge).
    rescue_entry_smooth_confirm: bool = True  # allow the SMOOTH branch (break-and-hold
    #   confirms the DOWN-move) -> enter SELL on confirm. False -> bounce-or-skip only.
    # --- v3.5.0 "all-16" wiring: per-feature flags ------------------------------
    # UTILITIES (8-11): read-only / alert-only telemetry -- DEFAULT ON; they NEVER
    # touch order flow (proven in the self-test), so they are safe on during the trial
    # and they are the keystone measurement for the keep-vs-delete decision.
    util_pullback_log: bool = True    # 8: per-anchor armed/pulled-back/entered/skipped -> daily JSON
    util_boost_ledger: bool = True    # 9: every boost event (arm/fire/skip px, P&L) -> ledger.csv
    util_daily_report: bool = True    # 10: per-anchor markdown report from the trades CSV (read-only)
    util_preflight: bool = True       # 11: boot self-check (offset detected / anchors / flags / market)
    # STRATEGY EXTRAS (12-14): WIRED but DEFAULT OFF -- owner flips post-trial, one at a
    # time, measured against the logged baseline. OFF -> v3.5.0 core behavior unchanged.
    entry_confirm_candle: bool = False  # 12: require an M5 close in the entry direction
    #   before filling (replaces first-touch) in BOTH the rally + rescue adaptive paths.
    entry_adaptive_depth: bool = False  # 13: pullback/bounce depth scales with recent ATR
    #   instead of the fixed $13/$6. OFF -> fixed depths.
    atr_period: int = 14              # 13: ATR lookback (M5 bars) when entry_adaptive_depth ON.
    atr_mult: float = 1.0             # 13: depth = atr_mult * ATR when entry_adaptive_depth ON.
    rescue_sl_wide: bool = False      # 14: widen the RESCUE boost SL $10 -> rescue_sl_wide_dollars.
    #   The rescue cap is DERIVED (count x SL x lot x 100) so this MOVES it -$700 -> -$910
    #   (recomputed via boosts.boost_sl_for; asserted in the self-test). OFF -> $10 / -$700.
    rescue_sl_wide_dollars: float = 13.0  # 14: the wide RESCUE SL when rescue_sl_wide ON.
    # HOTFIXES (15-16): telemetry/safety correctness, no order-logic change -- DEFAULT ON.
    fix_boost_telemetry: bool = True  # 15: emit the boost trail-advance (LOCK_ARM/TRAIL_ADVANCE)
    #   so a boost trail EXIT is never falsely flagged exit_trail_without_trail_advance.
    #   OFF restores the pre-v3.3.0 silent emission (telemetry noise only; no P&L effect).
    fix_a1_offset: bool = True        # 16: the A1 wake offset detector retries/awaits a fresh
    #   tick and NEVER falls back to 0h (already enforced by mt5_adapter._detect_tick_time_
    #   offset Tier 1/2; asserted in the self-test). OFF does NOT re-introduce a 0h guess --
    #   an undetected offset still BLOCKS placement (fail-safe; never trade on a wrong offset).
    # --- ROGUE: the self-anchoring monster-rider (SEPARATE from the clock anchors) ---
    # Rogue plants its OWN price-anchor where a strong move completes, then hunts the next
    # leg, reusing the rally/rescue/trail HELPERS from that anchor -- but ROGUE-tagged and
    # closed only against its own magic/label. rogue_enabled is the single master switch.
    # FREEZE: the RAW default is False -> all-flags-off == master (byte-identical). DEMO
    # default-ON is a RUNTIME promotion (rogue.funded_default: the boot sets it True on a
    # demo / non-funded account); a FUNDED account FORCE-disables it (rogue.should_run --
    # mandatory gate, un-proven Rogue never boots ON on real capital). With it OFF there
    # is NO watching, anchoring, or entering. Rogue is the deliberate demo-only exception;
    # all OTHER strategy flags stay default OFF.
    rogue_enabled: bool = False        # MASTER SWITCH (raw default OFF; demo boot promotes ON)
    rogue_daywatch: bool = True        # continuous M5 vision (only meaningful when rogue_enabled)
    rogue_reuse_rally: bool = True     # ride/pyramid via RALLY logic on strong continuation
    rogue_reuse_rescue: bool = True    # hedge via RESCUE logic when the catch goes against
    rogue_max_reentries_per_day: int = 10   # HARD ceiling on NEW entries/day (the cap)
    rogue_min_candles: int = 4         # strong-move trigger: >= this many same-dir M5 closes
    rogue_min_range: float = 15.0      # ... AND total range >= $15
    rogue_body_mult: float = 1.5       # ... AND combined body >= this x avg bar range (thrust)
    rogue_entry_confirm: float = 20.0  # early entry: enter ~$20 in off the anchor (not the top)
    rogue_init_sl: float = 5.0         # tight initial stop -> a fake-out is a small capped loss
    rogue_trail_arm: float = 5.0       # profit ($) before the adaptive trail engages
    rogue_trail_gap_early: float = 3.0 # tight trail until deep in profit (protect vs fake-out)
    rogue_trail_gap_deep: float = 6.0  # wider once proven (don't shake out a real monster)
    rogue_trail_widen_at: float = 15.0 # profit ($) at which the trail widens 3 -> 6
    # GOVERNORS on the 10-cap (mandatory brakes for a thin edge):
    rogue_daily_loss_stop: float = -525.0   # E-5 (owner decision): Rogue STOPS new entries
    # for the day at -$525 (was -$150). Rationale: one init-SL strike is -$175 (rogue_init_sl
    # $5 x 0.35 x 100), so the old -$150 halted Rogue on the FIRST fake-out -- the
    # 3-consecutive-fail pause and the 10/day cap could never engage. At -$525 = 3 init-SL
    # strikes, the 3-fail pause (and small-fakeout streaks) can fire BEFORE the daily halt,
    # while a genuine whipsaw day still hard-stops at -$525.
    rogue_consecutive_fail_stop: int = 3    # 3 init-SL fake-outs in a row -> pause new entries
    # E-4: at EOD, flatten an OPEN Rogue position instead of letting it ride overnight
    # on its own SL/TP. DEFAULT ON since 2026-07-02 (owner decision): an overnight /
    # weekend gap can jump straight past the resting SL, and E-15's gating already
    # blocks NEW Rogue entries post-EOD -- flipping this ON closes the existing-
    # position side of the same hole. Rogue-scoped (closes ONLY the Rogue ticket;
    # never an anchor 20260522 ticket). Set False to restore the overnight ride.
    rogue_flatten_at_eod: bool = True
    # E-5 (surfaced by E-2): one init-SL fake-out books -$175 (rogue_init_sl $5 x 0.35 x
    # 100 = 175) which already trips this -$150 daily stop -> a SINGLE strike halts Rogue
    # for the day, so the 3-consecutive-fail pause can essentially never fire. Left at -150
    # pending the owner's call (keep one-strike-halt, or scale to -525 for 3 strikes).
    # --- ML confidence gate (BOTH default OFF/safe -> pure pass-through) ----------
    # The gate at the Rogue entry point always computes + logs a model score, but only
    # BLOCKS a trade when rogue_model_gate_enabled is True AND the score is below
    # rogue_model_threshold. Default False => the gate is inert (logs the score, never
    # blocks); live behavior is byte-identical until a trained model is proven and the
    # flag is turned on. An untrained model / any predict error scores 1.0 (fail OPEN).
    rogue_model_gate_enabled: bool = False   # gate inert until proven
    rogue_model_threshold: float = 0.5       # min follow-through confidence to ENTER (when on)
    # --- Fix 4: Rogue A1-ANCHORED REDESIGN (NEW ENGINE, DEFAULT OFF) -------------
    # rogue_a1_anchor_mode OFF (default) -> the legacy monster-detection Rogue runs
    # byte-identically. ON -> a NEW engine: ANCHOR seeds from the day's A1 anchor price
    # (READ-ONLY cross-read from the anchor engine) and thereafter chains to the last
    # CLOSED Rogue level (no monster-detection wait); ENTRY fires once price moves
    # rogue_entry_confirm_redesign ($10) off the anchor, in the move direction, with a
    # tight init SL; same-dir CONTINUATION rides via the existing trail/rally machinery;
    # a confirmed REVERSAL (price crosses entry AND moves rogue_reversal_dollars ($10) PAST
    # entry against the trade -- measured in DOLLARS, not candles) closes the wrong-way leg
    # and recovers in the NEW direction (capped, NOT a two-way hedge). rogue_daily_soft_lock
    # ($30) is a soft floor that is BANKED but NEVER a hard stop (keep hunting). The BRAKE
    # is Fix 3's live daily loss stop + rogue_rescue_cap_dollars ($13) per recovery leg.
    # Engine-gated keys: inert/no-op while rogue_a1_anchor_mode is OFF. Rogue-only
    # (magic 20260626); the A1 read is READ-ONLY and never closes an anchor 20260522 leg.
    # DEFAULT ON (P1): the A1-anchored engine is the live Rogue engine. This is still
    # freeze-safe -- the all-flags-off==master freeze is gated by rogue_enabled (raw default
    # OFF), so with rogue OFF this key is never reached. (Closes the E-1 ghost: the stale
    # "DEFAULT OFF / legacy monster is live" comment no longer matched the live config.)
    rogue_a1_anchor_mode: bool = True           # Fix 4 master flag (DEFAULT ON, live engine)
    rogue_entry_confirm_redesign: float = 10.0  # $ off the anchor to ENTER in the move dir
    rogue_reversal_dollars: float = 10.0        # $ PAST entry against the trade = reversal
    rogue_daily_soft_lock: float = 30.0         # soft banked floor ($) -- NEVER a hard stop
    rogue_rescue_cap_dollars: float = 13.0      # per-recovery-leg SL cap on a reversal
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
    # --- F-B: trapped-leg CAPPED late-rescue (No-OCO whipsaw), DEFAULT OFF -------
    # Today a No-OCO LOSING straddle leg is boost_rally_only (allow_rescue=False) and
    # rides NAKED to its full -$18 SL (-$630 @ 0.35). F-B lets that trapped leg arm a
    # CAPPED late-rescue hedge (opposite direction) once it is trapped_rescue_arm_dollars
    # adverse from its fill, so the slide to the SL is partly recovered. CRITICAL: the
    # late hedge has its OWN hard SL (trapped_rescue_sl_dollars) + a per-event combined
    # cap -- a naked late hedge would DOUBLE the loss on a reverse-whipsaw; this bounds
    # it. Anchor-side ONLY (never touches a Rogue 20260626 ticket). DEFAULT OFF =>
    # byte-identical (the losing leg still rides to -$630) until the owner flips it.
    trapped_late_rescue_enabled: bool = False   # F-B master flag (DEFAULT OFF, freeze-safe)
    trapped_rescue_arm_dollars: float = 10.0    # $ adverse from fill before the hedge arms
    trapped_rescue_sl_dollars: float = 13.0     # the late hedge's OWN hard SL ($/leg)
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
    # --- E-6 BOOST RIDES WITH PARENT (2026-06-30 A1; flag-gated, DEFAULT OFF) -----
    # The 06-30 A1 RALLY boosts armed on their OWN small peak, floored at +$3, and
    # software-exited at 4001.61 (+$105) on the first bounce while the parent anchor
    # SELL rode its broker trail to 3997.27 (+$491) -- same move, 4.7x the result. When
    # ON, an ARMED rally boost holds its software exit no TIGHTER than the parent anchor
    # leg's current trailing stop (resolved READ-ONLY via parent_ticket), so it rides at
    # least as long as the parent instead of bailing early. Bounded at the boost's own
    # breakeven so it can never ride into a loss. RALLY-only; RESCUE untouched. DEFAULT
    # OFF -> byte-identical until the owner validates it live.
    boost_ride_with_parent: bool = True
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
        # A3 CUT 2026-07-02 (owner decision, per-anchor P&L): A3_1430_Overlap removed
        # after two negative months on the journal -- June -$2,255 (PF 0.68), July
        # -$385. This executes the v2.9.4 rule above: persistent losers get cut on
        # the live record. The v3.3.6 retime (16:20 -> 17:00 IST) did not fix it.
        # Schedule-list change ONLY -- A1/A2/A4/A5 and all trade logic / sizing /
        # straddle / boost / rescue knobs are UNCHANGED. To restore, re-add
        # ("A3_1430_Overlap", 14, 30) here.
        ("A4_1640_NYopen", 16, 40),
        # v3.3.8: 5th anchor A5 at 22:00 IST = 19:30 broker (UTC+3; IST = broker +
        # 2:30). A NORMAL anchor -- identical structure to A1-A4 (straddle +/-$5,
        # SL $18 / TP $30 / lot 0.35 from the shared cfg knobs, same boost / rescue /
        # gate / pullback logic); a timing addition only, no special rules. Its own
        # label A5_1930_LateUS tags its trades distinctly so it is isolated and judged
        # on its own P&L + drawdown at month-end. Nearest neighbour A4 19:10 IST is
        # 2h50m clear (no collision). label[:2] == 'A5'.
        ("A5_1930_LateUS", 19, 30),
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

    # --- E-12 FEED-DEATH WATCHDOG (2026-06-30 incident) -------------------------
    # That morning the XAUUSD subscription dropped 06:00-10:08 and `_market_closed_now`'s
    # probe raised "symbol_info_tick returned None -- symbol not subscribed?" ~13,833 times:
    # the bot logged a warning EVERY tick, never re-subscribed, never alerted, and went
    # blind through the morning monster. This watchdog (feed_watchdog.FeedWatchdog, wired in
    # _market_closed_now) re-subscribes on repeated failures and fires ONE throttled FEED DOWN
    # alert. feed_watchdog_enabled=False -> byte-identical to the pre-fix per-tick warning.
    feed_watchdog_enabled: bool = True    # ON by default -- it is a safety net, never trades
    feed_recover_after_fails: int = 30    # consecutive 'not subscribed' failures before a re-subscribe
    feed_recover_max_tries: int = 5       # failed re-subscribe attempts before the first FEED DOWN alert
    feed_alert_cooldown_min: float = 5.0  # min minutes between FEED DOWN alerts (then it keeps retrying)
    # Fix 4 (E-12) escalation ladder beyond re-subscribe: L2 full in-process MT5 reinit
    # (shutdown->initialize->select->verify fresh tick) once re-subscribe is exhausted OR the
    # feed has been blind past feed_reinit_blind_min; L3 controlled self-restart (persist
    # state + sys.exit(42), relaunched by run_aureon.bat / Task Scheduler) once the reinits
    # fail. Never self-restarts when the market is closed. All ON by default (safety nets).
    feed_reinit_blind_min: float = 3.0    # blind minutes that escalate straight to a full reinit
    feed_reinit_max_tries: int = 2        # full-reinit attempts before the self-restart escalation
    feed_selfrestart_enabled: bool = True # L3 controlled self-restart (exit 42) when the feed is dead
    # Fix 1 (E-13): route order sends through the SHARED place_with_retry wrapper (rc-check +
    # bounded retry + abort-alert; never resizes the lot). ON by default; False falls back to
    # the prior single-send path per order (an escape hatch; the wrapper never touches
    # geometry or lot size on the success path, so ON is byte-identical when orders fill).
    order_retry_enabled: bool = True

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
