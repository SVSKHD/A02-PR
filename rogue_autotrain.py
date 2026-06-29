"""AUREON ROGUE EOD auto-trainer — champion/challenger, fail-SAFE.

Called ONCE from the EOD block (after the dated archive), never per-trade, never mid-session.
It reads the captured Rogue pattern logs, trains a CHALLENGER on a TIME-ORDERED split (the
last ~20% is held out -- NEVER shuffled: trading data has time structure and a random split
leaks the future), scores it against the current CHAMPION (models/rogue_model.pkl) on the
SAME held-out set, and PROMOTES the challenger ONLY IF it beats the champion by a margin AND
is no worse at catching fakeouts. Otherwise the champion is kept.

Guarantees:
  * Champion is NEVER replaced by a worse challenger -- the model can only improve.
  * < MIN_ROWS labeled examples -> skip (log + return); no model is touched.
  * The promoted model is written ATOMICALLY (temp file + os.replace) so a half-written
    model can never be loaded at next boot.
  * Pure-Python logistic regression (standardize + gradient descent): NO sklearn / numpy
    dependency at the EOD path, so a missing ML library can never break EOD or the next boot.
  * Fully guarded: ANY error returns a skip verdict and leaves the champion untouched.

The exported weights dict (feature_order, mean, scale, coef, intercept) is exactly what
rogue_model.RogueModel consumes -- the bot scores it with stdlib alone.
"""
from __future__ import annotations

import csv
import glob
import logging
import math
import os
import pickle

log = logging.getLogger("AUREON")

FEATURE_ORDER = ['range_dollars', 'body_ratio', 'candle_count', 'atr', 'spread',
                 'confirm_dollars', 'time_bucket_code']
_BUCKETS = ['asia', 'london', 'london_ny', 'ny', 'off']

MIN_ROWS = 300
PROMOTE_MARGIN = 0.02     # challenger must beat champion accuracy by > 2%
DEFAULT_THRESHOLD = 0.5   # the score cutoff used for the metrics (matches the gate default)


def _bucket_code(name):
    try:
        return _BUCKETS.index(str(name))
    except Exception:
        return -1


def _row_to_xy(row):
    """A patterns row -> (feature_vector, label, ts) or None if it has no realized outcome.
    Label = 1 if the trade made money (outcome_dollars > 0), else 0."""
    out = str(row.get('outcome_dollars', '')).strip()
    if out == '':
        return None
    try:
        y = 1 if float(out) > 0 else 0
        x = [float(row.get('range_dollars', 0) or 0),
             float(row.get('body_ratio', 0) or 0),
             float(row.get('candle_count', 0) or 0),
             float(row.get('atr', 0) or 0),
             float(row.get('spread', 0) or 0),
             float(row.get('confirm_dollars', 0) or 0),
             float(_bucket_code(row.get('time_bucket', '')))]
    except Exception:
        return None
    return x, y, str(row.get('ts', ''))


def load_examples(run_dir, archive_dir):
    """Load labeled examples from the live patterns CSV + every archived day, de-duplicated
    and TIME-ORDERED (by ts). Returns (X, Y)."""
    paths = []
    live = os.path.join(run_dir, "rogue_patterns.csv")
    if os.path.exists(live):
        paths.append(live)
    if archive_dir:
        paths.extend(sorted(glob.glob(os.path.join(archive_dir, "*", "rogue_patterns.csv"))))
    seen, items = set(), []
    for p in paths:
        try:
            with open(p, newline='') as f:
                for row in csv.DictReader(f):
                    xy = _row_to_xy(row)
                    if xy is None:
                        continue
                    key = (xy[2], str(row.get('entry_price', '')), str(row.get('outcome_dollars', '')))
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(xy)
        except Exception as e:
            log.warning(f"[ROGUE-ML] skip {p}: {e!r}")
    items.sort(key=lambda t: t[2])   # time order
    return [x for x, _, _ in items], [y for _, y, _ in items]


# --- pure-Python standardize + logistic regression (gradient descent) -------------
def _standardize_fit(X):
    n = len(X)
    d = len(X[0])
    mean = [sum(r[j] for r in X) / n for j in range(d)]
    var = [sum((r[j] - mean[j]) ** 2 for r in X) / n for j in range(d)]
    scale = [math.sqrt(v) if v > 1e-12 else 1.0 for v in var]
    return mean, scale


def _standardize_apply(X, mean, scale):
    return [[(r[j] - mean[j]) / scale[j] for j in range(len(mean))] for r in X]


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _fit_logistic(X, Y, *, epochs=400, lr=0.1, l2=1e-4):
    d = len(X[0])
    coef = [0.0] * d
    intercept = 0.0
    n = len(X)
    for _ in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        for i in range(n):
            z = intercept + sum(coef[j] * X[i][j] for j in range(d))
            err = _sigmoid(z) - Y[i]
            for j in range(d):
                gw[j] += err * X[i][j]
            gb += err
        for j in range(d):
            coef[j] -= lr * (gw[j] / n + l2 * coef[j])
        intercept -= lr * (gb / n)
    return coef, intercept


def _predict_proba(x_std, coef, intercept):
    return _sigmoid(intercept + sum(coef[j] * x_std[j] for j in range(len(coef))))


