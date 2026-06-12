"""
AUREON A08 — telemetry shim.

Reuses the MT5 build's telemetry (Telegram + severity + rate limiting) so the
A08 port speaks the same channel with the same DNA. If the root module isn't on
the path, falls back to a stdlib-logging stub so the package stays importable.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:  # reuse the real telemetry
    from telemetry import (  # type: ignore  # noqa: F401
        Telemetry, Severity, telemetry_from_env,
    )
except Exception:  # pragma: no cover - fallback stub
    _log = logging.getLogger("A08.telemetry")

    class Severity:  # minimal stand-in
        DEBUG = 10; INFO = 20; SUCCESS = 25; WARN = 30; ERROR = 40; CRITICAL = 50

    class Telemetry:
        def __init__(self, component="A08"):
            self.component = component

        def _emit(self, level, msg):
            _log.log(level, msg)

        def debug(self, msg, **t): self._emit(logging.DEBUG, msg)
        def info(self, msg, **t): self._emit(logging.INFO, msg)
        def success(self, msg, **t): self._emit(logging.INFO, msg)
        def warn(self, msg, **t): self._emit(logging.WARNING, msg)
        def error(self, msg, **t): self._emit(logging.ERROR, msg)
        def critical(self, msg, **t): self._emit(logging.CRITICAL, msg)

    def telemetry_from_env(component="A08"):
        return Telemetry(component=component)
