# DEPLOY_RUNBOOK — AUREON v3.0.0 → `C:\A02-PR\`

For the human. Do this **only while the book is FLAT and the market is CLOSED**
(weekend). Each step is gated; stop and roll back if any check fails.

> Prereq: `PRE_DEPLOY_CHECK.md` shows PASS on all 7 items. PR #3 need NOT be merged
> to deploy — you deploy the branch tree this weekend, validate Monday live, then
> merge (see `MERGE_GATE.md`).

## Step 0 — Confirm flat + closed
- MT5 terminal: **no open XAUUSD positions, no pending orders** under magic 20260522.
- Market closed (weekend). The bot, if running, should already be in weekend-sleep.
- Stop the current bot/watchdog process cleanly (`/stop` via Telegram, or kill the watchdog).

## Step 1 — Back up the current live folder (this IS the 2.9.8 rollback)
```
cd C:\
powershell Compress-Archive -Path C:\A02-PR\* -DestinationPath C:\A02-PR_backup_2026-06-13_v2.9.8.zip
```
Verify the zip exists and is non-empty. **Do not proceed without it.**
Also copy `C:\A02-PR\state.json` (and any `state.json.bak`) somewhere safe — it is
git-ignored and must survive the deploy untouched.

## Step 2 — Copy the merged tree into `C:\A02-PR\`
Copy these files from the merged branch into `C:\A02-PR\` (overwrite):

**New modules (12):**
`utils.py  config.py  strategy.py  mt5_adapter.py  backtest.py  state.py  risk.py  anchors.py  fills.py  trails.py  journal.py  firebase_journal.py`

**Modified (3):**
`bot.py  live_trader.py  version.py`

**Modified support files:**
`requirements.txt  .gitignore`

**Unchanged vs 2.9.8 — re-copy is harmless, NOT required** (`git diff master HEAD`
is empty for these): `watchdog.py  telemetry.py  env_loader.py`

**DO NOT copy / DO NOT delete on the VPS:**
- `firebase_key.json` — must ALREADY be present at `C:\A02-PR\firebase_key.json`
  (git-ignored, never in the repo). If absent, the journal silently disables itself
  (fail-safe) — trading is unaffected; add the key when convenient.
- `state.json` / `state.json.bak` — the live state. **Leave it in place.**
- `aureon_v2_state.json` in the repo is a stale sample — ignore it; the live runtime
  state file is whatever `cfg.state_file` points to on the VPS.

> Tip: deleting `*.pyc` / `__pycache__\` in `C:\A02-PR\` before launch avoids stale
> bytecode from the old module layout.

## Step 3 — Install the new dependency
```
cd C:\A02-PR
pip install -r requirements.txt
```
`firebase-admin>=6.0` is the only new package. If the VPS has no outbound network,
skip it — the journal degrades fail-safe and trading is unaffected.

> ✅ **Prefer starting when the market is open.** `MT5Adapter` detects the broker
> time offset from a LIVE tick feed at init. With the hardening PR's #4 guard a
> closed-market start no longer crashes (it warns, sleeps, and re-detects the
> offset on Monday wake before any trade) — but a start at/after **Sunday ~22:00
> UTC pre-open** is cleanest: the offset detects immediately as **+3h** and you
> see it in the banner. (On plain v3.0.0 *without* the hardening PR, a
> closed-market start WILL crash at adapter init — there, you MUST start when the
> market is open.)

## Step 4 — Paper smoke test (GATE) — run at/after Sunday pre-open
```
cd C:\A02-PR
python bot.py paper
```
Confirm in Telegram:
1. The banner prints **`v3.0.0`** and the **`Modules (14): …`** receipt exactly as in
   `PRE_DEPLOY_CHECK.md §5`. Wrong version or a short module list ⇒ deploy didn't land.
2. The broker time offset logs as **`+3h`** (not `+0h`) — the live feed is detected.
3. No tracebacks in the console/log. (If you must verify earlier while the market is
   dead, expect an adapter-init error — that's #4; just retry once ticks are live.)
Then stop it (`Ctrl-C` / `/stop`). Paper mode places no orders.

## Step 5 — Arm for Monday (live) — at/after Sunday pre-open
Start the watchdog in live mode exactly as you do today:
```
cd C:\A02-PR
python watchdog.py live --i-understand-the-risks
```
It spawns `bot.py live`. Expect the v3.0.0 banner and the `+3h` offset. If launched
while the market is briefly between sessions, the process self-sleeps and
**auto-resumes** at the first fresh tick — no manual restart. Leave it running.

## Step 6 — Rollback (if anything looks wrong, any time)
1. Stop the watchdog/bot.
2. Delete the contents of `C:\A02-PR\` **except** `state.json`, `state.json.bak`,
   and `firebase_key.json`.
3. Extract `C:\A02-PR_backup_2026-06-13_v2.9.8.zip` back into `C:\A02-PR\`.
4. Relaunch the watchdog as usual → 2.9.8 behavior returns; the live `state.json`
   rehydrates into 2.9.8 unchanged (every persisted key is preserved by v3.0.0).

---
**After a clean Step 4 + a running Step 5, you're deployed. Validate Monday against
`MERGE_GATE.md`, then merge PR #3.**
