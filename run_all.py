#!/usr/bin/env python3
"""run_all.py — task entry points.

This file did not previously exist in the repo; it is created here solely as the
wiring point the threshold_8 task asks for. It adds two subcommands and nothing else,
so it does not touch any existing module:

    python run_all.py threshold8        [optional/path/to/gold_m1.csv]
        Run the threshold_8 backtest + full parameter sweep (control arm included).

    python run_all.py threshold8-test
        Run the threshold_8 invariant/behaviour test suite (stdlib unittest).
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _cmd_threshold8(argv):
    from threshold_8_backtest import main as bt_main
    csv_path = argv[0] if argv else None
    bt_main(csv_path)
    return 0


def _cmd_threshold8_test(argv):
    import unittest
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(_HERE, "tests"),
                            pattern="test_threshold_8.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


_COMMANDS = {
    "threshold8": _cmd_threshold8,
    "threshold8-test": _cmd_threshold8_test,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: python run_all.py <command> [args]")
        print("commands:")
        print("  threshold8 [csv]   run the threshold_8 backtest + sweep")
        print("  threshold8-test    run the threshold_8 test suite")
        return 0
    cmd = argv[0]
    if cmd not in _COMMANDS:
        print("unknown command: %s" % cmd)
        print("known commands: %s" % ", ".join(sorted(_COMMANDS)))
        return 2
    return _COMMANDS[cmd](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
