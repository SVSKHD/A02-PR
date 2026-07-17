"""AUREON — automatic stale-leg cancellation for the non-OCO straddle.

WHY THIS EXISTS
---------------
The bot trades a NON-OCO straddle: at each anchor it rests a BUY stop and a SELL
stop. When one leg fills and price runs on to the next anchor, the unfilled
OPPOSITE leg from the OLD anchor stays pending. Those stale legs later fill on a
pullback and open unwanted scratch trades. This module cancels every pending
order left behind by a prior anchor BEFORE the new anchor's straddle is placed,
with ONE hard exemption: a pending order that is the rescue leg (the INTERVAL-
point opposite leg) of a currently OPEN position is never swept.

RESTART SAFETY
--------------
Origin anchors are stamped into the MT5 order `comment` as ``A:<anchor_price>``
(e.g. ``A:4028.77``) at placement time, so the anchor registry survives a bot
restart: :func:`build_registry` reconstructs ticket -> origin-anchor purely from
``mt5.orders_get()`` comments, no state file required.

The pure decision helpers (:func:`is_stale`, :func:`detect_anchor_event`,
:func:`is_rescue_leg`, :func:`build_registry`) are import-light and side-effect
free so the unit tests can exercise them directly with mock MT5 objects; the one
side-effecting entrypoint is :func:`sweep_stale_legs`, which reads
``orders_get`` / ``positions_get`` and issues ``TRADE_ACTION_REMOVE`` sends.
"""
import logging
import re
from typing import Dict, List, Optional

log = logging.getLogger("AUREON")

# --- feature constants (the straddle geometry this sweep is built around) ---------
INTERVAL = 20.0                 # anchor spacing / stale threshold (points)
SYMBOL = "XAUUSD"
ANCHOR_TAG_RE = re.compile(r"A:(-?\d+(?:\.\d+)?)")   # parses "A:4028.77" anywhere
_TRADE_RETCODE_DONE = 10009     # TRADE_RETCODE_DONE fallback
_RESCUE_LEG_TOL = 0.75          # tolerance ($) when matching the INTERVAL-point leg
SWEEP_REASON = "stale_leg_sweep"
# The Aureon straddle/anchor magic (mt5_adapter.place_stop_order hardcodes 20260522).
# The sweep only ever cancels orders bearing THIS magic, so it can never touch the
# Rogue T2 bot (20260815), the rogue rider (20260626), the fetcher, or a manual trade.
STRADDLE_MAGIC = 20260522


# --- comment tagging (restart-safe origin-anchor stamp) ---------------------------
def anchor_comment(anchor_price: float) -> str:
    """The origin-anchor tag stamped into an order comment, e.g. ``A:4028.77``.
    Parsed back by :func:`parse_anchor_comment` on restart."""
    return f"A:{float(anchor_price):.2f}"


def tag_comment(base: str, anchor_price: float, max_len: int = 31) -> str:
    """Append the origin-anchor tag to an existing order comment, keeping the
    result within MT5's 31-char comment limit (MT5 silently rejects longer
    comments). The tag is preserved whole; only the base is trimmed if needed so
    the restart registry can always re-read the anchor."""
    tag = anchor_comment(anchor_price)
    base = (str(base) if base is not None else "").strip()
    if not base:
        return tag[:max_len]
    room = max_len - len(tag) - 1  # 1 for the separating space
    if room <= 0:
        return tag[:max_len]
    return f"{base[:room]} {tag}"


def parse_anchor_comment(comment) -> Optional[float]:
    """Extract the origin anchor price from an order comment tagged ``A:<price>``.
    Returns the float anchor or None when the comment carries no tag."""
    if not comment:
        return None
    m = ANCHOR_TAG_RE.search(str(comment))
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def build_registry(orders, magic: Optional[int] = None) -> Dict[int, float]:
    """Rebuild the anchor registry {ticket -> origin_anchor_price} by parsing the
    ``A:<price>`` comment of every order from ``mt5.orders_get()``. This is the
    restart path: the registry is reconstructed from the broker's own order state,
    so tagging survives a bot restart with no local file. Orders with no tag (or an
    unreadable ticket) are skipped. When ``magic`` is given, only our own orders are
    registered (foreign-magic orders on a shared account are ignored)."""
    registry: Dict[int, float] = {}
    for o in orders or []:
        if magic is not None and int(getattr(o, "magic", -1) or -1) != int(magic):
            continue
        anchor = parse_anchor_comment(getattr(o, "comment", None))
        if anchor is None:
            continue
        ticket = getattr(o, "ticket", None)
        if ticket is None:
            continue
        try:
            registry[int(ticket)] = float(anchor)
        except (TypeError, ValueError):
            continue
    return registry


