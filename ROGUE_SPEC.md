# ROGUE_SPEC — what ROGUE actually does

Read-only audit. No behaviour, config, or defaults were changed. Every claim below
cites the actual line (`file:line`) it comes from.

## 0. Orientation — which code is live

`live_trader._tick` calls `rogue.drive(...)` (`live_trader.py:2663`, and the post‑EOD
trail‑only call `live_trader.py:2565`). `rogue.drive()` immediately delegates to the
**Monster engine**:

```
rogue.py:1065-1066   import rogue_monster_live as _rml
                     _rml.drive_monster(trader, st, allow_new_entries=...)
```

So the **only live Rogue code path is**:

- `rogue_monster_live.py` — the live broker adapter (`drive_monster`, magic 20260626).
- `rogue_monster.py` — the pure decision core it imports (`gate_eval`, `arm_side`,
  `bias_of`, `entry_level`, `init_sl`, `chain_level`, `trail_target`, the guard
  predicates). Note: the `MonsterEngine` *class* in this file is the **backtest oracle**;
  live uses only its module‑level pure functions.

**Everything else in `rogue.py` is dead relative to the live path.** `rogue._drive_a1`
(`rogue.py:840`) has no caller outside `selftest.py`/tests; `rogue_stop.drive_stop`
(`rogue_stop.py:829`) has no caller at all. The legacy detector/entry/trail/governor
cores in `rogue.py` (`detect_monster`, `entry_decision`, `trail_gap`, `can_enter`,
`a1_entry_decision`, `runaway_*`, `chain_entry_allowed`, `resolve_seed`, …) are not
reached. A handful of `rogue.py` **helpers** are still live because
`rogue_monster_live` calls back into them: `_broker_day_range` (`rogue.py:302`),
`_rogue_close_pnl` (`rogue.py:1385`), `cancel_pendings` (`rogue.py:1626`),
`promote_on_boot` (`rogue.py:343`), `eod_flatten` (`rogue.py:1543`).

**Magic:** `ROGUE_MAGIC = 20260626` (`rogue.py:24`, `rogue_monster_live.py:39`). This is
the magic to filter trade history by. (20260811 is the separate `aureon_new_non_oco`
engine — `config.py:238` family — not ROGUE.)

---

## A1. Arming

### The arming predicate — OR, not AND

The gate is evaluated in `rogue_monster.gate_eval` (`rogue_monster.py:270-292`) and driven
live from `rogue_monster_live._maybe_arm` (`rogue_monster_live.py:531-606`).

The three conditions are **ORed** (any one arms), with a **label priority** — ATR wins the
reason string, then velocity, then box:

```
rogue_monster.py:281   if not np.isnan(a) and (last.high - last.low) > eff_atr_mult * a:
                            gate_hit = f"ATRx {(last.high-last.low)/a:.2f}"
rogue_monster.py:283-286   if len(vel_window_m1) >= 2:
                            vel = abs(vel_window_m1.close.iloc[-1] - vel_window_m1.close.iloc[0])
                            if vel >= cfg.vel_points:
                                gate_hit = gate_hit or f"VEL {vel:.1f}p/{cfg.vel_minutes}m"
rogue_monster.py:287-291   bx = m5_closed.iloc[-(cfg.box_bars + 1):-1]
                            if len(bx) == cfg.box_bars and (bx.high.max() - bx.low.min()) <= cfg.box_max_range:
                                box = (bx.low.min(), bx.high.max())
                                if px_c > box[1] or px_c < box[0]:
                                    gate_hit = gate_hit or f"BOX break {box[0]:.1f}-{box[1]:.1f}"
```

`gate_hit` is a truthy reason string if **any** branch fires. `gate_hit or …` only
governs which reason gets *reported*; it does not AND the conditions. So: **ATR‑expansion
OR M1‑velocity OR box‑breakout.** None gates another. There is no scoring.

Whole gate is guarded by `len(m5_closed) >= cfg.box_bars + 1 and anchor is not None`
(`rogue_monster.py:278`) → arming needs ≥ `box_bars+1` = **13 closed M5 bars** and a seeded
anchor. The `box` tuple is returned whenever a tight box exists (used later for the entry
level) even when arming was actually triggered by ATR or velocity.

### Exact formulas

- **ATR‑expansion** (`rogue_monster.py:281`): on **M5** closed bars, the *current* bar's
  range `last.high - last.low` compared to `eff_atr_mult * ATR(atr_period)`.
  `atr_period` default **20** (`config.py:119`, `rogue_monster.py:56`). `eff_atr_mult`
  = `atr_mult (1.5) + red‑day extra_atr + (caution_atr_boost if in caution)`
  (`rogue_monster.py:209-211`, `rogue_monster_live.py:535`).
- **ATR is a SIMPLE mean of True Range, NOT Wilder** (`rogue_monster.py:135-139`):

  ```
  tr = max(H-L, |H-Cprev|, |L-Cprev|)
  return tr.rolling(n).mean()      # SMA of TR — not Wilder's RMA/EWM
  ```
