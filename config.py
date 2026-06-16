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
    rescue_boost_sl: float = 6.0
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
