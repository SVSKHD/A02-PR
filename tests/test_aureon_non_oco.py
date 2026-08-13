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


# ======================================================================================
# Section-5 tests for the Discord CONTROL SURFACE (message identity + `!nno`).
# These cover the ops layer only; the strategy tests above are unchanged.
# ======================================================================================
import json as _json

import aureon_non_oco as _a
import discord_cards as _dc
from config import Config


class _Pos:
    """Minimal MT5-position stand-in (fields the command surface reads)."""
    def __init__(self, magic, ticket, typ=1, profit=0.0, price_open=4400.0,
                 price_current=4410.0, sl=4382.0, volume=0.10):
        self.magic = magic
        self.ticket = ticket
        self.type = typ            # 0 = buy, 1 = sell
        self.profit = profit
        self.price_open = price_open
        self.price_current = price_current
        self.sl = sl
        self.volume = volume


class _MT5:
    def __init__(self, positions):
        self._positions = positions

    def positions_get(self, **kw):
        # ticket=... queries (for _position_open) return truthy => "still open".
        if "ticket" in kw:
            return [object()]
        return list(self._positions)


class _RecAdapter:
    """Records the magic-scoped broker calls the surface makes."""
    def __init__(self, positions):
        self.mt5 = _MT5(positions)
        self.closed = []
        self.modified = []
        self.placed = []

    def close_position(self, ticket, dry_run=False):
        self.closed.append(int(ticket))

    def modify_position_sl(self, ticket, sl, dry_run=False):
        self.modified.append((ticket, sl))

    def place_market_order(self, *a, **k):
        self.placed.append((a, k))
        return {"ticket": 4242, "price": k.get("sl")}


class _CapTele:
    def __init__(self):
        self.sent = []      # (msg, card) tuples

    def send(self, msg, severity=None, card=None, **kw):
        self.sent.append((msg, card))

    # convenience wrappers, in case anything routes through them
    def _rec(self, msg, *a, **k):
        self.sent.append((msg, None))
    info = warn = success = error = critical = debug = _rec


class _CmdTrader:
    def __init__(self, positions, paper=False):
        self.cfg = Config()
        self.cfg.aureon_new_non_oco = True
        self.adapter = _RecAdapter(positions)
        self.tele = _CapTele()
        self.paper = paper
        self.state = {}
        self.save_count = 0

    def _save_state(self):
        self.save_count += 1


class TestNnoMessageIdentity(unittest.TestCase):
    """Section 5: notification embeds carry the NEW NON-OCO title, the configured
    colour, the [NNO] prefix, the anchor label, and the link index."""

    def _capture(self, tone, label, link, text):
        tr = _CmdTrader([])
        _a._emit(tr, tone, label, link, text)   # no drive() buffer => posts now
        self.assertEqual(len(tr.tele.sent), 1)
        msg, card = tr.tele.sent[0]
        return msg, card

    def test_title_emoji_and_prefix(self):
        cfg = Config()
        msg, card = self._capture("normal", "A2", 0, "SELL 0.10 @ 4416.20 sl 4434.20")
        self.assertEqual(card["title"], f"{cfg.nno_embed_emoji} {cfg.nno_embed_title}")
        self.assertNotIn("AUREON INFO", card["title"])
        self.assertTrue(msg.startswith(cfg.nno_notify_prefix))
        self.assertIn("A2", msg)
        self.assertIn("link 0", msg)

    def test_tone_colours(self):
        cfg = Config()
        _, c_norm = self._capture("normal", "A2", 0, "x")
        _, c_warn = self._capture("warn", "A2", 1, "x")
        _, c_bad = self._capture("bad", "A2", 2, "x")
        self.assertEqual(c_norm["color"], cfg.nno_embed_colour)
        self.assertEqual(c_warn["color"], cfg.nno_embed_colour_warn)
        self.assertEqual(c_bad["color"], cfg.nno_embed_colour_bad)

    def test_batch_keeps_identity(self):
        cfg = Config()
        tr = _CmdTrader([])
        tr._aurno_batch = []
        _a._emit(tr, "normal", "A2", 0, "locked +2.5")
        _a._emit(tr, "normal", "A2", 0, "secured +10, trailing 1.5")
        buf = tr._aurno_batch
        tr._aurno_batch = None
        _a._flush_nno(tr, buf)
        # one coalesced card, still NEW NON-OCO title + teal colour (never a plain msg)
        self.assertEqual(len(tr.tele.sent), 1)
        _, card = tr.tele.sent[0]
        self.assertEqual(card["title"], f"{cfg.nno_embed_emoji} {cfg.nno_embed_title}")
        self.assertEqual(card["color"], cfg.nno_embed_colour)
        self.assertIn("locked", card["description"])
        self.assertIn("secured", card["description"])


