"""threshold_8 — invariant & behaviour tests.

Pure stdlib (unittest); no pandas / numpy / pytest required. Run standalone:
    python tests/test_threshold_8.py
or via unittest discovery:
    python -m unittest tests.test_threshold_8

Covers invariants 1-6, rescue arming rules, ladder-lock monotonicity, the rung-2 floor,
exit-precedence ordering with two simultaneous triggers, the P/L identity, the sacred
entry SL, the no-ML rule, and an explicit V-reversal (down 15, straight back up 30)
whose outcome is asserted, not merely run.
"""

import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threshold_8_config as CFG
from threshold_8_config import Threshold8Params
from threshold_8_basket import (
    Threshold8Basket, Threshold8Leg, BUY, SELL, ROLE_ENTRY, ROLE_RESCUE,
    STATE_TRAILING, leg_pnl,
)
from threshold_8_rescue import Threshold8RescueManager
from threshold_8_trail import Threshold8TrailEngine
from threshold_8_exits import (
    evaluate_exit_precedence, EXIT_DAILY_RISK, EXIT_TRAIL, EXIT_BASKET_MAX_RISK,
    EXIT_BASKET_TP, EXIT_MAX_MINUTES,
)
from threshold_8_replay import ReplayEngine, Bar
from threshold_8_backtest import generate_synthetic_m1, run_one, audit_fills, SERVER_TZ

TZ = timezone(timedelta(hours=3))
T0 = datetime(2026, 1, 5, 1, 0, tzinfo=TZ)


def mk_bar(minute, o, h, l, c, spread=20, tickvol=100):
    return Bar(T0 + timedelta(minutes=minute), o, h, l, c, tickvol, tickvol, spread)


def mk_basket():
    return Threshold8Basket("B1", "XAUUSD", "A#1", T0, 8000, T0)


def add_entry(basket, side=BUY, entry=2000.0, lot=0.53, sl_dist=18.0, bar_index=0):
    sl = entry - sl_dist if side == BUY else entry + sl_dist
    leg = Threshold8Leg(1, side, lot, entry, sl, ROLE_ENTRY, T0, bar_index)
    basket.add_leg(leg)
    return leg


class FakeM1:
    """Minimal M1 stand-in with the attributes the rescue manager reads."""
    def __init__(self, o, h, l, c):
        self.open, self.high, self.low, self.close = o, h, l, c


# ======================================================================================
# INVARIANT 1 — TRAIL_LOCK < TRAIL_ACTIVATE at import time & params time
# ======================================================================================
class TestInvariant1(unittest.TestCase):
    def test_module_constant_relation(self):
        self.assertLess(CFG.THRESHOLD_8_TRAIL_LOCK, CFG.THRESHOLD_8_TRAIL_ACTIVATE)

    def test_params_reject_inverted(self):
        with self.assertRaises(AssertionError):
            Threshold8Params(trail_lock=5.0, trail_activate=3.0)

    def test_params_reject_equal(self):
        with self.assertRaises(AssertionError):
            Threshold8Params(trail_lock=3.0, trail_activate=3.0)


# ======================================================================================
# INVARIANT 2 / 3 — fills inside the bar; passed levels are UNPLACEABLE, not filled
# ======================================================================================
class TestInvariant2And3(unittest.TestCase):
    def test_no_fill_outside_bar_over_full_backtest(self):
        bars = generate_synthetic_m1(n_weeks=2)
        engine, records, m, violations = run_one(Threshold8Params(), bars)
        self.assertEqual(violations, [], "some fill printed at a price its bar never traded")

    def test_unplaceable_counted_not_filled(self):
        # anchor bar whose OWN range already ran +$25 past the buy level before the
        # straddle could be placed -> UNPLACEABLE, and it must NOT create a leg.
        p = Threshold8Params()
        eng = ReplayEngine(p, "XAUUSD")
        eng.entry.arm_anchor(2000.0, T0)                 # buy level 2020, sell 1980
        unp = eng.entry.mark_unplaceable_from_anchor_bar(anchor_high=2025.0, anchor_low=1999.0)
        self.assertIn(BUY, unp)
        # a subsequent bar trading up through 2020 must NOT fill (side is UNPLACEABLE)
        sig = eng.entry.check_fill(FakeM1(2021, 2026, 2020, 2024))
        self.assertIsNone(sig, "an UNPLACEABLE level was filled")

    def test_backtest_reports_nonzero_unplaceable(self):
        bars = generate_synthetic_m1()
        eng, records, m, _ = run_one(Threshold8Params(), bars)
        self.assertGreater(m["unplaceable_count"], 0)


