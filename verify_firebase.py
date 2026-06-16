"""AUREON — Firebase backfill VERIFIER (v3.0.4).

Read-only by construction. Confirms every trading day in the local journal CSVs
(run/journal/trades_*.csv) has a matching Firestore doc in `aureon_forex`, lists
what IS present (date ID + one-line summary), and names MISSING days. It NEVER
auto-writes; a single day can be re-written ONLY on explicit request:

    python bot.py verifyfb                      # read-only report
    python bot.py verifyfb --backfill 2026-06-15   # re-write ONE day (idempotent)

Fail-safe: if Firestore is unreachable it WARNS and exits 0, so this tool can
never touch trading. All Firestore access is routed through the already-fail-safe
firebase_journal helpers. Telegram output inherits the v3.0.4 ts_header()
timestamp automatically (telemetry prepends it to every message).
"""
import logging
import os
from datetime import datetime

import firebase_journal
from telemetry import telemetry_from_env, Severity

log = logging.getLogger("AUREON")


def _journal_dir():
    """Same resolution LiveTrader uses: <AUREON_RUN_DIR or ./run>/journal."""
    return os.path.join(os.environ.get("AUREON_RUN_DIR", "./run"), "journal")


def _short(day_str):
    """'2026-06-13' -> 'Sat Jun-13'. Falls back to the raw id on any parse error."""
    try:
        d = datetime.strptime(str(day_str)[:10], "%Y-%m-%d")
        return f"{d.strftime('%a')} {d.strftime('%b')}-{d.day}"
    except Exception:
        return str(day_str)


def _doc_net_trades(doc):
    """(net, n_trades) from a Firestore doc, tolerant of partial/legacy docs."""
    net = doc.get("total_pnl")
    if net is None:
        net = sum((t.get("pnl") or 0.0)
                  for a in (doc.get("anchors") or []) for t in (a.get("trades") or []))
    n = doc.get("n_trades")
    if n is None:
        n = sum(len(a.get("trades") or []) for a in (doc.get("anchors") or []))
    return float(net or 0.0), int(n or 0)


def _close_balance(doc):
    """Per-day close balance is NOT part of the schema (the journal CSV / doc
    track P&L + trade count, not balance). Surface it only if some doc/meta
    carries it, else 'n/a' — never invented."""
    for k in ("close_balance", "balance"):
        if doc.get(k) is not None:
            return doc.get(k)
    meta = doc.get("meta") or {}
    for k in ("close_balance", "balance"):
        if meta.get(k) is not None:
            return meta.get(k)
    return None


def _list_present(db):
    """All docs in the collection -> {day_id: doc_dict}. Read-only stream."""
    present = {}
    for snap in db.collection(firebase_journal.COLLECTION).stream():
        present[snap.id] = snap.to_dict() or {}
    return present


def verify(journal_dir=None, tele=None):
    """Read-only reconcile. Returns (missing_days, present_docs) or (None, None)
    if Firestore is unreachable. Prints to console and (if `tele`) Telegram."""
    journal_dir = journal_dir or _journal_dir()
    db = firebase_journal._client()
    if db is None:
        msg = "☁️ FB VERIFY — Firestore unreachable (key/network); skipped, no action."
        print(msg)
        if tele:
            tele.warn(msg)
        return None, None

    present = _list_present(db)
    days, _prices = firebase_journal._days_from_csvs(journal_dir)
    csv_days = sorted(days.keys())

    # Console: every present doc with its one-line summary.
    print(f"☁️ FB VERIFY — collection '{firebase_journal.COLLECTION}', "
          f"{len(present)} doc(s) present:")
    for day_id in sorted(present):
        net, n = _doc_net_trades(present[day_id])
        bal = _close_balance(present[day_id])
        bal_txt = f"${float(bal):,.2f}" if isinstance(bal, (int, float)) else "n/a"
        print(f"  {day_id} ({_short(day_id)}): net ${net:+,.2f} · "
              f"{n} trades · bal {bal_txt}")

    missing = [d for d in csv_days if d not in present]

    # Per-CSV-day present/missing roll-up line (matches the spec format).
    roll = " · ".join(
        f"{_short(d)} ✓" if d not in missing else f"{_short(d)} ✗ MISSING"
        for d in csv_days) or "(no CSV trading days found)"

    if missing:
        lines = [
            "☁️ FB VERIFY",
            f"docs present: {roll}",
            f"MISSING: [{', '.join(missing)}]",
            f"→ run: verifyfb --backfill {missing[0]}",
        ]
    else:
        lines = [f"☁️ FB VERIFY — ✅ all days reconciled ({len(present)} docs)"]
    report = "\n".join(lines)
    print(report)
    if tele:
        (tele.warn if missing else tele.success)(report)
    return missing, present


def backfill_day(date, journal_dir=None, tele=None):
    """Re-write a SINGLE day's doc from the journal CSVs. Idempotent (clean
    overwrite via firebase_journal.save_daily_journal -> Firestore .set()).
    Returns True on write, False otherwise. Never raises into the caller."""
    day_str = str(date)
    journal_dir = journal_dir or _journal_dir()
    try:
        db = firebase_journal._client()
        if db is None:
            msg = f"☁️ FB BACKFILL — Firestore unreachable; {day_str} NOT written."
            print(msg)
            if tele:
                tele.warn(msg)
            return False
        days, prices = firebase_journal._days_from_csvs(journal_dir)
        if day_str not in days:
            msg = (f"☁️ FB BACKFILL — no trades in journal CSV for {day_str}; "
                   f"nothing to write (check the date / journal dir).")
            print(msg)
            if tele:
                tele.warn(msg)
            return False
        grouped = {}
        for label, rec in days[day_str]:
            grouped.setdefault(label, []).append(rec)
        anchors = [firebase_journal.build_anchor(label, prices.get((day_str, label)), recs)
                   for label, recs in grouped.items()]
        total = round(sum((t.get("pnl") or 0.0)
                          for recs in grouped.values() for t in recs), 2)
        n_trades = sum(len(recs) for recs in grouped.values())
        ok = firebase_journal.save_daily_journal(
            day_str, anchors=anchors, total_pnl=total,
            meta={"source": "verifyfb_backfill"})
        if ok:
            msg = (f"☁️ FB BACKFILL — wrote {day_str} "
                   f"(net ${total:+,.2f}, {n_trades} trades, bal n/a) ✓")
            print(msg)
            if tele:
                tele.success(msg)
            return True
        msg = f"☁️ FB BACKFILL — write FAILED for {day_str} (see log)."
        print(msg)
        if tele:
            tele.error(msg)
        return False
    except Exception as e:
        log.warning(f"verify_firebase.backfill_day failed: {e!r}")
        print(f"☁️ FB BACKFILL — error for {day_str}: {e!r}")
        return False


def run_verifyfb(backfill=None):
    """CLI entry (python bot.py verifyfb [--backfill DATE]). Returns an exit code:
    0 = clean / unreachable (fail-safe) / backfill ok; 1 = missing days found or
    backfill failed. Read-only unless --backfill is passed."""
    tele = telemetry_from_env(component="AUREON-verifyfb")
    try:
        if backfill:
            ok = backfill_day(backfill, tele=tele)
            return 0 if ok else 1
        missing, present = verify(tele=tele)
        if missing is None:        # Firestore unreachable -> fail-safe exit 0
            return 0
        return 1 if missing else 0
    finally:
        try:
            tele.stop(timeout=6.0)
        except Exception:
            pass
