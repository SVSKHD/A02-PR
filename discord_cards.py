"""AUREON v3.1.1 — Discord embed CARD builders (pure, no network, no discord.py).

Every alert is a rich Discord embed (a "card"): a short headline TITLE, a state
COLOR, an AUTHOR line (AUREON · {anchor}), a GRID of inline fields, and a clean
single-line ts FOOTER. Cards are built to be SCANNABLE in under 2 seconds — the
title + color tell the outcome, the fields give the detail, P&L stands alone.

No Telegram MarkdownV2 here: embeds render structure, not *stars*/`backticks`.

These builders are pure dict factories so the selftest can prove each card BUILDS
and stays within Discord's embed limits without any network or discord.py.

Discord limits enforced: title <=256, field name <=256, field value <=1024,
description <=4096, <=25 fields, footer/author <=256.
"""

# State colors (one source of truth).
GREEN  = 0x22c55e   # TP, profitable close, CRASH_WIN
RED    = 0xef4444   # SL, losing close, WHIPSAW_LOSS, kill-switch
AMBER  = 0xf59e0b   # BE/scratch close, ladder locks (TIER/LOCK4/+4)
BLUE   = 0x3b82f6   # anchor placed, fill (info)
ORANGE = 0xf97316   # rescue, boost (high attention)
GREY   = 0x6b7280   # heartbeat, status, startup banner

MAX_TITLE = 256
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_DESC = 4096
MAX_FOOTER = 256
MAX_AUTHOR = 256
MAX_FIELDS = 25


def _card_footer():
    """Clean one-line timestamp footer:
        🕐 12:30 PM IST · server 10:00 · Wed Jun 17
    Derived from the single-source instant (telemetry._ts_components). Imported
    lazily so this module never imports telemetry at load time, and never raises."""
    try:
        from telemetry import _ts_components
        server, ist = _ts_components()
        h12 = ist.hour % 12 or 12
        ampm = "AM" if ist.hour < 12 else "PM"
        return (f"🕐 {h12}:{ist.minute:02d} {ampm} IST · "
                f"server {server.hour:02d}:{server.minute:02d} · "
                f"{ist.strftime('%a')} {ist.strftime('%b')} {ist.day}")
    except Exception:
        return "🕐"


def _clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _esc(s):
    """Backslash-escape Discord markdown specials so identifiers with underscores
    (anchor labels like A1_02h_Asia, order comments, tickets) render VERBATIM in
    field VALUES / descriptions instead of being eaten as *italic*/`code` — the
    'A102hAsia' bug. Embed titles/author/field-names don't render markdown, so
    only values + descriptions are escaped. Backslash itself is escaped first."""
    s = "" if s is None else str(s)
    for ch in ("\\", "_", "*", "`", "~"):
        s = s.replace(ch, "\\" + ch)
    return s


def _short(anchor):
    """Anchor short tag for the title: 'A2_10h_London' -> 'A2'."""
    if not anchor:
        return "?"
    return str(anchor).split("_")[0]


def _field(name, value, inline=True):
    v = "" if value is None else str(value)
    v = _esc(v) if v.strip() else "—"
    return {"name": _clip(name, MAX_FIELD_NAME),
            "value": _clip(v, MAX_FIELD_VALUE),
            "inline": bool(inline)}


def build_embed(title, color, fields=None, description=None, footer=None,
                author=None):
    """Assemble a limit-safe embed dict. `fields` is a list of (name, value[,
    inline]) tuples. `author` is a string (rendered as the small author line).
    NEVER raises."""
    try:
        out = {"title": _clip(title, MAX_TITLE), "color": int(color)}
        if author:
            out["author"] = {"name": _clip(author, MAX_AUTHOR)}
        if description:
            out["description"] = _clip(_esc(description), MAX_DESC)
        flds = []
        for f in (fields or [])[:MAX_FIELDS]:
            if isinstance(f, dict):
                flds.append(_field(f.get("name"), f.get("value"), f.get("inline", True)))
            else:
                name, value = f[0], f[1]
                inline = f[2] if len(f) > 2 else True
                flds.append(_field(name, value, inline))
        if flds:
            out["fields"] = flds
        out["footer"] = {"text": _clip(footer or _card_footer(), MAX_FOOTER)}
        return out
    except Exception:
        return {"title": _clip(str(title), MAX_TITLE), "color": int(GREY),
                "footer": {"text": _clip(_card_footer(), MAX_FOOTER)}}