- **M1‑velocity** (`rogue_monster.py:283-286`): timeframe **M1**; window is the last
  `vel_minutes` (default **5**, `config.py:121`) of M1 closes
  (`rogue_monster_live.py:541`: `vel_win = m1[m1.index > t - vel_minutes]`). Compares
  `|close[-1] - close[0]|` over that window to `vel_points` (default **12.0**,
  `config.py:120`). Absolute value → direction‑agnostic.
- **Consolidation box** (`rogue_monster.py:287-291`): the `box_bars` (default **12**,
  `config.py:122`) closed M5 bars *immediately before the current bar* (slice
  `iloc[-(box_bars+1):-1]`). **Box is valid** when the slice has exactly `box_bars` bars
  **and** `high.max() - low.min() <= box_max_range` (default **8.0** pts, `config.py:123`).
  A valid box **arms** only if the current close breaks out of it:
  `px_c > box_high or px_c < box_low`.

### `rogue_disarm_bars` hysteresis

Live in `rogue_monster_live._maybe_arm` (`rogue_monster_live.py:544-558`), only when a
pending entry stop is already resting and only on a new bar:

```
rogue_monster_live.py:544-555   if pendings:
    if new_bar:
        if not gate_hit:
            m["quiet_bars"] += 1
            if m["quiet_bars"] >= mcfg.disarm_bars:
                for tk in list(pendings):
                    trader.adapter.cancel_order(int(tk), ...)   # <-- cancels the resting order
                m["pend"] = None; m["quiet_bars"] = 0
                _log(..., "DISARM", ...)
        else:
            m["quiet_bars"] = 0
```

- `disarm_bars` default **6** (`config.py:124`, `rogue_monster.py:63`).
- Disarming **does cancel the resting pending stop order** (`cancel_order`,
  `rogue_monster_live.py:551`), not merely stop new ones. Any gate re‑hit resets the
  counter to 0 (`:557`).
- **Counter caveat:** `new_bar = str(t) != m["last_m1_ts"]` where `t = m1.index[-1]` is the
  last *closed **M1*** bar (`rogue_monster_live.py:653-655`). So the disarm counter
  advances **per M1 bar**, i.e. `disarm_bars=6` ≈ 6 minutes — despite the field being
  documented as "quiet **M5** bars" (`rogue_monster.py:29`; `_log(... quiet_m5=...)`
  `rogue_monster_live.py:555`). See Discrepancies.

### `rogue_asia_start_hour`

Gates **arming (and therefore entry) only** — anchor seed and re‑anchor are unaffected:

```
rogue_monster_live.py:566-568   if side and mcfg.asia_start_hour > 0 and pd.Timestamp(t).hour < mcfg.asia_start_hour:
                                    _log(trader, "ASIA", detail="arm suppressed", side=side); return
```
(mirror in the core, `rogue_monster.py:391-392`, comment: "anchor seed + re‑anchor
unchanged; the gate math above still ran for the log"). Default **7** (server hour,
`config.py:151`, `rogue_monster.py:101`). It suppresses the *arm* after the side is
computed; it does not touch `_ensure_anchor` or the sequence‑close re‑anchor.

### Fraction of M5 bars armed over the last 3 months — NOT MEASURED

**I could not measure this.** This audit environment is Linux with no MT5 terminal
(`MetaTrader5` does not import) and no cached M1/M5 history in the repo, so the gate cannot
be replayed against live bars here. I will not fabricate a number.

What the number *means* and how to produce it: the gate is an OR of three conditions
re‑evaluated every M1 bar (`gate_eval`), suppressed before server hour 7 and until 13 M5
bars exist. To measure, pull the account's M1 history for the window, resample to M5, and
run `rogue_monster.gate_eval` per M5 bar with the live `MonsterCfg` (from
`cfg_to_monster`), counting `gate_hit != ""`. A ready-to-run measurement harness:

```python
import pandas as pd, rogue_monster as rm
from rogue_monster_live import cfg_to_monster
mcfg = cfg_to_monster(cfg)                      # live values: atr_mult 1.5, vel 12/5m, box 12/8, asia 7
m5 = rm.resample(m1, "5min"); atr = rm.atr(m5, mcfg.atr_period)
armed = 0; total = 0
for i in range(mcfg.box_bars+1, len(m5)):
    t = m5.index[i]
    if mcfg.asia_start_hour and t.hour < mcfg.asia_start_hour:   # arming suppressed
        continue
    window = m5.iloc[:i+1]
    vel_win = m1[(m1.index <= t) & (m1.index > t - pd.Timedelta(minutes=mcfg.vel_minutes))]
    anchor = window.open.iloc[0]                # or the real 02:30 seed
    hit, _box = rm.gate_eval(window, atr.loc[:t].iloc[-1], vel_win, window.close.iloc[-1], anchor,
                             rm.effective_atr_mult(mcfg, 0.0, False), mcfg)
    total += 1; armed += bool(hit)
print(armed, total, armed/total)
```

If that fraction comes back anywhere near 50%, that is the single number most likely to
explain the backtest discrepancy: "arming" is not the rare event the config‑key‑name model
assumed — arming ≈ "any M5 range‑expansion / 12‑pt‑in‑5‑min move / 8‑pt‑box break," which
is common on XAUUSD. (This is exactly why the naïve blind‑ROGUE backtest that armed on every
bar over‑produced.) Number pending real data.