class TestOtherEnginesUnchanged(unittest.TestCase):
    """Section 5: the straddle's / ROGUE's / FETCHER's identity is untouched — same
    generic title + colour map as before, and DISTINCT from NEW NON-OCO."""

    def test_severity_colour_map_intact(self):
        self.assertEqual(_dc.SEVERITY_COLOR["INFO"], _dc.BLUE)
        self.assertEqual(_dc.SEVERITY_COLOR["SUCCESS"], _dc.GREEN)
        self.assertEqual(_dc.SEVERITY_COLOR["WARN"], _dc.AMBER)

    def test_generic_card_still_aureon_info(self):
        card = _dc.card_generic("AUREON INFO", "a fill", _dc.BLUE)
        self.assertEqual(card["title"], "AUREON INFO")
        self.assertEqual(card["color"], _dc.BLUE)

    def test_nno_is_distinct_from_straddle(self):
        cfg = Config()
        # different title AND different colour from the straddle's blue AUREON INFO
        self.assertNotEqual(cfg.nno_embed_title, "AUREON INFO")
        self.assertNotEqual(cfg.nno_embed_colour, _dc.BLUE)
        self.assertNotIn(cfg.nno_embed_colour,
                         (_dc.BLUE, _dc.GREEN, _dc.GREY))


class TestNnoFlatMagicIsolation(unittest.TestCase):
    """Section 5 (most important): `!nno flat` closes ONLY magic 20260811."""

    def test_flat_confirm_closes_only_aurno_magic(self):
        ours = _Pos(_a.AURNO_MAGIC, 111, profit=132.5)
        anchor = _Pos(20260522, 999, profit=-50.0)     # anchor leg — must survive
        rogue = _Pos(20260626, 888, profit=10.0)       # ROGUE leg — must survive
        fetch = _Pos(20260707, 777, profit=1.0)        # FETCHER leg — must survive
        tr = _CmdTrader([ours, anchor, rogue, fetch])
        # step 1: preview arms the confirm window, closes nothing
        _a.handle_command(tr, "flat", confirm=False)
        self.assertEqual(tr.adapter.closed, [])
        # step 2: confirm closes ONLY 111
        _a.handle_command(tr, "flat", confirm=True)
        self.assertEqual(tr.adapter.closed, [111])
        for foreign in (999, 888, 777):
            self.assertNotIn(foreign, tr.adapter.closed)

    def test_flat_confirm_without_preview_is_refused(self):
        ours = _Pos(_a.AURNO_MAGIC, 111, profit=1.0)
        tr = _CmdTrader([ours])
        # a confirm with no prior preview (no armed window) must NOT close anything
        _a.handle_command(tr, "flat", confirm=True)
        self.assertEqual(tr.adapter.closed, [])

    def test_flat_confirm_expired_is_refused(self):
        ours = _Pos(_a.AURNO_MAGIC, 111, profit=1.0)
        tr = _CmdTrader([ours])
        _a.handle_command(tr, "flat", confirm=False)      # arm
        tr._nno_flat_pending_ts -= (tr.cfg.nno_discord_flat_confirm_sec + 5)
        _a.handle_command(tr, "flat", confirm=True)        # too late
        self.assertEqual(tr.adapter.closed, [])


