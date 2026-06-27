# AUREON v3 — Multi-Anchor XAUUSD Bot

Automated **4-anchor daily straddle** bot for gold (XAUUSD), built on MetaTrader 5
(Python). Four session anchors per day (Asia, London, London-NY overlap, NY),
**No-OCO** straddle discipline per anchor, a profit-protecting hold + ladder, a
crash-branch rescue/boost fleet, and full Telegram operations.

> **Current version: `v3.0.1` (Astra Hawk).** `version.py` is the single source of
> truth — the startup banner prints the version + a module receipt so the version
> you see in Telegram is, by construction, the version that is running.

Deployed on a Windows VPS at `C:\A02-PR\`, watchdog-supervised, currently running
on a Pepperstone demo (account-validation phase).

---

## Strategy (frozen spec)

Four daily anchor straddles on gold. At each anchor: capture the M5 close, place a
**buy stop +$5** and a **sell stop −$5** from the anchor price. **No-OCO** — the
sibling stays live after the first fill, so a reversal can second-fill.

- **SL $18 · TP $30.** First fill starts a **45-minute hold** (no trail exits
  inside the hold; SL / TP / ladder stay live).
- **Anchors (broker = UTC+3 / IST):** A1 02:30 / 5:00 AM · A2 10:00 / 12:30 PM ·
  A3 14:30 / 5:00 PM · A4 16:40 / 7:10 PM · A5 19:30 / 10:00 PM. On **Mondays only**,
  A1 fires at 03:30 broker / 6:00 AM IST (cold-start cushion; `cfg.monday_a1_override`).
  _(v3.3.6: A3 retimed → 17:00 IST. v3.3.8: 5th anchor A5 `A5_1930_LateUS` added at
  22:00 IST — identical structure; each anchor's trades are journal-tagged A1–A5 for
  isolated month-end P&L.)_
- **Ladder (one-way ratchet).** NORMAL leg: +$2.5 → BE · +$6 → lock +$4 · +$10 →
  trail peak−$2 (floor +$8). RESCUE leg: only the +$10 tier.
- **$10 fleet trigger.** When a leg is −$10, the sibling fill becomes a RESCUE leg
  plus **2 market BOOSTS** in the rescue direction ($6 SL, $30 TP each).
- **TSTOP.** At minute 45, if peak favorable < $1 → close at market.
- **Post-hold trail.** arm $2.50, gap $2.00.
- **Kill switch.** −3% daily equity halts all anchors; EOD flatten at 23:00 broker.
- Worst-case anchor ≈ −$1,050; worst day (kill switch) ≈ −$1,500 — both inside
  risk limits.

The exact mechanics live in `strategy.update_position_on_bar()` — the same pure
function the backtest and the live loop both call, so live behavior matches
backtest modulo spread / slippage / execution delay.

---

## Architecture (v3.0.0 — 13-module split)

Refactored from two oversized files (`live_trader.py` ~2,374 lines, `bot.py`
~1,030) into focused, acyclic modules.

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
                    │  - auto-deploy gate  │  (optional, default OFF)
                    └──────────┬───────────┘
                               │ subprocess + run/ files
                    ┌──────────▼───────────┐
                    │  bot.py (CLI entry)  │
                    │     └─ live_trader   │  LiveTrader orchestrator
                    │        - event loop  │
                    │        - MT5 orders  │
                    │        - heartbeat   │
                    │        - status/cmds │
                    └──────────┬───────────┘
                               ▼
                          MT5 broker
```

### Module map

