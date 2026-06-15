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

---

## 9. CodeAnt AI review findings (2026-06-13) — disposition

CodeAnt left 9 substantive findings. Each was validated against `master` (2.9.8)
to establish provenance, because **Rule #1** governs: code moved byte-identically
from 2.9.8 must NOT be changed in this split PR — pre-existing issues are documented
here, not fixed. Findings in *new* code (commits 1/3/4) are in scope.

### Fixed (in new code added by this PR)

| # | File | Finding | Fix |
|---|------|---------|-----|
| 1 | live_trader.py `wait_until_market_open` (commit 4) | Weekend sleep touched the heartbeat once per **300s**, but `watchdog.py` restarts a bot whose heartbeat is >`HEARTBEAT_STALE_SECONDS` (**180s**) old → weekend-long restart loop. **VALID.** | Sleep the 5-min re-check in **30s chunks**, touching the heartbeat each chunk (max age 30s ≪ 180s). Market still re-probed every 5 min. |
| 3 | journal.py `_firebase_save_daily` (commit 3) | Daily export keyed off `now_ist` (IST wall clock). EOD fires at broker 23:00 = ~01:30 IST, so it filtered for the **next** IST day and saved ~0 trades. **VALID.** | Key the export off the **`broker_date`** passed in (and match `date_ist` on it); anchor closes are intraday so `date_ist` == broker calendar date. |
| 8 | fills.py `_reconcile_with_broker` rescue guard (commit 1) | Twin-open test read `self.shadow_positions`, which still contains a twin that closed at the broker until the closure-cleanup loop runs **later in the same reconcile** → a same-cycle close+sibling-fill could fire phantom boosts. **VALID** (narrow window; the live Jun-12 bug was an hour-later fill, already handled). | Require the twin to be **broker-confirmed open** (`_sib in broker_pos_tickets`, built earlier in the same function) — strengthens the exact guard commit 1 adds. |

### NOT fixed — pre-existing 2.9.8 behavior, byte-identical (Rule #1 → documented, awaiting maintainer decision)

| # | File | Finding | Severity | Provenance |
|---|------|---------|----------|------------|
| 2 | state.py `_acquire_pid_lock` | Non-atomic PID lock (TOCTOU): two near-simultaneous starts can both pass the existence check and write the lock → duplicate instances. Fix = `O_CREAT|O_EXCL` / OS file lock. | Critical | byte-identical to `master` live_trader.py 287–313 |
| 4 | mt5_adapter.py `__init__` | `_detect_tick_time_offset()` returns `None` when no live feed (e.g. **closed market**); the startup log formats it with `:+.0f` → `TypeError`, crashing adapter init. **Blocks weekend cold-start** (see §10). | Critical | byte-identical to `master` bot.py 558–624 |
| 5 | trails.py `_manage_trails_on_bar_close` | `modify_position_sl()` returns an MT5 result object (truthy) on both success and rejection; `if not ok:` never fires on a broker rejection → SL drift goes unwarned. | Critical | byte-identical to `master` (trail manager proven identical, §3) |
| 6 | utils.py `m5_close_at` | Returns `near[0]` (earliest within ±5min), not the **nearest** bar — can pick the wrong anchor close on M5 gaps (backtest only). | Major | byte-identical to `master` bot.py 342–349 |
| 7 | mt5_adapter.py | Time-offset detection + `server_time_utc` hardcode `"XAUUSD"` instead of `cfg.symbol`; breaks if run on another instrument or XAUUSD unsubscribed. (Bot only trades XAUUSD today.) | Major | byte-identical to `master` adapter |
| 9 | backtest.py `run_backtest` | `walk.iloc[-1]` with an empty `walk` (fill on the last bar of the window) → `IndexError` aborts the backtest. | Critical | byte-identical to `master` bot.py (walk.iloc[-1] at master:432) |

