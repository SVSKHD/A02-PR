# AUREON v3.0.0 — Refactor Notes

**Branch:** `claude/aureon-v3-refactor-djhifj` (the session's designated dev branch;
the original prompt named `A01` — see *Deviations from prompt* below).
**Type:** behavior-frozen structural refactor of AUREON v2.9.8 "Astra Hawk".
**Rule #1:** zero logic edits. Moved functions/classes are byte-identical except
import paths. No trading behavior changes. Anything suspicious is *noted, not fixed*.

---

## Deviations from the prompt (read first)

1. **Branch name.** The prompt said branch `A01`; this session is hard-pinned to
   develop on `claude/aureon-v3-refactor-djhifj` and may not push elsewhere without
   explicit permission. All work is on that branch. The pre-refactor 2.9.8 monolith
   stays on `master` untouched (rollback path preserved).
2. **`firebase_journal.py` was NOT present** in the repo, despite the prompt saying it
   was "already written; integrate as-is, do not rewrite." It did not exist (confirmed
   by `git ls-files` and a filesystem scan). I therefore *wrote* a new, fail-safe
   `firebase_journal.py` from scratch. It is designed so that any failure (missing
   `firebase-admin`, no credentials, network error) is swallowed and never blocks
   trading or the EOD flatten. If a canonical version exists elsewhere, replace this
   file with it — the call sites only depend on the documented function names.
3. **Line endings.** The repo was entirely **LF** on arrival. Rule #2 requires CRLF on
   the Windows VPS. I write every new/rewritten file as CRLF and add a `.gitattributes`
   (`*.py text eol=crlf`) so the whole tree checks out CRLF on the VPS without
   hand-editing the files the prompt says to leave untouched (`watchdog.py`,
   `telemetry.py`, `env_loader.py`).

---

## Step 0 — Repo cleanup classification

### KEEP — live system (root)

| File | Why |
|---|---|
| `live_trader.py` | orchestrator (slimmed in this refactor) |
| `bot.py` | CLI entry + backtest mode (slimmed) |
| `watchdog.py` | process supervisor — **left untouched** |
| `version.py` | single source of truth for version/banner — bumped to 3.0.0 |
| `telemetry.py` | Telegram/console notification engine — **left untouched** |
| `env_loader.py` | `.env` loader — **left untouched** |
| `requirements.txt` | runtime deps |
| `.gitignore` / `.gitattributes` | repo hygiene (updated/added) |
| `.env.example` | config template (no secrets) |
| `aureon.service.example` | systemd unit template |

### KEEP — new modules created by this refactor (root)

`config.py`, `strategy.py`, `mt5_adapter.py`, `anchors.py`, `fills.py`, `trails.py`,
`risk.py`, `journal.py`, `state.py`, `firebase_journal.py` — see the module map below.

### KEEP — documentation (root)

`README.md`, `AUREON_V2_SPEC.md`, `AUREON_v2_STRATEGY_PROMPT.md`, `QUICK_START.md`,
`TELEGRAM_SETUP.md`, `AUTO_ANALYSIS.md`, `WHOLE_PACKAGE.md`, `commands.md`,
`document.md`.
*Flag:* `document.md` describes the v2.5 → v2.5 hardening and is stale relative to
2.9.8/3.0.0; kept for history, not authoritative.

### MOVE → `tools/` (analysis / research / diagnostics)

| File | Kind |
|---|---|
| `aureon_replay.py` | replay/backtest research |
| `tick_backtest.py` | tick-level backtester |
| `monthly_analysis.py` | monthly report generator |
| `auto_analyze.py` | auto analysis |
| `validate_25.py` | validation harness |
| `fetch_data.py` | data fetcher |
| `fetch_lab.py` | data fetcher (lab) |
| `export_ticks.py` | tick exporter |
| `f_m.py` | XAUUSD tick fetcher |
| `diagnosis.py` | MT5 diagnostics |
| `dos.py` | diagnostics/ops script |
| `probe_ts.py` | broker time-offset probe |
| `test_place.py` | manual order-placement probe (a `test_*` name but a useful live MT5 tool — moved, not deleted; **flagged**) |
| `strategy_template.py` | strategy reference/template |

