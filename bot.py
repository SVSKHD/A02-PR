#!/usr/bin/env python3
"""
AUREON v2 — Multi-anchor anchor-breakout bot for XAUUSD.

Modes
-----
  backtest : Run on a historical M1 CSV. Outputs per-trade CSV + monthly summary.
  paper    : Live data from MT5, no real orders. Logs intended actions.
  live     : Live data from MT5, real orders placed. Requires --i-understand-the-risks.

Usage
-----
  python bot.py backtest --csv XAUUSD_M1.csv --start 2025-05-08 --end 2026-05-06
  python bot.py paper        # MT5 terminal must be running and logged in
  python bot.py live --i-understand-the-risks

See AUREON_V2_SPEC.md for the full strategy documentation.
"""

import argparse, json, logging, os, sys, time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date as DateType
from typing import Optional, List, Dict, Tuple
import pandas as pd

# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class Config:
    # Strategy
    symbol: str = "XAUUSD"
    contract_size: float = 100.0          # oz per 1.0 lot
    trigger_dist: float = 5.00
    tp_dist:      float = 30.00           # was 20.00 — let winners run longer
    sl_dist:      float = 18.00           # was 20.00 — slightly tighter (saves $118 per SL)
    lot_size:     float = 0.54            # was 0.50 — max safe @ $50k (1.94% per trade, worst day -3.98% safely under 4% FP daily)
    be_trigger:   float = 0.30            # unchanged: wait for $0.30 favorable before locking BE
    trail_gap:    float = 0.30            # v2.5.5: reverted 0.10->0.30. $0.10 was tighter than avg spread ($0.11);
                                          # its backtest gain was a phantom (un-fillable on live ticks). 0.30 is executable.
    min_step:     float = 0.10            # v2.5.5: back to 0.10 to match the 0.30 trail gap
    freeze_minutes: int = 15              # v2.5: ENABLED — trend-capture mode, matches backtest projections. 0 to disable for legacy v2.2 behavior.

    # Auto-sizing: read balance from MT5 at startup, compute the largest safe lot
    auto_lot: bool = True                # if True, override lot_size from live balance
    lot_conservatism: float = 0.99       # was 0.92 — produces lot 0.54 at $50k (1.94% per trade, safe buffer to 4% daily rule)
    risk_pct_under_50k: float = 0.03     # Funding Pips: 3% per-trade on <$50k accounts
    risk_pct_over_50k:  float = 0.02     # Funding Pips: 2% per-trade on ≥$50k accounts
    slippage_buffer: float = 0.98        # keep lot's worst-case loss to this fraction of the rule cap

    # Anchors — (label, broker_hour). Broker = UTC+3.
    anchors: List[Tuple[str, int]] = field(default_factory=lambda: [
        ("A1_02h_Asia",      2),
        ("A2_10h_London",   10),
        ("A3_14h_Overlap",  14),
        ("A4_17h_NYopen",   17),
    ])
    broker_tz_offset_hours: int = 3       # UTC+3
    eod_broker_hour: int = 23             # close all at 23:00 broker

    # Risk
    starting_balance: float = 50000.0
    daily_loss_pct:   float = 0.03        # 3% kill switch (Funding Pips Zero has 5% trailing DD — 3% daily gives a 2% multi-day buffer)
    weekly_loss_pct:  float = 0.08
    account_floor_pct: float = 0.85       # halt new entries below this multiple of starting

    # Operational
    log_level: str = "INFO"
    state_file: str = "aureon_v2_state.json"


# ============================================================================
# LOGGING
# ============================================================================

def setup_logging(level: str = "INFO", log_dir: str = "./logs",
                  app_name: str = "aureon"):
    """Set up logging to BOTH stdout and a daily-rotated file in log_dir.
    
    File naming: logs/aureon_YYYY-MM-DD.log (rotated daily at UTC midnight,
    keeping 30 days of history). All log levels from app modules go in.
    
    Format includes timestamp, level, module name, and message. Caller can
    grep for specific anchors, errors, or modules later.
    """
    os.makedirs(log_dir, exist_ok=True)
    
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))
    # Clear any pre-existing handlers so basicConfig calls don't double-log
    for h in list(root.handlers):
        root.removeHandler(h)
    
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Console handler (so terminal still shows everything)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    
    # Daily-rotated file handler
    from logging.handlers import TimedRotatingFileHandler
    log_file = os.path.join(log_dir, f"{app_name}.log")
    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', interval=1, backupCount=30, utc=True,
        encoding='utf-8'
    )
    file_handler.setFormatter(fmt)
    file_handler.suffix = "%Y-%m-%d"  # so rotated files become aureon.log.2026-05-25
    root.addHandler(file_handler)
    
    log = logging.getLogger("AUREON")
    log.info(f"Logging to console + {log_file} (daily rotation, 30-day retention)")
    return log


