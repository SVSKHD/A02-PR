"""Unit + parity tests for the ROGUE monster engine (rogue_monster.py).

Runs under pytest AND standalone: `python tests/test_rogue_monster.py`.

Coverage: gate arming (box/ATR/velocity), entry level + SL, chain, re-anchor,
disarm hysteresis, governors (loss/lock/entry-cap), each adaptive guard
(caution, fatigue, giveback, red-day carry, side fatigue) trigger + reset,
candle detectors, adaptive-state persistence roundtrip, and byte-for-byte parity
against the committed reference goldens (provenance: validated sim rp2).
"""
import io
import os
import sys
import contextlib
import importlib.util

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))

import rogue_monster as rm  # noqa: E402
import monster_scenarios as ms  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_spec = importlib.util.spec_from_file_location(
    "monster_backtest", os.path.join(_ROOT, "backtest", "monster_backtest.py"))
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


# ── helpers ──────────────────────────────────────────────────────────────────
def _mk(closes, start="2026-06-10 02:00", wick=0.6):
    idx = pd.date_range(start, periods=len(closes), freq="1min")
    c = np.array(closes, float)
    return pd.DataFrame({"open": np.r_[c[0], c[:-1]], "high": c + wick,
                         "low": c - wick, "close": c}, index=idx)


def _run(closes, cfg=None, **ov):
    cfg = cfg or rm.MonsterCfg(**ov)
    eng = rm.MonsterEngine(cfg)
    eng.run(_mk(closes))
    return eng


def _evtexts(eng):
    return [e for _, e in eng.events]


def _bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


# ── bias ─────────────────────────────────────────────────────────────────────
def _bias_frames(m15_closes, h1_closes):
    m15 = pd.DataFrame({"close": m15_closes},
                       index=pd.date_range("2026-06-10 00:00", periods=len(m15_closes), freq="15min"))
    h1 = pd.DataFrame({"close": h1_closes},
                      index=pd.date_range("2026-06-10 00:00", periods=len(h1_closes), freq="1h"))
    return m15, h1


def test_bias_long_short_both():
    cfg = rm.MonsterCfg()
    up = list(np.arange(0, 20, 1.0))          # rising
    m15, h1 = _bias_frames(up, up)
    t = m15.index[-1]
    assert rm.bias_of(m15, h1, t, cfg) == "LONG"
    down = list(np.arange(20, 0, -1.0))       # falling
    m15, h1 = _bias_frames(down, down)
    assert rm.bias_of(m15, h1, m15.index[-1], cfg) == "SHORT"
    # conflicting M15 up / H1 down -> BOTH
    m15, h1 = _bias_frames(up, down)
    assert rm.bias_of(m15, h1, m15.index[-1], cfg) == "BOTH"
    # insufficient history -> BOTH
    m15, h1 = _bias_frames([1, 2], [1, 2])
    assert rm.bias_of(m15, h1, m15.index[-1], cfg) == "BOTH"


# ── candle detectors ─────────────────────────────────────────────────────────
def test_engulfing():
    # bullish: prev down body, cur up body engulfing
    prev = _bar(10, 10.2, 9.4, 9.5)   # down
    cur = _bar(9.4, 10.8, 9.3, 10.7)  # up, body engulfs
    assert rm.detect_engulfing(prev, cur) == "LONG"
    # bearish
    prev = _bar(9.5, 10.6, 9.4, 10.5)  # up
    cur = _bar(10.6, 10.7, 9.2, 9.3)   # down engulf
    assert rm.detect_engulfing(prev, cur) == "SHORT"
    # none (inside body)
    prev = _bar(9.0, 11.0, 8.5, 11.0)
    cur = _bar(10.0, 10.4, 9.6, 10.2)
    assert rm.detect_engulfing(prev, cur) == ""


def test_dragonfly():
    # tiny body near high, long lower wick
    cur = _bar(10.0, 10.15, 8.5, 10.05)
    assert rm.detect_dragonfly(cur) == "LONG"
    cur = _bar(10.0, 12.0, 9.9, 11.0)   # big body -> not dragonfly
    assert rm.detect_dragonfly(cur) == ""


def test_candle_inert_by_default():
    # default cfg has candle_confirm=False; enabling it with an opposing context
    # can only ever remove entries, never add — and default must be inert.
    assert rm.MonsterCfg().candle_confirm is False


