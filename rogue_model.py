"""AUREON ROGUE model interface — the confidence gate's brain (CPU, in-process, no net).

RogueModel scores a Rogue setup's follow-through probability in [0,1]. The DESIGN GOAL
is freeze-safety + fail-open:

  * UNTRAINED (no weights file) -> predict() returns 1.0 (always confirm). The gate is a
    pure pass-through; live behavior is unchanged until a model is dropped in.
  * predict() error of ANY kind -> returns 1.0 (FAIL OPEN) + a loud log. A model bug can
    never silently kill Rogue.

No runtime ML dependency: train_rogue_model.py (which DOES use sklearn) exports a plain
weights dict (standardizer + logistic coef/intercept), and predict() applies a pure-Python
standardize -> sigmoid. So the bot loads the model with stdlib pickle alone -- sklearn is
needed only to TRAIN, never to RUN.
"""
from __future__ import annotations

import logging
import math
import os
import pickle

log = logging.getLogger("AUREON")

# the numeric feature vector the model consumes, in a FIXED order (the trainer writes the
# same order into the weights dict; predict() honors whatever the file says).
FEATURE_ORDER = ['range_dollars', 'body_ratio', 'candle_count', 'atr', 'spread',
                 'confirm_dollars', 'time_bucket_code']


def default_model_path():
    return os.path.join("models", "rogue_model.pkl")


class RogueModel:
    """Load-once, predict-many. Stateless after load(). Never raises out of predict()."""

    def __init__(self):
        self.trained = False
        self.feature_order = list(FEATURE_ORDER)
        self.mean = None
        self.scale = None
        self.coef = None
        self.intercept = 0.0
        self.path = None

    def load(self, path=None):
        """Load weights from `path` if present. Absent/unreadable -> trained=False (the
        pass-through). Never raises."""
        self.path = path or default_model_path()
        try:
            if not self.path or not os.path.exists(self.path):
                self.trained = False
                log.info(f"[ROGUE] no model at {self.path} -> gate is PASS-THROUGH (score=1.0).")
                return self
            with open(self.path, 'rb') as f:
                d = pickle.load(f)
            self.feature_order = list(d['feature_order'])
            self.mean = [float(x) for x in d['mean']]
            self.scale = [float(x) for x in d['scale']]
            self.coef = [float(x) for x in d['coef']]
            self.intercept = float(d['intercept'])
            self.trained = True
            log.info(f"[ROGUE] model loaded from {self.path} "
                     f"({len(self.feature_order)} features).")
        except Exception as e:
            self.trained = False
            log.warning(f"[ROGUE] model load failed ({e!r}) -> PASS-THROUGH (score=1.0).")
        return self

    def predict(self, features):
        """Return follow-through confidence in [0,1]. UNTRAINED -> 1.0. Any error -> 1.0
        (FAIL OPEN) + a loud log: a model fault must never block/kill Rogue silently."""
        try:
            if not self.trained:
                return 1.0
            z = float(self.intercept)
            for i, name in enumerate(self.feature_order):
                v = float(features.get(name, 0.0))
                mu = self.mean[i] if self.mean else 0.0
                sd = self.scale[i] if (self.scale and self.scale[i]) else 1.0
                z += self.coef[i] * ((v - mu) / sd)
            # numerically safe sigmoid
            if z >= 0:
                return 1.0 / (1.0 + math.exp(-z))
            ez = math.exp(z)
            return ez / (1.0 + ez)
        except Exception as e:
            log.error(f"[ROGUE] model predict FAILED ({e!r}) -> FAILING OPEN (score=1.0); "
                      f"Rogue is NOT killed.")
            return 1.0


# process-wide singleton so the file is read once at boot, not per-tick.
_SINGLETON = None


def get_model(path=None):
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = RogueModel().load(path)
    return _SINGLETON


def reset_singleton():
    """Test helper: force the next get_model() to reload."""
    global _SINGLETON
    _SINGLETON = None