| Module | Role |
|--------|------|
| `version.py` | **Single source of truth** for `__version__` + behavioral changelog + `banner()`. |
| `config.py` | `Config` dataclass — every tunable (distances, SL/TP, ladder, anchors, Monday override, offset, stale-tick retry, risk %). |
| `utils.py` | Pure helpers: logging, `initial_sl/tp`, anchor/EOD UTC math, `m5_close_at`. Imports no AUREON module (cycle-proof). |
| `strategy.py` | **Pure core:** `Position` + `update_position_on_bar` (freeze, role-aware ladder, trail, SL/TP) + `realize_pnl_usd`. No I/O. |
| `mt5_adapter.py` | **Only** importer of `MetaTrader5`. Offset detection (live + stale-tick), M5/M1 reads, order place/modify/cancel/close, `mt5_comment()` (≤31-char truncation). |
| `backtest.py` | `run_backtest` + `summarize_backtest` (reuses `strategy`). |
| `state.py` | Persistent state load/save (atomic + `.bak`), PID lock. Bound onto `LiveTrader`. |
| `risk.py` | Lot sizing, kill switch, day-start equity, flatten-all. Bound onto `LiveTrader`. |
| `anchors.py` | Anchor scheduling + placement, Monday override, stale-tick retry, A1 confirmation, warmup/reconnect, MT5 forensics. Bound onto `LiveTrader`. |
| `fills.py` | Broker reconcile: fill/closure detection, No-OCO sibling handling, RESCUE detection (twin-open check) + boost trigger. Bound onto `LiveTrader`. |
| `trails.py` | Per-bar trail management, SL push, stop-through handling, exit classifier. Bound onto `LiveTrader`. |
| `journal.py` | 19-col CSV journal, daily/today summaries, `summarize_recent`, Firebase EOD save + weekly reconcile. Bound onto `LiveTrader`. |
| `live_trader.py` | **Slim orchestrator:** `LiveTrader` class shell + event loop + `run_live`; binds the method modules above. |
| `bot.py` | CLI entry — `backtest` / `paper` / `live` / `selftest`; re-exports the old public surface. |
| `selftest.py` | On-demand placement + rescue/boost self-test harness (`python bot.py selftest`). Vol_min throwaway orders, PASS/FAIL per step, refuses unless flat. |
| `verify_firebase.py` | Firebase backfill verifier (`python bot.py verifyfb`). Read-only by default; cross-checks journal CSVs vs Firestore, names MISSING days, `--backfill <date>` re-writes one day idempotently. Fail-safe. |
| `rescue_log.py` | Rescue FLEET-EVENT logger (observer only). Records each $10 fleet trigger (trigger/rescue/2 boosts) → `rescue_events.csv` + Firestore sub-collection; branch label CRASH_WIN / WHIPSAW_LOSS / SCRATCH. `python bot.py rescuestats` prints the tally. |
| `bescratch.py` | READ-ONLY BE-scratch "left on table" analyzer (`python bot.py bescratchscan`). Measures how often the +$2.5→BE rung scratches a trend and what it costs; replays looser rungs over recorded trades. No engine change. |
| `firebase_journal.py` | Firestore client + record builders + idempotent daily/weekly writes (fail-safe). |
| `telemetry.py` | Thread-safe Telegram + console + file engine, severity levels, Markdown-escape + plain-text failover. |
| `watchdog.py` | Parent supervisor: spawn/restart, heartbeat, Telegram command loop, **auto-deploy**. |
| `env_loader.py` | Loads `.env` before submodules read env vars. |

The methods in `state / risk / anchors / fills / trails / journal` were moved
**byte-identical** out of `live_trader.py` and bound back onto `LiveTrader` (proof
in `REFACTOR_NOTES.md`), so every call site, `state.json` key, Telegram string and
the 19-col journal schema are unchanged.

---

## Event loop (`LiveTrader._tick`, every 5s)

1. **Market closed?** → weekend deep-sleep + auto-resume Monday (heartbeat kept
   alive; offset re-detect on wake).
2. **Heartbeat** → touch `run/heartbeat`.
3. **New broker day?** → reset daily P&L, clear processed-anchors, unlock kill switch.
4. **Reconcile** → pull open positions + pendings; detect fills (No-OCO sibling),
   detect closures (update daily P&L), trigger RESCUE + boosts when a leg is −$10.
5. **Commands** → consume `/flatten /pause /resume` from the watchdog.
6. **Kill switch?** → flatten everything, lock for the day.
7. **EOD?** → flatten the book, write the Firebase EOD journal (once/day).
8. **Anchor due?** → capture M5, place buy stop + sell stop (stale-tick retry, A1
   confirmation); complete any deferred placement.
9. **M1 bar closed?** → run `update_position_on_bar` for each leg, push SL if it
   advanced (stop-through → market close).
10. **Status snapshot** → write `run/status.json`.

---

## Auto-deploy (auto git pull) — implemented, default OFF

The watchdog can keep the VPS in sync with `master` on its own. **It is wired into
the watchdog run loop (`_autodeploy_check`) but gated behind an env toggle**, so a
merge never surprise-deploys.

