# AUREON — Known Errors Ledger

Tracked defects and their status. Rogue = the A1-anchored redesign engine
(magic `20260626`); it never touches the anchor engine (magic `20260522`).

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
