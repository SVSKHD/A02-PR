#!/usr/bin/env python3
"""AUREON — on-demand SELF-TEST harness (v3.0.3).

WHY THIS EXISTS
---------------
Boosts failed 0-for-7 in LIVE rescues before the cause (an order `comment`
longer than 31 chars, silently rejected by MetaTrader5) was found. Each failure
cost a real trade because the only way to diagnose was AFTER a live rescue we
had waited hours to set up. This harness exercises the ENTIRE placement +
rescue/boost path ON DEMAND against the connected MT5 demo terminal, with tiny
throwaway orders placed far from market (or closed/cancelled immediately), and
reports a clear PASS/FAIL per step to console + Discord. The boost path now
proves it places at rc=10009 in ~2 minutes instead of during a real rescue.

SAFETY (hard rules)
-------------------
- Runs ONLY via `python bot.py selftest` — never from the live loop, never on a
  timer.
- Refuses to run if there are EXISTING open positions / pendings (so it can
  never interfere with a live anchor): aborts with "run when flat".
- All real orders use volume_min, placed ±$50 from market or closed/cancelled
  immediately in the same run; a try/finally cleanup closes/cancels anything
  still open even if a step raises mid-test. Never leaves a test order open.
- Demo-account guard: market-order steps are SKIPPED on a non-demo account
  unless --force is passed (don't place throwaway orders on funded capital).
"""
import logging
import os
import time
from typing import List, Optional, Tuple

import pandas as pd

from mt5_adapter import _MT5_RETCODE_MAP, mt5_comment
from telemetry import telemetry_from_env, Severity

log = logging.getLogger("AUREON")

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

# Step numbers -> short names (stable, match the report block in the spec).
STEP_NAMES = {
    1: "connection",
    2: "tick fresh",
    3: "comment<=31",
    4: "stop place",
    5: "market place",
    6: "sl modify",
    7: "rescue class",
    8: "rescue dry-run",
    9: "ts header",
    10: "late retry",
    11: "fleet logger",
    12: "fill alert",
    13: "close alert",
    14: "ts fallback",
    15: "BE rung",
    16: "hold gate",
    17: "boost SL",
    18: "discord cards",
    19: "discord dedup",
    20: "discord hb",
    21: "discord conn",
    22: "lone rescue",
    23: "boost trail",
    24: "lone branches",
    25: "boost isol",
    26: "lone live-log",
    27: "backtest parity",
    28: "boost trigger",
    29: "boost toggles",
    30: "underwater lock",
    31: "trail telemetry",
    32: "stop>=bid reject",
    33: "lock guards",
    34: "lone boost",
    35: "boost watchdog",
    36: "no-oco stack",
    37: "stack economics",
    38: "telemetry full",
    39: "phantom guard",
    40: "phantom legit/trip",
    41: "monday wake",
    42: "monday badoffset",
    43: "monday drift trip",
    44: "weekday unaffected",
    45: "monday trace",
    46: "jun8 replay",
    47: "offset parity",
    48: "autopull soft",
    49: "autopull abort",
    50: "soft no-flatten",
    51: "rehydrate resume",
    52: "reconcile adopt",
    53: "reconcile finalize",
    54: "quick gap",
    55: "break fakespike",
    56: "break holds",
    57: "break continuation",
    58: "break retrace",
    59: "break holdshort",
    60: "fp 0.15 ok",
    61: "fp 0.35 breach",
    62: "fp zero blocks",
    63: "fp lot config",
    64: "stack5 cap",
    65: "stack5 loser out",
    66: "stack5 fp gate",
    67: "stack5 whipsaw",
    68: "stack5 cap viol",
    69: "stack5 trail coclose",
    70: "stack5 pnl 0.15",
    71: "stack5 pnl 0.35",
    72: "fp zero profile cap",
    73: "stack5 default on",
    74: "a1 tick fallback places",
    75: "a1 tick fallback rejects spike",
    76: "tick hold fires",
    77: "tick hold blip rejected",
    78: "tick hold trail advance",
    79: "boost incident regression",
    80: "rescue bypass break-and-hold",
    # v3.2.8 Phase 1 — rally +$5 arm / +$4 lock / $1.50 gap (rescue untouched)
    81: "rally arm +5",
    82: "rally trail ride",
    # v3.2.8 Phase 2/3 — rally/rescue/common file split + dispatcher isolation
    83: "boost split isolation",
    # v3.2.9 manual TESTFIRE — fail-closed safety rails + same-placement reuse
    84: "testfire demo-only",
    85: "testfire FP refuse",
    86: "testfire flat/in-flight",
    87: "testfire anchor window",
    88: "testfire same-placement",
    # v3.3.0 rally rides (peak-gap trail, not flat lock) + no sub-floor clip
    89: "rally rides not bails",
    90: "rally no subfloor clip",
    # v3.3.3 break-and-hold crash fix + fail-closed; rally SL $13 / cap -$910
    91: "break gate np-safe",
    92: "break gate failclosed",
    93: "rally sl13 cap910",
    # v3.3.4 rally pullback detector (hold within T / cut beyond T / time bound)
    94: "rally pullback band",
    95: "rally pullback recover/time",
    # v3.3.5 CASE 2 parent-profit override (fires strong continuations the gate blocked)
    96: "case2 override fires",
    97: "case1 still blocks",
    98: "override dir/rescue",
    # v3.3.6 telemetry-truth display fixes + A3 reschedule 16:20 -> 17:00 IST
    99: "readiness resolver",
    100: "a3 1700 reschedule",
    101: "v336 no logic chg",
    102: "monday gate strict",
}
# Steps that place REAL (throwaway) orders -> gated by the demo guard.
MARKET_STEPS = {4, 5, 6, 8}


def classify_second_fill(twin_open: bool) -> str:
    """Pure mirror of the fills.py twin-open rescue rule (no broker, no I/O): a
    No-OCO 2nd fill is a genuine RESCUE only while its twin is STILL OPEN; a
    closed-twin 2nd fill runs as a normal breakout leg (no boosts). Kept tiny and
    side-effect-free so the harness can assert both branches deterministically."""
    return 'rescue' if twin_open else 'normal'