def _author(anchor):
    return f"AUREON · {anchor}" if anchor else "AUREON"


def _money(v):
    """Signed, $-prefixed, 2dp: +$226.80 / -$210.00 / n/a."""
    try:
        f = float(v)
        return f"{'+' if f >= 0 else '-'}${abs(f):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _price(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _held(held_min):
    return f"{held_min:.1f}m" if isinstance(held_min, (int, float)) else "—"


# ============================================================================
# One builder per event type (signatures unchanged; layout cleaned up v3.1.1)
# ============================================================================
def card_anchor_placed(anchor, anchor_price, buy_sl, buy_tp, sell_sl, sell_tp,
                       lot, footer=None):
    return build_embed(
        f"⚓ {_short(anchor)} placed", BLUE, author=_author(anchor),
        fields=[
            ("Anchor", _price(anchor_price)),
            ("Lot", lot),
            ("BUY SL", _price(buy_sl)), ("BUY TP", _price(buy_tp)),
            ("SELL SL", _price(sell_sl)), ("SELL TP", _price(sell_tp)),
        ], footer=footer)


def card_fill(anchor, side, entry, ticket, role=None, sl=None, tp=None,
              sched_actual=None, footer=None):
    fields = [
        ("Entry", _price(entry)),
        ("Ticket", ticket),
        ("Role", role or "normal"),
        ("SL", _price(sl)),
        ("TP", _price(tp)),
    ]
    if sched_actual:
        fields.append(("Scheduled vs actual", sched_actual, False))
    return build_embed(f"🎯 {_short(anchor)} {side} FILL", BLUE,
                       author=_author(anchor), fields=fields, footer=footer)


def close_color(pnl, reason):
    r = (reason or "").upper()
    if r in ("TP",) or (pnl is not None and pnl > 0):
        return GREEN
    if r in ("SL",) or (pnl is not None and pnl < 0):
        return RED
    return AMBER          # BE / scratch / flat


def card_close(anchor, side, reason, entry, exit_price, pnl, held_min=None,
               day_total=None, nh_shadow=None, footer=None):
    # Grid: Entry | Exit | P&L  /  Held | Reason | Day total
    fields = [
        ("Entry", _price(entry)),
        ("Exit", _price(exit_price)),
        ("P&L", _money(pnl)),
        ("Held", _held(held_min)),
        ("Reason", reason or "—"),
        ("Day total", _money(day_total)),
    ]
    if nh_shadow:
        fields.append(("No-hold shadow", nh_shadow, False))
    return build_embed(f"📤 {_short(anchor)} {side} · {reason}",
                       close_color(pnl, reason), author=_author(anchor),
                       fields=fields, footer=footer)


def card_rescue(anchor, trapped_leg, rescue_leg, twin_pnl, footer=None):
    return build_embed(
        f"🚑 {_short(anchor)} RESCUE", ORANGE, author=_author(anchor),
        fields=[
            ("Trapped leg", trapped_leg, False),
            ("Rescue leg", rescue_leg, False),
            ("Twin P&L", _money(twin_pnl)),
            ("Boosts", "firing ⚡"),
        ], footer=footer)


def card_boost(n, side, entry, sl, tp, rc=None, footer=None):
    return build_embed(
        f"⚡ BOOST{n} {side}", ORANGE,
        fields=[
            ("Entry", _price(entry)),
            ("SL", _price(sl)),
            ("TP", _price(tp)),
            ("rc", rc if rc is not None else "—"),
        ], footer=footer)


_BRANCH_COLOR = {"CRASH_WIN": GREEN, "WHIPSAW_LOSS": RED, "SCRATCH": AMBER}
_BRANCH_TAG = {"CRASH_WIN": "CRASH WIN", "WHIPSAW_LOSS": "WHIPSAW", "SCRATCH": "SCRATCH"}