class TestNnoPauseBlocksEntriesButLadders(unittest.TestCase):
    """Section 5: `!nno pause` blocks NEW entries + NEW chain links, while an
    already-open position still ratchets its SL through the ladder."""

    def _params(self):
        return AncParams()

    def test_open_position_still_ladders_while_blocked(self):
        p = self._params()
        sess = AnchorDaySession("A2", 4400.0, p, flat_ts=None)
        sess.enter_live("BUY", 4400.0, T0)     # open a position, sl at 4382
        sess.open_ticket = 111
        tr = _CmdTrader([_Pos(_a.AURNO_MAGIC, 111)], paper=False)
        # a bar whose high reaches +4 favourable => ladder locks to +2.5 (sl 4402.5)
        fav_bar = bar(4400.0, 4404.0, 4399.9, 4403.0)
        _a._manage_session_live(tr, sess, fav_bar, T0, None,
                                allow_new_entries=False, p=p)
        # SL ratcheted despite entries being blocked
        self.assertTrue(tr.adapter.modified, "ladder must run while paused")
        self.assertEqual(tr.adapter.modified[-1][1], 4402.5)
        # and NO new order was placed
        self.assertEqual(tr.adapter.placed, [])

    def test_no_new_entry_and_confirmation_not_consumed_while_blocked(self):
        p = self._params()
        sess = AnchorDaySession("A2", 4400.0, p, flat_ts=None)
        # prime OBSERVE one candle short of a 3-up confirmation
        sess._open_observation(T0)
        sess.run_dir, sess.run_len = 1, 2
        tr = _CmdTrader([], paper=False)
        up = cbar(4400.0, 4401.0)              # would be the 3rd up candle => confirm
        _a._manage_session_live(tr, sess, up, T0, None,
                                allow_new_entries=False, p=p)
        # blocked path returns before poll_setup: no order, run not consumed
        self.assertEqual(tr.adapter.placed, [])
        self.assertEqual(sess.run_len, 2)
        self.assertEqual(sess.state, "OBSERVE")


class TestNnoPausePersistence(unittest.TestCase):
    """Section 5: pause state round-trips through the state file."""

    def test_pause_sets_and_persists_flag(self):
        tr = _CmdTrader([])
        _a.handle_command(tr, "pause")
        self.assertTrue(tr.state.get("nno_paused"))
        self.assertGreaterEqual(tr.save_count, 1)      # _save_state was called
        # the flag survives a state-file round-trip (json is what _save_state writes)
        reloaded = _json.loads(_json.dumps(tr.state))
        self.assertTrue(reloaded.get("nno_paused"))
        # resume clears it and persists again
        _a.handle_command(tr, "resume")
        self.assertFalse(tr.state.get("nno_paused"))
        self.assertFalse(_json.loads(_json.dumps(tr.state)).get("nno_paused"))


class TestNnoCommandsNoopWhenOff(unittest.TestCase):
    """Section 5: command handlers are no-ops when aureon_new_non_oco = False."""

    def test_all_subcommands_noop_when_flag_off(self):
        ours = _Pos(_a.AURNO_MAGIC, 111, profit=5.0)
        tr = _CmdTrader([ours])
        tr.cfg.aureon_new_non_oco = False           # master flag OFF
        # even a flat confirm must touch nothing
        tr._nno_flat_pending_ts = _a._now_wall()
        for sub in ("status", "anchors", "positions", "today", "config", "help",
                    "pause", "resume", "flat"):
            _a.handle_command(tr, sub, confirm=True)
        self.assertEqual(tr.tele.sent, [])          # no notifier interaction
        self.assertEqual(tr.adapter.closed, [])     # no broker interaction
        self.assertEqual(tr.state, {})              # no state mutation

    def test_commands_noop_when_surface_disabled(self):
        tr = _CmdTrader([])
        tr.cfg.nno_discord_commands_enabled = False
        _a.handle_command(tr, "status")
        self.assertEqual(tr.tele.sent, [])


