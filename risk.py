"""AUREON — risk: safe-lot sizing, kill switch, day-start equity, EOD flatten.

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


def _compute_safe_lot(self, balance: float) -> float:
    """
    Return the largest safe lot under Funding Pips per-trade risk rules.
    3% on accounts <$50k, 2% on ≥$50k. Apply slippage buffer + conservatism.
    Rounds DOWN to broker's actual volume_step precision (v2.5: validated).
    """
    risk_pct = (self.cfg.risk_pct_over_50k if balance >= 50_000
                else self.cfg.risk_pct_under_50k)
    max_loss = balance * risk_pct * self.cfg.slippage_buffer
    # SL distance × oz per lot (contract_size assumed 100 for XAUUSD)
    max_lot = max_loss / (self.cfg.sl_dist * 100)
    # Apply user conservatism multiplier
    effective_lot = max_lot * self.cfg.lot_conservatism

    # v2.5: validate against broker's actual volume_step/min/max
    try:
        si = self.adapter.mt5.symbol_info(self.cfg.symbol)
        if si is not None:
            step = si.volume_step if si.volume_step > 0 else 0.01
            vmin = si.volume_min if si.volume_min > 0 else 0.01
            vmax = si.volume_max if si.volume_max > 0 else 100.0
            # Floor to broker step
            steps = int(effective_lot / step)
            effective_lot = max(vmin, min(vmax, steps * step))
            # Round to step decimal precision for cleanness
            effective_lot = round(effective_lot, 2)
        else:
            effective_lot = max(0.01, int(effective_lot * 100) / 100)
    except Exception as e:
        log.warning(f"Lot validation against broker volume_step failed: {e}")
        effective_lot = max(0.01, int(effective_lot * 100) / 100)

    # v2.5.3: Hard lot cap for FP Zero 1% floating-loss compliance.
    # No-op when FP_ZERO_MAX_LOT is None (Pepperstone demo testing).
    if self.FP_ZERO_MAX_LOT is not None and effective_lot > self.FP_ZERO_MAX_LOT:
        log.info(
            f"Lot capped {effective_lot} → {self.FP_ZERO_MAX_LOT} "
            f"(FP_ZERO_MAX_LOT compliance)"
        )
        effective_lot = self.FP_ZERO_MAX_LOT
    return effective_lot


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


def _flatten_all(self, reason: str = "Manual", scope: str = "ALL"):
    """v2.5: hardened EOD flatten — retries up to 3x on rc=-1, verifies via broker query.
    Critical: if a position fails to close at EOD, it stays open OVERNIGHT
    with bracket SL, and the bot loses tracking. Worth fighting for the close.

    v3.6.0 scope: "ALL" (default -- every existing caller, byte-identical) also
    force-closes the open Rogue ticket on a non-EOD reason (the E-15 block below);
    "ANCHORS" (the /anchors flatten confirm command) touches ONLY the anchor
    engine's book -- shadow_positions/shadow_pendings hold exclusively magic-
    20260522 legs (Rogue's ticket lives in self._rogue['open'], never here) and
    the Rogue force-close is skipped, so a scoped flatten can never close a
    Rogue 20260626 ticket.

    v3.7.0 scope: "ALL" now ALSO force-closes the open Fetcher ticket (magic
    20260707) on a non-EOD reason; "FETCHER" (the /fetcher flatten confirm command)
    returns early having touched ONLY the Fetcher book (its ticket lives in
    self._fetcher['open'], never in shadow_positions), so it can never close an
    anchor or Rogue ticket -- the anchor shadow loop below is skipped entirely."""
    if str(scope) == "FETCHER":
        # scoped Fetcher flatten: magic 20260707 ONLY (never the anchor shadow book).
        try:
            import fetcher as _fetcher
            self.tele.warn(f"FLATTEN ({reason}) [FETCHER-scoped: anchors + Rogue untouched]")
            _fetcher.force_close_open(self, reason=reason)
            _fetcher.cancel_pendings(self, reason=reason)
        except Exception as e:
            log.warning(f"fetcher scoped flatten failed (non-fatal): {e!r}")
        return
    self.tele.warn(f"FLATTEN ({reason}) — closing {len(self.shadow_positions)} positions, "
                   f"cancelling {len(self.shadow_pendings)} pendings"
                   + (" [ANCHORS-scoped: Rogue untouched]" if scope == "ANCHORS" else ""))

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
            # D-6: keep it tracked on a genuine failure -- a caller polling until
            # broker-verified flat (the Friday weekend-hold-ban loop) needs this
            # ticket to still be in shadow_positions on its NEXT _flatten_all()
            # call, or the retry has nothing left to retry. Previously popped
            # unconditionally, silently dropping tracking of a still-open position.
        else:
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
            # D-6: same reasoning as the position loop above -- stay tracked so a
            # polling caller's next pass retries it.
        else:
            self.shadow_pendings.pop(ticket, None)

    # v2.5.2: if a deferred anchor was queued, drop it on flatten (consistent state)
    if self._deferred_anchor is not None:
        log.info(f"Flatten dropped deferred anchor: {self._deferred_anchor.get('label')}")
        self._deferred_anchor = None

    # Fix 3 (E-15): a kill-switch / manual flatten must ALSO close any open Rogue ticket
    # (magic 20260626). Rogue rides its own magic, so the anchor close loop above never
    # touches it. EOD is EXCLUDED here: rogue.eod_flatten (gated on rogue_flatten_at_eod,
    # default OFF) owns the EOD decision so a deliberate post-EOD ride is preserved.
    # Rogue-scoped + guarded so it can never block the anchor flatten.
    # v3.6.0: an ANCHORS-scoped flatten (/anchors flatten confirm) also skips this --
    # it must only ever touch magic 20260522.
    if str(reason) != "EOD" and str(scope) != "ANCHORS":
        try:
            import rogue as _rogue
            _rogue.force_close_open(self, reason=reason)
        except Exception as e:
            log.warning(f"rogue force_close_open during flatten failed (non-fatal): {e!r}")
    # v3.7.0: the same kill-switch / manual flatten must ALSO close any open Fetcher ticket
    # (magic 20260707) -- it rides its own magic, untouched by the anchor loop. EOD is
    # EXCLUDED (fetcher.eod_flatten owns the EOD ride decision, default ON). An ANCHORS-
    # scoped flatten skips it. Fetcher-scoped already returned early above.
    if str(reason) != "EOD" and str(scope) != "ANCHORS":
        try:
            import fetcher as _fetcher
            _fetcher.force_close_open(self, reason=reason)
        except Exception as e:
            log.warning(f"fetcher force_close_open during flatten failed (non-fatal): {e!r}")

    # Critical alert if anything failed to close — these are real money exposure
    if failed_closes or failed_cancels:
        self.tele.critical(
            f"🚨 *FLATTEN INCOMPLETE — manual intervention needed*\n"
            f"Failed to close {len(failed_closes)} positions: `{failed_closes}`\n"
            f"Failed to cancel {len(failed_cancels)} pendings: `{failed_cancels}`\n"
            f"These remain at broker with bracket SL but no bot tracking.\n"
            f"Check MT5 terminal manually."
        )
