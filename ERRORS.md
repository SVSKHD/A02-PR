# AUREON — Known Errors Ledger

Tracked defects and their status. Rogue = the A1-anchored redesign engine
(magic `20260626`); it never touches the anchor engine (magic `20260522`).

Config **decisions** (not bugs) are date-stamped in the Decision Log at the
bottom so the "why" survives the commit message.

---

## E-3 — Rogue goes dormant after ONE close (no chain re-anchor) — **FIXED**

**Status:** FIXED — 2026-07-01 — branch `claude/rogue-clear-open-on-close-hkfrn7`

**Symptom (live, 2026-07-01):** after a single Rogue close the engine stopped
taking new entries for the rest of the day (dormant).
LIVE PROOF: `MANUAL SEED 3984 -> ENTER SELL 3974.01 -> CLOSE +$206.50
(trailing-profit close) -> NO re-anchor, dormant.`

**Root cause:** the A1-redesign chain target `st['a1_last_close']` was never set
to the exit on a TRAILING/TP close. `detect_close()` (`rogue.py`) already cleared
`st['open']` for every broker-side close, but in the `_drive_a1()` continuation
path `st['a1_last_close']` was only reset to `None` while *still riding*; the
reversal path set it to the entry, but the trailing/TP path set nothing. After a
manual seed (no A1 event, so `_a1_anchor_price()` returns `None`), the next tick
computed `a1_seed_anchor(None, None) -> None` and returned early — Rogue had no
level to anchor from, so it never hunted the next $10 move.

**Fix:** on ANY non-reversal Rogue close, the moment the close is detected and
`day_pnl` is booked, `detect_close()` now re-anchors the A1 redesign at the EXIT
price (`st['a1_last_close'] = exit`) and logs
`[ROGUE] 🦏 CHAIN re-anchor @ <exit> -> hunting $10 both dirs`. The engine then
re-anchors there and a fresh $10 move (either direction) fires the next entry.
A reversal-recovery leg keeps its own entry-based anchor (`a1_reverted`); the
legacy monster path (flag OFF) is untouched. The next entry is still gated by the
existing brakes (`can_enter`: -$525 daily loss stop, 10/day cap, consecutive-fail
pause) and the $30 soft lock banks without halting. Rogue-only (magic
`20260626`); the anchor engine (`20260522`) is never touched. `st['open']` clears
so `rogue_patternlog.observe()` records the close (F-A exit data not starved).

**Files:** `rogue.py` (`detect_close` + new read-only `_rogue_close_price`
helper), `selftest.py` (step 189 `rogue e3 chain`).

**Live verification:** seed -> enter -> trailing close re-anchored at the exit and
a subsequent $10 move fired a SECOND entry (chain restored); the same holds for an
init-SL close; the -$525 brake still blocks re-entry after a catastrophic loss;
the anchor `20260522` ticket is never closed. Self-test 189 (`rogue e3 chain`)
PASS, and rogue steps 171-176 / 185 / 187 / 188 remain PASS.

---

## E-12 — Feed-death: bot went blind, never escalated — **EXTENDED (ladder)**

**Status:** EXTENDED — P1 branch `claude/p1-integrity-and-feed` — Fix 4.

**Symptom (live, 2026-06-30):** the XAUUSD subscription dropped for ~4h; the probe raised
"symbol not subscribed" ~13,833 times. The original E-12 fix added Level-1 re-subscribe +
one throttled FEED DOWN alert, but the live log still showed the re-subscribe counter
running **past its cap** ("attempt 6/5"), and re-subscribe was the ONLY recovery — a stuck
subscription stayed blind indefinitely.

**Fix (escalation ladder in `feed_watchdog.FeedWatchdog` + `live_trader._feed_watchdog_fail`):**
- **Level 1 (fixed):** the re-subscribe attempt counter now **STOPS at `feed_recover_max_tries`**
  (no more "6/5") and fires exactly ONE FEED DOWN alert at the cap.
