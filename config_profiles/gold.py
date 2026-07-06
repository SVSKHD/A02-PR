"""GOLD (XAUUSD) profile — INTENTIONALLY EMPTY.

The Config dataclass defaults ARE the gold profile; an empty override dict
guarantees `build_config('gold') == Config()` byte-identically (asserted in
the selftest's gold-regression step). Do NOT add keys here — change the
Config default itself if gold's behavior is meant to change.
"""
PROFILE = {}
