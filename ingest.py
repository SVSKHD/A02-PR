"""AUREON v3.1.9 — AUREON OS ingest emitter.

Ships structured events (logs, trades, rescue events, optional price) to the
AUREON OS FastAPI ingest endpoint so logs/trades are readable from the React app
WITHOUT SSHing the VPS. Postgres (behind FastAPI) is the system of record;
Firestore stays as the existing daily-journal mirror.

Design (mirrors the Discord client's network-robustness, because this VPS's ISP
is flaky / blocks endpoints):
  - emit() is NON-BLOCKING and is NEVER on the trading path.
  - Events queue in memory and are MIRRORED to a persistent NDJSON buffer on disk,
    so a restart or an outage never loses an event.
  - A background worker flushes batches to POST {url} with a bearer token; on
    success the acked events are dropped from the buffer; on failure it backs off
    (FailureStreak) and retries -- collapsed logging, no flood.
  - Every event carries a stable `id` so FastAPI/Postgres can DEDUP idempotently
    (a re-flush after a half-acked batch never double-inserts).

The bot holds NO database creds -- only AUREON_INGEST_URL + AUREON_INGEST_TOKEN.
"""
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

try:
    import requests
    _REQUESTS_OK = True
except Exception:
    _REQUESTS_OK = False

# FailureStreak is shared backoff infra (lives in discord_client since v3.1.3).
try:
    from discord_client import FailureStreak
except Exception:                       # pragma: no cover
    FailureStreak = None