- **Level 2 (new):** once re-subscribe is exhausted OR the feed has been blind past
  `feed_reinit_blind_min` (3 min), a full **in-process MT5 reinit** runs
  (`MT5Adapter.reinit`: `shutdown() → initialize() → symbol_select → verify a fresh tick
  within 60s`), up to `feed_reinit_max_tries` (2). Posts `FEED REINIT attempt N`.
- **Level 3 (new):** if both reinits fail, a **controlled self-restart** — persist state
  (E-16), Discord `SELF-RESTART: feed dead`, `sys.exit(42)`. Gated by
  `feed_selfrestart_enabled` (default ON) and a market-closed clock guard so it NEVER
  self-restarts on a weekend.

**Launch chain (P1 follow-up):** `watchdog.py` is the active supervised launcher and now
owns the exit-code relaunch policy — it **relaunches the bot ONLY on exit code 42** (the
controlled self-restart) and, on **any other exit code** (crash / clean `/stop` /
clock-drift abort exit 0), **alerts Discord and STOPS** rather than relaunching. This fixes
the prior bug where the supervisor relaunched on every exit (crash + code-0 respawn), which
could crash-loop and re-place orders on each boot. A runaway 42-loop
(`MAX_CONSECUTIVE_SELFRESTARTS`) also stops for a human; heartbeat-hung and manual
`/restart` remain separate controlled restarts. The E-16 same-day recovery means a 42
relaunch never re-places orders. `run_aureon.bat` is a documented **alternative** launcher
(same exit-42-only contract, direct `bot.py` launch) for setups not using the watchdog.

**Files:** `feed_watchdog.py`, `mt5_adapter.py` (`reinit`), `live_trader.py`
(`_feed_reinit` / `_feed_self_restart` / `_weekend_by_clock`), `watchdog.py`
(`relaunch_policy` + supervisor exit block), `config.py` (`feed_reinit_blind_min` /
`feed_reinit_max_tries` / `feed_selfrestart_enabled`), `run_aureon.bat`, `TASK_SCHEDULER.md`.
**Self-test:** 168 (counter capped + escalation), 193 (reinit fresh/stale + ladder), 195
(watchdog relaunches only on 42, stops otherwise). `feed_watchdog_enabled=False` stays
byte-identical.

---

## E-13 — No order rc-check / brick on a failed Rogue entry — **FIXED**

**Status:** FIXED — P1 branch `claude/p1-integrity-and-feed` — Fix 1.

**Symptom:** order sends were fire-and-forget. Rogue's market entry set `st['open']`
(and consumed a governor slot) regardless of the broker retcode, so a rejected send left a
**phantom open** the engine then "managed" forever — a brick — while a real fill was never
placed. There was no shared retry/abort policy across the two engines.

**Fix:** new SHARED `MT5Adapter.place_with_retry()` (+ `classify_retcode`) used by both the
anchor stop orders and Rogue's market entries. RETRYABLE (≤3 attempts, 0.5/1/2s backoff,
tick refreshed each try): `10004`/`10015`/`10021` (refresh), `10016` (recompute SL/TP vs
`stops_level`), `10008`-class / `None` / `-1` (plain). NEVER-RETRY (abort + ONE alert with
retcode + broker comment + params): `10014` **INVALID_VOLUME — the lot is NEVER resized**,
`10019`/`10018`/`10017`, and any unrecognized retcode. **Brick fix:** Rogue's LIVE entry
sets `st['open']` + consumes a slot **only on `rc==10009` with a real ticket**; on final
failure the state stays clean (no phantom open), NO slot is consumed, an abort alert has
fired, and the engine stays alive for the next signal. HARD RULE: the wrapper never touches
lot size.

**Files:** `mt5_adapter.py` (`place_with_retry` / `classify_retcode` / `_alert_order_abort`
/ `reinit`), `rogue.py` (`_place_rogue_entry` / `_mark_rogue_open` / `_rogue_recompute_sl`).
**Self-test:** 190 (classification + retry/abort + brick).

---

## E-14 — $0-P&L close booked as an init-SL fail — **FIXED**

