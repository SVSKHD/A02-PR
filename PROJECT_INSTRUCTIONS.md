# AUREON — Project Instructions

**Regenerated 2026-07-03 from current HEAD.** This file is the standing brief for
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
- **E-18** — trapped-leg STOP-THROUGH spam: a losing leg with no armed lock
  now computes NO stop advance; the warning is throttled to once/episode.
- **Chain LIVE-PROVEN 2026-07-02:** 4 trades, 3 chain re-anchors, day
  **+$72.10** — the E-3 fix held in production.

Still open on the ledger: **E-12** is EXTENDED (feed escalation ladder shipped;
watching), and **E-4** is answered by the 2026-07-02 EOD-flatten default flip.

**OPEN #1 CLOSED (2026-07-03):** the P3 (E-17) "slow grind survives the gates"
residual — Rogue day **+$918.05**, chain cooldown fired at **06:50:39**, and
the post-cooldown re-entry was a **win**. P3 is now **live-proven**, not just
selftest-proven; the displacement-quality follow-on stays a candidate only if
a FUTURE slow-chop day actually bleeds through, not a standing worry.

**OPEN #2 CLOSED (2026-07-03):** the W-7 / W-4 Watch Ledger items are now
DECIDED, not watched — see D-4 (`parent_established_dollars` 20→12, override
re-evaluates continuously, no code-level latch) and D-5 (F-B flipped LIVE) in
`ERRORS.md`.

## 5. Current queue

- **P3 — Rogue chop/chase gates — SHIPPED 2026-07-02, LIVE-PROVEN 2026-07-03**
  (branch `claude/p3-rogue-chop-chase`, E-17): chase cap $10–$20 entry band +
  chain cooldown 300s + $6 fresh displacement on chained anchors. The known
  slow-grind residual closed itself out on 2026-07-03 (see OPEN #1 above) — no
  further lever pulled; a displacement-QUALITY filter stays a candidate only if
  future demo days show slow-chop entries surviving these gates.
- **P4 — dead-code verdicts — SHIPPED 2026-07-03** (branch
  `claude/p4-boosts-trapped-leg-gbn54a`): deleted `a1_soft_lock_met`,
  `a1_rescue_cap` (`rogue.py`), `lock_confirm` (`config.py`, zero readers),
  `override_entry_first_touch` (`config.py`, superseded by v3.5.0's shared
  pullback_entry.step), `rogue_reuse_rally`/`rogue_reuse_rescue` (`config.py` +
  the `aureon_validator._EXPECTED_FLAGS` whitelist), and
  `rally.override_pullback_step` (the v3.4.0 state machine, superseded by the
  same shared helper). Subtraction pass, selftest-proven (full suite green).
- **P5 — watch list (data before action):** rung-2 of the ladder, TSTOP value,
  `rally_pullback_*` (still OFF), and the sl_dist 18 → 14 question. F-B moved
  off this list 2026-07-03 (D-5, now live — see §4). Journal evidence first; no
  further config motion until a month-end read.
- **P6 — daily P&L report — SHIPPED 2026-07-04** (branch
  `claude/daily-pnl-report`, new `pnl_report.py`): automates the CSV analysis
  that drove the A3 cut. Per-anchor net/PF/win%/whipsaw + original-vs-boost-
  vs-F-B P&L split + a Rogue section + a month-to-date cut/keep roll-up, from
  MT5 history deals (never from live trading state) — markdown to
  `run/reports/daily_<date>.md`, a stable-schema CSV row appended to
  `run/reports/pnl_ledger.csv`, and a Discord card, once per broker day at EOD
  (`cfg.util_daily_pnl_report`, default ON) or on demand via
  `python bot.py dailyreport [YYYY-MM-DD|YYYY-MM]`. READ-ONLY: no order flow,
  no `shadow_positions`/governor reads. **Flagged, not guessed:** the boost
  order comment (`AUR_{anchor}_{side}_B{n}`) is IDENTICAL for a RALLY pyramid,
  a RESCUE hedge, and the F-B trapped-late-rescue hedge — `boosts.py`'s
  `kind`/`event_type` is never written to the broker. The report joins boost
  tickets against `rescue_events.csv`'s `event_type` column to split them; a
  ticket with no matching row (fleet event not finalized yet) is reported as
  `BOOST_UNCLASSIFIED`, never guessed as RALLY/RESCUE. The minimal fix, if this
  ever matters at scale, is a 4th comment character (`_B1R`/`_B1S`/`_B1F`) —
  proposed, not implemented (out of scope for a read-only reporting branch).
  Also added ONE missing `log.info` mirror in `rogue.py` (`detect_close`) for
  the CHAIN re-anchor / CLOSE-brake lines, which were Discord/Telegram-only
  before — pure additive logging, makes "chain re-anchors" and "brake events"
  greppable from `aureon.log` like their CHASE-REJECT/CHAIN-COOLDOWN siblings
  already were.

## 6. Config decisions in effect (2026-07-03, this branch)

All are **decisions, not bugs** — dated in the `ERRORS.md` Decision Log
(D-1..D-5) with the numbers:

1. **A3 CUT.** `A3_1430_Overlap` removed from `cfg.anchors`. June **−$2,255
   (PF 0.68)**, July **−$385** — both months negative; the 17:00-IST retime
   didn't fix it. Stale `DEFER_WAIT_BY_ANCHOR` key kept deliberately (harmless,
   commented) for a possible restore.
2. **`rogue_flatten_at_eod` False → True.** Overnight/weekend gap risk; E-15
   already blocks post-EOD *entries*, this closes the existing-position side.
3. **`parent_established_dollars` 20 → 12 (D-4).** W-7: two forfeited
   continuations in 2 days (~$350, ~$2,000+) sat under the old $20 line long
   enough to run away untouched. Source-verified: no code-level latch — the
   override already re-evaluates every tick from the parent's live max_fav.
4. **`trapped_late_rescue_enabled` False → True (D-5, F-B live).** Three
   trapped-leg events in 2 days, all unhedged naked. Verified F-B already
   structurally bypasses break-and-hold (fires+continues before the gate is
   ever reached, `fills.py` ~604-620) — no gate change needed, only the flip.

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
