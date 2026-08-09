"""threshold_8 — Module D: exit precedence.

ONE function, ONE place. Evaluated every bar; the checks are tried in a fixed order
and the FIRST match wins. Nothing else closes a basket. The caller logs which rule
fired and acts on the returned scope:

  order  rule                                    scope
  1      DAILY_RISK breached (incl. floating)    day     -> flatten all, halt the day
  2      BASKET_MAX_RISK breached                basket  -> close basket
  3      SESSION_FLATTEN / FRIDAY_FLAT           basket  -> close basket
  4      BASKET_MAX_MINUTES exceeded             basket  -> close basket
  5      TRAIL locked hit (state == TRAILING)    basket  -> close basket
  6      BASKET_TP reached and not TRAILING      basket  -> close basket
  7      opposite anchor threshold triggers      basket  -> close basket, then the
                                                           entry engine may open a new one

Keeping this as a single ordered function is what makes check C11 (precedence with
two conditions simultaneously true) a property of the code, not of luck.
"""

from threshold_8_basket import STATE_TRAILING

# exit-reason tags (also the values written to the per-basket CSV)
EXIT_DAILY_RISK = "DAILY_RISK"
EXIT_BASKET_MAX_RISK = "BASKET_MAX_RISK"
EXIT_SESSION_FLATTEN = "SESSION_FLATTEN"
EXIT_FRIDAY_FLAT = "FRIDAY_FLAT"
EXIT_MAX_MINUTES = "MAX_MINUTES"
EXIT_TRAIL = "TRAIL"
EXIT_BASKET_TP = "BASKET_TP"
EXIT_OPPOSITE_ANCHOR = "OPPOSITE_ANCHOR"


class ExitDecision:
    __slots__ = ("reason", "scope", "order")

    def __init__(self, reason, scope, order):
        self.reason = reason
        self.scope = scope
        self.order = order

    def __repr__(self):
        return "ExitDecision(%s, %s, order=%d)" % (self.reason, self.scope, self.order)


def evaluate_exit_precedence(basket, params, now, *, day_net, session_flatten,
                             is_friday, opposite_anchor_triggered, duration_min):
    """Return the ExitDecision for this bar, or None if the basket stays open.

    Parameters
    ----------
    day_net : float
        Account net P/L for the whole day INCLUDING floating (all baskets).
    session_flatten : bool
        True once server time is at/after FLATTEN_SERVER_HHMM.
    is_friday : bool
        True on the broker-server Friday (weekend-hold ban).
    opposite_anchor_triggered : bool
        True when the entry engine's opposite-direction anchor threshold has fired.
    duration_min : float
        Basket age in minutes.
    """
    p = params

    # 1 — DAILY_RISK (incl. floating). Day-scope: flatten everything, halt the day.
    if day_net <= -p.daily_risk:
        return ExitDecision(EXIT_DAILY_RISK, "day", 1)

    # 2 — BASKET_MAX_RISK.
    if basket.net_pnl <= -p.basket_max_risk:
        return ExitDecision(EXIT_BASKET_MAX_RISK, "basket", 2)

    # 3 — SESSION_FLATTEN / FRIDAY_FLAT.
    if session_flatten:
        return ExitDecision(EXIT_SESSION_FLATTEN, "basket", 3)
    if is_friday and p.friday_flat and session_flatten:
        # Friday flatten only bites at/after the flatten time; the plain session
        # flatten above already covers it, but keep the explicit branch for clarity.
        return ExitDecision(EXIT_FRIDAY_FLAT, "basket", 3)

    # 4 — BASKET_MAX_MINUTES.
    if duration_min >= p.basket_max_minutes:
        return ExitDecision(EXIT_MAX_MINUTES, "basket", 4)

    # 5 — TRAIL locked hit (only while TRAILING).
    if basket.state == STATE_TRAILING and getattr(basket, "_trail_exit", False):
        return ExitDecision(EXIT_TRAIL, "basket", 5)

    # 6 — BASKET_TP reached AND not TRAILING.
    if basket.state != STATE_TRAILING and basket.net_pnl >= p.basket_tp:
        return ExitDecision(EXIT_BASKET_TP, "basket", 6)

    # 7 — opposite anchor threshold triggers.
    if opposite_anchor_triggered:
        return ExitDecision(EXIT_OPPOSITE_ANCHOR, "basket", 7)

    return None
