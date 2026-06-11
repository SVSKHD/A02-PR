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
"""

__version__ = "2.9.5"
CODENAME = "Astra Hawk"


def banner() -> str:
    return f"AUREON v{__version__} ({CODENAME})"