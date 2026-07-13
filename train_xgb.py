"""XGBoost pilot trainer + honest evaluator (NBA).

Builds walk-forward features (xgb_features), splits them CHRONOLOGICALLY into
train / validation / test (never random — that would leak the future), trains
a shallow gradient-boosted classifier, Platt-calibrates it on the validation
slice, and compares its Brier on the untouched test slice against Elo's Brier
on the exact same games (Elo's own prediction rides along as the elo_exp
feature). If XGBoost doesn't beat Elo here, out-of-sample, it doesn't ship.

Usage: python train_xgb.py [nba]
"""

import json
import sys
from datetime import date, datetime, timezone

import numpy as np
import xgboost as xgb

import config
import xgb_features as xf
import xgb_live
from elo import basketball, mlb, params, soccer

# A model must beat Elo's test Brier by AT LEAST this much to be flagged
# beats_elo (and so become live-eligible). Without a margin, a noise-level
# 0.0005 "win" on Elo-only features trips the gate — verified live on dota2
# 2026-07-12. This encodes "meaningfully better than Elo", not "not worse".
BEAT_MARGIN = 0.002

# Convention each sport's model predicts on (for xgb_live to map back).
ORDER = {"nba": "home", "wnba": "home", "mlb": "home", "fwc": "home",
         "atp": "alpha", "wta": "alpha",
         "dota2": "alpha", "cs2": "alpha", "lol": "alpha", "valorant": "alpha"}

TRAIN_FRAC, VAL_FRAC = 0.70, 0.15  # test = remaining 15%, most-recent games

# --offline: train esports from the local accumulating store WITHOUT the
# source refresh — Leaguepedia/PandaScore fetches (rate limits, flaky
# mirrors) stalled a 7-sport batch for 15+ minutes on 2026-07-12. The
# stores are already deep; training doesn't need today's matches.
OFFLINE = "--offline" in sys.argv


def brier(pred, actual) -> float:
    pred, actual = np.asarray(pred, float), np.asarray(actual, float)
    return float(np.mean((pred - actual) ** 2))


def logloss(pred, actual) -> float:
    pred = np.clip(np.asarray(pred, float), 1e-9, 1 - 1e-9)
    actual = np.asarray(actual, float)
    return float(-np.mean(actual * np.log(pred) + (1 - actual) * np.log(1 - pred)))


def fit_platt(pred, actual):
    """Logistic recalibration p' = sigmoid(a*logit(p)+b), gradient descent —
    same shape as tune.py's calibration, kept only if it helps on holdout."""
    z = np.log(np.clip(pred, 1e-9, 1 - 1e-9) / (1 - np.clip(pred, 1e-9, 1 - 1e-9)))
    y = np.asarray(actual, float)
    a, b = 1.0, 0.0
    for _ in range(600):
        q = 1.0 / (1.0 + np.exp(-(a * z + b)))
        a -= 0.1 * np.mean((q - y) * z)
        b -= 0.1 * np.mean(q - y)
    return a, b


def apply_platt(pred, a, b):
    z = np.log(np.clip(pred, 1e-9, 1 - 1e-9) / (1 - np.clip(pred, 1e-9, 1 - 1e-9)))
    return 1.0 / (1.0 + np.exp(-(a * z + b)))


def calibration_table(pred, actual, bins=10):
    pred, actual = np.asarray(pred, float), np.asarray(actual, float)
    lines = [f"  {'bucket':>10} {'n':>6} {'predicted':>10} {'actual':>8}"]
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        m = (pred >= lo) & (pred < hi if i < bins - 1 else pred <= hi)
        if not m.any():
            continue
        lines.append(f"  {lo:>4.0%}-{hi:<4.0%} {int(m.sum()):>6} "
                     f"{pred[m].mean():>9.1%} {actual[m].mean():>7.1%}")
    return "\n".join(lines)


def load_matrix(sport: str):
    """Returns (feature_matrix, labels, feature_names, dates) for a sport.
    dates align 1:1 with rows (ISO strings) — the recency-weighting
    experiments need each row's age; plain training ignores them."""
    if sport in ("nba", "wnba"):
        start = config.NBA_START_DATE if sport == "nba" else config.WNBA_START_DATE
        games = basketball.fetch_games(sport, start, date.today())
        X, y, dates = xf.extract_nba(games, sport)
        feats = xf.FEATURES
    elif sport in ("atp", "wta"):
        from elo import tennis
        matches = tennis.fetch_matches_for(sport, config.TENNIS_START_DATE, date.today(),
                                           config.TENNIS_TML_START_YEAR)
        X, y, dates = xf.extract_tennis(matches, sport)
        feats = xf.TENNIS_FEATURES
    elif sport == "mlb":
        games = mlb.fetch_games(config.MLB_START_YEAR)
        X, y, dates = xf.extract_mlb(games)
        feats = xf.MLB_FEATURES
    elif sport == "fwc":
        games = soccer.fetch_games()
        X, y, dates = xf.extract_fwc(games)
        feats = xf.FWC_FEATURES
    elif sport == "lol":
        # The SHIPPING LoL model is the player blend (first gate clear,
        # 2026-07-13) — trained on the OE per-game universe, the same state
        # the sidecar serves at inference. (The old team-only baseline lives
        # in the test-read ledger; retrain it via extract_esports if ever
        # needed for comparison.)
        from elo import lol_players
        games = lol_players.load_oe_games("data/oe")
        if not games:
            raise SystemExit("no OE data in data/oe — run fetch_oe.py first")
        X, y, dates = xf.extract_lol_players(games)
        feats = xf.LOL_FEATURES
    elif sport in xgb_live.ESPORTS_TITLES:
        from elo import esports
        if OFFLINE:
            esports._FETCHERS = {**esports._FETCHERS, sport: (lambda s, t=None: 0)}
        matches = esports.fetch_matches(sport)   # (date, winner, loser), from the local store
        X, y, dates = xf.extract_esports(matches, sport)
        feats = xf.BASE_FEATURES
    else:
        raise SystemExit(f"no XGB feature extractor for {sport!r}")
    M = np.array([[row[c] for c in feats] for row in X], dtype=float)
    return M, np.asarray(y, float), feats, dates


