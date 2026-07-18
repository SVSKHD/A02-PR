"""Deterministic synthetic M1 scenarios for the ROGUE monster-engine tests.

Self-contained (no MT5, no external rp2.py). The golden_*.txt outputs in this
directory were captured from the VALIDATED REFERENCE SIM (rp2) and verified to be
reproduced byte-for-byte by rogue_monster.MonsterEngine; they serve as the
in-repo parity/regression oracle. Regenerate with:

    python tests/fixtures/gen_goldens.py
"""
import numpy as np
import pandas as pd


def _bars(idx, close, wick=0.6):
    close = np.asarray(close, dtype=float)
    high = close + wick
    low = close - wick
    op = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": op, "high": high, "low": low, "close": close}, index=idx)


def scen_single_trend():
    """One day: long consolidation box then a clean sustained down-move."""
    idx = pd.date_range("2026-06-01 01:00", "2026-06-01 18:00", freq="1min")
    n = len(idx)
    rng = np.random.default_rng(11)
    drift = np.zeros(n)
    a, b = int(n * 0.40), int(n * 0.70)
    drift[a:b] = -40.0 / (b - a)
    close = 3350 + np.cumsum(drift + rng.normal(0, 0.5, n))
    return _bars(idx, close)


def scen_multi_day_redgreen():
    """Three days: whipsaw, clean trend, chop. Exercises red-day atr carry."""
    frames = []
    idx1 = pd.date_range("2026-06-02 01:00", "2026-06-02 18:00", freq="1min")
    n1 = len(idx1); rng1 = np.random.default_rng(21)
    d1 = np.zeros(n1)
    d1[int(n1*0.40):int(n1*0.50)] = 25.0 / (int(n1*0.50)-int(n1*0.40))
    d1[int(n1*0.50):int(n1*0.62)] = -34.0 / (int(n1*0.62)-int(n1*0.50))
    c1 = 3360 + np.cumsum(d1 + rng1.normal(0, 0.6, n1))
    frames.append(_bars(idx1, c1))
    idx2 = pd.date_range("2026-06-03 01:00", "2026-06-03 18:00", freq="1min")
    n2 = len(idx2); rng2 = np.random.default_rng(22)
    d2 = np.zeros(n2)
    d2[int(n2*0.42):int(n2*0.78)] = 46.0 / (int(n2*0.78)-int(n2*0.42))
    c2 = 3355 + np.cumsum(d2 + rng2.normal(0, 0.45, n2))
    frames.append(_bars(idx2, c2))
    idx3 = pd.date_range("2026-06-04 01:00", "2026-06-04 18:00", freq="1min")
    n3 = len(idx3); rng3 = np.random.default_rng(23)
    c3 = 3400 + np.cumsum(rng3.normal(0, 0.35, n3))
    frames.append(_bars(idx3, c3))
    return pd.concat(frames)


def scen_dark_chop():
    """One flat, low-vol day: mostly dark, few/no fills."""
    idx = pd.date_range("2026-06-05 01:00", "2026-06-05 18:00", freq="1min")
    n = len(idx); rng = np.random.default_rng(31)
    close = 3300 + np.cumsum(rng.normal(0, 0.15, n))
    return _bars(idx, close, wick=0.3)


SCENARIOS = {
    "single_trend": scen_single_trend,
    "multi_day_redgreen": scen_multi_day_redgreen,
    "dark_chop": scen_dark_chop,
}
