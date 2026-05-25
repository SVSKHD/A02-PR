# AUREON v2 — Strategy Specification

*Multi-anchor anchor-breakout strategy for XAUUSD (gold) on M1.*
*Successor to AUREON v1 (Single-OCO, single-anchor). Validated on 12 months of real broker M1 (May 2025 – May 2026).*

---

## 1. Asset & environment

| Item | Value |
|------|-------|
| Symbol | XAUUSD (Gold spot, USD) |
| Contract | 100 oz per 1.0 lot |
| Price precision | $0.01 |
| Broker timezone | UTC+3 (EEST) |
| Anchor capture | M5 bar |
| Trade walk | M1 bar |
| Days traded | Monday–Friday only |

---

## 2. The Four-Anchor Schedule

Each business day, AUREON v2 captures **four independent anchors**, one per major gold session open. Each anchor is its own **dual-side fill-or-kill** instrument (described below).

| Anchor | Broker time | UTC time | Session |
|--------|------------|----------|---------|
| A1 | 02:00 | 23:00 prior day | Asia/Sydney open |
| A2 | 10:00 | 07:00 | London open |
| A3 | 14:00 | 11:00 | London–NY overlap |
| A4 | 17:00 | 14:00 | NY post-open |

Sessions are intentionally spaced **3–8 hours apart**. Closer than ~3 hours and anchors capture overlapping moves → correlated trades, doubled SL risk for no extra edge. Past 4 anchors, returns flatten and drawdown increases. **4 anchors is the validated sweet spot.**

---

## 3. Per-Anchor Trade Logic

At each anchor time:

1. **Capture anchor:** `anchor = close of M5 bar ending at anchor_time`
2. **Place two pending stop orders simultaneously:**
   - Buy stop  at `anchor + $5.00`
   - Sell stop at `anchor − $5.00`
3. **Dual-side fill-or-kill (OCO):**
   - First fill = the trade for that anchor
   - The opposite pending is **killed immediately**
   - Maximum 1 position per anchor (so up to 4 simultaneous positions across the day, one per anchor, all independent)
4. **Manage filled position** with the per-trade rules in §4.
5. **EOD:** at 23:00 broker, cancel any unfilled pendings AND close any still-open positions at last bar's close.

### Per-trade parameters

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `TRIGGER_DIST` | $5.00 | anchor → pending entry distance |
| `TP_DIST` | $20.00 | entry → take-profit distance (hard exit) |
| `SL_DIST` | $20.00 | entry → initial stop-loss distance |
| `LOT_SIZE` | 0.5 | default; risk = $1,000/SL/leg at 0.5 lot |
| `BE_TRIGGER` | $0.30 | favorable move that arms breakeven trail |
| `TRAIL_GAP` | $0.30 | SL trails this far behind peak favorable |
| `MIN_STEP` | $0.00 | (clean math; set to $0.10 in live to avoid micro-adjustments) |

### Continuous trail logic (per filled position, independently)

At fill: `current_sl = entry ∓ $20`, `max_fav = entry`, `be_armed = False`.

Each subsequent M1 bar, **in this exact order**:

1. **Pre-bar SL check** (pessimistic — assume worst intrabar order):
   - BUY:  if `bar.low ≤ current_sl` → exit at `current_sl`. Stop.
   - SELL: if `bar.high ≥ current_sl` → exit at `current_sl`. Stop.
2. **Update peak favorable:**
   - BUY:  `max_fav = max(max_fav, bar.high)`
   - SELL: `max_fav = min(max_fav, bar.low)`
3. **Favorable distance:** `fav = |max_fav − entry|`, clamp ≥ 0.
4. **Compute candidate SL:**
   - if `fav < $0.30`: SL stays.
   - if `fav ≥ $0.30`:
     - BUY:  `candidate_sl = max(entry, max_fav − $0.30)`
     - SELL: `candidate_sl = min(entry, max_fav + $0.30)`
   - At exactly `fav = $0.30`, candidate_sl = entry. **Critical:** SL goes to entry, not entry+0.30. This $0.30 buffer between price and SL is what prevents whipsaw exits.
5. **One-way ratchet:**
   - BUY:  if `candidate_sl > current_sl + MIN_STEP` → `current_sl = candidate_sl`
   - SELL: if `candidate_sl < current_sl − MIN_STEP` → `current_sl = candidate_sl`
6. **TP check** against this bar's favorable extreme:
   - BUY:  if `bar.high ≥ entry + $20` → exit at `entry + $20`. Stop.
   - SELL: if `bar.low ≤ entry − $20` → exit at `entry − $20`. Stop.

### Exit priority each bar
1. SL touched (pre-bar check)  → exit at `current_sl`
2. TP touched                  → exit at `entry ± $20`
3. EOD 23:00 broker            → exit at last bar's close

---

## 4. Risk Management

### Daily kill switch
Compute running daily P&L across all 4 anchors at any moment. If `daily_pnl ≤ −4% of account balance` (default $50k → −$2,000), then:
- Immediately close all open positions at market
- Cancel all remaining pending orders
- **Skip remaining anchors for the calendar day**
- Resume next business day

### Per-anchor risk cap
Each anchor's max loss = `SL_DIST × 100 × LOT_SIZE` = $1,000 at 0.5 lot.
Theoretical max daily loss (all 4 anchors SL same day) = $4,000 = 8% of $50k.
The kill switch caps this at 4%.

