"""testorder (order-path verification) — offline, MT5 mocked.

Runnable under pytest or standalone (`python tests/test_testorder.py`).
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import testorder as TO
from config import Config

STEP_NAMES = ["startup_assertions", "place_pending", "modify_pending",
              "cancel_pending", "open_market", "modify_sl", "close_market"]


class Res:
    def __init__(self, retcode, ticket=None):
        self.retcode = retcode
        self.ticket = ticket


class FakeBroker:
    def __init__(self, mode="demo", fail_at=None, startup_raises=False):
        self.mode, self.fail_at, self.startup_raises = mode, fail_at, startup_raises
        self._pend, self._pos, self._t = {}, {}, 500

    def startup_assertions(self):
        if self.startup_raises:
            raise RuntimeError("margin_mode is not RETAIL_HEDGING")

    def trade_mode(self): return self.mode
    def mid(self): return 4000.0

    def place_stop(self, price, lot, comment):
        if self.fail_at == "place_pending":
            return Res(10016)
        self._t += 1; self._pend[self._t] = {"price": price, "comment": comment}
        return Res(10009, self._t)

    def pending_ticket(self, comment):
        return next((t for t, o in self._pend.items() if comment in o["comment"]), None)

    def pending_price(self, ticket):
        o = self._pend.get(int(ticket)); return o["price"] if o else None

    def modify_pending(self, ticket, price):
        if self.fail_at == "modify_pending":
            return Res(10016)
        self._pend[int(ticket)]["price"] = price; return Res(10009)

    def cancel(self, ticket):
        if self.fail_at == "cancel_pending":
            return Res(10016)
        self._pend.pop(int(ticket), None); return Res(10009)

    def place_market(self, lot, comment, sl):
        if self.fail_at == "open_market":
            return Res(10016)
        self._t += 1; self._pos[self._t] = {"sl": sl, "comment": comment}
        return Res(10009, self._t)

    def position_ticket(self, comment):
        return next((t for t, p in self._pos.items() if comment in p["comment"]), None)

    def position_sl(self, ticket):
        p = self._pos.get(int(ticket)); return p["sl"] if p else None

    def modify_sl(self, ticket, sl):
        if self.fail_at == "modify_sl":
            return Res(10016)
        self._pos[int(ticket)]["sl"] = sl; return Res(10009)

    def close(self, ticket):
        if self.fail_at == "close_market":
            return Res(10016)
        self._pos.pop(int(ticket), None); return Res(10009)


def _clock():
    c = [0.0]
    def clk():
        c[0] += 0.001
        return c[0]
    return clk


def _run(broker, tmp, **kw):
    return TO.run_testorder(Config(), broker=broker, clock=_clock(),
                            lock_check=kw.pop("lock_check", lambda: None),
                            ledger_path=str(tmp), now_iso="2026-07-18T00:00:00Z", **kw)


# --- full pass --------------------------------------------------------------------
def test_full_pass(tmp_path):
    code = _run(FakeBroker(), tmp_path / "t.csv")
    assert code == 0


def test_all_steps_pass_and_ordered():
    steps = TO.run_steps(FakeBroker(), _clock())
    assert [s.name for s in steps] == STEP_NAMES
    assert all(s.ok and s.retcode == 10009 for s in steps)
    assert TO.all_passed(steps)


# --- each failure mode -> nonzero exit + named failing step -----------------------
def test_each_failure_mode(tmp_path):
    for step in ["place_pending", "modify_pending", "cancel_pending",
                 "open_market", "modify_sl", "close_market"]:
        steps = TO.run_steps(FakeBroker(fail_at=step), _clock())
        assert not TO.all_passed(steps)
        assert steps[-1].name == step and steps[-1].ok is False, step
        assert steps[-1].retcode == 10016
        code = _run(FakeBroker(fail_at=step), tmp_path / f"{step}.csv")
        assert code == 1, step


def test_startup_raise_fails_first_step():
    steps = TO.run_steps(FakeBroker(startup_raises=True), _clock())
    assert len(steps) == 1 and steps[0].name == "startup_assertions" and not steps[0].ok


# --- guards -----------------------------------------------------------------------
def test_funded_account_refused(tmp_path):
    code = _run(FakeBroker(mode="real"), tmp_path / "t.csv")
    assert code == 3                                   # refused: not demo


def test_funded_override_allows_real(tmp_path):
    code = _run(FakeBroker(mode="real"), tmp_path / "t.csv", allow_real=True)
    assert code == 0                                   # override -> proceeds


def test_lockfile_refused(tmp_path):
    code = _run(FakeBroker(), tmp_path / "t.csv",
                lock_check=lambda: "live AUREON process pid=123 holds run/aureon.pid")
    assert code == 4                                   # refused: live bot running


# --- ledger row (test=1, excluded from stats) -------------------------------------
def test_ledger_row_written(tmp_path):
    path = tmp_path / "trades.csv"
    _run(FakeBroker(), path)
    rows = list(csv.reader(open(path)))
    assert rows[0] == TO.LEDGER_FIELDS
    row = dict(zip(rows[0], rows[1]))
    assert row["kind"] == "TESTORDER" and row["test"] == "1" and row["result"] == "PASS"
    assert int(row["steps_passed"]) == int(row["steps_total"]) == len(STEP_NAMES)
    assert row["magic"] == str(TO.TESTORDER_MAGIC)


# --- interaction: exempt from stale_leg_sweep -------------------------------------
def test_testorder_exempt_from_sweep():
    import stale_leg_sweep as sweep
    assert sweep._is_rescue_boost_comment("TESTORDER") is True


# --- standalone runner ------------------------------------------------------------
def _run_all():
    import tempfile, pathlib, inspect
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for name, fn in tests:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as d:
                    fn(pathlib.Path(d))
            else:
                fn()
            print(f"PASS  {name}")
        except Exception as e:
            fails += 1
            import traceback; print(f"FAIL  {name}: {e!r}"); traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
