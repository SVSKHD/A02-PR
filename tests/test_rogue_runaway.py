"""Rogue A1 RUNAWAY RE-ANCHOR — unit tests (MT5 fully mocked).

Replays the 2026-07-16 band-overshoot incident (magic 20260626, A1-anchor mode):
A1 ref 4030.59, $10 break latches the downside anchor at 4020.59, then a fast crash
steps ACROSS the $8-wide entry band (confirm 12 .. chase cap 20) without a tick
landing inside it -> ZERO entries. The runaway re-anchor plants a fresh continuation
anchor once price runs >= rogue_runaway_trigger past the active anchor.

Runnable under pytest or standalone (`python tests/test_rogue_runaway.py`).
"""
import os
import sys
import types
import dataclasses

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rogue as R
from config import Config


# --- controllable paper ROGUE trader + a settable single-price feed -----------------
def _mk(runaway_on=True, confirm=12.0, cap=20.0, brk=10.0, init_sl=10.0):
    cfg = dataclasses.replace(
        Config(), rogue_enabled=True, rogue_a1_anchor_mode=True,
        seed_break_dollars=brk, rogue_entry_confirm_redesign=confirm,
        rogue_chase_cap_dollars=cap, rogue_init_sl=init_sl, lot_size=0.01,
        rogue_seed_fallback='market_open',
        rogue_runaway_reanchor_enabled=runaway_on,
        rogue_runaway_trigger=25.0, rogue_runaway_confirm=8.0,
        # disable the earned-budget gate so tests exercise the anchor/entry mechanics
        # in isolation (budget behavior is covered by its own suite).
        engine_base_trades_per_anchor=0)
    px = {'p': 4030.59}
    open_tk = set()

    def _tick(s=None):
        p = px['p']
        return types.SimpleNamespace(bid=p - 0.05, ask=p + 0.05)

    def _positions_get(ticket=None):
        # keep any filled ticket OPEN so detect_close never spuriously books a close.
        if ticket is not None and int(ticket) in open_tk:
            return [types.SimpleNamespace(ticket=int(ticket))]
        return []

    placed = []

    def _place(*a, **k):
        tkn = 7000 + len(placed) + 1
        placed.append({'args': a, 'kw': k, 'ticket': tkn})
        open_tk.add(tkn)
        return types.SimpleNamespace(retcode=10009, order=tkn, deal=tkn)

    mt5 = types.SimpleNamespace(
        ACCOUNT_TRADE_MODE_DEMO=0,
        account_info=lambda: types.SimpleNamespace(trade_mode=0),
        symbol_info_tick=_tick, positions_get=_positions_get,
        history_deals_get=lambda position=None: [])
    ad = types.SimpleNamespace(
        mt5=mt5, get_latest_m5=lambda s, n: [],
        place_market_order=_place,
        modify_position_sl=lambda *a, **k: types.SimpleNamespace(retcode=10009),
        close_position=lambda tk, dry_run=False: open_tk.discard(int(tk)),
        place_with_retry=lambda send, describe=None, tele=None: send(1, False))
    tr = types.SimpleNamespace(
        cfg=cfg, adapter=ad, paper=True, run_dir='/tmp', _rogue=None,
        _last_boost_mid=4030.59, engines={'anchors': False, 'rogue': True},
        state={'last_broker_date': '2026-07-16'}, _market_closed_now=lambda: False,
        tele=types.SimpleNamespace(info=lambda *a, **k: None, warn=lambda *a, **k: None))
    st = {'day': '2026-07-16', 'gov': R.new_day_state(), 'anchor': None,
          'leg_dir': None, 'open': None, 'day_open_px': 4030.59}
    tr._rogue = st
    return tr, st, px, placed


def _run(tr, st, px, tape):
    """Drive one tick per price in `tape` through _drive_a1."""
    for p in tape:
        px['p'] = round(float(p), 2)
        R._drive_a1(tr, st)


# =====================================================================================
def test_runaway_plants_and_continuation_entry_fires():
    """The 2026-07-16 tape: the $8 band is skipped, the runaway plants in the ~3995-4005
    region, and the continuation SELL fires on the next $8 down-leg with a normal init SL."""
    tr, st, px, placed = _mk()
    # near-miss up, downside latch @ 4020.59, then a crash that skips [4000.59, 4008.59]
    _run(tr, st, px, [4030.59, 4040.26, 4020.59, 4011.00, 3999.00])
    assert not placed, "no entry should fire while the band is skipped (today's bug)"
    assert abs(st['seed_px'] - 4020.59) < 1e-9, "downside $10 break must latch at 4020.59"
    assert int(st.get('runaway_count', 0)) == 0, "no runaway until move >= trigger"

    # price runs to -25 off 4020.59 -> runaway plants at the settled tick (~3995)
    _run(tr, st, px, [3995.00])
    assert int(st['runaway_count']) == 1
    assert st.get('runaway_active') is True
    assert st.get('runaway_dir') == 'DN'
    assert 3995.0 <= st['runaway_anchor_px'] <= 4005.0, "re-anchor in the ~3995-4005 region"
    assert abs(st['a1_last_close'] - st['runaway_anchor_px']) < 1e-9, "chained at the re-anchor"
    assert not placed, "the plant itself takes no entry (and no governor slot)"

    # the next $8 down-leg off the new anchor (3995 -> 3987) fires the continuation SELL
    _run(tr, st, px, [3990.00, 3987.00])
    assert len(placed) == 1, "continuation entry fires on the $8 down-leg"
    side, _lot = placed[0]['args'][1], placed[0]['args'][2]
    entry_sl = placed[0]['kw'].get('sl')
    assert side == 'SELL', "continuation off a DN runaway is SELL"
    assert st['open'] is not None and st['open']['side'] == 'SELL'
    # normal init SL = rogue_init_sl ($10) on the wrong side of a 3987 entry -> 3997
    assert abs(float(entry_sl) - 3997.0) < 1e-9, f"init SL normal ($10): got {entry_sl}"
    assert abs(float(st['open']['entry']) - 3987.0) < 1e-9
    # entry consumed the runaway -> active markers cleared, count persists for the day
    assert st.get('runaway_active') is False
    assert int(st['runaway_count']) == 1
    print("PASS test_runaway_plants_and_continuation_entry_fires")