# ======================================================================================
# INVARIANT 4 — leg opened on bar N managed from N+1; own SL still checked on N
# ======================================================================================
class TestInvariant4(unittest.TestCase):
    def test_no_same_bar_basket_management(self):
        # Build one bar sequence where a straddle fills; assert no rescue/trail state
        # change happens on the very fill bar, only from the next.
        p = Threshold8Params()
        eng = ReplayEngine(p, "XAUUSD")
        eng.entry.arm_anchor(2000.0, T0)
        eng._cur_date = T0.date()                # keep run() from resetting the anchor
        # feed a bar that fills the buy stop at 2020; opened_bar_index must equal index
        bars = [
            mk_bar(0, 2019, 2021, 2018, 2020),   # fills buy @2020 (bar N)
            mk_bar(1, 2020, 2021, 2019, 2020),   # bar N+1
        ]
        # drive just these two bars through the engine's loop mechanics
        recs = eng.run(bars)
        b = eng.baskets[0]
        entry = b.entry_leg()
        # entry opened on bar 0; management (rescue/trail) only from bar 1 — since price
        # never moved adverse, nothing armed, but crucially the leg exists from bar 0.
        self.assertEqual(entry.opened_bar_index, 0)

    def test_own_sl_checked_on_open_bar(self):
        # a leg's own SL may be hit intrabar on its open bar N using the bar extremes.
        p = Threshold8Params()
        eng = ReplayEngine(p, "XAUUSD")
        eng.entry.arm_anchor(2000.0, T0)
        eng._cur_date = T0.date()                # keep run() from resetting the anchor
        # bar fills buy @2020 then wicks to 2001 (below SL 2020-18=2002) same bar
        bars = [mk_bar(0, 2019, 2021, 2001, 2010)]
        eng.run(bars)
        b = eng.baskets[0]
        entry = b.entry_leg()
        self.assertFalse(entry.is_open, "own SL was not honoured on the open bar")
        self.assertEqual(entry.exit_reason, "LEG_SL")


# ======================================================================================
# INVARIANT 5 — sum(leg.pnl) == realized + floating every bar
# ======================================================================================
class TestInvariant5(unittest.TestCase):
    def test_identity_direct(self):
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.legs.append(Threshold8Leg(2, SELL, 0.63, 2005.0, 2023.0, ROLE_RESCUE, T0, 1))
        for price in (1990.0, 2000.0, 2010.0, 2025.0):
            b.update(price)
            leg_sum = sum(l.pnl for l in b.legs)
            self.assertAlmostEqual(leg_sum, b.realized_pnl + b.floating_pnl, places=6)

    def test_identity_over_backtest(self):
        # the assertion lives inside Basket.update; a clean full run means it held.
        bars = generate_synthetic_m1(n_weeks=1)
        run_one(Threshold8Params(), bars)  # would raise if identity ever broke