**How it works:**
1. **Poll** — every `AUTODEPLOY_POLL_MIN` minutes the watchdog reads remote
   `master` HEAD via `git ls-remote` (no working-tree change).
2. **Validate off-tree** — a new sha is fetched and checked in an isolated
   `git worktree`: `py_compile` of every `*.py` + an import smoke test of all
   modules. A broken merge **never** touches the live tree.
3. **Stage** — a validated sha becomes `update_pending` (Telegram notice).
4. **Apply at a safe window only** — the bot publishes `flat` / `eod_done` in
   `status.json`; the watchdog applies **only when the book is flat or at EOD**
   (never mid-trade): graceful-stop → `git merge --ff-only origin/master` →
   relaunch. `ff-only` keeps git-ignored `.env` / `state.json` /
   `firebase_key.json` / `logs` intact. A failed ff-only merge relaunches the
   **current** code (never leaves the bot down) and alerts.

**Enable it** (on the VPS `.env`, which is git-ignored):
```bash
AUTODEPLOY_ENABLED=1
AUTODEPLOY_POLL_MIN=5
```
With it OFF (the default), the watchdog still runs normally — it just never pulls.
The startup banner prints `Auto-deploy: ON/off` so you can confirm which is live.

> **Status:** the pull/validate/gated-restart path is implemented and tested in
> code. Because the toggle lives in the VPS-local `.env` (not in git), whether it
> is *currently active* depends on that file — check the watchdog startup banner
> (`Auto-deploy: ON/off`) on the VPS to confirm.

---

## Quick start

### 1. Install
```bash
pip install -r requirements.txt
```

### 2. Configure (.env)
Copy `.env.example` → `.env` and fill in Telegram + (optionally) Firebase /
auto-deploy / test-scope toggles. `.env` is git-ignored.
```bash
AUREON_TELEGRAM_TOKEN="123:AAE..."        # from @BotFather
AUREON_TELEGRAM_CHAT="987654321"          # your chat id
AUREON_TELEGRAM_MIN_SEVERITY="INFO"       # INFO|SUCCESS|WARN|ERROR|CRITICAL
```

### 3. Backtest on an M1 CSV
CSV columns: `time, open, high, low, close` (UTC timestamps).
```bash
python bot.py backtest \
  --csv your_XAUUSD_M1.csv \
  --start 2025-01-01 --end 2026-05-19 \
  --lot 0.35 --balance 50000 \
  --output-dir ./output
```
Outputs `output/trades.csv` and `output/stats.json` (aggregate + monthly P&L).

### 4. Paper / live (watchdog-supervised)
```bash
python watchdog.py paper --lot 0.35
python watchdog.py live  --lot 0.35 --i-understand-the-risks
```

### 4b. Self-test the rescue/boost fleet (when flat, on demo)
```bash
python bot.py selftest          # add --force to run market steps on a non-demo account
```
Exercises the entire placement + rescue/boost path against the connected MT5 demo
terminal with vol_min throwaway orders (cancelled/closed immediately) and prints a
`RESULT: 9/9 PASS — fleet ready` block to console + Telegram. It proves the boost
MARKET path places at `rc=10009` (the historical 0-for-7 failure) in ~2 minutes
instead of waiting for a real live rescue. Refuses to run unless the book is flat;
runs only via this command (never the live loop). Run it before relying on the
fleet — see `MERGE_GATE.md`.

### 4c. Verify the Firebase journal (read-only; safe while flat)
```bash
python bot.py verifyfb                       # report-only: lists docs, names MISSING days
python bot.py verifyfb --backfill 2026-06-15 # re-write ONE day from the journal CSV (idempotent)
```
Cross-checks every trading day in `run/journal/trades_*.csv` against the
`aureon_forex` Firestore collection and reports which days are present vs MISSING
(e.g. confirming a Monday EOD write actually landed). Read-only by default — it
**never auto-writes**; a single day is re-written only with `--backfill`. If
Firestore is unreachable it warns and exits 0, so it can never touch trading.

