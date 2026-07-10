# AUREON — Known Errors Ledger

**Status:** version `v3.8.3` · selftest `293` steps · base master `e059297`
(pre-merge of the `claude/e23-and-ledger` branch) · last brought current
`2026-07-09`. Engines: Anchors (magic `20260522`), Rogue (`20260626`), Fetcher
(`20260707`). Realized-P&L truth = MT5 deal history via `pnl_source.magic_day_net`
(see GROUND TRUTH RULE at the bottom).

Tracked defects and their status. Rogue = the A1-anchored redesign engine
(magic `20260626`); it never touches the anchor engine (magic `20260522`).

Config **decisions** (not bugs) are date-stamped in the Decision Log at the
bottom so the "why" survives the commit message.

**Ledger was stale at E-3 (2026-07-01) for eight days across two silent safety
failures (E-22, E-23) — this pass brings it current.** The consolidated
DEVIATION LOG / OPEN / FIXED / WATCH summary directly below the detailed error
entries is the fast index; the per-defect write-ups (E-3 … E-22) remain in full.

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

## E-18 — STOP-THROUGH spam on a trapped leg with no armed lock — **FIXED**

**Status:** FIXED — 2026-07-03 — P4 branch `claude/p4-boosts-trapped-leg-gbn54a`.

**Symptom (live, 2026-07-03 05:55–06:21, 27x/~26min):** a trapped A1 SELL, ~$14
underwater (software computed stop 4123.10 vs bid ~4137), fired the
`⛔ STOP-THROUGH re-armed` warning once per M1 bar for the full episode. The
guard **correctly refused** to send the invalid (through-market) stop to the
broker each time — but something kept **recomputing an advancing/impossible
stop** for a deep-underwater leg once per minute.

**Root cause:** `trails._manage_trails_on_bar_close`'s STOP-THROUGH re-arm
(v3.3.0, Part 2 #3/#4) treated EVERY through-market stop identically — it never
distinguished a genuinely armed profit lock (tier 2/3, worth protecting with a
market-chasing correction) from a leg parked at (or behind) its resting SL with
NO meaningfully armed lock (`strategy.lock_level_for(pos, cfg) == 0` — this
covers both "never armed" and "armed only at exact breakeven", per the v2.9
"role-aware exits ... no small locks" design intent). For the latter, there is
nothing above the resting SL worth protecting, yet the old code still computed
a fresh market-pegged `corrected` value and re-armed it EVERY bar the position
stayed adverse, producing the recurring warning (the guard blocked the SEND
every time, but the RECOMPUTE never stopped).

**Fix:** `trails.py` (`_manage_trails_on_bar_close`, the STOP-THROUGH block)
now branches on `lock_level_for(pos, cfg)`: `== 0` (no armed lock) → **NO stop
advance at all** — `current_sl`/`shadow['current_sl']` are left exactly as they
were, nothing is sent to the broker; `>= 1` (a genuine tier-2/3 profit lock) →
the existing re-arm-near-market correction is unchanged. Both branches now
throttle their warning + PTRACE `stop_through_rearm` line to **once per
episode** (`shadow['_stopthru_episode_warned']`, cleared the moment `_through`
goes False again) instead of the old 1-per-60s rate limit, which still spammed
a slow multi-minute through-episode.

**Files:** `trails.py` (`_manage_trails_on_bar_close`). **Self-test:** 204
(`e18 no-lock no-adv` — drives the REAL method across 3 consecutive bar-closes;
asserts no stop advance, `modify_position_sl` never called, exactly one warning
+ one PTRACE line across all 3 bars).

---

## E-19 — boot with an undetected offset exits clean instead of surviving to market open — **FIXED**

**Status:** FIXED — 2026-07-04 — weekend branch `claude/weekend-report-boot-friday`.

**Symptom (live, 2026-07-03 23:02):** the bot booted into a dead/quiet Friday-
night feed; the offset detector's Tier 2 stale-consistency check REJECTed (no
usable tick to confirm the constant +3h broker offset against), so
`tick_time_offset_hours` stayed `None`; `mt5_adapter.py` logged "Proceeding;
will re-detect on market open" (by design, not a crash) — but the process then
exited with code **0** about 4 seconds after `LiveTrader` init. `watchdog.py`
only relaunches on exit code 42 (the controlled feed-death self-restart), so a
clean exit-0 during a boot-time closed-market condition left the bot down for
the rest of the weekend with no alert loop to bring it back.

**Root cause:** `MT5Adapter.server_time_utc()` decodes `tick.time` using
`self.tick_time_offset_hours or 0` — a deliberate fallback for the coarse
market-closed probe, but it means that while the offset is genuinely
UNCONFIRMED, the computed tick age is off by up to the real broker offset (3h
= 10800s). `LiveTrader.wait_until_market_open()` classifies that age into
three buckets: `>3600s` → enter the weekend sleep-probe loop (correct, and
already reused by both the boot path and the running `_tick()` loop); `<120s`
→ market genuinely open; the remaining `120s < |age| < 3600s` band → **ABORT**
("Broker server time drifts >2min... ABORTING", `return False`) on the
assumption that ticks are fresh but the OS clock has drifted. With the real
offset undetected and Friday close true-age ≈7320s, the mis-decoded age landed
at ≈−3480s — squarely in the ABORT band by pure arithmetic accident, even
though the true cause was "closed market, offset never confirmed," not "OS
clock drift." `wait_until_market_open()` returning `False` made `run()` return
early (a clean, intentional `return`, not an exception) → `run_live()`'s
`finally: adapter.shutdown()` → `main()` fell off the end → process exit 0.
The existing running-loop survival path (`_tick()` → `_market_closed_now()` →
the same `wait_until_market_open()`) never hit this because by the time the
bot is already running, the offset has almost always already been confirmed at
least once — it's specifically a FIRST-BOOT-into-a-dead-feed condition.

