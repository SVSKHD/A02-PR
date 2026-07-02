# AUREON self-restart via Windows Task Scheduler (Fix 4 / E-12, Level 3)

`run_aureon.bat` is the simplest self-restart launcher — a batch loop that relaunches
AUREON **only** on exit code `42` (a controlled feed-death self-restart) and stops on any
other exit code. This file documents a **Task Scheduler alternative** for operators who
prefer the OS scheduler to own process lifetime (auto-start at logon, restart on the VPS
after a reboot, run without an interactive console window).

## Why exit code 42

When the tick feed goes blind, AUREON escalates: re-subscribe → full in-process MT5 reinit
→ **controlled self-restart**. The self-restart persists `run/state.json` (Fix 5), posts a
`SELF-RESTART: feed dead` Discord alert, and calls `sys.exit(42)`. A relaunched process
recovers the same trading day (anchors already placed are skipped, Rogue governors + chain
anchor restored, an open position is adopted). Open positions stay protected by their
broker-side SL across the gap. The bot **never** self-restarts while the market is closed
(the weekend deep-sleep owns that case), so the relaunch only ever fires on a genuine
open-market feed death.

Any exit code **other than 42** (a clean `/stop`, an operator interrupt, or an unhandled
crash) is deliberately **not** relaunched by this mechanism — those go to the normal
watchdog / operator path so a real problem is seen, not silently respawned.

## Option A — Task Scheduler runs `run_aureon.bat` (recommended)

Let the batch loop keep the exit-42 contract and let Task Scheduler only (re)start the
launcher itself (e.g. at logon / after a reboot).

1. **Task Scheduler → Create Task…** (not "Basic Task").
2. **General:** name `AUREON`; *Run whether user is logged on or not*; *Run with highest
   privileges* (so the MT5 terminal is reachable).
3. **Triggers:** *At log on* (and optionally *At startup*).
4. **Actions:** *Start a program*
   - Program/script: `C:\A02-PR\run_aureon.bat`
   - Start in: `C:\A02-PR`
5. **Settings:**
   - *Allow task to be run on demand* ✔
   - *If the task fails, restart every* `1 minute`, *up to* `999` times (covers the batch
     window itself dying, not the exit-42 relaunch — that stays inside the loop).
   - *Do not start a new instance* (Instance policy) so you never get two bots racing on
     the magic numbers.

The exit-42 relaunch is handled **inside** `run_aureon.bat`; Task Scheduler just keeps the
launcher alive across logons/reboots.

## Option B — Task Scheduler drives the exit-42 relaunch directly (no .bat loop)

If you would rather not use the batch loop, point the action straight at the bot and let
the scheduler restart it — but note Task Scheduler cannot branch on a specific exit code,
so this restarts on **any** exit, which is coarser than the exit-42-only contract:

- **Actions → Start a program**
  - Program/script: `python`
  - Arguments: `bot.py live --lot 0.35 --i-understand-the-risks`
  - Start in: `C:\A02-PR`
- **Settings → If the task fails, restart every 1 minute, up to 999 times.**

Because this relaunches on every exit (not just 42), prefer **Option A** when you want a
clean `/stop` or a real crash to actually stop the process instead of respawning.

## Verifying the self-restart path

- Force the escalation on a demo terminal by dropping the XAUUSD subscription in Market
  Watch during open market; watch the log walk `re-subscribe → FEED REINIT attempt N →
  SELF-RESTART: feed dead`, then confirm the process exits 42 and the launcher relaunches.
- After relaunch, confirm the Discord `RESTART-RECOVERY OK …` line and that anchors already
  placed today are **not** re-placed and the Rogue governor counters are intact.
- `feed_selfrestart_enabled=False` (config) disables Level 3 — the feed then stays escalated
  at Level 2 (repeated reinit attempts) and the bot never exits 42.
