"""broker preflight (`bot.py test`) — offline, MT5 mocked.

Covers: table assembly, verdict logic (BLOCKED on trade_expert False / AutoTrading
off / a rejected probe), filling-mode fallback reporting on 10030, lot-advisor math
vs a hand-computed fixture, and the no-order-send constraint (grep-proven the command
can never reach order_send / REMOVE / SLTP / any placement helper).

Runnable under pytest or standalone (`python tests/test_broker_preflight.py`).
"""
import dataclasses
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker_preflight as BP
from config import Config


# --- fake broker ------------------------------------------------------------------
class FakeCheck:
    def __init__(self, retcode, margin=None, margin_level=None):
        self.retcode, self.margin, self.margin_level = retcode, margin, margin_level
        self.margin_free = None
        self.comment = ""


class FakeBroker:
    """Read-only preflight broker. `check()` returns 0 (accepted) unless `fill_10030`
    is set, in which case IOC returns 10030 and FOK returns 0 (fallback path)."""

    def __init__(self, *, trade_expert=True, autotrading=True, trade_allowed=True,
                 is_demo=True, fill_10030=False, filling_mask=BP.FILL_FOK_BIT | BP.FILL_IOC_BIT,
                 margin=4000.0 * 0.35, equity=50000.0, reject_market=False):
        self.trade_expert, self.autotrading, self.trade_allowed = trade_expert, autotrading, trade_allowed
        self.is_demo, self.fill_10030, self.filling_mask = is_demo, fill_10030, filling_mask
        self.margin, self.equity, self.reject_market = margin, equity, reject_market
        self._ticks = [BP.Tick(bid=3999.90, ask=4000.10, time_s=1000.0, server_now_s=1000.2)]

    def order_type_consts(self):
        return {"BUY": 0, "SELL": 1, "BUY_STOP": 4, "SELL_STOP": 5}

    def action_consts(self):
        return {"DEAL": 1, "PENDING": 5}

    def filling_consts(self):
        return {"FOK": 0, "IOC": 1, "RETURN": 2}

    def snapshot_account(self):
        return BP.AccountSnap(login=123, server="Demo-1", is_demo=self.is_demo,
                              balance=self.equity, equity=self.equity, leverage=100,
                              margin_mode=2, margin_mode_str="RETAIL_HEDGING",
                              currency="USD", margin_free=self.equity,
                              trade_allowed=self.trade_allowed, trade_expert=self.trade_expert)

    def snapshot_terminal(self):
        return BP.TerminalSnap(trade_allowed=self.autotrading, connected=True)

    def snapshot_symbol(self):
        return BP.SymbolSnap(name="XAUUSD", digits=2, point=0.01, tick_size=0.01,
                             tick_value=1.0, contract_size=100.0, volume_min=0.01,
                             volume_step=0.01, volume_max=100.0, stops_level=50,
                             freeze_level=0, filling_mask=self.filling_mask,
                             trade_mode=4, trade_mode_str="FULL",
                             swap_long=-3.0, swap_short=-1.0)

    def sample_tick(self):
        return self._ticks[0]

    def offset_hours(self):
        return 3.0

    def check(self, request):
        otype = request["type"]
        is_market = request["action"] == 1
        if self.reject_market and is_market:
            return FakeCheck(10019)  # NO_MONEY
        if self.fill_10030 and is_market:
            if request.get("type_filling") == 1:      # IOC -> reject
                return FakeCheck(10030)
            return FakeCheck(0, margin=self.margin, margin_level=1000.0)  # FOK/RETURN ok
        return FakeCheck(0, margin=self.margin, margin_level=1000.0)


# --- clock/sleeper that advance together (instant sampling) -----------------------
def _clock_sleeper():
    t = [0.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + max(s, 0.001)))


def _run(broker, **kw):
    clk, slp = _clock_sleeper()
    return BP.run_preflight(Config(), broker=broker, clock=clk, sleeper=slp,
                            sample_seconds=kw.pop("sample_seconds", 0.5), poll_s=0.1, **kw)


# --- verdict logic ----------------------------------------------------------------
def test_ready_when_all_ok():
    assert _run(FakeBroker()) == 0


