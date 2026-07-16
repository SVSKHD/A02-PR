"""Rogue v2 stop-mode tests (MT5 mocked; offline-runnable).

Runnable under pytest or standalone (`python tests/test_rogue_stop.py`). Covers the
OCO/chain/re-seed lifecycle, governor gating, magic isolation, the sweep exemption,
rescue-v2 non-attachment, and the synthetic 2026-07-16 crash replay.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rogue_stop as RS
from rogue_stop import RogueStopParams, RogueStopManager, ROGUE_MAGIC
import rogue as R
from config import Config

P = RogueStopParams()   # 17 / 12 / init SL 10 / cooldown 300


# --- fake broker (RGS-scoped, price-driven fills) ---------------------------------
class _Pos:
    def __init__(s, t, side, entry, sl, comment):
        s.ticket, s.side, s.entry, s.sl, s.comment = t, side, entry, sl, comment


class _Pend:
    def __init__(s, t, side, price, sl, comment):
        s.ticket, s.side, s.price, s.sl, s.comment = t, side, price, sl, comment


class FakeBroker:
    def __init__(self):
        self.pos, self.pend, self._t = {}, {}, 7000
        self.closed, self.cancelled = {}, []

    def positions(self): return list(self.pos.values())
    def pendings(self): return list(self.pend.values())

    def place_stop(self, side, price, sl, comment):
        self._t += 1
        self.pend[self._t] = _Pend(self._t, side, price, sl, comment)
        return self._t

    def cancel(self, ticket):
        self.cancelled.append(int(ticket))
        return self.pend.pop(int(ticket), None) is not None

    def modify_sl(self, ticket, sl):
        p = self.pos.get(int(ticket))
        if p: p.sl = sl; return True
        return False

    def closed_deal(self, ticket):
        return self.closed.get(int(ticket))

    def cancel_own_pendings(self):
        n = len(self.pend); self.pend.clear(); return n

    def flatten_own(self):
        n = 0
        for tk in list(self.pos): self._close(tk, self.pos[tk].sl); n += 1
        return n

    # price driver
    def set_price(self, mid):
        for tk in list(self.pend):
            o = self.pend[tk]
            if (o.side == "BUY" and mid >= o.price) or (o.side == "SELL" and mid <= o.price):
                self._t += 1
                self.pos[self._t] = _Pos(self._t, o.side, o.price, o.sl, o.comment)
                del self.pend[tk]
        for tk in list(self.pos):
            p = self.pos[tk]
            if (p.side == "BUY" and mid <= p.sl) or (p.side == "SELL" and mid >= p.sl):
                self._close(tk, p.sl)

    def _close(self, tk, exit_px):
        p = self.pos.pop(int(tk))
        sgn = 1.0 if p.side == "BUY" else -1.0
        pnl = sgn * (exit_px - p.entry) * 0.35 * 100
        self.closed[int(tk)] = {"pnl": pnl, "exit_price": exit_px}


def _mgr(br, anchor=4030.0, cfg=None):
    cfg = cfg or Config()
    gov = R.new_day_state()
    return RogueStopManager(br, P, gov, cfg, anchor_provider=lambda: anchor), gov


def _rgs(br, tag):
    return [o for o in list(br.pend.values()) + list(br.pos.values()) if RS.parse_rgs(o.comment) == tag]


# --- geometry ---------------------------------------------------------------------
def test_oco_geometry_and_sl():
    plan = RS.oco_plan(4030.0, P)
    assert [o.side for o in plan] == ["BUY", "SELL"]
    assert [o.price for o in plan] == [4047.0, 4013.0]        # anchor ± 17
    assert plan[0].sl == 4037.0 and plan[1].sl == 4023.0      # entry ∓ 10
    assert all(o.comment == "RGS:A1" for o in plan)
    c = RS.chain_next(4013.0, "SELL", 1, P)
    assert c.side == "SELL" and c.price == 4001.0 and c.sl == 4011.0 and c.comment == "RGS:C1"


def test_trail_arm_gap_oneway():
    # SELL entry 4013, init SL 4023; arms at +5, gap 3 early
    assert RS.update_trail("SELL", 4013, 4009, 4023, P) == 4023        # +4 < arm
    assert RS.update_trail("SELL", 4013, 4008, 4023, P) == 4011        # +5 -> peak+3
    assert RS.update_trail("SELL", 4013, 3996, 4011, P) == 4002        # +17 -> gap 6 (deep), peak+6
    assert RS.update_trail("SELL", 4013, 4002, 4002, P) == 4002        # never loosens


# --- OCO + first-fill sibling cancel ----------------------------------------------
def test_oco_placed_and_first_fill_cancels_sibling():
    br = FakeBroker(); mgr, gov = _mgr(br)
    mgr.on_tick(4030.0, 1000.0)                          # seed OCO
    assert sorted(o.price for o in br.pendings()) == [4013.0, 4047.0]
    assert all(o.comment == "RGS:A1" for o in br.pendings())
    br.set_price(4013.0); mgr.on_tick(4013.0, 1001.0)   # SELL fills
    assert len(br.pos) == 1 and list(br.pos.values())[0].side == "SELL"
    assert not any(o.comment == "RGS:A1" for o in br.pendings())   # buy sibling cancelled
    assert gov["reanchor_count"] == 1                    # fill consumed a slot


# --- chain -------------------------------------------------------------------------
def test_chain_one_at_a_time():
    br = FakeBroker(); mgr, gov = _mgr(br)
    mgr.on_tick(4030.0, 1000.0)
    br.set_price(4013.0); mgr.on_tick(4013.0, 1001.0)   # SELL fill -> chain C1 @ 4001
    c1 = [o for o in br.pendings() if o.comment == "RGS:C1"]
    assert len(c1) == 1 and c1[0].price == 4001.0
    assert sum(1 for o in br.pendings() if RS.parse_rgs(o.comment).startswith("C")) == 1
    br.set_price(4001.0); mgr.on_tick(4001.0, 1002.0)   # C1 fill -> chain C2 @ 3989
    assert [o.comment for o in br.pendings()] == ["RGS:C2"]
    assert [o.price for o in br.pendings()] == [3989.0]
    # never two unfilled chains at once
    assert sum(1 for o in br.pendings() if RS.parse_rgs(o.comment).startswith("C")) == 1
    assert gov["reanchor_count"] == 2                    # two fills


# --- SL -> cancel chain -> re-seed after cooldown ---------------------------------
def test_sl_cancels_chain_and_reseeds_after_cooldown():
    br = FakeBroker(); mgr, gov = _mgr(br)
    mgr.on_tick(4030.0, 1000.0)
    br.set_price(4013.0); mgr.on_tick(4013.0, 1001.0)   # SELL @4013, chain C1 @4001 resting
    assert gov["reanchor_count"] == 1
    br.set_price(4023.0); mgr.on_tick(4023.0, 1002.0)   # reverses up -> SELL SL @4023
    assert len(br.pos) == 0
    assert not any(o.comment == "RGS:C1" for o in br.pendings())   # chain cancelled
    assert gov["consec_fails"] == 1
    # cooldown NOT elapsed -> no re-seed yet
    mgr.on_tick(4023.0, 1200.0)
    assert br.pendings() == []
    # cooldown elapsed (>300s) -> re-seed ±17 at current price, budget decremented
    mgr.on_tick(4023.0, 1002.0 + 301)
    assert sorted(o.price for o in br.pendings()) == [4006.0, 4040.0]   # 4023 ± 17
    assert gov["reanchor_count"] == 2                    # fill(1) + re-seed(1)


# --- governor: cap + loss halt ----------------------------------------------------
def test_governor_cap_counts_fills_and_reseeds():
    br = FakeBroker(); mgr, gov = _mgr(br)
    gov["reanchor_count"] = 9                            # one slot from the cap (10)
    mgr.on_tick(4030.0, 1000.0)
    br.set_price(4013.0); mgr.on_tick(4013.0, 1001.0)   # fill -> slot 10 -> cap hit
    assert gov["reanchor_count"] == 10
    # cap reached -> no new chain placed
    assert not any(RS.parse_rgs(o.comment).startswith("C") for o in br.pendings())


def test_loss_halt_flattens_and_cancels_own_only():
    br = FakeBroker(); mgr, gov = _mgr(br)
    mgr.on_tick(4030.0, 1000.0)
    br.set_price(4013.0); mgr.on_tick(4013.0, 1001.0)   # a position + a chain pending exist
    assert len(br.pos) == 1 and len(br.pend) == 1
    gov["day_pnl"] = -400.0                              # <= -370 loss stop
    mgr.on_tick(4001.0, 1002.0)
    assert len(br.pos) == 0 and len(br.pend) == 0        # own pendings cancelled + flattened


# --- 2026-07-16 crash replay (the case the band engine missed) --------------------
def test_replay_0716_crash_fills_and_chains():
    br = FakeBroker(); mgr, gov = _mgr(br, anchor=4030.59)
    mgr.on_tick(4030.59, 1000.0)                         # OCO: buy 4047.59 / sell 4013.59
    # the $65 crash steps straight down through the sell stop and chain levels
    for i, px in enumerate([4020.0, 4013.0, 4001.0, 3989.0, 3977.0], start=1):
        br.set_price(px); mgr.on_tick(px, 1000.0 + i)
    filled = [p for p in br.positions()]
    assert len(filled) >= 1 and all(p.side == "SELL" for p in filled)   # FILLED on the crash
    assert mgr.chain_idx >= 1                            # chained at least once
    assert gov["reanchor_count"] >= 2                    # OCO fill + >=1 chain fill


# --- magic isolation (the live shim) ----------------------------------------------
class _MPos:
    def __init__(s, t, magic, comment, typ=1):
        s.ticket, s.magic, s.comment, s.type = t, magic, comment, typ
        s.price_open, s.sl, s.volume = 4013.0, 4023.0, 0.35


class _MOrd:
    def __init__(s, t, magic, comment, typ=5):
        s.ticket, s.magic, s.comment, s.type = t, magic, comment, typ
        s.price_open, s.sl, s.volume_current = 4001.0, 4011.0, 0.35


class FakeMT5:
    POSITION_TYPE_BUY = 0; POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY_STOP = 4; ORDER_TYPE_SELL_STOP = 5
    TRADE_RETCODE_DONE = 10009

    def __init__(self, pos, ords):
        self._p = {x.ticket: x for x in pos}
        self._o = {x.ticket: x for x in ords}

    def positions_get(self, symbol=None): return list(self._p.values())
    def orders_get(self, symbol=None): return list(self._o.values())


class FakeAdapter:
    def __init__(self, mt5): self.mt5 = mt5; self.closed, self.cancelled = [], []
    def close_position(self, t, dry_run=False): self.closed.append(int(t)); self.mt5._p.pop(int(t), None)
    def cancel_order(self, t, dry_run=False): self.cancelled.append(int(t)); self.mt5._o.pop(int(t), None)


class FakeTrader:
    def __init__(self, adapter): self.adapter = adapter; self.paper = True
    class cfg: symbol = "XAUUSD"; lot_size = 0.35


def test_shim_magic_isolation():
    ours_p = _MPos(1, ROGUE_MAGIC, "RGS:A1")
    foreign_p = _MPos(2, 20260522, "AUR_A1_BUY A:4028")     # straddle position
    ours_o = _MOrd(10, ROGUE_MAGIC, "RGS:C1")
    foreign_o = _MOrd(11, 20260522, "AUR_A1_SELL A:4028")
    mt5 = FakeMT5([ours_p, foreign_p], [ours_o, foreign_o])
    br = RS._StopBroker(FakeTrader(FakeAdapter(mt5)))
    assert {p.ticket for p in br.positions()} == {1}       # only our RGS+magic
    assert {o.ticket for o in br.pendings()} == {10}
    br.flatten_own(); br.cancel_own_pendings()
    assert set(mt5._p) == {2} and set(mt5._o) == {11}       # foreign untouched


# --- interaction: stale_leg_sweep exempts RGS -------------------------------------
def test_sweep_exempts_rgs():
    import stale_leg_sweep as sweep
    assert sweep._is_rescue_boost_comment("RGS:A1") is True
    assert sweep._is_rescue_boost_comment("RGS:C3") is True

    class O:
        def __init__(s, t, ty, po, c, m): s.ticket, s.type, s.price_open, s.comment, s.magic = t, ty, po, c, m
    class M:
        ORDER_TYPE_SELL_STOP = 5; ORDER_TYPE_BUY_STOP = 4; POSITION_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1; TRADE_ACTION_REMOVE = 2; TRADE_RETCODE_DONE = 10009
        def __init__(s, o): s._o = {x.ticket: x for x in o}; s.sent = []
        def orders_get(s, symbol=None): return list(s._o.values())
        def positions_get(s, symbol=None): return []
        def order_send(s, r): s.sent.append(r); return type("R", (), {"retcode": 10009})()
    # an RGS order carrying the STRADDLE magic (edge) must STILL be exempt by comment
    rgs = O(701, M.ORDER_TYPE_SELL_STOP, 4013.0, "RGS:A1", 20260522)
    stale = O(101, M.ORDER_TYPE_SELL_STOP, 4008.0, sweep.tag_comment("AUR_A1_SELL", 4028.77), 20260522)
    mt5 = M([rgs, stale])
    res = sweep.sweep_stale_legs(mt5, "XAUUSD", 4048.77, magic=20260522)
    assert [r["ticket"] for r in res] == [101]             # only the real stale leg
    assert all(r.get("order") != 701 for r in mt5.sent)


# --- interaction: rescue_boost_v2 does NOT attach to a Rogue fill -----------------
def test_rescue_boost_not_attach_to_rogue():
    import rescue_boost as RB
    class Pos:
        def __init__(s, t, side, entry, comment): s.ticket, s.side, s.entry, s.comment, s.lot, s.sl = t, side, entry, comment, 0.35, 0.0
    class Br:
        def __init__(s, pos): s._pos = pos; s.placed = []
        def positions(s): return s._pos
        def pendings(s): return []
        def place_pending(s, *a): s.placed.append(a); return 1
        def cancel(s, t): return True
        def modify_sl(s, t, sl): return True
    # the LIVE is_parent: only genuine anchor legs ("A:" tag, not RB) get boosts
    is_parent = lambda p: ("A:" in p.comment) and (not RB.is_boost_comment(p.comment))
    br = Br([Pos(1, "SELL", 4013.0, "RGS:A1")])            # a Rogue stop-mode fill
    mgr = RB.RescueBoostManager(br, RB.RescueBoostParams(), is_parent=is_parent)
    mgr.place_boosts_for_new_fills()
    assert br.placed == []                                  # no boosts on a Rogue leg


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
