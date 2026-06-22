# AUREON — FEATURES.md  (current version: v3.2.5)

Single source of truth for everything built and queued. XAUUSD 4-anchor straddle bot,
MT5 Python SDK, VPS-hosted, Discord alerts, frozen selftest baseline.

> **STATUS (as of v3.2.5 on `master`):** everything below through v3.2.5 is **BUILT,
> TESTED & MERGED** — v3.2.4 (break-and-hold + FP guard + 5-long) in PRs #39/#40, v3.2.5
> (A1 tick-fallback + tick-hold) in PR #41. `selftest` runs **78/78 PASS**, banner reads
> **AUREON v3.2.5**, backtest RAW net is identical to the frozen baseline ($40,787.42 on
> 2026-05 — the trade core never moved). The per-feature test numbers in the body below are
> the *design map*; the live `selftest` indices are contiguous 1–78 — see **TEST CASES BY
> VERSION** for the authoritative count and the actual index ranges.

---

## VERSION HISTORY
- **v3.2.2** — fire-at-fill fix, canonical plan_boost_event, rally/rescue toggles, rescuestats.
- **v3.2.3** — phantom-lock guard, stop-through re-arm, telemetry overhaul, Monday offset re-detect,
  weekend sleep + wake failsafe, auto-pull + soft restart.  (frozen baseline: 54/54)