*Flag:* these scripts import live modules (`env_loader`, `bot`, `telemetry`) by bare
name. Run them from the repo root (`python -m tools.<name>` or with the root on
`PYTHONPATH`); their internals were **not** modified.

### REMOVE (deleted on this branch — git history preserves everything)

| Path | Reason |
|---|---|
| `envtext.txt` | **LEAKED SECRET** — contains a live `AUREON_TELEGRAM_TOKEN` + chat id. Removed. **Rotate that bot token.** Superseded by `.env.example`. |
| `te.py` | 3-line scratch MT5 connectivity probe (dead) |
| `__pycache__/` (all `*.pyc`) | generated artifacts |

### UNTRACK (removed from git, kept on disk — already in `.gitignore`)

| Path | Reason |
|---|---|
| `aureon_v2_state.json`, `aureon_v2_state.json.bak` | live runtime state — belongs on the VPS, not git (kept on disk for the rehydration validation gate) |
| `run/` (`heartbeat`, `status.json`, `today_trades.csv`) | runtime IPC artifacts |
| `data/` (XAUUSD M1 CSVs) | large market data — VPS/research only |
| `results/` (monthly outputs) | generated analysis outputs |

---

## Module map (target structure)

| Module | Contents | Source |
|---|---|---|
| `config.py` | `Config` dataclass | from `bot.py` |
| `strategy.py` | `Position`, `update_position_on_bar`, `initial_sl/tp`, `realize_pnl_usd`, anchor/eod/m5 scheduling helpers — PURE logic, no I/O | from `bot.py` |
| `mt5_adapter.py` | `MT5Adapter` + `_MT5_RETCODE_MAP` | from `bot.py` |
| `state.py` | `StateMixin`: `_load_state`, `_save_state`, `_acquire_pid_lock`, `_release_pid_lock` | from `live_trader.py` |
| `risk.py` | `RiskMixin`: `_check_kill_switch`, `_ensure_day_start_equity`, `_flatten_all`, `_eod_reached` | from `live_trader.py` |
| `anchors.py` | `AnchorsMixin`: anchor scheduling, defer/retry state machine, gap-mode + in-flight breakout recovery, straddle placement, warmup/reconnect/diagnostic dump, `_extract_ticket` | from `live_trader.py` |
| `fills.py` | `FillsMixin`: `_reconcile_with_broker` (rehydration, fill detection, STRUCTURAL RESCUE, SL-RESCUE BOOST, close detection, exit classifier, FREEZE BREACH) | from `live_trader.py` |
| `trails.py` | `TrailsMixin`: `_manage_trails_on_bar_close` (ladder/trail, TSTOP, SL assert/drift heal, STOP-THROUGH, no-hold shadow) | from `live_trader.py` |
| `journal.py` | `JournalMixin`: `_write_journal`, `_send_daily_summary`, `_send_today_summary` + **NEW** Firebase EOD/weekly wiring | from `live_trader.py` + new |
| `live_trader.py` | slim `LiveTrader` orchestrator (inherits all mixins): `__init__`, IPC/status helpers, `run`, `_tick` + Firebase call sites + banner module receipt | rewritten |
| `bot.py` | CLI entry + backtest mode only (re-exports moved names) | rewritten |

**Design:** `LiveTrader` methods are split into **mixin classes**, one per module, and
`LiveTrader(StateMixin, RiskMixin, AnchorsMixin, FillsMixin, TrailsMixin, JournalMixin)`
inherits them. This keeps every moved method **byte-identical** (the `self.` references
and bodies are untouched; only the enclosing class name and the module-level imports
change). No circular imports: mixins import only stdlib + `mt5_adapter`/`strategy`/
`telemetry`/`firebase_journal`; none import `live_trader`.

---

## Suspicious findings (NOTED, NOT FIXED — per rule #1)

Found while moving code byte-identically. **None were changed.** All are
pre-existing in 2.9.8 and frozen for this refactor.

