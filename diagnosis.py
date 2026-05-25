"""
AUREON v2.3 — Broker Integration Diagnostic
============================================

Run this on the VPS while MT5 is connected. Safe to run while the bot is also
running (two Python processes can share the same MT5 terminal session).

Purpose:
  Diagnose the rc=noresponse failure pattern from today's A2 and A4 anchors.
  Both failures occurred in Gap Mode (lot 0.28, $10 SL). This script verifies:

  1. MT5 terminal state (connected, trade_allowed)
  2. Account permissions (trade enabled)
  3. XAUUSD symbol parameters (trade_stops_level — the key suspect)
  4. Current market state (bid/ask, spread, tick freshness)
  5. Existing pending orders (any orphans from today's failed sends?)
  6. Test order placement at safe distances (won't fill — cancelled immediately)
  7. Test order placement at exact gap-mode params that failed today

Usage:
  cd C:\\Users\\HitheshSunkara\\Desktop\\AUREON_MAY_BOT\\PROD
  python diagnostic.py

Output:
  Prints to console AND saves to logs\\diagnostic_YYYY-MM-DD_HHMM.txt
  Share the output file to debug.

Safety:
  - All test orders placed FAR from market (won't fill)
  - Every test order cancelled immediately after placement
  - Does NOT touch any existing bot positions or pendings
  - Does NOT modify any settings
"""

import sys
import os
import json
import time
import datetime as dt
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 module not installed. Run: pip install MetaTrader5")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Output setup — print + save to file
# ---------------------------------------------------------------------------
LOG_DIR = Path("./logs")
LOG_DIR.mkdir(exist_ok=True)
ts = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
LOG_FILE = LOG_DIR / f"diagnostic_{ts}.txt"

_log_lines = []
def log(msg=""):
    print(msg)
    _log_lines.append(msg)

def write_log_file():
    LOG_FILE.write_text("\n".join(_log_lines), encoding="utf-8")
    print(f"\n[Diagnostic saved to: {LOG_FILE.absolute()}]")

# ---------------------------------------------------------------------------
# Findings tracker (for PASS/FAIL summary at end)
# ---------------------------------------------------------------------------
findings = []
def note(level, key, msg):
    """level in {'PASS', 'WARN', 'FAIL', 'INFO'}"""
    findings.append((level, key, msg))

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
log("=" * 78)
log("AUREON v2.3 — BROKER INTEGRATION DIAGNOSTIC")
log(f"Run at: {dt.datetime.now().isoformat()}")
log("=" * 78)

# ---------------------------------------------------------------------------
# STEP 1: MT5 initialization
# ---------------------------------------------------------------------------
log("\n[STEP 1] MT5 initialization")
log("-" * 78)
if not mt5.initialize():
    err = mt5.last_error()
    log(f"  ❌ mt5.initialize() failed: {err}")
    note("FAIL", "init", f"MT5 init failed: {err}")
    write_log_file()
    sys.exit(1)
log("  ✅ MT5 initialized")
note("PASS", "init", "MT5 initialized")

# ---------------------------------------------------------------------------
# STEP 2: Terminal info
# ---------------------------------------------------------------------------
log("\n[STEP 2] Terminal info")
log("-" * 78)
ti = mt5.terminal_info()
if ti is None:
    log(f"  ❌ terminal_info() returned None: {mt5.last_error()}")
    note("FAIL", "terminal", "terminal_info unavailable")
else:
    log(f"  Build:           {ti.build}")
    log(f"  Connected:       {ti.connected}            {'✅' if ti.connected else '❌'}")
    log(f"  Trade allowed:   {ti.trade_allowed}        {'✅' if ti.trade_allowed else '❌'}")
    log(f"  DLLs allowed:    {ti.dlls_allowed}         {'✅' if ti.dlls_allowed else '⚠'}")
    log(f"  Path:            {ti.path}")
    log(f"  Data path:       {ti.data_path}")
    log(f"  Ping (msec):     {ti.ping_last}")
    if not ti.connected:
        note("FAIL", "terminal_connected", "Terminal not connected to broker")
    elif not ti.trade_allowed:
        note("FAIL", "trade_allowed", "Terminal has trading disabled (check Tools→Options→Expert Advisors)")
    elif ti.ping_last > 500000:  # > 500ms in microseconds
        note("WARN", "ping_high", f"High ping to broker: {ti.ping_last/1000:.0f}ms")
    else:
        note("PASS", "terminal", f"Terminal healthy (ping {ti.ping_last/1000:.0f}ms)")

