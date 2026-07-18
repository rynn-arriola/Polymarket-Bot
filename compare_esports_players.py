"""Chronologically compare player Elo with team Elo on identical games."""

import argparse

from elo import esports_players, params

PLAYER_KS = (12.0, 24.0, 32.0, 48.0, 64.0, 72.0)
MIN_TEST_GAMES = 100


def brier(rows: list[dict], key: str) -> float:
    return sum((row[key] - row["actual"]) ** 2 for row in rows) / len(rows)


def compare(title: str):
    games = esports_players.load_games(title)
    if not games:
        raise SystemExit(f"no {title} bootstrap data; run fetch_esports_players.py {title}")
    train_end, validation_end = int(len(games) * 0.70), int(len(games) * 0.85)
    evaluations = {}
    for player_k in PLAYER_KS:
        rows = esports_players.walk_forward(games, player_k, params.get(title)["k"],
                                             params.get(title)["min_games"])
        validation = [row for row in rows if train_end <= row["index"] < validation_end]
        test = [row for row in rows if row["index"] >= validation_end]
        if not validation or not test:
            raise SystemExit(f"not enough eligible {title} games for chronological evaluation")
        evaluations[player_k] = (brier(validation, "player"), rows, test)
    best_k = min(evaluations, key=lambda key: evaluations[key][0])
    validation_brier, _rows, test = evaluations[best_k]
    audit = esports_players.audit_games(games)
    print(f"{title.upper()}: {audit}")
    print(f"selected player K={best_k:.0f} on validation Brier {validation_brier:.4f}")
    if len(test) < MIN_TEST_GAMES:
        print(f"NO VERDICT: only {len(test)} eligible test games; need at least {MIN_TEST_GAMES}")
        return
    team_brier, player_brier = brier(test, "team"), brier(test, "player")
    print(f"test ({len(test)} games): team Elo {team_brier:.4f} | player Elo {player_brier:.4f} "
          f"| delta {player_brier - team_brier:+.4f}")
    print("research universe only; not comparable to the production PandaScore Brier gate")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("titles", nargs="*", choices=("dota2", "cs2", "valorant"),
                        default=("dota2", "cs2", "valorant"))
    args = parser.parse_args()
    for requested in args.titles:
        compare(requested)
