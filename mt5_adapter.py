"""AUREON — MT5Adapter + retcode map (split from bot.py, v3.0.0).

The ONE module that imports MetaTrader5 (lazily, in __init__). Byte-identical
except the commit-1 Fix B diagnostics already in place_market_order.
"""
import logging
import time
from typing import Optional

import pandas as pd

log = logging.getLogger("AUREON")


_MT5_RETCODE_MAP = {
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",  # ← success
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",  # ← stop price on wrong side of market
    10016: "INVALID_STOPS",  # ← SL/TP on wrong side
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
    10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS",
    10025: "NO_CHANGES",
    10026: "SERVER_DISABLES_AT",
    10027: "CLIENT_DISABLES_AT",
    10028: "LOCKED",
    10029: "FROZEN",
    10030: "INVALID_FILL",
    10031: "CONNECTION",
    10032: "ONLY_REAL",
    10033: "LIMIT_ORDERS",
    10034: "LIMIT_VOLUME",
}



def mt5_comment(s):
    """MetaTrader5 silently rejects an order `comment` longer than 31 chars with
    (-2, \'Invalid "comment" argument\') -- the 0-for-7 boost root cause (Jun-15
    A3: `AUREONv2_A3_1340_Overlap_SELL_BOOST1` = 34 chars). HARD-TRUNCATE every
    order comment to <= 31 here; routed through place_stop_order / place_market_order
    so no order type can ever recur this bug."""
    return (str(s) if s is not None else "")[:31]


# --- Fix 1 (E-13): shared order rc-check + bounded-retry classification -----------
# Retcode buckets consumed by MT5Adapter.place_with_retry(). RETRYABLE -> bounded
# re-send (refresh the tick / recompute the stops / plain resend). NEVER_RETRY -> abort
# with ONE alert (retcode + broker comment + the params attempted). HARD RULE: the
# wrapper NEVER modifies lot size -- 10014 INVALID_VOLUME is alert-only, never a resize.
RETRY_REFRESH_RCS = frozenset({10004, 10015, 10021})   # requote / invalid price / price off
RETRY_STOPS_RCS   = frozenset({10016})                 # invalid stops -> recompute SL/TP
RETRY_PLAIN_RCS   = frozenset({10008, 10012, 10020, 10024, 10031})  # placed/timeout/changed/busy/conn
NEVER_RETRY_RCS   = frozenset({10014, 10019, 10018, 10017})  # volume / no-money / closed / disabled
RETRY_BACKOFFS_S  = (0.5, 1.0, 2.0)


def classify_retcode(rc):
    """PURE bucket for a broker retcode, used by place_with_retry:
      'done'    -> rc == 10009 (success)
      'refresh' -> 10004/10015/10021: refresh the tick + resend
      'stops'   -> 10016: recompute SL/TP vs the symbol stops_level + resend
      'plain'   -> transient (10008/10012/10020/10024/10031) OR result None OR rc == -1
      'abort'   -> NEVER retry: 10014 volume / 10019 no-money / 10018 closed / 10017
                   disabled / ANY unrecognized retcode (fail-closed -> alert only)
    A None/-1 result is a lost ack, treated as 'plain' (safe to re-probe)."""
    if rc == 10009:
        return 'done'
    if rc in (None, -1):
        return 'plain'
    if rc in NEVER_RETRY_RCS:
        return 'abort'
    if rc in RETRY_REFRESH_RCS:
        return 'refresh'
    if rc in RETRY_STOPS_RCS:
        return 'stops'
    if rc in RETRY_PLAIN_RCS:
        return 'plain'
    return 'abort'   # unrecognized retcode -> NEVER retry (fail closed, alert)