log = logging.getLogger("AUREON")

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
DEFAULT_BATCH = 50
DEFAULT_FLUSH_S = 10.0
DEFAULT_BUFFER_CAP = 50000              # hard cap on buffered events (drop oldest)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class IngestEmitter:
    """Network-robust event emitter to the AUREON OS FastAPI ingest endpoint.

    Thread model: `emit()` is called from any thread (trading loop, telemetry
    worker) and only appends to an in-memory deque (non-blocking). A single
    background daemon thread owns the disk buffer + HTTP; a lock guards the shared
    state. Trading never blocks on the network."""

    def __init__(self, url, token, buffer_path, *, enabled=True,
                 batch=DEFAULT_BATCH, flush_s=DEFAULT_FLUSH_S,
                 buffer_cap=DEFAULT_BUFFER_CAP, logger=None, _post=None):
        self.url = (url or "").strip()
        self.token = (token or "").strip()
        self.buffer_path = buffer_path
        self.batch = int(batch)
        self.flush_s = float(flush_s)
        self._log = logger or log
        self.enabled = bool(enabled and self.url and _REQUESTS_OK)
        self._post = _post                 # injectable transport (tests)
        self._lock = threading.Lock()
        self._pending: "deque[dict]" = deque(maxlen=buffer_cap)
        self._streak = (FailureStreak("AUREON OS ingest", base_interval=15.0,
                                      max_interval=300.0, summary_every_s=300.0,
                                      logger=self._log) if FailureStreak else None)
        self._stop = threading.Event()
        self._worker = None
        self._load_buffer()
        if self.enabled:
            self._worker = threading.Thread(target=self._loop, name="ingest-worker",
                                            daemon=True)
            self._worker.start()
            self._log.info(f"AUREON OS ingest ON -> {self._host()} "
                           f"(buffered={len(self._pending)})")

    # ------------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------------
    def emit(self, event_type, payload, event_id=None):
        """Queue one event. Non-blocking; never raises. `event_id` makes the event
        idempotent on the server (e.g. 'close:123456'); auto-hashed if omitted."""
        if not self.enabled:
            return
        try:
            ev = {
                "id": str(event_id) if event_id else None,
                "type": str(event_type),
                "ts": _now_iso(),
                "payload": payload,
            }
            if ev["id"] is None:
                raw = json.dumps({"t": ev["type"], "p": payload, "ts": ev["ts"]},
                                 sort_keys=True, default=str)
                ev["id"] = f"{event_type}:{hashlib.sha1(raw.encode()).hexdigest()[:16]}"
            with self._lock:
                self._pending.append(ev)
        except Exception as e:
            self._log.debug(f"ingest emit dropped ({e!r})")

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=timeout)
        self._persist_buffer()             # final durability

    def status_line(self):
        if not self.enabled:
            return "AUREON OS ingest OFF (set AUREON_INGEST_URL/TOKEN)"
        return f"AUREON OS ingest ON -> {self._host()}"

    # ------------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.flush_s)
            try:
                self.flush_once()
            except Exception as e:
                self._log.debug(f"ingest flush error (non-fatal): {e!r}")

    def flush_once(self) -> bool:
        """Persist the buffer, then POST one batch (oldest-first). Returns True if
        a batch was acked. Safe to call directly from tests."""
        with self._lock:
            self._persist_buffer_locked()
            if not self._pending:
                return False
            batch = list(self._pending)[: self.batch]
        ok = self._send(batch)
        if ok:
            ids = {e["id"] for e in batch}
            with self._lock:
                # drop exactly the acked events (others may have arrived meanwhile)
                self._pending = deque((e for e in self._pending if e["id"] not in ids),
                                      maxlen=self._pending.maxlen)
                self._persist_buffer_locked()
            if self._streak:
                self._streak.on_success()
        else:
            if self._streak is None or self._streak.on_failure(None):
                self._log.warning(
                    f"AUREON OS ingest unreachable ({self._host()}); "
                    f"{len(self._pending)} event(s) buffered, will retry")
        return ok

    def _send(self, batch) -> bool:
        if self._post is not None:
            try:
                return bool(self._post(batch))
            except Exception:
                return False
        if not _REQUESTS_OK:
            return False
        try:
            r = requests.post(self.url, json={"events": batch},
                              headers={"Authorization": f"Bearer {self.token}",
                                       "Content-Type": "application/json"},
                              timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            return r.status_code in (200, 201, 202, 204)
        except Exception:
            return False

    # ------------------------------------------------------------------------
    # Persistent NDJSON buffer (restart-safe)
    # ------------------------------------------------------------------------
    def _load_buffer(self):
        try:
            if self.buffer_path and os.path.exists(self.buffer_path):
                with open(self.buffer_path) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self._pending.append(json.loads(line))
        except Exception as e:
            self._log.warning(f"ingest buffer load failed (starting empty): {e!r}")

    def _persist_buffer(self):
        with self._lock:
            self._persist_buffer_locked()

    def _persist_buffer_locked(self):
        if not self.buffer_path:
            return
        try:
            tmp = self.buffer_path + ".tmp"
            with open(tmp, "w") as f:
                for ev in self._pending:
                    f.write(json.dumps(ev, default=str) + "\n")
            os.replace(tmp, self.buffer_path)
        except Exception as e:
            self._log.debug(f"ingest buffer persist failed: {e!r}")

    def _host(self):
        try:
            from urllib.parse import urlparse
            return urlparse(self.url).netloc or self.url
        except Exception:
            return self.url


# ============================================================================
# Factory + module singleton (so telemetry/live can share one emitter)
# ============================================================================
_EMITTER: Optional[IngestEmitter] = None


def emitter_from_env(buffer_dir=".") -> IngestEmitter:
    """Build (once) the shared emitter from env. Always returns an IngestEmitter;
    it is simply disabled (no-op) when AUREON_INGEST_URL/TOKEN are unset."""
    global _EMITTER
    if _EMITTER is not None:
        return _EMITTER
    url = os.environ.get("AUREON_INGEST_URL", "").strip()
    token = os.environ.get("AUREON_INGEST_TOKEN", "").strip()
    enabled = os.environ.get("AUREON_INGEST_ENABLED", "on").strip().lower() \
        not in ("0", "off", "false", "no")
    try:
        batch = int(os.environ.get("AUREON_INGEST_BATCH", DEFAULT_BATCH))
    except ValueError:
        batch = DEFAULT_BATCH
    try:
        flush_s = float(os.environ.get("AUREON_INGEST_FLUSH_S", DEFAULT_FLUSH_S))
    except ValueError:
        flush_s = DEFAULT_FLUSH_S
    buf = os.path.join(buffer_dir, "ingest_buffer.ndjson")
    _EMITTER = IngestEmitter(url, token, buf, enabled=(enabled and bool(url)),
                             batch=batch, flush_s=flush_s)
    return _EMITTER


def get_emitter() -> Optional[IngestEmitter]:
    return _EMITTER
