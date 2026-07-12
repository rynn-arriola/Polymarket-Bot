"""NBA/WNBA Elo adapter, backed by ESPN's public (unofficial, no-key)
scoreboard endpoint via elo/history.py's cached fetcher.

Games are filtered against ESPN's official team list — verified live that
without this the scoreboard feed also contains All-Star exhibitions ("Team
Chuck", "Team Shaq") and NBA-vs-international-club preseason friendlies
(e.g. "Hapoel Jerusalem"), which contaminated real teams' ratings in the
first build of this project.

Upgrades over plain team Elo:
1. MARGIN-OF-VICTORY K scaling (elo.engine.mov_multiplier).
2. BACK-TO-BACK rest penalty: a team that also played yesterday gets
   b2b_penalty Elo points docked for that game only (fatigue is a real,
   measured effect in basketball). Each team's last-played date is kept in
   engine.extras so the live bot can apply the same penalty to upcoming
   games — which also means ratings should be rebuilt daily or the
   last-played data goes stale (build_ratings.py's job).
"""

import logging
from datetime import date, datetime, timedelta

from elo import history, params
from elo.engine import EloEngine, mov_multiplier

log = logging.getLogger("divergence_bot.elo.basketball")


def fetch_games(league: str, start: date, end: date) -> list[dict]:
    real_teams = history.espn_real_teams(f"basketball/{league}")
    if not real_teams:
        log.warning(f"{league.upper()}: official team list unavailable — "
                    f"exhibition games may contaminate ratings this build")
    games = history.espn_team_games(f"basketball/{league}", f"basketball_{league}",
                                    start, end, real_teams)
    log.info(f"{league.upper()}: {len(games)} finished games ({start}..{end})")
    return games


def _b2b(last_played: dict, team: str, game_date: str) -> bool:
    prev = last_played.get(team)
    if not prev:
        return False
    try:
        return (date.fromisoformat(game_date) - date.fromisoformat(prev)).days == 1
    except ValueError:
        return False


def _gap(engine: EloEngine, p: dict, home: str, away: str, game_date: str) -> float:
    last_played = engine.extras.setdefault("last_played", {})
    gap = engine.get_rating(home) - engine.get_rating(away) + p["home_adv"]
    if _b2b(last_played, home, game_date):
        gap -= p["b2b_penalty"]
    if _b2b(last_played, away, game_date):
        gap += p["b2b_penalty"]
    return gap


def replay(games: list[dict], p: dict, collect: bool = False,
           season_regress: float = 1 / 3) -> tuple[EloEngine, list]:
    """Chronological replay; with collect=True also returns walk-forward
    (predicted_prob, home_won) pairs, each made before that game updates
    any rating. Season boundaries are detected by a >60-day gap between
    consecutive games (works for both NBA's Oct-Jun and WNBA's May-Oct)."""
    engine = EloEngine(k_factor=p["k"])
    last_played = engine.extras.setdefault("last_played", {})
    predictions = []
    prev_date = None

    for g in games:
        if prev_date is not None:
            try:
                gap_days = (date.fromisoformat(g["date"]) - date.fromisoformat(prev_date)).days
                if gap_days > 60:
                    engine.regress_to_mean(season_regress)
            except ValueError:
                pass
        prev_date = g["date"]

        home, away = g["home"], g["away"]
        gap = _gap(engine, p, home, away, g["date"])
        exp_home = 1.0 / (1.0 + 10 ** (-gap / 400.0))
        home_won = g["hs"] > g["as_"]

        if collect and engine.games(home) >= p["min_games"] and engine.games(away) >= p["min_games"]:
            predictions.append((exp_home, 1.0 if home_won else 0.0))

        margin = abs(g["hs"] - g["as_"])
        diff_winner = gap if home_won else -gap
        mult = mov_multiplier(margin, diff_winner) if p.get("mov") else 1.0
        engine.record_result(home, away, 1.0 if home_won else 0.0, k_multiplier=mult)
        last_played[home] = g["date"]
        last_played[away] = g["date"]

    return engine, predictions


def build_engine(league: str, start: date, end: date) -> tuple[EloEngine, int]:
    games = fetch_games(league, start, end)
    engine, _ = replay(games, params.get(league))
    return engine, len(games)


def probability(engine: EloEngine, home_team: str, away_team: str, league: str,
                game_date: str | None = None) -> float | None:
    """P(home_team wins), or None if either team doesn't have enough games
    to trust its rating. game_date (ISO) enables the back-to-back check
    against each team's recorded last game."""
    p = params.get(league)
    if engine.games(home_team) < p["min_games"] or engine.games(away_team) < p["min_games"]:
        return None
    gap = _gap(engine, p, home_team, away_team, game_date or date.today().isoformat())
    return 1.0 / (1.0 + 10 ** (-gap / 400.0))
