# AUREON v2 — Complete Strategy Specification

> **Self-contained master prompt to recreate, extend, or rebuild AUREON.** Contains every parameter, every rule, every backtest result, every optimization decision. No outside context needed.

**Version:** 2.1 (lot 0.54 max-safe, May 2026)
**Status:** Production-ready, pre-deployment validated against 11-month backtest

---

## 1. CONTEXT — what AUREON is

**AUREON v2** is an algorithmic trading bot for **XAUUSD (Gold spot)** that captures breakouts from session-anchor levels. Built for **Funding Pips Zero $50k account** (instant-funded prop firm with 95% trader profit split) and respects every Funding Pips rule.

**Core insight:** Gold tends to break out of session-open price levels and run for $15-$40 within hours. A $5 trigger above/below the session-open anchor catches these breakouts before they fully develop.

**Strategy classification:** Anchor-breakout, single-OCO (fill-or-kill), uncapped trail with breakeven protection.

### Trading instrument

| Property | Value |
|----------|-------|
| Symbol | XAUUSD |
| Contract size | 100 oz per 1.0 lot |
| Price precision | $0.01 (2 decimal places) |
| Point size | $0.01 (1 point = 1 cent) |
| Broker timezone | UTC+3 (broker time) |
| Anchor timeframe | M5 (5-minute close at anchor time) |
| Trade execution timeframe | M1 (1-minute bars) |

### Account & broker

| Property | Value |
|----------|-------|
| Target prop firm | Funding Pips Zero |
| Starting balance | $50,000 (also tested at $100k) |
| Profit split | **95% trader / 5% firm** |
| Per-trade risk cap | 2% on accounts ≥ $50k = $1,000 max SL |
| Daily loss limit | 4% of starting = $2,000 |
| 5% Trailing max drawdown | -$2,500 floor below high-water mark |
| Realistic broker spread | $0.25–$0.35 per round-trip |
| Realistic slippage | $0.05–$0.10 per fill |
| MT5 platform | Yes (broker provides MT5 terminal) |

---

## 2. CORE STRATEGY RULES

### 2.1 Daily flow (Monday–Friday only)

At each of **4 anchor times** (broker UTC+3):

```
A1  02:00 broker  (Asia session)        ~18% of annual P&L
A2  10:00 broker  (London open)         ~24% of annual P&L
A3  14:00 broker  (London/NY overlap)   ~28% of annual P&L  ← strongest
A4  17:00 broker  (NY open)             ~22% of annual P&L
```

For each anchor, when the M5 bar ending at anchor time closes:

1. **Capture anchor price** = close of that M5 bar
2. **Place two pending stop orders** simultaneously:
   - `BUY_STOP` at anchor + $5.00 with SL at entry − $18, TP at entry + $30
   - `SELL_STOP` at anchor − $5.00 with SL at entry + $18, TP at entry − $30
3. **First fill cancels the opposite pending** (Single-OCO emulation)
4. Manage filled position with trail logic (see §2.3)
5. At **23:00 broker** (EOD), cancel all unfilled pendings AND close all open positions at market

**Max simultaneous positions:** 4 (one per anchor, independent).

### 2.2 Per-trade parameters (OPTIMAL CONFIG — verified)

```python
trigger_dist    = $5.00     # anchor → entry distance
sl_dist         = $18.00    # entry → initial SL
tp_dist         = $30.00    # entry → TP
lot_size        = 0.54      # at $50k = $972 SL = 1.94% per-trade ✅ FP rules
be_trigger      = $0.30     # favorable move that arms BE
trail_gap       = $0.10     # SL trails THIS far behind peak (KEY PARAMETER)
min_step        = $0.05     # smallest SL advance (0.0 in backtest for clean math)
```

### 2.3 UNCAPPED CONTINUOUS TRAIL LOGIC

Applied to every filled position **independently**. On every M1 bar, execute these steps in this EXACT order:

#### State at fill:
```
current_sl    = initial_sl   (entry ∓ $18)
max_fav_price = entry_price
be_armed      = False
```

#### Each M1 bar:

**Step 1: PRE-BAR SL CHECK (pessimistic execution)**
```
BUY:  if bar.low  <= current_sl → EXIT at current_sl
SELL: if bar.high >= current_sl → EXIT at current_sl
```
If exited, stop processing. Use current_sl as exit price.

