"""AUREON — Firebase (Firestore) daily / weekly trade journal.  schema_version 2.

Persists one document per trading day to the `aureon_forex` collection so the
live forward record survives VPS redeploys and is queryable off-box.

Fail-safe by construction: a missing firebase-admin package, a missing service
key, or any network / Firestore error is logged and swallowed -- this module
can NEVER raise into the caller, so it can never block trading, the EOD
flatten, or startup. The journal.py call sites double-guard anyway.

The service-account key lives at C:\\A02-PR\\firebase_key.json (git-ignored,
never committed); override the location with the AUREON_FIREBASE_KEY env var.
"""
import csv
import glob
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("AUREON")

SCHEMA_VERSION = 2
COLLECTION = "aureon_forex"
DEFAULT_KEY_PATH = os.environ.get("AUREON_FIREBASE_KEY", r"C:\A02-PR\firebase_key.json")

# 19-col journal CSV header (schema is sacred -- matches journal._write_journal)
_CSV_DATE = "date_ist"

_client_cache = {"tried": False, "db": None}


def _client():
    """Lazily initialise Firestore from the service-account key. Returns the
    client, or None on any failure (cached, so we try exactly once per process
    and never spam the log)."""
    if _client_cache["tried"]:
        return _client_cache["db"]
    _client_cache["tried"] = True
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        key = DEFAULT_KEY_PATH
        if not os.path.exists(key):
            log.warning(f"firebase_journal: key not found at {key}; journal disabled")
            return None
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(key))
        _client_cache["db"] = firestore.client()
        log.info("firebase_journal: Firestore client ready")
    except Exception as e:
        log.warning(f"firebase_journal: init failed ({e!r}); journal disabled")
        _client_cache["db"] = None
    return _client_cache["db"]


def _iso(t):
    """Best-effort ISO-8601 string from a pandas Timestamp / datetime / str."""
    if t in (None, ""):
        return None
    try:
        iso = getattr(t, "isoformat", None)
        return iso() if callable(iso) else str(t)
    except Exception:
        return str(t)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def make_trade_record(ticket, side, lot, entry_price, exit_price, pnl,
                      sibling_ticket=None, role="normal", open_time=None,
                      close_time=None, held_min=None, exit_reason=None,
                      slip=None, max_favorable=None, nohold_trail_exit=None,
                      anchor=None):
    """Build one normalized trade dict (schema_version 2). Pure; never raises."""
    return {
        "ticket": _i(ticket),
        "sibling_ticket": _i(sibling_ticket),
        "anchor": anchor,
        "role": role or "normal",
        "side": side,
        "lot": _f(lot),
        "entry_price": _f(entry_price),
        "exit_price": _f(exit_price),
        "open_time": _iso(open_time),
        "close_time": _iso(close_time),
        "held_min": _f(held_min),
        "exit_reason": exit_reason,
        "slip": _f(slip),
        "max_favorable": _f(max_favorable),
        "nohold_trail_exit": (nohold_trail_exit or None),
        "pnl": _f(pnl),
    }


def build_anchor(label, anchor_price=None, trades=None):
    """Group a list of trade records under one anchor (schema_version 2)."""
    trades = trades or []
    return {
        "label": label,
        "anchor_price": _f(anchor_price),
        "n_trades": len(trades),
        "pnl": round(sum((t.get("pnl") or 0.0) for t in trades), 2),
        "trades": trades,
    }


