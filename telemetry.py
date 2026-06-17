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
from collections import deque
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

# v3.0.8: all Telegram HTTP goes through telegram_net (DNS-pin past a poisoned
# ISP resolver + collapsed-log backoff). Imported lazily-safe: if it can't load,
# telemetry still works (telegram sink just uses plain requests below).
try:
    import telegram_net
    _TG_NET_OK = True
except Exception:
    _TG_NET_OK = False

# v3.1.0: Discord is the primary alert/command channel (Telegram is hard-blocked
# at the VPS ISP). Imported guarded so telemetry still works if discord_* fail.
try:
    import discord_client
    import discord_cards
    _DISCORD_OK = True
except Exception:
    _DISCORD_OK = False


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


def _clock_str(now_utc=None):
    """'5:00 AM IST (server 02:30 · IST 05:00)' for one instant — 12-hour IST
    then the server (UTC+3) and IST (broker+2:30) 24h clocks. Shared by ts_header
    and anchor_time_block so the server/IST derivation is single-source."""
    server, ist = _ts_components(now_utc)
    h12 = ist.hour % 12 or 12
    ampm = "AM" if ist.hour < 12 else "PM"
    return (f"{h12}:{ist.minute:02d} {ampm} IST "
            f"(server {server.hour:02d}:{server.minute:02d} · "
            f"IST {ist.hour:02d}:{ist.minute:02d})")


def _utc_fallback_header():
    """v3.0.7: the plain-UTC timestamp ts_header() degrades to if the normal
    server/IST derivation ever fails. Still a 🕐 line so every message visibly
    carries a stamp; the trailing tag makes the degradation auditable."""
    try:
        return f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} (utc-fallback)"
    except Exception:
        return "🕐 (timestamp unavailable)"


def ts_header(now_utc=None):
    """The timestamp line prepended to EVERY outbound Telegram message:
        🕐 5:00 AM IST (server 02:30 · IST 05:00) — Tue Jun 16
    12-hour human IST first, then `server HH:MM · IST HH:MM` (24h), then the IST
    weekday + date. Derived from a single instant (see _ts_components).

    v3.0.7 (silent-alert fix): this function must NEVER raise. A bad/None/missing
    datetime on the fill/close path used to throw here and the exception was
    swallowed by the send wrapper, dropping the message silently. On ANY internal
    error we fall back to a plain UTC string and CONTINUE -- a timestamp must
    never block a Telegram message."""
    try:
        _, ist = _ts_components(now_utc)
        return f"🕐 {_clock_str(now_utc)} — {ist.strftime('%a')} {ist.strftime('%b')} {ist.day}"
    except Exception:
        return _utc_fallback_header()


