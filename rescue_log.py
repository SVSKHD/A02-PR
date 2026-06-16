"""AUREON — rescue FLEET-EVENT logger (v3.0.6). OBSERVER ONLY.

Every time the $10 fleet trigger fires (a leg hits -$10 with its twin still open
-> RESCUE leg + 2 BOOSTS), this records ONE complete event: trigger leg, rescue
leg, both boosts (ticket / fill / rc / ≤31-char comment), and -- once all members
close -- the fleet's net P&L and a branch label:

    CRASH_WIN     net > 0 and |net| >= SCRATCH_BAND  (directional follow-through)
    WHIPSAW_LOSS  net < 0 and |net| >= SCRATCH_BAND  (mean-revert)
    SCRATCH       |net| < SCRATCH_BAND

This is the crash-vs-whipsaw dataset that tells us whether the rescue fleet is
+EV. It NEVER alters rescue/boost trigger logic, sizing, or geometry -- every
entry point is wrapped by the caller so a logging error can't reach the engine.

Rows append to run/rescue_events.csv (local source of truth) and mirror to
Firestore aureon_forex/{date}/rescue_events/{event_id}.
"""
import csv
import logging
import os

import pandas as pd

from telemetry import Severity

log = logging.getLogger("AUREON")

SCRATCH_BAND = 50.0   # |net| under this is a SCRATCH (not crash, not whipsaw)

RESCUE_CSV_HEADER = [
    "event_id", "date_ist", "anchor", "sched_iso", "open_iso", "close_iso",
    "trigger_ticket", "trigger_side", "trigger_pnl_at_fire",
    "rescue_ticket", "rescue_side", "rescue_fill",
    "boost1_ticket", "boost1_fill", "boost1_rc", "boost1_comment",
    "boost2_ticket", "boost2_fill", "boost2_rc", "boost2_comment",
    "boosts_placed_ok", "net_usd", "branch",
]

BRANCHES = ("CRASH_WIN", "WHIPSAW_LOSS", "SCRATCH")


def _rescue_csv_path_for(run_dir):
    return os.path.join(run_dir, "rescue_events.csv")


def rescue_tally(csv_path):
    """{branch: count} read from the CSV (the persistent source of truth).
    Always returns all three keys; fail-safe (zeros on any error)."""
    tally = {b: 0 for b in BRANCHES}
    try:
        if not os.path.exists(csv_path):
            return tally
        with open(csv_path, newline="") as f:
            for r in csv.DictReader(f):
                b = (r.get("branch") or "").strip()
                if b in tally:
                    tally[b] += 1
    except Exception as e:
        log.warning(f"rescue_tally read failed: {e!r}")
    return tally


def _branch_for(net):
    if abs(net) < SCRATCH_BAND:
        return "SCRATCH"
    return "CRASH_WIN" if net > 0 else "WHIPSAW_LOSS"


def _rescue_event_open(self, ev):
    """Register an in-flight fleet event (called right after the boosts are placed).
    Tracks member tickets so each close can be attributed; finalizes when all
    members have closed. OBSERVER -- caller wraps this in try/except."""
    members = set(ev.get("members") or [])
    if not members:
        log.warning(f"rescue_event_open {ev.get('event_id')}: no member tickets — skipping")
        return
    ev["closed"] = {}
    ev["members"] = members
    self._rescue_events[ev["event_id"]] = ev
    for tk in members:
        self._rescue_event_by_ticket[int(tk)] = ev["event_id"]
    log.info(
        f"FLEET EVENT opened {ev['event_id']} ({ev.get('anchor')}): "
        f"members={sorted(members)} boosts_ok={ev.get('boosts_placed_ok')}")


def _rescue_event_on_close(self, ticket, pnl):
    """Attribute a closed ticket's realized P&L to its fleet event (if any) and
    finalize once every member has closed. OBSERVER -- caller wraps this."""
    eid = self._rescue_event_by_ticket.get(int(ticket))
    if eid is None:
        return
    ev = self._rescue_events.get(eid)
    if ev is None:
        return
    ev["closed"][int(ticket)] = float(pnl)
    self._rescue_event_by_ticket.pop(int(ticket), None)
    if set(ev["closed"].keys()) >= set(ev["members"]):
        self._rescue_event_finalize(ev)


