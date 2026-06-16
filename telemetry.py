"""
AUREON v2 — telemetry.

Thread-safe notification engine with multiple sinks:
  - Console (always)
  - Log file (always, if path set)
  - Telegram (if credentials set)

Trading code calls Telemetry.send(msg, severity, tags=...).
Telegram delivery happens on a background worker thread so the trading
loop never blocks on network I/O. Failures during delivery are swallowed
and logged — telemetry must never crash the trading bot.

Severities and emojis
---------------------
  DEBUG     🔍  noisy diagnostics, log only
  INFO      ℹ️   normal operations: anchor processed, SL moved
  SUCCESS   ✅  trade closed profitably, EOD positive
  WARN      ⚠️  SL hit, time drift, anchor missed
  ERROR     ❌  order rejected, MT5 reconnect failed
  CRITICAL  🚨  kill switch, account-floor breach, repeated crashes

Rate limits per severity (Telegram only)
---------------------------------------
  DEBUG       skipped
  INFO        max 1 every 5 seconds
  SUCCESS     max 1 every 2 seconds
  WARN+       no limit (always sent)

Configuration via environment variables
---------------------------------------
  AUREON_TELEGRAM_TOKEN          bot token from @BotFather
  AUREON_TELEGRAM_CHAT           target chat id (your private chat or a group)
  AUREON_TELEGRAM_MIN_SEVERITY   INFO|SUCCESS|WARN|ERROR|CRITICAL (default INFO)
  AUREON_LOG_FILE                /var/log/aureon.log (optional)
"""

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from typing import Dict, Optional

# requests is the only network dep. Lazy-imported to keep the module usable
# even when requests isn't installed (telegram sink just disables itself).
try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False


# ============================================================================
# Severity
# ============================================================================

class Severity(IntEnum):
    DEBUG    = 10
    INFO     = 20
    SUCCESS  = 25
    WARN     = 30
    ERROR    = 40
    CRITICAL = 50


SEVERITY_EMOJI = {
    Severity.DEBUG:    "🔍",
    Severity.INFO:     "ℹ️",
    Severity.SUCCESS:  "✅",
    Severity.WARN:     "⚠️",
    Severity.ERROR:    "❌",
    Severity.CRITICAL: "🚨",
}

SEVERITY_FROM_STRING = {s.name: s for s in Severity}


# ============================================================================
# Config
# ============================================================================

@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    min_severity: Severity = Severity.INFO


# ============================================================================
# Timestamp header (v3.0.4) — the SINGLE source for every Telegram timestamp
# ============================================================================
# Server/broker clock is UTC+3; IST is broker+2:30 (= UTC+5:30). Both are derived
# from ONE captured instant in _ts_components() so server and IST can never drift
# apart. Do NOT hand-format timestamps anywhere else — call ts_header().
BROKER_UTC_OFFSET = timedelta(hours=3)
IST_FROM_BROKER = timedelta(hours=2, minutes=30)


def _ts_components(now_utc=None):
    """Return (server_dt, ist_dt) for one captured instant. server is UTC+3,
    ist is server+2:30; by construction ist - server == 2:30 exactly. `now_utc`
    is for testing (naive treated as UTC); defaults to datetime.now(UTC)."""
    base = now_utc or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base = base.astimezone(timezone.utc)
    server = base + BROKER_UTC_OFFSET
    ist = server + IST_FROM_BROKER
    return server, ist


def ts_header(now_utc=None):
    """The timestamp line prepended to EVERY outbound Telegram message:
        🕐 5:00 AM IST (server 02:30 · IST 05:00) — Tue Jun 16
    12-hour human IST first, then `server HH:MM · IST HH:MM` (24h), then the IST
    weekday + date. Derived from a single instant (see _ts_components)."""
    server, ist = _ts_components(now_utc)
    h12 = ist.hour % 12 or 12
    ampm = "AM" if ist.hour < 12 else "PM"
    return (f"🕐 {h12}:{ist.minute:02d} {ampm} IST "
            f"(server {server.hour:02d}:{server.minute:02d} · "
            f"IST {ist.hour:02d}:{ist.minute:02d}) — "
            f"{ist.strftime('%a')} {ist.strftime('%b')} {ist.day}")


