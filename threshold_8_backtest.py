"""threshold_8 — backtest & parameter sweep.

Drives ``threshold_8_replay.ReplayEngine`` (the single bar loop) over M1 gold data and
reports HONEST numbers, including the RESCUE_ENABLED=False control arm.

Data. If a CSV is supplied (columns DATE/TIME/OPEN/HIGH/LOW/CLOSE/TICKVOL/VOL/SPREAD)
it is used. Timestamps are interpreted in the broker SERVER timezone (UTC+3), declared
explicitly. The repo ships no gold CSV, so absent a path a deterministic, seeded
synthetic M1 series is generated — with real (non-zero) SPREAD and TICKVOL and genuine
weekend gaps — so the sweep runs end-to-end. Synthetic results are labelled as such.

This module contains NO second bar-iteration loop over price: it only builds/loads
bars and calls ReplayEngine.run (grep-clean for check C15).
"""

import csv
import math
import os
import random
from datetime import datetime, timedelta, timezone

from threshold_8_config import THRESHOLD_8_ANCHOR_HHMM, _parse_hhmm

from threshold_8_config import Threshold8Params, THRESHOLD_8_SERVER_TZ_OFFSET_H
from threshold_8_replay import ReplayEngine, Bar
from threshold_8_basket import ROLE_RESCUE


SERVER_TZ = timezone(timedelta(hours=THRESHOLD_8_SERVER_TZ_OFFSET_H))
_RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "backtest", "results")


# ======================================================================================
# data
# ======================================================================================
def generate_synthetic_m1(n_weeks=3, seed=8, start_price=2000.0):
    """Deterministic seeded gold M1 series in broker server time (UTC+3).

    Trading window Mon 00:00 -> Fri 23:00 server; weekends skipped -> real gap. Daily
    range is tuned so the +/-$20 breakout straddle fires intraday; a few session-open
    spike bars exceed the trigger within a single M5 so anchor-stop UNPLACEABLE events
    actually occur (never fabricated — the path is exercised by real spikes)."""
    rng = random.Random(seed)
    anchor_h, anchor_m = _parse_hhmm(THRESHOLD_8_ANCHOR_HHMM)
    anchor_minute = anchor_h * 60 + anchor_m       # minute-of-day the anchor window opens
    bars = []
    # first Monday
    day = datetime(2026, 1, 5, 0, 0, tzinfo=SERVER_TZ)   # 2026-01-05 is a Monday
    price = start_price
    trading_day = -1
    for _w in range(n_weeks):
        for _d in range(5):                              # Mon..Fri
            trading_day += 1
            day_drift = rng.uniform(-8.0, 8.0)
            intraday_amp = rng.uniform(18.0, 34.0)       # peak-to-trough intent
            phase = rng.uniform(0.0, 6.28)
            # On a scheduled subset of days a news spike lands inside the anchor M5
            # window itself, so the anchor bar's own range runs past +/-TRIGGER_DIST
            # before the straddle can be placed -> a genuine UNPLACEABLE event. This is
            # deterministic (every 3rd trading day) so the path is reliably exercised.
            anchor_news = (trading_day % 3 == 0)
            anchor_news_dir = rng.choice([-1, 1])
            # 23 hourly * 60 = M1 bars 00:00..22:59
            base = price
            for minute in range(23 * 60):
                t = day.replace(hour=0, minute=0) + timedelta(minutes=minute)
                frac = minute / (23 * 60.0)
                cyc = math.sin(phase + frac * 6.28 * rng.choice([1, 1, 2]))
                target = base + day_drift * frac + intraday_amp * 0.5 * cyc
                noise = rng.gauss(0, 0.6)
                o = price
                c = target + noise
                # occasional intrabar spike (news / session open) -> big M5 range
                spike = 0.0
                if rng.random() < 0.004:
                    spike = rng.choice([-1, 1]) * rng.uniform(12.0, 26.0)
                # anchor-window news spike (first minute of the anchor M5 window)
                if anchor_news and minute == anchor_minute:
                    spike += anchor_news_dir * rng.uniform(22.0, 30.0)
                hi = max(o, c) + abs(rng.gauss(0, 0.5)) + max(0.0, spike)
                lo = min(o, c) - abs(rng.gauss(0, 0.5)) + min(0.0, spike)
                spread_pts = rng.randint(15, 45)         # $0.15..$0.45, always > 0
                tickvol = rng.randint(20, 400)           # always > 0
                bars.append(Bar(t, round(o, 2), round(hi, 2), round(lo, 2),
                                round(c, 2), tickvol, tickvol, spread_pts))
                price = c
            # carry a small overnight drift
            price = price + rng.uniform(-3.0, 3.0)
            day = day + timedelta(days=1)
        # weekend gap: jump to next Monday with a gap move
        day = day + timedelta(days=2)
        price = price + rng.uniform(-15.0, 15.0)         # weekend gap
    return bars


