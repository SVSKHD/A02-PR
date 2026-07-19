"""AUREON — ROGUE monster engine persistence (run/rogue_monster_state.json).

Survives restart/reconnect the same way the anchor does (PR #121 semantics):
  * the rolling anchor + the day it was seeded — reloaded on restart, and the
    02:30 daily seed is NEVER re-snapshotted once stored for the day;
  * the adaptive-guard state (consec_sl, caution_until, day peak P/L, per-side SL
    counters, red-day carry) — so caution/fatigue/giveback/red-day survive a crash.

Atomic tmp+fsync+os.replace write, persist-on-change, fully guarded (a
persistence error never reaches the trading path). Same pattern as
rogue_t2/statestore.py.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

log = logging.getLogger("AUREON")

FILENAME = "rogue_monster_state.json"
_last_blob = {"v": None}


def _path(run_dir):
    return os.path.join(run_dir or "run", FILENAME)


def load(run_dir):
    """Return the stored dict, or {} if absent/unreadable. Never raises."""
    try:
        with open(_path(run_dir), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save(run_dir, data, force=False):
    """Atomically persist `data`. Persist-on-change unless force. Never raises."""
    try:
        blob = json.dumps(data, sort_keys=True, default=str)
        if blob == _last_blob["v"] and not force:
            return False
        d = os.path.dirname(_path(run_dir)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _path(run_dir))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        _last_blob["v"] = blob
        return True
    except Exception as e:  # pragma: no cover - guarded
        log.warning(f"rogue_monster_state.save non-fatal: {e!r}")
        return False
