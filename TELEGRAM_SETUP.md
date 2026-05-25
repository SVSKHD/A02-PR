# Telegram Setup for AUREON v2

Step-by-step guide to wiring AUREON v2 to your Telegram for notifications and remote control.

## 1. Create a bot

1. Open Telegram, search for `@BotFather`, start a chat
2. Send `/newbot`
3. Pick a name (e.g. "AUREON Trading Bot") and a unique username (must end in `bot`, e.g. `aureon_yourname_bot`)
4. BotFather replies with a token like `7234567890:AAEhBOLqxFh-...`. **Save this** — it's your `AUREON_TELEGRAM_TOKEN`

## 2. Get your chat ID

1. Send any message to your new bot (e.g. "hi")
2. Open this URL in a browser (replace `<TOKEN>` with your token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. You'll see JSON. Look for `"chat":{"id":987654321,...}`. The number is your `AUREON_TELEGRAM_CHAT`

If you don't see your message in the JSON, send another message in Telegram first and refresh.

## 3. Configure AUREON

Set environment variables (Linux/Mac):
```bash
export AUREON_TELEGRAM_TOKEN="7234567890:AAEhBOLqxFh-your-actual-token"
export AUREON_TELEGRAM_CHAT="987654321"
export AUREON_TELEGRAM_MIN_SEVERITY="INFO"     # INFO|SUCCESS|WARN|ERROR|CRITICAL
export AUREON_LOG_FILE="/var/log/aureon.log"   # optional
```

Or on Windows (PowerShell):
```powershell
$env:AUREON_TELEGRAM_TOKEN="7234567890:AAEhBOLqxFh-your-actual-token"
$env:AUREON_TELEGRAM_CHAT="987654321"
$env:AUREON_TELEGRAM_MIN_SEVERITY="INFO"
```

Persist these by adding them to `~/.bashrc` / `~/.zshrc` (Linux/Mac) or a startup script on Windows.

## 4. Test the connection

```bash
python telemetry.py
```

You should see 5 messages arrive in Telegram (one of each severity except DEBUG which is log-only).

## 5. Run AUREON with telemetry + watchdog

```bash
python watchdog.py paper
```

You'll get a startup message in Telegram like:
> ✅ AUREON-watchdog
> 🤖 AUREON Watchdog started
> Bot args: paper
> Run dir: ./run
> Telegram polling: on
> Send /help to see commands.

## What you'll receive in Telegram

| Event | Severity | Telegram-delivered? |
|-------|---------:|--------------------:|
| Bot started / stopped | INFO + SUCCESS | yes |
| Anchor processed (orders placed) | INFO | yes (rate-limited to 1 per 5s) |
| Position filled | INFO | yes |
| Trade closed at profit | SUCCESS | yes |
| Trade closed at SL | WARN | yes |
| Trail SL advancing | INFO | log only (too noisy) |
| EOD daily summary | SUCCESS / WARN | yes |
| Kill switch fired | CRITICAL | yes |
| Bot crash / restart | CRITICAL / SUCCESS | yes |
| Heartbeat stale | ERROR | yes |
| 5+ consecutive crashes | CRITICAL + watchdog exits | yes |

## Commands you can send

| Command | What it does |
|---------|--------------|
| `/help` | List commands |
| `/status` | Current state: positions, daily P&L, kill switch |
| `/today` | List today's trades and totals |
| `/restart` | Graceful bot restart (watchdog kills bot, respawns) |
| `/stop` | Stop watchdog + bot (you'll need to restart manually) |
| `/flatten` | Emergency: close every open position immediately |
| `/pause` | Stop placing new anchor orders (existing trails keep running) |
| `/resume` | Resume placing anchor orders |

Only messages from `AUREON_TELEGRAM_CHAT` are accepted — other chat IDs are silently ignored, so even if someone discovers your bot username, they can't control it.

## Severity tuning

If Telegram gets too chatty, raise the threshold:
```bash
export AUREON_TELEGRAM_MIN_SEVERITY="WARN"
```
Then only SL hits, errors, kill switches, and crashes notify Telegram. INFO and SUCCESS still appear in the log file and console.

If you want even more notifications during initial paper-trading shakedown:
```bash
export AUREON_TELEGRAM_MIN_SEVERITY="INFO"   # default
```

## Auto-start on boot (Linux systemd)

Create `/etc/systemd/system/aureon.service`:
```ini
[Unit]
Description=AUREON v2 Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/aureon_v2
Environment="AUREON_TELEGRAM_TOKEN=7234567890:AAEhBOLqxFh-..."
Environment="AUREON_TELEGRAM_CHAT=987654321"
Environment="AUREON_TELEGRAM_MIN_SEVERITY=INFO"
Environment="AUREON_LOG_FILE=/var/log/aureon.log"
ExecStart=/usr/bin/python3 watchdog.py live --lot 0.5 --i-understand-the-risks
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable aureon.service
sudo systemctl start aureon.service
sudo systemctl status aureon.service
journalctl -u aureon -f         # follow logs
```

Now AUREON survives reboots, watchdog auto-restarts the bot on crash, and you control it from your phone.
