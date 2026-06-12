"""
AUREON — Test Using Bot's Actual place_stop_order Function
============================================================

This test imports the bot's MT5Adapter class directly from bot.py and calls
its place_stop_order() method — the EXACT function that fails at anchors.

If THIS passes, the function is fine. If THIS fails, we have a code-level
issue inside place_stop_order itself (not just timing / broker load).

Replicates today's A2 exact params:
  - Half-lot 0.28 (gap mode)
  - $10 SL bracket
  - $30 TP bracket
  - Comment matches bot format
  - Same magic (20260522)
  - Same reconciliation logic

Usage:
  cd C:\\Users\\HitheshSunkara\\Desktop\\AUREON_MAY_BOT\\PROD
  python test_bot_placement.py
"""
import sys
import time
import datetime as dt

# Import the bot's actual code
sys.path.insert(0, '.')
try:
    from bot import MT5Adapter
except ImportError as e:
    print(f"Could not import MT5Adapter from bot.py: {e}")
    print("Make sure you're running this from the PROD directory.")
    sys.exit(1)

print("=" * 78)
print("TEST USING BOT'S OWN place_stop_order FUNCTION")
print(f"Run at: {dt.datetime.now().isoformat()}")
print("=" * 78)

print("\nInitializing MT5Adapter (same way the bot does)...")
try:
    adapter = MT5Adapter()
except Exception as e:
    print(f"FAIL — MT5Adapter init: {e}")
    sys.exit(1)
print("OK — adapter ready")

mt5 = adapter.mt5

# Get current market
tick = mt5.symbol_info_tick("XAUUSD")
ask, bid = tick.ask, tick.bid
mid = (ask + bid) / 2
print(f"\nMarket: ask ${ask:.2f}  bid ${bid:.2f}  spread ${(ask-bid):.2f}")
print(f"Ping: {mt5.terminal_info().ping_last/1000:.0f}ms")

# Track placed tickets for cleanup
placed_tickets = []

def report(label, result, elapsed_ms):
    """Decode a result from place_stop_order and print verdict."""
    if result is None:
        print(f"  [{label}] FAIL — returned None (rc=-1, no broker response)")
        return False, None
    rc = getattr(result, 'retcode', None)
    ticket = getattr(result, 'order', None)
    comment = getattr(result, 'comment', '?')
    if rc == 10009:
        print(f"  [{label}] PASS — ticket {ticket} placed in {elapsed_ms:.0f}ms ({comment})")
        if ticket and isinstance(ticket, int):
            placed_tickets.append(ticket)
        return True, ticket
    else:
        print(f"  [{label}] FAIL — retcode={rc} comment='{comment}' ({elapsed_ms:.0f}ms)")
        return False, None

# ===========================================================================
# TEST 1: Single BUY via bot's place_stop_order — gap mode params
# ===========================================================================
print(f"\n[TEST 1] BUY via bot.MT5Adapter.place_stop_order (gap mode params)")
buy_price = round(ask + 5, 2)
t0 = time.time()
res = adapter.place_stop_order(
    symbol="XAUUSD", side="BUY",
    price=buy_price, lot=0.28,
    sl=round(buy_price - 10, 2),
    tp=round(buy_price + 30, 2),
    comment="AUREONv2_A_test_BUY_GAP",
    dry_run=False,
)
elapsed_ms = (time.time() - t0) * 1000
ok1, t1 = report("T1 BUY gap mode", res, elapsed_ms)

time.sleep(0.5)

# ===========================================================================
# TEST 2: Single SELL via bot's place_stop_order — gap mode params
# ===========================================================================
print(f"\n[TEST 2] SELL via bot.MT5Adapter.place_stop_order (gap mode params)")
tick = mt5.symbol_info_tick("XAUUSD"); ask, bid = tick.ask, tick.bid
sell_price = round(bid - 5, 2)
t0 = time.time()
res = adapter.place_stop_order(
    symbol="XAUUSD", side="SELL",
    price=sell_price, lot=0.28,
    sl=round(sell_price + 10, 2),
    tp=round(sell_price - 30, 2),
    comment="AUREONv2_A_test_SELL_GAP",
    dry_run=False,
)
elapsed_ms = (time.time() - t0) * 1000
ok2, t2 = report("T2 SELL gap mode", res, elapsed_ms)

