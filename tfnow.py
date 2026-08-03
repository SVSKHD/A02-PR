"""AUREON — tfnow: the ZERO-GATE, SELF-CONTAINED TF_ straddle placer.

A deliberately ISOLATED path for `/testfire now` and `python bot.py tfnow`. It shares
NOTHING with the scheduled-anchor decision pipeline: it does not call, and is not called
by, _process_anchor_if_due / _place_orders_for_anchor / _complete_testfire_anchor /
_place_completed_anchor / arm_testfire_inproc / any preflight / any daily-stop /
kill-switch / pause / engine-switch / market-open probe / settle wait / tick-loop wait.
There is NO deferral: it reads a tick, computes a straddle, calls the adapter twice, and
posts ONE card with exactly what the broker returned.

The ONLY things that stop it are BROKER REALITIES (no tick, disconnect, market closed,
insufficient margin, invalid volume, any non-10009 retcode). Those are REPORTED verbatim
on the relevant row, never gated.

`fire()` takes an adapter, a cfg (read for symbol / sl_dist / tp_dist / lot_size only) and
an optional telemetry sink. Both entry points (the Discord command handler in the bot
process, and the standalone `bot.py tfnow` CLI) call this same function synchronously.

tag_comment is imported from stale_leg_sweep purely as the string helper that appends the
"A:<price>" origin tag (so the comment is byte-identical to what rescue_boost's parent
adoption + the TF_ P&L exclusion expect); no sweep logic runs.
"""
import logging
import time

from stale_leg_sweep import tag_comment  # pure "<base> A:<price>" string builder

log = logging.getLogger("AUREON")


def _tf_id():
    """TF_<HHMMSS> identity for one straddle (UTC wall clock; no pandas/tick-loop dep)."""
    return "TF_" + time.strftime("%H%M%S", time.gmtime())


def _rc_msg(res):
    """(retcode, broker_message) from an adapter result — tolerant of None / dict / obj.
    Never a '?': a missing retcode is reported as None with an explicit message."""
    if res is None:
        return None, "order_send returned None (no broker response)"
    if isinstance(res, dict):                       # paper/dry-run shim
        return (10009 if res.get("paper") else None), str(res.get("comment", "paper"))
    rc = getattr(res, "retcode", None)
    msg = getattr(res, "comment", "") or ""
    return rc, str(msg)


def fire(adapter, cfg, tele=None, dist=3.0, lot=None):
    """Place a non-OCO TF_ straddle at the CURRENT mid, SYNCHRONOUSLY, with ZERO gates.

    Returns {tf, mid, dist, lot, rows, card} (rows: one dict per side with rc + msg), or
    {tf, error, rows: []} if there was no tick. Never raises."""
    symbol = getattr(cfg, "symbol", "XAUUSD")
    sl_dist = float(getattr(cfg, "sl_dist", 18.0))
    tp_dist = float(getattr(cfg, "tp_dist", 30.0))
    try:
        d = float(dist)
    except (TypeError, ValueError):
        d = 3.0
    try:
        lot_v = float(lot) if lot is not None and str(lot).strip() != "" else float(
            getattr(cfg, "lot_size", 0.53))
    except (TypeError, ValueError):
        lot_v = float(getattr(cfg, "lot_size", 0.53))
    tf = _tf_id()

    # --- broker reality: the tick. REPORTED, not gated. ---
    try:
        tick = adapter.mt5.symbol_info_tick(symbol)
    except Exception as e:
        body = f"🧪🔥 *{tf}* — NO TICK ({e!r}); nothing placed."
        _post(tele, body)
        log.warning(f"TFNOW {tf}: tick read raised {e!r}")
        return {"tf": tf, "error": f"tick_raised:{e!r}", "rows": [], "card": body}
    if tick is None:
        body = f"🧪🔥 *{tf}* — NO TICK (symbol_info_tick returned None); nothing placed."
        _post(tele, body)
        log.warning(f"TFNOW {tf}: no tick")
        return {"tf": tf, "error": "no_tick", "rows": [], "card": body}

    mid = (float(tick.bid) + float(tick.ask)) / 2.0
    rows = []
    for side, level in (("BUY", round(mid + d, 2)), ("SELL", round(mid - d, 2))):
        sl = round(level - sl_dist, 2) if side == "BUY" else round(level + sl_dist, 2)
        tp = round(level + tp_dist, 2) if side == "BUY" else round(level - tp_dist, 2)
        comment = tag_comment(f"{tf}_{side[0]}", level)   # -> "TF_HHMMSS_B A:<level>"
        try:
            res = adapter.place_stop_order(symbol, side, level, lot_v,
                                           sl=sl, tp=tp, comment=comment)
            rc, msg = _rc_msg(res)
        except Exception as e:
            rc, msg = None, f"place_stop_order raised: {e!r}"
        rows.append({"side": side, "level": level, "sl": sl, "tp": tp, "rc": rc, "msg": msg})
        log.warning(f"TFNOW {tf} {side} @ {level} SL {sl} TP {tp} lot {lot_v} -> "
                    f"rc {rc} {msg}")

    body = _render(tf, mid, d, lot_v, rows)
    _post(tele, body)
    return {"tf": tf, "mid": mid, "dist": d, "lot": lot_v, "rows": rows, "card": body}


def _render(tf, mid, d, lot_v, rows):
    """The ONE card. Every field is filled — never a '?'."""
    lines = [f"🧪🔥 {tf}  mid {mid:.2f}  dist {d:g}  lot {lot_v:g}"]
    for r in rows:
        rc = "None" if r["rc"] is None else str(r["rc"])
        msg = r["msg"] if str(r["msg"]).strip() != "" else "(no broker message)"
        lines.append(
            f"{r['side']:<4} stop @ {r['level']:.2f}  SL {r['sl']:.2f}  "
            f"TP {r['tp']:.2f}  rc {rc} {msg}")
    return "```\n" + "\n".join(lines) + "\n```"


def _post(tele, body):
    """Best-effort single post. Never raises."""
    if tele is None:
        print(body, flush=True)
        return
    try:
        tele.warn(body)
    except Exception:
        try:
            print(body, flush=True)
        except Exception:
            pass
