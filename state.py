"""AUREON — persistent state: load/save (atomic + .bak) + PID lock.

Split out of live_trader.py in v3.0.0. These are the verbatim LiveTrader
methods (bodies byte-identical, dedented one level); they take `self` and
are bound back onto LiveTrader in live_trader.py. Behavior-frozen (except
the commit-1 fixes already in the fill path).
"""
import csv
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import date as DateType, timedelta, datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from telemetry import telemetry_from_env, Severity
from mt5_adapter import _MT5_RETCODE_MAP

log = logging.getLogger("AUREON")


def _load_state(self) -> Dict:
    # v2.5: try main state, then .bak fallback, then fresh
    for path, label in [(self.state_path, "main"),
                        (self.state_path + ".bak", "backup")]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    s = json.load(f)
                log.info(f"Restored state from {label}: {path}")
                return s
            except Exception as e:
                log.warning(f"State {label} corrupt ({e}); trying next source")
    log.warning("No usable state file; starting fresh")
    return {
        'daily_pnl': 0.0,
        'last_broker_date': None,
        'processed_anchors_today': [],
        'kill_switch_locked': False,
        'day_start_equity': None,  # v2.5.4: today's opening equity — kill baseline
        'shadow_positions_extended': {},  # v2.5: persisted max_fav/fill_time per ticket
    }


def _save_state(self):
    if self.paper:
        return
    # v2.5: atomic write + rolling .bak backup
    tmp = self.state_path + '.tmp'
    bak = self.state_path + '.bak'
    # Mirror in-memory shadow lock state into the dict before writing
    try:
        self.state['shadow_positions_extended'] = {
            str(ticket): {
                'max_fav':   shadow.get('max_fav'),
                'fill_time': shadow.get('fill_time'),
                'role':      shadow.get('role', 'normal'),
                'current_sl': shadow.get('current_sl'),
                'side':       shadow.get('side'),
                'entry_price': shadow.get('entry_price'),
                'anchor_label': shadow.get('anchor_label'),
            }
            for ticket, shadow in self.shadow_positions.items()
        }
    except Exception as e:
        log.warning(f"Could not snapshot shadow_positions to state: {e}")
    # v2.9.8: persist PENDINGS too -- a restart between placement and fill
    # previously orphaned them (fills undetected / rescue flag lost).
    try:
        self.state['shadow_pendings_extended'] = {
            str(tk): {
                'anchor_label':   p.get('anchor_label'),
                'side':           p.get('side'),
                'sibling_ticket': p.get('sibling_ticket'),
                'entry_price':    p.get('entry_price'),
                'rescue_on_fill': bool(p.get('rescue_on_fill', False)),
            }
            for tk, p in self.shadow_pendings.items()
            if not isinstance(tk, str)
        }
    except Exception as e:
        log.warning(f"Could not snapshot shadow_pendings to state: {e}")
    # Copy current main → .bak before overwriting
    if os.path.exists(self.state_path):
        try:
            import shutil
            shutil.copyfile(self.state_path, bak)
        except Exception:
            pass  # backup failure is not fatal
    with open(tmp, 'w') as f:
        json.dump(self.state, f, indent=2, default=str)
    os.replace(tmp, self.state_path)


def _acquire_pid_lock(self):
    """v2.5: Refuse to start if another bot instance is already running.

    Hardening #2: acquire the lock ATOMICALLY with O_CREAT|O_EXCL so two
    near-simultaneous starts can never both win (the old exists()-then-write
    was a TOCTOU hole -- both could pass the check and both write). On EEXIST we
    inspect the holder: a LIVE AUREON process means refuse; a stale/foreign lock
    is removed and the create retried once.
    """
    import psutil

    def _holder_is_live_aureon() -> bool:
        try:
            with open(self.pid_lock_path) as f:
                other_pid = int(f.read().strip())
        except (ValueError, OSError):
            return False  # malformed -> stale
        if not psutil.pid_exists(other_pid):
            return False
        try:
            cmdline = " ".join(psutil.Process(other_pid).cmdline()).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False  # can't confirm -> treat as stale, safe to take
        if "aureon" in cmdline or "live_trader" in cmdline or "bot.py" in cmdline:
            raise RuntimeError(
                f"Another AUREON bot is already running (PID {other_pid}). "
                f"Refusing to start a second instance — they would conflict on "
                f"magic number 20260522 and OCO sibling tracking. "
                f"Kill the other instance first: taskkill /F /PID {other_pid}"
            )
        return False  # live but not an AUREON bot -> foreign/stale lock

    for _attempt in (1, 2):
        try:
            fd = os.open(self.pid_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _holder_is_live_aureon():  # raises if a live AUREON bot holds it
                return
            try:
                os.remove(self.pid_lock_path)  # stale/foreign -> clear and retry
            except OSError:
                pass
            continue
        else:
            with os.fdopen(fd, 'w') as f:
                f.write(str(os.getpid()))
            log.info(f"PID lock acquired: {self.pid_lock_path} = {os.getpid()}")
            return
    raise RuntimeError(
        f"Could not acquire PID lock {self.pid_lock_path} after retrying — "
        f"another instance is racing to start. Aborting to stay single-instance."
    )


def _release_pid_lock(self):
    try:
        if os.path.exists(self.pid_lock_path):
            with open(self.pid_lock_path) as f:
                locked_pid = int(f.read().strip())
            if locked_pid == os.getpid():
                os.remove(self.pid_lock_path)
                log.info("PID lock released")
    except Exception as e:
        log.warning(f"Could not release PID lock: {e}")
