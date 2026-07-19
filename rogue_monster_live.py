"""AUREON — ROGUE monster engine LIVE adapter (magic 20260626).

Runs the parity-proven decision core (rogue_monster) against a live/paper MT5
broker. The core is a *simulator* (it fills its own orders); live, the BROKER
fills orders, so this module is a reconciliation state machine: it uses the
core's PURE decision helpers (gate_eval / arm_side / bias_of / entry_level /
init_sl / chain_level / trail_target / the guard predicates) to decide intent,
places/cancels/modifies ROGUE_MAGIC orders, and folds REAL broker fills/closes
back into its state.

Isolation rules (unchanged from the repo doctrine):
  * every order carries ROGUE_MAGIC (20260626) — never the anchor magic;
  * TF_ test orders are excluded from every magic-scoped enumeration + the P/L
    governor (exclude_test=True);
  * the open primary ticket is mirrored into trader._rogue['open'] so the shared
    flatten seams keep working; the monster's own flatten closes ALL its legs.

Anchor + adaptive-guard state persist via rogue_monster_state (PR #121 never-
re-snapshot). Fully guarded — never raises onto the live tick.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import rogue_monster as rm
import rogue_monster_state as rms

log = logging.getLogger("AUREON")

# imported lazily to avoid a hard cycle with rogue.py at module load
ROGUE_MAGIC = 20260626
ROGUE_LEG_TYPE = "rogue"
_GLYPH = "🗡️"

# broker order-type ints (MT5): 4=BUY_STOP, 5=SELL_STOP, 0=BUY, 1=SELL
_TYPE_BUY_STOP, _TYPE_SELL_STOP = 4, 5
_TYPE_BUY, _TYPE_SELL = 0, 1


# ── config mapping ───────────────────────────────────────────────────────────
def cfg_to_monster(cfg):
    """Build a MonsterCfg from the live Config's rogue_* keys (getattr-defaulted to
    the validated MonsterCfg defaults, so a missing key never crashes)."""
    d = rm.MonsterCfg()
    g = lambda k, dv: getattr(cfg, k, dv)
    d.lot = float(g("rogue_lot", g("lot_size", d.lot)))
    d.atr_mult = float(g("rogue_atr_mult", d.atr_mult))
    d.atr_period = int(g("rogue_atr_period", d.atr_period))
    d.vel_points = float(g("rogue_vel_points", d.vel_points))
    d.vel_minutes = int(g("rogue_vel_minutes", d.vel_minutes))
    d.box_bars = int(g("rogue_box_bars", d.box_bars))
    d.box_max_range = float(g("rogue_box_max_range", d.box_max_range))
    d.disarm_bars = int(g("rogue_disarm_bars", d.disarm_bars))
    d.edge_offset = float(g("rogue_edge_offset", d.edge_offset))
    d.fallback_trigger = float(g("rogue_fallback_trigger", d.fallback_trigger))
    d.sl_cap = float(g("rogue_sl_cap", d.sl_cap))
    d.chain_step = float(g("rogue_chain_step", d.chain_step))
    d.max_chains = int(g("rogue_max_chains", d.max_chains))
    d.trail_start = float(g("rogue_trail_start", d.trail_start))
    d.trail_gap = float(g("rogue_trail_gap", d.trail_gap))
    d.day_loss_halt = float(g("rogue_day_loss_halt", d.day_loss_halt))
    d.profit_lock = float(g("rogue_profit_lock", d.profit_lock))
    d.max_entries = int(g("rogue_max_entries", d.max_entries))
    d.consec_sl_limit = int(g("rogue_consec_sl_limit", d.consec_sl_limit))
    d.caution_cooldown_min = int(g("rogue_caution_cooldown_min", d.caution_cooldown_min))
    d.caution_atr_boost = float(g("rogue_caution_atr_boost", d.caution_atr_boost))
    d.day_profit_trail_start = float(g("rogue_day_profit_trail_start", d.day_profit_trail_start))
    d.day_profit_giveback = float(g("rogue_day_profit_giveback", d.day_profit_giveback))
    d.redday_atr_step = float(g("rogue_redday_atr_step", d.redday_atr_step))
    d.side_fatigue_sl = int(g("rogue_side_fatigue_sl", d.side_fatigue_sl))
    d.reanchor_cooldown_s = int(g("rogue_reanchor_cooldown_s", d.reanchor_cooldown_s))
    d.bias_m15_lookback = int(g("rogue_bias_m15_lookback", d.bias_m15_lookback))
    d.bias_h1_lookback = int(g("rogue_bias_h1_lookback", d.bias_h1_lookback))
    d.candle_confirm = bool(g("rogue_candle_confirm", d.candle_confirm))
    d.be_lock_arm = float(g("rogue_be_lock_arm", d.be_lock_arm))       # Fix A
    d.be_lock_floor = float(g("rogue_be_lock_floor", d.be_lock_floor))  # Fix A
    d.asia_start_hour = int(g("rogue_asia_start_hour", d.asia_start_hour))  # Fix C
    return d


# ── broker helpers ───────────────────────────────────────────────────────────
def _is_test(obj):
    return "TF_" in str(getattr(obj, "comment", "") or "")


def _bars(trader, mcfg, n=420):
    """Recent M1 OHLC as a UTC-indexed DataFrame (open/high/low/close)."""
    try:
        raw = trader.adapter.get_latest_m1(trader.cfg.symbol, n)
        if raw is None or len(raw) < mcfg.atr_period + 2:
            return None
        rows = [{"time": pd.Timestamp(b["time"], unit="s"),
                 "open": float(b["open"]), "high": float(b["high"]),
                 "low": float(b["low"]), "close": float(b["close"])} for b in raw]
        df = pd.DataFrame(rows).set_index("time")[["open", "high", "low", "close"]]
        # drop the still-forming last bar so decisions run on CLOSED bars only
        return df.iloc[:-1] if len(df) > 1 else df
    except Exception as e:
        log.debug(f"{_GLYPH} _bars non-fatal: {e!r}")
        return None


def _rogue_positions(trader):
    """Open ROGUE_MAGIC positions (TF_ excluded) -> {ticket: {side, entry, sl}}."""
    out = {}
    try:
        for p in (trader.adapter.mt5.positions_get(symbol=trader.cfg.symbol) or []):
            if int(getattr(p, "magic", -1)) != ROGUE_MAGIC or _is_test(p):
                continue
            side = "LONG" if int(p.type) == _TYPE_BUY else "SHORT"
            out[int(p.ticket)] = {"side": side, "entry": float(p.price_open),
                                  "sl": float(getattr(p, "sl", 0.0) or 0.0)}
    except Exception as e:
        log.debug(f"{_GLYPH} _rogue_positions non-fatal: {e!r}")
    return out


def _rogue_pendings(trader):
    """Resting ROGUE_MAGIC stop orders (TF_ excluded) -> {ticket: {side, price}}."""
    out = {}
    try:
        for o in (trader.adapter.mt5.orders_get(symbol=trader.cfg.symbol) or []):
            if int(getattr(o, "magic", -1)) != ROGUE_MAGIC or _is_test(o):
                continue
            side = "LONG" if int(o.type) == _TYPE_BUY_STOP else "SHORT"
            out[int(o.ticket)] = {"side": side, "price": float(o.price_open)}
    except Exception as e:
        log.debug(f"{_GLYPH} _rogue_pendings non-fatal: {e!r}")
    return out


def _day_pnl(trader):
    """Realized ROGUE day P/L from MT5 history, TF_ excluded symmetrically (#125)."""
    try:
        import pnl_source as _ps
        rng = _broker_day_range(trader)
        deals = trader.adapter.mt5.history_deals_get(*rng) or []
        return float(_ps.magic_day_net(deals, ROGUE_MAGIC, exclude_test=True))
    except Exception as e:
        log.debug(f"{_GLYPH} _day_pnl non-fatal: {e!r}")
        return 0.0


def _broker_day_range(trader):
    try:
        import rogue as _r
        return _r._broker_day_range(trader)
    except Exception:
        now = trader.adapter.server_time_utc()
        start = pd.Timestamp(now).normalize().to_pydatetime()
        return (start, pd.Timestamp(now).to_pydatetime())


# ── persisted monster state ──────────────────────────────────────────────────
def _new_monster_state():
    return {"day": None, "anchor": None, "anchor_day": None, "pend": None,
            "positions": {}, "seq_no": 0, "chains_in_seq": 0, "quiet_bars": 0,
            "last_m1_ts": None, "last_seq_close_t": None,
            "consec_sl": 0, "caution_until": None, "sl_by_side": {"LONG": 0, "SHORT": 0},
            "day_peak_pnl": 0.0, "extra_atr": 0.0, "halted": ""}


def _run_dir(trader):
    return getattr(trader, "run_dir", None) or "run"


def _persist(trader, m):
    try:
        rms.save(_run_dir(trader), m)
    except Exception:
        pass


def _load_same_day(trader, m, today):
    """On (re)start, restore anchor + adaptive state if the store is for TODAY —
    the 02:30 seed is NEVER re-snapshotted once stored (PR #121)."""
    try:
        stored = rms.load(_run_dir(trader))
        if stored and stored.get("day") == today:
            for k in ("anchor", "anchor_day", "seq_no", "consec_sl", "caution_until",
                      "sl_by_side", "day_peak_pnl", "extra_atr", "last_seq_close_t"):
                if k in stored:
                    m[k] = stored[k]
    except Exception:
        pass


# ── observability wrappers (guarded, optional) ───────────────────────────────
def _day(trader):
    try:
        return str(trader.state.get("last_broker_date", "")) or "0000-00-00"
    except Exception:
        return "0000-00-00"


def _now(trader):
    try:
        return str(trader.adapter.server_time_utc())
    except Exception:
        return _day(trader) + " 00:00:00"


def _lot(trader):
    return float(getattr(trader.cfg, "rogue_lot", getattr(trader.cfg, "lot_size", 0.35)))


def _log(trader, event, **kv):
    try:
        import rogue_monster_log as _l
        _l.emit(_day(trader), _now(trader), event, **kv)
    except Exception:
        pass


def _card(trader, which, **kw):
    try:
        import discord_cards as _dc
        fn = {"boot": _dc.card_monster_boot, "armed": _dc.card_monster_armed,
              "reanchor": _dc.card_monster_reanchor, "fill": _dc.card_monster_fill,
              "sequence": _dc.card_monster_sequence, "guard": _dc.card_monster_guard,
              "governor": _dc.card_monster_governor}.get(which)
        tele = getattr(trader, "tele", None)
        if fn is None or tele is None:
            return
        card = fn(**kw)
        try:
            tele.send(f"{_GLYPH} ROGUE {which}", getattr(tele, "INFO", 20), card=card)
        except TypeError:
            tele.send(f"{_GLYPH} ROGUE {which}", card=card)
    except Exception:
        pass


# ── order placement ──────────────────────────────────────────────────────────
def _place_stop(trader, side, price, sl, kind):
    """Place ONE ROGUE_MAGIC pending stop; return the ticket (int) or None."""
    try:
        broker_side = "BUY" if side == "LONG" else "SELL"
        tp = round(price + (200.0 if side == "LONG" else -200.0), 2)
        res = trader.adapter.place_stop_order(
            trader.cfg.symbol, broker_side, round(price, 2), _lot(trader),
            round(sl, 2), tp, comment=f"AUR_ROGUE_{kind[0]}",
            dry_run=bool(getattr(trader, "paper", False)), magic=ROGUE_MAGIC)
        rc = getattr(res, "retcode", None) if res is not None else None
        tk = getattr(res, "order", None) if res is not None else None
        if rc == 10009 and tk:
            return int(tk)
    except Exception as e:
        log.warning(f"{_GLYPH} place stop non-fatal: {e!r}")
    return None


def _closed_pnl(trader, ticket):
    try:
        import rogue as _r
        v = _r._rogue_close_pnl(trader, ticket)
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def _flatten_all(trader, m, reason):
    """Close EVERY open ROGUE position + cancel EVERY ROGUE pending."""
    for tk in list(_rogue_positions(trader).keys()):
        try:
            trader.adapter.close_position(int(tk), dry_run=bool(getattr(trader, "paper", False)))
        except Exception as e:
            log.warning(f"{_GLYPH} flatten close {tk} non-fatal: {e!r}")
    try:
        import rogue as _r
        _r.cancel_pendings(trader, reason=reason)
    except Exception:
        pass
    m["positions"] = {}
    m["pend"] = None
    try:
        trader._rogue["open"] = None
    except Exception:
        pass


# ── time helpers ─────────────────────────────────────────────────────────────
def _ts(v):
    try:
        return pd.Timestamp(v) if v is not None else None
    except Exception:
        return None


def _in_reanchor_cd(m, t, mcfg):
    last = _ts(m.get("last_seq_close_t"))
    return last is not None and (pd.Timestamp(t) - last).total_seconds() < mcfg.reanchor_cooldown_s


def _in_caution_cd(m, t):
    cu = _ts(m.get("caution_until"))
    return cu is not None and pd.Timestamp(t) < cu


# ── anchor (seed 02:30 server; never re-snapshot once stored today) ──────────
def _ensure_anchor(trader, m, m1, mcfg, today):
    if m.get("anchor_day") == today and m.get("anchor") is not None:
        return
    try:
        seed_t = pd.Timestamp(f"{today} {mcfg.anchor_hour:02d}:{mcfg.anchor_minute:02d}:00")
    except Exception:
        return
    todays = m1[m1.index.normalize() == pd.Timestamp(today)]
    if not len(todays):
        return
    at_seed = todays[todays.index >= seed_t]
    src = None
    if len(at_seed):
        anchor = float(at_seed.open.iloc[0]); src = "SCHEDULED"
    elif pd.Timestamp(m1.index[-1]) >= seed_t:
        # late start after 02:30 — capture from the earliest bar we have today
        anchor = float(todays.open.iloc[0]); src = "CAPTURE_LATE"
    else:
        return
    m["anchor"] = anchor
    m["anchor_day"] = today
    _log(trader, "ANCHOR", price=anchor, source=src)


# ── the reconciliation state machine ─────────────────────────────────────────
def _reconcile(trader, m, mcfg, positions, px, t):
    known = m["positions"]
    cur = {str(tk): v for tk, v in positions.items()}

    for tks, info in cur.items():
        if tks in known:
            continue
        side = info["side"]; entry = info["entry"]
        pend = m.get("pend")
        kind = pend["kind"] if (pend and pend.get("side") == side) else ("CHAIN" if known else "ENTRY")
        if kind == "ENTRY":
            m["seq_no"] += 1; m["chains_in_seq"] = 0; m["seq_pnl"] = 0.0
        else:
            m["chains_in_seq"] += 1
        known[tks] = {"side": side, "entry": entry, "peak": 0.0, "kind": kind,
                      "sl": info["sl"]}
        m["pend"] = None
        _log(trader, "FILL", kind=kind, side=side, price=entry, sl=info["sl"], ticket=int(tks))
        _card(trader, "fill", kind=kind, side=side, price=entry, sl=info["sl"], ticket=int(tks))
        if m["chains_in_seq"] < mcfg.max_chains:
            lvl = rm.chain_level(side, entry, mcfg)
            sl = rm.init_sl(side, lvl, mcfg)
            tk = _place_stop(trader, side, lvl, sl, "CHAIN")
            if tk:
                m["pend"] = {"ticket": tk, "side": side, "level": lvl, "sl": sl, "kind": "CHAIN"}
                _log(trader, "CHAIN", side=side, level=lvl)

    closed_any = False
    last_reason = None
    for tks in list(known.keys()):
        if tks in cur:
            continue
        pos = known.pop(tks)
        # Fix A: a stop-out with the BE lock engaged (peak < trail_start) is a
        # breakeven scratch, not a full SL.
        if pos["peak"] >= mcfg.trail_start:
            reason = "TRAIL"
        elif rm.be_engaged(pos["peak"], mcfg):
            reason = "BE"
        else:
            reason = "SL"
        _apply_close_guards(trader, m, mcfg, pos, reason, t)
        pnl = _closed_pnl(trader, int(tks))
        m["seq_pnl"] = float(m.get("seq_pnl", 0.0)) + pnl
        last_reason = reason
        _log(trader, "CLOSE", side=pos["side"], kind=pos["kind"], price=px, pnl=pnl, reason=reason)
        closed_any = True

    if closed_any and not known:
        # sequence summary card carries the exit reason (BE where applicable)
        _card(trader, "sequence", anchor=m.get("anchor"),
              entries=1, chains=int(m.get("chains_in_seq", 0)),
              pnl=round(float(m.get("seq_pnl", 0.0)), 2), exit_reason=last_reason or "SL")
        m["seq_pnl"] = 0.0
        # sequence done: cancel any dangling chain pending, roll the anchor, cooldown
        for tk in list(_rogue_pendings(trader).keys()):
            try:
                trader.adapter.cancel_order(int(tk), dry_run=bool(getattr(trader, "paper", False)))
            except Exception:
                pass
        m["pend"] = None
        m["last_seq_close_t"] = str(t)
        m["anchor"] = float(px)
        _log(trader, "REANCHOR", price=px, seq=m["seq_no"])
        _card(trader, "reanchor", anchor_price=px, seq=m["seq_no"])

    # mirror the primary (entry) ticket for the shared flatten seams
    try:
        if known:
            pk = sorted(known.keys(), key=lambda x: int(x))[0]
            pi = known[pk]
            trader._rogue["open"] = {"ticket": int(pk), "side": pi["side"], "entry": pi["entry"],
                                     "sl": pi["sl"], "peak": pi["entry"], "magic": ROGUE_MAGIC,
                                     "leg_type": ROGUE_LEG_TYPE}
        else:
            trader._rogue["open"] = None
    except Exception:
        pass


def _apply_close_guards(trader, m, mcfg, pos, reason, t):
    if reason == "SL":
        m["consec_sl"] += 1
        m["sl_by_side"][pos["side"]] = m["sl_by_side"].get(pos["side"], 0) + 1
        if m["consec_sl"] == mcfg.consec_sl_limit:
            m["caution_until"] = str(pd.Timestamp(t) + pd.Timedelta(minutes=mcfg.caution_cooldown_min))
            d = f"{m['consec_sl']} straight SLs, cooldown {mcfg.caution_cooldown_min}m, atr +{mcfg.caution_atr_boost}"
            _log(trader, "GUARD", guard="CAUTION_ON", detail=d)
            _card(trader, "guard", name="CAUTION_ON", detail=d)
    elif reason == "TRAIL":
        if m["consec_sl"] >= mcfg.consec_sl_limit:
            _log(trader, "GUARD", guard="CAUTION_OFF", detail="winner")
        m["consec_sl"] = 0
    # reason == "BE": neutral scratch (Fix A) — consec_sl / side-fatigue / caution
    # left untouched; a BE exit is neither a full SL nor a caution-resetting winner.


def _manage_trails(trader, m, mcfg, positions, px):
    known = m["positions"]
    for tks, info in positions.items():
        k = str(tks)
        if k not in known:
            continue
        p = known[k]
        fav = (px - p["entry"]) if p["side"] == "LONG" else (p["entry"] - px)
        p["peak"] = max(p["peak"], fav)
        if p["peak"] >= mcfg.trail_start:
            tr = rm.trail_target(p["side"], p["entry"], p["peak"], mcfg)
            better = (tr > p["sl"]) if p["side"] == "LONG" else (tr < p["sl"])
            if better:
                try:
                    trader.adapter.modify_position_sl(int(tks), round(tr, 2),
                                                      dry_run=bool(getattr(trader, "paper", False)))
                    _log(trader, "SLMOD", ticket=int(tks), new_sl=round(tr, 2), reason="TRAIL")
                    p["sl"] = tr
                except Exception as e:
                    log.warning(f"{_GLYPH} trail modify {tks} non-fatal: {e!r}")
        elif mcfg.be_lock_arm > 0 and p["peak"] >= mcfg.be_lock_arm:
            # Fix A: ratchet SL to breakeven+floor before the trail arms (same SL-modify
            # path as a trail move -> retry -> LOCK_FALLBACK_CLOSE). Ratchet only.
            be = rm.be_lock_target(p["side"], p["entry"], mcfg)
            better = (be > p["sl"]) if p["side"] == "LONG" else (be < p["sl"])
            if better:
                try:
                    trader.adapter.modify_position_sl(int(tks), round(be, 2),
                                                      dry_run=bool(getattr(trader, "paper", False)))
                    _log(trader, "BELOCK", ticket=int(tks), price=round(be, 2))  # "BE lock set @<price>"
                    p["sl"] = be
                    p["be_set"] = True
                except Exception as e:
                    log.warning(f"{_GLYPH} BE lock modify {tks} non-fatal: {e!r}")


def _maybe_arm(trader, m, mcfg, m1, t, px, positions, pendings, new_bar):
    if positions:
        return
    caution_on = rm.caution_active(m["consec_sl"], mcfg)
    eff = rm.effective_atr_mult(mcfg, m["extra_atr"], caution_on)
    m5 = rm.resample(m1, "5min"); m15 = rm.resample(m1, "15min"); h1 = rm.resample(m1, "1h")
    if len(m5) < mcfg.box_bars + 1 or m.get("anchor") is None:
        return
    atr = rm.atr(m5, mcfg.atr_period)
    m5_atr_last = float(atr.iloc[-1]) if len(atr) else float("nan")
    vel_win = m1[m1.index > pd.Timestamp(t) - pd.Timedelta(minutes=mcfg.vel_minutes)]
    gate_hit, box = rm.gate_eval(m5, m5_atr_last, vel_win, px, m["anchor"], eff, mcfg)

    if pendings:  # already resting an entry stop -> disarm on quiet bars
        if new_bar:
            if not gate_hit:
                m["quiet_bars"] += 1
                if m["quiet_bars"] >= mcfg.disarm_bars:
                    for tk in list(pendings):
                        try:
                            trader.adapter.cancel_order(int(tk), dry_run=bool(getattr(trader, "paper", False)))
                        except Exception:
                            pass
                    m["pend"] = None; m["quiet_bars"] = 0
                    _log(trader, "DISARM", quiet_m5=mcfg.disarm_bars)
            else:
                m["quiet_bars"] = 0
        return

    if not gate_hit or _in_reanchor_cd(m, t, mcfg) or _in_caution_cd(m, t):
        return
    b = rm.bias_of(m15, h1, t, mcfg)
    side = rm.arm_side(gate_hit, px, m1.close, len(m5), b)
    # Fix C: Asia block — suppress an otherwise-valid arm before the server start hour
    # (anchor seed + re-anchor unchanged; the gate math above still ran for the log).
    if side and mcfg.asia_start_hour > 0 and pd.Timestamp(t).hour < mcfg.asia_start_hour:
        _log(trader, "ASIA", detail="arm suppressed", side=side)
        return
    if side and rm.fatigue_blocks(m["sl_by_side"], side, b, mcfg):
        _log(trader, "GUARD", guard="FATIGUE", detail=f"{side} SLs {m['sl_by_side'][side]}")
        _card(trader, "guard", name="FATIGUE", detail=f"{side} needs real bias")
        return
    if side and caution_on and b == "BOTH":
        _log(trader, "GUARD", guard="CAUTION_BLOCK", detail=side)
        return
    if side and mcfg.candle_confirm:
        cc = rm.candle_context(m5, mcfg)
        if cc and cc != side:
            return
    if not side:
        return
    lvl = rm.entry_level(side, box, m["anchor"], mcfg)
    sl = rm.init_sl(side, lvl, mcfg)
    tk = _place_stop(trader, side, lvl, sl, "ENTRY")
    if tk:
        m["pend"] = {"ticket": tk, "side": side, "level": lvl, "sl": sl, "kind": "ENTRY"}
        m["quiet_bars"] = 0
        reason = f"{gate_hit} | bias {b}"
        _log(trader, "ARM", side=side, level=lvl, reason=reason)
        _card(trader, "armed", side=side, level=lvl, reason=reason, anchor=m["anchor"])


def _governor(trader, m, mcfg, day_pnl):
    m["day_peak_pnl"] = max(m.get("day_peak_pnl", 0.0), day_pnl)
    halt = ""
    if day_pnl <= mcfg.day_loss_halt:
        halt = "GOV-LOSS"
    elif mcfg.profit_lock and day_pnl >= mcfg.profit_lock:
        halt = "GOV-LOCK"
    elif rm.giveback_halt(m["day_peak_pnl"], day_pnl, mcfg):
        halt = "GOV-GIVEBACK"
    if halt:
        _flatten_all(trader, m, halt)
        m["halted"] = halt
        _log(trader, "GOV", halt=halt, day_pnl=day_pnl)
        _card(trader, "governor", name=halt, day_pnl=day_pnl)
        return True
    return False


def drive_monster(trader, st, allow_new_entries=True):
    """Per-tick monster driver. Guarded — never raises onto the live tick."""
    try:
        cfg = trader.cfg
        mcfg = cfg_to_monster(cfg)
        today = _day(trader)
        m = st.get("monster")
        if m is None or m.get("day") != today:
            prior_day = m.get("day") if m else None
            prior_final = float(m.get("day_peak_pnl", 0.0)) if m else 0.0
            new = _new_monster_state()
            new["day"] = today
            # red-day carry: tightened gate the day AFTER a losing day
            if m is not None and prior_day and _day_pnl_was_red(m):
                new["extra_atr"] = mcfg.redday_atr_step
                _log(trader, "GUARD", guard="RED_DAY_CARRY", detail=f"atr_mult +{mcfg.redday_atr_step}")
            m = new
            st["monster"] = m
            _load_same_day(trader, m, today)   # restore anchor/adaptive on same-day restart

        if m.get("halted"):
            return

        m1 = _bars(trader, mcfg)
        if m1 is None or len(m1) < mcfg.atr_period + 2:
            return
        t = m1.index[-1]
        px = float(m1.close.iloc[-1])
        new_bar = str(t) != m.get("last_m1_ts")

        _ensure_anchor(trader, m, m1, mcfg, today)

        positions = _rogue_positions(trader)
        pendings = _rogue_pendings(trader)
        day_pnl = _day_pnl(trader)

        if _governor(trader, m, mcfg, day_pnl):
            _persist(trader, m)
            return

        _reconcile(trader, m, mcfg, positions, px, t)
        _manage_trails(trader, m, mcfg, positions, px)
        if allow_new_entries:
            _maybe_arm(trader, m, mcfg, m1, t, px, positions, pendings, new_bar)

        if new_bar:
            m["last_m1_ts"] = str(t)
        m["_last_day_pnl"] = day_pnl
        _persist(trader, m)
    except Exception as e:
        log.warning(f"{_GLYPH} drive_monster non-fatal: {e!r}")


def _day_pnl_was_red(m):
    return float(m.get("_last_day_pnl", 0.0)) < 0.0


def boot_banner_impl():
    return "monster"
