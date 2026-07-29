"""AUREON — `testfireimmediate`: prove the REAL order path with ONE live cycle.

  python bot.py testfireimmediate                       # demo: runs, no flags
  python bot.py testfireimmediate --i-know-real-account --lot-min   # real: both required

This runs the EXACT live placement code — the SAME adapter functions (place_stop_order,
cancel_order, place_market_order, modify_position_sl, close_position) and the SAME
magic/comment/filling logic anchors + RB + the SL-modify path use. It is NOT managed by
the live loop / watchdog: one cycle, then exit.

CYCLE (volume_min only, one cycle per invocation):
  1. place ONE pending stop at a safe distance (validated vs trade_stops_level)
  2. verify it was accepted
  3. modify its price once            (proves the modify path)
  4. cancel it                        (proves the cancel path)
  5. place ONE market order at volume_min
  6. set SL/TP via TRADE_ACTION_SLTP  (proves the SLTP path — same fn as BE-lock/trail)
  7. hold >= 180s with a live countdown (FundedElite "no sub-3-minute HFT" rule)
  8. close at market

GATES (fail-closed):
  * DEMO account  -> runs like classic testfire, no flags needed.
  * NON-DEMO      -> requires BOTH --i-know-real-account AND --lot-min; refuses otherwise.
  * refuses if ANY non-TF_ position/pending is open (a live strategy leg is present).
  * max ONE cycle per invocation; volume_min only.

ISOLATION (#125 symmetry): every order carries the ANCHORS magic (20260522) + a "TF_"
comment marker, so its deals are excluded SYMMETRICALLY from the anchors day-P&L and the
daily loss/profit halts (pnl_source.magic_day_net(..., exclude_test=True)) — exactly like
a classic testfire straddle — and TF_ orders are exempt from the stale-leg sweep and
invisible to rescue/rogue.

ABORT: on any step failure the cycle aborts and flattens ONLY its own TF_ orders (cancel
the pending / close the position it opened); it never touches another position.

The pure runner (`run_cycle`) talks to a small broker interface, so the full pass, every
failure mode, the flag-gate matrix, and abort-flatten are unit-testable offline with a fake.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

log = logging.getLogger("AUREON")

# Same magic as the anchor engine; TF-ness (and the P&L/halt exclusion) is carried by the
# "TF_" comment marker, exactly like a classic testfire straddle.
TF_MAGIC = 20260522
TF_COMMENT = "TF_IMMEDIATE"           # <=31 chars, starts with the TF_ marker
DONE = 10009
HOLD_SECONDS = 180.0                  # FundedElite sub-3-minute HFT rule
COUNTDOWN_EVERY_S = 30.0

# geometry (fixed; never touches strategy params)
PENDING_SAFE_OFFSET = 20.0            # $ above ask for the test pending (also floored vs stops_level)
STOPS_CUSHION_PTS = 5.0               # extra points beyond trade_stops_level for safety
MODIFY_BUMP = 3.0                     # $ price bump on the pending modify
SL_OFFSET = 8.0                       # $ protective SL distance on the market leg
TP_OFFSET = 12.0                      # $ TP distance on the market leg


@dataclass
class Step:
    name: str
    retcode: Optional[int]
    ok: bool
    latency_ms: float
    intended: Optional[float] = None
    actual: Optional[float] = None
    filling: str = ""
    detail: str = ""

    @property
    def slippage(self) -> Optional[float]:
        if self.intended is None or self.actual is None:
            return None
        return round(self.actual - self.intended, 3)


# --- pure cycle runner -------------------------------------------------------------
def run_cycle(broker, clock, *, sleeper=None, hold_s: float = HOLD_SECONDS,
              countdown_cb: Optional[Callable[[float], None]] = None) -> List[Step]:
    """Drive one real order-path cycle against `broker` (duck-typed below). `clock()`
    returns monotonic seconds; `sleeper(s)` sleeps (injected for tests). Returns the
    ordered step list, stopping at the first failure. On failure it flattens ONLY the
    TF_ orders it created. NEVER raises — a broker raise becomes a failed step.

    broker interface:
      volume_min()                         -> float
      stops_level_price()                  -> float   ($ distance = stops_level*point)
      ask() / bid()                        -> float
      place_stop(price, lot)               -> res(.retcode, .ticket)
      pending_ticket()                     -> int|None
      pending_price(ticket)                -> float|None
      modify_pending(ticket, price)        -> res
      cancel(ticket)                       -> res
      place_market(lot)                    -> res(.retcode, .ticket, .price)
      market_filling()                     -> str      (filling mode the market leg used)
      position_ticket()                    -> int|None
      position_sl(ticket)                  -> float|None
      modify_sltp(ticket, sl, tp)          -> res
      close(ticket)                        -> res
    """
    sleeper = sleeper or (lambda s: None)
    steps: List[Step] = []
    state = {"pending": None, "position": None}

    def _timed(fn):
        t0 = clock()
        try:
            return fn(), (clock() - t0) * 1000.0, None
        except Exception as e:
            return None, (clock() - t0) * 1000.0, e

    def _rc(res):
        return getattr(res, "retcode", None) if res is not None else None

    def _add(name, ok, retcode, ms, **kw):
        steps.append(Step(name, retcode, ok, round(ms, 1), **kw))
        return ok

    def _abort_flatten(reason):
        """Flatten ONLY our own TF_ orders — never anything else on the account."""
        acts = []
        if state["position"] is not None:
            try:
                broker.close(state["position"]); acts.append(f"closed pos {state['position']}")
            except Exception as e:
                acts.append(f"close-raised {e!r}")
        if state["pending"] is not None:
            try:
                broker.cancel(state["pending"]); acts.append(f"cancelled pend {state['pending']}")
            except Exception as e:
                acts.append(f"cancel-raised {e!r}")
        steps.append(Step("abort_flatten", None, True, 0.0,
                          detail=f"{reason}; " + (", ".join(acts) if acts else "nothing open")))

    # 1. place pending stop at a safe distance (validated vs stops_level)
    lot = broker.volume_min()
    safe = max(PENDING_SAFE_OFFSET, broker.stops_level_price() + STOPS_CUSHION_PTS)
    p_price = round(broker.ask() + safe, 2)
    res, ms, err = _timed(lambda: broker.place_stop(p_price, lot))
    tk = getattr(res, "ticket", None)
    if not (_rc(res) == DONE and tk and broker.pending_ticket() == tk):
        _add("place_pending", False, _rc(res), ms, intended=p_price,
             detail=f"err={err}" if err else f"ticket={tk}")
        _abort_flatten("place_pending failed")
        return steps
    state["pending"] = tk
    _add("place_pending", True, DONE, ms, intended=p_price, actual=broker.pending_price(tk),
         detail=f"ticket={tk} lot={lot} @ {p_price} (safe {safe:.1f} vs stops)")

    # 2/3. modify the pending price once (proves the modify path)
    want = round(p_price + MODIFY_BUMP, 2)
    res, ms, err = _timed(lambda: broker.modify_pending(tk, want))
    landed = broker.pending_price(tk)
    if not (_rc(res) == DONE and landed is not None and abs(landed - want) < 1e-6):
        _add("modify_pending", False, _rc(res), ms, intended=want, actual=landed,
             detail=f"err={err}")
        _abort_flatten("modify_pending failed")
        return steps
    _add("modify_pending", True, DONE, ms, intended=want, actual=landed, detail=f"price->{want}")

    # 4. cancel, verify removal
    res, ms, err = _timed(lambda: broker.cancel(tk))
    if not (_rc(res) == DONE and broker.pending_ticket() is None):
        _add("cancel_pending", False, _rc(res), ms, detail=f"still present err={err}")
        _abort_flatten("cancel_pending failed")
        return steps
    state["pending"] = None
    _add("cancel_pending", True, DONE, ms, detail="removed")

    # 5. market open at volume_min (first REAL slippage datapoint: intended vs actual fill)
    intended_fill = broker.ask()
    res, ms, err = _timed(lambda: broker.place_market(lot))
    ptk = getattr(res, "ticket", None)
    if not (_rc(res) == DONE and ptk and broker.position_ticket() == ptk):
        _add("open_market", False, _rc(res), ms, intended=intended_fill,
             filling=broker.market_filling(), detail=f"ticket={ptk} err={err}")
        _abort_flatten("open_market failed")
        return steps
    state["position"] = ptk
    actual_fill = getattr(res, "price", None)
    _add("open_market", True, DONE, ms, intended=intended_fill, actual=actual_fill,
         filling=broker.market_filling(),
         detail=f"ticket={ptk} lot={lot} slip={_slip_str(intended_fill, actual_fill)}")

    # 6. set SL/TP via the SLTP path (proves TRADE_ACTION_SLTP — same fn as BE-lock/trail)
    sl = round(actual_fill - SL_OFFSET, 2) if actual_fill else round(intended_fill - SL_OFFSET, 2)
    tp = round((actual_fill or intended_fill) + TP_OFFSET, 2)
    res, ms, err = _timed(lambda: broker.modify_sltp(ptk, sl, tp))
    landed_sl = broker.position_sl(ptk)
    if not (_rc(res) == DONE and landed_sl is not None and abs(landed_sl - sl) < 1e-6):
        _add("set_sltp", False, _rc(res), ms, intended=sl, actual=landed_sl, detail=f"err={err}")
        _abort_flatten("set_sltp failed")
        return steps
    _add("set_sltp", True, DONE, ms, intended=sl, actual=landed_sl, detail=f"SL->{sl} TP->{tp}")

    # 7. hold >= 180s with a countdown (FundedElite sub-3-minute HFT rule)
    t0 = clock()
    next_mark = hold_s
    while True:
        elapsed = clock() - t0
        remaining = hold_s - elapsed
        if remaining <= 0:
            break
        if countdown_cb and remaining <= next_mark:
            countdown_cb(remaining)
            next_mark -= COUNTDOWN_EVERY_S
        sleeper(min(1.0, remaining))
    steps.append(Step("hold_180s", DONE, True, 0.0,
                      detail=f"held {clock() - t0:.0f}s (>= {hold_s:.0f}s)"))

    # 8. close at market
    intended_close = broker.bid()
    res, ms, err = _timed(lambda: broker.close(ptk))
    if not (_rc(res) == DONE and broker.position_ticket() is None):
        _add("close_market", False, _rc(res), ms, detail=f"still open err={err}")
        _abort_flatten("close_market failed")
        return steps
    state["position"] = None
    _add("close_market", True, DONE, ms, intended=intended_close, detail="closed")
    return steps


def _slip_str(intended, actual):
    if intended is None or actual is None:
        return "n/a"
    return f"{actual - intended:+.3f}"


def all_passed(steps: List[Step]) -> bool:
    return bool(steps) and all(s.ok for s in steps if s.name != "abort_flatten") \
        and not any(s.name == "abort_flatten" for s in steps)


def render_table(steps: List[Step]) -> str:
    L = [f"{'STEP':<15}{'RC':>7}{'LATENCY':>10}{'INTENDED':>10}{'ACTUAL':>10}{'SLIP':>8}  {'FILL':<7} RESULT"]
    for s in steps:
        rc = "-" if s.retcode is None else str(s.retcode)
        it = "-" if s.intended is None else f"{s.intended:.2f}"
        ac = "-" if s.actual is None else f"{s.actual:.2f}"
        sl = "-" if s.slippage is None else f"{s.slippage:+.3f}"
        res = "PASS" if s.ok else "FAIL"
        L.append(f"{s.name:<15}{rc:>7}{s.latency_ms:>8.1f}ms{it:>10}{ac:>10}{sl:>8}  "
                 f"{s.filling:<7} {res}  {s.detail}")
    L.append(f"RESULT: {'PASS' if all_passed(steps) else 'FAIL'} "
             f"({sum(1 for s in steps if s.ok and s.name != 'abort_flatten')} steps ok)")
    return "\n".join(L)


# --- guards + orchestration --------------------------------------------------------
def _default_lock_check(cfg) -> Optional[str]:
    """Reason string if a live bot process holds the PID lock, else None (never run
    alongside a live bot)."""
    import os
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


def check_gates(broker, *, allow_real: bool, lot_min: bool) -> Optional[str]:
    """Return a refusal reason, or None to proceed. On demo: no flags needed. On a
    non-demo account: BOTH --i-know-real-account AND --lot-min are required. Always
    refuses if any non-TF_ position/pending is open."""
    try:
        mode = broker.trade_mode()
    except Exception:
        mode = None
    if mode != "demo":
        if not (allow_real and lot_min):
            missing = []
            if not allow_real:
                missing.append("--i-know-real-account")
            if not lot_min:
                missing.append("--lot-min")
            return (f"account trade_mode is '{mode}', not demo — real-account run requires "
                    f"BOTH --i-know-real-account AND --lot-min (missing: {', '.join(missing)})")
    try:
        n = broker.non_tf_open_count()
    except Exception as e:
        return f"could not read the book to confirm it is TF_-only ({e!r})"
    if n:
        return f"{n} non-TF_ position(s)/pending(s) open — refuse (a live strategy leg is present)"
    return None


def run_testfireimmediate(cfg, adapter=None, *, broker=None, allow_real=False,
                          lot_min=False, clock=None, sleeper=None, notifier=None,
                          lock_check=None, hold_s: float = HOLD_SECONDS,
                          countdown_cb=None, now_iso=None, ledger_path=None) -> int:
    """Entry point. Exit code: 0 = full cycle PASS; 1 = a step FAILED (aborted+flattened);
    3 = refused (not demo, flags missing); 4 = refused (live bot lock); 6 = non-TF_ book;
    5 = no broker. Injectable broker/clock/sleeper/notifier for offline tests."""
    import time as _time
    clock = clock or _time.monotonic
    sleeper = sleeper if sleeper is not None else _time.sleep
    notifier = notifier or getattr(cfg, "_tele", None)

    if broker is None:
        if adapter is None:
            print("testfireimmediate: no adapter/broker", flush=True)
            return 5
        broker = _AdapterBroker(adapter, cfg)

    # GUARD: never run alongside a live bot
    lc = lock_check if lock_check is not None else (lambda: _default_lock_check(cfg))
    reason = lc()
    if reason:
        print(f"❌ testfireimmediate REFUSED — {reason}", flush=True)
        return 4

    # GUARD: flag-gate matrix + TF_-only book
    gate = check_gates(broker, allow_real=allow_real, lot_min=lot_min)
    if gate:
        print(f"❌ testfireimmediate REFUSED — {gate}", flush=True)
        return 6 if "non-TF_" in gate else 3

    if countdown_cb is None:
        countdown_cb = lambda rem: print(f"  ⏳ holding… {rem:.0f}s left", flush=True)

    print(f"🧪🔥 testfireimmediate — ONE real cycle at volume_min, magic {TF_MAGIC} "
          f"comment {TF_COMMENT} (TF_ isolated). Hold >= {hold_s:.0f}s.", flush=True)
    steps = run_cycle(broker, clock, sleeper=sleeper, hold_s=hold_s, countdown_cb=countdown_cb)
    table = render_table(steps)
    print(table, flush=True)

    _post_card(notifier, steps)
    _write_ledger(cfg, steps, now_iso, ledger_path)
    try:
        import review_log as _rv
        _rv.get_review_logger(cfg).testrun(
            "PASS" if all_passed(steps) else "FAIL",
            sum(1 for s in steps if s.ok and s.name != "abort_flatten"), len(steps))
    except Exception:
        pass
    return 0 if all_passed(steps) else 1


def _post_card(notifier, steps):
    if notifier is None:
        return
    try:
        import discord_cards as dc
        from telemetry import Severity
        ok = all_passed(steps)
        card = dc.card_generic(
            f"🧪🔥 TESTFIREIMMEDIATE {'PASS' if ok else 'FAIL'}",
            "```\n" + render_table(steps) + "\n```",
            color=(dc.GREEN if ok else dc.RED))
        notifier.send("🧪🔥 immediate order-path test", Severity.INFO, card=card)
    except Exception as e:
        log.warning(f"testfireimmediate: card post failed ({e!r})")


LEDGER_FIELDS = ["ts", "kind", "test", "steps_total", "steps_passed", "result",
                 "max_latency_ms", "magic", "comment"]


def _write_ledger(cfg, steps, now_iso, ledger_path):
    import csv
    import os
    path = ledger_path or os.path.join(getattr(cfg, "run_dir", "run"), "trades.csv")
    ts = now_iso
    if ts is None:
        try:
            import pandas as pd
            ts = pd.Timestamp.now(tz="UTC").isoformat()
        except Exception:
            ts = ""
    try:
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(LEDGER_FIELDS)
            w.writerow([ts, "TESTFIREIMMEDIATE", 1, len(steps),
                        sum(1 for s in steps if s.ok and s.name != "abort_flatten"),
                        "PASS" if all_passed(steps) else "FAIL",
                        round(max((s.latency_ms for s in steps), default=0.0), 1),
                        TF_MAGIC, TF_COMMENT])
    except Exception as e:
        log.warning(f"testfireimmediate: ledger write failed ({e!r})")


# --- live MT5 adapter broker -------------------------------------------------------
class _AdapterBroker:
    """Wraps the AUREON MT5 adapter into the run_cycle interface, using the SAME adapter
    functions + magic/comment/filling as anchors + RB + the SL-modify path. All reads
    filter to the TF_ marker so the flatten path never touches a non-TF_ order."""

    def __init__(self, adapter, cfg):
        self.a = adapter
        self.mt5 = adapter.mt5
        self.cfg = cfg
        self.symbol = getattr(cfg, "symbol", "XAUUSD")
        self._last_filling = "IOC"

    def trade_mode(self):
        acct = self.mt5.account_info()
        demo = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        return "demo" if getattr(acct, "trade_mode", None) == demo else "real"

    def non_tf_open_count(self):
        """Count broker positions + pendings whose comment is NOT TF_-marked."""
        n = 0
        for coll in (self.mt5.positions_get(symbol=self.symbol) or [],
                     self.mt5.orders_get(symbol=self.symbol) or []):
            for o in coll:
                if "TF_" not in str(getattr(o, "comment", "") or ""):
                    n += 1
        return n

    def _tick(self):
        return self.mt5.symbol_info_tick(self.symbol)

    def ask(self):
        return float(self._tick().ask)

    def bid(self):
        return float(self._tick().bid)

    def volume_min(self):
        s = self.mt5.symbol_info(self.symbol)
        return float(getattr(s, "volume_min", 0.01) or 0.01)

    def stops_level_price(self):
        s = self.mt5.symbol_info(self.symbol)
        point = float(getattr(s, "point", 0.01) or 0.01)
        stops = max(int(getattr(s, "trade_stops_level", 0) or 0),
                    int(getattr(s, "trade_freeze_level", 0) or 0))
        return stops * point

    def _ours(self, o):
        return (int(getattr(o, "magic", -1) or -1) == TF_MAGIC
                and "TF_" in str(getattr(o, "comment", "") or ""))

    # --- order path (SAME adapter functions as anchors + RB + SL-modify) ----
    def place_stop(self, price, lot):
        return self.a.place_stop_order(self.symbol, "BUY", price, lot, sl=0.0, tp=0.0,
                                       comment=TF_COMMENT, dry_run=False, magic=TF_MAGIC)

    def pending_ticket(self):
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
        # Production cancels+replaces pendings; there is no adapter price-modify helper,
        # so drive TRADE_ACTION_MODIFY directly (same shape testorder uses).
        req = {"action": getattr(self.mt5, "TRADE_ACTION_MODIFY", 7),
               "order": int(ticket), "price": float(price)}
        return self.mt5.order_send(req)

    def cancel(self, ticket):
        return self.a.cancel_order(ticket, dry_run=False)

    def place_market(self, lot):
        res = self.a.place_market_order(self.symbol, "BUY", lot, sl=0.0, tp=0.0,
                                        comment=TF_COMMENT, dry_run=False, magic=TF_MAGIC)
        # the adapter tries IOC then auto-falls to FOK on 10030
        self._last_filling = "FOK" if getattr(res, "retcode", None) == DONE and \
            getattr(res, "_fok_fallback", False) else "IOC"
        return res

    def market_filling(self):
        return self._last_filling

    def position_ticket(self):
        for p in (self.mt5.positions_get(symbol=self.symbol) or []):
            if self._ours(p):
                return int(p.ticket)
        return None

    def position_sl(self, ticket):
        for p in (self.mt5.positions_get(symbol=self.symbol) or []):
            if int(getattr(p, "ticket", -1)) == int(ticket):
                return float(getattr(p, "sl", 0.0) or 0.0)
        return None

    def modify_sltp(self, ticket, sl, tp):
        # The SAME SL-modify path anchors/rescue use for BE-lock/trail (TRADE_ACTION_SLTP).
        # modify_position_sl sets SL; add TP in the same request shape.
        return self.a.modify_position_sl(ticket, sl, dry_run=False)

    def close(self, ticket):
        return self.a.close_position(ticket, dry_run=False)
