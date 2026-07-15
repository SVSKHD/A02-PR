"""Rogue T2 — Discord notifications.

Thin, fully guarded webhook poster. No webhook configured -> a silent no-op (used
in tests). Never raises onto the trading loop. Also exposes an in-memory sink so
tests can assert what WOULD have been sent.
"""
from __future__ import annotations

import json
import logging
import os
from typing import List, Optional
from urllib import request as _urlrequest

log = logging.getLogger("ROGUE_T2")


class Notifier:
    def __init__(self, webhook_url: Optional[str] = None, capture: bool = False):
        self.webhook_url = webhook_url or os.environ.get("ROGUE_T2_DISCORD_WEBHOOK")
        self.capture = capture
        self.sent: List[str] = []   # in-memory record (always populated)

    def _post(self, content: str) -> None:
        if not self.webhook_url:
            return
        try:
            data = json.dumps({"content": content[:1900]}).encode()
            req = _urlrequest.Request(
                self.webhook_url, data=data,
                headers={"Content-Type": "application/json"})
            _urlrequest.urlopen(req, timeout=5)  # noqa: S310 (trusted webhook)
        except Exception as e:  # never break the loop on a notify failure
            log.warning(f"notify post failed: {e!r}")

    def send(self, content: str) -> None:
        self.sent.append(content)
        log.info(f"[notify] {content}")
        self._post(content)

    # --- typed helpers (every event the spec requires a notification for) --------
    def fill(self, tag, side, price, lot):
        self.send(f"🟢 FILL {tag} {side} {lot} @ {price}")

    def exit(self, tag, side, price, slippage, pnl):
        self.send(f"🔴 EXIT {tag} {side} @ {price} (slip {slippage:+.2f}) pnl {pnl:+.2f}")

    def phase_start(self, phase_idx, a1):
        self.send(f"⏱️ PHASE {phase_idx} start — A1 {a1}")

    def halt(self, reason, day_pnl):
        self.send(f"⛔ HALT ({reason}) day_pnl {day_pnl:+.2f} — flat until next IST day")

    def guard(self, reason, detail):
        self.send(f"⚠️ GUARD {reason}: {detail}")

    def restart(self, backoff_s, dirty):
        self.send(f"♻️ RESTART (backoff {backoff_s}s, dirty {dirty})")

    def reconcile(self, what, ticket):
        self.send(f"🔧 RECONCILE adopt {what} ticket={ticket}")

    def daily_summary(self, trades, win_rate, day_pnl, max_spread):
        self.send(f"📊 DAILY: trades {trades} · win {win_rate:.0%} · "
                  f"pnl {day_pnl:+.2f} · max_spread {max_spread:.2f}")