def test_no_reanchor_when_entry_was_taken():
    """If an entry WAS taken off the seed anchor, a later run past it does NOT re-anchor
    (Rogue is in the open/manage path, not the entry path)."""
    tr, st, px, placed = _mk()
    # latch @ 4020.59, then land INSIDE the band (4005 -> move -15.59) so the SELL fires
    _run(tr, st, px, [4030.59, 4020.59, 4005.00])
    assert len(placed) == 1 and placed[0]['args'][1] == 'SELL', "seed-anchor entry taken"
    # now the move runs far past the anchor while the position is open
    _run(tr, st, px, [3990.00, 3975.00, 3960.00])
    assert int(st.get('runaway_count', 0)) == 0, "no runaway while a position is open"
    assert st.get('runaway_active') in (None, False)
    assert len(placed) == 1, "no extra entries"
    print("PASS test_no_reanchor_when_entry_was_taken")


def test_three_per_day_cap():
    """A fast crash that gaps past the chase cap each step forces pure re-anchors; the loop
    guard caps them at 3/day, each >= trigger from the last."""
    tr, st, px, placed = _mk()
    _run(tr, st, px, [4030.59, 4020.59])          # latch @ 4020.59
    # each gap is 26 (> cap 20) so the continuation entry is always chased -> re-anchor only
    _run(tr, st, px, [3995.00])                    # runaway #1 @ ~3995
    assert int(st['runaway_count']) == 1
    _run(tr, st, px, [3969.00])                    # runaway #2 @ ~3969
    assert int(st['runaway_count']) == 2
    _run(tr, st, px, [3943.00])                    # runaway #3 @ ~3943
    assert int(st['runaway_count']) == 3
    _run(tr, st, px, [3917.00, 3891.00])           # further gaps -> NO 4th re-anchor
    assert int(st['runaway_count']) == 3, "capped at 3 runaway re-anchors per day"
    assert not placed, "cap-gapping tape takes no entry (all chased)"
    print("PASS test_three_per_day_cap")


def test_counter_trend_off_runaway_refused():
    """Off a DN runaway anchor, a counter-trend (UP) move is REFUSED; a same-direction (DN)
    move still enters -- proving the anchor is live, only the direction is locked."""
    tr, st, px, placed = _mk()
    _run(tr, st, px, [4030.59, 4020.59, 4011.00, 3999.00, 3995.00])  # runaway #1 DN @ ~3995
    assert int(st['runaway_count']) == 1 and st['runaway_dir'] == 'DN'
    # counter-trend: price bounces UP $8 off the re-anchor -> must NOT enter
    _run(tr, st, px, [4003.00])
    assert not placed, "counter-trend entry off a runaway re-anchor is refused"
    assert st.get('runaway_active') is True, "anchor still live, direction locked DN"
    # continuation: an $8 down-leg DOES enter
    _run(tr, st, px, [3987.00])
    assert len(placed) == 1 and placed[0]['args'][1] == 'SELL'
    print("PASS test_counter_trend_off_runaway_refused")


def test_flag_off_byte_identical():
    """rogue_runaway_reanchor_enabled=False -> today's behavior: the crash tape takes ZERO
    entries, plants NO re-anchor, and the anchor stays the latched seed (4020.59)."""
    tr, st, px, placed = _mk(runaway_on=False)
    _run(tr, st, px, [4030.59, 4040.26, 4020.59, 4011.00, 3999.00,
                      3995.00, 3990.00, 3982.00, 3975.00])
    assert not placed, "flag OFF: zero entries (unchanged from the incident)"
    assert int(st.get('runaway_count', 0)) == 0, "flag OFF: no runaway re-anchor"
    assert st.get('runaway_active') in (None, False)
    assert st.get('a1_last_close') is None, "flag OFF: seed anchor never re-anchored"
    assert abs(st['seed_px'] - 4020.59) < 1e-9
    print("PASS test_flag_off_byte_identical")


if __name__ == '__main__':
    test_runaway_plants_and_continuation_entry_fires()
    test_no_reanchor_when_entry_was_taken()
    test_three_per_day_cap()
    test_counter_trend_off_runaway_refused()
    test_flag_off_byte_identical()
    print("\nALL RUNAWAY TESTS PASSED")