def card_fleet(anchor, branch, leg_pnls, net, counterfactual=None, footer=None):
    """leg_pnls: list of (label, pnl) tuples."""
    b = str(branch).upper()
    fields = [(str(label), _money(pnl)) for label, pnl in (leg_pnls or [])]
    fields.append(("Event NET", _money(net)))
    fields.append(("Branch", _BRANCH_TAG.get(b, branch)))
    if counterfactual is not None:
        fields.append(("No-boost", _money(counterfactual)))
    return build_embed(f"🛟 {_short(anchor)} FLEET · {_BRANCH_TAG.get(b, branch)}",
                       _BRANCH_COLOR.get(b, GREY), author=_author(anchor),
                       fields=fields, footer=footer)


def card_eod(date, net, n_trades, balance=None, anchors_hit=None, footer=None):
    return build_embed(
        f"🌙 EOD {date}", GREY,
        fields=[
            ("Day net", _money(net)),
            ("Trades", n_trades),
            ("Balance", _price(balance)),
            ("Anchors hit", anchors_hit or "—"),
            ("Journal", "saved"),
        ], footer=footer)


def card_heartbeat(balance=None, equity=None, open_n=0, pending_n=0,
                   anchors_today=None, last_event=None, footer=None):
    return build_embed(
        "💓 AUREON alive", GREY,
        fields=[
            ("Balance", _price(balance)),
            ("Equity", _price(equity)),
            ("Open", open_n),
            ("Pending", pending_n),
            ("Anchors", anchors_today or "—"),
            ("Last event", last_event or "—"),
        ], footer=footer)


def card_status(snapshot, footer=None):
    """snapshot: dict of label -> value (account+anchors+positions)."""
    fields = [(str(k), v) for k, v in list((snapshot or {}).items())[:MAX_FIELDS]]
    return build_embed("📊 AUREON status", GREY, fields=fields, footer=footer)


def card_connect(footer=None):
    return build_embed(
        "✅ AUREON connected", GREY,
        description="Commands ready — try /status.", footer=footer)


def card_intent_warning(footer=None):
    return build_embed(
        "⚠️ Message Content Intent OFF", RED,
        description=("Alerts work, COMMANDS WILL NOT. Enable Message Content "
                     "Intent for this bot in the Discord Developer Portal "
                     "(Bot → Privileged Gateway Intents), then restart."),
        footer=footer)


# Severity name -> color, for generic (non-enriched) messages.
SEVERITY_COLOR = {
    "DEBUG": GREY, "INFO": BLUE, "SUCCESS": GREEN,
    "WARN": AMBER, "ERROR": RED, "CRITICAL": RED,
}


def card_generic(title, text, color=GREY, footer=None):
    """A plain colored card for any message without a dedicated builder. Telegram
    bold/code markers are dropped; underscores are KEPT (build_embed escapes them)
    so anchor names like A1_02h_Asia survive instead of becoming 'A102hAsia'."""
    return build_embed(_clip(title, MAX_TITLE), color,
                       description=_tg_clean(text), footer=footer)


def _tg_clean(text):
    """Normalize Telegram MarkdownV2 source into clean Discord text: undo Telegram
    backslash-escapes, then drop *bold*/`code` markers. Underscores are left for
    build_embed to escape (so identifiers render verbatim, not italicized)."""
    if not text:
        return text
    s = str(text)
    s = s.replace("\\_", "_").replace("\\*", "*").replace("\\`", "`")
    s = s.replace("*", "").replace("`", "")
    return s


def card_startup(version, mode, lot, kill, hold_tstop, ladder, boost_sl, alerts,
                 footer=None):
    """🚀 startup banner as a field grid (Title + bold-label fields), not a blob."""
    return build_embed(
        f"🚀 AUREON {version} {mode}", GREEN,
        fields=[
            ("Lot", lot),
            ("Kill switch", kill),
            ("Hold / TSTOP", hold_tstop),
            ("Ladder", ladder),
            ("Boost SL", boost_sl),
            ("Alerts", alerts),
        ], footer=footer)