- **v3.2.4** — break-and-hold filter, lot 0.15/0.35 + FP guard, 5-long No-OCO stack. (merged PR #39/#40)
- **v3.2.5** — A1 tick-fallback anchor capture, tick-hold boost/trail (0.3s refresh, 3-tick hold). (merged PR #41)

---

## CORE STRATEGY (unchanged)
- 4 daily anchors IST: A1 5:00 (Mon 6:00 override), A2 12:30, A3 16:20, A4 19:10.
- Straddle: buy stop = anchor +$5, sell stop = anchor -$5. Lot configurable.
- Per leg: SL $18, TP $30. No-OCO (sibling stays live as rescue-capable).
- Kill switch -3%. EOD flatten -> Firebase. Discord = sole alert channel.

---

## TEST CASES BY VERSION (authoritative — actual `selftest` indices on `master`)

| Version | Feature group | Tests added | selftest # | Cumulative |
|---|---|---:|:--:|---:|
| ≤ v3.2.3 | frozen trade core / telemetry / boosts / trail / timing / ops | 54 | 1–54 | 54 |
| v3.2.4 | break-and-hold filter | 5 | 55–59 | 59 |
| v3.2.4 | lot 0.15/0.35 + FP guard | 4 | 60–63 | 63 |
| v3.2.4 | 5-long No-OCO stack | 5 | 64–68 | 68 |
| v3.2.4 | 5-long additions (trail co-close, P&L 0.15/0.35, FPZERO profile cap, default-on) | 5 | 69–73 | 73 |
| v3.2.5 | A1 tick-fallback anchor capture | 2 | 74–75 | 75 |
| v3.2.5 | tick-hold boost/trail | 3 | 76–78 | 78 |
| **TOTAL** | | **78** | **1–78** | **78/78** |

> Note: earlier planning docs used a provisional numbering (e.g. "frozen 59/59", break-and-hold
> at 65–69). The features all shipped; the live indices above are the ground truth. The v3.2.3
> frozen baseline is 54 tests — the break-and-hold checks the plan had grouped into the frozen
> set actually landed under v3.2.4 at 55–59.

---

## FEATURES BY STATUS

### ✅ BUILT & TESTED (selftest 1-54, v3.2.3 — FROZEN, do not modify)

**Trade core**
- Connection / tick-fresh / comment-length / stop-place / market-place / SL-modify (tests 1-6)
- Rescue classification (twin-open=rescue, twin-closed=normal) (7)
- Rescue dry-run, timestamp header, late-retry (8-10)
- Fleet logger (CRASH_WIN / WHIPSAW_LOSS / SCRATCH branches) (11)

**Alerts / telemetry**
- Fill + close alerts, ts-fallback (12-14)
- BE rung, hold gate, boost SL, discord cards/dedup/heartbeat/conn (15-21)
- Full telemetry (gapless trace, all fields, predict line, discord fmt) (38)

**Boosts (rally + rescue)**
- Lone-leg rescue + boost trail + lone branches + isolation + live-log (22-26)
- Backtest parity, boost trigger, boost toggles (27-29)
- Lone boost L1-L5 (rally/rescue/sub10-none/fire-at-fill-blocked/one-shot) (34)
- Boost watchdog (MISSED_BOOST, BOOST_ARM_ORPHANED, below-trigger) (35)

**Trail / lock safety**
- Underwater-no-lock, trail telemetry, stop>=bid reject, lock guards (30-33)
- Phantom-lock guard (PL1-PL7: no lock without real max_fav, tripwire) (39-40)

**No-OCO stack (3-cap)**
- Stack-to-3, loser-rides, cap violation, trail floor (36)
- Stack economics (breakeven $630, $410each=+$600, whipsaw, exposure) (37)

**Timing / ops**
- Monday offset re-detect (M1-M7: weekend-wake +3, A1 0500 not 0600, Jun8 replay) (41-47)
- Weekend sleep + wake failsafe (W1-W5: sleep-enter, alarm-suppressed, fires-when-open, clock-math) (48-52)
- Auto-pull + SOFT restart (safe-gate, validate-abort, no-flatten, rehydrate, reconcile, quick-gap) (53-54, plus 41-52 group)

### ✅ BUILT & MERGED — v3.2.4 (build order: break-and-hold -> FP guard -> 5-long)

- **Break-and-hold filter** (selftest 55-59) — don't fire boosts on weak/fake breaks; stack only if price
  clears range by X ($3), holds N (2) M5 candles, retraces < Y (40%). Catches post-spike continuation.
  States CANDIDATE/CONFIRMED/FAILED; events BREAK_CANDIDATE/CONFIRMED/FAILED + CONTINUATION_STACK.
  THE PROFIT DECIDER.
- **Lot 0.15/0.35 + FP guard** (selftest 60-63) — lot config (0.35 demo / 0.15 FP-safe / 0.27 Zero);
  pre-trade guard blocks/reduces any stack that breaches 5% ($2,500) or Zero 1% ($500). Worst-case uses
  SL + spread buffer (18.6 eff): 5x0.35 -> -$3,255, 5x0.15 -> -$1,395. FPZERO_1PCT caps the 5-long to 3.
- **5-long No-OCO stack** (selftest 64-73) — orig + 2 rally + 2 rescue on winning side; loser rides to SL
  then out of exposure; trail: arm +$8, lock behind shared max_fav, all close together at peak-gap; unarmed
  -> own $10 SL. Cap 3->5 (DEFAULT ON, disableable; test 36 updated cap 3->5). Only fires after
  BREAK_CONFIRMED and at an FP-allowed lot. Includes P&L fixtures (0.15: +15/+315/+915; 0.35 modest +735).

### ✅ BUILT & MERGED — v3.2.5 (highest priority — protects Monday A1)

- **A1 tick-fallback anchor capture** (selftest 74-75) — Monday/post-weekend open: if M5 bar lags
  (get_m5_close no bar), capture anchor from a SANE held tick (passes max_tick_jump AND held >= 3 ticks)
  and PLACE. A1-open only. A2/A3/A4 keep bar capture. Fixes the Jun 22 A1 miss (bar lagged though offset
  was +3 correct). Events A1_BAR_MISSING / A1_TICK_FALLBACK / A1_PLACED_FROM_TICK + 🟢 Discord.
- **Tick-hold boost/trail** (selftest 76-78) — 0.3s tick refresh; boost fires only after +/-$10 holds
  >=3 ticks (blips rejected); trail lock advances only on held max_fav. Speed of tick + noise filter.
  Events TICK_CROSS_CANDIDATE / TICK_HOLD_CONFIRMED / TICK_BLIP_REJECTED.

---

## KEY BEHAVIOURS (the rules that matter)

- **Anchor capture:** A2/A3/A4 from M5 bar. A1 from bar, tick-fallback at open if bar lags (v3.2.5).
- **Boost trigger:** +/-$10 from fill; +$10 = rally (amplify), -$10 = rescue (hedge); 2 positions/fire;
  must hold >=3 ticks (v3.2.5); only after break confirmed (v3.2.4).
- **Stack cap:** 3 (legacy) -> 5 with 5-long (v3.2.4, DEFAULT ON), FP-gated; FPZERO_1PCT caps back to 3.
- **Trail:** arm at +$8, lock behind real shared max_fav (~$2.00 gap), all armed close together on reversal;
  unarmed -> own $10 SL. Never a phantom lock.
- **Loser leg:** rides to $18 SL (-$630 @0.35 / -$270 @0.15), then closed and out of exposure.
- **Stop-through:** re-arm + keep valid stop, NEVER market-close.
- **Lot/FP:** 0.35 = demo (breaches 5%); 0.15 = fundable (~2.5% DD); FP Zero 1% needs 3-cap not 5.

## ECONOMICS (June report-grounded)
- Actual June @0.35 (bugs): +$2,519 net, PF 1.23, DD 6.75% (breaches FP 5%).
- Gross profit +$13,410 / gross loss -$10,890 -> the drag is the target.
- 0.15 + fixes: ~+$4,000/mo, ~2.5% DD (fundable). 0.35 + fixes: ~+$9,300/mo (demo, ~5.8% DD).
- Floor ~$6k / target ~$10k (good month, gold trending).
- Daily target: ~$13 net price movement (~$200/day @0.15).

## SCALING (principle: more accounts at safe lot, never bigger lots)
- Phase 2 (Oct 2026 target): $50k FP Zero @ 0.15, hold Payout#1 reserve.
- Then $100k + $200k FP Zero from Payout#2 onward.
- 3x $50k @ 0.15 = ~$12k/mo combined, each under 5%.

## SELFTEST TARGETS
- v3.2.3 frozen baseline: 54/54.
- Current on `master`: **78/78** (v3.2.5) — 54 frozen + v3.2.4 (55-73) + v3.2.5 (74-78).
- See **TEST CASES BY VERSION** above for the per-version breakdown.

## BUILD ORDER (history — all merged)
1. v3.2.4 break-and-hold + FP guard + 5-long (PRs #39/#40).
2. v3.2.5 A1 tick-fallback + tick-hold (PR #41) — protects Monday A1.

## OPERATING RULES (every build)
- Never modify frozen tests / working logic. Additive only: logic change named + logging + Discord + tests.
- New tests start above the current max. No silent feature.
- selftest PASS + import-path identity + banner version confirm -> restart -> watch first live event.
- Scale by accounts, not lots. FP 5% (and Zero 1%) is the hard ceiling.
