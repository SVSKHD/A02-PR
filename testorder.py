"""AUREON — `testorder`: prove the LIVE order path end-to-end, risk-free.

  python bot.py testorder

NOTE ON NAMING: the task specified `python bot.py testfire`, but `testfire` is
ALREADY an existing, tested feature (v3.2.9 manual anchor entry that fires a REAL
strategy straddle). This risk-free order-path verifier is the opposite of that, so
it ships under a distinct verb (`testorder`) rather than clobbering a live feature.

WHAT IT DOES (demo account only; refuses on funded):
  1. Startup assertions (RETAIL_HEDGING, trade_expert, symbol visible) — same as boot.
  2. Rest a real pending BUY STOP far from market (mid + $80, lot 0.01, comment
     "TESTORDER", own magic 20260817); verify it exists via orders_get.
  3. Modify it (+$5); verify the modify landed.
  4. Cancel it; verify removal.
  5. Open a real 0.01 MARKET position (with an SL); verify via positions_get; modify
     the SL once (proves the lock path); close at market; verify removal.
  6. Post a Discord card with each step's retcode + round-trip latency; write a
     TESTORDER row to trades.csv flagged test=1 (excluded from PnL / journal stats).
  7. Exit 0 only if EVERY step returned DONE; print a step-by-step PASS/FAIL table.

GUARDS: refuses unless the account trade_mode is DEMO (override:
--i-know-this-is-real); refuses while a live bot process holds the PID lock. TESTORDER
orders carry their own magic + "TESTORDER" comment: exempt from stale_leg_sweep and
invisible to rescue/rogue (which key on the anchor "A:" tag / their own magics).

The pure step runner (`run_steps`) talks to a small broker interface, so every path
— full pass and each failure mode — is unit-testable offline with a fake.
"""
from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger("AUREON")

TESTORDER_MAGIC = 20260817
TESTORDER_COMMENT = "TESTORDER"
DONE = 10009

# far-from-market + sizing (fixed; this never touches strategy params)
FAR_OFFSET = 80.0
MODIFY_BUMP = 5.0
TEST_LOT = 0.01
SL_OFFSET = 5.0
SL_BUMP = 1.0


@dataclass
class Step:
    name: str
    retcode: Optional[int]
    ok: bool
    latency_ms: float
    detail: str = ""


# --- pure step runner -------------------------------------------------------------
def run_steps(broker, clock) -> List[Step]:
    """Drive the order path against `broker` (duck-typed interface below). `clock()`
    returns monotonic seconds. Returns the ordered step list; stops at the first
    failure (a broken order path should not keep sending). NEVER raises — a broker
    raise becomes a failed step.

    broker interface:
      startup_assertions()                 -> None (raises on failure)
      mid()                                -> float
      place_stop(price, lot, comment)      -> res(.retcode, .ticket)
      pending_ticket(comment)              -> int|None
      pending_price(ticket)                -> float|None
      modify_pending(ticket, price)        -> res
      cancel(ticket)                       -> res
      place_market(lot, comment, sl)       -> res(.retcode, .ticket)
      position_ticket(comment)             -> int|None
      position_sl(ticket)                  -> float|None
      modify_sl(ticket, sl)                -> res
      close(ticket)                        -> res
    """
    steps: List[Step] = []

    def _timed(fn):
        t0 = clock()
        try:
            res = fn()
            err = None
        except Exception as e:  # a raise is a failed step, never a crash
            res, err = None, e
        return res, (clock() - t0) * 1000.0, err

    def _rc(res):
        return getattr(res, "retcode", None) if res is not None else None

    def _add(name, ok, retcode, ms, detail=""):
        steps.append(Step(name, retcode, ok, round(ms, 1), detail))
        return ok

    # 1. startup assertions
    _res, ms, err = _timed(broker.startup_assertions)
    if err is not None:
        _add("startup_assertions", False, None, ms, f"refused: {err}")
        return steps
    _add("startup_assertions", True, DONE, ms, "hedging/expert/symbol OK")

    mid = broker.mid()

    # 2. place pending BUY STOP far from market
    res, ms, err = _timed(lambda: broker.place_stop(round(mid + FAR_OFFSET, 2), TEST_LOT, TESTORDER_COMMENT))
    tk = getattr(res, "ticket", None)
    if not (_rc(res) == DONE and tk and broker.pending_ticket(TESTORDER_COMMENT) == tk):
        _add("place_pending", False, _rc(res), ms, f"err={err}" if err else f"ticket={tk}")
        return steps
    _add("place_pending", True, DONE, ms, f"ticket={tk} @ {round(mid + FAR_OFFSET, 2)}")

    # 3. modify the pending (+$5), verify the new price landed
    want = round(mid + FAR_OFFSET + MODIFY_BUMP, 2)
    res, ms, err = _timed(lambda: broker.modify_pending(tk, want))
    landed = broker.pending_price(tk)
    if not (_rc(res) == DONE and landed is not None and abs(landed - want) < 1e-6):
        _add("modify_pending", False, _rc(res), ms, f"landed={landed} want={want} err={err}")
        return steps
    _add("modify_pending", True, DONE, ms, f"price→{want}")

    # 4. cancel, verify removal
    res, ms, err = _timed(lambda: broker.cancel(tk))
    if not (_rc(res) == DONE and broker.pending_ticket(TESTORDER_COMMENT) is None):
        _add("cancel_pending", False, _rc(res), ms, f"still present err={err}")
        return steps
    _add("cancel_pending", True, DONE, ms, "removed")

    # 5. market open (with SL) → verify → modify SL → close → verify
    sl0 = round(mid - SL_OFFSET, 2)
    res, ms, err = _timed(lambda: broker.place_market(TEST_LOT, TESTORDER_COMMENT, sl0))
    ptk = getattr(res, "ticket", None)
    if not (_rc(res) == DONE and ptk and broker.position_ticket(TESTORDER_COMMENT) == ptk):
        _add("open_market", False, _rc(res), ms, f"ticket={ptk} err={err}")
        return steps
    _add("open_market", True, DONE, ms, f"ticket={ptk} lot={TEST_LOT}")

    sl1 = round(sl0 + SL_BUMP, 2)
    res, ms, err = _timed(lambda: broker.modify_sl(ptk, sl1))
    landed = broker.position_sl(ptk)
    if not (_rc(res) == DONE and landed is not None and abs(landed - sl1) < 1e-6):
        _add("modify_sl", False, _rc(res), ms, f"landed={landed} want={sl1} err={err}")
        return steps
    _add("modify_sl", True, DONE, ms, f"SL→{sl1}")

    res, ms, err = _timed(lambda: broker.close(ptk))
    if not (_rc(res) == DONE and broker.position_ticket(TESTORDER_COMMENT) is None):
        _add("close_market", False, _rc(res), ms, f"still open err={err}")
        return steps
    _add("close_market", True, DONE, ms, "closed")
    return steps


