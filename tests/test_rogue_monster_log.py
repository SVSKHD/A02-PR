"""Tests for the ROGUE monster decision log (rogue_monster_log.py).

Runs under pytest AND standalone: `python tests/test_rogue_monster_log.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rogue_monster_log as rml  # noqa: E402


def _read(day, d):
    with open(os.path.join(d, f"rogue_{day}.log")) as f:
        return f.read().splitlines()


def test_clock_parsing():
    assert rml._clock("2026-06-10 14:03:11") == "14:03:11"
    assert rml._clock("2026-06-10T14:03:11+00:00") == "14:03:11"


def test_emit_and_format(tmp_path):
    d = str(tmp_path)
    day = "2026-06-10"
    rml.arm(day, "2026-06-10 03:06:00", side="LONG", level=3001.60,
            reason="BOX break", log_dir=d)
    rml.fill(day, "2026-06-10 03:06:30", kind="ENTRY", side="LONG", price=3001.60,
             sl=2991.60, ticket=123, log_dir=d)
    rml.close(day, "2026-06-10 03:09:00", side="LONG", kind="ENTRY", price=2991.60,
              pnl=-350.0, reason="SL", log_dir=d)
    rml.guard(day, "2026-06-10 03:09:00", name="RED_DAY_CARRY", detail="atr_mult+0.5", log_dir=d)
    rml.governor(day, "2026-06-10 15:37:00", name="GOV-LOCK", day_pnl=1000.0, log_dir=d)
    lines = _read(day, d)
    assert lines[0] == "03:06:00 ARM       side=LONG level=3001.60 reason=BOX break"
    assert lines[1] == "03:06:30 FILL      kind=ENTRY side=LONG price=3001.60 sl=2991.60 ticket=123"
    assert lines[2] == "03:09:00 CLOSE     side=LONG kind=ENTRY price=2991.60 pnl=-350.00 reason=SL"
    assert lines[3] == "03:09:00 GUARD     guard=RED_DAY_CARRY detail=atr_mult+0.5"
    assert lines[4] == "15:37:00 GOV       halt=GOV-LOCK day_pnl=+1000.00"


def test_boot_line(tmp_path):
    d = str(tmp_path)
    day = "2026-06-10"
    rml.boot(day, "2026-06-10 02:00:00", anchor=3000.00, armed="dark",
             guards="none", config_hash="abc123", log_dir=d)
    lines = _read(day, d)
    assert lines[0].startswith("02:00:00 BOOT")
    assert "impl=monster" in lines[0]
    assert "cfg=abc123" in lines[0]


def test_emit_never_raises_on_bad_dir():
    # a bad path must not raise onto the trading path
    out = rml.emit("2026-06-10", "2026-06-10 03:00:00", "ARM",
                   log_dir="/nonexistent\x00/definitely/bad", side="LONG")
    assert out == ""


def _run_all():
    import tempfile
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        if "tmp_path" in fn.__code__.co_varnames:
            with tempfile.TemporaryDirectory() as td:
                import pathlib
                fn(pathlib.Path(td))
        else:
            fn()
        print(f"ok   {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
