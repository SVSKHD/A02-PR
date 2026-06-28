"""AUREON — pure helpers (split from bot.py, v3.0.0).

stdlib + pandas only -- imports NO AUREON module, so it can never take part
in an import cycle. `from __future__ import annotations` keeps the Config
type hints lazy so this module needs no config import.
"""
from __future__ import annotations

import logging
import os
from datetime import date as DateType
from typing import Optional

import pandas as pd

log = logging.getLogger("AUREON")


from logging.handlers import TimedRotatingFileHandler as _TimedRotatingFileHandler


class _SafeRotatingFileHandler(_TimedRotatingFileHandler):
    """TimedRotatingFileHandler whose midnight rollover can NEVER raise.

    On Windows, doRollover() calls os.rename() on the still-open (or externally
    held) log file and raises PermissionError [WinError 32]. Unguarded, that
    error escapes through logging and crashes whatever was emitting -- including
    the selftest teardown. Here the rename is wrapped: on PermissionError/OSError
    we emit a single console warning, skip THIS rollover, and keep writing to the
    current file. The base class already advanced rolloverAt, so the next attempt
    is naturally the following midnight. No trading behavior depends on this."""

    def doRollover(self):  # noqa: D401 -- override
        try:
            super().doRollover()
        except (PermissionError, OSError) as e:
            # Keep the current stream usable: if the base class closed it before
            # the rename failed, reopen so logging continues uninterrupted.
            try:
                if self.stream is None and not self.delay:
                    self.stream = self._open()
            except Exception:
                pass
            try:
                logging.getLogger("AUREON").warning(
                    "log rotation skipped (file locked: %s); continuing on the "
                    "current file, will retry at next midnight.", e
                )
            except Exception:
                pass


def setup_logging(level: str = "INFO", log_dir: str = "./logs",
                  app_name: str = "aureon"):
    """Set up logging to BOTH stdout and a daily-rotated file in log_dir.

    File naming: logs/aureon_YYYY-MM-DD.log (rotated daily at UTC midnight,
    keeping 30 days of history). All log levels from app modules go in.

    Format includes timestamp, level, module name, and message. Caller can
    grep for specific anchors, errors, or modules later.
    """
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))
    # Clear any pre-existing handlers so basicConfig calls don't double-log
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler (so terminal still shows everything)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Daily-rotated file handler.
    # Windows-safe: TimedRotatingFileHandler.doRollover() renames the open log
    # file at midnight. On Windows the file is still held open (and may be held
    # by a tail/editor too), so os.rename raises PermissionError [WinError 32].
    # The stock handler has no guard, so that error propagates up through
    # logging.emit -> the telemetry worker -> SelfTest.run() teardown, scoring a
    # clean test run as FAILED. _SafeRotatingFileHandler swallows the rename
    # failure: it logs ONE warning to the console and keeps writing to the
    # current file, retrying the rollover at the next midnight. Rotation failure
    # must NEVER raise into the app. delay=True defers opening the file until the
    # first emit (so the handle isn't held needlessly before anything is logged).
    log_file = os.path.join(log_dir, f"{app_name}.log")
    file_handler = _SafeRotatingFileHandler(
        log_file, when='midnight', interval=1, backupCount=30, utc=True,
        encoding='utf-8', delay=True
    )
    file_handler.setFormatter(fmt)
    file_handler.suffix = "%Y-%m-%d"  # so rotated files become aureon.log.2026-05-25
    root.addHandler(file_handler)

    log = logging.getLogger("AUREON")
    log.info(f"Logging to console + {log_file} (daily rotation, 30-day retention)")
    return log


def initial_sl(side: str, entry: float, cfg: Config) -> float:
    return entry - cfg.sl_dist if side == 'BUY' else entry + cfg.sl_dist


def initial_tp(side: str, entry: float, cfg: Config) -> float:
    return entry + cfg.tp_dist if side == 'BUY' else entry - cfg.tp_dist


def anchor_datetime_utc(broker_date: DateType, broker_hour: int,
                        broker_tz_offset_hours: int = 3,
                        broker_minute: int = 0) -> pd.Timestamp:
    """Convert a broker-date + broker-hour(+minute) to a UTC timestamp."""
    ts = (pd.Timestamp(broker_date)
          + pd.Timedelta(hours=broker_hour - broker_tz_offset_hours)
          + pd.Timedelta(minutes=broker_minute))
    return ts.tz_localize('UTC')


def eod_datetime_utc(broker_date: DateType, cfg: Config) -> pd.Timestamp:
    """EOD UTC timestamp = broker 23:00 = UTC 20:00 same broker date."""
    return anchor_datetime_utc(broker_date, cfg.eod_broker_hour, cfg.broker_tz_offset_hours)


def m5_close_at(m5: pd.DataFrame, target_utc: pd.Timestamp) -> Optional[float]:
    """Get the close of the M5 bar ending at target_utc (or nearest within ±5min)."""
    if target_utc in m5.index:
        return float(m5.loc[target_utc, 'close'])
    near = m5.index[(m5.index >= target_utc - pd.Timedelta(minutes=5)) &
                    (m5.index <= target_utc + pd.Timedelta(minutes=5))]
    if len(near) == 0: return None
    # Hardening #6: return the close of the bar NEAREST to target_utc, not the
    # earliest in the window (near[0]) -- when bars straddle target on both
    # sides the earliest can be further away and pick the wrong anchor close.
    nearest = min(near, key=lambda ix: abs(ix - target_utc))
    return float(m5.loc[nearest, 'close'])
