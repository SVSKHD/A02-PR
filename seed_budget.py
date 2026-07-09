"""AUREON — SHARED pure cores for the $10-break seed anchor (Rule 1) + the earned trade
budget (Rule 2), reused VERBATIM by both the Rogue (magic 20260626) and Fetcher (20260707)
engines. No IO, no engine state assumptions beyond a plain dict `st` -- the impure driver
in each engine binds these and does the placement / telemetry / persistence.

Both rules are ANCHOR-PLACEMENT / trade-budget rules ONLY -- they change WHEN an anchor is
planted and WHETHER a new entry is permitted, never the entry/SL/trail logic itself, and
the anchors engine (20260522) is completely unaffected.

RULE 1 -- $10-BREAK SEED ANCHOR (break_seed_anchor): A1 is the seed REFERENCE; the day's
anchor is not planted until price first travels seed_break_dollars from A1 in either
direction, at which point the $-point (A1 +/- break) latches as the anchor for the day.

RULE 2 -- EARNED TRADE BUDGET (budget_*): each anchor gets engine_base_trades_per_anchor
free attempts; a further entry needs the last engine_extend_requires_wins closes to be ALL
wins. Budget spent without an earned extension -> EXHAUSTED -> block for
engine_exhausted_gap_sec (wall-clock), then plant a fresh anchor with a fresh budget.

The budget SESSION spans an anchor's chain re-anchors (a Fetcher/Rogue anchor re-anchors at
every close) -- it is NOT reset per chain re-anchor, only when a FRESH anchor is planted (the
day's first seed, a post-exhaustion fresh anchor, or a manual reseed). That is the whole
point: the chop the evidence showed was 4 chained re-entries inside one noise band.
"""
from __future__ import annotations

# Seed-source stamp for a Rule-1 break anchor (segmentable in the ledgers/CSVs, D-8).
SEED_A1_BREAK = 'A1_BREAK'


# --- RULE 1: $10-break seed anchor --------------------------------------------------------
def break_seed_anchor(st, a1_ref, price, break_dollars):
    """Withhold the seed anchor until price first travels `break_dollars` from the A1
    reference `a1_ref`; the $-point (a1_ref +/- break_dollars) then LATCHES as the day's
    anchor. Returns (anchor_px | None, just_latched). None means "no anchor yet" -> the
    engine WAITS (either no A1 ref yet, or price has not travelled the full break). The FIRST
    break latches for the day (st['break_latched']); the opposite side never re-seeds.

    Mutates only st['break_*']. Only called when break_dollars > 0 (the caller treats <= 0 as
    disabled -> seed at A1 directly). Guarded against a None ref / price. NEVER raises."""
    try:
        if st.get('break_latched'):
            return float(st.get('break_anchor_px')), False
        if a1_ref is None or price is None:
            return None, False
        disp = float(price) - float(a1_ref)
        if abs(disp) >= float(break_dollars):
            sign = 1.0 if disp > 0 else -1.0
            anchor = round(float(a1_ref) + sign * float(break_dollars), 2)
            st['break_latched'] = True
            st['break_anchor_px'] = anchor
            st['break_a1_ref'] = round(float(a1_ref), 2)
            return anchor, True
        return None, False
    except Exception:
        return None, False


# --- RULE 2: earned trade budget ----------------------------------------------------------
def new_budget():
    """A fresh per-anchor-SESSION budget. trades = entries taken off this anchor session;
    wl = trailing win(True)/loss(False) window of its closes; gap_until = wall-clock epoch the
    exhaustion gap ends (None when not exhausted)."""
    return {'trades': 0, 'wl': [], 'gap_until': None}


def budget_off(cfg):
    """PURE: True iff the earned-budget rule is disabled (engine_base_trades_per_anchor <= 0).
    A disabled budget never gates entries and never exhausts -- byte-neutral to today."""
    return int(getattr(cfg, 'engine_base_trades_per_anchor', 0) or 0) <= 0


def budget_record_entry(b):
    """A NEW entry was taken off this anchor session. Consumes one budget attempt. PURE."""
    b['trades'] = int(b.get('trades', 0)) + 1
    return b


def budget_record_close(b, is_win):
    """Append this close's outcome to the trailing win/loss window (bounded). is_win = the
    close was profitable (pnl > 0); anything <= 0 is a loss. PURE."""
    wl = list(b.get('wl', []))
    wl.append(bool(is_win))
    b['wl'] = wl[-8:]
    return b


def budget_can_trade(b, cfg):
    """PURE: may a NEW entry be taken from THIS anchor session now? (ok, reason), ignoring the
    gap timer (the caller checks that first). base free attempts, then the last
    engine_extend_requires_wins closes must be ALL wins to earn each +1. reason in
    ('disabled','base','earned','exhausted')."""
    base = int(getattr(cfg, 'engine_base_trades_per_anchor', 2) or 0)
    if base <= 0:
        return True, 'disabled'
    trades = int(b.get('trades', 0))
    if trades < base:
        return True, 'base'
    need = int(getattr(cfg, 'engine_extend_requires_wins', 2) or 0)
    if need <= 0:
        return True, 'earned'                 # extension requirement disabled -> always unlock
    wl = list(b.get('wl', []))
    if len(wl) >= need and all(wl[-need:]):
        return True, 'earned'
    return False, 'exhausted'


def budget_in_gap(b, now_epoch):
    """PURE: True while the exhaustion gap is still running (new entries blocked; open legs
    keep being managed by the caller upstream of this gate)."""
    u = b.get('gap_until')
    return u is not None and float(now_epoch) < float(u)


def budget_gap_ready(b, now_epoch):
    """PURE: True once the exhaustion gap has elapsed -> the caller plants a FRESH anchor at
    the current tick and resets the budget."""
    u = b.get('gap_until')
    return u is not None and float(now_epoch) >= float(u)


def budget_start_gap(b, cfg, now_epoch):
    """Latch the exhaustion gap timer (gap_until = now + engine_exhausted_gap_sec). Returns
    the gap seconds used (for the one-time alert). PURE w.r.t. IO."""
    gap = float(getattr(cfg, 'engine_exhausted_gap_sec', 900.0) or 0.0)
    b['gap_until'] = float(now_epoch) + gap
    return gap


def budget_reset(b):
    """A FRESH anchor session begins (day seed / post-gap fresh anchor / manual reseed):
    clear the trade count, the win/loss window and the gap. PURE."""
    b['trades'] = 0
    b['wl'] = []
    b['gap_until'] = None
    return b


def wl_tag(b, n=2):
    """PURE: the last `n` closes as a short 'W'/'L' string for the exhaustion alert
    ('W,L', 'L,L', ...). '-' when the window is empty."""
    wl = list(b.get('wl', []))[-n:]
    return ','.join('W' if x else 'L' for x in wl) or '-'
