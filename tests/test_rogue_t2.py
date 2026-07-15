"""Rogue T2 Continuation V1 — unit tests (MT5 fully mocked).

Runnable under pytest or standalone (`python tests/test_rogue_t2.py`). Covers the
frozen mechanism (OCO / T1 trail / T2 continuation / phase expiry), the hardening
(restart idempotency, cap halt persisted across restart, Monday 06:00), lot scaling,
and multi-bot magic isolation (own-magic-only flatten/cancel + stale_leg_sweep).
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rogue_t2 import engine as E
from rogue_t2.config import RogueT2Config, ROGUE_T2_MAGIC
from rogue_t2.broker import MT5Broker, OwnPosition, OwnPending, Tick
from rogue_t2.bot import RogueT2Bot, TAG_T2, TAG_BUY, TAG_SELL, _role
from rogue_t2.statestore import StateStore
from rogue_t2.notify import Notifier


# --- SimBroker: price-driven fills, own-magic only (for bot mechanism tests) ------
class _Pos:
    def __init__(self, ticket, side, entry, sl, lot, tag):
        self.ticket, self.side, self.entry, self.sl = ticket, side, entry, sl
        self.lot, self.tag, self.magic = lot, tag, ROGUE_T2_MAGIC


class _Pend:
    def __init__(self, ticket, side, price, sl, lot, tag):
        self.ticket, self.side, self.price, self.sl = ticket, side, price, sl
        self.lot, self.tag, self.magic = lot, tag, ROGUE_T2_MAGIC


class SimBroker:
    def __init__(self, cfg):
        self.cfg = cfg
        self._t = 1000
        self.positions = {}
        self.pendings = {}
        self.realized = 0.0
        self.closed = {}          # ticket -> {exit_price, pnl, slippage}
        self.place_calls = []     # tags placed

    # interface used by the bot
    def startup_assertions(self):
        return None

    def own_positions(self):
        return [OwnPosition(p.ticket, p.side, p.entry, p.sl, p.lot, p.tag, p.magic)
                for p in self.positions.values()]

    def own_pendings(self):
        return [OwnPending(o.ticket, o.side, o.price, o.sl, o.lot, o.tag, o.magic)
                for o in self.pendings.values()]

    def place_pending(self, side, price, sl, tag):
        self._t += 1
        self.pendings[self._t] = _Pend(self._t, side, price, sl, self.cfg.lot, tag)
        self.place_calls.append(tag)
        return self._t

    def cancel(self, ticket):
        return self.pendings.pop(int(ticket), None) is not None

    def modify_sl(self, ticket, new_sl):
        p = self.positions.get(int(ticket))
        if p:
            p.sl = new_sl
            return True
        return False

    def close_position(self, ticket):
        return self._book_close(int(ticket), self.positions[int(ticket)].sl)

    def cancel_own_pendings(self):
        n = len(self.pendings)
        self.pendings.clear()
        return n

    def flatten_own(self):
        n = 0
        for tk in list(self.positions.keys()):
            self._book_close(tk, self.positions[tk].sl)
            n += 1
        return n

    def day_realized_usd(self, frm, to):
        return self.realized

    def closed_deal(self, ticket):
        return self.closed.get(int(ticket))

    # test driver — move the market and process fills/exits at `mid`
    def set_price(self, mid):
        for tk in list(self.pendings.keys()):
            o = self.pendings[tk]
            hit = (o.side == "BUY" and mid >= o.price) or (o.side == "SELL" and mid <= o.price)
            if hit:
                self._t += 1
                self.positions[self._t] = _Pos(self._t, o.side, o.price, o.sl,
                                                self.cfg.lot, o.tag)
                del self.pendings[tk]
        for tk in list(self.positions.keys()):
            p = self.positions[tk]
            hit = (p.side == "BUY" and mid <= p.sl) or (p.side == "SELL" and mid >= p.sl)
            if hit:
                self._book_close(tk, p.sl)

    def _book_close(self, ticket, exit_price):
        p = self.positions.pop(int(ticket), None)
        if p is None:
            return False
        pnl = E.pnl_usd(p.side, p.entry, exit_price, p.lot, self.cfg.contract_size)
        self.realized += pnl
        self.closed[int(ticket)] = {"exit_price": exit_price, "pnl": pnl, "slippage": 0.0}
        return True


def _bot(tmp, cfg=None, store=None, broker=None):
    cfg = cfg or RogueT2Config()
    store = store or StateStore(tmp, cfg)
    broker = broker or SimBroker(cfg)
    return RogueT2Bot(cfg, broker, store, Notifier(capture=True),
                      history_window=lambda d: (0, 0)), broker, store


def _ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm)


def _tick(mid, spread=0.0, t=1):
    return Tick(bid=mid - spread / 2, ask=mid + spread / 2, time_msc=t * 1000)


# --- pure engine tests ------------------------------------------------------------
def test_cap_and_pnl_scale_across_lots(tmp_path=None):
    for lot, expected_cap in ((0.15, -262.50), (0.35, -612.50), (0.40, -700.0)):
        cfg = RogueT2Config(lot=lot)
        assert abs(E.daily_cap_usd(cfg) - expected_cap) < 1e-6, (lot, E.daily_cap_usd(cfg))
    # PnL scales linearly with lot for the same move (+10.00 on XAUUSD, cs=100)
    base = E.pnl_usd("BUY", 4000, 4010, 0.40, 100.0)
    assert abs(base - 400.0) < 1e-6
    assert abs(E.pnl_usd("BUY", 4000, 4010, 0.15, 100.0) - 150.0) < 1e-6
    assert abs(E.pnl_usd("SELL", 4000, 3990, 0.35, 100.0) - 350.0) < 1e-6


def test_monday_0600_schedule():
    cfg = RogueT2Config()
    monday = _ist(2026, 7, 13, 5, 30)   # 2026-07-13 is a Monday
    assert E.resolve_phase(cfg, monday) is None            # before 06:00 -> closed
    assert E.resolve_phase(cfg, _ist(2026, 7, 13, 6, 0)) == 0
    wed = _ist(2026, 7, 15, 5, 30)      # weekday phase 0 opens 05:00
    assert E.resolve_phase(cfg, wed) == 0
    assert E.resolve_phase(cfg, _ist(2026, 7, 15, 22, 0)) is None   # 22:00 closed
    assert E.resolve_phase(cfg, _ist(2026, 7, 15, 13, 0)) == 1


# --- bot mechanism tests ----------------------------------------------------------
def test_t1_fill_trail_exit(tmp_path):
    bot, sim, store = _bot(str(tmp_path / "s.json"))
    ist = _ist(2026, 7, 15, 10, 0)
    # arm at A1=4000 -> buy stop 4017 / sell stop 3983
    sim.set_price(4000); bot.on_tick(_tick(4000, t=1), ist)
    assert sorted(_role(t) for t in sim.place_calls) == [TAG_BUY, TAG_SELL]
    # price rises through 4017 -> T1 buy fills @4017, sibling sell cancelled
    sim.set_price(4017); bot.on_tick(_tick(4017, t=2), ist)
    assert len(sim.positions) == 1
    assert not any(o.side == "SELL" for o in sim.own_pendings())   # OCO sibling gone
    t1 = list(sim.positions.values())[0]
    assert abs(t1.sl - 4014.40) < 1e-6                            # entry - 2.60
    # trail up: at +1.5 it arms; ratchets in 0.50 steps to peak-2.60
    for i, px in enumerate((4018.5, 4022.0), start=3):
        sim.set_price(px); bot.on_tick(_tick(px, t=i), ist)
    t1 = list(sim.positions.values())[0]
    assert abs(t1.sl - 4019.40) < 1e-6                            # 4022 - 2.60
    # pull back to the trailed stop -> exit in profit
    sim.set_price(4019.40); bot.on_tick(_tick(4019.40, t=9), ist)
    assert len(sim.positions) == 0
    assert sim.realized > 0                                        # +2.40 * 0.35 * 100 = +84


def test_oco_sibling_cancel(tmp_path):
    bot, sim, store = _bot(str(tmp_path / "s.json"))
    ist = _ist(2026, 7, 15, 10, 0)
    sim.set_price(4000); bot.on_tick(_tick(4000, t=1), ist)
    assert len(sim.pendings) == 2
    sim.set_price(3983); bot.on_tick(_tick(3983, t=2), ist)        # SELL stop fills
    assert len(sim.positions) == 1 and list(sim.positions.values())[0].side == "SELL"
    assert all(_role(o.tag) != TAG_BUY for o in sim.own_pendings())  # buy sibling cancelled


def test_t2_fires_after_t1_dead(tmp_path):
    bot, sim, store = _bot(str(tmp_path / "s.json"))
    ist = _ist(2026, 7, 15, 10, 0)
    sim.set_price(4000); bot.on_tick(_tick(4000, t=1), ist)
    sim.set_price(4017); bot.on_tick(_tick(4017, t=2), ist)        # T1 buy fills
    # T2 armed: buy stop @ 4017+12 = 4029
    t2 = [o for o in sim.own_pendings() if _role(o.tag) == TAG_T2]
    assert len(t2) == 1 and abs(t2[0].price - 4029.0) < 1e-6
    # T1 dies at its SL; T2 stays armed
    sim.set_price(4014.40); bot.on_tick(_tick(4014.40, t=3), ist)
    assert len(sim.positions) == 0
    assert any(_role(o.tag) == TAG_T2 for o in sim.own_pendings())
    # continuation to 4029 -> T2 fires
    sim.set_price(4029.0); bot.on_tick(_tick(4029.0, t=4), ist)
    assert any(_role(p.tag) == TAG_T2 for p in sim.own_positions())


def test_t2_expiry_at_phase_end(tmp_path):
    bot, sim, store = _bot(str(tmp_path / "s.json"))
    p1 = _ist(2026, 7, 15, 11, 0)
    sim.set_price(4000); bot.on_tick(_tick(4000, t=1), p1)
    sim.set_price(4017); bot.on_tick(_tick(4017, t=2), p1)         # T1 fills, T2 armed
    sim.set_price(4014.40); bot.on_tick(_tick(4014.40, t=3), p1)   # T1 dead, T2 rests
    assert any(_role(o.tag) == TAG_T2 for o in sim.own_pendings())
    # cross the 12:30 boundary into phase 1 -> T2 pending cancelled
    p2 = _ist(2026, 7, 15, 13, 0)
    sim.set_price(4014.40); bot.on_tick(_tick(4014.40, t=4), p2)
    assert all(_role(o.tag) != TAG_T2 for o in sim.own_pendings())


def test_restart_midcycle_no_duplicate(tmp_path):
    path = str(tmp_path / "s.json")
    cfg = RogueT2Config()
    store = StateStore(path, cfg)
    sim = SimBroker(cfg)
    bot, _, _ = _bot(path, cfg=cfg, store=store, broker=sim)
    ist = _ist(2026, 7, 15, 10, 0)
    sim.set_price(4000); bot.on_tick(_tick(4000, t=1), ist)
    sim.set_price(4017); bot.on_tick(_tick(4017, t=2), ist)        # T1 open + T2 pending
    assert len(sim.positions) == 1
    assert any(_role(o.tag) == TAG_T2 for o in sim.own_pendings())
    placed_before = list(sim.place_calls)

    # RESTART: brand-new bot + store from the same file, same broker (adopted) state
    store2 = StateStore(path, cfg)
    bot2 = RogueT2Bot(cfg, sim, store2, Notifier(capture=True), history_window=lambda d: (0, 0))
    bot2.startup()
    sim.set_price(4017); bot2.on_tick(_tick(4017, t=3), ist)
    # no duplicate T1/T2 placed after restart
    assert sim.place_calls == placed_before, sim.place_calls
    assert len(sim.positions) == 1
    assert sum(1 for o in sim.own_pendings() if _role(o.tag) == TAG_T2) == 1


def test_daily_cap_halt_persists_across_restart(tmp_path):
    path = str(tmp_path / "s.json")
    cfg = RogueT2Config(lot=0.40)   # cap -700
    store = StateStore(path, cfg)
    sim = SimBroker(cfg)
    bot, _, _ = _bot(path, cfg=cfg, store=store, broker=sim)
    ist = _ist(2026, 7, 15, 10, 0)
    # Pre-load realized losses beyond the cap, then a tick triggers the halt.
    sim.realized = -750.0
    sim.set_price(4000); bot.on_tick(_tick(4000, t=1), ist)
    assert store.is_halted("2026-07-15")
    assert len(sim.pendings) == 0 and len(sim.positions) == 0   # cleaned up on breach

    # RESTART same day -> still halted, no arming
    store2 = StateStore(path, cfg)
    sim2 = SimBroker(cfg)
    bot2 = RogueT2Bot(cfg, sim2, store2, Notifier(capture=True), history_window=lambda d: (0, 0))
    bot2.on_tick(_tick(4000, t=2), ist)
    assert store2.is_halted("2026-07-15")
    assert sim2.place_calls == []                               # halt blocks new cycles

    # NEXT day clears the halt and trading resumes
    bot2.on_tick(_tick(4000, t=3), _ist(2026, 7, 16, 10, 0))
    assert not store2.is_halted("2026-07-16")
    assert len(sim2.place_calls) == 2                           # fresh OCO armed


# --- MT5Broker magic isolation (foreign magic must survive) -----------------------
class _MPos:
    def __init__(self, ticket, magic, ptype=0, volume=0.35):
        self.ticket, self.magic, self.type, self.volume = ticket, magic, ptype, volume
        self.price_open, self.sl, self.tp, self.comment = 4000.0, 3990.0, 0.0, "x"
        self.symbol = "XAUUSD"


class _MOrd:
    def __init__(self, ticket, magic, otype=5, price=3983.0):
        self.ticket, self.magic, self.type = ticket, magic, otype
        self.price_open, self.sl, self.volume_current, self.comment = price, 0.0, 0.35, "x"


class FakeMT5:
    ORDER_TYPE_BUY = 0; ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_STOP = 4; ORDER_TYPE_SELL_STOP = 5
    POSITION_TYPE_BUY = 0; POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1; TRADE_ACTION_PENDING = 5; TRADE_ACTION_REMOVE = 2
    TRADE_ACTION_SLTP = 6
    TRADE_RETCODE_DONE = 10009
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    ORDER_TIME_GTC = 0; ORDER_FILLING_IOC = 1

    def __init__(self, positions, orders):
        self._pos = {p.ticket: p for p in positions}
        self._ord = {o.ticket: o for o in orders}
        self.sends = []

    def positions_get(self, symbol=None):
        return list(self._pos.values())

    def orders_get(self, symbol=None):
        return list(self._ord.values())

    def order_send(self, req):
        self.sends.append(req)
        act = req.get("action")
        if act == self.TRADE_ACTION_REMOVE:
            self._ord.pop(int(req["order"]), None)
        elif act == self.TRADE_ACTION_DEAL and "position" in req:
            self._pos.pop(int(req["position"]), None)

        class _R:
            retcode = FakeMT5.TRADE_RETCODE_DONE
            order = 555
        return _R()


def test_foreign_magic_survives_flatten_and_cancel():
    cfg = RogueT2Config()   # trading_unlocked=False -> uses simulated send... but
    # destructive cleanup must go to the broker regardless of lock. For isolation we
    # exercise the real order_send path by unlocking (the FakeMT5 records the sends).
    cfg.trading_unlocked = True
    ours, foreign = ROGUE_T2_MAGIC, 20260522
    positions = [_MPos(1, ours), _MPos(2, foreign), _MPos(3, ours)]
    orders = [_MOrd(10, ours), _MOrd(11, foreign), _MOrd(12, 20260626)]
    mt5 = FakeMT5(positions, orders)
    broker = MT5Broker(mt5, cfg)

    assert {p.ticket for p in broker.own_positions()} == {1, 3}
    assert {o.ticket for o in broker.own_pendings()} == {10}

    broker.flatten_own()
    broker.cancel_own_pendings()
    # foreign magics untouched
    assert set(mt5._pos.keys()) == {2}, mt5._pos.keys()
    assert set(mt5._ord.keys()) == {11, 12}, mt5._ord.keys()


def test_simulated_send_is_default_order_path():
    cfg = RogueT2Config()   # trading_unlocked defaults False
    mt5 = FakeMT5([], [])
    broker = MT5Broker(mt5, cfg)
    tk = broker.place_pending("BUY", 4017.0, 4014.40, "P0#C1#A1B")
    assert tk is not None and tk >= 900_000_000        # synthetic simulated ticket
    assert mt5.sends == []                              # NOTHING sent to the broker


def test_startup_assertions_reject_non_hedging():
    cfg = RogueT2Config()

    class _Acct:
        margin_mode = 0        # NOT retail-hedging
        trade_expert = True

    class _BadMT5(FakeMT5):
        def account_info(self):
            return _Acct()
        def terminal_info(self):
            return type("T", (), {"trade_allowed": True})()
        def symbol_info(self, s):
            return type("S", (), {"visible": True})()
        def symbol_select(self, s, v):
            return True

    broker = MT5Broker(_BadMT5([], []), cfg)
    try:
        broker.startup_assertions()
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "startup must refuse a non-RETAIL_HEDGING account"


# --- watchdog ---------------------------------------------------------------------
def test_watchdog_state_mtime_and_backoff():
    from rogue_t2 import watchdog as W
    cfg = W.WatchdogConfig(heartbeat_timeout_s=60, state_mtime_timeout_s=120)
    st = W.WatchdogState()
    # healthy
    assert W.decide_action(cfg, st, now=1000, last_heartbeat=990, state_mtime=990,
                           in_phase=True, process_alive=True) == "ok"
    # stale state.json DURING a phase -> kill (the new check)
    assert W.decide_action(cfg, st, now=1000, last_heartbeat=990, state_mtime=800,
                           in_phase=True, process_alive=True) == "kill"
    # same staleness OUT of phase is tolerated
    assert W.decide_action(cfg, st, now=1000, last_heartbeat=990, state_mtime=800,
                           in_phase=False, process_alive=True) == "ok"
    # dead process -> restart
    assert W.decide_action(cfg, st, now=1000, last_heartbeat=990, state_mtime=990,
                           in_phase=True, process_alive=False) == "restart"
    # backoff grows and caps
    assert W.backoff_seconds(cfg, 0) == cfg.backoff_base_s
    assert W.backoff_seconds(cfg, 50) == cfg.backoff_max_s


def test_watchdog_dirty_restart_cooldown():
    from rogue_t2 import watchdog as W
    cfg = W.WatchdogConfig(dirty_restart_limit=6, dirty_window_s=600, cooldown_s=900)
    st = W.WatchdogState()
    for i in range(6):
        st = W.register_restart(cfg, st, now=100 + i, dirty=True)
    assert st.cooldown_until > 0                        # 6 dirty restarts -> cooldown
    assert W.decide_action(cfg, st, now=st.cooldown_until - 1, last_heartbeat=0,
                           state_mtime=0, in_phase=True, process_alive=False) == "cooldown"


# --- standalone runner ------------------------------------------------------------
def _run_all():
    import tempfile, pathlib, inspect
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for name, fn in tests:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as d:
                    fn(pathlib.Path(d))
            else:
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
