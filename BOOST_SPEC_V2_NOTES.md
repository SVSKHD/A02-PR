# boost_spec_v2 (D-31) — branch record

Flag-gated boost redesign. **`boost_spec_v2` defaults OFF; merging this changes
nothing until the flag is set.** Selftest 300 asserts the OFF path is
byte-identical (F-B still arms at $10 adverse).

## What it does (ON)

Replaces the F-B (`trapped_late_rescue`) + RALLY/RESCUE per-leg trigger with a
per-anchor **band** model (`boost_spec.py`, driven from
`fills._check_boost_triggers` when the flag is on — which also **gates F-B OFF**;
the F-B code is kept intact, D-5 stays in history):

- **R1** no boost inside the straddle band (between the two original fills), not
  even at the edge.
- **R2/R3** boost 1 fires `spec_break_dollars` ($1) past the band edge in the
  break direction; boost 2 `spec_boost2_gap` ($4) further, same direction.
- **R4** boosts **join the winning (break) side** — a downside break fires SELLs;
  they do NOT hedge the trapped leg (the inversion).
- **R5** each boost trails from entry, one-way ratchet, locks
  `spec_boost_min_lock` (+$1.50) minimum once reached, and **can never close
  negative** (its stop floors at breakeven until it locks).
- **R6** the trapped original dies at its SL, capped — the only permitted loser.
- **R7** when the trapped leg hits its SL, the entire winning side is closed near
  that level (one event).
- **R8** `freeze_minutes` is treated as **0** — the trail arms on PROFIT
  (`be_trigger` 2.50, guarded by `arm_buffer` 1.50 / `trail_gap` 2.00 /
  `max_tick_jump` 25.0), never on a clock.

Config (all new, default preserves today): `boost_spec_v2=False`,
`spec_break_dollars=1.00`, `spec_boost2_gap=4.00`, `spec_boost_min_lock=1.50`,
`spec_boost_sl_dollars=10.00`, `tstop_after_min=45`.

## Boost order geometry — the 2026-07-10 INVALID_STOPS fix (v3.9.0)

The first live spec boost (`AUR_A2_B_B1`, BUY @ 4114.76) was **rejected**
(`retcode=10016 INVALID_STOPS`) because the order was malformed two ways:

- **Opening SL was breakeven-at-mid** (`round(last_mid, 2)` ≈ 4114.52, only $0.24
  below a 4114.76 BUY entry) — inside the broker's minimum stop distance. The
  ratchet floor is a *trailing* lock, never the opening stop.
- **TP was `entry + $1000`** (5114.52) — a placeholder that was actually *sent* to
  the broker as the order's TP.

Fix (`boost_spec.py`, flag ON only):

- **Opening SL is now a REAL backstop**, `spec_boost_sl_dollars` ($10, defaults to
  the rescue backstop `boost_sl_dollars`) beyond entry, **validated against
  `symbol_info(symbol).trade_stops_level * point`** and widened to the broker
  minimum (with a log line) if it would land inside it. New pure helpers
  `spec_boost_sl` / `stops_min_dist` / `clear_stops_level` reuse the same
  `trade_stops_level` clamp the anchor/rogue/trails paths use — no reinvention.
- **No placeholder TP** — the broker order carries `tp=0.0` (no TP; the exit is
  governed by the ratchet / R7). The shadow keeps a wide *structural* `tp_level`
  only so the bar-close trail manager never reads it as a hit.
- **The +$1.50 ratchet is confirmed SEPARATE from the opening order.**
  `_ratchet_boost` now no-ops until the boost is `+spec_boost_min_lock` favorable
  (the opening backstop holds until then), and every stop it moves is likewise
  clamped to clear `trade_stops_level`. R5's "never close negative" is realized
  once the lock arms; the opening backstop is the capped worst case before that.

Selftest **305** (`boost_spec_v2 order stops`): drives the 07-10 BUY boost at
~4114.76 through a mock `order_send` that rejects INVALID_STOPS — asserts the new
SL is a valid `$10` backstop (not 4114.52), no TP is sent, the old geometry is
rejected while the new one is accepted, and the ratchet lock arms on the tick loop
at +$1.50 favorable (not at fill). **Total: 305 steps.**

## The tstop decision + rationale

**Chosen: ADD `tstop_after_min` (default 45), not disable tstop.**

`tstop_fav` (the dead-leg cut: market-close a leg whose best favorable excursion
never reached +$1) fired "at hold expiry" = `freeze_minutes` elapsed. With
`boost_spec_v2` ON the effective freeze is 0, so "hold expiry" no longer exists.
Disabling the tstop then would silently drop a distinct safety — the dead-leg cut
is independent of the freeze/trail arm; it exists to stop *riding a stagnant loser
all the way to its SL*, which is still desirable with freeze=0. So the tstop now
fires at `tstop_after_min` elapsed from entry. Default **45** preserves today's
timing exactly. It **never fires at t=0** (bound must be > 0 and elapsed ≥ bound;
`tstop_after_min=0` disables the bound). Flag OFF → the `freeze_minutes`
hold-expiry path is unchanged. (`trails.py`; selftest 302 asserts fires-once-at-45,
never-at-t0.)