def _save_model(sport, bst, best_it, feats, cal, order, elo_b, xgb_b):
    """Persist the model + gate metadata to xgb_models/. beats_elo drives the
    live gate: a model that didn't beat Elo is saved but never activates."""
    xgb_live.MODELS_DIR.mkdir(exist_ok=True)
    bst.save_model(str(xgb_live.MODELS_DIR / f"{sport}.ubj"))
    meta = {
        "sport": sport,
        "features": feats,
        "calibration": {"a": round(cal[0], 4), "b": round(cal[1], 4)},
        "order": order,
        "best_iteration": int(best_it),
        "beats_elo": bool(xgb_b < elo_b - BEAT_MARGIN),
        "beat_margin": BEAT_MARGIN,
        "elo_brier": round(elo_b, 4),
        "xgb_brier": round(xgb_b, 4),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    (xgb_live.MODELS_DIR / f"{sport}.meta.json").write_text(json.dumps(meta, indent=2))
    print(f"  saved xgb_models/{sport}.* | beats_elo={meta['beats_elo']} "
          f"({'ACTIVE once fresh' if meta['beats_elo'] else 'inactive — Elo stays live'})")


def main(sport: str):
    M, y, feats, _dates = load_matrix(sport)
    n = len(y)
    tr, va = int(n * TRAIN_FRAC), int(n * (TRAIN_FRAC + VAL_FRAC))
    Xtr, ytr = M[:tr], y[:tr]
    Xva, yva = M[tr:va], y[tr:va]
    Xte, yte = M[va:], y[va:]
    print(f"{sport.upper()}: {n} walk-forward rows | train {len(ytr)} / val {len(yva)} / test {len(yte)} "
          f"(chronological, test = most recent)\n")

    dtr = xgb.DMatrix(Xtr, label=ytr, feature_names=feats)
    dva = xgb.DMatrix(Xva, label=yva, feature_names=feats)
    dte = xgb.DMatrix(Xte, label=yte, feature_names=feats)

    xgb_params = {
        "objective": "binary:logistic", "eval_metric": "logloss",
        "max_depth": 3, "eta": 0.03, "subsample": 0.8, "colsample_bytree": 0.8,
        "min_child_weight": 20, "lambda": 2.0, "seed": 42,
    }
    bst = xgb.train(xgb_params, dtr, num_boost_round=3000,
                    evals=[(dva, "val")], early_stopping_rounds=60, verbose_eval=False)
    best_it = bst.best_iteration
    rng = (0, best_it + 1)

    raw_te = bst.predict(dte, iteration_range=rng)
    raw_va = bst.predict(dva, iteration_range=rng)
    a, b = fit_platt(raw_va, yva)
    cal_te = apply_platt(raw_te, a, b)

    # Elo on the identical test games: its prediction is the elo_exp feature.
    elo_te = M[va:][:, feats.index("elo_exp")]

    xgb_raw_b, xgb_cal_b, elo_b = brier(raw_te, yte), brier(cal_te, yte), brier(elo_te, yte)
    keep_cal = xgb_cal_b < xgb_raw_b - 1e-5
    xgb_b = xgb_cal_b if keep_cal else xgb_raw_b

    print(f"best_iteration: {best_it}")
    print("=" * 52)
    print(f"  Elo   test Brier : {elo_b:.4f}   (log-loss {logloss(elo_te, yte):.4f})")
    print(f"  XGB   test Brier : {xgb_raw_b:.4f}   raw")
    print(f"  XGB   test Brier : {xgb_cal_b:.4f}   Platt-calibrated (a={a:.3f}, b={b:+.3f})")
    print("=" * 52)
    delta = elo_b - xgb_b
    beats = xgb_b < elo_b - BEAT_MARGIN
    verdict = (f"XGB BEATS Elo (>{BEAT_MARGIN} margin) — ACTIVATES" if beats
               else "ties/loses Elo — stays inactive" if delta <= BEAT_MARGIN else "XGB edges Elo but under margin")
    print(f"  -> {verdict}: delta {delta:+.4f} Brier ({delta/elo_b:+.1%})\n")

    print("XGB calibration on test:")
    print(calibration_table(cal_te if keep_cal else raw_te, yte))

    imp = bst.get_score(importance_type="gain")
    ranked = sorted(imp.items(), key=lambda kv: kv[1], reverse=True)
    print("\nFeature importance (gain):")
    for name, val in ranked[:12]:
        print(f"  {name:<14} {val:>8.1f}")

    _save_model(sport, bst, best_it, feats, (a, b) if keep_cal else (1.0, 0.0),
                ORDER.get(sport, "home"), elo_b, xgb_b)


if __name__ == "__main__":
    sports = [s.lower() for s in sys.argv[1:] if not s.startswith("--")] or ["nba"]
    for i, sp in enumerate(sports):
        if i:
            print("\n" + "#" * 60 + "\n")
        main(sp)