### BE-scratch "left on table" analyzer (v3.0.7, read-only)
Measures whether the +$2.5→breakeven ladder rung is costing profit by scratching
trends flat — **before** anyone loosens it.
```bash
python bot.py bescratchscan                 # uses run/journal + run/price_log
python bot.py bescratchscan --m1csv bars.csv --horizon 30
```
Classifies each trade as a BE-scratch (BE/near-BE exit with the +$2.5 rung armed),
computes **left on table** over a stated lookforward (entry + 45m hold + 30m), splits
**continued-in-favor vs reversed (BE correctly saved us)**, breaks down per anchor
(A1–A4), and replays a **counterfactual rung grid** `[+2.5/+3.5/+4/+5]` through a
parity-tested mirror of the strategy engine — reporting net P&L, scratches avoided,
extra SL hits, and runners saved, then a data-driven verdict. **Read-only**: no
Firestore writes, no config change, no orders; missing price history is marked
`insufficient_data`, never guessed.

### Rescue fleet-event dataset (v3.0.6)
Every $10 fleet trigger (a leg hits −$10 with its twin open → RESCUE + 2 BOOSTS)
is logged as one event — trigger/rescue legs, both boosts (ticket / fill / rc /
≤31-char comment), and on close the fleet **net** + a branch: `CRASH_WIN`
(directional, net +), `WHIPSAW_LOSS` (mean-revert, net −), or `SCRATCH` (|net| <
$50). Rows append to `run/rescue_events.csv` and mirror to Firestore
`aureon_forex/{date}/rescue_events/{event_id}`; a `📊 FLEET EVENT` Telegram posts
on close with the running crash/whipsaw/scratch tally. Read the dataset with:
```bash
python bot.py rescuestats     # running tally + per-event table (read-only)
```
**Observer only** — this never alters rescue/boost trigger logic, sizing, or
geometry; all hooks are wrapped so a logging error can't reach the engine.

### Anchor late-retry (v3.0.5)
If an anchor doesn't **place** by its scheduled time — for any reason (quiet feed,
stale tick, Monday wake, channel-warmup fail, transient broker `rc`) — the bot
keeps re-attempting on the stale-retry cadence for `anchor_late_window_min`
(default **10**) minutes, re-capturing the anchor price at the moment it actually
places (straddle geometry ±$5 / SL $18 / TP $30 unchanged). A late fire posts
`⏰ LATE ANCHOR`; if the window elapses with no placement it posts a loud
`❌ ANCHOR MISSED` (scheduled time, reason, minutes waited) — the alert that ends
silent misses. Hard stops are never overridden: no late-place through the kill
switch, past EOD, on weekends, or once the window elapses; one placement per
anchor per day. Every anchor message (placement / LATE / MISSED / fill / close)
shows both the scheduled and actual time (server + IST).

### Timestamped alerts (v3.0.4)
Every outbound Telegram message is prefixed with a single-source header, e.g.
`🕐 5:00 AM IST (server 02:30 · IST 05:00) — Tue Jun 16` — 12-hour IST, then the
server (UTC+3) and IST (broker+2:30) 24-hour clocks derived from one captured
instant, then the date. Built once in `telemetry.ts_header()` and prepended in
`_send_telegram`; no call site hand-formats timestamps.
Control from Telegram: `/status`, `/today`, `/pause`, `/resume`, `/flatten`,
`/restart`, `/stop`. On a crash the watchdog auto-restarts with exponential
backoff and notifies on Telegram. `/status` works during weekend sleep and
returns last-trading-day per-anchor P&L + week-to-date.

---

## State, journal & secrets

- **`state.json`** (default `aureon_v2_state.json`) — daily P&L, processed
  anchors, kill-switch lock, shadow positions + pendings (rescue flags survive
  restarts). Atomic write + `.bak` fallback.
- **`run/`** — `heartbeat`, `status.json`, `commands.json`, `today_trades.csv`,
  `deployed_sha.txt`.
- **`trades_<YYYY-MM>.csv`** — the 19-column local journal.
- **Firestore `aureon_forex`** — EOD-only, one idempotent doc per day
  (`firebase_journal.py`); weekly reconcile backfills any missed day. Requires
  `firebase_key.json` (git-ignored, never committed).

---

## Operational docs

| Doc | What it covers |
|-----|----------------|
| `AUREON_V2_SPEC.md` | The full strategy specification. |
| `REFACTOR_NOTES.md` | The 13-module split, byte-identical move proof, change log. |
| `DEPLOY_RUNBOOK.md` | VPS deploy steps. |
| `PRE_DEPLOY_CHECK.md` | Pre-deploy checklist. |
| `MERGE_GATE.md` | Merge / validation gate. |
| `HARDENING_NOTES.md` | Wake/offset/A1 hardening notes. |

