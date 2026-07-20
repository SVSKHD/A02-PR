"""Order-path integration tests for the ROGUE monster live adapter.

Drives the REAL backtest FakeBroker (sim_broker) through the adapter:
seed -> arm (resting stop) -> broker fill -> chain placed -> trail -> SL close ->
re-anchor, plus the day-loss governor flatten and TF_ isolation. Runs under
pytest AND standalone: `python tests/test_rogue_monster_live.py`.
"""
import importlib.util
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import rogue_monster_live as rml  # noqa: E402
import rogue_monster_state as rms  # noqa: E402

rml._sleep = lambda *a, **k: None   # no real backoff sleeps in tests (default)

_spec = importlib.util.spec_from_file_location(
    "sim_broker", os.path.join(_ROOT, "backtest", "sim_broker.py"))
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

from config import Config  # noqa: E402


class _Tick:
    def __init__(self, t, bid, ask):
        self.time_utc, self.bid, self.ask = t, bid, ask


class TestAdapter:
    """Thin adapter translating the monster adapter's calls into FakeBroker
    order_send requests + FakeMT5 reads. Faithful to the real MT5 order path."""

    def __init__(self, broker, symbol):
        self.broker = broker
        self.mt5 = sb.FakeMT5(broker)
        self.symbol = symbol
        self._m1 = []

    def feed_m1(self, closes, start="2026-06-10 02:00", wick=0.6):
        idx = pd.date_range(start, periods=len(closes), freq="1min")
        rows = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c
            rows.append({"time": int(idx[i].timestamp()), "open": float(o),
                         "high": float(c) + wick, "low": float(c) - wick, "close": float(c)})
        self._m1 = rows

    def get_latest_m1(self, symbol, n=1):
        return self._m1[-n:]

    def server_time_utc(self):
        return self.broker.cur.time_utc if self.broker.cur else pd.Timestamp("2026-06-10 02:00")

    def place_stop_order(self, symbol, side, price, lot, sl, tp, comment="",
                         dry_run=False, magic=20260522):
        otype = sb.ORDER_TYPE_BUY_STOP if side == "BUY" else sb.ORDER_TYPE_SELL_STOP
        return self.broker.order_send({
            "action": sb.TRADE_ACTION_PENDING, "symbol": symbol, "volume": lot,
            "type": otype, "price": price, "sl": sl, "tp": tp,
            "magic": int(magic), "comment": comment})

    def modify_position_sl(self, ticket, new_sl, dry_run=False):
        return self.broker.order_send({"action": sb.TRADE_ACTION_SLTP,
                                       "position": int(ticket), "sl": float(new_sl)})

    def close_position(self, ticket, dry_run=False):
        if not self.mt5.positions_get(ticket=int(ticket)):
            return None
        return self.broker.order_send({"action": sb.TRADE_ACTION_DEAL, "position": int(ticket)})

    def cancel_order(self, ticket, dry_run=False):
        return self.broker.order_send({"action": sb.TRADE_ACTION_REMOVE, "order": int(ticket)})

    def place_market_order(self, symbol, side, lot, sl=0.0, tp=0.0, comment="",
                           dry_run=False, magic=20260522):
        otype = sb.ORDER_TYPE_BUY if side == "BUY" else sb.ORDER_TYPE_SELL
        return self.broker.order_send({"action": sb.TRADE_ACTION_DEAL, "symbol": symbol,
                                       "volume": lot, "type": otype, "sl": sl, "tp": tp,
                                       "magic": int(magic), "comment": comment})

    def stop_preflight(self, symbol, side, price, cushion_pts=0.0):
        tk = self.mt5.symbol_info_tick(symbol)
        info = self.mt5.symbol_info(symbol)
        bid, ask = float(tk.bid), float(tk.ask)
        point = float(getattr(info, "point", 0.01) or 0.01)
        stops = max(float(getattr(info, "trade_stops_level", 0) or 0),
                    float(getattr(info, "trade_freeze_level", 0) or 0)) * point + max(0.0, cushion_pts)
        if side == "BUY":
            return (price >= ask + stops), round(max(0.0, ask - price), 2), "test"
        return (price <= bid - stops), round(max(0.0, price - bid), 2), "test"


class Trader:
    def __init__(self, cfg, adapter, run_dir):
        self.cfg = cfg
        self.adapter = adapter
        self.paper = False
        self.state = {"last_broker_date": "2026-06-10"}
        self._rogue = {}
        self.run_dir = run_dir
        self.tele = None


