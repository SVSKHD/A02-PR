"""AUREON v3.1.0 — Discord client: sole alert + command channel.

Telegram is hard-IP-blocked at the VPS ISP; Discord (discord.com) is reachable.
This module SENDS rich embed cards via the Discord REST API (requests — works
with NO discord.py installed) and, optionally, runs a gateway bot (discord.py) to
RECEIVE commands. Both run OFF the trading path: sends are driven by the telemetry
worker thread, the command gateway runs in its own daemon thread. Trading never
blocks on Discord.

Design:
  - SEND  = REST POST /channels/{id}/messages with the bot token (webhook URL is a
            fallback). Backoff + quiet summaries on failure (FailureStreak).
  - DEDUP = critical events by EVENT KEY (e.g. "close:123456"), so a reconnect or
            queue-flush never double-posts; general messages by content hash in a
            60s bucket.
  - QUEUE = failed CRITICAL cards are queued (cap 50) and flushed NEWEST-FIRST on
            the next success, dedup-checked so a flush can't double-post.
  - CMDS  = gateway verifies Message Content Intent and warns LOUDLY if OFF.
"""
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Set

import discord_cards as cards

try:
    import requests
    _REQUESTS_OK = True
except Exception:
    _REQUESTS_OK = False

log = logging.getLogger("AUREON")

DISCORD_API = "https://discord.com/api/v10"
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0


# ============================================================================
# FailureStreak — exponential backoff + collapsed logging (shared infra)
# ============================================================================
class FailureStreak:
    """Tracks consecutive failures for one channel (sends or polls):
      - on_failure(): log the FIRST failure of a streak fully (returns True);
        suppress the rest and emit ONE summary line every `summary_every_s`.
      - interval(): the current retry/poll interval — `base` for the first few
        failures, then doubling once past 3 consecutive, capped at `cap`.
      - on_success(): reset; log a one-line recovery if we had been failing.
    Not thread-safe by design — each channel owns its own instance on one thread.
    """

    def __init__(self, name, base_interval=30.0, max_interval=300.0,
                 summary_every_s=300.0, logger=None):
        self.name = name
        self.base = float(base_interval)
        self.cap = float(max_interval)
        self.summary_every = float(summary_every_s)
        self.log = logger or log
        self.count = 0
        self._first_ts = None
        self._last_summary_ts = 0.0
        self._suppressed = 0

    def interval(self):
        if self.count <= 2:
            return self.base
        # 3 consecutive -> 2x base, then keep doubling, capped.
        return min(self.base * (2 ** (self.count - 2)), self.cap)

    def on_failure(self, err=None):
        """Record a failure; return True if the caller should log it FULLY now."""
        now = time.time()
        self.count += 1
        if self.count == 1:
            self._first_ts = now
            self._last_summary_ts = now
            self._suppressed = 0
            return True
        self._suppressed += 1
        if now - self._last_summary_ts >= self.summary_every:
            mins = (now - self._first_ts) / 60.0
            self.log.warning(
                f"{self.name} unreachable for {mins:.0f}m, "
                f"{self._suppressed} attempt(s) failed since last summary "
                f"(suppressing per-attempt logs)")
            self._last_summary_ts = now
            self._suppressed = 0
        return False

    def on_success(self):
        if self.count > 0:
            self.log.info(f"{self.name} reachable again (after {self.count} "
                          f"consecutive failure(s))")
        self.count = 0
        self._first_ts = None
        self._suppressed = 0


@dataclass
class DiscordConfig:
    bot_token: str
    channel_id: str
    allowed_user_ids: Set[str] = field(default_factory=set)
    webhook_url: str = ""