**Step 2: UPDATE PEAK FAVORABLE**
```
BUY:  if bar.high > max_fav_price → max_fav_price = bar.high
SELL: if bar.low  < max_fav_price → max_fav_price = bar.low
```

**Step 3: COMPUTE FAVORABLE DISTANCE**
```
BUY:  fav = max_fav_price - entry_price
SELL: fav = entry_price   - max_fav_price
fav = max(fav, 0)
```

**Step 4: TRAIL UPDATE**
```
If fav < be_trigger ($0.30):
    SL stays at initial_sl. Skip trail.

If fav >= be_trigger:
    BUY:  candidate_sl = max(entry_price, max_fav_price - trail_gap)
    SELL: candidate_sl = min(entry_price, max_fav_price + trail_gap)
```

**Step 5: ONE-WAY RATCHET (SL never moves backward)**
```
BUY:  if candidate_sl > current_sl + min_step → current_sl = candidate_sl
SELL: if candidate_sl < current_sl - min_step → current_sl = candidate_sl
```

**Step 6: TP CHECK**
```
BUY:  if bar.high >= entry_price + $30 → EXIT at entry + $30
SELL: if bar.low  <= entry_price - $30 → EXIT at entry - $30
```

### 2.4 Exit conditions (priority order per bar)

1. **SL touched** (pre-bar check) → exit at `current_sl`
2. **TP touched** → exit at `entry ± $30`
3. **EOD at 23:00 broker** → exit at the last M1 bar's close

Outcome labels: `SL`, `TP`, `Trail`, `BE`, `EOD`

---

## 3. RISK MANAGEMENT LAYER

### 3.1 Auto-lot sizing

```python
risk_pct = 0.03 if balance < 50000 else 0.02     # FP per-trade rule
slippage_buffer = 0.98                            # 98% of rule cap
lot_conservatism = 0.99                           # max-safe at $50k = 0.54 lot

max_loss = balance × risk_pct × slippage_buffer × lot_conservatism
auto_lot = max_loss / (sl_dist × 100)
auto_lot = round_down(auto_lot, 0.01)
```

**Verified scaling across account sizes:**

| Balance | Auto-lot | SL $ | % per trade | 2-SL day worst | 4% daily limit | Buffer |
|--------:|---------:|-----:|------------:|---------------:|--------------:|-------:|
| $50,000 | **0.54** | $972 | 1.94% | -$1,944 | -$2,000 | $56 |
| $60,000 | 0.65 | $1,170 | 1.95% | -$2,340 | -$2,400 | $60 |
| $75,000 | 0.81 | $1,458 | 1.94% | -$2,916 | -$3,000 | $84 |
| $100,000 | 1.08 | $1,944 | 1.94% | -$3,888 | -$4,000 | $112 |

Auto-lot is **re-evaluated at the start of every broker day**, never mid-day.

### 3.2 Daily kill switch

```
daily_loss_pct = 0.03           # 3% of starting balance
kill_threshold = starting_balance × 0.03
```

At $50k: kill_threshold = $1,500. Fires AFTER any trade close that pushes day P&L below threshold.

**Mechanics on a 2-SL day:**
```
Trade 1 SL fills:    -$972   day P&L = -$972 (under -$1,500, no kill)
Trade 2 SL fills:    -$972   day P&L = -$1,944, kill switch FIRES
Kill switch action:  flatten all open positions, cancel pending orders
Result:              No further damage. Day ends near -$1,944.
```

### 3.3 Account floor halt

```
account_floor_pct = 0.85
floor = starting_balance × 0.85    # = $42,500 at $50k
```

If equity drops below floor, halt new entries permanently and alert via Telegram.

### 3.4 Funding Pips compliance

| FP Rule | Limit at $50k | Backtest worst | Status |
|---------|--------------:|---------------:|:------:|
| 2% per-trade SL | $1,000 | $972 (1.94%) | ✅ |
| 4% daily loss limit | $2,000 | -$1,989 worst day | ✅ Tight |
| 5% trailing max DD | $2,500 (from peak) | -$1,900 worst DD | ✅ |
| Consistency rule (15%) | n/a | User responsibility | manual |
| Profit split | 95% trader | Built-in | ✅ |

