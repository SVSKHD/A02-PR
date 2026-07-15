"""Rogue T2 — watchdog.

Keeps the existing behavior the platform relies on: crash-restart with exponential
backoff, stale-heartbeat kill, a 6-dirty-restarts cooldown, and Discord alerts. Adds
the spec's new check: state.json mtime must advance during an active phase (a bot that
is "up" but not persisting is functionally dead).

Pure policy (decide_action) is separated from the OS-level supervise loop so it can be
unit-tested without spawning processes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class WatchdogConfig:
    heartbeat_timeout_s: float = 60.0     # no heartbeat within this -> kill/restart
    state_mtime_timeout_s: float = 120.0  # state.json must advance within this in-phase
    backoff_base_s: float = 2.0
    backoff_max_s: float = 300.0
    dirty_restart_limit: int = 6          # within window -> cooldown
    dirty_window_s: float = 600.0
    cooldown_s: float = 900.0


@dataclass
class WatchdogState:
    dirty_restarts: int = 0
    first_dirty_ts: float = 0.0
    cooldown_until: float = 0.0
    consecutive_restarts: int = 0


def backoff_seconds(cfg: WatchdogConfig, consecutive_restarts: int) -> float:
    """Exponential backoff, capped."""
    return min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** max(0, consecutive_restarts)))


def decide_action(cfg: WatchdogConfig, st: WatchdogState, *, now: float,
                  last_heartbeat: float, state_mtime: float,
                  in_phase: bool, process_alive: bool) -> str:
    """Pure supervisor decision. Returns one of:
      'ok'        — healthy, do nothing
      'cooldown'  — in the dirty-restart cooldown; wait
      'restart'   — (re)start the process now
      'kill'      — heartbeat/state stale; kill then it will restart next tick
    """
    if now < st.cooldown_until:
        return "cooldown"
    if not process_alive:
        return "restart"
    if (now - last_heartbeat) > cfg.heartbeat_timeout_s:
        return "kill"
    # new check: during an active phase, state.json mtime must advance
    if in_phase and (now - state_mtime) > cfg.state_mtime_timeout_s:
        return "kill"
    return "ok"


def register_restart(cfg: WatchdogConfig, st: WatchdogState, now: float,
                     dirty: bool) -> WatchdogState:
    """Track restarts; a burst of `dirty_restart_limit` dirty restarts inside the
    window triggers a cooldown. A clean restart resets the consecutive counter."""
    st.consecutive_restarts = st.consecutive_restarts + 1 if dirty else 0
    if dirty:
        if st.first_dirty_ts == 0.0 or (now - st.first_dirty_ts) > cfg.dirty_window_s:
            st.first_dirty_ts = now
            st.dirty_restarts = 1
        else:
            st.dirty_restarts += 1
        if st.dirty_restarts >= cfg.dirty_restart_limit:
            st.cooldown_until = now + cfg.cooldown_s
            st.dirty_restarts = 0
            st.first_dirty_ts = 0.0
    return st


def state_is_advancing(state_path: str, prev_mtime: float) -> bool:
    """True if state.json mtime is newer than prev_mtime (used by the in-phase check)."""
    try:
        return os.path.getmtime(state_path) > prev_mtime
    except OSError:
        return False