These six are real but predate this PR; fixing them changes 2.9.8 behavior and is out
of scope for a behavior-frozen split. Recommend a **follow-up hardening PR**. The
maintainer was asked (PR thread) whether to pull any forward into this PR.

## 10. ⚠ Deploy-blocking interaction (finding #4 × commit 4)

Finding #4 (adapter init crashes when the offset can't be detected on a closed
market) **directly conflicts with the commit-4 weekend deploy plan**: `MT5Adapter()`
is constructed in `run_live` *before* `LiveTrader.run()` reaches the market-closed
wait, so a weekend `python bot.py paper` / live arming (DEPLOY_RUNBOOK Steps 4–5)
would crash at adapter init — never reaching the self-sleep. 2.9.8 has the same
limitation (it was presumably always started while the market was open).

Options for the maintainer (asked on the PR):
- **(a)** Fix #4 now: on `None`, log a warning instead of crashing and default the
  offset so startup proceeds into the weekend wait; commit 4 already forces a fresh
  `ensure_time_offset()` on Monday wake *before* any trading. Enables weekend deploy.
- **(b)** Leave #4 as pre-existing and **start the bot when the market is open**
  (Sunday 22:00 UTC pre-open or Monday), not over the closed weekend — adjust the
  runbook accordingly.

## Monday-wake + A1 hardening (eliminate the Jun-8 silent-miss)

Defense in depth around the wake → A1 path. **No strategy change**; version held
at 3.0.0. All guards are additive and gated to LIVE mode — paper/backtest run
unguarded so the frozen behavior and the byte-identical backtest are unaffected.

ELIMINATES (now impossible to occur *silently*):
- The Jun-8 silent miss: a 0h offset misdetect → `get_m5_close` queried the wrong
  M5 window → "no bars" → A1 placed nothing, silently. **Guard 1** validates the
  offset on wake against `cfg.EXPECTED_BROKER_OFFSET_HOURS` (+3h) and BLOCKS A1
  with a loud ⚠️ critical on mismatch. **Guard 2** refuses to place on an
  unvalidated offset and retries the M5 fetch, alerting on a final no-bars instead
  of swallowing it.
- Silent A1 placement failure: **Guard 3** confirms A1's resting BUY+SELL stops
  exist at the broker after a "successful" send, re-places a confirmed-missing leg
  once (only after two consecutive broker reads both miss it — avoids duplicating
  on a transient empty read), else fires a ⚠️ `placement INCOMPLETE` alert.

