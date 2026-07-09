"""AUREON — BE-scratch "left on table" ANALYZER (read-only measurement).

`python bot.py bescratchscan` — quantifies how often the +$2.5 -> breakeven ladder
rung scratches a trade flat at $0 on a pullback that then keeps trending, and how
much profit that costs, BEFORE anyone decides to loosen the rung. It changes NO
live behavior: it only reads the recorded journal + the bot's own price log and
replays the strategy math off-line.

DATA SOURCES (best available, stated in the output header)
  trades : run/journal/trades_*.csv (live record; has max_favorable + exit_reason)
           -> fallback: Firestore aureon_forex docs (read-only stream).
  prices : run/price_log/price_<broker_date>.csv (the bot's own per-second mid,
           written live) resampled to M1 OHLC -> fallback: --m1csv <path>.
           If neither covers a trade's window it is marked insufficient_data.

ASSUMPTIONS (stated in header)
  - lookforward horizon = entry + HOLD_MIN(45) + POST_EXIT_HORIZON_MIN(30).
  - replay bars are built from MID prices (the live engine triggers on bid/ask);
    SL/TP can therefore under-trigger by ~half a spread. A replay-vs-journal
    fidelity line is printed so this approximation is visible, not hidden.

GUARDRAILS: read-only. No Firestore writes, no config change, no order placement.
"""
import csv
import glob
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from config import Config
from telemetry import telemetry_from_env

log = logging.getLogger("AUREON")

# Counterfactual BE-arm thresholds: +2.5 is current; the rest are "looser".
BE_THRESHOLDS = [2.5, 3.5, 4.0, 5.0]
HOLD_MIN = 45                 # the freeze/hold window (cfg.freeze_minutes)
POST_EXIT_HORIZON_MIN = 30    # lookforward past the BE exit for "left on table"
CONTINUE_EPS = 2.0           # post-exit MFE >= this ($) => "continued in-favor"
RUNNER_USD = 100.0           # a replayed scratch ending >= this is a "runner saved"


# ===========================================================================
# Trade loading
# ===========================================================================
@dataclass
class Trade:
    date_ist: str
    anchor: str
    side: str
    entry: float
    lot: float
    max_fav_dist: float          # favorable EXCURSION in $ (journal 'max_favorable')
    exit_reason: str
    pnl_usd: float
    entry_utc: Optional[pd.Timestamp] = None
    exit_utc: Optional[pd.Timestamp] = None
    role: str = "normal"
    # filled in by analysis
    is_be_scratch: bool = False
    left_on_table: Optional[float] = None   # $ (None = insufficient_data)
    continuation: str = ""                    # in_favor | reversed | flat | insufficient_data
    post_mfe: Optional[float] = None          # $ favorable excursion after exit
    post_mae: Optional[float] = None          # $ adverse excursion after exit


def _anchor_key(label: str) -> str:
    """'A2_10h_London' -> 'A2' for the per-anchor breakdown; tolerant of junk."""
    return (label or "?")[:2]


def _ist_to_utc(date_ist, hms):
    try:
        if not date_ist or not hms:
            return None
        ts = pd.Timestamp(f"{date_ist} {hms}", tz="Asia/Kolkata")
        return ts.tz_convert("UTC")
    except Exception:
        return None


def load_trades_from_journal(journal_dir) -> List[Trade]:
    out: List[Trade] = []
    for path in sorted(glob.glob(os.path.join(journal_dir, "trades_*.csv"))):
        try:
            with open(path, newline="", encoding='utf-8') as f:
                for r in csv.DictReader(f):
                    try:
                        entry = float(r.get("entry_price") or 0)
                        e_utc = _ist_to_utc(r.get("date_ist"), r.get("entry_time_ist"))
                        x_utc = _ist_to_utc(r.get("date_ist"), r.get("exit_time_ist"))
                        if e_utc is not None and x_utc is not None and x_utc < e_utc:
                            x_utc = x_utc + pd.Timedelta(days=1)  # crossed midnight IST
                        out.append(Trade(
                            date_ist=r.get("date_ist", ""),
                            anchor=r.get("anchor", "?"),
                            side=(r.get("side") or "").upper(),
                            entry=entry,
                            lot=float(r.get("lot") or 0) or 0.35,
                            max_fav_dist=float(r.get("max_favorable") or 0),
                            exit_reason=(r.get("exit_reason") or "").strip(),
                            pnl_usd=float(r.get("realized_pnl_usd") or 0),
                            entry_utc=e_utc, exit_utc=x_utc,
                            role=(r.get("role") or "normal").strip() or "normal"))
                    except Exception as e:
                        log.warning(f"bescratch: skip bad journal row in {path}: {e!r}")
        except Exception as e:
            log.warning(f"bescratch: could not read {path}: {e!r}")
    return out


