"""
AUREON — single source of truth for the version.

Bump __version__ HERE and nowhere else. Every module imports it; the Telegram
startup banner, logs, and journal all read from this file, so the version you
see in Telegram is by construction the version that is running.

History (one line per behavioral change):
  2.5.x  legacy: trail from bar one (freeze silently dead -- tz bug)
  2.7    FIX fill_time/bar_time clock skew -> 45m hold actually works;
         fallback inversion fix; hold-duration audit + FREEZE BREACH alarm;
         config: freeze 45, gap 1.0, arm 1.5, no_oco default ON
  2.7.1  TSTOP: cut legs that never reached +$1, at hold expiry, at market
  2.8    profit ladder during hold (3 -> BE, 10 -> +8)
  2.9    role-aware exits: No-OCO 2nd legs run as RESCUE (no small locks)
  2.9.1  top tier follows peak - $2 (floor +$8)
  2.9.2  BE tier 3.00 -> 2.50; trail gap 1.00 -> 2.00 (one rule: in profit,
         never more than $2 behind the peak)
  2.9.3  A1/A2 anchors disabled -- honest replay shows them net -$7.1k/29d
         and their losses kill-switch-block A3/A4 (the only green anchors)
  2.9.4  all 4 anchors re-enabled for live forward trial on demo; verdict
         per anchor from the live journal after 2 weeks (backtest set aside)
  2.9.5  SL-RESCUE BOOST (Hithesh): on rescue fill, +2 market trades in the
         rescue direction, tight $6 SL each -- covers the twins remaining
         $8-to-SL on crash days (~+$560), capped -$420 on whipsaw days
  2.9.6  trail arm 1.50 -> 2.50: sub-$2.5 peaks keep full SL post-hold
         (kills the $0 scratch-at-entry exits; Jun-11 A3 lesson)
  2.9.7  trail gap actually set 1.00 -> 2.00 (v2.9.2 manual edit was never
         applied; caught via the live banner config receipt on Jun-11 A4)
  2.9.8  Jun-12 A1 forensics: (1) RESCUE now detected STRUCTURALLY (2nd fill
         of a live anchor), flag is only a hint -- flag-loss no longer kills
         the fleet; pendings + rescue flag persisted/rehydrated across
         restarts; (2) STOP-THROUGH: ladder stop breached intrabar -> close
         at market (old clamp pinned SL to bid: the BUY -$2.20 'Trail');
         (3) exit classifier names the rule (BE/LOCK4/TIER/Trail/SL/TP +
         slip) from the bot's own current_sl -- kills false FREEZE BREACH on
         ladder exits; (4) boost orders: exceptions Telegram-visible, rc=None
         shows last_error, 10030 retries FOK (Jun-11 silent-boost mystery);
         (5) journal-only no-hold trail counterfactual per leg -- decides
         hold-vs-no-hold from live data
  2.9.9  FIX A stale rescue flag: a 2nd fill is a genuine RESCUE only if its
         twin is STILL OPEN at the moment of the 2nd fill (Jun-12 A4: SELL
         banked +$477 and closed, BUY filled an hour later, inherited the
         stale rescue_on_fill flag and fired 2 boosts with no twin to rescue;
         A2 same setup fired nothing -> nondeterministic). Twin-open is tested
         structurally (sibling_ticket or any non-boost open leg of the anchor);
         a stale flag with a closed twin runs as a normal breakout leg, no
         boosts. FIX B boost diagnostics: every exit of the boost path now
         self-reports (attempting / exception / result=None+last_error /
         rejected rc+name+comment / filled price+ticket) -- the 0-for-6 silent
         boosts get a full trace on the next live event (no param change)
"""

__version__ = "2.9.9"
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"