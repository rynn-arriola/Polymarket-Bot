"""MLB Elo adapter, backed by the free MLB Stats API (statsapi.mlb.com,
no key required) via elo/history.py's cached fetcher.

Two upgrades over plain team Elo, both motivated by the first backtest of
this project (team-only Elo scored Brier 0.248 — barely better than a
0.25 coin flip — and was overconfident at the extremes):

1. STARTING PITCHER adjustment. The starter dominates game-to-game variance
   in baseball far more than team strength does. Each starter carries his
   own rating (updated per start from how the game went vs expectation);
   a game's effective team gap is shifted by pitcher_weight × the starters'
   rating gap. Probable pitchers come from the same schedule API
   (hydrate=probablePitcher — verified live it's populated on historical
   games too, and available pregame for upcoming games).

2. MARGIN-OF-VICTORY K scaling (elo.engine.mov_multiplier) — a 10-run
   blowout says more than a 1-run squeaker.

Scope: MLB only. NPB/KBO (also traded on Polymarket) aren't covered by this
API and have no free source found — left out of config.SUPPORTED_SPORTS
rather than guessed at.
"""

import logging
import time
from datetime import date, timedelta

from elo import history, params
from elo.engine import EloEngine, mov_multiplier

log = logging.getLogger("divergence_bot.elo.mlb")

SPORT = "mlb"
PITCHER_DEFAULT = 1500.0

# ------------------------------------------------------------------
# Live probable pitchers for UPCOMING games (used by divergence_bot.py).
# Same schedule endpoint as history, hydrated pregame by MLB once starters
# are announced. Cached for 30 minutes.
# ------------------------------------------------------------------

_PP_CACHE: tuple[float, dict] | None = None
_PP_CACHE_TTL = 30 * 60


def probable_pitchers() -> dict[tuple[str, str], int]:
    """{(officialDate, team_name): pitcher_id} for today through +2 days.
    Empty dict on failure — predictions then fall back to team-only."""
    global _PP_CACHE
    now = time.monotonic()
    if _PP_CACHE and now - _PP_CACHE[0] < _PP_CACHE_TTL:
        return _PP_CACHE[1]
    start = date.today()
    url = history.MLB_SCHEDULE.format(start=start.isoformat(),
                                      end=(start + timedelta(days=2)).isoformat())
    data = history._get_json(url)
    out: dict[tuple[str, str], int] = {}
    for d in (data or {}).get("dates", []):
        for g in d.get("games", []):
            for side in ("home", "away"):
                t = (g.get("teams") or {}).get(side) or {}
                name = (t.get("team") or {}).get("name")
                pid = (t.get("probablePitcher") or {}).get("id")
                if name and pid:
                    name = history.MLB_NAME_ALIASES.get(name, name)
                    out[(d.get("date"), name)] = pid
    _PP_CACHE = (now, out)
    return out


def pitcher_for(team: str, game_date: date) -> int | None:
    """Probable starter for a team on/around a date. MLB's officialDate is
    US-local while market start times are UTC (a 10pm ET start is already
    'tomorrow' in UTC), so the previous day is checked too."""
    pp = probable_pitchers()
    for d in (game_date, game_date - timedelta(days=1)):
        pid = pp.get((d.isoformat(), team))
        if pid:
            return pid
    return None


def fetch_games(start_year: int, end_year: int | None = None) -> list[dict]:
    end_year = end_year or date.today().year
    games = []
    for year in range(start_year, end_year + 1):
        season = history.mlb_season(year)
        if season:
            log.info(f"MLB {year}: {len(season)} finished games")
        games.extend(season)
    return games


def _pitcher_rating(pitchers: dict, pid) -> float:
    if pid is None:
        return PITCHER_DEFAULT
    return pitchers.get(str(pid), [PITCHER_DEFAULT, 0])[0]


def _effective_gap(engine: EloEngine, p: dict, home: str, away: str,
                   home_pitcher, away_pitcher) -> float:
    """Home-minus-away effective rating gap for one game: team Elo gap +
    home advantage + weighted starters' rating gap."""
    pitchers = engine.extras.setdefault("pitchers", {})
    gap = engine.get_rating(home) - engine.get_rating(away) + p["home_adv"]
    gap += p["pitcher_weight"] * (
        _pitcher_rating(pitchers, home_pitcher) - _pitcher_rating(pitchers, away_pitcher)
    )
    return gap


