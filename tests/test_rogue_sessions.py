"""Rogue three-session mode (flag-gated) — offline, MT5 mocked.

Runnable under pytest or standalone (`python tests/test_rogue_sessions.py`).
"""
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import rogue_stop as RS
from rogue_stop import RogueStopParams, RogueStopManager
import rogue as R
from config import Config

P = RogueStopParams()
WED = _dt.date(2026, 7, 15)      # Wednesday
FRI = _dt.date(2026, 7, 17)      # Friday
MON = _dt.date(2026, 7, 13)      # Monday


# --- fakes ------------------------------------------------------------------------
class _Pos:
    def __init__(s, t, side, entry, sl, comment): s.ticket, s.side, s.entry, s.sl, s.comment = t, side, entry, sl, comment


class _Pend:
    def __init__(s, t, side, price, sl, comment): s.ticket, s.side, s.price, s.sl, s.comment = t, side, price, sl, comment


class Br:
    def __init__(s): s.pos, s.pend, s._t, s.closed = {}, {}, 8000, {}
    def positions(s): return list(s.pos.values())
    def pendings(s): return list(s.pend.values())
    def place_stop(s, side, price, sl, comment):
        s._t += 1; s.pend[s._t] = _Pend(s._t, side, price, sl, comment); return s._t
    def cancel(s, t): return s.pend.pop(int(t), None) is not None
    def modify_sl(s, t, sl):
        p = s.pos.get(int(t))
        if p: p.sl = sl; return True
        return False
    def closed_deal(s, t): return s.closed.get(int(t))
    def cancel_own_pendings(s): n = len(s.pend); s.pend = {}; return n
    def flatten_own(s): n = len(s.pos); s.pos = {}; return n
    def set_price(s, mid):
        for t in list(s.pend):
            o = s.pend[t]
            if (o.side == "BUY" and mid >= o.price) or (o.side == "SELL" and mid <= o.price):
                s._t += 1; s.pos[s._t] = _Pos(s._t, o.side, o.price, o.sl, o.comment); del s.pend[t]
        for t in list(s.pos):
            p = s.pos[t]
            if (p.side == "BUY" and mid <= p.sl) or (p.side == "SELL" and mid >= p.sl):
                pnl = (1 if p.side == "BUY" else -1) * (p.sl - p.entry) * 0.35 * 100
                s.closed[int(t)] = {"pnl": pnl, "exit_price": p.sl}; del s.pos[t]


class Tele:
    def __init__(s): s.cards = []
    def send(s, msg, sev=None, card=None, **k): s.cards.append((msg, card))


class Adapter:
    def __init__(s, anchors): s._anchors, s.m5_calls = list(anchors), []
    def get_m5_close(s, sym, when):
        s.m5_calls.append(when)
        return s._anchors.pop(0) if s._anchors else 4000.0


class Trader:
    def __init__(s, anchors, st=None, sessions=True):
        s.cfg = Config(); s.cfg.rogue_sessions_enabled = sessions
        s.adapter = Adapter(anchors); s.tele = Tele(); s.paper = True
        s._rogue = st if st is not None else {"gov": R.new_day_state()}
    def _broker_date(s, now_utc): return (now_utc + pd.Timedelta(hours=3)).date()


def _mgr(tr, br):
    m = RogueStopManager(br, P, tr._rogue.setdefault("gov", R.new_day_state()), tr.cfg,
                         anchor_provider=lambda: getattr(m, "_daily_anchor", None),
                         on_first_fill=lambda: RS._mark_oco_consumed(tr, tr._rogue))
    m._daily_anchor = None
    return m


def _tick(tr, m, now_utc, price, epoch):
    RS._ensure_session_anchor(tr, tr._rogue, now_utc, m)
    m.on_tick(price, epoch)


def _starts(cfg, d):
    w = {n: s for n, s, e in RS.session_windows_utc(cfg, d)}
    return w


