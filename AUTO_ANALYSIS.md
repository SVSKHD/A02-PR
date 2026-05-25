# Auto-Analysis Pipeline

Daily rolling 12-month backtest, fully automated. Runs unattended, sends a summary to your Telegram, saves a markdown report.

## Components

```
fetch_data.py    Pulls XAUUSD M1 from MT5 in 30-day chunks, dedupes, saves CSV
auto_analyze.py  Orchestrator: fetch → backtest → summarize → Telegram → save report
```

## Manual run

```bash
# Set credentials (once, in your shell profile)
export AUREON_TELEGRAM_TOKEN='123:AAE...'
export AUREON_TELEGRAM_CHAT='987654321'

# Just fetch
python fetch_data.py --days 365 --output data/XAUUSD_M1_last_year.csv

# Just analyze an existing CSV
python auto_analyze.py --csv data/XAUUSD_M1_last_year.csv

# Full pipeline: fetch + analyze (this is what cron runs)
python auto_analyze.py --days 365
```

## What lands in your Telegram

```
📊 AUREON v2 — rolling backtest
Window: 2025-05-22 → 2026-05-22 (365d)

💰 Total: $+47,494 (+950 pips)
📅 Avg / month: $+3,653 (+73.1 pips)
🎯 Win rate: 96.6%
📉 Max DD: $-2,000 (-4.0%)
🔴 SLs: 26  |  🚨 Kill days: 1

Monthly breakdown:
✅ 2025-05: $+758
✅ 2025-06: $+2,448
✅ 2025-07: $+2,859
... etc
```

And a full markdown report saved to `reports/AUREON_analysis_{date}.md` with per-anchor breakdown.

## Cron setup (simple)

```bash
crontab -e
```

Add a line:
```
# AUREON v2 daily backtest at 09:00 UTC, weekdays only
0 9 * * 1-5  cd /home/trader/aureon_v2 && /usr/bin/python3 auto_analyze.py >> /var/log/aureon-analyze.log 2>&1
```

If you use cron, **export the env vars in `~/.bashrc` OR put them in the crontab itself** — cron doesn't inherit your shell environment by default:

```cron
AUREON_TELEGRAM_TOKEN=123:AAE...
AUREON_TELEGRAM_CHAT=987654321

0 9 * * 1-5  cd /home/trader/aureon_v2 && /usr/bin/python3 auto_analyze.py >> /var/log/aureon-analyze.log 2>&1
```

## Systemd timer setup (preferred for production)

More robust than cron: better logging via journalctl, accurate scheduling even across reboots, supports randomized delays to avoid thundering-herd on shared brokers.

**`/etc/systemd/system/aureon-analyze.service`:**
```ini
[Unit]
Description=AUREON v2 daily auto-analysis
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=trader
WorkingDirectory=/home/trader/aureon_v2
EnvironmentFile=/home/trader/aureon_v2/.env
ExecStart=/usr/bin/python3 auto_analyze.py
StandardOutput=journal
StandardError=journal
```

**`/etc/systemd/system/aureon-analyze.timer`:**
```ini
[Unit]
Description=Run AUREON daily analysis weekdays 09:00 UTC

[Timer]
OnCalendar=Mon..Fri *-*-* 09:00:00 UTC
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

**`/home/trader/aureon_v2/.env`** (chmod 600 — has your broker password):
```
AUREON_TELEGRAM_TOKEN=123:AAE...
AUREON_TELEGRAM_CHAT=987654321
```

**Enable:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aureon-analyze.timer
sudo systemctl list-timers aureon-analyze.timer    # confirm next run
journalctl -u aureon-analyze.service -f             # follow output
```

## Outputs

| Path | Content |
|------|---------|
| `data/XAUUSD_M1_{start}_to_{end}.csv` | Raw M1 bars (one per day) |
| `reports/AUREON_analysis_{date}.md` | Full markdown report |
| `reports/trades_{date}.csv` | Per-trade detail |
| Telegram message | Summary card (see above) |
| `journalctl -u aureon-analyze` | Full log |

## Disk-usage note

A year of M1 is ~525,000 rows ≈ 35 MB CSV. Each daily run produces a fresh CSV. Add a cleanup line to your timer or cron to delete files older than 90 days:

```bash
find /home/trader/aureon_v2/data -name "XAUUSD_M1_*.csv" -mtime +90 -delete
find /home/trader/aureon_v2/reports -name "*.md" -mtime +180 -delete
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `MT5 init failed` | MT5 terminal not running on the host | On Windows VPS: start MT5 terminal first. On Linux: this script only works against a Wine-hosted MT5 terminal — easier to run on Windows. |
| `Symbol XAUUSD not found` | Broker uses different symbol (XAUUSDm, GOLD, XAUUSD.r) | `python fetch_data.py --symbol XAUUSDm ...` |
| `No data was returned by MT5 across the entire range` | Broker server didn't return history; possible auth issue | Check MT5 terminal manually first; verify symbol shows recent bars |
| Cron runs but no Telegram | Env vars not exported in cron context | Put them at the top of the crontab itself (see above) |
| Daily run takes >10 min | Network latency to MT5 server | Increase `--chunk-days` to 60, or run with `nice -n 10` |
