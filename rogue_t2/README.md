# Rogue T2 Continuation V1

A self-contained, **magic-isolated** trading bot integrated into the a02-pr (AUREON)
repo. XAUUSD only. Reconstructed from the frozen spec (no `rogue_bot/` reference was
present in the repo); the strategy numbers below are **frozen** and must not be tuned —
the work here is the execution/hardening layer.

> **SAFETY:** ships **simulated** (`TRADING_UNLOCKED=False`). Every order routes through
> `broker._simulated_send()` — nothing reaches the broker until an operator follows
> [UNLOCK.md](./UNLOCK.md).

## Strategy (frozen)

| Item | Value |
|---|---|
| Symbol | XAUUSD |
| Magic | **20260815** (dedicated; distinct from anchors `20260522`, rogue rider `20260626`, fetcher `20260707`, warmup `9999998`) |
| Lot | **config parameter** (launch `0.35`) — never hardcoded |
| Entries | A1 ± **17.00** (OCO buy-stop / sell-stop) |
| SL | entry ∓ **2.60** (broker-side, from moment of entry) |
| T2 | T1 fill ± **12.00** (continuation, same SL rule) |
| Trail | activate **+1.50**, distance **2.60**, ratchet **0.50** (server-side) |
| Phases (IST) | 05:00–12:30 / 12:30–17:00 / 17:00–22:00 · **Monday phase 1 starts 06:00** |
| Daily cap | **(-700/0.40)·LOT** — at 0.35 → **-612.50 USD** |

**Cycle:** phase start → OCO at A1±17. First fill = T1 (cancels sibling, carries SL,
trails). After T1 fills, arm T2 (continuation stop; survives T1's exit; cancelled at
phase end). Re-arm when **flat and no resting pending** — unlimited per phase, **never a
third position**. Flatten own-magic at every phase boundary and 22:00.

**Daily cap:** realized (actual deal history incl. commission+swap, own magic only) +
unrealized. On breach: cancel own pendings, flatten own positions, **halt until next IST
day** (halt persists across restarts).

## Modules

| File | Role |
|---|---|
| `config.py` | Frozen spec + LOT; `daily_cap_usd()`; `TRADING_UNLOCKED=False` |
| `engine.py` | PURE decisions: phases/Monday, OCO/SL/T2 geometry, trailing, cap, PnL, idempotency keys |
| `broker.py` | MT5 execution: `_simulated_send` default, **magic-filtered** flatten/cancel/reads, tick consumption (`copy_ticks_from`), startup assertions, deal-history PnL |
| `statestore.py` | Atomic persist-on-change; config hash + git commit; halt keyed by IST day |
| `bot.py` | The loop: phases, guards, OCO, T1/T2, trailing, re-arm, reconcile, cap halt |
| `watchdog.py` | Crash-restart backoff, stale-heartbeat kill, 6-dirty cooldown, **state.json mtime advance** check |
| `notify.py` | Discord (guarded, no-op without webhook) — fills, exits, phase, halt, guard, restart, reconcile, daily summary |
| `ledger.py` | `trades.csv` writer |

## Multi-bot coexistence (shared XAUUSD account)

**All** destructive operations filter strictly by magic `20260815` — foreign-magic
positions/pendings and manual trades are never touched (see
`test_foreign_magic_survives_flatten_and_cancel`). The Aureon straddle bot's
`stale_leg_sweep` was updated in this PR to filter by the straddle magic `20260522`, so
it likewise never cancels this bot's orders (see
`test_foreign_magic_pending_survives_sweep`).

## trades.csv schema

Append-only, header once. One row per booked exit:

| column | meaning |
|---|---|
| `ts` | exit timestamp (IST) |
| `side` | BUY / SELL |
| `tag` | idempotency tag (`<day>#P<phase>#C<cycle>#<A1B\|A1S\|T2>`) |
| `lot` | position lot (from config) |
| `intended_price` | the stop trigger we placed |
| `actual_price` | fill price at the broker |
| `slippage` | `actual_price - intended_price` |
| `pnl` | realized USD (deal profit) |
| `commission` | broker commission |
| `swap` | swap |
| `day_pnl` | running realized day PnL (own magic) |
| `config_hash` | frozen-spec fingerprint |
| `git_commit` | short commit that produced the row |

## Run

```bash
# tests (MT5 mocked)
python -m pytest tests/test_rogue_t2.py -q
python tests/test_rogue_t2.py          # standalone runner

# live loop is gated by TRADING_UNLOCKED (default False = simulated).
# Wire RogueT2Bot(cfg, MT5Broker(mt5, cfg), StateStore(path, cfg), Notifier(), history_window)
# into your MT5 driver; see UNLOCK.md before ever setting trading_unlocked=True.
```