def load_trades_from_firestore() -> List[Trade]:
    """Read-only fallback: stream aureon_forex docs -> trades. Times come from
    open_time/close_time (ISO) when present."""
    out: List[Trade] = []
    try:
        import firebase_journal
        db = firebase_journal._client()
        if db is None:
            return out
        for snap in db.collection(firebase_journal.COLLECTION).stream():
            d = snap.to_dict() or {}
            day = d.get("day") or snap.id
            for a in (d.get("anchors") or []):
                for t in (a.get("trades") or []):
                    try:
                        def _u(x):
                            try:
                                return pd.Timestamp(x).tz_convert("UTC") if x else None
                            except Exception:
                                try:
                                    return pd.Timestamp(x, tz="UTC") if x else None
                                except Exception:
                                    return None
                        out.append(Trade(
                            date_ist=str(day), anchor=a.get("label", "?"),
                            side=(t.get("side") or "").upper(),
                            entry=float(t.get("entry_price") or 0),
                            lot=float(t.get("lot") or 0) or 0.35,
                            max_fav_dist=float(t.get("max_favorable") or 0),
                            exit_reason=(t.get("exit_reason") or "").strip(),
                            pnl_usd=float(t.get("pnl") or 0),
                            entry_utc=_u(t.get("open_time")), exit_utc=_u(t.get("close_time")),
                            role=(t.get("role") or "normal")))
                    except Exception:
                        continue
    except Exception as e:
        log.warning(f"bescratch: firestore fallback failed: {e!r}")
    return out


# ===========================================================================
# Price history (the bot's own per-second price log -> M1 bars)
# ===========================================================================
def load_price_mid(run_dir, m1csv=None) -> Optional[pd.DataFrame]:
    """Return a UTC-indexed DataFrame with a 'mid' column from the bot's price
    logs (or an --m1csv fallback). None if no price data is available at all."""
    if m1csv:
        try:
            df = pd.read_csv(m1csv)
            tcol = next((c for c in df.columns if c.lower() in
                         ("time", "utc", "timestamp", "date")), df.columns[0])
            df["__t"] = pd.to_datetime(df[tcol], utc=True)
            mid_col = next((c for c in df.columns if c.lower() in
                            ("mid", "close", "c")), None)
            if mid_col is None:
                return None
            df = df[["__t", mid_col]].rename(columns={"__t": "utc", mid_col: "mid"})
            return df.dropna().set_index("utc").sort_index()
        except Exception as e:
            log.warning(f"bescratch: --m1csv read failed: {e!r}")
            return None
    frames = []
    for path in sorted(glob.glob(os.path.join(run_dir, "price_log", "price_*.csv"))):
        try:
            df = pd.read_csv(path, usecols=lambda c: c in ("utc", "mid"))
            if "utc" not in df.columns or "mid" not in df.columns:
                continue
            df["utc"] = pd.to_datetime(df["utc"], utc=True, errors="coerce")
            frames.append(df.dropna())
        except Exception as e:
            log.warning(f"bescratch: could not read {path}: {e!r}")
    if not frames:
        return None
    full = pd.concat(frames, ignore_index=True).dropna()
    return full.set_index("utc").sort_index()


def _m1_bars(mid: pd.DataFrame, start, end) -> Optional[pd.DataFrame]:
    """Resample the mid series in [start, end] to M1 OHLC. None if no coverage."""
    if mid is None:
        return None
    try:
        seg = mid.loc[(mid.index >= start) & (mid.index <= end), "mid"]
        if seg.empty:
            return None
        bars = seg.resample("1min").ohlc().dropna()
        return bars if not bars.empty else None
    except Exception:
        return None


