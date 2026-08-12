"""aureon_new_non_oco — the section-5 tests.

Pure stdlib (unittest); no pandas / numpy / pytest required. Run standalone:
    python tests/test_aureon_non_oco.py
or via unittest discovery:
    python -m unittest tests.test_aureon_non_oco

Covers, per the task spec section 5:
  * Confirmation: 3 green -> long; 3 red -> short; a doji in the middle resets the
    run; 61 minutes with no confirmation kills the setup; touching the UPPER level
    can still produce a SHORT (direction comes from the candles, not the level).
  * Exit ladder: stop starts at 18, moves to +2.5 at +3, to +10 at +10, then trails
    1.5 behind the peak, and never moves backwards.
  * Chain: reopens at the EXIT PRICE without a new touch, ends on a losing link, ends
    at cap 5, and link 0 bypasses the EMA filter while link >= 1 does not.
  * Regression: with aureon_new_non_oco = False the engine is a pure no-op (no broker
    or notifier interaction, no state) -> the rest of the bot is byte-identical.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aureon_non_oco import (
    AnchorDaySession, AncParams, candle_dir, ladder_sl_offset, ema, drive,
)

T0 = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def cbar(o, c):
    """A candle whose body is o->c with a 1-tick wick either side (for confirmation)."""
    return {"open": o, "high": max(o, c) + 1, "low": min(o, c) - 1, "close": c}


def observing(link=0, params=None):
    """A session forced into OBSERVE (as if a level was just touched), at link `link`."""
    s = AnchorDaySession("A2", 4000.0, params or AncParams(), flat_ts=None)
    s._open_observation(T0)
    s.trades_done = link
    return s


# ======================================================================================
# 1 — CONFIRMATION
# ======================================================================================
class TestConfirmation(unittest.TestCase):
    def _feed(self, s, candles, ema_value=None):
        side = None
        for i, (o, c) in enumerate(candles, start=1):
            out = s.on_m1_close(cbar(o, c), T0 + timedelta(minutes=i), ema_value)
            del out
            side = s.pending_side
        return side

    def test_three_green_is_long(self):
        s = observing()
        self.assertEqual(self._feed(s, [(4000, 4001), (4001, 4002), (4002, 4003)]), "BUY")

    def test_three_red_is_short(self):
        s = observing()
        self.assertEqual(self._feed(s, [(4000, 3999), (3999, 3998), (3998, 3997)]), "SELL")

    def test_doji_in_middle_resets_run(self):
        s = observing()
        # green, green, DOJI, green, green -> no confirm yet (run broken by the doji)
        self._feed(s, [(4000, 4001), (4001, 4002), (4002, 4002), (4002, 4003), (4003, 4004)])
        self.assertIsNone(s.pending_side)
        # one more green completes a fresh 3-run -> long
        s.on_m1_close(cbar(4004, 4005), T0 + timedelta(minutes=6), None)
        self.assertEqual(s.pending_side, "BUY")

    def test_61_minutes_no_confirm_kills_setup(self):
        s = observing()
        # a non-confirming candle at +60m keeps it alive ...
        s.on_m1_close(cbar(4000, 4001), T0 + timedelta(minutes=60), None)
        self.assertFalse(s.done)
        # ... but at +61m with still no 3-run the window expires and the setup dies.
        s.on_m1_close(cbar(4001, 4000), T0 + timedelta(minutes=61), None)
        self.assertTrue(s.done)
        self.assertTrue(s.chain_ended)

    def test_upper_touch_can_produce_short(self):
        # Direction comes from the candles, not the level touched: touch the UPPER level,
        # then three down candles -> SHORT.
        s = AnchorDaySession("A2", 4000.0, AncParams(), flat_ts=None)   # upper 4015
        s.on_m1_close(bar(4012, 4016, 4011, 4014), T0, None)            # touch upper
        self.assertEqual(s.state, "OBSERVE")
        for i, (o, c) in enumerate([(4014, 4013), (4013, 4012), (4012, 4011)], start=1):
            s.on_m1_close(cbar(o, c), T0 + timedelta(minutes=i), None)
        self.assertEqual(s.pending_side, "SELL")

    def test_candle_dir(self):
        self.assertEqual(candle_dir(1, 2), 1)
        self.assertEqual(candle_dir(2, 1), -1)
        self.assertEqual(candle_dir(2, 2), 0)


# ======================================================================================
# 2 — EXIT LADDER
# ======================================================================================
class TestLadder(unittest.TestCase):
    def test_ladder_offsets(self):
        p = AncParams()
        self.assertEqual(ladder_sl_offset(0.0, p), -18.0)     # initial
        self.assertEqual(ladder_sl_offset(2.9, p), -18.0)     # below +3
        self.assertEqual(ladder_sl_offset(3.0, p), 2.5)       # +3 -> lock +2.5
        self.assertEqual(ladder_sl_offset(9.9, p), 2.5)       # still locked
        self.assertEqual(ladder_sl_offset(10.0, p), 10.0)     # +10 -> secure +10
        self.assertAlmostEqual(ladder_sl_offset(12.0, p), 10.5)   # trail 1.5 behind peak
        self.assertAlmostEqual(ladder_sl_offset(20.0, p), 18.5)

    def test_stop_starts_at_18_buy(self):
        s = AnchorDaySession("A2", 4000.0, AncParams())
        s._start_position("BUY", 4000.0, T0)
        self.assertEqual(s.pos["sl_price"], 3982.0)           # entry - 18

    def test_ladder_progression_and_never_backwards_buy(self):
        s = AnchorDaySession("A2", 4000.0, AncParams())
        s._start_position("BUY", 4000.0, T0)
        self.assertEqual(s._ladder(bar(4000, 4003, 3999, 4002)), 4002.5)   # +3 -> +2.5
        self.assertEqual(s._ladder(bar(4002, 4010, 4001, 4009)), 4010.0)   # +10 -> secure
        self.assertEqual(s._ladder(bar(4009, 4014, 4008, 4013)), 4012.5)   # +14 -> trail 1.5
        # a bar that goes nowhere new must NOT pull the stop back
        self.assertIsNone(s._ladder(bar(4013, 4011, 4009, 4010)))
        self.assertEqual(s.pos["sl_price"], 4012.5)

    def test_ladder_progression_sell(self):
        s = AnchorDaySession("A2", 4000.0, AncParams())
        s._start_position("SELL", 4000.0, T0)
        self.assertEqual(s.pos["sl_price"], 4018.0)                          # entry + 18
        self.assertEqual(s._ladder(bar(4000, 4001, 3997, 3998)), 3997.5)     # +3 -> lock
        self.assertEqual(s._ladder(bar(3998, 3999, 3990, 3991)), 3990.0)     # +10 -> secure
        self.assertEqual(s._ladder(bar(3991, 3992, 3986, 3987)), 3987.5)     # +14 -> trail 1.5
        self.assertIsNone(s._ladder(bar(3987, 3989, 3988, 3989)))            # never backwards
        self.assertEqual(s.pos["sl_price"], 3987.5)


# ======================================================================================
# 3 — CHAIN
# ======================================================================================
class TestChain(unittest.TestCase):
    def test_reopens_at_exit_price_without_new_touch(self):
        # Drive link 0 end-to-end to a winning exit, then confirm link 1 with THREE
        # candles alone -- no second touch of anchor +/- 15.
        s = AnchorDaySession("A2", 4000.0, AncParams(), flat_ts=None)   # upper 4015
        m = 0

        def step(b):
            nonlocal m
            m += 1
            return s.on_m1_close(b, T0 + timedelta(minutes=m), None)

        step(bar(4012, 4016, 4011, 4014))                 # touch upper
        step(cbar(4014, 4015)); step(cbar(4015, 4016)); step(cbar(4016, 4017))  # 3 up -> confirm
        step(cbar(4017, 4017))                            # enter link0 at ~4017 open
        self.assertIsNotNone(s.pos)
        self.assertEqual(s.pos["link"], 0)
        entry = s.pos["entry"]
        # push to +3 so the stop locks to +2.5, then dip to that lock -> winning exit
        step(bar(entry, entry + 3, entry - 1, entry + 2))
        lock = s.pos["sl_price"]
        step(bar(entry + 2, entry + 2, lock - 0.5, lock - 0.4))   # dips through the +2.5 lock
        self.assertIsNone(s.pos)                          # link0 closed
        self.assertEqual(len(s.trades), 1)
        self.assertEqual(s.trades[0]["reason"], "lock")
        self.assertGreater(s.trades[0]["pnl_price"], 0)   # a win
        # chain reopened AT THE EXIT PRICE, in OBSERVE (no WAIT_TOUCH), trades_done=1
        self.assertEqual(s.state, "OBSERVE")
        self.assertEqual(s.reopen_price, lock)
        self.assertEqual(s.trades_done, 1)
        # three up candles from here -> link1 confirms with NO new touch
        step(cbar(lock, lock + 1)); step(cbar(lock + 1, lock + 2)); step(cbar(lock + 2, lock + 3))
        self.assertEqual(s.pending_side, "BUY")

    def test_losing_link_ends_chain(self):
        s = observing(link=0)
        s._start_position("BUY", 4000.0, T0)              # trades_done -> 1
        rec = s._close_and_chain(3982.0, T0 + timedelta(minutes=5), "SL")   # exit at -18 = loss
        self.assertEqual(rec["reason"], "stop")
        self.assertLess(rec["pnl_price"], 0)
        self.assertTrue(s.done)
        self.assertTrue(s.chain_ended)

    def test_cap_5_ends_chain(self):
        s = AnchorDaySession("A2", 4000.0, AncParams())
        for i in range(5):
            s._open_observation(T0)
            s._start_position("BUY", 4000.0, T0)
            s._ladder(bar(4000, 4003, 3999, 4002))        # lock +2.5 -> a win
            s._close_and_chain(4002.5, T0, "SL")
            if i < 4:
                self.assertFalse(s.done, f"chain ended early at link {i}")
        self.assertTrue(s.done)                           # capped after the 5th trade
        self.assertEqual(s.trades_done, 5)
        self.assertEqual(len(s.trades), 5)

    def test_link0_bypasses_ema_but_link1_is_filtered(self):
        # An against-trend 3-up run (close far BELOW the EMA -> BUY not aligned).
        run = [(4000, 4001), (4001, 4002), (4002, 4003)]
        high_ema = 5000.0    # price is below the EMA -> a BUY is against the trend

        def confirm(link, ema_value):
            s = observing(link=link)
            side = None
            for i, (o, c) in enumerate(run, start=1):
                side = s._advance_setup(cbar(o, c), T0 + timedelta(minutes=i), ema_value)
            return side

        # link 0 is NEVER filtered -> confirms BUY even against the EMA
        self.assertEqual(confirm(0, high_ema), "BUY")
        # link 1 IS filtered -> the against-trend BUY is rejected
        self.assertIsNone(confirm(1, high_ema))
        # link 1 with an aligned EMA (price above it) confirms normally
        self.assertEqual(confirm(1, 3000.0), "BUY")

    def test_chain_trend_none_disables_filter(self):
        p = AncParams(chain_trend="none")
        s = observing(link=1, params=p)
        side = None
        for i, (o, c) in enumerate([(4000, 4001), (4001, 4002), (4002, 4003)], start=1):
            side = s._advance_setup(cbar(o, c), T0 + timedelta(minutes=i), ema_value=5000.0)
        self.assertEqual(side, "BUY")   # no filter -> enters against the EMA


# ======================================================================================
# 4 — EMA helper
# ======================================================================================
class TestEma(unittest.TestCase):
    def test_ema_none_when_short(self):
        self.assertIsNone(ema([1, 2, 3], 5))

    def test_ema_matches_manual(self):
        # period 3, closes 1..6: SMA seed of first 3 = 2.0, then k=0.5
        # e = 2.0 -> (4)*.5 + 2*.5 = 3.0 -> (5)*.5 + 3*.5 = 4.0 -> (6)*.5 + 4*.5 = 5.0
        self.assertAlmostEqual(ema([1, 2, 3, 4, 5, 6], 3), 5.0)

    def test_ema_flat_series(self):
        self.assertAlmostEqual(ema([7.0] * 50, 10), 7.0)


# ======================================================================================
# 5 — REGRESSION: flag OFF -> engine is a pure no-op
# ======================================================================================
class _SpyAdapter:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _rec(*a, **k):
            self.calls.append((name, a, k))
        return _rec


class _SpyTele:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _rec(*a, **k):
            self.calls.append((name, a, k))
        return _rec


class _Trader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.adapter = _SpyAdapter()
        self.tele = _SpyTele()
        self.paper = False
        self.state = {}


class TestFlagOffRegression(unittest.TestCase):
    def test_default_is_off(self):
        from config import Config
        self.assertIs(Config().aureon_new_non_oco, False)

    def test_drive_is_noop_when_off(self):
        from config import Config
        cfg = Config()
        cfg.aureon_new_non_oco = False
        tr = _Trader(cfg)
        drive(tr)   # must return immediately, touching nothing
        self.assertEqual(tr.adapter.calls, [])
        self.assertEqual(tr.tele.calls, [])
        self.assertFalse(hasattr(tr, "_aurno"))   # no engine state created

    def test_drive_guarded_when_on_with_stub(self):
        # With the flag ON but a bare trader stub, drive() must still not raise
        # (fully guarded) -- it will bail cleanly on the first missing seam.
        from config import Config
        cfg = Config()
        cfg.aureon_new_non_oco = True
        tr = _Trader(cfg)
        try:
            drive(tr)
        except Exception as e:  # pragma: no cover
            self.fail(f"drive() raised with flag on: {e!r}")


def _run_all():
    unittest.main(verbosity=2)


if __name__ == "__main__":
    _run_all()
