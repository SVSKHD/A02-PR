# AUREON v2 — Commands Reference

Every command you'll use to operate the bot, grouped by context. Keep this on hand during deployment and operation.

---

## 🚀 Quick Reference Card

```bash
# DAILY OPERATION
python watchdog.py live --i-understand-the-risks   # start trading
python watchdog.py paper                            # paper-mode test
python telemetry.py                                 # test Telegram

# FROM YOUR PHONE (Telegram)
/status   /today   /flatten   /pause   /resume   /restart   /stop   /help

# BACKTEST
python bot.py backtest --csv data.csv --start 2025-05-08 --end 2026-05-06
python auto_analyze.py --days 365                   # rolling 12-month analysis

# DATA FETCH
python fetch_data.py --days 365 --output data/latest.csv
python fetch_lab.py --symbol XAUUSD --timeframe M5 --days 365

# RESEARCH NEW STRATEGY
python strategy_template.py --csv research_data/.../data.csv

# SYSTEMD (Linux production)
sudo systemctl start aureon
sudo systemctl status aureon
journalctl -u aureon -f
```

---

## 1. Bot Startup Commands

### Live trading
```bash
python watchdog.py live --i-understand-the-risks
```
Spawns the trading bot under watchdog supervision. The `--i-understand-the-risks` flag is **required** for live mode (safety gate against accidental real-money trading).

### Paper trading (no real orders)
```bash
python watchdog.py paper
```
Same execution path as live but `dry_run=True` on every order. Use this for the first 2 weeks to validate mechanics.

### Backtest (no MT5 needed)
```bash
python bot.py backtest --csv your_data.csv --balance 50000 --output-dir ./output
```
Processes a historical M1 CSV. Doesn't connect to MT5. Doesn't send Telegram messages. Just produces `output/trades.csv` and `output/stats.json`.

### Force a specific lot size (testing only)
```bash
python watchdog.py live --i-understand-the-risks --lot 0.30
```
Overrides auto-lot detection. **Almost never needed** — auto-lot computes the correct safe size for your account automatically.

---

## 2. Telegram Commands (from your phone)

All commands work in your private chat with the bot. Only messages from the configured `AUREON_TELEGRAM_CHAT` are accepted; everyone else is silently ignored.

### Status & monitoring

| Command | What you get back |
|---------|-------------------|
| `/status` | Live broker balance, equity, floating P&L, kill switch state, broker date, lot, realized P&L, open positions, pending orders, anchors processed today, heartbeat age |
| `/today` | Today's fills list with anchor, side, entry, exit, P&L per trade + running total |
| `/help` | The command list itself |

### Control

| Command | Effect | When to use |
|---------|--------|-------------|
| `/pause` | Stop placing new anchor orders | Before NFP/FOMC/CPI news |
| `/resume` | Resume placing anchor orders | After news passes |
| `/flatten` | Close all positions + cancel pending orders **immediately** | Emergency stop |
| `/restart` | Graceful bot restart (state preserved) | After config edit |
| `/stop` | Shut down watchdog + bot entirely | End of trading session, weekly maintenance |

### Example phone session

```
You:  /status
Bot:  🏦 Account: #5050361102 @ MetaQuotes-Demo
      💵 Balance: $50,247.50  Equity: $50,189.20
      📊 Floating P&L: -$58.30
      🛑 Kill switch at: -$1,500 (-3.0%)  🟢 OK
      📅 Broker date: 2026-05-25
      📦 Lot: 0.49
      💰 Realized P&L: $247.50
      📈 Open positions: 1
      📋 Pending orders: 2
      ⚓ Anchors today: 3/4
         A1_02h_Asia, A2_10h_London, A3_14h_Overlap
      💓 Heartbeat: 4s ago

You:  /pause
Bot:  ⏸ Bot paused. Existing positions still trailing.

You:  /resume
Bot:  ▶️ Bot resumed.
```

---

## 3. Data Fetching Commands

### Production fetcher (XAUUSD M1, used by auto_analyze.py)

```bash
# Rolling N days from now
python fetch_data.py --days 365 --output data/XAUUSD_M1_last_year.csv

# Specific date range
python fetch_data.py --start 2025-01-01 --end 2026-05-22 \
    --output data/XAUUSD_M1_jan_to_may.csv

# Different chunk size (advanced)
python fetch_data.py --days 365 --output data.csv --chunk-days 60
```

### Research fetcher (any symbol, any timeframe)