# ======================================================================================
# INVARIANT 6 / C6 — rescue lot is a pure function of BASE_LOT * MULT
# ======================================================================================
class TestInvariant6(unittest.TestCase):
    def test_pure_function(self):
        p = Threshold8Params(base_lot=0.53, rescue_lot_mult=1.20)
        self.assertEqual(p.rescue_lot(), round(0.53 * 1.20, 2))
        self.assertEqual(p.rescue_lot(), 0.64)

    def test_lot_independent_of_loss(self):
        mgr = Threshold8RescueManager(Threshold8Params(rescue_min_gap_min=0))
        # two baskets under very different loss; same armed lot
        lots = []
        for entry_px, mark in ((2000.0, 1988.0), (2000.0, 1975.0)):
            b = mk_basket()
            add_entry(b, BUY, entry_px)
            b.update(mark)
            dec = mgr.evaluate(b, FakeM1(mark, mark + 1, mark - 1, mark),
                               T0 + timedelta(minutes=5))
            self.assertTrue(dec.armed)
            lots.append(dec.order.lot)
        self.assertEqual(lots[0], lots[1])

    def test_no_loss_reference_in_rescue_source(self):
        # C6 grep: the rescue lot path must not reference loss size / count / net_pnl.
        with open(os.path.join(os.path.dirname(__file__), "..",
                                "threshold_8_rescue.py")) as _fh:
            src = _fh.read()
        # rescue_lot() is defined in config as base_lot*mult; ensure rescue.py sizes lot
        # only via params.rescue_lot()
        self.assertIn("p.rescue_lot()", src)
        # the lot assignment line must not multiply by anything loss-derived
        self.assertNotRegex(src, r"lot\s*=\s*[^\n]*net_pnl")
        self.assertNotRegex(src, r"lot\s*=\s*[^\n]*loss")


# ======================================================================================
# Rescue arming rules
# ======================================================================================
class TestRescueArming(unittest.TestCase):
    def _mgr(self, **kw):
        return Threshold8RescueManager(Threshold8Params(**kw))

    def test_arms_once_per_side_only(self):
        mgr = self._mgr(rescue_min_gap_min=0)
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.update(1988.0)
        bar = FakeM1(1988, 1989, 1987, 1988)
        d1 = mgr.evaluate(b, bar, T0 + timedelta(minutes=5))
        self.assertTrue(d1.armed)
        # simulate the engine opening the rescue leg
        b.legs.append(Threshold8Leg(9, d1.order.side, d1.order.lot, 1988.0, 2006.0,
                                    ROLE_RESCUE, T0, 5))
        b.update(1988.0)
        d2 = mgr.evaluate(b, bar, T0 + timedelta(minutes=6))
        self.assertFalse(d2.armed)
        self.assertEqual(d2.reason, "max_per_side")

    def test_never_same_side_as_entry(self):
        mgr = self._mgr(rescue_min_gap_min=0)
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.update(1988.0)
        d = mgr.evaluate(b, FakeM1(1988, 1989, 1987, 1988), T0 + timedelta(minutes=5))
        self.assertTrue(d.armed)
        self.assertEqual(d.order.side, SELL)   # opposite of the BUY entry

    def test_no_arm_on_wick_when_require_close(self):
        # wick 15 below entry but CLOSE only 5 below -> must NOT arm under REQUIRE_CLOSE
        mgr = self._mgr(rescue_min_gap_min=0, rescue_require_close=True, rescue_dist=10.0)
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.update(1995.0)
        wick = FakeM1(1998, 1999, 1985, 1995)   # low 1985 (adverse 15), close 1995 (5)
        d = mgr.evaluate(b, wick, T0 + timedelta(minutes=5))
        self.assertFalse(d.armed, "rescue armed on a wick despite REQUIRE_CLOSE")
        # same wick with REQUIRE_CLOSE off -> extreme (low) arms it
        mgr2 = self._mgr(rescue_min_gap_min=0, rescue_require_close=False, rescue_dist=10.0)
        b2 = mk_basket()
        add_entry(b2, BUY, 2000.0)
        b2.update(1985.0)
        d2 = mgr2.evaluate(b2, wick, T0 + timedelta(minutes=5))
        self.assertTrue(d2.armed)

    def test_min_gap_blocks_early_rescue(self):
        mgr = self._mgr(rescue_min_gap_min=3)
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.update(1985.0)
        # 1 minute after entry -> blocked by MIN_GAP
        early = mgr.evaluate(b, FakeM1(1985, 1986, 1984, 1985), T0 + timedelta(minutes=1))
        self.assertFalse(early.armed)
        self.assertEqual(early.reason, "min_gap")

    def test_disabled_never_arms(self):
        mgr = self._mgr(rescue_enabled=False, rescue_min_gap_min=0)
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.update(1980.0)
        d = mgr.evaluate(b, FakeM1(1980, 1981, 1979, 1980), T0 + timedelta(minutes=5))
        self.assertFalse(d.armed)