Does NOT prevent (outside code's reach) — but now ALERTS, not silent:
- VPS down at 02:00, OS reboot, broker-feed outage, power loss. **Guard 4** fires a
  repeating ⚠️ `WAKE FAILSAFE` alarm if the bot is still asleep past the
  expected weekly open (gold/FX ~Sun 22:00 UTC) + grace, until it wakes or the
  human intervenes. Recovery = watchdog relaunch + A2 fallback.

**Guard 5** posts a one-line `🔧 Ready: offset {x}h {validated/UNVALIDATED}
· next anchor … · state rehydrated {ok/fail}` on every startup/wake.

Honest residual risk: Guard 3's one-shot re-placement reads broker order state to
decide; a broker reporting an order list inconsistent with reality for >2s could in
principle cause a duplicate (mitigated by the two-read rule) or a missed re-place.
This is the first LIVE run of v3.0.0 — the guards make failures LOUD and
recoverable, the achievable form of certainty; Monday's run is still the validation
event.

## Offset detect: stale-tick consistency for the quiet Monday wake (no schedule change)

Mon 2026-06-15 proved the wake guards worked (A1 BLOCKED + CRITICAL, not silently
missed) but exposed the TRUE root cause: `_detect_tick_time_offset` requires an
ADVANCING tick feed (two reads 4s apart), and gold is near-dead pre-session at the
Monday wake — `feed not live (adv 0s/4s)` for 20 min, all validate attempts failed,
A1 blocked. The detector simply cannot measure a quiet feed.

Fix is ENTIRELY in the detector (no anchor timing change — A1 stays 02:30 every
day): a tiered `_detect_tick_time_offset`:
- **Tier 1** — the original live-feed measurement, now on a short ~20s budget so a
  quiet feed falls through fast.
- **Tier 2** — validate a single stale tick against the CONSTANT
  `cfg.EXPECTED_BROKER_OFFSET_HOURS` (+3h): accept only if the tick rounds to the
  expected offset AND sits within `STALE_TOL_S` (~10 min) of `utc+expected`.

Why it is safe:
- **Jun-8 (0h on a +3h broker):** the tick reads ~3h off expected → Tier 2 rounds
  to 0 ≠ 3 (and remainder ≫ tol) → REJECT → still blocked. Bug stays fixed.
- **Quiet Monday wake (tick present, ≈ utc+3h, feed not advancing):** Tier 2
  confirms +3h → A1 places at 02:30. This is the fix.
- **No data (tick None / time≤0):** None → block → alert. Correct.
- The whole-hour ambiguity ("could +3h actually be a 3h-stale 0h feed?") is bounded
  by `STALE_TOL_S`: a tick within ~10 min of `utc+3h` cannot also be `utc+0h` (that
  is 3h stale, far outside tolerance). Tier 2 cannot rubber-stamp a 0h broker as +3h.
- The expected offset is config, not hardcoded in logic; `EXPECTED_BROKER_OFFSET_HOURS
  = None` forces live-only detection for a different broker. Tier 2 feeds the SAME
  `offset_validated` flag, so the block-on-mismatch / failsafe / readiness guards are
  all retained. Supersedes the Monday-A1-time shift (that override becomes unnecessary).

## 2026-06-15 missed-anchor incident + Fix 1 (stale-tick retry at placement)

Today the offset detector worked (`offset 3.0h validated`), but the bot still
MISSED two anchors: at placement the latest tick was ~76s old (16s over the 60s
threshold) — a momentary MT5/broker stutter — and the code SKIPPED the whole
anchor. One of the misses was a clean ~$25 one-way gold drop (4332→4303) that the
A2/A3 straddles are designed to catch — a large missed TIER win.

**Fix 1 — retry instead of skip.** When the tick is stale at placement,
`_await_fresh_tick_for_placement` polls every `stale_retry_poll_s` (5s) for up to
`stale_retry_window_s` (90s); the straddle places the instant a fresh tick
(age ≤ `stale_tick_threshold_s`, 60s) appears (`placed after {x}s stale-tick
wait`). It nudges the feed each poll and calls `_attempt_mt5_reconnect` once
mid-window; kill switch / pause / EOD abort the wait (priority); heartbeat is kept
alive so the watchdog can't kill the bot mid-wait. It skips ONLY if the tick stays
stale the whole window (`skipped — stale tick after 90s of retries`). Same for all
anchors A1–A4. **Tradeoff:** the wait blocks the main tick loop (no position
management) for up to ~90s on a stale anchor; bounded and rare (anchors fire ≤4×/day,
and only on a blip).

**⚠️ DISCREPANCY for the human — anchor reference price.** The spec for Fix 1 says
the straddle must be placed off the captured ANCHOR price (the scheduled-time M5
close) and "MUST NOT be recomputed during retries … not the live price." But the
DEPLOYED code (v2.5.4) anchors on the CURRENT price at the moment of placement for
**every** placement (stale-retry or not) — it overwrites the captured M5-close
anchor with `current_price`. Fix 1 preserved that deployed behavior (it does not
silently change the straddle-anchoring strategy): the retry waits for a fresh tick,
then places off that fresh tick's price exactly as a normal placement would. If you
want the spec's fixed-anchor-price (straddle off the scheduled M5 close), that is a
separate strategy change to v2.5.4 — tell me and I'll do it; it is NOT in this commit.