def all_passed(steps: List[Step]) -> bool:
    return bool(steps) and all(s.ok for s in steps)


def render_table(steps: List[Step]) -> str:
    lines = [f"{'STEP':<20} {'RC':>6}  {'LATENCY':>9}  RESULT"]
    for s in steps:
        rc = "-" if s.retcode is None else str(s.retcode)
        lines.append(f"{s.name:<20} {rc:>6}  {s.latency_ms:>7.1f}ms  "
                     f"{'PASS' if s.ok else 'FAIL'}  {s.detail}")
    lines.append(f"RESULT: {'PASS' if all_passed(steps) else 'FAIL'} "
                 f"({sum(s.ok for s in steps)}/{len(steps)})")
    return "\n".join(lines)


# --- trades.csv (test=1 row, excluded from stats) ---------------------------------
LEDGER_FIELDS = ["ts", "kind", "test", "steps_total", "steps_passed", "result",
                 "max_latency_ms", "magic", "comment"]


def write_ledger_row(path: str, ts: str, steps: List[Step]) -> None:
    try:
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        d = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(d, exist_ok=True)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(LEDGER_FIELDS)
            w.writerow([ts, "TESTORDER", 1, len(steps), sum(s.ok for s in steps),
                        "PASS" if all_passed(steps) else "FAIL",
                        round(max((s.latency_ms for s in steps), default=0.0), 1),
                        TESTORDER_MAGIC, TESTORDER_COMMENT])
    except Exception as e:
        log.warning(f"testorder: ledger write failed ({e!r})")


# --- guards + orchestration -------------------------------------------------------
def _default_lock_check(cfg) -> Optional[str]:
    """Return a reason string if a live bot process holds the PID lock, else None."""
    run_dir = getattr(cfg, "run_dir", "run")
    lock_path = os.path.join(run_dir, "aureon.pid")
    if not os.path.exists(lock_path):
        return None
    try:
        import psutil
        with open(lock_path, encoding="utf-8") as f:
            pid = int(f.read().strip())
        if psutil.pid_exists(pid):
            cmd = " ".join(psutil.Process(pid).cmdline()).lower()
            if any(k in cmd for k in ("aureon", "live_trader", "bot.py")):
                return f"live AUREON process pid={pid} holds {lock_path}"
    except Exception:
        return None
    return None


def run_testorder(cfg, adapter=None, *, broker=None, allow_real=False, clock=None,
                  notifier=None, ledger_path=None, lock_check=None, now_iso=None) -> int:
    """Entry point. Returns a process exit code: 0 = all steps DONE; nonzero =
    refused (guard) or a failing step. Injectable broker/clock/lock_check/ledger for
    offline tests."""
    import time as _time
    clock = clock or _time.monotonic
    notifier = notifier or getattr(cfg, "_tele", None)

    if broker is None:
        if adapter is None:
            print("testorder: no adapter/broker", flush=True)
            return 5
        broker = _AdapterBroker(adapter, cfg)

    # GUARD: lockfile (never run alongside a live bot)
    lc = lock_check if lock_check is not None else (lambda: _default_lock_check(cfg))
    reason = lc()
    if reason:
        print(f"❌ testorder REFUSED — {reason}", flush=True)
        return 4

    # GUARD: demo-only (unless explicitly overridden)
    try:
        mode = broker.trade_mode()
    except Exception:
        mode = None
    if mode != "demo" and not allow_real:
        print(f"❌ testorder REFUSED — account trade_mode is '{mode}', not demo "
              f"(pass --i-know-this-is-real to override)", flush=True)
        return 3

    steps = run_steps(broker, clock)
    table = render_table(steps)
    print(table, flush=True)

    ts = now_iso or _now_iso()
    write_ledger_row(ledger_path or _default_ledger_path(cfg), ts, steps)
    _post_card(notifier, steps)
    return 0 if all_passed(steps) else 1


