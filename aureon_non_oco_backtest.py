"""aureon_new_non_oco — standalone backtest harness.

Replays an XAUUSD M1 CSV through the SAME pure decision core the live engine uses
(`aureon_non_oco.AnchorDaySession`), so the backtest and live behaviour are one and
the same code path (modulo real spread / slippage / execution friction). For each
broker day it arms an observation session at each configured anchor (default A2, A5),
drives it bar-by-bar, simulates the fills against the bar OHLC, and reports the trade
tally, net P&L, win rate, profit factor, max drawdown, the monthly breakdown, and the
exit-reason split — the section-6 numbers used as a correctness check.

CSV columns (UTC timestamps): time, open, high, low, close  [, spread].

    python aureon_non_oco_backtest.py --csv XAUUSD_M1.csv \
        --start 2025-08-01 --end 2026-07-31 --lot 1.0 --commission 7 --spread 0.0

The engine trades A2 (10:00) and A5 (19:30, skipped Fridays) in BROKER time
(UTC + broker_tz_offset_hours; default +3), flattens at 23:30 broker, and applies the
whole observe -> confirm -> ladder -> chain mechanism from config.py's anc_* defaults.

IMPORTANT: the section-6 reference numbers (~1,327 trades, net ~$121.7k, 83.3% win,
PF 1.38) are IN-SAMPLE on the author's own XAUUSD M1 data for Aug 2025 - Jul 2026 with
their spread series. They are a correctness check on the implementation, not an
expected return, and reproducing them requires that same data + spread. Point --csv at
your M1 history and set --spread/--commission to your broker's costs.
"""
import argparse
import logging

import pandas as pd

from config import Config
from aureon_non_oco import AnchorDaySession, AncParams, ema

log = logging.getLogger("AUREON")

CONTRACT = 100.0   # XAUUSD: $100 account per $1 price move per 1.0 lot


class _Ema:
    """Incremental SMA-seeded EMA over the continuous M1 close stream (O(1)/bar)."""
    def __init__(self, period):
        self.period = int(period)
        self.k = 2.0 / (self.period + 1.0)
        self.val = None
        self._seed = []

    def update(self, close):
        if self.val is None:
            self._seed.append(float(close))
            if len(self._seed) >= self.period:
                self.val = sum(self._seed) / self.period
        else:
            self.val = float(close) * self.k + self.val * (1.0 - self.k)
        return self.val


def _anchor_utc_minutes(cfg, want):
    """Map each active anchor label -> its minute-of-day in UTC (broker time - offset)."""
    import anchors as _anch
    off = int(getattr(cfg, "broker_tz_offset_hours", 3))
    out = {}
    for label, h, m in cfg.anchors:
        if label[:2] in want or label in want:
            out[label] = ((h * 60 + m) - off * 60) % 1440
    return out


def _flat_utc_minute(cfg):
    off = int(getattr(cfg, "broker_tz_offset_hours", 3))
    fh = float(getattr(cfg, "anc_flat_broker_hour", 23.5))
    hh = int(fh)
    mm = int(round((fh - hh) * 60))
    return ((hh * 60 + mm) - off * 60) % 1440


def run(df, cfg, lot=1.0, commission=7.0, spread=0.0):
    """Replay `df` (UTC-indexed M1 OHLC) and return (trades, stats).

    Cost model (both applied per trade, in account $):
      commission = commission * lot          (round-turn, e.g. $7/lot)
      spread     = spread * lot * CONTRACT    (spread is $/oz; 0.0 = ignore)
    """
    p = AncParams.from_cfg(cfg)
    want = set(str(x) for x in getattr(cfg, "anc_anchors", ["A2", "A5"]))
    anchor_min = _anchor_utc_minutes(cfg, want)
    flat_min = _flat_utc_minute(cfg)
    off = int(getattr(cfg, "broker_tz_offset_hours", 3))
    a5_skip_fri = bool(getattr(cfg, "a5_skip_friday", True))

    ema_state = _Ema(p.ema_period)
    sessions = {}          # label -> AnchorDaySession (active today)
    armed_today = {}       # label -> broker-date it was armed for (one per day)
    trades = []
    cur_broker_day = None

    times = df.index
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    lo = df["low"].to_numpy()
    c = df["close"].to_numpy()

    for i in range(len(df)):
        ts = times[i]
        ema_val = ema_state.update(c[i])
        broker_ts = ts + pd.Timedelta(hours=off)
        broker_day = broker_ts.date()
        if broker_day != cur_broker_day:
            cur_broker_day = broker_day
            sessions = {}
            armed_today = {}

        mod = ts.hour * 60 + ts.minute
        # arm a session when its anchor minute strikes (once per broker day)
        for label, amin in anchor_min.items():
            if mod != amin or armed_today.get(label) == broker_day:
                continue
            if label[:2] == "A5" and a5_skip_fri and broker_ts.weekday() == 4:
                armed_today[label] = broker_day
                continue
            flat_ts = ts.floor("D") + pd.Timedelta(minutes=flat_min)
            if flat_ts <= ts:
                flat_ts += pd.Timedelta(days=1)
            sess = AnchorDaySession(label, float(o[i]), p, flat_ts=flat_ts)
            sessions[label] = sess
            armed_today[label] = broker_day

        # drive every active session with this closed bar
        bar = {"open": float(o[i]), "high": float(h[i]),
               "low": float(lo[i]), "close": float(c[i])}
        for label, sess in list(sessions.items()):
            if sess.done:
                continue
            for ev in sess.on_m1_close(bar, ts, ema_val):
                if ev.get("kind") == "EXIT":
                    trades.append(ev)

    return trades, _summarize(trades, lot, commission, spread)