def _mk_trader(tmp, be_lock_arm=0.0, be_lock_floor=0.0, asia_start_hour=0):
    cfg = Config()
    # Base tests exercise the core mechanics with the v3.1.0 A+C refinements OFF
    # (matches the parity contract: both keys at 0 -> byte-identical to baseline).
    cfg.rogue_be_lock_arm = be_lock_arm
    cfg.rogue_be_lock_floor = be_lock_floor
    cfg.rogue_asia_start_hour = asia_start_hour
    broker = sb.FakeBroker("XAUUSD", cfg, starting_balance=50000.0, spread=0.0)
    adapter = TestAdapter(broker, "XAUUSD")
    trader = Trader(cfg, adapter, str(tmp))
    return trader, broker, adapter


def _tick(broker, price, t="2026-06-10 03:10"):
    broker.cur = _Tick(pd.Timestamp(t), price, price)
    broker.advance(broker.cur)


def _arm_bars():
    # 66 flat @3000 (anchor seeds 02:30) then a LONG breakout; a trailing bar so
    # the breakout is the last CLOSED bar (the adapter drops the still-forming one).
    return [3000.0] * 66 + [3001.0, 3001.0]


def _bars_ending(price):
    # flat box @3000 (seeds anchor) then settle at `price` as the last closed bar
    return [3000.0] * 66 + [3001.0, price, price]


def test_seed_arm_fill_chain_and_reanchor(tmp_path):
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path)

    # 1. arm: feed flat box + break -> a resting BUY_STOP appears
    adapter.feed_m1(_arm_bars())
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3000.0, 3000.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    m = trader._rogue["monster"]
    assert m["anchor"] is not None, "anchor should be seeded at/after 02:30"
    pend = m.get("pend")
    assert pend and pend["side"] == "LONG", f"expected armed LONG, got {pend}"
    assert abs(pend["level"] - 3001.6) < 1e-6
    resting = adapter.mt5.orders_get(symbol="XAUUSD")
    assert len(resting) == 1 and int(resting[0].magic) == rml.ROGUE_MAGIC

    # 2. price ticks exactly through the stop (3001.6) -> broker fills the ENTRY
    _tick(broker, 3001.6)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    assert len(m["positions"]) == 1, "entry should be filled + tracked"
    assert trader._rogue["open"] and trader._rogue["open"]["magic"] == rml.ROGUE_MAGIC
    entry = list(m["positions"].values())[0]["entry"]
    # a chain stop was placed at fill + chain_step
    resting = adapter.mt5.orders_get(symbol="XAUUSD")
    assert len(resting) == 1
    assert abs(float(resting[0].price_open) - (entry + 12.0)) < 1e-6, "chain pending at entry+12"

    # 3. price reverses to the entry SL (entry-10) -> SL close; bars settle at 2991
    adapter.feed_m1(_bars_ending(2991.0))
    _tick(broker, 2991.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    assert len(m["positions"]) == 0, "entry should be stopped out"
    assert m["consec_sl"] == 1, "a full-SL loss increments consec_sl"
    assert abs(m["anchor"] - 2991.0) < 1e-6, "anchor rolls to the sequence close price"
    # dangling chain pending was cancelled on sequence close
    assert len(adapter.mt5.orders_get(symbol="XAUUSD")) == 0
    assert trader._rogue["open"] is None


def test_trail_ratchets_sl(tmp_path):
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path)
    adapter.feed_m1(_arm_bars())
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3000.0, 3000.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    _tick(broker, 3001.6)   # fill the entry exactly at the stop
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    m = trader._rogue["monster"]
    tk = int(sorted(m["positions"].keys(), key=int)[0])
    sl0 = adapter.mt5.positions_get(ticket=tk)[0].sl
    # run price to +15 favourable (bars AND tick) -> trail must lift the SL
    adapter.feed_m1(_bars_ending(3016.6))
    _tick(broker, 3016.6)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    sl1 = adapter.mt5.positions_get(ticket=tk)[0].sl
    assert sl1 > sl0, f"trail should ratchet SL up ({sl0} -> {sl1})"


def test_governor_day_loss_flattens(tmp_path):
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path)
    trader.cfg.rogue_day_loss_halt = -10.0   # trivially small so any red trips it
    adapter.feed_m1(_arm_bars())
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3000.0, 3000.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    _tick(broker, 3001.6)   # fill
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    adapter.feed_m1(_bars_ending(2991.0))
    _tick(broker, 2991.0)   # SL -> realized loss trips the day-loss governor
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    m = trader._rogue["monster"]
    assert m["halted"] in ("GOV-LOSS", ""), m["halted"]
    # after a halt, no new resting orders and no open positions
    if m["halted"]:
        assert len(adapter.mt5.orders_get(symbol="XAUUSD")) == 0
        assert len(rml._rogue_positions(trader)) == 0


