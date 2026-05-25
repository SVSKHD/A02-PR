# AUREON v2 — Whole Package Overview

The complete picture of what's been built, what's in each file, what numbers it produces, and how to use it. Read this top-to-bottom if you want the full tour. Use it as a reference index after that.

---

## 1. Mission

A self-running, supervised, real-money trading bot for XAUUSD (gold) on MetaTrader 5, designed for prop-firm-funded accounts (Funding Pips Zero / Instant Funding). Plus the supporting infrastructure to backtest, paper-trade, monitor remotely, recover from crashes, and develop new strategies on top of the same foundation.

**Headline performance (backtest, May 2025 → May 2026):**
- **+73 pips / month average** on XAUUSD M1
- **96.5% win rate**, 28 SLs across 969 trades
- **Max drawdown −4%** of starting balance
- **0 negative months** in the 12-month sample

---

## 2. System architecture

```
                    ┌──────────────────────┐
                    │  Telegram (you)      │
                    └──────────┬───────────┘
                               │ commands ↑  alerts ↓
                    ┌──────────▼───────────┐
                    │  watchdog.py         │  parent process — always running
                    │  • spawns bot        │
                    │  • monitors heartbeat│
                    │  • restarts on crash │
                    │  • polls Telegram    │
                    └──────────┬───────────┘
                               │ subprocess + IPC files
                    ┌──────────▼───────────┐
                    │  bot.py + LiveTrader │  child process — trading brain
                    │  • CLI / Config      │
                    │  • MT5 adapter       │
                    │  • backtest engine   │
                    │  • 4-anchor loop     │
                    │  • OCO emulation     │
                    │  • trail manager     │
                    │  • state persistence │
                    └──────────┬───────────┘
                               │ MT5 Python API
                               ▼
                          MT5 broker
                          (your VPS terminal)

  Shared library used by both watchdog.py and bot.py:
    telemetry.py — queued, rate-limited Telegram notifications

  Daily analytics pipeline (runs via cron / systemd timer):
    fetch_data.py + auto_analyze.py — rolling 12-month backtest → Telegram

  Strategy R&D toolkit (offline, no MT5 connection needed):
    fetch_lab.py + strategy_template.py — multi-symbol fetcher + scaffold
```

---

## 3. Complete file inventory

### Production trading

| File | Lines | Role | Run mode |
|------|------:|------|----------|
| `watchdog.py` | ~420 | Parent supervisor. Auto-restart on crash. Telegram command listener. **Your entry point on the VPS.** | Always-on (systemd) |
| `bot.py` | ~530 | CLI + `Config` + `MT5Adapter` + `run_backtest()`. The boundary between strategy and broker. | Subprocess of watchdog, or standalone for backtest |
| `live_trader.py` | ~550 | `LiveTrader` event loop: 4 anchors, OCO emulation, continuous trail, EOD close, kill switch, state persistence, auto-lot from balance | Inside bot.py when mode=live/paper |
| `telemetry.py` | ~280 | Thread-safe notification queue → Telegram + console + log file. Rate-limited per severity. | Library |

### Daily analytics pipeline

| File | Lines | Role |
|------|------:|------|
| `fetch_data.py` | ~210 | Pulls XAUUSD M1 from MT5 in 30-day chunks. Hardcoded for the production strategy. |
| `auto_analyze.py` | ~290 | Daily orchestrator: fetch → backtest → summarize → Telegram → save markdown report. Designed for cron / systemd timer. |

### Strategy R&D toolkit

| File | Lines | Role |
|------|------:|------|
| `fetch_lab.py` | ~270 | **NEW.** Multi-symbol, multi-timeframe MT5 fetcher (any pair, any timeframe, any date range). For developing new strategies on different instruments. |
| `strategy_template.py` | ~290 | **NEW.** Scaffold for plugging a new strategy idea into the backtest framework. Just edit the `Strategy` class. |

### Documentation