log = logging.getLogger("AUREON")


# ============================================================================
# CORE STRATEGY ENGINE — shared between backtest and live
# ============================================================================

@dataclass
class Position:
    """A single open position (one leg from one anchor)."""
    anchor_label: str
    side: str            # 'BUY' or 'SELL'
    entry_price: float
    entry_time: pd.Timestamp
    current_sl: float
    tp_level: float
    max_fav: float
    lot: float
    closed: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    outcome: Optional[str] = None        # 'SL', 'TP', 'Trail', 'EOD', 'KillSwitch'

    @property
    def pnl_dist(self) -> float:
        """Current/realized price distance favorable to us."""
        ref = self.exit_price if self.closed else self.max_fav
        if self.side == 'BUY':
            return (ref - self.entry_price)
        return (self.entry_price - ref)


def initial_sl(side: str, entry: float, cfg: Config) -> float:
    return entry - cfg.sl_dist if side == 'BUY' else entry + cfg.sl_dist

def initial_tp(side: str, entry: float, cfg: Config) -> float:
    return entry + cfg.tp_dist if side == 'BUY' else entry - cfg.tp_dist


def update_position_on_bar(pos: Position, bar: pd.Series, ts: pd.Timestamp,
                           cfg: Config) -> Optional[str]:
    """
    Apply one M1 bar to an open position. Returns the outcome string if closed,
    else None. Mutates pos.
    """
    if pos.closed:
        return pos.outcome

    # 1. PRE-BAR SL CHECK
    if pos.side == 'BUY':
        if bar.low <= pos.current_sl:
            pos.exit_price = pos.current_sl
            pos.exit_time = ts
            pos.outcome = 'SL' if pos.current_sl <= pos.entry_price - cfg.sl_dist + 0.01 else 'Trail'
            pos.closed = True
            return pos.outcome
    else:
        if bar.high >= pos.current_sl:
            pos.exit_price = pos.current_sl
            pos.exit_time = ts
            pos.outcome = 'SL' if pos.current_sl >= pos.entry_price + cfg.sl_dist - 0.01 else 'Trail'
            pos.closed = True
            return pos.outcome

    # 2. UPDATE PEAK FAVORABLE (always, even during freeze — used for reporting & post-freeze trail snap)
    if pos.side == 'BUY':
        if bar.high > pos.max_fav: pos.max_fav = bar.high
        fav = pos.max_fav - pos.entry_price
    else:
        if bar.low < pos.max_fav: pos.max_fav = bar.low
        fav = pos.entry_price - pos.max_fav
    fav = max(fav, 0.0)

    # 3-5. TRAIL UPDATE — gated by freeze window
    # v2.3 FREEZE: for cfg.freeze_minutes after fill, do NOT engage BE-arm/trail.
    # Initial $18 SL stays as the broker-side stop. When freeze expires, normal
    # trail logic engages and will snap to (peak − trail_gap) automatically.
    in_freeze = False
    if cfg.freeze_minutes > 0 and pos.entry_time is not None:
        try:
            elapsed = (ts - pos.entry_time).total_seconds() / 60.0
            in_freeze = elapsed < cfg.freeze_minutes
        except Exception:
            in_freeze = False  # bad timestamp → fall through to normal logic

    # v2.5.5 PATCH A — BASE LOCK: at +$3 favorable, force SL to break-even.
    # Fires EVEN during freeze (safety valve for fast favorable spikes that
    # reverse before the post-freeze trail can engage — e.g. Fri 29-May A3).
    # This guarantees any trade that touches +$3 fav cannot become a loss.
    if fav >= 3.00:
        if pos.side == 'BUY':
            if pos.entry_price > pos.current_sl:
                pos.current_sl = pos.entry_price
        else:
            if pos.entry_price < pos.current_sl:
                pos.current_sl = pos.entry_price

    if not in_freeze and fav >= cfg.be_trigger:
        if pos.side == 'BUY':
            candidate_sl = max(pos.entry_price, pos.max_fav - cfg.trail_gap)
            if candidate_sl > pos.current_sl + cfg.min_step:
                pos.current_sl = candidate_sl
        else:
            candidate_sl = min(pos.entry_price, pos.max_fav + cfg.trail_gap)
            if candidate_sl < pos.current_sl - cfg.min_step:
                pos.current_sl = candidate_sl

    # v2.5.5 PATCH A: $5 SECONDARY LOCK now fires EVEN during freeze (dropped the
    # not-in_freeze gate). Once peak fav reaches $5, force SL to be at least $4 in
    # profit from entry. Guarantees: any trade that touches $5 fav exits with ≥$4/unit.
    if fav >= 5.00:
        if pos.side == 'BUY':
            floor_sl = pos.entry_price + 4.00
            if floor_sl > pos.current_sl:
                pos.current_sl = floor_sl
        else:
            floor_sl = pos.entry_price - 4.00
            if floor_sl < pos.current_sl:
                pos.current_sl = floor_sl

    # 6. TP CHECK
    if pos.side == 'BUY':
        if bar.high >= pos.tp_level:
            pos.exit_price = pos.tp_level
            pos.exit_time = ts
            pos.outcome = 'TP'
            pos.closed = True
            return 'TP'
    else:
        if bar.low <= pos.tp_level:
            pos.exit_price = pos.tp_level
            pos.exit_time = ts
            pos.outcome = 'TP'
            pos.closed = True
            return 'TP'

    return None


