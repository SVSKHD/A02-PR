"""AUREON v3.1.0 — Discord embed CARD builders (pure, no network, no discord.py).

Every alert is a rich Discord embed (a "card"): a title, a state color, a grid of
fields, and a footer = ts_header() (IST + server + date, the single timestamp
source). These builders are pure dict factories so the selftest can prove each
card BUILDS and stays within Discord's embed limits without any network or the
discord.py dependency. discord_client.py posts them; nothing here imports it.

Discord embed limits enforced here: title <=256, field name <=256, field value
<=1024, description <=4096, <=25 fields, footer <=2048.
"""

# State colors (one source of truth; mirrors the task's color system).
GREEN  = 0x22c55e   # TP, profitable close, CRASH_WIN
RED    = 0xef4444   # SL, losing close, WHIPSAW_LOSS, kill-switch
AMBER  = 0xf59e0b   # BE/scratch close, ladder locks (TIER/LOCK4/+4)
BLUE   = 0x3b82f6   # anchor placed, fill (info)
ORANGE = 0xf97316   # rescue, boost (high attention)
GREY   = 0x6b7280   # heartbeat, status, startup banner

# Discord hard limits.
MAX_TITLE = 256
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_DESC = 4096
MAX_FOOTER = 2048
MAX_FIELDS = 25


def _ts_footer():
    """Footer = the single-source timestamp header. Imported lazily so this
    module never imports telemetry at load time (avoids any import cycle) and so
    a ts_header failure can never break a card (ts_header is itself crash-proof)."""
    try:
        from telemetry import ts_header
        return ts_header()
    except Exception:
        return "🕐 (timestamp unavailable)"


def _clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _field(name, value, inline=True):
    # Discord rejects empty field values; coerce to a non-empty placeholder.
    v = "" if value is None else str(value)
    if v.strip() == "":
        v = "—"
    return {"name": _clip(name, MAX_FIELD_NAME),
            "value": _clip(v, MAX_FIELD_VALUE),
            "inline": bool(inline)}


def build_embed(title, color, fields=None, description=None, footer=None):
    """Assemble a limit-safe embed dict. `fields` is a list of (name, value[,
    inline]) tuples or pre-built field dicts. NEVER raises."""
    try:
        out = {"title": _clip(title, MAX_TITLE), "color": int(color)}
        if description:
            out["description"] = _clip(description, MAX_DESC)
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
        out["footer"] = {"text": _clip(footer or _ts_footer(), MAX_FOOTER)}
        return out
    except Exception:
        # A card must never crash the alert path; degrade to a minimal embed.
        return {"title": _clip(str(title), MAX_TITLE), "color": int(GREY),
                "footer": {"text": _clip(_ts_footer(), MAX_FOOTER)}}


def _money(v):
    try:
        return f"${float(v):+,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _price(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


# ============================================================================
# One builder per event type
# ============================================================================
def card_anchor_placed(anchor, anchor_price, buy_sl, buy_tp, sell_sl, sell_tp,
                       lot, footer=None):
    return build_embed(
        f"⚓ {anchor}", BLUE,
        fields=[
            ("Anchor price", _price(anchor_price)),
            ("Lot", lot),
            ("BUY stop", f"SL {_price(buy_sl)} / TP {_price(buy_tp)}", False),
            ("SELL stop", f"SL {_price(sell_sl)} / TP {_price(sell_tp)}", False),
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
        fields.append(("Scheduled vs Actual", sched_actual, False))
    return build_embed(f"🎯 FILL {anchor} {side}", BLUE, fields=fields, footer=footer)


def close_color(pnl, reason):
    r = (reason or "").upper()
    if r in ("TP",) or (pnl is not None and pnl > 0):
        return GREEN
    if r in ("SL",) or (pnl is not None and pnl < 0):
        return RED
    return AMBER          # BE / scratch / flat


def card_close(anchor, side, reason, entry, exit_price, pnl, held_min=None,
               day_total=None, nh_shadow=None, footer=None):
    held = f"{held_min:.1f}m" if isinstance(held_min, (int, float)) else "—"
    fields = [
        ("Entry → Exit", f"{_price(entry)} → {_price(exit_price)}", False),
        ("P&L", f"**{_money(pnl)}**"),
        ("Exit reason", reason or "—"),
        ("Held", held),
        ("Day total", _money(day_total)),
    ]
    if nh_shadow:
        fields.append(("No-hold shadow", nh_shadow, False))
    return build_embed(f"📤 CLOSE {anchor} {side} — {reason}",
                       close_color(pnl, reason), fields=fields, footer=footer)


def card_rescue(anchor, trapped_leg, rescue_leg, twin_pnl, footer=None):
    return build_embed(
        f"🚑 RESCUE {anchor}", ORANGE,
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


def card_fleet(anchor, branch, leg_pnls, net, counterfactual=None, footer=None):
    """leg_pnls: list of (label, pnl) tuples."""
    fields = []
    for label, pnl in (leg_pnls or []):
        fields.append((str(label), _money(pnl)))
    fields.append(("Event NET", f"**{_money(net)}**", False))
    fields.append(("Branch", branch))
    if counterfactual is not None:
        fields.append(("No-boost counterfactual", _money(counterfactual)))
    return build_embed(f"🛟 FLEET {anchor} — {branch}",
                       _BRANCH_COLOR.get(str(branch).upper(), GREY),
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
            ("Anchors today", anchors_today or "—"),
            ("Last event", last_event or "—", False),
        ], footer=footer)


def card_status(snapshot, footer=None):
    """snapshot: dict of label -> value (account+anchors+positions). Rendered as a
    grey full-snapshot card; long lists collapse into the description."""
    fields = []
    for k, v in list((snapshot or {}).items())[:MAX_FIELDS - 1]:
        fields.append((str(k), v))
    return build_embed("📊 AUREON Status", GREY, fields=fields, footer=footer)


def card_connect(footer=None):
    return build_embed(
        "✅ AUREON connected", GREY,
        description="Commands ready — try `/status`.", footer=footer)


def card_intent_warning(footer=None):
    return build_embed(
        "⚠️ Message Content Intent OFF", RED,
        description=("Alerts work, **COMMANDS WILL NOT**. Enable *Message Content "
                     "Intent* for this bot in the Discord Developer Portal "
                     "(Bot → Privileged Gateway Intents), then restart."),
        footer=footer)


# Severity name -> color, for generic (non-enriched) messages.
SEVERITY_COLOR = {
    "DEBUG": GREY, "INFO": BLUE, "SUCCESS": GREEN,
    "WARN": AMBER, "ERROR": RED, "CRITICAL": RED,
}


def card_generic(title, text, color=GREY, footer=None):
    """A plain colored card for any message without a dedicated builder. The text
    becomes the description (Discord markdown; no Telegram MarkdownV2 escaping)."""
    return build_embed(_clip(title, MAX_TITLE), color,
                       description=text, footer=footer)