| File | Content |
|------|---------|
| `AUREON_V2_SPEC.md` | **THE STRATEGY PROMPT** — full specification: anchors, triggers, trail, exits, risk management. The deterministic recipe. |
| `README.md` | Top-level usage and quick start |
| `TELEGRAM_SETUP.md` | Step-by-step Telegram bot creation (BotFather → chat ID → env vars) + systemd unit file |
| `AUTO_ANALYSIS.md` | Cron + systemd timer setup for daily auto-analysis |
| `WHOLE_PACKAGE.md` | This file |
| `commands.md` | **NEW.** Every CLI, Telegram, and systemd command — reference card |
| `.env.example` | **NEW.** Template for environment variables (Telegram token, chat ID, etc.). Copy to `.env`. |
| `.gitignore` | **NEW.** Excludes secrets and runtime state from version control |
| `aureon.service.example` | Production systemd unit template |
| `env_loader.py` | **NEW.** Loads `.env` at startup (OS env vars take precedence) |
| `requirements.txt` | Python dependencies (now includes python-dotenv) |

---

## 4. The strategy (the "prompt")

### Core specification

**4-anchor multi-session breakout, single-OCO fill-or-kill, continuous $0.30 trail.**

| | Anchor 1 | Anchor 2 | Anchor 3 | Anchor 4 |
|---|---|---|---|---|
| Broker time (UTC+3) | 02:00 | 10:00 | 14:00 | 17:00 |
| Session | Asia/Sydney | London open | London/NY overlap | NY post-open |
| What happens | M5 close captured, two stop orders placed at anchor ±$5, OCO sibling pair | (same) | (same) | (same) |

### Per-anchor parameters

| | Value | Why |
|---|---|---|
| Trigger distance | anchor ± $5.00 | Needs price to break out of post-anchor range |
| Initial SL | entry ± $20 | Max loss per trade — calibrated to Funding Pips 2% rule on $50k |
| Initial TP | entry ± $20 | Hard ceiling on per-trade profit |
| BE trigger | $0.30 fav move | Arms breakeven trail |
| Trail gap | $0.30 behind peak | Tight trail captures small wins, prevents whipsaw exits |
| MIN step | $0.10 | Avoids micro-SL-modification spam to broker |

### Exit priority (every M1 bar, in order)
1. Pre-bar SL check (pessimistic intrabar ordering)
2. Update peak favorable price
3. Ratchet SL up if favorable distance ≥ BE trigger
4. TP check at this bar's extreme
5. EOD at 23:00 broker — flatten everything

### Risk envelope

| Rule | Value |
|---|---|
| Per-trade max loss | $20 × 100 × lot |
| Daily kill switch | 3% of starting balance |
| Max simultaneous positions | 4 (one per anchor) |
| Trades per day (filled) | typically 3–4 |
| Trades per month | ~75 average |

The full deterministic recipe is in `AUREON_V2_SPEC.md`.

---

## 5. Performance numbers (verified end-to-end)

Backtest from May 8, 2025 → May 7, 2026 (13 calendar months), broker M1 from project file:

| Metric | Value |
|--------|------:|
| Total trades | **969** |
| Total pips | **+966** |
| Total USD @ lot 0.5 | **+$48,259** |
| Win rate | 96.5% |
| TP exits | 3 |
| SL exits | 28 |
| Trail exits | 938 |
| Max drawdown | -$2,000 (-4.0%) |
| Worst single day | -$2,000 (kill switch triggered, Feb 2 2026) |
| Best single day | +$1,331 |
| Kill switch days | 1 |
| Months observed | 13, **0 negative** |
| Avg / month | +$3,634 USD / +73 pips |

After spread + slippage adjustment (~$0.40 round-trip per trade), realistic live:
- **+577 pips / year**
- **+$28,873 USD at lot 0.5**
- ~48 pips / month
- ~$2,400 / month at lot 0.5

Per-anchor productivity (12 months, raw):

| Anchor | Pips/yr | SL rate | % of total |
|--------|--------:|--------:|----------:|
| A1 02:00 Asia | +222 | 3.5% | 23% |
| A2 10:00 London | +260 | 1.2% (best) | 27% |
| A3 14:00 Overlap | +267 | 3.5% | 28% |
| A4 17:00 NY | +201 | 3.5% | 21% |

---

## 6. Funding Pips integration (Zero / Instant Funding)