---

## A2. Entry

### Box‑edge path vs `rogue_fallback_trigger` path

`rogue_monster.entry_level` (`rogue_monster.py:232-237`):

```
if box:  return (box[1] + edge_offset) if LONG else (box[0] - edge_offset)   # box-edge path
return   (anchor + fallback_trigger) if LONG else (anchor - fallback_trigger) # fallback path
```

- **Box‑edge path** is used whenever a *valid tight box* exists (`box` truthy), regardless
  of which condition armed — the stop is placed 1 pt (`edge_offset`, `config.py:125`)
  beyond the box edge.
- **`rogue_fallback_trigger` path** (default **17.0**, `config.py:126`) is used **only when
  no valid box exists** — i.e. armed by ATR or velocity with no qualifying consolidation
  box. Then the stop is `anchor ± 17`.

### Pending stop orders, not market; one side only

Entries are **pending stop orders** — `BUY_STOP`/`SELL_STOP`
(`rogue_monster_live._place_stop`, `_TYPE_BUY_STOP=4`/`_TYPE_SELL_STOP=5`
`rogue_monster_live.py:44`, placed at `:276` `place_stop_order`). Only **one side at a
time** — the single armed side (`m["pend"]`, `rogue_monster_live.py:589`). It is **not** a
two‑sided straddle. A market order is used only as a *chase conversion* (see below), never
as the primary entry mechanism.

### How the side is chosen — bias lookbacks ARE used

`rogue_monster.arm_side` (`rogue_monster.py:295-304`):

```
mom = px_c - m1_close_upto.iloc[-min(5, m5_closed_len)]   # short-term M1 momentum
want = "LONG" if mom > 0 else "SHORT"
if bias in ("BOTH", want): side = want
```

Direction = sign of short‑term M1 momentum, **limited by H1+M15 bias**. `bias_of`
(`rogue_monster.py:142-154`) uses **both** lookbacks:

```
m_mom = m15.close[-1] - m15.close[-1 - bias_m15_lookback]   # bias_m15_lookback=8  (config.py:142)
h_mom = h1.close[-1]  - h1.close[-1 - bias_h1_lookback]     # bias_h1_lookback=4   (config.py:143)
LONG  if m_mom > 0 and h_mom >= 0
SHORT if m_mom < 0 and h_mom <= 0
else BOTH
```

So `rogue_bias_m15_lookback` and `rogue_bias_h1_lookback` are **live and used**. A `BOTH`
bias permits either side; a directional bias vetoes the opposing side.

### The seed / anchor — and `a1_time_snapshot`

The "seed" in the live engine is the **daily anchor**, set by
`rogue_monster_live._ensure_anchor` (`rogue_monster_live.py:362-383`):

```
seed_t = today 02:30 server (anchor_hour=2, anchor_minute=30; rogue_monster.py:85-86)
anchor = open of the first M1 bar at/after 02:30   (source SCHEDULED)
     or open of the earliest bar today if started late  (source CAPTURE_LATE)
```

- **`a1_time_snapshot` does NOT set the Monster seed.** `rogue_seed_fallback =
  "a1_time_snapshot"` (`config.py:277`) is read only by `rogue.resolve_seed`
  (`rogue.py:713`) inside the **dead** `_drive_a1` engine. On the live path the anchor is
  purely the 02:30 open; `a1_time_snapshot` is inert. (See Discrepancies.)
- **Persistence:** the anchor persists across the day and is **never re‑snapshotted** once
  stored (PR #121 — `_load_same_day` restores it on a same‑day restart,
  `rogue_monster_live.py:182-193`). It **is re‑captured** in one case: after a full sequence
  closes, the anchor **rolls to the close price** (`m["anchor"] = float(px)`,
  `rogue_monster_live.py:460`, "REANCHOR"). It resets next day at 02:30.

### `pending_chase_cap_pts` when price is already through the level

`rogue_monster_live._place_stop` (`rogue_monster_live.py:258-275`):

```
cap = getattr(cfg, "pending_chase_cap_pts", 3.0)          # config.py:155 -> 3.0
ok, through, _ = stop_preflight(...)                       # 'through' = pts price is past the level
if not ok:
    if 0.0 < through <= cap:  place_market_order(... same direction ...)  -> ("MARKET", ticket)
    return ("STALE", through)                              # beyond cap -> drop the arm
```

- If price is through the level by **≤ 3 pts**, the stop is **converted to a market order in
  the same direction** (chased).
- If through by **> 3 pts**, it returns `STALE`; the caller drops the arm and blocks that
  bar (`m["arm_blocked_bar"]`, `rogue_monster_live.py:598-602`) — no chase. This applies to
  both ENTRY and CHAIN stops.

### `rogue_candle_confirm` — implemented but inert