# ===========================================================================
# Strategy replay mirror (faithful copy of strategy.update_position_on_bar with
# the ONE BE-lock rung threshold parametrized). Kept in lock-step with
# strategy.py; varying be_lock + cfg.be_trigger is the ONLY change. Analysis
# only — the live engine is never touched.
# ===========================================================================
def _replay_one(entry, side, lot, entry_utc, bars, cfg, be_lock, eod_utc, role="normal"):
    """Replay one position over M1 `bars` (OHLC, UTC index) with the BE-lock rung
    armed at `be_lock`. Returns (outcome, exit_price, pnl_usd)."""
    sgn = 1.0 if side == "BUY" else -1.0
    sl = entry - sgn * cfg.sl_dist
    tp = entry + sgn * cfg.tp_dist
    max_fav_price = entry

    def settle(px, outcome):
        dist = (px - entry) if side == "BUY" else (entry - px)
        return outcome, px, dist * cfg.contract_size * lot

    for ts, bar in bars.iterrows():
        if ts < entry_utc:
            continue
        # EOD flatten (broker 23:00) — close at the bar close.
        if eod_utc is not None and ts >= eod_utc:
            return settle(float(bar["close"]), "EOD")
        # 1. SL (pre-bar)
        if side == "BUY":
            if bar["low"] <= sl:
                return settle(sl, "SL" if sl <= entry - cfg.sl_dist + 0.01 else "Trail")
        else:
            if bar["high"] >= sl:
                return settle(sl, "SL" if sl >= entry + cfg.sl_dist - 0.01 else "Trail")
        # 2. peak favorable
        if side == "BUY":
            if bar["high"] > max_fav_price:
                max_fav_price = bar["high"]
            fav = max_fav_price - entry
        else:
            if bar["low"] < max_fav_price:
                max_fav_price = bar["low"]
            fav = entry - max_fav_price
        fav = max(fav, 0.0)
        # freeze gate
        in_freeze = False
        try:
            in_freeze = ((ts - entry_utc).total_seconds() / 60.0) < cfg.freeze_minutes
        except Exception:
            in_freeze = False

        def ratchet(level):
            nonlocal sl
            if side == "BUY":
                if level > sl:
                    sl = level
            else:
                if level < sl:
                    sl = level
        # 3. ladder (fires even during freeze). be_lock is the parametrized rung.
        if fav >= 10.00:
            ratchet(entry + sgn * max(8.00, fav - 2.00))
        elif role != "rescue":
            if fav >= 6.00:
                ratchet(entry + sgn * 4.00)
            elif fav >= be_lock:
                ratchet(entry)
        # 4. post-freeze trail, armed at cfg.be_trigger
        if not in_freeze and fav >= cfg.be_trigger:
            if side == "BUY":
                cand = max(entry, max_fav_price - cfg.trail_gap)
                if cand > sl + cfg.min_step:
                    sl = cand
            else:
                cand = min(entry, max_fav_price + cfg.trail_gap)
                if cand < sl - cfg.min_step:
                    sl = cand
        # 5. TP
        if side == "BUY":
            if bar["high"] >= tp:
                return settle(tp, "TP")
        else:
            if bar["low"] <= tp:
                return settle(tp, "TP")
    # ran out of bars — close at last bar close (best-effort)
    last = bars.iloc[-1]
    return settle(float(last["close"]), "EOD")


def _eod_utc_for(entry_utc, broker_tz_offset_hours):
    """Broker 23:00 EOD flatten instant (UTC) on the entry's broker date."""
    try:
        broker_dt = entry_utc + pd.Timedelta(hours=broker_tz_offset_hours)
        eod_broker = broker_dt.normalize() + pd.Timedelta(hours=23)
        return eod_broker - pd.Timedelta(hours=broker_tz_offset_hours)
    except Exception:
        return None


# ===========================================================================
# Analysis
# ===========================================================================
def _classify_be_scratch(t: Trade, be_arm: float) -> bool:
    """BE-scratch = a BE/near-BE exit on a trade that had ARMED the +be_arm rung
    (max favorable excursion reached >= be_arm)."""
    be_exit = t.exit_reason in ("BE", "SL_be")
    armed = t.max_fav_dist >= be_arm
    return bool(be_exit and armed)