# ============================================================================
# Telemetry
# ============================================================================

class Telemetry:
    """
    Singleton-ish telemetry hub. Instantiate once at program start, share
    across modules. Stop with .stop() before exit to drain the queue.
    """

    def __init__(self,
                 telegram: Optional[TelegramConfig] = None,
                 log_file: Optional[str] = None,
                 component: str = "AUREON"):
        self.telegram = telegram if telegram and _REQUESTS_OK else None
        self.component = component
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=1000)
        self._stop_event = threading.Event()
        self._last_tg_sent: Dict[Severity, float] = {}

        # Console logger
        self._log = logging.getLogger("telemetry")

        # File sink
        self._fh = None
        if log_file:
            try:
                self._fh = open(log_file, "a", buffering=1)  # line-buffered
            except OSError as e:
                self._log.warning(f"Could not open log file {log_file}: {e}")

        # Worker thread
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="telemetry-worker",
                                        daemon=True)
        self._worker.start()

        startup_msg = (f"Telemetry started "
                       f"(telegram={'on' if self.telegram else 'off'}, "
                       f"log_file={'on' if self._fh else 'off'})")
        self.send(startup_msg, Severity.DEBUG)

    # ------------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------------

    def send(self, msg: str,
             severity: Severity = Severity.INFO,
             tags: Optional[dict] = None):
        """Enqueue a message for delivery. Non-blocking, thread-safe."""
        try:
            self._queue.put_nowait({
                "ts": datetime.now(timezone.utc).isoformat(),
                "component": self.component,
                "msg": msg,
                "severity": int(severity),
                "tags": tags or {},
            })
        except queue.Full:
            # Telemetry must never block trading — drop the message
            self._log.warning("Telemetry queue full, dropping message")

    # convenience wrappers
    def debug(self, msg, **tags):    self.send(msg, Severity.DEBUG,    tags)
    def info(self, msg, **tags):     self.send(msg, Severity.INFO,     tags)
    def success(self, msg, **tags):  self.send(msg, Severity.SUCCESS,  tags)
    def warn(self, msg, **tags):     self.send(msg, Severity.WARN,     tags)
    def error(self, msg, **tags):    self.send(msg, Severity.ERROR,    tags)
    def critical(self, msg, **tags): self.send(msg, Severity.CRITICAL, tags)

    def stop(self, timeout: float = 5.0):
        """Drain queue and stop worker."""
        self._stop_event.set()
        self._worker.join(timeout=timeout)
        # Drain any remaining
        while not self._queue.empty():
            try:
                self._deliver(self._queue.get_nowait())
            except Exception:
                pass
        if self._fh:
            self._fh.close()

    # ------------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------------

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._deliver(event)
            except Exception as e:
                self._log.exception(f"Telemetry delivery failed: {e}")

    def _deliver(self, event: dict):
        sev = Severity(event["severity"])
        msg = event["msg"]

        # Console
        method = {
            Severity.DEBUG:    self._log.debug,
            Severity.INFO:     self._log.info,
            Severity.SUCCESS:  self._log.info,
            Severity.WARN:     self._log.warning,
            Severity.ERROR:    self._log.error,
            Severity.CRITICAL: self._log.critical,
        }.get(sev, self._log.info)
        method(msg)

        # File
        if self._fh:
            try:
                line = json.dumps(event) + "\n"
                self._fh.write(line)
            except Exception as e:
                self._log.warning(f"File sink error: {e}")

        # Telegram
        if self.telegram and sev >= self.telegram.min_severity:
            if not self._rate_limited(sev):
                self._send_telegram(sev, msg, event.get("tags", {}))

    # ------------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------------

    _RATE_LIMITS = {
        Severity.DEBUG:    None,        # never sent
        Severity.INFO:     5.0,         # 1 per 5s
        Severity.SUCCESS:  2.0,         # 1 per 2s
    }
    # WARN/ERROR/CRITICAL: no limit

    def _rate_limited(self, sev: Severity) -> bool:
        if sev == Severity.DEBUG:
            return True
        limit = self._RATE_LIMITS.get(sev)
        if limit is None:
            return False  # WARN+
        now = time.time()
        last = self._last_tg_sent.get(sev, 0)
        if now - last < limit:
            return True
        self._last_tg_sent[sev] = now
        return False

    # ------------------------------------------------------------------------
    # Telegram delivery
    # ------------------------------------------------------------------------

    def _send_telegram(self, sev: Severity, msg: str, tags: dict):
        emoji = SEVERITY_EMOJI.get(sev, "")
        component = self.component
        # v3.0.4: prepend the timestamp header to EVERY outbound message from the
        # SINGLE source (ts_header), captured at send time so server/IST cannot
        # drift. Every alert type (anchor/fill/close/rescue/boost/TSTOP/EOD/
        # verifyfb) inherits it here — no call site hand-formats a timestamp.
        # Markdown-safe: escape underscores in tag values
        body = f"{ts_header()}\n{emoji} *{component}*\n{msg}"
        if tags:
            tag_lines = "\n".join(f"• `{k}`: {v}" for k, v in tags.items())
            body += f"\n{tag_lines}"
        # Telegram limit is 4096 chars
        if len(body) > 4000:
            body = body[:4000] + "\n... (truncated)"
        try:
            url = f"https://api.telegram.org/bot{self.telegram.bot_token}/sendMessage"
            r = requests.post(url, json={
                "chat_id": self.telegram.chat_id,
                "text": body,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=10)
            if r.status_code != 200:
                self._log.warning(f"Telegram returned {r.status_code}: {r.text[:200]}")
                # A Markdown parse failure must never DROP a message (an unescaped
                # _/*/backtick in an interpolated value). Retry once as PLAIN text.
                if r.status_code == 400 and "parse" in r.text.lower():
                    try:
                        requests.post(url, json={
                            "chat_id": self.telegram.chat_id,
                            "text": body,
                            "disable_web_page_preview": True,
                        }, timeout=10)
                    except Exception as e2:
                        self._log.warning(f"Telegram plain-text retry failed: {e2}")
        except Exception as e:
            self._log.warning(f"Telegram send failed: {e}")


# ============================================================================
# Factory from environment
# ============================================================================

def md_escape(s):
    """Escape Telegram (legacy) Markdown specials in an INTERPOLATED value so a
    dynamic _ / * / ` / [ cannot open an entity that never closes (the boost
    can't-parse-entities 400). Escape values, not whole pre-formatted messages."""
    s = str(s)
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def telemetry_from_env(component: str = "AUREON") -> Telemetry:
    """
    Build a Telemetry instance from environment variables.
    Always returns a working Telemetry; Telegram is enabled only if both
    AUREON_TELEGRAM_TOKEN and AUREON_TELEGRAM_CHAT are set.
    """
    token = os.environ.get("AUREON_TELEGRAM_TOKEN", "").strip()
    chat  = os.environ.get("AUREON_TELEGRAM_CHAT",  "").strip()
    sev_name = os.environ.get("AUREON_TELEGRAM_MIN_SEVERITY", "INFO").upper()
    min_sev = SEVERITY_FROM_STRING.get(sev_name, Severity.INFO)

    log_file = os.environ.get("AUREON_LOG_FILE", "").strip() or None

    tg = TelegramConfig(token, chat, min_sev) if (token and chat) else None
    return Telemetry(telegram=tg, log_file=log_file, component=component)


# ============================================================================
# Self-test
# ============================================================================

if __name__ == "__main__":
    # Load .env if present (so this self-test works after setting up .env)
    try:
        from env_loader import load_env
        load_env()
    except ImportError:
        pass

    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    t = telemetry_from_env("AUREON-TEST")
    t.send("Hello from AUREON telemetry self-test",  Severity.INFO)
    t.send("This is a SUCCESS event",                Severity.SUCCESS, tags={"trade_id": 42, "pnl_usd": 153.50})
    t.send("This is a WARNING",                      Severity.WARN)
    t.send("This is an ERROR",                       Severity.ERROR)
    t.send("This is CRITICAL",                       Severity.CRITICAL)
    # Wait for delivery
    time.sleep(3)
    t.stop()
    print("Self-test done.")