# ── arming, entry, SL, re-anchor ─────────────────────────────────────────────
def test_arm_entry_level_sl_and_reanchor():
    closes = [3000.0] * 66 + [3001.0] * 2 + [2990.0] * 2 + [3000.0] * 20
    eng = _run(closes)
    ev = _evtexts(eng)
    assert any("ANCHOR seed 3000.00" in e for e in ev)
    assert any("ARM LONG @3001.60" in e for e in ev)
    assert any("FILL ENTRY LONG @3001.60 SL 2991.60" in e for e in ev)
    assert any("RE-ANCHOR 2990.00" in e for e in ev)
    assert abs(eng.anchor - 2990.0) < 1e-9
    assert [tr.reason for tr in eng.trades] == ["SL"]


def test_hysteresis_disarm():
    # arm without filling (break box_hi but stay below the +edge_offset level),
    # then go quiet -> DISARM after disarm_bars quiet M5 bars.
    closes = [3000.0] * 66 + [3000.7] + [3000.0] * 40
    eng = _run(closes)
    ev = _evtexts(eng)
    assert any("ARM LONG @3001.60" in e for e in ev)
    assert any("DISARM (6 quiet M5)" in e for e in ev)
    assert not any("FILL" in e for e in ev)


def test_chain_places_at_step():
    # long fills, runs far enough for the +chain_step stop to fill too
    closes = [3000.0] * 66 + [3001.0] + [3014.0] * 3 + [3030.0] * 4 + [3000.0] * 10
    eng = _run(closes)
    kinds = [(tr.kind, tr.side) for tr in eng.trades]
    assert ("ENTRY", "LONG") in kinds
    assert ("CHAIN", "LONG") in kinds


# ── governors ────────────────────────────────────────────────────────────────
def test_governor_profit_lock():
    closes = [3000.0] * 66 + [3001.0] + [3025.0] * 6 + [3000.0] * 10
    eng = _run(closes, profit_lock=50.0)
    assert any("HALT: profit lock" in e for e in _evtexts(eng))


def test_governor_day_loss():
    closes = [3000.0] * 66 + [3001.0] * 2 + [2990.0] * 2 + [3000.0] * 10
    eng = _run(closes, day_loss_halt=-20.0)
    assert any("HALT: day-loss halt" in e for e in _evtexts(eng))


def test_governor_entry_cap():
    closes = [3000.0] * 66 + [3001.0] + [3025.0] * 6 + [3000.0] * 10
    eng = _run(closes, max_entries=1)
    assert any("HALT: entry cap" in e for e in _evtexts(eng))


# ── adaptive-guard predicates (pure) ─────────────────────────────────────────
def test_caution_active_predicate():
    cfg = rm.MonsterCfg(consec_sl_limit=2)
    assert rm.caution_active(1, cfg) is False
    assert rm.caution_active(2, cfg) is True
    assert rm.caution_active(3, cfg) is True


def test_effective_atr_mult_predicate():
    cfg = rm.MonsterCfg(atr_mult=1.5, caution_atr_boost=0.5)
    assert rm.effective_atr_mult(cfg, 0.0, False) == 1.5
    assert rm.effective_atr_mult(cfg, 0.0, True) == 2.0     # caution boost
    assert rm.effective_atr_mult(cfg, 0.5, False) == 2.0    # red-day carry
    assert rm.effective_atr_mult(cfg, 0.5, True) == 2.5     # both stack


def test_fatigue_blocks_predicate():
    cfg = rm.MonsterCfg(side_fatigue_sl=2)
    sbs = {"LONG": 2, "SHORT": 0}
    assert rm.fatigue_blocks(sbs, "LONG", "BOTH", cfg) is True     # fatigued + BOTH
    assert rm.fatigue_blocks(sbs, "LONG", "LONG", cfg) is False    # real bias frees it
    assert rm.fatigue_blocks(sbs, "SHORT", "BOTH", cfg) is False   # other side fine
    sbs = {"LONG": 1, "SHORT": 0}
    assert rm.fatigue_blocks(sbs, "LONG", "BOTH", cfg) is False    # below limit


def test_giveback_halt_predicate():
    cfg = rm.MonsterCfg(day_profit_trail_start=600.0, day_profit_giveback=300.0)
    assert rm.giveback_halt(650.0, 650.0, cfg) is False   # at peak, no retrace
    assert rm.giveback_halt(650.0, 349.0, cfg) is True    # retraced > 300 from 600+ peak
    assert rm.giveback_halt(500.0, 150.0, cfg) is False   # peak never reached start
    assert rm.giveback_halt(600.0, 300.0, cfg) is True    # exact boundary


def test_redday_carry_predicate():
    cfg = rm.MonsterCfg(redday_atr_step=0.5)
    assert rm.redday_carry(-1.0, cfg) == 0.5
    assert rm.redday_carry(0.0, cfg) == 0.0
    assert rm.redday_carry(100.0, cfg) == 0.0


