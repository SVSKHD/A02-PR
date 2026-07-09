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
    "event_id", "event_type", "date_ist", "anchor", "sched_iso", "open_iso", "close_iso",
    "trigger_ticket", "trigger_side", "trigger_pnl_at_fire",
    "rescue_ticket", "rescue_side", "rescue_fill",
    "boost1_ticket", "boost1_fill", "boost1_rc", "boost1_comment",
    "boost2_ticket", "boost2_fill", "boost2_rc", "boost2_comment",
    "boosts_placed_ok", "net_usd", "orig_pnl", "boost_pnl", "no_boost_net", "branch",
]

BRANCHES = ("CRASH_WIN", "WHIPSAW_LOSS", "SCRATCH")


def _rescue_csv_path_for(run_dir):
    return os.path.join(run_dir, "rescue_events.csv")


def ensure_rescue_events_csv(run_dir):
    """v3.2.1: create run/rescue_events.csv with its header at STARTUP if missing,
    so `rescuestats` always reads a valid (possibly empty) file and any path /
    permission problem surfaces loudly at startup instead of silently at the first
    finalize. (finalize also create-with-header on first write; this is belt-and-
    suspenders so the file exists even before the first lone/fleet event.)"""
    path = _rescue_csv_path_for(run_dir)
    try:
        if not os.path.exists(path):
            os.makedirs(run_dir, exist_ok=True)
            with open(path, "w", newline="", encoding='utf-8') as f:
                csv.DictWriter(f, fieldnames=RESCUE_CSV_HEADER).writeheader()
            log.info(f"rescue_events.csv created (header only) at {path}")
    except Exception as e:
        log.warning(f"could not create rescue_events.csv at startup: {e!r}")


def rescue_tally(csv_path):
    """{branch: count} read from the CSV (the persistent source of truth).
    Always returns all three keys; fail-safe (zeros on any error)."""
    tally = {b: 0 for b in BRANCHES}
    try:
        if not os.path.exists(csv_path):
            return tally
        with open(csv_path, newline="", encoding='utf-8') as f:
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
    # v3.1.7: tag the event TYPE so rescuestats can split lone-leg from twin-open
    # fleet events. A trigger ticket present == the original twin was still open
    # (FLEET); none == a LONE-leg rescue (the twin had already closed).
    if not ev.get("event_type"):
        ev["event_type"] = "FLEET" if (ev.get("trigger") or {}).get("ticket") \
            is not None else "LONE_RESCUE"
    self._rescue_events[ev["event_id"]] = ev
    for tk in members:
        self._rescue_event_by_ticket[int(tk)] = ev["event_id"]
    log.info(
        f"{ev['event_type']} EVENT opened {ev['event_id']} ({ev.get('anchor')}): "
        f"members={sorted(members)} boosts_ok={ev.get('boosts_placed_ok')}")
    # v3.1.7: persist immediately so a RESTART between open and member-close can
    # never orphan the event (the cause of the 2026-06-18 A1 lone rescue writing
    # nothing). _persist_rescue_events is best-effort; never raises into the engine.
    _persist = getattr(self, "_persist_rescue_events", None)
    if _persist:
        try:
            _persist()
        except Exception as e:
            log.warning(f"rescue_event persist (open) failed: {e!r}")


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

    # v3.1.4: NO-BOOST counterfactual — what the event would have netted with the
    # rescue/trigger legs ALONE (boost tickets excluded). Logged for every event
    # (fleet AND lone-leg) so rescuestats can answer "do the boosts help on LONE
    # legs specifically" separately. Pure observation; no engine change.
    _boost_tks = {int(b["ticket"]) for b in boosts if b.get("ticket") is not None}
    # v3.1.7 ISOLATION: original-leg (trigger + rescue) P&L and boost P&L are
    # SEPARATE fields, never pooled into one number. orig_pnl == the no-boost
    # counterfactual (what the originals netted alone); boost_pnl == net - orig.
    orig_pnl = round(sum(p for tk, p in ev["closed"].items()
                         if int(tk) not in _boost_tks), 2)
    boost_pnl = round(net - orig_pnl, 2)
    no_boost_net = orig_pnl
    ev["no_boost_net"] = no_boost_net
    event_type = ev.get("event_type") or (
        "FLEET" if (ev.get("trigger") or {}).get("ticket") is not None else "LONE_RESCUE")

    def _b(i, key):
        return boosts[i].get(key) if i < len(boosts) else None

    row = {
        "event_id": ev["event_id"], "event_type": event_type,
        "orig_pnl": orig_pnl, "boost_pnl": boost_pnl,
        "date_ist": ev.get("date_ist"),
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
        "net_usd": net, "orig_pnl": orig_pnl, "boost_pnl": boost_pnl,
        "no_boost_net": no_boost_net, "branch": branch,
    }

    path = _rescue_csv_path_for(self.run_dir)
    try:
        # R-8 self-heal: a rescue_events.csv created before a column was appended to
        # RESCUE_CSV_HEADER carries a stale narrower header; migrate it (rewrite header,
        # back up to .bak) BEFORE appending so the header always matches the rows. No-op
        # for a missing/current file. Guarded.
        try:
            import csv_schema
            csv_schema.ensure(path, RESCUE_CSV_HEADER)
        except Exception:
            pass
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding='utf-8') as f:
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
    # v3.1.0: rich fleet card (green/red/amber by branch), deduped by event_id.
    try:
        import discord_cards as _dc
        _legs = [(f"leg {tk}", pnl) for tk, pnl in (ev.get("closed") or {}).items()]
        _card = _dc.card_fleet(ev.get("anchor"), branch, _legs, net,
                               counterfactual=ev.get("no_boost_net"))
    except Exception:
        _card = None
    self.tele.send(
        f"📊 FLEET EVENT — {ev.get('anchor')}\n"
        f"boosts: {ok_mark} {n_ok}/{n_boost} @ rc=10009\n"
        f"branch: {branch}   net: ${net:+.0f}\n"
        f"running: crash {tally['CRASH_WIN']} · whipsaw {tally['WHIPSAW_LOSS']} "
        f"· scratch {tally['SCRATCH']}",
        sev, important=True, critical=True,
        card=_card, event_key=f"fleet:{ev.get('event_id')}",
    )
    log.info(f"{event_type} EVENT finalized {ev['event_id']}: net ${net:+.2f} "
             f"(orig ${orig_pnl:+.2f} / boost ${boost_pnl:+.2f}) {branch}")
    self._rescue_events.pop(ev["event_id"], None)
    for tk in list(ev["members"]):
        self._rescue_event_by_ticket.pop(int(tk), None)
    # v3.1.7: persist the cleanup so the finalized event isn't re-loaded on restart.
    _persist = getattr(self, "_persist_rescue_events", None)
    if _persist:
        try:
            _persist()
        except Exception as e:
            log.warning(f"rescue_event persist (finalize) failed: {e!r}")


