"""Rogue independent daily anchor + Discord cards (offline, MT5 mocked).

Runnable under pytest or standalone (`python tests/test_rogue_anchor.py`).
"""
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import rogue_stop as RS
import discord_cards as dc
from rogue_stop import RogueStopParams, RogueStopManager
import rogue as R
from config import Config

P = RogueStopParams()


# --- fakes ------------------------------------------------------------------------
class FakeTele:
    def __init__(s): s.sent = []
    def send(s, msg, sev=None, card=None, important=False, **k): s.sent.append((msg, card))
    def info(s, *a, **k): pass
    def warn(s, *a, **k): pass


class _FakeMT5:
    def __init__(s, mid): s._mid = mid
    def symbol_info_tick(s, sym):
        return type("T", (), {"bid": s._mid - 0.1, "ask": s._mid + 0.1, "time_msc": 0})()


class FakeAdapter:
    def __init__(s, m5=None, tick_mid=3977.80):
        s._m5, s.m5_calls, s.mt5 = m5, [], _FakeMT5(tick_mid)
    def get_m5_close(s, symbol, when): s.m5_calls.append(when); return s._m5


class FakeTrader:
    def __init__(s, st=None, m5=3977.80, tick_mid=3977.80):
        s.cfg = Config()
        s.adapter = FakeAdapter(m5=m5, tick_mid=tick_mid)
        s.tele = FakeTele()
        s.paper = True
        s._rogue = st if st is not None else {}
    def _broker_date(s, now_utc):
        # broker = UTC+3
        return (now_utc + pd.Timedelta(hours=3)).date()


class Br:
    """Minimal broker: tracks pendings/positions for reconcile checks."""
    def __init__(s, pend=None, pos=None): s._pend, s._pos, s.placed = list(pend or []), list(pos or []), []
    def positions(s): return list(s._pos)
    def pendings(s): return list(s._pend)
    def place_stop(s, side, price, sl, comment):
        s.placed.append((side, price, sl, comment))
        o = type("O", (), {"ticket": 900 + len(s.placed), "side": side, "price": price, "sl": sl, "comment": comment})()
        s._pend.append(o); return o.ticket
    def cancel(s, t): s._pend = [o for o in s._pend if o.ticket != t]; return True
    def modify_sl(s, t, sl): return True
    def closed_deal(s, t): return None
    def cancel_own_pendings(s): n = len(s._pend); s._pend = []; return n
    def flatten_own(s): n = len(s._pos); s._pos = []; return n


# --- pure scheduling --------------------------------------------------------------
def test_scheduled_utc_weekday_and_monday():
    cfg = Config()
    wed = _dt.date(2026, 7, 15)      # Wednesday
    s = RS.rogue_scheduled_utc(cfg, wed)
    # 02:30 broker (UTC+3) -> 23:30 UTC the previous calendar day
    assert s.hour == 23 and s.minute == 30
    mon = _dt.date(2026, 7, 13)      # Monday -> cushion 03:30 broker -> 00:30 UTC
    sm = RS.rogue_scheduled_utc(cfg, mon)
    assert sm.hour == 0 and sm.minute == 30


def test_anchor_decision_pure():
    sched = pd.Timestamp("2026-07-17T00:30:00Z")
    assert RS.anchor_decision(sched - pd.Timedelta(minutes=5), sched, False) == RS.WAIT
    assert RS.anchor_decision(sched + pd.Timedelta(minutes=2), sched, False) == RS.CAPTURE_SCHEDULED
    assert RS.anchor_decision(sched + pd.Timedelta(minutes=45), sched, False) == RS.CAPTURE_LATE
    assert RS.anchor_decision(sched + pd.Timedelta(hours=8), sched, True) == RS.RELOAD


# --- _ensure_daily_anchor: capture / reload / late / independence -----------------
def _ensure(tr, now_utc, br=None):
    br = br or Br()
    m = RogueStopManager(br, P, R.new_day_state(), tr.cfg, anchor_provider=lambda: getattr(m, "_daily_anchor", None))
    m._daily_anchor = None
    RS._ensure_daily_anchor(tr, tr._rogue, now_utc, m)
    return m, br


def test_scheduled_capture_persists_and_cards():
    tr = FakeTrader(st={}, m5=3977.80)
    broker_date = _dt.date(2026, 7, 17)
    sched = RS.rogue_scheduled_utc(tr.cfg, broker_date)
    m, _ = _ensure(tr, sched + pd.Timedelta(minutes=2))
    assert m._daily_anchor == 3977.80
    d = tr._rogue["rogue_daily"]
    assert d["source"] == "SCHEDULED" and d["anchor"] == 3977.80 and d["oco_consumed"] is False
    assert len(tr.adapter.m5_calls) == 1                 # captured via get_m5_close (scheduled)
    assert any(card and card.get("title", "").startswith("🗡️") for _msg, card in tr.tele.sent)


def test_restart_reloads_anchor_never_resnapshots():
    # a stored anchor for today already exists (prior capture)
    day = str(_dt.date(2026, 7, 17))
    st = {"rogue_daily": {"date": day, "anchor": 3977.80, "ts": "2026-07-17T00:30:00Z",
                          "source": "SCHEDULED", "oco_consumed": False}}
    tr = FakeTrader(st=st, m5=9999.99)                    # m5 would be a DIFFERENT price
    now = RS.rogue_scheduled_utc(tr.cfg, _dt.date(2026, 7, 17)) + pd.Timedelta(hours=10)
    m, _ = _ensure(tr, now)
    assert m._daily_anchor == 3977.80                     # RELOADED, not re-snapshot
    assert tr.adapter.m5_calls == []                      # never captured again
    assert m.traded is False                              # OCO not consumed -> may re-place