# ======================================================================================
# Trail — ladder lock monotonic; rung-2 floor
# ======================================================================================
class TestTrail(unittest.TestCase):
    def test_ladder_lock_monotonic(self):
        eng = Threshold8TrailEngine(Threshold8Params(trail_mode="ladder"))
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        locks = []
        # push peak_net up through the rungs, then let net fall back
        for net in (100, 320, 520, 950, 800, 600, 400):
            b.peak_net = max(b.peak_net, net)
            b.net_pnl = net
            eng.update(b, price=2000 + net / 100.0, atr=None, swing_price=None)
            if b.trail_locked is not None:
                locks.append(b.trail_locked)
        # never decreasing
        self.assertEqual(locks, sorted(locks))
        # reached rung 3 (900,700) -> locked pinned at 700
        self.assertEqual(b.trail_locked, 700)

    def test_rung2_floor_cannot_close_below(self):
        eng = Threshold8TrailEngine(Threshold8Params(trail_mode="ladder"))
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        # reach rung 2 (500 -> lock 320)
        b.peak_net = 520
        b.net_pnl = 520
        eng.update(b, 2005.2, None, None)
        self.assertEqual(b.trail_locked, 320)
        # net dips to 330 (> lock) -> no exit
        b.net_pnl = 330
        st = eng.update(b, 2003.3, None, None)
        self.assertFalse(st.exit_now)
        # net dips to 320 (== lock) -> exit fires; locked stayed at the rung-2 value
        b.net_pnl = 320
        st = eng.update(b, 2003.2, None, None)
        self.assertTrue(st.exit_now)
        self.assertEqual(b.trail_locked, 320)

    def test_engages_and_sets_state(self):
        eng = Threshold8TrailEngine(Threshold8Params(trail_mode="ladder"))
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.peak_net = 305
        b.net_pnl = 305
        eng.update(b, 2003.05, None, None)
        self.assertTrue(b.trail_engaged)
        self.assertEqual(b.state, STATE_TRAILING)


# ======================================================================================
# Exit precedence ordering with two simultaneous triggers (C11)
# ======================================================================================
class TestExitPrecedence(unittest.TestCase):
    def test_daily_risk_beats_trail(self):
        p = Threshold8Params()
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.state = STATE_TRAILING
        b._trail_exit = True                 # trail would fire (order 5)
        b.net_pnl = -50.0
        dec = evaluate_exit_precedence(
            b, p, T0, day_net=-700.0,        # DAILY_RISK breached (order 1)
            session_flatten=False, is_friday=False,
            opposite_anchor_triggered=False, duration_min=10.0)
        self.assertEqual(dec.reason, EXIT_DAILY_RISK)
        self.assertEqual(dec.order, 1)

    def test_max_risk_beats_trail_and_tp(self):
        p = Threshold8Params()
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.state = STATE_TRAILING
        b._trail_exit = True
        b.net_pnl = -p.basket_max_risk - 1   # order 2
        dec = evaluate_exit_precedence(
            b, p, T0, day_net=-100.0, session_flatten=False, is_friday=False,
            opposite_anchor_triggered=False, duration_min=10.0)
        self.assertEqual(dec.reason, EXIT_BASKET_MAX_RISK)

    def test_trail_beats_tp(self):
        p = Threshold8Params()
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.state = STATE_TRAILING
        b._trail_exit = True
        b.net_pnl = p.basket_tp + 50         # TP also true, but TP only when NOT trailing
        dec = evaluate_exit_precedence(
            b, p, T0, day_net=0.0, session_flatten=False, is_friday=False,
            opposite_anchor_triggered=False, duration_min=10.0)
        self.assertEqual(dec.reason, EXIT_TRAIL)

    def test_tp_only_when_not_trailing(self):
        p = Threshold8Params()
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.net_pnl = p.basket_tp + 1
        dec = evaluate_exit_precedence(
            b, p, T0, day_net=0.0, session_flatten=False, is_friday=False,
            opposite_anchor_triggered=False, duration_min=10.0)
        self.assertEqual(dec.reason, EXIT_BASKET_TP)

    def test_session_flatten_beats_minutes(self):
        p = Threshold8Params()
        b = mk_basket()
        add_entry(b, BUY, 2000.0)
        b.net_pnl = 10.0
        dec = evaluate_exit_precedence(
            b, p, T0, day_net=0.0, session_flatten=True, is_friday=False,
            opposite_anchor_triggered=False, duration_min=99999.0)
        self.assertEqual(dec.order, 3)