**Fix (`live_trader.py`, `wait_until_market_open`):** an UNCONFIRMED offset
(`adapter.tick_time_offset_hours is None`) now routes into the SAME sleep-probe
loop as a confirmed `>3600s` closed market, never into the clock-drift ABORT
branch (which is now only reachable once the offset IS confirmed, since it is
computed by construction from unreliable data otherwise). Inside the sleep
loop, the wake check itself branches the same way: while unconfirmed, the loop
does not trust a raw `tick_age_sec<60` read (which the same offset bias could
also read "fresh" long before the market genuinely reopens) — it instead calls
`adapter.ensure_time_offset()` each recheck, which can only succeed once Tier 1
(a genuinely LIVE, advancing feed) or Tier 2 (a tick within 10min of true now)
actually confirms, so it cannot wake early. Once real ticks resume, detection
succeeds quickly and the existing wake path (`_validate_offset_on_wake`,
already there pre-fix) re-derives and asserts the offset before any anchor
logic runs, exactly as it always has for a normal weekend wake. Never touches
the exit-42-only watchdog relaunch policy — this is purely about not exiting
in the first place.

**Files:** `live_trader.py` (`wait_until_market_open`). **Self-test:** 215
(`e19 boot survives` — drives the REAL bound method against a stub adapter
that reproduces the exact skewed-age arithmetic from the live incident;
asserts the ABORT/"ABORTING" alert never fires, the sleep-probe entry log
fires, and it wakes only once offset detection succeeds).

---

## E-22 — anchors day-P&L accumulator froze on the flatten path; the −$630 loss stop went blind — **FIXED**

**Status:** FIXED — 2026-07-09 — branch `claude/anchors-pnl-truth-defect-i8cp16`.

**Symptom (live, 2026-07-09):** `state['daily_pnl']` (the anchors realized day
P&L, magic 20260522) froze at **+$140.00** — A1's first close — while
`pnl_source.magic_day_net(deals, 20260522)` correctly reported **−$821.10**
across 5 closes for the same broker day. `anchors_daily_loss_stop` (−$630)
NEVER FIRED on a −$821 day; the engine's own hard brake was blind, and the
account-level kill switch had to catch the loss. A risk-control failure, not a
reporting bug. The "Anchor processing GATED" warning also spammed the log
(~10/min) and both the kill-switch trigger + GATED lines printed the frozen
`daily_pnl` mirror instead of the value the switch actually compared.

**Root cause:** `state['daily_pnl']` was accumulated ONLY in the fill-reconcile
loop (`fills.py`, `self.state['daily_pnl'] += pnl_usd` on a detected close).
`risk._flatten_all()` closes positions DIRECTLY through the MT5 adapter and pops
them from `shadow_positions`, so the reconcile loop never sees those closes and
never accumulates them. Every kill-switch / EOD / Friday flatten therefore
bypassed the accumulator, freezing it at whatever the last fill-loop close left
it. The anchors loss/profit governors, the account-target combined net and the
kill-switch paper fallback all READ that frozen accumulator as authoritative.

**Fix:** the anchors DECISION-path day P&L is now COMPUTED from broker deal
history (`pnl_source.magic_day_net`, magic 20260522) for the current broker day
via `daystops.computed_anchors_day_pnl()`, cached per tick and invalidated on
any close (`fills.py`) or flatten (`risk._flatten_all`). `state['daily_pnl']`
becomes a persisted MIRROR — overwritten from the computed truth on every read,
authoritative for nothing, only a fallback when history is unavailable
(paper / query failure); the `fills.py` increment stays as an optimistic
display value corrected on the next recompute. Every DECISION read-site now
reads the computed value: the anchors loss/profit governors (`_anchors_daystop`),
the account-target combined net (`_engine_day_pnls`), and the kill-switch paper
fallback (`risk._check_kill_switch`). The kill-switch trigger + the (now
once-per-lock-event) GATED warning print the COMPARED value (equity drawdown vs
threshold) via `risk._kill_switch_drawdown`. Display-only surfaces may still show
the mirror but are labelled.

**Files:** `daystops.py` (`computed_anchors_day_pnl`, `invalidate_pnl_cache`),
`live_trader.py` (`_anchors_day_pnl_computed`, `_engine_day_pnls`,
`_anchors_daystop`, `_log_kill_gated_once`, kill-switch block, day-roll reset),
`risk.py` (`_check_kill_switch`, `_kill_switch_drawdown`, `_flatten_all`),
`fills.py` (close-path cache invalidation). **Self-test:** 284 (`e22 flatten
truth` — synthetic 07-09 history incl. flatten-path closes: computed ==
magic_day_net == −$821.10, the accumulator diverges at +$140, the mirror is
corrected), 285 (`e22 flatten 3 closes` — `_flatten_all` closes 3 positions and
the day P&L reflects them within ONE tick via cache invalidation), 286 (`e22
loss over accum` — the loss stop fires at ≤−$630 computed even while the
accumulator reads +$140), 287 (`e22 gated once` — the GATED warning fires once
per lock event, not per tick, printing the compared value).

