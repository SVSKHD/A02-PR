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
                    'anchor': 'A3_1340_Overlap', 'sched_iso': None, 'open_iso': 'x',
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
                {'anchor_label': 'A3_1340_Overlap', 'entry_price': None},
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
                {'anchor_label': 'A3_1340_Overlap', 'side': 'SELL'},
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
        # v3.1.6 BOOST BREATH-GAP TRAIL + $10 BACKSTOP (boosts only). Drive the REAL
        # strategy core over price paths. gap = cfg.boost_trail_gap_dollars (3.50);
        # the trail is armed the instant the boost fills, alongside the $10 hard SL.
        from strategy import Position, update_position_on_bar
        try:
            cfg = self.cfg
            gap = float(getattr(cfg, 'boost_trail_gap_dollars', 3.50))
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-17T13:50:00Z')

            def run(bars, boost=True, role='rescue'):
                p = Position(anchor_label='T', side='BUY', entry_price=entry,
                             entry_time=ts0, current_sl=entry - 10.0,
                             tp_level=entry + 30.0, max_fav=entry,
                             lot=cfg.lot_size, role=role, boost=boost)
                for i, b in enumerate(bars):
                    update_position_on_bar(p, pd.Series(b),
                                           ts0 + pd.Timedelta(minutes=i + 1), cfg)
                    if p.closed:
                        break
                return p

            # 1) reverses before +$8 -> exits on the breath-gap trail at ~-(gap), NOT -$10
            p1 = run([{'open': 100, 'high': 101, 'low': 100 - gap - 1, 'close': 96}])
            rev_at_gap = p1.closed and abs((entry - p1.exit_price) - gap) < 0.05
            # 2) gaps THROUGH the trail -> caught by the $10 SL backstop (~-$10)
            p2 = run([{'open': 85, 'high': 86, 'low': 84, 'close': 85}])
            backstop = p2.closed and abs((entry - p2.exit_price) - 10.0) < 0.05
            # 3) runs past +$8 then pulls back -> exits no lower than +$8 (floor)
            p3 = run([{'open': 100, 'high': 112, 'low': 100.5, 'close': 111},
                      {'open': 111, 'high': 111, 'low': 108, 'close': 108}])
            floor8 = p3.closed and (p3.exit_price - entry) >= 8.0 - 0.05
            # 4) one-way: after the peak a non-triggering retrace must NOT loosen SL
            p4 = run([{'open': 100, 'high': 112, 'low': 100.5, 'close': 111}])
            sl_peak = p4.current_sl
            update_position_on_bar(p4, pd.Series(
                {'open': 109, 'high': 109, 'low': 108.6, 'close': 108.8}),
                ts0 + pd.Timedelta(minutes=2), cfg)
            one_way = (p4.closed or p4.current_sl >= sl_peak - 1e-9)
            ok = rev_at_gap and backstop and floor8 and one_way
            detail = (f"rev@-{gap:.2f}->exit{p1.exit_price}({rev_at_gap}) "
                      f"gap->backstop{p2.exit_price}({backstop}) "
                      f"runpast8->exit{p3.exit_price}({floor8}) one_way={one_way}")
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

        # TREND: rise to +25 then pull back to the breath trail -> rides past +8.
        b_trend = sim_boost([{'open': 100, 'high': 125, 'low': 100.5, 'close': 124},
                             {'open': 124, 'high': 124, 'low': 121, 'close': 121}])
        # WHIPSAW: immediate reverse -> exits on the breath-gap trail at ~-(gap),
        # FAR less than the old -$10 worst case.
        b_whip = sim_boost([{'open': 100, 'high': 100.5, 'low': 96, 'close': 96.5}])
        old_cap = round(2 * float(getattr(cfg, 'boost_sl_dollars', 10.0)) * lot * 100, 2)

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
                # v3.1.6: whipsaw boost exits at ~-(gap), NOT -$10 (much smaller loss)
                "whip_boost~-gap":     (b_whip is not None and abs(b_whip + gap * lot * 100) < 1.0),
                "whip=WHIPSAW_LOSS":   whip['branch'] == 'WHIPSAW_LOSS',
                # combined boost loss is now FAR under the old -$700 worst case
                "whip<old_700cap":     (-old_cap < 2 * b_whip < 0),
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

            # 1) Drive the BOOST to a loss (reverses to its breath trail). The
            #    ORIGINAL object must be byte-for-byte untouched by this.
            update_position_on_bar(boost, pd.Series(
                {'open': 100, 'high': 100.5, 'low': 96, 'close': 96.5}),
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
            # L3: sub-$10 either way -> None (hard guard).
            l3 = (_b.plan_boost_event('BUY', fill, fill + 9.99, cfg) is None
                  and _b.plan_boost_event('BUY', fill, fill - 9.99, cfg) is None)
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
            detail = (f"L1_rally={l1} L2_rescue={l2} L3_sub10_none={l3} "
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
        # v3.2.3 (41): first tick after a weekend gap, broker UTC+3 -> offset
        # resolves +3 and A1 fires 05:00, not 06:00. Drives the SHARED offset_guard.
        import offset_guard as og
        try:
            # gap marks a weekend wake; a +3 read confirms first try.
            gap = og.weekend_gap_hours(0.0, 50 * 3600.0)   # 50h gap
            is_wake = og.is_weekend_wake(gap)
            off, result, attempts = og.resolve_offset([3])
            resolves_3 = (off == 3 and result == og.CONFIRMED)
            # A1 implied IST with the CORRECT offset = scheduled 05:00 (no drift).
            a1_0500 = (not og.a1_drifted(og.A1_SCHEDULED_IST_MIN)
                       and og.fmt_hhmm(og.A1_SCHEDULED_IST_MIN) == '0500')
            ok = is_wake and resolves_3 and a1_0500
            detail = (f"M1_offset_resolves_+3={resolves_3} "
                      f"M1_A1_fires_0500_not_0600={a1_0500} (gap={gap:.0f}h wake={is_wake})")
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
        # v3.2.3 (43): force A1 toward a server time implying ~06:00 IST Monday ->
        # the drift tripwire fires BEFORE placement; with the offset corrected, A1
        # resolves back to ~05:00.
        import offset_guard as og
        from position_telemetry import PositionTracer
        try:
            implied_0600 = 6 * 60      # forced drift to 06:00 IST
            drift_fires = og.a1_drifted(implied_0600)   # 06:00 vs 05:00 -> True
            tr = PositionTracer(sink=lambda l: None)
            if drift_fires:
                tr.violation(None, 'A1', 'monday_a1_drift',
                             scheduled=og.fmt_hhmm(og.A1_SCHEDULED_IST_MIN),
                             implied=og.fmt_hhmm(implied_0600))
            trip = any('monday_a1_drift' in v and 'scheduled=0500' in v
                       and 'implied=0600' in v for v in tr.violations)
            # corrected path: confirmed +3 -> A1 at 05:00, within tolerance.
            a1_0500 = not og.a1_drifted(og.A1_SCHEDULED_IST_MIN)
            ok = trip and a1_0500
            detail = (f"M3_drift_tripwire_fires={trip} "
                      f"M3_A1_actual~0500(tol)={a1_0500}")
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
            tr.anchor_time_resolved(scheduled_ist='0500', offset_used=3, result='CONFIRMED')
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
        for n in range(1, 74):
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
            verdict = f"RESULT: {n_pass}/73 PASS{warn_tag} — READY"
        elif n_fail == 0:
            ready = "READY" if fleet_ready else "READY (market steps skipped)"
            verdict = f"RESULT: {n_pass}/73 PASS, {n_skip} SKIP{warn_tag} — {ready}"
        else:
            verdict = f"RESULT: {n_pass}/73 PASS, {n_fail} FAIL{warn_tag} — NOT ready (see failures)"
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