# --- pure decision helpers --------------------------------------------------------
def is_stale(origin_anchor: float, current_anchor: float,
             interval: float = INTERVAL) -> bool:
    """True iff a pending order's origin anchor is at least INTERVAL away from the
    current anchor (i.e. it belongs to a prior anchor and is a sweep candidate)."""
    if origin_anchor is None or current_anchor is None:
        return False
    return abs(float(origin_anchor) - float(current_anchor)) >= float(interval)


def detect_anchor_event(last_anchor: Optional[float], price: float,
                        interval: float = INTERVAL) -> Optional[float]:
    """Anchor-event detection for the tick/bar loop: when price has moved at least
    INTERVAL from the last anchor, a NEW anchor event has occurred. Returns the new
    anchor (snapped to the INTERVAL grid from ``last_anchor`` in the direction of
    travel) or None when price is still within INTERVAL of ``last_anchor``. With no
    prior anchor, the current price seeds the first anchor."""
    if price is None:
        return None
    if last_anchor is None:
        return float(price)
    delta = float(price) - float(last_anchor)
    steps = int(abs(delta) // float(interval))
    if steps < 1:
        return None
    return float(last_anchor) + (interval * steps if delta > 0 else -interval * steps)


def _order_side(order, mt5) -> Optional[str]:
    """'BUY' / 'SELL' for a pending stop/limit order, else None."""
    t = getattr(order, "type", None)
    buys = {getattr(mt5, "ORDER_TYPE_BUY_STOP", 4),
            getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2),
            getattr(mt5, "ORDER_TYPE_BUY", 0)}
    sells = {getattr(mt5, "ORDER_TYPE_SELL_STOP", 5),
             getattr(mt5, "ORDER_TYPE_SELL_LIMIT", 3),
             getattr(mt5, "ORDER_TYPE_SELL", 1)}
    if t in buys:
        return "BUY"
    if t in sells:
        return "SELL"
    return None


def _position_side(pos, mt5) -> Optional[str]:
    """'BUY' / 'SELL' for an open position (position type 0=BUY, 1=SELL)."""
    t = getattr(pos, "type", None)
    if t == getattr(mt5, "POSITION_TYPE_BUY", 0):
        return "BUY"
    if t == getattr(mt5, "POSITION_TYPE_SELL", 1):
        return "SELL"
    return None


def is_rescue_leg(order, positions, mt5, interval: float = INTERVAL,
                  tol: float = _RESCUE_LEG_TOL) -> bool:
    """True iff this pending order is the rescue leg of a currently OPEN position:
    the INTERVAL-point OPPOSITE-side leg mapped to that position. A No-OCO position
    keeps one opposite-side stop exactly INTERVAL away as its recovery hedge; that
    leg must NEVER be swept even though its origin anchor is stale. Matching is by
    (opposite side) AND (|order price - position entry| within tol of INTERVAL)."""
    o_side = _order_side(order, mt5)
    if o_side is None:
        return False
    o_price = getattr(order, "price_open", None)
    if o_price is None:
        return False
    for p in positions or []:
        p_side = _position_side(p, mt5)
        if p_side is None or p_side == o_side:
            continue  # a rescue leg is the OPPOSITE side of the open position
        p_entry = getattr(p, "price_open", None)
        if p_entry is None:
            continue
        if abs(abs(float(o_price) - float(p_entry)) - float(interval)) <= float(tol):
            return True
    return False


# --- the side-effecting sweep -----------------------------------------------------
def _remove_pending(mt5, ticket, logger) -> bool:
    """Issue a single TRADE_ACTION_REMOVE for ``ticket``. Returns True on
    TRADE_RETCODE_DONE, else False. Never raises."""
    action = getattr(mt5, "TRADE_ACTION_REMOVE", 2)
    done = getattr(mt5, "TRADE_RETCODE_DONE", _TRADE_RETCODE_DONE)
    try:
        res = mt5.order_send({"action": action, "order": int(ticket)})
    except Exception as e:  # a broker/transport raise must not abort the sweep
        (logger or log).warning(f"stale_leg_sweep: order_send raised for {ticket}: {e!r}")
        return False
    rc = getattr(res, "retcode", None) if res is not None else None
    return rc == done


