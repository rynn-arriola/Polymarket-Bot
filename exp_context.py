"""EXPERIMENT 3 (XGBOOST_RESEARCH.md protocol): esports match-context features.

HYPOTHESIS (pre-registered): three Elo-blind, match-level context signals —
  * bo_format  — a Bo1 is far higher-variance than a Bo5: the correct
                 probability sits closer to 50% in Bo1 and sharper in Bo5,
                 an effect Elo's single rating gap cannot express;
  * tier_rank  — tryhard/stakes differ across tournament tiers (s..d);
  * fatigue_diff — matches played in the trailing 7 days (tournament grind),
                 difference form per P7.
Expected effect: modest; bo_format is the strongest candidate. Gate: median
5-seed test delta vs Elo > BEAT_MARGIN (0.002).

FEATURES (fixed up front): BASE_FEATURES + [bo_format, tier_rank, fatigue_diff]
NaN discipline (P6): missing bo/tier -> NaN (e.g. cs2's pre-enrichment third).

Usage: python exp_context.py [dota2 cs2 valorant]
"""

import sys
from datetime import date, timedelta

import numpy as np
import xgboost as xgb

import train_xgb as t
from elo import esports, params
from elo.engine import EloEngine

CTX_FEATS = ["elo_exp", "elo_gap", "rating_a", "rating_b", "games_a", "games_b",
             "bo_format", "tier_rank", "fatigue_diff"]
TIER_RANK = {"s": 4.0, "a": 3.0, "b": 2.0, "c": 1.0, "d": 0.0}
SEEDS = (42, 43, 44, 45, 46)
NAN = float("nan")

XGB_PARAMS = {"objective": "binary:logistic", "eval_metric": "logloss",
              "max_depth": 3, "eta": 0.03, "subsample": 0.8, "colsample_bytree": 0.8,
              "min_child_weight": 20, "lambda": 2.0}


def extract(title: str):
    """Walk-forward: same team-Elo walk as the baseline extractor, plus the
    context columns from the enriched store. Alphabetical, P(first wins)."""
    p = params.get(title)
    engine = EloEngine(k_factor=p["k"])
    recent: dict[str, list] = {}   # team -> recent game dates (fatigue window)
    X, y = [], []
    for d, winner, loser, bo, tier in esports.matches_with_context(title):
        try:
            gd = date.fromisoformat(str(d)[:10])
        except ValueError:
            gd = None
        if engine.games(winner) >= p["min_games"] and engine.games(loser) >= p["min_games"]:
            a, b = sorted((winner, loser))
            row = {
                "elo_exp": engine.probability(a, b),
                "elo_gap": engine.get_rating(a) - engine.get_rating(b),
                "rating_a": engine.get_rating(a), "rating_b": engine.get_rating(b),
                "games_a": engine.games(a), "games_b": engine.games(b),
                "bo_format": float(bo) if bo else NAN,
                "tier_rank": TIER_RANK.get(tier, NAN) if tier else NAN,
                "fatigue_diff": NAN,
            }
            if gd is not None:
                cutoff = gd - timedelta(days=7)
                fa = sum(1 for x in recent.get(a, []) if x >= cutoff)
                fb = sum(1 for x in recent.get(b, []) if x >= cutoff)
                row["fatigue_diff"] = float(fa - fb)
            X.append(row)
            y.append(1 if a == winner else 0)
        engine.record_result(winner, loser, 1.0)
        if gd is not None:
            for team in (winner, loser):
                lst = recent.setdefault(team, [])
                lst.append(gd)
                if len(lst) > 40:
                    del lst[:-40]
    return X, y


def run(title: str):
    X, y = extract(title)
    M = np.array([[row[c] for c in CTX_FEATS] for row in X], dtype=float)
    y = np.asarray(y, float)
    n = len(y)
    tr, va = int(n * t.TRAIN_FRAC), int(n * (t.TRAIN_FRAC + t.VAL_FRAC))
    Xtr, ytr, Xva, yva, Xte, yte = M[:tr], y[:tr], M[tr:va], y[tr:va], M[va:], y[va:]
    known = 100 * (1 - np.isnan(M[:, CTX_FEATS.index("bo_format")]).mean())
    elo_va = t.brier(Xva[:, 0], yva)
    elo_te = t.brier(Xte[:, 0], yte)
    print(f"\n{title.upper()}: {n} rows | bo/tier known {known:.0f}% | Elo val {elo_va:.4f}")

    dtr = xgb.DMatrix(Xtr, label=ytr, feature_names=CTX_FEATS)
    dva = xgb.DMatrix(Xva, label=yva, feature_names=CTX_FEATS)
    dte = xgb.DMatrix(Xte, feature_names=CTX_FEATS)

    # VAL sanity first (seed 42): context must beat Elo on val to earn a test read
    bst = xgb.train({**XGB_PARAMS, "seed": 42}, dtr, num_boost_round=3000,
                    evals=[(dva, "val")], early_stopping_rounds=60, verbose_eval=False)
    rng = (0, bst.best_iteration + 1)
    xgb_va = t.brier(bst.predict(xgb.DMatrix(Xva, feature_names=CTX_FEATS),
                                 iteration_range=rng), yva)
    imp = sorted(bst.get_score(importance_type="gain").items(), key=lambda kv: -kv[1])
    print(f"  val: Elo {elo_va:.4f} vs XGB+context {xgb_va:.4f} | "
          f"importance: {', '.join(f'{k}={v:.0f}' for k, v in imp[:5])}")
    if xgb_va >= elo_va:
        print(f"  -> no val advantage: context features add nothing for {title} "
              f"(NO test read; ledger the negative).")
        return title, None, False

    deltas = []
    briers = []
    for seed in SEEDS:
        bst = xgb.train({**XGB_PARAMS, "seed": seed}, dtr, num_boost_round=3000,
                        evals=[(dva, "val")], early_stopping_rounds=60, verbose_eval=False)
        rng = (0, bst.best_iteration + 1)
        raw_va = bst.predict(xgb.DMatrix(Xva, feature_names=CTX_FEATS), iteration_range=rng)
        a, b = t.fit_platt(raw_va, yva)
        pred = t.apply_platt(bst.predict(dte, iteration_range=rng), a, b)
        bb = t.brier(pred, yte)
        briers.append(bb)
        deltas.append(elo_te - bb)
    med = float(np.median(deltas))
    beats = med > t.BEAT_MARGIN and min(deltas) > 0
    print(f"  TEST (5 seeds): Elo {elo_te:.4f} vs XGB {np.median(briers):.4f} "
          f"(range {min(briers):.4f}-{max(briers):.4f})")
    print(f"  -> median delta {med:+.4f} | {'CLEARS the gate' if beats else 'does NOT clear'}")
    return title, med, beats


if __name__ == "__main__":
    titles = [s for s in sys.argv[1:] if not s.startswith("--")] or ["dota2", "valorant", "cs2"]
    results = [run(x) for x in titles]
    print("\n" + "=" * 60 + "\nEXPERIMENT 3 SUMMARY (context features)")
    for title, med, beats in results:
        if med is None:
            print(f"  {title:<9} no val advantage — negative, no test read")
        else:
            print(f"  {title:<9} median delta {med:+.4f}  "
                  f"{'*** CLEARS GATE ***' if beats else 'under margin'}")