```bash
# Single instrument, single timeframe
python fetch_lab.py --symbol BTCUSD --timeframe M5 --days 365

# Multiple symbols at one timeframe
python fetch_lab.py --symbols XAUUSD,EURUSD,NAS100 --timeframe M1 \
    --start 2025-01-01 --end 2026-05-22

# Multi-symbol × multi-timeframe (Cartesian product)
python fetch_lab.py --symbols XAUUSD,EURUSD --timeframes M1,M5,H1 --days 365

# Skip files that already exist (idempotent re-runs)
python fetch_lab.py --symbol XAUUSD --timeframe M1 --days 365 --skip-existing

# Custom output directory
python fetch_lab.py --symbol XAUUSD --timeframe M1 --days 365 \
    --output-dir D:\Research\GoldData
```

**Supported timeframes:** M1, M5, M15, M30, H1, H4, D1, W1, MN1

### Daily analysis (rolling 12-month backtest)

```bash
# Fetch + backtest + Telegram summary in one command
python auto_analyze.py --days 365

# Re-analyze existing CSV without re-fetching
python auto_analyze.py --csv data/latest.csv

# Custom window
python auto_analyze.py --start 2025-05-22 --end 2026-05-22
```

---

## 4. Backtest Commands

### Standard backtest

```bash
python bot.py backtest \
    --csv data/XAUUSD_M1.csv \
    --start 2025-05-08 \
    --end 2026-05-06 \
    --balance 50000 \
    --output-dir ./output
```

### What you get

| File | Contents |
|------|----------|
| `output/trades.csv` | Every trade: date, anchor, side, entry, exit, max_favorable, outcome, pnl_dist, pnl_usd |
| `output/stats.json` | Headline numbers (total_pips, win_rate, max_dd, kill_days, monthly_pnl, etc.) |

### Backtest with non-default settings

```bash
# Different lot size
python bot.py backtest --csv data.csv --lot 0.7

# Different starting balance (affects max_dd % calc)
python bot.py backtest --csv data.csv --balance 100000

# Different date window
python bot.py backtest --csv data.csv --start 2025-09-01 --end 2025-12-31

# Verbose logging
python bot.py backtest --csv data.csv --log-level DEBUG
```

### Sanity-check expected numbers (after backtest runs)

```bash
# Quick stats from JSON
python -c "import json; print(json.load(open('output/stats.json'))['avg_per_month_pips'])"

# Best/worst single day
python -c "
import pandas as pd
df = pd.read_csv('output/trades.csv')
df['date'] = pd.to_datetime(df['date'])
daily = df.groupby(df['date'].dt.date)['pnl_usd'].sum()
print(f'Best:  {daily.idxmax()}  \${daily.max():+,.2f}')
print(f'Worst: {daily.idxmin()}  \${daily.min():+,.2f}')
"
```

---

## 5. New-Strategy Research Commands

### Develop a new strategy on fetched data

```bash
# 1. Fetch data for the instrument/timeframe you want
python fetch_lab.py --symbol XAUUSD --timeframe M5 --days 365

# 2. Edit strategy_template.py:Strategy.on_bar() and manage_position()
#    with your entry/exit logic

# 3. Backtest
python strategy_template.py \
    --csv research_data/XAUUSD/XAUUSD_M5_2025-05-22_to_2026-05-22.csv \
    --lot 0.5 \
    --sl 20 --tp 20 --trail 0.30 \
    --output-dir ./strategy_output

# 4. Inspect
cat strategy_output/stats.json
```

### Parameter sweeps

```bash
# Try different SL distances
for sl in 10 15 20 25 30; do
    python strategy_template.py --csv data.csv --sl $sl --output-dir ./sweep_$sl
done
```

---

## 6. Telemetry / Connectivity Tests

### Test that Telegram works

```bash
python telemetry.py
```
Sends 5 messages (INFO/SUCCESS/WARN/ERROR/CRITICAL severity each) to your configured chat. Verifies:
- `.env` is loaded correctly
- Token is valid
- Chat ID is valid
- Bot was activated (you sent it `/start` or any message first)

### Test that MT5 is connected

```bash
python -c "
import MetaTrader5 as mt5
ok = mt5.initialize()
if not ok:
    print('FAIL:', mt5.last_error())
else:
    info = mt5.account_info()
    if info is None:
        print('Connected but no account logged in')
    else:
        print(f'Account: {info.login} on {info.server}')
        print(f'Balance: \${info.balance:.2f}  Equity: \${info.equity:.2f}')
mt5.shutdown()
"
```

### Test that XAUUSD symbol is available

```bash
python -c "
import MetaTrader5 as mt5
mt5.initialize()
info = mt5.symbol_info('XAUUSD')
if info is None:
    print('XAUUSD NOT FOUND on this broker')
else:
    print(f'XAUUSD found  visible={info.visible}')
    print(f'  bid={info.bid}  ask={info.ask}  spread={info.spread}')
mt5.shutdown()
"
```

### Check broker server time