def _left_on_table(t: Trade, mid, cfg) -> None:
    """Fill t.left_on_table / continuation / post_mfe / post_mae from the price
    log over [exit, entry+HOLD+POST]. Marks insufficient_data when no coverage."""
    if t.entry_utc is None or t.exit_utc is None or mid is None:
        t.continuation = "insufficient_data"
        return
    look_end = t.entry_utc + pd.Timedelta(minutes=HOLD_MIN + POST_EXIT_HORIZON_MIN)
    look_end = max(look_end, t.exit_utc + pd.Timedelta(minutes=POST_EXIT_HORIZON_MIN))
    try:
        seg = mid.loc[(mid.index >= t.exit_utc) & (mid.index <= look_end), "mid"]
    except Exception:
        seg = None
    if seg is None or seg.empty:
        t.continuation = "insufficient_data"
        return
    if t.side == "BUY":
        mfe = float(seg.max()) - t.entry
        mae = t.entry - float(seg.min())
    else:
        mfe = t.entry - float(seg.min())
        mae = float(seg.max()) - t.entry
    t.post_mfe = round(mfe, 2)
    t.post_mae = round(mae, 2)
    t.left_on_table = round(max(mfe, 0.0) * cfg.contract_size * t.lot, 2)
    if mfe >= CONTINUE_EPS and mfe >= mae:
        t.continuation = "in_favor"
    elif mae >= CONTINUE_EPS and mae > mfe:
        t.continuation = "reversed"
    else:
        t.continuation = "flat"


def _replay_grid(trades: List[Trade], mid, cfg) -> Dict:
    """For each BE-arm threshold, replay every trade that has price coverage and
    tally net P&L, scratches avoided, extra full-SL hits, runners saved. Baseline
    is the replay at +2.5 (apples-to-apples with the same engine + data)."""
    SL_USD = cfg.sl_dist * cfg.contract_size * cfg.lot_size
    # Pre-build each replayable trade's bars once.
    replayable = []
    for t in trades:
        if t.entry_utc is None or t.side not in ("BUY", "SELL"):
            continue
        eod = _eod_utc_for(t.entry_utc, cfg.broker_tz_offset_hours)
        end = eod or (t.entry_utc + pd.Timedelta(hours=10))
        bars = _m1_bars(mid, t.entry_utc - pd.Timedelta(minutes=1), end)
        if bars is None:
            continue
        replayable.append((t, bars, eod))

    results = {}
    baseline_outcomes = {}
    for thr in BE_THRESHOLDS:
        net = 0.0
        outcomes = {}
        for t, bars, eod in replayable:
            oc, _px, pnl = _replay_one(t.entry, t.side, t.lot, t.entry_utc, bars,
                                       cfg, thr, eod, role=t.role)
            outcomes[id(t)] = (oc, pnl)
            net += pnl
        if thr == 2.5:
            baseline_outcomes = outcomes
        results[thr] = {"net": round(net, 2), "outcomes": outcomes}

    # Derive deltas vs baseline (+2.5).
    grid = []
    for thr in BE_THRESHOLDS:
        outs = results[thr]["outcomes"]
        scratches_avoided = extra_sl = runners_saved = 0
        for t, bars, eod in replayable:
            b_oc, b_pnl = baseline_outcomes.get(id(t), ("", 0.0))
            oc, pnl = outs.get(id(t), ("", 0.0))
            base_scratch = (b_oc in ("BE", "Trail") and abs(b_pnl) < 50.0)
            if base_scratch and not (oc in ("BE", "Trail") and abs(pnl) < 50.0):
                scratches_avoided += 1
                if pnl >= RUNNER_USD:
                    runners_saved += 1
            if oc == "SL" and b_oc != "SL":
                extra_sl += 1
        grid.append({
            "thr": thr, "net": results[thr]["net"],
            "delta": round(results[thr]["net"] - results[2.5]["net"], 2),
            "scratches_avoided": scratches_avoided,
            "extra_sl": extra_sl, "runners_saved": runners_saved,
        })
    return {"grid": grid, "n_replayed": len(replayable), "sl_usd": SL_USD,
            "baseline_net": results[2.5]["net"] if replayable else 0.0}