**Status:** FIXED — P1 branch `claude/p1-integrity-and-feed` — Fix 2.

**Symptom:** when a Rogue close deal had not yet landed in history, `_rogue_close_pnl`
returned `None`; the code booked `pnl=0` and — because `0 <= 0` — counted it as an init-SL
**fail**, wrongly advancing `consec_fails` toward the 3-fail pause on a close that may have
been a winner.

**Fix:** `_resolve_close_pnl` retries the history fetch (3 tries, 1s apart) before booking.
If still unresolved, `detect_close` books `pnl=0` but passes `was_fail=None` to
`record_close` (new sentinel) so the fail streak is **left untouched** (neither incremented
nor reset), logs `WARN pnl-unresolved ticket #X`, and posts a throttled Discord warning. An
unresolvable P&L can no longer trip the fail-pause brake.

**Files:** `rogue.py` (`_resolve_close_pnl`, `detect_close`, `record_close`). **Self-test:**
191.

---

## E-15 — Rogue took new entries above the kill-switch / EOD gates — **FIXED**

**Status:** FIXED — P1 branch `claude/p1-integrity-and-feed` — Fix 3.

**Symptom:** `rogue.drive()` ran at tick step 3c — ABOVE the kill-switch lock gate and the
EOD check — so a new Rogue entry could open on a kill-locked day or after EOD, and the
kill-switch flatten never closed an open Rogue ticket (Rogue rides its own magic
`20260626`).

**Fix:** the entry-taking `drive()` now runs **below** both gates (both `return` above it),
so a NEW entry can only open on a live, non-killed, pre-EOD tick. `_flatten_all` (kill /
manual flatten, NOT EOD) now closes any open Rogue ticket via `rogue.force_close_open`. An
EXISTING open Rogue position is still trail-managed post-EOD when `rogue_flatten_at_eod` is
False (a `drive(allow_new_entries=False)` call in the EOD branch) — the owner's ride flag is
preserved — but NEW entries (including reversal-recovery legs) are hard-blocked after EOD.

**Files:** `live_trader.py` (`_tick`), `risk.py` (`_flatten_all`), `rogue.py` (`drive` /
`_drive_a1` gain `allow_new_entries`; new `force_close_open`). **Self-test:** 192.

---

## E-16 — No state persistence / boot recovery (restart dormancy) — **FIXED**

**Status:** FIXED — P1 branch `claude/p1-integrity-and-feed` — Fix 5 (supersedes
restart-dormancy).

**Symptom:** the in-memory Rogue state (`_rogue`: governors + chain anchor + open ticket)
was rebuilt fresh on every restart, so a mid-day restart lost the day's Rogue governors and
chain anchor (and `_a1_anchor_price` read state keys — `a1_anchor_price` etc. — that were
never written). Anchors and open positions were not explicitly reconciled from a P1 snapshot.

**Fix:** new `p1_state.py` persists a compact snapshot to `run/state.json` on every change
(hooked into `state._save_state` + Rogue mutations, and forced before the Level-3 exit):
`trading_date`, `processed_anchors_today`, per-anchor markers, Rogue anchor / `a1_last_close`
/ open ticket / `day_pnl` / `consec_fails` / `reanchor_count` / latches, and boost trail
peaks. On a SAME trading-day boot (`recover_on_boot`, one-shot after the first new-day
reset): restore the Rogue governors + chain anchor, ADOPT an open Rogue position only if it
is still open at the broker, skip anchors already placed today, and log `RESTART-RECOVERY OK
…`. A NEW trading day ignores the stale file (fresh start).

**Files:** `p1_state.py` (new), `live_trader.py` (`_tick` one-shot recovery), `state.py`
(`_save_state` mirror), `rogue.py` (`_persist_state` hooks). **Self-test:** 194.

---

## E-17 — Rogue chop/chase exposure (chain re-anchor buys exhausted moves) — **FIXED**

**Status:** FIXED — 2026-07-02 — P3 branch `claude/p3-rogue-chop-chase`.

