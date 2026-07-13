"""EXPERIMENT 2 (XGBOOST_RESEARCH.md protocol): LoL player-aggregate features.

HYPOTHESIS (pre-registered): lineup-level player ratings carry roster
information team-name Elo cannot see — the one signal historically PROVEN
orthogonal here (player-Elo beat team-Elo, Brier 0.2253 vs 0.2300, 2023 OE).
Feeding walk-forward player aggregates ALONGSIDE team-Elo should let the
trees exploit roster context and beat BOTH single models. This is the
strongest gate-clear candidate of all planned experiments.

FEATURE SET (fixed up front — no iteration/fishing; diffs preferred per P7,
NaN when a lineup has <3 rated players per P6):
  team side : elo_exp, elo_gap, rating_a, rating_b, games_a, games_b
  player side: p_exp (player-Elo prob), p_gap (mean lineup rating diff),
               p_min_gap (weakest-rated-player diff), p_spread_diff,
               p_experience_diff (mean games played per lineup, diff)

BASELINES on the identical test rows:
  - team Elo (elo_exp)  — the harness gate target (BEAT_MARGIN 0.002)
  - player Elo (p_exp)  — the honest incumbent: live LoL uses player-Elo
    when its sidecar is fresh, so a model that beats team but not player
    Elo adds nothing deployable.

Data: Oracle's Elixir per-game CSVs in data/oe (local, through 2025-12-29).
Trains on the OE game universe (all leagues, per-game) — same scope the
original player-vs-team validation used.

Usage: python exp_lol_players.py
"""

import math

import numpy as np
import xgboost as xgb

import train_xgb as t
from elo import lol_players
from elo.engine import EloEngine

TEAM_K = 72.0        # tuned live value (params lol)
PLAYER_K = 48.0      # backtest-winning value (lol_players)
MIN_TEAM_GAMES = 8   # same row gate as the esports extractor
MIN_KNOWN = 3        # <3 rated players per lineup -> player features are NaN

FEATS = ["elo_exp", "elo_gap", "rating_a", "rating_b", "games_a", "games_b",
         "p_exp", "p_gap", "p_min_gap", "p_spread_diff", "p_experience_diff"]

SEEDS = (42, 43, 44, 45, 46)
NAN = float("nan")


def extract(games: list[dict]):
    """Dual walk-forward (team Elo + player ratings) over the same stream,
    emitting rows strictly pre-game. Alphabetical order, predicts P(first)."""
    team = EloEngine(k_factor=TEAM_K)
    ratings: dict[str, float] = {}
    played: dict[str, int] = {}
    X, y, dates = [], [], []
    for g in games:
        (t1, p1), (t2, p2) = sorted(g["teams"].items())
        if team.games(t1) >= MIN_TEAM_GAMES and team.games(t2) >= MIN_TEAM_GAMES:
            row = {
                "elo_exp": team.probability(t1, t2),
                "elo_gap": team.get_rating(t1) - team.get_rating(t2),
                "rating_a": team.get_rating(t1), "rating_b": team.get_rating(t2),
                "games_a": team.games(t1), "games_b": team.games(t2),
                "p_exp": NAN, "p_gap": NAN, "p_min_gap": NAN,
                "p_spread_diff": NAN, "p_experience_diff": NAN,
            }
            k1 = [ratings[x] for x in p1 if x in ratings]
            k2 = [ratings[x] for x in p2 if x in ratings]
            if len(k1) >= MIN_KNOWN and len(k2) >= MIN_KNOWN:
                m1, m2 = sum(k1) / len(k1), sum(k2) / len(k2)
                row["p_exp"] = 1.0 / (1.0 + 10 ** (-(m1 - m2) / 400.0))
                row["p_gap"] = m1 - m2
                row["p_min_gap"] = min(k1) - min(k2)
                sd = lambda v, m: math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
                row["p_spread_diff"] = sd(k1, m1) - sd(k2, m2)
                e1 = sum(played.get(x, 0) for x in p1) / len(p1)
                e2 = sum(played.get(x, 0) for x in p2) / len(p2)
                row["p_experience_diff"] = e1 - e2
            X.append(row)
            y.append(1.0 if g["winner"] == t1 else 0.0)
            dates.append(g["date"])
        # updates (after prediction)
        actual1 = 1.0 if g["winner"] == t1 else 0.0
        team.record_result(t1, t2, actual1)
        r1 = sum(ratings.get(x, 1500.0) for x in p1) / len(p1)
        r2 = sum(ratings.get(x, 1500.0) for x in p2) / len(p2)
        exp1 = 1.0 / (1.0 + 10 ** ((r2 - r1) / 400.0))
        delta = PLAYER_K * (actual1 - exp1)
        for x in p1:
            ratings[x] = ratings.get(x, 1500.0) + delta
            played[x] = played.get(x, 0) + 1
        for x in p2:
            ratings[x] = ratings.get(x, 1500.0) - delta
            played[x] = played.get(x, 0) + 1
    return X, y, dates


