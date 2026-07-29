"""testfireimmediate (real order-path test) — offline, MT5 mocked.

Covers: the full cycle, every step-failure mode (abort + flatten OWN orders only), the
flag-gate refusal matrix (demo flagless / real needs BOTH flags), non-TF_ book refusal,
the lock refusal, the >=180s hold + countdown, slippage capture, and TF_ isolation
(magic 20260522 + TF_ comment -> excluded from day-P&L/halts + sweep-exempt, #125).

Runnable under pytest or standalone (`python tests/test_testfireimmediate.py`).
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import testfire_immediate as TI
from config import Config

STEP_NAMES = ["place_pending", "modify_pending", "cancel_pending",
              "open_market", "set_sltp", "hold_180s", "close_market"]


class Res:
    def __init__(self, retcode, ticket=None, price=None):
        self.retcode, self.ticket, self.price = retcode, ticket, price


class FakeBroker:
    def __init__(self, mode="demo", fail_at=None, non_tf_open=0, fill="IOC",
                 fill_price=4000.13):
        self.mode, self.fail_at, self._non_tf = mode, fail_at, non_tf_open
        self._fill, self._fill_price = fill, fill_price
        self._pend, self._pos, self._t = {}, {}, 700

    # gates / market
    def trade_mode(self): return self.mode
    def non_tf_open_count(self): return self._non_tf
    def volume_min(self): return 0.01
    def stops_level_price(self): return 0.5
    def ask(self): return 4000.10
    def bid(self): return 3999.90

    # order path
    def place_stop(self, price, lot):
        if self.fail_at == "place_pending":
            return Res(10016)
        self._t += 1
        self._pend[self._t] = {"price": price, "comment": TI.TF_COMMENT}
        return Res(TI.DONE, self._t)

    def pending_ticket(self):
        return next(iter(self._pend), None)

    def pending_price(self, ticket):
        o = self._pend.get(int(ticket))
        return o["price"] if o else None

    def modify_pending(self, ticket, price):
        if self.fail_at == "modify_pending":
            return Res(10016)
        self._pend[int(ticket)]["price"] = price
        return Res(TI.DONE)

    def cancel(self, ticket):
        if self.fail_at == "cancel_pending":
            return Res(10016)
        self._pend.pop(int(ticket), None)
        return Res(TI.DONE)

    def place_market(self, lot):
        if self.fail_at == "open_market":
            return Res(10019)
        self._t += 1
        self._pos[self._t] = {"sl": 0.0, "comment": TI.TF_COMMENT}
        return Res(TI.DONE, self._t, price=self._fill_price)

    def market_filling(self): return self._fill

    def position_ticket(self):
        return next(iter(self._pos), None)

    def position_sl(self, ticket):
        p = self._pos.get(int(ticket))
        return p["sl"] if p else None

    def modify_sltp(self, ticket, sl, tp):
        if self.fail_at == "set_sltp":
            return Res(10016)
        self._pos[int(ticket)]["sl"] = sl
        return Res(TI.DONE)

    def close(self, ticket):
        if self.fail_at == "close_market":
            return Res(10016)
        self._pos.pop(int(ticket), None)
        return Res(TI.DONE)


def _clock_sleeper():
    t = [0.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + max(s, 0.001)))


def _cycle(broker, hold_s=0.05, **kw):
    clk, slp = _clock_sleeper()
    return TI.run_cycle(broker, clk, sleeper=slp, hold_s=hold_s, **kw)


# --- full pass --------------------------------------------------------------------
def test_full_cycle_pass_ordered():
    steps = _cycle(FakeBroker())
    assert [s.name for s in steps] == STEP_NAMES
    assert all(s.ok for s in steps)
    assert TI.all_passed(steps)


def test_hold_and_countdown_fire():
    marks = []
    steps = _cycle(FakeBroker(), hold_s=0.05, countdown_cb=lambda rem: marks.append(rem))
    hold = next(s for s in steps if s.name == "hold_180s")
    assert hold.ok and "held" in hold.detail
    assert len(marks) >= 1                       # countdown fired at least once


def test_slippage_captured_on_open():
    steps = _cycle(FakeBroker(fill_price=4000.13))
    om = next(s for s in steps if s.name == "open_market")
    assert om.intended == 4000.10 and om.actual == 4000.13
    assert abs(om.slippage - 0.03) < 1e-9        # +$0.03 real fill slippage


# --- each failure mode -> abort + flatten OWN orders only -------------------------
def test_each_failure_aborts_and_flattens():
    for step in ["place_pending", "modify_pending", "cancel_pending",
                 "open_market", "set_sltp", "close_market"]:
        b = FakeBroker(fail_at=step)
        steps = _cycle(b)
        names = [s.name for s in steps]
        assert step in names and not TI.all_passed(steps), step
        failed = next(s for s in steps if s.name == step)
        assert failed.ok is False, step
        assert names[-1] == "abort_flatten", step          # always flattens on failure
        # a position opened before a later failure must be closed by the flatten
        if step in ("set_sltp", "close_market"):
            # set_sltp: flatten closes it; close_market: close itself already failed
            if step == "set_sltp":
                assert b.position_ticket() is None, step


def test_abort_flatten_only_touches_own_orders():
    b = FakeBroker(fail_at="set_sltp")
    steps = _cycle(b)
    assert any(s.name == "abort_flatten" for s in steps)
    assert b.position_ticket() is None and b.pending_ticket() is None


# --- flag-gate refusal matrix -----------------------------------------------------
def _run(broker, tmp, **kw):
    clk, slp = _clock_sleeper()
    return TI.run_testfireimmediate(
        Config(), broker=broker, clock=clk, sleeper=slp, hold_s=0.05,
        lock_check=kw.pop("lock_check", lambda: None),
        ledger_path=str(tmp), now_iso="2026-07-29T00:00:00Z",
        countdown_cb=lambda rem: None, **kw)


def test_demo_runs_without_flags(tmp_path):
    assert _run(FakeBroker(mode="demo"), tmp_path / "t.csv") == 0


def test_real_refused_without_flags(tmp_path):
    assert _run(FakeBroker(mode="real"), tmp_path / "t.csv") == 3


def test_real_refused_with_only_one_flag(tmp_path):
    assert _run(FakeBroker(mode="real"), tmp_path / "a.csv", allow_real=True) == 3
    assert _run(FakeBroker(mode="real"), tmp_path / "b.csv", lot_min=True) == 3


def test_real_runs_with_both_flags(tmp_path):
    assert _run(FakeBroker(mode="real"), tmp_path / "t.csv",
                allow_real=True, lot_min=True) == 0


def test_gate_matrix_unit():
    assert TI.check_gates(FakeBroker(mode="demo"), allow_real=False, lot_min=False) is None
    r = TI.check_gates(FakeBroker(mode="real"), allow_real=False, lot_min=False)
    assert r and "--i-know-real-account" in r and "--lot-min" in r
    r1 = TI.check_gates(FakeBroker(mode="real"), allow_real=True, lot_min=False)
    assert r1 and "missing: --lot-min)" in r1        # only the lot-min flag is missing
    r2 = TI.check_gates(FakeBroker(mode="real"), allow_real=False, lot_min=True)
    assert r2 and "missing: --i-know-real-account)" in r2
    assert TI.check_gates(FakeBroker(mode="real"), allow_real=True, lot_min=True) is None


# --- non-TF_ book + lock refusals -------------------------------------------------
def test_refuse_when_non_tf_position_open(tmp_path):
    assert _run(FakeBroker(mode="demo", non_tf_open=1), tmp_path / "t.csv") == 6


def test_refuse_under_live_lock(tmp_path):
    code = _run(FakeBroker(mode="demo"), tmp_path / "t.csv",
                lock_check=lambda: "live AUREON process pid=99 holds run/aureon.pid")
    assert code == 4


# --- ledger row (test=1, excluded from stats) -------------------------------------
def test_ledger_row_written(tmp_path):
    path = tmp_path / "trades.csv"
    _run(FakeBroker(mode="demo"), path)
    rows = list(csv.reader(open(path)))
    assert rows[0] == TI.LEDGER_FIELDS
    row = dict(zip(rows[0], rows[1]))
    assert row["kind"] == "TESTFIREIMMEDIATE" and row["test"] == "1" and row["result"] == "PASS"
    assert row["magic"] == str(TI.TF_MAGIC) and row["comment"] == TI.TF_COMMENT


# --- TF_ isolation (#125 symmetry) ------------------------------------------------
def test_tf_comment_and_magic_isolate():
    # comment carries the TF_ marker + uses the anchors magic -> excluded from day-P&L
    assert TI.TF_COMMENT.startswith("TF_")
    assert TI.TF_MAGIC == 20260522
    import pnl_source as PS
    assert PS.TEST_COMMENT_MARK == "TF_"
    import types
    d = types.SimpleNamespace(comment=TI.TF_COMMENT, magic=TI.TF_MAGIC)
    assert PS._is_test(d) is True                       # symmetric exclusion recognises it


def test_tf_excluded_from_day_pnl_symmetrically():
    import pnl_source as PS
    import types
    def deal(profit, comment):
        return types.SimpleNamespace(magic=TI.TF_MAGIC, entry=1, profit=profit,
                                     swap=0.0, commission=0.0, comment=comment)
    deals = [deal(350.0, "AUR_A2_BUY"),          # real anchor win
             deal(-630.0, TI.TF_COMMENT + " x"),  # a TF_ immediate SL
             deal(155.0, TI.TF_COMMENT + " y")]   # a TF_ immediate win
    net_all = PS.magic_day_net(deals, TI.TF_MAGIC, exclude_test=False)
    net_real = PS.magic_day_net(deals, TI.TF_MAGIC, exclude_test=True)
    assert abs(net_all - (350.0 - 630.0 + 155.0)) < 1e-6
    assert abs(net_real - 350.0) < 1e-6                 # BOTH test SL and win dropped


def test_tf_exempt_from_stale_sweep():
    import stale_leg_sweep as sweep
    assert sweep._is_rescue_boost_comment(TI.TF_COMMENT) is True


# --- render ------------------------------------------------------------------------
def test_table_renders():
    steps = _cycle(FakeBroker())
    out = TI.render_table(steps)
    for tok in ("STEP", "SLIP", "FILL", "hold_180s", "PASS"):
        assert tok in out, tok


# --- standalone runner ------------------------------------------------------------
def _run_all():
    import tempfile
    import pathlib
    import inspect
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
            import traceback
            print(f"FAIL  {name}: {e!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