time.sleep(0.5)

# ===========================================================================
# TEST 3: BACK-TO-BACK via bot's adapter (the exact A2 pattern)
# ===========================================================================
print(f"\n[TEST 3] BACK-TO-BACK via bot.MT5Adapter (exact A2 failure pattern)")
tick = mt5.symbol_info_tick("XAUUSD"); ask, bid = tick.ask, tick.bid
buy_price  = round(ask + 5, 2)
sell_price = round(bid - 5, 2)

print(f"  Replicating: bot does BUY then SELL with NO gap between (like real anchor)")
t0 = time.time()
res_buy = adapter.place_stop_order(
    symbol="XAUUSD", side="BUY",
    price=buy_price, lot=0.28,
    sl=round(buy_price - 10, 2),
    tp=round(buy_price + 30, 2),
    comment="AUREONv2_A_test2_BUY_GAP",
    dry_run=False,
)
ms_buy = (time.time() - t0) * 1000
ok3a, t3a = report("T3a BUY", res_buy, ms_buy)

# No sleep — exactly like the bot
t0 = time.time()
res_sell = adapter.place_stop_order(
    symbol="XAUUSD", side="SELL",
    price=sell_price, lot=0.28,
    sl=round(sell_price + 10, 2),
    tp=round(sell_price - 30, 2),
    comment="AUREONv2_A_test2_SELL_GAP",
    dry_run=False,
)
ms_sell = (time.time() - t0) * 1000
ok3b, t3b = report("T3b SELL", res_sell, ms_sell)

# ===========================================================================
# CLEANUP — cancel everything we placed
# ===========================================================================
print(f"\nCleanup — cancelling {len(placed_tickets)} test orders...")
for ticket in placed_tickets:
    try:
        cancel = mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        })
        if cancel and cancel.retcode == 10009:
            print(f"  Cancelled {ticket}")
        else:
            print(f"  WARN — cancel {ticket} returned {cancel.retcode if cancel else 'None'}")
    except Exception as e:
        print(f"  ERROR cancelling {ticket}: {e}")

# ===========================================================================
# SUMMARY
# ===========================================================================
print("\n" + "=" * 78)
print("RESULTS")
print("=" * 78)

results = [
    ("T1 BUY via bot.place_stop_order",         ok1),
    ("T2 SELL via bot.place_stop_order",        ok2),
    ("T3a BUY back-to-back via bot",            ok3a),
    ("T3b SELL back-to-back via bot",           ok3b),
]
for name, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")

passes = sum(1 for _, ok in results if ok)
fails  = len(results) - passes

print(f"\n  {passes}/{len(results)} passed")

if fails == 0:
    print("\n  CONCLUSION:")
    print("  The bot's place_stop_order function works correctly in current conditions.")
    print("  Today's A2 failure (rc=noresponse on both sides) was timing-specific —")
    print("  the failure happened AT the M5 candle close + session open spike.")
    print()
    print("  Same function, same params, same broker, same network, same magic —")
    print("  works at 13:xx IST, fails at 12:30 IST (M5 candle boundary).")
    print()
    print("  RECOMMENDED FIX: add 5-second delay in _process_anchor before placement.")
elif fails == len(results):
    print("\n  CONCLUSION:")
    print("  Bot's place_stop_order is failing RIGHT NOW.")
    print("  → Either broker server is currently degraded, or the function itself")
    print("    has a bug (compare against test_placement.py output — if THAT passed")
    print("    but THIS fails, the bug is INSIDE place_stop_order)")
else:
    print(f"\n  CONCLUSION:")
    print(f"  Inconsistent — {fails} failed, {passes} passed.")
    print("  Compare specific failures to find the pattern (timing? back-to-back?)")