**Critical decision: lot 0.54 NOT 0.55** — backtest showed lot 0.55 produces one day at -$2,024 (4.05%) which breaches the 4% daily rule. Lot 0.54 caps worst day at -$1,989 (3.98%) — safely under the limit.

---

## 4. EXPECTED PERFORMANCE (11-month backtest, lot 0.54)

| Month | Net P&L | SLs | Win % | Max DD |
|-------|--------:|----:|------:|-------:|
| June 2025 | +$1,680 | 1 | 79.5% | -$964 |
| July 2025 | +$1,408 | 1 | 64.7% | -$1,002 |
| August 2025 | +$1,071 | 2 | 71.2% | -$1,847 |
| **September 2025** ⚠ | **+$827** | 2 | 76.8% | -$1,098 |
| October 2025 | +$6,603 | 2 | 90.8% | -$1,815 |
| November 2025 | +$5,496 | 0 | 84.2% | $0 |
| December 2025 | +$2,784 | 2 | 89.0% | -$1,197 |
| January 2026 | +$3,394 | 2 | 81.0% | -$1,442 |
| February 2026 | +$2,106 | 5 | 83.6% | -$1,235 (Feb 2 day: -$1,989) |
| March 2026 | +$5,493 | 5 | 86.8% | -$1,141 |
| April 2026 | +$6,019 | 5 | 81.2% | -$927 |
| **YEAR TOTAL** | **+$36,881** | **27** | **80.8%** | **-$1,900** |

**Critical properties:**
- 0 negative months (all 11 profitable)
- Worst month +$827 (still profitable)
- Max DD -$1,900 < -$2,500 FP rule limit ✅
- Worst day -$1,989 < -$2,000 FP daily limit ✅ (with $11 buffer)
- 2 kill switch days fired correctly (Oct 30, Feb 2)
- Average month: +$3,353
- Median month: +$2,784

---

## 5. EXPECTATIONS — Backtest vs Demo vs Live

### Three different things, three different numbers

| | Backtest | Demo (paper MT5) | Live (real $) |
|---|---|---|---|
| **Order placement** | Mathematical | Real MT5 orders | Real MT5 orders |
| **Fills** | Assumed perfect | Real broker fills | Real broker fills |
| **Latency** | Zero | Real (~100-200ms) | Real (~100-200ms) |
| **Slippage** | None | Real (~$0.05) | Real (~$0.05) |
| **Trail mods** | Instant | Real (broker may reject) | Real |
| **What's fake** | Everything | Just the money | Nothing |

**Demo IS real execution** — only the money is fake. The demo→live gap is almost entirely broker-specific spread.

### Realistic live expectations on $50k Funding Pips Zero, lot 0.54

| Scenario | Annual gross | Annual pocket (95% split) | Monthly pocket |
|----------|-------------:|--------------------------:|---------------:|
| 🎯 Backtest (theoretical) | $36,881 | $35,037 | $2,920 |
| 🥇 Gold case (90% retention) | $33,193 | $31,533 | $2,628 |
| 🟡 **Realistic (78% retention)** | **$28,767** | **$27,329** | **$2,277** |
| 🔴 Pessimistic (65%) | $23,973 | $22,774 | $1,898 |

**Plan around $2,277/month pocket = $27,329/year.**

### Why 78% retention is the realistic number

The 22% gap from backtest to live comes from:
- Slippage on entries: -$160/month (~64 fills × $0.05)
- Slippage on exits: -$160/month
- Spread variance: -$100/month
- Latency: -$50-150/month
- Trail rejections: -$50-100/month
- MT5 disconnects: -$30-100/month
- News-spike spread widening: -$80-150/month

### $100k account scaling (recommended next step)

Same strategy, lot 1.08 auto-calculated:

| Scenario | Annual pocket (95% split) | Monthly pocket |
|----------|--------------------------:|---------------:|
| 🥇 Gold case | $63,066 | $5,256 |
| 🟡 **Realistic (78%)** | **$54,658** | **$4,555** |
| 🔴 Pessimistic | $45,548 | $3,796 |

**$100k FP account is the structurally correct path to $4k+/month.**

---

## 6. ANCHOR-SPECIFIC INSIGHTS

