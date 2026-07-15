# UNLOCK — Rogue T2 going live

The bot ships **inert**: `RogueT2Config.trading_unlocked = False`, so every order routes
through `broker._simulated_send()` and nothing reaches the broker. Going live is a
deliberate, reversible operator action. Do not automate this.

## The one line to flip

`rogue_t2/config.py`:

```python
trading_unlocked: bool = False   #  ->  True
```

Prefer overriding at construction rather than editing the default:

```python
cfg = RogueT2Config(lot=0.35, trading_unlocked=True)
```

Leave `lot`, entries (17.00), SL (2.60), T2 (12.00), trail (1.50/2.60/0.50), and the cap
formula **unchanged** — they are frozen spec.

## Pre-flight — startup assertions that MUST pass

`broker.startup_assertions()` refuses to run unless **all** hold (RuntimeError otherwise):

1. `account_info().margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`
2. `account_info().trade_expert` is True (Expert-Advisor trading allowed)
3. `terminal_info().trade_allowed` is True
4. Symbol `XAUUSD` is visible (auto-`symbol_select` attempted once)

Also confirm before unlocking:

- Correct **account** (start on demo). The daily cap at the launch lot 0.35 is **-612.50 USD**.
- Magic is **20260815** and no other bot uses it.
- `state.json` path is writable; a prior day's halt (if any) is intentional.
- Discord webhook (`ROGUE_T2_DISCORD_WEBHOOK`) set if you want alerts.

## Verify after unlock

- First phase arms a **real** OCO at A1±17 (buy-stop/sell-stop, magic 20260815).
- Discord shows the phase-start + any fills; `trades.csv` gains rows on exits.
- Foreign positions/orders on the account are untouched.

## Rollback (make it inert again)

1. Set `trading_unlocked = False` (or pass `trading_unlocked=False`) and restart.
2. Flatten anything this bot left open — **own magic only**:

```python
from rogue_t2.broker import MT5Broker
from rogue_t2.config import RogueT2Config
import MetaTrader5 as mt5; mt5.initialize()
b = MT5Broker(mt5, RogueT2Config(trading_unlocked=True))  # unlock ONLY to send the cleanup
b.cancel_own_pendings()   # cancels magic-20260815 pendings only
b.flatten_own()           # closes magic-20260815 positions only
```

Both operations are magic-filtered — they never touch the Aureon straddle, the rogue
rider, the fetcher, or manual trades. Re-lock (`trading_unlocked=False`) when done.