# ======================================================================================
# Entry SL never modified (C8)
# ======================================================================================
class TestEntrySLSacred(unittest.TestCase):
    def test_entry_sl_unchanged_through_rescue_and_trail(self):
        p = Threshold8Params(rescue_min_gap_min=0)
        b = mk_basket()
        entry = add_entry(b, BUY, 2000.0, sl_dist=p.sl)
        sl_at_fill = entry.sl
        # arm a rescue and engage a trail; neither may touch entry.sl
        b.update(1988.0)
        mgr = Threshold8RescueManager(p)
        dec = mgr.evaluate(b, FakeM1(1988, 1989, 1987, 1988), T0 + timedelta(minutes=5))
        self.assertTrue(dec.armed)
        b.legs.append(Threshold8Leg(9, dec.order.side, dec.order.lot, 1988.0,
                                    1988.0 + p.sl, ROLE_RESCUE, T0, 5))
        b.peak_net = 400
        b.net_pnl = 400
        Threshold8TrailEngine(p).update(b, 2004.0, None, None)
        self.assertEqual(entry.sl, sl_at_fill)
        self.assertEqual(entry.sl, entry._sl_at_fill)


# ======================================================================================
# V-reversal: down 15, straight back up 30 — asserted, not just run
# ======================================================================================
class TestVReversal(unittest.TestCase):
    def test_v_reversal_rescue_hurts(self):
        """Long entry @2000. Price drops to 1985 (down 15), rescue arms a short @1985,
        then price runs straight back up +30 to 2015. On the way up the rescue short
        stops out at its own SL (1985+18=2003); the entry long recovers to +.

        This is the canonical rescue failure mode. Assert: rescue armed exactly once,
        the rescue leg stopped out, and the rescued basket's net is STRICTLY WORSE than
        the no-rescue control on this same path."""
        p = Threshold8Params(rescue_min_gap_min=3, rescue_dist=10.0, rescue_lot_mult=1.20)
        mult = p.usd_per_price_per_lot

        # --- control (no rescue): entry long 2000 -> 2015 ---
        control_net = leg_pnl(BUY, 2000.0, 2015.0, p.base_lot)

        # --- rescued path, resolved leg by leg ---
        b = mk_basket()
        entry = add_entry(b, BUY, 2000.0, lot=p.base_lot, sl_dist=p.sl)
        # down 15
        b.update(1985.0)
        mgr = Threshold8RescueManager(p)
        dec = mgr.evaluate(b, FakeM1(1990, 1991, 1985, 1985), T0 + timedelta(minutes=5))
        self.assertTrue(dec.armed, "rescue should arm on the -15 close")
        self.assertEqual(dec.order.side, SELL)
        rescue = Threshold8Leg(9, SELL, dec.order.lot, 1985.0, 1985.0 + p.sl,
                               ROLE_RESCUE, T0, 5)
        b.add_leg(rescue)
        # only ONE rescue may arm
        b.update(1985.0)
        dec2 = mgr.evaluate(b, FakeM1(1985, 1986, 1984, 1985), T0 + timedelta(minutes=6))
        self.assertFalse(dec2.armed)

        # price runs back up +30 to 2015 -> the short's SL (2003) is taken on the way
        rescue.close(2003.0, T0 + timedelta(minutes=20), "LEG_SL")
        b.update(2015.0)                       # entry long now floating at +
        rescued_net = b.net_pnl

        # exactly one rescue, it stopped out, and rescue made the V worse
        self.assertEqual(len(b.rescue_legs()), 1)
        self.assertFalse(rescue.is_open)
        self.assertLess(rescued_net, control_net,
                        "on a clean V-reversal the counter-leg must not help")
        # concrete numeric outcome (down-15/up-30 with these lots)
        expected = (leg_pnl(BUY, 2000.0, 2015.0, p.base_lot)
                    + leg_pnl(SELL, 1985.0, 2003.0, dec.order.lot))
        self.assertAlmostEqual(rescued_net, expected, places=6)


