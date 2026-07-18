"""AUREON — ROGUE monster-engine decision log (logs/rogue_YYYY-MM-DD.log).

One decision-grade line per event, review-log style (`HH:MM:SS KIND<pad> k=v ...`),
so `python bot.py review` can surface a Rogue section and a human can reconstruct
the whole day from the file alone: arm/disarm with numbers, bias changes,
re-anchors, fills, chains, SL modifies, closes with P/L, every guard trigger
(CAUTION on/off, FATIGUE, GIVEBACK, RED-DAY CARRY) and governor halt.

PURE + fully guarded: a logging error never reaches the trading path. Absolute
prices are formatted %.2f; P/L is signed %+.2f — same conventions as review_log.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("AUREON")

_DEFAULT_DIR = "logs"


def _path(day, log_dir=None):
    d = log_dir or _DEFAULT_DIR
    return os.path.join(d, f"rogue_{day}.log")


def _clock(now_utc):
    """now_utc: 'YYYY-MM-DD HH:MM:SS' (or ISO). Returns HH:MM:SS."""
    s = str(now_utc)
    # tolerate 'YYYY-MM-DD HH:MM:SS[...]' or ISO 'YYYY-MM-DDTHH:MM:SS'
    part = s.replace("T", " ").split(" ")
    return part[1][:8] if len(part) > 1 else s[:8]


def emit(day, now_utc, event, log_dir=None, **kv):
    """Append one decision-grade line. Never raises."""
    try:
        parts = []
        for k, v in kv.items():
            if isinstance(v, float):
                if k in ("pnl", "day_pnl", "peak_pnl"):
                    parts.append(f"{k}={v:+.2f}")
                else:
                    parts.append(f"{k}={v:.2f}")
            else:
                parts.append(f"{k}={v}")
        line = f"{_clock(now_utc)} {event:<9} " + " ".join(parts)
        d = log_dir or _DEFAULT_DIR
        os.makedirs(d, exist_ok=True)
        with open(_path(day, d), "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
        return line.rstrip()
    except Exception as e:  # pragma: no cover - guarded
        log.debug(f"rogue_monster_log.emit non-fatal: {e!r}")
        return ""


# ── typed helpers (the adapter calls these) ──────────────────────────────────
def boot(day, now_utc, *, anchor, armed, guards, config_hash, log_dir=None):
    return emit(day, now_utc, "BOOT", log_dir, impl="monster",
                anchor=anchor, state=armed, guards=guards, cfg=config_hash)


def anchor_seed(day, now_utc, *, price, source, log_dir=None):
    return emit(day, now_utc, "ANCHOR", log_dir, price=price, source=source)


def reanchor(day, now_utc, *, price, seq, log_dir=None):
    return emit(day, now_utc, "REANCHOR", log_dir, price=price, seq=seq)


def arm(day, now_utc, *, side, level, reason, log_dir=None):
    return emit(day, now_utc, "ARM", log_dir, side=side, level=level, reason=reason)


def disarm(day, now_utc, *, quiet_bars, log_dir=None):
    return emit(day, now_utc, "DISARM", log_dir, quiet_m5=quiet_bars)


def bias_change(day, now_utc, *, bias, log_dir=None):
    return emit(day, now_utc, "BIAS", log_dir, bias=bias)


def fill(day, now_utc, *, kind, side, price, sl, ticket=None, log_dir=None):
    return emit(day, now_utc, "FILL", log_dir, kind=kind, side=side,
                price=price, sl=sl, ticket=(ticket if ticket is not None else "-"))


def chain(day, now_utc, *, side, level, log_dir=None):
    return emit(day, now_utc, "CHAIN", log_dir, side=side, level=level)


def sl_modify(day, now_utc, *, ticket, new_sl, reason, log_dir=None):
    return emit(day, now_utc, "SLMOD", log_dir, ticket=ticket, new_sl=new_sl, reason=reason)


def close(day, now_utc, *, side, kind, price, pnl, reason, log_dir=None):
    return emit(day, now_utc, "CLOSE", log_dir, side=side, kind=kind,
                price=price, pnl=pnl, reason=reason)


def guard(day, now_utc, *, name, detail, log_dir=None):
    """name in {CAUTION_ON, CAUTION_OFF, FATIGUE, GIVEBACK, RED_DAY_CARRY, CANDLE}."""
    return emit(day, now_utc, "GUARD", log_dir, guard=name, detail=detail)


def governor(day, now_utc, *, name, day_pnl, log_dir=None):
    """name in {GOV-LOSS, GOV-LOCK, GOV-GIVEBACK, ENTRY-CAP}."""
    return emit(day, now_utc, "GOV", log_dir, halt=name, day_pnl=day_pnl)