def _default_ledger_path(cfg) -> str:
    return os.path.join(getattr(cfg, "run_dir", "run"), "trades.csv")


def _now_iso() -> str:
    try:
        import pandas as pd
        return pd.Timestamp.now(tz="UTC").isoformat()
    except Exception:
        return ""


def _post_card(notifier, steps) -> None:
    if notifier is None:
        return
    try:
        import discord_cards as _dc
        from telemetry import Severity
        card = _dc.card_generic(
            f"🧪 TESTORDER {'PASS' if all_passed(steps) else 'FAIL'}",
            "```\n" + render_table(steps) + "\n```",
            color=(_dc.GREEN if all_passed(steps) else _dc.RED))
        notifier.send("🧪 TESTORDER order-path verification", Severity.INFO, card=card)
    except Exception as e:
        log.warning(f"testorder: card post failed ({e!r})")


# --- live MT5 adapter broker ------------------------------------------------------
class _AdapterBroker:
    """Wraps the AUREON MT5 adapter into the run_steps interface, own magic +
    TESTORDER comment. All reads filter to this magic + comment so nothing else on
    the account is seen or touched."""

    def __init__(self, adapter, cfg):
        self.a = adapter
        self.mt5 = adapter.mt5
        self.cfg = cfg
        self.symbol = getattr(cfg, "symbol", "XAUUSD")

    def startup_assertions(self):
        mt5 = self.mt5
        acct = mt5.account_info()
        if acct is None:
            raise RuntimeError("account_info() is None (not logged in)")
        hedging = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        if getattr(acct, "margin_mode", None) != hedging:
            raise RuntimeError("margin_mode is not RETAIL_HEDGING")
        if not getattr(acct, "trade_expert", False):
            raise RuntimeError("expert trading not allowed")
        info = mt5.symbol_info(self.symbol)
        if (info is None or not getattr(info, "visible", False)) and not mt5.symbol_select(self.symbol, True):
            raise RuntimeError(f"symbol {self.symbol} not visible")

    def trade_mode(self):
        acct = self.mt5.account_info()
        demo = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        return "demo" if getattr(acct, "trade_mode", None) == demo else "real"

    def mid(self):
        t = self.mt5.symbol_info_tick(self.symbol)
        return (float(t.bid) + float(t.ask)) / 2.0

    def _ours(self, o):
        return (int(getattr(o, "magic", -1) or -1) == TESTORDER_MAGIC
                and TESTORDER_COMMENT in str(getattr(o, "comment", "")))

    def place_stop(self, price, lot, comment):
        return self.a.place_stop_order(self.symbol, "BUY", price, lot, sl=0.0, tp=0.0,
                                       comment=comment, dry_run=False)

    def pending_ticket(self, comment):
        for o in (self.mt5.orders_get(symbol=self.symbol) or []):
            if self._ours(o):
                return int(o.ticket)
        return None

    def pending_price(self, ticket):
        for o in (self.mt5.orders_get(symbol=self.symbol) or []):
            if int(getattr(o, "ticket", -1)) == int(ticket):
                return float(o.price_open)
        return None

    def modify_pending(self, ticket, price):
        req = {"action": getattr(self.mt5, "TRADE_ACTION_MODIFY", 7),
               "order": int(ticket), "price": float(price)}
        return self.mt5.order_send(req)

    def cancel(self, ticket):
        return self.a.cancel_order(ticket, dry_run=False)

    def place_market(self, lot, comment, sl):
        return self.a.place_market_order(self.symbol, "BUY", lot, sl=sl, tp=0.0,
                                         comment=comment, dry_run=False, magic=TESTORDER_MAGIC)

    def position_ticket(self, comment):
        for p in (self.mt5.positions_get(symbol=self.symbol) or []):
            if self._ours(p):
                return int(p.ticket)
        return None

    def position_sl(self, ticket):
        for p in (self.mt5.positions_get(symbol=self.symbol) or []):
            if int(getattr(p, "ticket", -1)) == int(ticket):
                return float(getattr(p, "sl", 0.0) or 0.0)
        return None

    def modify_sl(self, ticket, sl):
        return self.a.modify_position_sl(ticket, sl, dry_run=False)

    def close(self, ticket):
        return self.a.close_position(ticket, dry_run=False)
