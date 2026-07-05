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
    # v3.3.6 telemetry-truth display fixes; 100 repurposed 2026-07-02 for the A3 cut
    99: "readiness resolver",
    100: "a3 cut from list",
    101: "v336 no logic chg",
    102: "monday gate strict",
    # anchor-list validation (dynamic since the 2026-07-02 A3 cut; was hard-5 A1-A5)
    103: "anchor list valid",
    104: "anchor no collide",
    105: "a5 identical + fp5",
    # v3.4.0 RALLY override pullback-entry (flag-gated, DEFAULT OFF)
    106: "ovr freeze guard",
    107: "ovr arm no-fire",
    108: "retired(P4)",
    109: "retired(P4)",
    110: "retired(P4)",
    111: "ovr rescue unaff",
    112: "ovr $5arm unaff",
    113: "ovr no pb-collide",
    # v3.5.0 adaptive pullback entry (RALLY + RESCUE; flag-gated, DEFAULT OFF)
    114: "v35 rally freeze",
    115: "v35 rescue freeze",
    116: "v35 rally pull",
    117: "v35 rally smooth",
    118: "v35 rally timeout",
    119: "v35 rescue pull",
    120: "v35 rescue smooth",
    121: "v35 rescue timeout",
    122: "v35 dynamic sl",
    123: "v35 separation",
    124: "v35 $5arm unaff",
    125: "v35 cap unchanged",
    126: "R1 spike-collapse",
    127: "R2 pull-continue",
    128: "R3 rescue bounce",
    129: "R4 pump-fade",
    130: "R5 smooth runner",
    131: "R6 chop skip",
    # ROGUE — self-anchoring monster-rider (flag-gated; demo default ON, funded OFF)
    132: "rogue boot gate",       # v3.6.0: boot default ON; explicit-off kills; funded forced off
    133: "rogue detect monster",
    134: "rogue weak no-slot",
    135: "rogue cap blocks @10",
    136: "rogue early entry",
    137: "rogue adaptive trail",
    138: "rogue loss-stop",
    139: "rogue 3-fail pause",
    140: "rogue closure isol",
    141: "rogue rescue capped",
    142: "rogue rally reuse",
    143: "rogue demo/funded",
    144: "rogue tagging",
    145: "rogue ride-unlimited",
    # Watchdog boot validator (Task 1)
    146: "watchdog safe-start",
    147: "watchdog do-not-start",
    # v3.5.0 all-16 (renumbered 148-161 to avoid the 132-145 rogue collision)
    148: "f8 pullback log",
    149: "f9 boost ledger",
    150: "f10 daily report",
    151: "f11 preflight",
    152: "util no-order-fx",
    153: "f12 confirm cand",
    154: "f13 atr depth",
    155: "f14 rescue sl wide",
    156: "f15 boost telem",
    157: "f16 offset no-0h",
    158: "strat full freeze",
    159: "per-flag indep",
    160: "rescue gate ON arm",
    161: "flag table check",
    # Watchdog rogue promotion-rule line (post-trial)
    162: "watchdog rogue rule",
    # run_live() guaranteed rogue promotion on every live boot
    163: "rogue promote boot",
    # Rogue ML pipeline: pattern logger + model gate (pass-through default) + archive
    164: "rogue ml gate",
    # Rogue ML: EOD champion/challenger autotrain + exit-feature capture
    165: "rogue ml train",
    # E-12 feed-death watchdog (re-subscribe + throttled FEED DOWN alert)
    166: "feed resub @N",
    167: "feed resub reset",
    168: "feed alert+ladder",
    169: "feed warn throttle",
    170: "feed wd disabled",
    # E-2/E-3/E-4 Rogue brakes (NOTE: 171-176 collide with fix2's T-B 171-176 --
    # renumber to 177-182 on whichever of fix2/fix3 merges second; mechanical rebase)
    171: "rogue rec_close",
    172: "rogue reentry",
    173: "rogue obs close",
    174: "rogue loss-stop",
    175: "rogue eod flag",
    176: "rogue isolation",
    # E-6 boost rides with parent (RALLY-only, flag-gated). Renumbered 177-182:
    # 171-176 are fix3's Rogue brakes (merged to master first); this block followed.
    177: "boost ride parent",
    178: "boost parent gone",
    179: "boost ride OFF",
    180: "boost isolation",
    181: "boost rescue unaff",
    182: "boost A1 replay",
    # E-5: Rogue daily loss stop -150 -> -525
    183: "rogue stop -525",
    # F-B: trapped-leg capped late-rescue (No-OCO whipsaw), DEFAULT OFF
    184: "fb late rescue",
    # Fix 4: Rogue A1-anchored redesign (NEW ENGINE, DEFAULT OFF)
    185: "fix4 rogue a1",
    # selftest auto-summary reporter (report-only)
    186: "selftest summary",
    # Rogue manual current-tick seed command
    187: "rogue manual seed",
    # rogueseed command consumed by the live loop (idempotent)
    188: "rogueseed consume",
    # E-3 CHAIN: ANY Rogue close re-anchors the A1 redesign at the exit (no dormancy)
    189: "rogue e3 chain",
    # --- P1 "never blind, never brick" (E-13..E-16 + E-12 ladder) --------------
    190: "fix1 rc-retry+brick",   # E-13 shared place_with_retry + Rogue brick fix
    191: "fix2 pnl-unresolved",   # E-14 retry history, book $0 without a fail
    192: "fix3 rogue gated",      # E-15 rogue under kill/EOD gates + kill flatten
    193: "fix4 feed reinit/L3",   # E-12 L2 MT5 reinit + L3 self-restart guard
    194: "fix5 restart-recovery", # E-16 persist + same-day boot recovery
    195: "wd exit42 only",        # watchdog relaunches ONLY on exit 42; else stop
    # --- P3 (E-17): Rogue monster-catcher discipline -- chop/chase gates -------
    196: "rogue chase cap",       # Gate 1: reject beyond $20, re-allow on pullback, no slot
    197: "rogue chain cooldown",  # Gate 2a: chained entry blocked in cooldown, allowed after
    198: "rogue chain displace",  # Gate 2b: needs fresh $ off the re-anchor (since planting)
    199: "rogue reversal exempt", # recovery leg: no cooldown, but chase-capped
    200: "rogue seeds exempt",    # A1 morning seed + manual rogueseed are NOT chained
    201: "rogue gates off",       # all three knobs 0 -> old unbounded behavior (freeze)
    # Hotfix 2026-07-02: PTRACE BREAK_FAILED spam throttle (logging only)
    202: "ptrace break spam",     # 1 line per episode + suppressed count; gate unchanged
    # P4 2026-07-03: W-7 (D-4), E-18, F-B (D-5) live verification
    203: "d4 override re-eval",   # a FAILED verdict does not latch; re-crossing $12 fires
    204: "e18 no-lock no-adv",    # a losing leg w/ no armed lock computes NO stop advance
    205: "fb bypasses gate",      # F-B fires through break-and-hold entirely (never reached)
    206: "fb default on",         # D-5: trapped_late_rescue_enabled defaults True
    # P4 2026-07-04: daily P&L report (pnl_report.py) -- fixture-based, no MT5 needed
    207: "pnl classify",          # comment/magic -> engine/anchor/side/leg_class
    208: "pnl boost join",        # BOOST_UNCLASSIFIED -> RALLY/RESCUE/F-B via rescue_events.csv
    209: "pnl whipsaw",           # both-legs-open overlap detection
    210: "pnl pf math",           # PF/win% computed from raw sums, never averaged
    211: "pnl month rollup",      # multi-day roll-up sums raw fields before one PF/win% calc
    212: "pnl empty day",         # zero deals -> empty, well-formed result (never raises)
    213: "pnl render+ledger",     # markdown + CSV ledger rows are well-formed and stable-schema
    214: "pnl ledger idempotent", # re-running a day's report never duplicates ledger rows
    # Weekend branch 2026-07-04: E-19 boot-survives-closed-market + Friday policy
    215: "e19 boot survives",     # unconfirmed offset -> sleep-probe, never the clock-drift abort
    216: "friday flatten gate",   # Friday cutoff flattens anchor+boost, Rogue untouched, blocks entries
    217: "friday a4 a5 skip",     # a5_skip_friday/a4_skip_friday skip placement outright on Friday

    218: "r1 date correctness",   # explicit date_str (no now()-default) + IST midnight bucketing
    219: "fb silent-fire logging",  # F-B log line + ledger kind=FB + swallowed-write now alerts

    220: "d6 a4 default true",     # a4_skip_friday now defaults True (was False pre-D-6)
    221: "d6 poll until flat",     # poll loop retries a failed pass; latches only on verified flat
    222: "d6 entries blocked",     # anchor + Rogue entries both gated on the shared per-engine seams
    # v3.6.0 ENGINE SWITCHES + ROGUE SEED INDEPENDENCE
    223: "engine defaults wired",  # non_oco/rogue boot defaults ON + seed knob validator-wired + seed_source schema
    224: "anchors off manage-only",# no straddle at anchor time; boost family blocked on restored leg; trail/SL-close continue
    225: "rogue off manage-only",  # drive() takes no entry; restored open Rogue leg still trails/books its close
    226: "engine persist+override",# toggle survives a simulated restart; restored!=default fires ENGINE STATE OVERRIDE
    227: "seed fallback modes",    # anchors off -> Rogue seeds via A1-time snapshot (default) / market_open; chain runs
    228: "seed a1 regression",     # both engines ON -> seeds via the REAL A1 anchor read, byte-identical to master
    229: "no mid-day re-seed",     # toggling anchors mid-day never re-seeds/orphans an existing seed or chain
    230: "switch+friday compose",  # per-engine seam blocks on EITHER (engine off OR friday window)
    231: "scoped flatten confirm", # /anchors|/rogue flatten touch only their magic and require confirm
}
# Steps that place REAL (throwaway) orders -> gated by the demo guard.
MARKET_STEPS = {4, 5, 6, 8}

# --- selftest auto-summary reporter v2 (report-only; NO trading/test behavior change) ---
# End-of-run summary block + a SINGLE results table sorted FAILED-FIRST (then passed, each
# in step order) so the owner sees problems immediately without scrolling. The .md file is
# ASCII (PASS/FAIL, never ✅/❌) and written utf-8 -- fixes the Windows UnicodeEncodeError.


def build_selftest_summary(results, step_names):
    """PURE: reduce the harness result dict {step:(status,detail)} to a summary. The FAILED
    count reflects REAL failures ONLY -- a test is failed IFF the harness marked its status
    FAIL (the SAME signal that populates failed_tests=[]). Negative tests that intentionally
    log ERROR/violation lines but are RECORDED as PASS are counted as PASS (we never grep for
    the word ERROR). Returns counts + `rows` = every test as (step, name, status, detail)
    SORTED failed-first then step-ascending, and `failed_list` = (step,name,detail). PURE."""
    total = len(step_names)
    passed = failed = skipped = warned = 0
    rows = []
    for n in range(1, total + 1):
        status, detail = results.get(n, (FAIL, "did not run"))
        name = step_names.get(n, f"step {n}")
        if status == PASS:
            passed += 1
        elif status == FAIL:
            failed += 1
        elif status == SKIP:
            skipped += 1
        elif status == WARN:
            warned += 1
        else:
            status, detail = FAIL, f"status={status}"   # unknown = real failure
            failed += 1
        rows.append((n, name, status, detail))
    # sort key: primary FAIL-before-everything-else, secondary step-number ascending.
    rows.sort(key=lambda r: (0 if r[2] == FAIL else 1, r[0]))
    failed_list = [(s, nm, d) for (s, nm, st, d) in rows if st == FAIL]
    return {'total': total, 'passed': passed, 'failed': failed, 'skipped': skipped,
            'warned': warned, 'rows': rows, 'failed_list': failed_list}


def _summary_header(summary, meta):
    """PURE: the 6-line summary header block (identical in console + file). Report-only."""
    m = meta or {}
    result = "PASS" if summary['failed'] == 0 else "FAIL"
    return [
        "=" * 20 + " AUREON SELFTEST SUMMARY " + "=" * 20,
        f"Build: {m.get('build', '?')} | {m.get('ts', '?')} | "
        f"Account: {m.get('account', '?')} {m.get('server', '')}".rstrip(),
        f"Result: {result}",
        f"Total: {summary['total']}   Passed: {summary['passed']}   Failed: {summary['failed']}",
        f"Watchdog: {m.get('watchdog', '?')}   Rogue: {m.get('rogue', '?')}",
        "=" * 64,
    ]


def _result_cell(status, emoji):
    """The Result-column text. emoji=True (console) may prefix a glyph; emoji=False (the .md
    FILE) is pure ASCII PASS/FAIL/SKIP/WARN -- the Windows-safe form."""
    if not emoji:
        return status                                   # ASCII: PASS / FAIL / SKIP / WARN
    return {PASS: "✅ PASS", FAIL: "❌ FAIL", SKIP: "⏭ SKIP",
            WARN: "⚠ WARN"}.get(status, status)


def render_summary(summary, meta, *, emoji):
    """PURE: header block + the SINGLE results table (FAILED rows first, then the rest in
    step order). emoji=False -> ASCII PASS/FAIL (the .md file); emoji=True -> console glyphs.
    When 0 failed, a 'No failures' line precedes the table. Never raises on missing meta."""
    L = list(_summary_header(summary, meta))
    L.append("")
    if summary['failed'] == 0:
        L.append(f"No failures -- all {summary['total']} passed.")
    L.append("| # | Step | Name | Result | Detail |")
    L.append("|---|------|------|--------|--------|")
    for i, (step, name, status, detail) in enumerate(summary['rows'], 1):
        d = str(detail).replace("|", "\\|")
        L.append(f"| {i} | {step} | {name} | {_result_cell(status, emoji)} | {d} |")
    return "\n".join(L)