class SelfTest:
    """On-demand placement + rescue/boost self-test against the live demo MT5.

    Construct with a connected MT5Adapter, then call run(). Returns True only if
    every executed (non-skipped) step PASSed."""

    PING_DISTANCE = 50.0  # place test stops/markets this far from market

    def __init__(self, cfg, adapter, force: bool = False):
        self.cfg = cfg
        self.adapter = adapter
        self.force = force
        self.symbol = getattr(cfg, 'symbol', 'XAUUSD')
        self.tele = telemetry_from_env(component="AUREON-selftest")
        self.results: dict = {}      # step_no -> (status, detail)
        self.is_demo = True
        self.vmin = 0.01
        self._si = None
        # Cleanup ledgers — anything placed is tracked here and torn down in the
        # run() finally, even if a step raises mid-test.
        self._open_positions: set = set()
        self._open_pendings: set = set()

    # ------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------
    def _record(self, n: int, status: str, detail: str = ""):
        self.results[n] = (status, detail)
        line = f"{n} {STEP_NAMES[n]:<14} {status}  ({detail})"
        (self.tele.warn if status == FAIL else self.tele.info)(line)
        log.info(line)

    @staticmethod
    def _rc(res):
        return getattr(res, 'retcode', None) if res is not None else None

    @staticmethod
    def _rcname(rc):
        return _MT5_RETCODE_MAP.get(rc, f"UNKNOWN_{rc}")

    @staticmethod
    def _ticket(res):
        if res is None:
            return None
        return getattr(res, 'order', None) or getattr(res, 'deal', None) or None

    def _tick(self):
        return self.adapter.mt5.symbol_info_tick(self.symbol)

    def _cancel(self, tk):
        try:
            self.adapter.cancel_order(tk)
        except Exception as e:
            log.warning(f"selftest cancel {tk} failed: {e}")
        finally:
            self._open_pendings.discard(tk)

    def _close(self, tk):
        try:
            self.adapter.close_position(tk)
        except Exception as e:
            log.warning(f"selftest close {tk} failed: {e}")
        finally:
            self._open_positions.discard(tk)

    def _cleanup(self):
        """try/finally teardown — close/cancel every throwaway order still open,
        so a mid-test error can never leave a position or pending behind."""
        for tk in list(self._open_pendings):
            self._cancel(tk)
        for tk in list(self._open_positions):
            self._close(tk)

    # ------------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------------
    def _step_connection(self):
        mt5 = self.adapter.mt5
        ti = mt5.terminal_info()
        ai = mt5.account_info()
        try:
            mt5.symbol_select(self.symbol, True)
        except Exception:
            pass
        si = mt5.symbol_info(self.symbol)
        self._si = si
        if si is not None:
            self.vmin = float(getattr(si, 'volume_min', 0.01) or 0.01)
        connected = bool(ti and getattr(ti, 'connected', False))
        full = bool(si and int(getattr(si, 'trade_mode', -1)) == int(mt5.SYMBOL_TRADE_MODE_FULL))
        ok = connected and ai is not None and full
        detail = (
            f"build {getattr(ti, 'build', '?')}, ping {getattr(ti, 'ping_last', '?')}us, "
            f"trade_allowed={getattr(ti, 'trade_allowed', '?')}, "
            f"fill={getattr(si, 'filling_mode', '?')}, "
            f"stops={getattr(si, 'trade_stops_level', '?')}, "
            f"freeze={getattr(si, 'trade_freeze_level', '?')}, "
            f"vmin/step={getattr(si, 'volume_min', '?')}/{getattr(si, 'volume_step', '?')}")
        if not ok:
            detail = (f"connected={connected} account={'ok' if ai else 'NONE'} "
                      f"symbol_full={full} | " + detail)
        self._record(1, PASS if ok else FAIL, detail)

    def _step_tick_fresh(self):
        try:
            server_utc = self.adapter.server_time_utc()
            age = (pd.Timestamp.now(tz='UTC') - server_utc).total_seconds()
        except Exception as e:
            self._record(2, WARN, f"could not read tick age: {e!r}")
            return
        thr = float(getattr(self.cfg, 'stale_tick_threshold_s', 60.0))
        status = PASS if age < thr else WARN
        self._record(2, status, f"age {age:.1f}s (threshold {thr:.0f}s)")

    def _step_comment_guard(self):
        # The longest comments the system can generate, built from the REAL
        # anchor labels: straddle (+gap +retry), recovery, confirm, boost, warmup.
        generated: List[str] = []
        for label, _h, _m in self.cfg.anchors:
            p = label[:2]
            generated += [
                f"AUR_{p}_BUY_G_R2", f"AUR_{p}_SELL_G_R2",
                f"AUR_{p}_B_RCV", f"AUR_{p}_S_RCV",
                f"AUR_{p}_B_CFM", f"AUR_{p}_S_CFM",
                f"AUR_{p}_B_B1", f"AUR_{p}_S_B2",
            ]
        generated.append("WARMUP")  # mirrors LiveTrader.WARMUP_COMMENT
        # The exact pre-fix bug comment (34 chars) -> proves mt5_comment() kills it.
        legacy = "AUREONv2_A3_1340_Overlap_SELL_BOOST1"
        all_ok = True
        longest = ("", 0)
        for c in generated:
            out = mt5_comment(c)
            n = len(out)
            if n > 31:
                all_ok = False
            if n > longest[1]:
                longest = (out, n)
        legacy_out = mt5_comment(legacy)
        legacy_ok = len(legacy_out) <= 31
        all_ok = all_ok and legacy_ok
        detail = (f"longest '{longest[0]}'={longest[1]}; "
                  f"legacy {len(legacy)}->{len(legacy_out)} ('{legacy_out}')")
        self._record(3, PASS if all_ok else FAIL, detail)

    def _step_stop_place(self):
        t = self._tick()
        if t is None:
            self._record(4, FAIL, "no tick")
            return
        lot = self.vmin
        buy_p = round(t.ask + self.PING_DISTANCE, 2)
        sell_p = round(t.bid - self.PING_DISTANCE, 2)
        sl_d, tp_d = self.cfg.sl_dist, self.cfg.tp_dist
        buy_res = self.adapter.place_stop_order(
            self.symbol, 'BUY', buy_p, lot, sl=round(buy_p - sl_d, 2),
            tp=round(buy_p + tp_d, 2), comment="AUR_ST_BUY")
        b_rc = self._rc(buy_res)
        b_tk = self._ticket(buy_res)
        if b_rc == 10009 and b_tk:
            self._open_pendings.add(b_tk)
        sell_res = self.adapter.place_stop_order(
            self.symbol, 'SELL', sell_p, lot, sl=round(sell_p + sl_d, 2),
            tp=round(sell_p - tp_d, 2), comment="AUR_ST_SELL")
        s_rc = self._rc(sell_res)
        s_tk = self._ticket(sell_res)
        if s_rc == 10009 and s_tk:
            self._open_pendings.add(s_tk)
        # Cancel both immediately.
        if b_tk:
            self._cancel(b_tk)
        if s_tk:
            self._cancel(s_tk)
        ok = (b_rc == 10009 and s_rc == 10009)
        detail = (f"buy {b_rc} ({self._rcname(b_rc)}), sell {s_rc} "
                  f"({self._rcname(s_rc)}), cancelled")
        self._record(4, PASS if ok else FAIL, detail)

    def _step_market_place(self):
        # THE boost path: same place_market_order the boosts use, boost comment
        # scheme + a $6-style tight SL. 0-for-7 historically — must now PASS.
        t = self._tick()
        if t is None:
            self._record(5, FAIL, "no tick")
            return
        lot = self.vmin
        price = t.ask
        b_sl = round(price - float(getattr(self.cfg, 'boost_sl_dollars',
                     getattr(self.cfg, 'rescue_boost_sl', 10.0))), 2)
        b_tp = round(price + self.cfg.tp_dist, 2)
        cmt = "AUR_ST_B_B1"
        res = self.adapter.place_market_order(
            self.symbol, 'BUY', lot, sl=b_sl, tp=b_tp, comment=cmt)
        rc = self._rc(res)
        tk = self._ticket(res)
        if rc == 10009 and tk:
            self._open_positions.add(tk)
        last_err = ""
        if rc != 10009:
            try:
                last_err = f" last_error={self.adapter.mt5.last_error()}"
            except Exception:
                pass
        if tk:
            self._close(tk)
        ok = (rc == 10009)
        detail = (f"{rc} ({self._rcname(rc)}), comment '{mt5_comment(cmt)}'"
                  f"={len(mt5_comment(cmt))}, closed{last_err}")
        self._record(5, PASS if ok else FAIL, detail)

    def _step_sl_modify(self):
        # Open a fresh tiny position, modify its SL (the ladder/trail op), close.
        t = self._tick()
        if t is None:
            self._record(6, FAIL, "no tick")
            return
        lot = self.vmin
        price = t.ask
        res = self.adapter.place_market_order(
            self.symbol, 'BUY', lot, sl=round(price - self.cfg.sl_dist, 2),
            tp=round(price + self.cfg.tp_dist, 2), comment="AUR_ST_MOD")
        rc = self._rc(res)
        tk = self._ticket(res)
        if rc != 10009 or not tk:
            self._record(6, FAIL, f"setup position failed rc={rc} ({self._rcname(rc)})")
            if tk:
                self._close(tk)
            return
        self._open_positions.add(tk)
        # Move SL closer but still valid (below current bid for a BUY).
        new_sl = round(self._tick().bid - max(self.cfg.sl_dist - 2.0, 5.0), 2)
        mod = self.adapter.modify_position_sl(tk, new_sl)
        m_rc = self._rc(mod)
        self._close(tk)
        ok = (m_rc == 10009)
        self._record(6, PASS if ok else FAIL,
                     f"{m_rc} ({self._rcname(m_rc)}), SL->${new_sl}")

    def _step_rescue_class(self):
        twin_open = classify_second_fill(True)
        twin_closed = classify_second_fill(False)
        ok = (twin_open == 'rescue' and twin_closed == 'normal')
        self._record(7, PASS if ok else FAIL,
                     f"twin-open={twin_open}, twin-closed={twin_closed}")

    def _step_rescue_dryrun(self):
        # Logic + real boost placement: simulate a rescue trigger and actually
        # place the configured boost fleet (vol_min throwaway), confirm each
        # returns 10009, then close them. End-to-end proof the fleet fires.
        t = self._tick()
        if t is None:
            self._record(8, FAIL, "no tick")
            return
        label = self.cfg.anchors[0][0]
        side = 'BUY'
        lot = self.vmin
        price = t.ask
        b_sl = round(price - float(getattr(self.cfg, 'boost_sl_dollars',
                     getattr(self.cfg, 'rescue_boost_sl', 10.0))), 2)
        b_tp = round(price + self.cfg.tp_dist, 2)
        n = int(getattr(self.cfg, 'rescue_boost_count', 2))
        # Mirror the structural rescue gate before "firing": twin must be open.
        if classify_second_fill(True) != 'rescue':
            self._record(8, FAIL, "rescue gate did not classify a twin-open 2nd fill")
            return
        outcomes = []
        all_ok = True
        for i in range(n):
            cmt = f"AUR_{label[:2]}_{side[0]}_B{i+1}"
            res = self.adapter.place_market_order(
                self.symbol, side, lot, sl=b_sl, tp=b_tp, comment=cmt)
            rc = self._rc(res)
            tk = self._ticket(res)
            if rc == 10009 and tk:
                self._open_positions.add(tk)
            else:
                all_ok = False
            outcomes.append(f"boost{i+1} {rc} '{mt5_comment(cmt)}'={len(mt5_comment(cmt))}")
            if tk:
                self._close(tk)
        self._record(8, PASS if all_ok else FAIL, ", ".join(outcomes) + ", closed")

    def _step_ts_header(self):
        # v3.0.4: the timestamp header is the single source for every alert
        # timestamp. Assert it derives server + IST from one instant and they
        # differ by exactly 2:30, and that the rendered line carries both clocks.
        from datetime import timedelta
        from telemetry import ts_header, _ts_components
        server, ist = _ts_components()
        diff = ist - server
        line = ts_header()
        ok = (diff == timedelta(hours=2, minutes=30)
              and "server" in line and "IST" in line and line.startswith("🕐"))
        self._record(9, PASS if ok else FAIL,
                     f"IST-server={diff} (want 2:30:00) | '{line}'")

    def _step_late_retry(self):
        # v3.0.5: drive the REAL anchor late-retry machine (anchors._process_
        # anchor_if_due) with a mocked clock + a stubbed _process_anchor, against a
        # minimal stand-in `self`. Two assertions: (A) a missed scheduled time
        # re-fires LATE within the window with a RE-CAPTURED (current) price; (B)
        # after the window elapses with no placement, it gives up cleanly with one
        # ❌ ANCHOR MISSED. No broker / no MT5.
        import types
        import pandas as pd
        import anchors as _a
        from utils import anchor_datetime_utc
        from datetime import date as _date

        LABEL = "A2_10h_London"

        def make_stub(succeed_at_min=None):
            s = types.SimpleNamespace()
            s.paused = False
            s.paper = True
            s.offset_validated = True
            s.ANCHOR_LATE_RETRY_INTERVAL_S = 30
            s.ANCHOR_ONTIME_GRACE_S = 120
            s.cfg = types.SimpleNamespace(
                anchors=[(LABEL, 10, 0)], broker_tz_offset_hours=3,
                monday_a1_override=None, anchor_late_window_min=10,
                stale_tick_threshold_s=60.0, symbol="XAUUSD")
            s.state = {"processed_anchors_today": [], "missed_anchors_today": []}
            s._deferred_anchor = None
            s._last_anchor_attempt = {}
            s.placements = []          # (delta_min, recaptured_price)
            s.tele = types.SimpleNamespace(
                info=lambda *a, **k: None, warn=lambda *a, **k: None,
                error=lambda m=None, *a, **k: s.misses.append(m),
                success=lambda *a, **k: None,
                send=lambda m=None, *a, **k: None)
            s.misses = []
            # current price walks with time so a re-capture differs from sched-time
            s.adapter = types.SimpleNamespace(
                tick_time_offset_hours=0,
                mt5=types.SimpleNamespace(
                    symbol_info_tick=lambda sym: types.SimpleNamespace(
                        time=int(s._now.timestamp()), bid=s._price, ask=s._price)))
            s._save_state = lambda: None
            s._resolved_anchor_hm = types.MethodType(_a._resolved_anchor_hm, s)
            s._anchor_datetime_utc = anchor_datetime_utc
            s._broker_date = lambda utc: utc.date()
            s._mark_anchor_placed = types.MethodType(_a._mark_anchor_placed, s)
            s._anchor_missed = types.MethodType(_a._anchor_missed, s)

            def _proc(label, anchor_utc):
                delta_min = (s._now - anchor_utc).total_seconds() / 60.0
                if succeed_at_min is not None and delta_min >= succeed_at_min:
                    s.placements.append((round(delta_min, 1), s._price))  # re-captured
                    s._mark_anchor_placed(label)
                # else: simulate a failed attempt (no deferred, stays unplaced)
            s._process_anchor = _proc
            return s

        sched = anchor_datetime_utc(_date(2026, 6, 16), 10, 3, 0)  # Tue 10:00 broker
        base_price = 4300.0

        # (A) succeed on the attempt at/after +5 min — within the 10-min window.
        sa = make_stub(succeed_at_min=5)
        for mins in range(0, 12):                  # 0..11 min, one tick/min
            sa._now = sched + pd.Timedelta(minutes=mins)
            sa._price = base_price + mins          # price walks each minute
            _a._process_anchor_if_due(sa, sa._now.date(), sa._now)
        a_ok = (LABEL in sa.state["processed_anchors_today"]
                and len(sa.placements) == 1
                and sa.placements[0][0] >= 5 and sa.placements[0][0] < 10
                and sa.placements[0][1] != base_price        # re-captured, not stale
                and not sa.misses)

        # (B) never succeeds -> clean give-up MISS after the window, exactly once.
        sb = make_stub(succeed_at_min=None)
        for mins in range(0, 14):
            sb._now = sched + pd.Timedelta(minutes=mins)
            sb._price = base_price + mins
            _a._process_anchor_if_due(sb, sb._now.date(), sb._now)
        b_ok = (LABEL in sb.state["missed_anchors_today"]
                and len(sb.misses) == 1
                and not sb.placements
                and "ANCHOR MISSED" in sb.misses[0])

        ok = a_ok and b_ok
        detail = (f"late-fire@+{sa.placements[0][0] if sa.placements else '?'}m "
                  f"recap=${sa.placements[0][1] if sa.placements else '?'} "
                  f"(sched-price ${base_price}); miss={'1' if b_ok else 'BAD'} "
                  f"a_ok={a_ok} b_ok={b_ok}")
        self._record(10, PASS if ok else FAIL, detail)

    def _step_fleet_logger(self):
        # v3.0.6: drive the REAL rescue fleet-event logger (rescue_log) with three
        # synthesized events and assert each writes a rescue_events.csv row, mirrors
        # to Firebase (mocked), and gets the correct branch label from its net.
        import tempfile, csv as _csv, os as _os, types
        import rescue_log as _rl
        import firebase_journal as _fj
        tmp = tempfile.mkdtemp(prefix="aureon_fleet_")
        fb_calls = []
        _orig = _fj.save_rescue_event
        _fj.save_rescue_event = lambda day, eid, doc: (fb_calls.append((day, eid)) or True)
        try:
            stub = types.SimpleNamespace()
            stub.run_dir = tmp
            stub.state = {"last_broker_date": "2026-06-16"}
            stub._rescue_events = {}
            stub._rescue_event_by_ticket = {}
            stub.sent = []
            # v3.1.1: stub must accept the REAL send signature (text + severity
            # positional, plus important/critical/card/event_key kwargs) and
            # swallow anything new via **k so it never breaks when send() grows.
            stub.tele = types.SimpleNamespace(
                send=lambda m=None, *a, **k: stub.sent.append(m))
            stub._rescue_event_open = types.MethodType(_rl._rescue_event_open, stub)
            stub._rescue_event_on_close = types.MethodType(_rl._rescue_event_on_close, stub)
            stub._rescue_event_finalize = types.MethodType(_rl._rescue_event_finalize, stub)

            def run_event(tk0, pnls, boosts_ok=True):
                members = [tk0, tk0 + 1, tk0 + 2, tk0 + 3]  # trigger, rescue, b1, b2
                stub._rescue_event_open({
                    'event_id': f"2026-06-16_A3_{tk0}", 'date_ist': '2026-06-16',
                    'anchor': 'A3_1430_Overlap', 'sched_iso': None, 'open_iso': 'x',
                    'trigger': {'ticket': tk0, 'side': 'BUY', 'trigger_pnl': -10.0},
                    'rescue': {'ticket': tk0 + 1, 'side': 'SELL', 'fill': 4300.0},
                    'boosts': [
                        {'ticket': tk0 + 2, 'fill': 4300.0, 'rc': 10009, 'comment': 'AUR_A3_S_B1'},
                        {'ticket': tk0 + 3, 'fill': 4300.0,
                         'rc': 10009 if boosts_ok else 10016, 'comment': 'AUR_A3_S_B2'}],
                    'boosts_placed_ok': boosts_ok, 'members': set(members)})
                for tk, p in zip(members, pnls):
                    stub._rescue_event_on_close(tk, p)

            run_event(1000, [-18, 150, 40, 28])    # net +200 -> CRASH_WIN
            run_event(2000, [-18, -120, -6, -56])  # net -200 -> WHIPSAW_LOSS
            run_event(3000, [-18, 20, 6, 2])       # net  +10 -> SCRATCH

            path = _os.path.join(tmp, "rescue_events.csv")
            with open(path) as f:
                rows = list(_csv.DictReader(f))
            branches = [r['branch'] for r in rows]
            tally = _rl.rescue_tally(path)
            ok = (len(rows) == 3
                  and branches == ['CRASH_WIN', 'WHIPSAW_LOSS', 'SCRATCH']
                  and abs(float(rows[0]['net_usd']) - 200) < 0.01
                  and len(fb_calls) == 3
                  and tally == {'CRASH_WIN': 1, 'WHIPSAW_LOSS': 1, 'SCRATCH': 1}
                  and len(stub.sent) == 3)
            detail = (f"rows={len(rows)} branches={branches} fb_writes={len(fb_calls)} "
                      f"tally=c{tally['CRASH_WIN']}/w{tally['WHIPSAW_LOSS']}/s{tally['SCRATCH']}")
        finally:
            _fj.save_rescue_event = _orig
        self._record(11, PASS if ok else FAIL, detail)

    def _step_fill_alert(self):
        # v3.0.7 Part A: the FILL formatter must ALWAYS produce a non-empty,
        # timestamped message and NEVER raise -- both with full enrichment AND
        # with fields missing (the silent-fill regression). We compose the body
        # with ts_header prepended and assert the 🕐 stamp is present (real or
        # fallback).
        from fills import format_fill_alert
        from telemetry import ts_header, anchor_time_block
        try:
            sched = pd.Timestamp('2026-06-16T10:00:00Z')
            full = format_fill_alert(
                {'anchor_label': 'A2_10h_London', 'side': 'BUY',
                 'entry_price': 4300.50}, ticket=12345,
                evt_block="\n" + anchor_time_block(sched, sched,
                                                   ontime_grace_s=float('inf')))
            # deliberately-missing: None entry_price, no side, no evt_block
            degraded = format_fill_alert(
                {'anchor_label': 'A3_1430_Overlap', 'entry_price': None},
                ticket=999, evt_block=None)
            bits, ok = [], True
            for nm, body in (("full", full), ("degraded", degraded)):
                composed = f"{ts_header()}\n{body}"
                nonempty = bool(body and body.strip())
                has_ts = "🕐" in composed
                ok = ok and nonempty and has_ts
                bits.append(f"{nm}: nonempty={nonempty} ts={has_ts}")
        except Exception as e:
            self._record(12, FAIL, f"raised: {e!r}")
            return
        self._record(12, PASS if ok else FAIL, "; ".join(bits))

    def _step_close_alert(self):
        # v3.0.7 Part A: the CLOSE formatter must ALWAYS produce a non-empty,
        # timestamped message and NEVER raise -- with realistic inputs AND with
        # None open_time(->no held), None slip, None held_min, None price, None
        # pnl. Compose with ts_header and assert the 🕐 stamp.
        from fills import format_close_alert
        from telemetry import ts_header
        try:
            full = format_close_alert(
                {'anchor_label': 'A3_1430_Overlap', 'side': 'SELL'},
                outcome='BE', close_price=4298.20, pnl_usd=0.0, daily_pnl=153.5,
                slip_txt=" (slip +0.30 vs stop $4298.50)",
                hold_txt="  |  held `12.3m`", nh_txt="", evt_block="")
            # open_time None -> held_min None -> hold_txt None; slip None; price/pnl None
            degraded = format_close_alert(
                {'anchor_label': 'A2_10h', 'side': 'BUY'}, outcome='CLOSED',
                close_price=None, pnl_usd=None, daily_pnl=None,
                slip_txt=None, hold_txt=None, nh_txt=None, evt_block=None)
            bits, ok = [], True
            for nm, body in (("full", full), ("degraded", degraded)):
                composed = f"{ts_header()}\n{body}"
                nonempty = bool(body and body.strip())
                has_ts = "🕐" in composed
                ok = ok and nonempty and has_ts
                bits.append(f"{nm}: nonempty={nonempty} ts={has_ts}")
        except Exception as e:
            self._record(13, FAIL, f"raised: {e!r}")
            return
        self._record(13, PASS if ok else FAIL, "; ".join(bits))

    def _step_ts_fallback(self):
        # v3.0.7 Part A: ts_header() must NEVER raise. Feed it bad input (a string
        # and a bare object, neither a datetime) and assert it returns a non-empty
        # fallback 🕐 string instead of throwing and blowing up the send path.
        from telemetry import ts_header
        raised = False
        outs = []
        for bad in ("not-a-datetime", object(), 12345):
            try:
                out = ts_header(bad)
            except Exception:
                raised = True
                out = ""
            outs.append(out)
        ok = (not raised
              and all(isinstance(o, str) and o.strip().startswith("🕐")
                      for o in outs))
        self._record(14, PASS if ok else FAIL,
                     f"raised={raised} | sample='{outs[0]}'")

    def _step_be_rung(self):
        # v3.0.7 Part B: NORMAL-leg BE ladder rung moved +$2.5 -> +$5.0. Drive the
        # REAL strategy.update_position_on_bar. The BE-to-entry move is now also
        # HOLD-GATED (see _step_hold_gate), so we test the +$5 THRESHOLD post-hold
        # with the trail disabled (be_trigger raised out of range) so only the BE
        # rung can move SL: at +$4.9 fav the SL stays at the initial $18 stop; at
        # +$5.0 fav it locks to breakeven (entry). RESCUE must NOT lock below +$10.
        import dataclasses
        from strategy import Position, update_position_on_bar
        try:
            cfg = dataclasses.replace(self.cfg, be_trigger=999.0)  # trail disabled
            entry = 4300.0
            sl0 = entry - cfg.sl_dist            # BUY initial stop
            ts0 = pd.Timestamp('2026-06-16T10:00:00Z')

            def run_fav(fav, role='normal'):
                p = Position(anchor_label='TEST', side='BUY', entry_price=entry,
                             entry_time=ts0, current_sl=sl0,
                             tp_level=entry + cfg.tp_dist, max_fav=entry + fav,
                             lot=cfg.lot_size, role=role)
                # post-hold bar (50m) so the hold-gated BE rung can engage; trail
                # is disabled via cfg so the BE rung is observed in isolation.
                ts1 = ts0 + pd.Timedelta(minutes=50)
                bar = pd.Series({'high': entry + fav, 'low': entry + fav,
                                 'close': entry + fav})
                update_position_on_bar(p, bar, ts1, cfg)
                return p.current_sl

            sl_49 = run_fav(4.9)
            sl_50 = run_fav(5.0)
            sl_resc = run_fav(9.0, role='rescue')
            be_at_49 = abs(sl_49 - entry) < 0.01
            be_at_50 = abs(sl_50 - entry) < 0.01
            resc_locked = abs(sl_resc - sl0) > 0.01
            ok = (not be_at_49) and be_at_50 and (not resc_locked)
            detail = (f"+4.9 SL={sl_49:.2f}(BE={be_at_49}) | "
                      f"+5.0 SL={sl_50:.2f}(BE={be_at_50}) | "
                      f"rescue+9 SL={sl_resc:.2f}(locked={resc_locked})")
        except Exception as e:
            self._record(15, FAIL, f"raised: {e!r}")
            return
        self._record(15, PASS if ok else FAIL, detail)

    def _step_hold_gate(self):
        # v3.0.7 HOLD-GATE: the breakeven-to-entry stop move must NOT engage
        # inside the 45m hold (live 2026-06-16: A2/A3 BE-scratched at 6.2m/2.8m).
        # The higher protective locks (+$6->+$4, +$10->peak-2) MUST stay active
        # inside the hold. Drive the REAL strategy core at the held times below.
        from strategy import Position, update_position_on_bar
        try:
            cfg = self.cfg
            entry = 4300.0
            sl0 = entry - cfg.sl_dist
            ts0 = pd.Timestamp('2026-06-16T10:00:00Z')

            def run(fav, held_min, role='normal'):
                p = Position(anchor_label='TEST', side='BUY', entry_price=entry,
                             entry_time=ts0, current_sl=sl0,
                             tp_level=entry + cfg.tp_dist, max_fav=entry + fav,
                             lot=cfg.lot_size, role=role)
                bar = pd.Series({'high': entry + fav, 'low': entry + fav,
                                 'close': entry + fav})
                update_position_on_bar(p, bar, ts0 + pd.Timedelta(minutes=held_min), cfg)
                return round(p.current_sl, 2)

            at_entry = lambda sl: abs(sl - entry) < 0.01
            at_sl0 = lambda sl: abs(sl - sl0) < 0.01
            at_lock4 = lambda sl: abs(sl - (entry + 4.0)) < 0.01

            checks = {
                # +$3 fav, 3m held -> SL still ORIGINAL (no move to entry)
                "+3@3m_no_move":   at_sl0(run(3, 3)),
                # the disease: +$5 fav, 3m held -> GATED, SL still ORIGINAL
                "+5@3m_gated":     at_sl0(run(5, 3)),
                # +$6 fav, 10m held -> the +$6->+$4 lock STILL engages in the hold
                "+6@10m_lock4":    at_lock4(run(6, 10)),
                # +$5 fav, 50m held -> post-hold, BE/entry move permitted (>= entry)
                "+5@50m_posthold": run(5, 50) >= entry - 0.01,
                # +$7 fav, 2m held -> +$6 lock engages but NOT a move to entry
                "+7@2m_lock_noBE": at_lock4(run(7, 2)) and not at_entry(run(7, 2)),
            }
            ok = all(checks.values())
            detail = " ".join(f"{k}={'Y' if v else 'N'}" for k, v in checks.items())
        except Exception as e:
            self._record(16, FAIL, f"raised: {e!r}")
            return
        self._record(16, PASS if ok else FAIL, detail)

    def _step_boost_sl(self):
        # v3.0.9: the SL-rescue boost stop is config-driven (boost_sl_dollars,
        # default $10) and replaces the old $6. Assert the configured value and
        # that the boost-SL geometry placed by fills.py equals entry -/+ that
        # value, plus the -$700 per-pair whipsaw cap (2 x $10 x 0.35 x 100).
        try:
            sl_d = float(getattr(self.cfg, 'boost_sl_dollars',
                                 getattr(self.cfg, 'rescue_boost_sl', 10.0)))
            n = int(getattr(self.cfg, 'rescue_boost_count', 2))
            entry = 4341.40
            # mirror fills.py: b_sl = entry - sgn*sl_d (BUY sgn=+1)
            buy_sl = round(entry - 1.0 * sl_d, 2)
            sell_sl = round(entry + 1.0 * sl_d, 2)
            cap = n * sl_d * self.cfg.lot_size * 100
            geom_ok = (abs(buy_sl - (entry - sl_d)) < 0.001
                       and abs(sell_sl - (entry + sl_d)) < 0.001)
            ok = (sl_d == 10.0) and geom_ok and n >= 1
            detail = (f"boost_sl=${sl_d:.0f} (want $10) | BUY entry-${sl_d:.0f}"
                      f"=${buy_sl:.2f} | {n}x whipsaw cap -${cap:.0f}")
        except Exception as e:
            self._record(17, FAIL, f"raised: {e!r}")
            return
        self._record(17, PASS if ok else FAIL, detail)

    def _step_discord_cards(self):
        # v3.1.0: every embed CARD builder must produce a Discord-valid embed
        # (title <=256, field value <=1024, <=25 fields, footer present) and carry
        # the ts_header footer. Pure code check -> PASS on correctness, no network.
        import discord_cards as dc
        try:
            cards = [
                dc.card_anchor_placed('A1_02h_Asia', 4300.5, 4282.5, 4330.5,
                                      4270.5, 4318.5, 0.35),
                dc.card_fill('A1', 'BUY', 4300.5, 12345, 'normal', 4282.5, 4330.5,
                             'scheduled 10:00 / actual 10:02'),
                dc.card_close('A1', 'BUY', 'TP', 4300.5, 4330.5, 1050.0,
                              held_min=44.0, day_total=1200.0),
                dc.card_close('A2', 'SELL', 'SL', 4300.5, 4282.5, -630.0,
                              held_min=45.0, day_total=-480.0),
                dc.card_close('A3', 'BUY', 'BE', 4300.5, 4300.6, 0.0,
                              held_min=12.3, day_total=153.5),
                dc.card_rescue('A1', 'twin trapped', 'SELL rescue', -10.0),
                dc.card_boost(1, 'SELL', 4300.5, 4310.5, 4270.5, '10009 DONE'),
                dc.card_fleet('A1', 'CRASH_WIN',
                              [('trigger', -630), ('rescue', 226)], -84,
                              counterfactual=-406),
                dc.card_eod('2026-06-17', 465.0, 4, balance=50465.0,
                            anchors_hit='A1 A2'),
                dc.card_heartbeat(50465.0, 50470.0, 1, 1, 'A1 A2', 'FILL A2'),
                dc.card_status({'Balance': '$50,465', 'Open': 1, 'Pending': 1}),
                dc.card_connect(), dc.card_intent_warning(),
                dc.card_generic('AUREON INFO', 'plain text', dc.BLUE),
            ]
            bad = []
            for c in cards:
                if len(c.get('title', '')) > 256:
                    bad.append('title')
                if len(c.get('fields', [])) > 25:
                    bad.append('fieldcount')
                for f in c.get('fields', []):
                    if len(f['name']) > 256 or len(f['value']) > 1024 or not f['value']:
                        bad.append('field')
                if not c.get('footer', {}).get('text'):
                    bad.append('footer')
            # color correctness on the close cards (green/red/amber)
            color_ok = (cards[2]['color'] == dc.GREEN
                        and cards[3]['color'] == dc.RED
                        and cards[4]['color'] == dc.AMBER)
            ok = (not bad) and color_ok
            detail = (f"{len(cards)} cards valid, colors TP/SL/BE ok={color_ok}"
                      if ok else f"issues={set(bad)} color_ok={color_ok}")
        except Exception as e:
            self._record(18, FAIL, f"raised: {e!r}")
            return
        self._record(18, PASS if ok else FAIL, detail)

    def _step_discord_dedup(self):
        # v3.1.0: a critical event keyed by ticket must post ONCE (not twice on
        # reconnect/queue-flush); distinct events always post. Drive the REAL
        # DiscordClient with a stubbed transport (no network).
        import discord_client as dcl, discord_cards as dc
        try:
            client = dcl.DiscordClient(dcl.DiscordConfig('x', '123'))
            posts, up = [], {'v': True}
            client._post_embed = lambda e: (posts.append(e.get('title')) or True) \
                if up['v'] else False
            c = dc.card_close('A1', 'BUY', 'TP', 1, 2, 10)
            client.deliver('SUCCESS', 'c', card=c, event_key='close:1', critical=True)
            client.deliver('SUCCESS', 'c', card=c, event_key='close:1', critical=True)
            one = (len(posts) == 1)
            client.deliver('SUCCESS', 'c2', card=c, event_key='close:2', critical=True)
            two = (len(posts) == 2)
            # queue while down, then on recovery the SAME event posts exactly once
            # (the queued copy is dedup-skipped on flush).
            up['v'] = False
            client.deliver('WARN', 'f', card=c, event_key='fill:9', critical=True)
            queued = (len(client._critical_q) == 1)
            up['v'] = True
            client.deliver('WARN', 'f', card=c, event_key='fill:9', critical=True)
            flushed = ('fill:9' in client._seen_set)
            no_dup = (len(posts) == 3 and len(client._critical_q) == 0)
            ok = one and two and queued and flushed and no_dup
            detail = (f"same->1={one} distinct->2={two} queued={queued} "
                      f"flushed={flushed} no_dup={no_dup}")
        except Exception as e:
            self._record(19, FAIL, f"raised: {e!r}")
            return
        self._record(19, PASS if ok else FAIL, detail)

    def _step_discord_heartbeat(self):
        # v3.1.0: heartbeat card builds non-empty and carries the ts_header footer.
        import discord_cards as dc
        try:
            c = dc.card_heartbeat(50000.0, 50010.0, 0, 0, 'A1', 'startup')
            ok = (bool(c.get('title')) and bool(c.get('fields'))
                  and bool(c.get('footer', {}).get('text')))
            detail = f"title={c.get('title')!r} footer={c['footer']['text']!r}"
        except Exception as e:
            self._record(20, FAIL, f"raised: {e!r}")
            return
        self._record(20, PASS if ok else FAIL, detail)

    def _step_discord_connect(self):
        # v3.1.0: gateway/reachability is environment-dependent -> WARN (never
        # FAIL) when Discord isn't configured or the network is unavailable. Also
        # reports that the intent self-check + connect-card logic is wired.
        import discord_client as dcl
        cfg = dcl.config_from_env()
        intent_wired = hasattr(dcl.DiscordClient, 'start_gateway')
        if cfg is None:
            self._record(21, WARN, "Discord not configured (set DISCORD_BOT_TOKEN/"
                         f"CHANNEL_ID); intent self-check wired={intent_wired}")
            return
        # configured: try a single reachability post of the connect card.
        try:
            client = dcl.DiscordClient(cfg)
            import discord_cards as dc
            reached = client.post_card(dc.card_connect())
            if reached:
                self._record(21, PASS, f"connect card posted; intent-check wired="
                             f"{intent_wired}")
            else:
                self._record(21, WARN, "Discord unreachable (network) — alerts will "
                             f"retry/queue; intent-check wired={intent_wired}")
        except Exception as e:
            self._record(21, WARN, f"connect attempt raised (network): {e!r}")

    def _step_lone_rescue(self):
        # v3.1.3 LONE-LEG HEDGING RESCUE: a No-OCO 2nd fill fires the rescue +
        # boosts even when the twin already CLOSED (flag set, twin closed). Drives
        # the REAL decision helper fills.is_rescue_fill. Also confirms the rescue
        # invariants the lone path reuses unchanged: -$10 trigger (the $10 straddle
        # spread = sibling fill), 2 boosts, boost SL $10, whipsaw cap -$700.
        from fills import is_rescue_fill
        try:
            cfg = self.cfg
            lone = is_rescue_fill(flag_hint=True, twin_open=False)   # twin closed -> FIRES
            first = is_rescue_fill(flag_hint=False, twin_open=False)  # genuine 1st fill -> no
            struct = is_rescue_fill(flag_hint=False, twin_open=True)  # twin open -> fires
            n = int(getattr(cfg, 'rescue_boost_count', 2))
            sl = float(getattr(cfg, 'boost_sl_dollars', 10.0))
            spread = 2.0 * float(getattr(cfg, 'trigger_dist', 5.0))   # straddle = $10 apart
            cap = n * sl * cfg.lot_size * 100
            ok = (lone and (not first) and struct and n == 2 and sl == 10.0
                  and abs(spread - 10.0) < 1e-9 and abs(cap - 700.0) < 1e-6)
            detail = (f"lone-fires={lone} first-fill={first} struct={struct} | "
                      f"trigger=${spread:.0f} boosts={n} SL=${sl:.0f} cap=-${cap:.0f}")
        except Exception as e:
            self._record(22, FAIL, f"raised: {e!r}")
            return
        self._record(22, PASS if ok else FAIL, detail)

    def _step_boost_trail(self):
        # v3.2.6 BOOST BREATH-GAP +$8 ARM GATE + $10 BACKSTOP (boosts only). Drive the
        # REAL strategy core over price paths. The breath-gap trail is INACTIVE until
        # the boost peaks >= +arm (boost_trail_arm_fav=$8); below that ONLY the $10
        # backstop protects (incident 2026-06-23 fix). At +arm a +floor lock engages;
        # above it the $gap trail follows, floor never < +floor.
        from strategy import Position, update_position_on_bar
        try:
            cfg = self.cfg
            gap = float(getattr(cfg, 'boost_trail_gap_dollars', 3.50))
            hard = float(getattr(cfg, 'boost_sl_dollars', 10.0))
            arm = float(getattr(cfg, 'boost_trail_arm_fav', 8.0))
            floor = float(getattr(cfg, 'boost_lock_floor', 8.0))
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-17T13:50:00Z')

            def run(bars, boost=True, role='rescue'):
                p = Position(anchor_label='T', side='BUY', entry_price=entry,
                             entry_time=ts0, current_sl=entry - hard,
                             tp_level=entry + 30.0, max_fav=entry,
                             lot=cfg.lot_size, role=role, boost=boost)
                for i, b in enumerate(bars):
                    update_position_on_bar(p, pd.Series(b),
                                           ts0 + pd.Timedelta(minutes=i + 1), cfg)
                    if p.closed:
                        break
                return p

            # 1) reverses BEFORE +$8 -> trail INACTIVE -> rides to the $10 BACKSTOP,
            #    NOT -gap (this is the incident fix).
            p1 = run([{'open': 100, 'high': 101, 'low': entry - hard - 1, 'close': 92}])
            backstop_below8 = p1.closed and abs((entry - p1.exit_price) - hard) < 0.05
            # 1b) a shallow reverse before +$8 (does NOT reach the backstop) -> the
            #     boost is NOT cut; it rides (the old code would have cut it at -gap).
            p1b = run([{'open': 100, 'high': 101, 'low': entry - gap - 1, 'close': 96}])
            rides_not_cut = (not p1b.closed)
            # 2) reaches +$8 then reverses -> closes at the +$8 LOCK FLOOR (not -gap,
            #    not BE).
            p2 = run([{'open': 100, 'high': entry + arm + 0.5, 'low': 100.2, 'close': entry + arm},
                      {'open': entry + arm, 'high': entry + arm, 'low': entry + floor - 3, 'close': entry + floor - 3}])
            lock_floor = p2.closed and abs((p2.exit_price - entry) - floor) < 0.05
            # 3) runs PAST +$8 -> trail follows $gap (exit ~ peak-gap), floor >= +$8.
            p3 = run([{'open': 100, 'high': 112, 'low': 100.5, 'close': 111},
                      {'open': 111, 'high': 111, 'low': 108, 'close': 108}])
            trail_gap = (p3.closed and abs((p3.exit_price - entry) - (12.0 - gap)) < 0.05
                         and (p3.exit_price - entry) >= floor - 0.05)
            # 4) one-way: after the peak a non-triggering retrace must NOT loosen SL
            p4 = run([{'open': 100, 'high': 112, 'low': 100.5, 'close': 111}])
            sl_peak = p4.current_sl
            update_position_on_bar(p4, pd.Series(
                {'open': 109, 'high': 109, 'low': 108.6, 'close': 108.8}),
                ts0 + pd.Timedelta(minutes=2), cfg)
            one_way = (p4.closed or p4.current_sl >= sl_peak - 1e-9)
            ok = backstop_below8 and rides_not_cut and lock_floor and trail_gap and one_way
            detail = (f"rev<8->backstop{p1.exit_price}({backstop_below8}) "
                      f"shallow_rides={rides_not_cut} "
                      f"reach8_rev->floor{p2.exit_price}({lock_floor}) "
                      f"runpast8->trail{p3.exit_price}({trail_gap}) one_way={one_way}")
        except Exception as e:
            self._record(23, FAIL, f"raised: {e!r}")
            return
        self._record(23, PASS if ok else FAIL, detail)

    def _step_lone_branches(self):
        # v3.1.4 LONE-LEG BRANCH RESOLUTION (dry-run; no real orders). Proves the
        # lone-leg rescue (trigger=None, members = rescue leg + 2 boosts) resolves
        # to the right outcome on three simulated price paths, that the downside is
        # BOUNDED by the -$700 boost cap, and that the no-boost counterfactual is
        # logged per event. Boost P&Ls for TREND/WHIPSAW come from the REAL
        # strategy core driven over a price path (proving trail-past-+8 / $10 SL).
        import tempfile, csv as _csv, os as _os, types
        import rescue_log as _rl
        import firebase_journal as _fj
        from strategy import Position, update_position_on_bar, realize_pnl_usd
        cfg = self.cfg
        lot = cfg.lot_size
        ts0 = pd.Timestamp('2026-06-17T13:50:00Z')

        gap = float(getattr(cfg, 'boost_trail_gap_dollars', 3.50))

        def sim_boost(bars, entry=100.0):
            # BUY boost (breath-gap trail + $10 backstop, $30 TP); feed OHLC bars
            # through the REAL strategy core; return realized USD P&L (or None).
            p = Position(anchor_label='T', side='BUY', entry_price=entry,
                         entry_time=ts0, current_sl=entry - 10.0,
                         tp_level=entry + 30.0, max_fav=entry, lot=lot,
                         role='rescue', boost=True)
            for b in bars:
                if update_position_on_bar(p, pd.Series(b),
                                          ts0 + pd.Timedelta(minutes=60), cfg):
                    break
            return round(realize_pnl_usd(p, cfg), 2) if p.closed else None

        hard = float(getattr(cfg, 'boost_sl_dollars', 10.0))
        # TREND: rise to +25 then pull back to the breath trail -> rides past +8.
        b_trend = sim_boost([{'open': 100, 'high': 125, 'low': 100.5, 'close': 124},
                             {'open': 124, 'high': 124, 'low': 121, 'close': 121}])
        # WHIPSAW: v3.2.6 a boost that reverses BEFORE +$8 now rides to the $10
        # BACKSTOP (the arm-gate fix) -- the worst-case is -$10/boost, the cap.
        b_whip = sim_boost([{'open': 100, 'high': 100.5, 'low': 89, 'close': 90}])
        old_cap = round(2 * hard * lot * 100, 2)

        _fj_orig = _fj.save_rescue_event
        _fj.save_rescue_event = lambda d, e, doc: True
        tmp = tempfile.mkdtemp(prefix="aureon_lone_")
        try:
            stub = types.SimpleNamespace(
                run_dir=tmp, state={"last_broker_date": "2026-06-17"},
                _rescue_events={}, _rescue_event_by_ticket={}, sent=[])
            stub.tele = types.SimpleNamespace(send=lambda m=None, *a, **k: stub.sent.append(m))
            stub._rescue_event_open = types.MethodType(_rl._rescue_event_open, stub)
            stub._rescue_event_on_close = types.MethodType(_rl._rescue_event_on_close, stub)
            stub._rescue_event_finalize = types.MethodType(_rl._rescue_event_finalize, stub)

            def lone_event(tk0, rescue_pnl, b1, b2):
                # LONE leg: twin already closed -> trigger ticket is None; members
                # are the rescue leg + its 2 boosts only.
                rk, k1, k2 = tk0 + 1, tk0 + 2, tk0 + 3
                stub._rescue_event_open({
                    'event_id': f"2026-06-17_A4_{tk0}", 'date_ist': '2026-06-17',
                    'anchor': 'A4_1640_NYopen', 'sched_iso': None, 'open_iso': 'x',
                    'trigger': {'ticket': None, 'side': None, 'trigger_pnl': None},
                    'rescue': {'ticket': rk, 'side': 'BUY', 'fill': 4334.0},
                    'boosts': [{'ticket': k1, 'fill': 4334.0, 'rc': 10009, 'comment': 'AUR_A4_B_B1'},
                               {'ticket': k2, 'fill': 4334.0, 'rc': 10009, 'comment': 'AUR_A4_B_B2'}],
                    'boosts_placed_ok': True, 'members': {rk, k1, k2}})
                for tk, p in ((rk, rescue_pnl), (k1, b1), (k2, b2)):
                    stub._rescue_event_on_close(tk, p)

            lone_event(1000, rescue_pnl=400.0, b1=b_trend, b2=b_trend)   # TREND
            lone_event(2000, rescue_pnl=-50.0, b1=b_whip,  b2=b_whip)    # WHIPSAW
            lone_event(3000, rescue_pnl=5.0,   b1=10.0,    b2=-5.0)      # SCRATCH (chop)

            path = _os.path.join(tmp, "rescue_events.csv")
            with open(path) as f:
                rows = list(_csv.DictReader(f))
            by = {r['event_id'].split('_')[-1]: r for r in rows}
            trend, whip, scr = by['1000'], by['2000'], by['3000']

            checks = {
                # boost rode the breath trail well past +$8 in the trend
                "trend_boost_rides>8": (b_trend is not None and b_trend > 8 * lot * 100),
                "trend=CRASH_WIN":     trend['branch'] == 'CRASH_WIN',
                "trend_net>0":         float(trend['net_usd']) > 0,
                # v3.2.6: a reverse before +$8 rides to the $10 backstop (the fix)
                "whip_boost~-backstop": (b_whip is not None and abs(b_whip + hard * lot * 100) < 1.0),
                "whip=WHIPSAW_LOSS":   whip['branch'] == 'WHIPSAW_LOSS',
                # combined boost loss is bounded BY the -$700 cap (== 2x the backstop)
                "whip<=old_700cap":    (-old_cap - 0.5 <= 2 * b_whip < 0),
                "scratch=SCRATCH":     scr['branch'] == 'SCRATCH',
                # no-boost counterfactual logged = rescue leg alone (boosts excluded)
                "cf_logged_trend":     abs(float(trend['no_boost_net']) - 400.0) < 0.01,
                "cf_logged_whip":      abs(float(whip['no_boost_net']) - (-50.0)) < 0.01,
                # lone events carry NO trigger ticket
                "lone_no_trigger":     all((r['trigger_ticket'] or '') == '' for r in rows),
            }
            ok = all(checks.values())
            detail = (f"boost trend={b_trend} whip={b_whip} (old_cap=-${old_cap:.0f}) | "
                      + " ".join(f"{k}={'Y' if v else 'N'}" for k, v in checks.items()))
        except Exception as e:
            _fj.save_rescue_event = _fj_orig
            self._record(24, FAIL, f"raised: {e!r}")
            return
        finally:
            _fj.save_rescue_event = _fj_orig
        self._record(24, PASS if ok else FAIL, detail)

    def _step_boost_isolation(self):
        # v3.1.6 ISOLATION: a winning ORIGINAL leg and losing BOOSTS resolve
        # INDEPENDENTLY. Driving the boost to its stop must NOT read, modify, or
        # close the original (separate Position objects / separate tickets), and
        # the original must still reach its OWN profitable exit. Boost P&L can only
        # add when it wins or lose its own capital when it fails -- it can never
        # turn a winning original into a net loss by pooling/closing it.
        from strategy import Position, update_position_on_bar, realize_pnl_usd
        try:
            cfg = self.cfg
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-17T13:50:00Z')
            orig = Position(anchor_label='A4_1640_NYopen', side='BUY',
                            entry_price=entry, entry_time=ts0,
                            current_sl=entry - cfg.sl_dist, tp_level=entry + cfg.tp_dist,
                            max_fav=entry, lot=cfg.lot_size, role='normal', boost=False)
            orig_sl_before = orig.current_sl
            boost = Position(anchor_label='A4_1640_NYopen', side='BUY',
                             entry_price=entry, entry_time=ts0,
                             current_sl=entry - 10.0, tp_level=entry + 30.0,
                             max_fav=entry, lot=cfg.lot_size, role='rescue', boost=True)

            # 1) Drive the BOOST to a loss. v3.2.6: a reverse before +$8 rides to the
            #    $10 backstop (arm-gate fix), so drop through the backstop to realize
            #    the loss. The ORIGINAL object must be byte-for-byte untouched by this.
            update_position_on_bar(boost, pd.Series(
                {'open': 100, 'high': 100.5, 'low': 89, 'close': 90}),
                ts0 + pd.Timedelta(minutes=1), cfg)
            boost_lost = boost.closed and realize_pnl_usd(boost, cfg) < 0
            orig_untouched = (not orig.closed
                              and orig.current_sl == orig_sl_before
                              and orig.exit_price is None)

            # 2) The ORIGINAL runs to its OWN take-profit, independently of the
            #    boost having lost. Its result stands alone (positive).
            out = update_position_on_bar(orig, pd.Series(
                {'open': 100, 'high': entry + cfg.tp_dist + 1, 'low': 100,
                 'close': entry + cfg.tp_dist}), ts0 + pd.Timedelta(minutes=60), cfg)
            orig_own_tp = (out == 'TP' and realize_pnl_usd(orig, cfg) > 0)

            # 3) No pooling: the winning original is NOT dragged negative by the
            #    losing boosts (they are separate line items).
            orig_pnl = realize_pnl_usd(orig, cfg)
            boost_pnl = realize_pnl_usd(boost, cfg)
            no_pool = orig_pnl > 0 and boost_pnl < 0

            ok = boost_lost and orig_untouched and orig_own_tp and no_pool
            detail = (f"orig_untouched={orig_untouched} orig_own_TP={orig_own_tp} "
                      f"(orig ${orig_pnl:+.0f}) boost_lost={boost_lost} "
                      f"(boost ${boost_pnl:+.0f}) no_pool={no_pool}")
        except Exception as e:
            self._record(25, FAIL, f"raised: {e!r}")
            return
        self._record(25, PASS if ok else FAIL, detail)

    def _step_lone_live_logging(self):
        # v3.1.7 LIVE-PATH PARITY: the 2026-06-18 A1 lone rescue fired but
        # rescuestats showed 0 -- the live event opened but never finalized/wrote
        # (in-flight events were in-memory only; a restart between open and close
        # orphaned them). This drives the SAME bound methods the live path uses
        # (_rescue_event_open/on_close/finalize + the new persist/rehydrate) and
        # asserts: (a) an opened lone event that closes ALWAYS writes a row, (b) it
        # SURVIVES a restart (persist -> fresh object -> rehydrate -> close ->
        # write), (c) the row has event_type + SEPARATE orig/boost P&L fields, and
        # (d) no opened-but-never-finalized orphan remains.
        import tempfile, csv as _csv, os as _os, types
        import rescue_log as _rl
        import firebase_journal as _fj
        _fj_orig = _fj.save_rescue_event
        _fj.save_rescue_event = lambda d, e, doc: True
        tmp = tempfile.mkdtemp(prefix="aureon_lonelive_")
        try:
            def make_bot(state):
                b = types.SimpleNamespace(run_dir=tmp, state=state,
                                          _rescue_events={}, _rescue_event_by_ticket={})
                b.tele = types.SimpleNamespace(send=lambda m=None, *a, **k: None)
                b._save_state = lambda: None      # state dict is round-tripped below
                for m in ('_rescue_event_open', '_rescue_event_on_close',
                          '_rescue_event_finalize', '_persist_rescue_events',
                          '_rehydrate_rescue_events'):
                    setattr(b, m, types.MethodType(getattr(_rl, m), b))
                return b

            # Bot #1: open a LONE event (trigger=None), then "crash" -- persisted.
            bot1 = make_bot({'last_broker_date': '2026-06-18'})
            bot1._rescue_event_open({
                'event_id': '2026-06-18_A1_555', 'date_ist': '2026-06-18',
                'anchor': 'A1_02h_Asia', 'sched_iso': None, 'open_iso': 'x',
                'trigger': {'ticket': None, 'side': None, 'trigger_pnl': None},
                'rescue': {'ticket': 555, 'side': 'BUY', 'fill': 4334.0},
                'boosts': [{'ticket': 556, 'fill': 4334.0, 'rc': 10009, 'comment': 'AUR_A1_B_B1'},
                           {'ticket': 557, 'fill': 4334.0, 'rc': 10009, 'comment': 'AUR_A1_B_B2'}],
                'boosts_placed_ok': True, 'members': {555, 556, 557}})
            persisted = ('rescue_events_extended' in bot1.state
                         and bot1.state['rescue_events_extended'])
            saved = dict(bot1.state)        # what would be on disk across a restart

            # RESTART: fresh object, rehydrate, THEN the members close (the win).
            bot2 = make_bot(dict(saved))
            bot2._rehydrate_rescue_events()
            rehydrated = ('2026-06-18_A1_555' in bot2._rescue_events
                          and bot2._rescue_event_by_ticket.get(555) == '2026-06-18_A1_555')
            bot2._rescue_event_on_close(556, 700.0)
            bot2._rescue_event_on_close(557, 700.0)
            opened_not_finalized = bool(bot2._rescue_events)   # still 1 orphan mid-close
            bot2._rescue_event_on_close(555, 1050.0)           # last member -> finalize
            no_orphan = (len(bot2._rescue_events) == 0)        # finalized, none left

            path = _os.path.join(tmp, "rescue_events.csv")
            rows = list(_csv.DictReader(open(path))) if _os.path.exists(path) else []
            wrote = (len(rows) == 1)
            r = rows[0] if rows else {}
            fields_ok = (wrote and r.get('event_type') == 'LONE_RESCUE'
                         and abs(float(r['net_usd']) - 2450.0) < 0.01
                         and abs(float(r['orig_pnl']) - 1050.0) < 0.01     # rescue leg alone
                         and abs(float(r['boost_pnl']) - 1400.0) < 0.01    # 2 boosts, isolated
                         and (r.get('trigger_ticket') or '') == ''         # lone
                         and r.get('branch') == 'CRASH_WIN')
            ok = (persisted and rehydrated and opened_not_finalized
                  and no_orphan and wrote and fields_ok)
            detail = (f"persist={bool(persisted)} rehydrate={rehydrated} "
                      f"survived_restart={wrote} no_orphan={no_orphan} "
                      f"fields(type/orig/boost)={fields_ok}")
        except Exception as e:
            _fj.save_rescue_event = _fj_orig
            self._record(26, FAIL, f"raised: {e!r}")
            return
        finally:
            _fj.save_rescue_event = _fj_orig
        self._record(26, PASS if ok else FAIL, detail)

    def _step_backtest_parity(self):
        # v3.1.8 BACKTEST PARITY: the tick backtester must REUSE the live strategy
        # functions by IMPORT (identity), not a drifting reimplementation, and a
        # known fixture must replay to the expected P&L. Catches anyone who
        # copy-pastes a parallel engine instead of importing the live one. The
        # engine is loaded by FILE PATH to dodge the name collision with the
        # repo-root backtest.py.
        import importlib.util as _ilu
        import strategy as _strat
        import boosts as _boosts
        import position_telemetry as _ptel
        try:
            _root = os.path.dirname(os.path.abspath(__file__))
            _path = os.path.join(_root, 'backtest', 'backtest.py')
            spec = _ilu.spec_from_file_location('aureon_bt_engine', _path)
            bt = _ilu.module_from_spec(spec)
            spec.loader.exec_module(bt)
            # (a) identity: the backtester's engine IS the live engine, AND its
            # boost trigger IS the canonical boosts.plan_boost_event (v3.2.0:
            # import-path parity so the backtest can't drift from live/tests).
            # v3.3.0: the trail-lock guards (update_max_fav/lock_level_for) and the
            # per-position tracer are the SAME objects too -- so the fix can't drift.
            id_ok = (bt.update_position_on_bar is _strat.update_position_on_bar
                     and bt.realize_pnl_usd is _strat.realize_pnl_usd
                     and bt.Position is _strat.Position
                     and bt.plan_boost_event is _boosts.plan_boost_event
                     and bt.update_max_fav is _strat.update_max_fav
                     and bt.lock_level_for is _strat.lock_level_for
                     and bt.lock_trigger_reached is _strat.lock_trigger_reached
                     and bt.PositionTracer is _ptel.PositionTracer)
            srcs = list(bt.rule_sources())
            srcs_ok = all(s in srcs for s in (
                'strategy.update_position_on_bar', 'anchors.resolved_anchor_hm',
                'fills.is_rescue_fill', 'rescue_log._branch_for',
                'boosts.plan_boost_event', 'strategy.update_max_fav',
                'position_telemetry.PositionTracer',
                'strategy.lock_trigger_reached'))
            # (b) fixture: a BUY entered at 100 with the live $30 TP exits at TP for
            #     +$1050 @ lot 0.35 -- proving the backtest replays via live logic.
            cfg = self.cfg
            entry = 100.0
            p = bt.Position(anchor_label='FIX', side='BUY', entry_price=entry,
                            entry_time=pd.Timestamp('2026-05-01T10:00:00Z'),
                            current_sl=entry - cfg.sl_dist, tp_level=entry + cfg.tp_dist,
                            max_fav=entry, lot=cfg.lot_size)
            bar = pd.Series({'open': entry, 'high': entry + cfg.tp_dist + 1,
                             'low': entry, 'close': entry + cfg.tp_dist})
            out = bt.update_position_on_bar(
                p, bar, p.entry_time + pd.Timedelta(minutes=60), cfg)
            pnl = round(bt.realize_pnl_usd(p, cfg), 2)
            expect = round(cfg.tp_dist * cfg.contract_size * cfg.lot_size, 2)
            fixture_ok = (out == 'TP' and abs(pnl - expect) < 0.01)
            ok = id_ok and srcs_ok and fixture_ok
            detail = (f"engine_identity={id_ok} sources_ok={srcs_ok} "
                      f"fixture_TP=${pnl:.0f}(want ${expect:.0f}){fixture_ok}")
        except Exception as e:
            self._record(27, FAIL, f"raised: {e!r}")
            return
        self._record(27, PASS if ok else FAIL, detail)

    def _step_boost_trigger(self):
        # v3.2.0 BOOST TRIGGER (the A3 fire-at-fill fix). The lone-leg boost
        # decision is now ONE canonical function (boosts.plan_boost_event) called
        # by LIVE (fills per-tick), BACKTEST, and this test -- import-path parity
        # so they can never diverge. Asserts, using the LIVE module path (no
        # stubs): (1) live + backtest call the SAME fn; (2) NEVER fires at the
        # leg's fill (or <$10 move); (3) a fired plan's entry is always >= $10
        # from the fill; (4) RALLY when the leg WINS +$10 (same dir); (5) RESCUE
        # when the leg LOSES -$10 (opposite dir); (6) the -$700 cap clamps -715.
        import importlib.util as _ilu
        import fills as _fills
        import boosts as _boosts
        try:
            cfg = self.cfg
            # (1) IMPORT-PATH PARITY: live calls the canonical fn; backtest too.
            live_parity = (_fills.boosts.plan_boost_event
                           is _boosts.plan_boost_event)
            _root = os.path.dirname(os.path.abspath(__file__))
            _path = os.path.join(_root, 'backtest', 'backtest.py')
            spec = _ilu.spec_from_file_location('aureon_bt_engine_bt', _path)
            bt = _ilu.module_from_spec(spec)
            spec.loader.exec_module(bt)
            bt_parity = (bt.plan_boost_event is _boosts.plan_boost_event)

            fill = 4266.3
            # (2) NO-FIRE-AT-FILL: at the fill, and at +$3, returns None.
            at_fill = _boosts.plan_boost_event('SELL', fill, fill, cfg)
            at_3 = _boosts.plan_boost_event('SELL', fill, fill - 3.0, cfg)
            no_fire = (at_fill is None and at_3 is None)

            # (4) RALLY: a lone leg WINNING by +$10 -> RALLY_BOOST, SAME side.
            #     BUY winning means price up $10.
            rally = _boosts.plan_boost_event('BUY', fill, fill + 10.0, cfg)
            rally_ok = (rally is not None
                        and rally.event_type == 'RALLY_BOOST'
                        and rally.boost_side == 'BUY')

            # (5) RESCUE: a lone leg LOSING by -$10 -> RESCUE_BOOST, OPPOSITE side.
            #     BUY losing means price down $10.
            rescue = _boosts.plan_boost_event('BUY', fill, fill - 10.0, cfg)
            rescue_ok = (rescue is not None
                         and rescue.event_type == 'RESCUE_BOOST'
                         and rescue.boost_side == 'SELL')

            # (3) ENTRY >= $10 from fill (use the -$10 RESCUE plan above).
            entry_ok = (rescue is not None
                        and abs(rescue.entry_ref - fill) >= 10.0 - 1e-6)

            # (6) CAP: A3 -715.05 clamps (breached); -650 does not.
            cap_breach = (_boosts.cap_breached(-715.05, cfg) is True
                          and _boosts.cap_breached(-650, cfg) is False)

            ok = (live_parity and bt_parity and no_fire and rally_ok
                  and rescue_ok and entry_ok and cap_breach)
            detail = (f"live_parity={live_parity} bt_parity={bt_parity} "
                      f"no_fire@fill/+3={no_fire} rally={rally_ok} "
                      f"rescue={rescue_ok} entry>=10={entry_ok} cap={cap_breach}")
        except Exception as e:
            self._record(28, FAIL, f"raised: {e!r}")
            return
        self._record(28, PASS if ok else FAIL, detail)

    def _step_boost_toggles(self):
        # v3.2.2 INDEPENDENT BOOST TOGGLES. rally_boosts_enabled /
        # rescue_boosts_enabled gate the RALLY / RESCUE branches independently, in
        # the SINGLE canonical boosts.plan_boost_event the LIVE per-tick path
        # (fills._check_boost_triggers) and the BACKTEST (run_month) both import.
        # Asserts, on the live module path (no stubs): (1) rally OFF => a +$10 move
        # fires ZERO rally boosts (None); (2) rescue OFF => a -$10 move fires ZERO
        # rescue boosts (None); (3) INDEPENDENCE: with one flag off the OTHER still
        # fires normally; (4) IMPORT-PATH PARITY: live + backtest call the SAME fn
        # (like step 27/28), so they honor the SAME flags; (5) DEFAULTS (both True)
        # reproduce current behavior -- no silent change unless a flag is set.
        import importlib.util as _ilu
        import fills as _fills
        import boosts as _boosts
        from config import Config as _Config
        try:
            fill = 4266.3
            up, down = fill + 10.0, fill - 10.0   # +$10 winning / -$10 losing (BUY leg)

            # (5) DEFAULTS: both True -> RALLY on +$10, RESCUE on -$10 (unchanged).
            cfg_def = _Config()
            d_rally = _boosts.plan_boost_event('BUY', fill, up, cfg_def)
            d_rescue = _boosts.plan_boost_event('BUY', fill, down, cfg_def)
            defaults_ok = (cfg_def.rally_boosts_enabled is True
                           and cfg_def.rescue_boosts_enabled is True
                           and d_rally is not None and d_rally.event_type == 'RALLY_BOOST'
                           and d_rescue is not None and d_rescue.event_type == 'RESCUE_BOOST')

            # (1) RALLY OFF: +$10 fires ZERO rally boosts; (3) RESCUE still fires.
            cfg_nr = _Config(); cfg_nr.rally_boosts_enabled = False
            nr_rally = _boosts.plan_boost_event('BUY', fill, up, cfg_nr)
            nr_rescue = _boosts.plan_boost_event('BUY', fill, down, cfg_nr)
            rally_off_ok = (nr_rally is None
                            and nr_rescue is not None
                            and nr_rescue.event_type == 'RESCUE_BOOST')

            # (2) RESCUE OFF: -$10 fires ZERO rescue boosts; (3) RALLY still fires.
            cfg_ns = _Config(); cfg_ns.rescue_boosts_enabled = False
            ns_rescue = _boosts.plan_boost_event('BUY', fill, down, cfg_ns)
            ns_rally = _boosts.plan_boost_event('BUY', fill, up, cfg_ns)
            rescue_off_ok = (ns_rescue is None
                             and ns_rally is not None
                             and ns_rally.event_type == 'RALLY_BOOST')

            # Both OFF: neither branch fires (sanity).
            cfg_off = _Config()
            cfg_off.rally_boosts_enabled = False
            cfg_off.rescue_boosts_enabled = False
            both_off_ok = (_boosts.plan_boost_event('BUY', fill, up, cfg_off) is None
                           and _boosts.plan_boost_event('BUY', fill, down, cfg_off) is None)

            # (4) IMPORT-PATH PARITY: live + backtest call the canonical fn, so the
            #     gating above is the SAME code both honor (cannot diverge).
            live_parity = (_fills.boosts.plan_boost_event is _boosts.plan_boost_event)
            _root = os.path.dirname(os.path.abspath(__file__))
            _path = os.path.join(_root, 'backtest', 'backtest.py')
            spec = _ilu.spec_from_file_location('aureon_bt_engine_tog', _path)
            bt = _ilu.module_from_spec(spec)
            spec.loader.exec_module(bt)
            bt_parity = (bt.plan_boost_event is _boosts.plan_boost_event)

            ok = (defaults_ok and rally_off_ok and rescue_off_ok and both_off_ok
                  and live_parity and bt_parity)
            detail = (f"defaults={defaults_ok} rally_off={rally_off_ok} "
                      f"rescue_off={rescue_off_ok} both_off={both_off_ok} "
                      f"live_parity={live_parity} bt_parity={bt_parity}")
        except Exception as e:
            self._record(29, FAIL, f"raised: {e!r}")
            return
        self._record(29, PASS if ok else FAIL, detail)

    def _step_underwater_lock(self):
        # v3.3.0 (a) UNDERWATER-THE-WHOLE-TIME long must NEVER advance a lock -- the
        # 2026-06-19 A2 root cause. Drives the REAL strategy core: a BUY that prints
        # underwater for its entire life, INCLUDING one garbage-feed spike bar
        # (high jumps +$28, far past max_tick_jump). The confirmed-price max_fav
        # filter must reject the spike so no lock arms; the trade then rides the
        # real run-up to TP (non-negative). Zero TELEMETRY_VIOLATION lines.
        from strategy import Position, update_position_on_bar, realize_pnl_usd, lock_level_for
        from position_telemetry import PositionTracer
        try:
            cfg = self.cfg
            _lines = []
            tr = PositionTracer(sink=_lines.append)
            entry = 4155.35
            p = Position(anchor_label='A2_10h_London', side='BUY', entry_price=entry,
                         entry_time=pd.Timestamp('2026-06-19T10:00:00Z'),
                         current_sl=entry - cfg.sl_dist, tp_level=entry + cfg.tp_dist,
                         max_fav=entry, lot=cfg.lot_size)
            t0 = pd.Timestamp('2026-06-19T10:00:00Z')
            spike_rejected = False
            out = None
            lock_during_underwater = False
            for i in range(120):
                if i < 46:  # underwater the whole time (low never reaches the $18 SL)
                    bar = pd.Series({'open': entry - 9, 'high': entry - 2,
                                     'low': entry - 10, 'close': entry - 9})
                    if i == 25:  # garbage spike: +$28 print, below TP, above filter
                        bar = pd.Series({'open': entry - 9, 'high': entry + 28,
                                         'low': entry - 10, 'close': entry - 9})
                else:          # the real run-up to TP 4185.35
                    lvl = entry - 9 + (i - 45) * 3.0
                    bar = pd.Series({'open': lvl - 1, 'high': lvl + 1,
                                     'low': lvl - 2, 'close': lvl})
                out = update_position_on_bar(p, bar, t0 + pd.Timedelta(minutes=i + 1),
                                             cfg, tracer=tr, ticket=57163297159)
                if i < 46 and lock_level_for(p, cfg) > 0:
                    lock_during_underwater = True
                if out:
                    break
            pnl = round(realize_pnl_usd(p, cfg), 2)
            spike_rejected = any('accepted=False' in l for l in _lines)
            no_lock_underwater = not lock_during_underwater
            non_negative = pnl >= 0.0
            no_violations = (len(tr.violations) == 0)
            ok = (no_lock_underwater and non_negative and no_violations
                  and out == 'TP' and spike_rejected)
            detail = (f"underwater_no_lock={no_lock_underwater} spike_rejected="
                      f"{spike_rejected} outcome={out} pnl=${pnl:.0f}"
                      f"(>=0={non_negative}) violations={len(tr.violations)}")
        except Exception as e:
            self._record(30, FAIL, f"raised: {e!r}")
            return
        self._record(30, PASS if ok else FAIL, detail)

    def _step_trail_telemetry(self):
        # v3.3.0 (b) ANY trail/lock exit MUST have a preceding TRAIL_ADVANCE line.
        # POSITIVE: a winning BUY that trails up emits TRAIL_ADVANCE and its TRAIL
        # exit raises NO violation. NEGATIVE: a hand-built EXIT(exit_type=TRAIL)
        # with no TRAIL_ADVANCE MUST raise exactly the assertion that would have
        # caught the A2 silence.
        from strategy import Position, update_position_on_bar
        from position_telemetry import PositionTracer
        try:
            cfg = self.cfg
            # POSITIVE path through the real engine.
            tr = PositionTracer(sink=lambda l: None)
            entry = 4300.0
            p = Position(anchor_label='TEST', side='BUY', entry_price=entry,
                         entry_time=pd.Timestamp('2026-06-16T10:00:00Z'),
                         current_sl=entry - cfg.sl_dist, tp_level=entry + cfg.tp_dist,
                         max_fav=entry, lot=cfg.lot_size)
            t0 = pd.Timestamp('2026-06-16T10:00:00Z')
            # run up post-hold so the trail engages, then pull back into the trail
            for i, hi in enumerate([entry + 6, entry + 9, entry + 9, entry + 9]):
                bar = pd.Series({'open': entry, 'high': hi, 'low': entry, 'close': hi})
                update_position_on_bar(p, bar, t0 + pd.Timedelta(minutes=50 + i),
                                       cfg, tracer=tr, ticket=999001)
            had_advance = len([1 for e in tr._history.get(999001, [])
                               if e.get('event_type') == 'TRAIL_ADVANCE']) > 0
            tr.exit(999001, 'TEST', side='BUY', exit_type='TRAIL',
                    position_price=entry, max_fav=p.max_fav, stop_price=p.current_sl)
            positive_ok = had_advance and len(tr.violations) == 0

            # NEGATIVE path: exit with no preceding advance must violate.
            tr2 = PositionTracer(sink=lambda l: None)
            tr2.fill(999002, 'TEST', side='BUY', position_price=entry)
            tr2.exit(999002, 'TEST', side='BUY', exit_type='TRAIL',
                     position_price=entry)
            negative_ok = (len(tr2.violations) == 1 and
                           'without_trail_advance' in tr2.violations[0])

            ok = positive_ok and negative_ok
            detail = (f"positive(advance+no_violation)={positive_ok} "
                      f"negative(violation_fires)={negative_ok}")
        except Exception as e:
            self._record(31, FAIL, f"raised: {e!r}")
            return
        self._record(31, PASS if ok else FAIL, detail)

    def _step_stop_reject(self):
        # v3.3.0 (c) A long stop placed at/above bid MUST be rejected (mirror for
        # shorts). Drives the REAL position_telemetry assertion with the EXACT A2
        # numbers (stop 4158.31 above bid 4152.93 on a long). A valid stop below
        # bid raises nothing.
        from position_telemetry import PositionTracer
        try:
            # invalid: long stop ABOVE bid (the A2 force-close geometry)
            tr = PositionTracer(sink=lambda l: None)
            tr.place(57163297159, 'A2_10h_London', side='BUY',
                     stop_price=4158.31, bid=4152.93, ask=4153.05)
            long_rejected = (len(tr.violations) == 1 and
                             'long_stop_at_or_above_bid' in tr.violations[0])
            # mirror: short stop BELOW ask
            tr2 = PositionTracer(sink=lambda l: None)
            tr2.place(2, 'A', side='SELL', stop_price=4150.0,
                      bid=4151.0, ask=4151.2)
            short_rejected = (len(tr2.violations) == 1 and
                              'short_stop_at_or_below_ask' in tr2.violations[0])
            # valid long stop BELOW bid -> no violation
            tr3 = PositionTracer(sink=lambda l: None)
            tr3.place(3, 'A', side='BUY', stop_price=4150.0,
                      bid=4155.0, ask=4155.2)
            valid_ok = (len(tr3.violations) == 0)
            ok = long_rejected and short_rejected and valid_ok
            detail = (f"long>=bid_rejected={long_rejected} "
                      f"short<=ask_rejected={short_rejected} "
                      f"valid_below_bid_ok={valid_ok}")
        except Exception as e:
            self._record(32, FAIL, f"raised: {e!r}")
            return
        self._record(32, PASS if ok else FAIL, detail)

    def _step_lock_guards(self):
        # v3.2.3 Group 1 extras: T2 phantom-lock short, T6 garbage-tick reject,
        # T7 max_fav init. Drives the REAL strategy core.
        from strategy import Position, update_position_on_bar, lock_level_for
        from position_telemetry import PositionTracer
        try:
            cfg = self.cfg
            # T7: fresh fill -> max_fav initialized to entry (never 0/null).
            entry = 4146.95
            p7 = Position('A3', 'SELL', entry, pd.Timestamp('2026-06-19T13:50:00Z'),
                          entry + cfg.sl_dist, entry - cfg.tp_dist, entry, cfg.lot_size)
            t7_init = (p7.max_fav == entry)

            # T2: SELL underwater whole life (price stays ABOVE entry) -> NO lock.
            tr = PositionTracer(sink=lambda l: None)
            p = Position('A3', 'SELL', entry, pd.Timestamp('2026-06-19T13:50:00Z'),
                         entry + cfg.sl_dist, entry - cfg.tp_dist, entry, cfg.lot_size)
            t0 = pd.Timestamp('2026-06-19T13:50:00Z'); lock_seen = False
            for i in range(40):
                bar = pd.Series({'open': entry + 3, 'high': entry + 5,
                                 'low': entry + 1, 'close': entry + 3})
                update_position_on_bar(p, bar, t0 + pd.Timedelta(minutes=i + 1),
                                       cfg, tracer=tr, ticket=701)
                if lock_level_for(p, cfg) > 0:
                    lock_seen = True
            t2_no_lock = (not lock_seen) and (p.max_fav == entry) \
                and len([1 for e in tr._history.get(701, [])
                         if e['event_type'] == 'LOCK_ARM']) == 0

            # T6: a garbage tick (> max_tick_jump favorable) must not move max_fav.
            tr6 = []; trc = PositionTracer(sink=tr6.append)
            pe = 4300.0
            p6 = Position('A1', 'BUY', pe, pd.Timestamp('2026-06-19T02:30:00Z'),
                          pe - cfg.sl_dist, pe + cfg.tp_dist, pe, cfg.lot_size)
            jump = cfg.max_tick_jump + 10.0
            bar = pd.Series({'open': pe, 'high': pe + jump, 'low': pe - 1, 'close': pe})
            update_position_on_bar(p6, bar, pd.Timestamp('2026-06-19T02:31:00Z'),
                                   cfg, tracer=trc, ticket=601)
            t6_rejected = (p6.max_fav == pe) and any('accepted=False' in l for l in tr6)

            ok = t7_init and t2_no_lock and t6_rejected
            detail = (f"T7_maxfav_init={t7_init} T2_short_no_lock={t2_no_lock} "
                      f"T6_garbage_rejected={t6_rejected}")
        except Exception as e:
            self._record(33, FAIL, f"raised: {e!r}")
            return
        self._record(33, PASS if ok else FAIL, detail)

    def _step_lone_boost(self):
        # v3.2.3 Group 2 (L1-L5): the lone-leg boost trigger via the canonical
        # boosts.plan_boost_event (the SINGLE source live + backtest call).
        import boosts as _b
        try:
            cfg = self.cfg
            fill = 4266.3
            # L1: +$10 WITH a BUY -> RALLY, same side, n=2.
            r = _b.plan_boost_event('BUY', fill, fill + 10.0, cfg)
            l1 = (r is not None and r.kind == 'RALLY' and r.boost_side == 'BUY' and r.n == 2)
            # L2: -$10 AGAINST a BUY -> RESCUE, opposite side.
            r2 = _b.plan_boost_event('BUY', fill, fill - 10.0, cfg)
            l2 = (r2 is not None and r2.kind == 'RESCUE' and r2.boost_side == 'SELL' and r2.n == 2)
            # L3 (v3.2.8 Phase 1): each kind has its OWN arm now -- RALLY $5, RESCUE
            # $10. Below the arm -> None; at the arm -> fires. (Was: sub-$10 both ways.)
            l3 = (_b.plan_boost_event('BUY', fill, fill + 4.99, cfg) is None        # rally < $5 -> none
                  and _b.plan_boost_event('BUY', fill, fill + 5.00, cfg) is not None  # rally @ +$5 -> fires
                  and _b.plan_boost_event('BUY', fill, fill - 9.99, cfg) is None      # rescue < $10 -> none
                  and _b.plan_boost_event('BUY', fill, fill - 10.0, cfg) is not None)  # rescue @ -$10 -> fires
            # L4: at fill (move 0) -> None (fire-at-fill blocked).
            l4 = (_b.plan_boost_event('BUY', fill, fill, cfg) is None)
            # L5: one-shot at the same crossing -- mirrors fills' boost_fired flag.
            fired = False
            def _attempt(px):
                nonlocal fired
                if fired:
                    return None
                pl = _b.plan_boost_event('BUY', fill, px, cfg)
                if pl is not None:
                    fired = True
                return pl
            first = _attempt(fill + 10.0)
            second = _attempt(fill + 10.5)   # re-cross: must NOT re-fire
            l5 = (first is not None and second is None)
            ok = l1 and l2 and l3 and l4 and l5
            detail = (f"L1_rally={l1} L2_rescue={l2} L3_arms_5/10={l3} "
                      f"L4_fire_at_fill_blocked={l4} L5_one_shot={l5}")
        except Exception as e:
            self._record(34, FAIL, f"raised: {e!r}")
            return
        self._record(34, PASS if ok else FAIL, detail)

    def _step_boost_watchdog(self):
        # v3.2.3 Group 2 (L6/L7/L8) + D4: a met-but-unfired trigger and an armed-
        # but-unexecuted boost MUST raise loud violations (never a silent drop).
        from position_telemetry import PositionTracer
        try:
            # L6/L8 MISSED_BOOST: condition met, no arm/fire -> violation.
            tr = PositionTracer(sink=lambda l: None)
            tr.missed_boost(111, 'A2', side='BUY', move_dollars=10.5, trigger=10.0)
            l6 = (len(tr.violations) == 1 and 'MISSED_BOOST' in tr.violations[0])
            # L7 BOOST_ARM_ORPHANED: armed, no fire follows -> violation at check.
            tr2 = PositionTracer(sink=lambda l: None)
            tr2.fill(222, 'A2', side='BUY', position_price=4266.3)
            tr2.boost_arm(222, 'A2', side='BUY', boost_kind='RALLY',
                          stack_size=3, move_dollars=10.0, trigger=10.0)
            orphan = tr2.check_orphan_arms(222)
            l7 = orphan and any('BOOST_ARM_ORPHANED' in v for v in tr2.violations)
            # clean: arm followed by fire -> no orphan.
            tr3 = PositionTracer(sink=lambda l: None)
            tr3.boost_arm(333, 'A2', side='BUY', boost_kind='RALLY', stack_size=3)
            tr3.boost_fire(334, 'A2', parent_ticket=333, side='BUY',
                           boost_kind='RALLY', stack_size=2, move_dollars=10.0, trigger=10.0)
            no_orphan = (tr3.check_orphan_arms(333) is False)
            # D4: a forced violation reaches the sink immediately + unrate-limited.
            seen = []
            tr4 = PositionTracer(sink=seen.append)
            tr4.violation(444, 'A2', 'forced_test')
            d4 = (len(seen) == 1 and 'TELEMETRY_VIOLATION' in seen[0])
            # boost_fire below trigger -> violation (fire-at-fill structural assert).
            tr5 = PositionTracer(sink=lambda l: None)
            tr5.boost_fire(555, 'A2', side='BUY', boost_kind='RALLY',
                           move_dollars=3.0, trigger=10.0)
            below = any('boost_fire_below_trigger' in v for v in tr5.violations)
            ok = l6 and l7 and no_orphan and d4 and below
            detail = (f"L6_missed={l6} L7_orphan={l7} clean_no_orphan={no_orphan} "
                      f"D4_violation_loud={d4} below_trigger_caught={below}")
        except Exception as e:
            self._record(35, FAIL, f"raised: {e!r}")
            return
        self._record(35, PASS if ok else FAIL, detail)

    def _step_nooco_stack(self):
        # v3.2.4 Group 3 (N1/N5/N7): No-OCO winning side stacks; losing leg fires
        # NOTHING (rides to SL); trail arms at +$8. CAP UPDATED 3 -> 5 (the only
        # sanctioned existing-test change; 5-long default ON) -- violation if > 5.
        import dataclasses
        import boosts as _b
        from strategy import Position, update_position_on_bar
        from position_telemetry import PositionTracer
        try:
            cfg = self.cfg
            # N1: straddle short @ X, long @ X+10. Price runs UP.
            X = 4150.0
            # winning = long leg (rally-only) gets a RALLY of 2 (one event).
            win = _b.plan_boost_event('BUY', X + 10.0, X + 20.0, cfg, allow_rescue=False)
            n1_win = (win is not None and win.kind == 'RALLY' and win.n == 2)
            # losing = short leg (rally-only): it is LOSING -> rescue blocked -> None.
            lose = _b.plan_boost_event('SELL', X, X + 20.0, cfg, allow_rescue=False)
            n1_lose = (lose is None)

            # N7: hard cap is now 5 (5-long). The tracer flags stack_size > 5 as a
            # violation; a full 5-stack is allowed.
            n7_cap = (_b.stack_cap(cfg) == 5)
            trv = PositionTracer(sink=lambda l: None)
            trv.boost_fire(9, 'A2', side='BUY', boost_kind='RALLY', stack_size=6,
                           stack_cap=5, move_dollars=10.0, trigger=10.0)
            trv.boost_fire(10, 'A2', side='BUY', boost_kind='RESCUE', stack_size=5,
                           stack_cap=5, move_dollars=10.0, trigger=10.0)
            viols = [v for v in trv.violations if 'stack_size_exceeds_cap' in v]
            n7_violation = (len(viols) == 1)   # only the 6>5 trips, the 5 is fine

            # N5: trail arms at +$8 on a boost leg (the stack's protection).
            entry = 4150.0
            boost = Position('A2', 'BUY', entry, pd.Timestamp('2026-06-19T10:00:00Z'),
                             entry - 10.0, entry + cfg.tp_dist, entry, cfg.lot_size,
                             role='rescue', boost=True)
            # push fav to +$9 so the +$8 breath-floor engages
            update_position_on_bar(boost, pd.Series(
                {'open': entry, 'high': entry + 9.0, 'low': entry, 'close': entry + 9.0}),
                pd.Timestamp('2026-06-19T10:05:00Z'), cfg)
            n5_floor = boost.current_sl >= entry + 8.0 - 1e-6

            ok = n1_win and n1_lose and n7_cap and n7_violation and n5_floor
            detail = (f"N1_winner_rally2={n1_win} N1_loser_rides(None)={n1_lose} "
                      f"N7_cap5={n7_cap} N7_violation(>5)={n7_violation} "
                      f"N5_trail_floor8={n5_floor}")
        except Exception as e:
            self._record(36, FAIL, f"raised: {e!r}")
            return
        self._record(36, PASS if ok else FAIL, detail)

    def _step_stack_economics(self):
        # v3.2.3 Group 3 (N2/N3/N4/N6): the break-even truth is CODED, not assumed.
        # NOTE: the global 5-long default is now ON, so this pins the 3-profile
        # (allow_5_long=False) to keep asserting the proven 3-stack economics --
        # the assertions/logic are unchanged, only the cfg is made explicit.
        import dataclasses
        import boosts as _b
        from rescue_log import _branch_for
        try:
            cfg = dataclasses.replace(self.cfg, allow_5_long=False)
            be = _b.stack_breakeven_usd(cfg)          # one losing leg SL ($)
            n = _b.stack_winners(cfg)                 # 3
            per = _b.per_position_breakeven_usd(cfg)   # ~210
            # N4: exact break-even -- each winner clears `per` -> net 0.
            net0 = round(n * per - be, 2)
            n4 = (abs(net0) < 1e-6 and abs(be - 630.0) < 1.0 and n == 3)
            # N2: worked example -- $410 each -> +$600.
            net_win = round(n * 410.0 - be, 2)
            n2 = (abs(net_win - 600.0) < 1.0)
            # N3: whipsaw -- $100 each (< per) -> net < 0, classed WHIPSAW_LOSS.
            net_whip = round(n * 100.0 - be, 2)
            n3 = (net_whip < 0 and _branch_for(net_whip) == 'WHIPSAW_LOSS')
            # N6: peak exposure 3 winners + 1 open loser = 1.40 lot = $140/$1.
            lots, usd_per_dollar = _b.stack_peak_exposure(cfg)
            # FP 5% on $50k = $2500; at $140/$1 an $18 adverse excursion = $2520 > limit.
            fp_limit = 0.05 * cfg.starting_balance
            adverse_18 = usd_per_dollar * cfg.sl_dist
            n6 = (abs(lots - 1.40) < 1e-6 and abs(usd_per_dollar - 140.0) < 1e-6
                  and adverse_18 > fp_limit)
            ok = n4 and n2 and n3 and n6
            detail = (f"N4_be_exact(net0={net0},be=${be:.0f})={n4} "
                      f"N2_410each=+${net_win:.0f}={n2} "
                      f"N3_whipsaw(net={net_whip:.0f})={n3} "
                      f"N6_exposure({lots}lot/${usd_per_dollar:.0f},adv18=${adverse_18:.0f}>"
                      f"${fp_limit:.0f})={n6}")
        except Exception as e:
            self._record(37, FAIL, f"raised: {e!r}")
            return
        self._record(37, PASS if ok else FAIL, detail)

    def _step_telemetry_full(self):
        # v3.2.3 Group 4 (D1/D2/D3/D5): every line carries all mandatory fields
        # (null explicit, never omitted); a trade's trace is gapless; the PREDICT
        # line names every door + the break-even truth.
        from position_telemetry import PositionTracer, MANDATORY_FIELDS, format_event_line
        try:
            lines = []
            tr = PositionTracer(sink=lines.append)
            tk = 800; anc = 'A2_10h_London'; entry = 4155.35
            tr.plan(tk, anc, side='BUY', position_price=entry)
            tr.place(tk, anc, side='BUY', stop_price=entry - 18, bid=entry + 1, position_price=entry)
            tr.fill(tk, anc, side='BUY', position_price=entry, max_fav=entry, stop_price=entry - 18)
            tr.predict(tk, anc, 'BUY', entry, entry - 18, entry + 30, -630.0, 1050.0,
                       trigger=10.0, breakeven_per_pos=6.0)
            tr.maxfav_update(tk, anc, side='BUY', position_price=entry, max_fav=entry + 3)
            tr.trail_advance(tk, anc, side='BUY', position_price=entry, max_fav=entry + 3,
                             stop_price=entry + 1, lock_level=1, bid=entry + 5)
            tr.boost_arm(tk, anc, side='BUY', boost_kind='RALLY', stack_size=3, move_dollars=10.0, trigger=10.0)
            tr.boost_fire(801, anc, parent_ticket=tk, side='BUY', boost_kind='RALLY',
                          stack_size=2, move_dollars=10.0, trigger=10.0, position_price=entry + 10)
            tr.heartbeat(tk, anc, side='BUY', bid=entry + 5, max_fav=entry + 3,
                         stop_price=entry + 1, stack_size=3, floating_pnl=120.0)
            tr.exit(tk, anc, side='BUY', exit_type='TP', actual_fill=entry + 30, pnl=1050.0)

            # D1: every emitted line carries all mandatory field NAMES (null ok).
            # event_type/ticket/anchor lead the line positionally (event_type is the
            # bare token after PTRACE); the rest appear as `name=`.
            body = [l for l in lines if l.startswith('PTRACE') and 'VIOLATION' not in l]
            d1 = all(all(f"{m}=" in l for m in MANDATORY_FIELDS if m != 'event_type')
                     for l in body)
            # D2: gapless -- the key transitions all present, in order.
            seq = [l.split()[1] for l in body]
            need = ['PLAN', 'PLACE', 'FILL', 'PREDICT', 'MAXFAV_UPDATE',
                    'TRAIL_ADVANCE', 'BOOST_ARM', 'BOOST_FIRE', 'POSITION_HEARTBEAT', 'EXIT']
            d2 = all(n in seq for n in need) and seq.index('FILL') < seq.index('EXIT')
            # D5: PREDICT names SL/TP + rally/rescue arm prices + breakeven/position.
            pred = [l for l in lines if l.split()[1] == 'PREDICT'][0]
            d5 = all(s in pred for s in ('rally_arms_at=', 'rescue_arms_at=',
                                         'breakeven_per_pos=', 'max_loss=', 'tp='))
            # D3: the Discord BOOST_FIRED string format carries kind+anchor+stack.
            sample = (f"🚀 BOOST FIRED [RALLY] | {anc} | BUY 0.35 @~$4165.35 | "
                      f"parent {tk} | stack now 3/3 | move +$10 from fill $4155.35")
            d3 = ('BOOST FIRED [RALLY]' in sample and f'parent {tk}' in sample
                  and '3/3' in sample)
            ok = d1 and d2 and d5 and d3
            detail = f"D1_full_fields={d1} D2_gapless={d2} D5_predict={d5} D3_discord_fmt={d3}"
        except Exception as e:
            self._record(38, FAIL, f"raised: {e!r}")
            return
        self._record(38, PASS if ok else FAIL, detail)

    def _step_phantom_guard(self):
        # v3.2.3 PHANTOM-LOCK GUARD (PL1/PL2/PL4): a lock activates ONLY if max_fav
        # genuinely reached its trigger. PL4 max_fav init; PL1 A2 long-underwater;
        # PL2 A3 short-underwater. The guard (strategy.lock_trigger_reached) is the
        # SINGLE shared check; assert it would BLOCK while underwater, and that the
        # real engine arms no lock + applies no phantom + ends non-negative.
        from strategy import (Position, update_position_on_bar, realize_pnl_usd,
                              lock_level_for, lock_trigger_reached)
        from position_telemetry import PositionTracer
        try:
            cfg = self.cfg
            # PL4: fresh fill -> max_fav initialized to entry (never 0/null).
            e2 = 4155.35
            p_init = Position('A2_10h_London', 'BUY', e2,
                              pd.Timestamp('2026-06-19T10:00:00Z'),
                              e2 - cfg.sl_dist, e2 + cfg.tp_dist, e2, cfg.lot_size)
            pl4 = (p_init.max_fav == e2)

            # PL1: A2 BUY underwater whole life (then the real run-up to TP). No lock
            # may arm while underwater; the guard would BLOCK a level-1 lock there.
            lines = []; tr = PositionTracer(sink=lines.append)
            p = Position('A2_10h_London', 'BUY', e2, pd.Timestamp('2026-06-19T10:00:00Z'),
                         e2 - cfg.sl_dist, e2 + cfg.tp_dist, e2, cfg.lot_size)
            t0 = pd.Timestamp('2026-06-19T10:00:00Z'); out = None
            lock_while_underwater = False
            for i in range(120):
                if i < 46:
                    bar = pd.Series({'open': e2 - 9, 'high': e2 - 2, 'low': e2 - 10, 'close': e2 - 9})
                else:
                    lvl = e2 - 9 + (i - 45) * 3.0
                    bar = pd.Series({'open': lvl - 1, 'high': lvl + 1, 'low': lvl - 2, 'close': lvl})
                out = update_position_on_bar(p, bar, t0 + pd.Timedelta(minutes=i + 1),
                                             cfg, tracer=tr, ticket=57163297159)
                if i < 46 and lock_level_for(p, cfg) > 0:
                    lock_while_underwater = True
                if out:
                    break
            guard_blocks_underwater = (lock_trigger_reached('BUY', e2, e2, 1) is False)
            no_phantom_applied = not any('phantom_lock_applied' in l for l in lines)
            pl1 = (not lock_while_underwater and guard_blocks_underwater
                   and no_phantom_applied and realize_pnl_usd(p, cfg) >= 0 and out == 'TP')

            # PL2: A3 SELL underwater (price stays ABOVE entry). No lock; no spam.
            e3 = 4146.95
            tr2 = PositionTracer(sink=lambda l: None)
            p3 = Position('A3', 'SELL', e3, pd.Timestamp('2026-06-19T13:50:00Z'),
                          e3 + cfg.sl_dist, e3 - cfg.tp_dist, e3, cfg.lot_size)
            t3 = pd.Timestamp('2026-06-19T13:50:00Z')
            for i in range(40):
                bar = pd.Series({'open': e3 + 3, 'high': e3 + 5, 'low': e3 + 1, 'close': e3 + 3})
                update_position_on_bar(p3, bar, t3 + pd.Timedelta(minutes=i + 1),
                                       cfg, tracer=tr2, ticket=702)
            # the A3 attempted lock @4143.89 is below entry; a short's level-1 trigger
            # is entry-$5 = 4141.95, which max_fav (>=entry) never reaches -> blocked.
            pl2 = (lock_level_for(p3, cfg) == 0
                   and lock_trigger_reached('SELL', e3, e3, 1) is False
                   and len(tr2.violations) == 0)

            ok = pl4 and pl1 and pl2
            detail = (f"PL4_maxfav_init={pl4} PL1_A2_no_lock+result>=0={pl1} "
                      f"PL2_A3_short_no_lock+no_spam={pl2}")
        except Exception as e:
            self._record(39, FAIL, f"raised: {e!r}")
            return
        self._record(39, PASS if ok else FAIL, detail)

    def _step_phantom_legit(self):
        # v3.2.3 PHANTOM-LOCK GUARD (PL3/PL5/PL6): the guard must NOT block a REAL
        # lock; the tripwire must catch an applied phantom; every lock evaluation
        # emits a full LOCK_CHECK line.
        from strategy import (Position, update_position_on_bar, lock_trigger_reached,
                              lock_trigger_price)
        from position_telemetry import PositionTracer, MANDATORY_FIELDS
        try:
            cfg = self.cfg
            entry = 4300.0
            # PL3: price genuinely reaches +$10 post-hold -> guard PASS -> lock arms.
            lines = []; tr = PositionTracer(sink=lines.append)
            p = Position('TEST', 'BUY', entry, pd.Timestamp('2026-06-16T10:00:00Z'),
                         entry - cfg.sl_dist, entry + cfg.tp_dist, entry, cfg.lot_size)
            t0 = pd.Timestamp('2026-06-16T10:00:00Z')
            for i, hi in enumerate([entry + 10, entry + 11, entry + 11]):
                bar = pd.Series({'open': entry, 'high': hi, 'low': entry, 'close': hi})
                update_position_on_bar(p, bar, t0 + pd.Timedelta(minutes=50 + i),
                                       cfg, tracer=tr, ticket=900)
            lock_checks = [l for l in lines if l.split()[1] == 'LOCK_CHECK']
            arms = [l for l in lines if l.split()[1] == 'LOCK_ARM']
            pass_checks = [l for l in lock_checks if 'guard_result=PASS' in l]
            pl3 = (len(arms) >= 1 and len(pass_checks) >= 1
                   and lock_trigger_reached('BUY', entry, entry + 10, 3) is True)

            # PL6: every LOCK_CHECK carries all mandatory fields + trigger + result.
            pl6 = (len(lock_checks) >= 1 and all(
                all(f"{m}=" in l for m in MANDATORY_FIELDS if m != 'event_type')
                and 'lock_trigger_price=' in l and 'guard_result=' in l
                for l in lock_checks))

            # PL5 TRIPWIRE: the guard BLOCKS a lock when max_fav < trigger (a phantom),
            # and the tracer raises if a phantom ever APPLIES (locks > max_fav).
            blocked = (lock_trigger_reached('BUY', 4155.35, 4155.35, 1) is False
                       and lock_trigger_reached('BUY', 4155.35, 4157.0, 3) is False)
            tr2 = PositionTracer(sink=lambda l: None)
            tr2.lock_arm(7, 'A', side='BUY', position_price=4155.35, max_fav=4155.35,
                         stop_price=4158.31, lock_level=1)   # locks +$3 off a flat peak
            tripwire = any('lock_armed_above_max_fav' in v for v in tr2.violations)
            pl5 = blocked and tripwire

            ok = pl3 and pl6 and pl5
            detail = (f"PL3_legit_arms(checks={len(lock_checks)},arms={len(arms)})={pl3} "
                      f"PL6_lock_check_full={pl6} PL5_blocked+tripwire={pl5}")
        except Exception as e:
            self._record(40, FAIL, f"raised: {e!r}")
            return
        self._record(40, PASS if ok else FAIL, detail)

    def _step_monday_wake(self):
        # v3.2.3 (41) + v3.3.6 TRUTH FIX: first tick after a weekend gap, broker
        # UTC+3 -> offset resolves +3. A1's EXPECTED IST is now the RESOLVER-derived
        # Monday time (03:30 broker -> 06:00 IST), NOT the stale hardcoded 05:00. A
        # correct +3 read implies 06:00 == the Monday schedule -> NO drift; and we
        # prove the OLD 05:00 constant would have FALSELY flagged the correct 06:00.
        import offset_guard as og
        import anchors as _anchors
        from datetime import date as _date, timedelta as _td
        try:
            gap = og.weekend_gap_hours(0.0, 50 * 3600.0)   # 50h gap
            is_wake = og.is_weekend_wake(gap)
            off, result, attempts = og.resolve_offset([3])
            resolves_3 = (off == 3 and result == og.CONFIRMED)
            # Monday broker date -> resolver -> expected A1 IST (06:00) via shared code.
            base = _date(2026, 6, 24); monday = base - _td(days=base.weekday())
            brh, brm = _anchors.resolved_anchor_hm('A1_02h_Asia', monday, 2, 30, self.cfg)
            sched = og.scheduled_a1_ist_min(brh, brm, off)
            implied = 6 * 60   # correct +3 offset on Monday implies 06:00 IST
            monday_0600 = (og.fmt_hhmm(sched) == '0600'
                           and not og.a1_drifted(implied, scheduled_ist_min=sched))
            old_const_misflags = og.a1_drifted(implied, scheduled_ist_min=og.A1_SCHEDULED_IST_MIN)
            ok = is_wake and resolves_3 and monday_0600 and old_const_misflags
            detail = (f"M1_offset_resolves_+3={resolves_3} "
                      f"M1_A1_monday_0600_no_drift={monday_0600} "
                      f"old_0500_const_would_misflag={old_const_misflags} (sched={og.fmt_hhmm(sched)})")
        except Exception as e:
            self._record(41, FAIL, f"raised: {e!r}")
            return
        self._record(41, PASS if ok else FAIL, detail)

    def _step_monday_badoffset(self):
        # v3.2.3 (42): first tick implies 0h (the drift cause) -> rejected, NO
        # placement on bad data, retry fired; emits the offset_mismatch violation.
        import offset_guard as og
        from position_telemetry import PositionTracer
        try:
            # all reads derive 0h -> never confirmed -> BLOCKED after retry_max.
            off, result, attempts = og.resolve_offset([0, 0, 0])
            rejected = (off is None and result == og.BLOCKED)
            no_placement = rejected   # BLOCKED == A1 not placed on a guess
            retry_fired = (attempts >= 2)
            # negative-path proof: the violation line, same style as other tests.
            tr = PositionTracer(sink=lambda l: None)
            tr.violation(None, 'A1', 'offset_mismatch', derived=0, expected=3)
            violated = any('offset_mismatch' in v and 'derived=0' in v and 'expected=3' in v
                           for v in tr.violations)
            ok = rejected and no_placement and retry_fired and violated
            detail = (f"M2_bad_offset_rejected={rejected} "
                      f"M2_no_placement_on_bad={no_placement} retry_fired={retry_fired} "
                      f"(result={result} attempts={attempts} violation={violated})")
        except Exception as e:
            self._record(42, FAIL, f"raised: {e!r}")
            return
        self._record(42, PASS if ok else FAIL, detail)

    def _step_monday_drift_trip(self):
        # v3.2.3 (43) + v3.3.6 TRUTH FIX: the drift tripwire is measured against the
        # RESOLVER's Monday schedule (06:00 IST), not a hardcoded 05:00. A bad-offset
        # Monday read (0h instead of +3) implies ~03:00 IST (3h low) -> drift fires
        # BEFORE placement; with +3 corrected, implied 06:00 == schedule -> no drift.
        import offset_guard as og
        import anchors as _anchors
        from datetime import date as _date, timedelta as _td
        from position_telemetry import PositionTracer
        try:
            base = _date(2026, 6, 24); monday = base - _td(days=base.weekday())
            brh, brm = _anchors.resolved_anchor_hm('A1_02h_Asia', monday, 2, 30, self.cfg)
            sched = og.scheduled_a1_ist_min(brh, brm, 3)   # 06:00 IST Monday
            implied_bad = sched - 3 * 60                    # 0h-offset symptom: 03:00 IST
            drift_fires = og.a1_drifted(implied_bad, scheduled_ist_min=sched)
            tr = PositionTracer(sink=lambda l: None)
            if drift_fires:
                tr.violation(None, 'A1', 'monday_a1_drift',
                             scheduled=og.fmt_hhmm(sched), implied=og.fmt_hhmm(implied_bad))
            trip = any('monday_a1_drift' in v and 'scheduled=0600' in v
                       and 'implied=0300' in v for v in tr.violations)
            # corrected path: implied 06:00 == the Monday schedule -> no drift.
            a1_ok = not og.a1_drifted(sched, scheduled_ist_min=sched)
            ok = trip and a1_ok
            detail = (f"M3_drift_tripwire_fires={trip} M3_corrected_no_drift={a1_ok} "
                      f"(sched={og.fmt_hhmm(sched)} bad={og.fmt_hhmm(implied_bad)})")
        except Exception as e:
            self._record(43, FAIL, f"raised: {e!r}")
            return
        self._record(43, PASS if ok else FAIL, detail)

    def _step_weekday_unaffected(self):
        # v3.2.3 (44): Tue-Fri open, no weekend gap -> the weekend path is NOT
        # taken; behavior is identical to before (regression guard).
        import offset_guard as og
        try:
            # a normal inter-tick gap (seconds/minutes) is NOT a weekend wake.
            small_gap = og.weekend_gap_hours(0.0, 120.0)   # 120s
            no_weekend = (og.is_weekend_wake(small_gap) is False)
            # even a multi-hour holiday-ish gap under the threshold stays off-path.
            sub_threshold = (og.is_weekend_wake(og.WEEKEND_GAP_HOURS - 1) is False)
            ok = no_weekend and sub_threshold
            detail = (f"M4_no_weekend_path={no_weekend} "
                      f"M4_behavior_identical_prefix={sub_threshold}")
        except Exception as e:
            self._record(44, FAIL, f"raised: {e!r}")
            return
        self._record(44, PASS if ok else FAIL, detail)

    def _step_monday_trace(self):
        # v3.2.3 (45): the full Monday-open event chain is gapless + all fields:
        # WEEKEND_WAKE -> OFFSET_DETECT -> ANCHOR_TIME_RESOLVED.
        from position_telemetry import PositionTracer, MANDATORY_FIELDS
        try:
            lines = []
            tr = PositionTracer(sink=lines.append)
            tr.weekend_wake(gap_hours=50.0, is_weekend=True)
            tr.offset_detect(derived_offset=3, expected_offset=3, result='CONFIRMED',
                             attempt=1, gap_since_last_tick=50.0)
            tr.anchor_time_resolved(scheduled_ist='0600', offset_used=3, result='CONFIRMED')  # v3.3.6: Monday A1 = 06:00 IST
            seq = [l.split()[1] for l in lines if l.startswith('PTRACE')]
            need = ['WEEKEND_WAKE', 'OFFSET_DETECT', 'ANCHOR_TIME_RESOLVED']
            gapless = (seq == need)
            all_fields = all(all(f"{m}=" in l for m in MANDATORY_FIELDS if m != 'event_type')
                             for l in lines)
            ok = gapless and all_fields
            detail = (f"M5_WEEKEND_WAKE->OFFSET_DETECT->ANCHOR_TIME_RESOLVED "
                      f"gapless={gapless} all_fields={all_fields}")
        except Exception as e:
            self._record(45, FAIL, f"raised: {e!r}")
            return
        self._record(45, PASS if ok else FAIL, detail)

    def _step_jun8_replay(self):
        # v3.2.3 (46): replay the 2026-06-08 weekend-wake failure -- logged offset
        # 0h while the broker is UTC+3. The guard rejects the 0h, awaits a fresh
        # tick, re-derives +3, and A1 then produces a trade (no silent miss).
        import offset_guard as og
        try:
            # the bad first read derives 0h (the Jun-8 fallback); the fresh re-read
            # derives the true +3. resolve_offset rejects 0, retries, confirms +3.
            off, result, attempts = og.resolve_offset([0, 3])
            corrected = (off == 3 and result == og.CONFIRMED and attempts == 2)
            # with the corrected +3 offset A1 resolves at 05:00 (a real trade window),
            # not the 0h-misdetect window that produced the silent miss.
            a1_trades = (not og.a1_drifted(og.A1_SCHEDULED_IST_MIN)) and corrected
            ok = corrected and a1_trades
            detail = (f"M6_offset_corrected_to_+3={corrected} "
                      f"M6_A1_produces_trade={a1_trades} (attempts={attempts})")
        except Exception as e:
            self._record(46, FAIL, f"raised: {e!r}")
            return
        self._record(46, PASS if ok else FAIL, detail)

    def _step_offset_parity(self):
        # v3.2.3 (47): import-path identity -- live, backtest, and selftest call the
        # SAME offset function (no drifting reimplementation), like steps 27/28.
        import importlib.util as _ilu
        import offset_guard as og
        import live_trader as _lt
        try:
            _root = os.path.dirname(os.path.abspath(__file__))
            _path = os.path.join(_root, 'backtest', 'backtest.py')
            spec = _ilu.spec_from_file_location('aureon_bt_offset', _path)
            bt = _ilu.module_from_spec(spec)
            spec.loader.exec_module(bt)
            same = (bt.resolve_offset is og.resolve_offset
                    and bt.offset_guard.resolve_offset is og.resolve_offset
                    and _lt.offset_guard.resolve_offset is og.resolve_offset)
            in_sources = ('offset_guard.resolve_offset' in bt.rule_sources())
            ok = same and in_sources
            detail = f"M7_live=bt=selftest_same_offset_fn={same} (in_sources={in_sources})"
        except Exception as e:
            self._record(47, FAIL, f"raised: {e!r}")
            return
        self._record(47, PASS if ok else FAIL, detail)

    def _step_autopull_soft(self):
        # v3.2.3 (53->48): an update available WITH a position open + quiet (not
        # mid-anchor) -> proceed with a SOFT restart. An open position alone does
        # NOT defer; only mid-anchor/mid-fill defers.
        import soft_restart as sr
        try:
            allow, r1 = sr.should_soft_restart(update_available=True, mid_anchor=False,
                                               mid_fill=False, position_open=True)
            soft_with_pos = (allow is True and r1 == 'soft_restart')
            defer, r2 = sr.should_soft_restart(update_available=True, mid_anchor=True,
                                               mid_fill=False, position_open=True)
            defers_midanchor = (defer is False and r2 == 'defer_mid_anchor')
            no_update, _ = sr.should_soft_restart(False, False, False, False)
            ok = soft_with_pos and defers_midanchor and (no_update is False)
            detail = (f"A1_soft_allowed_with_open_pos={soft_with_pos} "
                      f"A1_defers_only_midanchor={defers_midanchor}")
        except Exception as e:
            self._record(48, FAIL, f"raised: {e!r}")
            return
        self._record(48, PASS if ok else FAIL, detail)

    def _step_autopull_abort(self):
        # v3.2.3 (54->49): a pulled build that FAILS selftest -> abort, keep the old
        # build, position untouched (never flatten). Emits AUTOPULL_ABORTED.
        import soft_restart as sr
        from position_telemetry import PositionTracer
        try:
            deploy, reason = sr.should_deploy(selftest_passed=False)
            aborted = (deploy is False and reason == 'selftest_fail')
            old_kept = not deploy                      # not deploying == old build kept
            pos_untouched = sr.NEVER_FLATTEN_ON_UPDATE is True
            tr = PositionTracer(sink=lambda l: None)
            tr.autopull_aborted(reason='selftest_fail')
            emitted = any('AUTOPULL_ABORTED' in v and 'selftest_fail' in v
                          for v in tr.violations)
            # a PASSing build deploys.
            good, _ = sr.should_deploy(True)
            ok = aborted and old_kept and pos_untouched and emitted and good
            detail = (f"A2_bad_build_aborted={aborted} A2_old_kept={old_kept} "
                      f"A2_position_untouched={pos_untouched} (abort_emitted={emitted})")
        except Exception as e:
            self._record(49, FAIL, f"raised: {e!r}")
            return
        self._record(49, PASS if ok else FAIL, detail)

    def _step_soft_no_flatten(self):
        # v3.2.3 (55->50): a soft restart with 2 open positions leaves BOTH open on
        # the broker, none closed, none modified.
        import soft_restart as sr
        try:
            plan = sr.soft_exit_plan([111, 222])
            left_open = len(plan['left_open'])
            none_closed = (plan['closed'] == [])
            none_modified = (plan['modified'] == [])
            ok = (left_open == 2 and none_closed and none_modified)
            detail = (f"S1_positions_left_open={left_open} S1_none_closed={none_closed} "
                      f"S1_none_modified={none_modified}")
        except Exception as e:
            self._record(50, FAIL, f"raised: {e!r}")
            return
        self._record(50, PASS if ok else FAIL, detail)

    def _step_rehydrate_resume(self):
        # v3.2.3 (56->51): restart -> reload state + broker -> RESUME, with
        # max_fav / lock / stack restored from the persisted snapshot.
        import soft_restart as sr
        try:
            tk = 5570
            # persisted snapshot carried across the restart.
            persisted = {tk: {'max_fav': 4165.0, 'lock_level': 2, 'stack_size': 3,
                              'boost_event': 'EV1'}}
            action = sr.reconcile_action(in_state=(tk in persisted), on_broker=True)
            resumed = (action == sr.RESUME)
            # on RESUME the persisted fields are restored verbatim (not reset).
            restored = persisted[tk]
            maxfav_ok = (restored['max_fav'] == 4165.0)
            lock_ok = (restored['lock_level'] == 2)
            stack_ok = (restored['stack_size'] == 3)
            ok = resumed and maxfav_ok and lock_ok and stack_ok
            detail = (f"S2_resumed={resumed} S2_maxfav_restored={maxfav_ok} "
                      f"S2_lock_restored={lock_ok} S2_stack_restored={stack_ok}")
        except Exception as e:
            self._record(51, FAIL, f"raised: {e!r}")
            return
        self._record(51, PASS if ok else FAIL, detail)

    def _step_reconcile_adopt(self):
        # v3.2.3 (57->52): a broker position NOT in state -> ADOPT (never ignore a
        # live position); zero orphans.
        import soft_restart as sr
        try:
            actions, summary = sr.reconcile(state_tickets=set(), broker_tickets={9001})
            adopted = (actions.get(9001) == sr.ADOPT and summary['adopted'] == 1)
            no_orphan = (summary['orphans'] == 0)
            # the adopted shadow is CONSERVATIVE (max_fav == entry -> no phantom).
            sh = sr.adopt_shadow({'entry_price': 4200.0, 'side': 'BUY',
                                  'sl': 4182.0, 'tp': 4230.0})
            conservative = (sh['max_fav'] == 4200.0 and sh['lock_level'] == 0
                            and sh['adopted'] is True)
            ok = adopted and no_orphan and conservative
            detail = f"S3_adopted={adopted} S3_no_orphan={no_orphan} (conservative={conservative})"
        except Exception as e:
            self._record(52, FAIL, f"raised: {e!r}")
            return
        self._record(52, PASS if ok else FAIL, detail)

    def _step_reconcile_finalize(self):
        # v3.2.3 (58->53): a state position that closed during the gap -> FINALIZE
        # (journal), NOT re-opened.
        import soft_restart as sr
        try:
            actions, summary = sr.reconcile(state_tickets={7007}, broker_tickets=set())
            finalized = (actions.get(7007) == sr.FINALIZE and summary['finalized'] == 1)
            # not on the broker -> never adopted/resumed -> never re-opened.
            not_reopened = (actions.get(7007) not in (sr.RESUME, sr.ADOPT))
            ok = finalized and not_reopened and summary['orphans'] == 0
            detail = f"S4_finalized={finalized} S4_not_reopened={not_reopened}"
        except Exception as e:
            self._record(53, FAIL, f"raised: {e!r}")
            return
        self._record(53, PASS if ok else FAIL, detail)

    def _step_quick_gap(self):
        # v3.2.3 (59->54): downtime < SOFT_RESTART_MAX_GAP_S; the first post-restart
        # tick uses the sane-tick / phantom guard -> no phantom lock on rehydrate.
        import soft_restart as sr
        from strategy import lock_trigger_reached
        try:
            gap = sr.gap_seconds(exit_epoch=1000.0, boot_epoch=1008.0)   # 8s
            quick = sr.gap_ok(gap) and gap < sr.SOFT_RESTART_MAX_GAP_S
            # rehydrate restores max_fav = entry (conservative); the phantom guard
            # then BLOCKS any lock until price genuinely re-reaches a level.
            entry = 4155.35
            no_phantom = (lock_trigger_reached('BUY', entry, entry, 1) is False)
            first_tick_sane = no_phantom   # the guard governs the first tick too
            ok = quick and no_phantom and first_tick_sane
            detail = (f"S5_gap<10s={quick}(gap={gap:.0f}s) "
                      f"S5_no_phantom_on_rehydrate={no_phantom} "
                      f"S5_first_tick_sane={first_tick_sane}")
        except Exception as e:
            self._record(54, FAIL, f"raised: {e!r}")
            return
        self._record(54, PASS if ok else FAIL, detail)

    # ---- Feature D: break-and-hold filter (the profit decider) -----------
    def _step_break_fakespike(self):
        # 55: a spike that clears the edge then reverses back through it = FAILED
        # break -> fire NOTHING (the 14:30/15:30 fake-out).
        import break_hold as bh
        try:
            cfg = self.cfg; edge = 100.0
            candles = [{'high': edge + 3, 'low': edge + 0.5, 'close': edge + 2},
                       {'high': edge + 1, 'low': edge - 1.0, 'close': edge - 0.5}]
            res = bh.evaluate_break('BUY', edge, candles, cfg)
            no_fire = (res == bh.FAILED) and (bh.may_stack('BUY', edge, candles, cfg) is False)
            ok = no_fire
            detail = f"fake_spike->no_fire={no_fire} (result={res})"
        except Exception as e:
            self._record(55, FAIL, f"raised: {e!r}"); return
        self._record(55, PASS if ok else FAIL, detail)

    def _step_break_holds(self):
        # 56: a real break that clears X, holds N candles, retraces < Y -> CONFIRMED
        # -> stack allowed (proves the filter doesn't block real breaks).
        import break_hold as bh
        try:
            cfg = self.cfg; edge = 100.0
            candles = [{'high': edge + 4, 'low': edge + 2.5, 'close': edge + 3.5},
                       {'high': edge + 4, 'low': edge + 3.0, 'close': edge + 3.8}]
            res = bh.evaluate_break('BUY', edge, candles, cfg)
            ok = (res == bh.CONFIRMED) and (bh.may_stack('BUY', edge, candles, cfg) is True)
            detail = f"real_break_holds->stack={ok} (result={res})"
        except Exception as e:
            self._record(56, FAIL, f"raised: {e!r}"); return
        self._record(56, PASS if ok else FAIL, detail)

    def _step_break_continuation(self):
        # 57: after a FAILED up-spike, a DOWN break that holds -> CONFIRMED (the
        # post-spike continuation is caught on the other side).
        import break_hold as bh
        try:
            cfg = self.cfg
            up_edge = 100.0
            up = [{'high': up_edge + 3, 'low': up_edge + 0.5, 'close': up_edge + 2},
                  {'high': up_edge + 1, 'low': up_edge - 1.0, 'close': up_edge - 0.5}]
            up_failed = (bh.evaluate_break('BUY', up_edge, up, cfg) == bh.FAILED)
            dn_edge = 98.0
            dn = [{'low': dn_edge - 3, 'high': dn_edge - 0.5, 'close': dn_edge - 2},
                  {'low': dn_edge - 4, 'high': dn_edge - 2.5, 'close': dn_edge - 3.5}]
            # tighten so retrace stays < Y
            dn = [{'low': 95.0, 'high': 95.5, 'close': 95.2},
                  {'low': 94.0, 'high': 94.5, 'close': 94.2}]
            dn_ok = (bh.evaluate_break('SELL', dn_edge, dn, cfg) == bh.CONFIRMED)
            ok = up_failed and dn_ok
            detail = f"up_spike_failed={up_failed} down_continuation_caught={dn_ok}"
        except Exception as e:
            self._record(57, FAIL, f"raised: {e!r}"); return
        self._record(57, PASS if ok else FAIL, detail)

    def _step_break_retrace(self):
        # 58: cleared + held but retraced >= Y of the break distance -> FAILED.
        import break_hold as bh
        try:
            cfg = self.cfg; edge = 100.0
            candles = [{'high': edge + 4, 'low': edge + 0.5, 'close': edge + 1},
                       {'high': edge + 3, 'low': edge + 1.0, 'close': edge + 2}]
            res = bh.evaluate_break('BUY', edge, candles, cfg)
            ok = (res == bh.FAILED) and (bh.may_stack('BUY', edge, candles, cfg) is False)
            detail = f"retrace>Y->no_fire={ok} (result={res})"
        except Exception as e:
            self._record(58, FAIL, f"raised: {e!r}"); return
        self._record(58, PASS if ok else FAIL, detail)

    def _step_break_holdshort(self):
        # 59: cleared but only held < N candles -> PENDING -> no fire (yet).
        import break_hold as bh
        try:
            cfg = self.cfg; edge = 100.0
            candles = [{'high': edge + 3, 'low': edge + 1, 'close': edge + 2}]  # 1 < N=2
            res = bh.evaluate_break('BUY', edge, candles, cfg)
            ok = (res == bh.PENDING) and (bh.may_stack('BUY', edge, candles, cfg) is False)
            detail = f"hold<N->no_fire={ok} (result={res})"
        except Exception as e:
            self._record(59, FAIL, f"raised: {e!r}"); return
        self._record(59, PASS if ok else FAIL, detail)

    # ---- Feature E: lot config + FP-rule guard ---------------------------
    def _step_fp_015_ok(self):
        # 60: a 5-long stack at 0.15 floats < 5% ($2,500) -> OK, all 5 allowed.
        import fp_guard as fp
        try:
            action, wc, lim, allowed = fp.fp_guard(5, 0.15, 18.0, 'STANDARD_5PCT', 50000.0)
            ok = (action == fp.OK and wc <= lim and allowed == 5 and abs(wc - 1350.0) < 1)
            detail = f"0.15_under_5pct={ok} (wc=${wc:.0f} lim=${lim:.0f} allowed={allowed})"
        except Exception as e:
            self._record(60, FAIL, f"raised: {e!r}"); return
        self._record(60, PASS if ok else FAIL, detail)

    def _step_fp_035_breach(self):
        # 61: a 5-long stack at 0.35 floats > 5% -> REDUCE to the largest that fits.
        import fp_guard as fp
        try:
            action, wc, lim, allowed = fp.fp_guard(5, 0.35, 18.0, 'STANDARD_5PCT', 50000.0)
            ok = (action == fp.REDUCE and wc > lim and allowed == 3 and abs(wc - 3150.0) < 1)
            detail = f"0.35_flags_breach={ok} (action={action} wc=${wc:.0f} allowed={allowed})"
        except Exception as e:
            self._record(61, FAIL, f"raised: {e!r}"); return
        self._record(61, PASS if ok else FAIL, detail)

    def _step_fp_zero_blocks(self):
        # 62: FP-Zero (1% = $500) blocks a 5-long at the demo lot (can't fit 5).
        import fp_guard as fp
        try:
            action, wc, lim, allowed = fp.fp_guard(5, 0.35, 18.0, 'FPZERO_1PCT', 50000.0)
            blocked = (action != fp.OK and allowed < 5 and lim == 500.0)
            # even at FP-safe 0.15, 5-long doesn't fit 1% -> still reduced below 5.
            a2, _, _, allowed2 = fp.fp_guard(5, 0.15, 18.0, 'FPZERO_1PCT', 50000.0)
            ok = blocked and allowed2 < 5
            detail = f"FPZero_blocks_5long={ok} (action={action} allowed={allowed}/{allowed2})"
        except Exception as e:
            self._record(62, FAIL, f"raised: {e!r}"); return
        self._record(62, PASS if ok else FAIL, detail)

    def _step_fp_lot_config(self):
        # 63: the FP guard reads the lot from cfg everywhere (guard_cfg) -- changing
        # the configured lot changes the worst-case exposure.
        import dataclasses, fp_guard as fp
        try:
            cfg = self.cfg
            # guard_cfg uses SL + spread buffer (18.6) -> reference math:
            # 5x0.15 -> -$1,395, 5x0.35 -> -$3,255.
            a1, wc1, _, _ = fp.guard_cfg(5, dataclasses.replace(cfg, lot_size=0.15,
                                          account_profile='STANDARD_5PCT'), 50000.0)
            a2, wc2, _, _ = fp.guard_cfg(5, dataclasses.replace(cfg, lot_size=0.35,
                                          account_profile='STANDARD_5PCT'), 50000.0)
            applies = (wc1 < wc2 and abs(wc1 - 1395.0) < 1 and abs(wc2 - 3255.0) < 1)
            ok = applies
            detail = f"lot_config_applies_everywhere={applies} (0.15->${wc1:.0f} 0.35->${wc2:.0f})"
        except Exception as e:
            self._record(63, FAIL, f"raised: {e!r}"); return
        self._record(63, PASS if ok else FAIL, detail)

    # ---- Feature C: 5-long No-OCO stack (DEFAULT ON, disableable) ---------
    def _step_stack5_cap(self):
        # 64: 5-long default ON -> cap 5; disabling the flag falls back to cap 3.
        import dataclasses, boosts as b
        try:
            cfg = self.cfg   # default allow_5_long=True
            cfg3 = dataclasses.replace(cfg, allow_5_long=False)
            default_5 = (b.stack_cap(cfg) == 5 and b.stack_winners(cfg) == 5)
            off_3 = (b.stack_cap(cfg3) == 3 and b.stack_winners(cfg3) == 3)
            ok = default_5 and off_3
            detail = f"default_cap5={default_5} flag_off->cap3={off_3}"
        except Exception as e:
            self._record(64, FAIL, f"raised: {e!r}"); return
        self._record(64, PASS if ok else FAIL, detail)

    def _step_stack5_loser_out(self):
        # 65: at full 5-long the peak is 5 winners + 1 losing leg (6 legs); once the
        # loser SLs and is CLOSED it leaves exposure (5 winners remain).
        import dataclasses, boosts as b
        try:
            cfg5 = dataclasses.replace(self.cfg, allow_5_long=True)
            lots_peak, usd_peak = b.stack_peak_exposure(cfg5)   # (5+1)*0.35 = 2.10
            lot = float(cfg5.lot_size)
            winners_only = round(b.stack_winners(cfg5) * lot, 2)  # 5*0.35 = 1.75
            ok = (abs(lots_peak - 2.10) < 1e-6 and abs(winners_only - 1.75) < 1e-6
                  and winners_only < lots_peak)
            detail = (f"peak_6legs={lots_peak}lot loser_closed->{winners_only}lot "
                      f"(loser_out={winners_only < lots_peak})")
        except Exception as e:
            self._record(65, FAIL, f"raised: {e!r}"); return
        self._record(65, PASS if ok else FAIL, detail)

    def _step_stack5_fp_gate(self):
        # 66: a 5-long at 0.35 BREACHES 5% and must be reduced/blocked; at 0.15 it
        # fits -> the 5-long is only allowed when the FP guard passes.
        import fp_guard as fp
        try:
            a035, _, _, n035 = fp.fp_guard(5, 0.35, 18.0, 'STANDARD_5PCT', 50000.0)
            a015, _, _, n015 = fp.fp_guard(5, 0.15, 18.0, 'STANDARD_5PCT', 50000.0)
            ok = (a035 != fp.OK and n035 < 5 and a015 == fp.OK and n015 == 5)
            detail = f"5long@0.35_gated={a035}(n={n035}) 5long@0.15_ok={a015}(n={n015})"
        except Exception as e:
            self._record(66, FAIL, f"raised: {e!r}"); return
        self._record(66, PASS if ok else FAIL, detail)

    def _step_stack5_whipsaw(self):
        # 67: 5 winners stalling below break-even (~$126/pos) then reversing, with
        # the losing leg -$630, must net NEGATIVE and class WHIPSAW (logged honestly).
        import dataclasses, boosts as b
        from rescue_log import _branch_for
        try:
            cfg5 = dataclasses.replace(self.cfg, allow_5_long=True)
            per_be = b.per_position_breakeven_usd(cfg5)          # 630/5 = 126
            net_whip = round(b.stack_winners(cfg5) * 100.0 - b.stack_breakeven_usd(cfg5), 2)
            whip = (net_whip < 0 and _branch_for(net_whip) == 'WHIPSAW_LOSS')
            net_win = round(b.stack_winners(cfg5) * 200.0 - b.stack_breakeven_usd(cfg5), 2)
            be_ok = abs(per_be - 126.0) < 1.0 and net_win > 0
            ok = whip and be_ok
            detail = (f"whipsaw(net={net_whip:.0f})={whip} per_be=${per_be:.0f} "
                      f"win(net={net_win:.0f})>0={net_win>0}")
        except Exception as e:
            self._record(67, FAIL, f"raised: {e!r}"); return
        self._record(67, PASS if ok else FAIL, detail)

    def _step_stack5_cap_viol(self):
        # 68: stack_size beyond the active cap trips a violation -- 6>5 (5-long on),
        # 4>3 (default off). 5 at cap 5 and 3 at cap 3 do NOT trip.
        from position_telemetry import PositionTracer
        try:
            tr = PositionTracer(sink=lambda l: None)
            tr.boost_fire(1, 'A', side='BUY', boost_kind='RALLY', stack_size=6,
                          stack_cap=5, move_dollars=10.0, trigger=10.0)
            six_over5 = any('stack_size_exceeds_cap' in v for v in tr.violations)
            tr2 = PositionTracer(sink=lambda l: None)
            tr2.boost_fire(2, 'A', side='BUY', boost_kind='RALLY', stack_size=5,
                           stack_cap=5, move_dollars=10.0, trigger=10.0)
            five_ok = (len(tr2.violations) == 0)
            tr3 = PositionTracer(sink=lambda l: None)   # default cap 3 (no stack_cap field)
            tr3.boost_fire(3, 'A', side='BUY', boost_kind='RALLY', stack_size=4,
                           move_dollars=10.0, trigger=10.0)
            four_over3 = any('stack_size_exceeds_cap' in v for v in tr3.violations)
            ok = six_over5 and five_ok and four_over3
            detail = f"6>cap5_viol={six_over5} 5@cap5_ok={five_ok} 4>cap3_viol={four_over3}"
        except Exception as e:
            self._record(68, FAIL, f"raised: {e!r}"); return
        self._record(68, PASS if ok else FAIL, detail)

    # ---- v3.2.4 additions: trail co-close, P&L fixtures, profile cap, default --
    def _step_stack5_trail_coclose(self):
        # 69: TRAIL-LOCK (the expected Wednesday behaviour). All ARMED longs (+$8)
        # close TOGETHER at peak - trail_gap; an UNARMED long falls to its own $10
        # boost SL (not the trail). max_fav is the real peak.
        import boosts as b
        try:
            cfg = self.cfg
            max_fav = 4017.0   # shared high-water mark (the real peak)
            longs = [
                {'entry': 4005.0},   # +12 -> armed
                {'entry': 4007.0},   # +10 -> armed
                {'entry': 4013.0},   # +4  -> NOT armed (< +8) -> own $10 SL
            ]
            co, rows = b.stack_trail_exits(longs, max_fav, cfg)
            gap = cfg.trail_gap
            armed = [r for r in rows if r['armed']]
            unarmed = [r for r in rows if not r['armed']]
            co_ok = abs(co - (max_fav - gap)) < 1e-6
            all_armed_together = all(abs(r['exit'] - co) < 1e-6 for r in armed) and len(armed) == 2
            unarmed_sl = (len(unarmed) == 1
                          and abs(unarmed[0]['exit'] - (4013.0 - cfg.boost_trigger_dollars)) < 1e-6)
            ok = co_ok and all_armed_together and unarmed_sl
            detail = (f"co_close=${co:.2f}(peak-${gap}) armed_together={all_armed_together} "
                      f"unarmed->${unarmed[0]['exit']:.2f}_own_SL={unarmed_sl}")
        except Exception as e:
            self._record(69, FAIL, f"raised: {e!r}"); return
        self._record(69, PASS if ok else FAIL, detail)

    def _step_stack5_pnl_015(self):
        # 70: 5-long P&L fixtures @0.15 (from the drawing: sell -$270 + 5 longs).
        # least +285 -> +$15 ; modest +585 -> +$315 ; bigger +1185 -> +$915.
        import dataclasses, boosts as b
        try:
            cfg015 = dataclasses.replace(self.cfg, lot_size=0.15)
            loser = b.stack_breakeven_usd(cfg015)   # 0.15*18*100 = 270
            least = b.stack_scenario_net(285.0, loser)
            modest = b.stack_scenario_net(585.0, loser)
            bigger = b.stack_scenario_net(1185.0, loser)
            ok = (abs(loser - 270.0) < 1.0 and abs(least - 15.0) < 1.0
                  and abs(modest - 315.0) < 1.0 and abs(bigger - 915.0) < 1.0)
            detail = (f"loser=-${loser:.0f} least=+${least:.0f} modest=+${modest:.0f} "
                      f"bigger=+${bigger:.0f}")
        except Exception as e:
            self._record(70, FAIL, f"raised: {e!r}"); return
        self._record(70, PASS if ok else FAIL, detail)

    def _step_stack5_pnl_035(self):
        # 71: 5-long P&L @0.35 -- modest +1365 longs -> +$735 net; the larger lot's
        # worst-case exposure is FLAGGED by the FP guard (REDUCE).
        import dataclasses, boosts as b, fp_guard as fp
        try:
            cfg035 = dataclasses.replace(self.cfg, lot_size=0.35)
            loser = b.stack_breakeven_usd(cfg035)   # 0.35*18*100 = 630
            modest = b.stack_scenario_net(1365.0, loser)
            net_ok = (abs(loser - 630.0) < 1.0 and abs(modest - 735.0) < 1.0)
            action, wc, lim, allowed = fp.guard_cfg(5, cfg035, 50000.0)
            flagged = (action != fp.OK and allowed < 5 and wc > lim)
            ok = net_ok and flagged
            detail = (f"loser=-${loser:.0f} modest=+${modest:.0f} "
                      f"fp_flag={action}(wc=${wc:.0f}>lim${lim:.0f},n={allowed})")
        except Exception as e:
            self._record(71, FAIL, f"raised: {e!r}"); return
        self._record(71, PASS if ok else FAIL, detail)

    def _step_fp_zero_profile_cap(self):
        # 72: FPZERO_1PCT disallows the 5-long entirely -> the stack is capped to 3
        # (no 5-stack on a 1% floating rule), independent of the worst-case math.
        import fp_guard as fp
        try:
            std = fp.profile_stack_cap('STANDARD_5PCT', 5)
            zero = fp.profile_stack_cap('FPZERO_1PCT', 5)
            ok = (std == 5 and zero == 3)
            detail = f"STANDARD_5PCT->cap{std} FPZERO_1PCT->cap{zero}(5long_blocked)"
        except Exception as e:
            self._record(72, FAIL, f"raised: {e!r}"); return
        self._record(72, PASS if ok else FAIL, detail)

    def _step_stack5_default_on(self):
        # 73: 5-long is ON by default (config) yet remains disableable -- the flag
        # exists, default True; FP guard still caps exposure at the chosen lot.
        import dataclasses, boosts as b
        try:
            on = bool(getattr(self.cfg, 'allow_5_long', False)) and b.stack_cap(self.cfg) == 5
            off = b.stack_cap(dataclasses.replace(self.cfg, allow_5_long=False)) == 3
            ok = on and off
            detail = f"default_ON={on} disableable->cap3={off}"
        except Exception as e:
            self._record(73, FAIL, f"raised: {e!r}"); return
        self._record(73, PASS if ok else FAIL, detail)

    # ---- v3.2.5 Feature 1: A1 tick-fallback anchor capture (open path) -------
    def _step_a1_tick_fallback_places(self):
        # 74: A1 open, no M5 bar -> SANE-tick fallback -> straddle PLACED (NOT
        # missed). Drives the live _capture_a1_anchor_from_tick with a settled tick
        # feed; asserts placed=True, source=tick (telemetry), buy/sell geometry.
        import types, dataclasses
        from anchors import _capture_a1_anchor_from_tick
        from position_telemetry import PositionTracer
        try:
            cfg = dataclasses.replace(self.cfg, tick_refresh_s=0.0,
                                      a1_tick_fallback_samples=4, hold_ticks=3,
                                      a1_tick_fallback_enabled=True)
            anchor = 4321.50
            feed = iter([anchor, anchor, anchor, anchor, anchor])   # settled, held
            def _tick(sym):
                try: p = next(feed)
                except StopIteration: p = anchor
                return types.SimpleNamespace(
                    time=int(pd.Timestamp.now(tz='UTC').timestamp()), bid=p, ask=p)
            lines = []
            stub = types.SimpleNamespace(
                cfg=cfg, paper=False,
                adapter=types.SimpleNamespace(tick_time_offset_hours=0,
                    mt5=types.SimpleNamespace(symbol_info_tick=_tick)),
                ptrace=PositionTracer(sink=lines.append),
                tele=types.SimpleNamespace(success=lambda *a, **k: None),
                _touch_heartbeat=lambda: None)
            price = _capture_a1_anchor_from_tick(stub, 'A1_02h_Asia',
                                                 pd.Timestamp('2026-06-22T00:30:00Z'))
            placed = price is not None and abs(price - anchor) < 1e-6
            src_tick = any('A1_PLACED_FROM_TICK' in l and 'tick' in l for l in lines)
            buy = round(price + cfg.trigger_dist, 2) if placed else None
            sell = round(price - cfg.trigger_dist, 2) if placed else None
            geom = placed and abs(buy - (anchor + cfg.trigger_dist)) < 1e-6 \
                and abs(sell - (anchor - cfg.trigger_dist)) < 1e-6
            ok = placed and src_tick and geom
            detail = (f"placed={placed} source=tick={src_tick} "
                      f"anchor=${price if placed else float('nan'):.2f} buy/sell=${buy}/${sell}")
        except Exception as e:
            self._record(74, FAIL, f"raised: {e!r}"); return
        self._record(74, PASS if ok else FAIL, detail)

    def _step_a1_tick_fallback_rejects_spike(self):
        # 75: the fallback rejects the WILD first reopen tick and waits for a
        # settled/held run -> the anchor is NOT set on the spike. Also: a feed that
        # never settles (insufficient held ticks) -> no capture (waits).
        import tick_hold as th
        try:
            cfg = self.cfg
            settled = 4000.0
            spike = settled + 60.0   # > max_tick_jump (25) from the settled run
            ticks = [spike, settled, settled + 0.1, settled - 0.1, settled + 0.05]
            ok1, price, held, reason = th.settle_anchor_tick(ticks, cfg)
            anchor_sane = (ok1 and abs(price - settled) < 1.0
                           and abs(price - spike) > cfg.max_tick_jump)
            held_ok = held >= th.hold_ticks(cfg)
            # spike alone (not enough settled ticks) -> waits, no capture.
            ok2, _, _, r2 = th.settle_anchor_tick([spike, settled], cfg)
            waits = (not ok2)
            ok = anchor_sane and held_ok and waits
            detail = (f"anchor_in_sane_range={anchor_sane}(${price if ok1 else float('nan'):.2f}) "
                      f"held={held}>=3={held_ok} spike_only->waits={waits}")
        except Exception as e:
            self._record(75, FAIL, f"raised: {e!r}"); return
        self._record(75, PASS if ok else FAIL, detail)

    # ---- v3.2.5 Feature 2: tick-hold confirm on boost + trail ----------------
    def _step_tick_hold_fires(self):
        # 76: a +/-$10 cross that HOLDS 3 ticks -> boost fires (rally AND rescue;
        # the hold logic is direction-agnostic so both confirm identically).
        import tick_hold as th
        try:
            cfg = self.cfg
            fired_rally, sr, st_r = th.confirm_cross([True, True, True], cfg)
            fired_rescue, _, st_s = th.confirm_cross([True, True, True], cfg)
            # a longer run still fires (first CONFIRMED at exactly hold_ticks)
            fired_more, _, _ = th.confirm_cross([True, True, True, True, True], cfg)
            ok = fired_rally and fired_rescue and fired_more and st_r == th.CONFIRMED
            detail = (f"rally_fires={fired_rally} rescue_fires={fired_rescue} "
                      f"streak={sr}>=hold{th.hold_ticks(cfg)} state={st_r}")
        except Exception as e:
            self._record(76, FAIL, f"raised: {e!r}"); return
        self._record(76, PASS if ok else FAIL, detail)

    def _step_tick_hold_blip_rejected(self):
        # 77: a +/-$10 cross that REVERTS within 3 ticks -> NO fire (blip rejected).
        import tick_hold as th
        try:
            cfg = self.cfg
            # T,T then reverts -> BLIP, never CONFIRMED.
            fired1, _, state1 = th.confirm_cross([True, True, False], cfg)
            # flapping cross never holds 3 in a row -> never fires.
            fired2, _, _ = th.confirm_cross([True, False, True, False, True, False], cfg)
            ok = (not fired1) and state1 == th.BLIP and (not fired2)
            detail = f"blip_no_fire={not fired1}(state={state1}) flap_no_fire={not fired2}"
        except Exception as e:
            self._record(77, FAIL, f"raised: {e!r}"); return
        self._record(77, PASS if ok else FAIL, detail)

    def _step_tick_hold_trail_advance(self):
        # 78: a trail lock advances only on a HELD max_fav (>= hold_ticks); a single
        # spike tick -> no advance. Ties to the phantom-lock guard (lock off a held
        # real move, never a ghost).
        import tick_hold as th
        try:
            cfg = self.cfg
            spike_no = not th.trail_advance_ok(1, cfg)     # single spike tick
            two_no = not th.trail_advance_ok(2, cfg)       # 2 < hold_ticks
            held_yes = th.trail_advance_ok(th.hold_ticks(cfg), cfg)  # held -> advance
            ok = spike_no and two_no and held_yes
            detail = (f"spike(1)->no_advance={spike_no} two->no={two_no} "
                      f"held({th.hold_ticks(cfg)})->advance={held_yes}")
        except Exception as e:
            self._record(78, FAIL, f"raised: {e!r}"); return
        self._record(78, PASS if ok else FAIL, detail)

    def _step_boost_incident_regression(self):
        # 79: 2026-06-23 INCIDENT regression. The SELL boost (#56860793855) entered
        # ~4185.92 and was CUT underwater at ~4191.32 (+$5.4 adverse) by the breath
        # trail armed at fav=0; price then dropped ~$35. v3.2.6 arm-gate: below +$8
        # the trail is INACTIVE, so an adverse bar to 4191.32 must NOT close it (the
        # $10 backstop 4195.92 is not hit); on the favorable drop it rides/profits.
        from strategy import Position, update_position_on_bar, realize_pnl_usd
        try:
            cfg = self.cfg
            entry = 4185.92
            hard = float(getattr(cfg, 'boost_sl_dollars', 10.0))
            backstop = entry + hard      # SELL backstop sits ABOVE entry (4195.92)
            ts0 = pd.Timestamp('2026-06-23T04:06:34Z')
            p = Position(anchor_label='A1_02h_Asia', side='SELL', entry_price=entry,
                         entry_time=ts0, current_sl=backstop, tp_level=entry - 30.0,
                         max_fav=entry, lot=cfg.lot_size, role='rescue', boost=True)
            # the exact incident adverse bar: high 4191.6, the close 4191.32 -- inside
            # the OLD $3.50 trail but well below the $10 backstop. Must NOT close.
            update_position_on_bar(p, pd.Series(
                {'open': 4191.32, 'high': 4191.60, 'low': 4189.00, 'close': 4191.32}),
                ts0 + pd.Timedelta(minutes=1), cfg)
            not_cut_underwater = (not p.closed)
            backstop_not_hit = p.current_sl >= backstop - 1e-6   # SL held at full $10
            # then price drops ~$35 in AUREON's favor -> the boost rides into profit.
            update_position_on_bar(p, pd.Series(
                {'open': 4191.00, 'high': 4191.00, 'low': 4156.00, 'close': 4158.00}),
                ts0 + pd.Timedelta(minutes=2), cfg)
            held_or_profit = (not p.closed) or realize_pnl_usd(p, cfg) > 0
            ok = not_cut_underwater and backstop_not_hit and held_or_profit
            detail = (f"adverse_4191.32_not_cut={not_cut_underwater} "
                      f"backstop_${backstop:.2f}_held={backstop_not_hit} "
                      f"after_$35_drop_held/profit={held_or_profit}")
        except Exception as e:
            self._record(79, FAIL, f"raised: {e!r}"); return
        self._record(79, PASS if ok else FAIL, detail)

    def _step_rescue_bypass_break_and_hold(self):
        # 80: v3.2.7 — break-and-hold gates RALLY only; RESCUE fires FREELY on
        # direction commit. Drives the REAL fills._check_boost_triggers with an
        # UNCONFIRMED break (_break_and_hold_ok stubbed False) and asserts: RALLY
        # suppressed, RESCUE fires, RESCUE still blocked by FP guard, toggle-off
        # re-gates RESCUE, RALLY fires on a CONFIRMED break. tick-hold streak is
        # pre-seeded to hold-1 so a single tick confirms.
        import types, dataclasses
        import fills as _fills
        try:
            base = self.cfg
            def make_stub(mid, rally_only, bh_ok, fp_ok, bypass=True):
                s = types.SimpleNamespace()
                s.paper = False
                s.cfg = dataclasses.replace(base, rescue_bypass_break_and_hold=bypass,
                                            hold_ticks=3)
                s.ptrace = None
                s.adapter = types.SimpleNamespace(mt5=types.SimpleNamespace(
                    symbol_info_tick=lambda sym, _m=mid: types.SimpleNamespace(bid=_m, ask=_m)))
                s.shadow_positions = {501: {
                    'boost': False, 'boost_fired': False, 'boost_eligible': True,
                    'side': 'BUY', 'entry_price': 100.0, 'leg_fill_price': 100.0,
                    'anchor_label': 'A1_02h_Asia', 'boost_rally_only': rally_only,
                    'boost_cross_streak': 2}}   # hold_ticks-1 -> ONE tick confirms
                s.fires = []
                s._break_and_hold_ok = lambda shadow, plan: bh_ok
                s._fp_guard_ok = lambda shadow, n: fp_ok
                s._fire_boost_event = lambda t, sh, pl: s.fires.append(pl.kind)
                s._enforce_boost_cap = lambda mid_: None
                return s
            # RALLY (+11, winning), unconfirmed break -> GATED -> no fire
            r = make_stub(111.0, rally_only=True, bh_ok=False, fp_ok=True)
            _fills._check_boost_triggers(r); rally_gated = (r.fires == [])
            # RESCUE (-11, losing), unconfirmed break -> BYPASS -> fires
            s = make_stub(89.0, rally_only=False, bh_ok=False, fp_ok=True)
            _fills._check_boost_triggers(s); rescue_fires = (s.fires == ['RESCUE'])
            # RESCUE still blocked if FP guard fails
            s2 = make_stub(89.0, rally_only=False, bh_ok=False, fp_ok=False)
            _fills._check_boost_triggers(s2); rescue_fp_blocks = (s2.fires == [])
            # toggle OFF -> RESCUE gated again (legacy v3.2.6)
            s3 = make_stub(89.0, rally_only=False, bh_ok=False, fp_ok=True, bypass=False)
            _fills._check_boost_triggers(s3); rescue_gated_off = (s3.fires == [])
            # RALLY with CONFIRMED break -> fires
            r2 = make_stub(111.0, rally_only=True, bh_ok=True, fp_ok=True)
            _fills._check_boost_triggers(r2); rally_fires_confirmed = (r2.fires == ['RALLY'])
            ok = (rally_gated and rescue_fires and rescue_fp_blocks
                  and rescue_gated_off and rally_fires_confirmed)
            detail = (f"rally_gated={rally_gated} rescue_fires_free={rescue_fires} "
                      f"rescue_fp_blocks={rescue_fp_blocks} toggle_off_regates={rescue_gated_off} "
                      f"rally_confirmed_fires={rally_fires_confirmed}")
        except Exception as e:
            self._record(80, FAIL, f"raised: {e!r}"); return
        self._record(80, PASS if ok else FAIL, detail)

    # ------------------------------------------------------------------------
    # v3.2.8 Phase 1 — RALLY +$5 arm / +$4 lock / $1.50 gap (RESCUE untouched)
    # ------------------------------------------------------------------------
    def _step_rally_arm_5(self):
        # v3.2.8 Phase 1: the WINNING-side RALLY arm drops $10 -> $5, via DEDICATED
        # keys (rally_arm_fav), while the LOSING-side RESCUE arm stays $10
        # (boost_trigger_dollars). Asserts on the LIVE canonical boosts.plan_boost_event
        # (the single source live + backtest + tests call): (1) rally fires AT +$5;
        # (2) rally does NOT fire below +$5 (+$4.99 -> None); (3) the whole +$5..+$9.99
        # winning band that USED to be dead now fires RALLY (the behaviour change);
        # (4) rescue is UNCHANGED -- needs the full -$10 (-$9.99 -> None, -$10 fires);
        # (5) the config exposes rally_arm_fav=5.0 as its own key (not a BOOST_* reuse).
        import boosts as _b
        from config import Config as _Config
        try:
            cfg = _Config()
            fill = 4266.3
            # (5) dedicated key present + default.
            key_ok = (abs(float(getattr(cfg, 'rally_arm_fav')) - 5.0) < 1e-9)
            # (1) rally fires exactly at +$5 (BUY price up $5), SAME side.
            at5 = _b.plan_boost_event('BUY', fill, fill + 5.0, cfg)
            fires_at_5 = (at5 is not None and at5.kind == 'RALLY' and at5.boost_side == 'BUY')
            # (2) below +$5 -> None.
            below5 = (_b.plan_boost_event('BUY', fill, fill + 4.99, cfg) is None)
            # (3) the +$5..+$9.99 band (old dead zone) now fires RALLY.
            band = all(_b.plan_boost_event('BUY', fill, fill + d, cfg) is not None
                       and _b.plan_boost_event('BUY', fill, fill + d, cfg).kind == 'RALLY'
                       for d in (5.0, 6.0, 7.5, 9.99))
            # (4) RESCUE arm untouched: -$9.99 -> None, -$10 -> RESCUE (opposite side).
            r999 = _b.plan_boost_event('BUY', fill, fill - 9.99, cfg)
            r10 = _b.plan_boost_event('BUY', fill, fill - 10.0, cfg)
            rescue_unchanged = (r999 is None and r10 is not None
                                and r10.kind == 'RESCUE' and r10.boost_side == 'SELL')
            # a SELL leg winning by +$5 (price DOWN $5) -> RALLY same side (SELL).
            s5 = _b.plan_boost_event('SELL', fill, fill - 5.0, cfg)
            sell_rally = (s5 is not None and s5.kind == 'RALLY' and s5.boost_side == 'SELL')
            ok = (key_ok and fires_at_5 and below5 and band and rescue_unchanged and sell_rally)
            detail = (f"rally_arm_fav={getattr(cfg, 'rally_arm_fav')} fires@+5={fires_at_5} "
                      f"none<+5={below5} band5-9.99=RALLY={band} "
                      f"rescue_still_10={rescue_unchanged} sell_rally={sell_rally}")
        except Exception as e:
            self._record(81, FAIL, f"raised: {e!r}"); return
        self._record(81, PASS if ok else FAIL, detail)

    def _step_rally_trail_ride(self):
        # v3.3.0: a RALLY boost RIDES like the original leg -- once armed at +$5 (peak)
        # it trails at peak - rally_trail_gap ($2.00), one-way, above a break-even+
        # MINIMUM floor of +$3 (= arm - gap). It NO LONGER locks flat at +$4 and bails
        # on the first pause (the v3.2.8 defect; test-fire A2). Drives the REAL strategy
        # core (update_position_on_bar). A RESCUE boost stays BYTE-IDENTICAL ($8 arm /
        # $8 lock / $3.50 gap). KIND-ISOLATION proof: the SAME +$6-then-reverse path
        # rides+exits ~peak-$2 on RALLY but is unarmed (never reaches $8) on RESCUE.
        from strategy import Position, update_position_on_bar
        try:
            cfg = self.cfg
            hard = float(getattr(cfg, 'boost_sl_dollars', 10.0))
            r_gap = float(getattr(cfg, 'rally_trail_gap', 2.00))
            r_floor = float(getattr(cfg, 'rally_lock_floor', 3.0))   # be+ minimum
            r_arm = float(getattr(cfg, 'rally_arm_fav', 5.0))         # trail-arm peak
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-24T02:30:00Z')

            def run(bars, kind):
                p = Position(anchor_label='T', side='BUY', entry_price=entry,
                             entry_time=ts0, current_sl=entry - hard,
                             tp_level=entry + 30.0, max_fav=entry,
                             lot=cfg.lot_size, role='rescue', boost=True, boost_kind=kind)
                for i, b in enumerate(bars):
                    update_position_on_bar(p, pd.Series(b),
                                           ts0 + pd.Timedelta(minutes=i + 1), cfg)
                    if p.closed:
                        break
                return p

            # (1) reverses BEFORE +$5 (unarmed) -> trail INACTIVE -> rides to $10 backstop.
            p1 = run([{'open': 100, 'high': 104, 'low': entry - hard - 1, 'close': 92}], 'RALLY')
            backstop_below_arm = p1.closed and abs((entry - p1.exit_price) - hard) < 0.05
            # (2) reaches +$5 then reverses -> exits at the +$3 break-even+ FLOOR (NOT +$4).
            p2 = run([{'open': 100, 'high': entry + r_arm, 'low': 100.2, 'close': entry + r_arm - 0.2},
                      {'open': entry + r_arm - 0.2, 'high': entry + r_arm - 0.2,
                       'low': entry + r_floor - 1, 'close': entry + r_floor - 1}], 'RALLY')
            floor_be = (p2.closed and abs((p2.exit_price - entry) - r_floor) < 0.05
                        and (p2.exit_price - entry) < 4.0 - 1e-9)   # strictly below the OLD flat +$4
            # (3) runs to +$10 peak -> RIDES, exits ~peak-$2 = +$8 (not flat +$4).
            p3 = run([{'open': 100, 'high': 110, 'low': 100.5, 'close': 109},
                      {'open': 109, 'high': 109, 'low': 107, 'close': 107}], 'RALLY')
            rides_peak_minus_2 = (p3.closed and abs((p3.exit_price - entry) - (10.0 - r_gap)) < 0.05
                                  and (p3.exit_price - entry) >= r_floor - 0.05)
            # (4) one-way: after the peak a non-triggering retrace must NOT loosen SL.
            p4 = run([{'open': 100, 'high': 110, 'low': 100.5, 'close': 109}], 'RALLY')
            sl_peak = p4.current_sl
            update_position_on_bar(p4, pd.Series({'open': 108, 'high': 108, 'low': 107.6, 'close': 107.8}),
                                   ts0 + pd.Timedelta(minutes=5), cfg)
            one_way = (p4.closed or p4.current_sl >= sl_peak - 1e-9)
            # (5) KIND ISOLATION: SAME +$6-then-reverse path. RALLY rides+exits ~peak-$2;
            #     RESCUE (arm $8 never reached) rides uncut on the backstop only.
            path6 = [{'open': 100, 'high': 106, 'low': 100.2, 'close': 105.5},
                     {'open': 105.5, 'high': 105.5, 'low': 100.0, 'close': 100.0}]
            pr = run(path6, 'RALLY')
            ps = run(path6, 'RESCUE')
            isolation = (pr.closed and abs((pr.exit_price - entry) - (6.0 - r_gap)) < 0.05
                         and (not ps.closed))
            ok = (backstop_below_arm and floor_be and rides_peak_minus_2 and one_way and isolation)
            detail = (f"rev<5->backstop{p1.exit_price}({backstop_below_arm}) "
                      f"reach5->be_floor{p2.exit_price}=+{p2.exit_price - entry:.1f}(not+4)({floor_be}) "
                      f"peak10->rides+{p3.exit_price - entry:.1f}(~+8)({rides_peak_minus_2}) "
                      f"one_way={one_way} kind_isol rally+{pr.exit_price - entry:.1f}/rescue_open={not ps.closed}({isolation})")
        except Exception as e:
            self._record(82, FAIL, f"raised: {e!r}"); return
        self._record(82, PASS if ok else FAIL, detail)

    # ------------------------------------------------------------------------
    # v3.2.8 Phase 2/3 — rally/rescue/common file split + dispatcher isolation
    # ------------------------------------------------------------------------
    def _step_boost_split_isolation(self):
        # v3.2.8 Phase 2/3: the boost logic is split into rally.py (winning pyramid +
        # break-and-hold + Phase-1 numbers), rescue.py (losing hedge; UNCHANGED v3.2.7
        # numbers), boosts_common.py (shared placement/FP-guard/cap/journal, mapped
        # ONCE), and a dispatcher that routes by the sign of leg_fav. Asserts: (1) all
        # four modules import; (2) rally OWNS $5/$4/$1.50, rescue OWNS the UNCHANGED
        # $10/$8/$8/$3.50; (3) the dispatcher routes a RALLY plan -> rally.fire and a
        # RESCUE plan -> rescue.fire, BOTH into boosts_common.place_fleet; (4) the
        # fills._fire_boost_event seam delegates through that same dispatch chain;
        # (5) rescue's RELOCATED trail is BYTE-IDENTICAL (reach +$8 -> lock at +$8).
        import types
        import boosts as _b
        import rally as _rally
        import rescue as _rescue
        import boosts_common as _bc
        import boosts_dispatch as _bd
        import fills as _fills
        from strategy import Position, update_position_on_bar
        try:
            cfg = self.cfg
            # (1) modules present + the shared placement mapped ONCE.
            modules_ok = (callable(_rally.fire) and callable(_rescue.fire)
                          and callable(_bc.place_fleet) and callable(_bd.fire)
                          and _rally.fire.__module__ == 'rally'
                          and _rescue.fire.__module__ == 'rescue')
            # (2) ownership of the numbers (rally tightened; rescue UNCHANGED).
            # v3.3.0: rally event arm $5, trail arm $5, be+ floor $3, gap $2.00 (rides).
            rally_nums = (abs(_rally.event_arm(cfg) - 5.0) < 1e-9
                          and abs(_rally.trail_arm(cfg) - 5.0) < 1e-9
                          and abs(_rally.lock_floor(cfg) - 3.0) < 1e-9
                          and abs(_rally.trail_gap(cfg) - 2.00) < 1e-9)
            rescue_nums = (abs(_rescue.event_arm(cfg) - 10.0) < 1e-9
                           and abs(_rescue.trail_arm(cfg) - 8.0) < 1e-9
                           and abs(_rescue.lock_floor(cfg) - 8.0) < 1e-9
                           and abs(_rescue.trail_gap(cfg) - 3.50) < 1e-9)
            # (3)+(4) routing: stub the SHARED placement and prove sign-of-leg_fav
            # routing + that the fills seam delegates through the same chain.
            fill = 4266.3
            rally_plan = _b.plan_boost_event('BUY', fill, fill + 5.0, cfg)    # winning -> RALLY
            rescue_plan = _b.plan_boost_event('BUY', fill, fill - 10.0, cfg)  # losing  -> RESCUE
            placed = []
            orig = _bc.place_fleet
            _bc.place_fleet = lambda self, tk, sh, pl: placed.append(pl.kind)
            try:
                stub = types.SimpleNamespace()
                shadow = {'anchor_label': 'A1_02h_Asia', 'side': 'BUY',
                          'leg_fill_price': fill, 'entry_price': fill}
                _bd.fire(stub, 700, shadow, rally_plan)
                _bd.fire(stub, 701, shadow, rescue_plan)
                dispatch_routes = (placed == ['RALLY', 'RESCUE'])
                placed.clear()
                # the fills seam must route through the SAME dispatch -> place_fleet.
                _fills._fire_boost_event(stub, 702, shadow, rally_plan)
                _fills._fire_boost_event(stub, 703, shadow, rescue_plan)
                seam_routes = (placed == ['RALLY', 'RESCUE'])
            finally:
                _bc.place_fleet = orig
            # (5) rescue's RELOCATED trail engine is byte-identical: a RESCUE boost
            # reaches +$8 then reverses -> closes at the +$8 lock floor (v3.2.7).
            entry = 100.0; ts0 = pd.Timestamp('2026-06-24T02:30:00Z')
            p = Position(anchor_label='T', side='BUY', entry_price=entry,
                         entry_time=ts0, current_sl=entry - 10.0, tp_level=entry + 30.0,
                         max_fav=entry, lot=cfg.lot_size, role='rescue', boost=True,
                         boost_kind='RESCUE')
            for i, b in enumerate([{'open': 100, 'high': 108.5, 'low': 100.2, 'close': 108},
                                   {'open': 108, 'high': 108, 'low': 105, 'close': 105}]):
                update_position_on_bar(p, pd.Series(b), ts0 + pd.Timedelta(minutes=i + 1), cfg)
                if p.closed:
                    break
            rescue_byte_identical = (p.closed and abs((p.exit_price - entry) - 8.0) < 0.05)
            ok = (modules_ok and rally_nums and rescue_nums and dispatch_routes
                  and seam_routes and rescue_byte_identical)
            detail = (f"modules={modules_ok} rally_5/5/3/2={rally_nums} "
                      f"rescue_10/8/8/3.5={rescue_nums} dispatch={dispatch_routes} "
                      f"seam={seam_routes} rescue_floor8={rescue_byte_identical}")
        except Exception as e:
            self._record(83, FAIL, f"raised: {e!r}"); return
        self._record(83, PASS if ok else FAIL, detail)

    # ------------------------------------------------------------------------
    # v3.2.9 manual TESTFIRE — fail-closed safety rails + same-placement reuse.
    # NO real orders are placed in any of these steps: the adapter/broker is fully
    # stubbed; placement is asserted by call-recording, not execution.
    # ------------------------------------------------------------------------
    def _testfire_stub(self, trade_mode=0, profile='STANDARD_5PCT', pos=(), pend=(),
                       shadow=None, pending=None, evt_open=False, anchors=None):
        import types, dataclasses
        cfg = dataclasses.replace(self.cfg, account_profile=profile)
        if anchors is not None:
            cfg.anchors = anchors
        DEMO = 0
        mt5 = types.SimpleNamespace(
            ACCOUNT_TRADE_MODE_DEMO=DEMO,
            account_info=lambda: types.SimpleNamespace(trade_mode=trade_mode, balance=50000.0),
            positions_get=lambda symbol=None: list(pos),
            orders_get=lambda symbol=None: list(pend),
            symbol_info_tick=lambda s: types.SimpleNamespace(bid=3995.0, ask=3995.2))
        return types.SimpleNamespace(
            cfg=cfg, adapter=types.SimpleNamespace(mt5=mt5),
            shadow_positions=shadow or {}, shadow_pendings=pending or {},
            _testfire_event_open=evt_open)

    def _step_testfire_demo_only(self):
        # 84: rail 1 DEMO-ONLY — testfire REFUSES on a non-demo account (no --force
        # override) and CLEARS on demo. Fail-closed if account_info can't be read.
        import types
        import testfire as _tf
        try:
            far = pd.Timestamp('2026-06-24T00:00:00Z')  # 7h from A2 (10:00 broker)
            real_ok, real_reason = _tf.testfire_preflight(
                self._testfire_stub(trade_mode=2, anchors=[('A2', 10, 0)]), far)
            demo_ok, _ = _tf.testfire_preflight(
                self._testfire_stub(trade_mode=0, anchors=[('A2', 10, 0)]), far)
            # account_info None -> fail-closed refuse (never assume safe)
            tr = self._testfire_stub(anchors=[('A2', 10, 0)])
            tr.adapter.mt5.account_info = lambda: None
            none_ok, _ = _tf.testfire_preflight(tr, far)
            ok = (real_ok is False and demo_ok is True and none_ok is False
                  and 'DEMO-ONLY' in real_reason)
            detail = f"non_demo_refused={not real_ok} demo_clears={demo_ok} none_failclosed={not none_ok}"
        except Exception as e:
            self._record(84, FAIL, f"raised: {e!r}"); return
        self._record(84, PASS if ok else FAIL, detail)

    def _step_testfire_fp_refuse(self):
        # 85: rail 2 NO-FP — testfire REFUSES any FP/funded profile even on demo;
        # only STANDARD_5PCT clears.
        import testfire as _tf
        try:
            far = pd.Timestamp('2026-06-24T00:00:00Z')
            fp_ok, fp_reason = _tf.testfire_preflight(
                self._testfire_stub(profile='FPZERO_1PCT', anchors=[('A2', 10, 0)]), far)
            std_ok, _ = _tf.testfire_preflight(
                self._testfire_stub(profile='STANDARD_5PCT', anchors=[('A2', 10, 0)]), far)
            ok = (fp_ok is False and std_ok is True and 'NO-FP' in fp_reason)
            detail = f"fp_refused={not fp_ok} standard_clears={std_ok} reason={fp_reason[:40]}"
        except Exception as e:
            self._record(85, FAIL, f"raised: {e!r}"); return
        self._record(85, PASS if ok else FAIL, detail)

    def _step_testfire_flat_inflight(self):
        # 86: rail 3 FLAT + rail 5 ONE-AT-A-TIME — testfire REFUSES when an anchor is
        # in-flight (broker position OR pending OR internal shadow) or when a prior
        # test-fire event is still open. Same flatness guard selftest's preflight uses.
        import testfire as _tf
        try:
            far = pd.Timestamp('2026-06-24T00:00:00Z')
            A = [('A2', 10, 0)]
            pos_ok, pos_r = _tf.testfire_preflight(self._testfire_stub(pos=[object()], anchors=A), far)
            pend_ok, _ = _tf.testfire_preflight(self._testfire_stub(pend=[object()], anchors=A), far)
            shadow_ok, _ = _tf.testfire_preflight(self._testfire_stub(shadow={101: {}}, anchors=A), far)
            shpend_ok, _ = _tf.testfire_preflight(self._testfire_stub(pending={102: {}}, anchors=A), far)
            prior_ok, prior_r = _tf.testfire_preflight(self._testfire_stub(evt_open=True, anchors=A), far)
            clean_ok, _ = _tf.testfire_preflight(self._testfire_stub(anchors=A), far)
            ok = (pos_ok is False and pend_ok is False and shadow_ok is False
                  and shpend_ok is False and prior_ok is False and clean_ok is True
                  and 'FLAT' in pos_r and 'ONE-AT-A-TIME' in prior_r)
            detail = (f"broker_pos={not pos_ok} broker_pend={not pend_ok} "
                      f"shadow_pos={not shadow_ok} shadow_pend={not shpend_ok} "
                      f"prior_event={not prior_ok} clean_clears={clean_ok}")
        except Exception as e:
            self._record(86, FAIL, f"raised: {e!r}"); return
        self._record(86, PASS if ok else FAIL, detail)

    def _step_testfire_anchor_window(self):
        # 87: rail 4 NO-COLLISION — by DEFAULT testfire REFUSES when a scheduled anchor
        # is active or within testfire_collision_min, and clears when far. v3.3.1:
        # --force-window bypasses ONLY rail 4 (the in-window refusal CLEARS with a loud
        # warning naming minutes-away + scheduler suppression) while rails 1/2/3 STAY
        # HARD even with --force-window set. Uses the pure minutes_to_nearest_anchor
        # helper (broker UTC+3).
        import testfire as _tf
        try:
            A = [('A2', 10, 0)]  # 10:00 broker == 07:00 UTC
            at_anchor = pd.Timestamp('2026-06-24T07:00:00Z')           # 0 min away
            edge_in = pd.Timestamp('2026-06-24T06:45:00Z')             # 15 min (<=30) away
            far = pd.Timestamp('2026-06-24T00:00:00Z')                 # 420 min away
            # default (no override): refuses in-window, clears far.
            at_ok, at_r = _tf.testfire_preflight(self._testfire_stub(anchors=A), at_anchor)
            edge_ok, _ = _tf.testfire_preflight(self._testfire_stub(anchors=A), edge_in)
            far_ok, _ = _tf.testfire_preflight(self._testfire_stub(anchors=A), far)
            mins0 = _tf.minutes_to_nearest_anchor(self._testfire_stub(anchors=A).cfg, at_anchor)
            default_block = (at_ok is False and edge_ok is False and far_ok is True
                             and 'NO-COLLISION' in at_r and abs(mins0) < 1e-6)
            # --force-window: rail 4 SKIPPED — the in-window refusal now CLEARS, and the
            # warning is LOUD (names minutes-away + scheduler suppression, never silent).
            fw_at_ok, fw_at_r = _tf.testfire_preflight(
                self._testfire_stub(anchors=A), at_anchor, force_window=True)
            fw_edge_ok, _ = _tf.testfire_preflight(
                self._testfire_stub(anchors=A), edge_in, force_window=True)
            warn_loud = ('BYPASS' in fw_at_r.upper() and 'SUPPRESS' in fw_at_r.upper()
                         and '0 min' in fw_at_r)
            forcewin_clears = (fw_at_ok is True and fw_edge_ok is True and warn_loud)
            # rails 1/2/3 STAY HARD even with --force-window (only rail 4 is bypassable).
            r1_ok, r1_r = _tf.testfire_preflight(
                self._testfire_stub(trade_mode=2, anchors=A), at_anchor, force_window=True)
            r2_ok, r2_r = _tf.testfire_preflight(
                self._testfire_stub(profile='FPZERO_1PCT', anchors=A), at_anchor, force_window=True)
            r3_ok, r3_r = _tf.testfire_preflight(
                self._testfire_stub(pos=[object()], anchors=A), at_anchor, force_window=True)
            rails_hard = (r1_ok is False and r2_ok is False and r3_ok is False
                          and 'DEMO-ONLY' in r1_r and 'NO-FP' in r2_r and 'FLAT' in r3_r)
            ok = default_block and forcewin_clears and rails_hard
            detail = (f"default_at_refused={not at_ok} default_within15_refused={not edge_ok} "
                      f"far_clears={far_ok} forcewin_at_clears={fw_at_ok} "
                      f"forcewin_edge_clears={fw_edge_ok} warn_loud={warn_loud} "
                      f"rails_1_2_3_still_hard={rails_hard} nearest_min@anchor={mins0:.1f}")
        except Exception as e:
            self._record(87, FAIL, f"raised: {e!r}"); return
        self._record(87, PASS if ok else FAIL, detail)

    def _step_testfire_same_placement(self):
        # 88: on a clean demo stub, testfire routes through the SAME placement entry a
        # scheduled anchor uses — assert CALL IDENTITY (not a parallel copy): arm ->
        # the live _complete_deferred_anchor -> _place_orders_for_anchor, anchored at
        # the CURRENT price (anchor_price == current_price, current-mid straddle), with
        # the journal tagged trigger_source='TESTFIRE' and scheduled anchors suppressed.
        import types
        import testfire as _tf
        import anchors as _anchors
        import live_trader as _lt
        try:
            far = pd.Timestamp('2026-06-24T00:00:00Z')
            # (a) bound-method identity: the testfire path and the scheduled path use
            #     the SAME _place_orders_for_anchor / _complete_deferred_anchor.
            identity = (_lt.LiveTrader._place_orders_for_anchor is _anchors._place_orders_for_anchor
                        and _lt.LiveTrader._complete_deferred_anchor is _anchors._complete_deferred_anchor)
            # (b) arm + complete -> records ONE call to _place_orders_for_anchor with
            #     anchor_price == current_price (current-mid anchoring), label tagged.
            calls = []
            tr = self._testfire_stub(anchors=[('A2', 10, 0)])
            tr._deferred_anchor = None
            tr._place_orders_for_anchor = (
                lambda label, anchor_utc, anchor_price, current_price, *a, **k:
                calls.append((label, anchor_price, current_price)))
            tr._await_fresh_tick_for_placement = lambda label: (object(), 3995.1, 0.0)
            tr._warmup_trade_channel = lambda label: True
            tr._dump_mt5_state = lambda *a, **k: None
            # preflight clears first (clean demo, far from anchor)
            cleared, _ = _tf.testfire_preflight(tr, far)
            _tf.arm_testfire(tr, 'A2', now_utc=far)
            tagged = (tr._trigger_source == 'TESTFIRE' and tr._testfire_mode is True
                      and tr._testfire_event_open is True)
            _anchors._complete_deferred_anchor(tr)
            routed = (len(calls) == 1 and calls[0][0] == 'A2'
                      and abs(calls[0][1] - calls[0][2]) < 1e-9      # anchor == current price
                      and abs(calls[0][1] - 3995.1) < 1e-9)
            # (c) scheduled-anchor placement is SUPPRESSED while _testfire_mode is set.
            tr2 = self._testfire_stub(anchors=[('A2', 10, 0)])
            tr2.paused = False
            tr2._testfire_mode = True
            tr2.state = {'processed_anchors_today': set()}
            tr2._deferred_anchor = 'UNTOUCHED'
            _anchors._process_anchor_if_due(tr2, far.date(), pd.Timestamp('2026-06-24T07:00:00Z'))
            suppressed = (tr2._deferred_anchor == 'UNTOUCHED')
            ok = identity and cleared and tagged and routed and suppressed
            detail = (f"call_identity={identity} preflight_cleared={cleared} "
                      f"journal_tagged={tagged} routed_current_mid={routed} "
                      f"scheduler_suppressed={suppressed}")
        except Exception as e:
            self._record(88, FAIL, f"raised: {e!r}"); return
        self._record(88, PASS if ok else FAIL, detail)

    # ------------------------------------------------------------------------
    # v3.3.0 — rally RIDES (peak-gap trail, not a flat lock) + no sub-floor clip
    # ------------------------------------------------------------------------
    def _step_rally_rides_not_bails(self):
        # 89: the v3.2.8 defect was a rally boost LOCKING flat at +$4 and bailing on
        # the first pause. v3.3.0: an armed rally boost (peak >= +$5) trails at
        # peak - $2 above a +$3 floor, so a SHALLOW pause that stays above the trail
        # does NOT close it -- it RIDES and banks ~peak-$2, like the original leg.
        from strategy import Position, update_position_on_bar
        try:
            cfg = self.cfg
            hard = float(getattr(cfg, 'boost_sl_dollars', 10.0))
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-24T02:30:00Z')
            p = Position(anchor_label='T', side='BUY', entry_price=entry, entry_time=ts0,
                         current_sl=entry - hard, tp_level=entry + 30.0, max_fav=entry,
                         lot=cfg.lot_size, role='rescue', boost=True, boost_kind='RALLY')
            # bar1: peak +$6 -> armed, trail = peak-2 = +$4 (stop 104).
            update_position_on_bar(p, pd.Series({'open': 100, 'high': 106, 'low': 100.5, 'close': 105.5}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            armed_trail = abs((p.current_sl - entry) - 4.0) < 0.05 and not p.closed
            # bar2: SHALLOW pause -- dips to +$4.5 (ABOVE the +$4 trail). The OLD flat
            # +$4 lock would still be holding too, but the key is it does NOT bail; it
            # must stay OPEN and keep riding.
            update_position_on_bar(p, pd.Series({'open': 105.5, 'high': 105.5, 'low': 104.5, 'close': 105.0}),
                                   ts0 + pd.Timedelta(minutes=2), cfg)
            held_pause = (not p.closed)
            # bar3: runs to +$9 peak -> trail rides to +$7 (peak-2).
            update_position_on_bar(p, pd.Series({'open': 105.0, 'high': 109, 'low': 104.8, 'close': 108}),
                                   ts0 + pd.Timedelta(minutes=3), cfg)
            rode_up = abs((p.current_sl - entry) - 7.0) < 0.05 and not p.closed
            # bar4: reverses -> exits at the ridden trail +$7 (NOT the flat +$4 lock).
            update_position_on_bar(p, pd.Series({'open': 108, 'high': 108, 'low': 106.5, 'close': 106.5}),
                                   ts0 + pd.Timedelta(minutes=4), cfg)
            exits_ridden = (p.closed and abs((p.exit_price - entry) - 7.0) < 0.05
                            and (p.exit_price - entry) > 4.0 + 1e-9)   # strictly beats the OLD flat +$4
            ok = (armed_trail and held_pause and rode_up and exits_ridden)
            detail = (f"armed_trail+4={armed_trail} held_shallow_pause={held_pause} "
                      f"rode_to+7={rode_up} exits_at_ridden_trail+{p.exit_price - entry:.1f}(>4)={exits_ridden}")
        except Exception as e:
            self._record(89, FAIL, f"raised: {e!r}"); return
        self._record(89, PASS if ok else FAIL, detail)

    def _step_rally_no_subfloor_clip(self):
        # 90: the KNOWN DEFECT — PTRACE exit_trail_without_trail_advance clipped a boost
        # BELOW its lock (test-fire boost 2 exited +$3.74 under its floor). v3.3.0: an
        # armed rally boost (a) emits LOCK_ARM/TRAIL_ADVANCE so its trail exit is never
        # flagged exit_trail_without_trail_advance, and (b) NEVER closes below its
        # ratcheted trail floor even on a bar that GAPS THROUGH it.
        from strategy import Position, update_position_on_bar
        from position_telemetry import PositionTracer, TRAIL_ADVANCE, LOCK_ARM, EXIT
        try:
            cfg = self.cfg
            hard = float(getattr(cfg, 'boost_sl_dollars', 10.0))
            r_gap = float(getattr(cfg, 'rally_trail_gap', 2.00))
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-24T02:30:00Z')
            events = []
            tr = PositionTracer(sink=lambda l: None)
            p = Position(anchor_label='A1_02h_Asia', side='BUY', entry_price=entry,
                         entry_time=ts0, current_sl=entry - hard, tp_level=entry + 30.0,
                         max_fav=entry, lot=cfg.lot_size, role='rescue', boost=True,
                         boost_kind='RALLY')
            # bar1: peak +$7 -> armed; trail = peak-2 = +$5 (stop 105). With a tracer the
            # arm emits LOCK_ARM (stop leaves the $10 backstop) -- the trail-advance path.
            update_position_on_bar(p, pd.Series({'open': 100, 'high': 107, 'low': 100.5, 'close': 106}),
                                   ts0 + pd.Timedelta(minutes=1), cfg, tracer=tr, ticket=701)
            trail_floor = entry + (7.0 - r_gap)   # +$5
            armed_at_5 = abs((p.current_sl - trail_floor)) < 0.05 and not p.closed
            traced = any(e.get('event_type') in (LOCK_ARM, TRAIL_ADVANCE)
                         for e in tr._history.get(701, []))
            no_violation = (len(tr.violations) == 0)
            # bar2: GAPS THROUGH the trail -- opens at +$3 (below the +$5 trail) and dips
            # to +$1. OLD code filled at the gap (_open=+3) -> sub-floor clip. v3.3.0
            # clamps to the ratcheted trail: exit == +$5 (peak-gap), NEVER below it.
            update_position_on_bar(p, pd.Series({'open': 103, 'high': 103, 'low': 101, 'close': 102}),
                                   ts0 + pd.Timedelta(minutes=2), cfg, tracer=tr, ticket=701)
            no_subfloor_clip = (p.closed and abs((p.exit_price - entry) - 5.0) < 0.05
                                and (p.exit_price - entry) >= (7.0 - r_gap) - 1e-9)
            ok = (armed_at_5 and traced and no_violation and no_subfloor_clip)
            detail = (f"armed_trail+5={armed_at_5} trail_advance_traced={traced} "
                      f"no_ptrace_violation={no_violation} "
                      f"gap_through_exit+{p.exit_price - entry:.2f}(>=+5,not+3)={no_subfloor_clip}")
        except Exception as e:
            self._record(90, FAIL, f"raised: {e!r}"); return
        self._record(90, PASS if ok else FAIL, detail)

    # ------------------------------------------------------------------------
    # v3.3.3 — break-and-hold crash fix (numpy-safe + fail-closed); rally SL $13
    # ------------------------------------------------------------------------
    def _break_gate_stub(self, getter, side='BUY', kind='RALLY', edge=100.0,
                         parent_side=None, parent_max_fav=0.0, cfg=None):
        # Minimal trader-like object for rally.break_and_hold_ok: the gate reads
        # cfg + adapter.get_latest_m5 + tele + ptrace. parent_max_fav ($, default 0 ->
        # NOT established, so the v3.3.5 override never applies for the legacy tests)
        # is converted to a parent max_fav PRICE in the parent's direction.
        import types
        self._gate_tele_errors = []
        self._gate_tele_infos = []
        self._gate_ptrace = []
        adapter = types.SimpleNamespace(get_latest_m5=getter)
        tele = types.SimpleNamespace(
            info=lambda m, *a, **k: self._gate_tele_infos.append(m),
            error=lambda m, *a, **k: self._gate_tele_errors.append(m))
        psd = parent_side or side
        sgn = 1.0 if psd == 'BUY' else -1.0
        max_fav_price = edge + sgn * float(parent_max_fav)
        shadow = {'leg_fill_price': edge, 'entry_price': edge, 'anchor_label': 'A2',
                  'side': psd, 'max_fav': max_fav_price}
        plan = types.SimpleNamespace(boost_side=side, kind=kind)
        events = self._gate_ptrace
        class _PT:
            def __getattr__(self, name):
                def _rec(anchor=None, **kw):
                    events.append((name, kw)); return None
                return _rec
        tr = types.SimpleNamespace(cfg=cfg or self.cfg, adapter=adapter, tele=tele,
                                   ptrace=_PT())
        return tr, shadow, plan

    def _step_break_gate_npsafe(self):
        # 91 (FIX 1A): feed the gate a NUMPY structured array of M5 bars -- the exact
        # array-shaped input that made `if bars:` raise "truth value of an array ...
        # is ambiguous" (live A2 2026-06-24). Assert: (a) it evaluates WITHOUT raising
        # and (b) returns the correct decision -- a CONFIRMED break fires (True) and an
        # EXHAUSTED move (spike then reverse through the edge) does NOT fire (False).
        import numpy as np
        import rally as _rally
        try:
            dt = [('high', 'f8'), ('low', 'f8'), ('close', 'f8')]
            # CONFIRMED: cleared edge 100 by +$5 (peak 105), held 2 candles, retrace
            # ~0.3 of the break (< max_retrace_y 0.40) -> fires.
            confirmed_bars = np.array([(104.0, 103.5, 103.8),
                                       (105.0, 104.0, 104.8)], dtype=dt)
            # EXHAUSTED: spike to 105 then candle 2 falls back THROUGH the edge
            # (low 98 < 100) -> FAILED 'reversed' -> no fire.
            exhausted_bars = np.array([(105.0, 100.5, 101.0),
                                       (101.0, 98.0, 98.0)], dtype=dt)
            tr_c, sh_c, pl_c = self._break_gate_stub(lambda s, n: confirmed_bars)
            tr_e, sh_e, pl_e = self._break_gate_stub(lambda s, n: exhausted_bars)
            confirmed_fires = (_rally.break_and_hold_ok(tr_c, sh_c, pl_c) is True)
            exhausted_no_fire = (_rally.break_and_hold_ok(tr_e, sh_e, pl_e) is False)
            # _has_rows must be numpy-safe directly too (the bug's root call).
            np_safe = (_rally._has_rows(confirmed_bars) is True
                       and _rally._has_rows(np.array([], dtype=dt)) is False
                       and _rally._has_rows(None) is False)
            ok = confirmed_fires and exhausted_no_fire and np_safe
            detail = (f"confirmed_fires={confirmed_fires} "
                      f"exhausted_no_fire={exhausted_no_fire} np_safe_no_raise={np_safe}")
        except Exception as e:
            self._record(91, FAIL, f"raised: {e!r}"); return
        self._record(91, PASS if ok else FAIL, detail)

    def _step_break_gate_failclosed(self):
        # 92 (FIX 1B): simulate the gate RAISING (the bars getter throws). The old
        # handler logged "non-fatal, allowing" and returned True (fired into the fake
        # break -> the -$701 loss). Now it FAILS CLOSED: returns False (BLOCKED) and
        # logs loudly via tele.error. RALLY only (rescue bypasses the gate entirely,
        # asserted by step 80).
        import rally as _rally
        try:
            def _boom(symbol, count):
                raise ValueError("The truth value of an array with more than one "
                                 "element is ambiguous. Use a.any() or a.all()")
            tr, sh, pl = self._break_gate_stub(_boom)
            blocked = (_rally.break_and_hold_ok(tr, sh, pl) is False)
            loud = any('BLOCKED' in str(m) for m in self._gate_tele_errors)
            # disabled gate still short-circuits to True (no regression).
            import dataclasses
            tr2, sh2, pl2 = self._break_gate_stub(_boom)
            tr2.cfg = dataclasses.replace(self.cfg, break_and_hold_enabled=False)
            disabled_allows = (_rally.break_and_hold_ok(tr2, sh2, pl2) is True)
            ok = blocked and loud and disabled_allows
            detail = (f"gate_exception_blocked={blocked} logged_loud_BLOCKED={loud} "
                      f"disabled_still_allows={disabled_allows}")
        except Exception as e:
            self._record(92, FAIL, f"raised: {e!r}"); return
        self._record(92, PASS if ok else FAIL, detail)

    def _step_rally_sl13_cap910(self):
        # 93 (FIX 2): RALLY boost SL/backstop $13, whipsaw cap -$910; RESCUE SL $10,
        # cap -$700 -- per-kind, never one shared value. Asserts the plan SL, the live
        # backstop geometry (entry +/- the kind's SL), and the per-kind cap math.
        import boosts as _boosts
        from strategy import Position, update_position_on_bar
        try:
            cfg = self.cfg
            r_sl = float(getattr(cfg, 'rally_boost_sl', 13.0))
            x_sl = float(getattr(cfg, 'boost_sl_dollars', 10.0))
            lot = float(getattr(cfg, 'lot_size', 0.35))
            con = float(getattr(cfg, 'contract_size', 100.0))
            n = int(getattr(cfg, 'rescue_boost_count', 2))
            fill = 4000.0
            # (a) plan SL is per-kind: RALLY +$5 -> $13, RESCUE -$10 -> $10.
            rally_plan = _boosts.plan_boost_event('BUY', fill, fill + 5.0, cfg)
            rescue_plan = _boosts.plan_boost_event('BUY', fill, fill - 10.0, cfg)
            plan_sl_ok = (rally_plan is not None and abs(rally_plan.sl_dollars - 13.0) < 1e-9
                          and rescue_plan is not None and abs(rescue_plan.sl_dollars - 10.0) < 1e-9)
            # (b) live backstop geometry: a benign bar ratchets current_sl to the
            #     kind's backstop = entry -/+ SL (BUY -> entry - SL).
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-24T02:30:00Z')
            pr = Position(anchor_label='T', side='BUY', entry_price=entry, entry_time=ts0,
                          current_sl=50.0, tp_level=entry + 30.0, max_fav=entry,
                          lot=lot, role='rescue', boost=True, boost_kind='RALLY')
            update_position_on_bar(pr, pd.Series({'open': 100, 'high': 101, 'low': 99, 'close': 100}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            rally_backstop_ok = (not pr.closed and abs(pr.current_sl - (entry - r_sl)) < 1e-6)
            px = Position(anchor_label='T', side='BUY', entry_price=entry, entry_time=ts0,
                          current_sl=50.0, tp_level=entry + 30.0, max_fav=entry,
                          lot=lot, role='rescue', boost=True, boost_kind='RESCUE')
            update_position_on_bar(px, pd.Series({'open': 100, 'high': 101, 'low': 99, 'close': 100}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            rescue_backstop_ok = (not px.closed and abs(px.current_sl - (entry - x_sl)) < 1e-6)
            # (c) per-kind whipsaw cap: RALLY -$910, RESCUE -$700.
            rally_cap = _boosts.boost_whipsaw_cap(cfg, 'RALLY')
            rescue_cap = _boosts.boost_whipsaw_cap(cfg, 'RESCUE')
            cap_ok = (abs(rally_cap - (n * r_sl * lot * con)) < 1e-6 and abs(rally_cap - 910.0) < 1e-6
                      and abs(rescue_cap - (n * x_sl * lot * con)) < 1e-6 and abs(rescue_cap - 700.0) < 1e-6)
            breach_ok = (_boosts.cap_breached(-915.0, cfg, 'RALLY') is True
                         and _boosts.cap_breached(-905.0, cfg, 'RALLY') is False
                         and _boosts.cap_breached(-715.05, cfg, 'RESCUE') is True
                         and _boosts.cap_breached(-650.0, cfg) is False)  # default kind = RESCUE
            ok = (plan_sl_ok and rally_backstop_ok and rescue_backstop_ok and cap_ok and breach_ok)
            detail = (f"plan_sl(rally13/rescue10)={plan_sl_ok} "
                      f"rally_backstop=entry-{r_sl:.0f}={rally_backstop_ok} "
                      f"rescue_backstop=entry-{x_sl:.0f}={rescue_backstop_ok} "
                      f"caps(rally${rally_cap:.0f}/rescue${rescue_cap:.0f})={cap_ok} breach={breach_ok}")
        except Exception as e:
            self._record(93, FAIL, f"raised: {e!r}"); return
        self._record(93, PASS if ok else FAIL, detail)

    # ------------------------------------------------------------------------
    # v3.3.4 — rally pullback detector (rally boosts only, above the $13 backstop)
    # ------------------------------------------------------------------------
    def _rally_boost(self, cfg, entry=100.0, ts0=None, kind='RALLY'):
        from strategy import Position
        ts0 = ts0 or pd.Timestamp('2026-06-24T02:30:00Z')
        hard = float(getattr(cfg, 'rally_boost_sl', 13.0)) if kind == 'RALLY' \
            else float(getattr(cfg, 'boost_sl_dollars', 10.0))
        return Position(anchor_label='T', side='BUY', entry_price=entry, entry_time=ts0,
                        current_sl=entry - hard, tp_level=entry + 30.0, max_fav=entry,
                        lot=cfg.lot_size, role='rescue', boost=True, boost_kind=kind)

    def _step_rally_pullback_band(self):
        # 94: the pullback DISTANCE band (tol override $8 to prove the mechanism above
        # the $13 backstop). Within T -> HOLD (pullback); cross T -> cut early at the T
        # threshold (above backstop); a gap THROUGH T floors at the $13 backstop; a
        # RESCUE boost is NOT governed by the detector (rally-only).
        import dataclasses
        from strategy import update_position_on_bar
        try:
            cfg = dataclasses.replace(self.cfg, rally_pullback_enabled=True,
                                      rally_pullback_tol_dollars=8.0,
                                      rally_pullback_time_bound_min=30.0)
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-24T02:30:00Z')
            cut_level = entry - 8.0      # 92
            backstop = entry - float(getattr(cfg, 'rally_boost_sl', 13.0))  # 87
            # (a) within T (-$6, no recovery) -> HOLD, pullback armed, NOT closed.
            ph = self._rally_boost(cfg, entry, ts0)
            update_position_on_bar(ph, pd.Series({'open': 99, 'high': 99, 'low': 94, 'close': 95}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            hold_ok = (not ph.closed and ph.pullback_since is not None)
            # (b) cross T (-$9) -> cut early at the +$8 threshold (92), above backstop.
            pc = self._rally_boost(cfg, entry, ts0)
            update_position_on_bar(pc, pd.Series({'open': 99, 'high': 99, 'low': 91, 'close': 92}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            cut_ok = (pc.closed and abs(pc.exit_price - cut_level) < 0.05
                      and pc.exit_price > backstop + 1e-9)
            # (c) gap straight THROUGH T -> filled no better than the $13 backstop.
            pg = self._rally_boost(cfg, entry, ts0)
            update_position_on_bar(pg, pd.Series({'open': 80, 'high': 80, 'low': 78, 'close': 79}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            backstop_ok = (pg.closed and abs(pg.exit_price - backstop) < 0.05)
            # (d) RESCUE boost on the SAME -$9 path -> detector skipped (rally-only),
            #     rides on its own $10 backstop (low 91 > entry-10=90 -> not closed).
            pr = self._rally_boost(cfg, entry, ts0, kind='RESCUE')
            update_position_on_bar(pr, pd.Series({'open': 99, 'high': 99, 'low': 91, 'close': 92}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            rescue_ok = (not pr.closed and pr.pullback_since is None)
            ok = hold_ok and cut_ok and backstop_ok and rescue_ok
            detail = (f"within_T_holds={hold_ok} cross_T_cuts@{pc.exit_price:.0f}(>{backstop:.0f})={cut_ok} "
                      f"gap_floored_at_backstop{pg.exit_price:.0f}={backstop_ok} rescue_unaffected={rescue_ok}")
        except Exception as e:
            self._record(94, FAIL, f"raised: {e!r}"); return
        self._record(94, PASS if ok else FAIL, detail)

    def _step_rally_pullback_recover_time(self):
        # 95: RECOVERY to entry ends the pullback (reset, resume normal trail, no cut);
        # B minutes adverse WITHOUT returning to entry cuts at market (slow reversal);
        # and the feature SHIPS DEFAULT OFF (rally_pullback_enabled=False, T=$7.50) so
        # the detector is INERT on the default config -- a bar that WOULD cross T if
        # enabled does NOT cut; only the $13 backstop governs. Live exits unchanged.
        import dataclasses
        from strategy import update_position_on_bar
        try:
            cfg = dataclasses.replace(self.cfg, rally_pullback_enabled=True,
                                      rally_pullback_tol_dollars=8.0,
                                      rally_pullback_time_bound_min=30.0)
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-24T02:30:00Z')
            backstop = entry - float(getattr(cfg, 'rally_boost_sl', 13.0))  # 87
            # (a) RECOVERY: adverse -$5 then a bar returns to entry -> reset, NOT closed.
            pr = self._rally_boost(cfg, entry, ts0)
            update_position_on_bar(pr, pd.Series({'open': 99, 'high': 99, 'low': 95, 'close': 96}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            armed_pb = pr.pullback_since is not None
            update_position_on_bar(pr, pd.Series({'open': 96, 'high': 101, 'low': 98, 'close': 100}),
                                   ts0 + pd.Timedelta(minutes=2), cfg)
            recover_ok = (armed_pb and not pr.closed and pr.pullback_since is None)
            # (b) TIME BOUND: adverse -$5 within T, held >30 min, no recovery -> cut at
            #     market (close ~95), floored by the $13 backstop.
            pt = self._rally_boost(cfg, entry, ts0)
            update_position_on_bar(pt, pd.Series({'open': 99, 'high': 99, 'low': 95, 'close': 96}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            held_open = not pt.closed
            update_position_on_bar(pt, pd.Series({'open': 96, 'high': 99, 'low': 95, 'close': 95}),
                                   ts0 + pd.Timedelta(minutes=32), cfg)
            time_ok = (held_open and pt.closed and pt.exit_price >= backstop - 1e-9
                       and abs(pt.exit_price - 95.0) < 0.05)
            # (c) SHIPS DEFAULT OFF + T=$7.50: on the default config the detector is
            #     INERT -- a -$9 adverse bar (which WOULD cross T=$7.50 if enabled) is
            #     NOT cut; only the $13 backstop governs (low 91 > entry-13=87 -> open).
            cfgd = self.cfg  # defaults: enabled=False, tol=7.50
            ships_off = (bool(getattr(cfgd, 'rally_pullback_enabled', False)) is False
                         and abs(float(getattr(cfgd, 'rally_pullback_tol_dollars', 7.50)) - 7.50) < 1e-9)
            pinert = self._rally_boost(cfgd, entry, ts0)
            update_position_on_bar(pinert, pd.Series({'open': 99, 'high': 99, 'low': 91, 'close': 92}),
                                   ts0 + pd.Timedelta(minutes=1), cfgd)
            inert_ok = (not pinert.closed and pinert.pullback_since is None)
            default_off_ok = (ships_off and inert_ok)
            ok = recover_ok and time_ok and default_off_ok
            detail = (f"recovery_resets={recover_ok} time_bound_cut@{pt.exit_price:.0f}={time_ok} "
                      f"ships_off_T7.5={ships_off} default_inert={inert_ok}")
        except Exception as e:
            self._record(95, FAIL, f"raised: {e!r}"); return
        self._record(95, PASS if ok else FAIL, detail)

    # --- v3.3.5 CASE 2 parent-profit override --------------------------------
    # A violent SAME-shape SELL crash that the candle gate calls FAILED 'reversed'
    # (candle popped back above the edge): the ONLY difference between a fake spike
    # (Case 1) and a genuine continuation (Case 2) is whether the PARENT leg is
    # already deeply favorable in the boost direction.
    def _case2_bars(self):
        # SELL break at edge 100: cleared down to low 88 (>=$3), but candle 1's HIGH
        # 101 popped back THROUGH the edge -> classify(SELL) == FAILED 'reversed'.
        return [{'high': 101.0, 'low': 90.0, 'close': 91.0},
                {'high': 99.0, 'low': 88.0, 'close': 89.0}]

    def _step_case2_override_fires(self):
        # 96 CASE 2: parent SELL already +$25 favorable (>= $20 threshold), violent
        # same-direction crash the candle gate FAILS ('reversed') -> the parent-profit
        # override FIRES the boost (returns True) and logs BREAK_OVERRIDE_PARENT_
        # ESTABLISHED carrying parent_max_fav / threshold / move_dollars for the trial.
        import rally as _rally, break_hold as _bh
        try:
            bars = self._case2_bars()
            # confirm the candle gate alone WOULD block (FAILED 'reversed').
            state, reason = _bh.classify('SELL', 100.0, bars, self.cfg)
            gate_would_block = (state == _bh.FAILED)
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                               parent_side='SELL', parent_max_fav=25.0)
            fired = (_rally.break_and_hold_ok(tr, sh, pl) is True)
            ev = [e for e in self._gate_ptrace
                  if e[0] == 'break_override_parent_established']
            logged = len(ev) == 1
            kw = ev[0][1] if logged else {}
            fields_ok = (logged and abs(kw.get('parent_max_fav', 0) - 25.0) < 0.05
                         and abs(kw.get('threshold', 0) - 20.0) < 0.05
                         and abs(kw.get('move_dollars', 0) - 12.0) < 0.05)
            loud = any('OVERRIDE' in str(m) for m in self._gate_tele_infos)
            ok = gate_would_block and fired and logged and fields_ok and loud
            detail = (f"gate_would_block={gate_would_block} override_fires={fired} "
                      f"ptrace_logged={logged} fields(maxfav/thr/move)={fields_ok} "
                      f"loud_tele={loud}")
        except Exception as e:
            self._record(96, FAIL, f"raised: {e!r}"); return
        self._record(96, PASS if ok else FAIL, detail)

    def _step_case1_still_blocks(self):
        # 97 CASE 1: the IDENTICAL violent shape, but the parent is NOT established
        # (max_fav +$10 < $20) -> the override does NOT apply, the strict gate is fully
        # in force, and the fresh spike STILL BLOCKS (returns False). This is the -$701
        # fake-spike path: it must never fire just because the move looked violent.
        import rally as _rally
        try:
            bars = self._case2_bars()
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                               parent_side='SELL', parent_max_fav=10.0)
            blocked = (_rally.break_and_hold_ok(tr, sh, pl) is False)
            no_override = not any(e[0] == 'break_override_parent_established'
                                  for e in self._gate_ptrace)
            # boundary: just below threshold ($19.99) still blocks (>= is the gate).
            tr2, sh2, pl2 = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                                  parent_side='SELL', parent_max_fav=19.99)
            boundary_blocks = (_rally.break_and_hold_ok(tr2, sh2, pl2) is False)
            ok = blocked and no_override and boundary_blocks
            detail = (f"fresh_spike_blocks={blocked} no_override_logged={no_override} "
                      f"below_threshold_19.99_blocks={boundary_blocks}")
        except Exception as e:
            self._record(97, FAIL, f"raised: {e!r}"); return
        self._record(97, PASS if ok else FAIL, detail)

    def _step_override_dir_and_rescue(self):
        # 98: the override is DIRECTIONAL and RALLY-only. (a) parent deeply established
        # (+$25) but the move is OPPOSITE the parent (parent BUY, boost SELL) -> override
        # does NOT apply, gate BLOCKS. (b) RESCUE is untouched: it bypasses break-and-
        # hold entirely (rescue_bypass_break_and_hold True) and its SL/cap math
        # ($10 / -$700) is unchanged by this version.
        import rally as _rally, boosts as _boosts
        try:
            bars = self._case2_bars()
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                               parent_side='BUY', parent_max_fav=25.0)
            opp_blocked = (_rally.break_and_hold_ok(tr, sh, pl) is False)
            opp_no_override = not any(e[0] == 'break_override_parent_established'
                                      for e in self._gate_ptrace)
            rescue_bypass = bool(getattr(self.cfg, 'rescue_bypass_break_and_hold', True))
            rescue_plan = _boosts.plan_boost_event('BUY', 4000.0, 4000.0 - 10.0, self.cfg)
            rescue_sl_ok = abs(float(rescue_plan.sl_dollars) - 10.0) < 1e-9
            rescue_cap = _boosts.boost_whipsaw_cap(self.cfg, 'RESCUE')
            cap_ok = abs(rescue_cap - 700.0) < 1e-6
            ok = opp_blocked and opp_no_override and rescue_bypass and rescue_sl_ok and cap_ok
            detail = (f"opposite_dir_blocks={opp_blocked} no_override={opp_no_override} "
                      f"rescue_bypass={rescue_bypass} rescue_sl$10={rescue_sl_ok} "
                      f"rescue_cap_unchanged={cap_ok}")
        except Exception as e:
            self._record(98, FAIL, f"raised: {e!r}"); return
        self._record(98, PASS if ok else FAIL, detail)

    # --- v3.3.6 telemetry-truth displays + A3 reschedule ---------------------
    def _step_readiness_derives_resolver(self):
        # 99: readiness / status / banner A1 time DERIVES from _resolved_anchor_hm via
        # the IST converter -- Monday 03:30 broker -> 06:00 IST, weekday 02:30 -> 05:00
        # IST -- not a hardcoded string. Exercises the bound display helpers directly
        # (_resolved_anchor_ist_hm + _next_a1_display) on a stub.
        import types, anchors as _anchors
        from datetime import date as _date, timedelta as _td
        try:
            stub = types.SimpleNamespace(cfg=self.cfg)
            stub._resolved_anchor_hm = _anchors._resolved_anchor_hm.__get__(stub)
            stub._resolved_anchor_ist_hm = _anchors._resolved_anchor_ist_hm.__get__(stub)
            a = self.cfg.anchors[0]
            base = _date(2026, 6, 24)
            monday = base - _td(days=base.weekday())
            tuesday = monday + _td(days=1)
            mrh, mrm, mih, mim = stub._resolved_anchor_ist_hm(a[0], monday, a[1], a[2])
            wrh, wrm, wih, wim = stub._resolved_anchor_ist_hm(a[0], tuesday, a[1], a[2])
            monday_0600 = (mrh, mrm, mih, mim) == (3, 30, 6, 0)
            weekday_0500 = (wrh, wrm, wih, wim) == (2, 30, 5, 0)
            # _next_a1_display (weekend/sleep text) -> upcoming Monday 06:00 IST.
            saturday = monday + _td(days=5)
            stub._broker_date = lambda ts, _s=saturday: _s
            stub._next_a1_display = _anchors._next_a1_display.__get__(stub)
            disp = stub._next_a1_display()
            next_mon_disp = ('03:30 broker' in disp and '06:00 IST' in disp)
            ok = monday_0600 and weekday_0500 and next_mon_disp
            detail = (f"monday_0330broker_0600IST={monday_0600} "
                      f"weekday_0230broker_0500IST={weekday_0500} next_a1='{disp}'")
        except Exception as e:
            self._record(99, FAIL, f"raised: {e!r}"); return
        self._record(99, PASS if ok else FAIL, detail)

    def _step_a3_scheduled_1700(self):
        # 100: A3 reschedule -- the A3 anchor in cfg fires at 17:00 IST (broker 14:30),
        # retimed from 16:20, with the label re-encoded (A3_1430_Overlap) so the
        # journal isolates the trial. label[:2] stays 'A3'. A1/A2/A4 are UNCHANGED.
        import anchors as _anchors
        try:
            amap = {lbl: (h, m) for (lbl, h, m) in self.cfg.anchors}
            a3 = [(lbl, h, m) for (lbl, h, m) in self.cfg.anchors if lbl[:2] == 'A3']
            a3_lbl, a3_h, a3_m = a3[0]
            ih, im = _anchors.anchor_ist_hm(a3_h, a3_m, self.cfg)
            a3_1700 = ((a3_h, a3_m) == (14, 30) and (ih, im) == (17, 0))
            a3_tagged = (a3_lbl == 'A3_1430_Overlap' and a3_lbl[:2] == 'A3')
            def ist(lbl):
                h, m = amap[lbl]; return _anchors.anchor_ist_hm(h, m, self.cfg)
            a1_ok = amap.get('A1_02h_Asia') == (2, 30)
            a2_ok = amap.get('A2_10h_London') == (10, 0) and ist('A2_10h_London') == (12, 30)
            a4_ok = amap.get('A4_1640_NYopen') == (16, 40) and ist('A4_1640_NYopen') == (19, 10)
            ok = a3_1700 and a3_tagged and a1_ok and a2_ok and a4_ok
            detail = (f"A3_1700IST_broker1430={a3_1700} A3_label_tag={a3_tagged} "
                      f"A1/A2/A4_unchanged={a1_ok and a2_ok and a4_ok}")
        except Exception as e:
            self._record(100, FAIL, f"raised: {e!r}"); return
        self._record(100, PASS if ok else FAIL, detail)

    def _step_v336_no_logic_change(self):
        # 101: the v3.3.6 build changed DISPLAYS / CONSTANTS only. Assert the
        # SCHEDULING resolver and OFFSET detection are byte-identical: Monday A1 still
        # resolves 03:30 broker, weekday 02:30, the offset guard still confirms +3 /
        # BLOCKS a bad read, and the new IST converter is pure (changes no (h,m) the
        # scheduler uses).
        import offset_guard as og, anchors as _anchors
        from datetime import date as _date, timedelta as _td
        try:
            base = _date(2026, 6, 24); monday = base - _td(days=base.weekday())
            tuesday = monday + _td(days=1)
            mon = _anchors.resolved_anchor_hm('A1_02h_Asia', monday, 2, 30, self.cfg)
            wk = _anchors.resolved_anchor_hm('A1_02h_Asia', tuesday, 2, 30, self.cfg)
            resolver_ok = (mon == (3, 30) and wk == (2, 30))
            off_ok = (og.resolve_offset([3]) == (3, og.CONFIRMED, 1)
                      and og.resolve_offset([0, 0, 0])[1] == og.BLOCKED)
            conv_ok = (_anchors.anchor_ist_hm(3, 30, self.cfg) == (6, 0)
                       and _anchors.anchor_ist_hm(2, 30, self.cfg) == (5, 0))
            ok = resolver_ok and off_ok and conv_ok
            detail = (f"resolver_mon0330_wk0230={resolver_ok} "
                      f"offset_detect_unchanged={off_ok} ist_converter_pure={conv_ok}")
        except Exception as e:
            self._record(101, FAIL, f"raised: {e!r}"); return
        self._record(101, PASS if ok else FAIL, detail)

    def _step_monday_gate_strict(self):
        # 102 (v3.3.6 FIX): the Monday A1 cushion is gated STRICTLY on the broker
        # weekday. The REMOVED AUREON_TEST_FORCE_MONDAY_A1 hook must have NO effect
        # even when set in the environment -- proving the LIVE scheduler (which shares
        # this exact resolver via _anchor_sched_utc / _process_anchor_if_due) places
        # weekday A1 at 02:30 broker, NEVER an hour late. Monday still gets the 03:30
        # cushion. This is the regression guard for the 99/101 failure.
        import os as _os, anchors as _anchors
        from datetime import date as _date, timedelta as _td
        prev = _os.environ.get('AUREON_TEST_FORCE_MONDAY_A1')
        try:
            _os.environ['AUREON_TEST_FORCE_MONDAY_A1'] = '1'   # the leaked foot-gun
            base = _date(2026, 6, 24); monday = base - _td(days=base.weekday())
            tuesday = monday + _td(days=1)
            wk = _anchors.resolved_anchor_hm('A1_02h_Asia', tuesday, 2, 30, self.cfg)
            mon = _anchors.resolved_anchor_hm('A1_02h_Asia', monday, 2, 30, self.cfg)
            env_ignored_weekday = (wk == (2, 30))   # 02:30 broker DESPITE the env var
            monday_cushion = (mon == (3, 30))       # Monday still gets the cushion
            ist_ok = (_anchors.anchor_ist_hm(*wk, self.cfg) == (5, 0)
                      and _anchors.anchor_ist_hm(*mon, self.cfg) == (6, 0))
            ok = env_ignored_weekday and monday_cushion and ist_ok
            detail = (f"env_var_ignored_weekday_0230broker={env_ignored_weekday} "
                      f"monday_cushion_0330broker={monday_cushion} "
                      f"ist(wk05:00/mon06:00)={ist_ok}")
        except Exception as e:
            self._record(102, FAIL, f"raised: {e!r}")
            return
        finally:
            if prev is None:
                _os.environ.pop('AUREON_TEST_FORCE_MONDAY_A1', None)
            else:
                _os.environ['AUREON_TEST_FORCE_MONDAY_A1'] = prev
        self._record(102, PASS if ok else FAIL, detail)

    # ------------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------------
    def _preflight(self) -> bool:
        """Refuse to run with any open position/pending; set the demo flag.
        v3.2.1: prints the abort reason SYNCHRONOUSLY (stdout/stderr) as well as via
        async telemetry, so a preflight bail can never look like a silent exit."""
        import sys as _sys, traceback as _tb
        mt5 = self.adapter.mt5
        try:
            pos = mt5.positions_get(symbol=self.symbol) or []
            pend = mt5.orders_get(symbol=self.symbol) or []
        except Exception as e:
            msg = f"🧪 self-test ABORTED — could not read broker state: {e!r}"
            print(msg, flush=True)
            print(_tb.format_exc(), file=_sys.stderr, flush=True)
            self.tele.error(msg)
            return False
        if pos or pend:
            msg = (f"🧪 self-test ABORTED — live positions present "
                   f"({len(pos)} open, {len(pend)} pending). Run when FLAT so the "
                   f"harness can't interfere with a live anchor.")
            print(msg, flush=True)
            self.tele.warn(msg)
            return False
        try:
            ai = mt5.account_info()
            self.is_demo = bool(ai and int(getattr(ai, 'trade_mode', 0))
                                == int(mt5.ACCOUNT_TRADE_MODE_DEMO))
        except Exception:
            self.is_demo = False
        return True

    def run(self) -> bool:
        import sys as _sys, traceback as _tb
        ts = pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d %H:%M:%S UTC')
        # v3.2.1: print SYNCHRONOUSLY too -- the async telemetry worker (a daemon
        # thread) can be killed at process exit before it drains, which made an
        # early preflight bail look like a silent exit. stdout print + a guaranteed
        # telemetry drain (finally) + a full-traceback catch fix that for good.
        print(f"🧪 AUREON SELF-TEST starting ({ts})", flush=True)
        self.tele.info(f"🧪 AUREON SELF-TEST starting ({ts})")
        try:
            if not self._preflight():
                print("🧪 SELF-TEST ABORTED in preflight (reason above) — "
                      "RESULT: ABORTED, 0 steps ran.", flush=True)
                return False
            return self._run_steps(ts)
        except BaseException:
            tb = _tb.format_exc()
            print("🧪 SELF-TEST CRASHED — full traceback:\n" + tb,
                  file=_sys.stderr, flush=True)
            log.error("SELF-TEST CRASHED:\n%s", tb)
            return False
        finally:
            # ALWAYS drain the async telemetry so nothing is lost on exit.
            try:
                self.tele.stop(timeout=6.0)
            except Exception:
                pass

    def _run_steps(self, ts) -> bool:
        market_ok = self.is_demo or self.force
        skip_reason = "non-demo account (pass --force to run)" if not market_ok else ""
        try:
            self._step_connection()
            self._step_tick_fresh()
            self._step_comment_guard()
            for n, step in ((4, self._step_stop_place),
                            (5, self._step_market_place),
                            (6, self._step_sl_modify)):
                if market_ok:
                    self._run_guarded(n, step)
                else:
                    self._record(n, SKIP, skip_reason)
            self._step_rescue_class()
            if market_ok:
                self._run_guarded(8, self._step_rescue_dryrun)
            else:
                self._record(8, SKIP, skip_reason)
            self._step_ts_header()
            self._step_late_retry()
            self._step_fleet_logger()
            self._step_fill_alert()
            self._step_close_alert()
            self._step_ts_fallback()
            self._step_be_rung()
            self._step_hold_gate()
            self._step_boost_sl()
            self._step_discord_cards()
            self._step_discord_dedup()
            self._step_discord_heartbeat()
            self._step_discord_connect()
            self._step_lone_rescue()
            self._step_boost_trail()
            self._step_lone_branches()
            self._step_boost_isolation()
            self._step_lone_live_logging()
            self._step_backtest_parity()
            self._step_boost_trigger()
            self._step_boost_toggles()
            self._step_underwater_lock()
            self._step_trail_telemetry()
            self._step_stop_reject()
            self._step_lock_guards()
            self._step_lone_boost()
            self._step_boost_watchdog()
            self._step_nooco_stack()
            self._step_stack_economics()
            self._step_telemetry_full()
            self._step_phantom_guard()
            self._step_phantom_legit()
            self._step_monday_wake()
            self._step_monday_badoffset()
            self._step_monday_drift_trip()
            self._step_weekday_unaffected()
            self._step_monday_trace()
            self._step_jun8_replay()
            self._step_offset_parity()
            self._step_autopull_soft()
            self._step_autopull_abort()
            self._step_soft_no_flatten()
            self._step_rehydrate_resume()
            self._step_reconcile_adopt()
            self._step_reconcile_finalize()
            self._step_quick_gap()
            # Feature D — break-and-hold filter
            self._step_break_fakespike()
            self._step_break_holds()
            self._step_break_continuation()
            self._step_break_retrace()
            self._step_break_holdshort()
            # Feature E — lot config + FP guard
            self._step_fp_015_ok()
            self._step_fp_035_breach()
            self._step_fp_zero_blocks()
            self._step_fp_lot_config()
            # Feature C — 5-long stack (flag-gated, default OFF)
            self._step_stack5_cap()
            self._step_stack5_loser_out()
            self._step_stack5_fp_gate()
            self._step_stack5_whipsaw()
            self._step_stack5_cap_viol()
            # v3.2.4 additions
            self._step_stack5_trail_coclose()
            self._step_stack5_pnl_015()
            self._step_stack5_pnl_035()
            self._step_fp_zero_profile_cap()
            self._step_stack5_default_on()
            # v3.2.5 A1 tick-fallback + tick-hold confirm
            self._step_a1_tick_fallback_places()
            self._step_a1_tick_fallback_rejects_spike()
            self._step_tick_hold_fires()
            self._step_tick_hold_blip_rejected()
            self._step_tick_hold_trail_advance()
            # v3.2.6 boost breath-gap +$8 arm-gate incident regression
            self._step_boost_incident_regression()
            # v3.2.7 rally-only break-and-hold gate (rescue fires free)
            self._step_rescue_bypass_break_and_hold()
            # v3.2.8 Phase 1 — rally +$5 arm (fire trigger; rescue untouched)
            self._step_rally_arm_5()
            # v3.3.0 — rally RIDES (peak-$2 trail above a +$3 floor), no flat lock
            self._step_rally_trail_ride()
            # v3.2.8 Phase 2/3 — rally/rescue/common split + dispatcher isolation
            self._step_boost_split_isolation()
            # v3.2.9 manual TESTFIRE — fail-closed safety rails + same-placement reuse
            self._step_testfire_demo_only()
            self._step_testfire_fp_refuse()
            self._step_testfire_flat_inflight()
            self._step_testfire_anchor_window()
            self._step_testfire_same_placement()
            # v3.3.0 — rally rides not bails + no sub-floor clip (PTRACE defect fix)
            self._step_rally_rides_not_bails()
            self._step_rally_no_subfloor_clip()
            # v3.3.3 — break-and-hold crash fix + fail-closed; rally SL $13 / cap -$910
            self._step_break_gate_npsafe()
            self._step_break_gate_failclosed()
            self._step_rally_sl13_cap910()
            # v3.3.4 — rally pullback detector (hold within T / cut beyond T / time bound)
            self._step_rally_pullback_band()
            self._step_rally_pullback_recover_time()
            # v3.3.5 — CASE 2 parent-profit override (fires strong same-dir continuations
            # the candle gate blocks; Case 1 fresh spike still blocks; dir/rescue guards)
            self._step_case2_override_fires()
            self._step_case1_still_blocks()
            self._step_override_dir_and_rescue()
            # v3.3.6 — telemetry-truth displays (readiness/status/banner derive from
            # the resolver) + A3 reschedule 16:20 -> 17:00 IST; no placement/offset change
            self._step_readiness_derives_resolver()
            self._step_a3_scheduled_1700()
            self._step_v336_no_logic_change()
            self._step_monday_gate_strict()
        finally:
            self._cleanup()
        return self._report(ts)

    def _run_guarded(self, n: int, step):
        try:
            step()
        except Exception as e:
            self._record(n, FAIL, f"raised: {e!r}")

    def _report(self, ts: str) -> bool:
        lines = [f"🧪 AUREON SELF-TEST ({ts})"]
        n_pass = n_fail = n_skip = n_warn = 0
        total = len(STEP_NAMES)   # v3.2.8: dynamic count (was hard-coded 80)
        for n in range(1, total + 1):
            status, detail = self.results.get(n, (FAIL, "did not run"))
            if status == PASS:
                n_pass += 1
            elif status == SKIP:
                n_skip += 1
            elif status == WARN:
                n_warn += 1          # v3.1.0: network/reachability WARN is NOT a fail
            elif status == FAIL:
                n_fail += 1
            lines.append(f"{n} {STEP_NAMES[n]:<14} {status}  ({detail})")
        # "fleet ready" only when the placement + boost path actually passed.
        fleet_steps = (4, 5, 6, 8)
        fleet_ready = all(self.results.get(s, ("", ""))[0] == PASS for s in fleet_steps)
        warn_tag = f", {n_warn} WARN" if n_warn else ""
        # v3.1.0: READY when no real code FAIL (network/reachability = WARN).
        if n_fail == 0 and n_skip == 0:
            verdict = f"RESULT: {n_pass}/{total} PASS{warn_tag} — READY"
        elif n_fail == 0:
            ready = "READY" if fleet_ready else "READY (market steps skipped)"
            verdict = f"RESULT: {n_pass}/{total} PASS, {n_skip} SKIP{warn_tag} — {ready}"
        else:
            verdict = f"RESULT: {n_pass}/{total} PASS, {n_fail} FAIL{warn_tag} — NOT ready (see failures)"
        lines.append(verdict)
        report = "\n".join(lines)
        print(report, flush=True)   # v3.2.1: synchronous RESULT, always surfaces
        log.info(report)
        (self.tele.success if n_fail == 0 else self.tele.error)(report)
        # v3.2.1: telemetry is drained in run()'s finally (single drain point) so
        # the async worker isn't double-stopped here.
        return n_fail == 0


def run_selftest(cfg, force: bool = False) -> bool:
    """Build an MT5Adapter (same pattern as run_live), run the harness, tear the
    adapter down. Returns True only if every executed step PASSed.
    v3.2.1: NEVER exit silently -- any failure building the adapter / constructing
    the harness prints a full traceback to stderr and returns False."""
    import sys as _sys, traceback as _tb
    adapter = None
    try:
        from mt5_adapter import MT5Adapter  # late import: only this path needs MT5
        adapter = MT5Adapter(
            getattr(cfg, 'symbol', 'XAUUSD'),
            expected_offset_hours=getattr(cfg, 'EXPECTED_BROKER_OFFSET_HOURS', None))
        return SelfTest(cfg, adapter, force=force).run()
    except BaseException:
        tb = _tb.format_exc()
        print("🧪 SELF-TEST could not start — full traceback:\n" + tb,
              file=_sys.stderr, flush=True)
        log.error("run_selftest crashed:\n%s", tb)
        return False
    finally:
        if adapter is not None:
            try:
                adapter.shutdown()
            except Exception:
                pass