def _summarize(trades, lot, commission, spread):
    cost = commission * lot + spread * lot * CONTRACT
    net = 0.0
    gross_win = 0.0
    gross_loss = 0.0
    wins = 0
    reasons = {"lock": 0, "target": 0, "stop": 0, "EOD": 0}
    reached_target = 0
    monthly = {}
    equity = 0.0
    peak_eq = 0.0
    max_dd = 0.0
    for t in trades:
        pnl = t["pnl_price"] * lot * CONTRACT - cost
        net += pnl
        if pnl > 0:
            wins += 1
            gross_win += pnl
        else:
            gross_loss += -pnl
        reasons[t.get("reason", "EOD")] = reasons.get(t.get("reason", "EOD"), 0) + 1
        if t.get("peak_fav", 0.0) >= 10.0:
            reached_target += 1
        mkey = pd.Timestamp(t["exit_time"]).strftime("%Y-%m")
        monthly[mkey] = monthly.get(mkey, 0.0) + pnl
        equity += pnl
        peak_eq = max(peak_eq, equity)
        max_dd = max(max_dd, peak_eq - equity)
    n = len(trades)
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else float("inf")
    return {
        "trades": n,
        "net": round(net, 2),
        "win_pct": round(100.0 * wins / n, 1) if n else 0.0,
        "profit_factor": round(pf, 2),
        "max_drawdown": round(max_dd, 2),
        "reached_+10": reached_target,
        "exits": reasons,
        "worst_month": round(min(monthly.values()), 2) if monthly else 0.0,
        "monthly": {k: round(v, 2) for k, v in sorted(monthly.items())},
    }


def load_csv(path, start=None, end=None):
    df = pd.read_csv(path)
    tcol = "time" if "time" in df.columns else df.columns[0]
    df[tcol] = pd.to_datetime(df[tcol], utc=True)
    df = df.set_index(tcol).sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)]
    return df


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="aureon_new_non_oco backtest")
    ap.add_argument("--csv", required=True, help="XAUUSD M1 CSV (time,open,high,low,close[,spread])")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--lot", type=float, default=1.0)
    ap.add_argument("--commission", type=float, default=7.0, help="$/lot round-turn")
    ap.add_argument("--spread", type=float, default=0.0, help="$/oz spread cost per trade")
    args = ap.parse_args()

    cfg = Config()
    df = load_csv(args.csv, args.start, args.end)
    log.info(f"Loaded {len(df):,} M1 bars {df.index[0]} -> {df.index[-1]}")
    trades, stats = run(df, cfg, lot=args.lot, commission=args.commission, spread=args.spread)

    print("\n" + "=" * 60)
    print("AUREON NEW NON-OCO — BACKTEST")
    print("=" * 60)
    for k in ("trades", "net", "win_pct", "profit_factor", "max_drawdown",
              "reached_+10", "worst_month"):
        print(f"  {k:16s} = {stats[k]}")
    print(f"  exits            = lock {stats['exits']['lock']} / "
          f"target {stats['exits']['target']} / stop {stats['exits']['stop']} / "
          f"EOD {stats['exits']['EOD']}")
    print("\nMonthly P&L:")
    for m, v in stats["monthly"].items():
        print(f"  {m}  ${v:>12,.2f}")
    print("\nReference (in-sample correctness check): ~1,327 trades, net ~$121,700, "
          "83.3% win, PF 1.38, max DD ~$11,500, exits ~907/180/163/77, ~180 reach +10.")


if __name__ == "__main__":
    main()