# --- windows / resolution ---------------------------------------------------------
def test_session_windows_and_friday_skip():
    cfg = Config()
    w = RS.session_windows_utc(cfg, WED)
    assert [n for n, _s, _e in w] == ["S1", "S2", "S3"]
    st = {n: (s, e) for n, s, e in w}
    # S2 12:30 IST -> 07:00 UTC ; S3 19:30 IST -> 14:00 UTC ; S2 end 19:10 IST -> 13:40 UTC
    assert st["S2"][0].hour == 7 and st["S2"][0].minute == 0
    assert st["S3"][0].hour == 14 and st["S3"][0].minute == 0
    assert st["S2"][1].hour == 13 and st["S2"][1].minute == 40
    # Friday -> no S3
    assert [n for n, _s, _e in RS.session_windows_utc(cfg, FRI)] == ["S1", "S2"]
    # Monday S1 follows the cushion (03:30 server -> 00:30 UTC), S2/S3 unchanged
    assert _starts(cfg, MON)["S1"].hour == 0 and _starts(cfg, MON)["S1"].minute == 30
    assert _starts(cfg, MON)["S2"].hour == 7


def test_resolve_session_and_gap():
    cfg = Config()
    s2 = _starts(cfg, WED)["S2"]
    assert RS.resolve_session(cfg, s2 + pd.Timedelta(minutes=1), WED)[0] == "S2"
    # gap 19:10–19:30 IST == 13:40–14:00 UTC
    gap = RS._ist_to_utc(WED, 19, 20)
    assert RS.resolve_session(cfg, gap, WED)[0] is None


# --- three captures + cards per day -----------------------------------------------
def test_three_captures_and_cards_per_day():
    tr = Trader(anchors=[3977.80, 4010.0, 4050.0])   # S1/S2/S3 anchors
    br = Br(); m = _mgr(tr, br)
    st = _starts(tr.cfg, WED)
    for name in ("S1", "S2", "S3"):
        _tick(tr, m, st[name] + pd.Timedelta(minutes=2), 0.0, 1000.0)
    titles = [c["title"] for _msg, c in tr.tele.cards if c]
    assert titles == ["🗡️ ROGUE_S1", "🗡️ ROGUE_S2", "🗡️ ROGUE_S3"]
    assert tr._rogue["rogue_session"]["session"] == "S3" and tr._rogue["rogue_session"]["anchor"] == 4050.0
    assert len(tr.adapter.m5_calls) == 3


def test_friday_two_captures():
    tr = Trader(anchors=[3977.80, 4010.0, 4050.0])
    br = Br(); m = _mgr(tr, br)
    st = _starts(tr.cfg, FRI)
    _tick(tr, m, st["S1"] + pd.Timedelta(minutes=2), 0.0, 1000.0)
    _tick(tr, m, st["S2"] + pd.Timedelta(minutes=2), 0.0, 1000.0)
    # S3 time on Friday -> gap, no capture
    _tick(tr, m, RS._ist_to_utc(FRI, 19, 45), 0.0, 1000.0)
    titles = [c["title"] for _msg, c in tr.tele.cards if c]
    assert titles == ["🗡️ ROGUE_S1", "🗡️ ROGUE_S2"]     # exactly two on Friday
    assert len(tr.adapter.m5_calls) == 2


# --- boundary: pendings cancelled, position carries -------------------------------
def test_boundary_cancels_pendings_position_carries():
    tr = Trader(anchors=[3977.80, 4010.0, 4050.0])
    br = Br(); m = _mgr(tr, br)
    st = _starts(tr.cfg, WED)
    # S1: seed OCO, fill the SELL (position), chain placed
    _tick(tr, m, st["S1"] + pd.Timedelta(minutes=2), 3977.80, 1000.0)
    br.set_price(3960.80); _tick(tr, m, st["S1"] + pd.Timedelta(minutes=3), 3960.80, 1001.0)
    assert any(RS.rgs_session(o.comment) == "S1" for o in br.pendings())   # S1 chain resting
    s1_pos = [p for p in br.positions() if RS.rgs_session(p.comment) == "S1"]
    assert len(s1_pos) == 1
    # cross into S2
    _tick(tr, m, st["S2"] + pd.Timedelta(minutes=2), 3960.80, 2000.0)
    assert not any(RS.rgs_session(o.comment) == "S1" for o in br.pendings())  # S1 pendings cancelled
    assert len([p for p in br.positions() if RS.rgs_session(p.comment) == "S1"]) == 1  # position carries
    assert any(RS.rgs_session(o.comment) == "S2" for o in br.pendings())     # S2 OCO placed despite carry