# ---------------------------------------------------------------------------
# STEP 3: Account info
# ---------------------------------------------------------------------------
log("\n[STEP 3] Account info")
log("-" * 78)
ai = mt5.account_info()
if ai is None:
    log(f"  ❌ account_info() returned None: {mt5.last_error()}")
    note("FAIL", "account", "Cannot read account info")
else:
    log(f"  Login:           {ai.login}")
    log(f"  Server:          {ai.server}")
    log(f"  Balance:         ${ai.balance:,.2f}")
    log(f"  Equity:          ${ai.equity:,.2f}")
    log(f"  Trade allowed:   {ai.trade_allowed}        {'✅' if ai.trade_allowed else '❌'}")
    log(f"  Trade expert:    {ai.trade_expert}         {'✅' if ai.trade_expert else '❌'}")
    log(f"  Trade mode:      {ai.trade_mode}  (0=DEMO, 1=CONTEST, 2=REAL)")
    log(f"  Margin mode:     {ai.margin_mode}  (0=RETAIL_NETTING, 1=EXCHANGE, 2=RETAIL_HEDGING)")
    log(f"  Leverage:        1:{ai.leverage}")
    log(f"  Free margin:     ${ai.margin_free:,.2f}")
    if not ai.trade_allowed:
        note("FAIL", "account_trade", "Account does NOT have trading enabled")
    elif not ai.trade_expert:
        note("FAIL", "account_expert", "Account does NOT have algo/expert trading enabled")
    else:
        note("PASS", "account", "Account permissions OK")

# ---------------------------------------------------------------------------
# STEP 4: XAUUSD symbol info — THE KEY SUSPECT
# ---------------------------------------------------------------------------
log("\n[STEP 4] XAUUSD symbol info — checking trade_stops_level")
log("-" * 78)
si = mt5.symbol_info("XAUUSD")
if si is None:
    log(f"  ❌ symbol_info('XAUUSD') returned None: {mt5.last_error()}")
    note("FAIL", "symbol", "XAUUSD symbol not available")
else:
    log(f"  Visible:             {si.visible}                 {'✅' if si.visible else '⚠'}")
    log(f"  Trade mode:          {si.trade_mode}  (0=DISABLED, 4=FULL)")
    log(f"  Digits:              {si.digits}")
    log(f"  Point:               {si.point}")
    log(f"  Volume min:          {si.volume_min}")
    log(f"  Volume max:          {si.volume_max}")
    log(f"  Volume step:         {si.volume_step}")
    log(f"  Contract size:       {si.trade_contract_size}")
    log(f"  Filling mode:        {si.filling_mode}  (1=FOK, 2=IOC, 3=BOTH)")
    log(f"  Spread current:      {si.spread}  points")
    log(f"  Spread $:            ${si.spread * si.point:.5f}")
    log()
    log(f"  >>> trade_stops_level:   {si.trade_stops_level} points = ${si.trade_stops_level * si.point:.4f} <<<")
    log(f"  >>> trade_freeze_level:  {si.trade_freeze_level} points = ${si.trade_freeze_level * si.point:.4f}")

    stops_dollars = si.trade_stops_level * si.point
    GAP_MODE_SL_DIST = 10.00  # AUREON gap mode SL distance
    NORMAL_SL_DIST = 18.00

    log(f"\n  AUREON Gap Mode uses ${GAP_MODE_SL_DIST} SL distance.")
    if stops_dollars > GAP_MODE_SL_DIST:
        log(f"  ❌ stops_level (${stops_dollars:.2f}) EXCEEDS gap-mode SL distance — this IS the bug!")
        log(f"     Broker rejects any pending whose SL is within ${stops_dollars:.2f} of entry.")
        log(f"     Recommended fix: bump gap_mode SL distance to ${stops_dollars + 2:.2f} or higher.")
        note("FAIL", "stops_level", f"stops_level=${stops_dollars:.2f} > gap-mode SL=${GAP_MODE_SL_DIST}")
    elif stops_dollars > NORMAL_SL_DIST:
        log(f"  ❌ stops_level (${stops_dollars:.2f}) EXCEEDS even normal-mode SL — major issue")
        note("FAIL", "stops_level_normal", f"stops_level breaches normal mode too")
    elif stops_dollars > GAP_MODE_SL_DIST - 2:
        log(f"  ⚠ stops_level (${stops_dollars:.2f}) is VERY CLOSE to gap-mode SL — sometimes fails")
        note("WARN", "stops_level_close", f"stops_level=${stops_dollars:.2f} marginal vs gap-mode")
    else:
        log(f"  ✅ stops_level (${stops_dollars:.2f}) is below gap-mode SL — not the bug")
        note("PASS", "stops_level", f"stops_level=${stops_dollars:.2f} OK for gap mode")