def cancel_stale_leg(mt5, ticket, origin_anchor, current_anchor, logger=None) -> bool:
    """Cancel one stale pending leg with a single retry on failure (requotes /
    busy), logging the outcome. Returns True if the broker confirmed the removal.
    On repeated failure it logs and returns False WITHOUT raising, so the caller
    (the new-straddle placement) is never blocked by a stubborn cancel."""
    logger = logger or log
    if _remove_pending(mt5, ticket, logger):
        logger.info(
            f"stale_leg_sweep: cancelled ticket={ticket} "
            f"origin_anchor={origin_anchor} current_anchor={current_anchor} "
            f"reason={SWEEP_REASON}")
        return True
    # Retry ONCE (transient requote / server-busy).
    logger.warning(
        f"stale_leg_sweep: cancel failed for ticket={ticket} "
        f"(origin_anchor={origin_anchor}) — retrying once")
    if _remove_pending(mt5, ticket, logger):
        logger.info(
            f"stale_leg_sweep: cancelled ticket={ticket} on retry "
            f"origin_anchor={origin_anchor} current_anchor={current_anchor} "
            f"reason={SWEEP_REASON}")
        return True
    logger.error(
        f"stale_leg_sweep: cancel FAILED after retry for ticket={ticket} "
        f"origin_anchor={origin_anchor} current_anchor={current_anchor} "
        f"— leaving order, continuing (placement not blocked)")
    return False


def sweep_stale_legs(mt5, symbol: str, current_anchor: float,
                     interval: float = INTERVAL, registry: Optional[Dict[int, float]] = None,
                     logger=None, magic: Optional[int] = None) -> List[dict]:
    """Sweep every stale pending leg for ``symbol`` before the new anchor's
    straddle is placed.

    For each pending order from ``mt5.orders_get(symbol=symbol)`` whose ORIGIN
    anchor differs from ``current_anchor`` by >= ``interval``, cancel it via
    ``TRADE_ACTION_REMOVE`` — UNLESS it is the rescue leg of a currently open
    position (checked against ``mt5.positions_get``), in which case it is skipped.

    MULTI-BOT ISOLATION: when ``magic`` is given, ONLY orders bearing that magic are
    considered — foreign-magic pendings (another bot on the same symbol/account, or a
    manual trade) are never touched. ``orders_get`` returns every order on the symbol
    regardless of magic, so this filter is what keeps the sweep from cancelling the
    Rogue T2 bot's (or anyone else's) resting orders. ``magic=None`` preserves the
    legacy symbol-only behavior for callers that own every order on the symbol.

    The origin anchor is taken from ``registry`` when supplied (the rebuilt-on-
    restart map) and otherwise parsed from the order comment, so a fresh process
    with no registry still sweeps correctly off the ``A:<price>`` tags. Retcode
    failures retry once then log-and-continue; the sweep NEVER raises, so the new
    straddle placement always proceeds.

    Returns a list of ``{ticket, origin_anchor, current_anchor, cancelled, reason}``
    records — one per stale leg acted on (cancelled or attempted), for the caller
    to log / journal."""
    logger = logger or log
    results: List[dict] = []
    try:
        orders = mt5.orders_get(symbol=symbol) or []
    except Exception as e:
        logger.warning(f"stale_leg_sweep: orders_get failed ({e!r}) — nothing swept")
        return results
    try:
        positions = mt5.positions_get(symbol=symbol) or []
    except TypeError:
        # some MT5 stubs accept no kwargs
        try:
            positions = mt5.positions_get() or []
        except Exception as e:
            logger.warning(f"stale_leg_sweep: positions_get failed ({e!r}) — exempting nothing")
            positions = []
    except Exception as e:
        logger.warning(f"stale_leg_sweep: positions_get failed ({e!r}) — exempting nothing")
        positions = []

    for o in orders:
        # MULTI-BOT ISOLATION: skip any order that is not ours. orders_get is
        # symbol-scoped, not magic-scoped, so without this a shared-account sweep
        # would cancel other bots' / manual pendings.
        if magic is not None and int(getattr(o, "magic", -1) or -1) != int(magic):
            continue
        # RESCUE-BOOST EXEMPTION: a live rescue boost (comment "RB1:<t>"/"RB2:<t>")
        # is the recovery leg of an open straddle position and shares our own magic;
        # it is never a stale straddle leg, so never sweep it. (It also carries no
        # "A:" origin tag, so it would be skipped below as unknown-origin — this is
        # the explicit, self-documenting guard + matches the rescue-boost feature.)
        if _is_rescue_boost_comment(getattr(o, "comment", None)):
            continue
        ticket = getattr(o, "ticket", None)
        origin = None
        if registry and ticket is not None:
            origin = registry.get(int(ticket)) if _try_int(ticket) is not None else None
        if origin is None:
            origin = parse_anchor_comment(getattr(o, "comment", None))
        if origin is None:
            # Untagged / unknown origin -> leave it alone (fail safe, never blind-cancel).
            continue
        if not is_stale(origin, current_anchor, interval):
            continue
        if is_rescue_leg(o, positions, mt5, interval):
            logger.info(
                f"stale_leg_sweep: SKIP rescue leg ticket={ticket} "
                f"origin_anchor={origin} current_anchor={current_anchor} "
                f"(INTERVAL-point opposite leg of an open position)")
            continue
        ok = cancel_stale_leg(mt5, ticket, origin, current_anchor, logger=logger)
        results.append({
            "ticket": ticket,
            "origin_anchor": origin,
            "current_anchor": current_anchor,
            "cancelled": ok,
            "reason": SWEEP_REASON,
        })
        if ok:
            try:  # decision-grade review line (stale anchor leg swept)
                import review_log as _rv
                _rv.get_review_logger().pending(
                    'ANCHOR', 'swept', tag=f"origin_{origin}",
                    price=(float(current_anchor) if current_anchor is not None else None))
            except Exception:
                pass
    return results