**Symptom (live 2026-07-02 + audit):** Rogue's design intent is MONSTER-CATCHER — fire on
real displacement, stay out of noise — but the A1-chain had no discipline on WHERE or WHEN
it re-entered. Two exposures:
1. **Chase (live trade 3):** after a close the chain re-anchored at 4058 MID-TREND; the
   +$10 confirm then fired at **BUY 4068.07 — $24 past the ORIGINAL anchor (4044)** — buying
   the exhausted extension. Init-SL'd for **−$178.50** (day finished +$72.10; without trade 3
   it is +$250.60).
2. **Chop grind (audit walk):** in a plain ±$12 ranging day, every $10 drift is a "confirm";
   each init-SL close re-anchors INTO the range and the next $10 wiggle re-enters — a
   3-strike grind to the **−$525** daily loss stop in ~12 minutes, with no requirement that
   any move be fresh or catchable.

**Fix (two protective gates, BOTH ON by default, each independently toggleable via 0;
Rogue A1-mode only — the legacy monster path and the anchor engine are untouched):**
- **GATE 1 — CHASE CAP** (`rogue_chase_cap_dollars = 20.0`): the A1-mode entry band is now
  `$10 <= |move off the ACTIVE anchor| <= $20` (`a1_entry_decision`). Beyond the cap the
  move is exhausted — no entry, NO governor slot consumed, one throttled
  `CHASE-REJECT` log per episode. NO latch: the anchor stays planted and the gate
  re-evaluates per tick, so a pullback inside the band enters normally. Mirrors the anchor
  engine's catchable-zone cap on in-flight breakout recovery (`anchors.py` ~:807). Applies
  to EVERY A1-mode entry including the reversal-recovery leg.
- **GATE 2 — CHAIN COOLDOWN + DISPLACEMENT** (`rogue_chain_cooldown_sec = 300`,
  `rogue_chain_min_displacement = 6.0`): an entry off a **CHAINED** anchor (a re-anchor
  planted by `detect_close` after a close) requires BOTH the cooldown elapsed since the
  close AND ≥ $6 of movement off the re-anchor price, in the entry direction, observed at
  some point since planting — the $10 confirm must build from FRESH movement, not the tail
  of the move that just closed. Blocked entries log one throttled `CHAIN-COOLDOWN Xs
  remaining` / `CHAIN-DISPLACEMENT` per re-anchor and consume no slot. **Exemptions:** the
  A1 morning seed, a manual `rogueseed`, and the reversal-recovery leg (time-critical by
  design; still chase-capped) are NOT chained — the first trade of the day is unaffected.
  Chain meta (`chain_time`/`chain_anchor`/displacement record) persists in the P1 snapshot
  so a same-day restart cannot bypass the cooldown.

**Replay evidence (real engine driven through the recorded/audit scenarios):** live
2026-07-02 replay — trades 1, 2, 4 fire identically, trade 3 is rejected by
`CHAIN-COOLDOWN (140s remaining)` and the extension dies before the cooldown expires; day
recomputed **+$72.10 → +$250.60**. Audit chop walk (fast, $10 leg / 2 min): the old path's
3-strike −$525 grind is broken (strike 1 only; 9 cooldown blocks). Monster day (one-way
$40): entries, trail closes, chain re-anchor and the post-cooldown second leg are
**byte-identical** with gates on. **Known residual (stated, not hidden):** a SLOW grind
(legs ≥ the 5-min cooldown) passes both gates — each move genuinely is "fresh" — and still
bottoms out at the existing −$525 loss stop / 3-fail pause. A displacement-QUALITY filter
(thrust, not just distance) remains the P3 follow-on candidate if the demo shows slow-chop
entries surviving these gates.

