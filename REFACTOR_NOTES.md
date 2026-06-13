# AUREON v3.0.0 — Refactor Notes (branch work for `A02-PR`)

Four commits, in order:

1. **Commit 1 — behavior fix** (v2.9.9): stale rescue-flag bug (Fix A) + boost
   diagnostics (Fix B), on the *current* file structure.
2. **Commit 2 — structural split** (v3.0.0): the two oversized files broken into
   13 modules, behavior-frozen, moved code byte-identical.
3. **Commit 3 — Firebase EOD journal**: `firebase_journal.py` + wiring in
   `journal.py`, double-guarded.
4. **Commit 4 — weekend self-sleep + Monday auto-resume**: `wait_until_market_open`
   factored from startup into the main loop.

> **Operational notes for the reviewer**
> - **Branch.** The task names `A02-PR`; this session's harness is pinned to
>   `claude/youthful-cerf-p9qrtx` and forbids pushing elsewhere, so the work
>   landed there. Rename/cherry-pick at merge. No path is hardcoded except the
>   configurable Firebase key path, so the `C:\A02-PR\` deploy target is unaffected.
> - **Entrypoint (rule #8).** `watchdog.py` spawns **`python bot.py <mode>`**
>   (`watchdog.py:128`), not `live_trader.py`. So the real invariant is that the
>   `bot.py` CLI is unchanged. `run_live` moved into `live_trader.py`; `bot.py`
>   keeps `main()`/argparse and late-imports `run_live` for the live path, and
>   re-exports `Config`/`MT5Adapter`/`run_backtest`/`setup_logging`/strategy fns
>   so every `from bot import X` site (watchdog, analysis scripts) is unchanged.
> - **`firebase_journal.py` did not exist** in the repo (no firebase references
>   anywhere), despite the task describing it as "already written." Per the
>   maintainer's instruction it was **written to the documented spec**
>   (schema_version 2, `aureon_forex`, `make_trade_record`/`build_anchor`/
>   `save_daily_journal`/`weekly_reconcile`), internally fail-safe.
> - **Line endings.** Repo was stored **LF** in git. Per the maintainer's
>   instruction every delivered file is now **CRLF in-repo**. Read the commit-1
>   diff with `git diff --ignore-cr-at-eol`.

---

## 1. KEEP / MOVE / REMOVE file table

| File | Action | Contents |
|------|--------|----------|
| `version.py` | EDIT | `2.9.8 → 2.9.9` (fix) `→ 3.0.0`; history records fix, split, Firebase, weekend. |
| `utils.py` | **NEW** | `setup_logging`, `initial_sl`, `initial_tp`, `anchor_datetime_utc`, `eod_datetime_utc`, `m5_close_at`. stdlib+pandas only; `from __future__ import annotations` keeps the `Config` hints lazy so it imports **no** AUREON module (no cycles). |
| `config.py` | **NEW** | `Config` dataclass. |
| `strategy.py` | **NEW** | `Position`, `update_position_on_bar` (+ `_ratchet`), `realize_pnl_usd`. PURE, no I/O, byte-identical — most precious code. |
| `mt5_adapter.py` | **NEW** | `MT5Adapter` (all methods) + `_MT5_RETCODE_MAP`. The ONE module importing MetaTrader5 (lazy in `__init__`). |
| `backtest.py` | **NEW** | `run_backtest`, `summarize_backtest`. |
| `state.py` | **NEW** | `_load_state`, `_save_state` (atomic + `.bak`), `_acquire_pid_lock`, `_release_pid_lock`. |
| `risk.py` | **NEW** | `_compute_safe_lot`, `_check_kill_switch`, `_ensure_day_start_equity`, `_flatten_all`. |
| `anchors.py` | **NEW** | `_process_anchor_if_due/_process_anchor/_complete_deferred_anchor/_place_orders_for_anchor/_dump_mt5_state/_warmup_trade_channel/_attempt_mt5_reconnect/_extract_ticket`. |
| `fills.py` | **NEW** | `_reconcile_with_broker` (commit-1 twin-open guard + boost diagnostics). |
| `trails.py` | **NEW** | `_manage_trails_on_bar_close` (ladder/trail, TSTOP, SL heal, STOP-THROUGH). |
| `journal.py` | **NEW** | `_write_journal` (19-col) + `_send_daily_summary` + `_send_today_summary` + **commit-3 Firebase wiring**. |
| `firebase_journal.py` | **NEW** | Written to spec (was missing). Fail-safe Firestore journal. |
| `live_trader.py` | SLIM | `__init__`, day/loop helpers, `_eod_reached`, **`_market_closed_now` + `wait_until_market_open` (commit 4)**, `run`, `_tick`, `run_live`, the method-binding block + module receipt. |
| `bot.py` | SLIM + FACADE | CLI `main()`/argparse + backtest invocation; re-exports the old public surface. |
| `.gitignore` | EDIT | + `firebase_key.json`, `*.bak`, `state.json*`, `trades_*.csv`, `today_trades.csv`. |
| `requirements.txt` | EDIT | + `firebase-admin>=6.0` (optional off-VPS). |
| `watchdog.py`, `telemetry.py`, `env_loader.py` | UNCHANGED | Read for imports only (left LF — "do not modify" beats CRLF for these three). |

**REMOVED:** nothing — every line of behavior is preserved, relocated.

---

## 2. Module map (mechanism) and import graph

Moved `LiveTrader` methods become **module-level functions taking `self`**
(byte-identical bodies, dedented one level) and are **bound back onto the class**
in `live_trader.py` (e.g. `LiveTrader._reconcile_with_broker = fills._reconcile_with_broker`,
`LiveTrader._extract_ticket = staticmethod(anchors._extract_ticket)`). So every
`self.x()` call site, every `state.json` key, every Telegram string and the
19-col journal schema are unchanged.

**Note on the binding pattern vs the spec's import edges:** because cross-module
method calls go through the bound `self` (e.g. `fills` calls `self._write_journal`),
`fills` does **not** import `journal` — the call is resolved by the binding. This
keeps `_reconcile_with_broker` byte-identical (no `self.x` → `journal.x` rewrite)
and the graph is acyclic by construction (no module imports `live_trader`; the
late `from bot import …` sites were re-pointed to `strategy`/`utils`/`mt5_adapter`).

```
utils       → (stdlib, pandas)            config → (dataclasses, typing)
strategy    → config                       mt5_adapter → (logging, time, pandas, MetaTrader5)
backtest    → config, strategy, utils      state/risk/anchors/fills/trails/journal → mt5_adapter, telemetry, (stdlib, pandas)
journal     → firebase_journal (lazy)      live_trader → state,risk,anchors,fills,trails,journal,mt5_adapter,config,strategy,utils
bot         → config, strategy, mt5_adapter, backtest, utils  (+ late: live_trader.run_live)
watchdog    → bot (unchanged; spawns `python bot.py <mode>`)
```

`python -c "import live_trader, bot, anchors, fills, trails, risk, journal, state, mt5_adapter, strategy, config, utils, backtest, firebase_journal"` is clean (MetaTrader5 mocked off-VPS).

**Banner module receipt (rule #6):** `Modules (14): utils config strategy mt5_adapter backtest state risk anchors fills trails journal live_trader bot firebase_journal`.

---

## 3. Byte-identical proof

Generated against `HEAD` at the split commit (commit 1, post-fix), whitespace-
normalized to ignore the one-level dedent and CRLF. The four required hunks:

```
[update_position_on_bar]                                        -> IDENTICAL (0 differing lines)
[_reconcile_with_broker (exit classifier + STRUCTURAL RESCUE)]  -> IDENTICAL
[_manage_trails_on_bar_close (trail manager + STOP-THROUGH)]    -> IDENTICAL
```

Full per-symbol containment (each moved symbol found verbatim, dedented, in its
target module) — all **OK**:

```
utils.py    : setup_logging, initial_sl, initial_tp, anchor_datetime_utc, eod_datetime_utc, m5_close_at
config.py   : Config
strategy.py : Position, update_position_on_bar, realize_pnl_usd
mt5_adapter : _MT5_RETCODE_MAP, MT5Adapter (full class)
backtest.py : run_backtest, summarize_backtest
state.py    : _load_state, _save_state, _acquire_pid_lock, _release_pid_lock
risk.py     : _compute_safe_lot, _check_kill_switch, _ensure_day_start_equity, _flatten_all
anchors.py  : _process_anchor*, _place_orders_for_anchor, _warmup_trade_channel, _attempt_mt5_reconnect, _dump_mt5_state, _extract_ticket
fills.py    : _reconcile_with_broker
trails.py   : _manage_trails_on_bar_close   (ONE line differs: the late import
              `from bot import update_position_on_bar` → `from strategy import …`)