**Implemented, not a stub** — `candle_context` (`rogue_monster.py:191-200`) reads closed M5
bars: `detect_engulfing` (M5 real‑body engulfing, `:158-170`) then `detect_dragonfly`
(dragonfly doji, `:173-188`). Live hook in `_maybe_arm` (`rogue_monster_live.py:576-579`):
if `candle_confirm` and the candle context contradicts the armed side, the arm is dropped.

**But it is OFF on every live path:** `rogue_candle_confirm` default **False**
(`config.py:144`, `rogue_monster.py:92`) and `cfg_to_monster` maps it straight through
(`rogue_monster_live.py:82`). With it False the `if side and mcfg.candle_confirm:` guard is
never entered → the module is inert. It checks M5 engulfing/dragonfly when enabled; it is
not on any live path as shipped.

---

## A3. Chain

### `rogue_chain_step` measures from the last ENTRY (fill) price

`rogue_monster.chain_level` (`rogue_monster.py:245-247`):

```
return entry + chain_step if LONG else entry - chain_step   # chain_step=12 (config.py:107)
```

Called in `_reconcile` right after a fill is detected, with `entry` = the **just‑filled
position's entry price** (`rogue_monster_live.py:407` `lvl = rm.chain_level(side, entry,
mcfg)`, where `entry` is the fill from `:401`). So the next link is placed
**`chain_step` (12 pts) beyond the last fill price** — not the exit price, not the seed.

### The next link is placed IMMEDIATELY, not after the previous closes

On each fill, `_reconcile` immediately places the next chain stop as a resting order
(`rogue_monster_live.py:406-421`), while prior legs stay open. Multiple chain legs can be
open at once. Cap = `rogue_max_chains` (default **3**, `config.py:128`;
`if m["chains_in_seq"] < mcfg.max_chains`, `rogue_monster_live.py:406`).

### `rogue_chain_min_displacement` and `rogue_chain_cooldown_sec` — DEAD on the live path

Both are read **only** by `rogue.chain_entry_allowed` (`rogue.py:549-550`), which belongs to
the dead `_drive_a1` engine. The Monster chain has **no cooldown and no displacement gate** —
it re‑arms the chain stop unconditionally on fill. So on the live path these two keys
**gate nothing**. (`rogue_chain_min_displacement` also read at `rogue.py:1181`, likewise in
dead code.) See Discrepancies.

### Chained links are always the same side as link 0

`chain_level` uses the filled position's own `side`, and `_reconcile` places the chain stop
with that same `side` (`rogue_monster_live.py:407-411`). All chain links ride the **same
direction** as the entry. There is no counter‑side chaining.

---

## A4. Exits

### Initial SL — measured from ENTRY

`rogue_monster.init_sl` (`rogue_monster.py:240-242`):

```
return entry - sl_cap if LONG else entry + sl_cap          # sl_cap=10 (config.py:127)
```

`sl_cap` is measured **from the entry (fill) price**. Because the fill price of a stop order
equals its trigger level, this is equivalently "from the trigger level" — but it is **not**
measured from the box edge. Default 10 pts behind entry.

### `rogue_be_lock_arm` / `rogue_be_lock_floor` — the ratchet

`_manage_trails` (`rogue_monster_live.py:515-528`; core `rogue_monster.py:449-453`):

```
elif mcfg.be_lock_arm > 0 and p["peak"] >= mcfg.be_lock_arm:      # be_lock_arm=5 (config.py:148)
    be = rm.be_lock_target(side, entry, mcfg)                     # entry ± be_lock_floor (0) = breakeven
    better = (be > p["sl"]) if LONG else (be < p["sl"])
    if better: modify_position_sl(...); p["sl"] = be
```

- Fires once peak‑favourable ≥ `be_lock_arm` (**5**) **and before the trail arms** (peak <
  `trail_start`, per `be_engaged` `rogue_monster.py:262-266`). It locks the stop to
  `entry ± be_lock_floor`; `be_lock_floor` default **0.0** (`config.py:149`) = exact
  breakeven.
- **The stop can never move backwards.** It only moves on the `better` test (LONG: new SL >
  old; SHORT: new SL < old) — a monotonic ratchet. Same for the trail. A stop‑out while the
  BE lock is engaged is classified `BE` (scratch), not a full `SL`
  (`rogue_monster_live.py:435`, `rogue_monster.py:458-459`).

### Trail — peak‑favourable, gap from the peak

`_manage_trails` (`rogue_monster_live.py:502-514`) and `trail_target`
(`rogue_monster.py:250-253`):

```
fav = (px - entry) if LONG else (entry - px)
p["peak"] = max(p["peak"], fav)                              # peak = max favourable excursion (MFE)
if p["peak"] >= trail_start:                                 # trail_start=10 (config.py:129)
    tr = entry + peak - trail_gap  (LONG)                    # trail_gap=5 (config.py:130)
       = entry - peak + trail_gap  (SHORT)