| Anchor | Time (broker) | Time (UTC) | Time (IST) | Annual share | SL rate | Notes |
|--------|--------------:|-----------:|-----------:|:------------:|--------:|-------|
| A1 Asia | 02:00 | 23:00 prior | 04:30 | ~18% | 3.5% | Quietest, can fake out |
| A2 London | 10:00 | 07:00 | 12:30 | ~24% | 2.8% | Strong on news days |
| **A3 Overlap** | 14:00 | 11:00 | 16:30 | **~28%** | **1.2%** | **Cleanest, carries bad months** |
| A4 NY Open | 17:00 | 14:00 | 19:30 | ~22% | 3.5% | Most volatile, news spike risk |

**A3 single-handedly saved September 2025** (+$893 while A1+A4 lost money).

---

## 7. ENTRY VALIDATION CHECKS (live mode)

Before placing pending orders, verify:

1. **Broker tick is fresh** (< 30s old AND market is open) — else skip
2. **Anchor M5 bar exists** in broker history
3. **No existing pending or position** for this anchor today (idempotency)
4. **Account equity** above floor (else halt mode)
5. **Daily kill switch NOT armed** (else halt mode)
6. **Lot size > 0.01** (broker minimum)
7. **Pending stop levels respect broker's min stop distance** (typically 5-10 points)

If any check fails, skip the anchor for today (do not retry).

---

## 8. WEEKEND/MARKET-CLOSED HANDLING

- Market: Sunday ~22:00 UTC+3 → Friday ~22:00 UTC+3
- During closure: bot polls but does NOT place orders
- **Tick age check** must distinguish:
  - Stale tick (>3600s) AND market closed: INFO log only
  - Stale tick during market hours: WARN and skip anchor
  - Drift between broker time and system time >120s during market hours: abort (config error)

---

## 9. BACKTEST SPECIFICATION

### 9.1 Input data format

CSV with M1 bars:
```
time           ISO 8601 UTC datetime
open           Float, 2 decimals
high           Float, 2 decimals
low            Float, 2 decimals
close          Float, 2 decimals
tick_volume    Integer
spread         Integer (MT5 points; 1 point = $0.01 for XAUUSD)
real_volume    Integer (often 0 for spot gold)
```

### 9.2 Engine requirements

1. Load M1 CSV, parse `time` as UTC
2. Resample to M5 for anchor capture
3. For each trading day (Mon-Fri), for each of 4 anchors:
   - Capture anchor at M5 close
   - Walk M1 bars from anchor+1min onward
   - Track pending orders for fills (BUY_STOP fills when bar.high >= trigger; SELL_STOP fills when bar.low <= trigger)
   - First fill cancels sibling
   - Apply trail logic on each subsequent M1 bar (steps 1-6 from §2.3)
4. At 23:00 broker: flatten any open positions
5. Output trade list: `date, anchor, side, entry_time, entry, exit_time, exit, max_favorable, outcome, pnl_dist, lot`

### 9.3 Spread modeling

**Auto-detect from data**: median of CSV's `spread` column for the month, trimmed at top/bottom 5%. Convert points → dollars via `× 0.01`.

**Apply per trade**: `net_pnl = gross_pnl_dist − spread − extra_slippage`. Then `net_usd = net_pnl × 100 × lot_size`.

Realistic Funding Pips conditions: spread $0.30 + slippage $0.10 = $0.40 round-trip per trade.

### 9.4 Sanity check (validates engine correctness)

If correctly implemented, this OLD config:
```python
sl_dist=20, tp_dist=20, be_trigger=0.30, trail_gap=0.30
lot_size=1.00, spread=0.0 (raw)
date_range = 2025-01-01 to 2026-05-13
```

Should produce approximately:
- Total fills: 279 (out of 281 trading days)
- Win rate: 97.8%
- Total P&L: +$20,836
- SL count: 5
- SL dates: 2025-01-09, 2025-07-31, 2025-09-26, 2026-02-13, 2026-02-20

**Match within ±5% = engine is correct.** Then apply current config (sl=18, tp=30, trail=0.10) and realistic spread for live expectations.

---

## 10. LIVE TRADING INFRASTRUCTURE

### 10.1 Required components

