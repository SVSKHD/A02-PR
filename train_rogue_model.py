#!/usr/bin/env python3
"""AUREON ROGUE model trainer — STANDALONE, run MANUALLY (never at boot).

Reads the captured Rogue pattern logs (run_dir/rogue_patterns.csv + every archived day under
logs/archive/*/rogue_patterns.csv), builds (features -> outcome) examples from the rows that
have a realized outcome_dollars, fits a logistic-regression follow-through classifier with
standardized features, and EXPORTS a plain weights dict to models/rogue_model.pkl.

The exported file is stdlib-pickle of a dict (feature_order, mean, scale, coef, intercept) --
NO sklearn object is pickled, so the bot loads + scores it with stdlib alone (sklearn is a
TRAIN-time dependency only). Prints train/validation accuracy and WARNS loudly if there are
fewer than 300 examples (insufficient data -- do not trust / do not enable the gate yet).

Usage:
    python train_rogue_model.py [--run-dir ./run] [--archive ./logs/archive]
                                [--out models/rogue_model.pkl] [--min-rows 300]
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import pickle
import sys

# the numeric feature vector (must match rogue_model.FEATURE_ORDER).
FEATURE_ORDER = ['range_dollars', 'body_ratio', 'candle_count', 'atr', 'spread',
                 'confirm_dollars', 'time_bucket_code']
_BUCKETS = ['asia', 'london', 'london_ny', 'ny', 'off']

MIN_ROWS_DEFAULT = 300


def _bucket_code(name):
    try:
        return _BUCKETS.index(str(name))
    except Exception:
        return -1


def _row_to_xy(row):
    """A patterns row -> (feature_vector, label) or None if it has no realized outcome.
    Label = 1 if the trade made money (outcome_dollars > 0), else 0."""
    out = str(row.get('outcome_dollars', '')).strip()
    if out == '':
        return None
    try:
        y = 1 if float(out) > 0 else 0
    except Exception:
        return None
    try:
        x = [
            float(row.get('range_dollars', 0) or 0),
            float(row.get('body_ratio', 0) or 0),
            float(row.get('candle_count', 0) or 0),
            float(row.get('atr', 0) or 0),
            float(row.get('spread', 0) or 0),
            float(row.get('confirm_dollars', 0) or 0),
            float(_bucket_code(row.get('time_bucket', ''))),
        ]
    except Exception:
        return None
    return x, y


def load_examples(run_dir, archive_dir):
    paths = []
    live = os.path.join(run_dir, "rogue_patterns.csv")
    if os.path.exists(live):
        paths.append(live)
    paths.extend(sorted(glob.glob(os.path.join(archive_dir, "*", "rogue_patterns.csv"))))
    X, Y, seen = [], [], 0
    for p in paths:
        try:
            with open(p, newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    seen += 1
                    xy = _row_to_xy(row)
                    if xy is not None:
                        X.append(xy[0])
                        Y.append(xy[1])
        except Exception as e:
            print(f"  ! skip {p}: {e!r}")
    print(f"Scanned {len(paths)} file(s), {seen} rows, {len(X)} labeled examples.")
    return X, Y


def train(X, Y, out_path):
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score
    except Exception as e:
        print(f"ERROR: sklearn is required to TRAIN (not to run): {e!r}", file=sys.stderr)
        print("Install with: pip install scikit-learn", file=sys.stderr)
        return False

    if len(set(Y)) < 2:
        print("ERROR: need BOTH win and loss examples to train; only one class present.",
              file=sys.stderr)
        return False

    strat = Y if len(X) >= 10 else None
    Xtr, Xva, Ytr, Yva = train_test_split(X, Y, test_size=0.25, random_state=42,
                                          stratify=strat)
    scaler = StandardScaler().fit(Xtr)
    clf = LogisticRegression(max_iter=1000).fit(scaler.transform(Xtr), Ytr)
    tr_acc = accuracy_score(Ytr, clf.predict(scaler.transform(Xtr)))
    va_acc = accuracy_score(Yva, clf.predict(scaler.transform(Xva)))
    print(f"Train accuracy: {tr_acc:.3f}  |  Validation accuracy: {va_acc:.3f}")

    weights = {
        'feature_order': list(FEATURE_ORDER),
        'mean': [float(m) for m in scaler.mean_],
        'scale': [float(s) for s in scaler.scale_],
        'coef': [float(c) for c in clf.coef_[0]],
        'intercept': float(clf.intercept_[0]),
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, 'wb') as f:
        pickle.dump(weights, f)
    print(f"Wrote weights -> {out_path}")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Train the Rogue follow-through model.")
    ap.add_argument("--run-dir", default="./run")
    ap.add_argument("--archive", default="./logs/archive")
    ap.add_argument("--out", default=os.path.join("models", "rogue_model.pkl"))
    ap.add_argument("--min-rows", type=int, default=MIN_ROWS_DEFAULT)
    args = ap.parse_args(argv)

    X, Y = load_examples(args.run_dir, args.archive)
    if len(X) < args.min_rows:
        print(f"\n⚠️  WARNING: only {len(X)} labeled examples (< {args.min_rows}). "
              f"INSUFFICIENT DATA — the model will be unreliable. Do NOT enable the gate "
              f"(rogue_model_gate_enabled) on this model. Keep collecting.\n")
    if not X:
        print("No labeled examples yet — nothing to train. Exiting.")
        return 1
    ok = train(X, Y, args.out)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