# ---------------------------------------------------------------------------
# STEP 5: Current market tick
# ---------------------------------------------------------------------------
log("\n[STEP 5] Current XAUUSD market")
log("-" * 78)
tick = mt5.symbol_info_tick("XAUUSD")
if tick is None:
    log(f"  ❌ symbol_info_tick returned None: {mt5.last_error()}")
    note("FAIL", "tick", "No tick data for XAUUSD")
else:
    tick_age_s = time.time() - tick.time
    log(f"  Bid:             ${tick.bid:,.2f}")
    log(f"  Ask:             ${tick.ask:,.2f}")
    log(f"  Spread now:      ${tick.ask - tick.bid:.2f}")
    log(f"  Last:            ${tick.last:,.2f}")
    log(f"  Volume:          {tick.volume}")
    log(f"  Time (unix):     {tick.time}")
    log(f"  Time:            {dt.datetime.fromtimestamp(tick.time)}")
    log(f"  Age (sec):       {tick_age_s:.1f}")
    if abs(tick_age_s) > 60:
        log(f"  ⚠ Tick is stale or future-dated — broker time offset issue")
        note("WARN", "tick_age", f"Tick age {tick_age_s:.0f}s (offset issue?)")
    else:
        note("PASS", "tick", "Fresh tick")

# ---------------------------------------------------------------------------
# STEP 6: Existing pendings + positions (orphan check)
# ---------------------------------------------------------------------------
log("\n[STEP 6] Existing pendings and positions")
log("-" * 78)
pendings = mt5.orders_get(symbol="XAUUSD") or []
positions = mt5.positions_get(symbol="XAUUSD") or []
log(f"  Open pendings:   {len(pendings)}")
for p in pendings:
    log(f"    ticket={p.ticket}  type={p.type}  price={p.price_open}  "
        f"sl={p.sl}  tp={p.tp}  vol={p.volume_current}  magic={p.magic}  "
        f"comment='{p.comment}'")
log(f"  Open positions:  {len(positions)}")
for p in positions:
    log(f"    ticket={p.ticket}  type={p.type}  open={p.price_open}  "
        f"sl={p.sl}  tp={p.tp}  vol={p.volume}  magic={p.magic}  "
        f"comment='{p.comment}'  profit=${p.profit:.2f}")

aureon_magic = 20260522
aureon_pendings = [p for p in pendings if p.magic == aureon_magic]
if aureon_pendings:
    log(f"\n  ⚠ Found {len(aureon_pendings)} AUREON pending(s) — possible orphans from today")
    note("WARN", "orphans", f"{len(aureon_pendings)} AUREON pendings exist")
else:
    log(f"  ✅ No AUREON pendings (no orphans from today's failed sends)")
    note("PASS", "no_orphans", "No orphan pending orders")

# ---------------------------------------------------------------------------
# STEP 7: Test order placement — the actual test
# ---------------------------------------------------------------------------
log("\n[STEP 7] TEST ORDER PLACEMENT — replicating today's failed gap-mode order")
log("-" * 78)
log("  (orders placed FAR from market, cancelled immediately, won't fill)")
log()

if tick is None or si is None:
    log("  Skipped — no tick or symbol info")
