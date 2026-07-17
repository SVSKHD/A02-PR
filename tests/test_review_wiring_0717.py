"""2026-07-17 review-log WIRING tests (MT5 mocked).

Proves the newly-added get_review_logger() call sites emit the right decision-grade
lines: rogue stop-mode lifecycle (seed / fill / chain / close), rescue-boost (RB) v2
place + cancel, and the stale-leg sweep. Also asserts summarize() tolerates the new
line shapes. Reuses the sibling test fakes so a broker-contract drift is caught here
too. Runnable under pytest or standalone (`python tests/test_review_wiring_0717.py`).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)   # so the sibling test modules' fakes are importable

import review_log as RV


def _fresh_logger(tmp):
    """Point the process-wide review logger at a clean tmp dir with a fixed clock."""
    RV._SHARED = RV.ReviewLogger(log_dir=str(tmp), clock=lambda: "05:00:00",
                                 date_fn=lambda: "d")
    return RV._SHARED


def _lines(rv):
    try:
        return open(rv.path("d")).read()
    except FileNotFoundError:
        return ""


# --- rogue stop-mode lifecycle ----------------------------------------------------
def test_rogue_stop_lifecycle_review_lines(tmp_path):
    from test_rogue_stop import FakeBroker, _mgr
    rv = _fresh_logger(tmp_path)
    br = FakeBroker()
    mgr, gov = _mgr(br)

    mgr.on_tick(4030.0, 1000.0)                          # seed OCO
    br.set_price(4013.0); mgr.on_tick(4013.0, 1001.0)    # SELL fills -> chain C1 placed
    br.set_price(4023.0); mgr.on_tick(4023.0, 1002.0)    # reverses up -> SELL hits SL (close)

    body = _lines(rv)
    # seed / re-seed OCO placement
    assert "PENDING" in body and "engine=ROGUE" in body and "action=seed" in body and "tag=OCO" in body
    # the fill
    assert "FILL" in body and "engine=ROGUE" in body and "side=SELL" in body
    # the next chain stop placed (action=placed carries the chain tag)
    assert "action=placed" in body and "tag=C1" in body
    # the close, reason SL (loss)
    assert "CLOSE" in body and "reason=SL" in body


# --- rescue-boost (RB) v2 place + cancel ------------------------------------------
def test_rescue_boost_place_and_cancel_review_lines(tmp_path):
    from test_rescue_boost import FakeBroker, P
    from rescue_boost import RescueBoostManager
    rv = _fresh_logger(tmp_path)
    br = FakeBroker()
    br.add_parent(7001, "BUY", 4072.0, 4054.0)
    mgr = RescueBoostManager(br, P)

    placed = mgr.place_boosts_for_new_fills()
    assert placed, "expected RB boosts placed"
    body = _lines(rv)
    assert "PENDING" in body and "engine=RB" in body and "action=placed" in body

    br.close_position(7001)                              # parent gone -> orphans cancel
    mgr.cancel_orphaned_boosts()
    body = _lines(rv)
    assert "action=cancelled" in body and body.count("engine=RB") >= 3


# --- stale-leg sweep --------------------------------------------------------------
def test_stale_sweep_review_line(tmp_path):
    import stale_leg_sweep as sweep
    from test_stale_leg_sweep import FakeMT5, FakeOrder, ListLogger, A1, A2, SYMBOL
    rv = _fresh_logger(tmp_path)
    a1_sell = FakeOrder(101, FakeMT5.ORDER_TYPE_SELL_STOP, 4023.77,
                        sweep.tag_comment("AUR_A1_SELL", A1))
    mt5 = FakeMT5(orders=[a1_sell], positions=[])
    res = sweep.sweep_stale_legs(mt5, SYMBOL, A2, logger=ListLogger())
    assert res and res[0]["cancelled"] is True
    body = _lines(rv)
    assert "PENDING" in body and "engine=ANCHOR" in body and "action=swept" in body


# --- summarize() tolerates the new lines ------------------------------------------
def test_summarize_tolerates_new_lines():
    lines = [
        "05:00:00 PENDING  engine=ROGUE action=seed tag=OCO price=4030.00",
        "05:00:00 FILL     engine=ROGUE side=SELL lot=0.35 price=4013.00 tag=A1",
        "05:00:00 PENDING  engine=ROGUE action=placed tag=C1 level=1 price=4001.00",
        "05:00:00 CLOSE    engine=ROGUE side=SELL lot=0.35 price=4023.00 reason=SL pnl=-350.00 tag=A1",
        "05:00:00 PENDING  engine=RB action=placed tag=RB1:7001 level=1 price=4087.00",
        "05:00:00 PENDING  engine=RB action=cancelled tag=RB1:7001",
        "05:00:00 PENDING  engine=ANCHOR action=swept tag=origin_4028.77 price=4048.77",
        "05:00:00 GOV      engine=ROGUE event=loss_stop detail=day_pnl=-370.00",
        "05:00:00 GOV      engine=ANCHOR event=state_override detail=rogue=restored_ON_vs_default_OFF",
    ]
    s = RV.summarize(lines)
    assert s["fills"] == 1
    assert s["closes_by_reason"].get("SL") == 1
    assert s["net_by_engine"].get("ROGUE") == -350.0


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
