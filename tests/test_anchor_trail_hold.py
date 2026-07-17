"""Anchor-leg trail: hold-exempt profit locks (2026-07-17) + modify-reject fallback.

Offline, MT5 mocked. Runnable under pytest or standalone.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from strategy import Position, update_position_on_bar, realize_pnl_usd
from config import Config
import trails as T

cfg = Config()
ENTRY = 3997.8
TS0 = pd.Timestamp("2026-07-17T10:00:00Z")


def _pos():
    return Position(anchor_label="A1", side="BUY", entry_price=ENTRY, entry_time=TS0,
                    current_sl=ENTRY - cfg.sl_dist, tp_level=ENTRY + cfg.tp_dist,
                    max_fav=ENTRY, lot=cfg.lot_size, role="normal")


def _bar(hi, lo):
    return pd.Series({"high": hi, "low": lo, "close": lo})


# --- CHANGE 1: hold-exempt profit locks -------------------------------------------
def test_be_and_lock4_fire_during_hold():
    p = _pos()
    # +5 fav at 3m held -> BE rung fires (SL -> entry), inside the 45m hold
    update_position_on_bar(p, _bar(ENTRY + 5, ENTRY + 5), TS0 + pd.Timedelta(minutes=3), cfg)
    assert abs(p.current_sl - ENTRY) < 1e-6, p.current_sl        # BE lands at +5
    # +6 fav at 4m -> the +$4 lock engages
    update_position_on_bar(p, _bar(ENTRY + 6, ENTRY + 6), TS0 + pd.Timedelta(minutes=4), cfg)
    assert abs(p.current_sl - (ENTRY + 4)) < 1e-6, p.current_sl  # lock+4 lands at +6


def test_0717_replay_peak_777_closes_at_plus4_140():
    p = _pos()
    # spike to +7.77 peak during the hold; discrete locks cap the SL at +$4
    for i, fav in enumerate((5.0, 6.0, 7.77), start=3):
        update_position_on_bar(p, _bar(ENTRY + fav, ENTRY + fav), TS0 + pd.Timedelta(minutes=i), cfg)
    assert abs(p.current_sl - (ENTRY + 4)) < 1e-6                # SL locked at +4 (not peak-2)
    # collapse: the +$4 lock is hit -> exit at 4001.8
    out = update_position_on_bar(p, _bar(ENTRY + 4.5, ENTRY + 4.0), TS0 + pd.Timedelta(minutes=6), cfg)
    assert p.closed and abs(p.exit_price - 4001.8) < 1e-6, (out, p.exit_price)
    pnl = realize_pnl_usd(p, cfg)
    assert abs(pnl - 140.0) < 1e-6, pnl                          # ~+$140


def test_ratchet_only_never_loosens_during_hold():
    p = _pos()
    update_position_on_bar(p, _bar(ENTRY + 6, ENTRY + 6), TS0 + pd.Timedelta(minutes=3), cfg)
    locked = p.current_sl
    assert abs(locked - (ENTRY + 4)) < 1e-6
    # a lower-fav bar must NEVER pull the SL back down
    update_position_on_bar(p, _bar(ENTRY + 5.2, ENTRY + 3.0), TS0 + pd.Timedelta(minutes=4), cfg)
    assert p.current_sl >= locked - 1e-9


def test_plus3_during_hold_no_lock():
    p = _pos()
    update_position_on_bar(p, _bar(ENTRY + 3, ENTRY + 3), TS0 + pd.Timedelta(minutes=3), cfg)
    assert abs(p.current_sl - (ENTRY - cfg.sl_dist)) < 1e-6      # no rung below +$5


# --- CHANGE 2: modify-reject fallback (pure helpers + orchestration) ---------------
class _R:
    def __init__(s, rc): s.retcode = rc


def test_pure_helpers():
    assert T.is_stops_reject(10016) and T.is_stops_reject(10013)
    assert not T.is_stops_reject(10009) and not T.is_stops_reject(10019)
    assert T.broker_min_sl("BUY", 4000.0, 4000.2, 0.30) == 3999.70
    assert T.broker_min_sl("SELL", 4000.0, 4000.2, 0.30) == 4000.50
    assert T.lock_would_fire("BUY", 4001.8, 4001.8, 4002.0)       # bid at the lock
    assert not T.lock_would_fire("BUY", 4001.8, 4002.5, 4002.7)   # still above
    assert T.lock_would_fire("SELL", 4001.8, 4001.6, 4001.8)      # ask at the lock


def _driver(results, closed):
    calls = {"n": 0}
    def modify_fn(sl):
        r = results[min(calls["n"], len(results) - 1)]; calls["n"] += 1; return r
    def close_fn(): closed.append(True)
    return modify_fn, close_fn


def test_modify_success_unchanged():
    closed = []
    mf, cf = _driver([_R(10009)], closed)
    plan = T.modify_sl_with_fallback(mf, cf, "BUY", 4001.8, 4002.0, 4002.2, 0.30)
    assert plan["outcome"] == "DONE" and plan["ok"] and closed == []


def test_modify_reject_then_retry_ok():
    closed = []
    mf, cf = _driver([_R(10016), _R(10009)], closed)   # reject, then adjusted accepted
    plan = T.modify_sl_with_fallback(mf, cf, "BUY", 4001.8, 4001.9, 4002.1, 0.30)
    assert plan["outcome"] == "RETRY_OK" and plan["ok"]
    assert plan["sl"] == 4001.60 and closed == []      # broker-min = bid - 0.30


def test_modify_reject_twice_lock_fired_fallback_close():
    closed = []
    mf, cf = _driver([_R(10016), _R(10016)], closed)
    # price is THROUGH the lock (bid 4001.8 <= intended 4001.8) -> market close
    plan = T.modify_sl_with_fallback(mf, cf, "BUY", 4001.8, 4001.8, 4002.0, 0.30)
    assert plan["outcome"] == "FALLBACK_CLOSE" and plan["ok"] and closed == [True]


def test_modify_reject_twice_not_fired_keeps_stop():
    closed = []
    mf, cf = _driver([_R(10016), _R(10016)], closed)
    # price still well above the lock -> KEEP old stop, retry next bar (never close a winner)
    plan = T.modify_sl_with_fallback(mf, cf, "BUY", 4001.8, 4003.0, 4003.2, 0.30)
    assert plan["outcome"] == "KEEP" and not plan["ok"] and closed == []


def test_non_stops_reject_no_retry_no_close():
    closed = []
    mf, cf = _driver([_R(10019)], closed)              # NO_MONEY-class -> not a stops reject
    plan = T.modify_sl_with_fallback(mf, cf, "BUY", 4001.8, 4001.8, 4002.0, 0.30)
    assert plan["outcome"] == "REJECT" and not plan["ok"] and closed == []


def test_paper_modify_is_ok():
    closed = []
    mf, cf = _driver([{"paper": True}], closed)
    plan = T.modify_sl_with_fallback(mf, cf, "BUY", 4001.8, 4002.0, 4002.2, 0.30)
    assert plan["ok"] and plan["outcome"] == "DONE"


# --- standalone runner ------------------------------------------------------------
def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for name, fn in tests:
        try:
            fn(); print(f"PASS  {name}")
        except Exception as e:
            fails += 1
            import traceback; print(f"FAIL  {name}: {e!r}"); traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
