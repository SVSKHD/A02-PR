# boost_spec_v3 (2026-07-13) — branch record

Confirm gate + re-entry invalidation + trapped-leg cut. Layered **on top of**
`boost_spec_v2`'s band model (only has effect when `boost_spec_v2` is also ON).
Gated by **`boost_spec_v3_enabled` (DEFAULT ON on demo)** — with the flag OFF the
v2 immediate-fire path is **byte-identical** (selftest 308 asserts it).

## Why (the 2026-07-13 episode)

`boost_spec_v2` (PR #111) fires a boost on the **first tick** past its level
(band edge ± ladder offset). On 2026-07-13 that fired **B1 on a fake break**:
price hugged `band_hi` (4079.83) for ~70s at +$0.0–0.9, poked to 4081.92
(level +$1.09), faded, and **B1 stopped −$350**. The *real* cross of the same
level 20 min later would have made ~**+$230**. The day closed **−$561** instead
of ~+$200. v3 makes breaks prove themselves before entry, kills filled boosts
whose premise dies, and cuts the trapped anchor leg once a break is confirmed.

## What it does (ON) — the exact 4 changes

All of this lives in `boost_spec.py` (the PURE decision functions are shared by
the live driver, the offline simulator, and the selftest so they can never
drift). Everything is **READ from cfg**.

1. **CONFIRM GATE** — per-boost-level state machine, **B1/B2 fully independent**
   (`IDLE → ARMED → FIRE`, `_v3_confirm_step`):
   - **ARMED** on the first tick that reaches the level. Records `t0` and the
     running **session extreme** since arming (max for an UP break, min for a
     SELL/DOWN break — fully mirrored).
   - **RESET** on any single tick back across the level → that level returns to
     `IDLE`, its `t0`/extreme cleared. **Per-level only** — a sibling's ARMED
     state is never touched (selftest 306).
   - **FIRE** once `elapsed ≥ boost_confirm_dwell_s` **AND** the session extreme
     has cleared `level ± boost_confirm_ext`. Entry is then market, **exactly as
     v2** (`_place_spec_boost` — same lot, same $10 backstop SL construction,
     same `magic`/comment `AUR_A1_B_Bn`).
   - Levels can be ARMED **concurrently** (a fast run arms B1 then B2 while B1 is
     still dwelling) — this is correct.
2. **RE-ENTRY INVALIDATION** (`_v3_reentry_invalidation`) — any **filled** boost
   closes immediately at market when price crosses back inside the band
   `[band_lo, band_hi]`; it does **not** wait for its $10 SL. Emits
   `BOOST_INVALIDATED_REENTRY (ticket, pnl)`.
3. **TRAPPED-LEG CUT** (`_v3_trapped_cut`) — on the **first confirmed FIRE per
   anchor episode**, the trapped opposite anchor leg is closed at market **via
   the existing close path** (`adapter.close_position`, the same call
   `risk._flatten_all` and R7 use). **SAFETY (E-22/E-23 class):** the −$630
   per-engine hard loss stop and the account kill switch are neither read nor
   written here, so both remain **armed and un-bypassed** — the cut is *additive*
   to the hard stop, never a substitute. The trapped leg's **broker SL is left
   in place**, so if the cut order rejects/raises the leg is still protected and
   **R7 stays armed as the fallback**; only a *confirmed* cut sets `r7_done`
   (which is why a clean v3 episode shows **no `R7_CLOSE`**).
4. **TELEMETRY** — new PTRACE events (same pipe format as v2):
   `BOOST_CONFIRM_ARMED (level, side, t0)`,
   `BOOST_CONFIRM_FAILED reason=re_entered|no_extension elapsed=<s> hi=<price>`,
   `BOOST_INVALIDATED_REENTRY (ticket, pnl)`, `TRAPPED_CUT (ticket, pnl)`.
   ARMED / FIRE / FAILED / TRAPPED_CUT are **mirrored to Discord** in the
   existing `self.tele.info/warn` one-line style.
   - Side note: fixed a pre-existing `_pt` bug where a `ticket=` field collided
     with the tracer's 2nd positional arg and silently dropped the whole line to
     the log fallback — so v2's `BOOSTn_FIRED`/`RATCHET_ARMED` now also emit as
     structured PTRACE.

## Config (all new, default preserves today unless the flag is set)

```
boost_spec_v3_enabled = True     # confirm gate + re-entry invalidation + trapped cut
boost_confirm_dwell_s = 12.0     # a break must hold >= this many seconds past its level
boost_confirm_ext     = 1.50     # AND extend >= this far past the level before entry
```

Plain `Config` bool/floats, so the dynamic preflight banner
(`boost_metrics.all_bool_flags`) picks up `boost_spec_v3_enabled` automatically.

## Lands in BOTH the live engine AND the offline simulator (same PR)

`boost_spec_tick` is called from `fills._check_boost_triggers` on the live tick
loop **and** from the offline simulator's real `trader._tick()` (the simulator
drives the actual `LiveTrader`, MT5 disconnected). There is **one** code path;
no strategy fork. The older vectorised `backtest.py` predates boost_spec and is
untouched.

## Selftest (new steps 306–308; 300/301/305 pinned to `boost_spec_v3_enabled=False`)

- **306** confirm gate: pure mirror rules (reached/reset/extension/fire, UP+DOWN);
  a fake break that never extends → `BOOST_CONFIRM_FAILED reason=no_extension`,
  no fire; a real break that re-crosses, dwells and extends → FIRES; **per-level
  reset isolation** (reset B1 leaves an ARMED B2 with the same `t0`).
- **307** re-entry + cut: the first fire cuts the trapped leg (`TRAPPED_CUT`,
  booked ~**−$444.5** vs its −$630 SL) and **suppresses R7**; a filled boost that
  re-enters the band is invalidated at market; DOWN-break mirror cuts the trapped
  BUY.
- **308** cut safety: a **rejected** cut leaves the trapped leg **open on its
  unchanged broker SL**, emits **no** `TRAPPED_CUT`, and leaves **R7 armed** (the
  −$630 hard stop + kill switch are never bypassed); v3 OFF → v2 immediate-fire
  byte-identical.

## Acceptance (replay 2026-07-13 through the sim gate)

The behavioural targets — fake B1 at 04:09 fails `no_extension` (hi 4081.92 <
4082.33); real B1 fires ~04:31:40 on the second cross of 4080.83; B2 within ~15s
of its 04:32:05 fill; trapped SELL 57495734965 cut at ~−$440 (not the −$630 SL);
no `R7_CLOSE`; episode P&L ≈ +$200 — are encoded as **mechanism** assertions in
306–308 (the `no_extension`/`+1.50` extension math, the second-cross re-arm, the
~−$440 cut, R7 suppression, the cut-reject SL safety all reproduce). The
**cent-level real-tick replay is NOT run offline**: the 07-13 all-tick cache is
not committed and `sim_config` does not enable the flag in the July as-traded
timeline, so — exactly as for v2 — the sim gate must be re-run on the VPS
all-tick cache with `boost_spec_v2=boost_spec_v3_enabled=True` to confirm the P&L
number before the flag is trusted live.

## OUT OF SCOPE (deliberately not added)

Volume / tick-rate anything (no logging either), lot scaling, ladder-spacing
changes, B4+, pullback entries. The hard loss stop logic, `promote_on_boot`
handling, and `pnl_source` are untouched.

## KNOWN FOLLOW-UP (not implemented here)

If live PTRACE shows **real** breaks false-resetting on one-tick spread jitter,
soften the reset from a single tick to **2 consecutive ticks** back across the
level. Not implemented in this PR — flagged for the first live PTRACE review.
