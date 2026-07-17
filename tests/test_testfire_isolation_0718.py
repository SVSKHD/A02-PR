"""2026-07-18 — Discord /testfire + TF_ test-anchor isolation (MT5 mocked, offline).

Feature 1 (in-process /testfire guards + rate limit) and Feature 2 (a TF_ test anchor
is fully isolated from the real anchor schedule, both directions). Runnable under pytest
or standalone (`python tests/test_testfire_isolation_0718.py`).
"""
import os
import sys
import types
import dataclasses

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import testfire as TF
import review_log as RV
from config import Config

A2_UTC = pd.Timestamp('2026-06-24T07:00:00Z')   # A2 broker 10:00, offset 3 -> 07:00 UTC


def _stub(trade_mode=0, profile='STANDARD_5PCT', anchors=None, evt_open=False,
          anchors_day_pnl=-100.0, anchors_engine=True, pos=(), pend=()):
    """Trader stub mirroring selftest._testfire_stub + a _resolved_anchor_hm the
    in-process active-window check needs. Binds the REAL anchors-brake methods."""
    import live_trader as LT, utils as U
    cfg = dataclasses.replace(Config(), account_profile=profile,
                              anchors_daily_profit_stop=400.0, account_target_pct=0.0)
    if anchors is not None:
        cfg.anchors = anchors

    def _hist(*a, **k):
        return [types.SimpleNamespace(entry=1, profit=float(anchors_day_pnl), swap=0.0,
                                      commission=0.0, time=1, magic=20260522)]
    mt5 = types.SimpleNamespace(
        ACCOUNT_TRADE_MODE_DEMO=0,
        account_info=lambda: types.SimpleNamespace(trade_mode=trade_mode, balance=50000.0),
        positions_get=lambda symbol=None: list(pos),
        orders_get=lambda symbol=None: list(pend),
        symbol_info_tick=lambda s: types.SimpleNamespace(bid=3995.0, ask=3995.2),
        history_deals_get=_hist)
    t = types.SimpleNamespace(
        cfg=cfg, adapter=types.SimpleNamespace(mt5=mt5),
        shadow_positions={}, shadow_pendings={},
        _testfire_event_open=evt_open, _deferred_anchor=None, _testfire_deferred=None,
        paused=False, _last_anchor_attempt={},
        ANCHOR_LATE_RETRY_INTERVAL_S=30, ANCHOR_ONTIME_GRACE_S=120,
        state={'daily_pnl': 0.0, 'day_start_equity': 50000.0,
               'last_broker_date': '2026-06-24', 'processed_anchors_today': set(),
               'missed_anchors_today': []},
        engines={'anchors': bool(anchors_engine), 'rogue': False, 'fetcher': False},
        _tick_counter=0, _rogue=None, _fetcher=None,
        tele=types.SimpleNamespace(info=lambda *a, **k: None, send=lambda *a, **k: None,
                                   warn=lambda *a, **k: None, success=lambda *a, **k: None,
                                   error=lambda *a, **k: None))
    t._anchor_datetime_utc = U.anchor_datetime_utc
    t._broker_date = lambda now=None: pd.Timestamp('2026-06-24').date()
    t._resolved_anchor_hm = lambda label, bd, h, m: (h, m)
    t._anchor_skipped_today_friday = lambda label, bd: False
    t._save_state = lambda: None
    for m in ('_anchor_entries_blocked', '_friday_entries_blocked',
              '_friday_flatten_reached', '_engine_enabled', '_anchors_daystop_blocked',
              '_anchors_daystop', '_anchors_day_pnl_computed', '_account_locked',
              '_account_target', '_post_a4_complete', '_engine_day_pnls'):
        fn = getattr(LT.LiveTrader, m, None)
        if fn is not None:
            setattr(t, m, fn.__get__(t))
    return t