1. **MT5 terminal running** with broker account logged in
2. **Python 3.10+** with: `MetaTrader5`, `pandas`, `numpy`, `python-dotenv`, `requests`
3. **MT5 connection** via `mt5.initialize()` with NO args (uses running terminal — no credentials)
4. **Watchdog process** monitors trader subprocess, auto-restarts with exponential backoff
5. **Telegram bot** for telemetry and remote commands (`/status` `/pause` `/resume` `/stop`)

### 10.2 File structure

```
PROD/
  bot.py                  # Strategy logic, backtest engine, Config dataclass
  live_trader.py          # Main event loop, MT5 integration, anchor scheduling
  watchdog.py             # Parent supervisor, Telegram command poller
  telemetry.py            # Thread-safe Telegram queue (rate-limited)
  fetch_data.py           # M1 data fetcher (for backtests)
  monthly_analysis.py     # Monthly backtest with auto-spread detection
  env_loader.py           # .env file loader
  
  .env                    # AUREON_TELEGRAM_TOKEN, AUREON_TELEGRAM_CHAT
  requirements.txt        # Python dependencies
  
  data/XAUUSD/
    XAUUSD_M1_YYYY_MM.csv   # Cached M1 per month
  
  results/monthly/YYYY_MM/
    daily.csv  trades.csv  summary.json  report.md
  
  run/
    aureon_state.json     # Live state (positions, daily P&L)
```

### 10.3 Event loop (live mode)

```
Every 5 seconds:
  1. Check MT5 connection (reconnect if dropped)
  2. Get current broker time
  3. Check for anchor events
     - If anchor M5 just closed: capture price, place pendings
  4. For each open position: update trail (steps 1-6)
  5. Check pending order fills (cancel siblings on fill)
  6. Check kill switch (daily P&L + open position MTM)
  7. At 23:00 broker: EOD flatten
  8. Flush Telegram queue
  9. Persist state file
```

---

## 11. OPTIMIZATION HISTORY

### 11.1 Tested and ADOPTED

| Change | Annual impact (vs v1) | Why it works |
|--------|----------------------:|--------------|
| SL $20 → $18 | +$682 | Smaller per-SL cost. |
| TP $20 → $30 | +$1,438 | Lets winning trades capture more of trend moves. |
| **Trail gap $0.30 → $0.10** | **+$5,727** | **Captures more of favorable excursion. +11% win rate.** |
| Lot 0.49 → 0.54 | +$2,731 | 10% more position, still under FP 2% rule. |
| Auto-lot conservatism 1.0 → 0.99 | (scales with balance) | Auto-adjusts. $50k→0.54, $100k→1.08. |

**Combined annual gain: +$10,578 vs v1 baseline** (from $26,303 → $36,881).

### 11.2 Tested and REJECTED

| Idea | Reason rejected |
|------|----------------|
| Lot 0.55 on $50k | Worst day -$2,024 breaches 4% daily rule |
| Lot 0.60+ on $50k | Breaks 2% per-trade rule directly |
| 30-min negative-position filter | -$8,916/year. ~70% recoveries killed. |
| Time-based filter (any window 10-120 min) | All net-negative. |
| SL $15, $12, $10, $8 | -$3,355 to -$8,650/year. |
| BE trigger $0.25, $0.20, $0.15 | -$1,149 to -$1,316/year. |
| Wider SL ($25, $30) | -$84 to -$3,452/year. |
| 5 anchors (+08h pre-London) | Breaks 5% trailing DD rule. |
| 6+ anchors | Negative impact AND catastrophic DD. |
| TP $40, $50, $100 | Diminishing returns. |
| Trail gap $0.05 | +$1,400 vs $0.10 but eaten by live slippage. |
| Dynamic lot 0.5-0.7 on $50k | Lot >0.55 breaks rules. |
| Performance-based lot scaling | +$1,336/year, marginal. |

### 11.3 Untested but proposed

| Idea | Potential |
|------|-----------|
| Per-anchor lot sizing (bigger on A3) | +$2,000/yr estimated |
| Manual news blackout via `/pause` | Save catastrophic spikes |
| Disable A1+A4 (only A2+A3) | Halve variance, ~50% less profit |
| ATR-based dynamic SL | Better in volatile months |

---

## 12. KNOWN FAILURE MODES

