# MERGE_GATE — Monday live validation for PR #3 (AUREON v3.0.0)

PR #3 merges to `master` **only after** Monday's live demo run confirms all 5
criteria below. Until then the PR stays open. On Monday, paste into this session:
the **Telegram trade closes** for the day, the **journal CSV** (`run/journal/trades_2026-06.csv`),
and the **resume + EOD** Telegram lines — and I'll check each criterion against what
actually happened.

> Do not merge autonomously — the **human merges** after this gate passes. If any
> criterion fails, keep the PR open; I diagnose from the pasted logs, propose a fix
> commit on this branch, and we re-validate.

## The 5 merge criteria (all must be TRUE)

### 1 — Monday wake (commit 4 + cold-start fix)
- Bot **auto-resumed** from weekend sleep (no manual restart).
- Posted **`📈 Market open — resuming. Week starting. Broker time offset re-detected: +3h …`**.
- Offset is **+3h, NOT 0h** (the Jun-8 A1-miss bug). A `+0h` / `DETECT FAILED` ⇒ FAIL.
- **A1 placed at 02:00 broker** by the normal anchor path.
- Evidence: the resume Telegram line + the A1 placement/fill lines.

### 2 — No regression vs 2.9.8 (commit 2 behavior-frozen)
- Every anchor's legs show the **correct exit labels** (BE / LOCK4 / TIER / Trail /
  SL / TP), **correct held-times**, and **correct P&L** — i.e. v3.0.0 trades exactly
  as 2.9.8 would have.
- No mislabeled exits, no false `FREEZE BREACH`, no `Trail` that should be a ladder tier.
- Evidence: journal CSV `exit_reason` + `realized_pnl_usd` + `entry/exit_time_ist`
  columns look like a normal 2.9.8 day; Telegram closes match the journal.

### 3 — Rescue fix (commit 1)
- **If a 2nd-leg fill occurs where the twin had already closed** → it is tagged
  **`role=normal`** with **NO ⚡ boost** lines. (This is the bug being fixed.)
- **If a genuine rescue occurs** (twin still open at the 2nd fill) → tagged
  **`role=rescue`** and the boost path emits its full diagnostics for every boost
  (`… attempting BOOST` then a ✅/❌ line with rc/ticket — never silent).
- Evidence: journal `role` column + any `⚡ SL-RESCUE BOOST` / `attempting BOOST` /
  `✅⚡ … FILLED` / `❌ … rejected rc=…` Telegram lines.
- Note: if **no** 2nd-leg fill happens Monday, this criterion is **N/A-pass** (the
  guard simply never triggers) — but the boost diagnostics remain available.

### 4 — Firebase (commit 3)
- Monday EOD writes **exactly one** `aureon_forex/{2026-06-15}` document
  (schema_version 2, the day's anchors + trades + total_pnl).
- The Sunday **weekly-reconcile ran at startup without blocking** (a `📒 Firebase
  weekly reconcile backfilled N day(s).` line, or silence if nothing to backfill —
  either is fine; what matters is startup was not blocked).
- A Firebase outage must NOT have blocked the flatten or trading (fail-safe).
- Evidence: the EOD Telegram/journal confirm + the Firestore doc (or, if the key
  isn't installed yet, the `… client unavailable` log line — also an acceptable
  fail-safe pass; the wiring is correct, only the key is pending).

### 5 — No crash / no silent state loss
- The process ran the **full day** through EOD with **no unhandled exception**.
- `state.json` saved throughout (daily_pnl, anchors, shadows persisted); a mid-day
  restart (if any) rehydrated cleanly.
- Evidence: no tracebacks in the log; EOD daily summary posted; `state.json`
  mtime advanced through the day.

## Decision
- **All 5 TRUE → human merges PR #3 to `master`.** (Squash or merge-commit per your
  preference; the 4 commits are individually meaningful, so a merge-commit preserves
  the fix/split/firebase/weekend history.)
- **Any FALSE → keep PR open.** Paste the failing evidence; I diagnose, push a fix
  commit to this branch, and we re-run the relevant criterion (next session or next
  trading day).

## Rollback trigger (independent of merge)
If Monday goes wrong **live** (bad fills, wrong exits, crash loop), follow
`DEPLOY_RUNBOOK.md §6` immediately — restore the 2.9.8 backup zip and relaunch.
Rolling back the deploy and keeping the PR open are independent: fix on the branch,
re-deploy when green.

## Addendum — weekend `status` stats (separate PR: `claude/weekend-status-stats`)

A follow-up PR (independent of the 5 criteria above) makes the `status` command
answer during weekend/holiday deep-sleep. To validate over a weekend/holiday:
- Send `status` while the bot is in the `💤 Weekend …` sleep. It must reply with
  the 💤 sleeping layout (NOT `No status available`): last trading day per-anchor
  P&L, day total, and week-to-date per-day totals + week total — read from the
  local `run/journal/trades_<YYYY-MM>.csv` (no Firebase dependency).
- `run/status.json` must keep refreshing while asleep (mtime advances ~every 30s)
  and carry `"sleeping": true` plus a `weekend_stats` block.
- If `trades_<month>.csv` is missing/empty/malformed, the reply still shows the
  💤 header + "Stats unavailable" — it must never error.
Version stays 3.0.0 (a history line was added). No trading-behavior change.