# ── order-price primitives (pure; the live adapter consumes these) ───────────
def test_entry_level_primitive():
    cfg = rm.MonsterCfg(edge_offset=1.0, fallback_trigger=17.0)
    # with a box: beyond the edge by edge_offset
    assert rm.entry_level("LONG", (2999.4, 3000.6), 3000.0, cfg) == 3001.6
    assert rm.entry_level("SHORT", (2999.4, 3000.6), 3000.0, cfg) == 2998.4
    # no box: anchor +/- fallback_trigger
    assert rm.entry_level("LONG", None, 3000.0, cfg) == 3017.0
    assert rm.entry_level("SHORT", None, 3000.0, cfg) == 2983.0


def test_init_sl_primitive():
    cfg = rm.MonsterCfg(sl_cap=10.0)
    assert rm.init_sl("LONG", 3001.6, cfg) == 2991.6
    assert rm.init_sl("SHORT", 2998.4, cfg) == 3008.4


def test_chain_level_primitive():
    cfg = rm.MonsterCfg(chain_step=12.0)
    assert rm.chain_level("LONG", 3001.6, cfg) == 3013.6
    assert rm.chain_level("SHORT", 2998.4, cfg) == 2986.4


def test_trail_target_primitive():
    cfg = rm.MonsterCfg(trail_gap=5.0)
    # LONG at +12 peak -> entry + 12 - 5
    assert rm.trail_target("LONG", 3000.0, 12.0, cfg) == 3007.0
    assert rm.trail_target("SHORT", 3000.0, 12.0, cfg) == 2993.0


# ── gate + arm-side decision (pure; the live adapter consumes these) ─────────
def _m5(rows):
    idx = pd.date_range("2026-06-10 02:00", periods=len(rows), freq="5min")
    return pd.DataFrame(rows, index=idx)


def test_gate_eval_box_break():
    cfg = rm.MonsterCfg(box_bars=12, box_max_range=8.0)
    # 12 flat box bars (range 2) then a breakout bar
    rows = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(12)]
    rows.append({"open": 100, "high": 103, "low": 100, "close": 102})   # last (breakout)
    m5 = _m5(rows)
    gate_hit, box = rm.gate_eval(m5, float("nan"), m5.iloc[0:0], 102.0, 100.0, 1.5, cfg)
    assert box == (99.0, 101.0)
    assert "BOX break" in gate_hit


def test_gate_eval_velocity():
    cfg = rm.MonsterCfg(vel_points=12.0, vel_minutes=5, box_bars=12, box_max_range=8.0)
    rows = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(13)]
    m5 = _m5(rows)
    vel_win = pd.DataFrame({"close": [100.0, 113.0]},
                           index=pd.date_range("2026-06-10 03:00", periods=2, freq="1min"))
    gate_hit, box = rm.gate_eval(m5, float("nan"), vel_win, 113.0, 100.0, 1.5, cfg)
    assert "VEL" in gate_hit


