#!/usr/bin/env python3
"""
AUREON — probe_stops.py
=======================
Read/measure the broker's stop-distance limits so the SL-clamp (v2.5.8) can be
tuned per-broker instead of guessed. SAFE: places only a tiny 0.01-lot test
position and removes it again; SL-modify probes are non-destructive (a rejected
modify changes nothing). Still, run on a DEMO account.

What it reports
---------------
1. Declared limits  : trade_stops_level / trade_freeze_level (in points AND $).
2. Symbol facts     : point, digits, spread, volume_min/step, filling modes.
3. EMPIRICAL test   : opens a 0.01-lot BUY, then tries to set its SL at a series
                      of distances below the bid (e.g. 5.00, 2.00, 1.00, 0.50,
                      0.30, 0.20, 0.10, 0.05). Records the SMALLEST distance the
                      broker ACCEPTS (rc=10009). That number is the real floor
                      your trail must respect.
4. Recommendation   : the trail_gap / min SL distance to use for THIS broker.

Usage
-----
  python probe_stops.py                # default symbol XAUUSD, opens test trade
  python probe_stops.py --symbol XAUUSD
  python probe_stops.py --no-trade     # skip the live test trade; declared-limits only
  python probe_stops.py --lot 0.01

MT5 terminal must be running and logged in (same as the bot).
Run with the SAME python that has MetaTrader5 installed:
  "C:\\Users\\hithe\\AppData\\Local\\Programs\\Python\\Python314\\python.exe" probe_stops.py
"""

import argparse
import sys
import time

# Distances (in price $) to test for SL acceptance, widest -> tightest.
PROBE_DISTANCES = [5.00, 3.00, 2.00, 1.00, 0.70, 0.50, 0.40, 0.30, 0.20, 0.10, 0.05]

MAGIC = 20260522  # match the bot's magic so these are clearly ours
TEST_COMMENT = "AUREON_PROBE"

_RC = {
    10009: "DONE",
    10016: "INVALID_STOPS",
    10015: "INVALID_PRICE",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10027: "CLIENT_DISABLES_AT",
    10029: "FROZEN",
    -1:    "NO_RESPONSE(None)",
}


def rcname(rc):
    return _RC.get(rc, f"UNKNOWN_{rc}")


def main():
    ap = argparse.ArgumentParser(description="Probe broker stop-distance limits")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--no-trade", action="store_true",
                    help="Only read declared limits; do NOT open a test position")
    args = ap.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("ERROR: MetaTrader5 not importable by THIS python. "
              "Run with the interpreter that has it installed.")
        sys.exit(1)

    if not mt5.initialize():
        print(f"ERROR: mt5.initialize() failed: {mt5.last_error()}")
        print("Make sure the MT5 terminal is running and logged in.")
        sys.exit(1)

    sym = args.symbol
    try:
        _run(mt5, sym, args.lot, no_trade=args.no_trade)
    finally:
        mt5.shutdown()