def realize_pnl_usd(pos: Position, cfg: Config) -> float:
    """Convert closed position to USD P&L. Returns 0 if not closed."""
    if not pos.closed: return 0.0
    return pos.pnl_dist * cfg.contract_size * pos.lot


# ============================================================================
# ANCHOR SCHEDULING
# ============================================================================

def anchor_datetime_utc(broker_date: DateType, broker_hour: int,
                        broker_tz_offset_hours: int = 3) -> pd.Timestamp:
    """Convert a broker-date + broker-hour to a UTC timestamp."""
    ts = pd.Timestamp(broker_date) + pd.Timedelta(hours=broker_hour - broker_tz_offset_hours)
    return ts.tz_localize('UTC')


def eod_datetime_utc(broker_date: DateType, cfg: Config) -> pd.Timestamp:
    """EOD UTC timestamp = broker 23:00 = UTC 20:00 same broker date."""
    return anchor_datetime_utc(broker_date, cfg.eod_broker_hour, cfg.broker_tz_offset_hours)


def m5_close_at(m5: pd.DataFrame, target_utc: pd.Timestamp) -> Optional[float]:
    """Get the close of the M5 bar ending at target_utc (or nearest within ±5min)."""
    if target_utc in m5.index:
        return float(m5.loc[target_utc, 'close'])
    near = m5.index[(m5.index >= target_utc - pd.Timedelta(minutes=5)) &
                    (m5.index <= target_utc + pd.Timedelta(minutes=5))]
    if len(near) == 0: return None
    return float(m5.loc[near[0], 'close'])


# ============================================================================
# BACKTEST ENGINE
# ============================================================================

