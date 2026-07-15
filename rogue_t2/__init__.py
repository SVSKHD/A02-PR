"""Rogue T2 Continuation V1 — a self-contained, magic-isolated bot integrated into
the a02-pr (AUREON) repo. XAUUSD only. Simulated by default (TRADING_UNLOCKED=False).

See README.md for the runbook and UNLOCK.md before ever going live.
"""
from .config import RogueT2Config, ROGUE_T2_MAGIC

__all__ = ["RogueT2Config", "ROGUE_T2_MAGIC"]
