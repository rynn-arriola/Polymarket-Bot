"""EXPERIMENT 1 (XGBOOST_RESEARCH.md protocol): recency sample-weighting.

HYPOTHESIS (written before running): sports data is non-stationary (esports
metas/patches, roster eras, rule changes). Down-weighting old training rows
by age — weight = 0.5 ** (age_days / half_life) — should let the trees fit
the CURRENT game rather than a historical average, most visibly in the deep-
history esports (dota2 to 2015, cs2 to 2022). Expected effect: small; must
clear BEAT_MARGIN (0.002 Brier vs Elo) to matter.

PROTOCOL COMPLIANCE:
- Selection happens on the VAL slice only (raw val Brier, seed 42), over
  half-life in {None, 730, 365, 180} days. Test is untouched during selection.
- Sports where VAL picks None (no weighting) get NO test read — their Phase-2
  baseline verdict stands unchanged.
- Sports where VAL picks a real half-life get ONE test read: 5 seeds, median
  delta vs Elo must exceed BEAT_MARGIN with no materially negative seed.
- Hyperparameters frozen (train_xgb.xgb defaults). Results go to the
  test-read ledger in XGBOOST_RESEARCH.md.

Usage: python exp_recency.py [sport ...] [--offline]   (default: all 10)
"""

import sys
from datetime import date

import numpy as np
import xgboost as xgb

import train_xgb as t

HALF_LIVES = (None, 730, 365, 180)   # days; None = unweighted baseline
SEEDS = (42, 43, 44, 45, 46)

XGB_PARAMS = {
    "objective": "binary:logistic", "eval_metric": "logloss",
    "max_depth": 3, "eta": 0.03, "subsample": 0.8, "colsample_bytree": 0.8,
    "min_child_weight": 20, "lambda": 2.0,
}


def ages_days(dates: list[str]) -> np.ndarray:
    """Row age in days relative to the newest row (bad/missing dates get the
    median age — neutral, never an extreme weight)."""
    parsed = []
    for d in dates:
        try:
            parsed.append(date.fromisoformat(str(d)[:10]).toordinal())
        except ValueError:
            parsed.append(None)
    known = [p for p in parsed if p is not None]
    newest = max(known)
    med = float(np.median([newest - p for p in known]))
    return np.array([(newest - p) if p is not None else med for p in parsed], float)


def train_once(Xtr, ytr, wtr, Xva, yva, feats, seed):
    dtr = xgb.DMatrix(Xtr, label=ytr, weight=wtr, feature_names=feats)
    dva = xgb.DMatrix(Xva, label=yva, feature_names=feats)
    bst = xgb.train({**XGB_PARAMS, "seed": seed}, dtr, num_boost_round=3000,
                    evals=[(dva, "val")], early_stopping_rounds=60, verbose_eval=False)
    return bst, (0, bst.best_iteration + 1)


def run(sport: str):
    M, y, feats, dates = t.load_matrix(sport)
    n = len(y)
    tr, va = int(n * t.TRAIN_FRAC), int(n * (t.TRAIN_FRAC + t.VAL_FRAC))
    Xtr, ytr = M[:tr], y[:tr]
    Xva, yva = M[tr:va], y[tr:va]
    Xte, yte = M[va:], y[va:]
    age_tr = ages_days(dates[:tr])
    elo_va = M[tr:va][:, feats.index("elo_exp")]
    elo_te = M[va:][:, feats.index("elo_exp")]

    print(f"\n{sport.upper()}: {n} rows | Elo val Brier {t.brier(elo_va, yva):.4f} | "
          f"train-age span {age_tr.max():.0f}d")

    # --- selection on VAL only (seed 42) ---
    val_scores = {}
    for hl in HALF_LIVES:
        w = np.ones(len(ytr)) if hl is None else 0.5 ** (age_tr / hl)
        bst, rng = train_once(Xtr, ytr, w, Xva, yva, feats, seed=42)
        val_scores[hl] = t.brier(bst.predict(dva_cache(Xva, feats), iteration_range=rng), yva)
        print(f"  hl={str(hl):>5}: val Brier {val_scores[hl]:.4f}")
    best_hl = min(val_scores, key=val_scores.get)
    if best_hl is None:
        print(f"  -> VAL picks NO weighting: recency adds nothing for {sport}; "
              f"Phase-2 baseline verdict stands (no test read).")
        return sport, None, None, None

    # --- ONE test read: chosen half-life, 5 seeds ---
    w = 0.5 ** (age_tr / best_hl)
    deltas, briers = [], []
    elo_b = t.brier(elo_te, yte)
    for seed in SEEDS:
        bst, rng = train_once(Xtr, ytr, w, Xva, yva, feats, seed)
        raw_va = bst.predict(dva_cache(Xva, feats), iteration_range=rng)
        a, b = t.fit_platt(raw_va, yva)
        pred_te = t.apply_platt(bst.predict(dva_cache(Xte, feats), iteration_range=rng), a, b)
        bb = t.brier(pred_te, yte)
        briers.append(bb)
        deltas.append(elo_b - bb)
    med = float(np.median(deltas))
    beats = med > t.BEAT_MARGIN and min(deltas) > 0
    print(f"  TEST (hl={best_hl}, 5 seeds): Elo {elo_b:.4f} vs XGB "
          f"{np.median(briers):.4f} (range {min(briers):.4f}-{max(briers):.4f})")
    print(f"  -> median delta {med:+.4f} | {'CLEARS the gate' if beats else 'does NOT clear the gate'}")
    return sport, best_hl, med, beats


_dm_cache: dict = {}


def dva_cache(X, feats):
    key = (id(X), X.shape)
    if key not in _dm_cache:
        _dm_cache[key] = xgb.DMatrix(X, feature_names=feats)
    return _dm_cache[key]


if __name__ == "__main__":
    sports = [s for s in sys.argv[1:] if not s.startswith("--")] or \
             ["dota2", "cs2", "lol", "valorant", "mlb", "nba", "wnba", "atp", "wta", "fwc"]
    results = []
    for sp in sports:
        _dm_cache.clear()
        try:
            results.append(run(sp))
        except Exception as e:
            print(f"{sp}: FAILED ({e})")
    print("\n" + "=" * 60)
    print("EXPERIMENT 1 SUMMARY (recency weighting)")
    for sport, hl, med, beats in results:
        if hl is None:
            print(f"  {sport:<9} val picked no weighting — baseline stands")
        else:
            print(f"  {sport:<9} hl={hl}d  median delta {med:+.4f}  "
                  f"{'*** CLEARS GATE ***' if beats else 'under margin'}")