# ===========================================================================
# Entry point
# ===========================================================================
def run_bescratchscan(start=None, end=None, run_dir=None, m1csv=None,
                      horizon_min=POST_EXIT_HORIZON_MIN):
    global POST_EXIT_HORIZON_MIN
    POST_EXIT_HORIZON_MIN = int(horizon_min)
    cfg = Config()
    run_dir = run_dir or os.environ.get("AUREON_RUN_DIR", "./run")
    journal_dir = os.path.join(run_dir, "journal")
    tele = telemetry_from_env(component="AUREON-bescratch")

    # --- load trades ---
    src = "journal trades_*.csv"
    trades = load_trades_from_journal(journal_dir)
    if not trades:
        trades = load_trades_from_firestore()
        src = "Firestore aureon_forex (journal CSVs not found)"
    # date-range filter (inclusive, on date_ist)
    if start:
        trades = [t for t in trades if t.date_ist and t.date_ist >= start]
    if end:
        trades = [t for t in trades if t.date_ist and t.date_ist <= end]
    trades = [t for t in trades if t.role != "rescue"]  # rescue legs have no BE rung

    mid = load_price_mid(run_dir, m1csv=m1csv)
    price_src = (f"--m1csv {m1csv}" if m1csv else
                 "price_log per-second mid -> M1" if mid is not None else
                 "NONE (no price_log / --m1csv)")

    # --- classify + left-on-table ---
    for t in trades:
        t.is_be_scratch = _classify_be_scratch(t, 2.5)
        if t.is_be_scratch:
            _left_on_table(t, mid, cfg)
    scratches = [t for t in trades if t.is_be_scratch]
    in_favor = [t for t in scratches if t.continuation == "in_favor"]
    reversed_ = [t for t in scratches if t.continuation == "reversed"]
    insuff = [t for t in scratches if t.continuation == "insufficient_data"]
    lot = []
    for t in in_favor:
        if t.left_on_table is not None:
            lot.append(t.left_on_table)
    total_lot = round(sum(lot), 2)
    mean_lot = round(total_lot / len(lot), 2) if lot else 0.0
    med_lot = round(float(pd.Series(lot).median()), 2) if lot else 0.0

    # per-anchor breakdown
    per_anchor: Dict[str, Dict] = {}
    for t in scratches:
        k = _anchor_key(t.anchor)
        d = per_anchor.setdefault(k, {"n": 0, "in_favor": 0, "left": 0.0})
        d["n"] += 1
        if t.continuation == "in_favor":
            d["in_favor"] += 1
            d["left"] += (t.left_on_table or 0.0)

    grid_info = _replay_grid(trades, mid, cfg)
    grid = grid_info["grid"]
    SL_USD = grid_info["sl_usd"]

    dates = sorted({t.date_ist for t in trades if t.date_ist})
    span = f"{dates[0]} … {dates[-1]} ({len(dates)} trading days)" if dates else "no trades"

    # ---------------- console report ----------------
    lines = []
    lines.append("=" * 72)
    lines.append("AUREON — BE-scratch 'left on table' analyzer (READ-ONLY)")
    lines.append("=" * 72)
    lines.append(f"trades source : {src}  ({len(trades)} non-rescue trades)")
    lines.append(f"price source  : {price_src}")
    lines.append(f"date range    : {span}")
    lines.append(f"lookforward   : entry + {HOLD_MIN}m hold + {POST_EXIT_HORIZON_MIN}m "
                 f"post-exit; bars = MID-based M1 (SL/TP may under-trigger ~½ spread)")
    lines.append(f"BE rung now   : +$2.50 -> breakeven   |   SL = ${SL_USD:,.0f} "
                 f"(lot {cfg.lot_size} × ${cfg.sl_dist:.0f})")
    lines.append("-" * 72)
    lines.append(f"BE-scratch events: {len(scratches)}")
    lines.append(f"  continued in-favor (cost us) : {len(in_favor)}")
    lines.append(f"  reversed (BE correctly saved): {len(reversed_)}")
    if insuff:
        lines.append(f"  insufficient_data (no price) : {len(insuff)}")
    lines.append(f"  $ left on table (in-favor)   : total ${total_lot:,.2f} | "
                 f"mean ${mean_lot:,.2f} | median ${med_lot:,.2f}")
    lines.append("-" * 72)
    lines.append("per-anchor BE-scratches (n / in-favor / $left):")
    for k in sorted(per_anchor):
        d = per_anchor[k]
        lines.append(f"  {k}: {d['n']:>2} / {d['in_favor']:>2} / ${d['left']:,.0f}")
    lines.append("-" * 72)
    lines.append(f"COUNTERFACTUAL RUNG TEST (replayed {grid_info['n_replayed']} trades "
                 f"with price coverage; baseline = +2.5 replay)")
    lines.append(f"replay fidelity: +2.5 replayed net = ${grid_info['baseline_net']:,.2f} "
                 f"(compare to your journal net for the range as a sanity check)")
    lines.append(f"{'rung':>6} {'net $':>11} {'Δ vs +2.5':>11} {'scratch_avoid':>14} "
                 f"{'extra_SL':>9} {'runners':>8}")
    for g in grid:
        tag = "  (current)" if g["thr"] == 2.5 else ""
        lines.append(f"{('+'+format(g['thr'],'.1f')):>6} {g['net']:>11,.0f} "
                     f"{g['delta']:>+11,.0f} {g['scratches_avoided']:>14} "
                     f"{g['extra_sl']:>9} {g['runners_saved']:>8}{tag}")
    lines.append("-" * 72)

    # ---------------- verdict ----------------
    verdict = _verdict(scratches, in_favor, total_lot, grid, SL_USD, span,
                       insuff, grid_info["n_replayed"])
    lines.append("VERDICT: " + verdict)
    lines.append("=" * 72)
    report = "\n".join(lines)
    print(report)
    log.info("bescratchscan complete")

    # Telegram: header + headline numbers + the grid + verdict (ts_header auto).
    tg = [
        "🔬 *BE-scratch scan* (read-only)",
        f"source: {src.split('(')[0].strip()} · price: "
        f"{'price_log' if (mid is not None and not m1csv) else (m1csv or 'NONE')}",
        f"range: {span}",
        f"BE-scratches: *{len(scratches)}* — in-favor *{len(in_favor)}* / "
        f"reversed *{len(reversed_)}*"
        + (f" / insuff {len(insuff)}" if insuff else ""),
        f"left on table: *${total_lot:,.0f}* (mean ${mean_lot:,.0f} / med ${med_lot:,.0f})",
        "rung Δnet / scratch-avoid / extra-SL:",
    ]
    for g in grid:
        if g["thr"] == 2.5:
            continue
        tg.append(f"  +{g['thr']:.1f}: Δ${g['delta']:+,.0f} / "
                  f"{g['scratches_avoided']} / {g['extra_sl']}")
    tg.append("➡️ " + verdict)
    try:
        tele.info("\n".join(tg))
    finally:
        try:
            tele.stop(timeout=6.0)
        except Exception:
            pass
    return 0


