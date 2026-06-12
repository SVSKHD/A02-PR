#!/usr/bin/env python3
"""
AUREON v2 — Daily auto-analysis.

Designed to run as a daily cron job (or systemd timer). Performs:

  1. Compute rolling 12-month window: [today − 365d, today]
  2. Fetch XAUUSD M1 from MT5 over that window  (fetch_data.fetch_m1)
  3. Run the AUREON v2 backtest                 (bot.run_backtest)
  4. Compute monthly stats                      (bot.summarize_backtest)
  5. Send a formatted summary to Telegram
  6. Save a markdown report under reports/AUREON_analysis_{date}.md

CLI usage
---------
    python auto_analyze.py
    python auto_analyze.py --days 180     # half year instead
    python auto_analyze.py --skip-fetch --csv data/old.csv   # skip fetch, reuse CSV

Cron recommendation
-------------------
Run once per day after the broker week settles, e.g. 09:00 UTC weekdays:

    # crontab -e
    0 9 * * 1-5  cd /home/trader/aureon_v2 && /usr/bin/python3 auto_analyze.py >> /var/log/aureon-analyze.log 2>&1

Or as a systemd timer (preferred):
    /etc/systemd/system/aureon-analyze.service  +  aureon-analyze.timer
    See README for the unit file.

Prerequisite
------------
The MetaTrader 5 terminal must be running and logged into your broker
account on this machine BEFORE running this script. No credentials are
read — mt5.initialize() inherits the active terminal session.

Optional env vars
-----------------
    AUREON_TELEGRAM_TOKEN  — bot token; if absent, summary only logs
    AUREON_TELEGRAM_CHAT   — chat id
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

import pandas as pd

from telemetry import telemetry_from_env, Severity


log = logging.getLogger("AUREON-analyze")


def format_summary(stats: Dict, start: date, end: date) -> str:
    """Format a Telegram-friendly summary message."""
    total_pips = stats["total_pips"]
    total_usd  = stats["total_usd"]
    win_rate   = stats["win_rate"]
    max_dd     = stats["max_dd"]
    max_dd_pct = stats["max_dd_pct"]
    sl_count   = stats["sl_count"]
    kill_days  = stats["kill_days"]
    avg_pips   = stats["avg_per_month_pips"]
    avg_usd    = stats["avg_per_month_usd"]

    lines = [
        f"📊 *AUREON v2 — rolling backtest*",
        f"Window: `{start}` → `{end}` ({(end-start).days}d)",
        f"",
        f"💰 Total: `${total_usd:+,.0f}` ({total_pips:+.0f} pips)",
        f"📅 Avg / month: `${avg_usd:+,.0f}` ({avg_pips:+.1f} pips)",
        f"🎯 Win rate: `{win_rate:.1f}%`",
        f"📉 Max DD: `${max_dd:,.0f}` ({max_dd_pct:.1f}%)",
        f"🔴 SLs: `{sl_count}`  |  🚨 Kill days: `{kill_days}`",
        f"",
        f"*Monthly breakdown:*",
    ]
    for m, p in stats["monthly_pnl"].items():
        emoji = "✅" if p > 0 else ("➖" if p == 0 else "📉")
        lines.append(f"{emoji} `{m}`: `${p:+,.0f}`")
    return "\n".join(lines)


def format_full_report(stats: Dict, df: pd.DataFrame,
                       start: date, end: date) -> str:
    """Full markdown report (more detailed than Telegram summary)."""
    monthly = stats["monthly_pnl"]
    md = []
    md.append(f"# AUREON v2 — Rolling Analysis")
    md.append(f"*Generated {datetime.now(timezone.utc).isoformat()}*")
    md.append("")
    md.append(f"## Window")
    md.append(f"- Start: `{start}`")
    md.append(f"- End:   `{end}`")
    md.append(f"- Days:  `{(end-start).days}`")
    md.append("")
    md.append(f"## Aggregate")
    md.append("")
    md.append("| Metric | Value |")
    md.append("|--------|------:|")
    md.append(f"| Total fills | {stats['fills']} |")
    md.append(f"| Total pips | {stats['total_pips']:+.2f} |")
    md.append(f"| Total USD (lot 0.5) | ${stats['total_usd']:+,.2f} |")
    md.append(f"| Win rate | {stats['win_rate']:.2f}% |")
    md.append(f"| Max drawdown | ${stats['max_dd']:,.2f} ({stats['max_dd_pct']:.2f}%) |")
    md.append(f"| TP exits | {stats['tp_count']} |")
    md.append(f"| SL exits | {stats['sl_count']} |")
    md.append(f"| Best day | ${stats['best_day']:+,.2f} |")
    md.append(f"| Worst day | ${stats['worst_day']:+,.2f} |")
    md.append(f"| Kill-switch days | {stats['kill_days']} |")
    md.append(f"| Months observed | {stats['months']} |")
    md.append(f"| Avg / month | ${stats['avg_per_month_usd']:+,.2f} ({stats['avg_per_month_pips']:+.2f} pips) |")
    md.append("")
    md.append("## Monthly P&L")
    md.append("")
    md.append("| Month | P&L USD | Status |")
    md.append("|-------|--------:|:------:|")
    for m, p in monthly.items():
        status = "✅" if p > 0 else ("➖" if p == 0 else "📉")
        md.append(f"| {m} | ${p:+,.2f} | {status} |")
    md.append("")
    md.append("## Per-anchor productivity")
    md.append("")
    if "anchor" in df.columns:
        agg = df.groupby("anchor").agg(
            fills=("pnl_dist", "count"),
            wins=("pnl_dist", lambda x: (x > 0).sum()),
            sls=("outcome", lambda x: (x == "SL").sum()),
            pips=("pnl_dist", "sum"),
            usd=("pnl_usd", "sum"),
        )
        md.append("| Anchor | Fills | Wins | SLs | Pips | USD |")
        md.append("|--------|------:|-----:|----:|-----:|----:|")
        for anch, r in agg.iterrows():
            md.append(f"| `{anch}` | {int(r['fills'])} | {int(r['wins'])} | "
                      f"{int(r['sls'])} | {r['pips']:+.2f} | ${r['usd']:+,.2f} |")
    md.append("")
    return "\n".join(md)


def run_analysis(days: int = 365,
                 reuse_csv: Optional[str] = None,
                 output_dir: str = "."):
    """Run one full analysis cycle. Returns the stats dict."""
    tele = telemetry_from_env(component="AUREON-analyze")

    today = date.today()
    start = today - timedelta(days=days)
    log.info(f"Window: {start} → {today}")

    try:
        # 1. Fetch
        if reuse_csv and os.path.exists(reuse_csv):
            log.info(f"Skipping fetch, using existing CSV: {reuse_csv}")
            data_path = reuse_csv
            tele.info(f"📊 Daily analysis (using cached CSV)\n"
                      f"Window: `{start}` → `{today}`")
        else:
            data_dir = os.path.join(output_dir, "data")
            os.makedirs(data_dir, exist_ok=True)
            data_path = os.path.join(
                data_dir,
                f"XAUUSD_M1_{start}_to_{today}.csv"
            )

            tele.info(f"📊 Daily analysis starting\nWindow: `{start}` → `{today}`")
            from fetch_data import fetch_m1
            t0 = time.time()
            fetch_m1(
                symbol="XAUUSD",
                start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                end=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc),
                output_path=data_path,
            )
            tele.success(f"✅ Fetched {(today-start).days}d of M1 in "
                         f"{time.time()-t0:.0f}s")

        # 2. Backtest
        from bot import Config, run_backtest, summarize_backtest
        cfg = Config()
        cfg.min_step = 0.0
        log.info("Running backtest...")
        t0 = time.time()
        df = run_backtest(data_path, str(start), str(today), cfg)
        if len(df) == 0:
            tele.error("Backtest produced no trades — check data")
            tele.stop()
            return None
        log.info(f"Backtest complete in {time.time()-t0:.1f}s ({len(df)} trades)")

        # 3. Summarize
        stats = summarize_backtest(df, cfg)

        # 4. Send Telegram summary
        summary = format_summary(stats, start, today)
        sev = Severity.SUCCESS if stats["total_usd"] > 0 else Severity.WARN
        tele.send(summary, sev)

        # 5. Save full report
        reports_dir = os.path.join(output_dir, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        report_path = os.path.join(reports_dir, f"AUREON_analysis_{today}.md")
        with open(report_path, "w") as f:
            f.write(format_full_report(stats, df, start, today))
        log.info(f"Saved report: {report_path}")

        # Also save the latest trades CSV
        trades_path = os.path.join(reports_dir, f"trades_{today}.csv")
        df.to_csv(trades_path, index=False)
        log.info(f"Saved trades: {trades_path}")

        tele.info(f"📄 Report saved: `{report_path}`")
        return stats

    except Exception as e:
        log.exception("Analysis failed")
        tele.critical(f"❌ Daily analysis FAILED: `{e}`")
        raise
    finally:
        tele.stop()


def main():
    # Load .env if present
    from env_loader import load_env
    load_env()

    p = argparse.ArgumentParser(description="AUREON v2 daily auto-analysis")
    p.add_argument("--days", type=int, default=365,
                   help="Rolling window in days (default 365)")
    p.add_argument("--csv", default=None,
                   help="Reuse this CSV instead of fetching fresh")
    p.add_argument("--skip-fetch", action="store_true",
                   help="Alias for --csv data/...latest.csv")
    p.add_argument("--output-dir", default=".",
                   help="Where to write data/ and reports/ subdirs")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    csv = args.csv
    if args.skip_fetch and not csv:
        # Find most recent CSV in data/
        data_dir = os.path.join(args.output_dir, "data")
        if os.path.isdir(data_dir):
            cands = sorted(f for f in os.listdir(data_dir)
                           if f.startswith("XAUUSD_M1_") and f.endswith(".csv"))
            if cands:
                csv = os.path.join(data_dir, cands[-1])

    stats = run_analysis(days=args.days,
                         reuse_csv=csv,
                         output_dir=args.output_dir)
    if stats is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
