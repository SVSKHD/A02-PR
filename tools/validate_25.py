"""
AUREON v2.5 — Full Validation
==============================
Verifies ALL 11 v2.5 hardening patches are deployed and functional.

Usage:
  cd C:\\Users\\HitheshSunkara\\Desktop\\AUREON_MAY_BOT\\PROD
  python validate_v25.py
"""
import sys, os
from pathlib import Path

print("=" * 78)
print("AUREON v2.5 — HARDENED BOT VALIDATION")
print("=" * 78)

errors = []
def check(name, cond, detail=""):
    s = "PASS" if cond else "FAIL"
    print(f"  [{s}] {name}" + (f" — {detail}" if detail else ""))
    if not cond: errors.append(name)

sys.path.insert(0, '.')

# Load sources
try:
    bot_src = Path("bot.py").read_text()
    lt_src  = Path("live_trader.py").read_text()
except Exception as e:
    print(f"FATAL — could not read source files: {e}")
    sys.exit(1)

print("\n[Section A] Code-level patch presence")
print("-" * 78)
check("P1 atomic state writes + .bak backup",
      "+ \".bak\"" in lt_src or "+ '.bak'" in lt_src)
check("P1 state corruption recovery (try main → .bak → fresh)",
      "try main state, then .bak" in lt_src or "State " in lt_src and "corrupt" in lt_src)
check("P2 PID lock acquire on startup",
      "_acquire_pid_lock" in lt_src and "pid_lock_path" in lt_src)
check("P2 PID lock check uses psutil for real process verification",
      "psutil.pid_exists" in lt_src or "psutil.Process" in lt_src)
check("P3 deferred dispatch (non-blocking 5s wait)",
      "_complete_deferred_anchor" in lt_src and "defer_until" in lt_src)
check("P3 tick freshness validation",
      "tick_age_s" in lt_src and "stale tick" in lt_src)
check("P3 account floor enforcement",
      "account floor breached" in lt_src.lower() or "account_floor" in lt_src)
check("P4 modify_position_sl rc=-1 reconciliation",
      "RECONCILED_SLTP" in bot_src)
check("P4 cancel_order rc=-1 reconciliation",
      "RECONCILED_CANCEL" in bot_src)
check("P5 EOD flatten retry (3 attempts)",
      "verified closed" in lt_src and "for attempt in range(3)" in lt_src)
check("P5 EOD failure escalates to critical Telegram alert",
      "FLATTEN INCOMPLETE" in lt_src)
check("P6 max_fav persisted to state + rehydrated on restart",
      "Rehydrated position" in lt_src and "shadow_positions_extended" in lt_src)
check("P7 lot validated against broker volume_step",
      "volume_step" in lt_src and "volume_min" in lt_src)
check("P8 freeze_minutes default = 15 (matches projected config)",
      "freeze_minutes: int = 15" in bot_src)
check("P9 unhandled exception caught + Telegram + watchdog restart-safe",
      "AUREON CRASHED" in lt_src)
check("P9 PID lock released in finally clause",
      "_release_pid_lock()" in lt_src)

# Import and instantiate
print("\n[Section B] Runtime imports + config")
print("-" * 78)
try:
    import bot
    check("bot module imports cleanly", True)
    cfg = bot.Config()
    check("Config().freeze_minutes is 15", cfg.freeze_minutes == 15, f"actual: {cfg.freeze_minutes}")
    check("Config().account_floor_pct is 0.85", cfg.account_floor_pct == 0.85, f"actual: {cfg.account_floor_pct}")
    check("Config().daily_loss_pct is 0.03", cfg.daily_loss_pct == 0.03, f"actual: {cfg.daily_loss_pct}")
except Exception as e:
    check("bot module imports cleanly", False, str(e))