def load_csv_m1(path):
    """Load M1 bars from a broker CSV. Columns (case-insensitive):
    DATE, TIME, OPEN, HIGH, LOW, CLOSE, TICKVOL, VOL, SPREAD. Timestamps are stamped
    into the broker server timezone (UTC+3)."""
    bars = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower(): c for c in reader.fieldnames}
        for row in reader:
            d = row[cols["date"]].replace(".", "-")
            t = row[cols["time"]]
            dt = datetime.strptime("%s %s" % (d, t), "%Y-%m-%d %H:%M:%S") \
                if len(t.split(":")) == 3 else \
                datetime.strptime("%s %s" % (d, t), "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=SERVER_TZ)
            bars.append(Bar(
                dt, float(row[cols["open"]]), float(row[cols["high"]]),
                float(row[cols["low"]]), float(row[cols["close"]]),
                float(row[cols.get("tickvol", cols.get("vol"))]),
                float(row[cols.get("vol", cols.get("tickvol"))]),
                float(row[cols["spread"]]),
            ))
    return bars


def weekend_gap_sanity(bars):
    """Return (n_weekend_gaps, max_gap_minutes). A healthy gold M1 dataset in server
    time has weekend gaps (~48h). Used for check C14."""
    gaps = 0
    max_gap = 0.0
    for i in range(1, len(bars)):
        dm = (bars[i].dt - bars[i - 1].dt).total_seconds() / 60.0
        if dm > max_gap:
            max_gap = dm
        if dm >= 12 * 60:      # >= 12h => a session/weekend gap
            gaps += 1
    return gaps, max_gap


# ======================================================================================
# post-pass fill audit (C2 / C3)
# ======================================================================================
def audit_fills(engine, tol=1e-6):
    """Every recorded fill price must satisfy bar_low - tol <= price <= bar_high + tol."""
    violations = []
    for f in engine.fills:
        if f.price < f.low - tol or f.price > f.high + tol:
            violations.append(f)
    return violations


# ======================================================================================
# metrics
# ======================================================================================
def summarize(records, engine):
    n = len(records)
    out = {
        "baskets": n,
        "rescue_rate": 0.0,
        "rescue_success_rate": None,
        "net_pnl": 0.0,
        "profit_factor": None,
        "win_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "max_daily_loss": 0.0,
        "max_drawdown": 0.0,
        "days_hitting_DAILY_RISK": len(engine.days_hit_daily_risk),
        "exit_reason_counts": dict(engine.exit_reason_counts),
        "unplaceable_count": engine.unplaceable_count,
        "mean_spread": (sum(engine.spread_prices_used) / len(engine.spread_prices_used)
                        if engine.spread_prices_used else 0.0),
    }
    if n == 0:
        return out

    nets = [r["net_pnl"] for r in records]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    rescued = [r for r in records if r["rescued"]]
    rescued_ok = [r for r in rescued if r["net_pnl"] >= 0]

    out["net_pnl"] = round(sum(nets), 2)
    out["win_rate"] = round(len(wins) / n, 4)
    out["avg_win"] = round(sum(wins) / len(wins), 2) if wins else 0.0
    out["avg_loss"] = round(sum(losses) / len(losses), 2) if losses else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    out["profit_factor"] = round(gross_win / gross_loss, 3) if gross_loss > 0 else None
    out["rescue_rate"] = round(len(rescued) / n, 4)
    out["rescue_success_rate"] = round(len(rescued_ok) / len(rescued), 4) if rescued else None

    # equity curve in close order -> max drawdown
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    out["max_drawdown"] = round(max_dd, 2)

    # daily P/L -> worst day
    by_day = {}
    for r in records:
        day = r["anchor_time"][:10]
        by_day[day] = by_day.get(day, 0.0) + r["net_pnl"]
    out["max_daily_loss"] = round(min(by_day.values()), 2) if by_day else 0.0
    return out


# ======================================================================================
# CSV writer
# ======================================================================================
_CSV_FIELDS = ["basket_id", "anchor_id", "anchor_time", "symbol", "trigger_dist",
               "entry_side", "entry_price", "rescued", "rescue_price", "rescue_lot",
               "max_floating_loss", "max_floating_profit", "peak_net", "trail_engaged",
               "trail_rung_reached", "exit_reason", "net_pnl", "duration_min",
               "unplaceable"]


def write_basket_csv(records, label):
    os.makedirs(_RESULTS_DIR, exist_ok=True)
    safe = label.replace(" ", "_").replace("=", "").replace(".", "p")
    path = os.path.join(_RESULTS_DIR, "threshold_8_%s.csv" % safe)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        w.writeheader()
        for r in records:
            w.writerow(r)
    return path


# ======================================================================================
# sweep
# ======================================================================================
def build_sweep():
    """RESCUE_ENABLED [False,True] x RESCUE_DIST [8,10,12] x RESCUE_LOT_MULT [1.0,1.2]
    x TRAIL_MODE [ladder,atr]. TRIGGER_DIST fixed at 20. The disabled cells collapse to
    one control per trail mode."""
    configs = []
    for trail_mode in ("ladder", "atr"):
        # control arm
        configs.append(Threshold8Params(rescue_enabled=False, trail_mode=trail_mode))
        for dist in (8.0, 10.0, 12.0):
            for mult in (1.00, 1.20):
                configs.append(Threshold8Params(
                    rescue_enabled=True, rescue_dist=dist, rescue_lot_mult=mult,
                    trail_mode=trail_mode))
    return configs


def run_one(params, bars, symbol="XAUUSD"):
    engine = ReplayEngine(params, symbol)
    records = engine.run(bars)
    metrics = summarize(records, engine)
    violations = audit_fills(engine)
    return engine, records, metrics, violations


def _cfg_label(p):
    if not p.rescue_enabled:
        return "CONTROL rescue=OFF trail=%s" % p.trail_mode
    return "rescue=ON dist=%.0f mult=%.2f trail=%s" % (
        p.rescue_dist, p.rescue_lot_mult, p.trail_mode)


def run_sweep(bars, synthetic=True):
    configs = build_sweep()
    gaps, max_gap = weekend_gap_sanity(bars)
    results = []
    print("=" * 100)
    print("threshold_8 SWEEP  |  bars=%d  |  server_tz=UTC+%d  |  weekend_gaps=%d  "
          "max_gap=%.0fmin  |  data=%s"
          % (len(bars), THRESHOLD_8_SERVER_TZ_OFFSET_H, gaps, max_gap,
             "SYNTHETIC(seed=8)" if synthetic else "CSV"))
    print("=" * 100)

    any_fill_violation = False
    for p in configs:
        engine, records, m, violations = run_one(p, bars)
        if violations:
            any_fill_violation = True
        label = _cfg_label(p)
        write_basket_csv(records, label)
        results.append((p, label, m, len(violations)))

        if m["baskets"] == 0:
            print("\n[%s]" % label)
            print("  why: 0 baskets — no anchor straddle filled. Check TRIGGER_DIST vs "
                  "intraday range, ANCHOR_HHMM alignment, or data span.")
            continue

        print("\n[%s]" % label)
        print("  baskets=%d  rescue_rate=%.2f  rescue_success=%s  unplaceable=%d  "
              "fill_violations=%d"
              % (m["baskets"], m["rescue_rate"],
                 "%.2f" % m["rescue_success_rate"] if m["rescue_success_rate"] is not None else "n/a",
                 m["unplaceable_count"], len(violations)))
        print("  net_pnl=%.2f  PF=%s  win_rate=%.2f  avg_win=%.2f  avg_loss=%.2f"
              % (m["net_pnl"],
                 "%.2f" % m["profit_factor"] if m["profit_factor"] is not None else "n/a",
                 m["win_rate"], m["avg_win"], m["avg_loss"]))
        print("  max_drawdown=%.2f  max_daily_loss=%.2f  days_hit_DAILY_RISK=%d  "
              "mean_spread=$%.3f"
              % (m["max_drawdown"], m["max_daily_loss"], m["days_hitting_DAILY_RISK"],
                 m["mean_spread"]))
        print("  exit_reasons=%s" % m["exit_reason_counts"])

    _print_verdict(results)
    return results, any_fill_violation


def _print_verdict(results):
    # control per trail mode
    controls = {}
    for p, label, m, _ in results:
        if not p.rescue_enabled:
            controls[p.trail_mode] = m

    beats = []
    for p, label, m, _ in results:
        if not p.rescue_enabled:
            continue
        ctrl = controls.get(p.trail_mode)
        if ctrl is None or m["baskets"] == 0:
            continue
        if m["net_pnl"] > ctrl["net_pnl"] and m["max_drawdown"] < ctrl["max_drawdown"]:
            beats.append((label, m, ctrl))

    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    if not beats:
        print("NO rescue configuration beats its RESCUE_ENABLED=False control on BOTH "
              "net_pnl AND max_drawdown.")
        print("The rescue counter-leg does not improve risk-adjusted P/L on this data; "
              "the control arm is at least as good.")
    else:
        print("The following rescue configs beat their control on BOTH net_pnl and "
              "max_drawdown:")
        for label, m, ctrl in beats:
            print("  %-40s net %.2f vs ctrl %.2f | dd %.2f vs ctrl %.2f"
                  % (label, m["net_pnl"], ctrl["net_pnl"], m["max_drawdown"],
                     ctrl["max_drawdown"]))
    for tm, cm in controls.items():
        print("  control[%s]: net=%.2f  max_drawdown=%.2f  baskets=%d"
              % (tm, cm["net_pnl"], cm["max_drawdown"], cm["baskets"]))
    print("=" * 100)


def main(csv_path=None):
    if csv_path and os.path.exists(csv_path):
        bars = load_csv_m1(csv_path)
        synthetic = False
    else:
        bars = generate_synthetic_m1()
        synthetic = True
    return run_sweep(bars, synthetic=synthetic)


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else None)