### Weekly stop
If weekly P&L ≤ −8% of balance, halt trading until manual review.

### Lot sizing
Default $0.5$ lot per leg. Suggested scaling rules:
- Account ≥ $50k: 0.5 lot/leg
- Account ≥ $100k: 1.0 lot/leg
- Never exceed 2% account risk per single SL

---

## 5. Realism / Live-Live Adjustments

These were "no" in the v1 backtest spec. In **live deployment**, model them:

| Item | Backtest assumption | Live reality |
|------|---------------------|--------------|
| Spread | None | ~$0.15 typical, $0.30+ during news. Subtract from every entry/exit. |
| Slippage | None | $0.05–$0.20 on stop-order fills, larger on news. |
| Commission | None | Broker-dependent (~$3.50/lot round-trip typical). |
| Swap/overnight | None | XAUUSD swaps are minor on daily intraday positions. |
| News blackout | None | **Add a $\pm 5$ min window around high-impact USD news (NFP, FOMC, CPI). Don't open during.** |
| Order rejection | None | Implement retry logic and fallback to market order if stop-order rejected. |

**Expected live performance vs backtest:** 10–20% degradation on total pips, primarily from spread + slippage on the small $0.30 trail.

---

## 6. Validated Performance (12-month backtest, 2025-05 → 2026-05)

### Aggregate
| Metric | Value |
|--------|------:|
| Total fills | 966 |
| Total pips secured | +944 |
| Total USD @ 0.5 lot | +$47,202 |
| Average per month (pips) | +73 |
| Win rate | 96.5% |
| SL count (full-stop -$20 trades) | 28 |
| Max drawdown | −$2,000 (−4.0% of $50k) |
| Days hitting −$2,000 daily limit | 0 |
| Worst single day | −$1,747 |
| Best single day | +$1,331 |

### Monthly breakdown
| Month | Pips | USD @ 0.5 lot |
|-------|-----:|--------------:|
| 2025-06 |  +49 |  +$2,448 |
| 2025-07 |  +57 |  +$2,859 |
| 2025-08 |  +45 |  +$2,230 |
| 2025-09 |  +33 |  +$1,641 |
| 2025-10 | +142 |  +$7,082 |
| 2025-11 | +118 |  +$5,890 |
| 2025-12 |  +74 |  +$3,695 |
| 2026-01 |  +95 |  +$4,756 |
| 2026-02 |  +80 |  +$4,016 |
| 2026-03 | +122 |  +$6,097 |
| 2026-04 |  +96 |  +$4,816 |

### Per-anchor productivity (annualized)
| Anchor | Pips/yr | SL rate |
|--------|--------:|--------:|
| A1 02:00 Asia    | +226 | 3.5% |
| A2 10:00 London  | +267 | 1.2% (best) |
| A3 14:00 Overlap | +235 | 3.5% |
| A4 17:00 NY      | +216 | 3.5% |

**Realistic live expectation: +60–70 pips/month average after spread/slippage drag.**

---

## 7. What was rejected (and why)

| Variant | Result | Why rejected |
|---------|--------|--------------|
| Dual-bracket (both legs run independently) | +39 pips/mo single-anchor | Adds catastrophic both-legs-SL risk. User preference: fill-or-kill only. |
| Step-ladder exit (lock+3, advance in $3 steps) | −6 pips/mo | Too tight on chop, doesn't outperform $0.30 trail on trends. |
| Hybrid (lock at +3, partial 30% at +10, trail $5) | −9 pips/mo | $5 trail bleeds wins back to market. |
| +15 long trigger / −10 short trigger | Asymmetric SL exposure | 27% SL rate vs <5% on $5/$20 spec. |
| 5–6 anchors per day | +68–69 pips/mo | Adjacent anchors capture overlapping moves → no edge gain, more SLs. |
| 4-anchor with pre-London 08:00 anchor | +56 pips/mo | Pre-London is a volatility dead zone; that anchor produces 13 SLs vs A2's 3. |

---

## 8. Implementation requirements

### Backtest mode
- Input: M1 CSV with columns `time, open, high, low, close, tick_volume, spread, real_volume`
- Times in UTC; broker is UTC+3
- Run from `start_date` to `end_date`
- Output: per-trade CSV, monthly summary CSV, equity curve

### Live / paper mode
- MT5 connection via `MetaTrader5` Python package (or REST broker API)
- Wake up at each anchor time minus 1 minute; fetch M5 close at anchor minute
- Place 2 stop orders; track which fills; manage independently
- Update SL every M1 bar close
- Honor daily/weekly kill switches
- Persist state across restarts (positions, daily P&L, last anchor processed)
- Log every action (anchor capture, order placement, fill, SL move, exit, kill-switch trigger)

### Required guardrails
- Verify broker server time = UTC+3 on startup; refuse to run if mismatched
- Refuse to open new positions if account balance < starting × 0.85 (15% drawdown circuit breaker)
- Refuse to place orders within ±5 minutes of major scheduled news (NFP, FOMC, CPI)
- Heartbeat alert if no anchor processed in 8 hours during business days

---

## 9. Out of scope (future work)
- Volatility filter (skip days with prior-day range < threshold)
- Per-anchor lot sizing based on historical anchor-specific edge
- Combining XAUUSD with NAS100 or oil on same scheduler
- Machine-learned anchor selection (skip a session if conditions disfavor it)
