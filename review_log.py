"""AUREON — structured session-review log.

A SECOND, decision-grade log alongside aureon.log: `logs/review_YYYY-MM-DD.log`,
one grep-free line per decision-grade event, human-readable top to bottom (a full
trading day fits in ~100-200 lines). aureon.log is untouched (full PTRACE detail
stays there for deep debugging).

Line format (key=value, readable AND parseable):
    HH:MM:SS TYPE     engine=... k=v k=v ...
e.g.
    14:03:11 FILL     engine=ANCHOR side=BUY lot=0.35 price=4028.77 tag=A1
    14:47:52 CLOSE    engine=ANCHOR side=BUY lot=0.35 price=4001.80 reason=LADDER_LOCK4 pnl=+140.00
    14:20:03 LOCK     engine=ANCHOR action=fallback intended=4001.80 landed=- level=LOCK4
    05:00:02 ANCHOR   engine=ROGUE price=3977.80 source=SCHEDULED label=ROGUE_S1

Daily rotation is automatic (the date is in the filename). The module is pure I/O +
a pure `summarize()`; both are unit-testable without MT5.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

log = logging.getLogger("AUREON")

# engines and close reasons the review vocabulary recognises
ENGINES = ("ANCHOR", "ROGUE", "RB", "RALLY", "FETCHER", "TEST")
CLOSE_REASONS = ("FILL", "SL", "TP", "EARLY_LOCK", "LADDER_BE", "LADDER_LOCK4",
                 "PEAK_TRAIL", "LOCK_FALLBACK_CLOSE", "RESEED")
LOCK_ACTIONS = ("armed", "floor_set", "modified", "rejected_retried", "fallback")


def _kv(**fields) -> str:
    """Render key=value pairs (skipping None), values compact and space-free."""
    out = []
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, float):
            v = f"{v:.2f}"
        out.append(f"{k}={v}")
    return " ".join(out)


class ReviewLogger:
    def __init__(self, log_dir: str = "logs", clock=None, date_fn=None):
        self.log_dir = log_dir
        # clock() -> 'HH:MM:SS' ; date_fn() -> 'YYYY-MM-DD' (both injectable for tests)
        self._clock = clock or self._default_clock
        self._date_fn = date_fn or self._default_date

    @staticmethod
    def _default_clock() -> str:
        import pandas as pd
        return pd.Timestamp.now(tz="UTC").strftime("%H:%M:%S")

    @staticmethod
    def _default_date() -> str:
        import pandas as pd
        return pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")

    def path(self, day: Optional[str] = None) -> str:
        return os.path.join(self.log_dir, f"review_{day or self._date_fn()}.log")

    def _emit(self, kind: str, kv: str) -> None:
        """Append exactly ONE line to today's review file. Never raises onto the
        trading path."""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            line = f"{self._clock()} {kind:<8} {kv}"
            with open(self.path(), "a") as f:
                f.write(line + "\n")
        except Exception as e:  # a review-log failure must never break trading
            log.warning(f"review_log: emit failed ({e!r})")

    # --- decision-grade events (one line each) ---------------------------------
    def fill(self, engine: str, side: str, lot: float, price: float, tag=None) -> None:
        self._emit("FILL", _kv(engine=engine, side=side, lot=lot, price=price, tag=tag))

    def close(self, engine: str, side: str, lot: float, price: float, reason: str,
              pnl: float, tag=None) -> None:
        self._emit("CLOSE", _kv(engine=engine, side=side, lot=lot, price=price,
                                reason=reason, pnl=_signed(pnl), tag=tag))

    def lock(self, engine: str, action: str, intended=None, landed=None,
             level=None) -> None:
        self._emit("LOCK", _kv(engine=engine, action=action,
                               intended=intended, landed=(landed if landed is not None else "-"),
                               level=level))

    def pending(self, engine: str, action: str, tag: str, level=None, price=None) -> None:
        # action: placed / cancelled / swept
        self._emit("PENDING", _kv(engine=engine, action=action, tag=tag,
                                  level=level, price=price))

    def anchor(self, engine: str, price: float, source: str, label=None, impl=None) -> None:
        self._emit("ANCHOR", _kv(engine=engine, price=price, source=source,
                                 label=label, impl=impl))

    def governor(self, engine: str, event: str, detail=None) -> None:
        # event: loss_stop / profit_lock / cap / halt / resume
        self._emit("GOV", _kv(engine=engine, event=event, detail=detail))

    def testrun(self, result: str, steps_passed: int, steps_total: int) -> None:
        self._emit("TEST", _kv(engine="TEST", result=result,
                               steps=f"{steps_passed}/{steps_total}"))


def _signed(v) -> str:
    try:
        return f"{float(v):+.2f}"
    except (TypeError, ValueError):
        return str(v)


# --- shared accessor --------------------------------------------------------------
_SHARED: Optional[ReviewLogger] = None


def get_review_logger(cfg=None) -> ReviewLogger:
    """Process-wide review logger. Engines call this and log one line per decision;
    a review-log failure never touches trading."""
    global _SHARED
    if _SHARED is None:
        d = getattr(cfg, "review_log_dir", "logs") if cfg is not None else "logs"
        _SHARED = ReviewLogger(log_dir=d)
    return _SHARED


# --- summary / EOD digest ---------------------------------------------------------
def parse_line(line: str) -> Optional[dict]:
    parts = line.strip().split()
    if len(parts) < 2:
        return None
    rec = {"_ts": parts[0], "_type": parts[1]}
    for tok in parts[2:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            rec[k] = v
    return rec


def summarize(lines: List[str]) -> dict:
    """Pure aggregation of a review file's lines into the EOD digest numbers."""
    fills = 0
    closes_by_reason: Dict[str, int] = {}
    net_by_engine: Dict[str, float] = {}
    locks = {"armed": 0, "fired": 0, "fallback": 0}
    rejects = 0
    anchors = 0
    for raw in lines:
        rec = parse_line(raw)
        if rec is None:
            continue
        t = rec["_type"]
        if t == "FILL":
            fills += 1
        elif t == "CLOSE":
            r = rec.get("reason", "?")
            closes_by_reason[r] = closes_by_reason.get(r, 0) + 1
            eng = rec.get("engine", "?")
            try:
                net_by_engine[eng] = net_by_engine.get(eng, 0.0) + float(rec.get("pnl", 0))
            except ValueError:
                pass
        elif t == "LOCK":
            a = rec.get("action")
            if a == "armed" or a == "floor_set":
                locks["armed"] += 1
            elif a == "modified":
                locks["fired"] += 1
            elif a == "fallback":
                locks["fallback"] += 1
            elif a == "rejected_retried":
                rejects += 1
        elif t == "ANCHOR":
            anchors += 1
    return {"fills": fills, "closes_by_reason": closes_by_reason,
            "net_by_engine": {k: round(v, 2) for k, v in net_by_engine.items()},
            "locks": locks, "rejects": rejects, "anchors": anchors,
            "net_total": round(sum(net_by_engine.values()), 2)}