---

## E-23 — testfire bypasses the anchors daily loss halt — **FIXED (this branch, v3.8.3)**

**Status:** FIXED — 2026-07-09 — branch `claude/e23-and-ledger`. HIGH.

**Evidence (live, 2026-07-09):** `python bot.py testfire` placed a real A2
straddle while the anchors engine was LOSS-HALTED at −$821.10 against its −$630
hard stop. Preflight CLEARED at 18:36:37; the anchors day-P&L rebuild landed at
18:36:41 — preflight ran BEFORE the governor knew the day's truth. The BUY
filled, floating drawdown pushed account equity $1.24 past the 3% line, and the
account kill switch had to catch it. `testfire.py` had no reference to
entries_blocked / loss_stop / daystop.

**Root cause:** the testfire path (`arm_testfire` → the deferred-anchor
completion) never consulted the anchors daily brake, and the preflight ran
before the day-P&L rebuild, so even a check would have read a cold state.

**Fix:** `testfire_preflight` gains **RAIL 6 (ANCHORS BRAKE)**, evaluated after
`run_testfire` primes the anchors day-P&L rebuild. It refuses when
`_anchors_daystop_blocked()` (the anchors LOSS halt / PROFIT lock / account lock,
read from the COMPUTED `pnl_source.magic_day_net`, never the `state['daily_pnl']`
mirror) **or** `_anchor_entries_blocked()` (Friday window / anchors engine OFF) is
active. Fail-closed on any error. `--force-window` does NOT bypass rail 6 (it
skips only rail 4, the collision guard). NOTE: `_anchor_entries_blocked` alone
does NOT cover the daily stop — the daystop is read explicitly, so a test-fire
obeys the exact brake a scheduled anchor does. **Files:** `testfire.py`
(`testfire_preflight` rail 6, `_prime_anchors_daypnl`, `run_testfire`).
**Self-test:** 288 (`e23 tf loss halt` — −$700 computed refuses, and refuses
again with `force_window=True`), 289 (`e23 tf profit lock`), 290 (`e23 tf
clean/off` — clean day clears all six rails; engine OFF refuses), 291 (`e23 tf
ordering` — the rebuild runs BEFORE preflight, source + functional).

---

## R-8 — rogue_trades.csv header/row width mismatch — **FIXED (rogue core in PR #102; rescue/journal hardened this branch)**