def _metrics(probas, Y, threshold=DEFAULT_THRESHOLD):
    """Return (accuracy, fakeout_recall). fakeout_recall = of the actual losers (y=0), the
    fraction the model would SKIP (proba < threshold) -- i.e. correctly avoided."""
    n = len(Y)
    correct = sum(1 for p, y in zip(probas, Y) if (1 if p >= threshold else 0) == y)
    losers = [p for p, y in zip(probas, Y) if y == 0]
    caught = sum(1 for p in losers if p < threshold)
    fakeout_recall = (caught / len(losers)) if losers else 1.0
    return (correct / n if n else 0.0), fakeout_recall


def decide_promotion(champion, challenger, *, margin=PROMOTE_MARGIN):
    """PURE promotion rule. champion/challenger are dicts {acc, fakeout_recall} (champion may
    be None = no current champion). PROMOTE iff: no champion, OR (challenger beats champion
    accuracy by > margin AND is no worse on fakeout-recall). Returns (promote: bool, reason)."""
    if champion is None:
        return True, "no champion -> promote challenger"
    acc_ok = challenger['acc'] > champion['acc'] + margin
    fr_ok = challenger['fakeout_recall'] >= champion['fakeout_recall']
    if acc_ok and fr_ok:
        return True, (f"challenger {challenger['acc']:.3f} > champion {champion['acc']:.3f}"
                      f"+{margin:.0%} and fakeout-recall not worse -> PROMOTE")
    return False, (f"champion={champion['acc']:.3f} challenger={challenger['acc']:.3f} "
                   f"(fr champ={champion['fakeout_recall']:.2f}/chal={challenger['fakeout_recall']:.2f})"
                   f" -> KEPT champion")


def _atomic_write(weights, out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, 'wb') as f:
        pickle.dump(weights, f)
    os.replace(tmp, out_path)   # atomic


def run(run_dir, *, archive_dir="./logs/archive",
        model_path=os.path.join("models", "rogue_model.pkl"),
        min_rows=MIN_ROWS, margin=PROMOTE_MARGIN, val_frac=0.2):
    """EOD champion/challenger. Returns a verdict dict {action, ...}. NEVER raises."""
    try:
        X, Y = load_examples(run_dir, archive_dir)
        n = len(X)
        if n < min_rows:
            log.info(f"[ROGUE-ML] insufficient data ({n} rows) — skip autotrain.")
            return {'action': 'skip_insufficient', 'rows': n}
        # TIME-ORDERED split: the tail is the validation set (never shuffled).
        cut = max(1, int(n * (1.0 - val_frac)))
        Xtr, Ytr, Xva, Yva = X[:cut], Y[:cut], X[cut:], Y[cut:]
        if len(set(Ytr)) < 2 or len(set(Yva)) < 2:
            log.info("[ROGUE-ML] one-class split — skip autotrain (need wins AND losses "
                     "in both train and validation).")
            return {'action': 'skip_one_class', 'rows': n}

        mean, scale = _standardize_fit(Xtr)
        coef, intercept = _fit_logistic(_standardize_apply(Xtr, mean, scale), Ytr)
        weights = {'feature_order': list(FEATURE_ORDER), 'mean': mean, 'scale': scale,
                   'coef': coef, 'intercept': intercept}

        # challenger metrics on the held-out validation set.
        Xva_std = _standardize_apply(Xva, mean, scale)
        chal_p = [_predict_proba(x, coef, intercept) for x in Xva_std]
        chal_acc, chal_fr = _metrics(chal_p, Yva)
        challenger = {'acc': chal_acc, 'fakeout_recall': chal_fr}

        # champion metrics on the SAME validation set (None if no champion yet).
        champion = None
        if os.path.exists(model_path):
            try:
                import rogue_model as _rm
                cm = _rm.RogueModel().load(model_path)
                if cm.trained:
                    champ_p = [cm.predict({FEATURE_ORDER[j]: Xva[i][j]
                                           for j in range(len(FEATURE_ORDER))})
                               for i in range(len(Xva))]
                    c_acc, c_fr = _metrics(champ_p, Yva)
                    champion = {'acc': c_acc, 'fakeout_recall': c_fr}
            except Exception as e:
                log.warning(f"[ROGUE-ML] champion score failed ({e!r}) — treat as no champion.")
                champion = None

        promote, reason = decide_promotion(champion, challenger, margin=margin)
        champ_acc_str = f"{champion['acc']:.3f}" if champion else "none"
        if promote:
            _atomic_write(weights, model_path)
            log.info(f"[ROGUE-ML] autotrain: champion={champ_acc_str} "
                     f"challenger={chal_acc:.3f} -> PROMOTED challenger ({reason})")
            return {'action': 'promoted', 'rows': n, 'champion_acc': (champion or {}).get('acc'),
                    'challenger_acc': chal_acc, 'reason': reason}
        log.info(f"[ROGUE-ML] autotrain: champion={champ_acc_str} "
                 f"challenger={chal_acc:.3f} -> KEPT champion ({reason})")
        return {'action': 'kept_champion', 'rows': n, 'champion_acc': champion['acc'],
                'challenger_acc': chal_acc, 'reason': reason}
    except Exception as e:
        log.warning(f"[ROGUE-ML] autotrain non-fatal: {e!r} — champion untouched.")
        return {'action': 'error', 'error': repr(e)}
