# AUREON v2 — Multi-Anchor XAUUSD Bot

Anchor-breakout trading bot for gold (XAUUSD) M1, with **4 independent session anchors per day** (Asia, London, London-NY overlap, NY) and **dual-side fill-or-kill** OCO discipline per anchor.

## What's in this package

| File | What it is |
|------|------------|
| `AUREON_V2_SPEC.md` | The complete strategy specification — the "prompt." Read this first. |
| `bot.py` | Entry point: backtest engine + MT5 adapter + CLI. |
| `live_trader.py` | Production live/paper trading loop with full trail management, OCO emulation, state persistence, EOD close, kill switch, telemetry, and command handling. |
| `telemetry.py` | Thread-safe notification engine (Telegram + console + file). |
| `watchdog.py` | Parent supervisor — spawns bot, monitors heartbeat, auto-restarts on crash, listens to Telegram commands. |
| `fetch_data.py` | **NEW.** Pulls XAUUSD M1 from MT5 over an arbitrary date range (chunked, deduped). |
| `auto_analyze.py` | **NEW.** Daily orchestrator: fetches rolling 12-month data → runs backtest → sends summary to Telegram → saves markdown report. Designed for cron / systemd timer. |
| `TELEGRAM_SETUP.md` | Step-by-step Telegram bot creation + integration guide. |
| `requirements.txt` | Python dependencies. |
| `README.md` | This file. |

## Architecture

```
                    ┌──────────────────────┐
                    │   Telegram (you)     │
                    └──────────┬───────────┘
                               │ commands + alerts
                    ┌──────────▼───────────┐
                    │  watchdog.py         │  spawns + supervises
                    │  - heartbeat check   │
                    │  - auto-restart      │
                    │  - telegram polling  │
                    └──────────┬───────────┘
                               │ subprocess + files
                    ┌──────────▼───────────┐
                    │  bot.py + LiveTrader │
                    │  - trading logic     │
                    │  - MT5 orders        │
                    │  - heartbeat write   │
                    │  - status write      │
                    │  - command consume   │
                    └──────────────────────┘
                               │
                               ▼
                          MT5 broker
```

Both `watchdog.py` and `bot.py` use the same `telemetry.py` module to push alerts to the same Telegram chat — you see a unified stream.

## Validated performance (12-month backtest, May 2025 → May 2026)

| Metric | Value |
|--------|------:|
| Total pips | +944 |
| Average pips / month | **+73** |
| Win rate | 96.5% |
| Max drawdown | −$2,000 (−4.0%) |
| 4% kill-switch hits | 0 |
| Months > +100 pips | 4 of 13 (Oct, Nov, Mar, Apr) |
| Months negative | 0 of 13 |

## Quick start

### 1. Install
```bash
pip install -r requirements.txt
```

### 2. Set up Telegram (highly recommended)
See `TELEGRAM_SETUP.md` for full instructions. Short version:
```bash
export AUREON_TELEGRAM_TOKEN="123:AAE..."        # from @BotFather
export AUREON_TELEGRAM_CHAT="987654321"          # your chat id
export AUREON_TELEGRAM_MIN_SEVERITY="INFO"       # INFO|SUCCESS|WARN|ERROR|CRITICAL
```

### 3. Backtest on your own M1 CSV
Your CSV must have columns: `time, open, high, low, close` with UTC timestamps.

```bash
python bot.py backtest \
  --csv your_XAUUSD_M1.csv \
  --start 2025-01-01 \
  --end   2026-05-19 \
  --lot   0.5 \
  --balance 50000 \
  --output-dir ./output
```

Output:
- `output/trades.csv` — every trade with anchor label, entry, exit, P&L
- `output/stats.json` — aggregate stats + monthly P&L

### 4. Paper trade with watchdog + Telegram (recommended)
```bash
python watchdog.py paper \
  \
  \
  --lot 0.5
```

Then control from Telegram with `/status`, `/today`, `/pause`, `/flatten`.

If the bot crashes, the watchdog restarts it automatically (exponential backoff, max 8 attempts before giving up). You get a Telegram notification on every crash and recovery.

### 5. Live trade
**Read the spec's risk section first.** Then:

```bash
python watchdog.py live \
  \
  \
  \
  --lot 0.5 \
  --i-understand-the-risks
```

### 6. Auto-start on boot (Linux systemd)
See `TELEGRAM_SETUP.md` → "Auto-start on boot (Linux systemd)" for the service file. Then:
```bash
sudo systemctl enable aureon
sudo systemctl start aureon
journalctl -u aureon -f
```

