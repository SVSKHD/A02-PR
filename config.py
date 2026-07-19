"""AUREON — Config dataclass (split from bot.py, v3.0.0). Byte-identical."""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
@dataclass
class Config:
    symbol: str = "XAUUSD"
    contract_size: float = 100.0
    # ── ANCHORS — core straddle engine ─────────────────────────────
    # ON always. 4 clock anchors/day, non-OCO ±trigger_dist, lot 0.35.
    # trigger 18 / SL 18 / TP 30. Halts for the day after one full SL (-630).
    trigger_dist: float = 18.00
    tp_dist: float = 30.00
    sl_dist: float = 18.00
    # EARLY LOCK — green never turns red: at +2.50 peak, floor SL at entry+1.00.
    # Turn OFF if too many legs scratch at +1 that would have run.
    early_lock_enabled: bool = True
    early_lock_arm: float = 2.50
    early_lock_floor: float = 1.00
    lot_size: float = 0.35
    be_trigger: float = 2.50
    trail_gap: float = 2.00
    min_step: float = 0.10
    # STALE LEG SWEEP — cancels leftover legs from prior anchors once price
    # reaches the next anchor. Keep ON. Exempts RB / RGS / TF_ orders.
    stale_leg_sweep_enabled: bool = True
    stale_leg_interval: float = 20.0
    # ── RESCUE BOOST v2 (THE rescue; Hithesh 15/25 design) ─────────
    # On any anchor fill: pending stops 0.45 at entry -15 and -25 (mirror short).
    # Fills before the -18 SL; trails +10/gap 5; pendings cancelled if parent
    # closes green. ONLY rescue allowed ON — keep the 4 legacy flags below OFF.
    rescue_boost_v2_enabled: bool = True
    rescue_boost_v2_lot: float = 0.45
    rescue_boost_v2_offset_1: float = 15.0
    rescue_boost_v2_offset_2: float = 25.0
    rescue_boost_v2_trail_activation: float = 10.0
    rescue_boost_v2_trail_gap: float = 5.0
    rescue_boost_v2_max_boosts: int = 2
    arm_buffer: float = 1.50
    max_tick_jump: float = 25.0
    break_and_hold_enabled: bool = True
    rescue_bypass_break_and_hold: bool = True
    break_timeframe: str = "M5"
    break_dist_x: float = 3.0
    hold_candles_n: int = 2
    max_retrace_y: float = 0.40
    parent_profit_override_enabled: bool = True
    parent_established_dollars: float = 12.0
    override_entry_enabled: bool = False
    override_entry_pullback_dollars: float = 13.0
    override_entry_arm_timeout_candles: int = 4
    override_entry_smooth_confirm: bool = True
    override_entry_dynamic_sl: bool = True
    rescue_entry_enabled: bool = False
    rescue_entry_bounce_dollars: float = 6.0
    rescue_entry_arm_timeout_candles: int = 4
    rescue_entry_smooth_confirm: bool = True
    util_pullback_log: bool = True
    util_boost_ledger: bool = True
    util_daily_report: bool = True
    util_preflight: bool = True
    util_daily_pnl_report: bool = True
    entry_confirm_candle: bool = False
    entry_adaptive_depth: bool = False
    atr_period: int = 14
    atr_mult: float = 1.0
    rescue_sl_wide: bool = False
    rescue_sl_wide_dollars: float = 13.0
    fix_boost_telemetry: bool = True
    fix_a1_offset: bool = True
    # ── ROGUE — monster-move engine ─────────────────────────────────
    # rogue_enabled = master switch (funded accounts force OFF).
    # rogue_stop_mode True = pending-stop engine (anchor ±17, chain +12,
    # init SL 10) — the live design. False = legacy band engine (do not use).
    # Boot banner must read 'ROGUE IMPL: stop' — if it says band, config lost.
    rogue_enabled: bool = True
    rogue_daywatch: bool = True
    rogue_max_reentries_per_day: int = 10
    rogue_min_candles: int = 4
    rogue_min_range: float = 15.0
    rogue_body_mult: float = 1.5
    rogue_entry_confirm: float = 20.0
    rogue_init_sl: float = 10.0
    rogue_trail_arm: float = 5.0
    rogue_trail_gap_early: float = 3.0
    rogue_trail_gap_deep: float = 6.0
    rogue_trail_widen_at: float = 15.0
    rogue_daily_loss_stop: float = -370.0
    rogue_daily_profit_stop: float = 400.0
    rogue_consecutive_fail_stop: int = 3
    rogue_flatten_at_eod: bool = True
    rogue_model_gate_enabled: bool = False
    rogue_model_threshold: float = 0.5
    rogue_a1_anchor_mode: bool = True
    rogue_entry_confirm_redesign: float = 12.0
    rogue_reversal_dollars: float = 10.0
    rogue_daily_soft_lock: float = 30.0
    rogue_rescue_cap_dollars: float = 13.0
    rogue_chase_cap_dollars: float = 20.0
    rogue_chain_cooldown_sec: float = 300.0
    rogue_chain_min_displacement: float = 6.0
    # RUNAWAY RE-ANCHOR — band-engine fallback only; inert in stop mode. OFF.
    rogue_runaway_reanchor_enabled: bool = False
    rogue_runaway_trigger: float = 25.0
    rogue_runaway_confirm: float = 8.0
    rogue_stop_mode: bool = True
    rogue_trigger: float = 17.0
    rogue_chain_step: float = 12.0
    rogue_stop_init_sl: float = 10.0
    rogue_anchor_grace_min: float = 10.0
    # ── ROGUE MONSTER ENGINE — the LIVE Rogue engine (magic 20260626) ──────────
    # The sole Rogue implementation (drive() -> rogue_monster_live.drive_monster).
    # Boot banner reads 'ROGUE IMPL: monster'. Values are the rp2-validated set
    # (+$19.4k May/Jun/Jul @ 0.35). Arming gate -> H1/M15 bias -> stop entry ->
    # chain -> trail; adaptive guards caution/giveback/red-day/side-fatigue.
    # NOTE: reuses rogue_chain_step (12) above; legacy stop/band keys above are
    # inert (dead-key removal + selftest cleanup tracked as follow-up).
    rogue_lot: float = 0.35
    rogue_atr_mult: float = 1.5            # M5 range expansion vs ATR(20)
    rogue_atr_period: int = 20
    rogue_vel_points: float = 12.0         # M1 velocity threshold
    rogue_vel_minutes: int = 5
    rogue_box_bars: int = 12               # M5 consolidation-box length
    rogue_box_max_range: float = 8.0       # box qualifies if range <= this (pts)
    rogue_disarm_bars: int = 6             # hysteresis: disarm after N quiet bars
    rogue_edge_offset: float = 1.0         # stop beyond the box edge
    rogue_fallback_trigger: float = 17.0   # anchor +/- when velocity-armed, no box
    rogue_sl_cap: float = 10.0             # hard SL cap behind entry
    rogue_max_chains: int = 3
    rogue_trail_start: float = 10.0        # trail arms at +this
    rogue_trail_gap: float = 5.0
    rogue_day_loss_halt: float = -1000.0   # governor: halt day at this realized loss
    rogue_profit_lock: float = 1000.0      # governor: lock day at this realized gain
    rogue_max_entries: int = 10
    rogue_consec_sl_limit: int = 2         # caution after N straight full SLs
    rogue_caution_cooldown_min: int = 90
    rogue_caution_atr_boost: float = 0.5   # gate tightening while in caution
    rogue_day_profit_trail_start: float = 600.0   # giveback arms once day P/L >= this
    rogue_day_profit_giveback: float = 300.0      # ...halt if it retraces by this
    rogue_redday_atr_step: float = 0.5     # next day after a red day starts tightened
    rogue_side_fatigue_sl: int = 2         # N same-side SLs -> that side needs real bias
    rogue_reanchor_cooldown_s: int = 300
    rogue_bias_m15_lookback: int = 8
    rogue_bias_h1_lookback: int = 4
    rogue_candle_confirm: bool = False     # M5 engulfing/dragonfly confirm; inert while False
    account_profile: str = "STANDARD_5PCT"
    fp_standard_pct: float = 0.05
    fp_zero_pct: float = 0.01
    fp_spread_buffer: float = 0.60
    allow_5_long: bool = True
    trail_arm_profit: float = 8.0
    a1_tick_fallback_enabled: bool = True
    a1_tick_fallback_samples: int = 6
    hold_ticks: int = 3
    tick_refresh_s: float = 0.3
    freeze_minutes: int = 45
    no_oco: bool = True
    # ── LEGACY RESCUE PATHS — all four MUST stay OFF (superseded by v2).
    # Turning any ON stacks multiple rescues on one loser = pileup.
    rescue_boost_enabled: bool = False
    rescue_boost_count: int = 2
    stack_depth: Optional[int] = None
    boost_sl_dollars: float = 10.0
    rally_boost_sl: float = 13.0
    boost_trigger_dollars: float = 10.0
    # ── RALLY BOOSTS — winning-side pyramid (arm +5, floor +3, gap 2).
    # ON by default. Turn OFF if boosts keep filling at tops right before
    # the parent trails out (watch the journal for top-fill pattern).
    rally_boosts_enabled: bool = True
    rescue_boosts_enabled: bool = False
    trapped_late_rescue_enabled: bool = False
    trapped_rescue_arm_dollars: float = 10.0
    trapped_rescue_sl_dollars: float = 13.0
    # ── SPEC v2/v3 BAND BOOSTS — $1.50 ratchet clipper. Keep OFF.
    boost_spec_v2: bool = False
    spec_break_dollars: float = 1.00
    spec_boost2_gap: float = 4.00
    spec_boost_min_lock: float = 1.50
    spec_boost_sl_dollars: float = 10.0
    boost_spec_v3_enabled: bool = False
    boost_confirm_dwell_s: float = 12.0
    boost_confirm_ext: float = 1.50
    boost_trail_gap_dollars: float = 3.50
    boost_trail_arm_fav: float = 8.0
    boost_lock_floor: float = 8.0
    max_boost_stack: int = 5
    rally_arm_fav: float = 5.0
    rally_lock_floor: float = 3.00
    rally_trail_gap: float = 2.00
    boost_ride_with_parent: bool = True
    rally_pullback_enabled: bool = False
    rally_pullback_tol_dollars: float = 7.50
    rally_pullback_time_bound_min: float = 30.0
    tstop_fav: float = 1.00
    tstop_after_min: int = 45
    auto_lot: bool = False
    lot_conservatism: float = 0.99
    risk_pct_under_50k: float = 0.03
    risk_pct_over_50k: float = 0.02
    slippage_buffer: float = 0.98
    anchors: List[Tuple[str, int, int]] = field(default_factory=lambda: [
        ("A1_02h_Asia", 2, 30),
        ("A2_10h_London", 10, 0),
        ("A4_1640_NYopen", 16, 40),
        ("A5_1930_LateUS", 19, 30),
    ])
    monday_a1_override: Optional[Tuple[int, int]] = (3, 30)
    broker_tz_offset_hours: int = 3
    EXPECTED_BROKER_OFFSET_HOURS: int = 3
    eod_broker_hour: int = 23
    friday_flatten_enabled: bool = True
    friday_flatten_broker_hour: float = 22.5
    friday_flatten_poll_seconds: float = 30.0
    a5_skip_friday: bool = True
    a4_skip_friday: bool = True
    non_oco_enabled: bool = True
    rogue_seed_fallback: str = "a1_time_snapshot"
    # ── FETCHER — grinder engine, retired after 07-08/09 chop losses. OFF.
    fetcher_enabled: bool = False
    fetcher_trigger_dollars: float = 5.0
    fetcher_tp_dollars: float = 5.0
    fetcher_sl_dollars: float = 5.0
    fetcher_max_entries_per_day: int = 20
    fetcher_daily_loss_stop: float = -370.0
    fetcher_daily_profit_stop: float = 400.0
    fetcher_consecutive_fail_stop: int = 3
    fetcher_flatten_at_eod: bool = True
    fetcher_seed_fallback: str = "a1_time_snapshot"
    seed_break_dollars: float = 10.0
    engine_base_trades_per_anchor: int = 2
    engine_extend_requires_wins: int = 2
    engine_exhausted_gap_sec: float = 900.0
    anchors_daily_profit_stop: float = 800.0
    # ── DAILY HALT — one full anchor SL (18 x 0.35 x 100 = $630) stops NEW
    # anchor risk for the day. Testfire (TF_) trades excluded from this math.
    anchors_daily_loss_stop: float = -630.0
    account_daily_profit_stop_pct: float = 0.0
    account_target_pct: float = 0.00
    account_target_min_pct: float = 0.80
    account_target_final_anchor: str = "A4_1640_NYopen"
    account_target_skip_a5_when_met: bool = True
    account_target_giveback_dollars: float = 200.0
    starting_balance: float = 50000.0
    testfire_collision_min: int = 30
    daily_loss_pct: float = 0.05
    weekly_loss_pct: float = 0.08
    account_floor_pct: float = 0.85
    stale_tick_threshold_s: float = 60.0
    stale_retry_window_s: float = 90.0
    stale_retry_poll_s: float = 5.0
    anchor_late_window_min: int = 10
    feed_watchdog_enabled: bool = True
    feed_recover_after_fails: int = 30
    feed_recover_max_tries: int = 5
    feed_alert_cooldown_min: float = 5.0
    feed_reinit_blind_min: float = 3.0
    feed_reinit_max_tries: int = 2
    feed_selfrestart_enabled: bool = True
    order_retry_enabled: bool = True
    log_level: str = "INFO"
    state_file: str = "aureon_v2_state.json"
    realism_haircut_dollars: float = 1000.0
    alert_channels: List[str] = field(default_factory=lambda: ["discord"])
    discord_heartbeat_min: int = 60