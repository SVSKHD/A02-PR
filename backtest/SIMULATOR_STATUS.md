# AUREON offline simulator + boost redesign — status & findings

Branch: `claude/simulator-and-boost-v2`. This document is the **"report before
writing code"** deliverable required by the task, plus an honest record of what
is built, what is **blocked on data**, and exactly what is needed to finish.

---

## TL;DR

- **Part 1A (tick cache) — BUILT & TESTED** (`python bot.py fetchticks --from … --to …`).
- **Part 1B/1C (the simulator + reports), THE GATE, Part 2 (boost redesign),
  Part 3 (variant runs) — BLOCKED.** They require real 2026-07 XAUUSD **ticks**,
  the **200-position deal export** (the MT5 truth), and **`AUREON_boost_redesign_spec.md`**
  — none of which exist in this sandbox (no MT5; empty `backtest/ticks/`; the spec
  file and deal export are not in the repo).
- Per the task's own rule — *"if the baseline does not reproduce the MT5 truth,
  the simulator is wrong and every downstream number is fiction … do not tune the
  sim to match … stop here and report"* — I did **not** fabricate a gate pass or
  build Parts 2–3 on synthetic data.

---

## PRELIMINARY 1 — what `backtest/` models today

`backtest/backtest.py::run_month` is a **single-engine, M1-BAR** replay:

- `ticks_to_m1()` aggregates ticks → M1 OHLC and drives the live
  `strategy.update_position_on_bar` **on bars**. Intrabar order is a heuristic
  (both stops in one bar → `SELL if close>=open else BUY`; boost trigger scans
  `high` then `low`).
- **Modeled** (via real live modules — see `LIVE_RULE_SOURCES`): anchor placement,
  No-OCO straddle (first fill + sibling), trails/locks/ladder, the RALLY/RESCUE
  boost family via `boosts.plan_boost_event` + whipsaw cap, per-leg EOD flatten,
  and a crude **per-day realized** kill (`daily_pnl <= -daily_loss_pct*balance`).

### MISSING (bounds what any current result can prove)

| Feature | In `backtest/` today? |
|---|---|
| **Rogue** engine (`rogue.py`) | ❌ |
| **Fetcher** engine (`fetcher.py`) | ❌ |
| **F-B `trapped_late_rescue`** (`rescue.py`) | ❌ (only RALLY/RESCUE) |
| Per-engine **loss stops / profit locks / fail pause / entry caps** (`daystops.py`) | ❌ |
| **3% kill switch on equity incl. unrealized** (`risk._check_kill_switch`) | ❌ (only realized per-day) |
| **Friday weekend-hold ban** (D-6) | ❌ |
| Real **magics / `AUR_*` comments** → `pnl_report` classifier | ❌ (uses `Position` objects + its own CSV) |
| `daystops.py`, `risk.py` driven | ❌ |
| `break_hold` / `fp_guard` | imported, **not invoked** in `run_month` |

So today's backtest speaks only to **anchors**, on bars, with no governors — it
cannot address the trapped-leg/boost question at the heart of this task.

## PRELIMINARY 2 — tick availability (SAID LOUDLY)

`backtest/tick_fetcher.py` has a real MT5 path (`copy_ticks_range(…, COPY_TICKS_INFO)`)
**and** a synthetic fallback. **In this sandbox:** `import MetaTrader5` fails
(Windows-only) → the fetch returns `None`; `backtest/ticks/` holds only `.gitkeep`.
**Real 2026-07 ticks are NOT obtainable here.** The only data available is a
deterministic random walk around \$4300 (explicitly "illustrative"), which cannot
reproduce real market P&L. **A bar sim cannot resolve intrabar wick ORDER** — the
single ambiguity that decides whether a trapped leg's SL fires before the winner's
trail locks (~\$1,100 per the task's own note). The new manifest records tick-vs-M1
resolution per day so this is never hidden.

## PRELIMINARY 3 — offline constraints (confirmed)

The current backtest already runs MT5-disconnected, places no orders, and writes
only under `backtest/`. Part 1A preserves this: `fetchticks` reads MT5 for history
ticks only and writes **only** under `backtest/ticks/` (asserted by selftest 297).

---

## BUILT — Part 1A: per-day tick cache + manifest

`backtest/tick_cache.py` + `python bot.py fetchticks --from D1 --to D2 [--force]`:

- One file per calendar day: `backtest/ticks/XAUUSD_YYYY-MM-DD.parquet` (CSV
  fallback when pyarrow is absent). **Idempotent** — a day on disk is not refetched
  unless `--force`; its resolution is preserved.
- `backtest/ticks/manifest.json` records, per day: **resolution actually obtained**
  (`tick` / `M1` / `unavailable`), row count, source, path, fetched-at.
- Real fetch tries **ticks first**, falls back to **M1 bars** (flagged, because
  intrabar order is then unknown), and reports **`unavailable`** (writing nothing)
  when MT5 is absent — honest, never fabricated.
- **selftest 297** proves the plumbing (per-day split, idempotency, resolution
  tagging, M1 flag, unavailable-writes-no-file, tz-aware round-trip, writes scoped
  to `backtest/ticks/`, MT5 read degrades cleanly) with an injected synthetic
  day-fetch — no MT5 needed.

Selftest total: **297 steps** (was 296; +1 = step 297 "sim tick cache").

---

## BLOCKED — and exactly what unblocks each

### THE GATE (Part 1B/1C validation)
Needs: **(a)** real ticks for 2026-07-01..07-10 — run `python bot.py fetchticks
--from 2026-07-01 --to 2026-07-10` **on the VPS** (live MT5), commit
`backtest/ticks/*.parquet` + `manifest.json`; **(b)** the **200-position deal
export** (comment-labelled) that produced the MT5 truth table, so the sim's
per-anchor / rogue / fetcher output can be diffed against it to a stated tolerance.

### Part 1B — the simulator (design, not yet built)
The correct design (no strategy fork) is a **fake adapter behind the real
`LiveTrader._tick()` loop** driven by cached ticks — `LiveTrader` already has a
`paper` mode; the seam is `self.adapter` (`symbol_info_tick`, `place_market_order`,
`positions_get`, `history_deals_get`, `account_info`). The fake broker fills at
tick+spread, fires SL/TP on tick touch, stamps **real magics + `AUR_*` comments**
(so `pnl_report`'s classifier and `magic_day_net` work unchanged), and computes
equity incl. unrealized so `risk._check_kill_switch` and every `daystops` governor
fire exactly as live. This is a **large integration** and — critically — is
**unvalidatable without the real ticks above**, so it was not built blind.

### Part 2 — boost redesign (`boost_spec_v2`, default OFF)
Gated on the GATE passing **and** on `AUREON_boost_redesign_spec.md` (not in repo).
The R1–R8 summary is captured in the task; the `tstop`-with-`freeze=0` decision
(add `tstop_after_min` vs disable) and the F-B gating are ready to implement once
the sim can validate them. Not started (task: "do not build (2) before (1)
validates").

### Part 3 — variant runs (baseline / freeze=0 / boost_v2 / combined / +sl_dist 20)
Entirely data-bound (trapped-leg survival vs a \$20 stop, R7 fire count, 07-03
outlier). Blocked until 1B runs on real ticks.

---

## Recommended next step

Run on the VPS:
```
python bot.py fetchticks --from 2026-07-01 --to 2026-07-10
```
commit `backtest/ticks/` (parquet + manifest), and provide the deal export + the
boost-redesign spec. Then Part 1B/1C are built against real ticks, the gate is run
honestly, and — only if it passes — Parts 2–3 follow.
