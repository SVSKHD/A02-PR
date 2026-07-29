"""AUREON — `test` (alias `--test`): READ-ONLY broker preflight.

  python bot.py test                 # runs on BOTH demo and real accounts
  python bot.py --test               # alias
  python bot.py test --daily-limit-pct 0.04 --lot 0.20

WHAT IT DOES (never places, modifies, or cancels ANYTHING):
  Every order-viability probe goes through `mt5.order_check()` ONLY. This module
  contains NO order_send / TRADE_ACTION_REMOVE / TRADE_ACTION_SLTP path and calls
  NONE of the adapter's placement helpers (place_stop_order / place_market_order /
  cancel_order / close_position / modify_position_sl). That "no-write" property is
  grep-provable (see tests/test_broker_preflight.py::test_no_order_send_reachable)
  and is the whole point: a preflight must be safe to run against a funded account.

  Sections printed as aligned console tables + one summary Discord card:
    1. ACCOUNT   — login/server/trade_mode(DEMO|REAL)/balance/equity/leverage/
                   margin-mode/currency/margin-free/trade_allowed/trade_expert.
    2. TERMINAL  — terminal trade_allowed (AutoTrading) + connection.
    3. SYMBOL    — digits/point/tick/contract/volume min-step-max/spread(30s)/
                   stops_level/freeze_level/filling modes/session state/swap.
    4. VIABILITY — order_check() for anchor BUY/SELL stop, RB pending, market
                   BUY/SELL, one SLTP shape. retcode+meaning, margin, margin level.
                   On 10030 (INVALID_FILL) each supported filling mode is re-checked
                   and the working one is recommended.
    5. TIMING    — broker offset, last-tick age, tick rate over the sample.
    6. LOT ADVISOR — per-lot risk table + RECOMMENDED lot/rescue.
    7. VERDICT   — READY / BLOCKED with the specific failing items.

  Exit code 0 = READY, nonzero = BLOCKED (so a watchdog/script can gate on it).
  Runs in well under 60s (a single ~30s spread/tick sample dominates).

The pure builders (`build_*`, `lot_advisor_row`, `decide_verdict`) take plain data
so every table + verdict is unit-testable offline behind a fake broker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

log = logging.getLogger("AUREON")

# The anchor engine's magic (its boost / rescue / F-B legs share it). Preflight probes
# are built with this magic so order_check() sees the exact orders the strategy sends.
ANCHORS_MAGIC = 20260522
# Rescue lot = anchor lot x this ratio, snapped to the symbol volume step (0.35 -> 0.45,
# which is exactly config.rescue_boost_v2_lot — the validated pairing).
RESCUE_RATIO = 1.29
ADVISOR_LOTS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]

# order_check() reports a would-be server retcode. A valid order returns 0 (checks
# passed) on most builds, DONE (10009) on some — treat both as "would be accepted".
CHECK_OK = (0, 10009)

# Verdict thresholds for the lot advisor (leg = one full anchor SL as a % of the firm
# daily limit; wf = worst floating stack as a % of the same limit).
LEG_GREEN_PCT = 30.0
LEG_AMBER_PCT = 50.0
WF_GREEN_PCT = 70.0
WF_AMBER_PCT = 100.0
MARGIN_LEVEL_FLOOR = 200.0


# --- retcode meanings (local copy; adapter's map is import-safe but we stay decoupled) --
_RETCODE_MEANING = {
    0: "OK (would be accepted)",
    10009: "DONE (would be accepted)",
    10004: "REQUOTE",
    10006: "REJECTED",
    10013: "INVALID request",
    10014: "INVALID volume",
    10015: "INVALID price",
    10016: "INVALID stops (too close / freeze)",
    10017: "trade DISABLED",
    10018: "market CLOSED",
    10019: "NO MONEY (insufficient margin)",
    10021: "no PRICES",
    10027: "AutoTrading DISABLED by client",
    10030: "INVALID filling mode",
}


def retcode_meaning(rc) -> str:
    if rc is None:
        return "no result"
    return _RETCODE_MEANING.get(int(rc), f"retcode {int(rc)}")


def check_ok(rc) -> bool:
    return rc is not None and int(rc) in CHECK_OK


# --- plain data snapshots (a fake broker builds these directly) --------------------
@dataclass
class AccountSnap:
    login: int = 0
    server: str = ""
    trade_mode: int = 0
    is_demo: bool = True
    balance: float = 0.0
    equity: float = 0.0
    leverage: int = 0
    margin_mode: int = 0
    margin_mode_str: str = ""
    currency: str = ""
    margin_free: float = 0.0
    trade_allowed: bool = True
    trade_expert: bool = True

    @property
    def mode_str(self) -> str:
        return "DEMO" if self.is_demo else "REAL"


@dataclass
class TerminalSnap:
    trade_allowed: bool = True
    connected: bool = True
    community_account: bool = False
    build: int = 0


@dataclass
class SymbolSnap:
    name: str = "XAUUSD"
    digits: int = 2
    point: float = 0.01
    tick_size: float = 0.01
    tick_value: float = 1.0
    contract_size: float = 100.0
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    stops_level: int = 0          # points
    freeze_level: int = 0         # points
    filling_mask: int = 0         # SYMBOL_FILLING_* bitmask
    trade_mode: int = 4           # SYMBOL_TRADE_MODE_*
    trade_mode_str: str = "FULL"
    swap_long: float = 0.0
    swap_short: float = 0.0

    @property
    def dollars_per_price_per_lot(self) -> float:
        """$ change for a 1.0 price move on 1.0 lot, from the REAL tick value/size."""
        ts = self.tick_size or self.point or 0.01
        return (self.tick_value or 0.0) / ts if ts else 0.0


@dataclass
class Tick:
    bid: float
    ask: float
    time_s: float = 0.0       # broker tick time, decoded to UTC epoch seconds
    server_now_s: float = 0.0  # "now" on the same clock, to age the tick


@dataclass
class CheckResult:
    retcode: Optional[int]
    margin: Optional[float] = None
    margin_free: Optional[float] = None
    margin_level: Optional[float] = None
    comment: str = ""


# --- filling-mode helpers ----------------------------------------------------------
# SYMBOL_FILLING_* bits (the symbol's allowed set) vs ORDER_FILLING_* order constants.
FILL_FOK_BIT = 1
FILL_IOC_BIT = 2


def supported_fillings(mask: int, order_consts: dict) -> List[tuple]:
    """Return [(name, order_filling_const)] the symbol permits, IOC first (the adapter
    default), then FOK, then RETURN as a universal fallback candidate."""
    out = []
    if mask & FILL_IOC_BIT:
        out.append(("IOC", order_consts.get("IOC", 1)))
    if mask & FILL_FOK_BIT:
        out.append(("FOK", order_consts.get("FOK", 0)))
    # RETURN is valid for pendings/exchange execution regardless of the bitmask —
    # always worth probing when IOC/FOK are refused.
    out.append(("RETURN", order_consts.get("RETURN", 2)))
    return out


# --- order-viability rows ----------------------------------------------------------
@dataclass
class ViabilityRow:
    label: str
    type_str: str
    price: float
    lot: float
    retcode: Optional[int]
    meaning: str
    accepted: bool
    margin: Optional[float]
    margin_level_after: Optional[float]
    filling_used: str = "IOC"
    filling_note: str = ""


def build_viability(broker, sym: SymbolSnap, tick: Tick, cfg) -> List[ViabilityRow]:
    """Probe every order the strategy would place, via order_check() ONLY. Nothing is
    sent. On 10030 each supported filling mode is re-checked and the working one named."""
    oc = broker.order_type_consts()
    ac = broker.action_consts()
    fc = broker.filling_consts()
    trig = float(getattr(cfg, "trigger_dist", 18.0))
    sl_d = float(getattr(cfg, "sl_dist", 18.0))
    tp_d = float(getattr(cfg, "tp_dist", 30.0))
    lot = float(getattr(cfg, "lot_size", 0.35))
    rb_lot = float(getattr(cfg, "rescue_boost_v2_lot", 0.45))
    rb_off = float(getattr(cfg, "rescue_boost_v2_offset_1", 15.0))
    sym_name = sym.name

    def _round(p):
        return round(p, sym.digits)

    ask, bid = float(tick.ask), float(tick.bid)
    specs = [
        # label, type_str, mt5 order type, action, price, lot, sl, tp
        ("anchor BUY stop", "BUY_STOP", oc["BUY_STOP"], ac["PENDING"],
         _round(ask + trig), lot, 0.0, 0.0),
        ("anchor SELL stop", "SELL_STOP", oc["SELL_STOP"], ac["PENDING"],
         _round(bid - trig), lot, 0.0, 0.0),
        # RB v2 mirrors short below a BUY parent's entry; probe at the rescue lot.
        ("RB pending", "SELL_STOP", oc["SELL_STOP"], ac["PENDING"],
         _round(bid - rb_off), rb_lot, 0.0, 0.0),
        ("market BUY", "BUY", oc["BUY"], ac["DEAL"], ask, lot, 0.0, 0.0),
        ("market SELL", "SELL", oc["SELL"], ac["DEAL"], bid, lot, 0.0, 0.0),
        # SLTP shape: a market order carrying SL+TP proves the protective levels clear
        # trade_stops_level (order_check can't validate a bare SLTP with no position).
        ("SLTP shape", "BUY+SL/TP", oc["BUY"], ac["DEAL"], ask, lot,
         _round(ask - sl_d), _round(ask + tp_d)),
    ]

    rows: List[ViabilityRow] = []
    for label, tstr, otype, action, price, vol, sl, tp in specs:
        req = {
            "action": action,
            "symbol": sym_name,
            "volume": vol,
            "type": otype,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": ANCHORS_MAGIC,
            "comment": "PREFLIGHT",
            "type_filling": fc.get("IOC", 1),
        }
        res = broker.check(req)
        rc = getattr(res, "retcode", None)
        used, note = "IOC", ""
        if rc == 10030:  # INVALID_FILL — try each supported mode, name the winner
            used, note = "-", "IOC rejected"
            for fname, fconst in supported_fillings(sym.filling_mask, fc):
                if fname == "IOC":
                    continue
                req2 = dict(req)
                req2["type_filling"] = fconst
                res2 = broker.check(req2)
                if check_ok(getattr(res2, "retcode", None)):
                    res, rc, used = res2, getattr(res2, "retcode", None), fname
                    note = f"IOC rejected -> use {fname}"
                    break
            else:
                note = "no supported filling accepted"
        rows.append(ViabilityRow(
            label=label, type_str=tstr, price=price, lot=vol, retcode=rc,
            meaning=retcode_meaning(rc), accepted=check_ok(rc),
            margin=getattr(res, "margin", None),
            margin_level_after=getattr(res, "margin_level", None),
            filling_used=used, filling_note=note))
    return rows


def recommended_filling(rows: List[ViabilityRow]) -> Optional[str]:
    """The filling mode a market probe actually accepted (for the 10030 advisory)."""
    for r in rows:
        if r.type_str in ("BUY", "SELL") and r.accepted:
            return r.filling_used
    return None


# --- spread / timing sampling ------------------------------------------------------
@dataclass
class SpreadSample:
    n: int = 0
    current_pts: float = 0.0
    avg_pts: float = 0.0
    max_pts: float = 0.0
    current_usd: float = 0.0
    avg_usd: float = 0.0
    max_usd: float = 0.0


@dataclass
class TimingInfo:
    offset_hours: Optional[float] = None
    last_tick_age_s: float = 0.0
    tick_rate_per_s: float = 0.0
    n_ticks: int = 0
    duration_s: float = 0.0


def sample_market(broker, sym: SymbolSnap, cfg, *, seconds: float, poll_s: float,
                  clock: Callable[[], float], sleeper: Callable[[float], None]):
    """Sample bid/ask for `seconds`, returning (SpreadSample, TimingInfo). Spread is
    reported in points AND dollars at the config lot; timing gives tick age + rate.
    clock/sleeper are injected so tests run instantly."""
    lot = float(getattr(cfg, "lot_size", 0.35))
    dpp = sym.dollars_per_price_per_lot
    point = sym.point or 0.01
    spreads_price: List[float] = []
    seen_times = set()
    last_tick_time = 0.0
    last_server_now = 0.0
    t0 = clock()
    while True:
        tk = broker.sample_tick()
        if tk is not None:
            sp = max(0.0, float(tk.ask) - float(tk.bid))
            spreads_price.append(sp)
            last_tick_time = float(tk.time_s)
            last_server_now = float(tk.server_now_s)
            seen_times.add(round(float(tk.time_s), 3))
        elapsed = clock() - t0
        if elapsed >= seconds:
            break
        sleeper(poll_s)
    dur = max(1e-9, clock() - t0)

    def _px_to_pts(p):
        return p / point if point else 0.0

    def _px_to_usd(p):
        return p * dpp * lot

    ss = SpreadSample(n=len(spreads_price))
    if spreads_price:
        cur = spreads_price[-1]
        mx = max(spreads_price)
        avg = sum(spreads_price) / len(spreads_price)
        ss.current_pts, ss.avg_pts, ss.max_pts = _px_to_pts(cur), _px_to_pts(avg), _px_to_pts(mx)
        ss.current_usd, ss.avg_usd, ss.max_usd = _px_to_usd(cur), _px_to_usd(avg), _px_to_usd(mx)

    n_unique = max(len(seen_times), 1)
    ti = TimingInfo(
        offset_hours=broker.offset_hours(),
        last_tick_age_s=max(0.0, last_server_now - last_tick_time),
        tick_rate_per_s=(n_unique / dur),
        n_ticks=len(seen_times),
        duration_s=dur)
    return ss, ti


# --- lot advisor -------------------------------------------------------------------
@dataclass
class LotAdvisorRow:
    lot: float
    rescue_lot: float
    config_rb_lot: float
    anchor_leg_sl: float
    worst_realized_day: float
    worst_floating: float
    stack_lots: float
    stack_margin: Optional[float]
    stack_margin_level: Optional[float]
    leg_pct: float
    wf_pct: float
    verdict: str


def round_to_step(x: float, step: float) -> float:
    if step <= 0:
        return round(x, 2)
    return round(round(x / step) * step, 2)


def lot_advisor_row(lot: float, *, cfg, sym: SymbolSnap, equity: float,
                    margin_per_lot: Optional[float], daily_limit: float) -> LotAdvisorRow:
    """PURE per-lot risk math (hand-computable — see the test fixture).

      anchor leg SL $      = sl_dist x lot x contract_size
      worst realized day   = one full leg SL (the day HALTS after it)
      worst floating stack = dpp x (lot*sl_dist + rescue_lot*rb_trail*max_boosts)
                             i.e. parent at -SL plus 2 RBs each at their trail-arm
                             adverse, valued from the REAL tick value (dpp)
      stack margin         = margin_per_lot x (lot + max_boosts*rescue_lot)
      leg_pct / wf_pct     = leg SL / worst floating as a % of the firm daily limit
    Verdict: GREEN if leg<=30% AND wf<=70% AND margin level>200%; AMBER if within the
    looser band; else RED."""
    contract = float(getattr(cfg, "contract_size", sym.contract_size or 100.0))
    sl_d = float(getattr(cfg, "sl_dist", 18.0))
    dpp = sym.dollars_per_price_per_lot or contract
    rb_trail = float(getattr(cfg, "rescue_boost_v2_trail_activation", 10.0))
    max_boosts = int(getattr(cfg, "rescue_boost_v2_max_boosts", 2))
    config_rb = float(getattr(cfg, "rescue_boost_v2_lot", 0.45))

    rescue_lot = round_to_step(lot * RESCUE_RATIO, sym.volume_step)
    anchor_leg_sl = sl_d * lot * contract
    worst_realized = anchor_leg_sl  # one full SL, then the -630 halt stops new risk
    worst_floating = dpp * (lot * sl_d + rescue_lot * rb_trail * max_boosts)

    stack_lots = round(lot + max_boosts * rescue_lot, 2)
    stack_margin = (margin_per_lot * stack_lots) if margin_per_lot else None
    if stack_margin and stack_margin > 0:
        stack_ml = equity / stack_margin * 100.0
    else:
        stack_ml = float("inf") if equity > 0 else None

    leg_pct = (anchor_leg_sl / daily_limit * 100.0) if daily_limit else float("inf")
    wf_pct = (worst_floating / daily_limit * 100.0) if daily_limit else float("inf")

    ml_ok = stack_ml is None or stack_ml > MARGIN_LEVEL_FLOOR
    if not ml_ok:
        verdict = "❌"
    elif leg_pct <= LEG_GREEN_PCT and wf_pct <= WF_GREEN_PCT:
        verdict = "✅"
    elif leg_pct <= LEG_AMBER_PCT and wf_pct <= WF_AMBER_PCT:
        verdict = "⚠️"
    else:
        verdict = "❌"

    return LotAdvisorRow(
        lot=lot, rescue_lot=rescue_lot, config_rb_lot=config_rb,
        anchor_leg_sl=anchor_leg_sl, worst_realized_day=worst_realized,
        worst_floating=worst_floating, stack_lots=stack_lots,
        stack_margin=stack_margin, stack_margin_level=stack_ml,
        leg_pct=leg_pct, wf_pct=wf_pct, verdict=verdict)


def build_lot_advisor(cfg, sym: SymbolSnap, equity: float,
                      margin_per_lot: Optional[float], daily_limit: float,
                      lots: Optional[List[float]] = None) -> List[LotAdvisorRow]:
    return [lot_advisor_row(l, cfg=cfg, sym=sym, equity=equity,
                            margin_per_lot=margin_per_lot, daily_limit=daily_limit)
            for l in (lots or ADVISOR_LOTS)]


def recommended_lot(rows: List[LotAdvisorRow]) -> Optional[LotAdvisorRow]:
    """The largest ✅ row (best size that still clears every gate)."""
    greens = [r for r in rows if r.verdict == "✅"]
    return max(greens, key=lambda r: r.lot) if greens else None


# --- verdict -----------------------------------------------------------------------
@dataclass
class Verdict:
    ready: bool
    blocked_items: List[str] = field(default_factory=list)


def decide_verdict(acct: AccountSnap, term: TerminalSnap, sym: SymbolSnap,
                   rows: List[ViabilityRow]) -> Verdict:
    """BLOCKED (nonzero exit) on any hard stop; otherwise READY."""
    items: List[str] = []
    if not acct.trade_expert:
        items.append("EA trading DISABLED (account.trade_expert=False) — the bot cannot trade")
    if not acct.trade_allowed:
        items.append("account.trade_allowed=False — trading not permitted on this account")
    if not term.trade_allowed:
        items.append("terminal AutoTrading is OFF (press the AutoTrading button)")
    if not term.connected:
        items.append("terminal is DISCONNECTED from the broker")
    if sym.trade_mode_str not in ("FULL", "LONGONLY", "SHORTONLY", "CLOSEONLY") and sym.trade_mode == 0:
        items.append(f"symbol {sym.name} trading is DISABLED (trade_mode={sym.trade_mode})")
    # Any anchor/market probe the broker would reject blocks the go decision.
    for r in rows:
        if r.label in ("anchor BUY stop", "anchor SELL stop", "market BUY", "market SELL") and not r.accepted:
            items.append(f"{r.label} would be REJECTED: {r.meaning} (rc={r.retcode})")
    return Verdict(ready=(not items), blocked_items=items)


# --- rendering ---------------------------------------------------------------------
def _fmt(x, nd=2, dash="-"):
    if x is None:
        return dash
    try:
        if x != x:  # NaN
            return dash
        if x == float("inf"):
            return "inf"
        return f"{x:,.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def render_account(a: AccountSnap) -> str:
    ea = "✅" if a.trade_expert else "❌ EA TRADING DISABLED"
    ta = "✅" if a.trade_allowed else "❌"
    L = [
        "── ACCOUNT ──────────────────────────────────────────────",
        f"  login          {a.login}   server {a.server}",
        f"  TRADE MODE     {a.mode_str}",
        f"  balance        {_fmt(a.balance)} {a.currency}    equity {_fmt(a.equity)} {a.currency}",
        f"  margin free    {_fmt(a.margin_free)} {a.currency}    leverage 1:{a.leverage}",
        f"  margin mode    {a.margin_mode_str}",
        f"  trade_allowed  {ta}     trade_expert {ea}",
    ]
    return "\n".join(L)


def render_terminal(t: TerminalSnap) -> str:
    at = "✅" if t.trade_allowed else "❌ AUTOTRADING OFF"
    cx = "✅ connected" if t.connected else "❌ DISCONNECTED"
    return "\n".join([
        "── TERMINAL ─────────────────────────────────────────────",
        f"  AutoTrading    {at}     connection {cx}",
    ])


def render_symbol(s: SymbolSnap, ss: SpreadSample) -> str:
    fills = []
    if s.filling_mask & FILL_FOK_BIT:
        fills.append("FOK")
    if s.filling_mask & FILL_IOC_BIT:
        fills.append("IOC")
    fills.append("RETURN")
    return "\n".join([
        f"── SYMBOL {s.name} ───────────────────────────────────────",
        f"  digits {s.digits}   point {_fmt(s.point, 5)}   tick_size {_fmt(s.tick_size, 5)}   tick_value {_fmt(s.tick_value, 4)}",
        f"  contract_size {_fmt(s.contract_size)}   volume min/step/max {s.volume_min}/{s.volume_step}/{s.volume_max}",
        f"  spread(30s)   cur {_fmt(ss.current_pts, 1)}pt ({_fmt(ss.current_usd)}$)  "
        f"avg {_fmt(ss.avg_pts, 1)}pt ({_fmt(ss.avg_usd)}$)  max {_fmt(ss.max_pts, 1)}pt ({_fmt(ss.max_usd)}$)",
        f"  stops_level {s.stops_level}pt   freeze_level {s.freeze_level}pt   filling [{', '.join(fills)}]",
        f"  session {s.trade_mode_str}   swap long/short {_fmt(s.swap_long)}/{_fmt(s.swap_short)}",
    ])


def render_viability(rows: List[ViabilityRow]) -> str:
    L = ["── ORDER VIABILITY (order_check only — nothing sent) ─────",
         f"  {'TYPE':<16}{'PRICE':>10}{'LOT':>7}{'RC':>7}  {'MARGIN':>10}{'ML%after':>11}  MEANING"]
    for r in rows:
        flag = "✅" if r.accepted else "❌"
        note = f"  [{r.filling_note}]" if r.filling_note else ""
        L.append(f"  {r.label:<16}{_fmt(r.price):>10}{r.lot:>7.2f}{('-' if r.retcode is None else r.retcode):>7}  "
                 f"{_fmt(r.margin):>10}{_fmt(r.margin_level_after, 1):>11}  {flag} {r.meaning}{note}")
    rf = recommended_filling(rows)
    if rf:
        L.append(f"  recommended filling mode: {rf}")
    return "\n".join(L)


def render_timing(ti: TimingInfo) -> str:
    off = "unknown" if ti.offset_hours is None else f"{ti.offset_hours:+.2f}h"
    return "\n".join([
        "── TIMING ───────────────────────────────────────────────",
        f"  broker offset {off}   last-tick age {_fmt(ti.last_tick_age_s, 1)}s   "
        f"tick rate {_fmt(ti.tick_rate_per_s, 2)}/s over {_fmt(ti.duration_s, 0)}s ({ti.n_ticks} ticks)",
    ])


def render_lot_advisor(rows: List[LotAdvisorRow], cfg, lot_override: Optional[float]) -> str:
    L = ["── LOT ADVISOR ──────────────────────────────────────────",
         f"  {'LOT':>5}{'RESCUE':>8}{'legSL$':>9}{'wDay$':>9}{'wFloat$':>10}"
         f"{'stkLots':>8}{'stkML%':>9}{'leg%':>7}{'wf%':>7}  V"]
    for r in rows:
        ml = "inf" if (r.stack_margin_level == float("inf")) else _fmt(r.stack_margin_level, 0)
        L.append(f"  {r.lot:>5.2f}{r.rescue_lot:>8.2f}{_fmt(r.anchor_leg_sl, 0):>9}"
                 f"{_fmt(r.worst_realized_day, 0):>9}{_fmt(r.worst_floating, 0):>10}"
                 f"{r.stack_lots:>8.2f}{ml:>9}{_fmt(r.leg_pct, 1):>7}{_fmt(r.wf_pct, 1):>7}  {r.verdict}")
    rec = recommended_lot(rows)
    if rec:
        L.append(f"  RECOMMENDED: lot {rec.lot:.2f} / rescue {rec.rescue_lot:.2f}")
    else:
        L.append("  RECOMMENDED: none — no lot clears every gate (reduce size / risk)")
    cfg_lot = float(getattr(cfg, "lot_size", 0.35))
    cfg_rb = float(getattr(cfg, "rescue_boost_v2_lot", 0.45))
    L.append(f"  config: lot {cfg_lot:.2f} / rb lot {cfg_rb:.2f}")
    if lot_override is not None and abs(lot_override - cfg_lot) > 1e-9:
        L.append(f"  ⚠️  --lot override ACTIVE: {lot_override:.2f} (config default is {cfg_lot:.2f})")
    return "\n".join(L)


def render_verdict(v: Verdict) -> str:
    if v.ready:
        head = "── VERDICT: ✅ READY ─────────────────────────────────────"
        body = "  all preflight checks passed."
    else:
        head = "── VERDICT: ❌ BLOCKED ───────────────────────────────────"
        body = "\n".join(f"  ✗ {it}" for it in v.blocked_items)
    tail = ("  NOTE: slippage is NOT measurable without fills — the spread above is the "
            "floor cost.\n        Compare live fills vs intended levels after the first trades.")
    return "\n".join([head, body, tail])


def render_report(acct, term, sym, ss, viability, timing, advisor, verdict, cfg,
                  lot_override) -> str:
    return "\n\n".join([
        render_account(acct),
        render_terminal(term),
        render_symbol(sym, ss),
        render_viability(viability),
        render_timing(timing),
        render_lot_advisor(advisor, cfg, lot_override),
        render_verdict(verdict),
    ])


# --- Discord summary card ----------------------------------------------------------
def _summary_card(acct, sym, ss, viability, advisor, verdict):
    try:
        import discord_cards as dc
        rec = recommended_lot(advisor)
        rec_line = (f"RECOMMENDED lot {rec.lot:.2f} / rescue {rec.rescue_lot:.2f}"
                    if rec else "RECOMMENDED: none")
        n_ok = sum(1 for r in viability if r.accepted)
        lines = [
            f"{acct.mode_str} · {acct.server} · {sym.name}",
            f"equity {_fmt(acct.equity)} {acct.currency} · lev 1:{acct.leverage}",
            f"spread avg {_fmt(ss.avg_pts, 1)}pt ({_fmt(ss.avg_usd)}$)",
            f"order_check {n_ok}/{len(viability)} accepted",
            rec_line,
        ]
        color = dc.GREEN if verdict.ready else dc.RED
        title = f"🩺 PREFLIGHT {'READY' if verdict.ready else 'BLOCKED'}"
        return dc.card_generic(title, "\n".join(lines), color=color)
    except Exception as e:
        log.warning(f"preflight: card build failed ({e!r})")
        return None


def _post_card(notifier, card):
    if notifier is None or card is None:
        return
    try:
        from telemetry import Severity
        notifier.send("🩺 broker preflight", Severity.INFO, card=card)
    except Exception as e:
        log.warning(f"preflight: card post failed ({e!r})")


# --- orchestration -----------------------------------------------------------------
def run_preflight(cfg, adapter=None, *, broker=None, daily_limit_pct=None,
                  lot_override=None, notifier=None, clock=None, sleeper=None,
                  sample_seconds: float = 30.0, poll_s: float = 0.4) -> int:
    """Entry point. Returns a process exit code: 0 = READY, 2 = BLOCKED, 5 = no broker.
    Injectable broker/clock/sleeper for offline tests (pass sample_seconds=0 to skip
    the live sample)."""
    import time as _time
    clock = clock or _time.monotonic
    sleeper = sleeper or _time.sleep
    notifier = notifier or getattr(cfg, "_tele", None)

    if broker is None:
        if adapter is None:
            print("preflight: no adapter/broker", flush=True)
            return 5
        broker = _AdapterBroker(adapter, cfg)

    acct = broker.snapshot_account()
    term = broker.snapshot_terminal()
    sym = broker.snapshot_symbol()
    ss, timing = sample_market(broker, sym, cfg, seconds=sample_seconds, poll_s=poll_s,
                               clock=clock, sleeper=sleeper)
    tick = broker.sample_tick()
    if tick is None:
        tick = Tick(bid=acct.balance and 0.0 or 0.0, ask=0.0)
    viability = build_viability(broker, sym, tick, cfg)

    # margin per lot from the market-BUY probe (order_check margin / probed lot)
    margin_per_lot = None
    for r in viability:
        if r.type_str == "BUY" and r.margin:
            margin_per_lot = r.margin / r.lot if r.lot else None
            break

    dlp = daily_limit_pct if daily_limit_pct is not None else float(getattr(cfg, "daily_loss_pct", 0.05))
    start_bal = float(getattr(cfg, "starting_balance", 50000.0))
    daily_limit = dlp * start_bal
    advisor = build_lot_advisor(cfg, sym, acct.equity, margin_per_lot, daily_limit)
    verdict = decide_verdict(acct, term, sym, viability)

    report = render_report(acct, term, sym, ss, viability, timing, advisor, verdict,
                           cfg, lot_override)
    print(report, flush=True)
    print(f"\n(daily limit basis: {dlp:.3%} of {_fmt(start_bal)} = {_fmt(daily_limit)} "
          f"{acct.currency})", flush=True)

    _post_card(notifier, _summary_card(acct, sym, ss, viability, advisor, verdict))
    return 0 if verdict.ready else 2


# --- live MT5 adapter broker (READ-ONLY + order_check) -----------------------------
class _AdapterBroker:
    """Wraps the AUREON MT5 adapter into the read-only preflight interface. The ONLY
    trade-server call it makes is `mt5.order_check()`. It NEVER sends, modifies, or
    cancels an order — see tests/test_broker_preflight.py::test_no_order_send_reachable."""

    def __init__(self, adapter, cfg):
        self.a = adapter
        self.mt5 = adapter.mt5
        self.cfg = cfg
        self.symbol = getattr(cfg, "symbol", "XAUUSD")

    # --- constants ----
    def order_type_consts(self):
        m = self.mt5
        return {"BUY": m.ORDER_TYPE_BUY, "SELL": m.ORDER_TYPE_SELL,
                "BUY_STOP": m.ORDER_TYPE_BUY_STOP, "SELL_STOP": m.ORDER_TYPE_SELL_STOP}

    def action_consts(self):
        m = self.mt5
        return {"DEAL": m.TRADE_ACTION_DEAL, "PENDING": m.TRADE_ACTION_PENDING}

    def filling_consts(self):
        m = self.mt5
        return {"FOK": getattr(m, "ORDER_FILLING_FOK", 0),
                "IOC": getattr(m, "ORDER_FILLING_IOC", 1),
                "RETURN": getattr(m, "ORDER_FILLING_RETURN", 2)}

    # --- snapshots ----
    def snapshot_account(self) -> AccountSnap:
        m = self.mt5
        a = m.account_info()
        demo = getattr(m, "ACCOUNT_TRADE_MODE_DEMO", 0)
        mm = int(getattr(a, "margin_mode", 0) or 0)
        mm_map = {getattr(m, "ACCOUNT_MARGIN_MODE_RETAIL_NETTING", 0): "RETAIL_NETTING",
                  getattr(m, "ACCOUNT_MARGIN_MODE_EXCHANGE", 1): "EXCHANGE",
                  getattr(m, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2): "RETAIL_HEDGING"}
        return AccountSnap(
            login=int(getattr(a, "login", 0) or 0),
            server=str(getattr(a, "server", "")),
            trade_mode=int(getattr(a, "trade_mode", 0) or 0),
            is_demo=(int(getattr(a, "trade_mode", 0) or 0) == demo),
            balance=float(getattr(a, "balance", 0.0) or 0.0),
            equity=float(getattr(a, "equity", 0.0) or 0.0),
            leverage=int(getattr(a, "leverage", 0) or 0),
            margin_mode=mm, margin_mode_str=mm_map.get(mm, str(mm)),
            currency=str(getattr(a, "currency", "")),
            margin_free=float(getattr(a, "margin_free", 0.0) or 0.0),
            trade_allowed=bool(getattr(a, "trade_allowed", True)),
            trade_expert=bool(getattr(a, "trade_expert", True)))

    def snapshot_terminal(self) -> TerminalSnap:
        t = self.mt5.terminal_info()
        return TerminalSnap(
            trade_allowed=bool(getattr(t, "trade_allowed", True)),
            connected=bool(getattr(t, "connected", True)),
            community_account=bool(getattr(t, "community_account", False)),
            build=int(getattr(t, "build", 0) or 0))

    def snapshot_symbol(self) -> SymbolSnap:
        m = self.mt5
        s = m.symbol_info(self.symbol)
        if (s is None or not getattr(s, "visible", False)):
            m.symbol_select(self.symbol, True)
            s = m.symbol_info(self.symbol)
        tm = int(getattr(s, "trade_mode", 4) or 0)
        tm_map = {getattr(m, "SYMBOL_TRADE_MODE_DISABLED", 0): "DISABLED",
                  getattr(m, "SYMBOL_TRADE_MODE_LONGONLY", 1): "LONGONLY",
                  getattr(m, "SYMBOL_TRADE_MODE_SHORTONLY", 2): "SHORTONLY",
                  getattr(m, "SYMBOL_TRADE_MODE_CLOSEONLY", 3): "CLOSEONLY",
                  getattr(m, "SYMBOL_TRADE_MODE_FULL", 4): "FULL"}
        return SymbolSnap(
            name=self.symbol,
            digits=int(getattr(s, "digits", 2) or 2),
            point=float(getattr(s, "point", 0.01) or 0.01),
            tick_size=float(getattr(s, "trade_tick_size", 0.0) or getattr(s, "point", 0.01)),
            tick_value=float(getattr(s, "trade_tick_value", 0.0) or 0.0),
            contract_size=float(getattr(s, "trade_contract_size", 100.0) or 100.0),
            volume_min=float(getattr(s, "volume_min", 0.01) or 0.01),
            volume_step=float(getattr(s, "volume_step", 0.01) or 0.01),
            volume_max=float(getattr(s, "volume_max", 100.0) or 100.0),
            stops_level=int(getattr(s, "trade_stops_level", 0) or 0),
            freeze_level=int(getattr(s, "trade_freeze_level", 0) or 0),
            filling_mask=int(getattr(s, "filling_mode", 0) or 0),
            trade_mode=tm, trade_mode_str=tm_map.get(tm, str(tm)),
            swap_long=float(getattr(s, "swap_long", 0.0) or 0.0),
            swap_short=float(getattr(s, "swap_short", 0.0) or 0.0))

    def sample_tick(self) -> Optional[Tick]:
        t = self.mt5.symbol_info_tick(self.symbol)
        if t is None:
            return None
        off = getattr(self.a, "tick_time_offset_hours", None)
        import time as _t
        tsec = float(getattr(t, "time", 0) or 0) - (float(off) * 3600.0 if off else 0.0)
        return Tick(bid=float(t.bid), ask=float(t.ask), time_s=tsec, server_now_s=_t.time())

    def offset_hours(self) -> Optional[float]:
        off = getattr(self.a, "tick_time_offset_hours", None)
        return float(off) if off is not None else None

    def check(self, request) -> CheckResult:
        res = self.mt5.order_check(request)
        if res is None:
            return CheckResult(retcode=None, comment="order_check returned None")
        return CheckResult(
            retcode=int(getattr(res, "retcode", -1)),
            margin=float(getattr(res, "margin", 0.0) or 0.0) or None,
            margin_free=float(getattr(res, "margin_free", 0.0) or 0.0) or None,
            margin_level=float(getattr(res, "margin_level", 0.0) or 0.0) or None,
            comment=str(getattr(res, "comment", "")))