def format_digest(summary: dict, day: str = "") -> str:
    cbr = ", ".join(f"{k} {v}" for k, v in sorted(summary["closes_by_reason"].items())) or "—"
    nbe = ", ".join(f"{k} {v:+.2f}" for k, v in sorted(summary["net_by_engine"].items())) or "—"
    lk = summary["locks"]
    return (f"📋 REVIEW {day}\n"
            f"fills: {summary['fills']} · anchors: {summary['anchors']}\n"
            f"closes: {cbr}\n"
            f"net by engine: {nbe}  (total {summary['net_total']:+.2f})\n"
            f"locks: armed {lk['armed']} / fired {lk['fired']} / fallback {lk['fallback']} · "
            f"rejects {summary['rejects']}")


def read_summary(log_dir: str, day: str) -> dict:
    path = os.path.join(log_dir, f"review_{day}.log")
    try:
        with open(path) as f:
            return summarize(f.readlines())
    except FileNotFoundError:
        return summarize([])


def post_review_digest(cfg, notifier, day: Optional[str] = None) -> str:
    """Build today's digest from the review file and post it (EOD / /review). Returns
    the digest text. Reads from the SAME directory the shared logger writes to (so the
    digest always matches the live file), falling back to cfg / 'logs'. Guarded."""
    d = _SHARED.log_dir if _SHARED is not None else (
        getattr(cfg, "review_log_dir", "logs") if cfg is not None else "logs")
    day = day or ReviewLogger(log_dir=d)._date_fn()
    summary = read_summary(d, day)
    text = format_digest(summary, day)
    if notifier is not None:
        try:
            import discord_cards as _dc
            from telemetry import Severity
            card = _dc.card_generic(f"📋 Daily Review {day}", "```\n" + text + "\n```",
                                    color=_dc.BLUE)
            notifier.send(f"📋 Daily review {day}", Severity.INFO, card=card)
        except Exception as e:
            log.warning(f"review_log: digest post failed ({e!r})")
    return text
