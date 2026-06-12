"""
AUREON adapter diagnostic — exercise bot.py's own MT5Adapter, not raw mt5.

Run from the bot's PROD directory (where bot.py lives):
    python diag_bot_adapter.py

Tests, in order:
  0. CLEANUP — close any orphan DIAG positions from previous diagnostic
  1. Import bot.py's MT5Adapter and Config, initialize as the bot does
  2. adapter.place_stop_order — exact same call the bot makes at anchor time
  3. Raw mt5.order_send with same params for comparison
  4. Hammer test — 10 rapid place+cancel cycles to expose state corruption
  5. Cleanup
"""
import sys, os, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import MetaTrader5 as mt5

print("=" * 75)
print("AUREON ADAPTER DIAGNOSTIC")
print("=" * 75)

# --- Step 0: cleanup any orphan DIAG positions from previous diag run -------
print("\n[0] Cleanup orphan DIAG positions from previous diagnostic...")
mt5.initialize()
positions = mt5.positions_get(symbol="XAUUSD") or []
orphans = [p for p in positions if p.comment and 'DIAG' in p.comment]
for p in orphans:
    close_req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": p.volume,
        "type":   mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
        "position": p.ticket,
        "price":  mt5.symbol_info_tick(p.symbol).bid if p.type == 0
                  else mt5.symbol_info_tick(p.symbol).ask,
        "deviation": 50,
        "magic": p.magic,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    r = mt5.order_send(close_req)
    rc_str = "OK" if (r and r.retcode == 10009) else f"FAIL rc={getattr(r, 'retcode', None)}"
    print(f"  Closed orphan ticket {p.ticket} ({p.comment}): {rc_str}")
if not orphans:
    print("  No orphan positions found.")

# --- Step 1: Import bot.py's MT5Adapter --------------------------------------
print("\n[1] Importing from bot.py...")
try:
    import bot as botmod
    print(f"  ✓ Imported bot.py from: {botmod.__file__}")
    # Probe for adapter class — try common names
    AdapterCls = None
    for name in ['MT5Adapter', 'Mt5Adapter', 'MT5Broker', 'Adapter']:
        if hasattr(botmod, name):
            AdapterCls = getattr(botmod, name)
            print(f"  ✓ Found adapter class: bot.{name}")
            break
    if AdapterCls is None:
        print("  ✗ No adapter class found in bot.py. Class names tried: "
              "MT5Adapter, Mt5Adapter, MT5Broker, Adapter")
        print(f"  Available in bot module: {[n for n in dir(botmod) if not n.startswith('_')]}")
        raise SystemExit(2)
    # Probe for Config class
    CfgCls = None
    for name in ['Config', 'AureonConfig', 'BotConfig', 'CONFIG']:
        if hasattr(botmod, name):
            CfgCls = getattr(botmod, name)
            print(f"  ✓ Found config: bot.{name}")
            break
    if CfgCls is None:
        print(f"  ✗ No Config class found. Available: "
              f"{[n for n in dir(botmod) if not n.startswith('_')]}")
        raise SystemExit(2)
except Exception as e:
    print(f"  ✗ Import failed: {e}")
    traceback.print_exc()
    raise SystemExit(2)

# --- Instantiate config + adapter --------------------------------------------
print("\n  Instantiating config + adapter...")
try:
    cfg = CfgCls()
    print(f"  ✓ Config created. symbol={getattr(cfg, 'symbol', '?')}, "
          f"lot_size={getattr(cfg, 'lot_size', '?')}, "
          f"sl_dist={getattr(cfg, 'sl_dist', '?')}, "
          f"trigger_dist={getattr(cfg, 'trigger_dist', '?')}")
except Exception as e:
    print(f"  ✗ Config() failed: {e}")
    traceback.print_exc()
    raise SystemExit(2)