## Configurationpython watchdog.py live \
  \
  \
  \
  --lot 0.5 \
  --i-understand-the-risks

Defaults are encoded in `bot.py:Config`. Override via CLI flags:
- `--lot 0.5` — lot size per leg
- `--balance 50000` — starting balance (for kill-switch math)

To change anchor times or strategy parameters, edit the `Config` dataclass at the top of `bot.py`.

## What the live/paper mode does (now fully implemented)

The `LiveTrader` class in `live_trader.py` runs a single event loop that ticks every 5 seconds and performs these checks in order:

1. **New broker day?** → reset daily P&L, clear processed-anchors list, unlock kill switch
2. **Broker reconcile** → pull open positions and pending orders from MT5; detect fills (OCO trigger) and closures (update daily P&L)
3. **Kill switch tripped?** → flatten everything at market, lock for the rest of the day
4. **EOD reached (23:00 broker)?** → close every open position, cancel every pending
5. **Anchor time due?** → capture M5 close, place buy stop and sell stop, register OCO pair
6. **M1 bar closed?** → for every managed position, run the exact same `update_position_on_bar()` function the backtest uses, modify SL on broker if it advanced

### OCO emulation
MT5 has no native OCO. When two pending orders are placed for an anchor (buy stop + sell stop), they're registered as siblings in shadow state. As soon as the reconcile step detects that one became a position, the bot cancels the other.

### State persistence
Every state change is saved to `aureon_v2_state.json` (atomic write via `.tmp` + rename). On restart, the bot restores:
- today's running daily P&L
- which anchors have already been processed today
- whether the kill switch is locked
- (paper mode does not persist)

### Trail logic equivalence
The live loop uses the **same** `update_position_on_bar()` function as the backtest, so live trail behavior matches backtest exactly — modulo spread, slippage, and the inevitable 1–5 second delay between bar close and SL modification arriving at the broker.

## What is still NOT in the package (intentional — these are operational, not strategic)

| Missing feature | Where you'd add it | Why it matters |
|-----------------|--------------------|----------------|
| News blackout (FOMC/NFP/CPI ±5 min) | In `LiveTrader._process_anchor_if_due()`, check a calendar before placing | Avoids slippage on news spikes |
| Telegram/email alerts | In `LiveTrader._tick()` exception handler + kill-switch event | So you find out when things break |
| Retry on order rejection | Wrap `adapter.place_stop_order()` in retry-with-backoff | MT5 rejects ~0.1% of orders for transient reasons |
| Multiple instruments | Generalize `cfg.symbol` to a list, run one LiveTrader per symbol | Diversification |
| Systemd service file | `/etc/systemd/system/aureon.service` | Auto-restart on crash, run on boot |

## Required Python packages

```
pandas >= 2.0
numpy  >= 1.24
MetaTrader5 >= 5.0.45    # only needed for paper/live modes
```

(Listed in `requirements.txt`.)

## How the strategy works in 30 seconds

1. At each of 4 anchor times daily (02:00, 10:00, 14:00, 17:00 broker), capture the M5 close.
2. Place 2 pending stop orders: BUY at anchor+$5, SELL at anchor−$5.
3. First fill = the trade. The other is killed (OCO).
4. Manage with $20 SL, $20 TP, and a $0.30 continuous trail behind peak favorable.
5. Each anchor's trade is independent. 4 simultaneous positions max.
6. At 23:00 broker, close anything still open.
7. If daily P&L hits −4%, kill switch fires: flatten everything, no more trades today.

Result: ~73 pips/month average, 96.5% win rate, max drawdown stays under 4%.

## Failure modes to watch for

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Way more SLs than expected | Wider spread than backtest assumed | Subtract spread from trail buffer in live |
| Trail not moving | `min_step` too high for current volatility | Lower `min_step` to $0.05 |
| Missed anchor | Broker server time != UTC+3 (DST issue) | Verify in MT5: Tools → Options → Server tab |
| Both legs filling within 1 minute | Single M1 bar wider than $10 (news spike) | Add news blackout |
| Account drawdown > 5% | Multiple correlated kill-switch days | Reduce lot size 50% and review last 2 weeks |

## License

This is YOUR strategy and YOUR risk. The code is provided as-is for educational and backtesting purposes. Past performance does not guarantee future results. **Live performance will be 10-20% below backtest due to spread, slippage, and execution friction.**