def save_daily_journal(day, anchors=None, trades=None, total_pnl=None, meta=None):
    """Write ONE document aureon_forex/{day}. `anchors` is a list of build_anchor
    dicts; `trades` is an optional flat fallback list. Returns True on write,
    False otherwise. Fail-safe: swallows every error."""
    try:
        day_str = str(day)
        anchors = anchors or []
        trades = trades or [t for a in anchors for t in a.get("trades", [])]
        if total_pnl is None:
            total_pnl = sum((t.get("pnl") or 0.0) for t in trades)
        doc = {
            "schema_version": SCHEMA_VERSION,
            "day": day_str,
            "anchors": anchors,
            "n_trades": len(trades),
            "total_pnl": round(float(total_pnl), 2),
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        if meta:
            doc["meta"] = meta
        db = _client()
        if db is None:
            log.info(f"firebase_journal: would save {day_str} "
                     f"({len(trades)} trades, ${doc['total_pnl']:+.2f}) -- client unavailable")
            return False
        db.collection(COLLECTION).document(day_str).set(doc)
        log.info(f"firebase_journal: saved {day_str} "
                 f"({len(trades)} trades, ${doc['total_pnl']:+.2f}) -> {COLLECTION}")
        return True
    except Exception as e:
        log.warning(f"firebase_journal.save_daily_journal failed: {e!r}")
        return False


def day_exists(day):
    """True if a doc for `day` already exists. Fail-safe (False on any error)."""
    try:
        db = _client()
        if db is None:
            return False
        return db.collection(COLLECTION).document(str(day)).get().exists
    except Exception as e:
        log.warning(f"firebase_journal.day_exists failed: {e!r}")
        return False


def _days_from_csvs(journal_dir):
    """Parse the monthly trades_*.csv files under journal_dir into
    {day_str: [trade_record, ...]} using make_trade_record. Fail-safe."""
    days = {}
    prices = {}
    try:
        for path in sorted(glob.glob(os.path.join(journal_dir, "trades_*.csv"))):
            try:
                with open(path, newline="") as f:
                    for r in csv.DictReader(f):
                        day = r.get(_CSV_DATE)
                        if not day:
                            continue
                        rec = make_trade_record(
                            ticket=r.get("ticket"), side=r.get("side"), lot=r.get("lot"),
                            entry_price=r.get("entry_price"),
                            exit_price=r.get("actual_exit_price"),
                            pnl=r.get("realized_pnl_usd"), role=r.get("role"),
                            open_time=r.get("entry_time_ist"),
                            close_time=r.get("exit_time_ist"),
                            exit_reason=r.get("exit_reason"), slip=r.get("trail_slip"),
                            max_favorable=r.get("max_favorable"),
                            nohold_trail_exit=r.get("nohold_trail_exit"),
                            anchor=r.get("anchor"))
                        days.setdefault(day, []).append((r.get("anchor") or "?", rec))
                        prices.setdefault((day, r.get("anchor") or "?"), r.get("anchor_price"))
            except Exception as e:
                log.warning(f"firebase_journal: could not read {path}: {e!r}")
    except Exception as e:
        log.warning(f"firebase_journal._days_from_csvs failed: {e!r}")
    return days, prices


def weekly_reconcile(journal_dir, force=False):
    """Backfill any trading day whose EOD write was missed, by reconciling the
    monthly trades CSVs in `journal_dir` against Firestore. For each day not
    already present (unless force=True), build its anchors and save it. Returns
    the number of days written. Fail-safe -- never raises."""
    written = 0
    try:
        days, prices = _days_from_csvs(journal_dir)
        for day_str in sorted(days):
            try:
                if not force and day_exists(day_str):
                    continue
                grouped = {}
                for label, rec in days[day_str]:
                    grouped.setdefault(label, []).append(rec)
                anchors = [build_anchor(label, prices.get((day_str, label)), recs)
                           for label, recs in grouped.items()]
                if save_daily_journal(day_str, anchors=anchors,
                                      meta={"source": "weekly_reconcile"}):
                    written += 1
            except Exception as e:
                log.warning(f"firebase_journal.weekly_reconcile day {day_str}: {e!r}")
        if written:
            log.info(f"firebase_journal.weekly_reconcile backfilled {written} day(s)")
    except Exception as e:
        log.warning(f"firebase_journal.weekly_reconcile failed: {e!r}")
    return written