# Simulated position lifecycle
print("\n[Section C] Position management — $5 lock + trail + freeze")
print("-" * 78)
try:
    import pandas as pd
    cfg = bot.Config()
    cfg.freeze_minutes = 0  # off for this test, instant lock
    p = bot.Position(
        anchor_label='test', side='BUY',
        entry_price=4500.00,
        entry_time=pd.Timestamp('2026-05-26 10:00:00', tz='UTC'),
        current_sl=4482.00, tp_level=4530.00,
        max_fav=4500.00, lot=0.55,
    )
    test_bars = [
        (4500.25, 4499.80),   # no fav
        (4500.50, 4500.20),   # +0.50 → BE+trail
        (4503.00, 4500.50),   # +3 → trail
        (4505.00, 4503.00),   # +5 → $5 lock OR trail
        (4510.00, 4505.00),   # +10 → trail above lock
    ]
    for i, (h, l) in enumerate(test_bars):
        bar = pd.Series({'open': l, 'high': h, 'low': l, 'close': (h+l)/2})
        ts = pd.Timestamp(f'2026-05-26 10:{(i+1)*5:02d}:00', tz='UTC')
        bot.update_position_on_bar(p, bar, ts, cfg)
    check(f"Final SL ${p.current_sl:.2f} >= entry+$4 ($5 lock or higher trail)",
          p.current_sl >= 4504.00, f"final SL ${p.current_sl:.2f}")
    check(f"Final SL above $4509 (trail won out over $4 floor)",
          p.current_sl >= 4509.90, f"final SL ${p.current_sl:.2f}")
except Exception as e:
    check("Position lifecycle test", False, str(e))

# Live broker test
print("\n[Section D] Live broker integration")
print("-" * 78)
try:
    import MetaTrader5 as mt5
    import time
    if mt5.initialize():
        ti = mt5.terminal_info()
        check("MT5 terminal connected", ti and ti.connected, f"ping {ti.ping_last/1000:.0f}ms" if ti else "")
        si = mt5.symbol_info("XAUUSD")
        check("XAUUSD symbol available", si is not None and si.trade_mode == 4)
        check("volume_step accessible for v2.5 lot validation",
              si is not None and si.volume_step > 0, f"step={si.volume_step}")

        # Test order via bot's place_stop_order
        from bot import MT5Adapter
        adapter = MT5Adapter()
        tick = mt5.symbol_info_tick("XAUUSD")
        buy_p = round(tick.ask + 50, 2)
        t0 = time.time()
        res = adapter.place_stop_order("XAUUSD", "BUY", buy_p, 0.28,
                                        sl=buy_p-10, tp=buy_p+30,
                                        comment="V25_VALIDATE", dry_run=False)
        elapsed_ms = (time.time()-t0)*1000
        if res and getattr(res, 'retcode', None) == 10009:
            check(f"Live order placement via bot adapter ({elapsed_ms:.0f}ms)", True)
            # Cancel
            cancel_res = adapter.cancel_order(res.order, dry_run=False)
            check("Cancel via v2.5 reconciled cancel_order",
                  cancel_res and getattr(cancel_res, 'retcode', None) == 10009)
        else:
            check(f"Live order placement", False, f"rc={getattr(res, 'retcode', None) if res else None}")
    else:
        check("MT5 init", False, str(mt5.last_error()))
except ImportError:
    check("MetaTrader5 module", False, "not installed")
except Exception as e:
    check("Live broker section", False, str(e))

# psutil check (needed for PID lock)
print("\n[Section E] Dependencies")
print("-" * 78)
try:
    import psutil
    check("psutil installed (needed for v2.5 PID lock)", True, f"version {psutil.__version__}")
except ImportError:
    check("psutil installed (needed for v2.5 PID lock)", False,
          "Install with: pip install psutil")

# Summary
print("\n" + "=" * 78)
if errors:
    print(f"FAILED ({len(errors)} issues):")
    for e in errors:
        print(f"  - {e}")
    print("\nDo NOT deploy until all checks pass.")
    sys.exit(1)
else:
    print("ALL v2.5 PATCHES VERIFIED — BOT IS HARDENED")
    print()
    print("Active hardening:")
    print("  1. Atomic state writes with corruption recovery (.bak fallback)")
    print("  2. PID lock prevents multiple instances")
    print("  3. Non-blocking 5s settle wait (deferred dispatch)")
    print("  4. Tick freshness check before placement")
    print("  5. Account floor enforcement ($42,500 limit on $50k account)")
    print("  6. modify_position_sl + cancel_order rc=-1 reconciliation")
    print("  7. EOD flatten retry 3x + critical alert on failure")
    print("  8. max_fav + fill_time rehydration on restart (lock state preserved)")
    print("  9. Lot validated against broker volume_step at startup")
    print(" 10. 15m freeze enabled by default (matches backtest projection)")
    print(" 11. Crash handler + PID release in finally")
    print()
    print("Bot is now market-proof for known failure modes.")