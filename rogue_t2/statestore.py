"""Rogue T2 — durable state.

Persist-on-change only, atomic write (temp + os.replace), and every persisted blob
carries the config hash and git commit that produced it. The halt flag (daily-cap
breach) is keyed by IST calendar day so a restart on the SAME day stays halted and a
new day clears it.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional


def config_hash(cfg: Any) -> str:
    """Short stable hash of the config values (frozen-spec fingerprint)."""
    if is_dataclass(cfg):
        payload = asdict(cfg)
    elif isinstance(cfg, dict):
        payload = cfg
    else:
        payload = {k: getattr(cfg, k) for k in dir(cfg)
                   if not k.startswith("_") and not callable(getattr(cfg, k))}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def git_commit(default: str = "unknown") -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            return out.stdout.strip() or default
    except Exception:
        pass
    return default


class StateStore:
    """Atomic JSON state with config/commit provenance. Writes only when the blob
    actually changed (persist-on-change)."""

    def __init__(self, path: str, cfg: Any):
        self.path = path
        self.cfg_hash = config_hash(cfg)
        self.commit = git_commit()
        self._last_serialized: Optional[str] = None
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
                self._last_serialized = json.dumps(data, sort_keys=True)
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self) -> bool:
        """Atomic persist-on-change. Returns True if a write happened."""
        self.data["_config_hash"] = self.cfg_hash
        self.data["_git_commit"] = self.commit
        serialized = json.dumps(self.data, sort_keys=True)
        if serialized == self._last_serialized:
            return False
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(serialized)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self._last_serialized = serialized
        return True

    # --- halt (daily-cap breach), keyed by IST day -------------------------------
    def is_halted(self, ist_day: str) -> bool:
        return self.data.get("halt_day") == ist_day

    def set_halt(self, ist_day: str, reason: str) -> None:
        self.data["halt_day"] = ist_day
        self.data["halt_reason"] = reason
        self.save()

    def clear_halt_if_new_day(self, ist_day: str) -> None:
        if self.data.get("halt_day") not in (None, ist_day):
            self.data.pop("halt_day", None)
            self.data.pop("halt_reason", None)
            self.save()

    # --- idempotency: which idem keys have been placed already --------------------
    def placed_keys(self) -> Dict[str, Any]:
        return self.data.setdefault("placed", {})

    def mark_placed(self, idem_key: str, ticket: Any) -> None:
        self.placed_keys()[idem_key] = ticket
        self.save()

    def was_placed(self, idem_key: str) -> bool:
        return idem_key in self.placed_keys()

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()