```

- The trail arms off **peak‑favourable excursion**, not current profit (`p["peak"]` is a
  running max, `:503`).
- `trail_gap` is measured **from the peak** (`entry + peak − gap`), not from entry. So the
  stop trails 5 pts behind the best price seen. Ratcheted monotonically (`better` test
  `:506`).

### Stops are BROKER‑side

Initial SL is attached to the order at placement (`sl=sl` in `place_stop_order`
`rogue_monster_live.py:277`, and in the market‑chase `place_market_order` `:267`). Trail /
BE moves are pushed to the broker via `modify_position_sl` (`rogue_monster_live.py:509`,
`:522`). So the protective stop is a **real broker‑side SL on the position**; the engine
ratchets it in‑process each bar but the broker enforces it (a trail‑out is booked as
`DEAL_REASON_SL` in history). There is no in‑process "virtual stop" that the engine has to
be running to honour.

---

## A5. Governors — evaluation order per cycle

Per‑tick order in `drive_monster` (`rogue_monster_live.py:627-675`):

1. **New‑day rebuild + red‑day carry** (`:634-645`) — on a new broker day, if the prior day
   was red, seed `extra_atr = redday_atr_step`.
2. **`_governor(...)` FIRST** (`:663`), before reconcile/trails/arm.
3. `_reconcile` (`:667`), `_manage_trails` (`:668`).
4. **`_maybe_arm`** (`:669-670`, only when `allow_new_entries`) — where the per‑arm guards
   run.

`_governor` (`rogue_monster_live.py:609-624`) checks, in order:

| Order | Key | Threshold | What it does |
|---|---|---|---|
| 1 | `rogue_day_loss_halt` | `day_pnl <= -1000` (`config.py:131`) | **FLATTENS all** + halts the day (`_flatten_all`, `m["halted"]="GOV-LOSS"`, `:612,619`) |
| 2 | `rogue_profit_lock` | `day_pnl >= 1000` (`config.py:132`) | **FLATTENS all** + halts (`GOV-LOCK`, `:614`) |
| 3 | `rogue_day_profit_trail_start` / `_giveback` | peak ≥ 600 and `day_pnl <= peak − 300` (`config.py:137-138`; `giveback_halt` `rogue_monster.py:219-224`) | **FLATTENS all** + halts (`GOV-GIVEBACK`, `:616`) |

Once any of these three trip, `m["halted"]` is set and every subsequent tick returns early
(`rogue_monster_live.py:647-648`) — the day is done.

Then, inside `_maybe_arm`, the per‑arm brakes (these **block new arming/entries only** —
they never flatten):

| Key | Where | Effect |
|---|---|---|
| `rogue_consec_sl_limit` (2) | `_apply_close_guards` `:482-486` | On the *N*th straight full SL, set `caution_until = now + caution_cooldown_min` and turn caution on |
| `rogue_caution_cooldown_min` (90) | `_in_caution_cd` `:356-358`, checked `:560` | While in the caution window, **arming is blocked** |
| `rogue_caution_atr_boost` (0.5) | `effective_atr_mult` `rogue_monster.py:211` | Raises the ATR gate threshold while cautious → **harder to arm** |
| `rogue_side_fatigue_sl` (2) | `fatigue_blocks` `rogue_monster.py:214-216`, checked `:569` | A side with ≥2 SLs needs a real (non‑BOTH) bias, else that side's **arm is blocked** |
| `rogue_redday_atr_step` (0.5) | `drive_monster` `:640-642` | The day after a red day starts with a raised ATR gate → **harder to arm** |
| `rogue_flatten_at_eod` (True) | `rogue.eod_flatten` `rogue.py:1543-1568`, called `live_trader.py:2558` | At EOD, **flattens** the open Rogue leg (True) — else rides trail‑only with `allow_new_entries=False` (`live_trader.py:2564-2565`) |

**`rogue_max_entries` (10) — NOT ENFORCED LIVE.** It is mapped into `MonsterCfg`
(`rogue_monster_live.py:71`) but never compared anywhere in `rogue_monster_live.py`. Only
the *backtest core* enforces it (`rogue_monster.py:421` `self.entries < c.max_entries`, and
the "entry cap" halt `:500-501`). So on the live path there is **no per‑day entry cap** — new
sequences keep arming until a flatten governor or caution/fatigue brake stops them. See
Discrepancies.

Also note the caution/fatigue/asia guards' **order inside `_maybe_arm`** is: reanchor‑cooldown
& caution‑cooldown (`:560`) → asia block (`:566`) → side‑fatigue (`:569`) → caution+BOTH
(`:573`) → candle_confirm (`:576`).

---

## A6. Live vs dead config

### The two key families

| Key | Read by (file:line) | Live or dead |
|---|---|---|
| `rogue_enabled` | `rogue.py:82` (`should_run`), set in `rogue.py:354/357/360` (`promote_on_boot`) | **LIVE** (master switch) |
| `rogue_daywatch` | `rogue.py:1049` (`drive`) | **LIVE** (gate) |
| `rogue_lot` | `rogue_monster_live.py:54,212` | **LIVE** (order lot) |
| `rogue_atr_mult` | `rogue_monster_live.py:55` | **LIVE** |
| `rogue_atr_period` | `rogue_monster_live.py:56` | **LIVE** |
| `rogue_vel_points` | `rogue_monster_live.py:57` | **LIVE** |
| `rogue_vel_minutes` | `rogue_monster_live.py:58` | **LIVE** |
| `rogue_box_bars` | `rogue_monster_live.py:59` | **LIVE** |
| `rogue_box_max_range` | `rogue_monster_live.py:60` | **LIVE** |
| `rogue_disarm_bars` | `rogue_monster_live.py:61` | **LIVE** (counts M1 bars — see Discrepancies) |
| `rogue_edge_offset` | `rogue_monster_live.py:62` | **LIVE** |
| `rogue_fallback_trigger` | `rogue_monster_live.py:63` | **LIVE** (no‑box entry) |
| `rogue_sl_cap` | `rogue_monster_live.py:64` | **LIVE** (initial SL) |
| `rogue_chain_step` | `rogue_monster_live.py:65` | **LIVE** (chain spacing) |
| `rogue_max_chains` | `rogue_monster_live.py:66` | **LIVE** |
| `rogue_trail_start` | `rogue_monster_live.py:67` | **LIVE** |
| `rogue_trail_gap` | `rogue_monster_live.py:68` | **LIVE** |
| `rogue_day_loss_halt` | `rogue_monster_live.py:69` | **LIVE** (flatten) |
| `rogue_profit_lock` | `rogue_monster_live.py:70` | **LIVE** (flatten) |
| `rogue_max_entries` | `rogue_monster_live.py:71` (mapped only) | **DEAD live** (never enforced; only backtest `rogue_monster.py:421,500`) |
| `rogue_consec_sl_limit` | `rogue_monster_live.py:72` | **LIVE** (caution) |
| `rogue_caution_cooldown_min` | `rogue_monster_live.py:73` | **LIVE** |
| `rogue_caution_atr_boost` | `rogue_monster_live.py:74` | **LIVE** |
| `rogue_day_profit_trail_start` | `rogue_monster_live.py:75` | **LIVE** (giveback) |
| `rogue_day_profit_giveback` | `rogue_monster_live.py:76` | **LIVE** (giveback) |
| `rogue_redday_atr_step` | `rogue_monster_live.py:77` | **LIVE** |
| `rogue_side_fatigue_sl` | `rogue_monster_live.py:78` | **LIVE** |
| `rogue_reanchor_cooldown_s` | `rogue_monster_live.py:79` | **LIVE** |
| `rogue_bias_m15_lookback` | `rogue_monster_live.py:80` | **LIVE** |
| `rogue_bias_h1_lookback` | `rogue_monster_live.py:81` | **LIVE** |
| `rogue_candle_confirm` | `rogue_monster_live.py:82` | **LIVE‑but‑inert** (default False → module never runs) |
| `rogue_be_lock_arm` | `rogue_monster_live.py:83` | **LIVE** |
| `rogue_be_lock_floor` | `rogue_monster_live.py:84` | **LIVE** |
| `rogue_asia_start_hour` | `rogue_monster_live.py:85` | **LIVE** |
| `pending_chase_cap_pts` | `rogue_monster_live.py:258` | **LIVE** |
| `rogue_seed_fallback` | `live_trader.py:1631` (status string), `aureon_validator.py:121` | **DEAD for trading** (display/validation only; the live seed is the 02:30 open) |
| `rogue_daily_loss_stop` | `daystops.py:371` (status string), else dead `rogue.py`/`rogue_stop.py` | **DEAD for trading** (only renders `/daystops`; the live loss governor is `rogue_day_loss_halt`) |
| `rogue_daily_profit_stop` | `daystops.py:370` (status string), else dead | **DEAD for trading** (renders `/daystops`; live profit governor is `rogue_profit_lock`) |
| `rogue_trigger` (17.0) | *no reader anywhere* | **DEAD** |
| `rogue_stop_mode` (True) | only `rogue_stop.py:831` (dead engine) | **DEAD** |
| `rogue_stop_init_sl` (10.0) | *no reader anywhere* | **DEAD** |
| `rogue_init_sl` (10.0) | `rogue.py:129,440,502` (dead `_drive_a1`/legacy) | **DEAD** |
| `rogue_entry_confirm` (20.0) | `rogue.py:128` (dead) | **DEAD** |
| `rogue_entry_confirm_redesign` (12.0) | `rogue.py:439,1796` (dead) | **DEAD** |
| `rogue_trail_arm` (5.0) | `rogue.py:1370` (dead) | **DEAD** |
| `rogue_trail_gap_early`/`_deep`/`_widen_at` | `rogue.py:147-149` (dead `trail_gap`) | **DEAD** |
| `rogue_min_candles`/`_min_range`/`_body_mult` | `rogue.py:94-96` (dead `detect_monster`) | **DEAD** |
| `rogue_max_reentries_per_day` (10) | `rogue.py:172,1291,1356` (dead) | **DEAD** |
| `rogue_consecutive_fail_stop` (3) | `rogue.py:175,203,286` (dead) | **DEAD** |
| `rogue_reversal_dollars` (10.0) | `rogue.py:572` (dead) | **DEAD** |
| `rogue_chain_cooldown_sec` (300) | `rogue.py:549` (dead `chain_entry_allowed`) | **DEAD** |
| `rogue_chain_min_displacement` (6.0) | `rogue.py:550,1181` (dead) | **DEAD** |
| `rogue_chase_cap_dollars` (20.0) | `rogue.py:441,503,529,1160` (dead) | **DEAD** (live chase uses `pending_chase_cap_pts`) |
| `rogue_runaway_reanchor_enabled`/`_trigger`/`_confirm` | `rogue.py:468,470,501,805` (dead) | **DEAD** |
| `rogue_anchor_grace_min` (10.0) | `rogue_stop.py:657,793` (dead engine) | **DEAD** |
| `rogue_daily_soft_lock` (30.0) | *no reader anywhere* | **DEAD** |
| `rogue_rescue_cap_dollars` (13.0) | `rogue.py:1019` (dead) | **DEAD** |
| `rogue_model_gate_enabled` / `rogue_model_threshold` | `rogue.py:1214-1215` (dead A1 model hook) | **DEAD** |
| `rogue_a1_anchor_mode` (True) | `rogue.py:1499,1686` (dead engine + `rogueseed` gate) | **DEAD** (live Monster engine ignores it) |
| `seed_break_dollars` (10.0) | `rogue.py:909` (dead), `fetcher.py:559` (fetcher OFF), `aureon_validator.py:176` | **DEAD for rogue** |

### Where the running values (0.35 lot, enabled) actually come from

- **`rogue_lot`:** the running value is the **dataclass default `0.35`** (`config.py:117`).
  It is **not** overridden by any env var, profile file, or funded branch anywhere in
  non‑test code (`Config()` is built with no args at `bot.py:136`; the only lot override is
  the CLI `--lot` which sets the *general* `lot_size` at `bot.py:139`, not `rogue_lot`). The
  live‑order lot resolves via `getattr(cfg, "rogue_lot", getattr(cfg, "lot_size", 0.35))`
  (`rogue_monster_live.py:54,212`) → 0.35.
- **`rogue_enabled`:** the config default is **`True`** (`config.py:75`).
  `rogue.promote_on_boot` (`rogue.py:343-360`, called `live_trader.py:2239,2758`) then sets
  it per account type: **funded → forced False** (`:354`, mandatory gate), **demo → keeps
  the explicit `True`** (`:359-360`). So on a demo account Rogue boots **ON at 0.35**.
- There is **no env override, no profile file, and no second config** that feeds `rogue_lot`
  or `rogue_enabled`. The only gate is the demo/funded branch in `promote_on_boot`.
- **On the task's premise** ("the config in hand says `rogue_enabled = False` and
  `rogue_lot = 0.25`"): that does not match this repository. `config.py` ships
  `rogue_enabled = True` and `rogue_lot = 0.35`, and nothing in the code reads a `.env` or
  profile to change them (`.env.example` contains no `rogue_*` keys). If an operator is
  holding a config that shows `False`/`0.25`, **it is not the config this code path reads** —
  the live values come entirely from `config.py` defaults + the demo/funded promotion. This
  is itself a discrepancy (see below).

---

## Part B — trade export (`rogue_trades_export.py` / `rogue_trades_export.csv`)

- **Magic filtered:** ROGUE uses **`20260626`** (`rogue.py:24`, `rogue_monster_live.py:39`,
  `pnl_source.py:28`). (Not `20260811` — that is `aureon_new_non_oco`.) The script keys on
  this magic and, like the engine's own accounting (`pnl_source.magic_day_net(...,
  exclude_test=True)`), drops `TF_` testfire deals symmetrically.
- **Method:** `history_deals_get(from, to)` → filter to the ROGUE magic → group by
  `position_id` → one CSV row per **closed** position (a position with ≥1 OUT deal). Net =
  `sum(profit) over OUT deals + sum(commission) + sum(swap)` across all of the position's
  deals (ground‑truth per `pnl_source.deal_pnl`). Columns are exactly the requested schema.
- **Fields left empty by design (nothing guessed):**
  - `arm_reason` — **always empty**; the broker comment is only `AUR_ROGUE_E` /
    `AUR_ROGUE_C` (`rogue_monster_live.py:268,278`). The ATRx/VEL/BOX arm reason exists only
    in the local decision log (`rogue_monster_log`), not in broker history.
  - `chain_link` — `0` for an ENTRY leg (comment `..._E`); **empty** for a chained leg
    (comment `..._C`) because the link ordinal (1/2/3) is not encoded in the comment.
  - `exit_reason` — the **broker** deal reason (SL / TP / CLIENT / EXPERT / SO). A trail‑out
    is a broker SL modify, so the terminal books it as `SL`; "TRAIL" vs "initial SL" is not
    distinguishable from history.
  - `sl_at_open` — read from the opening order's stop (`history_orders_get`); empty if the
    order record is unavailable.
- **The CSV could NOT be populated in this audit environment.** This is a Linux sandbox with
  no MT5 terminal (`MetaTrader5` does not import) and no cached history, so
  `rogue_trades_export.csv` is delivered **header‑only**. Run `python rogue_trades_export.py`
  on the Windows MT5 terminal host to fill it and print the summary (trade count, date range,
  net, win rate, profit factor, avg win/loss, max drawdown, per‑month net, split by chain
  link). The script's pairing/summary logic was unit‑tested against synthetic deals and is
  correct; only the live data is missing here. No trades were reconstructed or estimated.

---

## Discrepancies

Facts where the code disagrees with the config comments, the key names, or the stated live
behaviour. **Reported only — nothing was changed.**

1. **`rogue_max_entries` is a dead governor live.** The config comment
   (`config.py:133`) and the A5 "entry cap" are only honoured by the backtest core
   (`rogue_monster.py:421,500`). `rogue_monster_live.py` maps the value (`:71`) but never
   checks it, so **the live engine has no per‑day entry cap.** A backtest that relies on the
   10‑entry cap to bound risk will diverge from live, which can keep opening sequences all
   day. (Bug — not fixed.)

2. **The `/daystops` panel shows loss/profit thresholds the engine does not enforce.**
   `daystops.py:370-371` renders `rogue_daily_profit_stop` (400) / `rogue_daily_loss_stop`
   (−370) for ROGUE, but the live governor actually uses `rogue_profit_lock` (1000) /
   `rogue_day_loss_halt` (−1000) (`rogue_monster_live.py:612-615`). The status readout is
   off by ~2.7× from the real halts.

3. **`rogue_flatten_at_eod` default contradicts its own docstrings.** Every docstring says
   "DEFAULT OFF" (`rogue.py:1544-1545`, `live_trader.py:2554`, and the `getattr(..., False)`
   fallbacks at `rogue.py:1549`, `live_trader.py:2564`), but the dataclass default is
   **`True`** (`config.py:90`). On shipped config the EOD **flatten** path fires and the
   trail‑only ride path (`live_trader.py:2565`) is dead.

4. **"quiet M5 bars" actually counts M1 bars.** `rogue_disarm_bars` is documented as quiet
   **M5** bars (`rogue_monster.py:29`; log key `quiet_m5`, `rogue_monster_live.py:555`) but
   the counter advances on each new **M1** close (`new_bar`, `rogue_monster_live.py:653-655`;
   increment `:547`). So `disarm_bars=6` disarms after ≈ 6 minutes, not ≈ 30 minutes. The
   backtest core has the same per‑M1‑bar behaviour under the "M5" name (`rogue_monster.py:414`).

5. **Config header comments describe the wrong engine.** The ROGUE config block header
   (`config.py:70-74`) says `rogue_stop_mode True = pending‑stop engine … the live design`
   and "Boot banner must read 'ROGUE IMPL: stop'". The live engine is neither — `rogue_impl`
   is a **constant `'monster'`** (`rogue.py:30-35`) and `rogue_stop_mode` has no live reader.
   The correct note is 40 lines lower (`config.py:110-116`). Legacy header is stale.

6. **A whole family of ROGUE keys is dead but still shipped** (see the A6 table): `rogue_trigger`,
   `rogue_stop_mode`, `rogue_stop_init_sl`, `rogue_init_sl`, `rogue_entry_confirm`,
   `rogue_entry_confirm_redesign`, `rogue_trail_arm`, `rogue_trail_gap_early/_deep/_widen_at`,
   `rogue_min_candles/_min_range/_body_mult`, `rogue_max_reentries_per_day`,
   `rogue_daily_loss_stop`, `rogue_daily_profit_stop`, `rogue_consecutive_fail_stop`,
   `rogue_reversal_dollars`, `rogue_chain_cooldown_sec`, `rogue_chain_min_displacement`,
   `rogue_chase_cap_dollars`, `rogue_runaway_*`, `rogue_anchor_grace_min`,
   `rogue_daily_soft_lock`, `rogue_rescue_cap_dollars`, `rogue_model_gate_enabled/_threshold`,
   `rogue_a1_anchor_mode`, `seed_break_dollars`. The config itself flags this
   (`config.py:115-116`: "legacy stop/band keys above are inert (dead‑key removal … tracked
   as follow‑up"). Anyone tuning these keys is tuning nothing.

7. **`rogue_candle_confirm` is implemented but never runnable as shipped.** The
   engulfing/dragonfly module (`rogue_monster.py:158-200`) is fully written and wired
   (`rogue_monster_live.py:576-579`) but gated by a default‑False flag (`config.py:144`), so
   it is dead weight on the live path.

8. **The task's stated config values don't match the code.** Premise: "config says
   `rogue_enabled = False` and `rogue_lot = 0.25`." The repo ships `rogue_enabled = True`
   (`config.py:75`) and `rogue_lot = 0.35` (`config.py:117`), and there is no `.env`/profile
   override path for either (Config is built argument‑free, `bot.py:136`). The live 0.35/ON
   behaviour is fully explained by `config.py` defaults + the demo branch of
   `promote_on_boot` — no hidden second config is involved.

9. **`a1_time_snapshot` / `rogue_seed_fallback` does not seed the live engine.** The comment
   and key name imply it sets the day's seed, but it is read only by the dead
   `rogue.resolve_seed` (`rogue.py:713`). The live Monster seed is unconditionally the 02:30
   server open (`rogue_monster_live.py:362-383`).