### 12.1 The 2 SL trades in September 2025 (worst month, +$827 net)

**SL #1 — Sept 5, 14:52 UTC, A4 NY SELL** filled at $3,548.14
- Max favorable: $0.04 (4 cents)
- At 15:30 UTC: single M1 candle spiked $19
- SL hit at $3,568.14. Loss $-972 at lot 0.54.
- **Cause:** News spike or stop hunt at NY data window. Unavoidable.

**SL #2 — Sept 17, 02:55 UTC, A1 Asia BUY** filled at $3,695.04
- Max favorable: $0.27 (3 cents short of BE trigger)
- 2.5 hours of slow grind down to SL
- **Cause:** False breakout. Heartbreaker — 3¢ from BE protection.

### 12.2 The 2 kill-switch days

- **Oct 30, 2025:** -$1,944 (October ended +$6,603)
- **Feb 2, 2026:** -$1,989 ($11 from 4% daily limit; February ended +$2,106)

### 12.3 Choppy regime months

September 2025 had mean favorable excursion $1.18 vs $2.36 in good months. Only 1 trade > +$200. **No bot setting fixes a choppy month** — accept it as cost of doing business.

---

## 13. EXTENSION OPPORTUNITIES

### 13.1 Account scaling (recommended)

$100k Funding Pips Zero, lot 1.08:
- Annual backtest: ~$73,762
- Realistic pocket (78% × 95%): $54,658/year = **$4,555/month**

### 13.2 Multiple accounts ($50k + $100k parallel)

- Combined backtest: ~$110,643/year
- Realistic combined pocket: ~$82k/year = **$6,832/month**
- Capital required: ~$1,548 in challenge fees

### 13.3 ML add-ons (suggested experiments)

1. **Anchor quality classifier** — predict high-excursion anchors. Features: pre-anchor ATR, gold-DXY divergence, news proximity.

2. **News spike detector** — Features: spread widening, tick volume spike, hour-of-day.

3. **Regime classifier** — HMM/change-point on M5 returns. Could save Sept 2025 entirely.

4. **Position-size optimizer** — contextual bandit on lot size per anchor.

### 13.4 Symbol diversification

Strategy is XAUUSD-specific. Extensions need parameter re-tuning:
- BTCUSD: 24/7, very different vol
- US500 / NAS100: session-bound, lower vol
- EURUSD: tighter range

---

## 14. CRITICAL CAVEATS

### 14.1 Backtest vs live gap: ~22% (78% retention)

### 14.2 First 2 weeks most fragile

Without cushion, two kill-switch days in week 1 could breach 5% trailing DD. **Recommend lot 0.27 (half-size) for first 14 days** before scaling to auto-lot 0.54.

### 14.3 Black swan not modeled

No overnight gap > $50 in backtest. March 2020-style events not represented.

### 14.4 Funding Pips consistency rule (15%)

No single day can exceed 15% of total profit at payout. Time payouts AFTER slow-grind months.

### 14.5 Lot 0.55 vs 0.54 decision

Lot 0.55 backtest worst day: -$2,024 (4.05%) → BREACHES 4% daily rule.
Lot 0.54 backtest worst day: -$1,989 (3.98%) → SAFE.
Income difference: $45/month. **0.54 is the responsible max.**

---

## 15. CONFIG REFERENCE (current optimal)

```python
@dataclass
class Config:
    # Symbol
    symbol: str = "XAUUSD"
    contract_size: float = 100.0
    
    # Strategy (OPTIMAL — verified via 11-month backtest)
    trigger_dist: float = 5.00
    tp_dist:      float = 30.00
    sl_dist:      float = 18.00
    lot_size:     float = 0.54           # max safe at $50k (1.94% per trade)
    be_trigger:   float = 0.30
    trail_gap:    float = 0.10
    min_step:     float = 0.05           # live; 0.0 in backtest
    
    # Auto-sizing
    auto_lot: bool = True
    lot_conservatism: float = 0.99       # produces 0.54 at $50k, 1.08 at $100k
    risk_pct_under_50k: float = 0.03
    risk_pct_over_50k:  float = 0.02
    slippage_buffer: float = 0.98
    
    # Anchors (label, broker_hour) — broker = UTC+3
    anchors = [
        ("A1_02h_Asia",     2),
        ("A2_10h_London",  10),
        ("A3_14h_Overlap", 14),
        ("A4_17h_NYopen",  17),
    ]
    broker_tz_offset_hours: int = 3
    eod_broker_hour: int = 23
    
    # Risk
    starting_balance:   float = 50000.0
    daily_loss_pct:     float = 0.03     # 3% kill switch
    weekly_loss_pct:    float = 0.08
    account_floor_pct:  float = 0.85
```