# --- helpers ----------------------------------------------------------------------
def test_label_helpers():
    assert TF.is_testfire_label("TF_093000") is True
    assert TF.is_testfire_label("A2") is False
    lbl = TF.make_testfire_label(A2_UTC)
    assert lbl.startswith("TF_") and lbl[3:].isdigit()


# --- Feature 2: in-process arm never suppresses the scheduler ---------------------
def test_inproc_arm_isolated_slot_no_scheduler_suppression():
    t = _stub(anchors=[('A2', 10, 0)])
    label = TF.arm_testfire_inproc(t, now_utc=A2_UTC - pd.Timedelta(minutes=2))
    assert label.startswith("TF_")
    assert t._testfire_deferred is not None and t._testfire_deferred['label'] == label
    assert t._deferred_anchor is None                    # real slot untouched
    assert getattr(t, '_testfire_mode', False) is False  # scheduler NOT suppressed
    assert t._testfire_event_open is True


def test_scheduler_places_A2_while_testfire_armed():
    """The core independence guarantee: with a /testfire armed, the real A2 scheduler
    still fires at A2's window (a testfire never suppresses/consumes a real anchor)."""
    import anchors as A
    t = _stub(anchors=[('A2', 10, 0)])
    calls = []
    t._process_anchor = lambda label, anchor_utc: calls.append(label)
    TF.arm_testfire_inproc(t, now_utc=A2_UTC - pd.Timedelta(minutes=2))
    # now A2's window is active; the scheduler must attempt A2 regardless of the armed test
    A._process_anchor_if_due(t, t._broker_date(), A2_UTC + pd.Timedelta(seconds=30))
    assert calls == ['A2'], calls
    assert 'A2' not in t.state['processed_anchors_today']  # scheduler, not testfire, owns A2


# --- Feature 2: TF_ placement body isolates from real anchor state -----------------
def _placement_stub():
    cfg = dataclasses.replace(Config(), symbol='XAUUSD')
    marks, sweeps = [], []
    adapter = types.SimpleNamespace(
        place_stop_order=lambda *a, **k: types.SimpleNamespace(retcode=10009, order=111),
        mt5=types.SimpleNamespace(
            symbol_info_tick=lambda s: types.SimpleNamespace(bid=4000.0, ask=4000.2),
            last_error=lambda: (0, 'ok')))
    t = types.SimpleNamespace(
        cfg=cfg, paper=True, adapter=adapter, shadow_pendings={}, shadow_positions={},
        state={'processed_anchors_today': set()},
        ANCHOR_ONTIME_GRACE_S=120,
        tele=types.SimpleNamespace(info=lambda *a, **k: None, warn=lambda *a, **k: None,
                                   error=lambda *a, **k: None, success=lambda *a, **k: None))
    t._extract_ticket = lambda res, fb: fb
    t._mark_anchor_placed = lambda label: marks.append(label)
    t._sweep_stale_legs = lambda ap: sweeps.append(ap)
    t._confirm_a1_placement = lambda *a, **k: None
    t._dump_mt5_state = lambda *a, **k: None
    return t, marks, sweeps


def test_tf_placement_skips_sweep_and_mark_and_tags():
    """Driving the REAL _place_orders_for_anchor with a TF_ label: no NameError, the
    stale sweep is NOT run, the anchor is NOT marked placed / processed, and both legs
    carry the TF_ comment + test flag + TESTFIRE trigger_source."""
    import anchors as A
    t, marks, sweeps = _placement_stub()
    A._place_orders_for_anchor(t, 'TF_093000', A2_UTC, 4000.0, 4000.0)
    assert sweeps == []                                  # sweep never touched real legs
    assert marks == []                                   # NOT marked placed
    assert t.state['processed_anchors_today'] == set()   # real slot state untouched
    assert len(t.shadow_pendings) == 2
    for info in t.shadow_pendings.values():
        assert info['anchor_label'] == 'TF_093000'
        assert info['trigger_source'] == 'TESTFIRE'
        assert info['test'] is True