def test_reconcile_does_not_duplicate_oco_when_pendings_present():
    day = str(_dt.date(2026, 7, 17))
    st = {"rogue_daily": {"date": day, "anchor": 3977.80, "ts": "t", "source": "SCHEDULED",
                          "oco_consumed": False}}
    tr = FakeTrader(st=st)
    # OCO already resting at the broker
    oco = [type("O", (), {"ticket": 1, "side": "BUY", "price": 3994.8, "sl": 3984.8, "comment": "RGS:A1"})(),
           type("O", (), {"ticket": 2, "side": "SELL", "price": 3960.8, "sl": 3970.8, "comment": "RGS:A1"})()]
    br = Br(pend=oco)
    now = RS.rogue_scheduled_utc(tr.cfg, _dt.date(2026, 7, 17)) + pd.Timedelta(hours=10)
    m = RogueStopManager(br, P, R.new_day_state(), tr.cfg, anchor_provider=lambda: getattr(m, "_daily_anchor", None))
    m._daily_anchor = None
    RS._ensure_daily_anchor(tr, st, now, m)
    m.on_tick(3977.7, 1000.0)
    assert br.placed == []                                # no duplicate OCO placed


def test_reconcile_replaces_oco_when_missing_and_no_fill():
    day = str(_dt.date(2026, 7, 17))
    st = {"rogue_daily": {"date": day, "anchor": 3977.80, "ts": "t", "source": "SCHEDULED",
                          "oco_consumed": False}}
    tr = FakeTrader(st=st)
    br = Br()                                             # no pendings, flat, not consumed
    now = RS.rogue_scheduled_utc(tr.cfg, _dt.date(2026, 7, 17)) + pd.Timedelta(hours=10)
    m = RogueStopManager(br, P, R.new_day_state(), tr.cfg, anchor_provider=lambda: getattr(m, "_daily_anchor", None))
    m._daily_anchor = None
    RS._ensure_daily_anchor(tr, st, now, m)
    m.on_tick(3977.7, 1000.0)
    assert sorted(pr for _s, pr, _sl, _c in br.placed) == [3960.8, 3994.8]   # re-placed off stored anchor


def test_consumed_day_does_not_replace_oco():
    day = str(_dt.date(2026, 7, 17))
    st = {"rogue_daily": {"date": day, "anchor": 3977.80, "ts": "t", "source": "SCHEDULED",
                          "oco_consumed": True}}                 # a fill already happened today
    tr = FakeTrader(st=st)
    br = Br()
    now = RS.rogue_scheduled_utc(tr.cfg, _dt.date(2026, 7, 17)) + pd.Timedelta(hours=10)
    m = RogueStopManager(br, P, R.new_day_state(), tr.cfg, anchor_provider=lambda: getattr(m, "_daily_anchor", None))
    m._daily_anchor = None
    RS._ensure_daily_anchor(tr, st, now, m)
    assert m.traded is True
    m.on_tick(3977.7, 1000.0)
    assert br.placed == []                                # initial OCO NOT re-placed after a fill


def test_late_capture_when_first_boot_after_schedule():
    # M5 missing -> tick fallback settles the current tick (3975.60) via the REAL
    # seed_tick_price (no module monkeypatch -> no cross-test pollution).
    tr = FakeTrader(st={}, m5=None, tick_mid=3975.60)
    sched = RS.rogue_scheduled_utc(tr.cfg, _dt.date(2026, 7, 17))
    m, _ = _ensure(tr, sched + pd.Timedelta(minutes=45))  # well past schedule, nothing stored
    assert tr._rogue["rogue_daily"]["source"] == "LATE-CAPTURE"
    assert m._daily_anchor == 3975.60


def test_capture_independent_of_anchors_engine():
    # capture reads get_m5_close directly, never the A1 object -> works with anchors off
    tr = FakeTrader(st={}, m5=3977.80)
    tr.cfg.rogue_stop_mode = True
    sched = RS.rogue_scheduled_utc(tr.cfg, _dt.date(2026, 7, 17))
    m, _ = _ensure(tr, sched + pd.Timedelta(minutes=1))
    assert m._daily_anchor == 3977.80                     # captured regardless of anchors engine


def test_reseed_unaffected_uses_current_price():
    # after an SL the re-seed is at the CURRENT price, not the daily anchor
    br = Br()
    tr = FakeTrader(st={})
    m = RogueStopManager(br, P, R.new_day_state(), tr.cfg, anchor_provider=lambda: 3977.80)
    m._daily_anchor = 3977.80
    m.traded = True                                       # a fill happened -> re-seed path
    m.reseed_after = 999.0                                 # a close set it; cooldown elapsed (now 1000)
    m._seed_or_reseed(3900.0, 1000.0)                     # current price 3900
    assert sorted(pr for _s, pr, _sl, _c in br.placed) == [3883.0, 3917.0]   # 3900 ± 17, NOT 3977.80


# --- cards: content from the actual payload ---------------------------------------
def test_chain_and_reseed_cards_match_payload():
    order = RS.chain_next(4013.0, "SELL", 1, P)           # SELL stop @ 4001, SL 4011
    card = dc.card_rogue_chain(order, 4013.0)
    body = card["fields"][0]["value"]
    assert "$4,001.00" in body and "$4,011.00" in body and "$4,013.00" in body
    plan = {o.side: o for o in RS.oco_plan(3900.0, P)}
    rc = dc.card_rogue_reseed(3900.0, plan["BUY"], plan["SELL"])
    vals = {f["name"]: f["value"] for f in rc["fields"]}
    assert "$3,917.00" in vals["BUY"] and "$3,883.00" in vals["SELL"]


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
