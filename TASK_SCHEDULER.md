# AUREON self-restart via Windows Task Scheduler (Fix 4 / E-12, Level 3)

> **Active launch chain: `watchdog.py`.** `python watchdog.py live` is the supervised
> launcher AUREON runs under. As of the P1 follow-up it owns the exit-code relaunch policy
> itself: it **relaunches the bot ONLY on exit code `42`** (the controlled feed-death
> self-restart) and, on **any other exit code** (a crash, a clean `/stop`, or a clock-drift
> abort — exit 0), it **alerts Discord and stops** rather than relaunching — so a crashing
> bot can never crash-loop and spam-place orders on every boot. A runaway 42-loop
> (`MAX_CONSECUTIVE_SELFRESTARTS`) also stops for a human. Heartbeat-hung and manual
> `/restart` remain separate controlled restarts.
>
> **`run_aureon.bat` is a documented ALTERNATIVE only** — a minimal batch loop that applies
> the same exit-42-only relaunch contract when launching the bot **directly** (`python
> bot.py live`, bypassing the watchdog). Use it only if you are not running under
> `watchdog.py`; do not run both at once.

`run_aureon.bat` is the simplest standalone self-restart launcher — a batch loop that
relaunches AUREON **only** on exit code `42` and stops on any other exit code. This file
documents a **Task Scheduler alternative** for operators who prefer the OS scheduler to own
process lifetime (auto-start at logon, restart on the VPS after a reboot, run without an
interactive console window).

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
crash) is deliberately **not** relaunched — the watchdog (or `run_aureon.bat`) alerts and
stops so a real problem is seen, not silently respawned.

## Option A — Task Scheduler runs `watchdog.py` (recommended — the active chain)

The watchdog is the supervised launcher and already owns the exit-42-only relaunch policy
(relaunch on 42, stop + alert on anything else, stop on a runaway 42-loop). Let Task
Scheduler only (re)start the **watchdog** at logon / after a reboot.

1. **Task Scheduler → Create Task…** (not "Basic Task").
2. **General:** name `AUREON`; *Run whether user is logged on or not*; *Run with highest
   privileges* (so the MT5 terminal is reachable).
3. **Triggers:** *At log on* (and optionally *At startup*).
4. **Actions:** *Start a program*
   - Program/script: `python`
   - Arguments: `watchdog.py live --lot 0.35 --i-understand-the-risks`
   - Start in: `C:\A02-PR`
5. **Settings:**
   - *Allow task to be run on demand* ✔
   - *If the task fails, restart every* `1 minute`, *up to* `999` times (this covers the
     **watchdog process itself** dying — e.g. the OS killing it — not the exit-42 relaunch,
     which the watchdog handles internally; it also re-starts the watchdog after it
     deliberately stops on a non-42 bot exit, so keep this modest and rely on the Discord
     alert to intervene).
   - *Do not start a new instance* (Instance policy) so you never get two watchdogs racing
     on the magic numbers.

The exit-42 relaunch is handled **inside `watchdog.py`**; Task Scheduler just keeps the
watchdog alive across logons/reboots.

## Option B — `run_aureon.bat` (alternative, no watchdog)

If you are **not** running under `watchdog.py`, `run_aureon.bat` applies the same
exit-42-only relaunch contract while launching the bot directly (`python bot.py live`).
Point a Task Scheduler action at the batch file instead:

- **Actions → Start a program**
  - Program/script: `C:\A02-PR\run_aureon.bat`
  - Start in: `C:\A02-PR`
- **Settings → If the task fails, restart every 1 minute, up to 999 times** (covers the
  batch window dying, not the exit-42 relaunch — that stays inside the loop).

Do **not** run Option A and Option B at the same time — one supervisor only, or two
processes will race on the magic numbers.

## Verifying the self-restart path

- Force the escalation on a demo terminal by dropping the XAUUSD subscription in Market
  Watch during open market; watch the log walk `re-subscribe → FEED REINIT attempt N →
  SELF-RESTART: feed dead`, then confirm the process exits 42 and the launcher relaunches.
- After relaunch, confirm the Discord `RESTART-RECOVERY OK …` line and that anchors already
  placed today are **not** re-placed and the Rogue governor counters are intact.
- `feed_selfrestart_enabled=False` (config) disables Level 3 — the feed then stays escalated
  at Level 2 (repeated reinit attempts) and the bot never exits 42.