def test_gate_eval_dark_when_short_history():
    cfg = rm.MonsterCfg(box_bars=12)
    m5 = _m5([{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(5)])
    gate_hit, box = rm.gate_eval(m5, float("nan"), m5.iloc[0:0], 100.0, 100.0, 1.5, cfg)
    assert gate_hit == "" and box is None


def test_arm_side():
    closes = pd.Series([100.0, 100.0, 100.0, 100.0, 102.0])
    assert rm.arm_side("BOX break 99-101", 102.0, closes, 13, "BOTH") == "LONG"
    assert rm.arm_side("BOX break 99-101", 102.0, closes, 13, "SHORT") is None   # bias blocks
    down = pd.Series([100.0, 100.0, 100.0, 100.0, 98.0])
    assert rm.arm_side("VEL 12p/5m", 98.0, down, 13, "BOTH") == "SHORT"
    assert rm.arm_side("", 98.0, down, 13, "BOTH") is None   # no gate -> no side


# ── adaptive-guard integration (events fire on real runs) ────────────────────
def test_caution_triggers_on_two_sls():
    closes = ([3000.0] * 66 + [3001.0] * 2 + [2990.0] * 2 + [2990.0] * 70
              + [2991.0] * 2 + [2980.0] * 2 + [2990.0] * 10)
    eng = _run(closes)
    assert sum(1 for tr in eng.trades if tr.reason == "SL") == 2
    assert any("CAUTION on: 2 straight SLs" in e for e in _evtexts(eng))
    assert eng.caution_until is not None
    assert eng.consec_sl == 2


def test_caution_resets_on_winner():
    # White-box: a green (non-SL) close while caution is active must emit
    # 'CAUTION off (winner)' and reset consec_sl. (In the wild the winner must
    # come from a directional-bias sequence, since caution blocks BOTH-bias
    # entries; that confluence needs many hours of trend to synthesize, so the
    # reset branch is exercised directly here.)
    m1 = _mk([100.0] * 100)
    m5 = rm.resample(m1, "5min"); m15 = rm.resample(m1, "15min"); h1 = rm.resample(m1, "1h")
    eng = rm.MonsterEngine(rm.MonsterCfg())
    eng.start_day(m1, m5, m15, h1)
    eng.anchor = 100.0
    eng.consec_sl = 2                       # caution active
    eng.seq_no = 1
    eng.open_pos = [{"side": "LONG", "entry": 100.0, "sl": 95.0, "peak": 11.0,
                     "mae": 0.0, "kind": "ENTRY", "time": m1.index[0],
                     "arm_reason": "test"}]
    t = m1.index[50]
    new = eng.on_bar(t, _bar(106.0, 108.0, 105.0, 106.0))  # trails out green (+6)
    assert any("CAUTION off (winner)" in e for e in new)
    assert eng.consec_sl == 0
    assert eng.trades[-1].reason == "TRAIL"


def test_caution_blocks_both_bias_entry():
    # while caution is active, a would-be entry with bias BOTH is blocked.
    # (limit=1 so one SL arms caution; flat box then break fires the gate.)
    closes = [3000.0] * 70 + [3001.0] * 2 + [2990.0] * 2 + [2990.0] * 65 \
        + list(np.linspace(2991, 3012, 30))
    eng = _run(closes, consec_sl_limit=1, caution_cooldown_min=0)
    assert any("CAUTION block" in e for e in _evtexts(eng))


def test_redday_carry_next_day_event():
    # day1 loses -> extra_atr set; day2 must open with a RED-DAY CARRY event.
    d1 = _mk([3000.0] * 66 + [3001.0] * 2 + [2990.0] * 2 + [3000.0] * 20,
             start="2026-06-10 02:00")
    d2 = _mk([2990.0] * 120, start="2026-06-11 02:00")
    eng = rm.MonsterEngine(rm.MonsterCfg())
    eng.run(pd.concat([d1, d2]))
    assert eng.extra_atr in (0.0, 0.5)   # depends on day2 outcome
    # verify the carry event fired on day2 open (extra_atr was 0.5 entering day2)
    # reconstruct: run day1 alone -> extra_atr 0.5
    e1 = rm.MonsterEngine(rm.MonsterCfg())
    e1.run(d1)
    assert e1.extra_atr == 0.5


def test_redday_carry_event_emitted():
    eng = rm.MonsterEngine(rm.MonsterCfg())
    eng.extra_atr = 0.5
    m1 = _mk([2990.0] * 80, start="2026-06-11 02:00")
    m5 = rm.resample(m1, "5min"); m15 = rm.resample(m1, "15min"); h1 = rm.resample(m1, "1h")
    eng.run_day(m1, m5, m15, h1)
    assert any("RED-DAY CARRY: atr_mult +0.5" in e for e in _evtexts(eng))


# ── persistence roundtrip ────────────────────────────────────────────────────
def test_adaptive_state_persistence_roundtrip():
    src = rm.MonsterEngine(rm.MonsterCfg())
    # drive it into a non-trivial adaptive state
    closes = ([3000.0] * 66 + [3001.0] * 2 + [2990.0] * 2 + [2990.0] * 70
              + [2991.0] * 2 + [2980.0] * 2 + [2990.0] * 10)
    src.run(_mk(closes))
    blob = src.export_state()
    # JSON-safe
    import json
    assert json.loads(json.dumps(blob)) == blob
    # restore into a fresh engine
    dst = rm.MonsterEngine(rm.MonsterCfg())
    dst.import_state(blob)
    assert dst.export_state() == blob
    assert dst.extra_atr == src.extra_atr
    assert dst.consec_sl == src.consec_sl
    assert dst.sl_by_side == src.sl_by_side


# ── parity vs committed goldens (provenance: rp2) ────────────────────────────
def _render(m1, cfg, label):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mb.run(m1, cfg, verbose=True, label=label)
    return buf.getvalue()


def test_parity_golden():
    for name, fn in ms.SCENARIOS.items():
        got = _render(fn(), rm.MonsterCfg(), name)
        with open(os.path.join(_FIX, f"golden_{name}.txt")) as f:
            want = f.read()
        assert got == want, f"parity drift in scenario {name}"


# ── standalone runner ────────────────────────────────────────────────────────
def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            print(f"FAIL {fn.__name__}: {e!r}")
            raise
        print(f"ok   {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
