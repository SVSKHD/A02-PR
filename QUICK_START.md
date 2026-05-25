# AUREON v2 — Quick Start (30 minutes to live)

For the impatient. Full details live in the other .md files; this is the minimum to get from zero to running bot.

## 0. Prerequisites

- **Windows VPS** (4+ GB RAM, always-on). Linux works for backtest only.
- **Funding Pips account** (any size — bot auto-detects).
- **MetaTrader 5 terminal** installed on the VPS, logged into your Funding Pips account, "Allow algorithmic trading" enabled in Tools → Options → Expert Advisors.
- **Python 3.10+** on the VPS.
- **Telegram bot token + your chat ID** — see `TELEGRAM_SETUP.md` if you don't have one.

## 1. Install (5 min)

```bash
git clone <repo>   # or unzip the package
cd aureon_v2
pip install -r requirements.txt
```

## 2. Set Telegram env vars (5 min)

```bash
# Add to ~/.bashrc, or set in your systemd unit
export AUREON_TELEGRAM_TOKEN="123:AAE..."
export AUREON_TELEGRAM_CHAT="987654321"
```

Test:
```bash
python telemetry.py
# → 5 test messages should arrive in Telegram
```

## 3. Backtest sanity check (5 min)

```bash
# Use the project's historic CSV, OR fetch fresh:
python fetch_data.py --days 365 --output data/XAUUSD_M1_last_year.csv

# Backtest
python bot.py backtest --csv data/XAUUSD_M1_last_year.csv --output-dir ./output

# Expect: ~+72 pips/month, win rate ~96%, max DD ~4%, kill_days 1-2
```

If your numbers diverge by >15% from the expected baseline, **stop and investigate** before going live.

## 4. Paper trade for 2 weeks (mandatory, 10 min setup)

Make sure MT5 terminal is running and logged in. Then:

```bash
python watchdog.py paper
```

Watch the Telegram feed for 2 weeks:
- 4 anchor messages per trading day
- 0–4 fill messages per day
- 0–4 close messages per day
- 1 EOD summary per day
- Daily P&L should track at roughly 60–85% of the backtest's pace (the spread/slippage drag)

If P&L is wildly different from backtest, **stop and investigate**.

## 5. Go live (5 min)

```bash
# Stop paper, start live
sudo systemctl stop aureon-paper      # if using systemd
python watchdog.py live --i-understand-the-risks
```

Or set up systemd (template in `aureon.service.example`):

```bash
sudo cp aureon.service.example /etc/systemd/system/aureon.service
sudo systemctl daemon-reload
sudo systemctl enable --now aureon.service
```

## What to do from your phone

```
/status     current P&L, positions, kill switch state, balance
/today      today's fills + running total
/flatten    emergency close everything
/pause      stop placing new anchors
/resume     resume anchor processing
/restart    graceful bot restart
/stop       shut down
/help       command list
```

## What to monitor in the first week

| Day | Check |
|----:|-------|
| 1 | All 4 anchors fired? Daily summary arrived? |
| 2-3 | Number of fills matches paper-trade expectations? |
| 7 | Week 1 P&L within ±25% of backtest projection? |
| 14 | First payout window (if cushion + consistency met) |

## When things go wrong

| Symptom | Action |
|---------|--------|
| Telegram silent for 1 hour | `journalctl -u aureon -f` on VPS |
| Watchdog crashed (no heartbeat) | Watchdog auto-restarts; if not, `systemctl restart aureon` |
| Account drawdown > 4% | `/flatten` from phone, investigate |
| Anchor missed | Check broker time drift (see Telegram alert) |
| Kill switch fires twice in a row | Stop, investigate market regime; do NOT just resume |

## Files reference card

```
bot.py                   The strategy + MT5 adapter + backtest
live_trader.py           4-anchor event loop, OCO emulation, trail
watchdog.py              Supervisor + Telegram command listener
telemetry.py             Telegram + log notifications
fetch_data.py            Production data fetcher (XAUUSD M1)
auto_analyze.py          Daily rolling 12-month backtest
fetch_lab.py             Strategy R&D: any symbol, any timeframe
strategy_template.py     Scaffold for new strategies
AUREON_V2_SPEC.md        The strategy recipe (deterministic spec)
WHOLE_PACKAGE.md         12-section deep tour
TELEGRAM_SETUP.md        Telegram bot + chat ID setup
AUTO_ANALYSIS.md         Cron / systemd timer setup
QUICK_START.md           This file
aureon.service.example   Systemd unit template
```

That's it. The bot does everything else automatically.
