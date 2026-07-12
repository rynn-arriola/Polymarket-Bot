"""Hyperparameter tuning + probability calibration — the "training" step
beyond the Elo updates themselves.

For each sport:
1. GRID SEARCH: sweep the model's hyperparameters (K-factor, home advantage,
   pitcher weight, back-to-back penalty, surface blend, draw rate — whatever
   that sport has), re-running the full walk-forward backtest for each
   candidate and scoring by Brier. All data comes from elo/history.py's
   local cache, so hundreds of replays cost seconds, not API calls.
2. PLATT CALIBRATION: with the winning params, fit p' = sigmoid(a*logit(p)+b)
   on the chronologically FIRST 70% of walk-forward predictions and check it
   improves Brier on the LAST 30% (an honest holdout — no peeking). Kept
   only if it helps; identity otherwise.

Winners are written to model_params.json, which elo/params.py reads —
build_ratings.py, backtest.py, and the live bot all pick them up
automatically. Re-run after big data additions (e.g. a new season).

Usage: python tune.py [sport ...]     (default: every sport)
"""

import itertools
import logging
import math
import sys
from datetime import date

import config
from elo import basketball, esports, mlb, params, soccer, tennis

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tune")

GRIDS: dict[str, dict[str, list]] = {
    "mlb": {
        # First pass picked the smallest value on every axis of a wider grid,
        # so this grid brackets below those winners too.
        "k": [1.0, 2.0, 3.0, 4.0, 6.0],
        "home_adv": [14.0, 20.0, 28.0],
        "pitcher_weight": [0.15, 0.25, 0.4],
        "pitcher_k": [4.0, 8.0, 12.0],
    },
    # Each grid brackets the previous pass's winner (several landed on a
    # grid edge the first time).
    "nba": {
        "k": [6.0, 9.0, 12.0, 16.0],
        "home_adv": [25.0, 32.0, 40.0, 50.0],
        "b2b_penalty": [40.0, 50.0, 65.0, 80.0],
    },
    "wnba": {
        "k": [10.0, 13.0, 16.0, 20.0],
        "home_adv": [25.0, 32.0, 40.0, 50.0],
        "b2b_penalty": [0.0, 10.0, 20.0, 30.0],
    },
    "fwc": {
        "k": [60.0, 75.0, 90.0, 110.0],
        "draw_rate": [0.10, 0.12, 0.14, 0.17],
    },
    "atp": {
        "k": [24.0, 32.0, 40.0],
        "surface_weight": [0.0, 0.15, 0.3, 0.5],
    },
    "wta": {
        "k": [24.0, 32.0, 40.0],
        "surface_weight": [0.0, 0.15, 0.3, 0.5],
    },
    "dota2": {"k": [10.0, 14.0, 18.0, 24.0]},
    "cs2": {"k": [24.0, 32.0, 40.0, 56.0]},
    "lol": {"k": [24.0, 32.0, 40.0, 56.0]},
    "valorant": {"k": [24.0, 32.0, 40.0, 56.0]},
}


def _replay_fn(sport: str):
    """Returns (games, replay_callable) for a sport — games fetched once,
    replayed many times with different params."""
    if sport == "mlb":
        games = mlb.fetch_games(config.MLB_START_YEAR)
        return games, mlb.replay
    if sport in ("nba", "wnba"):
        start = config.NBA_START_DATE if sport == "nba" else config.WNBA_START_DATE
        games = basketball.fetch_games(sport, start, date.today())
        return games, basketball.replay
    if sport == "fwc":
        return soccer.fetch_games(), soccer.replay
    if sport in ("atp", "wta"):
        matches = tennis.fetch_matches_for(sport, config.TENNIS_START_DATE, date.today(),
                                           config.TENNIS_TML_START_YEAR)
        return matches, tennis.replay
    if sport in esports.TITLES:
        return esports.fetch_matches(sport), esports.replay
    raise ValueError(f"no tuning support for {sport}")


def brier(preds) -> float:
    return sum((p - a) ** 2 for p, a in preds) / len(preds)


def grid_search(sport: str) -> tuple[dict, list]:
    games, replay = _replay_fn(sport)
    if not games:
        print(f"{sport}: no data — skipped")
        return {}, []
    base = params.get(sport)
    grid = GRIDS[sport]
    keys = list(grid)
    best_score, best_combo, best_preds = None, None, None
    n_combos = 1
    for k in keys:
        n_combos *= len(grid[k])
    print(f"{sport}: grid-searching {n_combos} combinations over {len(games)} games...")

    for values in itertools.product(*(grid[k] for k in keys)):
        candidate = dict(base)
        candidate.update(dict(zip(keys, values)))
        candidate["calibration"] = {"a": 1.0, "b": 0.0}  # score raw model only
        _, preds = replay(games, candidate, collect=True)
        if not preds:
            continue
        score = brier(preds)
        if best_score is None or score < best_score:
            best_score, best_combo, best_preds = score, dict(zip(keys, values)), preds

    baseline_preds = replay(games, base, collect=True)[1]
    baseline = brier(baseline_preds) if baseline_preds else float("nan")
    print(f"{sport}: best Brier {best_score:.4f} (baseline {baseline:.4f}) with {best_combo}")
    return best_combo, best_preds


def fit_platt(preds: list[tuple[float, float]]) -> dict | None:
    """Fits sigmoid(a*logit(p)+b) on the first 70% (chronological), keeps it
    only if it improves Brier on the untouched last 30%."""
    if len(preds) < 300:
        return None  # too little data to fit a correction worth trusting
    cut = int(len(preds) * 0.7)
    train, hold = preds[:cut], preds[cut:]

    def logit(p):
        p = min(max(p, 1e-9), 1 - 1e-9)
        return math.log(p / (1 - p))

    a, b = 1.0, 0.0
    xs = [logit(p) for p, _ in train]
    ys = [y for _, y in train]
    lr = 0.1
    for _ in range(500):  # plain gradient descent on log-loss; 2 params, converges easily
        ga = gb = 0.0
        for x, y in zip(xs, ys):
            q = 1.0 / (1.0 + math.exp(-(a * x + b)))
            ga += (q - y) * x
            gb += (q - y)
        a -= lr * ga / len(xs)
        b -= lr * gb / len(xs)

    def apply(p):
        return 1.0 / (1.0 + math.exp(-(a * logit(p) + b)))

    raw_hold = brier(hold)
    cal_hold = brier([(apply(p), y) for p, y in hold])
    if cal_hold < raw_hold - 1e-5:
        print(f"  calibration kept: holdout Brier {raw_hold:.4f} -> {cal_hold:.4f} (a={a:.3f}, b={b:+.3f})")
        return {"a": round(a, 4), "b": round(b, 4)}
    print(f"  calibration NOT kept: holdout Brier {raw_hold:.4f} -> {cal_hold:.4f} (no real improvement)")
    return None


def tune(sport: str) -> dict | None:
    combo, preds = grid_search(sport)
    if not combo:
        return None
    tuned = dict(combo)
    cal = fit_platt(preds)
    if cal:
        tuned["calibration"] = cal
    return tuned


if __name__ == "__main__":
    requested = [s.lower() for s in sys.argv[1:]] or list(GRIDS)
    results = {}
    for sport in requested:
        if sport not in GRIDS:
            print(f"{sport}: no tuning grid defined — skipped")
            continue
        tuned = tune(sport)
        if tuned:
            results[sport] = tuned
    if results:
        params.save(results)
        print(f"\nWrote {params.PARAMS_FILE.name}: {list(results)}")
        print("Re-run `python build_ratings.py` so cached ratings reflect the tuned params.")
