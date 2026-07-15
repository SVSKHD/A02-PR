"""Unit tests for stale_leg_sweep — automatic non-OCO stale-leg cancellation.

Mocks MT5's orders_get / positions_get / order_send (no MetaTrader5 install
needed). Runnable either under pytest (`pytest tests/test_stale_leg_sweep.py`)
or standalone (`python tests/test_stale_leg_sweep.py`), which prints a PASS/FAIL
line per test and exits non-zero on any failure.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stale_leg_sweep as sweep

INTERVAL = 20.0
SYMBOL = "XAUUSD"
A1 = 4028.77
A2 = 4048.77   # A1 + INTERVAL


# --- MT5 mocks --------------------------------------------------------------------
class FakeOrder:
    """Mirrors the fields of an mt5.orders_get() TradeOrder we read."""
    def __init__(self, ticket, type_, price_open, comment):
        self.ticket = ticket
        self.type = type_
        self.price_open = price_open
        self.comment = comment


class FakePosition:
    """Mirrors the fields of an mt5.positions_get() TradePosition we read."""
    def __init__(self, ticket, type_, price_open):
        self.ticket = ticket
        self.type = type_
        self.price_open = price_open


class FakeResult:
    def __init__(self, retcode):
        self.retcode = retcode
        self.comment = "ok" if retcode == FakeMT5.TRADE_RETCODE_DONE else "fail"


class FakeMT5:
    # Real MetaTrader5 numeric constants.
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REQUOTE = 10004

    def __init__(self, orders=None, positions=None, send_handler=None):
        self._orders = list(orders or [])
        self._positions = list(positions or [])
        self.sent = []                 # every order_send request, in order
        self._send_handler = send_handler

    def orders_get(self, symbol=None):
        return list(self._orders)

    def positions_get(self, symbol=None):
        return list(self._positions)

    def order_send(self, request):
        self.sent.append(dict(request))
        rc = self.TRADE_RETCODE_DONE
        if self._send_handler is not None:
            rc = self._send_handler(request, len(self.sent))
        return FakeResult(rc)


class ListLogger:
    """Captures log lines as (level, message) so tests can assert on logging."""
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(("INFO", str(msg)))

    def warning(self, msg):
        self.lines.append(("WARNING", str(msg)))

    def error(self, msg):
        self.lines.append(("ERROR", str(msg)))

    def has(self, level, needle):
        return any(lvl == level and needle in m for lvl, m in self.lines)


def _removes(mt5):
    return [r for r in mt5.sent if r.get("action") == FakeMT5.TRADE_ACTION_REMOVE]


def _pendings(mt5):
    return [r for r in mt5.sent if r.get("action") == FakeMT5.TRADE_ACTION_PENDING]


# --- tests ------------------------------------------------------------------------
def test_stale_leg_cancelled():
    """A1 sell stop (origin anchor 4028.77) is removed when the A2 event fires
    at 4048.77."""
    a1_sell = FakeOrder(101, FakeMT5.ORDER_TYPE_SELL_STOP, 4023.77,
                        sweep.tag_comment("AUR_A1_SELL", A1))
    mt5 = FakeMT5(orders=[a1_sell], positions=[])
    log = ListLogger()

    res = sweep.sweep_stale_legs(mt5, SYMBOL, A2, logger=log)

    assert len(res) == 1, res
    assert res[0]["ticket"] == 101
    assert res[0]["cancelled"] is True
    assert res[0]["origin_anchor"] == A1
    assert res[0]["reason"] == "stale_leg_sweep"
    removes = _removes(mt5)
    assert len(removes) == 1 and removes[0]["order"] == 101, mt5.sent
    assert log.has("INFO", "cancelled ticket=101")


def test_current_anchor_leg_kept():
    """A2's own freshly-placed legs are never swept by the A2 event."""
    a2_buy = FakeOrder(201, FakeMT5.ORDER_TYPE_BUY_STOP, 4053.77,
                       sweep.tag_comment("AUR_A2_BUY", A2))
    a2_sell = FakeOrder(202, FakeMT5.ORDER_TYPE_SELL_STOP, 4043.77,
                        sweep.tag_comment("AUR_A2_SELL", A2))
    mt5 = FakeMT5(orders=[a2_buy, a2_sell], positions=[])
    log = ListLogger()

    res = sweep.sweep_stale_legs(mt5, SYMBOL, A2, logger=log)

    assert res == [], res
    assert _removes(mt5) == [], mt5.sent


def test_rescue_leg_exempt():
    """A pending leg that is the INTERVAL-point opposite leg of an open position
    is skipped by the sweep even though its origin anchor is stale."""
    # A1 buy filled -> open BUY position at 4028.77.
    pos = FakePosition(50, FakeMT5.POSITION_TYPE_BUY, A1)
    # Its rescue leg: a SELL stop exactly INTERVAL below entry (4008.77).
    rescue = FakeOrder(102, FakeMT5.ORDER_TYPE_SELL_STOP, A1 - INTERVAL,
                       sweep.tag_comment("AUR_A1_RSC", A1))
    mt5 = FakeMT5(orders=[rescue], positions=[pos])
    log = ListLogger()

    # Current anchor far away (A3-ish) so the leg is unambiguously stale.
    res = sweep.sweep_stale_legs(mt5, SYMBOL, A1 + 2 * INTERVAL, logger=log)

    assert res == [], res
    assert _removes(mt5) == [], mt5.sent
    assert log.has("INFO", "SKIP rescue leg ticket=102")