def config_from_env() -> Optional[DiscordConfig]:
    """Build a DiscordConfig from the environment, or None if not configured."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    chan = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    allowed = {u.strip() for u in
               os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",") if u.strip()}
    if not token or not chan:
        return None
    return DiscordConfig(bot_token=token, channel_id=chan,
                         allowed_user_ids=allowed, webhook_url=webhook)


class DiscordClient:
    """Thread-safe-enough Discord sender + optional command gateway. `deliver()`
    is called from the single telemetry worker thread; the gateway thread only
    calls post_card() for replies. A lock serializes the actual HTTP + queues."""

    def __init__(self, cfg: DiscordConfig, logger=None):
        self.cfg = cfg
        self._log = logger or log
        self._lock = threading.Lock()
        self._streak = FailureStreak("Discord", base_interval=30.0,
                                     max_interval=300.0, summary_every_s=300.0,
                                     logger=self._log)
        # Dedup state.
        self._seen_keys: "deque[str]" = deque(maxlen=1000)   # event keys posted
        self._seen_set: Set[str] = set()
        self._recent_hashes = {}                              # content hash -> ts
        # Failed CRITICAL cards awaiting a good connection (newest at right).
        self._critical_q: "deque[tuple]" = deque(maxlen=50)  # (event_key, embed)
        self._last_hb_sig = None
        self._intent_warned = False
        self.gateway_state = "not-started"                   # for selftest reporting

    # ------------------------------------------------------------------------
    # Dedup helpers
    # ------------------------------------------------------------------------
    def _key_seen(self, key):
        return key in self._seen_set

    def _mark_key(self, key):
        if key in self._seen_set:
            return
        if len(self._seen_keys) == self._seen_keys.maxlen:
            old = self._seen_keys[0]
            self._seen_set.discard(old)
        self._seen_keys.append(key)
        self._seen_set.add(key)

    def _hash_dup(self, embed, window_s=60.0):
        """True if an identical card was sent within `window_s` (general dedup)."""
        try:
            raw = json.dumps({"t": embed.get("title"),
                              "d": embed.get("description"),
                              "f": embed.get("fields")}, sort_keys=True)
        except Exception:
            raw = str(embed)
        h = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()
        now = time.time()
        # prune
        for k in [k for k, ts in self._recent_hashes.items() if now - ts > window_s]:
            self._recent_hashes.pop(k, None)
        if h in self._recent_hashes:
            return True
        self._recent_hashes[h] = now
        return False

    # ------------------------------------------------------------------------
    # Public send API (called from the telemetry worker thread)
    # ------------------------------------------------------------------------
    def deliver(self, severity_name, text, card=None, event_key=None,
                critical=False):
        """Post one alert. `card` is a pre-built embed (rich card); if None a
        generic colored card is built from (severity, text). Dedups, posts, and
        on failure queues CRITICAL cards for later flush. Never raises."""
        try:
            embed = card or cards.card_generic(
                f"AUREON {severity_name}", text,
                cards.SEVERITY_COLOR.get(severity_name, cards.GREY))
            with self._lock:
                if event_key:
                    if self._key_seen(event_key):
                        return                      # already posted this event
                elif self._hash_dup(embed):
                    return                          # general 60s content dedup
                ok = self._post_embed(embed)
                if ok:
                    if event_key:
                        self._mark_key(event_key)
                    if self._streak:
                        self._streak.on_success()
                    self._flush_critical()
                else:
                    if self._streak is None or self._streak.on_failure(None):
                        self._log.warning(
                            f"Discord send failed (unreachable) | title="
                            f"{embed.get('title')!r}")
                    if critical:
                        self._critical_q.append((event_key, embed))
        except Exception as e:
            self._log.warning(f"Discord deliver error (non-fatal): {e!r}")

    def post_card(self, embed):
        """Fire a single card immediately (connect/intent/command replies). Used
        by the gateway thread; lock-guarded. Never raises; returns reached bool."""
        try:
            with self._lock:
                return self._post_embed(embed)
        except Exception as e:
            self._log.warning(f"Discord post_card error: {e!r}")
            return False

    def heartbeat(self, embed, signature=None):
        """Post a heartbeat card unless nothing changed since the last one
        (signature equal). Low priority — never queued, never preempts."""
        if signature is not None and signature == self._last_hb_sig:
            return
        self._last_hb_sig = signature
        with self._lock:
            self._post_embed(embed)

    # ------------------------------------------------------------------------
    # Critical queue flush (newest-first, dedup-checked)
    # ------------------------------------------------------------------------
    def _flush_critical(self):
        while self._critical_q:
            event_key, embed = self._critical_q.pop()       # newest first
            if event_key and self._key_seen(event_key):
                continue                                    # already delivered
            if self._post_embed(embed):
                if event_key:
                    self._mark_key(event_key)
            else:
                self._critical_q.append((event_key, embed))  # put back, stop
                break

    # ------------------------------------------------------------------------
    # Actual HTTP (bot REST, webhook fallback). Caller holds the lock.
    # ------------------------------------------------------------------------
    def _post_embed(self, embed) -> bool:
        if not _REQUESTS_OK:
            return False
        url = f"{DISCORD_API}/channels/{self.cfg.channel_id}/messages"
        headers = {"Authorization": f"Bot {self.cfg.bot_token}",
                   "Content-Type": "application/json"}
        try:
            r = requests.post(url, headers=headers, json={"embeds": [embed]},
                              timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if r.status_code in (200, 201):
                return True
            if r.status_code == 429:                        # rate limited
                self._log.warning(f"Discord 429 rate-limited: {r.text[:120]}")
                return False
            self._log.warning(f"Discord HTTP {r.status_code}: {r.text[:160]}")
            # fall through to webhook on auth/other failures
        except Exception as e:
            self._log.debug(f"Discord bot POST failed: {e!r}")
        return self._post_webhook(embed)

    def _post_webhook(self, embed) -> bool:
        if not (self.cfg.webhook_url and _REQUESTS_OK):
            return False
        try:
            r = requests.post(self.cfg.webhook_url, json={"embeds": [embed]},
                              timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            return r.status_code in (200, 204)
        except Exception as e:
            self._log.debug(f"Discord webhook POST failed: {e!r}")
            return False

    # ------------------------------------------------------------------------
    # Command gateway (discord.py optional). Runs in its own daemon thread.
    # ------------------------------------------------------------------------
    def start_gateway(self, command_handler):
        """Start the gateway bot to RECEIVE commands. `command_handler(cmd, text)`
        is called for each '/...' message in the configured channel from an
        allowed user. If discord.py is missing or the Message Content Intent is
        OFF, sending still works (REST) — only commands are disabled, loudly."""
        try:
            import discord
        except Exception:
            self.gateway_state = "no-discord.py"
            self._log.warning(
                "discord.py not installed — Discord COMMANDS disabled (alerts "
                "still send via REST). Run: pip install discord.py")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            self.gateway_state = "connected"
            self._log.info(f"Discord gateway connected as {client.user}")
            self.post_card(cards.card_connect())

        @client.event
        async def on_message(message):
            try:
                if message.author == client.user or message.author.bot:
                    return
                if str(message.channel.id) != str(self.cfg.channel_id):
                    return
                if (self.cfg.allowed_user_ids and
                        str(message.author.id) not in self.cfg.allowed_user_ids):
                    return
                content = (message.content or "").strip()
                if not content:
                    # Empty content => Message Content Intent almost certainly OFF.
                    self._warn_intent_off()
                    return
                if content.startswith("/"):
                    command_handler(content.split()[0], content)
            except Exception as e:
                self._log.warning(f"Discord command error: {e!r}")

        def _run():
            try:
                client.run(self.cfg.bot_token, log_handler=None)
            except Exception as e:
                # PrivilegedIntentsRequired (intent OFF in portal) lands here.
                name = type(e).__name__
                if "PrivilegedIntents" in name or "intent" in str(e).lower():
                    self.gateway_state = "intent-off"
                    self._warn_intent_off()
                else:
                    self.gateway_state = f"error:{name}"
                    self._log.warning(f"Discord gateway stopped: {e!r}")

        threading.Thread(target=_run, name="discord-gateway", daemon=True).start()

    def _warn_intent_off(self):
        if self._intent_warned:
            return
        self._intent_warned = True
        self._log.warning("⚠️ Discord Message Content Intent OFF — alerts work, "
                          "COMMANDS WILL NOT. Enable it in the Developer Portal.")
        self.post_card(cards.card_intent_warning())
