"""Tests for `/testfire now` — the ungated in-process non-OCO straddle — and the
rescue-boost P&L isolation fix that ships with it.

Two things are proven here without any broker:
  1. pnl_source.magic_day_net(exclude_test=True) now ALSO drops the RB rescue legs of a
     TF_ parent position, while a NORMAL anchor's RB legs are unchanged (still counted).
  2. arm_testfire_inproc / handle_testfire_now_command thread the per-call dist + lot
     overrides onto the SEPARATE testfire deferred slot (never the real _deferred_anchor),
     so _place_orders_for_anchor picks up the short trigger distance + custom lot.
"""
import types

import pandas as pd

import pnl_source as ps
import testfire as tf


class _Deal:
    """MT5-deal-like: object attributes, matching the repo's getattr-style readers."""
    def __init__(self, magic, entry, profit, comment="", position_id=None):
        self.magic = magic
        self.entry = entry
        self.profit = profit
        self.comment = comment
        self.position_id = position_id
        self.swap = 0.0
        self.commission = 0.0


A = ps.ANCHORS_MAGIC  # 20260522 (TF_ legs + RB legs share the anchor magic)


# ── 1. RB-of-TF exclusion ─────────────────────────────────────────────────────────
def test_rb_legs_of_tf_parent_are_excluded():
    deals = [
        # TESTFIRE parent straddle (position ids 1000 / 1001) — excluded by TF_ marker.
        _Deal(A, 1, -954.0, "TF_AUR_TF_BUY A:4000.00", position_id=1000),
        _Deal(A, 1, 120.0, "TF_AUR_TF_SELL A:4000.00", position_id=1001),
        # RB rescue leg of the TF_ parent (comment has NO TF_ marker) — must inherit exclusion.
        _Deal(A, 1, -510.0, "RB1:1000", position_id=1002),
        # REAL anchor + its RB rescue leg — must BOTH still count.
        _Deal(A, 1, 200.0, "AUR_A1_BUY A:4000.00", position_id=3000),
        _Deal(A, 1, -75.0, "RB1:3000", position_id=3001),
    ]
    # exclude_test drops the TF_ parent (-954, +120) AND its RB leg (-510); keeps the real
    # anchor (+200) and the real anchor's RB leg (-75) => +125.00
    assert ps.magic_day_net(deals, A, exclude_test=True) == 125.0
    # legacy (no exclusion) counts everything, unchanged.
    assert ps.magic_day_net(deals, A) == round(-954 + 120 - 510 + 200 - 75, 2)


def test_normal_anchor_rb_unchanged_when_no_testfire_present():
    """With no TF_ deal in the window, exclude_test must not drop any RB leg."""
    deals = [
        _Deal(A, 1, 200.0, "AUR_A1_BUY A:4000.00", position_id=3000),
        _Deal(A, 1, -75.0, "RB1:3000", position_id=3001),
    ]
    assert ps.magic_day_net(deals, A, exclude_test=True) == 125.0
    assert ps.magic_day_net(deals, A) == 125.0


def test_rb_helpers_pure():
    assert ps._rb_parent_ticket(_Deal(A, 1, 0, "RB2:987654")) == 987654
    assert ps._rb_parent_ticket(_Deal(A, 1, 0, "AUR_A1_BUY A:1")) is None
    ids = ps._test_position_ids([
        _Deal(A, 1, 0, "TF_AUR_TF_BUY A:1", position_id=5),
        _Deal(A, 1, 0, "AUR_A1_BUY A:1", position_id=6),
    ])
    assert ids == {5}


# ── 2. dist + lot threading onto the testfire deferred slot ─────────────────────────
def _stub_trader():
    tick = types.SimpleNamespace(bid=4000.0, ask=4000.2)
    mt5 = types.SimpleNamespace(symbol_info_tick=lambda sym: tick)
    cfg = types.SimpleNamespace(symbol="XAUUSD", lot_size=0.53, trigger_dist=5.0,
                                sl_dist=18.0, tp_dist=30.0)
    tele = types.SimpleNamespace(warn=lambda *a, **k: None, info=lambda *a, **k: None,
                                 error=lambda *a, **k: None)
    return types.SimpleNamespace(cfg=cfg, adapter=types.SimpleNamespace(mt5=mt5),
                                 tele=tele, state={}, _testfire_event_open=False,
                                 _testfire_deferred=None)


def test_arm_inproc_threads_overrides():
    tr = _stub_trader()
    now = pd.Timestamp("2026-08-03T10:00:00Z")
    label = tf.arm_testfire_inproc(tr, now, dist=3.0, lot=0.68)
    d = tr._testfire_deferred
    assert label.startswith("TF_")
    assert d is not None
    # the SEPARATE testfire slot is used; the real _deferred_anchor slot is never created here.
    assert getattr(tr, "_deferred_anchor", None) is None
    assert d["trigger_dist_override"] == 3.0
    assert d["lot_override"] == 0.68
    # defers to the CURRENT mid, on the next tick, tagged TF_ (isolated slot).
    assert d["label"] == label and d["defer_until"] == now


def test_arm_inproc_no_override_is_byte_identical_none():
    tr = _stub_trader()
    now = pd.Timestamp("2026-08-03T10:00:00Z")
    tf.arm_testfire_inproc(tr, now)  # the gated /testfire path passes no dist/lot
    d = tr._testfire_deferred
    assert d["trigger_dist_override"] is None
    assert d["lot_override"] is None


def test_handle_now_defaults_dist_3_lot_cfg():
    tr = _stub_trader()
    now = pd.Timestamp("2026-08-03T10:00:00Z")
    assert tf.handle_testfire_now_command(tr, dist=None, lot=None, now_utc=now) is True
    d = tr._testfire_deferred
    assert d["trigger_dist_override"] == 3.0    # default short distance
    assert d["lot_override"] is None            # None -> _place_orders_for_anchor uses cfg.lot_size


def test_handle_now_parses_string_args():
    tr = _stub_trader()
    now = pd.Timestamp("2026-08-03T10:00:00Z")
    tf.handle_testfire_now_command(tr, dist="4.5", lot="0.68", now_utc=now)
    d = tr._testfire_deferred
    assert d["trigger_dist_override"] == 4.5
    assert d["lot_override"] == 0.68
