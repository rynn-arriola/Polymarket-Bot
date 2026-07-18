"""Research-only XGBoost blend for esports team and player ratings.

This reuses the fixed feature set and protocol that produced the live LoL
blend. Player K must be selected on validation before this experiment runs.
Validation must beat both single-model baselines before the untouched test
slice is read. Nothing in the live prediction path imports this module.

Usage: python exp_esports_players.py valorant --player-k 32
"""

import argparse
import math

import numpy as np
import xgboost as xgb

import train_xgb as training
from elo import esports_players, params
from elo.engine import EloEngine

FEATURES = ["elo_exp", "elo_gap", "rating_a", "rating_b", "games_a", "games_b",
            "p_exp", "p_gap", "p_min_gap", "p_spread_diff", "p_experience_diff"]
SEEDS = (42, 43, 44, 45, 46)
MIN_KNOWN = 3
NAN = float("nan")


def extract(games: list[dict], title: str, player_k: float):
    """Dual walk-forward rows emitted strictly before each map update."""
    sport_params = params.get(title)
    team = EloEngine(k_factor=sport_params["k"])
    ratings: dict[str, float] = {}
    played: dict[str, int] = {}
    features, outcomes, chronology = [], [], []
    for game in games:
        (team1, lineup1), (team2, lineup2) = sorted(game["teams"].items())
        if (team.games(team1) >= sport_params["min_games"]
                and team.games(team2) >= sport_params["min_games"]):
            row = {
                "elo_exp": team.probability(team1, team2),
                "elo_gap": team.get_rating(team1) - team.get_rating(team2),
                "rating_a": team.get_rating(team1),
                "rating_b": team.get_rating(team2),
                "games_a": team.games(team1),
                "games_b": team.games(team2),
                "p_exp": NAN,
                "p_gap": NAN,
                "p_min_gap": NAN,
                "p_spread_diff": NAN,
                "p_experience_diff": NAN,
            }
            known1 = [ratings[player] for player in lineup1 if player in ratings]
            known2 = [ratings[player] for player in lineup2 if player in ratings]
            if len(known1) >= MIN_KNOWN and len(known2) >= MIN_KNOWN:
                mean1, mean2 = sum(known1) / len(known1), sum(known2) / len(known2)
                row["p_exp"] = 1.0 / (1.0 + 10 ** (-(mean1 - mean2) / 400.0))
                row["p_gap"] = mean1 - mean2
                row["p_min_gap"] = min(known1) - min(known2)
                spread = lambda values, mean: math.sqrt(
                    sum((value - mean) ** 2 for value in values) / len(values))
                row["p_spread_diff"] = spread(known1, mean1) - spread(known2, mean2)
                experience1 = sum(played.get(player, 0) for player in lineup1) / len(lineup1)
                experience2 = sum(played.get(player, 0) for player in lineup2) / len(lineup2)
                row["p_experience_diff"] = experience1 - experience2
            features.append(row)
            outcomes.append(1.0 if game["winner"] == team1 else 0.0)
            chronology.append(game.get("sequence", game.get("date")))

        actual = 1.0 if game["winner"] == team1 else 0.0
        team.record_result(team1, team2, actual)
        rating1 = sum(ratings.get(player, 1500.0) for player in lineup1) / len(lineup1)
        rating2 = sum(ratings.get(player, 1500.0) for player in lineup2) / len(lineup2)
        expected = 1.0 / (1.0 + 10 ** ((rating2 - rating1) / 400.0))
        delta = player_k * (actual - expected)
        for player in lineup1:
            ratings[player] = ratings.get(player, 1500.0) + delta
            played[player] = played.get(player, 0) + 1
        for player in lineup2:
            ratings[player] = ratings.get(player, 1500.0) - delta
            played[player] = played.get(player, 0) + 1
    return features, outcomes, chronology


def _brier_column(matrix, outcomes, column):
    values = matrix[:, FEATURES.index(column)]
    known = ~np.isnan(values)
    return training.brier(values[known], outcomes[known]), int(known.sum())