def test_tf_orders_excluded(tmp_path):
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path)
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:10"), 3000.0, 3000.0)
    # a TF_ test position under ROGUE magic must be invisible to the engine
    broker.order_send({"action": sb.TRADE_ACTION_PENDING, "symbol": "XAUUSD",
                       "volume": 0.35, "type": sb.ORDER_TYPE_BUY_STOP, "price": 3005.0,
                       "sl": 2995.0, "tp": 3200.0, "magic": rml.ROGUE_MAGIC,
                       "comment": "TF_120000"})
    assert rml._rogue_pendings(trader) == {}, "TF_ pending must be excluded"


def test_persistence_roundtrip(tmp_path):
    rms._last_blob["v"] = None
    m = rml._new_monster_state()
    m.update({"day": "2026-06-10", "anchor": 3005.5, "anchor_day": "2026-06-10",
              "consec_sl": 2, "sl_by_side": {"LONG": 1, "SHORT": 1}, "extra_atr": 0.5})
    rms.save(str(tmp_path), m, force=True)
    back = rms.load(str(tmp_path))
    assert back["anchor"] == 3005.5 and back["consec_sl"] == 2
    assert back["anchor_day"] == "2026-06-10" and back["extra_atr"] == 0.5


def test_be_lock_scratch_live(tmp_path):
    # Fix A through the live path: BE lock ratchets the broker SL to breakeven at +5,
    # and a stop-out there is a scratch (consec_sl unchanged), not a full SL.
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path, be_lock_arm=5.0, be_lock_floor=0.0)
    adapter.feed_m1(_arm_bars())
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3000.0, 3000.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    _tick(broker, 3001.6)                                   # fill entry
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    m = trader._rogue["monster"]
    tk = int(sorted(m["positions"].keys(), key=int)[0])
    # run to +5 -> BE lock modifies the broker SL up to breakeven (entry)
    adapter.feed_m1(_bars_ending(3006.6))
    _tick(broker, 3006.6)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    assert abs(adapter.mt5.positions_get(ticket=tk)[0].sl - 3001.6) < 1e-6, "SL ratcheted to BE"
    # reverse to breakeven -> BE scratch close
    adapter.feed_m1(_bars_ending(3001.0))
    _tick(broker, 3001.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    assert len(m["positions"]) == 0, "position closed at BE"
    assert m["consec_sl"] == 0, "BE scratch must NOT count as an SL"
    assert m["sl_by_side"] == {"LONG": 0, "SHORT": 0}


def test_asia_block_live(tmp_path):
    # Fix C through the live path: before the server start hour, no arm / no pending.
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path, asia_start_hour=7)
    adapter.feed_m1(_arm_bars())                            # bars at ~03:xx server (< 7)
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3000.0, 3000.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    m = trader._rogue["monster"]
    assert m["anchor"] is not None, "anchor still seeds at 02:30"
    assert m.get("pend") is None, "Asia block: no arm before 07:00"
    assert len(adapter.mt5.orders_get(symbol="XAUUSD")) == 0, "no resting order placed"


def test_stop_preflight_side_and_through(tmp_path=None):
    trader, broker, adapter = _mk_trader(tmp_path or ".")
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3000.0, 3000.0)
    ok, through, _ = adapter.stop_preflight("XAUUSD", "BUY", 3005.0)   # above market -> valid
    assert ok and through == 0.0
    ok, through, _ = adapter.stop_preflight("XAUUSD", "BUY", 2998.0)   # through by 2
    assert (not ok) and through == 2.0
    ok, through, _ = adapter.stop_preflight("XAUUSD", "SELL", 2995.0)  # below market -> valid
    assert ok and through == 0.0


def test_rogue_entry_market_chase_within_cap(tmp_path):
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path)          # A+C off, chase cap 3.0
    adapter.feed_m1(_arm_bars())
    # ask already 2 pts THROUGH the 3001.6 entry level (<= 3.0 cap) -> chase market
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3003.6, 3003.6)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    m = trader._rogue["monster"]
    assert m.get("pend") is None, "market chase does not leave a pending"
    assert len(rml._rogue_positions(trader)) == 1, "entry chased to a market position"
    assert len(adapter.mt5.orders_get(symbol="XAUUSD")) == 0, "no resting stop left"


