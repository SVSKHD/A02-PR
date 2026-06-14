# HARDENING_NOTES — AUREON post-v3.0.0 review fixes

Follow-up to merged PR #3 (the v3.0.0 split). PR #3 landed the three review
findings that were in *new* code (heartbeat cadence, Firebase day-key, rescue
twin-open). This PR fixes the **six remaining CodeAnt findings**, which were
**pre-existing 2.9.8 behavior** carried through the byte-identical split and so
were held out of the split PR under its Rule #1 (documented in REFACTOR_NOTES §9).
The maintainer approved fixing all six here.

> **#4 + the runbook:** the #4 guard means a closed-market start no longer
> crashes, so the weekend deploy works. Even so, `DEPLOY_RUNBOOK.md` Steps 4–5
> still **recommend** starting at/after Sunday pre-open — with a live feed the
> offset detects immediately as `+3h`, which is the cleanest start; the #4 guard
> is the safety net for a closed-market start, not a license to skip the check.

Branch off `master` (post-merge). Each fix is minimal and localized; no module
boundaries change. All 14 modules import; a real backtest produces the **same**
result as before (`19 fills, total_usd -828.45`), confirming the edge-case fixes
don't move the happy path.

| # | Sev | File | Fix |
|---|-----|------|-----|
| 4 | Critical | mt5_adapter.py | **Adapter no longer crashes on a closed market.** `_detect_tick_time_offset()` returns `None` with no live feed (weekend cold-start); the startup log formatted it with `:+.0f` → `TypeError`, aborting `MT5Adapter()` before the weekend self-sleep was reached. Now logs a warning and proceeds; `server_time_utc` treats `None` as `0` for the coarse probe, and commit-4's `ensure_time_offset()` on Monday wake sets the real offset **before any trade**. Unblocks the weekend deploy. |
| 2 | Critical | state.py | **Atomic PID lock.** Replaced the `os.path.exists()`-then-`open('w')` TOCTOU with `os.open(O_CREAT\|O_EXCL\|O_WRONLY)`. On `EEXIST` the holder is inspected: a live AUREON process → refuse (same RuntimeError as before); a stale/foreign lock → removed and the create retried once. Two near-simultaneous starts can no longer both win. |
| 5 | Critical | trails.py | **SL re-assert failures are no longer silent.** `modify_position_sl()` returns an MT5 result object (truthy) on both success and rejection, so `if not ok:` never fired on a broker rejection. Now `ok` requires `retcode == 10009` (DONE) — or the `{'paper': True}` dry-run shim — so a real rejection takes the `⚠️ SL modify FAILED` warning path and SL drift is surfaced. |
| 9 | Critical | backtest.py | **Backtest no longer crashes on a last-bar fill.** When the fill lands on the final bar of the window, `walk` is empty and `walk.iloc[-1]` raised `IndexError`, aborting the whole run. Now falls back to the entry bar (`window.iloc[fi]`) for the EOD close. |
| 6 | Major | utils.py | **`m5_close_at` returns the NEAREST bar.** It returned `near[0]` (earliest within ±5min); when bars straddle the target it could pick the further one and skew the backtest anchor price. Now `min(near, key=abs(ix - target))`. |
| 7 | Major | mt5_adapter.py | **Time/offset probes use the configured symbol.** `_detect_tick_time_offset` and `server_time_utc` hardcoded `"XAUUSD"`. `MT5Adapter.__init__` now takes `symbol="XAUUSD"` (default preserves every existing caller) and `run_live` passes `cfg.symbol`. |

## Validation

```
py_compile (14 modules) ........................................ OK
import graph (MetaTrader5 mocked) .............................. OK (14)
#4  MT5Adapter() with _detect -> None: no crash; server_time_utc
    tolerates None ............................................ OK
#7  adapter probes self.symbol (e.g. XAGUSD) .................. OK
#2  1st acquire wins; foreign/stale lock taken; live AUREON
    holder -> RuntimeError refuse ............................. OK
#5  retcode 10009 -> ok; 10016 -> warn; None -> warn; paper -> ok  OK
#6  straddling bars -> nearest close returned ................. OK
#9  real backtest runs end-to-end (last-bar-fill guarded) ..... OK
    backtest output unchanged vs pre-fix: 19 fills, -828.45 ... OK
```

## Not deployed automatically
This PR is a **draft** for review. It is independent of Monday's v3.0.0
MERGE_GATE validation — merge/deploy it when you choose (recommended: after
v3.0.0 validates Monday, unless you want the #4 guard in before a weekend
cold-start).