else:
    # Place test BUY_STOP $50 above market (way too far to fill)
    test_cases = [
        ("Normal mode lot+SL",    0.5,   tick.ask + 50,  tick.ask + 50 - 18),
        ("Gap mode lot+SL",       0.28,  tick.ask + 50,  tick.ask + 50 - 10),
        ("Tight SL test ($5)",    0.28,  tick.ask + 50,  tick.ask + 50 - 5),
        ("Wider SL test ($15)",   0.28,  tick.ask + 50,  tick.ask + 50 - 15),
        ("Very tight SL ($2)",    0.28,  tick.ask + 50,  tick.ask + 50 - 2),
    ]

    for label, lot, entry, sl in test_cases:
        log(f"  Test: {label}  lot={lot}  entry=${entry:.2f}  SL=${sl:.2f}  (SL dist=${entry-sl:.2f})")
        req = {
            "action":       mt5.TRADE_ACTION_PENDING,
            "symbol":       "XAUUSD",
            "volume":       lot,
            "type":         mt5.ORDER_TYPE_BUY_STOP,
            "price":        round(entry, 2),
            "sl":           round(sl, 2),
            "tp":           round(entry + 30, 2),
            "deviation":    20,
            "magic":        99999999,  # different magic so it doesn't conflict with bot
            "comment":      f"DIAG_{label[:20].replace(' ', '_')}",
            "type_time":    mt5.ORDER_TIME_DAY,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        t_start = time.time()
        result = mt5.order_send(req)
        elapsed_ms = (time.time() - t_start) * 1000

        if result is None:
            err = mt5.last_error()
            log(f"     ❌ order_send returned None  ({elapsed_ms:.0f}ms)  last_error={err}")
            note("FAIL", f"test_{label}", f"order_send None on {label}: {err}")
        else:
            log(f"     retcode={result.retcode}  comment='{result.comment}'  ({elapsed_ms:.0f}ms)")
            if result.retcode == 10009:  # DONE
                log(f"     ✅ Placed (ticket {result.order}) — cancelling now")
                cancel_req = {"action": mt5.TRADE_ACTION_REMOVE, "order": result.order}
                cancel_res = mt5.order_send(cancel_req)
                if cancel_res and cancel_res.retcode == 10009:
                    log(f"     ✅ Cancelled cleanly")
                    note("PASS", f"test_{label}", f"Placed and cancelled OK ({elapsed_ms:.0f}ms)")
                else:
                    log(f"     ⚠ Cancel failed: {cancel_res.retcode if cancel_res else None}")
                    note("WARN", f"test_{label}", "Placed but cancel failed — orphan!")
            else:
                rc_name = {10004:"REQUOTE",10006:"REJECT",10013:"INVALID",10014:"INVALID_VOLUME",
                          10015:"INVALID_PRICE",10016:"INVALID_STOPS",10017:"TRADE_DISABLED",
                          10018:"MARKET_CLOSED",10019:"NO_MONEY",10027:"CLIENT_DISABLES_AT"}.get(result.retcode, "OTHER")
                log(f"     ❌ Rejected: {rc_name}  ({elapsed_ms:.0f}ms)")
                note("FAIL", f"test_{label}", f"Rejected with {rc_name}")
        time.sleep(0.3)  # gentle between tests

# ---------------------------------------------------------------------------
# STEP 8: SUMMARY
# ---------------------------------------------------------------------------
log("\n" + "=" * 78)
log("SUMMARY")
log("=" * 78)
counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "INFO": 0}
for level, key, msg in findings:
    icon = {"PASS":"✅", "WARN":"⚠ ", "FAIL":"❌", "INFO":"ℹ "}[level]
    log(f"  {icon} [{level:4}] {key}: {msg}")
    counts[level] += 1
log(f"\n  Total: {counts['PASS']} pass, {counts['WARN']} warn, {counts['FAIL']} fail")

# Diagnosis
log("\n" + "=" * 78)
log("DIAGNOSIS")
log("=" * 78)
fails = [(k, m) for lvl, k, m in findings if lvl == "FAIL"]
if not fails:
    log("  All checks passed. The bug is intermittent or environment-specific.")
    log("  Possible causes:")
    log("    - News-driven server timeouts (rare, transient)")
    log("    - VPS network latency spike during volatile periods")
    log("  Next step: instrument order_send with retry/jitter, log every retcode.")
else:
    if any("stops_level" in k for k, _ in fails):
        log("  ❌ ROOT CAUSE: broker's stops_level rejects gap-mode SL.")
        log("     FIX: in bot.py / live_trader.py, change gap_sl_dist from $10 to a")
        log("     value greater than the stops_level reported above (+ a $2 safety margin).")
    if any("test_Gap" in k for k, _ in fails):
        log("  ❌ Test reproduced the gap-mode failure. See exact retcode above.")
    if any("test_Normal" in k for k, _ in fails):
        log("  ❌ Normal mode ALSO failed in test — issue is deeper than gap-mode SL.")
    if any(k in ("init", "terminal_connected", "trade_allowed", "account_trade") for k, _ in fails):
        log("  ❌ MT5 terminal or account permission issue — check MT5 GUI settings.")

log("\n  Share this file with Claude or another LLM to interpret:")
log(f"    {LOG_FILE.absolute()}")

write_log_file()

# Don't shutdown — main bot may still be using the connection
# mt5.shutdown()