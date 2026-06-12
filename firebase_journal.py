"""
AUREON — Firebase end-of-day journal (NEW in v3.0.0).

Pushes a per-day trade journal to Cloud Firestore after the EOD flatten, and
reconciles the week from the monthly trade CSVs on a market-closed startup.

DESIGN CONTRACT — this module is FAIL-SAFE. Every public function is wrapped so
that a missing `firebase-admin` package, absent credentials, or a network error
is logged and swallowed. It must NEVER raise into the trading loop or block the
EOD flatten. The live_trader call sites are *also* wrapped in try/except as a
belt-and-braces second layer.

Credentials (first one found wins; all optional):
  AUREON_FIREBASE_KEY            path to a service-account JSON key
  GOOGLE_APPLICATION_CREDENTIALS path to a service-account JSON key
  AUREON_FIREBASE_PROJECT        explicit project id (optional)

If `firebase-admin` is not installed or no key is found, all functions become
no-ops (a single warning, then silence). The key file must NEVER be committed —
.gitignore covers firebase_key*.json.

NOTE: this file was specified as "already written; integrate as-is" but was not
present in the repo, so it was authored here to satisfy the v3.0.0 wiring. If a
canonical implementation exists, drop it in — the call sites only depend on the
function names below: make_trade_record, build_anchor, save_daily_journal,
weekly_reconcile.
"""

import csv
import datetime as _dt
import logging
import os

log = logging.getLogger("AUREON")

