"""Tests for the ROGUE monster Discord cards (discord_cards.card_monster_*).

Runs under pytest AND standalone: `python tests/test_rogue_monster_cards.py`.
Absolute prices must come through verbatim; no card may raise.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord_cards as dc  # noqa: E402


def _ok(card):
    assert isinstance(card, dict)
    assert "title" in card and "color" in card
    return card


def test_boot_card():
    c = _ok(dc.card_monster_boot(3000.0, "dark", "none", "abc123"))
    assert c["title"] == "🗡️ ROGUE IMPL: monster"
    vals = [f["value"] for f in c["fields"]]
    assert "$3,000.00" in vals            # absolute anchor price
    assert any("abc123" in v for v in vals)   # config hash


def test_armed_card_has_absolute_level():
    c = _ok(dc.card_monster_armed("LONG", 3001.60, "BOX break | bias BOTH", anchor=3000.0))
    assert c["title"] == "🗡️ ROGUE armed LONG"
    assert any(f["value"] == "$3,001.60" for f in c["fields"])


def test_sequence_card_color_by_pnl():
    win = _ok(dc.card_monster_sequence(3000.0, 2, 1, 461.30, "TRAIL"))
    loss = _ok(dc.card_monster_sequence(3000.0, 1, 0, -350.0, "SL"))
    assert win["color"] == dc.GREEN
    assert loss["color"] == dc.RED
    assert any(f["value"] == "+$461.30" for f in win["fields"])
    assert any(f["value"] == "-$350.00" for f in loss["fields"])


def test_fill_and_reanchor_and_guard_and_gov():
    _ok(dc.card_monster_fill("CHAIN", "SHORT", 2988.0, 2998.0, ticket=7))
    _ok(dc.card_monster_reanchor(2990.0, 1))
    g = _ok(dc.card_monster_guard("RED_DAY_CARRY", "atr_mult +0.5"))
    assert g["color"] == dc.AMBER
    gov = _ok(dc.card_monster_governor("GOV-LOSS", -1000.0))
    assert gov["color"] == dc.RED
    assert any(f["value"] == "-$1,000.00" for f in gov["fields"])


def test_cards_never_raise_on_bad_input():
    # None / bad values must degrade, not raise
    _ok(dc.card_monster_armed("LONG", None, "x", anchor=None))
    _ok(dc.card_monster_sequence(None, 0, 0, "n/a", "—"))


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok   {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
