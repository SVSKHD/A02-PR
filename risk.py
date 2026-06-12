"""
AUREON — risk controls (RiskMixin): daily kill switch + EOD/manual flatten.

_check_kill_switch measures loss from the day's OPENING equity (live equity in
live mode, realized daily_pnl in paper). _flatten_all is the hardened EOD close:
retry-with-verify on every position/pending, critical alert if anything is left
open. _eod_reached gates the EOD branch.

Methods extracted verbatim from live_trader.py (v3.0.0 refactor). Byte-identical.
"""

import logging
from datetime import date as DateType

import pandas as pd

log = logging.getLogger("AUREON")


class RiskMixin:
    def _check_kill_switch(self) -> bool:
        """
        Daily kill switch — fires if losses exceed cfg.daily_loss_pct of the
        DAY'S OPENING equity (v2.5.4). Previously measured from cfg.starting_balance,
        a fixed baseline, which double-counted prior-day losses and the start-of-day
        gap — that's what gated A2/A3/A4 off after A1's loss on 2026-05-28.
        In LIVE mode: uses live equity (includes unrealized P&L), which matches how
                      Funding Pips actually measures the daily loss rule.
        In PAPER mode: falls back to internal daily_pnl (realized only).
        """
        self._ensure_day_start_equity()
        base = self.state.get('day_start_equity') or self.cfg.starting_balance
        threshold = self.cfg.daily_loss_pct * base
        equity = self._live_equity()
        if equity is not None:
            live_daily_loss = base - equity
            return live_daily_loss >= threshold
        # Fallback (paper or MT5 query failure)
        return self.state['daily_pnl'] <= -threshold

    def _ensure_day_start_equity(self):
        """v2.5.4: lazily backfill today's opening equity if missing (e.g. the bot
        is restarted mid-day before any new-broker-day reset has run). Reconstructs
        opening equity = current balance - realized daily P&L, so the kill baseline
        is correct immediately on restart instead of falling back to starting_balance."""
        if self.state.get('day_start_equity') is not None:
            return
        base = self.cfg.starting_balance
        try:
            info = self.adapter.get_account_info() if self.adapter else None
            if info and info.get('balance') is not None:
                base = info['balance'] - self.state.get('daily_pnl', 0.0)
        except Exception as e:
            log.warning(f"_ensure_day_start_equity: account read failed ({e}); "
                        f"using starting_balance ${base:,.2f}")
        self.state['day_start_equity'] = base
        self._save_state()
        log.info(f"Daily kill baseline backfilled to opening equity ${base:,.2f}")

    def _eod_reached(self, broker_date: DateType, utc_now: pd.Timestamp) -> bool:
        eod = self._eod_datetime_utc(broker_date, self.cfg)
        return utc_now >= eod

    def _flatten_all(self, reason: str = "Manual"):
        """v2.5: hardened EOD flatten — retries up to 3x on rc=-1, verifies via broker query.
        Critical: if a position fails to close at EOD, it stays open OVERNIGHT
        with bracket SL, and the bot loses tracking. Worth fighting for the close."""
        self.tele.warn(f"FLATTEN ({reason}) — closing {len(self.shadow_positions)} positions, "
                       f"cancelling {len(self.shadow_pendings)} pendings")

        import time as _time

        # Close positions with retry+verify
        failed_closes = []
        for ticket in list(self.shadow_positions.keys()):
            closed = False
            for attempt in range(3):
                try:
                    result = self.adapter.close_position(ticket, dry_run=self.paper)
                    if self.paper:
                        closed = True
                        break
                    # Verify by querying broker
                    _time.sleep(0.3)
                    still_open = self.adapter.mt5.positions_get(ticket=ticket)
                    if not still_open:
                        closed = True
                        log.info(f"Position {ticket} verified closed (attempt {attempt+1})")
                        break
                    else:
                        log.warning(f"Position {ticket} still open after close attempt {attempt+1} — retrying")
                except Exception as e:
                    log.warning(f"Close {ticket} attempt {attempt+1} raised: {e}")
                _time.sleep(0.5)
            if not closed:
                failed_closes.append(ticket)
            self.shadow_positions.pop(ticket, None)

        # Cancel pendings with retry+verify
        failed_cancels = []
        for ticket in list(self.shadow_pendings.keys()):
            cancelled = False
            for attempt in range(3):
                try:
                    self.adapter.cancel_order(ticket, dry_run=self.paper)
                    if self.paper:
                        cancelled = True
                        break
                    _time.sleep(0.2)
                    still_pending = self.adapter.mt5.orders_get(ticket=int(ticket))
                    if not still_pending:
                        cancelled = True
                        break
                except Exception as e:
                    log.warning(f"Cancel {ticket} attempt {attempt+1} raised: {e}")
                _time.sleep(0.3)
            if not cancelled:
                failed_cancels.append(ticket)
            self.shadow_pendings.pop(ticket, None)

        # v2.5.2: if a deferred anchor was queued, drop it on flatten (consistent state)
        if self._deferred_anchor is not None:
            log.info(f"Flatten dropped deferred anchor: {self._deferred_anchor.get('label')}")
            self._deferred_anchor = None

        # Critical alert if anything failed to close — these are real money exposure
        if failed_closes or failed_cancels:
            self.tele.critical(
                f"🚨 *FLATTEN INCOMPLETE — manual intervention needed*\n"
                f"Failed to close {len(failed_closes)} positions: `{failed_closes}`\n"
                f"Failed to cancel {len(failed_cancels)} pendings: `{failed_cancels}`\n"
                f"These remain at broker with bracket SL but no bot tracking.\n"
                f"Check MT5 terminal manually."
            )
