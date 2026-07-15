"""Unit tests for rescue_boost (v2) — pre-SL counter-direction recovery.

Mocks the broker (positions / pendings / place_pending / cancel / modify_sl). No
MetaTrader5 needed. Runnable under pytest or standalone
(`python tests/test_rescue_boost.py`).

Frozen launch values exercised: BOOST_LOT 0.45, offsets 15/25, trail +10 / gap 5,
MAX_BOOSTS 2. Resolved rules: boost SL = original entry; unfilled boosts cancelled
when the parent closes; original untouched.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rescue_boost as RB
from rescue_boost import RescueBoostParams, RescueBoostManager, boost_plan, update_boost_trail

P = RescueBoostParams()   # launch defaults


# --- fake broker ------------------------------------------------------------------
class Obj:
    def __init__(self, ticket, side, price_or_entry, sl, lot, comment):
        self.ticket = ticket
        self.side = side
        self.entry = price_or_entry      # positions read .entry
        self.price = price_or_entry      # pendings read .price (unused by manager)
        self.sl = sl
        self.lot = lot
        self.comment = comment


class FakeBroker:
    def __init__(self):
        self._pos = {}
        self._pend = {}
        self._t = 5000
        self.cancelled = []
        self.sl_mods = []

    def positions(self):
        return list(self._pos.values())

    def pendings(self):
        return list(self._pend.values())

    def place_pending(self, side, price, sl, lot, comment):
        self._t += 1
        self._pend[self._t] = Obj(self._t, side, price, sl, lot, comment)
        return self._t

    def cancel(self, ticket):
        self.cancelled.append(int(ticket))
        return self._pend.pop(int(ticket), None) is not None

    def modify_sl(self, ticket, new_sl):
        p = self._pos.get(int(ticket))
        if p:
            p.sl = new_sl
            self.sl_mods.append((int(ticket), new_sl))
            return True
        return False

    # test helpers
    def add_parent(self, ticket, side, entry, sl, comment="AUR_A1 A:4072.00"):
        self._pos[ticket] = Obj(ticket, side, entry, sl, 0.35, comment)

    def fill_pending(self, ticket):
        o = self._pend.pop(int(ticket))
        self._pos[int(ticket)] = Obj(o.ticket, o.side, o.price, o.sl, o.lot, o.comment)

    def close_position(self, ticket):
        self._pos.pop(int(ticket), None)


# --- pure geometry ----------------------------------------------------------------
def test_boost_plan_buy_parent():
    plan = boost_plan("BUY", 4072.0, 7001, P)
    assert [b.side for b in plan] == ["SELL", "SELL"]
    assert [b.price for b in plan] == [4057.0, 4047.0]     # entry-15, entry-25
    assert all(b.sl == 4072.0 for b in plan)               # SL at original entry
    assert all(b.lot == 0.45 for b in plan)
    assert [b.comment for b in plan] == ["RB1:7001", "RB2:7001"]


def test_boost_plan_sell_parent_mirror():
    plan = boost_plan("SELL", 4072.0, 7002, P)
    assert [b.side for b in plan] == ["BUY", "BUY"]
    assert [b.price for b in plan] == [4087.0, 4097.0]     # entry+15, entry+25
    assert all(b.sl == 4072.0 for b in plan)


def test_trail_inactive_until_plus10_then_gap5_oneway():
    # SELL boost entered @4057 (initial SL at original entry 4072)
    entry, sl0 = 4057.0, 4072.0
    assert update_boost_trail("SELL", entry, 4050.0, sl0, P) == sl0     # fav 7 < 10
    sl1 = update_boost_trail("SELL", entry, 4045.0, sl0, P)             # fav 12 -> peak+5
    assert sl1 == 4050.0
    sl2 = update_boost_trail("SELL", entry, 4040.0, sl1, P)             # fav 17 -> 4045
    assert sl2 == 4045.0
    # one-way: a shallower low never loosens the stop
    assert update_boost_trail("SELL", entry, 4048.0, sl2, P) == sl2
    # BUY boost mirror (entered @4087, original entry 4072)
    assert update_boost_trail("BUY", 4087.0, 4097.0, 4072.0, P) == 4092.0


# --- manager ----------------------------------------------------------------------
def test_places_two_boosts_on_new_fill_idempotent():
    br = FakeBroker()
    br.add_parent(7001, "BUY", 4072.0, 4054.0)
    mgr = RescueBoostManager(br, P)
    placed = mgr.place_boosts_for_new_fills()
    assert len(placed) == 2
    assert sorted(o.comment for o in br.pendings()) == ["RB1:7001", "RB2:7001"]
    # calling again does NOT duplicate (idempotent / restart-safe)
    mgr.place_boosts_for_new_fills()
    assert len(br.pendings()) == 2


def test_only_boosts_anchor_legs_via_is_parent():
    br = FakeBroker()
    br.add_parent(7001, "BUY", 4072.0, 4054.0, comment="AUR_A1_BUY A:4072.00")
    br.add_parent(8001, "BUY", 5000.0, 4990.0, comment="ROGUE_X")   # no A: tag
    # live-style predicate: only genuine anchor legs (A: tag, not RB)
    is_parent = lambda p: ("A:" in p.comment) and (not RB.is_boost_comment(p.comment))
    mgr = RescueBoostManager(br, P, is_parent=is_parent)
    mgr.place_boosts_for_new_fills()
    parents = {RB.parse_boost_comment(o.comment)[1] for o in br.pendings()}
    assert parents == {7001}, parents      # 8001 (non-anchor) never boosted


def test_trail_open_boost_modifies_sl():
    br = FakeBroker()
    br.add_parent(7001, "BUY", 4072.0, 4054.0)
    mgr = RescueBoostManager(br, P)
    mgr.place_boosts_for_new_fills()
    rb1 = [t for t, o in br._pend.items() if o.comment == "RB1:7001"][0]
    br.fill_pending(rb1)                    # boost #1 fills (sell @4057)
    mgr.on_tick(4045.0)                      # +12 favorable -> trail to 4050
    assert br._pos[rb1].sl == 4050.0


def test_cancel_orphan_boosts_when_parent_closes():
    br = FakeBroker()
    br.add_parent(7001, "BUY", 4072.0, 4054.0)
    mgr = RescueBoostManager(br, P)
    mgr.on_tick(4072.0)                      # places RB1 + RB2 pendings
    assert len(br.pendings()) == 2
    br.close_position(7001)                  # original SLs out
    mgr.on_tick(4055.0)
    assert br.pendings() == []               # both unfilled boosts cancelled
    assert sorted(br.cancelled) != []        # (and logged as orphan cancels)


def test_acceptance_full_flow():
    """buy 0.35 @ 4072 -> boosts sell 0.45 @ 4057/4047 (SL 4072). Boost#1 fills and
    trails on continuation; the parent SLs out; the still-unfilled boost#2 is
    cancelled; boost#1 keeps running on its trail (original untouched throughout)."""
    br = FakeBroker()
    br.add_parent(7001, "BUY", 4072.0, 4054.0)
    mgr = RescueBoostManager(br, P)

    mgr.on_tick(4072.0)                      # 1) place both boosts
    pend = {o.comment: t for t, o in br._pend.items()}
    assert set(pend) == {"RB1:7001", "RB2:7001"}
    assert br._pend[pend["RB1:7001"]].price == 4057.0 and br._pend[pend["RB1:7001"]].sl == 4072.0
    assert br._pend[pend["RB2:7001"]].price == 4047.0

    br.fill_pending(pend["RB1:7001"])        # 2) price hits 4057 -> boost#1 fills
    mgr.on_tick(4045.0)                       # 3) continuation -> boost#1 trails to 4050
    assert br._pos[pend["RB1:7001"]].sl == 4050.0

    br.close_position(7001)                  # 4) original hits its own hard SL (untouched by us)
    mgr.on_tick(4043.0)                       # 5) parent gone -> orphan boost#2 cancelled
    assert "RB2:7001" not in {o.comment for o in br.pendings()}
    assert pend["RB1:7001"] in br._pos       # boost#1 still open, running its trail
    # boost#1 trailed further on the new low (4043 -> peak+5 = 4048)
    assert br._pos[pend["RB1:7001"]].sl == 4048.0


# --- standalone runner ------------------------------------------------------------
def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"FAIL  {name}: {e!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