```bash
python -c "
import MetaTrader5 as mt5
import pandas as pd
mt5.initialize()
tick = mt5.symbol_info_tick('XAUUSD')
broker_t = pd.Timestamp(tick.time, unit='s', tz='UTC')
now = pd.Timestamp.now(tz='UTC')
age_h = (now - broker_t).total_seconds() / 3600
print(f'Last tick: {broker_t}')
print(f'Now:       {now}')
print(f'Age:       {age_h:.2f} hours')
print('Market is:', 'CLOSED' if age_h > 1 else 'OPEN')
mt5.shutdown()
"
```

---

## 7. systemd Service Commands (Linux production)

Once `aureon.service` is installed (see `aureon.service.example`):

```bash
# Start
sudo systemctl start aureon

# Stop
sudo systemctl stop aureon

# Restart
sudo systemctl restart aureon

# Check status
sudo systemctl status aureon

# Enable autostart on boot
sudo systemctl enable aureon

# Disable autostart
sudo systemctl disable aureon

# View live logs (Ctrl+C to exit)
journalctl -u aureon -f

# View last 100 lines of logs
journalctl -u aureon -n 100

# View logs since this morning
journalctl -u aureon --since today

# View logs for a specific date range
journalctl -u aureon --since "2026-05-22 00:00" --until "2026-05-23 00:00"

# Reload after editing the unit file
sudo systemctl daemon-reload && sudo systemctl restart aureon
```

### Daily analysis timer (separate from main bot)

```bash
sudo systemctl start aureon-analyze.timer    # one-shot enable
sudo systemctl status aureon-analyze.timer   # see next scheduled run
sudo systemctl list-timers                   # all timers on system
```

---

## 8. State & Runtime File Commands

The bot writes state files to `./run/` (or `$AUREON_RUN_DIR`).

```bash
# What's the current status?
cat run/status.json | python -m json.tool

# When did the bot last write a heartbeat?
ls -la run/heartbeat                # see modified time
python -c "
import os, time
mt = os.path.getmtime('run/heartbeat')
age = time.time() - mt
print(f'Heartbeat age: {age:.1f}s ({\"STALE\" if age > 60 else \"OK\"})')
"

# What state was saved?
cat run/state.json | python -m json.tool

# Today's trade log
cat run/today_trades.csv

# Clear all state (fresh start tomorrow)
rm -rf run/*

# Send a command via the file interface (bypassing Telegram)
echo '{"cmd":"flatten","ts":"manual"}' >> run/commands.json
```

---

## 9. Setup Commands (one-time, during install)

### Install Python dependencies

```bash
pip install -r requirements.txt
```

### Set up .env file

```bash
cp .env.example .env

# Edit with your editor
nano .env             # Linux
notepad .env          # Windows

# Lock permissions
chmod 600 .env                                      # Linux/Mac
icacls .env /inheritance:r /grant:r "%USERNAME%:R"  # Windows PowerShell
```

### Install systemd service (Linux)

```bash
sudo cp aureon.service.example /etc/systemd/system/aureon.service
# Edit User= and WorkingDirectory= if needed
sudo nano /etc/systemd/system/aureon.service
sudo systemctl daemon-reload
sudo systemctl enable aureon
sudo systemctl start aureon
```

### Install daily analysis timer (Linux)

```bash
sudo cp aureon-analyze.service /etc/systemd/system/
sudo cp aureon-analyze.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aureon-analyze.timer
```

### Set up MT5 on Windows VPS

```powershell
# Download MT5 from your broker
# Install
# Launch MT5 terminal
# Log in (File → Login to Trade Account)
# Tools → Options → Expert Advisors → ✓ Allow algorithmic trading
# Drag XAUUSD into Market Watch if not visible
```

---

## 10. Common Scenarios (Cookbook)

### Scenario: "I want to start fresh this Monday"

```bash
# Sunday evening
cd ~/aureon_v2
rm -rf run/*                                # clear stale state
python telemetry.py                          # verify Telegram works
python -c "import MetaTrader5 as mt5; print(mt5.initialize(), mt5.account_info())"  # verify MT5
python watchdog.py paper                     # if testing
# or
python watchdog.py live --i-understand-the-risks
```

### Scenario: "Bot is acting weird, I want to investigate"

```bash
# From phone
/pause                                       # stop new entries
/status                                      # snapshot state

# On VPS
journalctl -u aureon -n 200                  # last 200 log lines
cat run/state.json | python -m json.tool     # current state
cat run/today_trades.csv                     # what trades happened
ls -la run/                                  # see all state files

# When ready
/resume   (or /restart)
```

### Scenario: "Emergency, get out of all positions NOW"

```
From phone:  /flatten

Or on VPS:   echo '{"cmd":"flatten"}' >> run/commands.json
```

### Scenario: "I want to switch from $50k to $100k account"