# Cached Firestore client (None = unavailable / not yet initialised).
_CLIENT = None
_INIT_TRIED = False
_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Firestore client (lazy, cached, fail-safe)
# ---------------------------------------------------------------------------
def _get_client():
    """Return a Firestore client or None. Never raises. Cached after first try."""
    global _CLIENT, _INIT_TRIED
    if _CLIENT is not None:
        return _CLIENT
    if _INIT_TRIED:
        return None
    _INIT_TRIED = True
    try:
        key_path = (os.environ.get("AUREON_FIREBASE_KEY")
                    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        if not key_path or not os.path.isfile(key_path):
            log.warning("firebase: no credentials found "
                        "(AUREON_FIREBASE_KEY / GOOGLE_APPLICATION_CREDENTIALS) "
                        "— journal upload disabled")
            return None
        import firebase_admin
        from firebase_admin import credentials, firestore
        if not firebase_admin._apps:
            cred = credentials.Certificate(key_path)
            opts = {}
            proj = os.environ.get("AUREON_FIREBASE_PROJECT")
            if proj:
                opts["projectId"] = proj
            firebase_admin.initialize_app(cred, opts or None)
        _CLIENT = firestore.client()
        log.info("firebase: Firestore client ready")
        return _CLIENT
    except Exception as e:
        log.warning(f"firebase: client init failed ({e}) — upload disabled")
        return None


# ---------------------------------------------------------------------------
# Record builders (PURE — no Firebase, never raise on bad input)
# ---------------------------------------------------------------------------
def _f(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _iso(day, hms):
    """Combine an ISO date string + 'HH:MM:SS' (IST) into an ISO-8601 string."""
    try:
        if not day or not hms:
            return None
        t = _dt.datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S")
        return t.replace(tzinfo=_IST).isoformat()
    except Exception:
        return None


def _held_min(entry_hms, exit_hms):
    """Approximate held minutes from two IST 'HH:MM:SS' clock strings."""
    try:
        if not entry_hms or not exit_hms:
            return None
        e = _dt.datetime.strptime(entry_hms, "%H:%M:%S")
        x = _dt.datetime.strptime(exit_hms, "%H:%M:%S")
        delta = (x - e).total_seconds() / 60.0
        if delta < 0:  # crossed midnight
            delta += 24 * 60.0
        return round(delta, 1)
    except Exception:
        return None


def make_trade_record(cfg, row, rich=None):
    """Build one per-trade record dict from a today_trades.csv row plus (optional)
    the rich monthly-journal row. Pure and tolerant — returns a dict, or None only
    if the input is unusable.

    today_trades row: date, anchor, side, entry, exit, outcome, pnl_usd, ticket
    rich (trades_YYYY-MM.csv): max_favorable, trail_slip, nohold_trail_exit, role,
        lot, entry_time_ist, exit_time_ist, exit_reason, anchor_price, ...
    """
    rich = rich or {}
    try:
        day = str(row.get("date") or rich.get("date_ist") or "").strip()
        entry_hms = rich.get("entry_time_ist") or ""
        exit_hms = rich.get("exit_time_ist") or ""
        rec = {
            "ticket": str(row.get("ticket", "")).strip(),
            "sibling_ticket": None,  # not persisted to the run-dir CSVs
            "role": (rich.get("role") or "normal").strip() or "normal",
            "anchor": (row.get("anchor") or rich.get("anchor") or "").strip(),
            "side": (row.get("side") or rich.get("side") or "").strip(),
            "lot": _f(rich.get("lot"), _f(getattr(cfg, "lot_size", None))),
            "entry_price": _f(row.get("entry"), _f(rich.get("entry_price"))),
            "exit_price": _f(row.get("exit"), _f(rich.get("actual_exit_price"))),
            "open_time": _iso(day, entry_hms),
            "close_time": _iso(day, exit_hms),
            "held_min": _held_min(entry_hms, exit_hms),
            "exit_reason": (rich.get("exit_reason") or row.get("outcome") or "").strip(),
            "slip": _f(rich.get("trail_slip")),
            "max_favorable": _f(rich.get("max_favorable")),
            "nohold_trail_exit": _f(rich.get("nohold_trail_exit")),
            "pnl": _f(row.get("pnl_usd"), 0.0),
        }
        return rec
    except Exception as e:
        log.warning(f"firebase: make_trade_record failed: {e}")
        return None


def build_anchor(anchor_label, records):
    """Aggregate a list of per-trade records into one anchor-level summary dict."""
    try:
        legs = [r for r in records if r and r.get("anchor") == anchor_label]
        pnl = sum((r.get("pnl") or 0.0) for r in legs)
        return {
            "anchor": anchor_label,
            "legs": len(legs),
            "pnl": round(pnl, 2),
            "roles": sorted({r.get("role", "normal") for r in legs}),
            "sides": sorted({r.get("side", "") for r in legs if r.get("side")}),
            "tickets": [r.get("ticket") for r in legs],
        }
    except Exception as e:
        log.warning(f"firebase: build_anchor failed: {e}")
        return {"anchor": anchor_label, "legs": 0, "pnl": 0.0}


# ---------------------------------------------------------------------------
# Public API (fail-safe)
# ---------------------------------------------------------------------------
def save_daily_journal(day, daily_pnl, trades, paper=False):
    """Write the end-of-day journal for `day` to Firestore. Returns True on a
    successful write, False if skipped/failed. Never raises."""
    try:
        records = [t for t in (trades or []) if t]
        anchors = sorted({r.get("anchor") for r in records if r.get("anchor")})
        doc = {
            "day": str(day),
            "daily_pnl": round(float(daily_pnl or 0.0), 2),
            "trade_count": len(records),
            "trades": records,
            "anchors": [build_anchor(a, records) for a in anchors],
            "mode": "paper" if paper else "live",
            "written_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "version": "3.0.0",
        }
        client = _get_client()
        if client is None:
            log.info(f"firebase: save_daily_journal skipped for {day} "
                     f"(no client) — {len(records)} trades, pnl=${doc['daily_pnl']:+.2f}")
            return False
        collection = "aureon_journal_paper" if paper else "aureon_journal"
        client.collection(collection).document(str(day)).set(doc)
        log.info(f"firebase: saved {collection}/{day} ({len(records)} trades)")
        return True
    except Exception as e:
        log.warning(f"firebase: save_daily_journal failed (non-fatal): {e}")
        return False


def _read_journal_rows(journal_dir):
    """Read every trades_YYYY-MM.csv row from journal_dir. Never raises."""
    rows = []
    try:
        if not journal_dir or not os.path.isdir(journal_dir):
            return rows
        for fn in sorted(os.listdir(journal_dir)):
            if not (fn.startswith("trades_") and fn.endswith(".csv")):
                continue
            try:
                with open(os.path.join(journal_dir, fn), newline="") as f:
                    rows.extend(list(csv.DictReader(f)))
            except Exception as e:
                log.warning(f"firebase: could not read {fn}: {e}")
    except Exception as e:
        log.warning(f"firebase: journal scan failed: {e}")
    return rows


def weekly_reconcile(journal_dir, paper=False):
    """On a market-closed (e.g. Sunday) startup, aggregate the trailing 7 days from
    the monthly trade CSVs and write a weekly summary doc. Fail-safe; never raises."""
    try:
        rows = _read_journal_rows(journal_dir)
        if not rows:
            log.info("firebase: weekly_reconcile — no journal rows found, skipping")
            return False
        today = _dt.date.today()
        cutoff = today - _dt.timedelta(days=7)
        recent = []
        for r in rows:
            try:
                d = _dt.datetime.strptime(str(r.get("date_ist", "")).strip(),
                                          "%Y-%m-%d").date()
            except Exception:
                continue
            if cutoff <= d <= today:
                recent.append(r)
        total = 0.0
        per_day = {}
        per_anchor = {}
        for r in recent:
            pnl = _f(r.get("realized_pnl_usd"), 0.0) or 0.0
            total += pnl
            per_day[r.get("date_ist", "")] = round(
                per_day.get(r.get("date_ist", ""), 0.0) + pnl, 2)
            a = r.get("anchor", "")
            per_anchor[a] = round(per_anchor.get(a, 0.0) + pnl, 2)
        doc = {
            "week_ending": str(today),
            "from": str(cutoff),
            "trade_count": len(recent),
            "total_pnl": round(total, 2),
            "per_day": per_day,
            "per_anchor": per_anchor,
            "mode": "paper" if paper else "live",
            "written_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "version": "3.0.0",
        }
        client = _get_client()
        if client is None:
            log.info(f"firebase: weekly_reconcile computed (no client) — "
                     f"{len(recent)} trades, total ${total:+.2f}")
            return False
        collection = "aureon_weekly_paper" if paper else "aureon_weekly"
        client.collection(collection).document(str(today)).set(doc)
        log.info(f"firebase: saved {collection}/{today} ({len(recent)} trades, "
                 f"total ${total:+.2f})")
        return True
    except Exception as e:
        log.warning(f"firebase: weekly_reconcile failed (non-fatal): {e}")
        return False