try:
    # Try no-args first (MT5Adapter() — user's actual signature)
    try:
        adapter = AdapterCls()
        print(f"  ✓ Adapter created: {type(adapter).__name__}()")
    except TypeError:
        # Fall back to passing config
        adapter = AdapterCls(cfg)
        print(f"  ✓ Adapter created: {type(adapter).__name__}(cfg)")
    # Try common init/connect method names — pass cfg if it takes 1 param
    init_called = False
    for init_name in ('connect', 'initialize', 'init', 'login', 'start'):
        if hasattr(adapter, init_name):
            m = getattr(adapter, init_name)
            try:
                sig = inspect.signature(m)
                req_params = [p for p in sig.parameters.values()
                              if p.default is inspect.Parameter.empty
                              and p.kind != inspect.Parameter.VAR_POSITIONAL
                              and p.kind != inspect.Parameter.VAR_KEYWORD]
                print(f"  Calling adapter.{init_name}{sig}  (required params: {len(req_params)})")
                if len(req_params) == 0:
                    result = m()
                elif len(req_params) == 1:
                    result = m(cfg)
                else:
                    print(f"  Skipping {init_name} — unclear signature")
                    continue
                print(f"  ✓ adapter.{init_name}() returned {result}")
                init_called = True
                break
            except Exception as e:
                print(f"  ⚠ adapter.{init_name}() raised: {e}")
                # If a connect/login failed, that might be the actual bug
    if not init_called:
        print(f"  ℹ No init method called. Adapter may rely on global mt5.initialize().")
except Exception as e:
    print(f"  ✗ Adapter instantiation failed: {e}")
    traceback.print_exc()
    raise SystemExit(2)

# --- Step 2: Place an order via the bot's adapter ----------------------------
print("\n[2] Testing adapter.place_stop_order — exact bot code path...")
tick = mt5.symbol_info_tick(cfg.symbol)
print(f"  Current: bid=${tick.bid:.2f}, ask=${tick.ask:.2f}")
test_price = round(tick.ask + 10.0, 2)   # well above market, safe
test_sl    = round(test_price - 18.0, 2)
test_tp    = round(test_price + 30.0, 2)
test_lot   = 0.01

print(f"  BUY STOP @ ${test_price}, SL ${test_sl}, TP ${test_tp}, lot {test_lot}")

# Probe for the method signature
import inspect
if hasattr(adapter, 'place_stop_order'):
    sig = inspect.signature(adapter.place_stop_order)
    print(f"  Method signature: place_stop_order{sig}")
else:
    print(f"  ✗ adapter has no place_stop_order method")
    print(f"  Methods: {[m for m in dir(adapter) if not m.startswith('_')]}")
    raise SystemExit(2)

adapter_ticket = None
try:
    result = adapter.place_stop_order(
        cfg.symbol, 'BUY', test_price, test_lot,
        sl=test_sl, tp=test_tp,
        comment="DIAG_ADAPTER",
        dry_run=False,
    )
    last_err = mt5.last_error()
    print(f"  result type      : {type(result).__name__}")
    print(f"  result value     : {result}")
    if result is not None:
        print(f"  result.retcode   : {getattr(result, 'retcode', 'N/A')}")
        print(f"  result.order     : {getattr(result, 'order', 'N/A')}")
        print(f"  result.comment   : {getattr(result, 'comment', 'N/A')}")
        if hasattr(result, 'retcode') and result.retcode == 10009:
            adapter_ticket = result.order
            print(f"  ✓ ADAPTER PLACEMENT SUCCEEDED — ticket {adapter_ticket}")
        else:
            print(f"  ✗ ADAPTER REJECTED — retcode={getattr(result, 'retcode', '?')}")
    else:
        print(f"  ✗ ADAPTER RETURNED None — this matches the live bot's failure mode")
    print(f"  mt5.last_error() : {last_err}")
except Exception as e:
    print(f"  ✗ adapter.place_stop_order raised exception: {e}")
    traceback.print_exc()

# --- Step 3: Raw mt5.order_send with same params for comparison --------------
print("\n[3] Raw mt5.order_send with same params (control)...")
raw_price = round(tick.ask + 12.0, 2)   # offset $2 to not collide
raw_request = {
    "action":       mt5.TRADE_ACTION_PENDING,
    "symbol":       cfg.symbol,
    "volume":       test_lot,
    "type":         mt5.ORDER_TYPE_BUY_STOP,
    "price":        raw_price,
    "sl":           round(raw_price - 18.0, 2),
    "tp":           round(raw_price + 30.0, 2),
    "deviation":    20,
    "magic":        99997,
    "comment":      "DIAG_RAW",
    "type_time":    mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC,
}
raw_result = mt5.order_send(raw_request)
raw_err = mt5.last_error()
raw_ticket = None
print(f"  raw result       : {raw_result}")
print(f"  raw mt5.last_error(): {raw_err}")
if raw_result and raw_result.retcode == 10009:
    raw_ticket = raw_result.order
    print(f"  ✓ RAW PLACEMENT SUCCEEDED — ticket {raw_ticket}")