def write_selftest_report(text, path):
    """Write the report to `path` as UTF-8 (atomic temp+replace). FULLY GUARDED: returns
    True on success, False on ANY error (never raises) -- a report-write failure must NEVER
    fail the suite. JOB 1: utf-8 encoding + ASCII body together fix the Windows crash."""
    try:
        import os as _os
        d = _os.path.dirname(path)
        if d:
            _os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        _os.replace(tmp, path)
        return True
    except Exception as e:
        log.warning(f"selftest report write non-fatal: {e!r}")
        return False


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

    # MARKET_CLOSED retcode: on a weekend the broker rejects every live order
    # with rc=10018. That is an ENVIRONMENTAL condition (the market is shut),
    # not a logic failure -- the live-order steps (4/5/6/8) SKIP, never FAIL, so
    # a clean weekend run still verdicts READY.
    MARKET_CLOSED_RC = 10018

    @classmethod
    def _market_closed(cls, *rcs):
        """True if ANY of the supplied retcodes is MARKET_CLOSED (10018)."""
        return any(rc == cls.MARKET_CLOSED_RC for rc in rcs)

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
        if not ok and self._market_closed(b_rc, s_rc):
            self._record(4, SKIP, f"MARKET_CLOSED — {detail}")
            return
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
        if not ok and self._market_closed(rc):
            self._record(5, SKIP, f"MARKET_CLOSED — {detail}")
            return
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
            status = SKIP if self._market_closed(rc) else FAIL
            prefix = "MARKET_CLOSED — " if status == SKIP else ""
            self._record(6, status,
                         f"{prefix}setup position failed rc={rc} ({self._rcname(rc)})")
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
        if not ok and self._market_closed(m_rc):
            self._record(6, SKIP,
                         f"MARKET_CLOSED — {m_rc} ({self._rcname(m_rc)}), SL->${new_sl}")
            return
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
        rcs = []
        all_ok = True
        for i in range(n):
            cmt = f"AUR_{label[:2]}_{side[0]}_B{i+1}"
            res = self.adapter.place_market_order(
                self.symbol, side, lot, sl=b_sl, tp=b_tp, comment=cmt)
            rc = self._rc(res)
            rcs.append(rc)
            tk = self._ticket(res)
            if rc == 10009 and tk:
                self._open_positions.add(tk)
            else:
                all_ok = False
            outcomes.append(f"boost{i+1} {rc} '{mt5_comment(cmt)}'={len(mt5_comment(cmt))}")
            if tk:
                self._close(tk)
        detail = ", ".join(outcomes) + ", closed"
        if not all_ok and self._market_closed(*rcs):
            self._record(8, SKIP, f"MARKET_CLOSED — {detail}")
            return
        self._record(8, PASS if all_ok else FAIL, detail)

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
            s._anchor_skipped_today_friday = types.MethodType(
                _a._anchor_skipped_today_friday, s)

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
                         parent_side=None, parent_max_fav=0.0, cfg=None, last_mid=None):
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
        import dataclasses as _dc
        # v3.5.0 unified: silence the util-8/9 file writers during gate tests (now that
        # rally/rescue carry the boost_metrics hooks). The utilities are validated
        # separately via their pure cores (tests 148-152). Defaults are ON in production.
        _cfg = _dc.replace(cfg or self.cfg, util_pullback_log=False, util_boost_ledger=False)
        tr = types.SimpleNamespace(cfg=_cfg, adapter=adapter, tele=tele, ptrace=_PT())
        if last_mid is not None:        # v3.4.0: current tick price for the pullback gate
            tr._last_boost_mid = float(last_mid)
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

    def _step_ptrace_break_spam(self):
        # 202 (hotfix 2026-07-02): the live spam scenario. break_and_hold evaluated a
        # persistent FAILED break once per second (A4 SELL @edge 4131.02) and the
        # PTRACE emitter wrote 60+ identical BREAK_FAILED lines (17:10:29->17:11:32+);
        # E-11 had already throttled the human alert to one. Drive the REAL gate with
        # the REAL PositionTracer through 60 ticks of the same failed break. Asserts:
        # (a) exactly ONE PTRACE BREAK_FAILED line (repeats suppressed); (b) the gate
        # DECISION is unchanged -- blocked (False) on EVERY tick; (c) the single E-11
        # telemetry alert behavior is unchanged; (d) a NEW break level starts a new
        # episode (re-emits, carrying suppressed_repeats=59 for the closed one); (e) a
        # CONFIRMED gate reset ends the episode (carries the count) and a following
        # FAILED re-emits; (f) no telemetry violations.
        import numpy as np
        import rally as _rally
        from position_telemetry import PositionTracer
        try:
            dt = [('high', 'f8'), ('low', 'f8'), ('close', 'f8')]
            # SELL break of edge 100: cleared by $5 (low 95) but candle 2's high
            # popped back above the edge -> FAILED 'reversed' on every re-evaluation.
            failed_bars = np.array([(99.0, 95.0, 96.0),
                                    (101.0, 96.0, 100.5)], dtype=dt)
            lines = []
            tr, sh, pl = self._break_gate_stub(lambda s, n: failed_bars, side='SELL')
            tr.ptrace = PositionTracer(sink=lines.append)
            blocked = all(_rally.break_and_hold_ok(tr, sh, pl) is False
                          for _ in range(60))
            n_failed = sum('PTRACE BREAK_FAILED' in l for l in lines)
            one_line = (n_failed == 1)
            one_alert = (sum('no fire' in str(m) for m in self._gate_tele_infos) == 1)
            # (d) NEW BREAK LEVEL = new episode: same bars vs edge 101 classify
            # FAILED 'retrace' -> still blocked, ONE new line, count of the 59
            # suppressed repeats stamped on the line that ends the old episode.
            sh['leg_fill_price'] = sh['entry_price'] = 101.0
            blocked_new_edge = (_rally.break_and_hold_ok(tr, sh, pl) is False)
            new_ep_lines = [l for l in lines if 'PTRACE BREAK_FAILED' in l
                            and 'suppressed_repeats=59' in l]
            new_ep_emitted = (sum('PTRACE BREAK_FAILED' in l for l in lines) == 2
                              and len(new_ep_lines) == 1)
            # repeats of the NEW episode suppress again...
            for _ in range(5):
                _rally.break_and_hold_ok(tr, sh, pl)
            still_two = (sum('PTRACE BREAK_FAILED' in l for l in lines) == 2)
            # (e) ...until a CONFIRMED gate reset ends it (carrying suppressed=5);
            # the next FAILED for the SAME key then re-emits (fresh episode).
            tr.ptrace.break_confirmed('A2', side='SELL', break_level=101.0)
            confirm_carries = any('PTRACE BREAK_CONFIRMED' in l
                                  and 'suppressed_repeats=5' in l for l in lines)
            blocked_after_reset = (_rally.break_and_hold_ok(tr, sh, pl) is False)
            reemits_after_reset = (sum('PTRACE BREAK_FAILED' in l for l in lines) == 3)
            no_violation = (len(tr.ptrace.violations) == 0)
            ok = (blocked and one_line and one_alert and blocked_new_edge
                  and new_ep_emitted and still_two and confirm_carries
                  and blocked_after_reset and reemits_after_reset and no_violation)
            detail = (f"60_ticks_blocked={blocked} one_ptrace_line={one_line} "
                      f"one_e11_alert={one_alert} new_edge_reemits+59={new_ep_emitted} "
                      f"repeats_suppressed={still_two} confirm_carries+5={confirm_carries} "
                      f"reset_reemits={reemits_after_reset} "
                      f"decision_unchanged={blocked_new_edge and blocked_after_reset} "
                      f"no_violation={no_violation}")
        except Exception as e:
            self._record(202, FAIL, f"raised: {e!r}"); return
        self._record(202, PASS if ok else FAIL, detail)

    # --- P4 2026-07-03: W-7 (D-4), E-18, F-B (D-5) -----------------------------
    def _step_d4_override_reevaluates(self):
        # 203 D-4: a FAILED break-and-hold verdict must NOT permanently kill the
        # episode -- the parent-profit override re-evaluates EVERY tick from the
        # parent's live max_fav, so a parent that proves the move AFTER an earlier
        # FAILED classification still fires. Live gap: 2026-07-03 A1 BUY BREAK
        # FAILED(retrace) @4134.57, parent crossed +$20 later and (per the old $20
        # threshold) it sat too close to the wire; no boost ever fired, ~$2,000+
        # forfeited on a +$56 move. Source read (rally.py break_and_hold_ok /
        # _override_grade): the gate stores NO per-episode "FAILED" state anywhere
        # -- classify() and the parent-profit check both recompute fresh from the
        # current M5 bars / shadow['max_fav'] on every call. This proves that
        # property with the REAL gate: (a) parent at +$8 (< the new $12 threshold,
        # D-4) on a FAILED shape -> blocked, no override logged; (b) the SAME
        # shadow object, same FAILED bar shape, parent later marked +$15 (>= $12)
        # -> fires on the very next call -- no latch survives the earlier FAILED.
        import rally as _rally
        try:
            bars = self._case2_bars()
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                               parent_side='SELL', parent_max_fav=8.0)
            blocked_first = (_rally.break_and_hold_ok(tr, sh, pl) is False)
            no_override_yet = not any(e[0] == 'break_override_parent_established'
                                      for e in self._gate_ptrace)
            # the parent proves the move: max_fav advances past the $12 threshold
            # (exactly what trails.py does on a genuine bar update) -- NO other
            # state on `sh` or `tr` is touched, simulating pure time passing.
            sh['max_fav'] = sh['entry_price'] - 15.0   # SELL: favorable = price DOWN
            fires_now = (_rally.break_and_hold_ok(tr, sh, pl) is True)
            override_logged = any(e[0] == 'break_override_parent_established'
                                  for e in self._gate_ptrace)
            ok = blocked_first and no_override_yet and fires_now and override_logged
            detail = (f"blocked_at_$8={blocked_first} no_override_yet={no_override_yet} "
                      f"fires_at_$15_same_episode={fires_now} "
                      f"override_logged={override_logged}")
        except Exception as e:
            self._record(203, FAIL, f"raised: {e!r}"); return
        self._record(203, PASS if ok else FAIL, detail)

    def _e18_trapped_trader(self, entry=4123.10, bid=4137.0, ask=4137.2):
        # Minimal LiveTrader-shaped stub driving the REAL trails._manage_trails_on_
        # bar_close (not a stripped copy): a No-OCO SELL leg parked at EXACT
        # breakeven (current_sl == entry -> lock_level_for == 0, "no armed lock")
        # now $13.90 ADVERSE (bid above entry) -- the 2026-07-03 05:55-06:21 A1
        # SELL live shape (computed stop 4123.10 vs bid ~4137).
        import types
        import strategy as _S
        m1_bars = [{'open': bid, 'high': bid, 'low': bid, 'close': bid, 'time': 1000},
                   {'open': bid, 'high': bid, 'low': bid, 'close': bid, 'time': 1060}]
        mt5 = types.SimpleNamespace(
            symbol_info_tick=lambda s: types.SimpleNamespace(bid=bid, ask=ask),
            symbol_info=lambda s: types.SimpleNamespace(trade_stops_level=0, point=0.01),
            positions_get=lambda ticket=None: [types.SimpleNamespace(sl=entry)])
        modify_calls = []
        adapter = types.SimpleNamespace(
            mt5=mt5, symbol=self.cfg.symbol,
            get_latest_m1=lambda s, n: m1_bars,
            modify_position_sl=lambda tk, px: (modify_calls.append((tk, px)) or None))
        shadow = {'anchor_label': 'A1', 'side': 'SELL', 'entry_price': entry,
                  'current_sl': entry, 'tp_level': entry - 30.0, 'max_fav': entry,
                  'role': 'normal'}
        warns = []
        ptrace_calls = []
        class _PT:
            def __getattr__(self, name):
                def _rec(*a, **kw):
                    if name == 'stop_through_rearm':
                        ptrace_calls.append(kw)
                    return None
                return _rec
        tr = types.SimpleNamespace(
            cfg=self.cfg, adapter=adapter, paper=False,
            shadow_positions={12345: shadow}, ptrace=_PT(),
            tele=types.SimpleNamespace(warn=lambda m, *a, **k: warns.append(m),
                                       info=lambda *a, **k: None),
            _rl_ok=lambda *a, **k: True, _save_state=lambda: None,
            _Position=_S.Position)
        return tr, shadow, modify_calls, warns, ptrace_calls

    def _step_e18_no_lock_no_advance(self):
        # 204 E-18 (FIXED 2026-07-03): a losing leg with NO genuinely armed lock
        # (current_sl at/behind its resting SL, lock_level_for == 0) must compute
        # NO stop advance at all -- the pre-fix code recomputed an "advancing"
        # STOP-THROUGH correction every bar it stayed adverse (27x/26min live,
        # 05:55-06:21). Drives the REAL trails._manage_trails_on_bar_close across
        # 3 consecutive bar-closes. Asserts: (a) current_sl is UNCHANGED after
        # every bar (no advance, no chase); (b) modify_position_sl is NEVER called
        # for this ticket (nothing sent to the broker); (c) exactly ONE
        # STOP-THROUGH warning + ONE PTRACE line fire across all 3 bars (episode
        # throttle), not one per bar.
        from trails import _manage_trails_on_bar_close as _mtc
        try:
            tr, shadow, modify_calls, warns, ptrace_calls = self._e18_trapped_trader()
            sl_before = shadow['current_sl']
            for _ in range(3):
                _mtc(tr)
            sl_unchanged = (shadow['current_sl'] == sl_before == 4123.10)
            never_modified = (len(modify_calls) == 0)
            one_warn = (len(warns) == 1 and 'no armed lock' in warns[0].lower())
            one_ptrace = (len(ptrace_calls) == 1
                          and ptrace_calls[0].get('reason') == 'stop_through_no_armed_lock_no_advance')
            ok = sl_unchanged and never_modified and one_warn and one_ptrace
            detail = (f"sl_unchanged@{sl_before}={sl_unchanged} "
                      f"never_modified={never_modified} one_warn_x3bars={one_warn} "
                      f"one_ptrace_x3bars={one_ptrace}")
        except Exception as e:
            self._record(204, FAIL, f"raised: {e!r}"); return
        self._record(204, PASS if ok else FAIL, detail)

    def _step_fb_bypasses_gate(self):
        # 205 F-B (D-5): a trapped No-OCO leg's late-rescue hedge fires THROUGH/PAST
        # the break-and-hold gate -- it is never even offered to the gate. Source:
        # fills.py _check_boost_triggers calls boosts.plan_trapped_late_rescue and,
        # on a plan, fires + `continue`s (fills.py ~604-620) BEFORE the tick-hold
        # streak and the `_break_and_hold_ok` call (~651-695) are ever reached. This
        # asserts that end-to-end shape directly against the live method: with the
        # break-and-hold gate stubbed to ALWAYS refuse (it must never even be asked),
        # a trapped leg $14 adverse still fires the hedge.
        import types, dataclasses
        try:
            cfg = dataclasses.replace(self.cfg, trapped_late_rescue_enabled=True,
                                      trapped_rescue_arm_dollars=10.0,
                                      rally_boosts_enabled=False, rescue_boosts_enabled=False)
            fired = []
            gate_asked = {'n': 0}
            logged = []
            tk = types.SimpleNamespace(bid=4055.00, ask=4055.20)  # mid 4055.10: -$10.54 adverse
            mt5 = types.SimpleNamespace(symbol_info_tick=lambda s: tk)
            adapter = types.SimpleNamespace(mt5=mt5)
            shadow = {'side': 'BUY', 'leg_fill_price': 4065.64, 'entry_price': 4065.64,
                      'anchor_label': 'A1', 'boost': False, 'boost_fired': False,
                      'boost_eligible': True, 'boost_rally_only': True}
            tr = types.SimpleNamespace(
                cfg=cfg, adapter=adapter, paper=False, symbol=cfg.symbol,
                shadow_positions={777: shadow},
                _last_boost_mid=None,
                tele=types.SimpleNamespace(info=lambda msg, **k: logged.append(msg)),
                _break_and_hold_ok=lambda sh, pl: (gate_asked.__setitem__(
                    'n', gate_asked['n'] + 1), False)[1],   # would ALWAYS refuse
                _rescue_entry_ok=lambda sh, pl: False,
                _fp_guard_ok=lambda sh, n: True,
                _fire_boost_event=lambda tk_, sh, pl: fired.append(pl),
                _enforce_boost_cap=lambda mid: None)
            import fills as _fills
            _fills._check_boost_triggers(tr)
            hedge_fired = (len(fired) == 1 and fired[0].event_type == 'TRAPPED_LATE_RESCUE'
                          and fired[0].boost_side == 'SELL')
            gate_never_asked = (gate_asked['n'] == 0)
            # Branch 2 (2a): a distinct log line now fires BEFORE the hedge (no
            # ptrace attr on this stub -> tr.break_eval is skipped, tele.info isn't).
            logged_ok = (len(logged) == 1 and 'F-B TRAPPED RESCUE FIRED' in logged[0]
                        and 'parent 777' in logged[0] and 'SELL' in logged[0])
            ok = hedge_fired and gate_never_asked and logged_ok
            detail = (f"hedge_fired={hedge_fired} break_and_hold_never_asked={gate_never_asked} "
                      f"(gate_would_have_refused=True) fb_log_line_emitted={logged_ok}")
        except Exception as e:
            self._record(205, FAIL, f"raised: {e!r}"); return
        self._record(205, PASS if ok else FAIL, detail)

    def _step_fb_default_on(self):
        # 206 D-5: trapped_late_rescue_enabled now DEFAULTS True (was False) -- flip
        # rationale: 3 trapped-leg events in 2 days, all unhedged naked (2026-07-02
        # A4 $27 collapse, 2026-07-02 A5 overnight, 2026-07-03 A1 $70 rally).
        try:
            default_on = (self.cfg.trapped_late_rescue_enabled is True)
            still_toggleable = True
            try:
                import dataclasses
                dataclasses.replace(self.cfg, trapped_late_rescue_enabled=False)
            except Exception:
                still_toggleable = False
            ok = default_on and still_toggleable
            detail = f"default_on={default_on} still_toggleable_off={still_toggleable}"
        except Exception as e:
            self._record(206, FAIL, f"raised: {e!r}"); return
        self._record(206, PASS if ok else FAIL, detail)

    # --- P4 2026-07-04: daily P&L report (pnl_report.py) -----------------------
    def _step_pnl_classify(self):
        # 207 classification (PURE, no MT5): comment+magic -> engine/anchor/side/
        # leg_class for every family the report needs to tell apart -- anchor
        # original (BUY/SELL, incl. _G/_RCV/_CFM/_R{n} suffixes), an anchor boost
        # fleet member (comment alone can't say RALLY vs RESCUE vs F-B -- see 208),
        # rogue (by magic AND by comment), and an unrecognized comment (UNKNOWN,
        # never silently dropped).
        import pnl_report as _pr
        try:
            orig_buy = _pr.classify_comment('AUR_A1_BUY', 20260522)
            orig_sell_gap = _pr.classify_comment('AUR_A2_SELL_G', 20260522)
            orig_retry = _pr.classify_comment('AUR_A4_BUY_R1', 20260522)
            orig_rcv = _pr.classify_comment('AUR_A5_SELL_RCV', 20260522)
            boost = _pr.classify_comment('AUR_A1_B_B1', 20260522)
            rogue_by_magic = _pr.classify_comment('anything', 20260626)
            rogue_by_comment = _pr.classify_comment('AUR_ROGUE_S', None)
            garbage = _pr.classify_comment('not_a_real_comment', 12345)
            none_comment = _pr.classify_comment(None, 20260522)

            orig_ok = (orig_buy == {'engine': 'ANCHOR', 'anchor2': 'A1', 'side': 'BUY',
                                    'leg_class': _pr.ORIGINAL, 'boost_seq': None}
                      and orig_sell_gap['anchor2'] == 'A2' and orig_sell_gap['side'] == 'SELL'
                      and orig_sell_gap['leg_class'] == _pr.ORIGINAL
                      and orig_retry['anchor2'] == 'A4' and orig_retry['leg_class'] == _pr.ORIGINAL
                      and orig_rcv['anchor2'] == 'A5' and orig_rcv['side'] == 'SELL'
                      and orig_rcv['leg_class'] == _pr.ORIGINAL)
            boost_ok = (boost['engine'] == 'ANCHOR' and boost['anchor2'] == 'A1'
                       and boost['side'] == 'BUY' and boost['boost_seq'] == 1
                       and boost['leg_class'] == _pr.BOOST_UNCLASSIFIED)
            rogue_ok = (rogue_by_magic['engine'] == 'ROGUE'
                       and rogue_by_magic['leg_class'] == _pr.ROGUE_LEG
                       and rogue_by_comment['engine'] == 'ROGUE'
                       and rogue_by_comment['side'] == 'SELL')
            unknown_ok = (garbage['leg_class'] == _pr.UNKNOWN and garbage['engine'] is None
                         and none_comment['leg_class'] == _pr.UNKNOWN)
            ok = orig_ok and boost_ok and rogue_ok and unknown_ok
            detail = (f"originals(BUY/SELL_G/retry/RCV)={orig_ok} "
                      f"boost_unclassified_by_default={boost_ok} "
                      f"rogue(magic+comment)={rogue_ok} unknown_never_dropped={unknown_ok}")
        except Exception as e:
            self._record(207, FAIL, f"raised: {e!r}"); return
        self._record(207, PASS if ok else FAIL, detail)

    def _step_pnl_boost_join(self):
        # 208 the ONE flagged ambiguity: AUR_{A}_{side}_B{n} is IDENTICAL for a
        # RALLY pyramid, a RESCUE hedge, and the new F-B TRAPPED_LATE_RESCUE hedge
        # -- boosts.BoostPlan.kind/event_type is never written to the broker
        # comment. Verifies the join against rescue_events.csv's event_type (by
        # ticket) correctly splits all three, and that an UNMATCHED boost ticket
        # (event not finalized / file doesn't reach back) stays
        # BOOST_UNCLASSIFIED -- never guessed as RALLY or RESCUE.
        import pnl_report as _pr
        try:
            idx = {101: 'RALLY_BOOST', 102: 'RESCUE_BOOST', 103: 'TRAPPED_LATE_RESCUE'}
            rally = _pr.resolve_boost_leg_class(101, idx)
            rescue = _pr.resolve_boost_leg_class(102, idx)
            fb = _pr.resolve_boost_leg_class(103, idx)
            unmatched = _pr.resolve_boost_leg_class(999, idx)
            deals = [
                {'position_id': 101, 'entry': 0, 'comment': 'AUR_A1_B_B1', 'magic': 20260522, 'time': 1, 'price': 4000.0},
                {'position_id': 101, 'entry': 1, 'comment': 'AUR_A1_B_B1', 'magic': 20260522, 'time': 2, 'price': 4010.0, 'profit': 100.0, 'swap': 0, 'commission': 0},
                {'position_id': 999, 'entry': 0, 'comment': 'AUR_A1_S_B2', 'magic': 20260522, 'time': 1, 'price': 4000.0},
                {'position_id': 999, 'entry': 1, 'comment': 'AUR_A1_S_B2', 'magic': 20260522, 'time': 2, 'price': 3995.0, 'profit': -25.0, 'swap': 0, 'commission': 0},
            ]
            trades = _pr.build_trades(deals, idx)
            by_tk = {t['ticket']: t for t in trades}
            end_to_end_ok = (by_tk[101]['leg_class'] == _pr.RALLY_BOOST
                             and by_tk[999]['leg_class'] == _pr.BOOST_UNCLASSIFIED)
            ok = (rally == _pr.RALLY_BOOST and rescue == _pr.RESCUE_BOOST
                 and fb == _pr.TRAPPED_LATE_RESCUE
                 and unmatched == _pr.BOOST_UNCLASSIFIED and end_to_end_ok)
            detail = (f"rally={rally} rescue={rescue} fb={fb} "
                      f"unmatched_stays_unclassified={unmatched} "
                      f"end_to_end_via_build_trades={end_to_end_ok}")
        except Exception as e:
            self._record(208, FAIL, f"raised: {e!r}"); return
        self._record(208, PASS if ok else FAIL, detail)

    def _step_pnl_whipsaw(self):
        # 209 whipsaw = an opposite-side pair of ORIGINAL anchor trades whose
        # [open,close] windows overlap (both legs genuinely live at the broker at
        # once -- the same structural signature fills.py's `_twin_open` checks
        # live). Two SEPARATE, non-overlapping same-anchor opposite-side trades
        # (e.g. a morning SELL and an evening BUY) must NOT count as a whipsaw.
        import pnl_report as _pr
        try:
            overlapping = [
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'side': 'BUY', 'leg_class': _pr.ORIGINAL,
                 'open_time': 100, 'close_time': 300},
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'side': 'SELL', 'leg_class': _pr.ORIGINAL,
                 'open_time': 200, 'close_time': 400},
            ]
            separate = [
                {'engine': 'ANCHOR', 'anchor2': 'A2', 'side': 'SELL', 'leg_class': _pr.ORIGINAL,
                 'open_time': 100, 'close_time': 200},
                {'engine': 'ANCHOR', 'anchor2': 'A2', 'side': 'BUY', 'leg_class': _pr.ORIGINAL,
                 'open_time': 500, 'close_time': 600},
            ]
            boost_ignored = [
                {'engine': 'ANCHOR', 'anchor2': 'A4', 'side': 'BUY', 'leg_class': _pr.RALLY_BOOST,
                 'open_time': 100, 'close_time': 300},
                {'engine': 'ANCHOR', 'anchor2': 'A4', 'side': 'SELL', 'leg_class': _pr.ORIGINAL,
                 'open_time': 100, 'close_time': 300},
            ]
            wc = _pr.detect_whipsaws(overlapping + separate + boost_ignored)
            ok = (wc.get('A1') == 1 and wc.get('A2', 0) == 0 and wc.get('A4', 0) == 0)
            detail = f"overlap_counts_1={wc.get('A1')} separate_counts_0={wc.get('A2', 0)} boost_never_pairs={wc.get('A4', 0)}"
        except Exception as e:
            self._record(209, FAIL, f"raised: {e!r}"); return
        self._record(209, PASS if ok else FAIL, detail)

    def _step_pnl_pf_math(self):
        # 210 PF = gross_win / gross_loss, win% = wins / (wins+losses) -- computed
        # from RAW dollar sums, never as an average of per-trade ratios. All-
        # winner (zero losers) -> PF renders as "inf", never a ZeroDivisionError
        # or a silently wrong number.
        import pnl_report as _pr
        try:
            trades = [
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'leg_class': _pr.ORIGINAL, 'pnl': 300.0},
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'leg_class': _pr.ORIGINAL, 'pnl': -100.0},
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'leg_class': _pr.RALLY_BOOST, 'pnl': 100.0},
                {'engine': 'ANCHOR', 'anchor2': 'A2', 'leg_class': _pr.ORIGINAL, 'pnl': 50.0},
                {'engine': 'ANCHOR', 'anchor2': 'A2', 'leg_class': _pr.ORIGINAL, 'pnl': 25.0},
            ]
            pa = _pr.per_anchor_stats(trades)
            a1, a2 = pa['A1'], pa['A2']
            # A1: gross_win 300+100=400, gross_loss 100 -> PF 4.0; wins=2 (the +300
            # original AND the +100 boost each count as a trade), losses=1 -> 66.7%.
            a1_ok = (abs(a1['pf'] - 4.0) < 1e-9 and abs(a1['win_pct'] - 66.7) < 0.05
                    and abs(a1['net'] - 300.0) < 1e-9 and abs(a1['orig_pnl'] - 200.0) < 1e-9
                    and abs(a1['rally_pnl'] - 100.0) < 1e-9)
            a2_ok = (a2['pf'] == float('inf') and abs(a2['win_pct'] - 100.0) < 1e-9
                    and _pr._fmt_pf(a2['pf']) == 'inf')
            ok = a1_ok and a2_ok
            detail = f"A1(pf={a1['pf']},win%={a1['win_pct']})={a1_ok} A2_all_win_inf={a2_ok}"
        except Exception as e:
            self._record(210, FAIL, f"raised: {e!r}"); return
        self._record(210, PASS if ok else FAIL, detail)

    def _step_pnl_month_rollup(self):
        # 211 month roll-up sums RAW fields (gross_win/gross_loss/wins/losses)
        # across days and recomputes PF/win% ONCE at the end -- never averages
        # already-computed per-day ratios (the classic PF-of-PFs bug). A 2-day
        # fixture: day 1 PF=3.0 (300/100), day 2 PF=0.5 (50/100) -- naively
        # averaging those PFs gives 1.75; the correct combined PF from raw sums
        # (350/200) is 1.75 too by coincidence here, so the fixture also checks
        # win% and net, which WOULD differ under a naive per-day average.
        import pnl_report as _pr
        try:
            day1 = _pr.per_anchor_stats([
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'leg_class': _pr.ORIGINAL, 'pnl': 300.0},
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'leg_class': _pr.ORIGINAL, 'pnl': -100.0},
            ])
            day2 = _pr.per_anchor_stats([
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'leg_class': _pr.ORIGINAL, 'pnl': 50.0},
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'leg_class': _pr.ORIGINAL, 'pnl': -100.0},
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'leg_class': _pr.ORIGINAL, 'pnl': -20.0},
            ])
            month = _pr.rollup_period([day1, day2])
            a1 = month['A1']
            trades_ok = (a1['trades'] == 5 and a1['wins'] == 2 and a1['losses'] == 3)
            net_ok = abs(a1['net'] - 130.0) < 1e-9
            pf_ok = abs(a1['pf'] - round(350.0 / 220.0, 2)) < 1e-9
            win_pct_ok = abs(a1['win_pct'] - 40.0) < 1e-9
            ok = trades_ok and net_ok and pf_ok and win_pct_ok
            detail = (f"trades=5/wins=2/losses=3={trades_ok} net=$130={net_ok} "
                      f"pf_from_raw_sums={pf_ok} win%=40={win_pct_ok}")
        except Exception as e:
            self._record(211, FAIL, f"raised: {e!r}"); return
        self._record(211, PASS if ok else FAIL, detail)

    def _step_pnl_empty_day(self):
        # 212 a day with zero deals must produce a well-formed EMPTY result
        # (never raise, never a KeyError on render) -- the CLI/EOD hook runs this
        # unconditionally every broker day, including weekends/holidays.
        import pnl_report as _pr
        try:
            trades = _pr.build_trades([], {})
            pa = _pr.per_anchor_stats(trades)
            wc = _pr.detect_whipsaws(trades)
            rg = _pr.rogue_stats(trades, {})
            md = _pr.render_markdown('2026-01-04', pa, rg, whipsaw_counts=wc)
            rows = _pr.ledger_rows('2026-01-04', pa, rg)
            ok = (trades == [] and pa == {} and wc == {}
                 and rg['entries'] == 0 and rg['day_pnl'] == 0.0
                 and isinstance(md, str) and 'AUREON daily P&L report' in md
                 and len(rows) == 2  # TOTAL + ROGUE rows, no anchor rows
                 and rows[0]['scope'] == 'TOTAL' and rows[1]['scope'] == 'ROGUE')
            detail = f"trades=[]{trades == []} per_anchor={{}}={pa == {}} rogue_zeroed={rg['entries'] == 0} md_ok={'AUREON' in md} rows={len(rows)}"
        except Exception as e:
            self._record(212, FAIL, f"raised: {e!r}"); return
        self._record(212, PASS if ok else FAIL, detail)

    def _step_pnl_render_and_ledger(self):
        # 213 markdown renders every section without raising, and the CSV ledger
        # rows carry the STABLE PNL_LEDGER_COLUMNS schema (task requirement:
        # "stable schema for later analysis") regardless of which anchors traded
        # -- adding/cutting an anchor (e.g. the A3 cut) never changes the column
        # set, only which `scope` values appear.
        import pnl_report as _pr
        try:
            trades = [
                {'engine': 'ANCHOR', 'anchor2': 'A1', 'side': 'BUY', 'leg_class': _pr.ORIGINAL,
                 'pnl': 300.0, 'ticket': 1, 'open_time': 1, 'close_time': 2},
                {'engine': 'ROGUE', 'anchor2': None, 'side': 'BUY', 'leg_class': _pr.ROGUE_LEG,
                 'pnl': 175.0, 'ticket': 2, 'open_time': 1, 'close_time': 2},
            ]
            pa = _pr.per_anchor_stats(trades)
            rg = _pr.rogue_stats(trades, {'chain_reanchors': 2})
            md = _pr.render_markdown('2026-07-04', pa, rg,
                                     w2={'A1': {'n': 1, 'avg_actual_exit': 4010.0,
                                                'avg_nohold_exit': 4005.0, 'avg_delta': 5.0}},
                                     month_rollup_stats=pa, month_str='2026-07')
            sections_ok = all(s in md for s in ('## Per anchor', '## W-2 tracking',
                                                '## Rogue', '## Month-to-date'))
            rows = _pr.ledger_rows('2026-07-04', pa, rg)
            cols_ok = all(set(r.keys()) == set(_pr.PNL_LEDGER_COLUMNS) for r in rows)
            scopes = {r['scope'] for r in rows}
            scopes_ok = scopes == {'A1', 'TOTAL', 'ROGUE'}
            rogue_row = next(r for r in rows if r['scope'] == 'ROGUE')
            rogue_ok = (rogue_row['rogue_chain_reanchors'] == 2 and rogue_row['net'] == '')
            ok = sections_ok and cols_ok and scopes_ok and rogue_ok
            detail = (f"sections={sections_ok} stable_columns={cols_ok} "
                      f"scopes={sorted(scopes)}={scopes_ok} rogue_row_ok={rogue_ok}")
        except Exception as e:
            self._record(213, FAIL, f"raised: {e!r}"); return
        self._record(213, PASS if ok else FAIL, detail)

    def _step_pnl_ledger_idempotent(self):
        # 214 re-running a day's report (manual CLI re-run, or the EOD hook
        # somehow firing twice) must NEVER duplicate rows in pnl_ledger.csv --
        # upsert_ledger_rows replaces that date's rows in place, leaving every
        # OTHER date's rows untouched.
        import pnl_report as _pr, tempfile, os as _os, csv as _csv
        try:
            tmp = tempfile.mkdtemp(prefix='aureon_pnl_ledger_')
            csv_path = _os.path.join(tmp, 'pnl_ledger.csv')
            rows_d1 = [{**{c: '' for c in _pr.PNL_LEDGER_COLUMNS},
                       'date': '2026-07-01', 'scope': 'A1', 'net': 10.0}]
            rows_d2_v1 = [{**{c: '' for c in _pr.PNL_LEDGER_COLUMNS},
                          'date': '2026-07-02', 'scope': 'A1', 'net': 20.0}]
            rows_d2_v2 = [{**{c: '' for c in _pr.PNL_LEDGER_COLUMNS},
                          'date': '2026-07-02', 'scope': 'A1', 'net': 999.0}]
            _pr.upsert_ledger_rows(csv_path, '2026-07-01', rows_d1)
            _pr.upsert_ledger_rows(csv_path, '2026-07-02', rows_d2_v1)
            _pr.upsert_ledger_rows(csv_path, '2026-07-02', rows_d2_v1)  # re-run, same data
            _pr.upsert_ledger_rows(csv_path, '2026-07-02', rows_d2_v2)  # re-run, CHANGED data
            with open(csv_path, newline='') as f:
                final = list(_csv.DictReader(f))
            d1_rows = [r for r in final if r['date'] == '2026-07-01']
            d2_rows = [r for r in final if r['date'] == '2026-07-02']
            ok = (len(final) == 2 and len(d1_rows) == 1 and len(d2_rows) == 1
                 and d1_rows[0]['net'] == '10.0' and d2_rows[0]['net'] == '999.0')
            detail = (f"total_rows=2={len(final) == 2} d1_untouched={d1_rows[0]['net'] if d1_rows else None} "
                      f"d2_replaced_not_duplicated=({len(d2_rows)}row,net={d2_rows[0]['net'] if d2_rows else None})")
        except Exception as e:
            self._record(214, FAIL, f"raised: {e!r}"); return
        self._record(214, PASS if ok else FAIL, detail)

    def _step_e19_boot_survives(self):
        # 215 E-19: boot with a dead/quiet feed -> offset Tier 2 REJECT ->
        # tick_time_offset_hours stays None -> server_time_utc()'s age is computed
        # with the "or 0" fallback, which can land in the false 120-3600s "clock
        # drift ABORT" window purely by arithmetic accident (2026-07-03 23:02 live
        # incident: exit code 0, watchdog never relaunches since it only relaunches
        # on 42). wait_until_market_open() must route an UNCONFIRMED offset into
        # the SAME sleep-probe loop as a confirmed closed market -- never abort --
        # and only "wake" once a detection genuinely succeeds (Tier 1/2, which
        # require a live/consistent tick and so cannot fire early).
        import live_trader as _lt, types as _types
        try:
            class _Stub(_lt.LiveTrader):
                def __init__(self):
                    pass  # skip the real (MT5-needing) __init__

            real_now = pd.Timestamp.now(tz='UTC')
            # Mimics the live incident: real broker offset +3h undetected (decoded
            # as 0h) skews the computed age into the 120-3600s window (here
            # -3480s) instead of the >3600s "obviously closed" window a CONFIRMED
            # offset would show for the same true elapsed time.
            skewed_server_utc = real_now + pd.Timedelta(seconds=3480)

            class _StubAdapter:
                def __init__(self):
                    self.tick_time_offset_hours = None
                    self.ensure_calls = 0
                def server_time_utc(self):
                    return skewed_server_utc
                def ensure_time_offset(self, max_wait_s=90.0):
                    self.ensure_calls += 1
                    self.tick_time_offset_hours = 3.0   # detection just succeeded
                    return True

            class _StubTele:
                def __init__(self):
                    self.lines = []
                def info(self, m): self.lines.append(('info', m))
                def warn(self, m): self.lines.append(('warn', m))
                def success(self, m): self.lines.append(('success', m))
                def critical(self, m): self.lines.append(('critical', m))

            t = _Stub()
            t.adapter = _StubAdapter()
            t.tele = _StubTele()
            t.state = {}
            t.ptrace = _types.SimpleNamespace(weekend_wake=lambda **k: None)
            t._next_a1_display = lambda: "A1 03:30 broker"
            t._save_state = lambda: None
            t._touch_heartbeat = lambda: None
            t._write_status = lambda *a, **k: None
            t._expected_market_open_utc = lambda now: None
            t._validate_offset_on_wake = lambda reason='wake': True
            t._post_readiness = lambda reason='wake': None

            orig_sleep = _lt.time.sleep
            _lt.time.sleep = lambda s: None   # collapse the 30s heartbeat sleeps for the test
            try:
                result = t.wait_until_market_open(reason="startup")
            finally:
                _lt.time.sleep = orig_sleep

            never_aborted = not any(lvl == 'critical' and 'ABORTING' in m
                                    for lvl, m in t.tele.lines)
            entered_sleep = any(lvl == 'info' and 'Weekend' in m for lvl, m in t.tele.lines)
            woke_on_detect = (t.adapter.ensure_calls >= 1
                             and t.adapter.tick_time_offset_hours == 3.0)
            ok = (result is True) and never_aborted and entered_sleep and woke_on_detect
            detail = (f"result={result} never_aborted={never_aborted} "
                      f"entered_sleep_probe={entered_sleep} woke_on_offset_detect={woke_on_detect}")
        except Exception as e:
            self._record(215, FAIL, f"raised: {e!r}"); return
        self._record(215, PASS if ok else FAIL, detail)

    def _step_friday_flatten_gate(self):
        # 216 Friday weekend-hold ban: _friday_flatten_reached fires ONLY on
        # Friday once cfg.friday_flatten_broker_hour is reached (a decimal broker
        # hour split into (hour,minute) through the SAME anchor_datetime_utc
        # conversion _eod_reached uses -- not a new time idiom); _flatten_all
        # (reason="EOD") flattens the anchor+boost shadow stack but must NOT
        # force-close an open Rogue ticket (risk.py's `reason != "EOD"` gate) --
        # Rogue's own EOD-hour flatten owns that, ~30min later.
        import live_trader as _lt, risk as _risk, dataclasses, types
        from datetime import date as _date
        from utils import anchor_datetime_utc as _adu
        try:
            cfg = dataclasses.replace(self.cfg, friday_flatten_enabled=True,
                                      friday_flatten_broker_hour=22.5,
                                      broker_tz_offset_hours=3)

            class _Stub:
                pass
            t = _Stub()
            t.cfg = cfg
            t._anchor_datetime_utc = _adu

            friday = _date(2026, 7, 3)      # a real Friday
            saturday = _date(2026, 7, 4)
            before = pd.Timestamp('2026-07-03 18:59:00', tz='UTC')   # 21:59 broker
            after = pd.Timestamp('2026-07-03 19:31:00', tz='UTC')    # 22:31 broker
            not_yet = _lt.LiveTrader._friday_flatten_reached(t, friday, before) is False
            reached = _lt.LiveTrader._friday_flatten_reached(t, friday, after) is True
            not_on_saturday = _lt.LiveTrader._friday_flatten_reached(t, saturday, after) is False

            # _flatten_all(reason="EOD") flattens the anchor stack, never Rogue.
            rogue_force_close_calls = []
            import rogue as _rogue
            orig_fc = _rogue.force_close_open
            _rogue.force_close_open = lambda *a, **k: rogue_force_close_calls.append(1)
            try:
                closed = []
                ft = types.SimpleNamespace(
                    tele=types.SimpleNamespace(warn=lambda *a, **k: None,
                                               critical=lambda *a, **k: None),
                    shadow_positions={11: {}, 12: {}}, shadow_pendings={},
                    paper=True, _deferred_anchor=None,
                    adapter=types.SimpleNamespace(
                        close_position=lambda tk, dry_run=False: closed.append(tk),
                        cancel_order=lambda tk, dry_run=False: None,
                        mt5=types.SimpleNamespace(positions_get=lambda ticket=None: [],
                                                  orders_get=lambda ticket=None: [])))
                _risk._flatten_all(ft, reason="EOD")
                anchor_flattened = (sorted(closed) == [11, 12] and ft.shadow_positions == {})
                rogue_untouched = (len(rogue_force_close_calls) == 0)
            finally:
                _rogue.force_close_open = orig_fc

            ok = (not_yet and reached and not_on_saturday
                 and anchor_flattened and rogue_untouched)
            detail = (f"not_yet_before_cutoff={not_yet} reached_after_cutoff={reached} "
                      f"friday_only={not_on_saturday} anchor_flattened={anchor_flattened} "
                      f"rogue_untouched_on_EOD_reason={rogue_untouched}")
        except Exception as e:
            self._record(216, FAIL, f"raised: {e!r}"); return
        self._record(216, PASS if ok else FAIL, detail)

    def _step_friday_a4_a5_skip(self):
        # 217 a5_skip_friday (default True) / a4_skip_friday (D-6: now ALSO
        # defaults True, was False pre-D-6 -- see step 220) skip an anchor's
        # placement OUTRIGHT for the whole Friday -- distinct from
        # friday_flatten_broker_hour, which only closes whatever IS open later
        # in the day. Other anchors (A1/A2) are never Friday-skipped by this
        # check, on Friday or any other day. This step exercises the FLAG
        # MECHANICS with explicit cfg overrides (both True and False), not the
        # actual default -- step 220 covers the real default value.
        import anchors as _an, dataclasses
        from datetime import date as _date
        try:
            friday = _date(2026, 7, 3)
            saturday = _date(2026, 7, 4)

            class _Stub:
                pass
            t = _Stub()
            t.cfg = dataclasses.replace(self.cfg, a5_skip_friday=True, a4_skip_friday=False)

            a5_skipped_friday = _an._anchor_skipped_today_friday(
                t, 'A5_1930_LateUS', friday) is True
            a4_default_not_skipped = _an._anchor_skipped_today_friday(
                t, 'A4_1640_NYopen', friday) is False
            a1_never_skipped = _an._anchor_skipped_today_friday(
                t, 'A1_02h_Asia', friday) is False
            a5_not_skipped_saturday = _an._anchor_skipped_today_friday(
                t, 'A5_1930_LateUS', saturday) is False

            t.cfg = dataclasses.replace(self.cfg, a5_skip_friday=True, a4_skip_friday=True)
            a4_funded_skipped_friday = _an._anchor_skipped_today_friday(
                t, 'A4_1640_NYopen', friday) is True

            ok = (a5_skipped_friday and a4_default_not_skipped and a1_never_skipped
                 and a5_not_skipped_saturday and a4_funded_skipped_friday)
            detail = (f"a5_friday_skipped={a5_skipped_friday} "
                      f"a4_demo_default_not_skipped={a4_default_not_skipped} "
                      f"a1_never_skipped={a1_never_skipped} "
                      f"a5_weekday_unaffected={a5_not_skipped_saturday} "
                      f"a4_funded_flag_skips={a4_funded_skipped_friday}")
        except Exception as e:
            self._record(217, FAIL, f"raised: {e!r}"); return
        self._record(217, PASS if ok else FAIL, detail)

    def _step_r1_date_correctness(self):
        # 218 R-1: two date bugs, one acceptance test.
        # (a) run_daily_report(trader, date_str=...) must use the EXACT date_str
        #     passed (the live EOD hook now passes broker_date explicitly instead
        #     of letting it default to IST wall-clock now(), which used to leak
        #     whichever day now() landed on -- e.g. an early-morning-IST EOD firing
        #     for YESTERDAY's broker day would otherwise misfile as today).
        # (b) load_rogue_closes buckets a Rogue exit by its IST calendar day, not
        #     a raw UTC ts[:10] slice -- a close at 18:45 UTC (= 00:15 IST the
        #     NEXT day) must land in the next day's Rogue section, not today's.
        import boost_metrics as _bm, tempfile, os as _os, csv as _csv
        try:
            tmp = tempfile.mkdtemp(prefix='aureon_r1_date_')

            # (a) explicit date_str is honored even though "now" would differ.
            month_csv = _os.path.join(tmp, "trades_2026-07.csv")
            with open(month_csv, 'w', newline='') as f:
                w = _csv.writer(f)
                w.writerow(['date_ist', 'anchor', 'realized_pnl_usd'])
                w.writerow(['2026-07-02', 'A1', '100.00'])   # requested day
                w.writerow(['2026-07-03', 'A1', '999.00'])   # a DIFFERENT day -- must be excluded

            import types as _types
            trader = _types.SimpleNamespace(
                cfg=_types.SimpleNamespace(util_daily_report=True),
                _journal_dir=lambda: tmp, shadow_positions={})
            out_path = _bm.run_daily_report(trader, date_str='2026-07-02')
            with open(out_path) as f:
                md = f.read()
            explicit_date_honored = ('$+100.00' in md and '999.00' not in md
                                     and out_path.endswith('daily_report_2026-07-02.md'))

            # (b) IST midnight bucketing on load_rogue_closes.
            ledger_csv = _os.path.join(tmp, "boost_ledger.csv")
            with open(ledger_csv, 'w', newline='') as f:
                w = _csv.writer(f)
                w.writerow(_bm.LEDGER_COLUMNS)
                # 18:45 UTC on 07-03 = 00:15 IST on 07-04 -- belongs to the NEXT IST day.
                w.writerow(['2026-07-03T18:45:00+00:00', 'A4', 'ROGUE', 'exit',
                           '', '', '', '50.00'])
                # 10:00 UTC on 07-03 = 15:30 IST on 07-03 -- same-day control case.
                w.writerow(['2026-07-03T10:00:00+00:00', 'A4', 'ROGUE', 'exit',
                           '', '', '', '25.00'])

            day_03 = _bm.load_rogue_closes(tmp, '2026-07-03')
            day_04 = _bm.load_rogue_closes(tmp, '2026-07-04')
            midnight_boundary_ok = (len(day_03) == 1 and day_03[0]['pnl'] == 25.0
                                    and len(day_04) == 1 and day_04[0]['pnl'] == 50.0)

            ok = explicit_date_honored and midnight_boundary_ok
            detail = (f"explicit_date_str_honored={explicit_date_honored} "
                      f"ist_midnight_bucketing={midnight_boundary_ok} "
                      f"(07-03={[r['pnl'] for r in day_03]} 07-04={[r['pnl'] for r in day_04]})")
        except Exception as e:
            self._record(218, FAIL, f"raised: {e!r}"); return
        self._record(218, PASS if ok else FAIL, detail)

    def _mk_fb_trader(self, tmp, extra_shadow=None):
        """Shared stub for step 219: a trapped BUY leg $10.54 adverse, wired through
        the REAL fills._fire_boost_event -> boosts_dispatch -> rescue.fire ->
        boosts_common.place_fleet chain (only the break-and-hold/rescue-entry/FP-guard
        seams are stubbed -- the trapped path never reaches them) so the ledger write
        and the new log line are exercised end-to-end, not just the gate bypass."""
        import types, dataclasses
        import fills as _fills
        cfg = dataclasses.replace(self.cfg, trapped_late_rescue_enabled=True,
                                  trapped_rescue_arm_dollars=10.0,
                                  rescue_boost_count=1, lot_size=0.10,
                                  contract_size=100.0)
        tk = types.SimpleNamespace(bid=4055.00, ask=4055.20)  # mid 4055.10: -$10.54 adverse
        mt5 = types.SimpleNamespace(symbol_info_tick=lambda s: tk)
        adapter = types.SimpleNamespace(
            mt5=mt5, place_market_order=lambda sym, side, lot, sl=None, tp=None,
            magic=None, comment=None, dry_run=False: types.SimpleNamespace(
                retcode=10009, order=90001, deal=90001, price=4054.85))
        shadow = dict(extra_shadow or {
            'side': 'BUY', 'leg_fill_price': 4065.64, 'entry_price': 4065.64,
            'anchor_label': 'A1', 'boost': False, 'boost_fired': False,
            'boost_eligible': True, 'boost_rally_only': True})
        logged = []
        tr = types.SimpleNamespace(
            cfg=cfg, adapter=adapter, paper=False, symbol=cfg.symbol,
            shadow_positions={777: shadow}, _last_boost_mid=None,
            state={'last_broker_date': '2026-07-03'},
            tele=types.SimpleNamespace(
                info=lambda msg, **k: logged.append(('info', msg)),
                send=lambda msg, *a, **k: logged.append(('send', msg)),
                error=lambda msg, **k: logged.append(('error', msg))),
            _journal_dir=lambda: tmp,
            _break_and_hold_ok=lambda sh, pl: False,
            _rescue_entry_ok=lambda sh, pl: False,
            _fp_guard_ok=lambda sh, n: True,
            _enforce_boost_cap=lambda mid: None)
        tr._fire_boost_event = lambda tkt, sh, pl: _fills._fire_boost_event(tr, tkt, sh, pl)
        return tr, logged

    def _step_fb_silent_fire_logging(self):
        # 219 Branch 2 (2a-2d): the F-B trapped late-rescue path fires through the
        # SAME dispatch -> boosts_common.place_fleet as a normal RALLY/RESCUE boost.
        # Exercise the REAL chain end-to-end and assert: (a) the distinct F-B log
        # line fires before the hedge; (b) the ledger row lands kind=FB (not
        # RESCUE, which is all plan.kind ever says for F-B) with ts populated;
        # (c) a ledger-write failure is now logged + alerted, never silently
        # swallowed -- R-3(d)'s real fills went missing for weeks with zero trace
        # via exactly this kind of bare except:pass.
        import tempfile, os as _os, csv as _csv
        import fills as _fills
        try:
            tmp = tempfile.mkdtemp(prefix='aureon_fb_ledger_')
            tr, logged = self._mk_fb_trader(tmp)
            _fills._check_boost_triggers(tr)

            fb_log = any(k == 'info' and 'F-B TRAPPED RESCUE FIRED' in m
                        and 'parent 777' in m and 'SELL' in m for k, m in logged)
            ledger_path = _os.path.join(tmp, 'boost_ledger.csv')
            with open(ledger_path) as f:
                rows = list(_csv.DictReader(f))
            ledger_ok = (len(rows) == 1 and rows[0]['kind'] == 'FB'
                        and rows[0]['event'] == 'enter' and bool(rows[0]['ts']))

            # (2d) force the ledger write to fail and confirm it now surfaces loud
            # instead of vanishing into a bare except:pass.
            import boost_metrics as _bm
            orig_append = _bm.append_ledger

            def _boom(*a, **k):
                raise OSError('disk full (simulated)')
            _bm.append_ledger = _boom
            try:
                tmp2 = tempfile.mkdtemp(prefix='aureon_fb_ledger2_')
                tr2, logged2 = self._mk_fb_trader(tmp2, extra_shadow={
                    'side': 'BUY', 'leg_fill_price': 4065.64, 'entry_price': 4065.64,
                    'anchor_label': 'A1', 'boost': False, 'boost_fired': False,
                    'boost_eligible': True, 'boost_rally_only': True})
                _fills._check_boost_triggers(tr2)
                swallow_hardened = any(k == 'error' for k, m in logged2)
            finally:
                _bm.append_ledger = orig_append

            ok = fb_log and ledger_ok and swallow_hardened
            detail = (f"fb_log_line={fb_log} ledger_kind_fb={ledger_ok} "
                      f"(row={rows[0] if rows else None}) "
                      f"write_failure_now_alerted={swallow_hardened}")
        except Exception as e:
            self._record(219, FAIL, f"raised: {e!r}"); return
        self._record(219, PASS if ok else FAIL, detail)

    def _step_d6_a4_default_true(self):
        # 220 D-6 (3a): a4_skip_friday's ACTUAL default is now True (was False)
        # -- the weekend-hold ban makes any Friday anchor that might still be
        # open into the weekend too risky, even with the D-6 poll-flatten as a
        # backstop. Reads self.cfg directly (no override), unlike step 217
        # which only exercises the flag mechanics with explicit values.
        try:
            default_true = (self.cfg.a4_skip_friday is True)
            a5_still_true = (self.cfg.a5_skip_friday is True)
            import dataclasses
            still_toggleable = True
            try:
                dataclasses.replace(self.cfg, a4_skip_friday=False)
            except Exception:
                still_toggleable = False
            ok = default_true and a5_still_true and still_toggleable
            detail = (f"a4_skip_friday_default={self.cfg.a4_skip_friday} "
                      f"a5_skip_friday_default={self.cfg.a5_skip_friday} "
                      f"still_toggleable_off={still_toggleable}")
        except Exception as e:
            self._record(220, FAIL, f"raised: {e!r}"); return
        self._record(220, PASS if ok else FAIL, detail)

    def _step_d6_poll_until_flat(self):
        # 221 D-6 (3b/3c): the Friday flatten is a POLL LOOP, never single-shot.
        # (i) a pass that fails to close a stuck anchor leg (3x rc=-1, matching
        # _flatten_all's own bounded retry) is alerted and does NOT latch --
        # the NEXT poll (>= friday_flatten_poll_seconds later) retries it and
        # only latches once a broker query confirms flat; (iii) pendings AND
        # positions of BOTH magics (anchor 20260522, Rogue 20260626) are
        # cancelled/closed and broker-verified before the Discord confirm.
        # An immediate re-poll at the SAME instant is rate-limited (no new
        # attempt) -- proving this is wall-clock-paced, not per-tick, so a
        # fast tick loop (or a stalled feed -- see wait_until_market_open's
        # analogous wall-clock probe) can't hammer the broker or stall it.
        import types, dataclasses
        import live_trader as _lt
        import risk as _risk
        import rogue as _rogue
        from datetime import date as _date
        try:
            cfg = dataclasses.replace(self.cfg, friday_flatten_enabled=True,
                                      friday_flatten_broker_hour=22.5,
                                      broker_tz_offset_hours=3,
                                      friday_flatten_poll_seconds=30.0)
            friday = _date(2026, 7, 3)
            t0 = pd.Timestamp('2026-07-03 19:31:00', tz='UTC')  # 22:31 broker -- past cutoff

            # env: a STUCK anchor position (501, magic 20260522) that fails to
            # close for 3 attempts on poll 1 and succeeds on poll 2; one anchor
            # PENDING (502) that cancels cleanly first try; a Rogue position
            # (900, magic 20260626) that closes cleanly first try.
            env = {'positions': {501: {'magic': 20260522}, 900: {'magic': 20260626}},
                  'pendings': {502: {'magic': 20260522}},
                  'close_attempts': {}, 'logged': []}

            def positions_get(ticket=None, symbol=None):
                if ticket is not None:
                    return ([types.SimpleNamespace(ticket=ticket)]
                            if int(ticket) in env['positions'] else [])
                return [types.SimpleNamespace(ticket=tk, magic=v['magic'], type=0,
                                              price_open=4000.0, sl=3982.0, tp=4030.0)
                        for tk, v in env['positions'].items()]

            def orders_get(ticket=None, symbol=None):
                if ticket is not None:
                    return ([types.SimpleNamespace(ticket=ticket)]
                            if int(ticket) in env['pendings'] else [])
                return [types.SimpleNamespace(ticket=tk, magic=v['magic'], type=2,
                                              price_open=4010.0)
                        for tk, v in env['pendings'].items()]

            def close_position(ticket, dry_run=False):
                n = env['close_attempts'].get(ticket, 0)
                env['close_attempts'][ticket] = n + 1
                if ticket == 501 and n < 3:   # poll 1's 3 attempts all fail
                    return
                env['positions'].pop(ticket, None)   # poll 2 (or ticket 900): succeeds

            def cancel_order(ticket, dry_run=False):
                env['pendings'].pop(ticket, None)

            mt5 = types.SimpleNamespace(positions_get=positions_get, orders_get=orders_get)
            adapter = types.SimpleNamespace(mt5=mt5, close_position=close_position,
                                            cancel_order=cancel_order)
            tr = types.SimpleNamespace(
                cfg=cfg, adapter=adapter, paper=False,
                shadow_positions={501: {'anchor_label': 'A1', 'side': 'BUY'}},
                shadow_pendings={502: {'anchor_label': 'A1', 'side': 'BUY',
                                      'sibling_ticket': None}},
                _deferred_anchor=None, state={},
                _rogue={'open': {'ticket': 900}, 'gov': {}},
                tele=types.SimpleNamespace(
                    warn=lambda m, **k: env['logged'].append(('warn', m)),
                    error=lambda m, **k: env['logged'].append(('error', m)),
                    critical=lambda m, **k: env['logged'].append(('critical', m))),
                _save_state=lambda: None,
                _FRIDAY_ANCHOR_MAGIC=_lt.LiveTrader._FRIDAY_ANCHOR_MAGIC)
            tr._friday_query_flat = lambda: _lt.LiveTrader._friday_query_flat(tr)
            tr._friday_resync_shadow_from_broker = (
                lambda: _lt.LiveTrader._friday_resync_shadow_from_broker(tr))
            tr._flatten_all = lambda reason="Manual": _risk._flatten_all(tr, reason=reason)

            orig_record_close, orig_persist, orig_close_pnl = (
                _rogue.record_close, _rogue._persist_state, _rogue._rogue_close_pnl)
            _rogue.record_close = lambda *a, **k: None
            _rogue._persist_state = lambda *a, **k: None
            _rogue._rogue_close_pnl = lambda *a, **k: 0.0
            try:
                # --- poll 1: cutoff just reached -- anchor leg fails 3x; pending
                # cancels clean; Rogue closes clean. Overall: NOT flat (501 stuck). ---
                _lt.LiveTrader._friday_poll_flatten(tr, friday, t0)
                pass1_not_flat = (not tr.state.get('friday_flatten_done')
                                  and 501 in env['positions']
                                  and env['close_attempts'].get(501) == 3)
                pass1_alerted = any(k == 'error' for k, m in env['logged'])
                pending_cancelled_pass1 = (502 not in env['pendings'])
                rogue_closed_pass1 = (tr._rogue['open'] is None
                                      and 900 not in env['positions'])

                # --- immediate re-poll, SAME instant: rate-limited, no new attempt ---
                _lt.LiveTrader._friday_poll_flatten(tr, friday, t0)
                rate_limited = (env['close_attempts'].get(501) == 3)

                # --- poll 2: >= poll_seconds later -- this pass succeeds -> latch ---
                t1 = t0 + pd.Timedelta(seconds=31)
                _lt.LiveTrader._friday_poll_flatten(tr, friday, t1)
                pass2_flat_and_latched = (tr.state.get('friday_flatten_done') is True
                                          and 501 not in env['positions'])
                confirm_sent = any(k == 'warn' and 'CONFIRMED flat' in m
                                   for k, m in env['logged'])
            finally:
                _rogue.record_close, _rogue._persist_state, _rogue._rogue_close_pnl = (
                    orig_record_close, orig_persist, orig_close_pnl)

            ok = (pass1_not_flat and pass1_alerted and pending_cancelled_pass1
                 and rogue_closed_pass1 and rate_limited and pass2_flat_and_latched
                 and confirm_sent)
            detail = (f"pass1_not_flat_retries={pass1_not_flat} "
                      f"pass1_alerted={pass1_alerted} "
                      f"pending_cancelled_pass1={pending_cancelled_pass1} "
                      f"rogue_closed_pass1={rogue_closed_pass1} "
                      f"rate_limited={rate_limited} "
                      f"pass2_flat_and_latched={pass2_flat_and_latched} "
                      f"confirm_sent={confirm_sent}")
        except Exception as e:
            self._record(221, FAIL, f"raised: {e!r}"); return
        self._record(221, PASS if ok else FAIL, detail)

    def _step_d6_entries_blocked(self):
        # 222 D-6 (3b): new entries for BOTH engines (anchor + Rogue) are
        # blocked from the instant the Friday flatten window opens --
        # independent of the poll/verify progress in step 221. Verifies (a)
        # _friday_entries_blocked is a live wrapper around _friday_flatten_reached
        # (True post-cutoff Friday, False pre-cutoff Friday, False any other
        # day) and (b) BOTH _tick() call sites (Rogue drive(), anchor-due) are
        # wired through the shared per-engine seams (v3.6.0:
        # _rogue_entries_blocked / _anchor_entries_blocked, each = the SAME
        # _friday_entries_blocked OR its engine switch) by inspecting the real
        # methods' source -- so the call sites can never silently drift apart.
        import inspect
        import live_trader as _lt
        import dataclasses
        from datetime import date as _date
        try:
            cfg = dataclasses.replace(self.cfg, friday_flatten_enabled=True,
                                      friday_flatten_broker_hour=22.5,
                                      broker_tz_offset_hours=3)

            class _Stub:
                pass
            t = _Stub()
            t.cfg = cfg
            from utils import anchor_datetime_utc as _adu
            t._anchor_datetime_utc = _adu
            t._friday_flatten_reached = (
                lambda broker_date, utc_now: _lt.LiveTrader._friday_flatten_reached(
                    t, broker_date, utc_now))
            t._friday_entries_blocked = (
                lambda broker_date, utc_now: _lt.LiveTrader._friday_entries_blocked(
                    t, broker_date, utc_now))
            t._engine_enabled = (
                lambda engine: _lt.LiveTrader._engine_enabled(t, engine))
            t.engines = {'anchors': True, 'rogue': True}   # both switches ON here

            friday = _date(2026, 7, 3)
            saturday = _date(2026, 7, 4)
            before = pd.Timestamp('2026-07-03 18:59:00', tz='UTC')   # 21:59 broker
            after = pd.Timestamp('2026-07-03 19:31:00', tz='UTC')    # 22:31 broker

            blocked_after_cutoff = _lt.LiveTrader._friday_entries_blocked(t, friday, after) is True
            not_blocked_before_cutoff = _lt.LiveTrader._friday_entries_blocked(t, friday, before) is False
            not_blocked_other_day = _lt.LiveTrader._friday_entries_blocked(t, saturday, after) is False
            # the per-engine seams inherit the Friday window (switches ON here).
            seams_follow_friday = (
                _lt.LiveTrader._anchor_entries_blocked(t, friday, after) is True
                and _lt.LiveTrader._rogue_entries_blocked(t, friday, after) is True
                and _lt.LiveTrader._anchor_entries_blocked(t, friday, before) is False
                and _lt.LiveTrader._rogue_entries_blocked(t, friday, before) is False)

            src = inspect.getsource(_lt.LiveTrader._tick)
            rogue_gated = ('allow_new_entries=not self._rogue_entries_blocked(' in src)
            anchor_gated = ('if not self._anchor_entries_blocked(broker_date, utc_now):' in src)
            # both per-engine seams route through the ONE Friday wrapper.
            seam_src = (inspect.getsource(_lt.LiveTrader._anchor_entries_blocked)
                        + inspect.getsource(_lt.LiveTrader._rogue_entries_blocked))
            shared_wrapper = (seam_src.count('self._friday_entries_blocked(') == 2)

            ok = (blocked_after_cutoff and not_blocked_before_cutoff
                 and not_blocked_other_day and seams_follow_friday
                 and rogue_gated and anchor_gated and shared_wrapper)
            detail = (f"blocked_after_cutoff={blocked_after_cutoff} "
                      f"not_blocked_before_cutoff={not_blocked_before_cutoff} "
                      f"not_blocked_other_day={not_blocked_other_day} "
                      f"seams_follow_friday={seams_follow_friday} "
                      f"rogue_drive_gated={rogue_gated} anchor_due_gated={anchor_gated} "
                      f"shared_wrapper={shared_wrapper}")
        except Exception as e:
            self._record(222, FAIL, f"raised: {e!r}"); return
        self._record(222, PASS if ok else FAIL, detail)

    # ------------------------------------------------------------------------
    # v3.6.0 ENGINE SWITCHES + ROGUE SEED INDEPENDENCE (223-231)
    # ------------------------------------------------------------------------

    def _mk_rogue_a1_trader(self, *, anchors_on=True, rogue_on=True, price=4000.0,
                            a1_leg_px=None, run_dir=None, **cfg_over):
        """v3.6.0 shared fixture: a stub A1-mode Rogue trader with a controllable
        tick/book/close-deal env. Returns (tr, env, placed, modified, closed)."""
        import types, dataclasses
        import rogue as _r
        env = {'price': float(price), 'book': {}, 'deal': None, 'deal_px': None,
               'logs': []}
        placed, modified, closed = [], [], []
        mt5 = types.SimpleNamespace(
            ACCOUNT_TRADE_MODE_DEMO=0,
            account_info=lambda: types.SimpleNamespace(trade_mode=0),   # demo
            symbol_info_tick=lambda s=None: types.SimpleNamespace(
                bid=env['price'] - 0.1, ask=env['price'] + 0.1),
            positions_get=lambda ticket=None, symbol=None: (
                [types.SimpleNamespace(ticket=ticket)]
                if ticket is not None and env['book'].get(int(ticket)) else []),
            history_deals_get=lambda position=None: (
                [types.SimpleNamespace(entry=1, profit=env['deal'], swap=0.0,
                                       commission=0.0, price=env['deal_px'])]
                if env['deal'] is not None else []))

        def _place(symbol, side, lot, sl=None, tp=None, magic=None,
                   comment=None, dry_run=False):
            placed.append({'side': side, 'magic': magic, 'sl': sl})
            tk = 900 + len(placed)
            env['book'][tk] = True
            return types.SimpleNamespace(retcode=10009, order=tk, deal=tk)

        adapter = types.SimpleNamespace(
            mt5=mt5, place_market_order=_place,
            modify_position_sl=lambda tk, sl: modified.append((int(tk), sl)),
            close_position=lambda tk, dry_run=False: (
                closed.append(int(tk)), env['book'].pop(int(tk), None)))
        over = dict(rogue_enabled=True, rogue_a1_anchor_mode=True,
                    rogue_daywatch=True, rogue_chain_cooldown_sec=0.0,
                    rogue_chain_min_displacement=0.0, util_boost_ledger=False,
                    lot_size=0.01)
        over.update(cfg_over)
        cfg = dataclasses.replace(self.cfg, **over)
        shadows = {}
        if a1_leg_px is not None:
            shadows[55] = {'anchor_label': 'A1_02h_Asia', 'side': 'BUY',
                           'leg_fill_price': float(a1_leg_px),
                           'entry_price': float(a1_leg_px), 'magic': 20260522}
        tr = types.SimpleNamespace(
            cfg=cfg, adapter=adapter, paper=True, _last_boost_mid=env['price'],
            engines={'anchors': bool(anchors_on), 'rogue': bool(rogue_on)},
            state={'last_broker_date': '2026-07-06', 'missed_anchors_today': []},
            shadow_positions=shadows, shadow_pendings={}, _rogue=None,
            tele=types.SimpleNamespace(
                info=lambda m, **k: env['logs'].append(('info', str(m))),
                warn=lambda m, **k: env['logs'].append(('warn', str(m))),
                error=lambda m, **k: env['logs'].append(('error', str(m)))))
        if run_dir:
            tr.run_dir = run_dir
            tr._journal_dir = lambda: run_dir
        return tr, env, placed, modified, closed

    def _step_engine_defaults_wired(self):
        # 223 v3.6.0 boot defaults + validator wiring + seed_source schema:
        # (a) non_oco_enabled / rogue_enabled default True, rogue_seed_fallback
        # defaults 'a1_time_snapshot'; (b) the validator carries the new flags +
        # the seed-mode validity check + the three new LiveTrader seams, all
        # passing on the real cfg; (c) a typo'd seed mode is a WIRING FAILURE
        # (DO-NOT-START); (d) seed_source is the LAST column of the boost ledger
        # and present in both pattern-log schemas (append-safe for old files).
        import aureon_validator as _v, dataclasses
        import boost_metrics as _bm, rogue_patternlog as _pl
        try:
            defaults_ok = (getattr(self.cfg, 'non_oco_enabled', None) is True
                           and getattr(self.cfg, 'rogue_enabled', None) is True
                           and getattr(self.cfg, 'rogue_seed_fallback', None)
                           == 'a1_time_snapshot')
            rep = _v.validate(self.cfg)
            names_ok = {c['name'] for c in rep['wiring_ok']}
            wired = ({'flag:non_oco_enabled', 'flag:rogue_seed_fallback',
                      'rogue:seed_fallback_valid',
                      'seam:LiveTrader._engine_enabled',
                      'seam:LiveTrader._anchor_entries_blocked',
                      'seam:LiveTrader._rogue_entries_blocked'} <= names_ok
                     and rep['verdict'] == 'SAFE-TO-START')
            bad = _v.validate(dataclasses.replace(self.cfg, rogue_seed_fallback='typo'))
            typo_blocks = (bad['verdict'] == 'DO-NOT-START'
                           and any(c['name'] == 'rogue:seed_fallback_valid'
                                   for c in bad['wiring_failures']))
            schema_ok = (_bm.LEDGER_COLUMNS[-1] == 'seed_source'
                         and _pl.PATTERN_COLUMNS[-1] == 'seed_source'
                         and _pl.TRADE_COLUMNS[-1] == 'seed_source'
                         and _bm.ledger_row({'ts': 't'})[-1] == '')
            ok = defaults_ok and wired and typo_blocks and schema_ok
            detail = (f"defaults_on={defaults_ok} validator_wired={wired} "
                      f"typo_mode_do_not_start={typo_blocks} "
                      f"seed_source_schema={schema_ok}")
        except Exception as e:
            self._record(223, FAIL, f"raised: {e!r}"); return
        self._record(223, PASS if ok else FAIL, detail)

    def _step_anchors_off_manage_only(self):
        # 224 (spec i) anchors OFF = MANAGE-ONLY:
        # (a) a DUE anchor does not place (the per-pass due-check gate) and flips
        # back on with the switch; (b) the boost family (F-B trapped late-rescue,
        # the immediate-fire path) never fires on a leg RESTORED into
        # shadow_positions while OFF -- but the whipsaw-cap enforcement still runs;
        # (c) wiring: trails / EOD / kill-switch / Friday poll-flatten in _tick are
        # OUTSIDE the engine gates (the ONLY engine-gated lines are the two entry
        # seams), so OFF can never orphan an open leg.
        import types, dataclasses, inspect
        import anchors as _anch, fills as _fills, live_trader as _lt
        from datetime import date as _date
        from utils import anchor_datetime_utc as _adu
        try:
            # --- (a) due-check gate ---
            def mk_due(anchors_on):
                processed, missed = [], []
                t = types.SimpleNamespace(
                    cfg=self.cfg, paused=False,
                    engines={'anchors': anchors_on, 'rogue': True},
                    state={'processed_anchors_today': [],
                           'missed_anchors_today': []},
                    _deferred_anchor=None, _last_anchor_attempt={},
                    _anchor_datetime_utc=_adu,
                    _save_state=lambda: None,
                    _process_anchor=lambda lbl, at: processed.append(lbl),
                    _anchor_missed=lambda lbl, at, now: missed.append(lbl))
                t._resolved_anchor_hm = (
                    lambda lbl, bd, h, m: _anch.resolved_anchor_hm(
                        lbl, bd, h, m, self.cfg))
                t._anchor_skipped_today_friday = (
                    lambda lbl, bd: _anch._anchor_skipped_today_friday(t, lbl, bd))
                return t, processed
            tuesday = _date(2026, 7, 7)
            at_a2 = pd.Timestamp('2026-07-07 07:00:30', tz='UTC')   # 10:00:30 broker
            t_off, proc_off = mk_due(False)
            _anch._process_anchor_if_due(t_off, tuesday, at_a2)
            t_on, proc_on = mk_due(True)
            _anch._process_anchor_if_due(t_on, tuesday, at_a2)
            no_place_off = (proc_off == [])
            places_on = ('A2_10h_London' in proc_on)

            # --- (b) boost family blocked on a RESTORED leg ---
            def mk_boost(anchors_on):
                fired, capped = [], []
                mt5 = types.SimpleNamespace(
                    symbol_info_tick=lambda s=None: types.SimpleNamespace(
                        bid=4051.54, ask=4051.74))   # mid 4051.64 = -$14 vs 4065.64
                # a trapped No-OCO leg exactly as restored from state.json
                shadow = {'anchor_label': 'A4_1640_NYopen', 'side': 'BUY',
                          'entry_price': 4065.64, 'leg_fill_price': 4065.64,
                          'boost_eligible': True, 'boost_rally_only': True}
                t = types.SimpleNamespace(
                    cfg=dataclasses.replace(self.cfg,
                                            trapped_late_rescue_enabled=True,
                                            trapped_rescue_arm_dollars=10.0),
                    paper=False, adapter=types.SimpleNamespace(mt5=mt5),
                    engines={'anchors': anchors_on, 'rogue': True},
                    shadow_positions={77: shadow},
                    tele=types.SimpleNamespace(info=lambda *a, **k: None),
                    _fire_boost_event=lambda tk, sh, plan: fired.append(plan.kind),
                    _enforce_boost_cap=lambda mid: capped.append(mid))
                return t, shadow, fired, capped
            tb_off, sh_off, fired_off, capped_off = mk_boost(False)
            _fills._check_boost_triggers(tb_off)
            tb_on, sh_on, fired_on, capped_on = mk_boost(True)
            _fills._check_boost_triggers(tb_on)
            boosts_blocked_off = (fired_off == [] and len(capped_off) == 1
                                  and not sh_off.get('trapped_rescue_fired'))
            boosts_fire_on = (len(fired_on) == 1
                              and sh_on.get('trapped_rescue_fired') is True)

            # --- (c) manage paths outside the gates ---
            src = inspect.getsource(_lt.LiveTrader._tick)
            manage_ungated = (
                src.count('self._anchor_entries_blocked(') == 1
                and src.count('self._rogue_entries_blocked(') == 1
                and 'self._manage_trails_on_bar_close()' in src
                and '_flatten_all(reason="EOD")' in src
                and 'self._friday_poll_flatten(broker_date, utc_now)' in src)

            ok = (no_place_off and places_on and boosts_blocked_off
                  and boosts_fire_on and manage_ungated)
            detail = (f"due_check_blocked_off={no_place_off} places_on={places_on} "
                      f"boost_family_blocked_off={boosts_blocked_off} "
                      f"cap_still_enforced={len(capped_off) == 1} "
                      f"boosts_fire_on={boosts_fire_on} "
                      f"manage_paths_ungated={manage_ungated}")
        except Exception as e:
            self._record(224, FAIL, f"raised: {e!r}"); return
        self._record(224, PASS if ok else FAIL, detail)

    def _step_rogue_off_manage_only(self):
        # 225 (spec ii) rogue OFF = MANAGE-ONLY: with allow_new_entries=False (the
        # _rogue_entries_blocked wiring), drive() still (a) advances the adaptive
        # trail on a restored OPEN Rogue leg and (b) books its broker close into
        # the governor -- but (c) takes NO fresh entry off the chain; (d) the same
        # env WITH entries allowed does enter (the block is the switch, nothing
        # else); (e) the seam blocks on the switch alone (a plain Tuesday).
        import rogue as _r, live_trader as _lt, types
        from datetime import date as _date
        try:
            tr, env, placed, modified, closed = self._mk_rogue_a1_trader(
                anchors_on=True, rogue_on=False, price=4010.0)
            env['book'][900] = True
            tr._rogue = {'day': '2026-07-06', 'gov': _r.new_day_state(),
                         'anchor': 4000.0, 'leg_dir': 'BUY', 'a1_last_close': None,
                         'a1_reverted': False,
                         'open': {'ticket': 900, 'side': 'BUY', 'entry': 4000.0,
                                  'sl': 3995.0, 'peak': 4000.0,
                                  'magic': _r.ROGUE_MAGIC,
                                  'leg_type': _r.ROGUE_LEG_TYPE}}
            _r.drive(tr, allow_new_entries=False)          # +$10: trail must advance
            trail_advanced = (modified == [(900, 4007.0)])
            env['book'].pop(900, None)                     # broker trail-out at 4007
            env['deal'], env['deal_px'] = 7.0, 4007.0
            _r.drive(tr, allow_new_entries=False)          # close books, no new entry
            close_booked = (tr._rogue['open'] is None
                            and abs(tr._rogue['gov']['day_pnl'] - 7.0) < 1e-9
                            and abs(tr._rogue['a1_last_close'] - 4007.0) < 1e-9)
            env['price'] = 4017.2                          # +$10.2 off the chain
            _r.drive(tr, allow_new_entries=False)
            no_entry_off = (placed == [])
            _r.drive(tr, allow_new_entries=True)           # control: flag is the block
            entered_on = (len(placed) == 1
                          and placed[0]['magic'] == _r.ROGUE_MAGIC)

            class _Stub:
                pass
            t = _Stub(); t.engines = {'anchors': True, 'rogue': False}
            t._friday_entries_blocked = lambda bd, un: False   # a plain Tuesday
            t._engine_enabled = lambda e: _lt.LiveTrader._engine_enabled(t, e)
            seam_blocks = (_lt.LiveTrader._rogue_entries_blocked(
                t, _date(2026, 7, 7), pd.Timestamp('2026-07-07 07:00:00', tz='UTC'))
                is True)

            ok = (trail_advanced and close_booked and no_entry_off and entered_on
                  and seam_blocks)
            detail = (f"trail_advanced={trail_advanced} close_booked={close_booked} "
                      f"no_entry_while_off={no_entry_off} control_enters={entered_on} "
                      f"seam_blocks_on_switch={seam_blocks}")
        except Exception as e:
            self._record(225, FAIL, f"raised: {e!r}"); return
        self._record(225, PASS if ok else FAIL, detail)

    def _step_engine_persist_override(self):
        # 226 (spec iii) toggle persists across a simulated restart + the
        # override-vs-config alert: (a) /anchors off persists engines into
        # run/state.json (force-saved, like the governors); (b) a SAME-day restart
        # restores it (persisted wins) and fires "ENGINE STATE OVERRIDE" naming
        # both values; (c) a NEW-day restart ignores the stale file (boot defaults,
        # no alert).
        import types, json, os, tempfile, shutil
        import live_trader as _lt, p1_state as _p1
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix='aureon_engswitch_')

            def mk(day):
                logs = []
                t = types.SimpleNamespace(
                    cfg=self.cfg, paper=True, run_dir=tmp,
                    engines={'anchors': True, 'rogue': True},
                    _engine_boot_defaults={'anchors': True, 'rogue': True},
                    state={'last_broker_date': day},
                    shadow_positions={}, shadow_pendings={}, _rogue=None,
                    _post_engines_status=lambda note='': None,
                    tele=types.SimpleNamespace(
                        info=lambda m, **k: logs.append(('info', str(m))),
                        warn=lambda m, **k: logs.append(('warn', str(m)))))
                t._engine_enabled = lambda e: _lt.LiveTrader._engine_enabled(t, e)
                return t, logs

            t1, _logs1 = mk('2026-07-06')
            _lt.LiveTrader._set_engine(t1, 'anchors', False, source='selftest')
            with open(os.path.join(tmp, 'state.json')) as f:
                snap = json.load(f)
            persisted = (snap.get('engines') == {'anchors': False, 'rogue': True}
                         and t1.engines['anchors'] is False)

            t2, logs2 = mk('2026-07-06')                    # SAME-day restart
            _p1.recover_on_boot(t2)
            restored = (t2.engines == {'anchors': False, 'rogue': True})
            alert = [m for k, m in logs2
                     if k == 'warn' and 'ENGINE STATE OVERRIDE' in m]
            alert_names_both = (len(alert) == 1 and 'OFF' in alert[0]
                                and 'ON' in alert[0] and 'anchors' in alert[0])

            t3, logs3 = mk('2026-07-07')                    # NEW-day restart
            _p1.recover_on_boot(t3)
            new_day_defaults = (t3.engines == {'anchors': True, 'rogue': True}
                                and not any('ENGINE STATE OVERRIDE' in m
                                            for _k, m in logs3))

            ok = persisted and restored and alert_names_both and new_day_defaults
            detail = (f"persisted={persisted} same_day_restored={restored} "
                      f"override_alert_names_both={alert_names_both} "
                      f"new_day_boot_defaults={new_day_defaults}")
        except Exception as e:
            self._record(226, FAIL, f"raised: {e!r}")
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            return
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        self._record(226, PASS if ok else FAIL, detail)

    def _step_seed_fallback_modes(self):
        # 227 (spec iv) anchors OFF + rogue ON -> Rogue still seeds via the
        # fallback and the chain runs: (a) a1_time_snapshot (DEFAULT) waits for
        # A1's scheduled time, captures the tick, logs "ROGUE SEED via
        # A1_TIME_SNAPSHOT @ px", enters on the $10 move and CHAINS after the
        # close; (b) the boost-ledger rows carry seed_source; (c) market_open
        # seeds off the first tick of the day without waiting for A1 time.
        import rogue as _r, csv, os, tempfile, shutil
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix='aureon_seedfb_')
            orig_sched = _r._a1_sched_reached
            a1_due = {'v': False}
            _r._a1_sched_reached = lambda trader: a1_due['v']
            try:
                tr, env, placed, modified, closed = self._mk_rogue_a1_trader(
                    anchors_on=False, rogue_on=True, price=4000.0, run_dir=tmp,
                    util_boost_ledger=True)
                _r.drive(tr)                       # pre-A1: day-open captured only
                pre_a1_waits = (placed == [] and tr._rogue.get('a1_snap_px') is None
                                and tr._rogue.get('day_open_px') == 4000.0
                                and not any('ROGUE SEED' in m
                                            for _k, m in env['logs']))
                a1_due['v'] = True
                env['price'] = 4005.0
                _r.drive(tr)                       # A1 time: snapshot -> seed @ 4005
                seed_logs = [m for _k, m in env['logs'] if 'ROGUE SEED' in m]
                seeded_at_a1 = (tr._rogue.get('a1_snap_px') == 4005.0
                                and tr._rogue.get('seed_px') == 4005.0
                                and tr._rogue.get('seed_source') == 'A1_TIME_SNAPSHOT'
                                and len(seed_logs) == 1
                                and 'via A1_TIME_SNAPSHOT @ 4005.0' in seed_logs[0]
                                and placed == [])   # move $0 -> no entry yet
                env['price'] = 4015.2              # +$10.2 off the 4005 seed
                _r.drive(tr)
                entered = (len(placed) == 1 and placed[0]['side'] == 'BUY'
                           and placed[0]['magic'] == _r.ROGUE_MAGIC)
                tk = 900 + len(placed)
                env['book'].pop(tk, None)          # broker closes it
                env['deal'], env['deal_px'] = 4.9, 4020.1
                _r.drive(tr)                       # books + CHAIN re-anchors @ exit
                env['price'] = 4030.5              # +$10.4 off the 4020.1 chain
                _r.drive(tr)
                chain_ran = (len(placed) == 2
                             and abs(tr._rogue['gov']['day_pnl'] - 4.9) < 1e-9)
                with open(os.path.join(tmp, 'boost_ledger.csv')) as f:
                    rows = [r for r in csv.DictReader(f) if r.get('kind') == 'ROGUE']
                ledger_tagged = (len(rows) >= 2 and all(
                    r.get('seed_source') == 'A1_TIME_SNAPSHOT' for r in rows))

                # (c) market_open: seeds off the FIRST tick, no A1-time wait.
                a1_due['v'] = False
                tr2, env2, placed2, _m2, _c2 = self._mk_rogue_a1_trader(
                    anchors_on=False, rogue_on=True, price=3990.0,
                    rogue_seed_fallback='market_open')
                _r.drive(tr2)                      # first tick = the seed
                env2['price'] = 4000.2             # +$10.2 off 3990
                _r.drive(tr2)
                mo_logs = [m for _k, m in env2['logs'] if 'ROGUE SEED' in m]
                market_open_seeds = (tr2._rogue.get('seed_source') == 'MARKET_OPEN'
                                     and tr2._rogue.get('seed_px') == 3990.0
                                     and len(placed2) == 1 and len(mo_logs) == 1
                                     and 'via MARKET_OPEN @ 3990.0' in mo_logs[0])
            finally:
                _r._a1_sched_reached = orig_sched
            ok = (pre_a1_waits and seeded_at_a1 and entered and chain_ran
                  and ledger_tagged and market_open_seeds)
            detail = (f"pre_a1_waits={pre_a1_waits} seeded_at_a1_time={seeded_at_a1} "
                      f"entered={entered} chain_ran={chain_ran} "
                      f"ledger_seed_source={ledger_tagged} "
                      f"market_open_mode={market_open_seeds}")
        except Exception as e:
            self._record(227, FAIL, f"raised: {e!r}")
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            return
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        self._record(227, PASS if ok else FAIL, detail)

    def _step_seed_a1_regression(self):
        # 228 (spec v, REGRESSION GUARD) both engines ON -> Rogue seeds off the
        # REAL A1 anchor exactly as master: resolve_seed returns the live
        # _a1_anchor_price read (source A1_ANCHOR), nothing latches (seed_px stays
        # None -- the per-tick read is preserved), the entry price/SL are exactly
        # a1_entry_decision off the A1 price, and a master-shape trader WITHOUT
        # the engines dict resolves identically (guarded default = ON).
        import rogue as _r, types
        try:
            tr, env, placed, _m, _c = self._mk_rogue_a1_trader(
                anchors_on=True, rogue_on=True, price=3981.0, a1_leg_px=3980.0)
            tr._rogue = None
            _r.drive(tr)                            # +$1: seeds, no entry
            st = tr._rogue
            px, src = _r.resolve_seed(tr, st)
            live_read = (px == 3980.0 and src == _r.SEED_A1_ANCHOR
                         and px == _r._a1_anchor_price(tr)
                         and st.get('seed_px') is None)
            seed_logs = [m for _k, m in env['logs'] if 'ROGUE SEED' in m]
            logged_a1 = (len(seed_logs) == 1
                         and 'via A1_ANCHOR @ 3980.0' in seed_logs[0])
            env['price'] = 3990.2                   # +$10.2 off the A1 anchor
            _r.drive(tr)
            exp_enter, exp_side, exp_px, exp_sl = _r.a1_entry_decision(
                3980.0, 3990.2, tr.cfg)
            entry_master = (exp_enter is True and len(placed) == 1
                            and placed[0]['side'] == exp_side == 'BUY'
                            and abs(placed[0]['sl'] - exp_sl) < 1e-9
                            and abs(st['open']['entry'] - exp_px) < 1e-9)
            # a master-shape trader (NO engines dict) resolves the same way.
            tr2, _e2, _p2, _m2, _c2 = self._mk_rogue_a1_trader(
                anchors_on=True, rogue_on=True, price=3981.0, a1_leg_px=3980.0)
            del tr2.engines
            st2 = {'day': '2026-07-06', 'gov': _r.new_day_state(), 'anchor': None,
                   'leg_dir': None, 'open': None, 'a1_last_close': None}
            px2, src2 = _r.resolve_seed(tr2, st2)
            legacy_shape_same = (px2 == 3980.0 and src2 == _r.SEED_A1_ANCHOR)
            ok = live_read and logged_a1 and entry_master and legacy_shape_same
            detail = (f"live_a1_read_no_latch={live_read} seed_logged_a1={logged_a1} "
                      f"entry_matches_master_decision={entry_master} "
                      f"legacy_trader_shape_same={legacy_shape_same}")
        except Exception as e:
            self._record(228, FAIL, f"raised: {e!r}"); return
        self._record(228, PASS if ok else FAIL, detail)

    def _step_no_midday_reseed(self):
        # 229 (spec vi) a mid-day toggle never double-seeds or orphans the day's
        # seed/chain: (a) a FALLBACK seed is LATCHED -- flipping anchors back ON
        # (with a live A1 leg at a DIFFERENT price) keeps the latched seed and
        # logs NO second "ROGUE SEED"; (b) a day seeded via the REAL A1 read that
        # then loses the anchor engine keeps the recorded A1 price (source stays
        # A1_ANCHOR, never flips to the snapshot); (c) an existing CHAIN target
        # always wins over any seed (a1_seed_anchor precedence).
        import rogue as _r, types
        try:
            orig_sched = _r._a1_sched_reached
            _r._a1_sched_reached = lambda trader: True
            try:
                # (a) fallback seed latches through an anchors-ON toggle
                tr, env, placed, _m, _c = self._mk_rogue_a1_trader(
                    anchors_on=False, rogue_on=True, price=4005.0)
                _r.drive(tr)                        # seeds via snapshot @ 4005
                st = tr._rogue
                seeded = (st.get('seed_px') == 4005.0
                          and st.get('seed_source') == 'A1_TIME_SNAPSHOT')
                tr.engines['anchors'] = True        # mid-day toggle ON
                tr.shadow_positions[55] = {'anchor_label': 'A1_02h_Asia',
                                           'side': 'BUY', 'leg_fill_price': 3980.0,
                                           'entry_price': 3980.0, 'magic': 20260522}
                _r.drive(tr)
                px, src = _r.resolve_seed(tr, st)
                latched = (px == 4005.0 and src == 'A1_TIME_SNAPSHOT')
                one_seed_log = (sum(1 for _k, m in env['logs']
                                    if 'ROGUE SEED' in m) == 1)

                # (b) A1-seeded day survives an anchors-OFF toggle unchanged
                tr2, env2, _p2, _m2, _c2 = self._mk_rogue_a1_trader(
                    anchors_on=True, rogue_on=True, price=3981.0, a1_leg_px=3980.0)
                _r.drive(tr2)                       # seeds via the real A1 read
                st2 = tr2._rogue
                st2['a1_snap_px'] = 4005.0          # a snapshot also exists...
                tr2.engines['anchors'] = False      # ...then the switch goes OFF
                px2, src2 = _r.resolve_seed(tr2, st2)
                a1_latched = (px2 == 3980.0 and src2 == _r.SEED_A1_ANCHOR
                              and st2.get('seed_px') == 3980.0)
                one_seed_log2 = (sum(1 for _k, m in env2['logs']
                                     if 'ROGUE SEED' in m) == 1)

                # (c) an existing chain target always wins over any seed
                chain_wins = (_r.a1_seed_anchor(4020.0, 4005.0) == 4020.0
                              and _r.a1_seed_anchor(4020.0, None) == 4020.0)
            finally:
                _r._a1_sched_reached = orig_sched
            ok = (seeded and latched and one_seed_log and a1_latched
                  and one_seed_log2 and chain_wins)
            detail = (f"fallback_seeded={seeded} latched_through_toggle={latched} "
                      f"single_seed_log={one_seed_log} a1_seed_survives_off="
                      f"{a1_latched} single_seed_log_b={one_seed_log2} "
                      f"chain_wins={chain_wins}")
        except Exception as e:
            self._record(229, FAIL, f"raised: {e!r}"); return
        self._record(229, PASS if ok else FAIL, detail)

    def _step_switch_friday_compose(self):
        # 230 (spec vii) the per-engine seams compose with the Friday window:
        # EITHER condition blocks (engine OFF on a plain Tuesday; engine ON past
        # the Friday cutoff; both ON pre-cutoff Friday = not blocked), and each
        # engine's switch only blocks ITS OWN seam.
        import live_trader as _lt, dataclasses
        from datetime import date as _date
        from utils import anchor_datetime_utc as _adu
        try:
            cfg = dataclasses.replace(self.cfg, friday_flatten_enabled=True,
                                      friday_flatten_broker_hour=22.5,
                                      broker_tz_offset_hours=3)

            def blocked(engine, *, anchors_on, rogue_on, day, when):
                class _Stub:
                    pass
                t = _Stub(); t.cfg = cfg
                t.engines = {'anchors': anchors_on, 'rogue': rogue_on}
                t._anchor_datetime_utc = _adu
                t._friday_flatten_reached = (
                    lambda bd, un: _lt.LiveTrader._friday_flatten_reached(t, bd, un))
                t._friday_entries_blocked = (
                    lambda bd, un: _lt.LiveTrader._friday_entries_blocked(t, bd, un))
                t._engine_enabled = lambda e: _lt.LiveTrader._engine_enabled(t, e)
                fn = (_lt.LiveTrader._anchor_entries_blocked if engine == 'anchors'
                      else _lt.LiveTrader._rogue_entries_blocked)
                return fn(t, day, when)

            tue = _date(2026, 7, 7)
            tue_noon = pd.Timestamp('2026-07-07 09:00:00', tz='UTC')
            fri = _date(2026, 7, 3)
            fri_pre = pd.Timestamp('2026-07-03 18:59:00', tz='UTC')   # 21:59 broker
            fri_post = pd.Timestamp('2026-07-03 19:31:00', tz='UTC')  # 22:31 broker

            checks = {
                'switch_blocks_tuesday_anchor': blocked(
                    'anchors', anchors_on=False, rogue_on=True,
                    day=tue, when=tue_noon) is True,
                'switch_blocks_tuesday_rogue': blocked(
                    'rogue', anchors_on=True, rogue_on=False,
                    day=tue, when=tue_noon) is True,
                'friday_blocks_switch_on_anchor': blocked(
                    'anchors', anchors_on=True, rogue_on=True,
                    day=fri, when=fri_post) is True,
                'friday_blocks_switch_on_rogue': blocked(
                    'rogue', anchors_on=True, rogue_on=True,
                    day=fri, when=fri_post) is True,
                'both_open_pre_cutoff_anchor': blocked(
                    'anchors', anchors_on=True, rogue_on=True,
                    day=fri, when=fri_pre) is False,
                'both_open_pre_cutoff_rogue': blocked(
                    'rogue', anchors_on=True, rogue_on=True,
                    day=fri, when=fri_pre) is False,
                'anchor_switch_not_cross_wired': blocked(
                    'rogue', anchors_on=False, rogue_on=True,
                    day=tue, when=tue_noon) is False,
                'rogue_switch_not_cross_wired': blocked(
                    'anchors', anchors_on=True, rogue_on=False,
                    day=tue, when=tue_noon) is False,
            }
            ok = all(checks.values())
            detail = " ".join(f"{k}={v}" for k, v in checks.items())
        except Exception as e:
            self._record(230, FAIL, f"raised: {e!r}"); return
        self._record(230, PASS if ok else FAIL, detail)

    def _step_scoped_flatten_confirm(self):
        # 231 (spec viii) the confirm-gated per-magic flatten commands:
        # (a) bare `/rogue flatten` and `/anchors flatten` only REPLY with the
        # open-position count + confirm hint (nothing closed/cancelled);
        # (b) `/rogue flatten confirm` closes ONLY the Rogue 20260626 position +
        # cancels ONLY the Rogue pending; (c) `/anchors flatten confirm` closes
        # ONLY the 20260522 book (position + pending) and SKIPS the Rogue
        # force-close inside risk._flatten_all (scope="ANCHORS").
        import types
        import live_trader as _lt, risk as _risk, rogue as _rogue
        try:
            env = {'positions': {501: 20260522, 900: _rogue.ROGUE_MAGIC},
                   'pendings': {502: 20260522, 901: _rogue.ROGUE_MAGIC},
                   'closed': [], 'cancelled': [], 'logs': []}
            mt5 = types.SimpleNamespace(
                positions_get=lambda ticket=None, symbol=None: (
                    ([types.SimpleNamespace(ticket=ticket)]
                     if int(ticket) in env['positions'] else [])
                    if ticket is not None else
                    [types.SimpleNamespace(ticket=tk, magic=mg, type=0,
                                           price_open=4000.0, sl=3982.0, tp=4030.0)
                     for tk, mg in env['positions'].items()]),
                orders_get=lambda ticket=None, symbol=None: (
                    ([types.SimpleNamespace(ticket=ticket)]
                     if int(ticket) in env['pendings'] else [])
                    if ticket is not None else
                    [types.SimpleNamespace(ticket=tk, magic=mg, type=2,
                                           price_open=4010.0)
                     for tk, mg in env['pendings'].items()]))
            adapter = types.SimpleNamespace(
                mt5=mt5,
                close_position=lambda tk, dry_run=False: (
                    env['closed'].append(int(tk)),
                    env['positions'].pop(int(tk), None)),
                cancel_order=lambda tk, dry_run=False: (
                    env['cancelled'].append(int(tk)),
                    env['pendings'].pop(int(tk), None)))
            tr = types.SimpleNamespace(
                cfg=self.cfg, adapter=adapter, paper=True,
                engines={'anchors': True, 'rogue': True},
                shadow_positions={501: {'anchor_label': 'A1', 'side': 'BUY'}},
                shadow_pendings={502: {'anchor_label': 'A1', 'side': 'BUY',
                                       'sibling_ticket': None}},
                _deferred_anchor=None, state={},
                _rogue={'open': {'ticket': 900, 'side': 'BUY', 'entry': 4000.0,
                                 'magic': _rogue.ROGUE_MAGIC},
                        'gov': _rogue.new_day_state()},
                tele=types.SimpleNamespace(
                    info=lambda m, **k: env['logs'].append(('info', str(m))),
                    warn=lambda m, **k: env['logs'].append(('warn', str(m))),
                    error=lambda m, **k: env['logs'].append(('error', str(m))),
                    critical=lambda m, **k: env['logs'].append(('critical', str(m)))),
                _FRIDAY_ANCHOR_MAGIC=_lt.LiveTrader._FRIDAY_ANCHOR_MAGIC)
            tr._friday_query_flat = lambda: _lt.LiveTrader._friday_query_flat(tr)
            tr._open_counts_per_magic = (
                lambda: _lt.LiveTrader._open_counts_per_magic(tr))
            tr._flatten_all = (lambda reason="Manual", scope="ALL":
                               _risk._flatten_all(tr, reason=reason, scope=scope))
            orig_pnl, orig_persist = _rogue._rogue_close_pnl, _rogue._persist_state
            _rogue._rogue_close_pnl = lambda *a, **k: 0.0
            _rogue._persist_state = lambda *a, **k: None
            try:
                # (a) bare flatten: count + confirm hint, nothing touched
                _lt.LiveTrader._handle_engine_flatten(tr, 'rogue', False)
                _lt.LiveTrader._handle_engine_flatten(tr, 'anchors', False)
                bare_asks = (env['closed'] == [] and env['cancelled'] == []
                             and sum(1 for _k, m in env['logs']
                                     if 'flatten confirm' in m) == 2)
                # (b) rogue confirm: ONLY 900 closed + ONLY 901 cancelled
                _lt.LiveTrader._handle_engine_flatten(tr, 'rogue', True)
                rogue_scoped = (env['closed'] == [900] and env['cancelled'] == [901]
                                and 501 in env['positions']
                                and 502 in env['pendings']
                                and tr._rogue['open'] is None)
                # (c) anchors confirm: ONLY the 20260522 book; Rogue block skipped
                env['positions'][900] = _rogue.ROGUE_MAGIC   # a NEW Rogue ticket...
                tr._rogue['open'] = {'ticket': 900, 'side': 'BUY', 'entry': 4000.0,
                                     'magic': _rogue.ROGUE_MAGIC}   # ...tracked open
                _lt.LiveTrader._handle_engine_flatten(tr, 'anchors', True)
                anchors_scoped = (env['closed'] == [900, 501]   # 900 from (b) only
                                  and 900 in env['positions']    # Rogue untouched
                                  and tr._rogue['open'] is not None
                                  and 501 not in env['positions']
                                  and 502 not in env['pendings'])
            finally:
                _rogue._rogue_close_pnl = orig_pnl
                _rogue._persist_state = orig_persist
            ok = bare_asks and rogue_scoped and anchors_scoped
            detail = (f"bare_replies_and_asks_confirm={bare_asks} "
                      f"rogue_confirm_scoped={rogue_scoped} "
                      f"anchors_confirm_scoped={anchors_scoped}")
        except Exception as e:
            self._record(231, FAIL, f"raised: {e!r}"); return
        self._record(231, PASS if ok else FAIL, detail)

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
        # 96 CASE 2: parent SELL already +$25 favorable (>= the D-4 $12 threshold,
        # lowered from $20 2026-07-03 -- W-7), violent same-direction crash the candle
        # gate FAILS ('reversed') -> the parent-profit override FIRES the boost
        # (returns True) and logs BREAK_OVERRIDE_PARENT_ESTABLISHED carrying
        # parent_max_fav / threshold / move_dollars for the trial.
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
                         and abs(kw.get('threshold', 0) - 12.0) < 0.05
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
        # (max_fav +$8 < the D-4 $12 threshold) -> the override does NOT apply, the
        # strict gate is fully in force, and the fresh spike STILL BLOCKS (returns
        # False). This is the -$701 fake-spike path: it must never fire just because
        # the move looked violent.
        import rally as _rally
        try:
            bars = self._case2_bars()
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                               parent_side='SELL', parent_max_fav=8.0)
            blocked = (_rally.break_and_hold_ok(tr, sh, pl) is False)
            no_override = not any(e[0] == 'break_override_parent_established'
                                  for e in self._gate_ptrace)
            # boundary: just below threshold ($11.99) still blocks (>= is the gate).
            tr2, sh2, pl2 = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                                  parent_side='SELL', parent_max_fav=11.99)
            boundary_blocks = (_rally.break_and_hold_ok(tr2, sh2, pl2) is False)
            ok = blocked and no_override and boundary_blocks
            detail = (f"fresh_spike_blocks={blocked} no_override_logged={no_override} "
                      f"below_threshold_11.99_blocks={boundary_blocks}")
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
        # 100 (repurposed 2026-07-02): A3 CUT. A3_1430_Overlap was removed from
        # cfg.anchors per its per-anchor P&L (June -$2,255 PF 0.68, July -$385 --
        # both months negative; the v3.3.6 17:00-IST retime did not fix it).
        # Assert NO A3-prefixed anchor remains configured, and that the survivors
        # A1/A2/A4/A5 keep their exact broker + IST times (the cut changed the
        # list only). The stale DEFER_WAIT_BY_ANCHOR['A3_1430_Overlap'] key is
        # deliberate (harmless lookup-only; kept for a possible restore).
        import anchors as _anchors
        try:
            amap = {lbl: (h, m) for (lbl, h, m) in self.cfg.anchors}
            a3_gone = not any(lbl[:2] == 'A3' for lbl in amap)
            def ist(lbl):
                h, m = amap[lbl]; return _anchors.anchor_ist_hm(h, m, self.cfg)
            a1_ok = amap.get('A1_02h_Asia') == (2, 30) and ist('A1_02h_Asia') == (5, 0)
            a2_ok = amap.get('A2_10h_London') == (10, 0) and ist('A2_10h_London') == (12, 30)
            a4_ok = amap.get('A4_1640_NYopen') == (16, 40) and ist('A4_1640_NYopen') == (19, 10)
            a5_ok = amap.get('A5_1930_LateUS') == (19, 30) and ist('A5_1930_LateUS') == (22, 0)
            ok = a3_gone and a1_ok and a2_ok and a4_ok and a5_ok
            detail = (f"a3_cut={a3_gone} "
                      f"A1/A2/A4/A5_unchanged={a1_ok and a2_ok and a4_ok and a5_ok}")
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

    # --- anchor-list validation (dynamic) -------------------------------------
    def _step_five_anchors_times(self):
        # 103 (rewritten 2026-07-02, A3 cut): validate the CONFIGURED anchor list
        # dynamically instead of hard-asserting len==5 / prefixes A1-A5 -- the cut
        # rule (v2.9.4: persistent losers get cut on the live record) means the
        # list may shrink or grow again. Asserts, for WHATEVER is configured:
        # labels well-formed ('A<n>_' tag + non-empty suffix, so label[:2] per-
        # anchor logic and mt5_comment prefixes keep working), broker times valid
        # (0-23h / 0-59m), NO duplicate labels / prefixes / times, the IST
        # converter is pure for every entry, chronological order, and the Monday
        # override shifts ONLY A1 (every other anchor resolves unchanged).
        import anchors as _anchors, re as _re
        from datetime import date as _date, timedelta as _td
        try:
            entries = list(self.cfg.anchors)
            labels = [lbl for (lbl, _, _) in entries]
            prefixes = [l[:2] for l in labels]
            non_empty = len(entries) >= 1
            labels_ok = all(bool(_re.fullmatch(r'A\d_[A-Za-z0-9_]+', l)) for l in labels)
            times_valid = all(0 <= h <= 23 and 0 <= m <= 59 for (_, h, m) in entries)
            no_dupes = (len(set(labels)) == len(labels)
                        and len(set(prefixes)) == len(prefixes)
                        and len({(h, m) for (_, h, m) in entries}) == len(entries))
            ordered = all((entries[i][1], entries[i][2]) < (entries[i + 1][1], entries[i + 1][2])
                          for i in range(len(entries) - 1))
            ist_pure = all(
                (lambda ih_im: 0 <= ih_im[0] <= 23 and 0 <= ih_im[1] <= 59)
                (_anchors.anchor_ist_hm(h, m, self.cfg)) for (_, h, m) in entries)
            # Monday: ONLY A1 gets the cold-start cushion; everyone else unchanged.
            base = _date(2026, 6, 24); monday = base - _td(days=base.weekday())
            mon_ok = True
            for (lbl, h, m) in entries:
                res = _anchors.resolved_anchor_hm(lbl, monday, h, m, self.cfg)
                want = (tuple(self.cfg.monday_a1_override)
                        if lbl[:2] == 'A1' and self.cfg.monday_a1_override else (h, m))
                mon_ok = mon_ok and (res == want)
            ok = (non_empty and labels_ok and times_valid and no_dupes and ordered
                  and ist_pure and mon_ok)
            detail = (f"n={len(entries)} labels_ok={labels_ok} times_valid={times_valid} "
                      f"no_dupes={no_dupes} ordered={ordered} ist_pure={ist_pure} "
                      f"monday_only_A1={mon_ok}")
        except Exception as e:
            self._record(103, FAIL, f"raised: {e!r}"); return
        self._record(103, PASS if ok else FAIL, detail)

    def _step_anchor_no_collision(self):
        # 104: NO collision among the CONFIGURED anchors -- the minimum pairwise gap
        # (with the 24h wrap, on BOTH a weekday and Monday) is well clear of testfire_
        # collision_min; A4<->A5 is 2h50m. The rail-4 guard (testfire.minutes_to_
        # nearest_anchor) handles the full list without error. Anchor-count agnostic
        # (2026-07-02 A3 cut). If any pair collided this FAILS loudly.
        import anchors as _anchors, testfire as _tf
        from datetime import date as _date, timedelta as _td
        try:
            def min_gap(broker_date):
                mins = sorted(sum(_anchors.resolved_anchor_hm(l, broker_date, h, m, self.cfg)[i] * (60 if i == 0 else 1)
                                  for i in (0, 1)) for (l, h, m) in self.cfg.anchors)
                g = []
                for i in range(len(mins)):
                    d = (mins[(i + 1) % len(mins)] - mins[i]) % 1440
                    g.append(min(d, 1440 - d))
                return min(g)
            base = _date(2026, 6, 24); monday = base - _td(days=base.weekday()); tuesday = monday + _td(days=1)
            min_wk = min_gap(tuesday); min_mon = min_gap(monday)
            COLL = int(getattr(self.cfg, 'testfire_collision_min', 30))
            no_collision = (min_wk > COLL and min_mon > COLL and min_wk >= 60 and min_mon >= 60)
            a4 = next((h, m) for (l, h, m) in self.cfg.anchors if l[:2] == 'A4')
            a5 = next((h, m) for (l, h, m) in self.cfg.anchors if l[:2] == 'A5')
            a4a5 = abs((a5[0] * 60 + a5[1]) - (a4[0] * 60 + a4[1]))
            a4a5_ok = (a4a5 == 170)   # 2h50m
            rail4_ok = (_tf.minutes_to_nearest_anchor(self.cfg, pd.Timestamp('2026-06-24T12:00:00Z')) is not None)
            ok = no_collision and a4a5_ok and rail4_ok
            detail = (f"min_gap_weekday={min_wk}m monday={min_mon}m (>{COLL}) "
                      f"A4<->A5={a4a5}m(2h50m)={a4a5_ok} rail4_guard_all_anchors={rail4_ok}")
        except Exception as e:
            self._record(104, FAIL, f"raised: {e!r}"); return
        self._record(104, PASS if ok else FAIL, detail)

    def _step_a5_identical_fp5(self):
        # 105: A5 uses IDENTICAL logic (no special-casing) and the FP guard handles 5
        # anchors. A5: label[:2]=='A5', shares the same SL $18 / TP $30 / lot 0.35
        # knobs as every anchor, NO Monday override (only A1), and a real defer wait.
        # FP guard: per-stack worst-case = n legs (anchor-count agnostic) and the result
        # is INVARIANT to how many anchors are configured (it caps the STACK, and total
        # exposure is bounded per anchor since the 5 fire 2h+ apart, never overlapping).
        import anchors as _anchors, fp_guard as _fp, dataclasses, live_trader as _lt
        from datetime import date as _date, timedelta as _td
        try:
            a5 = next(l for (l, _, _) in self.cfg.anchors if l[:2] == 'A5')
            shared_ok = (a5 == 'A5_1930_LateUS'
                         and float(self.cfg.sl_dist) == 18.0
                         and float(self.cfg.tp_dist) == 30.0
                         and float(self.cfg.lot_size) == 0.35)
            base = _date(2026, 6, 24); monday = base - _td(days=base.weekday())
            no_override = (_anchors.resolved_anchor_hm(a5, monday, 19, 30, self.cfg) == (19, 30))
            defer_ok = ('A5_1930_LateUS' in _lt.LiveTrader.DEFER_WAIT_BY_ANCHOR)
            # FP guard handles 5 anchors:
            bal = 50000.0
            per = _fp.per_leg_loss_usd(self.cfg.lot_size, _fp.effective_sl_dist(self.cfg), 100.0)
            g5 = _fp.guard_cfg(5, self.cfg, bal)
            fp_wc_ok = abs(g5[1] - 5 * per) < 0.01                 # worst-case = 5 legs
            fp_action_valid = g5[0] in (_fp.OK, _fp.REDUCE, _fp.BLOCK)
            fp_allowed_bound = (0 <= g5[3] <= 5)
            fp_ok_iff_within = ((g5[0] == _fp.OK) == (g5[1] <= g5[2]))  # OK iff worst-case <= limit
            # invariant to anchor count: the guard ignores the anchor list entirely.
            # (a PROPER subset -- since the 2026-07-02 A3 cut the full list is 4, so
            # slice to 3 to keep the invariance check non-trivial.)
            cfg4 = dataclasses.replace(self.cfg, anchors=list(self.cfg.anchors[:3]))
            fp_invariant = (_fp.guard_cfg(5, cfg4, bal) == g5)
            fp_handles_5 = (fp_wc_ok and fp_action_valid and fp_allowed_bound
                            and fp_ok_iff_within and fp_invariant)
            ok = shared_ok and no_override and defer_ok and fp_handles_5
            detail = (f"A5_shared_knobs(SL18/TP30/lot0.35)={shared_ok} no_monday_override={no_override} "
                      f"defer_wait={defer_ok} fp_guard_handles_5={fp_handles_5} "
                      f"(wc=${g5[1]:.0f} lim=${g5[2]:.0f} act={g5[0]} allowed={g5[3]})")
        except Exception as e:
            self._record(105, FAIL, f"raised: {e!r}"); return
        self._record(105, PASS if ok else FAIL, detail)

    # --- v3.4.0 RALLY override pullback-entry (flag-gated, DEFAULT OFF) -------
    def _step_override_freeze_guard(self):
        # 106 FREEZE GUARD: with override_entry_enabled=False (default), break_and_hold_ok
        # fires the override IMMEDIATELY exactly as v3.3.8 -- no arm, no pullback. Same
        # override-grade SELL crash as test 96: returns True and emits the LEGACY
        # BREAK_OVERRIDE event (no entry_mode field), and NO arm/skip events.
        import rally as _rally
        try:
            flag_off = (bool(getattr(self.cfg, 'override_entry_enabled', False)) is False)
            bars = self._case2_bars()
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                               parent_side='SELL', parent_max_fav=25.0)
            fired = (_rally.break_and_hold_ok(tr, sh, pl) is True)
            ev = [e for e in self._gate_ptrace if e[0] == 'break_override_parent_established']
            legacy_immediate = (len(ev) == 1 and 'entry_mode' not in ev[0][1])
            no_arm_events = not any(e[0] in ('override_entry_armed', 'override_entry_skipped')
                                    for e in self._gate_ptrace)
            ok = flag_off and fired and legacy_immediate and no_arm_events
            detail = (f"flag_default_off={flag_off} fires_immediately={fired} "
                      f"legacy_event_no_pullback={legacy_immediate} no_arm/skip={no_arm_events}")
        except Exception as e:
            self._record(106, FAIL, f"raised: {e!r}"); return
        self._record(106, PASS if ok else FAIL, detail)

    def _step_override_arm_no_fire(self):
        # 107 ARM-NO-FIRE: flag ON, parent override-grade (+$25) -> first gate eval ARMS
        # (state registered) and returns False (NO order). OVERRIDE_ENTRY_ARMED emitted.
        import rally as _rally, dataclasses
        try:
            cfg_on = dataclasses.replace(self.cfg, override_entry_enabled=True)
            bars = [{'high': 101.0, 'low': 99.0, 'close': 100.0},
                    {'high': 102.0, 'low': 100.0, 'close': 101.0}]
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='BUY',
                                               parent_side='BUY', parent_max_fav=25.0,
                                               cfg=cfg_on, last_mid=100.0)
            no_fire = (_rally.break_and_hold_ok(tr, sh, pl) is False)
            armed = bool(sh.get('override_arm', {}).get('armed'))
            armed_ev = any(e[0] == 'override_entry_armed' for e in self._gate_ptrace)
            ok = no_fire and armed and armed_ev
            detail = (f"flag_on_no_fire={no_fire} state_armed={armed} armed_ptrace={armed_ev}")
        except Exception as e:
            self._record(107, FAIL, f"raised: {e!r}"); return
        self._record(107, PASS if ok else FAIL, detail)

    def _step_retired_108(self):
        # 108 RETIRED (P4, 2026-07-03): was "ovr pullback fire", the dedicated test for
        # rally.override_pullback_step (v3.4.0 first-touch arm-then-pullback). That
        # standalone state machine was DEAD CODE -- superseded by the shared
        # pullback_entry.step adaptive machine (v3.5.0), which is fully covered by
        # steps 114-121. Deleted with the function (P4 dead-code pass); the slot is
        # kept (not renumbered) since _report()/STEP_NAMES require contiguous 1..N.
        self._record(108, PASS, "retired: override_pullback_step deleted (dead, "
                                "superseded by pullback_entry.step, see 114-121)")

    def _step_retired_109(self):
        # 109 RETIRED (P4, 2026-07-03): was "ovr timeout skip" -- see 108's note.
        self._record(109, PASS, "retired: override_pullback_step deleted (dead, "
                                "superseded by pullback_entry.step, see 114-121)")

    def _step_retired_110(self):
        # 110 RETIRED (P4, 2026-07-03): was "ovr parent-exit" -- see 108's note.
        self._record(110, PASS, "retired: override_pullback_step deleted (dead, "
                                "superseded by pullback_entry.step, see 114-121)")

    def _step_override_rescue_unaffected(self):
        # 111 RESCUE-UNAFFECTED: with override_entry_enabled ON, the RESCUE plan is
        # byte-identical (SL $10, cap -$700, bypass) -- the override gate is RALLY-only
        # and rescue never reaches it. Plan identical flag ON vs OFF.
        import boosts as _boosts, dataclasses
        try:
            cfg_on = dataclasses.replace(self.cfg, override_entry_enabled=True)
            r_on = _boosts.plan_boost_event('BUY', 4000.0, 4000.0 - 10.0, cfg_on)
            r_off = _boosts.plan_boost_event('BUY', 4000.0, 4000.0 - 10.0, self.cfg)
            sl_ok = (abs(r_on.sl_dollars - 10.0) < 1e-9 and r_on.kind == r_off.kind
                     and abs(r_on.sl_dollars - r_off.sl_dollars) < 1e-9)
            cap_ok = abs(_boosts.boost_whipsaw_cap(cfg_on, 'RESCUE') - 700.0) < 1e-6
            bypass_ok = bool(getattr(cfg_on, 'rescue_bypass_break_and_hold', True))
            ok = sl_ok and cap_ok and bypass_ok
            detail = (f"rescue_SL$10_identical_on/off={sl_ok} cap-$700={cap_ok} bypass={bypass_ok}")
        except Exception as e:
            self._record(111, FAIL, f"raised: {e!r}"); return
        self._record(111, PASS if ok else FAIL, detail)

    def _step_override_5arm_unaffected(self):
        # 112 +$5-ARM-UNAFFECTED: the normal +$5 RALLY arm (boosts.plan_boost_event) is
        # unchanged with the flag ON or OFF -- it does not read override_entry_*. A +$5
        # winning leg yields the same RALLY plan (SL $13) regardless of the flag.
        import boosts as _boosts, dataclasses
        try:
            cfg_on = dataclasses.replace(self.cfg, override_entry_enabled=True)
            p_on = _boosts.plan_boost_event('BUY', 4000.0, 4005.0, cfg_on)
            p_off = _boosts.plan_boost_event('BUY', 4000.0, 4005.0, self.cfg)
            arm_ok = (p_on is not None and p_on.kind == 'RALLY'
                      and abs(p_on.sl_dollars - 13.0) < 1e-9
                      and p_off is not None and p_on.kind == p_off.kind
                      and abs(p_on.sl_dollars - p_off.sl_dollars) < 1e-9)
            ok = arm_ok
            detail = (f"+$5_RALLY_arm_identical_on/off(kind={p_on.kind if p_on else None},"
                      f"sl={p_on.sl_dollars if p_on else None})={arm_ok}")
        except Exception as e:
            self._record(112, FAIL, f"raised: {e!r}"); return
        self._record(112, PASS if ok else FAIL, detail)

    def _step_override_no_pullback_collision(self):
        # 113 NO-COLLISION: the new ENTRY keys are DISTINCT from the rally_pullback_*
        # EXIT detector, defaults intact, and the EXIT detector still cuts with the
        # ENTRY flag ON (override_entry_* never touches strategy._update_boost_on_bar).
        import dataclasses
        from strategy import update_position_on_bar
        try:
            entry_keys = {'override_entry_enabled', 'override_entry_pullback_dollars',
                          'override_entry_arm_timeout_candles'}
            exit_keys = {'rally_pullback_enabled', 'rally_pullback_tol_dollars',
                         'rally_pullback_time_bound_min'}
            disjoint = (entry_keys.isdisjoint(exit_keys)
                        and all(not k.startswith('rally_pullback') for k in entry_keys))
            defaults_ok = (getattr(self.cfg, 'rally_pullback_enabled') is False
                           and abs(getattr(self.cfg, 'rally_pullback_tol_dollars') - 7.5) < 1e-9
                           and getattr(self.cfg, 'override_entry_enabled') is False
                           and abs(getattr(self.cfg, 'override_entry_pullback_dollars') - 13.0) < 1e-9
                           and int(getattr(self.cfg, 'override_entry_arm_timeout_candles')) == 4)
            # EXIT detector still cuts with the ENTRY flag ON (entry flag is inert here).
            cfg = dataclasses.replace(self.cfg, rally_pullback_enabled=True,
                                      rally_pullback_tol_dollars=8.0,
                                      override_entry_enabled=True)
            entry = 100.0
            ts0 = pd.Timestamp('2026-06-24T02:30:00Z')
            pc = self._rally_boost(cfg, entry, ts0)
            update_position_on_bar(pc, pd.Series({'open': 99, 'high': 99, 'low': 91, 'close': 92}),
                                   ts0 + pd.Timedelta(minutes=1), cfg)
            exit_still_cuts = bool(pc.closed)
            ok = disjoint and defaults_ok and exit_still_cuts
            detail = (f"keys_disjoint={disjoint} defaults_intact={defaults_ok} "
                      f"exit_detector_cuts_with_entry_flag_on={exit_still_cuts}")
        except Exception as e:
            self._record(113, FAIL, f"raised: {e!r}"); return
        self._record(113, PASS if ok else FAIL, detail)

    # --- v3.5.0 adaptive pullback entry (RALLY + RESCUE) ---------------------
    def _pb_run(self, prices, *, direction, depth, fixed_sl, timeout=4, dynamic=True,
                allow_smooth=False, smooth_from=None, buckets=None):
        # Drive the PURE pullback_entry.step over a price path until ENTER/SKIP (or end).
        # smooth_from = index from which break-and-hold is treated as CONFIRMED.
        # Returns (decision, state, index, first_action).
        import pullback_entry as _pe
        st = {}
        first = None
        last = None
        for i, p in enumerate(prices):
            b = buckets[i] if buckets else 0
            sc = bool(smooth_from is not None and i >= smooth_from)
            last = _pe.step(st, direction=direction, pullback_depth=depth,
                            fixed_sl=fixed_sl, timeout_candles=timeout,
                            current_price=float(p), m5_bucket=b, parent_alive=True,
                            smooth_confirm=sc, allow_smooth=allow_smooth, dynamic_sl=dynamic)
            if first is None:
                first = last['action']
            if last['action'] in (_pe.ENTER, _pe.SKIP):
                return last, st, i, first
        return last, st, len(prices) - 1, first

    def _step_v35_rally_freeze(self):
        # 114 FREEZE — RALLY: override_entry_enabled=False (default) -> the override
        # fires IMMEDIATELY exactly as v3.4.0/v3.3.8 (legacy BREAK_OVERRIDE, no entry_mode,
        # no arm/skip). Byte-identical OFF path.
        import rally as _rally
        try:
            flag_off = (bool(getattr(self.cfg, 'override_entry_enabled', False)) is False)
            bars = self._case2_bars()
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                               parent_side='SELL', parent_max_fav=25.0)
            fired = (_rally.break_and_hold_ok(tr, sh, pl) is True)
            ev = [e for e in self._gate_ptrace if e[0] == 'break_override_parent_established']
            legacy = (len(ev) == 1 and 'entry_mode' not in ev[0][1])
            no_arm = not any(e[0] in ('override_entry_armed', 'override_entry_skipped',
                                      'rescue_entry_armed') for e in self._gate_ptrace)
            ok = flag_off and fired and legacy and no_arm
            detail = f"flag_off={flag_off} immediate_fire={fired} legacy_event={legacy} no_arm={no_arm}"
        except Exception as e:
            self._record(114, FAIL, f"raised: {e!r}"); return
        self._record(114, PASS if ok else FAIL, detail)

    def _step_v35_rescue_freeze(self):
        # 115 FREEZE — RESCUE: rescue_entry_enabled=False (default) -> the scan's rescue
        # branch is NOT gated (today's immediate bypass-fire preserved). Proven by the
        # gating boolean (kind=='RESCUE' AND flag) being False on the default cfg, plus
        # the rescue plan SL $10 / cap -$700 unchanged.
        import boosts as _boosts, dataclasses
        try:
            flag_off = (bool(getattr(self.cfg, 'rescue_entry_enabled', False)) is False)
            # the exact scan gating condition for a RESCUE plan:
            gated_off = not (True and bool(getattr(self.cfg, 'rescue_entry_enabled', False)))
            cfg_on = dataclasses.replace(self.cfg, rescue_entry_enabled=True)
            gated_on = (True and bool(getattr(cfg_on, 'rescue_entry_enabled', False)))
            rescue_plan = _boosts.plan_boost_event('BUY', 4000.0, 4000.0 - 10.0, self.cfg)
            sl_ok = abs(rescue_plan.sl_dollars - 10.0) < 1e-9
            cap_ok = abs(_boosts.boost_whipsaw_cap(self.cfg, 'RESCUE') - 700.0) < 1e-6
            ok = flag_off and gated_off and gated_on and sl_ok and cap_ok
            detail = (f"flag_off={flag_off} bypass_when_off={gated_off} gated_when_on={gated_on} "
                      f"rescue_SL$10={sl_ok} cap-$700={cap_ok}")
        except Exception as e:
            self._record(115, FAIL, f"raised: {e!r}"); return
        self._record(115, PASS if ok else FAIL, detail)

    def _step_v35_rally_pullback(self):
        # 116 RALLY pullback entry: dip $13 from the high then TURN back up -> ENTER at
        # the turn, dynamic SL BELOW the dip low (dip_low - $13).
        try:
            d, st, i, first = self._pb_run([3982.0, 4005.0, 3992.0, 3994.0],
                                           direction='BUY', depth=13.0, fixed_sl=13.0,
                                           dynamic=True, allow_smooth=False)
            ok = (first == 'ARM' and d['action'] == 'ENTER' and d['mode'] == 'pullback'
                  and abs(d['price'] - 3994.0) < 1e-9 and abs(d['sl'] - 3979.0) < 1e-9)
            detail = f"enter@{d['price']}(=3994) SL@{d['sl']}(=3979,below dip 3992) mode={d['mode']}"
        except Exception as e:
            self._record(116, FAIL, f"raised: {e!r}"); return
        self._record(116, PASS if ok else FAIL, detail)

    def _step_v35_rally_smooth(self):
        # 117 RALLY smooth entry: no qualifying dip, break-and-hold CONFIRMS the up-move
        # -> ENTER on confirm, fixed SL entry-$13.
        try:
            d, st, i, first = self._pb_run([4000.0, 4002.0, 4004.0, 4006.0],
                                           direction='BUY', depth=13.0, fixed_sl=13.0,
                                           dynamic=True, allow_smooth=True, smooth_from=1)
            ok = (d['action'] == 'ENTER' and d['mode'] == 'smooth'
                  and abs(d['price'] - 4002.0) < 1e-9 and abs(d['sl'] - 3989.0) < 1e-9)
            detail = f"smooth_enter@{d['price']} SL@{d['sl']}(=entry-13) mode={d['mode']}"
        except Exception as e:
            self._record(117, FAIL, f"raised: {e!r}"); return
        self._record(117, PASS if ok else FAIL, detail)

    def _step_v35_rally_timeout(self):
        # 118 RALLY timeout: no dip, no smooth confirm -> SKIP after the timeout candles.
        try:
            d, st, i, first = self._pb_run([4000.0, 4001.0, 4000.0, 4001.0, 4000.0],
                                           direction='BUY', depth=13.0, fixed_sl=13.0,
                                           timeout=4, allow_smooth=True, smooth_from=None,
                                           buckets=[0, 1, 2, 3, 4])
            ok = (d['action'] == 'SKIP' and first == 'ARM')
            detail = f"action={d['action']}(=SKIP) first={first}"
        except Exception as e:
            self._record(118, FAIL, f"raised: {e!r}"); return
        self._record(118, PASS if ok else FAIL, detail)

    def _step_v35_rescue_pullback(self):
        # 119 RESCUE pullback entry: bounce UP $>=6 toward parent fill then ROLLOVER ->
        # ENTER SELL at the rollover, dynamic SL ABOVE the bounce high (bounce_high + $10).
        try:
            d, st, i, first = self._pb_run([4032.90, 4042.0, 4040.0],
                                           direction='SELL', depth=6.0, fixed_sl=10.0,
                                           dynamic=True, allow_smooth=False)
            ok = (first == 'ARM' and d['action'] == 'ENTER' and d['mode'] == 'pullback'
                  and abs(d['price'] - 4040.0) < 1e-9 and abs(d['sl'] - 4052.0) < 1e-9
                  and d['sl'] > 4042.0)
            detail = f"sell_enter@{d['price']}(=4040) SL@{d['sl']}(=4052,above bounce 4042) mode={d['mode']}"
        except Exception as e:
            self._record(119, FAIL, f"raised: {e!r}"); return
        self._record(119, PASS if ok else FAIL, detail)

    def _step_v35_rescue_smooth(self):
        # 120 RESCUE smooth entry: smooth DOWN-move, break-and-hold confirms -> ENTER
        # SELL on confirm, fixed SL entry+$10.
        try:
            d, st, i, first = self._pb_run([4032.0, 4030.0, 4028.0],
                                           direction='SELL', depth=6.0, fixed_sl=10.0,
                                           dynamic=True, allow_smooth=True, smooth_from=1)
            ok = (d['action'] == 'ENTER' and d['mode'] == 'smooth'
                  and abs(d['price'] - 4030.0) < 1e-9 and abs(d['sl'] - 4040.0) < 1e-9)
            detail = f"sell_smooth@{d['price']} SL@{d['sl']}(=entry+10) mode={d['mode']}"
        except Exception as e:
            self._record(120, FAIL, f"raised: {e!r}"); return
        self._record(120, PASS if ok else FAIL, detail)

    def _step_v35_rescue_timeout(self):
        # 121 RESCUE timeout: no bounce, no smooth confirm -> SKIP (parent takes its SL
        # alone, no hedge -- owner-confirmed acceptable).
        try:
            d, st, i, first = self._pb_run([4032.0, 4031.0, 4032.0, 4031.0, 4032.0],
                                           direction='SELL', depth=6.0, fixed_sl=10.0,
                                           timeout=4, allow_smooth=True, smooth_from=None,
                                           buckets=[0, 1, 2, 3, 4])
            ok = (d['action'] == 'SKIP' and first == 'ARM')
            detail = f"action={d['action']}(=SKIP) first={first}"
        except Exception as e:
            self._record(121, FAIL, f"raised: {e!r}"); return
        self._record(121, PASS if ok else FAIL, detail)

    def _step_v35_dynamic_sl(self):
        # 122 DYNAMIC SL: on the pullback path the SL is anchored BEYOND the retrace
        # extreme (dip_low - $13), NOT entry - $13. Same path, dynamic vs fixed differ.
        try:
            dyn, _, _, _ = self._pb_run([3982.0, 4005.0, 3992.0, 3994.0], direction='BUY',
                                        depth=13.0, fixed_sl=13.0, dynamic=True)
            fix, _, _, _ = self._pb_run([3982.0, 4005.0, 3992.0, 3994.0], direction='BUY',
                                        depth=13.0, fixed_sl=13.0, dynamic=False)
            dynamic_anchored = abs(dyn['sl'] - (3992.0 - 13.0)) < 1e-9   # dip_low - 13
            fixed_from_entry = abs(fix['sl'] - (3994.0 - 13.0)) < 1e-9   # entry - 13
            differ_more_room = dyn['sl'] < fix['sl']                     # dynamic gives more room
            ok = dynamic_anchored and fixed_from_entry and differ_more_room
            detail = (f"dynamic@{dyn['sl']}(=dip-13) fixed@{fix['sl']}(=entry-13) "
                      f"dynamic_more_room={differ_more_room}")
        except Exception as e:
            self._record(122, FAIL, f"raised: {e!r}"); return
        self._record(122, PASS if ok else FAIL, detail)

    def _step_v35_separation(self):
        # 123 SEPARATION: the shared helper is STATELESS (no module state) -> rally and
        # rescue runs are independent; and the two flags toggle independently (own keys).
        import pullback_entry as _pe, dataclasses
        try:
            st_rally = {}
            st_rescue = {}
            # interleave: a rescue step must not perturb the rally state and vice versa.
            _pe.step(st_rally, direction='BUY', pullback_depth=13.0, fixed_sl=13.0,
                     timeout_candles=4, current_price=100.0, m5_bucket=0, parent_alive=True,
                     smooth_confirm=False, allow_smooth=False, dynamic_sl=True)
            _pe.step(st_rescue, direction='SELL', pullback_depth=6.0, fixed_sl=10.0,
                     timeout_candles=4, current_price=200.0, m5_bucket=0, parent_alive=True,
                     smooth_confirm=False, allow_smooth=False, dynamic_sl=True)
            independent = (st_rally.get('cont_ext') == 100.0 and st_rescue.get('cont_ext') == 200.0
                           and st_rally is not st_rescue)
            # flags toggle independently (distinct config fields).
            a = dataclasses.replace(self.cfg, override_entry_enabled=True, rescue_entry_enabled=False)
            b = dataclasses.replace(self.cfg, override_entry_enabled=False, rescue_entry_enabled=True)
            flags_independent = (a.override_entry_enabled and not a.rescue_entry_enabled
                                 and not b.override_entry_enabled and b.rescue_entry_enabled)
            ok = independent and flags_independent
            detail = f"helper_stateless_independent={independent} flags_independent={flags_independent}"
        except Exception as e:
            self._record(123, FAIL, f"raised: {e!r}"); return
        self._record(123, PASS if ok else FAIL, detail)

    def _step_v35_5arm_unaffected(self):
        # 124 $5-ARM UNAFFECTED: the +$5 RALLY arm (plan_boost_event) is identical with
        # ALL new flags ON vs OFF -- it never reads override_entry_*/rescue_entry_*.
        import boosts as _boosts, dataclasses
        try:
            cfg_on = dataclasses.replace(self.cfg, override_entry_enabled=True,
                                         rescue_entry_enabled=True)
            p_on = _boosts.plan_boost_event('BUY', 4000.0, 4005.0, cfg_on)
            p_off = _boosts.plan_boost_event('BUY', 4000.0, 4005.0, self.cfg)
            ok = (p_on is not None and p_on.kind == 'RALLY' and abs(p_on.sl_dollars - 13.0) < 1e-9
                  and p_off is not None and p_on.kind == p_off.kind
                  and abs(p_on.sl_dollars - p_off.sl_dollars) < 1e-9)
            detail = f"+$5_RALLY_arm_identical(kind={p_on.kind},sl={p_on.sl_dollars})={ok}"
        except Exception as e:
            self._record(124, FAIL, f"raised: {e!r}"); return
        self._record(124, PASS if ok else FAIL, detail)

    def _step_v35_cap_unchanged(self):
        # 125 CAP UNCHANGED: rescue cap stays -$700 (SL stays $10) with rescue_entry ON.
        import boosts as _boosts, dataclasses
        try:
            cfg_on = dataclasses.replace(self.cfg, rescue_entry_enabled=True)
            cap_ok = abs(_boosts.boost_whipsaw_cap(cfg_on, 'RESCUE') - 700.0) < 1e-6
            sl_ok = abs(float(getattr(cfg_on, 'boost_sl_dollars', 10.0)) - 10.0) < 1e-9
            rally_cap_ok = abs(_boosts.boost_whipsaw_cap(cfg_on, 'RALLY') - 910.0) < 1e-6
            ok = cap_ok and sl_ok and rally_cap_ok
            detail = f"rescue_cap-$700={cap_ok} rescue_SL$10={sl_ok} rally_cap-$910={rally_cap_ok}"
        except Exception as e:
            self._record(125, FAIL, f"raised: {e!r}"); return
        self._record(125, PASS if ok else FAIL, detail)

    def _step_R1_spike_collapse(self):
        # R1 (126) SPIKE-THEN-COLLAPSE (rally): spike +$23 then straight down, no
        # dip-then-resume -> ARM, no qualifying turn, no confirm -> TIMEOUT-SKIP. (Old
        # behavior bought the spike top -> loss. New = $0.)
        try:
            d, st, i, first = self._pb_run(
                [3982.0, 4005.0, 4000.0, 3995.0, 3990.0, 3985.0, 3980.0, 3975.0],
                direction='BUY', depth=13.0, fixed_sl=13.0, timeout=4,
                allow_smooth=True, smooth_from=None,   # collapse never confirms
                buckets=[0, 0, 1, 2, 3, 4, 4, 4])
            ok = (first == 'ARM' and d['action'] == 'SKIP')
            detail = f"first={first} action={d['action']}(=SKIP, no top-buy)"
        except Exception as e:
            self._record(126, FAIL, f"raised: {e!r}"); return
        self._record(126, PASS if ok else FAIL, detail)

    def _step_R2_pull_continue(self):
        # R2 (127) PULLBACK-THEN-CONTINUE (Jun25 A3): parent BUY 3981.97, spike ~4005,
        # pullback to ~3992, then runs to 4030. ASSERT: enter ~3994 on the turn, SL below
        # the dip (~3979). Boost PROFITABLE (vs actual -905). Headline rally win.
        try:
            d, st, i, first = self._pb_run([3981.97, 4005.0, 3992.0, 3994.0, 4030.0],
                                           direction='BUY', depth=13.0, fixed_sl=13.0,
                                           dynamic=True, allow_smooth=False)
            entered = (d['action'] == 'ENTER' and d['mode'] == 'pullback')
            good_entry = abs(d['price'] - 3994.0) < 1e-9 and abs(d['sl'] - 3979.0) < 1e-9
            profitable_dir = (4030.0 - d['price']) > 0   # up-move continues above entry
            ok = entered and good_entry and profitable_dir
            detail = (f"enter@{d['price']} SL@{d['sl']} continues_to_4030_profit={profitable_dir} "
                      f"(vs actual -$905)")
        except Exception as e:
            self._record(127, FAIL, f"raised: {e!r}"); return
        self._record(127, PASS if ok else FAIL, detail)

    def _step_R3_rescue_bounce(self):
        # R3 (128) RESCUE BOUNCE-THEN-DROP (Jun25 A5): parent BUY 4042.90, rescue arms -10
        # (~4032.90), bounce to ~4042 then drops to 4025/4008. ASSERT: does NOT fire at -10,
        # enters SELL ~4040 on the rollover, SL above bounce high (~4052). PROFITABLE (vs -700).
        try:
            d, st, i, first = self._pb_run([4032.90, 4042.0, 4040.0, 4025.0, 4008.0],
                                           direction='SELL', depth=6.0, fixed_sl=10.0,
                                           dynamic=True, allow_smooth=False)
            not_edge = (first == 'ARM')                 # did NOT fire at the -10 edge
            entered = (d['action'] == 'ENTER' and d['mode'] == 'pullback')
            good_entry = abs(d['price'] - 4040.0) < 1e-9 and d['sl'] > 4042.0
            profitable_dir = (d['price'] - 4008.0) > 0   # drop continues below the SELL entry
            ok = not_edge and entered and good_entry and profitable_dir
            detail = (f"no_edge_fire={not_edge} sell@{d['price']} SL@{d['sl']}(above bounce) "
                      f"drops_to_4008_profit={profitable_dir} (vs actual -$700)")
        except Exception as e:
            self._record(128, FAIL, f"raised: {e!r}"); return
        self._record(128, PASS if ok else FAIL, detail)

    def _step_R4_pump_fade(self):
        # R4 (129) PUMP-THEN-FADE (rescue): price pumps up then fades back. ASSERT: ARMS,
        # enters on the fade ROLLOVER (not blindly at the edge). Must NOT fire at the arm.
        try:
            d, st, i, first = self._pb_run([4032.0, 4040.0, 4038.0, 4032.0, 4025.0],
                                           direction='SELL', depth=6.0, fixed_sl=10.0,
                                           dynamic=True, allow_smooth=False)
            not_edge = (first == 'ARM' and abs(d['price'] - 4032.0) > 1e-9)
            on_rollover = (d['action'] == 'ENTER' and d['mode'] == 'pullback'
                           and abs(d['price'] - 4038.0) < 1e-9)
            ok = not_edge and on_rollover
            detail = f"no_edge_fire={not_edge} enter_on_fade@{d['price']}(=4038) mode={d['mode']}"
        except Exception as e:
            self._record(129, FAIL, f"raised: {e!r}"); return
        self._record(129, PASS if ok else FAIL, detail)

    def _step_R5_smooth_runner(self):
        # R5 (130) SMOOTH RUNNER (rally): clean run-up, no pullback. ASSERT: smooth branch
        # fires IF break-and-hold confirms; if NO confirm -> SKIP (never blind-buy the top).
        try:
            up = [4000.0, 4002.0, 4004.0, 4006.0, 4008.0]
            d_conf, _, _, _ = self._pb_run(up, direction='BUY', depth=13.0, fixed_sl=13.0,
                                           allow_smooth=True, smooth_from=1)
            d_noconf, _, _, f2 = self._pb_run(up, direction='BUY', depth=13.0, fixed_sl=13.0,
                                              timeout=4, allow_smooth=True, smooth_from=None,
                                              buckets=[0, 1, 2, 3, 4])
            confirm_enters = (d_conf['action'] == 'ENTER' and d_conf['mode'] == 'smooth')
            noconfirm_skips = (d_noconf['action'] == 'SKIP')
            ok = confirm_enters and noconfirm_skips
            detail = (f"with_confirm_enters_smooth={confirm_enters} "
                      f"no_confirm_skips(no top-buy)={noconfirm_skips}")
        except Exception as e:
            self._record(130, FAIL, f"raised: {e!r}"); return
        self._record(130, PASS if ok else FAIL, detail)

    def _step_R6_chop_skip(self):
        # R6 (131) CHOP (rally + rescue): tight-range wiggles never qualify as a pullback
        # and never confirm -> SKIP (no boost on every wiggle).
        try:
            chop = [4000.0, 4002.0, 3999.0, 4001.0, 3998.0, 4000.0]
            bk = [0, 1, 2, 3, 4, 4]
            dr, _, _, fr = self._pb_run(chop, direction='BUY', depth=13.0, fixed_sl=13.0,
                                        timeout=4, allow_smooth=True, smooth_from=None, buckets=bk)
            dx, _, _, fx = self._pb_run(chop, direction='SELL', depth=6.0, fixed_sl=10.0,
                                        timeout=4, allow_smooth=True, smooth_from=None, buckets=bk)
            rally_skips = (dr['action'] == 'SKIP')
            rescue_skips = (dx['action'] == 'SKIP')
            ok = rally_skips and rescue_skips
            detail = f"rally_chop_skips={rally_skips} rescue_chop_skips={rescue_skips}"
        except Exception as e:
            self._record(131, FAIL, f"raised: {e!r}"); return
        self._record(131, PASS if ok else FAIL, detail)

    # --- ROGUE: self-anchoring monster-rider --------------------------------
    def _step_rogue_freeze_gate(self):
        # 132 GATE (v3.6.0): rogue_enabled boot default is now TRUE (the engine-switch
        # boot default; the old raw-False only existed to be demo-promoted anyway) ->
        # should_run True on a non-funded account. The MANDATORY account gates are
        # unchanged: a FUNDED account force-disables rogue even with the flag ON, and
        # an EXPLICIT rogue_enabled=False still kills the whole mechanism (should_run
        # False -> no watch/anchor/entry).
        import rogue as _rogue, dataclasses
        try:
            on_default = (bool(getattr(self.cfg, 'rogue_enabled', False)) is True
                          and _rogue.should_run(self.cfg, is_funded=False) is True)
            cfg_off = dataclasses.replace(self.cfg, rogue_enabled=False)
            off_kills = (_rogue.should_run(cfg_off, is_funded=False) is False)
            funded_gate = (_rogue.should_run(self.cfg, is_funded=True) is False)
            ok = on_default and off_kills and funded_gate
            detail = (f"boot_default_on={on_default} explicit_off_kills={off_kills} "
                      f"funded_force_off={funded_gate}")
        except Exception as e:
            self._record(132, FAIL, f"raised: {e!r}"); return
        self._record(132, PASS if ok else FAIL, detail)

    def _step_rogue_detect_monster(self):
        # 133 SELF-ANCHORING: a strong move (4 same-dir M5 closes, range>=$15, thrust)
        # drops the anchor at the MOVE-COMPLETION PRICE (the turn extreme), not a clock
        # time. Jun26-like: 4024 -> 3983 SELL monster -> anchor @ 3983.
        import rogue as _rogue
        try:
            cs = [{'open': 4024, 'high': 4025, 'low': 4014, 'close': 4015},
                  {'open': 4015, 'high': 4016, 'low': 4004, 'close': 4005},
                  {'open': 4005, 'high': 4006, 'low': 3994, 'close': 3995},
                  {'open': 3995, 'high': 3996, 'low': 3983, 'close': 3984}]
            is_m, mdir, comp = _rogue.detect_monster(cs, self.cfg)
            ok = (is_m is True and mdir == 'SELL' and abs(comp - 3983.0) < 1e-9)
            detail = f"is_monster={is_m} dir={mdir} anchor@completion={comp}(=3983,not_clock)"
        except Exception as e:
            self._record(133, FAIL, f"raised: {e!r}"); return
        self._record(133, PASS if ok else FAIL, detail)

    def _step_rogue_weak_no_slot(self):
        # 134 SETUP GATE: a WEAK move (mixed direction OR range < $15) is NOT a monster ->
        # no anchor, no entry, no re-anchor slot consumed.
        import rogue as _rogue
        try:
            mixed = [{'open': 4000, 'high': 4006, 'low': 3999, 'close': 4005},   # up
                     {'open': 4005, 'high': 4006, 'low': 3998, 'close': 3999},   # down
                     {'open': 3999, 'high': 4006, 'low': 3998, 'close': 4005},   # up
                     {'open': 4005, 'high': 4006, 'low': 3998, 'close': 3999}]   # down
            small = [{'open': 4000, 'high': 4001, 'low': 3999, 'close': 4000.5},
                     {'open': 4000.5, 'high': 4001.5, 'low': 3999.5, 'close': 4001},
                     {'open': 4001, 'high': 4002, 'low': 4000, 'close': 4001.5},
                     {'open': 4001.5, 'high': 4002.5, 'low': 4000.5, 'close': 4002}]
            no_mixed = (_rogue.detect_monster(mixed, self.cfg)[0] is False)
            no_small = (_rogue.detect_monster(small, self.cfg)[0] is False)  # range < $15
            gov = _rogue.new_day_state()
            slot_unconsumed = (gov['reanchor_count'] == 0)   # detection never calls record_entry
            ok = no_mixed and no_small and slot_unconsumed
            detail = f"mixed_not_monster={no_mixed} small_range_not_monster={no_small} no_slot={slot_unconsumed}"
        except Exception as e:
            self._record(134, FAIL, f"raised: {e!r}"); return
        self._record(134, PASS if ok else FAIL, detail)

    def _step_rogue_cap_blocks(self):
        # 135 CAP: re-entry blocked once the daily cap (rogue_max_reentries_per_day=10)
        # is hit. Each NEW entry consumes one slot; the 11th is refused.
        import rogue as _rogue
        try:
            cap = int(getattr(self.cfg, 'rogue_max_reentries_per_day', 10))
            gov = _rogue.new_day_state()
            allowed = 0
            for _ in range(cap):
                ok_i, _r = _rogue.can_enter(gov, self.cfg)
                if ok_i:
                    allowed += 1
                    _rogue.record_entry(gov)
            blocked, reason = _rogue.can_enter(gov, self.cfg)
            ok = (cap == 10 and allowed == 10 and gov['reanchor_count'] == 10
                  and blocked is False and reason == 'daily_cap')
            detail = f"cap={cap} allowed={allowed} 11th_blocked={blocked is False} reason={reason}"
        except Exception as e:
            self._record(135, FAIL, f"raised: {e!r}"); return
        self._record(135, PASS if ok else FAIL, detail)

    def _step_rogue_early_entry(self):
        # 136 EARLY ENTRY (Jun26): anchor 3983, next leg BUY. Enter ~$20 in (4003), SL $5
        # tight (3998) -- NOT chasing the obvious top (~4080). A too-early move ($12 in,
        # 3995) does NOT enter.
        import rogue as _rogue
        try:
            en, epx, sl = _rogue.entry_decision(3983.0, 'BUY', 4003.0, self.cfg)
            early_ok = (en is True and abs(epx - 4003.0) < 1e-9 and abs(sl - 3998.0) < 1e-9
                        and epx < 4080.0)
            too_early = _rogue.entry_decision(3983.0, 'BUY', 3995.0, self.cfg)[0]
            ok = early_ok and (too_early is False)
            detail = f"enter@{epx}(~$20 in) SL@{sl}($5) not_top(<4080)={epx<4080} 12in_no_enter={not too_early}"
        except Exception as e:
            self._record(136, FAIL, f"raised: {e!r}"); return
        self._record(136, PASS if ok else FAIL, detail)

    def _step_rogue_adaptive_trail(self):
        # 137 ADAPTIVE TRAIL: tight early ($3) until profit >= widen ($15), then wide
        # ($6) -- the transition that lets a real monster ride. early/deep/widen tested.
        import rogue as _rogue
        try:
            early = _rogue.trail_gap(10.0, self.cfg)   # < 15 -> 3
            at = _rogue.trail_gap(15.0, self.cfg)      # == 15 -> 6
            deep = _rogue.trail_gap(25.0, self.cfg)    # > 15 -> 6
            ok = (abs(early - 3.0) < 1e-9 and abs(at - 6.0) < 1e-9 and abs(deep - 6.0) < 1e-9)
            detail = f"early@$10={early}(=3) at_widen@$15={at}(=6) deep@$25={deep}(=6)"
        except Exception as e:
            self._record(137, FAIL, f"raised: {e!r}"); return
        self._record(137, PASS if ok else FAIL, detail)

    def _step_rogue_loss_stop(self):
        # 138 GOVERNOR: cumulative day P&L <= rogue_daily_loss_stop (E-5: -$525) STOPS new
        # entries for the day.
        import rogue as _rogue
        try:
            gov = _rogue.new_day_state()
            _rogue.record_close(gov, -300.0, was_fail=True, cfg=self.cfg)
            still_ok = _rogue.can_enter(gov, self.cfg)[0]            # -300 > -525 -> ok
            _rogue.record_close(gov, -260.0, was_fail=False, cfg=self.cfg)  # -560 -> stop
            blocked, reason = _rogue.can_enter(gov, self.cfg)
            ok = (still_ok is True and gov['loss_stopped'] is True
                  and blocked is False and reason == 'daily_loss_stop')
            detail = f"at_-300_ok={still_ok} at_-560_stops={gov['loss_stopped']} reason={reason}"
        except Exception as e:
            self._record(138, FAIL, f"raised: {e!r}"); return
        self._record(138, PASS if ok else FAIL, detail)

    def _step_rogue_fail_pause(self):
        # 139 GOVERNOR: 3 consecutive init-SL fake-outs PAUSE new entries; a winner in
        # between RESETS the streak.
        import rogue as _rogue
        try:
            gov = _rogue.new_day_state()
            for _ in range(3):
                _rogue.record_close(gov, -5.0, was_fail=True, cfg=self.cfg)
            blocked, reason = _rogue.can_enter(gov, self.cfg)
            pauses = (gov['consec_fails'] == 3 and gov['fail_paused'] is True
                      and blocked is False and reason == 'consecutive_fail_pause')
            g2 = _rogue.new_day_state()
            _rogue.record_close(g2, -5.0, True, self.cfg)
            _rogue.record_close(g2, -5.0, True, self.cfg)
            _rogue.record_close(g2, +12.0, False, self.cfg)   # a winner resets
            resets = (g2['consec_fails'] == 0 and g2['fail_paused'] is False)
            ok = pauses and resets
            detail = f"3_fails_pause={pauses} winner_resets_streak={resets}"
        except Exception as e:
            self._record(139, FAIL, f"raised: {e!r}"); return
        self._record(139, PASS if ok else FAIL, detail)

    def _step_rogue_closure_isolation(self):
        # 140 CLOSURE ISOLATION: a ROGUE close closes ONLY rogue legs; an ANCHOR close
        # closes ONLY anchor legs. No generic close-all; distinct magic/leg_type.
        import rogue as _rogue
        try:
            rogue_pos = {'magic': _rogue.ROGUE_MAGIC, 'leg_type': 'rogue'}
            anchor_pos = {'magic': 20260522, 'leg_type': 'normal'}
            r_closes_r = _rogue.closes(rogue_pos, 'ROGUE')
            r_skips_anchor = (_rogue.closes(anchor_pos, 'ROGUE') is False)
            a_closes_a = _rogue.closes(anchor_pos, 'ANCHOR')
            a_skips_rogue = (_rogue.closes(rogue_pos, 'ANCHOR') is False)
            ok = r_closes_r and r_skips_anchor and a_closes_a and a_skips_rogue
            detail = (f"rogue_close_only_rogue={r_closes_r and r_skips_anchor} "
                      f"anchor_close_only_anchor={a_closes_a and a_skips_rogue}")
        except Exception as e:
            self._record(140, FAIL, f"raised: {e!r}"); return
        self._record(140, PASS if ok else FAIL, detail)

    def _step_rogue_rescue_capped(self):
        # 141 REUSE — RESCUE on fail is CAPPED: a Rogue rescue leg reuses the RESCUE state
        # machine from the Rogue anchor and inherits the SAME derived cap discipline
        # (boost_whipsaw_cap -$700), so a bad Rogue catch cannot stack uncapped loss.
        # ROGUE-tagged (own magic, distinct from the anchor).
        import boosts as _boosts, rogue as _rogue
        try:
            cap = _boosts.boost_whipsaw_cap(self.cfg, 'RESCUE')
            capped = abs(cap - 700.0) < 1e-6
            breach = _boosts.cap_breached(-705.0, self.cfg, 'RESCUE')   # binds at the cap
            rogue_tagged = (_rogue.ROGUE_MAGIC != 20260522)
            ok = capped and (breach is True) and rogue_tagged
            detail = f"rescue_cap-$700={capped} cap_binds={breach} rogue_tagged={rogue_tagged}"
        except Exception as e:
            self._record(141, FAIL, f"raised: {e!r}"); return
        self._record(141, PASS if ok else FAIL, detail)

    def _step_rogue_rally_reuse(self):
        # 142 REUSE — RALLY on strength: a Rogue ride/pyramid reuses the shared RALLY
        # accessors ($5 arm) from the Rogue anchor, ROGUE-tagged. Shared HELPERS only --
        # no merged path (rogue has its own magic/leg_type/counter).
        import rally as _rally, rogue as _rogue
        try:
            arm = _rally.event_arm(self.cfg)              # reused $5 rally arm
            reuse_ok = abs(arm - 5.0) < 1e-9
            tag_ok = (_rogue.ROGUE_LEG_TYPE == 'rogue' and _rogue.ROGUE_MAGIC != 20260522)
            ok = reuse_ok and tag_ok
            detail = f"rally_arm_reused=${arm}(=5) rogue_tagged={tag_ok}"
        except Exception as e:
            self._record(142, FAIL, f"raised: {e!r}"); return
        self._record(142, PASS if ok else FAIL, detail)

    def _step_rogue_demo_funded(self):
        # 143 DEMO default-ON / FUNDED default-OFF: funded_default(demo, funded). A funded
        # account is NEVER promoted (the mandatory gate); only demo/non-funded runs hot.
        import rogue as _rogue
        try:
            demo_on = (_rogue.funded_default(True, False) is True)
            funded_off = (_rogue.funded_default(True, True) is False)   # funded wins
            funded_off2 = (_rogue.funded_default(False, True) is False)
            nondemo_off = (_rogue.funded_default(False, False) is False)
            ok = demo_on and funded_off and funded_off2 and nondemo_off
            detail = f"demo_ON={demo_on} funded_OFF(gate)={funded_off and funded_off2} nondemo_OFF={nondemo_off}"
        except Exception as e:
            self._record(143, FAIL, f"raised: {e!r}"); return
        self._record(143, PASS if ok else FAIL, detail)

    def _step_rogue_tagging(self):
        # 144 TAGGING: tag ROGUE, leg_type 'rogue', alert prefix [ROGUE], distinct magic,
        # own counter -- all distinct from the anchor + warmup magics.
        import rogue as _rogue
        try:
            ok = (_rogue.ROGUE_LABEL == 'ROGUE' and _rogue.ROGUE_LEG_TYPE == 'rogue'
                  and _rogue.ROGUE_ALERT_PREFIX == '[ROGUE]'
                  and _rogue.ROGUE_MAGIC not in (20260522, 9999998)
                  and 'reanchor_count' in _rogue.new_day_state()
                  and hasattr(self.cfg, 'rogue_max_reentries_per_day'))
            detail = (f"tag={_rogue.ROGUE_LABEL} leg_type={_rogue.ROGUE_LEG_TYPE} "
                      f"prefix={_rogue.ROGUE_ALERT_PREFIX} magic={_rogue.ROGUE_MAGIC}(distinct) own_counter=True")
        except Exception as e:
            self._record(144, FAIL, f"raised: {e!r}"); return
        self._record(144, PASS if ok else FAIL, detail)

    def _step_rogue_ride_unlimited(self):
        # 145 RIDE-WINNER-UNLIMITED: the cap counts ONLY new entries (record_entry).
        # Trailing an open winner consumes NO slot -- a single catch can ride a monster
        # as far as the trail goes. The cap (10) bounds NEW entries, not the ride.
        import rogue as _rogue
        try:
            gov = _rogue.new_day_state()
            _rogue.record_entry(gov)                      # one NEW entry
            # simulate a long ride: many trail updates, ZERO new entries -> count stays 1.
            for _profit in (5, 10, 20, 40, 80, 111):
                _ = _rogue.trail_gap(_profit, self.cfg)   # trailing only; no record_entry
            ride_uncapped = (gov['reanchor_count'] == 1)
            # the cap still bounds NEW entries to 10 regardless of how far winners ride.
            for _ in range(9):
                _rogue.record_entry(gov)
            blocked, reason = _rogue.can_enter(gov, self.cfg)
            cap_on_new_only = (gov['reanchor_count'] == 10 and blocked is False
                               and reason == 'daily_cap')
            ok = ride_uncapped and cap_on_new_only
            detail = f"trailing_consumes_no_slot={ride_uncapped} cap_counts_new_entries_only={cap_on_new_only}"
        except Exception as e:
            self._record(145, FAIL, f"raised: {e!r}"); return
        self._record(145, PASS if ok else FAIL, detail)

    # --- Watchdog boot validator (Task 1) -----------------------------------
    def _step_watchdog_safe_start(self):
        # 146 SAFE-TO-START: the validator probes the REAL cfg (its checks carry the
        # ACTUAL flag values, not a template) and returns 0 wiring failures -> SAFE.
        # LIVE-pending checks are present but do NOT block the verdict.
        import aureon_validator as _v
        try:
            rep = _v.validate(self.cfg)
            safe = (rep['verdict'] == 'SAFE-TO-START' and len(rep['wiring_failures']) == 0)
            flag_checks = [c for c in rep['checks'] if c['name'].startswith('flag:')]
            real_val = f"rogue_enabled={getattr(self.cfg, 'rogue_enabled')!r}"
            reflects_real = any(real_val in c['detail'] for c in flag_checks)  # actual state
            pending_nonblocking = (len(rep['pending']) >= 1 and rep['verdict'] == 'SAFE-TO-START')
            proceeds = (_v.run_boot_validation(self.cfg, skip=False) is True)
            default_on = (_v.VALIDATOR_ENABLED is True)
            ok = safe and reflects_real and pending_nonblocking and proceeds and default_on
            detail = (f"verdict={rep['verdict']} reflects_real_config={reflects_real} "
                      f"pending_nonblocking={pending_nonblocking} default_on={default_on}")
        except Exception as e:
            self._record(146, FAIL, f"raised: {e!r}"); return
        self._record(146, PASS if ok else FAIL, detail)

    def _step_watchdog_do_not_start(self):
        # 147 DO-NOT-START: a cfg MISSING the wired flags trips wiring failures ->
        # verdict DO-NOT-START and the boot gate returns False (abort, the bot must NOT
        # trade). --skip-validator bypasses (returns True), proving the escape exists but
        # is not the default.
        import aureon_validator as _v
        try:
            class _BadCfg:   # a cfg with NONE of the wired feature flags
                pass
            bad = _BadCfg()
            rep = _v.validate(bad)
            do_not_start = (rep['verdict'] == 'DO-NOT-START' and len(rep['wiring_failures']) >= 1)
            aborts = (_v.run_boot_validation(bad, skip=False) is False)   # boot gate aborts
            skip_bypasses = (_v.run_boot_validation(bad, skip=True) is True)  # explicit escape
            ok = do_not_start and aborts and skip_bypasses
            detail = (f"verdict={rep['verdict']} wiring_failures={len(rep['wiring_failures'])} "
                      f"boot_aborts={aborts} --skip_bypasses={skip_bypasses}")
        except Exception as e:
            self._record(147, FAIL, f"raised: {e!r}"); return
        self._record(147, PASS if ok else FAIL, detail)

    def _step_watchdog_rogue_rule(self):
        # 162 rogue promotion-rule line: the validator reports the rogue PROMOTION RULE
        # (report-only), not the live state. At watchdog time cfg.rogue_enabled is the RAW
        # value (promote_on_boot flips it later, inside the LiveTrader), so the line must
        # exist, be a passing WIRING check, and never gate the verdict.
        import aureon_validator as _v
        try:
            rep = _v.validate(self.cfg)
            named = [c for c in rep['checks'] if c['name'] == 'rogue:promotion_rule']
            present = (len(named) == 1)
            c = named[0] if present else {}
            ok_true = (present and c.get('ok') is True)
            is_wiring = (present and c.get('kind') == _v.WIRING)
            in_wiring_ok = any(x['name'] == 'rogue:promotion_rule' for x in rep['wiring_ok'])
            # report-only: it must NOT introduce a wiring failure for the real cfg.
            no_fail = all(x['name'] != 'rogue:promotion_rule' for x in rep['wiring_failures'])
            mentions_rule = (present and 'demo boot promotes ON' in c.get('detail', ''))
            ok = present and ok_true and is_wiring and in_wiring_ok and no_fail and mentions_rule
            detail = (f"present={present} ok={ok_true} wiring={is_wiring} "
                      f"in_wiring_ok={in_wiring_ok} reports_rule={mentions_rule}")
        except Exception as e:
            self._record(162, FAIL, f"raised: {e!r}"); return
        self._record(162, PASS if ok else FAIL, detail)

    def _step_rogue_promote_live_boot(self):
        # 163 run_live() promotes rogue on EVERY live boot. The in-class call
        # lives inside wait_until_market_open(), which RETURNS EARLY on the
        # weekend/sleep->wake path -> promotion never ran and rogue stayed
        # dormant. run_live() must promote unconditionally on the LIVE path
        # (after the adapter connects, before trading) and NEVER on paper.
        # Driven with stubs so no MT5 / live loop is needed: a demo-account stub
        # adapter, a no-op LiveTrader, and a throwaway cfg (the real self.cfg is
        # never mutated).
        import live_trader as _lt
        import mt5_adapter as _mt5a
        import types as _types
        try:
            DEMO = 0
            def _mk_adapter(is_demo):
                mt5 = _types.SimpleNamespace(
                    ACCOUNT_TRADE_MODE_DEMO=DEMO,
                    account_info=lambda: _types.SimpleNamespace(
                        trade_mode=(DEMO if is_demo else 2)))
                return _types.SimpleNamespace(mt5=mt5, shutdown=lambda: None)

            class _StubTrader:   # captures cfg/adapter; run() is a no-op (no loop)
                def __init__(self, cfg, adapter, paper=True):
                    self.cfg, self.adapter, self.paper = cfg, adapter, paper
                def run(self):
                    return None

            def _drive(paper, is_demo):
                # throwaway cfg so the shared self.cfg is never mutated.
                cfg = _types.SimpleNamespace(symbol='XAUUSD', rogue_enabled=False,
                                             EXPECTED_BROKER_OFFSET_HOURS=None)
                adapter = _mk_adapter(is_demo)
                orig_LT, orig_AD = _lt.LiveTrader, _mt5a.MT5Adapter
                _lt.LiveTrader = _StubTrader
                _mt5a.MT5Adapter = lambda *a, **k: adapter
                try:
                    _lt.run_live(cfg, paper=paper)
                finally:
                    _lt.LiveTrader, _mt5a.MT5Adapter = orig_LT, orig_AD
                return cfg.rogue_enabled

            live_demo_on = (_drive(paper=False, is_demo=True) is True)    # LIVE+demo -> ON
            live_funded_off = (_drive(paper=False, is_demo=False) is False)  # LIVE+funded -> OFF
            paper_never = (_drive(paper=True, is_demo=True) is False)     # PAPER -> never promotes
            ok = live_demo_on and live_funded_off and paper_never
            detail = (f"live_demo_promotes={live_demo_on} "
                      f"live_funded_forced_off={live_funded_off} "
                      f"paper_never_promotes={paper_never}")
        except Exception as e:
            self._record(163, FAIL, f"raised: {e!r}"); return
        self._record(163, PASS if ok else FAIL, detail)

    def _step_rogue_ml_gate(self):
        # 164 Rogue ML pipeline (pattern logger + model gate + archive). Covers:
        # (a) a pattern row is written on a Rogue eval, (b) the gate is pass-through when
        # DISABLED (trade goes through), (c) it BLOCKS when ENABLED + score<threshold,
        # (d) an untrained model returns 1.0, (e) anchors write NO rogue_patterns rows
        # (every row is the ROGUE magic; rogue-OFF writes nothing), (f) a predict() error
        # FAILS OPEN (score 1.0). Driven with stubs -- no MT5, no order ever placed for
        # real (place is captured).
        import rogue as _r
        import rogue_model as _rm
        import rogue_patternlog as _rpl
        import tempfile, shutil, os, csv, types
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix="aureon_mlgate_")

            # monster: 4 up M5 closes, range $20, real thrust -> BUY move, anchor=high.
            monster = [{'open': 4082.0, 'high': 4100.0, 'low': 4080.0, 'close': 4098.0}] * 4

            def _mk_trader(run_dir, *, rogue_on, gate_on, threshold=0.5, mid=4075.0):
                os.makedirs(run_dir, exist_ok=True)
                placed = []
                mt5 = types.SimpleNamespace(
                    ACCOUNT_TRADE_MODE_DEMO=0,
                    account_info=lambda: types.SimpleNamespace(trade_mode=0),
                    symbol_info_tick=lambda s=None: types.SimpleNamespace(
                        bid=mid - 0.1, ask=mid + 0.1),
                    positions_get=lambda ticket=None: [])
                def _place(symbol, side, lot, sl=None, tp=None, magic=None,
                           comment=None, dry_run=False):
                    placed.append({'side': side, 'magic': magic})
                    return types.SimpleNamespace(retcode=10009, order=5001, deal=5001)
                adapter = types.SimpleNamespace(
                    mt5=mt5, get_latest_m5=lambda sym, n: monster,
                    place_market_order=_place,
                    modify_position_sl=lambda *a, **k: types.SimpleNamespace(retcode=10009))
                cfg = types.SimpleNamespace(
                    symbol='XAUUSD', lot_size=0.01, rogue_enabled=rogue_on,
                    rogue_daywatch=True, rogue_min_candles=4, rogue_min_range=15.0,
                    rogue_body_mult=1.5, rogue_entry_confirm=20.0, rogue_init_sl=5.0,
                    rogue_max_reentries_per_day=10, rogue_daily_loss_stop=-150.0,
                    rogue_consecutive_fail_stop=3, rogue_trail_arm=5.0,
                    rogue_model_gate_enabled=gate_on, rogue_model_threshold=threshold,
                    rogue_model_path=os.path.join(tmp, "no_such_model.pkl"))
                tele = types.SimpleNamespace(info=lambda *a, **k: None,
                                             warning=lambda *a, **k: None)
                tr = types.SimpleNamespace(cfg=cfg, adapter=adapter, run_dir=run_dir,
                                           paper=True, tele=tele, _rogue=None, _rpl=None,
                                           state={'last_broker_date': '2026-06-29'},
                                           _last_boost_mid=mid)
                return tr, placed

            def _patterns(run_dir):
                p = os.path.join(run_dir, "rogue_patterns.csv")
                if not os.path.exists(p):
                    return []
                with open(p) as f:
                    return list(csv.DictReader(f))

            # (d) untrained model -> 1.0
            _rm.reset_singleton()
            untrained_one = (abs(_rm.RogueModel().predict(
                {'range_dollars': 20, 'body_ratio': 0.8}) - 1.0) < 1e-9)

            # (f) predict() error -> FAIL OPEN (1.0). A trained model with broken weights.
            broke = _rm.RogueModel()
            broke.trained = True
            broke.feature_order = ['range_dollars']
            broke.mean = None; broke.scale = None; broke.coef = None  # -> raises inside
            fail_open = (abs(broke.predict({'range_dollars': 20}) - 1.0) < 1e-9)

            # (b) gate DISABLED -> pass-through: entry placed, row decision ENTER.
            _rm.reset_singleton()
            d_off = os.path.join(tmp, "off")
            tr, placed = _mk_trader(d_off, rogue_on=True, gate_on=False)
            _r.drive(tr)
            rows_off = _patterns(d_off)
            passthrough_trades = (len(placed) == 1 and placed[0]['magic'] == _r.ROGUE_MAGIC)
            wrote_row = (len(rows_off) >= 1)
            row_is_enter = any(x['decision'] == 'ENTER' and x['model_score'] != ''
                               for x in rows_off)        # (a) row written w/ score

            # (c) gate ENABLED + forced LOW score -> BLOCK (no place, SKIP_BY_MODEL row).
            _rm.reset_singleton()
            class _LowModel:
                trained = True
                def predict(self, f): return 0.10
            _orig_get = _rm.get_model
            _rm.get_model = lambda path=None: _LowModel()
            try:
                d_on = os.path.join(tmp, "on")
                tr2, placed2 = _mk_trader(d_on, rogue_on=True, gate_on=True, threshold=0.5)
                _r.drive(tr2)
            finally:
                _rm.get_model = _orig_get
            rows_on = _patterns(d_on)
            gate_blocks = (len(placed2) == 0
                           and any(x['decision'] == 'SKIP_BY_MODEL' for x in rows_on))

            # (e) anchors write NO rows: rogue DISABLED -> drive no-op -> no file. And
            #     every row ever written carries the ROGUE magic (never the anchor magic).
            _rm.reset_singleton()
            d_anchor = os.path.join(tmp, "anchor")
            tr3, placed3 = _mk_trader(d_anchor, rogue_on=False, gate_on=False)
            _r.drive(tr3)
            anchor_silent = (not os.path.exists(os.path.join(d_anchor, "rogue_patterns.csv"))
                             and len(placed3) == 0)
            all_magics = {int(x['magic']) for x in (rows_off + rows_on)}
            rogue_only_magic = (all_magics == {_r.ROGUE_MAGIC}
                                and _r.ROGUE_MAGIC == 20260626)

            ok = (untrained_one and fail_open and passthrough_trades and wrote_row
                  and row_is_enter and gate_blocks and anchor_silent and rogue_only_magic)
            detail = (f"(a)row={wrote_row}&enter={row_is_enter} (b)passthrough={passthrough_trades} "
                      f"(c)blocks={gate_blocks} (d)untrained1.0={untrained_one} "
                      f"(e)anchor_silent={anchor_silent}&rogue_magic={rogue_only_magic} "
                      f"(f)fail_open={fail_open}")
        except Exception as e:
            self._record(164, FAIL, f"raised: {e!r}")
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            _rm.reset_singleton()
            return
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            _rm.reset_singleton()
        self._record(164, PASS if ok else FAIL, detail)

    def _step_rogue_ml_train(self):
        # 165 Rogue ML autotrain (champion/challenger, fail-safe) + exit-feature capture.
        # Covers: (g) autotrain SKIPS when rows<300; (h) champion is KEPT when the challenger
        # is not better (promotion rule + same-data re-train doesn't replace); a fresh model
        # is PROMOTED when there is no champion (model can come into being); and (i) EXIT
        # features (entry_price, max_fav, trail_path_summary, exit_price, held_minutes,
        # outcome_dollars) are captured on a simulated close, with a losing close re-labeled
        # FAKEOUT. All pure-Python -- no sklearn, no MT5.
        import rogue_autotrain as _rat
        import rogue_patternlog as _pl
        import tempfile, shutil, os, csv, types
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix="aureon_mltrain_")

            # --- (g) insufficient data -> skip ---
            run_small = os.path.join(tmp, "small")
            os.makedirs(run_small)
            for k in range(20):
                _pl.log_eval(run_small, ts=f"2026-06-29 0{k%6}:00:00", direction='BUY',
                             features={'range_dollars': 20, 'body_ratio': 0.8,
                                       'candle_count': 4, 'atr': 5, 'spread': 0.2,
                                       'confirm_dollars': 22, 'time_bucket': 'asia'},
                             decision='ENTER', model_score=1.0, entry_price=4000.0,
                             outcome_dollars=(10.0 if k % 2 else -5.0))
            v_small = _rat.run(run_small, archive_dir=os.path.join(tmp, "noarch"),
                               model_path=os.path.join(tmp, "m_small.pkl"))
            skips_small = (v_small['action'] == 'skip_insufficient' and v_small['rows'] < 300
                           and not os.path.exists(os.path.join(tmp, "m_small.pkl")))

            # --- (h) promotion rule: champion kept when challenger not better ---
            keep_worse = (_rat.decide_promotion({'acc': 0.71, 'fakeout_recall': 0.6},
                                                {'acc': 0.68, 'fakeout_recall': 0.6})[0] is False)
            promote_none = (_rat.decide_promotion(None, {'acc': 0.6, 'fakeout_recall': 0.5})[0] is True)
            promote_better = (_rat.decide_promotion({'acc': 0.60, 'fakeout_recall': 0.5},
                                                    {'acc': 0.70, 'fakeout_recall': 0.6})[0] is True)
            keep_equal = (_rat.decide_promotion({'acc': 0.70, 'fakeout_recall': 0.6},
                                                {'acc': 0.70, 'fakeout_recall': 0.6})[0] is False)

            # --- (h) integration: >=300 rows with a learnable signal. First run has NO
            #     champion -> PROMOTE; second run on the SAME data -> challenger == champion
            #     (not better by margin) -> KEEP. Proves a champion is never replaced by a
            #     non-better model. ---
            run_big = os.path.join(tmp, "big")
            os.makedirs(run_big)
            for k in range(360):
                win = (k % 2 == 0)                       # learnable: confirm high -> win
                _pl.log_eval(run_big, ts=f"2026-06-{1 + k // 24:02d} {k % 24:02d}:00:00",
                             direction=('BUY' if win else 'SELL'),
                             features={'range_dollars': (25 if win else 16),
                                       'body_ratio': (0.85 if win else 0.55),
                                       'candle_count': 4, 'atr': (6 if win else 4),
                                       'spread': 0.2,
                                       'confirm_dollars': (28 if win else 20),
                                       'time_bucket': ('london' if win else 'asia')},
                             decision='ENTER', model_score=1.0, entry_price=4000.0 + k,
                             outcome_dollars=(15.0 if win else -6.0))
            mp = os.path.join(tmp, "champ.pkl")
            v1 = _rat.run(run_big, archive_dir=os.path.join(tmp, "noarch"), model_path=mp)
            v2 = _rat.run(run_big, archive_dir=os.path.join(tmp, "noarch"), model_path=mp)
            promoted_from_nothing = (v1['action'] == 'promoted' and os.path.exists(mp))
            kept_on_rerun = (v2['action'] == 'kept_champion')

            # --- (i) exit-feature capture on a close (winner) + FAKEOUT relabel (loser) ---
            run_x = os.path.join(tmp, "exit")
            os.makedirs(run_x)
            # seed an ENTER row, then simulate the close via observe()'s backfill path.
            ts_enter = "2026-06-29 06:00:00"
            _pl.log_eval(run_x, ts=ts_enter, direction='SELL',
                         features={'range_dollars': 20, 'body_ratio': 0.8, 'candle_count': 4,
                                   'atr': 5, 'spread': 0.2, 'confirm_dollars': 25,
                                   'time_bucket': 'asia'},
                         decision='ENTER', model_score=1.0, entry_price=4075.0)
            # winning SELL: entry 4075 -> peak 4040 -> exit(sl) 4050 => +25 fav, +25 outcome.
            _pl.backfill_exit(run_x, ts_enter,
                              {'max_fav': 35.0, 'trail_path_summary': '4075.0->4040.0->4050.0',
                               'exit_price': 4050.0, 'held_minutes': 42.0,
                               'outcome_dollars': 25.0})
            with open(os.path.join(run_x, "rogue_patterns.csv")) as f:
                xrow = list(csv.DictReader(f))[0]
            exit_captured = all(str(xrow.get(c, '')) != '' for c in
                                ('entry_price', 'max_fav', 'trail_path_summary',
                                 'exit_price', 'held_minutes', 'outcome_dollars'))

            # losing close -> decision relabeled FAKEOUT.
            ts_enter2 = "2026-06-29 07:00:00"
            _pl.log_eval(run_x, ts=ts_enter2, direction='BUY',
                         features={'range_dollars': 18, 'body_ratio': 0.7, 'candle_count': 4,
                                   'atr': 4, 'spread': 0.2, 'confirm_dollars': 22,
                                   'time_bucket': 'london'},
                         decision='ENTER', model_score=1.0, entry_price=4000.0)
            _pl.backfill_exit(run_x, ts_enter2,
                              {'max_fav': 1.0, 'trail_path_summary': '4000->4001->3995',
                               'exit_price': 3995.0, 'held_minutes': 5.0,
                               'outcome_dollars': -5.0}, decision=_pl.FAKEOUT)
            with open(os.path.join(run_x, "rogue_patterns.csv")) as f:
                rows_x = list(csv.DictReader(f))
            fakeout_relabel = any(r['decision'] == 'FAKEOUT'
                                  and str(r['outcome_dollars']) == '-5.0' for r in rows_x)

            ok = (skips_small and keep_worse and promote_none and promote_better
                  and keep_equal and promoted_from_nothing and kept_on_rerun
                  and exit_captured and fakeout_relabel)
            detail = (f"(g)skip<300={skips_small} (h)keep_worse={keep_worse} "
                      f"promote_none={promote_none} promote_better={promote_better} "
                      f"keep_equal={keep_equal} promoted_new={promoted_from_nothing} "
                      f"kept_rerun={kept_on_rerun} (i)exit_captured={exit_captured} "
                      f"fakeout_relabel={fakeout_relabel}")
        except Exception as e:
            self._record(165, FAIL, f"raised: {e!r}")
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            return
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        self._record(165, PASS if ok else FAIL, detail)

    # --- E-12 feed-death watchdog (re-subscribe + throttled FEED DOWN alert) ----
    # All five drive the PURE feed_watchdog.FeedWatchdog with a synthetic monotonic
    # clock -- no MT5, no Discord -- so the live probe path and these tests honor the
    # exact same rule (import-path identity). 30 fails -> re-subscribe; 5 failed
    # attempts -> one alert then cooldown; warning throttled; disabled == byte-identical.
    def _feed_wd_cfg(self):
        import dataclasses
        return dataclasses.replace(
            self.cfg, feed_watchdog_enabled=True, feed_recover_after_fails=30,
            feed_recover_max_tries=5, feed_alert_cooldown_min=5.0)

    def _step_f1_feed_resubscribe_after_n(self):
        # 166 (T-F1): re-subscribe fires after EXACTLY feed_recover_after_fails (30)
        # consecutive failures -- not before. The first 29 never re-subscribe; the 30th does.
        import feed_watchdog as fw
        try:
            cfg = self._feed_wd_cfg()
            wd = fw.FeedWatchdog()
            none_before = all(not wd.on_failure(cfg, float(t)).resubscribe for t in range(29))
            a30 = wd.on_failure(cfg, 29.0)   # the 30th failure
            fires_at_30 = (a30.resubscribe and a30.attempt == 1 and a30.fails == 30)
            ok = none_before and fires_at_30
            detail = f"none_in_first29={none_before} resub_at_30={fires_at_30} attempt={a30.attempt}"
        except Exception as e:
            self._record(166, FAIL, f"raised: {e!r}"); return
        self._record(166, PASS if ok else FAIL, detail)

    def _step_f2_feed_success_resets(self):
        # 167 (T-F2): a successful probe (on_success) resets the counter + attempts +
        # last-alert, so the NEXT failure starts a clean episode (fails=1, no re-subscribe).
        import feed_watchdog as fw
        try:
            cfg = self._feed_wd_cfg()
            wd = fw.FeedWatchdog()
            for t in range(30):
                wd.on_failure(cfg, float(t))     # drive to 30 -> one re-subscribe (attempt 1)
            recovered = wd.on_success()
            reset = (wd.fails == 0 and wd.attempts == 0 and wd.last_alert_s is None)
            a_next = wd.on_failure(cfg, 100.0)   # fresh episode
            clean_restart = (a_next.fails == 1 and not a_next.resubscribe)
            ok = recovered and reset and clean_restart
            detail = f"recovered={recovered} reset={reset} clean_restart={clean_restart}"
        except Exception as e:
            self._record(167, FAIL, f"raised: {e!r}"); return
        self._record(167, PASS if ok else FAIL, detail)

    def _step_f3_feed_alert_then_cooldown(self):
        # 168 (Fix 4 ladder): after feed_recover_max_tries (5) failed re-subscribes EXACTLY
        # ONE FEED DOWN alert fires (at attempt 5 / fail 150) and the re-subscribe counter
        # STOPS at 5 (the 'attempt 6/5' bug fix -- it must never exceed the cap). The episode
        # then ESCALATES: Level 2 full reinit (up to feed_reinit_max_tries=2) then Level 3 a
        # single self-restart -- no endless re-alert loop.
        import feed_watchdog as fw
        try:
            cfg = self._feed_wd_cfg()
            wd = fw.FeedWatchdog()
            alerts = []; resub_attempts = []; reinits = []; restarts = 0
            for i in range(1, 400):              # 1s apart; blind grows with i
                a = wd.on_failure(cfg, float(i))
                if a.alert:
                    alerts.append((i, a.attempt, a.fails))
                if a.resubscribe:
                    resub_attempts.append(a.attempt)
                if a.reinit:
                    reinits.append(a.reinit_attempt)
                if a.self_restart:
                    restarts += 1
            one_alert = (len(alerts) == 1 and alerts[0][1] == 5 and alerts[0][2] == 150)
            # THE bug fix: the re-subscribe attempt counter never runs past the cap (no 6/5).
            counter_capped = (resub_attempts == [1, 2, 3, 4, 5])
            escalates = (reinits == [1, 2] and restarts == 1)
            ok = one_alert and counter_capped and escalates
            detail = (f"one_alert@attempt5/fail150={one_alert} "
                      f"resub_attempts={resub_attempts}(capped@5={counter_capped}) "
                      f"reinits={reinits} restarts={restarts}")
        except Exception as e:
            self._record(168, FAIL, f"raised: {e!r}"); return
        self._record(168, PASS if ok else FAIL, detail)

    def _step_f4_feed_warn_throttled(self):
        # 169 (T-F4): 1000 consecutive failures must NOT log 1000 warnings (the 13,833-line
        # bug). Throttled to the episode-start line + a count every N -> <= 1 + floor(1000/N).
        import feed_watchdog as fw
        try:
            cfg = self._feed_wd_cfg()
            wd = fw.FeedWatchdog()
            warns = sum(1 for i in range(1000) if wd.on_failure(cfg, float(i)).warn)
            heartbeat = 1000 // 30
            ok = (warns <= (1 + heartbeat)) and warns < 50
            detail = f"warns={warns} (<= 1+{heartbeat}={1 + heartbeat}; not 1000)"
        except Exception as e:
            self._record(169, FAIL, f"raised: {e!r}"); return
        self._record(169, PASS if ok else FAIL, detail)

    def _step_f5_feed_disabled_byte_identical(self):
        # 170 (T-F5): feed_watchdog_enabled=False -> on_failure warns EVERY call and NEVER
        # re-subscribes or alerts == the pre-fix per-tick warning (byte-identical behavior).
        import feed_watchdog as fw, dataclasses
        try:
            cfg_off = dataclasses.replace(self._feed_wd_cfg(), feed_watchdog_enabled=False)
            wd = fw.FeedWatchdog()
            acts = [wd.on_failure(cfg_off, float(i)) for i in range(100)]
            all_warn = all(a.warn for a in acts)
            no_action = not any(a.resubscribe or a.alert for a in acts)
            ok = all_warn and no_action
            detail = f"all_warn={all_warn} no_resubscribe_no_alert={no_action} (n=100)"
        except Exception as e:
            self._record(170, FAIL, f"raised: {e!r}"); return
        self._record(170, PASS if ok else FAIL, detail)

    # --- E-2/E-3/E-4 Rogue brakes (close-detection + governor + EOD flatten) --------
    # Drive the PURE rogue functions + rogue_patternlog.observe with a stub trader (no
    # MT5). ROGUE_TK is a Rogue ticket; ANCHOR_TK carries the anchor magic 20260522 so the
    # isolation step can prove the Rogue close path NEVER closes an anchor ticket.
    _R_ROGUE_TK = 900001
    _R_ANCHOR_TK = 20260522001

    def _r_trader(self, open_at_broker, pnl, cfg=None, paper=False):
        import types, rogue as _R
        tk = self._R_ROGUE_TK
        closes = []          # records every close_position(ticket) call
        def positions_get(ticket=None):
            return [object()] if (ticket == tk and open_at_broker) else []
        def history_deals_get(position=None):
            return ([types.SimpleNamespace(entry=1, profit=pnl, swap=0.0, commission=0.0)]
                    if position == tk else [])
        def close_position(t, dry_run=False):
            closes.append(int(t))
        mt5 = types.SimpleNamespace(positions_get=positions_get, history_deals_get=history_deals_get,
                                    account_info=lambda: types.SimpleNamespace(trade_mode=0),
                                    ACCOUNT_TRADE_MODE_DEMO=0)
        tr = types.SimpleNamespace(
            cfg=cfg or self.cfg, paper=paper,
            adapter=types.SimpleNamespace(mt5=mt5, close_position=close_position),
            tele=types.SimpleNamespace(info=lambda *a, **k: None, warn=lambda *a, **k: None),
            state={'last_broker_date': '2026-06-30'})
        tr._rogue = {'day': '2026-06-30', 'gov': _R.new_day_state(), 'anchor': 4000.0,
                     'leg_dir': 'BUY',
                     'open': {'ticket': tk, 'side': 'BUY', 'entry': 4000.0, 'sl': 3995.0,
                              'peak': 4010.0}}
        return tr, closes

    def _step_r1_rogue_record_close(self):
        # 171 (T-R1): a broker-side Rogue close books the governor ONCE (day_pnl reflects
        # the realized $) and clears st['open'] -- and issues NO close itself.
        import rogue as _R
        try:
            tr, closes = self._r_trader(open_at_broker=False, pnl=50.0)
            booked = _R.detect_close(tr, tr._rogue)
            ok = (booked and abs(tr._rogue['gov']['day_pnl'] - 50.0) < 1e-9
                  and tr._rogue['open'] is None and closes == [])
            detail = f"booked={booked} day_pnl={tr._rogue['gov']['day_pnl']} open_cleared={tr._rogue['open'] is None} closes={closes}"
        except Exception as e:
            self._record(171, FAIL, f"raised: {e!r}"); return
        self._record(171, PASS if ok else FAIL, detail)

    def _step_r2_rogue_reentry_allowed(self):
        # 172 (T-R2): after a (winning) close, st['open'] is None AND can_enter -> ok, so
        # Rogue can take its next entry the SAME day (E-3 re-entry restored).
        import rogue as _R
        try:
            tr, _ = self._r_trader(open_at_broker=False, pnl=50.0)
            _R.detect_close(tr, tr._rogue)
            ok_enter, reason = _R.can_enter(tr._rogue['gov'], tr.cfg)
            ok = tr._rogue['open'] is None and ok_enter and reason == 'ok'
            detail = f"open_cleared={tr._rogue['open'] is None} can_enter={ok_enter} reason={reason}"
        except Exception as e:
            self._record(172, FAIL, f"raised: {e!r}"); return
        self._record(172, PASS if ok else FAIL, detail)

    def _step_r3_rogue_observe_close(self):
        # 173 (T-R3): once st['open'] is cleared on a close, rogue_patternlog.observe()'s
        # CLOSE branch runs (F-A exit data captured) -- proven by its state effect
        # (open_ticket -> None, ticket added to the 'closed' set).
        import rogue_patternlog as _RPL, dataclasses, tempfile, shutil
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix='aureon_rogueobs_')
            tr, _ = self._r_trader(open_at_broker=False, pnl=20.0,
                                   cfg=dataclasses.replace(self.cfg, rogue_enabled=True))
            tr.run_dir = tmp
            _RPL.observe(tr)                       # snapshot the open position into _rpl
            snap_ok = tr._rpl.get('open_ticket') == self._R_ROGUE_TK
            tr._rogue['open'] = None               # detect_close cleared it
            _RPL.observe(tr)                       # open gone + ticket closed -> CLOSE branch
            ran = (tr._rpl.get('open_ticket') is None
                   and self._R_ROGUE_TK in tr._rpl.get('closed', set()))
            ok = snap_ok and ran
            detail = f"snapshot={snap_ok} close_branch_ran={ran}"
        except Exception as e:
            self._record(173, FAIL, f"raised: {e!r}")
            if tmp: shutil.rmtree(tmp, ignore_errors=True)
            return
        finally:
            if tmp: shutil.rmtree(tmp, ignore_errors=True)
        self._record(173, PASS if ok else FAIL, detail)

    def _step_r4_rogue_loss_stop_trips(self):
        # 174 (T-R4): with the governor now fed (E-2), a losing day that crosses the daily
        # loss stop (E-5: -$525) trips loss_stopped -> can_enter blocks. No longer inert.
        # (E-5 raised the stop -150 -> -525, so a single -$175 strike no longer halts; a
        # -$600 day does -- proven here via the live close path.)
        import rogue as _R
        try:
            tr, _ = self._r_trader(open_at_broker=False, pnl=-600.0)
            _R.detect_close(tr, tr._rogue)
            ok_enter, reason = _R.can_enter(tr._rogue['gov'], tr.cfg)
            ok = (tr._rogue['gov']['loss_stopped'] and not ok_enter
                  and reason == 'daily_loss_stop')
            detail = f"day_pnl={tr._rogue['gov']['day_pnl']} loss_stopped={tr._rogue['gov']['loss_stopped']} reason={reason}"
        except Exception as e:
            self._record(174, FAIL, f"raised: {e!r}"); return
        self._record(174, PASS if ok else FAIL, detail)

    def _step_r5_rogue_eod_flag(self):
        # 175 (T-R5): rogue_flatten_at_eod OFF -> eod_flatten is a no-op (rides);
        # ON -> it closes the open Rogue ticket and clears st['open']. The DEFAULT
        # flipped OFF -> ON 2026-07-02 (overnight/weekend gap risk), so the OFF leg
        # now forces the flag False explicitly, and the default itself is asserted ON.
        import rogue as _R, dataclasses
        try:
            default_on = (self.cfg.rogue_flatten_at_eod is True)
            tr_off, closes_off = self._r_trader(
                open_at_broker=True, pnl=30.0,
                cfg=dataclasses.replace(self.cfg, rogue_flatten_at_eod=False))
            did_off = _R.eod_flatten(tr_off)
            tr_on, closes_on = self._r_trader(
                open_at_broker=True, pnl=30.0,
                cfg=dataclasses.replace(self.cfg, rogue_flatten_at_eod=True))
            did_on = _R.eod_flatten(tr_on)
            ok = (default_on
                  and did_off is False and closes_off == [] and tr_off._rogue['open'] is not None
                  and did_on is True and closes_on == [self._R_ROGUE_TK]
                  and tr_on._rogue['open'] is None)
            detail = (f"default_ON={default_on} | OFF: did={did_off} closes={closes_off} "
                      f"rides={tr_off._rogue['open'] is not None} | ON: did={did_on} closes={closes_on}")
        except Exception as e:
            self._record(175, FAIL, f"raised: {e!r}"); return
        self._record(175, PASS if ok else FAIL, detail)

    def _step_r6_rogue_isolation(self):
        # 176 (T-R6): isolation -- the Rogue close path NEVER closes a 20260522 anchor
        # ticket. detect_close issues no close at all; eod_flatten(ON) closes ONLY the
        # Rogue ticket. Assert no anchor ticket is ever passed to close_position.
        import rogue as _R, dataclasses
        try:
            tr_d, closes_d = self._r_trader(open_at_broker=False, pnl=-10.0)
            _R.detect_close(tr_d, tr_d._rogue)                       # books, no close
            tr_e, closes_e = self._r_trader(
                open_at_broker=True, pnl=10.0,
                cfg=dataclasses.replace(self.cfg, rogue_flatten_at_eod=True))
            _R.eod_flatten(tr_e)                                     # closes ROGUE tk only
            all_closes = closes_d + closes_e
            anchor_touched = any(c == self._R_ANCHOR_TK for c in all_closes)
            only_rogue = all(c == self._R_ROGUE_TK for c in all_closes)
            ok = (closes_d == [] and closes_e == [self._R_ROGUE_TK]
                  and not anchor_touched and only_rogue)
            detail = f"detect_closes={closes_d} eod_closes={closes_e} anchor_touched={anchor_touched}"
        except Exception as e:
            self._record(176, FAIL, f"raised: {e!r}"); return
        self._record(176, PASS if ok else FAIL, detail)

    # --- E-6 boost rides with parent (RALLY-only, flag boost_ride_with_parent) ------
    # Renumbered 177-182 (fix3's Rogue brakes took 171-176 on master). Drives the PURE
    # strategy._update_boost_on_bar + trails._resolve_parent_sl, no MT5. 2026-06-30 A1:
    # RALLY boost SELL entry 4004.61, just armed at +$5 -> own breath/current_sl = +$3
    # floor = 4001.61; parent anchor SELL still riding with a looser (higher) stop 4003.
    def _b6_boost(self, side='SELL', entry=4004.61, max_fav=3999.61, current_sl=4001.61,
                  parent_sl=4003.0, kind='RALLY'):
        import strategy as _S
        return _S.Position(anchor_label='A1', side=side, entry_price=entry, entry_time=None,
                           current_sl=current_sl, tp_level=(entry - 200 if side == 'SELL' else entry + 200),
                           max_fav=max_fav, lot=0.35, boost=True, boost_kind=kind,
                           parent_sl=parent_sl)

    def _b6_step(self, cfg, b, hi):
        import strategy as _S
        import pandas as _pd
        bar = _pd.Series({'open': 3999.61, 'high': hi, 'low': 3999.61, 'close': hi})
        return _S._update_boost_on_bar(b, bar, _pd.Timestamp('2026-06-30T07:00:00Z'), cfg)

    def _b6_cfg(self, on):
        import dataclasses
        return dataclasses.replace(self.cfg, boost_ride_with_parent=bool(on))

    def _step_b1_boost_rides_parent(self):
        # 177 (T-B1): flag ON, parent still riding (stop 4003 looser than the boost's own
        # +$3 floor 4001.61) -> the armed boost does NOT exit on the 4001.61 bounce; its
        # stop tracks the parent (4003). It rides instead of bailing at +$105.
        try:
            b = self._b6_boost(parent_sl=4003.0)
            self._b6_step(self._b6_cfg(True), b, 4001.61)
            ok = (not b.closed) and abs(b.current_sl - 4003.0) < 1e-6
            detail = f"closed={b.closed} (expect False) boost_sl={round(b.current_sl,2)} tracks parent 4003"
        except Exception as e:
            self._record(177, FAIL, f"raised: {e!r}"); return
        self._record(177, PASS if ok else FAIL, detail)

    def _step_b2_boost_parent_closed(self):
        # 178 (T-B2): flag ON but parent already closed (parent_sl None) -> the boost falls
        # back to its OWN trail and exits ~4001.61, no crash (edge case #2).
        try:
            b = self._b6_boost(parent_sl=None)
            self._b6_step(self._b6_cfg(True), b, 4001.61)
            ok = b.closed and abs(b.exit_price - 4001.61) < 1e-6
            detail = f"closed={b.closed} exit={round(b.exit_price,2) if b.exit_price is not None else None} (own trail fallback)"
        except Exception as e:
            self._record(178, FAIL, f"raised: {e!r}"); return
        self._record(178, PASS if ok else FAIL, detail)

    def _step_b3_boost_ride_off_identical(self):
        # 179 (T-B3): flag OFF -> byte-identical to today: the boost floors out at 4001.61
        # (+$105 @ 0.35x100=35/$) exactly as before, proving the flag isolates the change.
        try:
            b = self._b6_boost(parent_sl=4003.0)
            self._b6_step(self._b6_cfg(False), b, 4001.61)
            pnl = (4004.61 - b.exit_price) * 35.0
            ok = b.closed and abs(b.exit_price - 4001.61) < 1e-6 and abs(pnl - 105.0) < 0.6
            detail = f"closed={b.closed} exit={round(b.exit_price,2)} pnl=${round(pnl,2)} (expect +105, unchanged)"
        except Exception as e:
            self._record(179, FAIL, f"raised: {e!r}"); return
        self._record(179, PASS if ok else FAIL, detail)

    def _step_b4_boost_isolation(self):
        # 180 (T-B4): isolation. trails._resolve_parent_sl only READS the parent's
        # current_sl; it never mutates the parent shadow and never closes it. Missing parent
        # -> None (no crash); flag OFF -> None (no resolve). The boost carries only a float
        # parent_sl, never a reference to the parent dict -> no cross-magic close path.
        import trails as _T, types, copy
        try:
            parent = {'side': 'SELL', 'current_sl': 3997.27, 'anchor_label': 'A1'}
            parent_before = copy.deepcopy(parent)
            on = types.SimpleNamespace(cfg=self._b6_cfg(True),
                                       shadow_positions={555: parent},
                                       _rl_ok=lambda *a, **k: True)
            sh = {'boost': True, 'parent_ticket': 555, 'anchor_label': 'A1', 'boost_event': 'e1'}
            psl = _T._resolve_parent_sl(on, sh)
            reads_ok = abs(psl - 3997.27) < 1e-6
            parent_unmutated = (parent == parent_before)
            none_missing = _T._resolve_parent_sl(on, {'boost': True, 'parent_ticket': 999,
                                                      'boost_event': 'e2'}) is None
            off = types.SimpleNamespace(cfg=self._b6_cfg(False),
                                        shadow_positions={555: parent},
                                        _rl_ok=lambda *a, **k: True)
            none_off = _T._resolve_parent_sl(off, sh) is None
            float_only = isinstance(psl, float)   # boost gets a value, not the parent object
            ok = reads_ok and parent_unmutated and none_missing and none_off and float_only
            detail = (f"read={reads_ok} parent_unmutated={parent_unmutated} "
                      f"none_missing={none_missing} none_off={none_off} float_only={float_only}")
        except Exception as e:
            self._record(180, FAIL, f"raised: {e!r}"); return
        self._record(180, PASS if ok else FAIL, detail)

    def _step_b5_boost_rescue_unaffected(self):
        # 181 (T-B5): RESCUE boosts are unaffected (RALLY-only change). A RESCUE boost with
        # the same numbers + flag ON still exits on its own trail (the gate is is_rally).
        try:
            b = self._b6_boost(parent_sl=4003.0, kind='RESCUE')
            self._b6_step(self._b6_cfg(True), b, 4001.61)
            ok = b.closed   # RESCUE ignores the parent ride; closes as before
            detail = f"closed={b.closed} (expect True; RESCUE ignores ride-with-parent)"
        except Exception as e:
            self._record(181, FAIL, f"raised: {e!r}"); return
        self._record(181, PASS if ok else FAIL, detail)

    def _step_b6_boost_a1_replay(self):
        # 182 (T-B6): replay the 2026-06-30 A1 event. OFF -> both boosts floor at 4001.61
        # (+$105 each). ON -> neither boost exits on the 4001.61 bounce (rides with the
        # parent anchor that went on to 3997.27). This is the +105-vs-rides divergence.
        try:
            b_off = self._b6_boost(parent_sl=4003.0)
            self._b6_step(self._b6_cfg(False), b_off, 4001.61)
            b_on = self._b6_boost(parent_sl=4003.0)
            self._b6_step(self._b6_cfg(True), b_on, 4001.61)
            pnl_off = (4004.61 - b_off.exit_price) * 35.0
            ok = (b_off.closed and abs(pnl_off - 105.0) < 0.6 and not b_on.closed)
            detail = (f"OFF: closed={b_off.closed} +${round(pnl_off,2)} | "
                      f"ON: closed={b_on.closed} (rides past 4001.61 with parent)")
        except Exception as e:
            self._record(182, FAIL, f"raised: {e!r}"); return
        self._record(182, PASS if ok else FAIL, detail)

    def _step_e5_rogue_stop_525(self):
        # 183 E-5: rogue_daily_loss_stop -150 -> -525. Ladder: at -$175 (one init-SL strike)
        # Rogue still trades, at -$350 (two) still trades, at -$525 (three) it HALTS
        # (reason=daily_loss_stop). And the 3-consecutive-fail PAUSE can now fire BEFORE the
        # loss stop (3 small fakeouts of -$5 = -$15, far above -$525) -- which was impossible
        # at the old -$150 (a single -$175 strike halted first).
        import rogue as _rogue
        try:
            # ladder on init-SL strikes (-$175 each)
            gov = _rogue.new_day_state()
            _rogue.record_close(gov, -175.0, was_fail=True, cfg=self.cfg)
            at_175 = _rogue.can_enter(gov, self.cfg)
            _rogue.record_close(gov, -175.0, was_fail=True, cfg=self.cfg)
            at_350 = _rogue.can_enter(gov, self.cfg)
            _rogue.record_close(gov, -175.0, was_fail=True, cfg=self.cfg)   # -525
            at_525_blocked, at_525_reason = _rogue.can_enter(gov, self.cfg)
            # NOTE: at exactly 3 strikes both brakes trip; 'daily_loss_stop' OR
            # 'consecutive_fail_pause' are both valid HALTS -- assert it halted.
            ladder_ok = (at_175[0] is True and at_350[0] is True
                         and at_525_blocked is False
                         and at_525_reason in ('daily_loss_stop', 'consecutive_fail_pause')
                         and gov['loss_stopped'] is True)
            # fail-pause fires BEFORE the loss stop on SMALL fakeouts (now reachable).
            g2 = _rogue.new_day_state()
            for _ in range(3):
                _rogue.record_close(g2, -5.0, was_fail=True, cfg=self.cfg)   # -15 total
            paused_blocked, paused_reason = _rogue.can_enter(g2, self.cfg)
            fail_before_loss = (g2['fail_paused'] is True and g2['loss_stopped'] is False
                                and paused_blocked is False
                                and paused_reason == 'consecutive_fail_pause')
            # the config value itself
            stop_val = (abs(float(getattr(self.cfg, 'rogue_daily_loss_stop')) + 525.0) < 1e-9)
            ok = ladder_ok and fail_before_loss and stop_val
            detail = (f"-175_ok={at_175[0]} -350_ok={at_350[0]} -525_halts={not at_525_blocked}"
                      f"({at_525_reason}) fail_pause_before_loss={fail_before_loss} "
                      f"stop=-525={stop_val}")
        except Exception as e:
            self._record(183, FAIL, f"raised: {e!r}"); return
        self._record(183, PASS if ok else FAIL, detail)

    def _step_fix4_rogue_a1(self):
        # 185 Fix 4: Rogue A1-anchored redesign (NEW ENGINE, flag-gated DEFAULT OFF).
        # PURE cores + gating + isolation:
        #  (a) OFF -> drive() does NOT call _drive_a1 (legacy byte-identical); ON -> it does.
        #  (b) anchor SEEDS from A1 when no prior close; CHAINS to last close otherwise.
        #  (c) entry fires at exactly $10 off the anchor in the correct direction; <$10 holds.
        #  (d) reversal: $10 PAST entry against the trade -> confirmed; <$10 -> holds (pause).
        #  (f) whipsaw day: braked -> can_enter halts at rogue_daily_loss_stop (value-agnostic).
        #  (h) isolation: the A1 read is READ-ONLY; a reversal closes ONLY the ROGUE ticket,
        #      never an anchor 20260522 ticket.
        import rogue as _r, dataclasses, types
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True,
                                      rogue_enabled=True,   # so should_run passes the gate
                                      rogue_entry_confirm_redesign=10.0,
                                      rogue_reversal_dollars=10.0,
                                      rogue_daily_soft_lock=30.0,
                                      rogue_rescue_cap_dollars=13.0)
            # (b) seed / chain
            seed_a1 = (_r.a1_seed_anchor(None, 4000.0) == 4000.0)
            chain = (_r.a1_seed_anchor(4050.0, 4000.0) == 4050.0)
            # (c) entry
            e_buy = _r.a1_entry_decision(4000.0, 4010.0, cfg)   # +$10 -> BUY
            e_sell = _r.a1_entry_decision(4000.0, 3990.0, cfg)  # -$10 -> SELL
            e_hold = _r.a1_entry_decision(4000.0, 4005.0, cfg)  # +$5 -> none
            entry_ok = (e_buy[:2] == (True, 'BUY') and abs(e_buy[2] - 4010.0) < 1e-9
                        and abs(e_buy[3] - 4005.0) < 1e-9
                        and e_sell[:2] == (True, 'SELL') and abs(e_sell[3] - 3995.0) < 1e-9
                        and e_hold[0] is False)
            # (d) reversal
            rev_buy = _r.a1_reversal_confirmed(4010.0, 'BUY', 4000.0, cfg)   # -$10 -> True
            hold_buy = _r.a1_reversal_confirmed(4010.0, 'BUY', 4004.0, cfg)  # -$6 -> False
            rev_sell = _r.a1_reversal_confirmed(3990.0, 'SELL', 4000.0, cfg)  # +$10 -> True
            reversal_ok = (rev_buy is True and hold_buy is False and rev_sell is True)
            # (f) brake: a losing day halts at the daily loss stop (value-agnostic)
            g2 = _r.new_day_state()
            _r.record_close(g2, float(cfg.rogue_daily_loss_stop) - 1.0, True, cfg)
            braked = (_r.can_enter(g2, cfg)[0] is False
                      and _r.can_enter(g2, cfg)[1] == 'daily_loss_stop')

            # (a) gating: drive() routes to _drive_a1 ONLY when the flag is ON.
            sentinel = {'a1': 0}
            orig = _r._drive_a1
            _r._drive_a1 = lambda tr, st: sentinel.__setitem__('a1', sentinel['a1'] + 1)
            def _mk(a1_on):
                mt5 = types.SimpleNamespace(
                    ACCOUNT_TRADE_MODE_DEMO=0,
                    account_info=lambda: types.SimpleNamespace(trade_mode=0),
                    symbol_info_tick=lambda s=None: types.SimpleNamespace(bid=4000.0, ask=4000.2),
                    positions_get=lambda ticket=None: [])
                ad = types.SimpleNamespace(mt5=mt5, get_latest_m5=lambda s, n: [],
                                           close_position=lambda *a, **k: None)
                c = dataclasses.replace(cfg, rogue_a1_anchor_mode=a1_on)
                return types.SimpleNamespace(cfg=c, adapter=ad, paper=True,
                                             state={'last_broker_date': '2026-06-29'},
                                             _rogue=None, _last_boost_mid=4000.0,
                                             tele=types.SimpleNamespace(info=lambda *a, **k: None))
            try:
                _r.drive(_mk(False)); off_legacy = (sentinel['a1'] == 0)
                _r.drive(_mk(True));  on_a1 = (sentinel['a1'] == 1)
            finally:
                _r._drive_a1 = orig
            gating_ok = off_legacy and on_a1

            # (h) isolation: a reversal closes ONLY the ROGUE ticket; A1 read is read-only.
            closed = []
            mt5 = types.SimpleNamespace(
                ACCOUNT_TRADE_MODE_DEMO=0,
                account_info=lambda: types.SimpleNamespace(trade_mode=0),
                symbol_info_tick=lambda s=None: types.SimpleNamespace(bid=3998.0, ask=3998.2),
                positions_get=lambda ticket=None: [],   # rogue ticket already gone
                history_deals_get=lambda position=None: [
                    types.SimpleNamespace(entry=1, profit=-175.0, swap=0.0, commission=0.0)])
            ad = types.SimpleNamespace(
                mt5=mt5, get_latest_m5=lambda s, n: [],
                close_position=lambda tk, dry_run=False: closed.append(int(tk)))
            tr = types.SimpleNamespace(
                cfg=cfg, adapter=ad, paper=True, state={'last_broker_date': '2026-06-29'},
                _last_boost_mid=4000.0,
                tele=types.SimpleNamespace(info=lambda *a, **k: None),
                # an ANCHOR leg (magic 20260522) is in the book; it must NOT be touched.
                shadow_positions={55: {'anchor_label': 'A1', 'leg_fill_price': 3980.0,
                                       'magic': 20260522}})
            tr._rogue = {'day': '2026-06-29', 'gov': _r.new_day_state(),
                         'anchor': 4010.0, 'leg_dir': 'BUY', 'open':
                         {'ticket': 99, 'side': 'BUY', 'entry': 4010.0, 'sl': 4005.0,
                          'peak': 4010.0, 'magic': _r.ROGUE_MAGIC,
                          'leg_type': _r.ROGUE_LEG_TYPE}}
            a1_read = _r._a1_anchor_price(tr)                    # read-only A1 price
            _r._drive_a1(tr, tr._rogue)                          # price 4000 = -$10 -> reversal
            iso_ok = (closed == [99]                             # ONLY the rogue ticket
                      and 55 not in closed                       # NEVER the anchor 20260522
                      and abs((a1_read or 0) - 3980.0) < 1e-9    # A1 read worked, read-only
                      and tr.shadow_positions[55]['leg_fill_price'] == 3980.0)  # untouched

            ok = (seed_a1 and chain and entry_ok and reversal_ok and braked
                  and gating_ok and iso_ok)
            detail = (f"(a)gate_off={off_legacy}/on={on_a1} (b)seed={seed_a1}&chain={chain} "
                      f"(c)entry={entry_ok} (d)reversal={reversal_ok} "
                      f"(f)brake={braked} (h)isolation={iso_ok}")
        except Exception as e:
            self._record(185, FAIL, f"raised: {e!r}"); return
        self._record(185, PASS if ok else FAIL, detail)

    def _step_fb_trapped_late_rescue(self):
        # 184 F-B: trapped-leg CAPPED late-rescue (No-OCO whipsaw), flag-gated DEFAULT OFF.
        # (a) flag OFF -> plan is None (byte-identical: the trapped loser rides to its full
        # -$18 SL, no late hedge); (b) flag ON + >= arm adverse -> a capped hedge OPPOSITE
        # the trapped leg with its OWN $13 SL; (c) flag ON but < arm adverse -> None (not yet
        # armed); (d) a SELL trapped leg arms a BUY hedge; (e) the reverse-whipsaw is BOUNDED
        # -- combined worst case = n x $13 x lot x 100 (trapped_rescue_cap), finite, never the
        # naked unbounded double-loss. Pure decision (boosts.plan_trapped_late_rescue) -- the
        # live fills.py hook is a thin wrapper gated by the SAME flag, so flag OFF is a no-op.
        # D-5 (2026-07-03): the flag now DEFAULTS ON, so "flag OFF" is forced explicitly here
        # (both states are still proven; 187 asserts the new default separately).
        import boosts as _b, dataclasses
        try:
            cfg_off = dataclasses.replace(self.cfg, trapped_late_rescue_enabled=False)
            cfg_on = dataclasses.replace(self.cfg, trapped_late_rescue_enabled=True,
                                         trapped_rescue_arm_dollars=10.0,
                                         trapped_rescue_sl_dollars=13.0)
            # trapped losing BUY leg: fill 4065.64 -> price slides to 4055.64 (-$10 adverse)
            off = _b.plan_trapped_late_rescue('BUY', 4065.64, 4055.64, cfg_off)
            on = _b.plan_trapped_late_rescue('BUY', 4065.64, 4055.64, cfg_on)
            early = _b.plan_trapped_late_rescue('BUY', 4065.64, 4059.64, cfg_on)  # -$6 < arm
            on_sell = _b.plan_trapped_late_rescue('SELL', 4000.0, 4010.0, cfg_on)  # -$10
            cap = _b.trapped_rescue_cap(cfg_on)
            expected_cap = int(getattr(cfg_on, 'rescue_boost_count', 2)) * 13.0 \
                * float(cfg_on.lot_size) * 100.0

            flag_off_none = (off is None)                       # (a) byte-identical
            armed = (on is not None and on.boost_side == 'SELL'
                     and abs(on.sl_dollars - 13.0) < 1e-9       # (b) own $13 SL
                     and on.kind == 'RESCUE'
                     and on.event_type == 'TRAPPED_LATE_RESCUE')
            not_early = (early is None)                         # (c) not yet armed
            sell_arms_buy = (on_sell is not None and on_sell.boost_side == 'BUY')  # (d)
            # (e) reverse-whipsaw bounded: the hedge's own-SL worst case <= the cap (finite)
            bounded = (cap > 0 and abs(cap - expected_cap) < 1e-6 and on is not None
                       and (on.n * on.sl_dollars * float(cfg_on.lot_size) * 100.0)
                       <= cap + 1e-6)
            ok = flag_off_none and armed and not_early and sell_arms_buy and bounded
            detail = (f"(a)OFF_none={flag_off_none} (b)armed_SELL_sl13={armed} "
                      f"(c)not_armed<arm={not_early} (d)SELL->BUY={sell_arms_buy} "
                      f"(e)cap=${cap:.0f}_bounded={bounded}")
        except Exception as e:
            self._record(184, FAIL, f"raised: {e!r}"); return
        self._record(184, PASS if ok else FAIL, detail)

    def _step_selftest_summary(self):
        # 186 selftest auto-summary reporter v2 (report-only; NO trading/test behavior change).
        # KNOWN mix: 5 tests, 2 forced-FAIL (steps 2 + 4) + a NEGATIVE test (step 3) whose
        # DETAIL contains an ERROR/violation string but is RECORDED PASS -> counts as PASS
        # (keyed on STATUS, never grep 'ERROR'). Asserts: Total=5 Passed=3 Failed=2; the
        # results table lists BOTH FAIL rows FIRST (step order) then the 3 PASS; a forced
        # write-error is caught NON-FATALLY (returns False, no raise); the .md is utf-8 with
        # ASCII PASS/FAIL (no glyphs).
        import selftest as _st, tempfile, os
        tmp = None
        try:
            results = {
                1: (PASS, 'ok one'),
                2: (FAIL, 'boom two'),
                3: (PASS, 'DO-NOT-START + TELEMETRY_VIOLATION ERROR logged (negative test)'),
                4: (FAIL, 'boom four'),
                5: (PASS, 'ok five'),
            }
            names = {1: 'alpha', 2: 'beta', 3: 'neg fail-open', 4: 'delta', 5: 'epsilon'}
            s = _st.build_selftest_summary(results, names)
            counts_ok = (s['total'] == 5 and s['passed'] == 3 and s['failed'] == 2)
            # FAILED FIRST (step order 2,4) then PASSED (step order 1,3,5)
            order = [step for (step, _n, _st2, _d) in s['rows']]
            statuses = [st2 for (_step, _n, st2, _d) in s['rows']]
            sort_ok = (order == [2, 4, 1, 3, 5]
                       and statuses[:2] == [FAIL, FAIL]
                       and set(statuses[2:]) == {PASS})
            # negative test (ERROR in detail) is PASS, never in failed_list
            failed_steps = [step for (step, _n, _d) in s['failed_list']]
            neg_is_pass = (3 not in failed_steps and failed_steps == [2, 4])

            meta = {'build': 5958, 'ts': '2026-07-01 01:00:00', 'account': 5051188745,
                    'server': 'Demo', 'watchdog': 'SAFE-TO-START', 'rogue': 'PROMOTED ON (demo)'}
            # FILE render is ASCII (no glyphs); the two FAIL rows appear before any PASS row.
            md = _st.render_summary(s, meta, emoji=False)
            header_ok = ('AUREON SELFTEST SUMMARY' in md and 'Result: FAIL' in md
                         and 'Total: 5   Passed: 3   Failed: 2' in md)
            i_fail2 = md.index('| beta |')
            i_fail4 = md.index('| delta |')
            i_pass1 = md.index('| alpha |')
            failed_first = (i_fail2 < i_pass1 and i_fail4 < i_pass1)
            ascii_only = ('PASS' in md and 'FAIL' in md
                          and '✅' not in md and '❌' not in md and '⚠' not in md)

            # write the file utf-8 to a tempdir + read it back as utf-8; assert ASCII body.
            tmp = tempfile.mkdtemp(prefix='aureon_report_')
            good_path = os.path.join(tmp, 'logs', 'selftest_report.md')
            wrote = _st.write_selftest_report(md, good_path)
            with open(good_path, encoding='utf-8') as f:
                body = f.read()
            file_ok = (wrote is True and 'Total: 5   Passed: 3   Failed: 2' in body
                       and '| beta |' in body and '✅' not in body
                       and body.encode('ascii', 'strict'))   # raises if non-ASCII slipped in

            # JOB 1: a forced write-ERROR is caught NON-FATALLY (returns False, never raises).
            blocker = os.path.join(tmp, 'blocker')
            with open(blocker, 'w') as f:
                f.write('x')                          # a FILE where a dir is needed
            bad_path = os.path.join(blocker, 'sub', 'selftest_report.md')
            write_failed_safely = (_st.write_selftest_report(md, bad_path) is False)

            # the 0-failed branch prints the "No failures" line above the table.
            s_all_pass = _st.build_selftest_summary({1: (PASS, 'a'), 2: (PASS, 'b')},
                                                    {1: 'a', 2: 'b'})
            nofail_line = ('No failures -- all 2 passed.'
                           in _st.render_summary(s_all_pass, meta, emoji=False))

            ok = (counts_ok and sort_ok and neg_is_pass and header_ok and failed_first
                  and ascii_only and file_ok and write_failed_safely and nofail_line)
            detail = (f"Total=5 Passed={s['passed']} Failed={s['failed']} "
                      f"failed_first={failed_first} order={order} neg_PASS={neg_is_pass} "
                      f"ascii_file={bool(file_ok)} write_error_caught={write_failed_safely}")
        except Exception as e:
            self._record(186, FAIL, f"raised: {e!r}")
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            return
        finally:
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
        self._record(186, PASS if ok else FAIL, detail)

    def _step_rogue_manual_seed(self):
        # 187 Rogue manual current-tick seed command (rogueseed). Mid-day restart has no A1
        # event to seed Fix 4, so this plants the anchor at the current tick on demand.
        #  (a) DEMO + a1-mode ON -> plants the anchor at the given tick (st['a1_last_close']).
        #  (b) from that seed a $10 move triggers the EXISTING Fix 4 entry (ROGUE magic).
        #  (c) DEMO-only gate: a FUNDED account refuses (fail-closed), no seed planted.
        #  (d) a1-mode OFF -> refuses with 'disabled' (tells the user to enable it).
        #  (e) isolation: never closes/touches an anchor 20260522 ticket.
        #  (f) the CLI enqueue writes a 'rogueseed' command onto the command channel.
        import rogue as _r, dataclasses, types, os, json, tempfile
        tmp = None
        try:
            def _mk(demo, a1_on, mid=4000.0):
                placed = []
                closed = []
                mt5 = types.SimpleNamespace(
                    ACCOUNT_TRADE_MODE_DEMO=0,
                    account_info=lambda: types.SimpleNamespace(trade_mode=(0 if demo else 2)),
                    symbol_info_tick=lambda s=None: types.SimpleNamespace(
                        bid=mid - 0.1, ask=mid + 0.1),
                    positions_get=lambda ticket=None: [],
                    history_deals_get=lambda position=None: [])
                def _place(symbol, side, lot, sl=None, tp=None, magic=None,
                           comment=None, dry_run=False):
                    placed.append({'side': side, 'magic': magic})
                    return types.SimpleNamespace(retcode=10009, order=7001, deal=7001)
                ad = types.SimpleNamespace(
                    mt5=mt5, get_latest_m5=lambda s, n: [], place_market_order=_place,
                    modify_position_sl=lambda *a, **k: types.SimpleNamespace(retcode=10009),
                    close_position=lambda tk, dry_run=False: closed.append(int(tk)))
                cfg = dataclasses.replace(
                    self.cfg, rogue_enabled=True, rogue_a1_anchor_mode=a1_on,
                    rogue_daywatch=True, rogue_entry_confirm_redesign=10.0,
                    rogue_reversal_dollars=10.0, lot_size=0.01)
                tr = types.SimpleNamespace(
                    cfg=cfg, adapter=ad, paper=True, _rogue=None, _last_boost_mid=mid,
                    state={'last_broker_date': '2026-06-29'},
                    tele=types.SimpleNamespace(info=lambda *a, **k: None,
                                               warn=lambda *a, **k: None),
                    shadow_positions={55: {'anchor_label': 'A1', 'leg_fill_price': 3980.0,
                                           'magic': 20260522}})   # an ANCHOR leg present
                return tr, placed, closed

            # gate (pure)
            g_ok = (_r.manual_seed_ok(_mk(True, True)[0].cfg, True) == (True, 'ok'))
            g_funded = (_r.manual_seed_ok(_mk(False, True)[0].cfg, False)[1] == 'funded')
            g_disabled = (_r.manual_seed_ok(_mk(True, False)[0].cfg, True)[1] == 'disabled')

            # (a) DEMO + a1-mode ON -> plants anchor at 4000
            tr, placed, closed = _mk(True, True, mid=4000.0)
            ok_seed, reason, seeded = _r.manual_seed(tr, 4000.0)
            planted = (ok_seed is True and reason == 'ok' and abs(seeded - 4000.0) < 1e-9
                       and abs(tr._rogue['a1_last_close'] - 4000.0) < 1e-9)

            # (b) a $10 move off the seed -> EXISTING Fix 4 engine enters (ROGUE magic)
            tr.adapter.mt5.symbol_info_tick = lambda s=None: types.SimpleNamespace(
                bid=4009.9, ask=4010.1)   # mid 4010 = +$10 off the 4000 seed
            tr._last_boost_mid = 4010.0
            _r.drive(tr)
            entered = (len(placed) == 1 and placed[0]['side'] == 'BUY'
                       and placed[0]['magic'] == _r.ROGUE_MAGIC == 20260626)

            # (c) FUNDED refuses, no seed planted
            trf, placedf, closedf = _mk(False, True, mid=4000.0)
            okf, reasonf, seededf = _r.manual_seed(trf, 4000.0)
            funded_refused = (okf is False and reasonf == 'funded' and seededf is None
                              and (trf._rogue is None
                                   or trf._rogue.get('a1_last_close') is None))

            # (d) a1-mode OFF refuses with 'disabled'
            trd, _pd, _cd = _mk(True, False, mid=4000.0)
            okd, reasond, _ = _r.manual_seed(trd, 4000.0)
            disabled_refused = (okd is False and reasond == 'disabled')

            # (e) isolation: the anchor 20260522 ticket #55 was NEVER closed/touched
            iso_ok = (55 not in closed and tr.shadow_positions[55]['magic'] == 20260522
                      and tr.shadow_positions[55]['leg_fill_price'] == 3980.0)

            # (f) CLI enqueue writes a rogueseed command to AUREON_RUN_DIR/commands.json
            tmp = tempfile.mkdtemp(prefix='aureon_seedcmd_')
            _prev = os.environ.get('AUREON_RUN_DIR')
            os.environ['AUREON_RUN_DIR'] = tmp
            try:
                rc = _r.enqueue_seed_command(tr.cfg)
                with open(os.path.join(tmp, 'commands.json')) as f:
                    cmds = json.load(f)
            finally:
                if _prev is None:
                    os.environ.pop('AUREON_RUN_DIR', None)
                else:
                    os.environ['AUREON_RUN_DIR'] = _prev
            cli_ok = (rc == 0 and any(c.get('cmd') == 'rogueseed' for c in cmds))

            ok = (g_ok and g_funded and g_disabled and planted and entered
                  and funded_refused and disabled_refused and iso_ok and cli_ok)
            detail = (f"(a)planted={planted} (b)entry_ROGUE={entered} (c)funded_refused="
                      f"{funded_refused} (d)disabled_refused={disabled_refused} "
                      f"(e)isolation={iso_ok} (f)cli_enqueue={cli_ok} "
                      f"gates(ok/funded/disabled)={g_ok}/{g_funded}/{g_disabled}")
        except Exception as e:
            self._record(187, FAIL, f"raised: {e!r}")
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            return
        finally:
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
        self._record(187, PASS if ok else FAIL, detail)

    def _step_rogueseed_consume(self):
        # 188 the RUNNING loop consumes a queued rogueseed (bugfix). Drives the REAL bound
        # LiveTrader._handle_commands / _consume_commands against a commands.json holding
        # [{"cmd":"rogueseed"}]:
        #  (a) the loop CONSUMES it -> plants the anchor at the current tick + logs MANUAL SEED;
        #  (b) the command is REMOVED from commands.json (cleared to []);
        #  (c) a SECOND loop does NOT replant (idempotent -- fires exactly once);
        #  (d) a FUNDED account refuses (no seed planted); and
        #  (e) isolation: the anchor 20260522 ticket is never touched.
        import live_trader as _lt, rogue as _r, dataclasses, types, os, json, tempfile
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix='aureon_seedconsume_')

            def _mk(demo, mid=4100.0):
                closed = []
                mt5 = types.SimpleNamespace(
                    ACCOUNT_TRADE_MODE_DEMO=0,
                    account_info=lambda: types.SimpleNamespace(trade_mode=(0 if demo else 2)),
                    symbol_info_tick=lambda s=None: types.SimpleNamespace(
                        bid=mid - 0.1, ask=mid + 0.1))
                ad = types.SimpleNamespace(
                    mt5=mt5, close_position=lambda tk, dry_run=False: closed.append(int(tk)))
                cfg = dataclasses.replace(self.cfg, rogue_enabled=True,
                                          rogue_a1_anchor_mode=True)
                cpath = os.path.join(tmp, f"commands_{'demo' if demo else 'funded'}.json")
                json.dump([{"cmd": "rogueseed"}], open(cpath, "w"))   # launcher queued it
                stub = types.SimpleNamespace(
                    commands_path=cpath, adapter=ad, cfg=cfg, paper=True, _rogue=None,
                    state={'last_broker_date': '2026-06-29'},
                    tele=types.SimpleNamespace(info=lambda *a, **k: None,
                                               warn=lambda *a, **k: None),
                    shadow_positions={55: {'anchor_label': 'A1', 'magic': 20260522,
                                           'leg_fill_price': 3980.0}})
                # bind the REAL production consumer methods
                stub._consume_commands = types.MethodType(
                    _lt.LiveTrader._consume_commands, stub)
                stub._handle_commands = types.MethodType(
                    _lt.LiveTrader._handle_commands, stub)
                return stub, cpath, closed

            # DEMO: consume -> plant -> clear -> idempotent
            tr, cpath, closed = _mk(True)
            tr._handle_commands()                                   # (a) the per-tick poll
            planted = (tr._rogue is not None
                       and abs(tr._rogue.get('a1_last_close', 0) - 4100.0) < 1e-9)
            cleared = (json.load(open(cpath)) == [])                # (b) command removed
            # (c) idempotent: a second loop must NOT replant (file already empty)
            tr._rogue['a1_last_close'] = None
            tr._handle_commands()
            idempotent = (tr._rogue.get('a1_last_close') is None)

            # (d) FUNDED refuses -> no seed planted, but the command is still consumed once
            trf, cpathf, _cf = _mk(False)
            trf._handle_commands()
            funded_refused = (trf._rogue is None
                              or trf._rogue.get('a1_last_close') is None)

            # (e) isolation: the anchor 20260522 ticket #55 was never closed
            iso_ok = (55 not in closed and tr.shadow_positions[55]['magic'] == 20260522)

            ok = planted and cleared and idempotent and funded_refused and iso_ok
            detail = (f"(a)consumed+planted={planted} (b)file_cleared={cleared} "
                      f"(c)idempotent_no_replant={idempotent} (d)funded_refused={funded_refused} "
                      f"(e)isolation={iso_ok}")
        except Exception as e:
            self._record(188, FAIL, f"raised: {e!r}")
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            return
        finally:
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
        self._record(188, PASS if ok else FAIL, detail)

    def _step_r7_rogue_e3_chain(self):
        # 189 (E-3 CHAIN): on ANY Rogue close the A1 redesign clears st['open'], re-anchors at
        # the EXIT price, and keeps hunting -> a fresh $10 move fires a SECOND entry (proves the
        # chain, no dormancy). Verified for a TRAILING-profit close AND an init-SL close; brakes
        # still gate the next entry; the anchor 20260522 ticket is NEVER touched.
        import rogue as _r, dataclasses, types
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True,
                                      rogue_entry_confirm_redesign=10.0,
                                      rogue_reversal_dollars=10.0, rogue_daily_loss_stop=-525.0)

            def mk():
                # a controllable A1-mode trader: env['book'] = live broker positions;
                # env['deal'] = the close deal surfaced once the ticket leaves the book.
                env = {'price': None, 'book': {}, 'deal': None, 'orders': [], 'logs': []}
                def place_market_order(sym, side, lot, sl=None, tp=None, magic=None,
                                       comment=None, dry_run=False):
                    tk = 900000 + len(env['orders']) + 1
                    env['orders'].append({'ticket': tk, 'side': side})
                    env['book'][tk] = True
                    return types.SimpleNamespace(order=tk, deal=tk, retcode=10009)
                def positions_get(ticket=None):
                    return [object()] if env['book'].get(int(ticket)) else []
                def history_deals_get(position=None):
                    d = env['deal']
                    return ([types.SimpleNamespace(entry=1, profit=d['pnl'], price=d['price'],
                                                   swap=0.0, commission=0.0)]
                            if (d and int(position) == int(d['ticket'])) else [])
                def close_position(tk, dry_run=False):
                    env['book'].pop(int(tk), None)
                mt5 = types.SimpleNamespace(
                    positions_get=positions_get, history_deals_get=history_deals_get,
                    symbol_info_tick=lambda s=None: types.SimpleNamespace(
                        bid=env['price'], ask=env['price']),
                    account_info=lambda: types.SimpleNamespace(trade_mode=0),
                    ACCOUNT_TRADE_MODE_DEMO=0)
                ad = types.SimpleNamespace(
                    mt5=mt5, place_market_order=place_market_order,
                    modify_position_sl=lambda tk, sl: None, close_position=close_position)
                tr = types.SimpleNamespace(
                    cfg=cfg, paper=True, adapter=ad, _last_boost_mid=None,
                    state={'last_broker_date': '2026-07-01'},
                    tele=types.SimpleNamespace(
                        info=lambda *a, **k: env['logs'].append(a[0] if a else ''),
                        warn=lambda *a, **k: None))
                tr._rogue = {'day': '2026-07-01', 'gov': _r.new_day_state(), 'anchor': None,
                             'leg_dir': None, 'open': None, 'a1_last_close': None,
                             'a1_reverted': False}
                return tr, env

            def tick(tr, env, price, close=None):
                env['price'] = float(price)
                tr._last_boost_mid = float(price)
                if close is not None:            # broker closes the open ticket at (pnl, exit)
                    o = tr._rogue.get('open') or {}
                    tk = o.get('ticket')
                    if tk is not None:
                        env['book'].pop(int(tk), None)
                        env['deal'] = {'ticket': tk, 'pnl': close[0], 'price': close[1]}
                _r._drive_a1(tr, tr._rogue)

            # (A) TRAILING-profit close chains to a SECOND entry.
            trA, envA = mk()
            envA['book'][self._R_ANCHOR_TK] = True     # an anchor 20260522 leg is present
            _r.manual_seed(trA, 3984.0)                # seed (mid-day restart, no A1 event)
            tick(trA, envA, 3974.0)                    # -$10 -> ENTER SELL #1
            entered1 = (trA._rogue.get('open') is not None
                        and (trA._rogue['open'] or {}).get('side') == 'SELL')
            tick(trA, envA, 3950.0, close=(206.50, 3953.0))    # trailing-profit CLOSE
            closedA = trA._rogue.get('open') is None
            reanchoredA = abs((trA._rogue.get('a1_last_close') or 0) - 3953.0) < 1e-9
            dayA = abs(trA._rogue['gov']['day_pnl'] - 206.50) < 1e-9
            chain_logged = any('CHAIN re-anchor' in str(m) and 'hunting $10 both dirs' in str(m)
                               for m in envA['logs'])
            # P3 (E-17): the chained re-anchor now carries a cooldown; simulate it
            # elapsing (timing only -- every assertion below is unchanged).
            self._p3_warp(trA)
            tick(trA, envA, 3963.0)                    # +$10 off 3953 exit -> SECOND entry
            entered2 = (trA._rogue.get('open') is not None
                        and trA._rogue['gov']['reanchor_count'] == 2)
            iso_A = self._R_ANCHOR_TK in envA['book']  # the anchor ticket was never closed
            trailing_ok = (entered1 and closedA and reanchoredA and dayA and chain_logged
                           and entered2 and iso_A)

            # (B) init-SL close ALSO chains to a SECOND entry (fail streak advances, still trades).
            trB, envB = mk()
            _r.manual_seed(trB, 4000.0)
            tick(trB, envB, 4010.0)                    # +$10 -> ENTER BUY #1
            tick(trB, envB, 4005.0, close=(-5.0, 4005.0))      # init-SL fake-out CLOSE
            closedB = trB._rogue.get('open') is None
            reanchoredB = abs((trB._rogue.get('a1_last_close') or 0) - 4005.0) < 1e-9
            failB = trB._rogue['gov']['consec_fails'] == 1
            self._p3_warp(trB)                         # P3: cooldown elapses (timing only)
            tick(trB, envB, 4015.0)                    # +$10 off 4005 exit -> SECOND entry
            entered2B = (trB._rogue.get('open') is not None
                         and trB._rogue['gov']['reanchor_count'] == 2)
            sl_ok = (closedB and reanchoredB and failB and entered2B)

            # (C) brakes still gate: after a loss past the daily stop, the chain does NOT re-enter.
            trC, envC = mk()
            _r.manual_seed(trC, 4000.0)
            tick(trC, envC, 4008.0)                    # +$8 -> no entry yet (holds)
            tick(trC, envC, 4010.0)                    # +$10 -> ENTER BUY #1
            tick(trC, envC, 4002.0, close=(-600.0, 4002.0))    # catastrophic CLOSE (< -$525)
            stoppedC = trC._rogue['gov']['loss_stopped'] is True
            cntC = trC._rogue['gov']['reanchor_count']
            self._p3_warp(trC)                         # P3: cooldown elapses, so the BRAKE
            tick(trC, envC, 4012.0)                    # (not the cooldown) blocks this entry
            braked = (trC._rogue.get('open') is None
                      and trC._rogue['gov']['reanchor_count'] == cntC)
            brake_ok = stoppedC and braked

            ok = trailing_ok and sl_ok and brake_ok
            detail = (f"trailing[e1={entered1} closed={closedA} reanchor@3953={reanchoredA} "
                      f"day={dayA} chainlog={chain_logged} e2={entered2} iso={iso_A}] "
                      f"sl[reanchor@4005={reanchoredB} fail={failB} e2={entered2B}] "
                      f"brake[stopped={stoppedC} no_reentry={braked}]")
        except Exception as e:
            self._record(189, FAIL, f"raised: {e!r}"); return
        self._record(189, PASS if ok else FAIL, detail)

    # === P1 "never blind, never brick" — E-13..E-16 + E-12 ladder ==============
    def _step_fix1_rc_retry_brick(self):
        # 190 (Fix 1 / E-13): the SHARED place_with_retry classifies retcodes (retry vs abort,
        # NEVER resizing the lot), and Rogue's LIVE entry sets st['open'] + consumes ONE slot
        # ONLY on rc==10009 -- a final failure leaves NO phantom open and NO slot consumed
        # (the engine does not brick; it stays alive for the next signal).
        import mt5_adapter as _mad, rogue as _r, dataclasses, types, tempfile
        try:
            cls = _mad.classify_retcode
            classify_ok = (cls(10009) == 'done' and cls(10004) == 'refresh'
                           and cls(10015) == 'refresh' and cls(10021) == 'refresh'
                           and cls(10016) == 'stops' and cls(10008) == 'plain'
                           and cls(None) == 'plain' and cls(-1) == 'plain'
                           and cls(10014) == 'abort' and cls(10019) == 'abort'
                           and cls(10018) == 'abort' and cls(10017) == 'abort'
                           and cls(99999) == 'abort')

            class _FS:
                def __init__(s): s.aborts = 0
                def _alert_order_abort(s, *a, **k): s.aborts += 1
            fs = _FS()
            r1 = _mad.MT5Adapter.place_with_retry(
                fs, lambda a, rc: types.SimpleNamespace(retcode=10009, order=1),
                describe={'label': 't'}, tele=None, backoffs=(0, 0, 0))
            seq = [types.SimpleNamespace(retcode=10016, comment='x'),
                   types.SimpleNamespace(retcode=10009, order=2)]
            seen = []
            r2 = _mad.MT5Adapter.place_with_retry(
                fs, lambda a, rc: (seen.append(rc) or seq[a - 1]),
                describe={'label': 't'}, tele=None, backoffs=(0, 0, 0))
            fs2 = _FS()   # 10014 volume -> abort on the FIRST try (lot never resized)
            vcalls = []
            r3 = _mad.MT5Adapter.place_with_retry(
                fs2, lambda a, rc: (vcalls.append(a) or types.SimpleNamespace(retcode=10014, comment='v')),
                describe={'label': 't', 'lot': 0.35}, tele=None, backoffs=(0, 0, 0))
            fs3 = _FS()   # 10004 requote -> retries to exhaustion (3 calls) then abort-alert
            rcalls = []
            _mad.MT5Adapter.place_with_retry(
                fs3, lambda a, rc: (rcalls.append(a) or types.SimpleNamespace(retcode=10004, comment='rq')),
                describe={'label': 't'}, tele=None, max_attempts=3, backoffs=(0, 0, 0))
            pwr_ok = (r1.retcode == 10009 and r2.retcode == 10009 and seen == [False, True]
                      and r3.retcode == 10014 and len(vcalls) == 1 and fs2.aborts == 1
                      and len(rcalls) == 3 and fs3.aborts == 1)

            # BRICK fix on the LIVE Rogue entry path.
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True)

            def mk(fail_rc=None):
                env = {'orders': []}
                def pmo(sym, side, lot, sl=None, tp=None, magic=None, comment=None, dry_run=False):
                    if fail_rc is not None:
                        return types.SimpleNamespace(retcode=fail_rc, comment='rej', order=None, deal=None)
                    tk = 800000 + len(env['orders']) + 1; env['orders'].append(tk)
                    return types.SimpleNamespace(order=tk, deal=tk, retcode=10009)
                mt5 = types.SimpleNamespace(
                    symbol_info=lambda s=None: types.SimpleNamespace(point=0.01, trade_stops_level=0),
                    symbol_info_tick=lambda s=None: types.SimpleNamespace(bid=4000.0, ask=4000.0))
                ad = types.SimpleNamespace(mt5=mt5, place_market_order=pmo)
                ad.place_with_retry = _mad.MT5Adapter.place_with_retry.__get__(ad)
                ad._alert_order_abort = _mad.MT5Adapter._alert_order_abort.__get__(ad)
                tr = types.SimpleNamespace(
                    cfg=cfg, paper=False, adapter=ad, run_dir=tempfile.mkdtemp(),
                    state={'last_broker_date': '2026-07-02'},
                    tele=types.SimpleNamespace(info=lambda *a, **k: None,
                                               warn=lambda *a, **k: None,
                                               critical=lambda *a, **k: None))
                tr._rogue = {'day': '2026-07-02', 'gov': _r.new_day_state(), 'anchor': 4000.0,
                             'leg_dir': 'BUY', 'open': None, 'a1_last_close': 4000.0,
                             'a1_reverted': False}
                return tr
            trS = mk(); _r._place_rogue_entry(trS, trS._rogue, 4010.0, 4005.0)
            succ = (trS._rogue['open'] is not None and trS._rogue['gov']['reanchor_count'] == 1)
            trF = mk(fail_rc=10019); _r._place_rogue_entry(trF, trF._rogue, 4010.0, 4005.0)
            brick = (trF._rogue['open'] is None and trF._rogue['gov']['reanchor_count'] == 0)
            ok = classify_ok and pwr_ok and succ and brick
            detail = (f"classify={classify_ok} pwr[retry/abort/no-resize]={pwr_ok} "
                      f"live_success[open+1slot]={succ} final_fail[no_phantom+no_slot]={brick}")
        except Exception as e:
            self._record(190, FAIL, f"raised: {e!r}"); return
        self._record(190, PASS if ok else FAIL, detail)

    def _step_fix2_pnl_unresolved(self):
        # 191 (Fix 2 / E-14): a close whose P&L is None retries the history fetch; if STILL
        # unresolved it books $0 but does NOT increment consec_fails (was_fail=None) and logs
        # a WARN -- an unresolvable P&L can never trip the fail-pause. A resolved close books
        # normally. _resolve_close_pnl returns the first non-None and stops.
        import rogue as _r, dataclasses, types
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True)
            # (a) record_close None-sentinel: streak untouched, P&L booked.
            g = _r.new_day_state(); g['consec_fails'] = 2
            _r.record_close(g, 0.0, None, cfg)
            sentinel_ok = (g['consec_fails'] == 2 and g['day_pnl'] == 0.0)
            # (b) _resolve_close_pnl: resolves on first hit (no wait); None after tries=1.
            hits = {'n': 0}
            class _Ad:
                class mt5:
                    @staticmethod
                    def history_deals_get(position=None):
                        hits['n'] += 1
                        return ([types.SimpleNamespace(entry=1, profit=7.0, swap=0, commission=0)]
                                if hits['n'] >= 1 else [])
            tr = types.SimpleNamespace(adapter=_Ad())
            resolved = _r._resolve_close_pnl(tr, 1, tries=3, delay=0)
            resolve_ok = (abs(resolved - 7.0) < 1e-9)
            # (c) detect_close unresolved branch (monkeypatch _resolve to None -> no real sleep).
            logs = []
            orig = _r._resolve_close_pnl
            _r._resolve_close_pnl = lambda *a, **k: None
            try:
                env = {'book': {}}   # ticket already gone -> closed at broker
                ad = types.SimpleNamespace(mt5=types.SimpleNamespace(
                    positions_get=lambda ticket=None: [],
                    history_deals_get=lambda position=None: []))
                trx = types.SimpleNamespace(
                    cfg=cfg, paper=True, adapter=ad, run_dir='.',
                    state={'last_broker_date': '2026-07-02'},
                    tele=types.SimpleNamespace(info=lambda *a, **k: None,
                                               warn=lambda *a, **k: logs.append(a[0] if a else '')))
                trx._rogue = {'day': '2026-07-02', 'gov': _r.new_day_state(), 'anchor': None,
                              'leg_dir': None, 'a1_reverted': False, 'a1_last_close': None,
                              'open': {'ticket': 5001, 'side': 'BUY', 'entry': 4000.0,
                                       'sl': 3995.0, 'peak': 4000.0, 'magic': _r.ROGUE_MAGIC,
                                       'leg_type': 'rogue'}}
                trx._rogue['gov']['consec_fails'] = 2
                booked = _r.detect_close(trx, trx._rogue)
            finally:
                _r._resolve_close_pnl = orig
            unresolved_ok = (booked is True and trx._rogue['open'] is None
                             and trx._rogue['gov']['consec_fails'] == 2      # NOT incremented
                             and trx._rogue['gov']['day_pnl'] == 0.0
                             and any('pnl-unresolved' in str(m) for m in logs))
            ok = sentinel_ok and resolve_ok and unresolved_ok
            detail = (f"sentinel[streak_untouched]={sentinel_ok} resolve_first_hit={resolve_ok} "
                      f"detect_close[book0+no_fail+warn]={unresolved_ok}")
        except Exception as e:
            self._record(191, FAIL, f"raised: {e!r}"); return
        self._record(191, PASS if ok else FAIL, detail)

    def _step_fix3_rogue_gated(self):
        # 192 (Fix 3 / E-15): with allow_new_entries=False (the post-EOD trail-only call) a
        # NEW Rogue entry is REFUSED while an EXISTING open position still trails; and
        # force_close_open (the kill-switch path) closes the Rogue ticket + books the governor.
        import rogue as _r, dataclasses, types
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True,
                                      rogue_entry_confirm_redesign=10.0)

            def mk():
                env = {'book': {}, 'orders': [], 'deal': None, 'price': None}
                def pmo(sym, side, lot, sl=None, tp=None, magic=None, comment=None, dry_run=False):
                    tk = 700000 + len(env['orders']) + 1; env['orders'].append(tk); env['book'][tk] = True
                    return types.SimpleNamespace(order=tk, deal=tk, retcode=10009)
                def hist(position=None):
                    d = env['deal']
                    return ([types.SimpleNamespace(entry=1, profit=d['pnl'], price=d['price'],
                                                   swap=0, commission=0)]
                            if (d and int(position) == int(d['ticket'])) else [])
                mt5 = types.SimpleNamespace(
                    positions_get=lambda ticket=None: [object()] if env['book'].get(int(ticket)) else [],
                    history_deals_get=hist,
                    symbol_info_tick=lambda s=None: types.SimpleNamespace(bid=env['price'], ask=env['price']),
                    account_info=lambda: types.SimpleNamespace(trade_mode=0),
                    ACCOUNT_TRADE_MODE_DEMO=0)   # DEMO so manual_seed is accepted
                ad = types.SimpleNamespace(mt5=mt5, place_market_order=pmo,
                                           modify_position_sl=lambda tk, sl: None,
                                           close_position=lambda tk, dry_run=False: env['book'].pop(int(tk), None))
                tr = types.SimpleNamespace(cfg=cfg, paper=True, adapter=ad, _last_boost_mid=None,
                                           state={'last_broker_date': '2026-07-02'},
                                           tele=types.SimpleNamespace(info=lambda *a, **k: None,
                                                                      warn=lambda *a, **k: None))
                tr._rogue = {'day': '2026-07-02', 'gov': _r.new_day_state(), 'anchor': None,
                             'leg_dir': None, 'open': None, 'a1_last_close': None, 'a1_reverted': False}
                return tr, env

            # (a) post-EOD (allow_new_entries=False) BLOCKS a fresh entry.
            trA, envA = mk(); _r.manual_seed(trA, 4000.0)
            envA['price'] = 4010.0; trA._last_boost_mid = 4010.0
            _r._drive_a1(trA, trA._rogue, allow_new_entries=False)   # +$10 but blocked
            blocked_new = (trA._rogue['open'] is None and trA._rogue['gov']['reanchor_count'] == 0)

            # (b) post-EOD still TRAILS an existing open position.
            trB, envB = mk(); _r.manual_seed(trB, 4000.0)
            envB['price'] = 4010.0; trB._last_boost_mid = 4010.0
            _r._drive_a1(trB, trB._rogue)                            # ENTER BUY @4010 (open)
            entered = trB._rogue['open'] is not None
            envB['price'] = 4035.0; trB._last_boost_mid = 4035.0
            _r._drive_a1(trB, trB._rogue, allow_new_entries=False)   # trail-only manage
            trailed = (trB._rogue['open'] is not None and trB._rogue['open']['peak'] >= 4035.0)

            # (c) force_close_open (kill-switch) closes the Rogue ticket + books it.
            trC, envC = mk(); _r.manual_seed(trC, 4000.0)
            envC['price'] = 4010.0; trC._last_boost_mid = 4010.0
            _r._drive_a1(trC, trC._rogue)
            tkC = trC._rogue['open']['ticket']
            envC['deal'] = {'ticket': tkC, 'pnl': 40.0, 'price': 4010.0}
            fc = _r.force_close_open(trC, reason='KillSwitch')
            killed = (fc is True and trC._rogue['open'] is None
                      and abs(trC._rogue['gov']['day_pnl'] - 40.0) < 1e-9 and tkC not in envC['book'])

            ok = blocked_new and entered and trailed and killed
            detail = (f"post_eod_blocks_new={blocked_new} trails_existing={trailed} "
                      f"kill_force_close[closed+booked]={killed}")
        except Exception as e:
            self._record(192, FAIL, f"raised: {e!r}"); return
        self._record(192, PASS if ok else FAIL, detail)

    def _step_fix4_feed_reinit_l3(self):
        # 193 (Fix 4 / E-12): Level 2 MT5Adapter.reinit() confirms a FRESH tick (fresh->True,
        # stale/init-fail->False), and the FeedWatchdog ladder escalates re-subscribe (capped)
        # -> reinit -> ONE self-restart (Level 3). The self-restart is the caller's to gate on
        # feed_selfrestart_enabled + the market-closed guard (covered live; the flag defaults ON).
        import mt5_adapter as _mad, feed_watchdog as _fw, types, time as _time
        try:
            now = _time.time()

            class _MT5:
                def __init__(s, tick_time, init_ok=True):
                    s._tt = tick_time; s._init_ok = init_ok
                def shutdown(s): pass
                def initialize(s): return s._init_ok
                def symbol_select(s, sym, on): return True
                def symbol_info_tick(s, sym): return types.SimpleNamespace(time=s._tt, bid=1.0, ask=1.0)
                def last_error(s): return (0, 'ok')

            def fake(tick_time, init_ok=True):
                return types.SimpleNamespace(mt5=_MT5(tick_time, init_ok), symbol='XAUUSD',
                                             tick_time_offset_hours=0)
            fresh = _mad.MT5Adapter.reinit(fake(now)) is True
            stale = _mad.MT5Adapter.reinit(fake(now - 4000.0)) is False
            init_fail = _mad.MT5Adapter.reinit(fake(now, init_ok=False)) is False
            reinit_ok = fresh and stale and init_fail

            cfg = self._feed_wd_cfg()
            wd = _fw.FeedWatchdog()
            resub = []; reinits = []; restarts = 0
            for i in range(1, 400):
                a = wd.on_failure(cfg, float(i))
                if a.resubscribe: resub.append(a.attempt)
                if a.reinit: reinits.append(a.reinit_attempt)
                if a.self_restart: restarts += 1
            ladder_ok = (resub == [1, 2, 3, 4, 5]      # capped -- no 'attempt 6/5'
                         and reinits == [1, 2] and restarts == 1)
            ok = reinit_ok and ladder_ok
            detail = (f"reinit[fresh={fresh} stale={stale} init_fail={init_fail}] "
                      f"ladder[resub={resub} reinit={reinits} restarts={restarts}]")
        except Exception as e:
            self._record(193, FAIL, f"raised: {e!r}"); return
        self._record(193, PASS if ok else FAIL, detail)

    def _step_fix5_restart_recovery(self):
        # 194 (Fix 5 / E-16): a simulated MID-DAY restart restores the Rogue governors +
        # chain anchor from run/state.json (anchors already placed are NOT re-placed -- the
        # snapshot keeps processed_anchors_today), ADOPTS an open Rogue position only if it is
        # still open at the broker, and a NEW trading day ignores the stale file.
        import p1_state as _p1, rogue as _r, types, tempfile
        try:
            run_dir = tempfile.mkdtemp()

            def mk(day, book_open=True, ticket=7007):
                env = {'book': {}}
                if book_open and ticket is not None:
                    env['book'][ticket] = True
                ad = types.SimpleNamespace(mt5=types.SimpleNamespace(
                    positions_get=lambda ticket=None: [object()] if env['book'].get(int(ticket)) else []))
                tr = types.SimpleNamespace(adapter=ad, run_dir=run_dir, paper=False,
                                           shadow_positions={}, state={'last_broker_date': day},
                                           tele=types.SimpleNamespace(info=lambda *a, **k: None))
                return tr

            # write a snapshot for 2026-07-02 with 2 placed anchors + rogue governors + open.
            src = mk('2026-07-02')
            src.state['processed_anchors_today'] = ['A1_0500', 'A2_1230']
            src._rogue = {'day': '2026-07-02', 'gov': _r.new_day_state(), 'anchor': None,
                          'leg_dir': 'SELL', 'open': {'ticket': 7007, 'side': 'SELL',
                          'entry': 3950.0, 'sl': 3955.0, 'peak': 3940.0, 'magic': _r.ROGUE_MAGIC,
                          'leg_type': 'rogue'}, 'a1_last_close': 3953.0, 'a1_reverted': False}
            src._rogue['gov'].update({'reanchor_count': 4, 'day_pnl': 123.45, 'consec_fails': 1})
            _p1.save(src, force=True)

            # SAME-day restart -> governors + anchor restored; open (still at broker) adopted.
            r1 = mk('2026-07-02', book_open=True); r1._rogue = None
            s1 = _p1.recover_on_boot(r1)
            g = (r1._rogue or {}).get('gov', {})
            same_day_ok = (s1['recovered'] and g.get('reanchor_count') == 4
                           and abs(g.get('day_pnl', 0) - 123.45) < 1e-9 and g.get('consec_fails') == 1
                           and r1._rogue.get('a1_last_close') == 3953.0
                           and r1._rogue.get('open') and r1._rogue['open']['ticket'] == 7007
                           and s1['anchors'] == 2)   # 2 placed anchors carried (skipped on re-fire)

            # ticket NO LONGER open at broker -> governors restored, open NOT adopted.
            r2 = mk('2026-07-02', book_open=False); r2._rogue = None
            _p1.recover_on_boot(r2)
            not_adopt_ok = (r2._rogue.get('open') is None
                            and r2._rogue['gov']['reanchor_count'] == 4)

            # NEW trading day -> stale file ignored (fresh start).
            r3 = mk('2026-07-03'); r3._rogue = None
            s3 = _p1.recover_on_boot(r3)
            new_day_ok = (not s3['recovered'] and 'new-day' in s3['reason'] and r3._rogue is None)

            ok = same_day_ok and not_adopt_ok and new_day_ok
            detail = (f"same_day[gov+anchor+adopt+anchors_kept]={same_day_ok} "
                      f"not_open->no_adopt={not_adopt_ok} new_day_ignored={new_day_ok}")
        except Exception as e:
            self._record(194, FAIL, f"raised: {e!r}"); return
        self._record(194, PASS if ok else FAIL, detail)

    def _step_watchdog_exit_policy(self):
        # 195 (watchdog relaunch policy): the watchdog relaunches bot.py ONLY on exit code
        # 42 (the controlled feed self-restart) and STOPS on every other exit code (crash /
        # clean /stop / clock-drift abort exit 0) -- so a crashing bot can never crash-loop
        # and spam-place orders on each boot. A runaway 42-loop (>= the cap, feed
        # unrecoverable) also stops for a human. PURE (watchdog.relaunch_policy).
        import watchdog as _wd
        try:
            cap = _wd.MAX_CONSECUTIVE_SELFRESTARTS
            relaunch_42 = (_wd.relaunch_policy(42, 1) == 'relaunch'
                           and _wd.relaunch_policy(42, cap - 1) == 'relaunch')
            runaway = (_wd.relaunch_policy(42, cap) == 'stop_runaway'
                       and _wd.relaunch_policy(42, cap + 5) == 'stop_runaway')
            # every non-42 exit code STOPS (no relaunch): clean stop, generic crash,
            # segfault, negative signal codes, and a near-miss (43) that must not count.
            stop_others = all(_wd.relaunch_policy(rc, 0) == 'stop'
                              for rc in (0, 1, 2, 43, 139, -1, 255))
            ok = relaunch_42 and runaway and stop_others
            detail = (f"only42_relaunches={relaunch_42} runaway@{cap}_stops={runaway} "
                      f"all_other_codes_stop={stop_others}")
        except Exception as e:
            self._record(195, FAIL, f"raised: {e!r}"); return
        self._record(195, PASS if ok else FAIL, detail)

    # === P3 (E-17): Rogue monster-catcher discipline — chase cap + chain gates ====
    # Shared harness: the same controllable A1-mode stub trader as step 189 (env['book']
    # = live broker positions; env['deal'] = the close deal; env['logs'] captures tele).
    def _p3_mk(self, cfg):
        import rogue as _r, types
        env = {'price': None, 'book': {}, 'deal': None, 'orders': [], 'logs': []}
        def place_market_order(sym, side, lot, sl=None, tp=None, magic=None,
                               comment=None, dry_run=False):
            tk = 910000 + len(env['orders']) + 1
            env['orders'].append({'ticket': tk, 'side': side, 'sl': sl})
            env['book'][tk] = True
            return types.SimpleNamespace(order=tk, deal=tk, retcode=10009)
        def positions_get(ticket=None):
            return [object()] if env['book'].get(int(ticket)) else []
        def history_deals_get(position=None):
            d = env['deal']
            return ([types.SimpleNamespace(entry=1, profit=d['pnl'], price=d['price'],
                                           swap=0.0, commission=0.0)]
                    if (d and int(position) == int(d['ticket'])) else [])
        mt5 = types.SimpleNamespace(
            positions_get=positions_get, history_deals_get=history_deals_get,
            symbol_info_tick=lambda s=None: types.SimpleNamespace(
                bid=env['price'], ask=env['price']),
            account_info=lambda: types.SimpleNamespace(trade_mode=0),
            ACCOUNT_TRADE_MODE_DEMO=0)
        ad = types.SimpleNamespace(
            mt5=mt5, place_market_order=place_market_order,
            modify_position_sl=lambda tk, sl: None,
            close_position=lambda tk, dry_run=False: env['book'].pop(int(tk), None))
        tr = types.SimpleNamespace(
            cfg=cfg, paper=True, adapter=ad, _last_boost_mid=None,
            state={'last_broker_date': '2026-07-02'},
            tele=types.SimpleNamespace(
                info=lambda *a, **k: env['logs'].append(a[0] if a else ''),
                warn=lambda *a, **k: None))
        tr._rogue = {'day': '2026-07-02', 'gov': _r.new_day_state(), 'anchor': None,
                     'leg_dir': None, 'open': None, 'a1_last_close': None,
                     'a1_reverted': False}
        return tr, env

    def _p3_tick(self, tr, env, price, close=None):
        import rogue as _r
        env['price'] = float(price)
        tr._last_boost_mid = float(price)
        if close is not None:            # broker closes the open ticket at (pnl, exit)
            o = tr._rogue.get('open') or {}
            tk = o.get('ticket')
            if tk is not None:
                env['book'].pop(int(tk), None)
                env['deal'] = {'ticket': tk, 'pnl': close[0], 'price': close[1]}
        _r._drive_a1(tr, tr._rogue)

    @staticmethod
    def _p3_warp(tr, sec=400):
        # simulate wall-clock passing: backdate the chain plant time (the gate reads
        # rogue._epoch() - chain_time). Assertion-neutral -- timing only.
        if tr._rogue.get('chain_time') is not None:
            tr._rogue['chain_time'] -= float(sec)

    def _step_p3_chase_cap(self):
        # 196 GATE 1 (E-17): entry band $10 <= |move| <= $20 off the ACTIVE anchor.
        # Beyond the cap: NO entry, NO governor slot, ONE throttled CHASE-REJECT log.
        # No latch: a later pullback inside the band enters normally. Pure band edges:
        # exactly $20 enters, $20.01 doesn't; both directions; cap=0 disables.
        import rogue as _r, dataclasses
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True,
                                      rogue_entry_confirm_redesign=10.0)
            # pure band edges
            band_ok = (_r.a1_entry_decision(4000.0, 4020.0, cfg)[0] is True      # == cap
                       and _r.a1_entry_decision(4000.0, 4020.01, cfg)[0] is False  # > cap
                       and _r.a1_entry_decision(4000.0, 3980.0, cfg)[:2] == (True, 'SELL')
                       and _r.a1_entry_decision(4000.0, 3979.0, cfg)[0] is False
                       and _r.a1_entry_decision(
                           4000.0, 4025.0,
                           dataclasses.replace(cfg, rogue_chase_cap_dollars=0.0))[0] is True)
            # driver: reject -> throttled log -> pullback re-allows, slot only on the fill
            tr, env = self._p3_mk(cfg)
            _r.manual_seed(tr, 4000.0)                     # seed anchor (not chained)
            self._p3_tick(tr, env, 4025.0)                 # +$25 > cap -> CHASE-REJECT
            rej1 = (tr._rogue['open'] is None and tr._rogue['gov']['reanchor_count'] == 0)
            n_logs = sum(1 for m in env['logs'] if 'CHASE-REJECT' in str(m))
            self._p3_tick(tr, env, 4026.0)                 # still beyond -> same episode
            n_logs2 = sum(1 for m in env['logs'] if 'CHASE-REJECT' in str(m))
            throttled = (n_logs == 1 and n_logs2 == 1)
            self._p3_tick(tr, env, 4015.0)                 # pullback inside band -> ENTER
            reallowed = (tr._rogue['open'] is not None
                         and tr._rogue['open']['side'] == 'BUY'
                         and tr._rogue['gov']['reanchor_count'] == 1)
            ok = band_ok and rej1 and throttled and reallowed
            detail = (f"band(20in/20.01out/0off)={band_ok} reject_no_slot={rej1} "
                      f"log_once={throttled} pullback_reenters={reallowed}")
        except Exception as e:
            self._record(196, FAIL, f"raised: {e!r}"); return
        self._record(196, PASS if ok else FAIL, detail)

    def _step_p3_chain_cooldown(self):
        # 197 GATE 2a (E-17): after a close, the CHAINED re-anchor refuses the next entry
        # until rogue_chain_cooldown_sec elapses -- ONE throttled CHAIN-COOLDOWN log, no
        # slot consumed -- then the SAME signal enters once the cooldown has passed.
        import rogue as _r, dataclasses
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True,
                                      rogue_entry_confirm_redesign=10.0)
            tr, env = self._p3_mk(cfg)
            _r.manual_seed(tr, 4000.0)
            self._p3_tick(tr, env, 4010.0)                       # ENTER BUY #1 (seed exempt)
            e1 = tr._rogue['open'] is not None
            self._p3_tick(tr, env, 4020.0, close=(350.0, 4020.0))   # close -> chain @ 4020
            chained = (abs((tr._rogue.get('chain_anchor') or 0) - 4020.0) < 1e-9
                       and tr._rogue.get('chain_time') is not None)
            self._p3_tick(tr, env, 4030.0)                       # +$10 but INSIDE cooldown
            blocked = (tr._rogue['open'] is None
                       and tr._rogue['gov']['reanchor_count'] == 1)
            n1 = sum(1 for m in env['logs'] if 'CHAIN-COOLDOWN' in str(m))
            self._p3_tick(tr, env, 4031.0)                       # still cooling -> no relog
            n2 = sum(1 for m in env['logs'] if 'CHAIN-COOLDOWN' in str(m))
            throttled = (n1 == 1 and n2 == 1)
            self._p3_warp(tr, float(cfg.rogue_chain_cooldown_sec) + 5)   # cooldown elapses
            self._p3_tick(tr, env, 4030.0)                       # same signal -> ENTER #2
            entered2 = (tr._rogue['open'] is not None
                        and tr._rogue['gov']['reanchor_count'] == 2
                        and tr._rogue.get('chain_anchor') is None)  # chain meta consumed
            ok = e1 and chained and blocked and throttled and entered2
            detail = (f"e1={e1} chained@4020={chained} inside_cooldown_blocked={blocked} "
                      f"log_once={throttled} after_cooldown_enters={entered2}")
        except Exception as e:
            self._record(197, FAIL, f"raised: {e!r}"); return
        self._record(197, PASS if ok else FAIL, detail)

    def _step_p3_chain_displacement(self):
        # 198 GATE 2b (E-17): the chained entry needs >= rogue_chain_min_displacement of
        # movement off the re-anchor IN the entry direction at some point since planting.
        # Tuned ABOVE the $10 confirm here (disp=15, cooldown=0) so the check bites:
        # +$12 is a valid confirm but < $15 fresh -> blocked; a spike to +$25 (chase-
        # rejected, no entry) RECORDS the displacement, and the pullback to +$12 then
        # enters -- 'at some point since planting', exactly as specified. Also proves the
        # two Gate-2 checks are independently toggleable (cooldown=0 here).
        import rogue as _r, dataclasses
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True,
                                      rogue_entry_confirm_redesign=10.0,
                                      rogue_chain_cooldown_sec=0.0,
                                      rogue_chain_min_displacement=15.0)
            # pure reasons
            pure_ok = (_r.chain_entry_allowed(None, 0.0, 12.0, cfg)[:2] == (False, 'displacement')
                       and _r.chain_entry_allowed(None, 0.0, 15.0, cfg)[0] is True
                       and _r.chain_entry_allowed(0.0, 100.0, 5.0, dataclasses.replace(
                           cfg, rogue_chain_min_displacement=0.0))[0] is True)
            tr, env = self._p3_mk(cfg)
            _r.manual_seed(tr, 4000.0)
            self._p3_tick(tr, env, 4010.0)                       # ENTER #1 (seed exempt)
            self._p3_tick(tr, env, 4020.0, close=(350.0, 4020.0))   # chain @ 4020
            self._p3_tick(tr, env, 4032.0)                       # +$12 confirm but disp<15
            blocked = (tr._rogue['open'] is None
                       and any('CHAIN-DISPLACEMENT' in str(m) for m in env['logs']))
            self._p3_tick(tr, env, 4045.0)                       # +$25: chase-reject BUT
            spike_no_entry = tr._rogue['open'] is None           # ...displacement recorded
            disp_recorded = float(tr._rogue.get('chain_disp_up', 0.0)) >= 15.0
            self._p3_tick(tr, env, 4032.0)                       # pullback +$12 -> ENTER
            entered = (tr._rogue['open'] is not None
                       and tr._rogue['gov']['reanchor_count'] == 2)
            ok = pure_ok and blocked and spike_no_entry and disp_recorded and entered
            detail = (f"pure={pure_ok} 12<15_blocked={blocked} spike_rejected={spike_no_entry} "
                      f"disp_recorded>=15={disp_recorded} pullback_enters={entered}")
        except Exception as e:
            self._record(198, FAIL, f"raised: {e!r}"); return
        self._record(198, PASS if ok else FAIL, detail)

    def _step_p3_reversal_exempt(self):
        # 199 (E-17): the reversal-recovery leg is time-critical -- NO chain cooldown on
        # it (fires immediately at $10 past entry), but the CHASE CAP still applies (a
        # recovery $35 past the anchor is an exhausted move too). Recovery SL still the
        # $13 rescue cap.
        import rogue as _r, dataclasses
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True,
                                      rogue_entry_confirm_redesign=10.0,
                                      rogue_reversal_dollars=10.0,
                                      rogue_rescue_cap_dollars=13.0)
            # (a) recovery fires immediately (no cooldown), SL = entry + $13 for a SELL.
            tr, env = self._p3_mk(cfg)
            _r.manual_seed(tr, 4000.0)
            self._p3_tick(tr, env, 4010.0)                       # ENTER BUY @4010
            tk1 = tr._rogue['open']['ticket']
            env['deal'] = {'ticket': tk1, 'pnl': -175.0, 'price': 4000.0}
            self._p3_tick(tr, env, 4000.0)                       # -$10 past entry -> REVERSAL
            reverted = (tr._rogue['open'] is None and tr._rogue.get('a1_reverted') is True
                        and tr._rogue.get('chain_time') is None)  # NOT chained
            self._p3_tick(tr, env, 4000.0)                       # -$10 off 4010 -> RECOVER NOW
            o = tr._rogue.get('open') or {}
            recovered = (o.get('side') == 'SELL'
                         and abs(float(env['orders'][-1]['sl']) - 4013.0) < 1e-9)
            # (b) the chase cap DOES apply to the recovery leg.
            tr2, env2 = self._p3_mk(cfg)
            _r.manual_seed(tr2, 4000.0)
            self._p3_tick(tr2, env2, 4010.0)                     # ENTER BUY @4010
            tk2 = tr2._rogue['open']['ticket']
            env2['deal'] = {'ticket': tk2, 'pnl': -175.0, 'price': 4000.0}
            self._p3_tick(tr2, env2, 4000.0)                     # REVERSAL (anchor 4010)
            self._p3_tick(tr2, env2, 3975.0)                     # -$35 off 4010 > cap -> reject
            capped = (tr2._rogue['open'] is None
                      and any('CHASE-REJECT' in str(m) for m in env2['logs']))
            self._p3_tick(tr2, env2, 3995.0)                     # -$15: inside band -> enters
            capped_then_ok = tr2._rogue.get('open') is not None
            ok = reverted and recovered and capped and capped_then_ok
            detail = (f"reversal_not_chained={reverted} recovery_immediate_sl13={recovered} "
                      f"recovery_chase_capped={capped} band_reentry={capped_then_ok}")
        except Exception as e:
            self._record(199, FAIL, f"raised: {e!r}"); return
        self._record(199, PASS if ok else FAIL, detail)

    def _step_p3_seeds_exempt(self):
        # 200 (E-17): gates apply ONLY to re-anchors from closes. The A1 morning seed and
        # a manual rogueseed are NOT chained -> the first trade of the day fires at the
        # plain $10 confirm with zero cooldown/displacement wait.
        import rogue as _r, dataclasses
        try:
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True,
                                      rogue_entry_confirm_redesign=10.0)
            # A1 morning seed (read-only cross-read of the A1 anchor price)
            tr, env = self._p3_mk(cfg)
            tr.shadow_positions = {55: {'anchor_label': 'A1', 'leg_fill_price': 4000.0,
                                        'magic': 20260522}}
            self._p3_tick(tr, env, 4010.0)                       # +$10 off A1 seed -> ENTER
            a1_seed = (tr._rogue['open'] is not None
                       and tr._rogue.get('chain_anchor') is None)
            # manual rogueseed
            tr2, env2 = self._p3_mk(cfg)
            _r.manual_seed(tr2, 4000.0)
            seed_unchained = (tr2._rogue.get('chain_time') is None
                              and tr2._rogue.get('chain_anchor') is None)
            self._p3_tick(tr2, env2, 3990.0)                     # -$10 off seed -> ENTER NOW
            manual = (tr2._rogue['open'] is not None
                      and tr2._rogue['open']['side'] == 'SELL')
            no_gate_logs = not any(('CHAIN-COOLDOWN' in str(m) or 'CHAIN-DISPLACEMENT' in str(m))
                                   for m in env['logs'] + env2['logs'])
            ok = a1_seed and seed_unchained and manual and no_gate_logs
            detail = (f"a1_seed_immediate={a1_seed} rogueseed_unchained={seed_unchained} "
                      f"rogueseed_immediate={manual} no_gate_logs={no_gate_logs}")
        except Exception as e:
            self._record(200, FAIL, f"raised: {e!r}"); return
        self._record(200, PASS if ok else FAIL, detail)

    def _step_p3_gates_off_freeze(self):
        # 201 (E-17) FREEZE: with all three knobs 0 the pre-P3 behavior is reproduced
        # exactly -- an entry $25 past the anchor fires (no cap) and the chain re-enters
        # immediately after a close (no cooldown, no displacement wait). Also pins the
        # protective defaults ON (cap 20 / cooldown 300 / displacement 6).
        import rogue as _r, dataclasses
        try:
            defaults_on = (abs(float(self.cfg.rogue_chase_cap_dollars) - 20.0) < 1e-9
                           and abs(float(self.cfg.rogue_chain_cooldown_sec) - 300.0) < 1e-9
                           and abs(float(self.cfg.rogue_chain_min_displacement) - 6.0) < 1e-9)
            cfg = dataclasses.replace(self.cfg, rogue_a1_anchor_mode=True, rogue_enabled=True,
                                      rogue_entry_confirm_redesign=10.0,
                                      rogue_chase_cap_dollars=0.0,
                                      rogue_chain_cooldown_sec=0.0,
                                      rogue_chain_min_displacement=0.0)
            tr, env = self._p3_mk(cfg)
            _r.manual_seed(tr, 4000.0)
            self._p3_tick(tr, env, 4025.0)                       # +$25 -> ENTERS (no cap)
            unbounded = (tr._rogue['open'] is not None
                         and abs(tr._rogue['open']['entry'] - 4025.0) < 1e-9)
            self._p3_tick(tr, env, 4040.0, close=(500.0, 4040.0))   # close -> chain @ 4040
            self._p3_tick(tr, env, 4050.0)                       # IMMEDIATE re-entry (old)
            immediate = (tr._rogue['open'] is not None
                         and tr._rogue['gov']['reanchor_count'] == 2)
            no_gate_logs = not any(any(k in str(m) for k in
                                       ('CHASE-REJECT', 'CHAIN-COOLDOWN', 'CHAIN-DISPLACEMENT'))
                                   for m in env['logs'])
            ok = defaults_on and unbounded and immediate and no_gate_logs
            detail = (f"defaults(20/300/6)={defaults_on} no_cap_entry@+25={unbounded} "
                      f"immediate_chain_reentry={immediate} silent={no_gate_logs}")
        except Exception as e:
            self._record(201, FAIL, f"raised: {e!r}"); return
        self._record(201, PASS if ok else FAIL, detail)

    # --- v3.5.0 all-16 features (renumbered 148-161; logic identical to
    #     feature/v3.5.0-all16 132-145 -- only the _record() numbers shifted) ---
    def _step_f8_pullback_log(self):
        # 132 (feature 8): per-anchor armed/pulled-back/entered/skipped counts + JSON.
        import boost_metrics as _bm
        try:
            counts = {}
            _bm.pullback_bump(counts, 'A3', 'RALLY', 'armed')
            _bm.pullback_bump(counts, 'A3', 'RALLY', 'pulled_back')
            _bm.pullback_bump(counts, 'A3', 'RALLY', 'entered')
            _bm.pullback_bump(counts, 'A5', 'RESCUE', 'armed')
            _bm.pullback_bump(counts, 'A5', 'RESCUE', 'skipped')
            _bm.pullback_bump(counts, 'A3', 'RALLY', 'bogus')   # ignored, no crash
            c = counts['A3:RALLY']
            shape_ok = (c['armed'] == 1 and c['pulled_back'] == 1 and c['entered'] == 1
                        and c['skipped'] == 0 and counts['A5:RESCUE']['skipped'] == 1)
            import json as _json
            body = _bm.pullback_json(counts, '2026-06-27')
            parsed = _json.loads(body)
            json_ok = (parsed['date'] == '2026-06-27'
                       and parsed['counts']['A3:RALLY']['entered'] == 1)
            ok = shape_ok and json_ok
            detail = f"counts_shape={shape_ok} json_roundtrip={json_ok}"
        except Exception as e:
            self._record(148, FAIL, f"raised: {e!r}"); return
        self._record(148, PASS if ok else FAIL, detail)

    def _step_f9_boost_ledger(self):
        # 133 (feature 9): one ledger row per boost event in LEDGER_COLUMNS order.
        import boost_metrics as _bm
        try:
            row = _bm.ledger_row({'ts': 't', 'anchor': 'A3', 'kind': 'RALLY',
                                  'event': 'enter', 'arm_px': 4005.0, 'entry_px': 3994.0})
            order_ok = (len(row) == len(_bm.LEDGER_COLUMNS)
                        and row[_bm.LEDGER_COLUMNS.index('entry_px')] == 3994.0
                        and row[_bm.LEDGER_COLUMNS.index('kind')] == 'RALLY')
            missing = _bm.ledger_row({'anchor': 'A5'})   # missing keys -> '' (no crash)
            missing_ok = (missing[_bm.LEDGER_COLUMNS.index('pnl_usd')] == ''
                          and missing[_bm.LEDGER_COLUMNS.index('anchor')] == 'A5')
            ok = order_ok and missing_ok
            detail = f"row_order={order_ok} missing_keys_blank={missing_ok}"
        except Exception as e:
            self._record(149, FAIL, f"raised: {e!r}"); return
        self._record(149, PASS if ok else FAIL, detail)

    def _step_f10_daily_report(self):
        # 134 (feature 10): per-anchor markdown from trades rows (read-only formatting).
        import boost_metrics as _bm
        try:
            rows = [{'anchor': 'A3_1430_Overlap', 'pnl': 100.0},
                    {'anchor': 'A3_1430_Overlap', 'pnl': -40.0},
                    {'anchor': 'A5_1930_LateUS', 'pnl': 25.5}]
            md = _bm.daily_report_md(rows, '2026-06-27')
            ok = ('# AUREON daily report' in md and '| A3 |' in md and '| A5 |' in md
                  and '+60.00' in md and 'Day net: $+85.50' in md)
            detail = f"markdown_has_per_anchor_and_net={ok}"
        except Exception as e:
            self._record(150, FAIL, f"raised: {e!r}"); return
        self._record(150, PASS if ok else FAIL, detail)

    def _step_f11_preflight(self):
        # 135 (feature 11): boot self-check -> abort (ok False) when offset UNDETECTED;
        # ok True when detected; flags split into ON/OFF.
        import boost_metrics as _bm
        try:
            ok_none, lines_none = _bm.preflight_lines(None, ['A1', 'A2'],
                                                      {'override_entry_enabled': True}, True)
            aborts = (ok_none is False and any('ABORT' in l for l in lines_none))
            ok_ok, lines_ok = _bm.preflight_lines(3, ['A1', 'A2', 'A3', 'A4', 'A5'],
                                                  {'override_entry_enabled': False,
                                                   'util_preflight': True}, True)
            proceeds = (ok_ok is True and any('+3h' in l for l in lines_ok))
            flags_split = (any('flags ON:  util_preflight' in l for l in lines_ok)
                           and any('flags OFF: override_entry_enabled' in l for l in lines_ok))
            ok = aborts and proceeds and flags_split
            detail = f"undetected_aborts={aborts} detected_proceeds={proceeds} flags_split={flags_split}"
        except Exception as e:
            self._record(151, FAIL, f"raised: {e!r}"); return
        self._record(151, PASS if ok else FAIL, detail)

    def _step_util_no_order_effect(self):
        # 136: utilities (8-11) NEVER touch order flow. The pure entry state machine
        # decision is byte-identical whether the util flags are ON or OFF (step never
        # reads them); and record_pullback_event with the flag OFF is an inert no-op.
        import pullback_entry as _pe, boost_metrics as _bm, types, dataclasses
        try:
            def decide():
                st = {}
                last = None
                for p, b in [(3982.0, 0), (4005.0, 0), (3992.0, 0), (3994.0, 0)]:
                    last = _pe.step(st, direction='BUY', pullback_depth=13.0, fixed_sl=13.0,
                                    timeout_candles=4, current_price=p, m5_bucket=b,
                                    parent_alive=True, smooth_confirm=False,
                                    allow_smooth=False, dynamic_sl=True)
                return last
            d1 = decide(); d2 = decide()
            decision_independent = (d1 == d2 and d1['action'] == 'ENTER')
            # record with flag OFF -> no counts created, no file, no raise.
            stub = types.SimpleNamespace(cfg=dataclasses.replace(self.cfg,
                                         util_pullback_log=False, util_boost_ledger=False))
            _bm.record_pullback_event(stub, 'A3', 'RALLY', 'armed')
            inert = not hasattr(stub, '_pullback_counts')
            ok = decision_independent and inert
            detail = f"decision_independent_of_util={decision_independent} record_off_is_noop={inert}"
        except Exception as e:
            self._record(152, FAIL, f"raised: {e!r}"); return
        self._record(152, PASS if ok else FAIL, detail)

    # --- v3.5.0 all-16: strategy extras (12-14) -----------------------------
    def _step_f12_confirm_candle(self):
        # 137 (feature 12): confirm_candle ON -> the turn must hold to an M5 CLOSE (a new
        # bucket) before entry; OFF -> first-touch (enter at the turn immediately).
        import pullback_entry as _pe
        def _s(state, p, b, confirm):
            return _pe.step(state, direction='BUY', pullback_depth=13.0, fixed_sl=13.0,
                            timeout_candles=4, current_price=p, m5_bucket=b, parent_alive=True,
                            smooth_confirm=False, allow_smooth=False, dynamic_sl=True,
                            confirm_candle=confirm)
        try:
            # path: arm@100 -> high 110 -> dip 96 (>=13) -> turn 98. OFF enters at the turn.
            off = {}
            _s(off, 100.0, 0, False); _s(off, 110.0, 0, False); _s(off, 96.0, 0, False)
            d_off = _s(off, 98.0, 0, False)           # turn (98-96=2) -> first-touch ENTER
            first_touch_enters = (d_off['action'] == 'ENTER')
            # ON: the same turn on bucket 0 HOLDS (waits for the M5 close); enters only
            # once the bucket advances with price still in-direction.
            on = {}
            _s(on, 100.0, 0, True); _s(on, 110.0, 0, True); _s(on, 96.0, 0, True)
            d_hold = _s(on, 98.0, 0, True)            # turn but same candle -> ARM (hold)
            d_confirm = _s(on, 98.5, 1, True)         # next M5 close, still up -> ENTER
            gates_then_enters = (d_hold['action'] == 'ARM' and d_confirm['action'] == 'ENTER')
            ok = first_touch_enters and gates_then_enters
            detail = f"first_touch_enters={first_touch_enters} confirm_gates_then_enters={gates_then_enters}"
        except Exception as e:
            self._record(153, FAIL, f"raised: {e!r}"); return
        self._record(153, PASS if ok else FAIL, detail)

    def _step_f13_atr_depth(self):
        # 138 (feature 13): effective_depth = atr_mult*ATR when entry_adaptive_depth ON;
        # fixed when OFF. atr_from_candles = mean(high-low).
        import pullback_entry as _pe, dataclasses
        try:
            candles = [{'high': 10.0, 'low': 6.0, 'close': 8.0},
                       {'high': 12.0, 'low': 6.0, 'close': 9.0}]   # ranges 4,6 -> ATR 5
            atr = _pe.atr_from_candles(candles)
            atr_ok = abs(atr - 5.0) < 1e-9
            off = _pe.effective_depth(self.cfg, 13.0, atr)          # flag OFF -> fixed 13
            cfg_on = dataclasses.replace(self.cfg, entry_adaptive_depth=True, atr_mult=2.0)
            on = _pe.effective_depth(cfg_on, 13.0, atr)             # 2.0 * 5 = 10
            ok = (atr_ok and abs(off - 13.0) < 1e-9 and abs(on - 10.0) < 1e-9)
            detail = f"atr={atr}(=5) off_fixed={off}(=13) on_atrx2={on}(=10)"
        except Exception as e:
            self._record(154, FAIL, f"raised: {e!r}"); return
        self._record(154, PASS if ok else FAIL, detail)

    def _step_f14_rescue_sl_wide(self):
        # 139 (feature 14): rescue_sl_wide ON -> RESCUE boost SL $10->$13 AND the DERIVED
        # cap -$700->-$910 (recomputed together via boost_sl_for). OFF -> $10/-$700.
        # RALLY cap is independent (stays -$910).
        import boosts as _boosts, dataclasses
        try:
            off_sl = _boosts.boost_sl_for(self.cfg, 'RESCUE')
            off_cap = _boosts.boost_whipsaw_cap(self.cfg, 'RESCUE')
            cfg_on = dataclasses.replace(self.cfg, rescue_sl_wide=True)
            on_sl = _boosts.boost_sl_for(cfg_on, 'RESCUE')
            on_cap = _boosts.boost_whipsaw_cap(cfg_on, 'RESCUE')
            rally_cap = _boosts.boost_whipsaw_cap(cfg_on, 'RALLY')
            ok = (abs(off_sl - 10.0) < 1e-9 and abs(off_cap - 700.0) < 1e-6
                  and abs(on_sl - 13.0) < 1e-9 and abs(on_cap - 910.0) < 1e-6
                  and abs(rally_cap - 910.0) < 1e-6)
            detail = (f"off(SL$10/cap-$700)={abs(off_cap-700)<1e-6} "
                      f"on(SL$13/cap-$910)={abs(on_cap-910)<1e-6} rally_cap-$910={abs(rally_cap-910)<1e-6}")
        except Exception as e:
            self._record(155, FAIL, f"raised: {e!r}"); return
        self._record(155, PASS if ok else FAIL, detail)

    # --- v3.5.0 all-16: hotfixes (15-16) ------------------------------------
    def _step_f15_boost_telemetry(self):
        # 140 (feature 15): fix_boost_telemetry ON -> an armed rally boost emits its trail
        # advance (LOCK_ARM/TRAIL_ADVANCE) so its EXIT is never falsely flagged. OFF ->
        # no emission (pre-v3.3.0 silence; telemetry only, no P&L change).
        import dataclasses
        from strategy import Position, update_position_on_bar
        from position_telemetry import PositionTracer, LOCK_ARM, TRAIL_ADVANCE
        try:
            ts0 = pd.Timestamp('2026-06-24T02:30:00Z')
            def emits(cfg):
                ev = []
                tr = PositionTracer(sink=lambda l: None)
                # patch emit to capture types
                p = Position(anchor_label='T', side='BUY', entry_price=100.0, entry_time=ts0,
                             current_sl=90.0, tp_level=130.0, max_fav=100.0, lot=0.35,
                             role='rescue', boost=True, boost_kind='RALLY')
                update_position_on_bar(p, pd.Series({'open': 100, 'high': 108, 'low': 100, 'close': 107}),
                                       ts0 + pd.Timedelta(minutes=1), cfg, tracer=tr, ticket=701)
                hist = tr._history.get(701, [])
                return any(h.get('event_type') in (LOCK_ARM, TRAIL_ADVANCE) for h in hist)
            on = emits(self.cfg)                                              # default ON
            off = emits(dataclasses.replace(self.cfg, fix_boost_telemetry=False))
            ok = (on is True and off is False)
            detail = f"flag_on_emits={on} flag_off_silent={off}"
        except Exception as e:
            self._record(156, FAIL, f"raised: {e!r}"); return
        self._record(156, PASS if ok else FAIL, detail)

    def _step_f16_offset_no_0h(self):
        # 141 (feature 16): the wake offset resolver NEVER falls back to 0h -- all-0 reads
        # BLOCK (None), a +3 read confirms. fix_a1_offset defaults ON; OFF never re-adds a
        # 0h guess (the block is the fail-safe).
        import offset_guard as og
        try:
            off, result, attempts = og.resolve_offset([0, 0, 0])
            never_0h = (off is None and result == og.BLOCKED)
            confirms_3 = (og.resolve_offset([3]) == (3, og.CONFIRMED, 1))
            flag_default_on = (bool(getattr(self.cfg, 'fix_a1_offset', True)) is True)
            ok = never_0h and confirms_3 and flag_default_on
            detail = f"all0_blocks(no_0h)={never_0h} +3_confirms={confirms_3} flag_default_on={flag_default_on}"
        except Exception as e:
            self._record(157, FAIL, f"raised: {e!r}"); return
        self._record(157, PASS if ok else FAIL, detail)

    # --- v3.5.0 all-16: freeze / independence / routing / flag table --------
    def _step_strat_full_freeze(self):
        # 142 FREEZE: with ALL strategy flags FALSE (the defaults) behavior == master.
        # The rally override fires IMMEDIATELY (override_entry OFF) and every strategy
        # extra defaults OFF (12/13/14) so order logic is byte-identical to v3.5.0 core.
        import rally as _rally
        try:
            strat_off = (getattr(self.cfg, 'override_entry_enabled') is False
                         and getattr(self.cfg, 'rescue_entry_enabled') is False
                         and getattr(self.cfg, 'entry_confirm_candle') is False
                         and getattr(self.cfg, 'entry_adaptive_depth') is False
                         and getattr(self.cfg, 'rescue_sl_wide') is False)
            bars = self._case2_bars()
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL',
                                               parent_side='SELL', parent_max_fav=25.0)
            immediate = (_rally.break_and_hold_ok(tr, sh, pl) is True)
            ev = [e for e in self._gate_ptrace if e[0] == 'break_override_parent_established']
            legacy = (len(ev) == 1 and 'entry_mode' not in ev[0][1])
            ok = strat_off and immediate and legacy
            detail = f"all_strategy_flags_off={strat_off} immediate_override_fire={immediate} legacy={legacy}"
        except Exception as e:
            self._record(158, FAIL, f"raised: {e!r}"); return
        self._record(158, PASS if ok else FAIL, detail)

    def _step_per_flag_indep(self):
        # 143: each strategy flag toggles INDEPENDENTLY -- flipping one leaves the others
        # at their defaults (no cross-wiring).
        import dataclasses
        try:
            checks = []
            for key in ('override_entry_enabled', 'rescue_entry_enabled',
                        'entry_confirm_candle', 'entry_adaptive_depth', 'rescue_sl_wide'):
                c = dataclasses.replace(self.cfg, **{key: True})
                others = [k for k in ('override_entry_enabled', 'rescue_entry_enabled',
                                      'entry_confirm_candle', 'entry_adaptive_depth',
                                      'rescue_sl_wide') if k != key]
                only_this = (getattr(c, key) is True
                             and all(getattr(c, o) is False for o in others))
                checks.append(only_this)
            ok = all(checks)
            detail = f"each_flag_independent={ok} ({sum(checks)}/{len(checks)})"
        except Exception as e:
            self._record(159, FAIL, f"raised: {e!r}"); return
        self._record(159, PASS if ok else FAIL, detail)

    def _step_rescue_gate_on_arm(self):
        # 144: the RESCUE gate, with rescue_entry_enabled ON, ARMS (does NOT immediate-fire
        # at -10) -- the gate-level proof that the flag routes through the adaptive helper
        # (mirror of the rally gate-ON arm). R-case logic itself is covered by 119/128.
        import rescue as _rescue, dataclasses
        try:
            cfg_on = dataclasses.replace(self.cfg, rescue_entry_enabled=True,
                                         util_pullback_log=False, util_boost_ledger=False)
            bars = [{'high': 4033.0, 'low': 4031.0, 'close': 4032.0},
                    {'high': 4033.0, 'low': 4031.0, 'close': 4032.0}]
            tr, sh, pl = self._break_gate_stub(lambda s, n: bars, side='SELL', kind='RESCUE',
                                               parent_side='BUY', cfg=cfg_on, last_mid=4032.0)
            first = _rescue.entry_gate_ok(tr, sh, pl)
            armed = bool(sh.get('rescue_entry_arm', {}).get('armed'))
            no_fire = (first is False)
            armed_ev = any(e[0] == 'rescue_entry_armed' for e in self._gate_ptrace)
            ok = no_fire and armed and armed_ev
            detail = f"rescue_on_no_immediate_fire={no_fire} armed={armed} armed_ptrace={armed_ev}"
        except Exception as e:
            self._record(160, FAIL, f"raised: {e!r}"); return
        self._record(160, PASS if ok else FAIL, detail)

    def _step_flag_table_check(self):
        # 145: all 16 features have their flag/param on Config with the directive defaults
        # (the flag-reference table, asserted in code).
        try:
            spec = {
                'override_entry_enabled': False, 'rescue_entry_enabled': False,
                'override_entry_smooth_confirm': True, 'rescue_entry_smooth_confirm': True,
                'override_entry_dynamic_sl': True,
                'override_entry_arm_timeout_candles': 4, 'rescue_entry_arm_timeout_candles': 4,
                'util_pullback_log': True, 'util_boost_ledger': True,
                'util_daily_report': True, 'util_preflight': True,
                'entry_confirm_candle': False, 'entry_adaptive_depth': False,
                'atr_period': 14, 'atr_mult': 1.0,
                'rescue_sl_wide': False, 'rescue_sl_wide_dollars': 13.0,
                'fix_boost_telemetry': True, 'fix_a1_offset': True,
            }
            missing = [k for k in spec if not hasattr(self.cfg, k)]
            wrong = [k for k, v in spec.items()
                     if hasattr(self.cfg, k) and getattr(self.cfg, k) != v]
            ok = (not missing and not wrong)
            detail = f"all_flags_present={not missing} defaults_correct={not wrong} missing={missing} wrong={wrong}"
        except Exception as e:
            self._record(161, FAIL, f"raised: {e!r}"); return
        self._record(161, PASS if ok else FAIL, detail)

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
            # v3.3.8 — 5th anchor A5 @ 22:00 IST (identical structure; no collision)
            self._step_five_anchors_times()
            self._step_anchor_no_collision()
            self._step_a5_identical_fp5()
            # v3.4.0 — RALLY override pullback-entry (flag-gated, DEFAULT OFF)
            self._step_override_freeze_guard()
            self._step_override_arm_no_fire()
            self._step_retired_108()
            self._step_retired_109()
            self._step_retired_110()
            self._step_override_rescue_unaffected()
            self._step_override_5arm_unaffected()
            self._step_override_no_pullback_collision()
            # v3.5.0 — adaptive pullback entry (RALLY + RESCUE; flag-gated, DEFAULT OFF)
            self._step_v35_rally_freeze()
            self._step_v35_rescue_freeze()
            self._step_v35_rally_pullback()
            self._step_v35_rally_smooth()
            self._step_v35_rally_timeout()
            self._step_v35_rescue_pullback()
            self._step_v35_rescue_smooth()
            self._step_v35_rescue_timeout()
            self._step_v35_dynamic_sl()
            self._step_v35_separation()
            self._step_v35_5arm_unaffected()
            self._step_v35_cap_unchanged()
            # v3.5.0 regression fixtures from real charts (R1-R6)
            self._step_R1_spike_collapse()
            self._step_R2_pull_continue()
            self._step_R3_rescue_bounce()
            self._step_R4_pump_fade()
            self._step_R5_smooth_runner()
            self._step_R6_chop_skip()
            # ROGUE — self-anchoring monster-rider (flag-gated; demo default ON)
            self._step_rogue_freeze_gate()
            self._step_rogue_detect_monster()
            self._step_rogue_weak_no_slot()
            self._step_rogue_cap_blocks()
            self._step_rogue_early_entry()
            self._step_rogue_adaptive_trail()
            self._step_rogue_loss_stop()
            self._step_rogue_fail_pause()
            self._step_rogue_closure_isolation()
            self._step_rogue_rescue_capped()
            self._step_rogue_rally_reuse()
            self._step_rogue_demo_funded()
            self._step_rogue_tagging()
            self._step_rogue_ride_unlimited()
            # Watchdog boot validator (Task 1)
            self._step_watchdog_safe_start()
            self._step_watchdog_do_not_start()
            # v3.5.0 all-16 features (148-161)
            self._step_f8_pullback_log()
            self._step_f9_boost_ledger()
            self._step_f10_daily_report()
            self._step_f11_preflight()
            self._step_util_no_order_effect()
            self._step_f12_confirm_candle()
            self._step_f13_atr_depth()
            self._step_f14_rescue_sl_wide()
            self._step_f15_boost_telemetry()
            self._step_f16_offset_no_0h()
            self._step_strat_full_freeze()
            self._step_per_flag_indep()
            self._step_rescue_gate_on_arm()
            self._step_flag_table_check()
            # Watchdog rogue promotion-rule line (post-trial)
            self._step_watchdog_rogue_rule()
            # run_live() guaranteed rogue promotion on every live boot
            self._step_rogue_promote_live_boot()
            # Rogue ML pipeline: pattern logger + model gate + archive
            self._step_rogue_ml_gate()
            # Rogue ML: EOD champion/challenger autotrain + exit-feature capture
            self._step_rogue_ml_train()
            # E-12 feed-death watchdog (pure FeedWatchdog; no MT5 needed)
            self._step_f1_feed_resubscribe_after_n()
            self._step_f2_feed_success_resets()
            self._step_f3_feed_alert_then_cooldown()
            self._step_f4_feed_warn_throttled()
            self._step_f5_feed_disabled_byte_identical()
            # E-2/E-3/E-4 Rogue brakes (pure rogue + stubs; no MT5 needed)
            self._step_r1_rogue_record_close()
            self._step_r2_rogue_reentry_allowed()
            self._step_r3_rogue_observe_close()
            self._step_r4_rogue_loss_stop_trips()
            self._step_r5_rogue_eod_flag()
            self._step_r6_rogue_isolation()
            # E-6 boost rides with parent (177-182; pure strategy/trails, no MT5)
            self._step_b1_boost_rides_parent()
            self._step_b2_boost_parent_closed()
            self._step_b3_boost_ride_off_identical()
            self._step_b4_boost_isolation()
            self._step_b5_boost_rescue_unaffected()
            self._step_b6_boost_a1_replay()
            # E-5: Rogue daily loss stop -150 -> -525
            self._step_e5_rogue_stop_525()
            # F-B: trapped-leg capped late-rescue (DEFAULT OFF)
            self._step_fb_trapped_late_rescue()
            # Fix 4: Rogue A1-anchored redesign (NEW ENGINE, DEFAULT OFF)
            self._step_fix4_rogue_a1()
            # selftest auto-summary reporter (report-only)
            self._step_selftest_summary()
            # Rogue manual current-tick seed command
            self._step_rogue_manual_seed()
            # rogueseed command consumed by the live loop (idempotent)
            self._step_rogueseed_consume()
            # E-3 CHAIN: ANY Rogue close re-anchors the A1 redesign at the exit (no dormancy)
            self._step_r7_rogue_e3_chain()
            # P1 "never blind, never brick" — E-13..E-16 + E-12 ladder (pure; no MT5 needed)
            self._step_fix1_rc_retry_brick()
            self._step_fix2_pnl_unresolved()
            self._step_fix3_rogue_gated()
            self._step_fix4_feed_reinit_l3()
            self._step_fix5_restart_recovery()
            # watchdog relaunch policy — relaunch ONLY on exit 42, stop on any other code
            self._step_watchdog_exit_policy()
            # P3 (E-17): Rogue monster-catcher discipline — chase cap + chain gates
            self._step_p3_chase_cap()
            self._step_p3_chain_cooldown()
            self._step_p3_chain_displacement()
            self._step_p3_reversal_exempt()
            self._step_p3_seeds_exempt()
            self._step_p3_gates_off_freeze()
            # Hotfix 2026-07-02: PTRACE BREAK_FAILED spam throttle (logging only)
            self._step_ptrace_break_spam()
            # P4 2026-07-03: W-7 (D-4), E-18, F-B (D-5)
            self._step_d4_override_reevaluates()
            self._step_e18_no_lock_no_advance()
            self._step_fb_bypasses_gate()
            self._step_fb_default_on()
            # P4 2026-07-04: daily P&L report (pnl_report.py)
            self._step_pnl_classify()
            self._step_pnl_boost_join()
            self._step_pnl_whipsaw()
            self._step_pnl_pf_math()
            self._step_pnl_month_rollup()
            self._step_pnl_empty_day()
            self._step_pnl_render_and_ledger()
            self._step_pnl_ledger_idempotent()
            self._step_e19_boot_survives()
            self._step_friday_flatten_gate()
            self._step_friday_a4_a5_skip()
            # R-1: EOD hook passes broker_date explicitly + Rogue closes bucket by
            # IST calendar day, not a raw UTC ts[:10] slice.
            self._step_r1_date_correctness()
            # Branch 2 (V-3): F-B trapped late-rescue log line + ledger kind=FB
            # + hardened ledger-write-failure alerting.
            self._step_fb_silent_fire_logging()
            # D-6: Friday poll-flatten (never single-shot) + a4 default flip +
            # both-engine entry blocking.
            self._step_d6_a4_default_true()
            self._step_d6_poll_until_flat()
            self._step_d6_entries_blocked()
            # v3.6.0: engine switches (/anchors /rogue /engines) + Rogue seed
            # independence (rogue_seed_fallback + seed_source provenance).
            self._step_engine_defaults_wired()
            self._step_anchors_off_manage_only()
            self._step_rogue_off_manage_only()
            self._step_engine_persist_override()
            self._step_seed_fallback_modes()
            self._step_seed_a1_regression()
            self._step_no_midday_reseed()
            self._step_switch_friday_compose()
            self._step_scoped_flatten_confirm()
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
        # AUREON auto-summary: emit a clean pass/fail digest to console + logs/
        # selftest_report.md AFTER the full per-test dump above. Report-only, fully
        # guarded -- it never changes the return value / exit code below.
        try:
            self._emit_summary(ts)
        except Exception as e:
            log.warning(f"selftest summary non-fatal: {e!r}")
        # v3.2.1: telemetry is drained in run()'s finally (single drain point) so
        # the async worker isn't double-stopped here.
        return n_fail == 0

    def _emit_summary(self, ts: str):
        """Emit the AUREON SELFTEST SUMMARY (report-only) from self.results -- to console
        AND logs/selftest_report.md (overwrite; latest only). Taps the EXISTING result
        stream (no re-run). The FAILED count is REAL failures only (status == FAIL); the
        negative tests that log ERROR while PASSing are counted PASS. Live meta (build /
        account / watchdog / rogue) is gathered guardedly; any missing piece degrades to '?'
        and never blocks the summary. Never raises; never affects the exit code."""
        summary = build_selftest_summary(self.results, STEP_NAMES)
        # --- gather live meta, each guarded independently ---
        build = account = server = '?'
        try:
            ti = self.adapter.mt5.terminal_info()
            build = getattr(ti, 'build', '?')
        except Exception:
            pass
        try:
            ai = self.adapter.mt5.account_info()
            account = getattr(ai, 'login', '?')
            server = getattr(ai, 'server', '')
        except Exception:
            pass
        watchdog = '?'
        try:
            import aureon_validator as _v          # the REAL boot check
            watchdog = _v.validate(self.cfg).get('verdict', '?')
        except Exception:
            pass
        rogue = ('PROMOTED ON (demo)' if getattr(self, 'is_demo', False)
                 else 'FORCED OFF (funded)')
        meta = {'build': build, 'ts': ts, 'account': account, 'server': server,
                'watchdog': watchdog, 'rogue': rogue}
        # --- console block (may use glyphs) ---
        console = render_summary(summary, meta, emoji=True)
        print(console, flush=True)
        try:
            log.info("\n" + console)
        except Exception:
            pass
        # --- .md file: SAME content, ASCII PASS/FAIL, utf-8, overwrite (latest only;
        #     history lives in aureon.log). write_selftest_report is fully guarded so a
        #     write failure logs ONE warning and can NEVER fail the suite. ---
        import os as _os
        md = render_summary(summary, meta, emoji=False)
        write_selftest_report(md, _os.path.join(".", "logs", "selftest_report.md"))


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