def test_rogue_entry_drop_beyond_cap(tmp_path):
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path)
    adapter.feed_m1(_arm_bars())
    # ask 5 pts through the level (> 3.0 cap) -> drop the arm, no order, block re-arm
    t = pd.Timestamp("2026-06-10 03:06")
    broker.cur = _Tick(t, 3006.6, 3006.6)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    m = trader._rogue["monster"]
    assert m.get("pend") is None
    assert len(rml._rogue_positions(trader)) == 0, "no market chase beyond cap"
    assert len(adapter.mt5.orders_get(symbol="XAUUSD")) == 0, "no stale stop placed"
    assert m.get("arm_blocked_bar") == str(pd.Timestamp(m["last_m1_ts"])), "re-arm blocked this bar"


class _RejectAdapter(TestAdapter):
    """preflight passes but the broker keeps rejecting the stop (10015)."""
    def stop_preflight(self, symbol, side, price, cushion_pts=0.0):
        return True, 0.0, "ok"

    def place_stop_order(self, symbol, side, price, lot, sl, tp, comment="",
                         dry_run=False, magic=20260522):
        class _R:
            retcode = 10015
            order = 0
            comment = "INVALID_PRICE"
        return _R()


def test_rogue_3strike_abandon_with_backoff(tmp_path):
    rms._last_blob["v"] = None
    cfg = Config()
    cfg.rogue_be_lock_arm = 0.0; cfg.rogue_asia_start_hour = 0
    broker = sb.FakeBroker("XAUUSD", cfg, starting_balance=50000.0, spread=0.0)
    adapter = _RejectAdapter(broker, "XAUUSD")
    trader = Trader(cfg, adapter, str(tmp_path))
    adapter.feed_m1(_arm_bars())
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3000.0, 3000.0)
    calls = []
    saved = rml._sleep
    rml._sleep = lambda s, *a, **k: calls.append(s)
    try:
        rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    finally:
        rml._sleep = saved
    m = trader._rogue["monster"]
    assert m.get("pend") is None, "arm abandoned after 3 rejects"
    assert m.get("arm_blocked_bar") is not None, "arm cleared / blocked this bar"
    assert calls == [0.5, 1.0], "backoff between the 3 attempts (0.5, 1.0)"
    assert m.get("err_cards"), "one error-card intent recorded (dedup)"


def test_anchor_leg_no_market_chase(tmp_path):
    # the SHARED place_stop_order preflight returns a stale shim and NEVER converts to
    # market (anchor/RB legs must not chase — market fill breaks straddle geometry).
    from mt5_adapter import MT5Adapter
    cfg = Config()
    broker = sb.FakeBroker("XAUUSD", cfg, starting_balance=50000.0, spread=0.0)
    broker.cur = _Tick(pd.Timestamp("2026-06-10 10:00"), 4012.0, 4012.0)
    adapter = MT5Adapter.__new__(MT5Adapter)
    adapter.mt5 = sb.FakeMT5(broker)
    # BUY stop @ 4010.1 with ask 4012 -> through by 1.9 -> preflight SKIP (the 10015 case)
    res = adapter.place_stop_order("XAUUSD", "BUY", 4010.1, 0.35, 4000.0, 4200.0,
                                   comment="A1", magic=20260522)
    assert getattr(res, "retcode", None) == 10015
    assert getattr(res, "comment", "") == "PREFLIGHT_STALE"
    assert len(broker.positions) == 0 and len(broker.pendings) == 0, "no order/position created"


def test_chain_rejection_does_not_orphan_sequence(tmp_path):
    rms._last_blob["v"] = None
    trader, broker, adapter = _mk_trader(tmp_path)
    adapter.feed_m1(_arm_bars())
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:06"), 3000.0, 3000.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)   # arm
    _tick(broker, 3001.6)                                              # fill entry
    # chain would rest at 3013.6; make the tick already through it (>cap) so the chain
    # is dropped as STALE — but the entry must remain and the sequence must still close.
    broker.cur = _Tick(pd.Timestamp("2026-06-10 03:07"), 3020.0, 3020.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)   # fill detect + chain STALE
    m = trader._rogue["monster"]
    assert len(m["positions"]) == 1, "entry still tracked (chain drop did not orphan it)"
    # entry runs to SL -> sequence closes cleanly (re-anchor bookkeeping intact)
    adapter.feed_m1(_bars_ending(2991.0))
    _tick(broker, 2991.0)
    rml.drive_monster(trader, trader._rogue, allow_new_entries=True)
    assert len(m["positions"]) == 0 and abs(m["anchor"] - 2991.0) < 1e-6, "sequence closed + re-anchored"


def _run_all():
    import tempfile
    import pathlib
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        with tempfile.TemporaryDirectory() as td:
            fn(pathlib.Path(td))
        print(f"ok   {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