# ======================================================================================
# 6 — v3.10.1 funded port: anc_confirm ("candles" | "closes")
# ======================================================================================
class TestConfirmModes(unittest.TestCase):
    def _feed(self, s, candles, ema_value=None):
        for i, (o, c) in enumerate(candles, start=1):
            s.on_m1_close(cbar(o, c), T0 + timedelta(minutes=i), ema_value)
        return s.pending_side

    # -- REGRESSION GUARD (the important one): "candles" == today's exact behaviour --
    def test_candles_default_in_config(self):
        self.assertEqual(Config().anc_confirm, "candles")
        self.assertEqual(Config().anc_closes_n, 1)
        self.assertEqual(AncParams.from_cfg(Config()).confirm, "candles")

    def test_candles_reproduces_three_green_long(self):
        s = observing(params=AncParams(confirm="candles"))
        self.assertEqual(self._feed(s, [(4000, 4001), (4001, 4002), (4002, 4003)]), "BUY")

    def test_candles_reproduces_three_red_short(self):
        s = observing(params=AncParams(confirm="candles"))
        self.assertEqual(self._feed(s, [(4000, 3999), (3999, 3998), (3998, 3997)]), "SELL")

    def test_candles_doji_still_resets(self):
        s = observing(params=AncParams(confirm="candles"))
        self._feed(s, [(4000, 4001), (4001, 4002), (4002, 4002), (4002, 4003)])
        self.assertIsNone(s.pending_side)      # doji broke the run -> no confirm yet

    # -- "closes" link 0: one close beyond the TOUCHED level, in the close's direction --
    def test_closes_link0_long_above_upper(self):
        p = AncParams(confirm="closes", closes_n=1)
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)          # upper 4015
        s.on_m1_close(bar(4013, 4016, 4012, 4014), T0, None)         # touch upper
        self.assertEqual(s.state, "OBSERVE")
        self.assertEqual(s.touch_level, 4015.0)
        s.on_m1_close(bar(4014, 4018, 4013, 4017), T0 + timedelta(minutes=1), None)
        self.assertEqual(s.pending_side, "BUY")                      # close 4017 > 4015

    def test_closes_link0_short_off_upper_level(self):
        # Upper level touched but the close is BELOW it -> SHORT off the upper level.
        p = AncParams(confirm="closes", closes_n=1)
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)
        s.on_m1_close(bar(4013, 4016, 4012, 4014), T0, None)         # touch upper 4015
        s.on_m1_close(bar(4014, 4014, 4010, 4011), T0 + timedelta(minutes=1), None)
        self.assertEqual(s.pending_side, "SELL")                     # close 4011 < 4015

    def test_closes_exactly_equal_is_no_signal(self):
        p = AncParams(confirm="closes", closes_n=1)
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)
        s._open_observation(T0)
        s.touch_level = 4015.0
        s.on_m1_close(bar(4014, 4016, 4013, 4015), T0 + timedelta(minutes=1), None)
        self.assertIsNone(s.pending_side)                            # close == ref -> wait

    def test_closes_link1_ref_is_prev_exit_not_level(self):
        # Chained link (>=1) measures off the PREVIOUS exit fill price, not the level.
        p = AncParams(confirm="closes", closes_n=1)
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)          # upper 4015
        s._open_observation(T0, reopen_price=4050.0)                 # chain reopened at 4050
        s.trades_done = 1
        # close 4020 is ABOVE the level (4015) but BELOW the exit (4050): ref=exit -> SELL.
        s.on_m1_close(bar(4019, 4021, 4018, 4020), T0 + timedelta(minutes=1), None)
        self.assertEqual(s.pending_side, "SELL")

    def test_closes_n2_needs_two_consecutive_other_side_resets(self):
        p = AncParams(confirm="closes", closes_n=2)
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)
        s._open_observation(T0)
        s.touch_level = 4015.0
        s.on_m1_close(bar(4014, 4018, 4013, 4017), T0 + timedelta(minutes=1), None)  # >ref: 1 up
        self.assertIsNone(s.pending_side)                            # need 2
        s.on_m1_close(bar(4017, 4017, 4010, 4011), T0 + timedelta(minutes=2), None)  # <ref: reset
        self.assertIsNone(s.pending_side)
        s.on_m1_close(bar(4011, 4011, 4008, 4009), T0 + timedelta(minutes=3), None)  # <ref: 2 down
        self.assertEqual(s.pending_side, "SELL")