---

## Known open items

- **Boost fleet — fixed but unproven live.** `v3.0.1` fixes the root cause (MT5
  silently rejects order comments > 31 chars); the next genuine rescue must show
  boosts placing at `rc=10009` to confirm the crash-branch upside.
- **Multi-week green demo record** required before any funded-account money.
- **A1 Monday time** — RESOLVED (v3.3.6): placement was audit-confirmed correct
  (Monday A1 = 03:30 broker / 6:00 AM IST via `monday_a1_override`; weekdays 02:30 /
  5:00 AM). The defect was display-only — readiness/status/banner now derive A1's
  time from the resolver (`_resolved_anchor_hm`) instead of stale hardcoded strings.

### v3.4.0 — RALLY override pullback-entry (flag-gated, **DEFAULT OFF**, month-end candidate)

**What's new.** The +$20 parent-direction RALLY override (v3.3.5) fires at the *extreme*
of the move and gets knifed by the natural breath (Jun 25 A3: fired the top, pulled back
$13, −$905). When `override_entry_enabled=True`, the override no longer fires immediately:
it **arms** at +$20, tracks the running extreme, and **enters on the first touch** of a
`override_entry_pullback_dollars` ($13) retrace from that extreme (SL still $13 from the
pullback entry). If no pullback appears within `override_entry_arm_timeout_candles` (4 M5
candles ≈ 20 min) it **skips** the boost (a skip is free; a bad entry costs ~$905). New
PTRACE `OVERRIDE_ENTRY_ARMED` / `OVERRIDE_ENTRY_SKIPPED` give the trial its
pullback-frequency data. RALLY override **only** — RESCUE, the +$5 rally arm, and the
`rally_pullback_*` **exit** detector are untouched.

**OFF-by-default guarantee.** With `override_entry_enabled=False` (the default),
`rally.break_and_hold_ok` runs the v3.3.8 logic **verbatim** — the override fires
immediately, byte-identical. The live demo trial keeps running on the OFF path. Proven by
selftest 106 (freeze guard) plus the unchanged override tests 96/97/98.

**Open numbers the trial will tune.** Band depth ($13), arm timeout (4 M5 candles), and
first-touch-vs-confirm-candle (`override_entry_first_touch=True`; confirm-candle reserved,
not implemented) are first guesses, not final.

> ⚠️ **May become DELETE, not SHIP.** This was built *before* the pullback-frequency data
> exists. If the trial shows override-grade moves rarely pull back, the detector will skip
> most of the time and the correct action is to **remove the override entirely**
> (subtraction > addition), not enable this. Built flag-OFF so both options stay open.

### v3.5.0 — adaptive pullback entry, RALLY + RESCUE (flag-gated, **DEFAULT OFF**, month-end candidate)

**What's new.** Both the rally override and the rescue hedge stop firing *at the edge* of the
move. They **arm**, then pick the best entry via one shared pure helper (`pullback_entry.step`):
- **Pullback** — a counter-move (a dip for a BUY boost / a bounce for a SELL boost) then a
  **turn** back toward the trade → enter at the turn, with the **SL placed beyond the retrace
  extreme** (below the dip low / above the bounce high) so the natural retrace can't stop it.
- **Smooth** — no pullback, but break-and-hold **confirms** the continuation (the same proven
  mechanism as the +$5 arm) → enter on confirm, fixed SL.
- **Skip** — neither within the timeout → no boost (parent runs alone).

**Rally** extends v3.4.0's `override_entry_*` (now adaptive: smooth branch + dynamic SL).
**Rescue is the new mirror** — instead of today's immediate −$10 bypass-fire, it **keeps the
losing parent**, arms at −$10, and enters SELL on a bounce-rollover (SL above the bounce high)
or a confirmed smooth drop (SL entry+$10), addressing the Jun 25 A5 −$1,330. RALLY and RESCUE
stay **architecturally separate** — separate flags, keys, and call sites; they share only the
pure helper (no shared state, no merged path).

**OFF-by-default guarantee.** With `override_entry_enabled=False` and `rescue_entry_enabled=False`
(both default), master behavior is **byte-identical** — rally = v3.4.0 OFF path, rescue = today's
immediate bypass-fire. Proven by freeze tests 114/115. New PTRACE `RESCUE_ENTRY_ARMED/SKIPPED/FIRED`
gives the rescue trial its data.