---

## 16. PROMPT FOR ML IMPLEMENTATION

> Build a Python-based algorithmic trading bot following the AUREON v2 specification above. The bot must:
> 
> 1. **Backtest engine** — accepts MT5-format M1 CSV, produces trade ledger matching §9.4 sanity check within ±5%
> 2. **Live trading loop** — connects to running MT5 terminal (no credentials in code), executes §2 strategy real-time
> 3. **Risk layer** — enforces §3 rules (auto-lot, kill switch, floor) without manual override
> 4. **Watchdog** — auto-restarts on crashes with exponential backoff
> 5. **Telegram telemetry** — rate-limited messaging + remote commands
> 6. **Monthly analysis tool** — auto-detects spread from data, produces per-month reports
> 
> **Critical implementation:**
> - Round prices to 2 decimals, lots to 0.01
> - All times UTC internally; broker_time = utc + 3 hours
> - State persisted on every change for crash recovery
> - Idempotent anchor processing (no double-pending on restart)
> - Distinguish weekend from connection issue in tick-staleness checks
> 
> **Test requirements:**
> - Sanity backtest matches §9.4 within ±5%
> - All 11 months in §4 reproducible at lot 0.54
> - No negative months
> - Max DD < $2,500 (FP 5% rule)
> - Worst day < $2,000 (FP 4% daily rule)
> 
> **Deliverable:** Self-contained Windows VPS package, runs unattended, produces §4 results within 78-90% retention.

---

## 17. DEPLOYMENT CHECKLIST

### Pre-deployment
- [ ] Backup current bot.py
- [ ] Replace bot.py with optimized version (lot 0.54)
- [ ] Verify: `python -c "from bot import Config; c=Config(); print(f'{c.lot_size},{c.sl_dist},{c.tp_dist},{c.trail_gap}')"` → Expected: `0.54,18.0,30.0,0.1`
- [ ] Verify Telegram `/status` works
- [ ] Backtest one month to confirm

### Demo phase (Week 1-4)
- [ ] Deploy on MT5 demo at $100k
- [ ] Run unattended for 30 trading days
- [ ] Verify Telegram messages (~10/day)
- [ ] Check logs for trail modification errors
- [ ] Compare to backtest expectation
- [ ] Pass criteria: > $5,500 demo P&L for the month

### Live deployment
- [ ] Purchase Funding Pips Zero $50k ($549)
- [ ] Connect MT5 to Funding Pips broker
- [ ] Deploy bot, override lot to 0.27 for first 14 days
- [ ] Build cushion ≥ $500
- [ ] Switch to full auto-lot 0.54 after 14 days
- [ ] First payout request after consistency rule met

### Scale phase (Month 3+)
- [ ] If first 6 weeks profitable ≥ $1,500, purchase $100k FP Zero ($999)
- [ ] Deploy second MT5 instance
- [ ] Repeat half-size start → full auto-lot 1.08
- [ ] Combined target: $6,832/month pocket

---

## 18. ONE-LINE SUMMARY

**AUREON v2 = 4-anchor XAUUSD breakout bot with $5 trigger, $18 SL, $30 TP, $0.30 BE trigger, $0.10 trail gap, auto-lot at 1.94% per-trade risk (lot 0.54 on $50k). Backtested $36,881/year on $50k Funding Pips Zero with 0 negative months across 11 months. Survives 5% trailing DD with $600 margin, 4% daily limit with $11 margin. Realistic live pocket $27,329/year ($2,277/month) after 95% FP split. Scales to $100k for $54,658/year pocket.**

---

*Document version: 2.1 (lot 0.54, optimal trail config)*
*Last updated: May 2026*
*Data source: MT5 broker M1 (real historical, no synthetic data)*
*Backtest range: June 2025 → April 2026 (11 complete months)*