def main():
    games = lol_players.load_oe_games("data/oe")
    if not games:
        raise SystemExit("no OE data in data/oe — fetch_oe.py first")
    X, y, dates = extract(games)
    M = np.array([[row[c] for c in FEATS] for row in X], dtype=float)
    y = np.asarray(y, float)
    n = len(y)
    tr, va = int(n * t.TRAIN_FRAC), int(n * (t.TRAIN_FRAC + t.VAL_FRAC))
    print(f"LOL players: {len(games)} OE games -> {n} rows "
          f"({dates[0][:10]}..{dates[-1][:10]}) | train {tr} / val {va-tr} / test {n-va}")
    pk = np.isnan(M[:, FEATS.index('p_exp')])
    print(f"player features known on {100*(1-pk.mean()):.1f}% of rows (NaN elsewhere)")

    Xtr, ytr, Xva, yva, Xte, yte = M[:tr], y[:tr], M[tr:va], y[tr:va], M[va:], y[va:]
    dva = xgb.DMatrix(Xva, feature_names=FEATS)
    dte = xgb.DMatrix(Xte, feature_names=FEATS)

    def briers_on(col, Xs, ys):
        v = Xs[:, FEATS.index(col)]
        m = ~np.isnan(v)
        return t.brier(v[m], ys[m]), int(m.sum())

    # Baselines on test (computed once, reported at the end)
    team_te, _ = briers_on("elo_exp", Xte, yte)
    player_te, player_n = briers_on("p_exp", Xte, yte)

    print("\n--- VAL sanity (seed 42) ---")
    team_va, _ = briers_on("elo_exp", Xva, yva)
    player_va, _ = briers_on("p_exp", Xva, yva)
    dtr = xgb.DMatrix(Xtr, label=ytr, feature_names=FEATS)
    dva_l = xgb.DMatrix(Xva, label=yva, feature_names=FEATS)
    params = {"objective": "binary:logistic", "eval_metric": "logloss",
              "max_depth": 3, "eta": 0.03, "subsample": 0.8, "colsample_bytree": 0.8,
              "min_child_weight": 20, "lambda": 2.0, "seed": 42}
    bst = xgb.train(params, dtr, num_boost_round=3000,
                    evals=[(dva_l, "val")], early_stopping_rounds=60, verbose_eval=False)
    rng = (0, bst.best_iteration + 1)
    xgb_va = t.brier(bst.predict(dva, iteration_range=rng), yva)
    print(f"val Brier: team Elo {team_va:.4f} | player Elo {player_va:.4f} | XGB {xgb_va:.4f}")
    imp = sorted(bst.get_score(importance_type="gain").items(), key=lambda kv: -kv[1])
    print("feature importance (gain): " + ", ".join(f"{k}={v:.0f}" for k, v in imp[:6]))

    if xgb_va >= min(team_va, player_va):
        print("\n-> VAL shows no advantage over the best single model — "
              "NO test read; verdict: player features don't add on top (ledger it).")
        return

    print("\n--- ONE test read (5 seeds) ---")
    deltas_team, deltas_player, tb = [], [], []
    for seed in SEEDS:
        bst = xgb.train({**params, "seed": seed}, dtr, num_boost_round=3000,
                        evals=[(dva_l, "val")], early_stopping_rounds=60, verbose_eval=False)
        rng = (0, bst.best_iteration + 1)
        raw_va = bst.predict(dva, iteration_range=rng)
        a, b = t.fit_platt(raw_va, yva)
        pred = t.apply_platt(bst.predict(dte, iteration_range=rng), a, b)
        bb = t.brier(pred, yte)
        tb.append(bb)
        deltas_team.append(team_te - bb)
        deltas_player.append(player_te - bb)
    med_t, med_p = float(np.median(deltas_team)), float(np.median(deltas_player))
    print(f"test Brier: team Elo {team_te:.4f} | player Elo {player_te:.4f} (n={player_n}) | "
          f"XGB median {np.median(tb):.4f} (range {min(tb):.4f}-{max(tb):.4f})")
    print(f"median delta vs team Elo   {med_t:+.4f}  "
          f"{'CLEARS gate' if med_t > t.BEAT_MARGIN and min(deltas_team) > 0 else 'under margin'}")
    print(f"median delta vs player Elo {med_p:+.4f}  "
          f"{'BEATS the real incumbent' if med_p > t.BEAT_MARGIN and min(deltas_player) > 0 else 'does not beat the incumbent'}")


if __name__ == "__main__":
    main()