def test_real_placement_still_sweeps_and_marks():
    """The same body for a REAL anchor label DOES sweep + mark (isolation is label-gated,
    nothing changed for real anchors)."""
    import anchors as A
    t, marks, sweeps = _placement_stub()
    A._place_orders_for_anchor(t, 'A2', A2_UTC, 4000.0, 4000.0)
    assert sweeps == [4000.0] and marks == ['A2']
    for info in t.shadow_pendings.values():
        assert info['trigger_source'] == 'SCHEDULED' and info['test'] is False


# --- Feature 1: active-window rail (allow 2-min-before, refuse in-window) ----------
def test_active_window_allows_near_future_refuses_in_window():
    t = _stub(anchors=[('A2', 10, 0)])
    # 2 minutes BEFORE A2 -> not active -> allowed
    assert TF._active_real_anchor(t, A2_UTC - pd.Timedelta(minutes=2)) is None
    ok, _ = TF.testfire_preflight_inproc(t, now_utc=A2_UTC - pd.Timedelta(minutes=2))
    assert ok is True
    # inside A2's placement window -> refuse
    assert TF._active_real_anchor(t, A2_UTC + pd.Timedelta(seconds=30)) == 'A2'
    ok2, reason2 = TF.testfire_preflight_inproc(t, now_utc=A2_UTC + pd.Timedelta(seconds=30))
    assert ok2 is False and 'ACTIVE-WINDOW' in reason2


# --- Feature 1: guards -------------------------------------------------------------
def test_preflight_funded_and_demo_refusals():
    far = A2_UTC - pd.Timedelta(hours=3)
    assert TF.testfire_preflight_inproc(_stub(trade_mode=2), now_utc=far)[0] is False  # not demo
    assert TF.testfire_preflight_inproc(_stub(profile='FPZERO_1PCT'), now_utc=far)[0] is False  # FP
    ok, _ = TF.testfire_preflight_inproc(_stub(), now_utc=far)
    assert ok is True


def test_preflight_inflight_refusal():
    far = A2_UTC - pd.Timedelta(hours=3)
    ok, reason = TF.testfire_preflight_inproc(_stub(evt_open=True), now_utc=far)
    assert ok is False and 'ONE-AT-A-TIME' in reason


def test_rate_limit_refuses_second_run():
    t = _stub(anchors=[('A2', 10, 0)])
    far = A2_UTC - pd.Timedelta(hours=3)
    assert TF.handle_testfire_command(t, now_utc=far) is True       # first run arms
    assert t._testfire_deferred is not None
    # immediately again (well within 10 min) -> rate-limited refusal, no re-arm
    t._testfire_deferred = None
    t._testfire_event_open = False    # isolate the rate-limit gate from one-at-a-time
    assert TF.handle_testfire_command(t, now_utc=far + pd.Timedelta(minutes=1)) is False
    assert t._testfire_deferred is None
    # after the window, a run is allowed again
    assert TF.handle_testfire_command(t, now_utc=far + pd.Timedelta(minutes=11)) is True


# --- Feature 2 reverse: real stale sweep never touches TF_ orders ------------------
def test_stale_sweep_exempts_tf():
    import stale_leg_sweep as sweep
    from test_stale_leg_sweep import FakeMT5, FakeOrder, ListLogger, A1, A2, SYMBOL
    tf_leg = FakeOrder(301, FakeMT5.ORDER_TYPE_SELL_STOP, 4023.77,
                       sweep.tag_comment("TF_AUR_TF_SELL", A1))
    mt5 = FakeMT5(orders=[tf_leg], positions=[])
    res = sweep.sweep_stale_legs(mt5, SYMBOL, A2, logger=ListLogger())
    assert res == []                       # TF_ leg exempt — never swept
    assert sweep._is_rescue_boost_comment("TF_AUR_TF_SELL A:4028.77") is True