def test_blocked_on_trade_expert_false():
    code = _run(FakeBroker(trade_expert=False))
    assert code == 2
    v = BP.decide_verdict(FakeBroker(trade_expert=False).snapshot_account(),
                          BP.TerminalSnap(), BP.SymbolSnap(), [])
    assert not v.ready and any("EA trading DISABLED" in it for it in v.blocked_items)


def test_blocked_on_autotrading_off():
    assert _run(FakeBroker(autotrading=False)) == 2
    v = BP.decide_verdict(BP.AccountSnap(), BP.TerminalSnap(trade_allowed=False),
                          BP.SymbolSnap(), [])
    assert not v.ready and any("AutoTrading is OFF" in it for it in v.blocked_items)


def test_blocked_on_rejected_market_probe():
    assert _run(FakeBroker(reject_market=True)) == 2


def test_runs_on_real_account():
    # preflight is READ-ONLY -> runs on BOTH demo and real (never refuses on account type)
    assert _run(FakeBroker(is_demo=False)) == 0


# --- filling-mode fallback --------------------------------------------------------
def test_filling_fallback_reported_on_10030():
    b = FakeBroker(fill_10030=True)
    sym = b.snapshot_symbol()
    rows = BP.build_viability(b, sym, b.sample_tick(), Config())
    mkt = [r for r in rows if r.type_str in ("BUY", "SELL")]
    assert mkt and all(r.accepted for r in mkt)
    assert all(r.filling_used == "FOK" for r in mkt)
    assert all("use FOK" in r.filling_note for r in mkt)
    assert BP.recommended_filling(rows) == "FOK"


def test_supported_fillings_enumerates_ioc_first():
    fc = {"FOK": 0, "IOC": 1, "RETURN": 2}
    got = BP.supported_fillings(BP.FILL_FOK_BIT | BP.FILL_IOC_BIT, fc)
    assert [n for n, _ in got] == ["IOC", "FOK", "RETURN"]
    only_fok = BP.supported_fillings(BP.FILL_FOK_BIT, fc)
    assert [n for n, _ in only_fok] == ["FOK", "RETURN"]


# --- table assembly ---------------------------------------------------------------
def test_tables_assemble_without_error():
    b = FakeBroker()
    a, t, s = b.snapshot_account(), b.snapshot_terminal(), b.snapshot_symbol()
    rows = BP.build_viability(b, s, b.sample_tick(), Config())
    ss = BP.SpreadSample(n=3, current_pts=20.0, avg_pts=18.0, max_pts=25.0,
                         current_usd=7.0, avg_usd=6.3, max_usd=8.75)
    adv = BP.build_lot_advisor(Config(), s, 50000.0, 4000.0, 2500.0)
    v = BP.decide_verdict(a, t, s, rows)
    report = BP.render_report(a, t, s, ss, rows, BP.TimingInfo(offset_hours=3.0), adv, v,
                              Config(), None)
    for token in ("ACCOUNT", "TERMINAL", "SYMBOL XAUUSD", "ORDER VIABILITY",
                  "TIMING", "LOT ADVISOR", "VERDICT", "RECOMMENDED",
                  "slippage is NOT measurable"):
        assert token in report, token
    assert "anchor BUY stop" in report and "anchor SELL stop" in report
    assert "RB pending" in report and "SLTP shape" in report


def test_lot_override_warning_shown():
    s = FakeBroker().snapshot_symbol()
    adv = BP.build_lot_advisor(Config(), s, 50000.0, 4000.0, 2500.0)
    out = BP.render_lot_advisor(adv, Config(), lot_override=0.20)
    assert "--lot override ACTIVE: 0.20" in out