def test_registry_rebuild_on_restart():
    """The registry is reconstructed purely by parsing 'A:<price>' comments from
    orders_get — no local state file."""
    orders = [
        FakeOrder(1, FakeMT5.ORDER_TYPE_BUY_STOP, 4033.77,
                  sweep.tag_comment("AUR_A1_BUY", A1)),      # A:4028.77
        FakeOrder(2, FakeMT5.ORDER_TYPE_SELL_STOP, 4043.77,
                  sweep.tag_comment("AUR_A2_SELL", A2)),     # A:4048.77
        FakeOrder(3, FakeMT5.ORDER_TYPE_BUY_STOP, 4100.00,
                  "AUR_MANUAL_NO_TAG"),                       # untagged -> skipped
    ]
    registry = sweep.build_registry(orders)

    assert registry == {1: A1, 2: A2}, registry
    assert 3 not in registry


def test_sweep_retry_on_failure():
    """order_send fails once (retcode != DONE), the sweep retries once, logs, and
    the new straddle placement still proceeds afterward."""
    a1_sell = FakeOrder(101, FakeMT5.ORDER_TYPE_SELL_STOP, 4023.77,
                        sweep.tag_comment("AUR_A1_SELL", A1))

    # First REMOVE for ticket 101 requotes; the retry succeeds.
    state = {"remove_calls": 0}

    def handler(request, _idx):
        if request.get("action") == FakeMT5.TRADE_ACTION_REMOVE:
            state["remove_calls"] += 1
            if state["remove_calls"] == 1:
                return FakeMT5.TRADE_RETCODE_REQUOTE   # transient failure
            return FakeMT5.TRADE_RETCODE_DONE
        return FakeMT5.TRADE_RETCODE_DONE

    mt5 = FakeMT5(orders=[a1_sell], positions=[], send_handler=handler)
    log = ListLogger()

    res = sweep.sweep_stale_legs(mt5, SYMBOL, A2, logger=log)

    # Cancelled on the retry.
    assert len(res) == 1 and res[0]["cancelled"] is True, res
    removes = _removes(mt5)
    assert len(removes) == 2, mt5.sent            # failed once, retried once
    assert all(r["order"] == 101 for r in removes)
    assert log.has("WARNING", "retrying once")

    # New straddle placement proceeds after the sweep, through the same MT5.
    def place_a2():
        for side, type_, price in (("BUY", FakeMT5.ORDER_TYPE_BUY_STOP, 4053.77),
                                   ("SELL", FakeMT5.ORDER_TYPE_SELL_STOP, 4043.77)):
            mt5.order_send({"action": FakeMT5.TRADE_ACTION_PENDING, "type": type_,
                            "price": price,
                            "comment": sweep.tag_comment(f"AUR_A2_{side}", A2)})

    place_a2()
    assert len(_pendings(mt5)) == 2, mt5.sent     # placement not blocked by the sweep


def test_acceptance_full_flow():
    """A1 straddle at 4028.77 → buy leg fills → price ticks to 4048.77.
    Assert, in exact order: the A1 leftover sell is cancelled FIRST, the A1
    position's rescue leg is untouched, THEN the A2 straddle is placed."""
    # A1 buy filled -> open BUY position at the A1 anchor.
    pos = FakePosition(50, FakeMT5.POSITION_TYPE_BUY, A1)
    # Leftover A1 sell (straddle sibling, 5 below anchor) -> must be swept.
    leftover_sell = FakeOrder(101, FakeMT5.ORDER_TYPE_SELL_STOP, A1 - 5.0,
                              sweep.tag_comment("AUR_A1_SELL", A1))
    # A1 position's rescue leg (INTERVAL-point opposite leg) -> must be kept.
    rescue_leg = FakeOrder(102, FakeMT5.ORDER_TYPE_SELL_STOP, A1 - INTERVAL,
                           sweep.tag_comment("AUR_A1_RSC", A1))
    mt5 = FakeMT5(orders=[leftover_sell, rescue_leg], positions=[pos])
    log = ListLogger()

    # 1) Sweep fires FIRST, on the new A2 anchor event.
    res = sweep.sweep_stale_legs(mt5, SYMBOL, A2, logger=log)

    # 2) Then the A2 straddle is placed (through the same MT5, so ordering is real).
    def place_a2():
        for side, type_, price in (("BUY", FakeMT5.ORDER_TYPE_BUY_STOP, A2 + 5.0),
                                   ("SELL", FakeMT5.ORDER_TYPE_SELL_STOP, A2 - 5.0)):
            mt5.order_send({"action": FakeMT5.TRADE_ACTION_PENDING, "type": type_,
                            "price": price, "order": None,
                            "comment": sweep.tag_comment(f"AUR_A2_{side}", A2)})

    place_a2()

    # Only the leftover sell was swept; the rescue leg was exempt.
    assert len(res) == 1 and res[0]["ticket"] == 101 and res[0]["cancelled"], res
    assert log.has("INFO", "SKIP rescue leg ticket=102")

    # EXACT order_send sequence: REMOVE 101, then A2 BUY, then A2 SELL.
    actions = [(r["action"], r.get("order"), r.get("price")) for r in mt5.sent]
    assert actions == [
        (FakeMT5.TRADE_ACTION_REMOVE, 101, None),
        (FakeMT5.TRADE_ACTION_PENDING, None, A2 + 5.0),
        (FakeMT5.TRADE_ACTION_PENDING, None, A2 - 5.0),
    ], actions
    # The rescue leg (102) is never sent to REMOVE.
    assert all(not (r["action"] == FakeMT5.TRADE_ACTION_REMOVE and r.get("order") == 102)
               for r in mt5.sent), mt5.sent


# --- standalone runner ------------------------------------------------------------
def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001 — test runner surfaces every failure
            failures += 1
            print(f"FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