def _rescue_event_finalize(self, ev):
    """All members closed -> compute net + branch, append the CSV row, mirror to
    Firestore, update the running tally, and post the FLEET EVENT telegram."""
    net = round(sum(ev["closed"].values()), 2)
    branch = _branch_for(net)
    boosts = ev.get("boosts") or []
    close_iso = pd.Timestamp.now(tz="UTC").isoformat()

    def _b(i, key):
        return boosts[i].get(key) if i < len(boosts) else None

    row = {
        "event_id": ev["event_id"], "date_ist": ev.get("date_ist"),
        "anchor": ev.get("anchor"), "sched_iso": ev.get("sched_iso"),
        "open_iso": ev.get("open_iso"), "close_iso": close_iso,
        "trigger_ticket": (ev.get("trigger") or {}).get("ticket"),
        "trigger_side": (ev.get("trigger") or {}).get("side"),
        "trigger_pnl_at_fire": (ev.get("trigger") or {}).get("trigger_pnl"),
        "rescue_ticket": (ev.get("rescue") or {}).get("ticket"),
        "rescue_side": (ev.get("rescue") or {}).get("side"),
        "rescue_fill": (ev.get("rescue") or {}).get("fill"),
        "boost1_ticket": _b(0, "ticket"), "boost1_fill": _b(0, "fill"),
        "boost1_rc": _b(0, "rc"), "boost1_comment": _b(0, "comment"),
        "boost2_ticket": _b(1, "ticket"), "boost2_fill": _b(1, "fill"),
        "boost2_rc": _b(1, "rc"), "boost2_comment": _b(1, "comment"),
        "boosts_placed_ok": bool(ev.get("boosts_placed_ok")),
        "net_usd": net, "branch": branch,
    }

    path = _rescue_csv_path_for(self.run_dir)
    try:
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=RESCUE_CSV_HEADER)
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.warning(f"rescue_events.csv append failed: {e!r}")

    # Mirror to Firestore (fail-safe; never blocks).
    try:
        import firebase_journal
        doc = dict(row)
        doc["boosts"] = boosts
        firebase_journal.save_rescue_event(ev.get("date_ist"), ev["event_id"], doc)
    except Exception as e:
        log.warning(f"rescue_event firebase mirror skipped: {e!r}")

    tally = rescue_tally(path)
    n_boost = len(boosts)
    n_ok = sum(1 for b in boosts if b.get("rc") == 10009)
    ok_mark = "✅" if ev.get("boosts_placed_ok") else "❌"
    sev = Severity.SUCCESS if net > 0 else Severity.WARN
    self.tele.send(
        f"📊 FLEET EVENT — {ev.get('anchor')}\n"
        f"boosts: {ok_mark} {n_ok}/{n_boost} @ rc=10009\n"
        f"branch: {branch}   net: ${net:+.0f}\n"
        f"running: crash {tally['CRASH_WIN']} · whipsaw {tally['WHIPSAW_LOSS']} "
        f"· scratch {tally['SCRATCH']}",
        sev,
    )
    log.info(f"FLEET EVENT finalized {ev['event_id']}: net ${net:+.2f} {branch}")
    self._rescue_events.pop(ev["event_id"], None)
    for tk in list(ev["members"]):
        self._rescue_event_by_ticket.pop(int(tk), None)


def run_rescuestats():
    """CLI (python bot.py rescuestats): print the running crash-vs-whipsaw tally +
    per-event table from rescue_events.csv. Read-only. Returns an exit code."""
    run_dir = os.environ.get("AUREON_RUN_DIR", "./run")
    path = _rescue_csv_path_for(run_dir)
    tally = rescue_tally(path)
    total = sum(tally.values())
    print(f"☄️  AUREON rescue fleet stats — {path}")
    print(f"running: crash {tally['CRASH_WIN']} · whipsaw {tally['WHIPSAW_LOSS']} "
          f"· scratch {tally['SCRATCH']}  (total {total} events)")
    if total == 0:
        print("(no fleet events recorded yet — empty until the first live rescue)")
        return 0
    if total:
        wins = tally["CRASH_WIN"]
        decisive = tally["CRASH_WIN"] + tally["WHIPSAW_LOSS"]
        if decisive:
            print(f"crash-win rate (excl. scratch): {100.0*wins/decisive:.0f}% "
                  f"({wins}/{decisive})")
    print("")
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        hdr = f"{'date':<11} {'anchor':<18} {'branch':<13} {'net':>9} {'boosts':>7}"
        print(hdr)
        print("-" * len(hdr))
        net_sum = 0.0
        for r in rows:
            try:
                net = float(r.get("net_usd") or 0.0)
            except ValueError:
                net = 0.0
            net_sum += net
            bo = "ok" if str(r.get("boosts_placed_ok")).lower() == "true" else "FAIL"
            print(f"{(r.get('date_ist') or '?'):<11} {(r.get('anchor') or '?'):<18} "
                  f"{(r.get('branch') or '?'):<13} {net:>+9.2f} {bo:>7}")
        print("-" * len(hdr))
        print(f"{'TOTAL':<11} {'':<18} {'':<13} {net_sum:>+9.2f}")
    except Exception as e:
        print(f"(could not read per-event table: {e!r})")
    return 0