# --- Feature 2: TF_ fleet events excluded from CRASH_WIN/whipsaw tally -------------
def test_rescue_log_excludes_tf_fleet_event():
    import rescue_log as RL
    self = types.SimpleNamespace(_rescue_events={}, _rescue_event_by_ticket={},
                                 _persist_rescue_events=lambda: None)
    RL._rescue_event_open(self, {'anchor': 'TF_093000', 'members': {1, 2},
                                 'event_id': 'tf_ev', 'boosts_placed_ok': True})
    assert self._rescue_events == {}                 # TF_ event never opened -> never tallied
    # a real anchor event still opens
    RL._rescue_event_open(self, {'anchor': 'A2', 'members': {3, 4},
                                 'event_id': 'real_ev', 'boosts_placed_ok': True})
    assert 'real_ev' in self._rescue_events


# --- Feature 2: review-log test=1 + digest TEST section (never in real totals) -----
def test_review_test_flag_separated_from_real_stats():
    lines = [
        "05:00:00 FILL     engine=ANCHOR side=BUY lot=0.35 price=4000.00 tag=A1",
        "05:01:00 CLOSE    engine=ANCHOR side=BUY lot=0.35 price=4010.00 reason=TP pnl=+350.00 tag=A1",
        "05:02:00 FILL     engine=ANCHOR side=BUY lot=0.35 price=4000.00 tag=TF_093000 test=1",
        "05:03:00 CLOSE    engine=ANCHOR side=BUY lot=0.35 price=3982.00 reason=SL pnl=-630.00 tag=TF_093000 test=1",
    ]
    s = RV.summarize(lines)
    assert s['fills'] == 1 and s['net_total'] == 350.0              # real only
    assert s['test']['fills'] == 1 and s['test']['closes'] == 1
    assert s['test']['net'] == -630.0
    assert s['net_by_engine'].get('ANCHOR') == 350.0               # test SL NOT folded in
    dig = RV.format_digest(s, "2026-07-18")
    assert 'TEST' in dig and 'test net: -630.00' in dig


def test_review_logger_emits_test_marker(tmp_path):
    r = RV.ReviewLogger(log_dir=str(tmp_path), clock=lambda: "05:00:00", date_fn=lambda: "d")
    r.fill("ANCHOR", "BUY", 0.35, 4000.0, tag="TF_093000", test=1)
    r.fill("ANCHOR", "BUY", 0.35, 4000.0, tag="A1")   # real: no test marker
    body = open(r.path("d")).read().splitlines()
    assert "test=1" in body[0] and "tag=TF_093000" in body[0]
    assert "test=" not in body[1]                     # real line unchanged


# --- Feature 2: journal test column -----------------------------------------------
def test_journal_has_test_column_and_flags_tf(tmp_path):
    import journal as J
    assert J.JOURNAL_COLUMNS[-1] == 'test'
    self = types.SimpleNamespace(run_dir=str(tmp_path), cfg=Config())
    shadow = {'side': 'BUY', 'entry_price': 4000.0, 'max_fav': 4005.0,
              'anchor_label': 'TF_093000', 'trigger_source': 'TESTFIRE',
              'anchor_price': 4000.0}
    close_deal = types.SimpleNamespace(time=1, volume=0.35)
    J._write_journal(self, shadow, close_deal, 4010.0, 'TP', 155.75, 5001)
    import glob, csv
    path = glob.glob(os.path.join(str(tmp_path), 'journal', 'trades_*.csv'))[0]
    rows = list(csv.reader(open(path)))
    assert rows[0][-1] == 'test' and rows[1][-1] == '1'


# --- Feature 1: CLI arm_testfire unchanged ----------------------------------------
def test_cli_arm_testfire_unchanged():
    t = _stub(anchors=[('A2', 10, 0)])
    d = TF.arm_testfire(t, 'A2', now_utc=A2_UTC)
    assert t._testfire_mode is True and t._trigger_source == 'TESTFIRE'
    assert t._testfire_event_open is True and d['label'] == 'A2'
    assert t._deferred_anchor is d                    # CLI uses the real deferred slot


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