# ======================================================================================
# 7 — v3.10.1 funded port: SL-avoidance opposite-candle flip / scratch (stage 0 only)
# ======================================================================================
class TestOppositeFlip(unittest.TestCase):
    def _down(self, e):
        """A down (against-a-BUY) candle near price e that neither SLs nor locks."""
        return bar(e, e, e - 1.0, e - 0.5)

    def test_flip_reverses_same_stage0_sl18(self):
        p = AncParams(opposite_candles=3, opposite_action="flip")
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)
        s._start_position("BUY", 4000.0, T0)                         # link 0, sl 3982
        for i in range(3):
            s.on_m1_close(self._down(4000.0), T0 + timedelta(minutes=i + 1), None)
        # closed the BUY as a flip and immediately reversed to a fresh SELL link 1
        self.assertEqual(len(s.trades), 1)
        self.assertEqual(s.trades[0]["reason"], "flip")
        self.assertIsNotNone(s.pos)
        self.assertEqual(s.pos["side"], "SELL")
        self.assertEqual(s.pos["link"], 1)
        self.assertEqual(s.pos["sl_off"], -18.0)                     # fresh SL, back to stage 0
        self.assertEqual(s.pos["sl_price"], round(s.pos["entry"] + 18.0, 2))
        self.assertEqual(s.trades_done, 2)                           # flip consumed a slot

    def test_flip_disabled_once_locked(self):
        p = AncParams(opposite_candles=3, opposite_action="flip")
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)
        s._start_position("BUY", 4000.0, T0)
        s._ladder(bar(4000, 4003, 3999, 4002))                      # +3 -> lock (stage >= 1)
        self.assertTrue(s._locked())
        # down-body candles that stay ABOVE the +2.5 lock (no SL): must NOT flip
        for i in range(3):
            s.on_m1_close(bar(4004, 4004, 4003, 4003.5), T0 + timedelta(minutes=i + 1), None)
        self.assertIsNotNone(s.pos)
        self.assertEqual(s.pos["side"], "BUY")                       # unchanged, never flipped
        self.assertFalse(any(t["reason"] == "flip" for t in s.trades))

    def test_scratch_loss_does_not_end_chain(self):
        p = AncParams(opposite_candles=1, opposite_action="close")
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)
        s._start_position("BUY", 4000.0, T0)
        s.on_m1_close(bar(4000, 4000, 3999, 3999), T0 + timedelta(minutes=1), None)
        self.assertEqual(s.trades[0]["reason"], "scratch")
        self.assertLess(s.trades[0]["pnl_price"], 0)                 # a losing-P&L scratch ...
        self.assertFalse(s.done)                                    # ... but the chain lives
        self.assertFalse(s.chain_ended)
        self.assertEqual(s.state, "OBSERVE")
        self.assertEqual(s.reopen_price, 3999.0)

    def test_flip_loss_does_not_end_chain(self):
        p = AncParams(opposite_candles=1, opposite_action="flip")
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)
        s._start_position("BUY", 4000.0, T0)
        s.on_m1_close(bar(4000, 4000, 3999, 3999), T0 + timedelta(minutes=1), None)
        self.assertEqual(s.trades[0]["reason"], "flip")
        self.assertLess(s.trades[0]["pnl_price"], 0)
        self.assertFalse(s.done)
        self.assertFalse(s.chain_ended)
        self.assertEqual(s.pos["side"], "SELL")

    def test_flip_respects_chain_cap_5(self):
        p = AncParams(opposite_candles=1, opposite_action="flip", max_chain=5)
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None)
        s._start_position("BUY", 4000.0, T0)                        # link 0
        m = 0
        while not s.done and m < 30:
            m += 1
            if s.pos and s.pos["side"] == "BUY":
                b = bar(4000, 4000, 3999, 3999)                     # against BUY (down)
            else:
                e = s.pos["entry"]
                b = bar(e, e + 1.0, e, e + 0.5)                     # against SELL (up)
            s.on_m1_close(b, T0 + timedelta(minutes=m), None)
        self.assertTrue(s.done)
        self.assertEqual(s.trades_done, 5)                          # capped at 5 links
        self.assertEqual(len(s.trades), 5)

    def test_flip_live_closes_and_reverses_same_lot(self):
        # LIVE path: original ticket closed, opposite market order placed at anc_lot,
        # fresh SL 18, session flipped to the reverse side as the next link.
        p = AncParams(opposite_candles=3, opposite_action="flip")
        sess = AnchorDaySession("A2", 4400.0, p, flat_ts=None)
        sess.enter_live("BUY", 4400.0, T0)                          # sl 4382, link 0
        sess.open_ticket = 111
        tr = _CmdTrader([_Pos(_a.AURNO_MAGIC, 111)], paper=False)
        for i in range(3):
            down = bar(4400.0, 4400.0, 4399.0, 4399.5)              # against BUY, no SL/lock
            _a._manage_session_live(tr, sess, down, T0 + timedelta(minutes=i + 1),
                                    None, allow_new_entries=True, p=p)
        self.assertEqual(tr.adapter.closed, [111])                  # original closed
        self.assertEqual(len(tr.adapter.placed), 1)                 # one reverse order
        args, kw = tr.adapter.placed[0]
        self.assertEqual(args[1], "SELL")                           # opposite side
        self.assertAlmostEqual(float(args[2]), 0.10)                # same lot (anc_lot)
        self.assertEqual(sess.pos["side"], "SELL")
        self.assertEqual(sess.pos["link"], 1)
        self.assertEqual(sess.pos["sl_off"], -18.0)                 # fresh SL, stage 0
        self.assertEqual(sess.trades[-1]["reason"], "flip")