**Open numbers (trial-tunable).** Bounce depth ($6), arm timeouts (4 M5 candles), the turn-confirm
($1.5), and the smooth-confirm toggles are first guesses. **Rescue SL stays $10 / cap −$700** — the
$10→$13 question is deferred to month-end (it would move the derived cap to −$910) and pullback
entry may make $13 unnecessary.

> ⚠️ **Ship vs delete, per path.** Built before the data. If a path rarely finds a clean pullback
> or confirm, it will skip often — and the right call may be to **drop that path**, not enable it.
> Both rally and rescue are flag-OFF so every option stays open. The six chart-derived regression
> fixtures (R1–R6) encode the patterns the trial must reproduce.

### v3.5.0 "all-16" — full feature wiring (flag reference)

All 16 features are wired, each behind its own flag. **Every strategy flag defaults OFF**, so with
the strategy flags off the live behavior is **byte-identical to master** (v3.5.0 core). The owner
flips features on one at a time and measures each against the logged baseline. Utilities (8–11) and
hotfixes (15–16) default ON — they never change order logic (proven in the self-test).

| # | flag / param | default | effect | OFF → ON |
|---|---|---|---|---|
| 1 | `override_entry_enabled` | **OFF** | rally adaptive entry (arm +$20, pullback turn) | immediate override fire → arm-then-pullback |
| 2 | `rescue_entry_enabled` | **OFF** | rescue adaptive entry (arm −$10, SELL on rollover) | immediate −$10 bypass-fire → arm-then-bounce |
| 3 | `override_entry_smooth_confirm` / `rescue_entry_smooth_confirm` | ON\* | smooth break-and-hold capture | pullback-or-skip → also enter on confirm |
| 4 | `override_entry_dynamic_sl` | ON\* | SL beyond the retrace extreme | fixed $13 → beyond dip low |
| 5 | `override_entry_arm_timeout_candles` / `rescue_entry_arm_timeout_candles` | 4 | M5 candles before skip | — |
| 6 | (flag-off defaults) | — | dormant unless flipped | — |
| 7 | R1–R6 regression fixtures | — | encode real-chart patterns | self-test only |
| 8 | `util_pullback_log` | ON | per-anchor armed/pulled-back/entered/skipped → daily JSON | telemetry only |
| 9 | `util_boost_ledger` | ON | every boost event → `boost_ledger.csv` | telemetry only |
| 10 | `util_daily_report` | ON | per-anchor markdown from trades CSV (EOD) | read-only |
| 11 | `util_preflight` | ON | boot self-check (offset/anchors/flags/market) | alert-only (the offset block is the real gate) |
| 12 | `entry_confirm_candle` | **OFF** | require an M5 close in direction before fill | first-touch → confirm-candle (both paths) |
| 13 | `entry_adaptive_depth` (`atr_period`=14, `atr_mult`=1.0) | **OFF** | depth scales with ATR | fixed $13/$6 → `atr_mult`×ATR |
| 14 | `rescue_sl_wide` (`rescue_sl_wide_dollars`=13) | **OFF** | widen rescue SL $10→$13 | SL $10 / cap −$700 → SL $13 / **cap −$910** (derived, recomputed) |
| 15 | `fix_boost_telemetry` | ON | emit boost trail-advance so EXIT isn't falsely flagged | telemetry only, no P&L |
| 16 | `fix_a1_offset` | ON | wake offset never falls back to 0h (already enforced) | OFF never re-adds a 0h guess (block is fail-safe) |

\* within their parent entry flag — inert while `override_entry_enabled` / `rescue_entry_enabled` is OFF.

**Measurement plan:** run the trial with CORE (1–7) ON and strategy extras (12–14) OFF; utilities
(8–11) collect. At month-end the pullback-frequency log (8) + boost ledger (9) show whether the
override pulls back often enough to keep — and whether 12/13/14 are justified. Flip them one at a
time **after**, each measured against the logged baseline.

---

## License

This is YOUR strategy and YOUR risk. Provided as-is for educational and
backtesting purposes. Past performance does not guarantee future results. **Live
performance will be below backtest due to spread, slippage, and execution
friction.**