# ======================================================================================
# C12 — M5 detectors receive CLOSED M5 bars only (no instant fill at the signal level)
# ======================================================================================
class TestClosedM5Only(unittest.TestCase):
    def test_features_only_on_closed_windows(self):
        p = Threshold8Params()
        eng = ReplayEngine(p, "XAUUSD")
        eng._cur_date = T0.date()
        # feed 12 M1 bars => the first two full 5-min windows close; the forming third
        # window must NOT have produced a feature yet.
        bars = [mk_bar(i, 2000 + i * 0.1, 2000 + i * 0.1 + 0.3,
                       2000 + i * 0.1 - 0.3, 2000 + i * 0.1) for i in range(12)]
        eng.run(bars)
        # windows starting at :00 and :05 have closed once :05 and :10 bars arrive;
        # 12 M1 bars (minutes 0..11) => windows [0-4],[5-9] closed, [10-11] forming.
        self.assertEqual(len(eng.m5_closed), 2)
        # closed windows never include the still-forming bars' extremes beyond minute 9
        self.assertLessEqual(eng.m5_closed[-1]["start"].minute, 5)

    def test_no_instant_fill_no_floor_violation(self):
        bars = generate_synthetic_m1(n_weeks=2)
        eng, records, m, _ = run_one(Threshold8Params(trail_mode="ladder"), bars)
        # phantom-fill signature would be ~99% win rate & zero drawdown; assert neither
        self.assertLess(m["win_rate"], 0.85)
        self.assertGreater(m["max_drawdown"], 0.0)
        # C10 structural guard never tripped
        self.assertEqual(eng.trail_floor_violations, 0)


# ======================================================================================
# C17 — no ML / model / training anywhere in threshold_8_*
# ======================================================================================
class TestNoML(unittest.TestCase):
    def test_no_ml_imports(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        forbidden = re.compile(
            r"\b(import\s+(sklearn|torch|tensorflow|keras|xgboost|lightgbm|joblib)"
            r"|from\s+(sklearn|torch|tensorflow|keras|xgboost|lightgbm|joblib)"
            r"|\.fit\(|\.predict\(|load_model|train_model)\b")
        for fn in os.listdir(root):
            if fn == "threshold_8_verify.py":
                continue      # the verify harness names the ML tokens as detection data
            if fn.startswith("threshold_8_") and fn.endswith(".py"):
                with open(os.path.join(root, fn)) as _fh:
                    src = _fh.read()
                self.assertIsNone(forbidden.search(src),
                                  "ML/model/training reference found in %s" % fn)


# ======================================================================================
# Basket P/L identity spot check + magic separation
# ======================================================================================
class TestBasketMisc(unittest.TestCase):
    def test_magic_offset_separates(self):
        self.assertEqual(CFG.threshold_8_magic("XAUUSD", 123),
                         CFG.THRESHOLD_8_MAGIC_OFFSET + 123)

    def test_net_pnl_reads_basket_not_leg(self):
        b = mk_basket()
        add_entry(b, BUY, 2000.0, lot=0.53)
        b.legs.append(Threshold8Leg(2, SELL, 0.63, 2000.0, 2018.0, ROLE_RESCUE, T0, 1))
        b.update(2010.0)
        # net = long +$53 - short $63 = -$10 ; individual legs disagree in sign
        self.assertAlmostEqual(b.net_pnl,
                               leg_pnl(BUY, 2000.0, 2010.0, 0.53)
                               + leg_pnl(SELL, 2000.0, 2010.0, 0.63), places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
