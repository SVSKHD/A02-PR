# PRE_DEPLOY_CHECK — AUREON v3.0.0 (PR #3, branch A02-PR)

Independent re-verification before deploying to `C:\A02-PR\`. Every item below was
re-run against the actual branch tree and against `master` (2.9.8). **Result: all
checks PASS — clear to deploy.**

Branch = `claude/youthful-cerf-p9qrtx` (harness-pinned; the task's `A02-PR` name is
blocked from push in this environment). 4 commits: `7de826d` fix · `9545287` split
· `59d338e` Firebase · `5bc59dc` weekend.

---

## 1. Byte-identical proof — re-run vs 2.9.8 (`master`)

Each hunk extracted from `master` (2.9.8) and from the A02-PR module, CR-stripped
and dedented, then diffed:

| Hunk | 2.9.8 location | A02-PR location | Result |
|------|----------------|-----------------|--------|
| `update_position_on_bar` (+ `_ratchet`) | bot.py 202–314 | strategy.py | **IDENTICAL — 0 lines** |
| exit classifier (BE/LOCK4/TIER/Trail/SL/TP + slip) | live_trader.py `_reconcile` 1482–1770 | fills.py | **unchanged** (see below) |
| `_manage_trails_on_bar_close` incl. STOP-THROUGH | live_trader.py 1855–2058 | trails.py | **IDENTICAL** except 1 import line |
| `_flatten_all` | live_trader.py 2064–2134 | risk.py | **IDENTICAL — 0 lines** |

- `_manage_trails`: the only delta is the late import `from bot import update_position_on_bar` → `from strategy import update_position_on_bar` (an allowed import-line change; logic untouched, STOP-THROUGH block identical).
- **`_reconcile_with_broker` full diff vs 2.9.8 contains ONLY commit-1 Fix A (rescue twin-open guard) and Fix B (boost diagnostics).** The exit-classifier lines are byte-for-byte present in both: `outcome = 'TP'`, `outcome = 'SL'`, `outcome = 'BE'`, `LOCK4`, `'Trail'`, `slip_txt`, `FREEZE BREACH` — none changed.

**No logic delta outside commit 1 / commit 4 was found.** ✔

## 2. Commit-1 fix present & correct

Rescue classification (`fills.py`, in `_reconcile_with_broker`):
```python
_flag_hint = bool(info.get('rescue_on_fill'))
is_rescue = False
if getattr(self.cfg, 'no_oco', False):
    _sib = info.get('sibling_ticket')
    _twin_open = (_sib is not None and _sib in self.shadow_positions) or any(
        sp.get('anchor_label') == info['anchor_label'] and not sp.get('boost')
        for sp in self.shadow_positions.values())
    is_rescue = _twin_open
    if _twin_open and not _flag_hint:   self.tele.warn("… recovered structurally (twin still open) …")
    elif _flag_hint and not _twin_open: self.tele.warn("ℹ️ stale rescue flag IGNORED … twin already closed; running as a normal breakout leg (no boosts).")
```
- Guard requires the twin to be **currently open** — `_sib in self.shadow_positions` (which holds only OPEN positions; closed ones are popped earlier in the same function) — and **prefers the explicit `sibling_ticket`**, falling back to a non-boost open leg of the anchor. ✔

Boost path — a log/Telegram line on **every** exit, no silent path:
`… attempting BOOST{i}` (before send) → exception `❌ … EXCEPTION {repr}` → `❌ … result=None … last_error=…` → `❌ … rejected rc={rc} ({name}) comment=…` → success `✅⚡ … FILLED @ {price} (ticket {tk}) rc=… (name)`. ✔

## 3. state.json compatibility

Loaded (a) the in-repo `aureon_v2_state.json` and (b) a synthetic state carrying
**all** sacred keys, through the refactored `LiveTrader(paper=True)`:
```
keys read back: daily_pnl, day_start_equity, firebase_eod_date, kill_switch_locked,
                last_broker_date, processed_anchors_today,
                shadow_pendings_extended, shadow_positions_extended
shadow_positions_extended -> _pending_shadow_rehydrate  : OK
shadow_pendings_extended  -> _pending_pendings_rehydrate : OK (rescue_on_fill preserved)
live aureon_v2_state.json -> last_broker_date = 2026-05-30 : OK
```
No KeyError; same in-memory shape as 2.9.8. `firebase_eod_date` is a **new additive**
key (does not affect 2.9.8 rehydration). ✔

> ⚠ The in-repo `aureon_v2_state.json` is a stale sample (last_broker_date 2026-05-30,
> empty shadows). Before Monday, confirm the **actual** `C:\A02-PR\state.json` rehydrates
> — paste it and I'll load it. (The synthetic full-keys test already exercises the
> extended/rescue paths.)

## 4. Entrypoint contract (rule #8)

`watchdog.py:128` spawns `python bot.py <args>` — unchanged. `bot.py main()` dispatches:
`backtest` → `backtest.run_backtest`; `paper` → `run_live(cfg, paper=True)`; `live` →
`run_live(cfg, paper=False)`. `run_live` is late-imported from `live_trader` (avoids
the cycle) and defined at `live_trader.py:713`. A real `python bot.py backtest …` run
completed end-to-end. ✔

## 5. Banner / module receipt (exact text the human will see in Telegram)

```
🚀 *AUREON v3.0.0 PAPER starting*
Lot: `0.35` (auto\_lot=off)
Kill switch: `-3.0%`
Hold: `45m` | TSTOP: `fav<$1.00` | NoOCO: `True`
Ladder: `2.5>BE | 6>+4 | 10>peak-2` | Trail: `gap $2.00, arm $2.50`
SL/TP: `$18/$30` | Roles: `normal + RESCUE 2nd legs`
Defer waits: A1/A3=15s, A2/A4=30s | rc=-1 retries: 2 (15s, 30s)
v3.0.0: `rescue=twin-open guard` | `boost-diag v2` | `13-module split`
Modules (14): `utils config strategy mt5_adapter backtest state risk anchors fills trails journal live_trader bot firebase_journal`
FP\_ZERO\_MAX\_LOT: `None` (Pepperstone demo — no cap)
```
On LIVE the second word becomes `LIVE`. If the Telegram banner shows a different
version or a short module list, the deploy did not land — redeploy. ✔

## 6. Firebase fail-safe

- Both call sites in `journal.py` are `try/except Exception`-wrapped: `_firebase_save_daily` (139 `try` / 178 `except`) and `_firebase_weekly_reconcile` (185 `try` / 190 `except`) — a Firebase error cannot block the EOD flatten, trading, or startup. The module itself is additionally fail-safe (no firebase-admin / no key → logged + swallowed, `save_daily_journal` returns False).
- `firebase_key.json` is **not tracked** (`git ls-files` empty) and **git-ignored** (`git check-ignore` confirms). ✔

## 7. Weekend feature

- `wait_until_market_open()` is called from **startup** (`live_trader.py:542`) AND the **top of `_tick()`** (`:627`), gated by the cheap `_market_closed_now()` probe.
- `_touch_heartbeat()` is inside the sleep loop (`:474`) — watchdog continuity.
- `_save_state()` runs **before** sleeping (`:469`).
- `ensure_time_offset()` is **forced on wake** (`:488`) before any `get_m5_close`, then the offset is announced — the Jun-8 cold-start (0h misdetect → A1 miss) fix. ✔

## Untouched files (confirmed identical to 2.9.8)
`watchdog.py`, `telemetry.py`, `env_loader.py` — `git diff master HEAD` is empty.

---
**Verdict: PASS on all 7 items. Clear to deploy per DEPLOY_RUNBOOK.md.**