def test_boundary_crossing_reseed_dropped():
    tr = Trader(anchors=[3977.80, 4010.0, 4050.0])
    br = Br(); m = _mgr(tr, br)
    st = _starts(tr.cfg, WED)
    _tick(tr, m, st["S1"] + pd.Timedelta(minutes=2), 3977.80, 1000.0)
    br.set_price(3960.80); _tick(tr, m, st["S1"] + pd.Timedelta(minutes=3), 3960.80, 1001.0)  # SELL fills
    br.set_price(3970.80); _tick(tr, m, st["S1"] + pd.Timedelta(minutes=4), 3970.80, 1002.0)  # SL -> reseed armed
    assert m.reseed_after > 0
    # cross into S2 BEFORE the cooldown elapses -> the S1 re-seed is dropped
    _tick(tr, m, st["S2"] + pd.Timedelta(minutes=2), 3970.80, 1003.0)
    assert m.reseed_after == 0.0                            # cooldown dropped at the boundary
    assert not any(RS.rgs_session(o.comment) == "S1" for o in br.pendings())


# --- restart inside S2 reloads S2 anchor ------------------------------------------
def test_restart_in_s2_reloads_anchor():
    day = str(WED)
    st_stored = {"gov": R.new_day_state(),
                 "rogue_session": {"date": day, "session": "S2", "anchor": 4010.0,
                                   "ts": "t", "source": "SCHEDULED", "oco_consumed": False}}
    tr = Trader(anchors=[9999.0], st=st_stored)            # m5 would differ if called
    br = Br(); m = _mgr(tr, br)
    s2 = _starts(tr.cfg, WED)["S2"]
    RS._ensure_session_anchor(tr, tr._rogue, s2 + pd.Timedelta(hours=1), m)
    assert m._daily_anchor == 4010.0                        # RELOADED
    assert tr.adapter.m5_calls == []                        # never re-snapshotted


# --- daily governor spans sessions ------------------------------------------------
def test_daily_governor_spans_sessions():
    tr = Trader(anchors=[3977.80, 4010.0, 4050.0])
    br = Br(); m = _mgr(tr, br)
    st = _starts(tr.cfg, WED)
    _tick(tr, m, st["S1"] + pd.Timedelta(minutes=2), 3977.80, 1000.0)
    br.set_price(3960.80); _tick(tr, m, st["S1"] + pd.Timedelta(minutes=3), 3960.80, 1001.0)  # S1 fill
    n_after_s1 = tr._rogue["gov"]["reanchor_count"]
    _tick(tr, m, st["S2"] + pd.Timedelta(minutes=2), 4010.0, 2000.0)                          # S2 seed
    br.set_price(3993.0); _tick(tr, m, st["S2"] + pd.Timedelta(minutes=3), 3993.0, 2001.0)     # S2 fill
    assert tr._rogue["gov"]["reanchor_count"] == n_after_s1 + 1     # count accumulates across sessions
    # halt in S1 blocks later sessions
    tr._rogue["gov"]["day_pnl"] = -400.0
    _tick(tr, m, st["S3"] + pd.Timedelta(minutes=2), 4050.0, 3000.0)
    assert br.pendings() == [] and br.positions() == []             # loss-stop flattened, no S3 entries


# --- flag OFF = single-session (byte-identical path) ------------------------------
def test_flag_off_uses_single_daily_anchor():
    tr = Trader(anchors=[3977.80], sessions=False)
    br = Br(); m = _mgr(tr, br)
    now = RS.rogue_scheduled_utc(tr.cfg, WED) + pd.Timedelta(minutes=2)
    # flag OFF -> the single daily-anchor path (#121), not the session path
    RS._ensure_daily_anchor(tr, tr._rogue, now, m)
    assert "rogue_daily" in tr._rogue and "rogue_session" not in tr._rogue
    assert m._daily_anchor == 3977.80


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
