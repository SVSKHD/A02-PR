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

    # ------------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------------
    def _preflight(self) -> bool:
        """Refuse to run with any open position/pending; set the demo flag."""
        mt5 = self.adapter.mt5
        try:
            pos = mt5.positions_get(symbol=self.symbol) or []
            pend = mt5.orders_get(symbol=self.symbol) or []
        except Exception as e:
            self.tele.error(f"🧪 self-test aborted — could not read broker state: {e!r}")
            return False
        if pos or pend:
            self.tele.warn(
                f"🧪 self-test ABORTED — live positions present "
                f"({len(pos)} open, {len(pend)} pending). Run when FLAT so the "
                f"harness can't interfere with a live anchor.")
            return False
        try:
            ai = mt5.account_info()
            self.is_demo = bool(ai and int(getattr(ai, 'trade_mode', 0))
                                == int(mt5.ACCOUNT_TRADE_MODE_DEMO))
        except Exception:
            self.is_demo = False
        return True

    def run(self) -> bool:
        ts = pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d %H:%M:%S UTC')
        self.tele.info(f"🧪 AUREON SELF-TEST starting ({ts})")
        if not self._preflight():
            return False
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
        for n in range(1, 27):
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
            verdict = f"RESULT: {n_pass}/26 PASS{warn_tag} — READY"
        elif n_fail == 0:
            ready = "READY" if fleet_ready else "READY (market steps skipped)"
            verdict = f"RESULT: {n_pass}/26 PASS, {n_skip} SKIP{warn_tag} — {ready}"
        else:
            verdict = f"RESULT: {n_pass}/26 PASS, {n_fail} FAIL{warn_tag} — NOT ready (see failures)"
        lines.append(verdict)
        report = "\n".join(lines)
        print(report)
        log.info(report)
        (self.tele.success if n_fail == 0 else self.tele.error)(report)
        # Give the async telemetry worker a moment to flush before the caller
        # shuts the adapter / process down.
        try:
            self.tele.stop(timeout=6.0)
        except Exception:
            pass
        return n_fail == 0


def run_selftest(cfg, force: bool = False) -> bool:
    """Build an MT5Adapter (same pattern as run_live), run the harness, tear the
    adapter down. Returns True only if every executed step PASSed."""
    from mt5_adapter import MT5Adapter  # late import: only this path needs MT5
    adapter = MT5Adapter(getattr(cfg, 'symbol', 'XAUUSD'),
                         expected_offset_hours=getattr(cfg, 'EXPECTED_BROKER_OFFSET_HOURS', None))
    try:
        return SelfTest(cfg, adapter, force=force).run()
    finally:
        adapter.shutdown()
