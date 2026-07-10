# Boost redesign — timezone section (authoritative notes, 2026-07-10)

Recorded here because `AUREON_boost_redesign_spec.md` is not yet committed. These
are the corrected §2/§3/§4 and the two "confirm" items the owner sent. Part 2
(`boost_spec_v2`) is NOT started until the gate passes on real ticks; these bind
the SIMULATOR now and the boost work later.

## §2 — motivating evidence, timestamps corrected to BROKER (server) time

From the MT5 deal export (broker = server time, UTC+3; IST = server + 2.5h):

- **07-10, SELL 4117.35** filled **03:28:38 server (05:58:38 IST)**.
- **Low 4108.88 at ~04:38 server** — **inside the 45-minute freeze window**. The
  leg was ≈ **+$300** at that low; the trail **could not act** (frozen).
- Price **round-tripped** and the leg **SL'd at 4127.79 (05:11:39 server)** for
  **−$365.40**.

This is the freeze-window failure the redesign targets: a leg that was solidly in
profit gives it all back and stops out because the trail is clock-frozen. (Pair
with the four-leg −$1,695.40 case: three BUYs at 4127.x while the trapped SELL sat
at 4117.35, price fell $18, all three died, the SELL round-tripped into its SL.)

## §3 — every simulated leg's comment MUST classify (build integrity)

Any simulated leg whose comment does not match a known `AUR_*` pattern is a
**BUILD ERROR**, not an "unknown" bucket. The simulator asserts that every emitted
OUT-deal comment classifies (`simulator.unclassified_comments` -> empty; enforced
by the gate and selftest 298). `pnl_report` still shows phantom `??` and `ext`
buckets that correspond to no real position; **the simulator must never create
their like** — a non-`AUR_*` comment means a wiring bug to fix, not a bucket to
absorb it.

## §4 — the gate passes ONLY on all-tick data with every feature wired

The gate passes ONLY when **(i)** every day in the range resolved to **TICK** —
never M1 — **and (ii)** every engine bucket matches the deal-export truth **to the
cent with ALL features wired**. "Rogue is off by \$X because rogue isn't
implemented" is **not an explanation; it is the gate failing.** On M1 data the
gate **HARD-REFUSES** (does not warn): intrabar wick order is invented, and **nine
of fifteen** July whipsaw events turn on it by margins of **cents**.

Implemented: `sim_gate.run_gate` sets `refused=True` (no verdict) when any day is
non-tick or any comment fails to classify; `render_gate` prints
**GATE HARD-REFUSED**; the CLI exits non-zero.

## Confirmed in the simulator

- **Cached ticks only, never a resampled bar.** `FakeMT5`/`simulator` replay the
  cached tick sequence verbatim (`simulator._ticks_from_frame`). A day that is
  only M1 is **REFUSED** (`is_tick_frame` false → recorded in `refused_days`),
  never interpolated into invented ticks. (selftest 298: `m1_refused`.)
- **The injected clock drives scheduling AND the broker-day roll**, not just tick
  timestamps. `pandas.Timestamp.now` is patched to the sim time, which
  `LiveTrader._tick` reads for `_broker_date` / `_reset_if_new_day` (the governor
  reset) and the anchor scheduler. A day-roll from the wall clock would silently
  break every governor reset; selftest 298 asserts `last_broker_date` equals the
  SIM day (`dayroll_sim_clock`), not today's wall-clock date.