### Rules the bot enforces automatically

| Funding Pips rule | How AUREON handles it |
|-------------------|----------------------|
| **2% max risk/trade on ≥$50k accounts** | Auto-lot reads balance, computes max safe lot, applies 2% slippage buffer |
| **3% max risk/trade on <$50k accounts** | Same, with 3% threshold |
| **5% trailing max drawdown** | 3% daily kill switch leaves 2% buffer for multi-day downside |
| **7 profitable days / 30 rolling** | Strategy averages ~70% daily win rate — easily met |
| **15% consistency rule at payout** | Bot reports best day in `/status` — you decide when to request payout |
| **3% safety cushion (1st 3% locked)** | Bot doesn't request payouts — you do, when total profit ≥ 3% threshold |

### Auto-lot table (with default conservatism = 1.0)

| Account | Auto-lot | Max risk/trade | % of balance |
|---------|---------:|---------------:|-------------:|
| $10k | 0.14 | $280 | 2.80% |
| $25k | 0.36 | $720 | 2.88% |
| $50k | 0.49 | $980 | 1.96% |
| $100k | 0.98 | $1,960 | 1.96% |

The bot reads `mt5.account_info().balance` at startup and at each new broker day, then auto-computes the lot.

### Year-end equity projections (trader's 95% share after spread/slippage)

| Account | Lot | Year-end profit (mid-realistic) |
|---------|----:|---------------------------------:|
| $10k Zero | 0.14 | **~$7,700** |
| $25k Zero | 0.30–0.36 | **~$16,500** |
| $50k Zero | 0.49 | **~$27,000** |
| $100k Zero | 0.70 (conservative) – 0.98 (max) | **~$38,000 – $54,000** |

Payouts: bi-weekly cycle, 95% trader / 5% firm. Account balance stays at starting level; profit drains to your bank every 14 days.

---

## 7. Deployment workflow

### Phase 1 — Local backtest validation (1 day)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Backtest on the historic data you have
python bot.py backtest \
  --csv your_XAUUSD_M1.csv \
  --start 2025-01-01 --end 2026-05-22 \
  --balance 50000 \
  --output-dir ./output

# 3. Inspect output/stats.json — should show ~+73 pips/mo, max DD <5%
```

### Phase 2 — Telegram setup (30 min)

See `TELEGRAM_SETUP.md`. End state:

```bash
export AUREON_TELEGRAM_TOKEN="123:AAE..."
export AUREON_TELEGRAM_CHAT="987654321"
python telemetry.py     # should fire 5 test messages to your phone
```

### Phase 3 — Paper trading on VPS (2 weeks minimum)

```bash
# On Windows VPS with MT5 terminal logged into Funding Pips Zero

python watchdog.py paper
```

Watch Telegram. You should see:
- Daily: 1 watchdog start, 4 anchor messages, 0–4 fills, 0–4 closes, 1 EOD summary
- Per-trade: anchor capture → fill → close (with P&L)
- Heartbeat issues, crashes, restarts → all alerted

After 2 weeks of clean paper run with metrics matching backtest (±15% drag), proceed.

### Phase 4 — Daily auto-analysis (set once, runs forever)

Cron or systemd timer. See `AUTO_ANALYSIS.md`. Every weekday morning at 09:00:
1. `fetch_data.py` pulls latest 365 days of XAUUSD M1
2. `auto_analyze.py` runs the backtest on it
3. `telemetry.py` sends summary to Telegram
4. Markdown report saved to `reports/AUREON_analysis_{date}.md`

If the rolling number drifts downward for 3 weeks straight, that's your signal to size down before live drawdown.

### Phase 5 — Go live (final cutover)

```bash
sudo systemctl stop aureon-paper
# Edit /etc/systemd/system/aureon.service:  paper → live --i-understand-the-risks
sudo systemctl daemon-reload
sudo systemctl start aureon-live
```

First week: send `/status` 2–3× daily from Telegram. Don't request the first payout if a single big day exceeds 15% of total profit yet.

---

## 8. New strategy R&D workflow

For developing a strategy other than AUREON v2 (e.g., for a different instrument, different timeframe, different setup).

### Step 1 — Fetch any data you want

```bash
# Single symbol+timeframe
python fetch_lab.py --symbol BTCUSD --timeframe M5 --days 365