journal.py  : _write_journal, _send_daily_summary, _send_today_summary
```

`pyflakes` finds no undefined names in any moved body (the three `utils.py`
`Config`-in-annotation hits are lazy under `from __future__ import annotations`
and resolve at no runtime cost; `import utils` succeeds).

**Diffs that ARE expected (documented as intended, not violations):**
- `fills.py` rescue path + boost block — **commit 1** (Fix A & B), §4.
- `live_trader.py` startup market-closed block — **commit 4** extraction, §6.
- `mt5_adapter.place_market_order` — **commit 1** Fix B diagnostics.

---

## 4. Commit 1 (v2.9.9) — the two intentional fixes

### Fix A — stale rescue flag (the live bug)
A 2nd fill is a genuine RESCUE **only if its twin is still OPEN** at the moment of
the 2nd fill. `rescue_on_fill` was set when the first leg filled and never
re-checked against the twin later closing — Jun-12 A4: SELL banked +$477 and
closed, BUY filled an hour later, inherited the stale flag, was tagged RESCUE and
fired 2 boosts with no twin to rescue (A2, identical setup, fired nothing →
nondeterministic).

**Before:**
```python
is_rescue = bool(info.get('rescue_on_fill'))
if not is_rescue and getattr(self.cfg, 'no_oco', False):
    is_rescue = any(... non-boost open leg of anchor ...)
    if is_rescue: self.tele.warn("… recovered structurally (2nd fill of a live anchor) …")