def _run(mt5, sym, lot, no_trade):
    info = mt5.account_info()
    if info is None:
        print("ERROR: no account logged in.")
        return
    print("=" * 64)
    print(f"AUREON stop-limit probe")
    print(f"Account #{info.login} on {info.server}   balance=${info.balance:.2f}")
    print("=" * 64)

    # Ensure the symbol is selected / subscribed
    if not mt5.symbol_select(sym, True):
        print(f"WARNING: symbol_select({sym}) returned False — continuing anyway")

    si = mt5.symbol_info(sym)
    tk = mt5.symbol_info_tick(sym)
    if si is None or tk is None:
        print(f"ERROR: could not read symbol_info/tick for {sym}")
        return

    point = si.point
    stops_pts = si.trade_stops_level
    freeze_pts = si.trade_freeze_level
    stops_usd = stops_pts * point
    freeze_usd = freeze_pts * point
    spread_usd = tk.ask - tk.bid

    print("\n--- DECLARED LIMITS (symbol_info) ---")
    print(f"  symbol            : {sym}")
    print(f"  point             : {point}")
    print(f"  digits            : {si.digits}")
    print(f"  bid / ask         : {tk.bid:.{si.digits}f} / {tk.ask:.{si.digits}f}")
    print(f"  spread            : ${spread_usd:.{si.digits}f}")
    print(f"  trade_stops_level : {stops_pts} pts  = ${stops_usd:.{si.digits}f}")
    print(f"  trade_freeze_level: {freeze_pts} pts  = ${freeze_usd:.{si.digits}f}")
    print(f"  volume_min/step   : {si.volume_min} / {si.volume_step}")
    print(f"  filling_mode      : {si.filling_mode}  (1=FOK,2=IOC,3=both,4=RETURN)")
    print(f"  trade_mode        : {si.trade_mode}  (0=disabled..4=full)")

    declared_floor = max(stops_usd, freeze_usd, spread_usd)
    print(f"\n  Declared minimum SL distance from market ≈ ${declared_floor:.{si.digits}f}")
    print(f"  (= max of stops_level, freeze_level, current spread)")

    if no_trade:
        print("\n--no-trade set: skipping empirical test. Declared limits only.")
        _recommend(declared_floor, empirical=None, digits=si.digits)
        return

    # ---------------- EMPIRICAL TEST ----------------
    print("\n--- EMPIRICAL TEST (0.01-lot BUY, non-destructive SL probes) ---")
    print("Opening a tiny market BUY to probe real SL acceptance...")

    tk = mt5.symbol_info_tick(sym)
    open_req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tk.ask,
        "deviation": 30,
        "magic": MAGIC,
        "comment": TEST_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling(mt5, si),
    }
    res = mt5.order_send(open_req)
    rc = res.retcode if res else -1
    if rc != 10009:
        print(f"  Could not open test position: rc={rc} ({rcname(rc)}) "
              f"comment={getattr(res,'comment','')}")
        print("  Falling back to declared limits only.")
        _recommend(declared_floor, empirical=None, digits=si.digits)
        return

    pos_ticket = getattr(res, "order", None) or getattr(res, "deal", None)
    # find the actual position ticket
    time.sleep(0.4)
    positions = mt5.positions_get(symbol=sym) or []
    test_pos = None
    for p in positions:
        if int(getattr(p, "magic", 0)) == MAGIC and p.comment.startswith("AUREON_PROBE"):
            test_pos = p
            break
    if test_pos is None and positions:
        # fall back to most recent
        test_pos = sorted(positions, key=lambda p: p.time)[-1]
    if test_pos is None:
        print("  Opened but could not locate the test position. Aborting test.")
        _recommend(declared_floor, empirical=None, digits=si.digits)
        return

    ticket = int(test_pos.ticket)
    entry = float(test_pos.price_open)
    print(f"  Test position open: ticket={ticket} entry=${entry:.{si.digits}f} lot={lot}")

    smallest_accepted = None
    results = []
    try:
        for dist in PROBE_DISTANCES:
            tk = mt5.symbol_info_tick(sym)
            # BUY position: SL below bid. Place SL at (bid - dist).
            sl_price = round(tk.bid - dist, si.digits)
            mreq = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "sl": sl_price,
                "tp": 0.0,
            }
            mres = mt5.order_send(mreq)
            mrc = mres.retcode if mres else -1
            ok = (mrc == 10009)
            results.append((dist, sl_price, mrc, ok))
            print(f"  SL dist ${dist:>5.2f}  (sl={sl_price:.{si.digits}f})  "
                  f"-> rc={mrc} ({rcname(mrc)})  {'ACCEPTED' if ok else 'rejected'}")
            if ok:
                smallest_accepted = dist
            time.sleep(0.25)
    finally:
        # Always clean up the test position
        print("\n  Closing test position...")
        tk = mt5.symbol_info_tick(sym)
        close_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "position": ticket,
            "volume": float(test_pos.volume),
            "type": mt5.ORDER_TYPE_SELL,  # opposite of BUY
            "price": tk.bid,
            "deviation": 30,
            "magic": MAGIC,
            "comment": "AUREON_PROBE_CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _pick_filling(mt5, si),
        }
        cres = mt5.order_send(close_req)
        crc = cres.retcode if cres else -1
        if crc == 10009:
            print(f"  Test position {ticket} closed cleanly.")
        else:
            print(f"  WARNING: close returned rc={crc} ({rcname(crc)}). "
                  f"CHECK MT5 MANUALLY and close ticket {ticket} if still open.")

    print("\n--- RESULT ---")
    if smallest_accepted is not None:
        print(f"  Smallest SL distance the broker ACCEPTED: ${smallest_accepted:.{si.digits}f}")
    else:
        print("  No tested distance was accepted (all rejected).")
    _recommend(declared_floor, empirical=smallest_accepted, digits=si.digits)


def _pick_filling(mt5, si):
    """Choose a filling mode the symbol supports."""
    fm = si.filling_mode
    # filling_mode is a bitmask: 1=FOK, 2=IOC. Prefer IOC, else FOK.
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _recommend(declared_floor, empirical, digits):
    print("\n--- RECOMMENDATION ---")
    # The real floor is the larger of declared and empirically-observed.
    if empirical is not None:
        floor = max(declared_floor, empirical)
        basis = "max(declared, empirical-accepted)"
    else:
        floor = declared_floor
        basis = "declared only (no live test)"
    # Add a small safety pad so we sit just OUTSIDE the limit, not exactly on it.
    pad = round(floor * 0.5, digits) if floor > 0 else 0.10
    suggested = round(floor + max(pad, 0.05), digits)
    print(f"  Broker minimum SL distance (floor): ${floor:.{digits}f}  [{basis}]")
    print(f"  Suggested CLAMP distance (floor + pad): ${suggested:.{digits}f}")
    print()
    print("  How to use in the bot:")
    print("  - The v2.5.8 SL clamp already pulls the SL to (bid - stops_level) for BUY")
    print("    / (ask + stops_level) for SELL automatically each bar.")
    print(f"  - If you also want a fixed minimum trail gap, set trail_gap >= ${suggested:.{digits}f}")
    print("    in Config so the trail never even attempts an illegal stop.")
    print("  - If 'empirical accepted' is much larger than 'declared', trust the")
    print("    empirical number — some brokers reject inside a wider band than they declare.")
    print("=" * 64)


if __name__ == "__main__":
    main()