## PTRACE line formats (every decision greppable)

Emitted via `ptrace.emit(event_type, ticket=None, anchor, **fields)` (falls back
to a `[BOOST_SPEC] <EVENT> anchor=… k=v …` log line if no tracer):

| event | fields |
|---|---|
| `BAND_ESTABLISHED` | `band_lo`, `band_hi` |
| `BREAK_CONFIRMED` | `direction` (DOWN/UP), `level`, `trapped` (ticket) |
| `BOOST1_FIRED` | `ticket`, `side`, `level` |
| `BOOST2_FIRED` | `ticket`, `side`, `level` |
| `RATCHET_ARMED` | `ticket`, `side`, `lock_level`, `max_fav` |
| `RATCHET_EXIT` | `ticket` (on the ratchet stop-out) |
| `R7_CLOSE` | `legs` (closed tickets), `trapped`, `winning_side` |
| `BOOST_SUPPRESSED_IN_BAND` | `side`, `adverse`, `band_lo`, `band_hi` — every tick F-B WOULD have fired ($10 adverse) but R1 blocked it; also fed to `util_pullback_log` |

`BOOST_SUPPRESSED_IN_BAND` is the one that matters most: it counts how often the
old behavior would have fired.

## Selftest

New steps **300** (off byte-identical + pure R1–R5 + driver in-band-zero / SELL
boosts / never-negative / R7-once), **301** (the exact 07-10 A1 tape: flag ON →
no boost at 4127.x, SELL boosts on the downside, four-leg total **+$1.05 vs the
−$1,695.40 disaster**), **302** (freeze=0 arms at be_trigger not the clock; tstop
fires once at 45m, never at t=0). **Total: 302 steps, 301 PASS, 0 FAIL** (1 env
WARN = Discord unconfigured).

## Visibility (v3.8.9 — display only, flag OFF byte-identical)

The flag was live but invisible on the boot banner. Fixed:
1. **Preflight flag list is now DYNAMIC** — `boost_metrics.all_bool_flags(cfg)`
   iterates every bool field on `Config`, so `boost_spec_v2` (and any future flag)
   appears in the ON/OFF banner automatically. No more stale hardcoded list.
2. **Loud boot block** when ON (`[BOOST-SPEC-V2] ACTIVE — …` + the R1/R2/R3/R5/F-B/
   freeze/tstop summary, all read from cfg): logged at INFO and posted once to
   Discord. Empty when OFF (byte-identical boot).
3. **Startup card** gains a `Boost mode: SPEC_V2` / `F-B` line where Ladder/Trail/
   SL-TP print.
4. **/status + /engines** show `Boost mode: SPEC_V2 · suppressed-in-band today: N`,
   where N counts `BOOST_SUPPRESSED_IN_BAND` events this broker day (reset at the
   day roll) — how many boosts the old F-B would have fired into the band.
5. **State-machine visibility**: `[BOOST-SPEC-V2] armed for A2 — awaiting fills`
   once per anchor when its straddle is pending, and `[BOOST-SPEC-V2]
   BAND_ESTABLISHED A2 lo=… hi=…` when the band forms — so a no-fill day is not
   silent. Selftest **304** covers all of the above (total was **304 steps**; the
   order-geometry fix below adds **305**, current total **305 steps**).

## WHAT REMAINS UNPROVEN

**This spec has never been validated against real tick data.** The simulator's
gate FAILED on its first run (A3 and A4 never place; FETCH off by $1,416; ROGUE by
$589), so no tick-level number exists. (The gate-fix branch `claude/sim-gate-fixes`
addresses those defects but has not re-run against the real cache.)

Every projection came from crude arithmetic with the "extreme" approximated by the
furthest close price, no spread, no slippage:
- +$609 on 07-10 (vs −$1,695 actual)
- +$8,576 across 15 July whipsaws (vs −$1,063 actual)
- 9 of 15 trapped legs "survived" a $20 stop — several by cents
- **R7 NEVER FIRED ONCE** in the crude sim — price stopped short of the trapped SL
  every time and the ratchet exited instead.

If R7 never fires on real data, the exit rule at the heart of this spec is
decorative and the +$1.50 ratchet is doing all the work. **That is worth knowing
before the flag is flipped in live trading.** The selftest 301 regression is a
hand-built tape, not real ticks — it proves the *mechanism* inverts correctly, not
that it is +EV. Do not flip `boost_spec_v2` live until the gate passes on the real
all-tick cache and R7's real fire-rate is measured.