# --- lot-advisor math vs a hand-computed fixture ----------------------------------
def test_lot_advisor_row_matches_hand_fixture():
    cfg = Config()   # sl_dist 18, contract 100, rb_trail 10, max_boosts 2, rb_lot 0.45
    sym = FakeBroker().snapshot_symbol()  # tick_value 1.0 / tick_size 0.01 -> dpp 100
    r = BP.lot_advisor_row(0.35, cfg=cfg, sym=sym, equity=50000.0,
                           margin_per_lot=4000.0, daily_limit=2500.0)
    assert r.rescue_lot == 0.45                       # round_to_step(0.35*1.29=0.4515) -> 0.45
    assert r.anchor_leg_sl == 630.0                   # 18 * 0.35 * 100
    assert r.worst_realized_day == 630.0              # one full SL then halt
    assert r.worst_floating == 1530.0                 # 100*(0.35*18 + 0.45*10*2)
    assert r.stack_lots == 1.25                       # 0.35 + 2*0.45
    assert r.stack_margin == 5000.0                   # 4000 * 1.25
    assert abs(r.stack_margin_level - 1000.0) < 1e-6  # 50000/5000*100
    assert abs(r.leg_pct - 25.2) < 1e-6               # 630/2500*100
    assert abs(r.wf_pct - 61.2) < 1e-6                # 1530/2500*100
    assert r.verdict == "✅"


def test_lot_advisor_verdicts_and_recommendation():
    cfg, sym = Config(), FakeBroker().snapshot_symbol()
    rows = BP.build_lot_advisor(cfg, sym, 50000.0, 4000.0, 2500.0)
    assert all(r.verdict == "✅" for r in rows)        # every size clears at 5% limit
    assert BP.recommended_lot(rows).lot == 0.35        # largest green

    # tighter 4% limit pushes 0.35 to ⚠️ (leg 31.5% > 30, wf 76.5% > 70, still within amber)
    tight = BP.lot_advisor_row(0.35, cfg=cfg, sym=sym, equity=50000.0,
                               margin_per_lot=4000.0, daily_limit=2000.0)
    assert tight.verdict == "⚠️"

    # a starved account (margin level <= 200%) is ❌ regardless of leg/wf
    starved = BP.lot_advisor_row(0.35, cfg=cfg, sym=sym, equity=1000.0,
                                 margin_per_lot=4000.0, daily_limit=2500.0)
    assert starved.stack_margin_level < BP.MARGIN_LEVEL_FLOOR and starved.verdict == "❌"


def test_recommended_none_when_no_green():
    cfg, sym = Config(), FakeBroker().snapshot_symbol()
    rows = BP.build_lot_advisor(cfg, sym, 50000.0, 4000.0, daily_limit=300.0)  # brutal limit
    assert BP.recommended_lot(rows) is None
    assert "RECOMMENDED: none" in BP.render_lot_advisor(rows, cfg, None)


# --- the no-order-send constraint (grep-proven) -----------------------------------
def test_no_order_send_reachable():
    """Command 1 must be provably READ-ONLY. AST-walk the module: NO call targets a
    write method (order_send / any placement/cancel/close/SL-modify helper) and NO
    write action constant (REMOVE/SLTP/MODIFY) is referenced anywhere in the CODE
    (docstring prose that merely names them for humans is ignored). The only trade-
    server call reachable is order_check()."""
    import ast
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "broker_preflight.py")
    tree = ast.parse(open(path).read())
    forbidden_calls = {"order_send", "place_market_order", "place_stop_order",
                       "cancel_order", "close_position", "modify_position_sl"}
    forbidden_names = {"TRADE_ACTION_REMOVE", "TRADE_ACTION_SLTP", "TRADE_ACTION_MODIFY"}
    called_attrs, referenced_names, saw_order_check = set(), set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called_attrs.add(node.func.attr)
            if node.func.attr == "order_check":
                saw_order_check = True
        if isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)
        if isinstance(node, ast.Name):
            referenced_names.add(node.id)
    assert not (called_attrs & forbidden_calls), \
        f"reachable write call(s): {called_attrs & forbidden_calls}"
    assert not (referenced_names & forbidden_names), \
        f"write action constant(s) referenced: {referenced_names & forbidden_names}"
    assert saw_order_check, "preflight must call order_check()"


def test_check_ok_predicate():
    assert BP.check_ok(0) and BP.check_ok(10009)
    assert not BP.check_ok(10030) and not BP.check_ok(None) and not BP.check_ok(10019)


# --- standalone runner ------------------------------------------------------------
def _run_all():
    import inspect
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for name, fn in tests:
        try:
            fn(*([__import__("pathlib").Path("/tmp")] if "tmp_path" in inspect.signature(fn).parameters else []))
            print(f"PASS  {name}")
        except Exception as e:
            fails += 1
            import traceback
            print(f"FAIL  {name}: {e!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