**Files:** `rogue.py` (`a1_entry_decision` band, `chase_rejected`, `chain_entry_allowed`,
`_drive_a1` gate wiring, `detect_close` chain stamp, `manual_seed` exemption, throttled
logs, `_epoch` seam), `config.py` (3 new keys), `p1_state.py` (chain meta persisted).
**Self-test:** 196 (chase band + re-allow + no slot), 197 (cooldown block→allow), 198
(displacement, incl. spike-records-then-pullback-enters), 199 (reversal exempt from
cooldown, still capped), 200 (A1 seed + rogueseed exempt), 201 (all knobs 0 = old behavior,
defaults pinned ON); 189 re-verified with gates ON (timing warped, assertions unchanged).

---

## Watch Ledger — patterns under observation (NOT confirmed bugs, NO lever pulled)

### W-7 — break_and_hold false negative on a real continuation — **WATCH** — opened 2026-07-02

**Instance (2026-07-02, A4 SELL @edge 4131.02):** the break-and-hold gate
classified the break **reversed** (4 M5 candles) while price continued **+$17.7**
in the break direction — rally boosts stayed blocked the whole way and an
estimated **~$300–400 was forfeited**. The parent leg was only **+$10–16**
favorable during the window, just UNDER the $20 `parent_established_dollars`
CASE 2 override, so the override never engaged.

**Candidate lever (if repeated):** lower the override threshold to **~$12–15**.
**Status:** NEEDS more instances before ANY change — the gate exists because of
the −$701 fake-break loss and is not loosened on a single counter-example.
(Same-episode side effect — 60+ identical PTRACE `BREAK_FAILED` lines, one per
second — fixed by the 2026-07-02 spam-throttle hotfix: once per
(anchor, side, break_level) episode with a `suppressed_repeats` count. Logging
only; the gate decision is untouched.)

### W-2 — evidence append — 2026-07-02

**A4 BUY:** no-hold shadow **+$36.75** vs actual **−$630** (held 33.9m).

### W-4 — evidence append — 2026-07-02

**A4 BUY:** rode a **$27 collapse naked** (rescue suppressed by design).

---

## Decision Log — dated config decisions (NOT bugs)

### D-1 — A3 anchor CUT — 2026-07-02

**Decision:** `A3_1430_Overlap` removed from `cfg.anchors` (`config.py`). Branch
`claude/p2-anchor-cut-eod-vo0an6` (P2).

**Rationale (per-anchor P&L, journal record):** June −$2,255 with PF 0.68; July
−$385 — both months negative. The v3.3.6 retime (16:20 → 17:00 IST) did not fix
it. This executes the v2.9.4 rule: each anchor is judged on its own live record
and persistent losers get cut based on the journal, not sims.

**Scope:** schedule-list change ONLY — A1/A2/A4/A5 and every trade-logic /
sizing / straddle / boost / rescue knob unchanged; no engine logic touched.
`DEFER_WAIT_BY_ANCHOR['A3_1430_Overlap']` (`live_trader.py`) is deliberately
left as a stale, harmless lookup-only key (comment marks it) for a possible
restore. Self-test 100 now asserts the cut; 103 validates the anchor list
dynamically (well-formed labels, valid times, no duplicates) instead of
hard-asserting five anchors.

**Restore path:** re-add `("A3_1430_Overlap", 14, 30)` in `config.py`.

### D-2 — `rogue_flatten_at_eod` default False → True — 2026-07-02

**Decision:** the E-4 flag now defaults ON (`config.py`): at EOD an OPEN Rogue
position is flattened instead of riding overnight on its own SL/TP. Same branch
as D-1.

**Rationale:** overnight/weekend gap risk — a gap can jump straight past the
resting SL, so the "ride" exposure is unbounded in practice. E-15's gating
already hard-blocks NEW Rogue entries post-EOD; this closes the
existing-position side of the same hole. Rogue-scoped as before (closes ONLY
the Rogue `20260626` ticket, never an anchor `20260522` ticket); the kill-switch
path (`force_close_open`) is unaffected.

**Scope:** default flip + comment only; `rogue.eod_flatten` logic unchanged.
Self-test 175 asserts the new default ON and still proves both flag states
(OFF now forced explicitly). Set `rogue_flatten_at_eod=False` to restore the
overnight ride.