# Multiple symbols × multiple timeframes
python fetch_lab.py \
  --symbols XAUUSD,EURUSD,NAS100,US30 \
  --timeframes M1,M5,H1 \
  --start 2025-01-01 --end 2026-05-22

# Re-runnable, skips files already present
python fetch_lab.py --symbol XAUUSD --timeframe M1 --days 365 --skip-existing
```

Output goes to `research_data/{SYMBOL}/{SYMBOL}_{TF}_{start}_to_{end}.csv` with the same column format as `bot.py` expects.

### Step 2 — Write the strategy

Edit `strategy_template.py`, specifically the `Strategy` class:

```python
class Strategy:
    def on_bar(self, bar, ts):
        # YOUR ENTRY LOGIC HERE
        # Return Action.OPEN_BUY / OPEN_SELL / STAY
        # Use self.state dict to store anything across bars
        pass

    def manage_position(self, pos, bar, ts):
        # YOUR EXIT LOGIC HERE  
        # Update pos.sl to trail
        # Return 'SL', 'TP', 'Trail', or None
        pass
```

The default implementation: open BUY every 1000 bars, AUREON-style $0.30 trail. Replace with your idea.

### Step 3 — Backtest it

```bash
python strategy_template.py \
  --csv research_data/XAUUSD/XAUUSD_M5_2025-05-22_to_2026-05-22.csv \
  --sl 20 --tp 20 --trail 0.30 \
  --output-dir ./strategy_output
