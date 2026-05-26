# AUREON v2.5 — Hardened Bot Deployment

## What changed from v2.4 → v2.5

11 hardening patches addressing the audit:

| # | Patch | Risk fixed |
|---|---|---|
| 1 | Atomic state writes + .bak corruption recovery | State file corruption could brick startup |
| 2 | PID lock prevents multiple instances | Two bots would conflict on magic/OCO |
| 3 | Non-blocking 5s deferred dispatch | v2.4 sleep blocked position management |
| 4 | Tick freshness validation (>60s = skip) | Stale tick could trigger bad pre-flight |
| 5 | Account floor enforcement | Bot would trade down to $0 ignoring 85% floor |
| 6 | `modify_position_sl` rc=-1 reconciliation | SL trail updates silently failed |
| 7 | `cancel_order` rc=-1 reconciliation | Sibling cancels silently failed |
| 8 | EOD flatten retry 3x + verify + critical alert | Positions could stay open overnight |
| 9 | `max_fav` + `fill_time` rehydration on restart | Restart wiped $5 lock state |
| 10 | Lot validated against broker `volume_step` | Wrong lot size on different brokers |
| 11 | Crash handler + PID release in finally | Unhandled exception left stale PID lock |

Plus: `freeze_minutes` default flipped to 15 (matches the $198k/year projection that was previously aspirational).

## Pre-deployment

Install psutil if not already present (needed for PID lock):
```powershell
pip install psutil
```

## Deploy

```powershell
cd C:\Users\HitheshSunkara\Desktop\AUREON_MAY_BOT\PROD

# Stop the running bot (Ctrl+C in its terminal, or kill the watchdog)

# Backup current files
Copy-Item bot.py         bot.py.v24.bak
Copy-Item live_trader.py live_trader.py.v24.bak

# Drop v2.5 files in (replace bot.py and live_trader.py)

# Validate
python validate_v25.py

# If all PASS, restart bot
python live_trader.py --i-understand-the-risks --lot 0.5
```

## What to expect at the next anchor (A3 at 16:30 IST)

New log lines you'll see:
```
A3_14h_Overlap: anchor captured @ $4XXX.XX, deferring placement to HH:MM:SS UTC (5s settle wait — non-blocking)
```

Then ~5 seconds later:
```
Trail advance ticket=XXX side=XXX SL $XXX → $XXX (max_fav=$XXX.XX)
```

If a position fills, on each M1 close:
```
✅ Modify SL ticket=XXX → $XXX: retcode=10009 (DONE)
```

Or if it hits rc=-1:
```
⚠ Modify SL ticket=XXX: rc=-1, broker SL still $XXX (wanted $XXX) — retrying
✅ Modify SL ticket=XXX → $XXX on RETRY: retcode=10009
```

## What changes in Telegram

New messages possible:

| Trigger | Message |
|---|---|
| Account below 85% floor | `⛔ A2_10h_London BLOCKED — account floor breached` |
| Stale tick from MT5 | `⚠️ A2 skipped — stale tick (62s > 60s)` |
| Position rehydrated after restart | `♻️ Rehydrated position 12345 BUY entry=$4550 max_fav=$4555 ...` |
| EOD failed to close everything | `🚨 FLATTEN INCOMPLETE — manual intervention needed` |
| Crash | `🚨 AUREON CRASHED — unhandled exception` |

## Rollback (if needed)

```powershell
cd C:\Users\HitheshSunkara\Desktop\AUREON_MAY_BOT\PROD
Copy-Item bot.py.v24.bak         bot.py
Copy-Item live_trader.py.v24.bak live_trader.py
# Restart bot
```

State files (`aureon_v2_state.json`) are compatible across v2.4 and v2.5 — no migration needed.

## Known limitations remaining (NOT fixed in v2.5)

These were lower priority and deferred:

1. **Test suite** — still none. Patches were validated against live broker tests, not unit tests.
2. **Paper mode** — still a dry-run logger, not a simulator with virtual fills.
3. **News blackout** — no economic calendar integration.
4. **Weekly stop** — only daily kill switch.
5. **Native OCO** — still software-emulated with ~200ms race window. Would need MQL EA on broker side.
6. **Heartbeat-based watchdog upgrade** — watchdog still only checks process alive, not stuck-but-alive.

These are not market-breaking. They're maturity gaps that can be addressed over time.