def anchor_time_block(scheduled_utc, actual_utc=None, ontime_grace_s=120):
    """v3.0.5: the scheduled-vs-actual anchor time block used by every anchor
    message (placement / LATE / MISSED / fill / close):

        scheduled: 12:30 PM IST (server 10:00 · IST 12:30)
        actual:    12:38 PM IST (server 10:08 · IST 12:38)  ⏰ +8m LATE

    Both clocks come from _clock_str (single source). The `⏰ +Nm LATE` tag is
    appended only when actual is more than ontime_grace_s after scheduled; for an
    on-time anchor actual==scheduled and the tag is omitted. Accepts datetime /
    pandas Timestamp (naive treated as UTC)."""
    sched_lbl = _clock_str(scheduled_utc)
    if actual_utc is None:
        actual_utc = scheduled_utc
    act_lbl = _clock_str(actual_utc)
    s_server, _ = _ts_components(scheduled_utc)
    a_server, _ = _ts_components(actual_utc)
    secs = (a_server - s_server).total_seconds()
    tag = f"  ⏰ +{int(secs // 60)}m LATE" if secs >= ontime_grace_s else ""
    return f"  scheduled: {sched_lbl}\n  actual:    {act_lbl}{tag}"


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
                 component: str = "AUREON",
                 discord=None,
                 alert_channels=None,
                 min_severity: "Severity" = None):
        # v3.1.0: Discord is the primary channel; Telegram is secondary/disabled.
        # alert_channels (default ["discord"]) gates which sinks are live.
        self.alert_channels = [c.strip().lower() for c in
                               (alert_channels or ["discord"]) if c.strip()]
        self.telegram = (telegram if telegram and _REQUESTS_OK
                         and "telegram" in self.alert_channels else None)
        self._discord_min_sev = min_severity if min_severity is not None else Severity.INFO
        self._discord = None
        if discord is not None and _DISCORD_OK and "discord" in self.alert_channels:
            try:
                self._discord = discord_client.DiscordClient(
                    discord, logger=logging.getLogger("discord"))
            except Exception as e:
                logging.getLogger("telemetry").warning(
                    f"Discord client init failed (non-fatal): {e!r}")
        self.component = component
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=1000)
        self._stop_event = threading.Event()
        self._last_tg_sent: Dict[Severity, float] = {}
        # v3.0.8: collapse Telegram-send failure spam into one warning + periodic
        # summary, and never raise on the trading thread (sends run on the worker).
        self._tg_streak = (telegram_net.FailureStreak("Telegram sends",
                                                      logger=logging.getLogger("telemetry"))
                           if _TG_NET_OK else None)
        # v3.0.9: critical messages (fills/closes/rescue/boost/EOD) that fail to
        # send are queued here (newest-first) and flushed on the next successful
        # connection, each carrying its ORIGINAL rendered body (ts_header intact).
        self._critical_q: "deque[str]" = deque(maxlen=50)

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
                       f"(discord={'on' if self._discord else 'off'}, "
                       f"telegram={'on' if self.telegram else 'off'}, "
                       f"log_file={'on' if self._fh else 'off'})")
        self.send(startup_msg, Severity.DEBUG)
        # v3.0.8/9: Telegram DNS-pin receipt (only if Telegram is still a channel).
        if self.telegram and _TG_NET_OK:
            telegram_net.rebuild("startup")
            telegram_net.start_refresh_timer()
            self._log.info(telegram_net.pin_status_line())
        if self._discord:
            self._log.info("Alerts: Discord (embed cards) primary")

    def discord_status_line(self) -> str:
        """One-line banner receipt of the alert channel state."""
        if self._discord:
            return "Alerts: Discord (embed cards)"
        if self.telegram:
            return "Alerts: Telegram (text)"
        return "Alerts: console/log only"

    @property
    def discord(self):
        """The DiscordClient (or None) — for the command gateway + heartbeat."""
        return self._discord

    # ------------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------------

    def send(self, msg: str,
             severity: Severity = Severity.INFO,
             tags: Optional[dict] = None,
             important: bool = False,
             critical: bool = False,
             card: Optional[dict] = None,
             event_key: Optional[str] = None):
        """Enqueue a message for delivery. Non-blocking, thread-safe.

        v3.1.0: `card` is a pre-built Discord embed (rich card) for this event; if
        omitted Discord posts a generic colored card from (severity, msg). The
        SAME msg is the Telegram/console text. `event_key` (e.g. "close:123456")
        dedups critical events on Discord so a reconnect/flush never double-posts.

        `important=True` exempts the message from per-severity rate limiting so a
        must-see event is never silently dropped. v3.0.7: fills and closes are
        sent important=True -- a fill arriving within 5s of its placement (both
        INFO) used to be rate-limited away, vanishing with no trace.

        `critical=True` (v3.0.9): fills/closes/rescue/boost/EOD. If the send fails
        (Telegram unreachable) the rendered body is QUEUED and re-sent the instant
        any connection succeeds, so the operator never has to open MT5 to learn a
        fill/close happened."""
        try:
            self._queue.put_nowait({
                "ts": datetime.now(timezone.utc).isoformat(),
                "component": self.component,
                "msg": msg,
                "severity": int(severity),
                "tags": tags or {},
                "important": bool(important),
                "critical": bool(critical),
                "card": card,
                "event_key": event_key,
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

        # Decide rate-limit/important ONCE so every channel agrees. important
        # events (fills/closes) bypass rate limiting so a must-see event is never
        # silently throttled away.
        allow = bool(event.get("important")) or not self._rate_limited(sev)

        # Discord (v3.1.0 primary): rich embed cards, dedup by event_key.
        if self._discord and sev >= self._discord_min_sev and allow:
            self._discord.deliver(sev.name, msg, card=event.get("card"),
                                  event_key=event.get("event_key"),
                                  critical=bool(event.get("critical")))

        # Telegram (secondary/disabled by default).
        if self.telegram and sev >= self.telegram.min_severity and allow:
            self._send_telegram(sev, msg, event.get("tags", {}),
                                critical=bool(event.get("critical")))

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

    def _send_telegram(self, sev: Severity, msg: str, tags: dict,
                       critical: bool = False):
        emoji = SEVERITY_EMOJI.get(sev, "")
        component = self.component
        # v3.0.4: prepend the timestamp header to EVERY outbound message from the
        # SINGLE source (ts_header), captured at send time so server/IST cannot
        # drift. Every alert type (anchor/fill/close/rescue/boost/TSTOP/EOD/
        # verifyfb) inherits it here — no call site hand-formats a timestamp.
        body = f"{ts_header()}\n{emoji} *{component}*\n{msg}"
        if tags:
            tag_lines = "\n".join(f"• `{k}`: {v}" for k, v in tags.items())
            body += f"\n{tag_lines}"
        # Telegram limit is 4096 chars
        if len(body) > 4000:
            body = body[:4000] + "\n... (truncated)"

        if self._post_body(body):
            if self._tg_streak:
                self._tg_streak.on_success()
            self._flush_critical()          # ride a good connection to drain queue
        else:
            # v3.0.7: never silent. v3.0.8: collapse the flood (first failure of a
            # streak logged fully + body, then summaries). v3.0.9: queue criticals.
            if self._tg_streak is None or self._tg_streak.on_failure(None):
                self._log.warning(f"Telegram send failed (unreachable) | body was:\n{body}")
            if critical:
                self._critical_q.append(body)

    def _post_body(self, body: str) -> bool:
        """POST one fully-rendered body to Telegram. Returns True if Telegram was
        REACHED (any HTTP reply), False if the connection failed even after a
        fresh-connect retry. Never raises. v3.0.9: a failure on the (transient)
        pooled session is retried ONCE on a brand-new fresh-resolved connection —
        many jams are a single dead socket that a fresh connect gets through."""
        _http = telegram_net if _TG_NET_OK else None
        url = f"https://api.telegram.org/bot{self.telegram.bot_token}/sendMessage"
        payload = {"chat_id": self.telegram.chat_id, "text": body,
                   "parse_mode": "Markdown", "disable_web_page_preview": True}
        for fresh in (False, True):
            try:
                if _http:
                    r = _http.post(url, json=payload, fresh=fresh)
                else:
                    r = requests.post(url, json=payload, timeout=(5, 10))
                if r.status_code != 200:
                    self._log.warning(f"Telegram returned {r.status_code}: {r.text[:200]}")
                    # A Markdown parse failure must never DROP a message (an
                    # unescaped _/*/backtick). Retry once as PLAIN text.
                    if r.status_code == 400 and "parse" in r.text.lower():
                        plain = {"chat_id": self.telegram.chat_id, "text": body,
                                 "disable_web_page_preview": True}
                        try:
                            (_http.post(url, json=plain) if _http
                             else requests.post(url, json=plain, timeout=(5, 10)))
                        except Exception as e2:
                            self._log.warning(
                                f"Telegram plain-text retry failed: {e2} | body was:\n{body}")
                return True                  # reached Telegram (any HTTP reply)
            except Exception:
                if not _http:
                    return False             # no fresh-connect path without telegram_net
                continue                     # fall through to the fresh retry
        return False

    def _flush_critical(self):
        """Drain queued critical messages NEWEST-FIRST on a good connection, each
        with its original ts_header body. Stops (and re-queues) on the first
        failure so order/state survive a flaky connection."""
        while self._critical_q:
            body = self._critical_q.pop()    # rightmost = newest
            if not self._post_body(body):
                self._critical_q.append(body)
                break


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
    v3.1.0: Discord is the primary channel (enabled with DISCORD_BOT_TOKEN +
    DISCORD_CHANNEL_ID). Telegram is secondary, active only if "telegram" is in
    AUREON_ALERT_CHANNELS (default "discord"). Always returns a working Telemetry.
    """
    token = os.environ.get("AUREON_TELEGRAM_TOKEN", "").strip()
    chat  = os.environ.get("AUREON_TELEGRAM_CHAT",  "").strip()
    sev_name = os.environ.get("AUREON_TELEGRAM_MIN_SEVERITY", "INFO").upper()
    min_sev = SEVERITY_FROM_STRING.get(sev_name, Severity.INFO)

    log_file = os.environ.get("AUREON_LOG_FILE", "").strip() or None

    raw_channels = os.environ.get("AUREON_ALERT_CHANNELS", "discord")
    channels = [c.strip().lower() for c in raw_channels.split(",") if c.strip()] \
        or ["discord"]

    tg = TelegramConfig(token, chat, min_sev) if (token and chat) else None
    dc = discord_client.config_from_env() if _DISCORD_OK else None
    return Telemetry(telegram=tg, log_file=log_file, component=component,
                     discord=dc, alert_channels=channels, min_severity=min_sev)


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