def run_backtest(csv_path: str, start: str, end: str, cfg: Config) -> pd.DataFrame:
    log.info(f"Loading M1 from {csv_path}")
    m1 = pd.read_csv(csv_path)
    m1['time'] = pd.to_datetime(m1['time'], utc=True)
    m1 = m1.set_index('time').sort_index()[['open','high','low','close']]
    log.info(f"Loaded {len(m1):,} M1 bars from {m1.index.min()} to {m1.index.max()}")

    m5 = m1.resample('5min', label='right', closed='right').agg(
        {'open':'first','high':'max','low':'min','close':'last'}).dropna()
    log.info(f"Resampled to {len(m5):,} M5 bars")

    days = pd.date_range(start, end, freq='B')
    trades_records: List[Dict] = []
    daily_pnl_running: Dict[DateType, float] = {}
    kill_switch_days: List[DateType] = []

    for d in days:
        broker_date = d.date()
        eod_ts = eod_datetime_utc(broker_date, cfg)
        daily_pnl = 0.0
        kill_triggered = False

        for label, broker_hour in cfg.anchors:
            if kill_triggered: break

            at = anchor_datetime_utc(broker_date, broker_hour, cfg.broker_tz_offset_hours)
            if at >= eod_ts: continue
            anchor_price = m5_close_at(m5, at)
            if anchor_price is None: continue

            buy_stop  = round(anchor_price + cfg.trigger_dist, 2)
            sell_stop = round(anchor_price - cfg.trigger_dist, 2)
            window = m1.loc[at:eod_ts]
            if len(window) < 3: continue

            # Single-OCO fill scan
            side, fi = None, None
            for i, (ts, bar) in enumerate(window.iterrows()):
                b_hit = bar.high >= buy_stop
                s_hit = bar.low  <= sell_stop
                if b_hit and s_hit:
                    side = 'SELL' if bar.close >= bar.open else 'BUY'
                    fi = i; break
                elif b_hit:
                    side = 'BUY'; fi = i; break
                elif s_hit:
                    side = 'SELL'; fi = i; break

            if side is None: continue

            entry_price = buy_stop if side == 'BUY' else sell_stop
            entry_time  = window.index[fi]

            pos = Position(
                anchor_label = label,
                side         = side,
                entry_price  = entry_price,
                entry_time   = entry_time,
                current_sl   = initial_sl(side, entry_price, cfg),
                tp_level     = initial_tp(side, entry_price, cfg),
                max_fav      = entry_price,
                lot          = cfg.lot_size,
            )

            # Walk forward from next bar
            walk = window.iloc[fi+1:]
            for ts, bar in walk.iterrows():
                outcome = update_position_on_bar(pos, bar, ts, cfg)
                if outcome:
                    break
            if not pos.closed:
                last = walk.iloc[-1]
                pos.exit_price = float(last.close)
                pos.exit_time = walk.index[-1]
                pos.outcome = 'EOD'
                pos.closed = True

            usd = realize_pnl_usd(pos, cfg)
            daily_pnl += usd
            trades_records.append({
                'date': str(broker_date),
                'anchor': pos.anchor_label,
                'side': pos.side,
                'entry_time': str(pos.entry_time),
                'entry': pos.entry_price,
                'exit_time': str(pos.exit_time),
                'exit': pos.exit_price,
                'max_favorable': round(pos.max_fav, 2),
                'outcome': pos.outcome,
                'pnl_dist': round(pos.pnl_dist, 3),
                'pnl_usd': round(usd, 2),
                'lot': pos.lot,
            })

            # Daily kill switch check
            if daily_pnl <= -cfg.daily_loss_pct * cfg.starting_balance:
                log.warning(f"KILL SWITCH triggered on {broker_date}: daily P&L ${daily_pnl:.2f}")
                kill_triggered = True
                kill_switch_days.append(broker_date)
                break

        daily_pnl_running[broker_date] = daily_pnl

    df = pd.DataFrame(trades_records)
    if len(df):
        df['date'] = pd.to_datetime(df['date'])
        log.info(f"Backtest complete: {len(df)} trades, ${df['pnl_usd'].sum():,.2f} P&L, "
                 f"{kill_switch_days and len(kill_switch_days) or 0} kill-switch days")
    return df


def summarize_backtest(df: pd.DataFrame, cfg: Config) -> Dict:
    if len(df) == 0:
        return {'fills': 0, 'total_usd': 0, 'total_pips': 0}

    daily = df.groupby(df['date'].dt.date)['pnl_usd'].sum()
    monthly = df.groupby(df['date'].dt.to_period('M'))['pnl_usd'].sum()
    eq = df['pnl_usd'].cumsum()
    dd = (eq - eq.cummax()).min()

    return {
        'fills': len(df),
        'total_pips': round(df['pnl_dist'].sum(), 2),
        'total_usd':  round(df['pnl_usd'].sum(), 2),
        'win_rate':   round(100 * (df['pnl_usd'] > 0).mean(), 2),
        'max_dd':     round(dd, 2),
        'max_dd_pct': round(100 * dd / cfg.starting_balance, 2),
        'sl_count':   int((df['outcome']=='SL').sum()),
        'tp_count':   int((df['outcome']=='TP').sum()),
        'worst_day':  round(daily.min(), 2),
        'best_day':   round(daily.max(), 2),
        'kill_days':  int((daily <= -cfg.daily_loss_pct * cfg.starting_balance).sum()),
        'months':     len(monthly),
        'avg_per_month_usd':  round(monthly.mean(), 2),
        'avg_per_month_pips': round(df['pnl_dist'].sum() / len(monthly), 2),
        'monthly_pnl': {str(k): round(v,2) for k,v in monthly.items()},
    }