# ======================================================================================
# 8 — v3.10.1 funded port: EOD new-entry cutoff (open positions still ladder)
# ======================================================================================
class TestEodEntryCutoff(unittest.TestCase):
    def test_no_new_entry_at_or_after_cutoff(self):
        cutoff = T0 + timedelta(minutes=2)
        p = AncParams(confirm="candles")
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None, entry_cutoff_ts=cutoff)
        s._open_observation(T0)
        # a full 3-up confirmation lands AT/AFTER the cutoff -> no entry taken
        s.on_m1_close(cbar(4000, 4001), T0 + timedelta(minutes=1), None)
        s.on_m1_close(cbar(4001, 4002), T0 + timedelta(minutes=2), None)
        s.on_m1_close(cbar(4002, 4003), T0 + timedelta(minutes=3), None)
        self.assertIsNone(s.pending_side)
        self.assertIsNone(s.pos)

    def test_cutoff_none_keeps_todays_behaviour(self):
        p = AncParams(confirm="candles")
        s = AnchorDaySession("A2", 4000.0, p, flat_ts=None, entry_cutoff_ts=None)
        s._open_observation(T0)
        for i, (o, c) in enumerate([(4000, 4001), (4001, 4002), (4002, 4003)], start=1):
            s.on_m1_close(cbar(o, c), T0 + timedelta(minutes=i), None)
        self.assertEqual(s.pending_side, "BUY")


def _run_all():
    unittest.main(verbosity=2)


if __name__ == "__main__":
    _run_all()