def run(title: str, player_k: float):
    games = esports_players.load_games(title)
    if not games:
        raise SystemExit(f"no {title} player data; run fetch_esports_players.py {title}")
    rows, outcomes, chronology = extract(games, title, player_k)
    matrix = np.asarray([[row[column] for column in FEATURES] for row in rows], dtype=float)
    outcomes = np.asarray(outcomes, dtype=float)
    train_end = int(len(outcomes) * training.TRAIN_FRAC)
    validation_end = int(len(outcomes) * (training.TRAIN_FRAC + training.VAL_FRAC))
    x_train, y_train = matrix[:train_end], outcomes[:train_end]
    x_validation, y_validation = matrix[train_end:validation_end], outcomes[train_end:validation_end]
    x_test, y_test = matrix[validation_end:], outcomes[validation_end:]
    print(f"{title.upper()} player blend: {len(games)} maps -> {len(outcomes)} rows "
          f"({chronology[0]}..{chronology[-1]}) | train {len(y_train)} / "
          f"val {len(y_validation)} / test {len(y_test)} | player K={player_k:g}")
    missing = np.isnan(matrix[:, FEATURES.index("p_exp")])
    print(f"player features known on {100 * (1 - missing.mean()):.1f}% of rows")

    d_train = xgb.DMatrix(x_train, label=y_train, feature_names=FEATURES)
    d_validation = xgb.DMatrix(x_validation, label=y_validation, feature_names=FEATURES)
    d_validation_predict = xgb.DMatrix(x_validation, feature_names=FEATURES)
    d_test = xgb.DMatrix(x_test, feature_names=FEATURES)
    model_params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "max_depth": 3,
        "eta": 0.03,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 20,
        "lambda": 2.0,
        "seed": 42,
    }
    model = xgb.train(model_params, d_train, num_boost_round=3000,
                      evals=[(d_validation, "val")], early_stopping_rounds=60,
                      verbose_eval=False)
    iteration_range = (0, model.best_iteration + 1)
    team_validation, _ = _brier_column(x_validation, y_validation, "elo_exp")
    player_validation, _ = _brier_column(x_validation, y_validation, "p_exp")
    validation_prediction = model.predict(d_validation_predict, iteration_range=iteration_range)
    validation_player_mask = ~np.isnan(x_validation[:, FEATURES.index("p_exp")])
    xgb_validation = training.brier(validation_prediction, y_validation)
    xgb_player_validation = training.brier(
        validation_prediction[validation_player_mask], y_validation[validation_player_mask])
    print(f"validation Brier: team Elo {team_validation:.4f} | "
          f"player Elo {player_validation:.4f} | XGB {xgb_validation:.4f} "
          f"(player-known rows {xgb_player_validation:.4f})")
    importance = sorted(model.get_score(importance_type="gain").items(),
                        key=lambda item: -item[1])
    print("feature importance: " + ", ".join(
        f"{name}={gain:.0f}" for name, gain in importance[:6]))
    if xgb_validation >= team_validation or xgb_player_validation >= player_validation:
        print("NO TEST READ: validation did not beat both baselines on identical rows")
        return

    team_test, _ = _brier_column(x_test, y_test, "elo_exp")
    player_test, player_count = _brier_column(x_test, y_test, "p_exp")
    test_player_mask = ~np.isnan(x_test[:, FEATURES.index("p_exp")])
    briers, player_masked_briers, team_deltas, player_deltas = [], [], [], []
    for seed in SEEDS:
        seeded = xgb.train({**model_params, "seed": seed}, d_train, num_boost_round=3000,
                           evals=[(d_validation, "val")], early_stopping_rounds=60,
                           verbose_eval=False)
        iteration_range = (0, seeded.best_iteration + 1)
        raw_validation = seeded.predict(d_validation_predict, iteration_range=iteration_range)
        scale, offset = training.fit_platt(raw_validation, y_validation)
        prediction = training.apply_platt(
            seeded.predict(d_test, iteration_range=iteration_range), scale, offset)
        score = training.brier(prediction, y_test)
        player_masked_score = training.brier(
            prediction[test_player_mask], y_test[test_player_mask])
        briers.append(score)
        player_masked_briers.append(player_masked_score)
        team_deltas.append(team_test - score)
        player_deltas.append(player_test - player_masked_score)
    median_team = float(np.median(team_deltas))
    median_player = float(np.median(player_deltas))
    print(f"test Brier: team Elo {team_test:.4f} | player Elo {player_test:.4f} "
          f"(n={player_count}) | XGB median {np.median(briers):.4f} "
          f"(range {min(briers):.4f}-{max(briers):.4f}; "
          f"player-known rows {np.median(player_masked_briers):.4f})")
    print(f"delta vs team Elo {median_team:+.4f}; delta vs player Elo {median_player:+.4f}")
    cleared = (median_team > training.BEAT_MARGIN and median_player > training.BEAT_MARGIN
               and min(team_deltas) > 0 and min(player_deltas) > 0)
    print("VERDICT: " + ("CLEARS RESEARCH GATE" if cleared else "DOES NOT CLEAR GATE"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("title", choices=("dota2", "cs2", "valorant"))
    parser.add_argument("--player-k", required=True, type=float)
    arguments = parser.parse_args()
    run(arguments.title, arguments.player_k)
