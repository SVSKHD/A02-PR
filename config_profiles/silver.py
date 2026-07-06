"""SILVER (XAGUSD) profile — measured Pepperstone-Demo #61533831, 2026-07-06,
verify_silver_market.py 11 gates PASS. Values are the owner-approved scaled
set; every key is a real Config field (loader rejects unknowns loudly)."""
PROFILE = dict(
    symbol="XAGUSD", contract_size=5000.0,
    anchor_magic=20260710, rogue_magic=20260711, price_digits=3,
    starting_balance=49579.39,
    trigger_dist=0.17, sl_dist=0.61, tp_dist=1.017, lot_size=0.21,
    be_trigger=0.085, trail_gap=0.068, min_step=0.003, arm_buffer=0.056,
    max_tick_jump=0.848, trail_arm_profit=0.271, tstop_fav=0.034,
    break_dist_x=0.102, parent_established_dollars=0.407,
    override_entry_pullback_dollars=0.441, rescue_entry_bounce_dollars=0.203,
    boost_trigger_dollars=0.339, boost_sl_dollars=0.339, rally_boost_sl=0.441,
    boost_trail_gap_dollars=0.119, boost_trail_arm_fav=0.271,
    boost_lock_floor=0.271, rally_arm_fav=0.17, rally_lock_floor=0.102,
    rally_trail_gap=0.068, trapped_rescue_arm_dollars=0.339,
    trapped_rescue_sl_dollars=0.441, fp_spread_buffer=0.045,
    rogue_entry_confirm_redesign=0.339, rogue_chase_cap_dollars=0.678,
    rogue_init_sl=0.17, rogue_reversal_dollars=0.339,
    rogue_rescue_cap_dollars=0.441, rogue_chain_min_displacement=0.203,
    rogue_trail_arm=0.17, rogue_trail_gap_early=0.102,
    rogue_trail_gap_deep=0.203, rogue_trail_widen_at=0.509,
    rogue_min_range=0.509, rogue_entry_confirm=0.678,
    anchor_drift_tol=0.005, recovery_slip_min=0.017, recovery_slip_max=0.51,
    sl_mismatch_alert=0.015, shadow_entry_tol=0.02, broker_sl_assert_tol=0.005,
    # unchanged by design: rogue_daily_loss_stop=-525.0 (dollars),
    # rogue_chain_cooldown_sec=300.0 (time), counts, pct governors,
    # freeze_minutes=45, hold_candles_n=2, max_retrace_y=0.40,
    # tp_detect_tol (gold 0.05 kept), anchors schedule (runtime-restricted
    # to A1+A2 week one via /anchors, NOT edited here).
)
