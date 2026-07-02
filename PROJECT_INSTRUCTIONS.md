# AUREON — Project Instructions

**Regenerated 2026-07-02 from current HEAD.** This file is the standing brief for
working on this repo (and the text to prime a fresh chat with). It states what is
true *now* — the ledger status, the queue, and the discipline — so nobody has to
re-derive it from the commit history. When it drifts from HEAD, regenerate it;
never patch it from memory.

---

## 1. System snapshot

Multi-anchor XAUUSD straddle bot (MT5, Python) + the ROGUE A1-anchored chain
engine. Two engines, hard-isolated by magic number: anchors `20260522`, Rogue
`20260626` — neither ever closes the other's tickets.

- **Anchors (post 2026-07-02 A3 cut): A1 02:30 · A2 10:00 · A4 16:40 · A5 19:30
  broker (UTC+3).** A3_1430_Overlap was CUT on its per-anchor P&L — see §6.
  Straddle ±$5, SL $18 / TP $30, lot 0.35, No-OCO, 45-min hold, ladder, rescue +
  2-boost fleet at −$10.
- **Rogue (demo ON, funded force-OFF):** A1-anchored redesign is the live engine
  (`rogue_a1_anchor_mode=True`). Chains off the last close, enters ±$10 off the
  anchor, init SL $5, adaptive trail. Brakes: −$525 daily stop, 3-fail pause,
  10/day cap, −$13 recovery-leg cap, $30 soft lock (banks, never halts).
  **At EOD an open Rogue position is now FLATTENED (`rogue_flatten_at_eod=True`,
  default ON since 2026-07-02)** — see §6.
- **Launch chain — state it plainly: `watchdog.py` is the launcher.** It
  relaunches the bot **only on exit code 42** (the controlled feed-death
  self-restart). Any other exit — crash, clean `/stop`, clock-drift abort —
  alerts and **stops**; no auto-respawn loops. `run_aureon.bat` is a documented
  alternative with the same exit-42-only contract. Nothing else supervises.

## 2. Standing discipline (non-negotiable)

1. **Config + selftest + docs before engine logic.** Behavior changes ship
   flag-gated, DEFAULT OFF; with all strategy flags off (and `rogue_enabled`
   raw-off) the build stays **byte-identical to master** — the freeze is proven
   by selftest, not asserted in prose.
2. **The journal decides, not sims.** Per-anchor / per-feature verdicts come
   from the live demo record (the v2.9.4 rule). Persistent losers get cut;
   subtraction beats addition.
3. **Isolation is sacred.** Rogue never touches a `20260522` ticket; anchors
   never touch `20260626`. No generic close-all.
4. **Lots are never resized by plumbing.** Retry wrappers, rescue math and
   boosts use the configured lot; `10014 INVALID_VOLUME` aborts, never resizes.
5. **Every merge: full selftest green + `py_compile` clean.** Live-order steps
   may SKIP off-demo; nothing may FAIL.
6. **Decisions get date-stamped** in `ERRORS.md` (Decision Log) with the P&L
   rationale, so "why" survives the commit message.

## 3. Master multiplier (all dollar talk hangs off this)

**account $ = price-$ move × lot × 100.** At the standing lot **0.35**, $1 of
XAUUSD movement = **$35** per leg. Anchor leg worst case = 18 × 35 = **−$630**;
Rogue init-SL strike = 5 × 35 = **−$175** (hence −$525 = exactly 3 strikes);
rescue-leg cap = 13 × 35 = **−$455**. Config knobs are quoted in price-$; when
the lot changes, every derived account-$ figure scales linearly — recompute
before quoting any dollar number.

## 4. Ledger status — CLOSED

Closed and verified (details + self-test numbers in `ERRORS.md`):

- **E-1** — stale "legacy monster is live" config ghost (config truth restored).
- **E-2** — Rogue closes never fed the governor (close-detection + booking wired).
- **E-3** — dormant after one close → **chain re-anchor at the exit price**.
- **E-5** — daily loss stop −$150 → −$525 so the 3-fail pause can engage first.
- **E-6** — boost rides with parent (RALLY-only, flag-gated).
- **E-8, E-11** — closed (E-11: "BREAK no fire" log spam throttled to state
  transitions).
- **E-13** — order rc-check / brick: shared `place_with_retry`, open state only
  on `rc=10009` + real ticket.
- **E-14** — unresolved close P&L no longer counted as a fail (sentinel).
- **E-15** — Rogue entries gated under kill-switch + EOD; kill flatten closes
  the Rogue ticket.
- **E-16** — P1 state snapshot + same-day boot recovery (restart dormancy dead).
- **Chain LIVE-PROVEN 2026-07-02:** 4 trades, 3 chain re-anchors, day
  **+$72.10** — the E-3 fix held in production.

Still open on the ledger: **E-12** is EXTENDED (feed escalation ladder shipped;
watching), and **E-4** is answered by the 2026-07-02 EOD-flatten default flip.

## 5. Current queue

- **P3 — Rogue displacement/chop filter + chase cap.** Stop entering on
  chop-grade $10 drifts (displacement/impulse quality gate) and cap how far a
  chain entry may chase away from its anchor.
- **P4 — dead-code verdicts.** Rule on and remove-or-wire: `a1_soft_lock_met`,
  `a1_rescue_cap`, and the other confirmed-dead flags (the flag-OFF experiments
  that will never flip). Subtraction pass, selftest-proven.
- **P5 — watch list (data before action):** rung-2 of the ladder, TSTOP value,
  F-B, `rally_pullback_*` (still OFF), and the sl_dist 18 → 14 question. Journal
  evidence first; no config motion until a month-end read.

## 6. Config decisions in effect (2026-07-02, this branch)

Both are **decisions, not bugs** — dated in the `ERRORS.md` Decision Log
(D-1/D-2) with the numbers:

1. **A3 CUT.** `A3_1430_Overlap` removed from `cfg.anchors`. June **−$2,255
   (PF 0.68)**, July **−$385** — both months negative; the 17:00-IST retime
   didn't fix it. Stale `DEFER_WAIT_BY_ANCHOR` key kept deliberately (harmless,
   commented) for a possible restore.
2. **`rogue_flatten_at_eod` False → True.** Overnight/weekend gap risk; E-15
   already blocks post-EOD *entries*, this closes the existing-position side.

## 7. Start-of-chat prompt shape

Open a work chat on this repo like this (keep this shape):

```
AUREON <phase> BRANCH — <one-line goal> (repo SVSKHD/A02-PR, branch off current master)
Branch: <branch-name>

Read README.md + ERRORS.md first. <scope line, e.g. "Config + selftest + docs only — no engine logic.">

1. <numbered, file-anchored tasks with line refs>
...
N. Full selftest green; py_compile clean.
```

Rules of the shape: name the branch up front; state the scope boundary
explicitly (what must NOT change); anchor each task to files/lines; carry the
P&L or incident rationale inside the task so the commit can quote it; and end
with the merge gate (selftest + compile). Decisions made mid-chat get written
back to `ERRORS.md` (Decision Log) and this file gets regenerated when reality
moves.