# --- Step 4: Hammer test ---------------------------------------------------
print("\n[4] Hammer test: 10 rapid place+cancel cycles via adapter...")
print("    (This exposes any state corruption in the bot's adapter over many calls)")
hammer_results = []
for i in range(10):
    tk = mt5.symbol_info_tick(cfg.symbol)
    hp = round(tk.ask + 15.0 + i * 0.1, 2)
    try:
        r = adapter.place_stop_order(
            cfg.symbol, 'BUY', hp, 0.01,
            sl=round(hp - 18.0, 2),
            tp=round(hp + 30.0, 2),
            comment=f"DIAG_HAMMER_{i}",
            dry_run=False,
        )
        err = mt5.last_error()
        rc = getattr(r, 'retcode', None) if r is not None else None
        ok = (rc == 10009)
        ticket = getattr(r, 'order', None) if r is not None else None
        hammer_results.append((i, ok, rc, err, ticket))
        status = "✓" if ok else "✗"
        print(f"  [{i+1:>2}] {status}  rc={rc} ticket={ticket} last_err={err}")
        # Cancel immediately
        if ticket:
            mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
    except Exception as e:
        hammer_results.append((i, False, 'EXC', str(e), None))
        print(f"  [{i+1:>2}] ✗ exception: {e}")
    time.sleep(0.1)

successes = sum(1 for r in hammer_results if r[1])
print(f"\n  Summary: {successes}/10 succeeded")
if successes < 10:
    print(f"  ⚠ Some hammer placements failed — state corruption is reproducible!")
    print(f"  Failure indices: {[r[0] for r in hammer_results if not r[1]]}")

# --- Step 5: Cleanup --------------------------------------------------------
print("\n[5] Cleanup...")
for label, tk in [('adapter', adapter_ticket), ('raw', raw_ticket)]:
    if tk:
        r = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": tk})
        ok = r and r.retcode == 10009
        print(f"  Cancel {label} ticket {tk}: {'OK' if ok else 'FAILED'}")

# Final sweep for any DIAG positions that may have filled
positions = mt5.positions_get(symbol=cfg.symbol) or []
diag_positions = [p for p in positions if p.comment and 'DIAG' in p.comment]
if diag_positions:
    print(f"  Closing {len(diag_positions)} DIAG position(s) that filled during test:")
    for p in diag_positions:
        cur = mt5.symbol_info_tick(p.symbol)
        close_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type":   mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
            "position": p.ticket,
            "price":  cur.bid if p.type == 0 else cur.ask,
            "deviation": 50,
            "magic": p.magic,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        r = mt5.order_send(close_req)
        print(f"    Closed {p.ticket}: {'OK' if r and r.retcode == 10009 else f'FAIL'}")

mt5.shutdown()

# --- Final verdict ---------------------------------------------------------
print("\n" + "=" * 75)
print("VERDICT")
print("=" * 75)
if successes == 10 and adapter_ticket is not None:
    print("✓ Adapter code is FINE. Problem is in the bot's RUNTIME STATE.")
    print("  Likely culprits:")
    print("    a) Stale mt5 connection in long-running bot process")
    print("    b) Race condition between reconcile loop and placement")
    print("    c) Hot polling thrashing the SDK (0.2s loops during hot window)")
    print("    d) State after a previous failed call persists")
    print("  Next step: capture mt5.last_error() in bot adapter and restart bot.")
else:
    print("✗ Adapter has a bug. Even fresh-process placement fails.")
    print("  Compare what the adapter does differently from raw mt5.order_send.")
    print(f"  Adapter ticket: {adapter_ticket}, Raw ticket: {raw_ticket}")
print("=" * 75)