def replay(games: list[dict], p: dict, collect: bool = False,
           season_regress: float = 1 / 3) -> tuple[EloEngine, list]:
    """Runs every game through the model in chronological order. With
    collect=True, also returns walk-forward (predicted_prob, home_won)
    pairs — each prediction made BEFORE that game updates any rating, i.e.
    with exactly the information the live bot would have had."""
    engine = EloEngine(k_factor=p["k"])
    pitchers = engine.extras.setdefault("pitchers", {})
    predictions = []
    prev_year = None
    seeds: dict[str, float] = {}

    def _stat_seeds(season: int) -> dict[str, float]:
        """Stat-implied pitcher ratings from the PRIOR season's ERA — prior
        season only, so this is walk-forward honest (a rookie's eventual
        full-season ERA is never used to predict his own debut). Weighted by
        innings reliability: below ~30 IP an ERA is mostly noise."""
        scale = p.get("pitcher_seed_scale", 0.0)
        if not scale:
            return {}
        stats = history.mlb_pitching(season - 1)
        if not stats:
            return {}
        eras = [(v["era"], v["ip"]) for v in stats.values() if v["ip"] >= 30]
        if not eras:
            return {}
        league_avg = sum(e * ip for e, ip in eras) / sum(ip for _, ip in eras)
        out = {}
        for pid, v in stats.items():
            if v["ip"] < 30:
                continue
            reliability = min(1.0, v["ip"] / 120.0)
            out[pid] = PITCHER_DEFAULT + scale * (league_avg - v["era"]) * reliability
        return out

    for g in games:
        year = int(g["date"][:4])
        if prev_year is None or year != prev_year:
            if prev_year is not None:
                engine.regress_to_mean(season_regress)
            seeds = _stat_seeds(year)
            # Pitchers regress toward their stat-implied level (or default
            # when unknown) — a proven ace shouldn't drift to average just
            # because the calendar turned.
            if prev_year is not None:
                for pid in pitchers:
                    target = seeds.get(pid, PITCHER_DEFAULT)
                    pitchers[pid][0] += (target - pitchers[pid][0]) * season_regress
        prev_year = year

        home, away = g["home"], g["away"]
        # First sighting of a starter this run: seed from prior-season stats.
        for pid in (g["home_pitcher"], g["away_pitcher"]):
            if pid is not None and str(pid) not in pitchers:
                pitchers[str(pid)] = [seeds.get(str(pid), PITCHER_DEFAULT), 0]

        gap = _effective_gap(engine, p, home, away, g["home_pitcher"], g["away_pitcher"])
        exp_home = 1.0 / (1.0 + 10 ** (-gap / 400.0))
        home_won = g["hs"] > g["as_"]

        if collect and engine.games(home) >= p["min_games"] and engine.games(away) >= p["min_games"]:
            predictions.append((exp_home, 1.0 if home_won else 0.0))

        margin = abs(g["hs"] - g["as_"])
        diff_winner = gap if home_won else -gap
        mult = mov_multiplier(margin, diff_winner) if p.get("mov") else 1.0
        actual = 1.0 if home_won else (0.0 if g["hs"] < g["as_"] else 0.5)
        # Team update uses the plain team-vs-team expectation (pitcher part
        # of the surprise shouldn't permanently move the TEAM's rating).
        engine.record_result(home, away, actual,
                             k_override=None, k_multiplier=mult)
        # Starters absorb their share of the game surprise at their own K.
        surprise = actual - exp_home
        for pid, sign in ((g["home_pitcher"], 1), (g["away_pitcher"], -1)):
            if pid is None:
                continue
            entry = pitchers.setdefault(str(pid), [PITCHER_DEFAULT, 0])
            entry[0] += p["pitcher_k"] * surprise * sign
            entry[1] += 1

    return engine, predictions


def build_engine(start_year: int, end_year: int | None = None) -> tuple[EloEngine, int]:
    games = fetch_games(start_year, end_year)
    engine, _ = replay(games, params.get(SPORT))
    return engine, len(games)


def probability(engine: EloEngine, home_team: str, away_team: str,
                home_pitcher=None, away_pitcher=None) -> float | None:
    """P(home_team wins), or None if either team doesn't have enough games
    to trust its rating. Pitcher IDs (MLB Stats API player ids) are optional
    — without them the model falls back to team-plus-home-advantage only."""
    p = params.get(SPORT)
    if engine.games(home_team) < p["min_games"] or engine.games(away_team) < p["min_games"]:
        return None
    gap = _effective_gap(engine, p, home_team, away_team, home_pitcher, away_pitcher)
    return 1.0 / (1.0 + 10 ** (-gap / 400.0))