class MT5Adapter:
    """
    Optional MT5 integration. Imports MetaTrader5 lazily so the backtest
    works on machines without MT5 installed.

    Connects to the ALREADY-RUNNING MT5 terminal on this machine (no creds
    passed). The terminal must be launched and logged into your broker
    account before starting the bot.

    On startup, autodetects how this broker reports tick.time:
      - "utc": broker sends real UTC Unix timestamps (most brokers)
      - "broker_local": broker sends broker-local time encoded as Unix UTC
        (some brokers, including a few MetaQuotes setups)

    The detected convention is stored in self.tick_time_offset_hours (0 for
    "utc", +3 for "broker_local" if broker is UTC+3). Use this offset to
    decode any future tick.time and to encode times we send to copy_rates.
    """

    # Tiered offset detection (quiet-Monday-wake fix)
    LIVE_DETECT_BUDGET_S = 20.0   # Tier 1 advancing-feed budget (was effectively 90)
    STALE_TOL_S          = 600.0  # Tier 2: accept a tick within ~10min of utc+offset

    def __init__(self, symbol: str = "XAUUSD", expected_offset_hours=None):
        import MetaTrader5 as mt5
        self.mt5 = mt5
        # Hardening #7: time-offset detection + server_time read use the
        # configured trading symbol (still defaults to XAUUSD for every existing
        # caller -- test_place/validate_25 construct MT5Adapter() with no args).
        self.symbol = symbol
        # Tier-2 offset consistency check: the broker offset is a CONSTANT, so a
        # quiet pre-session tick can be VALIDATED against it (not guessed). None
        # disables Tier 2 (live-feed detection only); run_live passes
        # cfg.EXPECTED_BROKER_OFFSET_HOURS.
        self.expected_offset_hours = expected_offset_hours
        if not mt5.initialize():
            raise RuntimeError(
                f"MT5 init failed: {mt5.last_error()}. "
                "Make sure the MetaTrader 5 terminal is running and logged in."
            )
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(
                "MT5 connected but no account is logged in. "
                "Open the MT5 terminal, log into your account, then start the bot."
            )
        log.info(f"Connected to MT5: account #{info.login} on {info.server}")

        # Autodetect tick.time convention by comparing broker's claimed time
        # to local UTC. Done ONCE at startup.
        # Autodetect tick.time convention by comparing broker's claimed time
        # to local UTC. Done ONCE at startup.
        self.tick_time_offset_hours = self._detect_tick_time_offset()
        # Hardening #4: detection returns None when there is no LIVE feed (e.g. a
        # CLOSED market on weekend cold-start). Do NOT crash formatting None --
        # log a warning and proceed; the weekend self-sleep forces a fresh
        # ensure_time_offset() on Monday wake BEFORE any trading, and
        # server_time_utc treats None as 0 for the coarse market-closed probe.
        if self.tick_time_offset_hours is None:
            log.warning(
                "Broker tick.time offset NOT detected at startup (no live feed -- "
                "market likely closed). Proceeding; will re-detect on market open "
                "before any trade. Market-open probes treat the offset as 0 meanwhile."
            )
        else:
            log.info(
                f"Detected broker tick.time convention: offset = "
                f"{self.tick_time_offset_hours:+.0f}h "
                f"({'real UTC' if self.tick_time_offset_hours == 0 else 'broker-local-as-UTC'})"
            )

    def _detect_tick_time_offset(self, max_wait_s: float = 90.0):
        """Detect the broker tick.time offset (hours to SUBTRACT), or None if it
        cannot be established safely. Tiered:
          Tier 1 (preferred) measures from a LIVE advancing feed (short budget).
          Tier 2 (quiet-wake) validates a single stale tick against the configured
            constant offset when the feed is not advancing (the Monday pre-session
            case where gold ticks are near-dead and Tier 1 can never succeed).
        Returns None when neither tier can confirm an offset -- the caller must
        NOT trade on a guessed offset (the wake-validation guard then blocks)."""
        live = self._detect_offset_live(min(max_wait_s, self.LIVE_DETECT_BUDGET_S))
        if live is not None:
            return live
        return self._detect_offset_stale_consistency()

    def _detect_offset_live(self, max_wait_s: float):
        """Tier 1: offset from a LIVE feed -- tick.time must ADVANCE with the wall
        clock between two reads (the original method). Best when liquid; returns
        None on a quiet / non-advancing feed so the caller falls through to Tier 2."""
        import time as _time
        from datetime import datetime as _dt, timezone as _tz
        FRESH_TOL_S = 15.0; ADVANCE_S = 4.0; POLL_S = 1.0
        deadline = _time.monotonic() + max_wait_s
        last_age = None
        # A tick ~N whole-hours stale looks identical to an N-hour offset, so a
        # single timestamp can't disambiguate. Require the feed to be LIVE:
        # tick.time must advance with the wall clock between two reads.
        while _time.monotonic() < deadline:
            t1 = self.mt5.symbol_info_tick(self.symbol); w1 = _dt.now(_tz.utc).timestamp()
            if t1 is None or t1.time <= 0:
                _time.sleep(POLL_S); continue
            _time.sleep(ADVANCE_S)
            t2 = self.mt5.symbol_info_tick(self.symbol); w2 = _dt.now(_tz.utc).timestamp()
            if t2 is None or t2.time <= 0:
                _time.sleep(POLL_S); continue
            wall = w2 - w1; adv = t2.time - t1.time
            if adv < max(1.0, 0.5 * wall):
                last_age = "feed not advancing"
                log.warning(f"Offset detect: feed not live (adv {adv:.0f}s/{wall:.0f}s) — waiting")
                _time.sleep(POLL_S); continue
            diff = t2.time - w2; offset = round(diff / 3600.0)
            if -12 <= offset <= 12:
                remainder = abs(diff - offset * 3600.0); last_age = remainder
                if remainder <= FRESH_TOL_S:
                    log.info(f"Broker time offset detected: {offset:+d}h [live feed]")
                    return float(offset)
            _time.sleep(POLL_S)
        log.warning(f"Tier 1 (live-feed) offset detect timed out in {max_wait_s:.0f}s "
                    f"(last {last_age}); trying stale-tick consistency.")
        return None

    def _detect_offset_stale_consistency(self):
        """Tier 2 -- the quiet-wake path. A single tick timestamp is ambiguous on
        its own, but the broker offset is a CONSTANT, so ACCEPT a stale tick only
        if it is CONSISTENT with the configured EXPECTED_BROKER_OFFSET_HOURS: it
        must round to the expected offset AND sit within STALE_TOL_S of
        utc+expected. This confirms the verified constant; it can NEVER
        rubber-stamp a wrong offset -- a 0h broker reads ~3h off expected and is
        REJECTED, so the Jun-8 case stays blocked. Returns float(expected) or None."""
        from datetime import datetime as _dt, timezone as _tz
        expected = self.expected_offset_hours
        if expected is None:
            log.warning("offset Tier 2 skipped: no EXPECTED_BROKER_OFFSET configured "
                        "(live-only mode); returning None.")
            return None
        expected = int(expected)
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or getattr(tick, "time", 0) <= 0:
            log.error("offset Tier 2: no usable tick (None/time<=0) — returning None (block).")
            return None
        now = _dt.now(_tz.utc).timestamp()
        diff = tick.time - now
        offset = round(diff / 3600.0)
        remainder = abs(diff - expected * 3600.0)
        if offset == expected and remainder <= self.STALE_TOL_S:
            log.info(f"offset confirmed +{expected}h via stale-tick consistency "
                     f"(feed quiet; tick {remainder:.0f}s from utc+{expected}h).")
            return float(expected)
        log.error(f"offset Tier 2 REJECT: tick implies {offset}h (diff {diff:.0f}s, "
                  f"remainder {remainder:.0f}s vs tol {self.STALE_TOL_S:.0f}s) != expected "
                  f"+{expected}h — returning None (block). [the Jun-8 0h case lands here]")
        return None

    def ensure_time_offset(self, max_wait_s: float = 90.0) -> bool:
        off = self._detect_tick_time_offset(max_wait_s=max_wait_s)
        if off is None:
            return False
        self.tick_time_offset_hours = off
        return True

    def resubscribe(self) -> bool:
        """E-12: re-subscribe the trading symbol to Market Watch after a feed drop.
        symbol_info_tick returns None when the symbol falls out of Market Watch (the
        2026-06-30 'not subscribed' storm); symbol_select(symbol, True) re-adds it so
        ticks resume. Returns True iff MT5 reported the select succeeded. Guarded -- a
        re-subscribe attempt must never raise onto the probe/tick loop. NOTE: a True
        return means the select call succeeded, not that ticks are flowing yet; the
        next market-closed probe confirms recovery (FeedWatchdog.on_success)."""
        try:
            return bool(self.mt5.symbol_select(self.symbol, True))
        except Exception as e:
            log.warning(f"resubscribe({self.symbol}) raised: {e!r}")
            return False

    def reinit(self, fresh_tol_s: float = 60.0) -> bool:
        """Fix 4 Level 2 (E-12): full IN-PROCESS MT5 reconnect after a feed-death episode --
        mt5.shutdown() -> mt5.initialize() -> symbol_select(symbol, True) -> verify a FRESH
        tick (tick.time within fresh_tol_s of now, decoded with the detected offset). Returns
        True IFF a fresh tick is confirmed. Guarded -- never raises onto the feed loop. Does
        NOT change the detected offset (the wake path re-validates that separately)."""
        import time as _time
        from datetime import datetime as _dt, timezone as _tz
        try:
            try:
                self.mt5.shutdown()
            except Exception as e:
                log.warning(f"reinit shutdown raised (continuing): {e!r}")
            _time.sleep(0.5)
            if not self.mt5.initialize():
                log.error(f"reinit: MT5 initialize failed: {self.mt5.last_error()}")
                return False
            try:
                self.mt5.symbol_select(self.symbol, True)
            except Exception as e:
                log.warning(f"reinit symbol_select raised: {e!r}")
            tick = self.mt5.symbol_info_tick(self.symbol)
            if tick is None or getattr(tick, "time", 0) <= 0:
                log.warning("reinit: no usable tick after re-init (feed still dead).")
                return False
            now = _dt.now(_tz.utc).timestamp()
            off = self.tick_time_offset_hours or 0
            age = abs((float(tick.time) - off * 3600.0) - now)
            fresh = age <= float(fresh_tol_s)
            log.warning(f"reinit: fresh-tick check age={age:.0f}s tol={fresh_tol_s:.0f}s "
                        f"-> {'FRESH' if fresh else 'STALE'}")
            return bool(fresh)
        except Exception as e:
            log.error(f"reinit raised: {e!r}")
            return False

    def shutdown(self):
        self.mt5.shutdown()

    def get_m5_close(self, symbol: str, utc_time: pd.Timestamp) -> Optional[float]:
        # Use copy_rates_range to specifically request the M5 bar ENDING at
        # utc_time. Apply the autodetected offset so the time we send matches
        # this broker's expected encoding.
        m5_start = utc_time - pd.Timedelta(minutes=5)
        broker_offset = pd.Timedelta(hours=self.tick_time_offset_hours)
        m5_start_send = (m5_start + broker_offset).tz_localize(None).to_pydatetime()
        m5_end_send = (utc_time + broker_offset).tz_localize(None).to_pydatetime()
        bars = self.mt5.copy_rates_range(symbol, self.mt5.TIMEFRAME_M5,
                                         m5_start_send, m5_end_send)
        if bars is None or len(bars) == 0:
            log.warning(f"get_m5_close: no bars in [{m5_start_send} → {m5_end_send}]")
            return None
        return float(bars[-1]['close'])

    def get_latest_m1(self, symbol: str, n: int = 1):
        return self.mt5.copy_rates_from_pos(symbol, self.mt5.TIMEFRAME_M1, 0, n)

    def server_time_utc(self) -> pd.Timestamp:
        # tick.time is decoded using the convention we detected at startup.
        # If broker sends real UTC: offset=0, no change.
        # If broker sends broker-local-as-UTC: offset=+3 (UTC+3), subtract it.
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None:
            raise RuntimeError("symbol_info_tick returned None — symbol not subscribed?")
        broker_ts = pd.Timestamp(tick.time, unit='s', tz='UTC')
        # Hardening #4: offset may be None pre-detection (closed-market cold-start);
        # treat as 0 for the coarse staleness math. The real offset is set by
        # ensure_time_offset() on market open before any trade decision.
        return broker_ts - pd.Timedelta(hours=self.tick_time_offset_hours or 0)

    def get_account_info(self) -> dict:
        """Pull current account state from MT5. Returns {} on failure."""
        try:
            info = self.mt5.account_info()
            if info is None:
                return {}
            return {
                'login': int(info.login),
                'balance': float(info.balance),
                'equity': float(info.equity),
                'margin': float(info.margin),
                'margin_free': float(info.margin_free),
                'currency': info.currency,
                'leverage': int(info.leverage),
                'server': info.server,
            }
        except Exception as e:
            log.warning(f"get_account_info failed: {e}")
            return {}

    def find_pending_by_price(self, symbol: str, side: str, price: float,
                              lot: float, magic: int = 20260522,
                              tolerance: float = 0.05):
        """v2.3: Reconciliation helper — find an existing pending order matching the
        spec we just tried to send. Used when order_send returned None / rc=-1, to
        decide if the order actually got placed despite the missing ack.

        Returns the matching order object (from mt5.orders_get) or None.
        Matches on: symbol + side (BUY_STOP/SELL_STOP) + price within tolerance +
        magic + volume within 0.005."""
        mt5 = self.mt5
        try:
            orders = mt5.orders_get(symbol=symbol) or []
        except Exception:
            return None
        want_type = mt5.ORDER_TYPE_BUY_STOP if side == 'BUY' else mt5.ORDER_TYPE_SELL_STOP
        matches = []
        for o in orders:
            if int(o.type) != int(want_type): continue
            if int(getattr(o, 'magic', 0)) != int(magic): continue
            if abs(float(o.price_open) - float(price)) > tolerance: continue
            if abs(float(o.volume_current) - float(lot)) > 0.005: continue
            matches.append(o)
        if not matches:
            return None
        # If multiple, return the most recently placed (highest ticket)
        matches.sort(key=lambda o: int(o.ticket), reverse=True)
        return matches[0]

    def stop_preflight(self, symbol: str, side: str, price: float,
                       cushion_pts: float = 0.0):
        """Validate a pending STOP against the live tick + broker stops/freeze level
        BEFORE sending (root-cause guard for the 10015 INVALID_PRICE flood).

        Returns (ok, through_pts, detail):
          ok          - True if the stop is placeable (correct side AND at least the
                        stops-level distance away from the market).
          through_pts - how far price has moved BEYOND the level in the fill direction
                        (>0 means the stop is on the wrong side / would fill at once);
                        0 when the stop is on the right side (valid, or merely too close).
          detail      - human string for logs.
        FAIL-OPEN: any read error returns ok=True so a telemetry glitch never blocks a
        placement — the broker stays the final arbiter."""
        try:
            tk = self.mt5.symbol_info_tick(symbol)
            info = self.mt5.symbol_info(symbol)
            bid, ask = float(tk.bid), float(tk.ask)
            point = float(getattr(info, "point", 0.01) or 0.01)
            stops = max(float(getattr(info, "trade_stops_level", 0) or 0),
                        float(getattr(info, "trade_freeze_level", 0) or 0)) * point
            stops += max(0.0, float(cushion_pts))
            if side == "BUY":
                ok = price >= ask + stops
                through = max(0.0, ask - price)
                detail = f"BUY stop {price:.2f} vs ask {ask:.2f} + stops {stops:.2f}"
            else:
                ok = price <= bid - stops
                through = max(0.0, price - bid)
                detail = f"SELL stop {price:.2f} vs bid {bid:.2f} - stops {stops:.2f}"
            return bool(ok), round(through, 2), detail
        except Exception as e:
            log.debug(f"stop_preflight non-fatal: {e!r}")
            return True, 0.0, "preflight-unavailable"

    def place_stop_order(self, symbol: str, side: str, price: float,
                         lot: float, sl: float, tp: float,
                         comment: str = "AUREON_v2", dry_run: bool = False,
                         magic: int = 20260522):
        # `magic` defaults to the anchor magic (20260522) so every existing caller
        # is byte-identical; the ROGUE monster adapter passes rogue.ROGUE_MAGIC
        # (20260626) to tag its own resting stops (own-magic isolation rule).
        comment = mt5_comment(comment)  # MT5 rejects comments > 31 chars (boost root cause)
        mt5 = self.mt5
        if side == 'BUY':
            order_type = mt5.ORDER_TYPE_BUY_STOP
        else:
            order_type = mt5.ORDER_TYPE_SELL_STOP
        req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": int(magic),
            "comment": comment,
            "type_time": mt5.ORDER_TIME_DAY,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if dry_run:
            log.info(f"[PAPER] Would place {side} stop {symbol} @ {price} lot={lot} SL={sl} TP={tp}")
            return {'paper': True, 'request': req}
        # Pre-flight: never SEND a stop that is on the wrong side of / inside the
        # broker stops-level (the 2026 10015 flood came from re-sending exactly this).
        # Returns a stale shim (retcode 10015, .stale) so callers skip/chase without a
        # wasted broker round-trip; place_with_retry then bounds anchor/RB legs cleanly.
        pf_ok, pf_through, pf_detail = self.stop_preflight(symbol, side, price)
        if not pf_ok:
            log.warning(f"⚠ stop preflight SKIP: {pf_detail} (through {pf_through:.2f}) — not sent")

            class _StaleResult:
                retcode = 10015
                order = 0
                deal = 0
                comment = "PREFLIGHT_STALE"
                stale = True
                through_pts = pf_through

            return _StaleResult()
        result = mt5.order_send(req)
        # Decode retcode for human-readable logging
        rc = result.retcode if result else -1
        rc_name = _MT5_RETCODE_MAP.get(rc, f"UNKNOWN_{rc}")
        is_ok = (rc == 10009)  # TRADE_RETCODE_DONE

        # v2.3 RECONCILIATION: order_send returned None (rc=-1) means we don't know
        # if the order was actually placed. Query broker state to find out, then
        # retry only if confirmed absent (cannot create duplicates).
        if rc == -1:
            import time as _time
            _time.sleep(0.5)  # let broker settle
            existing = self.find_pending_by_price(symbol, side, price, lot, magic=int(magic))
            if existing is not None:
                log.info(
                    f"✅ Placed {side} stop @ {price} lot={lot}: rc=-1 but RECONCILED — "
                    f"ticket {existing.ticket} found in broker state"
                )

                # Build a minimal SendResult-like shim so callers can read .retcode/.order
                class _ReconciledResult:
                    retcode = 10009
                    order = int(existing.ticket)
                    deal = 0
                    comment = "RECONCILED_FROM_BROKER_STATE"

                return _ReconciledResult()
            # Truly not placed — safe to retry exactly once
            log.warning(
                f"⚠ {side} stop @ {price}: rc=-1 + no matching pending in broker state — retrying once"
            )
            result = mt5.order_send(req)
            rc = result.retcode if result else -1
            rc_name = _MT5_RETCODE_MAP.get(rc, f"UNKNOWN_{rc}")
            is_ok = (rc == 10009)
            if is_ok:
                log.info(f"✅ Placed {side} stop @ {price} lot={lot} on RETRY: retcode={rc} ({rc_name})")
                return result
            log.error(f"❌ {side} stop @ {price} RETRY also failed: retcode={rc} ({rc_name})")
            # fall through to standard rejection logging below

        # Log explicitly whether it actually went through, with the retcode meaning
        if is_ok:
            log.info(f"✅ Placed {side} stop @ {price} lot={lot}: retcode={rc} ({rc_name})")
        else:
            err_detail = result.comment if result and hasattr(result, 'comment') else ''
            log.error(f"❌ {side} stop @ {price} REJECTED: retcode={rc} ({rc_name}) {err_detail}")
        return result

    def modify_position_sl(self, ticket: int, new_sl: float,
                           dry_run: bool = False):
        """v2.5: rc=-1 reconciliation symmetric with place_stop_order.
        If order_send returns None, query broker for actual position SL.
        If broker already has the new SL, return success silently.
        If broker still has old SL, retry once."""
        mt5 = self.mt5
        if dry_run:
            log.info(f"[PAPER] Would modify ticket {ticket} SL → {new_sl}")
            return {'paper': True}
        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": new_sl,
        }
        result = mt5.order_send(req)
        rc = result.retcode if result else -1

        # v2.5 reconciliation
        if rc == -1:
            import time as _time
            _time.sleep(0.5)
            positions = mt5.positions_get(ticket=ticket)
            if positions:
                actual_sl = positions[0].sl
                if abs(actual_sl - new_sl) < 0.05:
                    log.info(f"✅ Modify SL ticket={ticket} → ${new_sl}: rc=-1 but RECONCILED — broker SL matches")

                    class _R:
                        retcode = 10009; comment = "RECONCILED_SLTP"

                    return _R()
                else:
                    log.warning(
                        f"⚠ Modify SL ticket={ticket}: rc=-1, broker SL still ${actual_sl} (wanted ${new_sl}) — retrying"
                    )
                    result = mt5.order_send(req)
                    rc = result.retcode if result else -1
                    if rc == 10009:
                        log.info(f"✅ Modify SL ticket={ticket} → ${new_sl} on RETRY: retcode=10009")
                        return result
                    log.error(f"❌ Modify SL ticket={ticket} RETRY also failed: retcode={rc}")
            else:
                log.warning(
                    f"⚠ Modify SL ticket={ticket}: rc=-1 + position not found in broker state — position may have closed")
        return result

    def cancel_order(self, ticket, dry_run: bool = False):
        """Cancel a pending order by ticket id."""
        if dry_run or isinstance(ticket, str):
            log.info(f"[PAPER] Would cancel pending order {ticket}")
            return {'paper': True}
        mt5 = self.mt5
        req = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(ticket),
        }
        result = mt5.order_send(req)
        rc = result.retcode if result else -1
        # v2.5: reconcile rc=-1 by checking if order still exists
        if rc == -1:
            import time as _time
            _time.sleep(0.3)
            orders = mt5.orders_get(ticket=int(ticket)) or []
            if not orders:
                log.info(f"✅ Cancel order {ticket}: rc=-1 but RECONCILED — order is gone")

                class _R: retcode = 10009; comment = "RECONCILED_CANCEL"

                return _R()
            log.warning(f"⚠ Cancel order {ticket}: rc=-1 + order still exists — retrying")
            result = mt5.order_send(req)
        return result

    def close_position(self, ticket, dry_run: bool = False):
        mt5 = self.mt5
        if dry_run:
            log.info(f"[PAPER] Would close ticket {ticket}")
            return {'paper': True}
        pos = mt5.positions_get(ticket=ticket)
        if not pos: return None
        p = pos[0]
        tick = mt5.symbol_info_tick(p.symbol)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if p.type == 0 else tick.ask,
            "deviation": 20,
            "magic": 20260522,
            "comment": "AUREON_v2_close",
        }
        return mt5.order_send(req)

    def place_market_order(self, symbol: str, side: str, lot: float,
                           sl: float, tp: float, comment: str = "AUREON_v2_market",
                           dry_run: bool = False, magic: int = 20260522):
        """Place an IMMEDIATE market order. Used only for in-flight breakout
        recovery: when pre-flight passed but broker rejected anyway because
        price moved past the threshold during the millisecond order was in flight."""
        comment = mt5_comment(comment)  # MT5 rejects comments > 31 chars (boost root cause)
        mt5 = self.mt5
        if dry_run:
            log.info(f"[PAPER] Would place MARKET {side} {symbol} lot={lot} SL={sl} TP={tp}")
            return {'paper': True, 'price': 0.0}
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error("place_market_order: no tick available")
            return None
        if side == 'BUY':
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 50,
            "magic": int(magic),   # ROGUE passes ROGUE_MAGIC for label-scoped isolation
            "comment": comment,
            "type_time": mt5.ORDER_TIME_DAY,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        # v3.0.0 Fix B: announce the attempt before order_send so the log has a
        # line at EVERY exit of this path (the boost path was 0-for-6 with no
        # trace at all). Telegram visibility for boosts is layered on by the
        # live_trader boost loop, which wraps this call.
        log.info(f"… attempting MARKET {side} {symbol} lot={lot} @ {price} SL={sl} TP={tp} ({comment})")
        try:
            result = mt5.order_send(req)
        except Exception as e:  # v2.9.8: a raise here was SILENT in the boost path
            log.error(f"place_market_order order_send raised: {e!r}")
            return None
        rc = result.retcode if result else -1
        if rc == 10030:  # v2.9.8 INVALID_FILL: retry once with FOK
            log.warning("MARKET order rc=10030 (filling mode) with IOC -- retrying FOK")
            req["type_filling"] = mt5.ORDER_FILLING_FOK
            try:
                result = mt5.order_send(req)
            except Exception as e:
                log.error(f"place_market_order FOK retry raised: {e!r}")
                return None
            rc = result.retcode if result else -1
        rc_name = _MT5_RETCODE_MAP.get(rc, f"UNKNOWN_{rc}")
        if rc == 10009:
            tk = getattr(result, 'order', None) or getattr(result, 'deal', None)
            log.info(f"✅ MARKET {side} filled @ {price} lot={lot} ticket={tk}: retcode={rc} ({rc_name})")
        else:
            err = result.comment if result and hasattr(result, 'comment') else ''
            if result is None:  # v2.9.8: capture WHY when broker gave no response
                try:
                    err = f"last_error={mt5.last_error()}"
                except Exception:
                    pass
            log.error(f"❌ MARKET {side} REJECTED: retcode={rc} ({rc_name}) {err}")
        return result

    def place_with_retry(self, sender, *, describe=None, tele=None,
                         max_attempts=3, backoffs=RETRY_BACKOFFS_S):
        """Fix 1 (E-13): SHARED bounded-retry order sender for BOTH engines -- the anchor
        stop orders AND Rogue's market entries route their order_send through here so ONE
        rc-classification rule governs every order (never-blind, never-brick).

        `sender(attempt:int, recompute_stops:bool) -> result` MUST (re)read a fresh tick,
        (re)build the request, and call order_send, returning the raw result (or None). It
        is called once per attempt, so 'refresh the tick each try' and 'recompute SL/TP on
        10016' happen INSIDE it. `describe` is a params dict for the abort alert; `tele`
        (optional) is the telemetry sink for the ABORT/EXHAUSTED alert.

        RETRYABLE (<= max_attempts, 0.5/1/2s backoff): 10004/10015/10021 (refresh tick),
        10016 (recompute stops), 10008-class / None / -1 (plain). NEVER RETRY (abort + ONE
        alert with retcode + broker comment + params): 10014 INVALID_VOLUME (the lot is
        NEVER resized), 10019 / 10018 / 10017, and ANY unrecognized retcode.

        HARD RULE: this wrapper never touches volume -- position size is sacred. Returns the
        final result object (rc==10009 on success, else the last failing result or None).
        Guarded per attempt so a sender raise becomes a plain None result (retryable)."""
        import time as _time
        result = None
        recompute_stops = False
        max_attempts = max(1, int(max_attempts))
        for attempt in range(1, max_attempts + 1):
            try:
                result = sender(attempt, recompute_stops)
            except Exception as e:
                log.error(f"place_with_retry sender raised (attempt {attempt}): {e!r}")
                result = None
            rc = getattr(result, 'retcode', None) if result is not None else None
            bucket = classify_retcode(rc)
            if bucket == 'done':
                return result
            rc_name = _MT5_RETCODE_MAP.get(rc, f"UNKNOWN_{rc}")
            if bucket == 'abort':
                self._alert_order_abort(tele, rc, result, describe, attempt)
                return result
            recompute_stops = (bucket == 'stops')   # next attempt recomputes SL/TP
            if attempt >= max_attempts:
                log.error(f"place_with_retry EXHAUSTED {attempt}/{max_attempts} "
                          f"rc={rc} ({rc_name}) [{(describe or {}).get('label', 'order')}]")
                self._alert_order_abort(tele, rc, result, describe, attempt, exhausted=True)
                return result
            delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
            log.warning(f"place_with_retry rc={rc} ({rc_name}) -> {bucket} retry "
                        f"{attempt + 1}/{max_attempts} in {delay}s "
                        f"[{(describe or {}).get('label', 'order')}]")
            _time.sleep(delay)
        return result

    def _alert_order_abort(self, tele, rc, result, describe, attempt, exhausted=False):
        """Fire ONE alert for a NEVER-RETRY (or retry-exhausted) order: retcode + the
        broker comment + the params attempted. ALERT ONLY -- never resizes/modifies the
        order. The Discord dedup/throttle in the telemetry layer collapses repeats of the
        identical alert (1/type). Fully guarded."""
        rc_name = _MT5_RETCODE_MAP.get(rc, f"UNKNOWN_{rc}")
        try:
            broker_comment = (getattr(result, 'comment', '') or '') if result is not None else ''
        except Exception:
            broker_comment = ''
        d = describe or {}
        kind = 'EXHAUSTED' if exhausted else 'ABORT'
        volnote = ' (lot NOT resized)' if rc == 10014 else ''
        params = (f"{d.get('side', '?')} {d.get('symbol', '?')} lot={d.get('lot', '?')} "
                  f"@ {d.get('price', 'mkt')} SL={d.get('sl', '?')} TP={d.get('tp', '?')} "
                  f"magic={d.get('magic', '?')}")
        log.error(f"ORDER {kind} rc={rc} ({rc_name}){volnote} comment='{broker_comment}' "
                  f"params[{params}] after {attempt} attempt(s)")
        if tele is not None:
            try:
                tele.critical(
                    f"🚫 *ORDER {kind}* — `{d.get('label', 'order')}`\n"
                    f"retcode `{rc}` ({rc_name}){volnote}\n"
                    f"broker: `{broker_comment or '—'}`\n"
                    f"params: `{params}`\n"
                    f"No retry — check the terminal if this repeats.")
            except Exception:
                pass
