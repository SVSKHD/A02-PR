#!/usr/bin/env python3
"""Export ROGUE's REAL closed trades from the MT5 terminal history.

Standalone, read-only. Pulls every closed position tagged with ROGUE's magic
(20260626 — the monster engine; NOT 20260811, which is the aureon_new_non_oco
engine) straight from MT5 deal history, pairs entry/exit deals into positions,
and writes one row per closed position to rogue_trades_export.csv, then prints a
summary.

GROUND TRUTH (mirrors pnl_source.py): realized $ of a position =
sum(profit + swap + commission) over its deals; the realized P&L lives on the
OUT deal (entry == 1). We sum across ALL of a position's deals so commission
booked on the entry deal and any partial closes are never dropped. TESTFIRE
deals (comment marker "TF_") are excluded symmetrically, exactly as the engine's
own accounting excludes them.

NOTHING is reconstructed or estimated. Any field the terminal does not provide
is left EMPTY (see the per-field notes below).

Run ON THE MT5 TERMINAL HOST (Windows, with the MetaTrader5 package and a logged-in
terminal):

    python rogue_trades_export.py                 # full history the terminal holds
    python rogue_trades_export.py --from 2026-01-01 --to 2026-08-13
    python rogue_trades_export.py --magic 20260626 --out rogue_trades_export.csv

Optional connection args (only if the terminal is not already running/attached):
    --terminal "C:\\Path\\terminal64.exe" --login 12345 --password ... --server ...

Field notes (honesty):
  * sl_at_open : taken from the opening ORDER's stop (history_orders_get). If the
                 order record is unavailable, left EMPTY (never guessed).
  * exit_reason: the BROKER's deal reason on the closing deal (SL / TP / CLIENT /
                 EXPERT / SO / ...). NOTE a trail-out is a broker SL modify, so it
                 is booked as "SL" by the broker — the terminal does not label
                 "TRAIL" vs "initial SL". Left EMPTY if the broker gives no reason.
  * chain_link : the order comment encodes only ENTRY vs CHAIN ("AUR_ROGUE_E" /
                 "AUR_ROGUE_C", see rogue_monster_live.py:268,278). So link 0
                 (entry) => 0; a chained link => EMPTY, because the exact link
                 ordinal (1/2/3) is NOT encoded in the comment. Not guessed.
  * arm_reason : NOT encoded in the broker comment (the comment is just
                 "AUR_ROGUE_E"/"AUR_ROGUE_C"). The ATRx/VEL/BOX arm reason lives
                 only in the local decision log (rogue_monster_log), not in broker
                 history. Left EMPTY here.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict, OrderedDict
from datetime import datetime, timezone

ROGUE_MAGIC_DEFAULT = 20260626
TEST_COMMENT_MARK = "TF_"

CSV_COLUMNS = [
    "open_time", "close_time", "side", "lot", "entry", "exit", "sl_at_open",
    "exit_reason", "profit", "commission", "swap", "net", "magic", "comment",
    "chain_link", "arm_reason",
]


# ── MT5 connection ────────────────────────────────────────────────────────────
def connect(args):
    try:
        import MetaTrader5 as mt5
    except Exception as e:
        sys.exit(f"MetaTrader5 package not importable ({e!r}). Run this on the "
                 f"Windows MT5 terminal host with `pip install MetaTrader5`.")
    kw = {}
    if args.terminal:
        kw["path"] = args.terminal
    if args.login:
        kw.update(login=int(args.login), password=args.password or "", server=args.server or "")
    ok = mt5.initialize(**kw) if kw else mt5.initialize()
    if not ok:
        sys.exit(f"mt5.initialize() failed: {mt5.last_error()}. Is the terminal "
                 f"running and logged in?")
    return mt5


def _reason_name(mt5, code):
    """Map an MT5 deal reason int to a short label, else the raw code / ''. """
    if code is None:
        return ""
    table = {
        getattr(mt5, "DEAL_REASON_CLIENT", 0): "CLIENT",
        getattr(mt5, "DEAL_REASON_MOBILE", 1): "MOBILE",
        getattr(mt5, "DEAL_REASON_WEB", 2): "WEB",
        getattr(mt5, "DEAL_REASON_EXPERT", 3): "EXPERT",
        getattr(mt5, "DEAL_REASON_SL", 4): "SL",
        getattr(mt5, "DEAL_REASON_TP", 5): "TP",
        getattr(mt5, "DEAL_REASON_SO", 6): "SO",          # stop-out (margin)
    }
    try:
        return table.get(int(code), str(int(code)))
    except Exception:
        return ""


def _order_sl(mt5, position_id, order_ticket):
    """Best-effort stop of the OPENING order. Returns a float or None (never guessed)."""
    for getter in (
        lambda: mt5.history_orders_get(ticket=int(order_ticket)) if order_ticket else None,
        lambda: mt5.history_orders_get(position=int(position_id)) if position_id else None,
    ):
        try:
            orders = getter() or []
        except Exception:
            orders = []
        for o in orders:
            sl = getattr(o, "sl", None)
            if sl not in (None, 0.0):
                return float(sl)
    return None


# ── core export ───────────────────────────────────────────────────────────────
def build_rows(mt5, deals):
    """Group ROGUE deals by position_id and emit one row per CLOSED position
    (a position with at least one OUT deal). Oldest-open first."""
    by_pos = defaultdict(list)
    for d in deals:
        pid = getattr(d, "position_id", None)
        if pid is None:
            continue
        by_pos[int(pid)].append(d)

    rows = []
    for pid, dl in by_pos.items():
        dl.sort(key=lambda d: (getattr(d, "time", 0) or 0,
                               0 if getattr(d, "entry", 1) == 0 else 1))
        ins = [d for d in dl if getattr(d, "entry", None) == 0]
        outs = [d for d in dl if getattr(d, "entry", None) == 1]
        if not outs:
            continue  # still open — not a closed trade
        first_in = ins[0] if ins else None
        last_out = outs[-1]

        # side from the opening deal type: DEAL_TYPE_BUY(0)=LONG, DEAL_TYPE_SELL(1)=SHORT
        side = ""
        if first_in is not None:
            t = int(getattr(first_in, "type", -1))
            side = "LONG" if t == 0 else ("SHORT" if t == 1 else "")

        # realized $ summed across ALL of the position's deals (commission often on IN)
        profit = round(sum(float(getattr(d, "profit", 0.0) or 0.0) for d in outs), 2)
        commission = round(sum(float(getattr(d, "commission", 0.0) or 0.0) for d in dl), 2)
        swap = round(sum(float(getattr(d, "swap", 0.0) or 0.0) for d in dl), 2)
        net = round(profit + commission + swap, 2)

        lot = float(getattr(first_in, "volume", 0.0) or 0.0) if first_in else \
            float(getattr(last_out, "volume", 0.0) or 0.0)
        entry_px = float(getattr(first_in, "price", 0.0) or 0.0) if first_in else ""
        exit_px = float(getattr(last_out, "price", 0.0) or 0.0)
        magic = int(getattr(first_in if first_in else last_out, "magic", 0) or 0)
        comment = str(getattr(first_in if first_in else last_out, "comment", "") or "")

        sl = _order_sl(mt5, pid, getattr(first_in, "order", None) if first_in else None)

        # chain_link from the comment marker only (E => 0; C => unknown ordinal => blank)
        cl = ""
        if comment.endswith("_E") or comment.endswith("_ENTRY"):
            cl = 0
        # comment.endswith("_C") -> chained link, ordinal not encoded -> leave blank

        rows.append({
            "open_time": _fmt(first_in) if first_in else "",
            "close_time": _fmt(last_out),
            "side": side,
            "lot": lot,
            "entry": entry_px,
            "exit": exit_px,
            "sl_at_open": "" if sl is None else round(sl, 2),
            "exit_reason": _reason_name(mt5, getattr(last_out, "reason", None)),
            "profit": profit,
            "commission": commission,
            "swap": swap,
            "net": net,
            "magic": magic,
            "comment": comment,
            "chain_link": cl,
            "arm_reason": "",  # not in broker history
            "_open_epoch": int(getattr(first_in, "time", 0) or 0) if first_in else int(getattr(last_out, "time", 0) or 0),
            "_close_epoch": int(getattr(last_out, "time", 0) or 0),
        })

    rows.sort(key=lambda r: r["_open_epoch"])
    return rows


def _fmt(deal):
    ts = int(getattr(deal, "time", 0) or 0)
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ── summary ───────────────────────────────────────────────────────────────────
def summarize(rows):
    print("\n" + "=" * 68)
    print(f"ROGUE closed-trade summary  (magic {rows[0]['magic'] if rows else 'n/a'})")
    print("=" * 68)
    n = len(rows)
    if not n:
        print("No closed ROGUE trades found in the terminal history.")
        return
    closed = [r for r in rows if r["_close_epoch"]]
    closed.sort(key=lambda r: r["_close_epoch"])
    d0 = datetime.fromtimestamp(min(r["_open_epoch"] for r in rows if r["_open_epoch"]), tz=timezone.utc)
    d1 = datetime.fromtimestamp(max(r["_close_epoch"] for r in closed), tz=timezone.utc)
    nets = [r["net"] for r in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    net_total = round(sum(nets), 2)
    gross_win = round(sum(wins), 2)
    gross_loss = round(sum(losses), 2)
    pf = (gross_win / abs(gross_loss)) if gross_loss else float("inf")
    avg_win = round(gross_win / len(wins), 2) if wins else 0.0
    avg_loss = round(gross_loss / len(losses), 2) if losses else 0.0
    win_rate = round(100.0 * len(wins) / n, 1)

    # max drawdown on the closed-trade equity curve (ordered by close time)
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    print(f"trades         : {n}")
    print(f"date range     : {d0:%Y-%m-%d %H:%M} -> {d1:%Y-%m-%d %H:%M} UTC")
    print(f"net            : ${net_total:,.2f}")
    print(f"win rate       : {win_rate}%  ({len(wins)}W / {len(losses)}L"
          f"{f' / {n - len(wins) - len(losses)} scratch' if n - len(wins) - len(losses) else ''})")
    print(f"profit factor  : {pf:.2f}" + ("" if losses else "  (no losing trades)"))
    print(f"avg win        : ${avg_win:,.2f}")
    print(f"avg loss       : ${avg_loss:,.2f}")
    print(f"max drawdown   : ${max_dd:,.2f}  (on closed-trade equity curve)")

    # per-month net
    print("\nper-month net:")
    months = OrderedDict()
    for r in closed:
        key = datetime.fromtimestamp(r["_close_epoch"], tz=timezone.utc).strftime("%Y-%m")
        months.setdefault(key, [0.0, 0])
        months[key][0] += r["net"]
        months[key][1] += 1
    for k, (v, c) in months.items():
        print(f"  {k} : ${v:>12,.2f}   ({c} trades)")

    # split by chain link (0 = entry; blank = chained; anything else as-is)
    print("\nsplit by chain link:")
    buckets = OrderedDict()
    for r in rows:
        key = "entry (link 0)" if r["chain_link"] == 0 else (
            "chained (link >=1)" if r["chain_link"] == "" else f"link {r['chain_link']}")
        buckets.setdefault(key, [0.0, 0])
        buckets[key][0] += r["net"]
        buckets[key][1] += 1
    for k, (v, c) in buckets.items():
        print(f"  {k:<20}: ${v:>12,.2f}   ({c} trades)")
    print("=" * 68)
    print("Notes: exit_reason is the BROKER deal reason (a trail-out books as 'SL'); "
          "arm_reason is blank (not in broker history); chained links show blank "
          "chain_link because the ordinal is not encoded in the order comment.")


# ── main ──────────────────────────────────────────────────────────────────────
def parse_dt(s, end=False):
    if not s:
        return None
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt


def main():
    ap = argparse.ArgumentParser(description="Export ROGUE closed trades from MT5 history.")
    ap.add_argument("--magic", type=int, default=ROGUE_MAGIC_DEFAULT)
    ap.add_argument("--from", dest="dt_from", default=None, help="YYYY-MM-DD (default: earliest history)")
    ap.add_argument("--to", dest="dt_to", default=None, help="YYYY-MM-DD (default: now)")
    ap.add_argument("--out", default="rogue_trades_export.csv")
    ap.add_argument("--include-test", action="store_true", help="do NOT exclude TF_ testfire deals")
    ap.add_argument("--terminal", default=None)
    ap.add_argument("--login", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None)
    args = ap.parse_args()

    mt5 = connect(args)
    try:
        dt_from = parse_dt(args.dt_from) or datetime(2000, 1, 1, tzinfo=timezone.utc)
        dt_to = parse_dt(args.dt_to) or datetime.now(tz=timezone.utc)
        deals = mt5.history_deals_get(dt_from, dt_to)
        if deals is None:
            sys.exit(f"history_deals_get failed: {mt5.last_error()}")
        deals = list(deals)
        # filter to ROGUE magic, drop TF_ testfire deals (symmetric) unless asked
        ours = []
        for d in deals:
            if int(getattr(d, "magic", 0) or 0) != args.magic:
                continue
            if (not args.include_test) and TEST_COMMENT_MARK in str(getattr(d, "comment", "") or ""):
                continue
            ours.append(d)
        rows = build_rows(mt5, ours)
        write_csv(rows, args.out)
        print(f"Wrote {len(rows)} closed-position row(s) to {args.out}")
        if ours:
            times = [int(getattr(d, "time", 0) or 0) for d in ours if getattr(d, "time", 0)]
            if times:
                lo = datetime.fromtimestamp(min(times), tz=timezone.utc)
                hi = datetime.fromtimestamp(max(times), tz=timezone.utc)
                print(f"Deal history covered: {lo:%Y-%m-%d} -> {hi:%Y-%m-%d} UTC "
                      f"({len(ours)} ROGUE deals)")
        summarize(rows)
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