**Status:** FIXED — the rogue/fetcher/boost writers self-heal (`csv_schema.ensure`)
and `migrate_run_dir` sweeps them at boot (PR #102). This branch AUDITED every
writer and added the same self-heal + boot-sweep coverage to the two that lacked
it: `rescue_events.csv` (`rescue_log.finalize`) and the monthly anchors journal
`trades_<YYYY-MM>.csv` (`journal._write_journal`, now built from a single
`JOURNAL_COLUMNS` constant so header width can never drift from row width).

**Evidence (2026-07-09):** `run/rogue_trades.csv` carried a 9-column HEADER over
10-column ROWS (`seed_source` appended to rows ~07-06 without rewriting the
header); `csv.DictReader` dropped the 10th value into `restkey`.

**Audit result:** rogue_trades ✓, rogue_patterns ✓, fetcher_trades ✓,
boost_ledger ✓ (all self-heal on append + in the boot sweep); pnl_ledger ✓
(whole-rewrite each run, structurally immune); rescue_events + journal ✗ → fixed
here. journal's header/row were in lockstep (20 cols, not mismatched) — the
docstring said "19-col" (stale, pre-`trigger_source`); corrected to 20. **Files:**
`rescue_log.py`, `journal.py`, `csv_schema.py`. **Self-test:** 292 (`r8
rescue+journal heal` — both writers self-heal, `migrate_run_dir` sweeps both);
278 / 279 unchanged.

---

## R-10 — status surfaces disagree with the engine registry — **FIXED (this branch, v3.8.3)**

**Status:** FIXED — 2026-07-09 — branch `claude/e23-and-ledger`.

**Evidence (2026-07-09):** `run/state.json` showed `engines: {anchors, rogue}`
(no fetcher) while `day_pnl_by_engine` carried a fetcher entry — mismatched key
sets; and Rogue + Fetcher both rendered `lock: "active"` while switched OFF and
`loss_stopped=True`.

**Root cause:** the `engines` field in `_write_status` was hardcoded to
`{anchors, rogue}` (fetcher literally omitted); and the per-engine lock label was
derived per-surface without consulting the engine SWITCH, so a switched-OFF (or
empty-gov) engine defaulted to `active`.

**Fix:** one canonical engine set (`_ENGINE_KEYS`) + one source
(`LiveTrader._engine_state` → registry `_engine_enabled` + computed governors,
rendered through `_engine_display_state`). Every surface now derives from it:
the `state.json` `engines` mirror lists all three; `day_pnl_by_engine` carries
`switch`/`lock`/`state` per engine with a matching key set; `/daylock`
(`daystops.render_status`, now passed `engine_states`) shows OFF / LOSS-HALTED
for Rogue+Fetcher (was hardcoded "🟢 live"); the `/status` card (watchdog
`_engine_state_row`) renders one engine switch+lock line. A DISABLED engine reads
**OFF**, never `active`. `_engine_state` uses the PURE `anchors_daystop` (not the
latching `_anchors_daystop`) so rendering a surface never latches the profit lock.
pnl_report carries no engine switch/lock surface, so it needed no change (its P&L
is already single-sourced by magic). **Files:** `live_trader.py`, `daystops.py`,
`watchdog.py`. **Self-test:** 293 (`r10 engine surfaces` — disabled reads OFF,
enabled+loss-stopped reads LOSS-HALTED, registry + day_pnl_by_engine key sets
match, /status + /daylock agree with the payload).

---

## Status Ledger (2026-07-09) — DEVIATION / OPEN / FIXED index

### DEVIATION LOG

- **DEV-2 (07-07):** rogue confirm-5 believed deployed at 12:30 was never merged;
  the bot ran confirm=10 all morning. Caught via the seed card reading "$10".
  **Lesson: believed ≠ merged ≠ loaded. Verify the VALUE IN THE FILE at HEAD.**
- **DEV-3 (07-07):** three restarts + two param changes + one new engine in one
  session → 07-07 attribution is void.
- **DEV-4 (07-08/09):** PRs #93–#101 (v3.7.0 → v3.8.0) landed across few restarts
  with no single clean observation day. Bisect newest→oldest if a regression
  needs attributing.

### OPEN

- **E-23** (07-09, HIGH): testfire bypassed the anchors loss halt; preflight ran
  before the day-P&L rebuild. **FIXED-AT-HEAD by this branch** (see E-23 above) —
  becomes closed on merge.
- **R-8**: rogue_trades.csv 9-col header / 10-col rows. **FIXED** (rogue in
  PR #102; rescue/journal hardened this branch — see R-8 above).
- **R-9**: three P&L surfaces disagreed materially (07-08 ROGUE: report +$538.65
  vs raw CSV +$7.00; FETCHER ~$170 offset both days). Root cause: trade CSVs were
  summed as a P&L source. Superseded by the `magic_day_net` single source
  (PR #103). **VERIFICATION DONE by R-14 (this branch):** the reconcile pass over
  07-01…07-09 surfaced the remaining `report`-surface drift (see R-14) and fixed
  it; the four surfaces now agree with MT5 to the cent on every day.
- **R-10**: status surfaces vs engine registry. **FIXED-AT-HEAD by this branch**
  (see R-10 above) — becomes closed on merge.
- **R-13** (07-09, MEDIUM — split-only, not total): fleet events never finalize.
  `run/state.json` shows `"closed": {}` on both 07-09 rescue events, so a boost
  ticket never resolves to RALLY/RESCUE/TRAPPED_LATE_RESCUE and stays
  `BOOST_UNCLASSIFIED`. Root cause (verified in code, NOT a wiring gap):
  `_rescue_event_on_close` is only reached from the `fills.py:392` close-detection
  loop, which iterates `self.shadow_positions`. On a kill-switch / EOD / Friday
  flatten day (07-09 was a loss-halt flatten day) `risk._flatten_all` closes each
  member DIRECTLY via `adapter.close_position` and `shadow_positions.pop(ticket)`
  (`risk.py:163-191`) — never calling `_rescue_event_on_close` — so the fills loop
  never sees those members, `ev["closed"]` stays `{}`, and the finalize condition
  `closed.keys() >= members` is never met. This is the SAME bypass class as E-22
  (flatten skips the fills reconcile/attribution loop). **Impact bound (verified
  against R-14, finding 4): corrupts the per-leg-class SPLIT only, NOT the anchor
  TOTAL** — `per_anchor_stats` books a `BOOST_UNCLASSIFIED` leg into `net` (via the
  `else` branch that adds to both `unclassified_pnl` AND `net`), and R-14 anyway
  sources the anchor total from `magic_day_net`, which counts the boost's OUT deal
  regardless of label. **Minimal fix (NOT applied here — engine close-path change,
  out of scope for this report/reconcile branch):** in `risk._flatten_all`, after a
  broker-verified close and BEFORE the `pop`, read the close deal's realized P&L and
  call `self._rescue_event_on_close(ticket, pnl)` (guarded), mirroring `fills.py:426`
  — the same remedy family E-22 used (attribute the flatten-path close instead of
  letting it bypass).
- **G-1** (unchanged, still blocks funded): F-B bypasses the FP guard; worst
  floating ≈ 4×($286+$260) = −$2,184 vs FPZERO −$500. 07-09 made this concrete —
  the kill switch fired on FLOATING equity drawdown ($1,738.45 vs $1,737.21).
  Verified in code this pass: `fills.py:651` fires `plan_trapped_late_rescue`
  through its own call site and `continue`s BEFORE the break-and-hold gate and
  the FP guard; gated only by `trapped_late_rescue_enabled`, not
  `rescue_entry_enabled`.

### FIXED (live-verified)

- **R-14** (07-09→10): `pnl_report` drifted from the MT5 authority on EVERY trading
  day (07-01…07-09 reconcile: report claimed +$1,728 while `magic_day_net` said
  −$586; a $2,314 cumulative, mostly one-directional gap, with sign flips on
  07-03/07-08/07-09). This is the reconcile pass R-9 flagged as outstanding. **Two
  independent root causes, both proven with a synthetic faithful-reproduction
  harness (production 07 history isn't reachable from CI):** (1) **the DROP** —
  `pnl_report.build_trade` returned `None` unless BOTH an IN (entry==0) and an OUT
  (entry==1) deal for the position fell inside the window, so a position OPENED
  BEFORE the window and CLOSED INSIDE it (A5 fires 19:30 broker = 22:00 IST and
  holds past midnight) was dropped from the report while `magic_day_net` (OUT deals
  only, IN not required) counted it → `delta = −(dropped P&L)`, both directions.
  The reverse leg (opened in-window, closed after) was dropped from today AND from
  tomorrow (no IN there either), making the drift cumulative. (2) **the WINDOW** —
  the report cut at IST midnight (`ist_day_window_utc`, UTC+5:30) while the
  authority/live/ledger/EOD-flatten all use the broker day (UTC+3), a 2.5h shift
  that put boundary-straddling closes on different days. Also found: partial closes
  under-counted (only the LAST OUT tranche booked, vs `magic_day_net` summing all),
  and anchor-magic closes with an unrecognized comment / `position_id=None` reached
  `magic_day_net` but not `per_anchor_stats`. **FIX (v3.8.7):** the report + ledger
  now use the broker-day window (`day_window_utc`, byte-identical to
  `pnl_source.broker_day_range`); each engine's reported total is SOURCED FROM
  `magic_day_net` (anchors via a `reconcile_anchor_total` residual `ext` bucket so
  the per-anchor sum equals the authority to the cent, with a best-effort `outside`
  attribution for opened-before-window legs; rogue/fetcher `day_pnl` overwritten
  with their magic's net); `build_trade` never drops a realized close (OUT-only
  positions are built and summed across all tranches). **Verified:** `reconcile
  --date 2026-07` over a straddle+partial synthetic month prints "ALL SURFACES
  AGREE WITH MT5 ON EVERY DAY — no corrections needed" (all four surfaces:
  authority == ledger == report == live). **Self-test 296** asserts OUT-only is
  counted, IN-only is not double-booked, 3 OUT tranches sum once, `report_net ==
  magic_day_net` for every fixture, and `reconcile_day()` ok=True on a clean
  straddle day. PR (this branch), v3.8.7.
- **R-12** (07-09): the reconcile CLI had NEVER executed. `pnl_reconcile.run_cli`
  built its adapter as `MT5Adapter(cfg)` + `adapter.connect()` — the ctor takes the
  SYMBOL STRING (connects in `__init__`) and there is NO `connect()` method, so
  `python bot.py reconcile` raised `AttributeError` on every run. The tool whose
  whole job is to prove the P&L surfaces agree was itself never verified against the
  real adapter — selftests 281/282 passed against a PRE-BUILT stub trader
  (`reconcile_day` directly), never exercising `run_cli`'s construction. THIRD
  instance of the class this session (DEV-2 believed≠loaded; E-23 rail-6-behind-
  rail-4). **FIX:** `MT5Adapter(getattr(cfg,'symbol','XAUUSD'))` (canonical idiom =
  `pnl_report.run_dailyreport`); keep `adapter.shutdown()`. Audit of every
  `MT5Adapter(` / `.connect()` call site: 5 sites, 4 already correct
  (live_trader:2566, pnl_report:984, selftest, testfire — all pass a symbol string),
  1 wrong (pnl_reconcile) — the only `.connect()` in the repo. **Self-test 295**
  drives `run_cli`'s REAL construction path with a fake backend (asserts the ctor
  gets a str, `.shutdown()` is called, and rc∈(0,1) — a broken ctor lands in the
  except → rc=2) and guards the real MT5Adapter API contract (symbol-first ctor /
  has shutdown / NO connect). Verified: reconcile on synthetic 07-09 data corrects
  anchors ledger +$319 → −$821.10 authority, with the `live` column also −$821.10
  (past-day rebuild from history via the CLI trader's adapter — meaningful, never a
  silent 0.0). PR #107, v3.8.6.
- **E-20**: Rogue/Fetcher/anchors day governors rebuilt from broker deal history
  on same-day restart (never zeroed). Fetcher first (PR #93), anchors in PR #103.
- **E-21** (07-09): FIRST LIVE PROOF of per-engine loss-stop independence —
  Fetcher LOSS-HALTED at −$536 while Rogue and Anchors continued trading
  independently.
- **E-22** (07-09, the most serious): `state['daily_pnl']` was accumulated ONLY at
  `fills.py:401` (the reconcile loop). `risk._flatten_all()` closes positions
  directly via the adapter, so kill-switch / EOD / Friday flattens never reached
  the accumulator. On 07-09 it froze at +$140.00 while broker truth was −$821.10 —
  so `anchors_daily_loss_stop` (−$630) NEVER FIRED on a −$821 day; the account
  kill switch had to catch it. **FIX (PR #103, v3.8.2):** all decision read-sites
  compute from `pnl_source.magic_day_net`; `state['daily_pnl']` is a mirror,
  authoritative for nothing; the kill-switch + GATED logs print the COMPARED value
  (equity drawdown), GATED throttled to once per lock. Selftests rewired to stub
  `magic_day_net` so a governor reading the mirror now FAILS. **Lesson: 277 green
  steps did not catch a −$821 day sailing past a −$630 stop, because the tests set
  the accumulator directly.** (See the full E-22 entry above.)
- **promote_on_boot override (07-09) — PARTIAL / DISCREPANCY.** The failure:
  `rogue.py` / `fetcher.py` force-set `enabled=True` on demo, silently reverting
  an explicit config `False`. **`rogue.promote_on_boot` IS fixed** (Optional[bool]
  sentinel: None → auto-promote on demo, True/False → explicit owner override;
  funded forced OFF first and unconditionally). **`fetcher.promote_on_boot` is
  NOT** — it still auto-promotes to `True` on ANY demo account (`fetcher.py:95`),
  so on a demo boot the D-28 "fetcher OFF" week is SILENTLY REVERTED to ON. Left
  as-is (this is a documentation branch; behavior unchanged) and flagged as a
  follow-up: fetcher needs the same sentinel to make its explicit `False`
  authoritative on demo. The config comment on `fetcher_enabled` now states this.

---

## Watch Ledger — patterns under observation (NOT confirmed bugs, NO lever pulled)

### W-2 — evidence append — 2026-07-02

**A4 BUY:** no-hold shadow **+$36.75** vs actual **−$630** (held 33.9m).

### W-11 — Rogue and Fetcher are CORRELATED, not complementary — **UPGRADED to structural (2026-07-09)**

Same $5-chase logic, different SL widths. 07-09: both took losing trades in the
same 4076–4086 band within minutes. Real offset comes from Anchors only. Genuine
diversification requires a DIFFERENT instrument (XAGUSD), not a third gold
chaser. (Feeds D-28: the anchors-only week.)

### F-B is the biggest two-way swing factor and is UNGATED — evidence append (2026-07-09)

+$948.15 on 07-08 (A1), and the 07-09 A2 event (rescue SELL 4102.84, boosts at
4113.05 / 4112.93) filled at the top. `rescue_entry_enabled` does NOT govern it
(F-B rides `trapped_late_rescue_enabled` through its own call site — see G-1).

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

### D-4 — W-7 DECIDED: `parent_established_dollars` 20 → 12 + override widened — 2026-07-03

**Decision:** the CASE 2 break-and-hold override threshold (`config.py`,
`parent_established_dollars`) is lowered **$20 → $12**. Branch
`claude/p4-boosts-trapped-leg-gbn54a` (P4). W-7 is now DECIDED, not WATCH.

**Rationale (2 instances in 2 days, journal + live logs):**
- 2026-07-02 A4 SELL @edge 4131.02: BREAK FAILED(reversed), price continued
  **+$17.7** in the break direction, parent only +$10–16 favorable (under the
  old $20 line) — **~$300–400 forfeited**.
- 2026-07-03 A1 BUY @4134.57: BREAK FAILED(retrace), price ran **+$56**, parent
  crossed the old $20 line only well into the move — **~$2,000+ forfeited**.

**Source-verified answer to the latch question:** `rally.break_and_hold_ok` /
`_override_grade` store **no per-episode FAILED state** — `break_hold.classify`
re-evaluates fresh from the current M5 bars every call, and the CASE 2 check
reads `shadow['max_fav']` (the position's live, monotonic peak) fresh every
call too. There is **no code-level latch**: a parent that later proves the
move (crosses the threshold) fires on the very next tick's gate evaluation,
even after an earlier FAILED verdict for the same episode — proven directly
against the live gate in self-test 203 (`d4 override re-eval`: blocked at
parent +$8, then the SAME shadow/episode fires at parent +$15 with no reset).
The forfeiture in both live instances is explained by the parent staying
under the (old, higher) threshold long enough for the move to run away
untouched, not by a latch bug.

**Scope:** `parent_established_dollars` 20.0 → 12.0 (config value only; the
override mechanism, the gate itself, and RESCUE's bypass are unchanged — this
widens CASE 2's escape hatch, it does not touch the strict gate that blocks
real fakeouts, tests 55-59). Self-test 96/97 boundary fixtures updated to the
new $12 line (was $19.99/$20.0, now $11.99/$12.0); new self-test 203 proves
continuous re-evaluation post-FAILED.

**Restore path:** set `parent_established_dollars=20.0` in `config.py`.

### D-5 — F-B (`trapped_late_rescue_enabled`) flipped LIVE — 2026-07-03

**Decision:** the F-B trapped-leg capped late-rescue flag (`config.py`,
`trapped_late_rescue_enabled`) defaults **False → True**. Same branch as D-4.
W-4 is now DECIDED, not WATCH.

**Rationale (3 trapped-leg events in 2 days, all unhedged naked):** 2026-07-02
A4 rode a $27 collapse naked; 2026-07-02 A5 rode naked overnight; 2026-07-03 A1
rode a $70 rally naked. In each case the No-OCO losing leg (`boost_rally_only`)
had rescue suppressed by design and rode to its full SL with no hedge offered.

**Gate check (source-verified, file:line):** F-B does **NOT** pass through
break-and-hold — it bypasses it entirely, structurally, the same way
`rescue_bypass_break_and_hold` bypasses RESCUE. `fills._check_boost_triggers`
calls `boosts.plan_trapped_late_rescue` and, on a plan, fires
(`self._fire_boost_event`) and `continue`s (`fills.py` ~604–620) **before**
the tick-hold streak check and the `_break_and_hold_ok` call are ever reached
(`fills.py` ~651–695) — the gate is never even asked. No code change was
needed for this: F-B already bypasses the gate by construction (a hedge
armed $10+ underwater IS the confirmation). Self-test 205 (`fb bypasses gate`)
proves this end-to-end against the real `_check_boost_triggers`: with the
break-and-hold seam stubbed to ALWAYS refuse, the hedge still fires and the
gate is never called.

**Scope:** default flip only; `boosts.plan_trapped_late_rescue` /
`trapped_rescue_cap` logic unchanged. Anchor-side only (never touches a Rogue
`20260626` ticket). Self-test 184 (`fb late rescue`) now forces the OFF case
explicitly (was reading the old default); self-test 206 (`fb default on`)
asserts the new default.

**Restore path:** set `trapped_late_rescue_enabled=False` in `config.py`.

### D-6 — Friday weekend-hold ban: flatten cutoff + A4/A5 Friday skip — 2026-07-04

**Decision:** four new `config.py` flags (weekend branch
`claude/weekend-report-boot-friday`): `friday_flatten_enabled=True`,
`friday_flatten_broker_hour=22.5`, `a5_skip_friday=True`,
`a4_skip_friday=False`.

**Rationale:** FundingPips Zero (and prop-firm rules generally) treat ANY
position held over the weekend as a hard breach / account termination — a
Friday gap is not an ordinary P&L risk to size around, it can end the account
outright. The existing daily EOD flatten (`eod_broker_hour=23`) is a same-day
mechanism with no Friday-specific margin; this adds an earlier, Friday-only
cutoff (22:30 broker, 30min ahead of the normal 23:00 EOD) so anchor + boost
legs are flat with margin before the week genuinely closes, not right at it.
A5 (19:30 broker, the latest-firing normal anchor) is skipped outright every
Friday — its fill would sit for barely ~3h before the flatten cutoff closes it
again, all fee/spread cost for no time to develop. A4 (16:40 broker) has more
runway and stays ON by default on demo; a funded (FundingPips Zero) deploy
should flip `a4_skip_friday=True` explicitly, since that profile's weekend-hold
rule makes any Friday anchor that might still be open into a slow-closing week
not worth the risk of a hard account breach.

**Scope (source-verified, file:line):**
- `live_trader.py` `_friday_flatten_reached(broker_date, utc_now)`: Friday-only
  (`broker_date.weekday() == 4`), splits the decimal `friday_flatten_broker_hour`
  into (hour, minute) and runs it through the SAME `anchor_datetime_utc`
  conversion `_eod_reached` already uses — not a new time-comparison idiom.
- `live_trader.py` `_tick()` step 5.5 (between the kill-switch gate and step 6
  EOD): on the Friday cutoff, calls `_flatten_all(reason="EOD")` exactly once
  per day (`state['friday_flatten_done']`, reset in `_reset_if_new_day`
  alongside `processed_anchors_today`). **Rogue is deliberately untouched here**
  — `reason="EOD"` is the SAME sentinel `risk._flatten_all` already checks
  (`str(reason) != "EOD"` gates the `rogue.force_close_open` call, E-15) to
  skip force-closing Rogue's ticket; `rogue_flatten_at_eod` already flattens
  Rogue at `eod_broker_hour` (23:00) every day, weekends included, independent
  of this flag. Step 7 (anchor processing) also checks
  `state['friday_flatten_done']` so no NEW anchor entry can open between the
  cutoff and the next broker day.
- `anchors.py` `_anchor_skipped_today_friday(label, broker_date)`: a SEPARATE,
  earlier-acting check wired into `_process_anchor_if_due`'s per-anchor loop
  (alongside the existing `processed_anchors_today` / `missed_anchors_today`
  skips) — this skips a Friday anchor's placement OUTRIGHT (so it never opens
  a position that would need the later flatten at all), not just closes
  whatever is open by the cutoff hour. A1/A2 are never Friday-skipped by this
  check.

**Self-test:** 215 covers E-19 (above); 216 (`friday flatten gate`) drives the
real `_friday_flatten_reached` across before/after-cutoff and a non-Friday day,
and drives the real `risk._flatten_all(reason="EOD")` to confirm it closes the
anchor/boost stack while `rogue.force_close_open` is never called; 217
(`friday a4 a5 skip`) drives the real `_anchor_skipped_today_friday` for the
demo defaults (A5 skipped Friday, A4 not), the funded override
(`a4_skip_friday=True`), A1 never skipped, and non-Friday days unaffected.

**Restore path:** set `friday_flatten_enabled=False` and `a5_skip_friday=False`
in `config.py` to fully restore pre-D-6 behavior (plain daily EOD only).

### D-11 — `rogue_entry_confirm_redesign` 10 → 5 — LIVE 14:58 07-07

The A1-anchored Rogue confirm distance dropped 10 → 5 (`config.py`,
`rogue_entry_confirm_redesign = 5.0`, verified at HEAD). NOTE the chain
displacement gate (`rogue_chain_min_displacement = 6.0`) now EXCEEDS the confirm
by design — a chained entry needs $6 of fresh displacement even though the
initial confirm is $5. (See DEV-2: a believed-but-unmerged confirm-5 ran as
confirm-10 all morning of 07-07.)

### D-13 — `rogue_init_sl` 5 → 10, PAIRED with `rogue_daily_loss_stop` −525 → −1050 (E-5 rule)

**SUPERSEDED the same week** by the −$370 tight cap (D-16/17): at
`rogue_daily_loss_stop = -370` the 3-fail pause is UNREACHABLE for Rogue at
current defaults. The pause code is kept (it re-arms if the SL / stop values
change). Current HEAD: `rogue_init_sl = 10.0`, `rogue_daily_loss_stop = -370.0`.

### D-14 — FETCHER engine LIVE (PR #93, v3.7.0, magic 20260707)

$5 trigger / +$5 TP / −$5 SL / no trail / re-anchor at close / cap 20 / 3-fail
pause / funded force-off. **Review gate:** win rate vs the ~54% breakeven line.

### D-15 — manual seeds `/rogueseed` `/fetchseed` LIVE

`seed_source=MANUAL` rows are EXCLUDED from D-8 / D-12 evidence. Manual seeds =
deliberate tests only.

### D-16 / D-17 — per-engine daily profit locks + hard loss stops

Profit locks **+$400 soft** (overridable once/day by reseed); hard loss stops
**Rogue / Fetcher −$370**, **anchors −$630** (= exactly one full anchor SL).
Losses hard, profits soft — the discipline line. Verified at HEAD:
`rogue/fetcher_daily_profit_stop = 400`, `rogue/fetcher_daily_loss_stop = -370`,
`anchors_daily_loss_stop = -630`.

### D-18 — `anchors_daily_profit_stop` 400 → 800 for the anchors-only week

Verified at HEAD: `anchors_daily_profit_stop = 800.0`.

### D-24 — daily 2% target with the A4 decision gate — currently DISABLED

80% minimum, skip A5 when secured post-A4, $200 give-back, no rollover.
Currently **DISABLED** (`account_target_pct = 0.00` at HEAD) for the anchors-only
week, so the +$800 engine lock (D-18) is the sole profit governor.
(`account_target_min_pct = 0.80`, `account_target_giveback_dollars = 200`,
`account_target_final_anchor = A4_1640_NYopen` remain configured for re-enable.)

### D-26 / D-27 — $10-break seed anchor + earned trade budget (PR #101)

- **D-26:** $10-break seed anchor — A1 is the REFERENCE, not the anchor; the
  first break latches; no break → no trades. (`seed_break_dollars = 10.0`.)
- **D-27:** earned trade budget — 2 free trades per anchor; a 3rd requires the
  last two closes both WINS; exhaustion → 15m gap → fresh anchor at the tick.
  (`engine_base_trades_per_anchor = 2`.) Both per-engine tunable; 0 disables.

### D-28 — ANCHORS-ONLY EXPERIMENT, 07-09 → 07-16

`rogue_enabled = False`, `fetcher_enabled = False`. **Rationale:** Rogue and
Fetcher are correlated (W-11), both net-negative for July (~−$1,470 combined),
took identical losing trades in the same chop band on 07-09, and their drawdown
tripped the ACCOUNT kill switch — flattening a profitable anchors book. Anchors
alone is the funded configuration (Rogue is force-off on funded regardless).
**⚠️ Caveat (see FIXED / promote_on_boot discrepancy):** on a DEMO account
`fetcher.promote_on_boot` still auto-promotes `fetcher_enabled` back to ON at
boot — the D-28 fetcher-OFF intent is NOT authoritative on demo until fetcher
gets the Optional[bool] sentinel. Verify `/fetcher status` reads OFF after boot,
or `/fetcher off` manually, for the duration of the experiment.

### D-29 — `rescue_entry_enabled` flipped True — 07-09 (concurrent with D-28)

**ACKNOWLEDGED TWO-VARIABLE WEEK:** results are not attributable to either change
alone. And per the F-B note (WATCH / G-1), `rescue_entry_enabled` does NOT govern
the F-B path that motivated the flip — it governs the NORMAL rescue path only.

---

## FUNDED / AUG-15 GATE

From the 26-day MT5 export (all engines, rescaled per-trade to each lot):

| lot  | monthly       | worst day | worst 3-day streak |
|------|---------------|-----------|--------------------|
| 0.15 | +$2,383 (4.8%) | −$338     | −$844              |
| 0.20 | +$3,178 (6.4%) | −$451     | −$1,126            |
| 0.25 | +$3,972 (7.9%) | −$564     | −$1,407            |
| 0.35 | +$5,561 (11.1%)| −$789     | −$1,970            |

Worst streak in the whole sample = **3 days** (06-19 → 06-23), once.
**Recommend 0.20** to start a $50k challenge: clears the target with ~45% of a
~$2,500 trailing buffer unused. **NOTE** these blend in Rogue (force-off on
funded) — recompute anchors-only before deciding.

**GO / NO-GO criteria (pre-committed):**
1. Anchors net positive for the window at the FUNDED lot (requires **G-1**
   resolved first — it sets the lot).
2. No single day breaches the funded daily limit with the brakes live.
3. Trailing drawdown never threatened in any clustered-loss stretch.
4. Rogue / Fetcher each judged on their own MTD, A3-style (cut, keep, tune).

---

## GROUND TRUTH RULE (strengthened)

Realized-P&L truth = **MT5 deal history via `pnl_source.magic_day_net`, by
magic.** `pnl_ledger.csv` is the MT5-rebuilt record. `rogue_trades.csv` /
`fetcher_trades.csv` are **DECISION LOGS — NEVER sum their rows to produce a
reported number** (their `outcome_dollars` is a live price delta, not an
account-dollar realized P&L — the R-8/R-9 corruption). The journal
(`trades_<YYYY-MM>.csv`) is anchors-only (magic 20260522) and EXCLUDES
Rogue/Fetcher. **D-9: name the surface** whenever a P&L number is quoted.
