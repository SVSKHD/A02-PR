"""2026-07-17 live fixes — offline tests (MT5 mocked).

ISSUE 1 (rogue impl / stop-mode dispatch), ISSUE 2 (test-trade exclusion),
ISSUE 3a (lock-rung exit violation), ISSUE 3b (interrupt-safe telemetry.stop).
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --- ISSUE 1: rogue impl + dispatch -----------------------------------------------
def test_rogue_impl_precedence():
    import rogue as R
    assert R.rogue_impl(types.SimpleNamespace(rogue_stop_mode=True, rogue_a1_anchor_mode=True)) == "stop"
    assert R.rogue_impl(types.SimpleNamespace(rogue_stop_mode=False, rogue_a1_anchor_mode=True)) == "band"
    assert R.rogue_impl(types.SimpleNamespace(rogue_stop_mode=False, rogue_a1_anchor_mode=False)) == "legacy"


def test_stop_mode_dispatch_precludes_band_not_held():
    import rogue as R, rogue_stop as RS
    calls = {"stop": 0, "band": 0, "band_not_held": 0}
    orig = (RS.drive_stop, R._drive_a1, R._ptrace_reject)
    RS.drive_stop = lambda trader, st, allow_new_entries=True, **k: calls.__setitem__("stop", calls["stop"] + 1)
    R._drive_a1 = lambda *a, **k: calls.__setitem__("band", calls["band"] + 1)
    def _pt(trader, st, reason, price, anchor):
        if reason == "BAND_NOT_HELD":
            calls["band_not_held"] += 1
    R._ptrace_reject = _pt
    try:
        cfg = types.SimpleNamespace(rogue_enabled=True, rogue_stop_mode=True,
                                    rogue_a1_anchor_mode=True, rogue_daywatch=True, symbol="XAUUSD")

        class MT5:
            ACCOUNT_TRADE_MODE_DEMO = 0
            def account_info(self):
                return types.SimpleNamespace(trade_mode=0)

        trader = types.SimpleNamespace(cfg=cfg, adapter=types.SimpleNamespace(mt5=MT5()),
                                       state={"last_broker_date": "2026-07-17"}, _rogue=None)
        R.drive(trader)
        assert calls == {"stop": 1, "band": 0, "band_not_held": 0}
        # a STALE band-mode _rogue state must NOT let band run while stop_mode is on
        trader._rogue = {"day": "2026-07-17", "gov": R.new_day_state(), "anchor": 4000.0,
                         "leg_dir": "BUY", "open": None, "a1_last_close": 3990.0}
        R.drive(trader)
        assert calls["stop"] == 2 and calls["band"] == 0 and calls["band_not_held"] == 0
    finally:
        RS.drive_stop, R._drive_a1, R._ptrace_reject = orig


# --- ISSUE 2: symmetric test-trade exclusion --------------------------------------
class _Deal:
    def __init__(self, magic, entry, profit, comment=""):
        self.magic, self.entry, self.profit, self.comment = magic, entry, profit, comment
        self.swap = self.commission = 0.0


def test_is_test_and_symmetric_exclusion():
    import pnl_source as ps
    A = 20260522
    assert ps._is_test(_Deal(A, 1, 0, "TF_AUR_A1_BUY")) is True
    assert ps._is_test(_Deal(A, 1, 0, "AUR_A1_BUY")) is False
    deals = [
        _Deal(A, 1, -630.0, "TF_AUR_A2_BUY"),   # testfire SL  (must be excluded)
        _Deal(A, 1, 155.75, "TF_AUR_A2_SELL"),  # testfire win (must be excluded)
        _Deal(A, 1, 156.10, "AUR_A1_BUY"),      # real win
        _Deal(A, 1, 346.50, "AUR_A3_SELL"),     # real win
        _Deal(A, 1, -6.30, "AUR_A4_BUY"),       # real loss
    ]
    # WITH the fix: only real trades count -> +156.10 +346.50 -6.30 = +496.30 (to the cent)
    assert ps.magic_day_net(deals, A, exclude_test=True) == 496.30
    # legacy (no exclusion) is the one-sided-prone total that counted the test SL too
    assert ps.magic_day_net(deals, A) == round(-630 + 155.75 + 156.10 + 346.50 - 6.30, 2)


# --- ISSUE 3a: lock-rung exit no longer false-violates ----------------------------
def test_lock_arm_exit_no_false_violation():
    from position_telemetry import PositionTracer
    t = PositionTracer()
    t.lock_arm(1, "A1", lock_level=1, stop_price=4001.80)     # a rung engaged
    t.exit(1, "A1", exit_type="TRAIL", exit_price=4001.80)    # journal SL_lock_5 / TRAIL
    assert not any("exit_trail_without_trail_advance" in v for v in t.violations)


def test_genuine_trail_exit_without_advance_still_violates():
    from position_telemetry import PositionTracer
    t = PositionTracer()
    t.exit(2, "A2", exit_type="TRAIL", exit_price=4000.0)     # neither lock_arm nor trail_advance
    assert any("exit_trail_without_trail_advance" in v for v in t.violations)


# --- ISSUE 3b: telemetry.stop idempotent + interrupt-tolerant ----------------------
def test_telemetry_stop_idempotent():
    from telemetry import telemetry_from_env
    tele = telemetry_from_env(component="AUREON-test")
    tele.stop(timeout=1.0)
    tele.stop(timeout=1.0)          # second call must be a no-op, never raise
    assert getattr(tele, "_stopped", False) is True


# --- review event carries impl ----------------------------------------------------
def test_review_anchor_carries_impl(tmp_path):
    import review_log as RV
    r = RV.ReviewLogger(log_dir=str(tmp_path), clock=lambda: "05:00:00", date_fn=lambda: "d")
    r.anchor("ROGUE", 3977.80, "SCHEDULED", label="ROGUE_S1", impl="stop")
    line = open(r.path("d")).read()
    assert "impl=stop" in line and "engine=ROGUE" in line


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