def _verdict(scratches, in_favor, total_lot, grid, SL_USD, span, insuff, n_replayed):
    if not scratches:
        return f"Over {span}: 0 BE-scratch events found — nothing to loosen."
    if n_replayed == 0:
        return (f"Over {span}: {len(scratches)} BE-scratches, "
                f"{len(in_favor)} continued in-favor (~${total_lot:,.0f} left on "
                f"table), but NO price history was available to replay the rung "
                f"counterfactual — re-run on the VPS where run/price_log exists "
                f"(insufficient_data on {len(insuff)} events).")
    # best looser rung by net delta
    loosers = [g for g in grid if g["thr"] != 2.5]
    best = max(loosers, key=lambda g: g["delta"]) if loosers else None
    head = (f"Over {span}: {len(scratches)} BE-scratches, {len(in_favor)} continued "
            f"in-favor costing ~${total_lot:,.0f} left on table.")
    if best is None or best["delta"] <= 0:
        worst_cost = min((g["delta"] for g in loosers), default=0)
        return (head + f" Every looser rung tested nets WORSE on the replay "
                f"(best Δ ${best['delta']:+,.0f} at +{best['thr']:.1f}); "
                f"loosening NOT supported by this data.")
    return (head + f" Loosening to +{best['thr']:.1f}→BE would net "
            f"${best['delta']:+,.0f} ({best['scratches_avoided']} scratches avoided, "
            f"{best['runners_saved']} runners saved, {best['extra_sl']} extra "
            f"${SL_USD:,.0f} SL hits) → {'WORTH IT' if best['delta'] > 0 else 'marginal'} "
            f"on recorded trades. Confirm on a longer window before changing live.")