```bash
# In MT5 terminal:
# File → Login to Trade Account → enter new credentials → OK

# Bot will auto-detect on next refresh:
# Option A: /restart from Telegram
# Option B: wait until next broker day (auto-refresh happens then)
```

### Scenario: "I want to run multiple accounts in parallel"

```bash
# Create separate folders for each
cp -r aureon_v2 aureon_50k
cp -r aureon_v2 aureon_100k

# Each gets its own .env with potentially different Telegram tokens/chats
nano aureon_50k/.env
nano aureon_100k/.env

# Launch separate MT5 terminals (Windows: copy MT5 install dir, log each into different account)

# Start one bot per terminal
cd aureon_50k  && python watchdog.py live --i-understand-the-risks &
cd aureon_100k && python watchdog.py live --i-understand-the-risks &
```

### Scenario: "I want to test on weekends without market data"

```bash
python watchdog.py paper
# Bot detects market closed, sits idle politely, sends heartbeats
# When Monday comes, anchors fire normally
```

### Scenario: "I edited the strategy, want to redeploy"

```bash
# From phone
/stop                                        # graceful shutdown

# On VPS
# (Pull/edit files)
python bot.py backtest --csv data.csv        # quick sanity check
python watchdog.py paper                     # test for a day
# When confident:
python watchdog.py live --i-understand-the-risks
```

### Scenario: "I want a fresh backtest report"

```bash
python auto_analyze.py --days 365
# Creates: reports/AUREON_analysis_YYYY-MM-DD.md
# Sends Telegram summary
```

### Scenario: "Daily analysis is showing the strategy degrading"

```bash
# Compare last 4 weeks vs the rolling 12-month
python -c "
import pandas as pd
df = pd.read_csv('output/trades.csv')
df['date'] = pd.to_datetime(df['date'])
last_4w = df[df['date'] > df['date'].max() - pd.Timedelta(days=28)]
all_12m = df
print(f'Last 4 weeks: {last_4w[\"pnl_dist\"].sum():.1f} pips, {len(last_4w)} trades')
print(f'12-month avg same period: {all_12m[\"pnl_dist\"].sum() * 28 / 365:.1f} pips')
"

# If consistent underperformance: consider lot reduction or strategy review
# If single bad week: probably noise, wait
```

---

## 11. Emergency / Force Commands

### Kill everything immediately (no graceful shutdown)

```bash
# Linux
sudo pkill -9 -f "watchdog.py"
sudo pkill -9 -f "bot.py"

# Windows PowerShell
Stop-Process -Force -Name "python" -ErrorAction SilentlyContinue
```

⚠️ **This does NOT close open positions.** You must then go to MT5 manually and close them. Always prefer `/flatten` first.

### Disable systemd auto-restart temporarily

```bash
sudo systemctl mask aureon                   # prevents start
# do whatever
sudo systemctl unmask aureon
sudo systemctl start aureon
```

### Reset the bot to a clean state

```bash
/stop                                        # graceful from phone
rm -rf run/                                  # clear all state
mkdir run
python watchdog.py live --i-understand-the-risks
```

### Recover from "bot exited cleanly but watchdog keeps restarting"

This is the bug pattern you saw on Saturday. Check:

```bash
# Was it a market-closed exit?
journalctl -u aureon -n 50 | grep -i "market\|drift\|abort"

# Was Telegram or MT5 the issue?
python telemetry.py
python -c "import MetaTrader5 as mt5; print(mt5.initialize())"

# Check Telegram for any CRITICAL messages from the bot's last run
# (they tell you why it aborted)
```

---

## 12. Command Cheat Sheet (printable)

```
═══════════════════════════════════════════════════════════════
 AUREON v2 — Commands Cheat Sheet
═══════════════════════════════════════════════════════════════

 START / STOP
   python watchdog.py paper                    # paper trade
   python watchdog.py live --i-understand-...  # real money
   sudo systemctl start aureon                 # systemd
   sudo systemctl stop aureon

 FROM PHONE (Telegram)
   /status     /today      /help
   /pause      /resume
   /flatten    /restart    /stop

 BACKTEST
   python bot.py backtest --csv data.csv
   python auto_analyze.py --days 365

 DATA
   python fetch_data.py --days 365 --output X.csv
   python fetch_lab.py --symbol BTCUSD --timeframe M5 --days 365

 DEBUG
   python telemetry.py                          # test Telegram
   journalctl -u aureon -f                      # live logs
   cat run/status.json | python -m json.tool    # current state
   /status (from phone)                         # snapshot

 EMERGENCY
   /flatten                                     # close all positions
═══════════════════════════════════════════════════════════════
```

Keep this file open on a second monitor during the first 2 weeks of live trading. Once the workflow is muscle-memory, you'll only need `/status` and `/flatten`.