1. **`_write_journal` reads keys the shadow dict never sets.** `shadow.get('entry_time')`,
   `shadow.get('anchor_price', '')` and `shadow.get('lot', ...)` are used to fill the
   journal's `entry_time_ist`, `anchor_price` and `lot` columns, but the shadow-position
   dicts only ever store `fill_time`, `anchor_label`, (no `lot`). Net effect in live:
   the journal's `entry_time_ist` and `anchor_price` columns are always blank and `lot`
   always falls back to `cfg.lot_size`. The CSV *schema* (19 columns) is unchanged; only
   those cell values are empty. Consequence for the new Firebase records: `open_time` /
   `held_min` derive from `entry_time_ist`, so they will be `null` for real trades until
   this is fixed upstream. (Left as-is — behavior frozen.)
2. **`_compute_safe_lot` hardcodes contract size 100** (`max_loss / (sl_dist * 100)`)
   instead of `cfg.contract_size`. Harmless while XAUUSD stays at 100 oz/lot; a different
   instrument would mis-size. The inline comment already acknowledges the assumption.
3. **Leaked secret (handled in Step 0):** `envtext.txt` contained a live
   `AUREON_TELEGRAM_TOKEN` and chat id. Removed from the tree; **rotate that bot token**
   regardless, since it is in git history on `master`.
4. **`firebase_journal.py` did not exist** (see *Deviations*). The new fail-safe module
   is the author's, not a pre-validated drop-in.

## Validation gate outputs

All six gates run on this branch (MetaTrader5 stubbed for import on non-Windows; on the
VPS the real package is installed):

```
GATE 1  py_compile — config, strategy, mt5_adapter, state, risk, anchors, fills,
        trails, journal, bot, live_trader, firebase_journal, version, telemetry,
        env_loader, watchdog ......................................... COMPILE ALL OK
GATE 2  import live_trader, bot, anchors, fills, trails, risk, journal, state,
        mt5_adapter, strategy, config, firebase_journal .............. IMPORT ALL OK
GATE 3  paper-mode startup (fake adapter, closed market):
          banner shows "AUREON v3.0.0" .............................. PASS
          module receipt present .................................... PASS
          receipt = config, strategy, mt5_adapter, anchors, fills,
            trails, risk, journal, state, firebase_journal,
            telemetry, version, live_trader ......................... PASS
          idles cleanly on closed market ("Market closed ...") ...... PASS
GATE 4  byte-identical proof vs master (git diff of moved hunks):
          update_position_on_bar (bot.py -> strategy.py) ............ BYTE-IDENTICAL
          _reconcile_with_broker (-> fills.py) ...................... BYTE-IDENTICAL
            exit-classifier block ................................... BYTE-IDENTICAL
            SL-RESCUE BOOST block ................................... BYTE-IDENTICAL
          _manage_trails_on_bar_close (-> trails.py) FULL ........... BYTE-IDENTICAL
            STOP-THROUGH block ...................................... BYTE-IDENTICAL
GATE 5  state.json rehydration:
          existing aureon_v2_state.json loads, all keys intact ...... OK
          save/load round-trip preserves exact persisted key names:
            shadow_positions_extended{anchor_label,current_sl,
              entry_price,fill_time,max_fav,role,side} .............. OK
            shadow_pendings_extended{anchor_label,entry_price,
              rescue_on_fill,sibling_ticket,side} .................. OK
GATE 6  CRLF check on all refactor source ........................... ALL CRLF OK
```

**Extra behavior-frozen proof (not a required gate):** ran the *identical* backtest on
`master` (monolith) and on this branch over 2026-04-29..2026-05-31; both produced
**54 trades / $2,493.40 P&L** with byte-identical `trades.csv` and `stats.json`. The
strategy split is provably behavior-preserving end to end.

**Firebase wiring proof:** `make_trade_record` builds the full per-trade record
(ticket, sibling_ticket, role, side, lot, entry/exit price, open/close ISO, held_min,
exit_reason, slip, max_favorable, nohold_trail_exit, pnl); `save_daily_journal` and
`weekly_reconcile` return cleanly (no-op with a warning) when no credentials are present
— proving the fail-safe contract: Firebase never blocks trading or the flatten.

**Rollback:** `master` retains the untouched 2.9.8 monoliths.
