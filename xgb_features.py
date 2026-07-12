"""Walk-forward training data for the gated XGBoost models.

Replays each sport's history exactly as its Elo adapter does, and at every
qualifying game emits the SAME feature vector xgb_live builds at inference
time — the builders live in xgb_live and are imported here, so training and
live prediction can never drift. Returns (X, y): X a list of feature dicts
(keys == the sport's feature list), y the 1/0 labels, chronological and
strictly pre-game (same min_games gate as the Elo backtest).
"""

from datetime import date

import xgb_live
from elo import basketball, params, tennis
from elo.engine import EloEngine, mov_multiplier

FEATURES = xgb_live.NBA_FEATURES
TENNIS_FEATURES = xgb_live.TENNIS_FEATURES


def extract_nba(games: list[dict], league: str = "nba", season_regress: float = 1 / 3):
    """Basketball (nba/wnba): predicts P(home wins)."""
    p = params.get(league)
    engine = EloEngine(k_factor=p["k"])
    last_played = engine.extras.setdefault("last_played", {})
    X, y = [], []
    prev_date = None

    for g in games:
        if prev_date is not None:
            try:
                if (date.fromisoformat(g["date"]) - date.fromisoformat(prev_date)).days > 60:
                    engine.regress_to_mean(season_regress)
            except ValueError:
                pass
        prev_date = g["date"]

        home, away = g["home"], g["away"]
        home_won = g["hs"] > g["as_"]
        if engine.games(home) >= p["min_games"] and engine.games(away) >= p["min_games"]:
            X.append(xgb_live.basketball_features(engine, league, home, away, g["date"]))
            y.append(1 if home_won else 0)

        gap = basketball._gap(engine, p, home, away, g["date"])
        margin = abs(g["hs"] - g["as_"])
        diff_winner = gap if home_won else -gap
        mult = mov_multiplier(margin, diff_winner) if p.get("mov") else 1.0
        engine.record_result(home, away, 1.0 if home_won else 0.0, k_multiplier=mult)
        last_played[home] = g["date"]
        last_played[away] = g["date"]

    return X, y


def extract_tennis(matches: list[dict], tour: str):
    """Tennis (atp/wta): alphabetical player order, predicts P(first wins)."""
    p = params.get(tour)
    engine = EloEngine(k_factor=p["k"])
    X, y = [], []

    for m in matches:
        winner, loser = m["winner"], m["loser"]
        surface = m.get("surface") or tennis.surface_of(m.get("tournament", ""))
        if engine.games(winner) >= p["min_games"] and engine.games(loser) >= p["min_games"]:
            a, b = sorted((winner, loser))
            X.append(xgb_live.tennis_features(engine, tour, a, b, surface))
            y.append(1 if a == winner else 0)

        engine.record_result(winner, loser, 1.0)
        engine.record_result(tennis._skey(winner, surface), tennis._skey(loser, surface), 1.0)

    return X, y
