"""Regenerate golden_*.txt from the engine. Provenance: these outputs were
first captured from the validated reference sim (rp2) and confirmed identical to
MonsterEngine; this script re-renders them from the engine so the repo can
regression-check without the external rp2.py file.
"""
import io
import os
import sys
import contextlib
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import rogue_monster as rm  # noqa: E402
import monster_scenarios as ms  # noqa: E402

# load the backtester renderer by path (backtest/ is not a package)
_spec = importlib.util.spec_from_file_location(
    "monster_backtest", os.path.join(_ROOT, "backtest", "monster_backtest.py"))
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


def render(m1, cfg, label):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mb.run(m1, cfg, verbose=True, label=label)
    return buf.getvalue()


def main():
    for name, fn in ms.SCENARIOS.items():
        out = render(fn(), rm.MonsterCfg(), name)
        with open(os.path.join(_HERE, f"golden_{name}.txt"), "w") as f:
            f.write(out)
        print(f"wrote golden_{name}.txt ({len(out)} bytes)")


if __name__ == "__main__":
    main()