# ============================================================================
# LIVE / PAPER MODES (MT5 integration)
# ============================================================================

# MT5 trade retcode names (from MetaTrader5 docs)
_MT5_RETCODE_MAP = {
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",                  # ← success
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",         # ← stop price on wrong side of market
    10016: "INVALID_STOPS",         # ← SL/TP on wrong side
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
    def __init__(self):
        import MetaTrader5 as mt5
        self.mt5 = mt5
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
        self.tick_time_offset_hours = self._detect_tick_time_offset()
        log.info(
            f"Detected broker tick.time convention: offset = "
            f"{self.tick_time_offset_hours:+.0f}h "
            f"({'real UTC' if self.tick_time_offset_hours == 0 else 'broker-local-as-UTC'})"
        )

    def _detect_tick_time_offset(self) -> float:
        """Compare broker's reported tick time to our local UTC clock.
        Returns the integer-hour offset that needs to be SUBTRACTED from
        the broker's tick.time to convert it to real UTC. Returns 0 if
        the broker is already using real UTC.

        Falls back to 0 if no fresh tick is available."""
        import time as _time
        from datetime import datetime as _dt, timezone as _tz
        # Try up to 3 times to get a fresh tick
        for _ in range(3):
            tick = self.mt5.symbol_info_tick("XAUUSD")
            if tick is not None and tick.time > 0:
                broker_unix = tick.time
                now_unix = _dt.now(_tz.utc).timestamp()
                diff_hours = (broker_unix - now_unix) / 3600.0
                # Round to nearest hour
                offset = round(diff_hours)
                # Sanity: only accept offsets in [-12, +12] hours
                if -12 <= offset <= 12:
                    # If diff is < 5 minutes, broker is sending real UTC
                    if abs(diff_hours) < (5/60):
                        return 0
                    return float(offset)
            _time.sleep(0.5)
        log.warning("Could not detect broker time offset — assuming real UTC (0h)")
        return 0.0

    def shutdown(self):
        self.mt5.shutdown()

    def get_m5_close(self, symbol: str, utc_time: pd.Timestamp) -> Optional[float]:
        # Use copy_rates_range to specifically request the M5 bar ENDING at
        # utc_time. Apply the autodetected offset so the time we send matches
        # this broker's expected encoding.
        m5_start = utc_time - pd.Timedelta(minutes=5)
        broker_offset = pd.Timedelta(hours=self.tick_time_offset_hours)
        m5_start_send = (m5_start + broker_offset).tz_localize(None).to_pydatetime()
        m5_end_send   = (utc_time  + broker_offset).tz_localize(None).to_pydatetime()
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
        tick = self.mt5.symbol_info_tick("XAUUSD")
        if tick is None:
            raise RuntimeError("symbol_info_tick returned None — symbol not subscribed?")
        broker_ts = pd.Timestamp(tick.time, unit='s', tz='UTC')
        return broker_ts - pd.Timedelta(hours=self.tick_time_offset_hours)

    def get_account_info(self) -> dict:
        """Pull current account state from MT5. Returns {} on failure."""
        try:
            info = self.mt5.account_info()
            if info is None:
                return {}
            return {
                'login': int(info.login),
                'balance': float(info.balance),
                'equity':  float(info.equity),
                'margin':  float(info.margin),
                'margin_free': float(info.margin_free),
                'currency': info.currency,
                'leverage': int(info.leverage),
                'server':   info.server,
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

    def place_stop_order(self, symbol: str, side: str, price: float,
                         lot: float, sl: float, tp: float,
                         comment: str = "AUREON_v2", dry_run: bool = False):
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
            "magic": 20260522,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_DAY,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if dry_run:
            log.info(f"[PAPER] Would place {side} stop {symbol} @ {price} lot={lot} SL={sl} TP={tp}")
            return {'paper': True, 'request': req}
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
            existing = self.find_pending_by_price(symbol, side, price, lot)
            if existing is not None:
                log.info(
                    f"✅ Placed {side} stop @ {price} lot={lot}: rc=-1 but RECONCILED — "
                    f"ticket {existing.ticket} found in broker state"
                )
                # Build a minimal SendResult-like shim so callers can read .retcode/.order
                class _ReconciledResult:
                    retcode = 10009
                    order   = int(existing.ticket)
                    deal    = 0
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
                    class _R: retcode = 10009; comment = "RECONCILED_SLTP"
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
                log.warning(f"⚠ Modify SL ticket={ticket}: rc=-1 + position not found in broker state — position may have closed")
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
                           dry_run: bool = False):
        """Place an IMMEDIATE market order. Used only for in-flight breakout
        recovery: when pre-flight passed but broker rejected anyway because
        price moved past the threshold during the millisecond order was in flight."""
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
            "magic": 20260522,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_DAY,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        rc = result.retcode if result else -1
        rc_name = _MT5_RETCODE_MAP.get(rc, f"UNKNOWN_{rc}")
        if rc == 10009:
            log.info(f"✅ MARKET {side} filled @ {price} lot={lot}: retcode={rc} ({rc_name})")
        else:
            err = result.comment if result and hasattr(result, 'comment') else ''
            log.error(f"❌ MARKET {side} REJECTED: retcode={rc} ({rc_name}) {err}")
        return result


def run_live(cfg: Config, paper: bool = True):
    """
    Live or paper trading. Connects to the already-running MT5 terminal
    on this machine (which must be logged into your broker account first).
    Delegates to LiveTrader (live_trader.py) for the full event loop.
    """
    from live_trader import LiveTrader
    adapter = MT5Adapter()
    try:
        trader = LiveTrader(cfg, adapter, paper=paper)
        trader.run()
    finally:
        adapter.shutdown()


# ============================================================================
# CLI
# ============================================================================

def main():
    # Load .env if present (no-op if not). Must run BEFORE telemetry import
    # reads env vars in submodules.
    from env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(description="AUREON v2 bot — XAUUSD multi-anchor")
    parser.add_argument('mode', choices=['backtest', 'paper', 'live'])
    parser.add_argument('--csv', help="Path to M1 CSV (backtest mode)")
    parser.add_argument('--start', default='2025-01-01')
    parser.add_argument('--end',   default='2026-12-31')
    parser.add_argument('--output-dir', default='./output')
    parser.add_argument('--lot', type=float, default=None)
    parser.add_argument('--balance', type=float, default=None)

    parser.add_argument('--i-understand-the-risks', action='store_true',
                        help="Required for live mode")
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args()

    global log
    log = setup_logging(args.log_level)

    cfg = Config()
    if args.lot is not None: cfg.lot_size = args.lot
    if args.balance is not None: cfg.starting_balance = args.balance

    if args.mode == 'backtest':
        if not args.csv:
            log.error("Backtest mode requires --csv"); sys.exit(1)
        cfg.min_step = 0.0  # clean math in backtest
        os.makedirs(args.output_dir, exist_ok=True)
        df = run_backtest(args.csv, args.start, args.end, cfg)
        if len(df) == 0:
            log.warning("No trades produced. Check CSV and date range.")
            return
        stats = summarize_backtest(df, cfg)
        log.info(f"\n{'='*60}\nBACKTEST SUMMARY\n{'='*60}")
        for k, v in stats.items():
            if k == 'monthly_pnl': continue
            log.info(f"  {k:20s} = {v}")
        log.info("\nMonthly P&L:")
        for m, p in stats['monthly_pnl'].items():
            log.info(f"  {m}  ${p:>10,.2f}")
        # Save outputs
        trades_path = os.path.join(args.output_dir, 'trades.csv')
        stats_path  = os.path.join(args.output_dir, 'stats.json')
        df.to_csv(trades_path, index=False)
        with open(stats_path, 'w') as f:
            json.dump(stats, f, indent=2)
        log.info(f"\nWrote {trades_path} and {stats_path}")

    elif args.mode == 'paper':
        run_live(cfg, paper=True)

    elif args.mode == 'live':
        if not args.i_understand_the_risks:
            log.error("Live mode requires --i-understand-the-risks flag. Real money at stake. "
                      "Re-read AUREON_V2_SPEC.md §4 (Risk Management) and §5 (Live Adjustments) first.")
            sys.exit(1)
        run_live(cfg, paper=False)


if __name__ == '__main__':
    main()