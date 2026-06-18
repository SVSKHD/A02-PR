# AUREON backtest — tick-resolution monthly replay

A tick-resolution monthly backtester for the AUREON XAUUSD straddle bot. Its
whole point is **backtest == live**: it *imports the real live strategy
functions* and never reimplements or copy-pastes parallel logic.

## STANDING RULE

> Every new strategy feature must be added to BOTH live modules AND exercised by
> the backtest. The backtester imports live functions, so reused logic updates
> automatically — but any new config/threshold/branch must be verified to flow
> through backtest.py. A feature is not 'done' until backtest reflects it. This
> keeps backtest == live.

## How to run

```bash
python backtest/back_main.py 2026-05
```

Argument is the month as `YYYY-MM`. On the VPS (real MT5) this pulls real ticks;
in a sandbox with no MT5 it automatically falls back to a deterministic synthetic
tick stream and prints a loud warning that the numbers are illustrative.

## What's cached

Ticks for a month are cached under `backtest/ticks/`:

- `ticks_YYYY_MM.parquet` (preferred; needs `pyarrow`)
- `ticks_YYYY_MM.csv` (automatic fallback when `pyarrow` is unavailable)

If a cache file exists it is loaded and the fetch is skipped (prints `cache
hit`). Cached tick files are git-ignored; the `.gitkeep` placeholders keep the
`ticks/` and `results/` directories tracked.

Per-trade audit output (one row per leg, including role) is written to
`backtest/results/results_YYYY-MM.csv` (also git-ignored).

## Reused live functions (imported, never reimplemented)

- `config.Config` — all thresholds are sourced from here, never hardcoded.
- `strategy.Position`, `strategy.update_position_on_bar`, `strategy.realize_pnl_usd`
  — the per-bar SL/TP/45m-hold/BE-gate/profit-ladder engine, plus the boost
  breath-gap-$3.50 trail / $10 backstop / +$8 floor (boosts are `Position(boost=True)`).
- `utils.initial_sl`, `utils.initial_tp`, `utils.anchor_datetime_utc`,
  `utils.eod_datetime_utc` — straddle SL/TP geometry and anchor/EOD UTC time
  (broker = UTC+3 via `cfg.broker_tz_offset_hours`).
- `anchors.resolved_anchor_hm` — the Monday-only A1 cushion (Mondays A1 @ 03:30
  broker / 6:00 IST, else 02:30; A2/A3/A4 unchanged).
- `fills.is_rescue_fill` — the lone-leg rule (a No-OCO sibling fill is a RESCUE if
  the twin is still open OR the rescue-on-fill flag is set).
- `rescue_log._branch_for` — boost-event classification
  (`CRASH_WIN` / `WHIPSAW_LOSS` / `SCRATCH`, SCRATCH band `|net| < $50`).

`backtest.rule_sources()` returns the audited list and the selftest asserts
`backtest.update_position_on_bar is strategy.update_position_on_bar` (true module
identity, not a copy).

## Pipeline

1. **fetcher** — `fetch_month_ticks` (cache-first, MT5 chunked by day, broker
   epoch → UTC by subtracting `broker_tz_offset_hours`); `synthetic_month_ticks`
   (deterministic, seeded `year*100+month`) when MT5 is absent.
2. **backtest.run_month** — aggregates ticks → M1 OHLC from MID=(bid+ask)/2 plus
   per-bar mean spread, places the No-OCO straddle at each anchor, and feeds each
   leg bar-by-bar to the LIVE `update_position_on_bar`. Siblings → rescue legs +
   isolated boosts; each leg is its own `Position` managed independently (a
   boost's outcome never closes/modifies the rescue or original leg).
3. **back_main** — prints the day-by-day / per-anchor / boost / drawdown report
   and writes the per-trade audit CSV.

## Realism caveats

- The **REALISM-ADJUSTED net** subtracts `cfg.realism_haircut_dollars` (default
  $1000) from RAW to approximate live drag not modeled here (late fills, partial
  fills, requote slippage, weekend gaps). Month-level only.
- **Live drawdown is likely deeper** than the backtest's (the "late-fire tax":
  anchors place a touch late live). Use the **raw** DD plus margin for the funded
  5% trailing-DD check, not the optimistic backtest DD.
- Entry fills use the resting stop ± half the M1 bar's mean spread (BUY at the
  ask side, SELL at the bid side); boosts add one more half-spread of market
  slippage. These are modeling approximations, not guarantees of live fills.
- In the sandbox the ticks are **synthetic** (deterministic random walk around
  ~4300). They demonstrate the pipeline end-to-end; they are NOT real market
  data. Run on the VPS for numbers that mean anything.