```

Get per-trade CSV + monthly summary + stats JSON. Iterate on parameters and logic.

### Step 4 — When you have a working strategy

Either:
- **Plug it into the live bot** by replacing AUREON's logic in `live_trader.py:_process_anchor()` (more work, integrates with watchdog/Telegram/risk system)
- **Run it in parallel** as a separate process with its own MT5 login (simpler — copy the bot package, edit `bot.py` and `live_trader.py`, point at a different account)

---

## 9. Customization knobs

### In `bot.py` → `Config` dataclass

| Knob | Default | What changing it does |
|------|--------:|----------------------|
| `lot_size` | 0.49 | Manual lot (used only if `auto_lot=False`) |
| `auto_lot` | True | Whether to compute lot from balance |
| `lot_conservatism` | 1.0 | Multiplier on max-safe lot (1.0 = max, 0.7 = conservative) |
| `daily_loss_pct` | 0.03 | Daily kill switch threshold |
| `trigger_dist` | $5 | Distance from anchor to pending stop |
| `tp_dist` | $20 | Take-profit distance |
| `sl_dist` | $20 | Initial stop-loss distance |
| `trail_gap` | $0.30 | How far behind peak the trail SL sits |
| `be_trigger` | $0.30 | Favorable move that arms the trail |
| `min_step` | $0.10 | Minimum SL advance (live) |
| `anchors` | 4 entries | The 4 anchor times. Add/remove to change schedule |
| `eod_broker_hour` | 23 | When all positions are flattened |
| `broker_tz_offset_hours` | 3 | UTC+3 |
| `starting_balance` | $50,000 | Used by kill switch math (overridden by auto-detect in live) |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `AUREON_TELEGRAM_TOKEN` | Bot token from @BotFather |
| `AUREON_TELEGRAM_CHAT` | Chat ID for alerts and commands |
| `AUREON_TELEGRAM_MIN_SEVERITY` | INFO / SUCCESS / WARN / ERROR / CRITICAL |
| `AUREON_LOG_FILE` | Optional log file path |
| `AUREON_RUN_DIR` | Where heartbeat / status.json / commands.json live |

---

## 10. What's automated vs what's manual

### Automated (the bot handles it)
- ✅ Anchor fires at all 4 session times
- ✅ Order placement (buy_stop + sell_stop with hard SL/TP)
- ✅ OCO emulation (cancel sibling when one fills)
- ✅ Continuous trail SL on every M1 bar close
- ✅ EOD position flatten at 23:00 broker
- ✅ Daily kill switch (3% of balance, live equity-based)
- ✅ State persistence across crashes
- ✅ Auto-restart by watchdog (exponential backoff)
- ✅ Heartbeat monitoring + force-restart on hang
- ✅ Balance auto-detect from MT5
- ✅ Lot auto-computation from balance + risk rules
- ✅ Daily summary to Telegram
- ✅ Per-trade Telegram notifications (anchor, fill, close)
- ✅ Daily rolling 12-month backtest sanity check
- ✅ Telegram remote control: /status /flatten /pause /resume /restart /stop /today /help

### Manual (your judgment required)
- ❌ News blackout (NFP/FOMC/CPI ±5 min — use `/pause` from phone)
- ❌ First payout timing (skip if 15% consistency rule not yet met)
- ❌ Decision to scale lot up after qualifying for Hot Seat
- ❌ Running multiple accounts (one bot per MT5 login — manually spawn)
- ❌ Account swap if 5% trailing DD blows out (treat each account as expendable, run 2-3 in parallel)
- ❌ Tuning `lot_conservatism` to your risk preference

---

## 11. Verification status

| Item | Status |
|------|--------|
| Backtest engine reproduces published numbers | ✅ +73 pips/mo, 96.5% win, max DD 4% |
| All modules import cleanly with MT5 absent (no Windows env required for testing) | ✅ |
| Live MT5 connection tested | ⚠️ Cannot test from sandbox — verify on first paper run |
| Telegram sink tested | ⚠️ Self-test in telemetry.py works; live delivery requires real credentials |
| Auto-lot computation matches rules | ✅ Verified for $10k/$25k/$50k/$100k |
| Auto-analysis pipeline runs end-to-end | ✅ Tested via project CSV |
| Watchdog restart logic | ⚠️ Code-reviewed; runtime requires live deployment |
| OCO emulation | ⚠️ Code-reviewed; live verification requires real fills |

The ⚠️ items can only be confirmed once running against a real MT5 terminal. That's what the 2-week paper trading phase is for.

---

## 12. Known limitations and future work

| Limitation | Workaround | Permanent fix |
|------------|-----------|---------------|
| Single instrument (XAUUSD) | Run multiple bots per instrument | Generalize Config.symbol |
| One broker per process | Multiple watchdog instances on the VPS | Multi-broker abstraction |
| Hardcoded sessions (4 anchors) | Edit `Config.anchors` | Make schedule configurable per session |
| No news blackout | Manual `/pause` from Telegram | Hook into ForexFactory/Investing.com calendar API |
| No volatility filter | Run all anchors regardless | Add ATR-based gate before placing orders |
| No correlated-instrument trading | Single asset class | Cross-asset position scaling |
| New-strategy bot is separate scaffold | Run as parallel process | Plugin architecture in live_trader.py |
| No web dashboard | Telegram /status command | Build Flask UI reading status.json |

---

## Where to start reading the code

1. **`AUREON_V2_SPEC.md`** — what the strategy does (deterministic recipe)
2. **`bot.py`** — Config dataclass + MT5Adapter + run_backtest() — the boundaries
3. **`live_trader.py`** — the 4-anchor event loop, the heart
4. **`telemetry.py`** — how messages get to your phone
5. **`watchdog.py`** — supervisor process
6. **`auto_analyze.py`** — daily sanity check pipeline
7. **`fetch_lab.py` + `strategy_template.py`** — new strategy R&D toolkit

Reading order maps to deployment order: spec → backtest → telemetry → live → watchdog → analytics → new strategies.

---

## Quick reference card

```bash
# Backtest
python bot.py backtest --csv data.csv --balance 50000

# Paper trade (supervised)
python watchdog.py paper

# Live trade
python watchdog.py live --i-understand-the-risks

# Daily rolling analysis
python auto_analyze.py --days 365

# Fetch any symbol/timeframe for research
python fetch_lab.py --symbol BTCUSD --timeframe M5 --days 365

# Backtest a new strategy
python strategy_template.py --csv research_data/.../data.csv --sl 20 --tp 20

# From Telegram (when bot is running):
/help /status /today /flatten /pause /resume /restart /stop
```
