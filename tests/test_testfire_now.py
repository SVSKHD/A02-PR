"""Tests for the ZERO-GATE `/testfire now` path (tfnow.fire + bot.py tfnow) and the
rescue-boost P&L isolation fix that a TF_ straddle relies on.

Proven here without any broker:
  1. tfnow.fire places a two-leg non-OCO straddle at the current mid via the ADAPTER
     ONLY, honours dist + lot, tags each leg TF_.../A:<level>, and REPORTS the broker
     retcode + message (including no-tick and reject cases) — never a "?".
  2. pnl_source.magic_day_net(exclude_test=True) drops the RB rescue legs of a TF_ parent
     position, while a NORMAL anchor's RB legs are unchanged (still counted).
"""
import types

import pnl_source as ps
import tfnow


# ── 1. tfnow.fire — adapter-only, zero-gate, reports the broker verbatim ─────────────
class _FakeResult:
    def __init__(self, retcode, comment=""):
        self.retcode = retcode
        self.comment = comment
        self.order = 1


class _FakeMT5:
    def __init__(self, tick=(4000.0, 4000.2)):
        self._tick = types.SimpleNamespace(bid=tick[0], ask=tick[1]) if tick else None

    def symbol_info_tick(self, symbol):
        return self._tick


class _FakeAdapter:
    def __init__(self, mt5, result=_FakeResult(10009, "")):
        self.mt5 = mt5
        self._result = result
        self.calls = []

    def place_stop_order(self, symbol, side, price, lot, sl, tp, comment):
        self.calls.append({"symbol": symbol, "side": side, "price": price, "lot": lot,
                           "sl": sl, "tp": tp, "comment": comment})
        return self._result


class _Tele:
    def __init__(self):
        self.posts = []

    def warn(self, body):
        self.posts.append(body)


def _cfg(**kw):
    base = dict(symbol="XAUUSD", sl_dist=18.0, tp_dist=30.0, lot_size=0.53)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_fire_places_both_legs_at_mid_dist_lot():
    ad = _FakeAdapter(_FakeMT5((4000.0, 4000.20)))
    tele = _Tele()
    out = tfnow.fire(ad, _cfg(), tele, dist=3.0, lot=0.68)
    mid = (4000.0 + 4000.20) / 2.0            # 4000.10
    assert out["mid"] == mid and out["dist"] == 3.0 and out["lot"] == 0.68
    assert len(ad.calls) == 2 and len(out["rows"]) == 2
    buy, sell = ad.calls[0], ad.calls[1]
    assert buy["side"] == "BUY" and buy["price"] == round(mid + 3.0, 2)
    assert buy["sl"] == round(mid + 3.0 - 18.0, 2) and buy["tp"] == round(mid + 3.0 + 30.0, 2)
    assert sell["side"] == "SELL" and sell["price"] == round(mid - 3.0, 2)
    assert sell["sl"] == round(mid - 3.0 + 18.0, 2) and sell["tp"] == round(mid - 3.0 - 30.0, 2)
    assert buy["lot"] == 0.68 and sell["lot"] == 0.68
    # TF_ marker (P&L exclusion) + A:<price> origin tag (rescue_boost adoption) on every leg.
    for c in ad.calls:
        assert c["comment"].startswith("TF_") and "A:" in c["comment"]
    # exactly ONE card, both rows, rc filled (never "?").
    assert len(tele.posts) == 1
    card = tele.posts[0]
    assert "BUY  stop @" in card and "SELL stop @" in card and "rc 10009" in card


def test_fire_lot_defaults_to_cfg_and_dist_defaults_to_3():
    ad = _FakeAdapter(_FakeMT5((4000.0, 4000.0)))
    out = tfnow.fire(ad, _cfg(lot_size=0.53), _Tele(), dist=None, lot=None)
    assert out["dist"] == 3.0            # default short distance
    assert out["lot"] == 0.53            # cfg.lot_size
    assert all(c["lot"] == 0.53 for c in ad.calls)


def test_fire_no_tick_reports_and_places_nothing():
    ad = _FakeAdapter(_FakeMT5(tick=None))
    tele = _Tele()
    out = tfnow.fire(ad, _cfg(), tele, dist=3.0)
    assert out["error"] == "no_tick"
    assert ad.calls == []                          # nothing placed
    assert len(tele.posts) == 1 and "NO TICK" in tele.posts[0]


def test_fire_reports_reject_retcode_and_broker_message():
    ad = _FakeAdapter(_FakeMT5((4000.0, 4000.2)),
                      result=_FakeResult(10019, "No money"))
    tele = _Tele()
    out = tfnow.fire(ad, _cfg(), tele, dist=3.0, lot=5.0)
    assert all(r["rc"] == 10019 for r in out["rows"])
    assert all("No money" in r["msg"] for r in out["rows"])
    assert "rc 10019 No money" in tele.posts[0]


def test_fire_none_result_is_not_a_question_mark():
    ad = _FakeAdapter(_FakeMT5((4000.0, 4000.2)), result=None)
    out = tfnow.fire(ad, _cfg(), _Tele(), dist=3.0)
    assert all(r["rc"] is None for r in out["rows"])
    assert all(r["msg"] for r in out["rows"])       # a real message, never empty/"?"
    assert "rc None" in out["card"]


def test_fire_string_args_parse():
    ad = _FakeAdapter(_FakeMT5((4000.0, 4000.2)))
    out = tfnow.fire(ad, _cfg(), _Tele(), dist="4.5", lot="0.68")
    assert out["dist"] == 4.5 and out["lot"] == 0.68


# ── 2. RB-of-TF exclusion (the isolation fix, unchanged from #136) ───────────────────
class _Deal:
    def __init__(self, magic, entry, profit, comment="", position_id=None):
        self.magic, self.entry, self.profit, self.comment = magic, entry, profit, comment
        self.position_id = position_id
        self.swap = self.commission = 0.0


A = ps.ANCHORS_MAGIC


def test_rb_legs_of_tf_parent_are_excluded():
    deals = [
        _Deal(A, 1, -954.0, "TF_143001_B A:4000.00", position_id=1000),
        _Deal(A, 1, 120.0, "TF_143001_S A:4000.00", position_id=1001),
        _Deal(A, 1, -510.0, "RB1:1000", position_id=1002),     # RB of a TF_ parent -> excluded
        _Deal(A, 1, 200.0, "AUR_A1_BUY A:4000.00", position_id=3000),
        _Deal(A, 1, -75.0, "RB1:3000", position_id=3001),      # RB of a REAL anchor -> counted
    ]
    assert ps.magic_day_net(deals, A, exclude_test=True) == 125.0
    assert ps.magic_day_net(deals, A) == round(-954 + 120 - 510 + 200 - 75, 2)


def test_normal_anchor_rb_unchanged_when_no_testfire_present():
    deals = [
        _Deal(A, 1, 200.0, "AUR_A1_BUY A:4000.00", position_id=3000),
        _Deal(A, 1, -75.0, "RB1:3000", position_id=3001),
    ]
    assert ps.magic_day_net(deals, A, exclude_test=True) == 125.0
    assert ps.magic_day_net(deals, A) == 125.0