def _try_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_RESCUE_BOOST_RE = re.compile(r"RB[12]:\d+")
# Rogue v2 stop-mode orders ("RGS:A1" / "RGS:C<n>") are the Rogue engine's own
# resting stops (magic 20260626), NOT anchor straddle legs — never sweep them.
_ROGUE_STOP_RE = re.compile(r"RGS:(?:S\d:)?(?:A1|C\d+)")


def _is_rescue_boost_comment(comment) -> bool:
    """True for an order the sweep must never touch — a rescue-boost recovery leg
    ("RB1:<ticket>"/"RB2:<ticket>"), a Rogue stop-mode order ("RGS:A1"/"RGS:C<n>"),
    a TESTORDER order-path-verification order, or a TESTFIRE test leg ("TF_..."). A
    TF_ test straddle is FULLY isolated: a real anchor's stale-leg sweep must never
    cancel it (Feature-2 reverse isolation). None is ever a stale straddle leg."""
    if not comment:
        return False
    c = str(comment)
    return bool(_RESCUE_BOOST_RE.search(c) or _ROGUE_STOP_RE.search(c)
                or "TESTORDER" in c or "TF_" in c)


# --- LiveTrader binding (hooked in live_trader.py) --------------------------------
def _sweep_stale_legs(self, current_anchor: float) -> List[dict]:
    """LiveTrader method: sweep prior-anchor pending legs before a new straddle is
    placed. Flag-gated on ``cfg.stale_leg_sweep_enabled`` (default ON) and fully
    guarded so a sweep problem can never block anchor placement. Reads the interval
    and symbol from config. Paper runs sweep too — ``cancel_order`` is dry-run in
    paper, and here the same order_send path is a no-op when there is nothing to
    cancel."""
    cfg = getattr(self, "cfg", None)
    if cfg is not None and not bool(getattr(cfg, "stale_leg_sweep_enabled", True)):
        return []
    try:
        mt5 = self.adapter.mt5
    except Exception:
        return []
    symbol = getattr(cfg, "symbol", SYMBOL) if cfg is not None else SYMBOL
    interval = float(getattr(cfg, "stale_leg_interval", INTERVAL)) if cfg is not None else INTERVAL
    registry = getattr(self, "_anchor_registry", None)
    # MULTI-BOT ISOLATION: only ever cancel our own straddle magic. On the shared
    # XAUUSD account this keeps the sweep off the Rogue T2 bot and any manual order.
    magic = int(getattr(cfg, "stale_leg_sweep_magic", STRADDLE_MAGIC)) if cfg is not None else STRADDLE_MAGIC
    try:
        swept = sweep_stale_legs(mt5, symbol, current_anchor,
                                 interval=interval, registry=registry, logger=log,
                                 magic=magic)
    except Exception as e:
        log.warning(f"stale_leg_sweep: unexpected failure ({e!r}) — placement proceeds")
        return []
    # Prune cancelled tickets from the in-memory registry so it stays truthful.
    if isinstance(registry, dict):
        for rec in swept:
            if rec.get("cancelled"):
                registry.pop(_try_int(rec.get("ticket")), None)
    return swept


def rebuild_registry_from_broker(self) -> Dict[int, float]:
    """Restart hook: rebuild ``self._anchor_registry`` from live broker order state
    by parsing the ``A:<price>`` comments. Safe to call on boot; returns the map."""
    reg: Dict[int, float] = {}
    try:
        mt5 = self.adapter.mt5
        cfg = getattr(self, "cfg", None)
        symbol = getattr(cfg, "symbol", SYMBOL)
        magic = int(getattr(cfg, "stale_leg_sweep_magic", STRADDLE_MAGIC)) if cfg is not None else STRADDLE_MAGIC
        reg = build_registry(mt5.orders_get(symbol=symbol) or [], magic=magic)
    except Exception as e:
        log.warning(f"stale_leg_sweep: registry rebuild failed ({e!r})")
    self._anchor_registry = reg
    return reg