def _persist_rescue_events(self):
    """v3.1.7: snapshot the in-flight rescue events into state and save, so a
    restart between a rescue OPEN and its members CLOSING cannot orphan the event
    (= write nothing). Members (set) and closed (int-keyed) are JSON-normalized."""
    ser = {}
    for eid, ev in self._rescue_events.items():
        e = dict(ev)
        e["members"] = sorted(int(t) for t in (ev.get("members") or []))
        e["closed"] = {str(int(t)): float(p) for t, p in (ev.get("closed") or {}).items()}
        ser[eid] = e
    self.state["rescue_events_extended"] = ser
    self.state["rescue_event_by_ticket"] = {
        str(int(tk)): eid for tk, eid in self._rescue_event_by_ticket.items()}
    self._save_state()


def _rehydrate_rescue_events(self):
    """v3.1.7: restore in-flight rescue events on startup so an event opened
    before a restart still finalizes (and WRITES) when its members close after."""
    try:
        ser = self.state.get("rescue_events_extended") or {}
        evs = {}
        for eid, e in ser.items():
            ev = dict(e)
            ev["members"] = {int(t) for t in (e.get("members") or [])}
            ev["closed"] = {int(t): float(p) for t, p in (e.get("closed") or {}).items()}
            evs[eid] = ev
        self._rescue_events = evs
        self._rescue_event_by_ticket = {
            int(tk): eid for tk, eid in (self.state.get("rescue_event_by_ticket") or {}).items()}
        if evs:
            log.info(f"rehydrated {len(evs)} in-flight rescue event(s) from state: "
                     f"{sorted(evs.keys())}")
    except Exception as e:
        log.warning(f"rescue_event rehydrate failed (starting fresh): {e!r}")
        self._rescue_events = {}
        self._rescue_event_by_ticket = {}


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
        with open(path, newline="", encoding='utf-8') as f:
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