```
**After (now in `fills.py`):**
```python
_flag_hint = bool(info.get('rescue_on_fill'))
is_rescue = False
if getattr(self.cfg, 'no_oco', False):
    _sib = info.get('sibling_ticket')
    _twin_open = (_sib is not None and _sib in self.shadow_positions) or any(
        sp.get('anchor_label') == info['anchor_label'] and not sp.get('boost')
        for sp in self.shadow_positions.values())
    is_rescue = _twin_open
    if _twin_open and not _flag_hint:  self.tele.warn("… recovered structurally (twin still open) …")
    elif _flag_hint and not _twin_open: self.tele.warn("ℹ️ stale rescue flag IGNORED … twin already closed; running as a normal breakout leg (no boosts).")
```
`shadow_positions` holds only OPEN positions, so membership **is** the "twin still
open" test; the explicit `sibling_ticket` is preferred when known.
- Jun-11 A4 genuine rescue (twin pinned/open) → still **rescue** ✓ (fix costs nothing).
- Jun-12 A4/A3 false rescues (twin closed) → now **normal**, no boosts ✓ (bug fixed).

### Fix B — boost-fill diagnostics
Every exit of the boost loop (`fills.py`) and of `place_market_order`
(`mt5_adapter.py`) self-reports: `attempting BOOST` before send, full `repr` on
exception, `mt5.last_error()` on `result=None`, `retcode + name + comment` on
reject, fill price + ticket on success. No order parameters changed. (The adapter
has no Telegram handle — it is the pure I/O layer — so the **Telegram** coverage
for boosts is layered on by the `fills` boost loop that wraps the call.)

---

## 5. Commit 3 — Firebase EOD journal
- **Daily:** in `_tick`, after the EOD flatten when the day's P&L is final,
  `_firebase_save_daily()` builds one `make_trade_record` per closed trade from
  today's `trades_YYYY-MM.csv`, groups with `build_anchor`, and makes **one**
  `save_daily_journal()` call. Guarded by `state['firebase_eod_date']` → once per
  broker day, never during anchor capture.
- **Weekly:** on closed-market (Sunday) **startup only**, `weekly_reconcile()`
  over the monthly CSVs backfills any missed day.
- **Fail-safe:** module is internally fail-safe **and** both call sites are
  `try/except`-guarded. Verified: no firebase-admin → logged + swallowed,
  `save_daily_journal` returns False, EOD/weekly never raise. With firebase-admin
  mocked: writes `aureon_forex/{day}` `schema_version=2`, correct aggregation;
  `weekly_reconcile` skips existing days and backfills missing ones.
- **Key:** `C:\A02-PR\firebase_key.json` (override `AUREON_FIREBASE_KEY`), git-ignored.

---

## 6. Commit 4 — weekend self-sleep + Monday auto-resume

The startup market-closed wait is factored into ONE reusable method
`wait_until_market_open(reason)` (on `LiveTrader`), called from **both** startup
and the top of `_tick()` (guarded by the cheap `_market_closed_now()` probe), so
one long-lived process spans the week and wakes itself Monday.

**Before** (startup-only inline block) → **After** (extracted + called from both).
This is the one place the startup block legitimately differs from 2.9.8.

Behavior contract, all verified in a simulated stale→fresh cycle (sleep mocked):
- **Enter weekend:** ONE Telegram line `💤 Weekend — market closed, sleeping, will
  auto-resume Monday. Next anchor A1 02:00 broker.` (announce-once; the sleep loop
  blocks here until Monday). **`state.json` saved before sleeping** (mid-weekend
  reboot rehydrates and re-enters the wait).
- **All weekend:** quiet 5-min re-checks; **heartbeat touched every iteration** so
  the watchdog never kills a sleeping bot.
- **Monday wake:** ticks fresh (<60s) → exit → **`ensure_time_offset()` forces a
  broker offset re-detect BEFORE any data call** (Jun-8 cold-start A1-miss fix) →
  ONE Telegram line `📈 Market open — resuming. Week starting. Broker time offset
  re-detected: +3h.` Then the normal loop runs; `_reset_if_new_day` fires on the
  Monday tick via `broker_date`, and A1 02:00 is placed by the normal anchor path
  (no weekend catch-up). The clock-drift (>2min) abort is preserved at startup.

---

## 7. Validation-gate outputs

```
GATE 1  py_compile (14 .py modules) ........................... ALL OK
GATE 2  import live_trader,bot,anchors,fills,trails,risk,journal,state,
        mt5_adapter,strategy,config,utils,backtest,firebase_journal
        (MetaTrader5 mocked off-VPS) .......................... ALL 14 OK
GATE 3  paper startup: version=3.0.0, banner prints 14-module receipt OK
GATE 4  live aureon_v2_state.json loads + rehydrates through the
        refactored LiveTrader ................................ OK
GATE 5  CRLF on every delivered file ........................... OK (0 bare LF)
GATE 6  strings present: "twin still open", "attempting BOOST",
        "STRUCTURAL RESCUE", "save_daily_journal", "weekly_reconcile",
        "wait_until_market_open", "Weekend", "auto-resume" ... OK
EXTRA   pyflakes: no undefined names (utils Config hints lazy) . OK
EXTRA   real backtest runs end-to-end: bot → backtest → strategy → utils  OK
EXTRA   Firebase fail-safe + happy-path (mocked) ............... OK
EXTRA   weekend sleep/wake simulated: entry-once, heartbeat kept,
        state saved, offset re-detect, resume-once ............ OK
```

## 8. Rollback
The pre-PR 2.9.8 files remain on `master` untouched. If Monday looks wrong,
redeploy those to `C:\A02-PR\`.
