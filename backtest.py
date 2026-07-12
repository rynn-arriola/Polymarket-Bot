"""Offline calibration check for each sport's Elo model: Brier score,
log-loss, and a bucketed calibration table.

Every prediction is walk-forward — produced by each adapter's replay()
BEFORE that game updates any rating, i.e. with exactly the information the
live bot would have had at the time. Data comes from elo/history.py's local
cache, so re-runs are fast and don't hammer the APIs.

This validates the MODEL only. Whether a 5%+ divergence from Polymarket's
price actually predicts an edge can only be checked by running
divergence_bot.py in dry-run and watching `status` accumulate settled
positions over time.

Usage: python backtest.py [sport ...]     (default: every sport)
"""

import logging
import math
import sys
from datetime import date

import config
from elo import basketball, esports, mlb, params, soccer, tennis

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def brier(predictions: list[tuple[float, float]]) -> float:
    return sum((p - a) ** 2 for p, a in predictions) / len(predictions)


def predictions_for(sport: str) -> list[tuple[float, float]]:
    p = params.get(sport)
    if sport == "mlb":
        games = mlb.fetch_games(config.MLB_START_YEAR)
        _, preds = mlb.replay(games, p, collect=True)
    elif sport in ("nba", "wnba"):
        start = config.NBA_START_DATE if sport == "nba" else config.WNBA_START_DATE
        games = basketball.fetch_games(sport, start, date.today())
        _, preds = basketball.replay(games, p, collect=True)
    elif sport == "fwc":
        games = soccer.fetch_games()
        _, preds = soccer.replay(games, p, collect=True)
    elif sport in ("atp", "wta"):
        matches = tennis.fetch_matches_for(sport, config.TENNIS_START_DATE, date.today(),
                                           config.TENNIS_TML_START_YEAR)
        _, preds = tennis.replay(matches, p, collect=True)
    elif sport in esports.TITLES:
        matches = esports.fetch_matches(sport)
        _, preds = esports.replay(matches, p, collect=True)
    else:
        preds = []
    return preds


def report(name: str, predictions: list[tuple[float, float]], sport: str | None = None):
    if not predictions:
        print(f"\n{name}: no walk-forward predictions available (not enough data yet)")
        return
    n = len(predictions)
    eps = 1e-9
    b = brier(predictions)
    logloss = -sum(
        a * math.log(max(p, eps)) + (1 - a) * math.log(max(1 - p, eps)) for p, a in predictions
    ) / n
    line = f"\n{name}: {n} walk-forward predictions | Brier {b:.4f} (lower=better, 0.25=coin-flip) | log-loss {logloss:.4f}"
    if sport:
        cal = params.get(sport).get("calibration", {})
        if cal.get("a", 1.0) != 1.0 or cal.get("b", 0.0) != 0.0:
            calibrated = [(params.apply_calibration(p, sport), a) for p, a in predictions]
            line += f" | calibrated Brier {brier(calibrated):.4f}"
    print(line)
    print(f"  {'bucket':>10} {'n':>6} {'predicted avg':>14} {'actual rate':>12}")
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        in_bucket = [(p, a) for p, a in predictions if lo <= p < hi or (i == 9 and p == 1.0)]
        if not in_bucket:
            continue
        avg_p = sum(p for p, _ in in_bucket) / len(in_bucket)
        avg_a = sum(a for _, a in in_bucket) / len(in_bucket)
        print(f"  {lo:>4.0%}-{hi:<4.0%} {len(in_bucket):>6} {avg_p:>13.1%} {avg_a:>11.1%}")


if __name__ == "__main__":
    requested = [s.lower() for s in sys.argv[1:]] or ["mlb", "nba", "wnba", "fwc", "atp", "wta", "dota2", "cs2", "lol", "valorant"]
    labels = {"mlb": "MLB", "nba": "NBA", "wnba": "WNBA", "fwc": "FIFA World Cup",
              "atp": "ATP", "wta": "WTA", "dota2": "Dota 2", "cs2": "CS2", "lol": "LoL", "valorant": "Valorant"}
    for sport in requested:
        report(labels.get(sport, sport.upper()), predictions_for(sport), sport)
