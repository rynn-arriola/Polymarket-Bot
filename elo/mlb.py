"""MLB Elo adapter, backed by ESPN's free scoreboard API (keyless) via
elo/history.py's cached fetcher — switched from statsapi.mlb.com on
2026-07-18 after MLB started blocking the droplet's IP (origin-level 406,
~2026-07-10). ESPN carries finals AND per-game probable starters on both
historical and upcoming games; pitcher ids are ESPN athlete ids end to end
(never mix them with statsapi ids — different id space).

Two upgrades over plain team Elo, both motivated by the first backtest of
this project (team-only Elo scored Brier 0.248 — barely better than a
0.25 coin flip — and was overconfident at the extremes):

1. STARTING PITCHER adjustment. The starter dominates game-to-game variance
   in baseball far more than team strength does. Each starter carries his
   own rating (updated per start from how the game went vs expectation);
   a game's effective team gap is shifted by pitcher_weight × the starters'
   rating gap. Probable pitchers come from the same ESPN scoreboard
   (`probables` per competitor — verified live it's populated on historical
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
_PP_RETRY_AFTER_FAILURE = 2 * 60


def probable_pitchers() -> dict[tuple[str, str], int]:
    """{(officialDate, team_name): pitcher_id} for today through +2 days.

    A FAILED fetch never poisons the cache: the last good answer keeps
    serving (a 30-min-stale starter beats none — the market has priced the
    real one, and predicting team-only against it is the adverse-selection
    trap) and the next attempt comes after _PP_RETRY_AFTER_FAILURE instead
    of a full TTL. Only a fetch that ANSWERED — even with no starters
    announced yet, a real pregame state — is cached as truth for the TTL.
    statsapi.mlb.com serves intermittent 406s (seen 2026-07-16/18), so the
    failure path is routine, not hypothetical. Serving stale across a long
    outage is self-limiting: entries are keyed by officialDate, so an old
    snapshot simply has no keys for later dates and predictions fall back
    to team-only, same as before."""
    global _PP_CACHE
    now = time.monotonic()
    if _PP_CACHE and now - _PP_CACHE[0] < _PP_CACHE_TTL:
        return _PP_CACHE[1]
    start = date.today()
    url = history.ESPN_SCOREBOARD.format(
        path=history.ESPN_MLB_PATH,
        d1=start.strftime("%Y%m%d"),
        d2=(start + timedelta(days=2)).strftime("%Y%m%d"), limit=100)
    data = history._get_json(url)
    if data is None:
        stale = _PP_CACHE[1] if _PP_CACHE else {}
        # Backdate the timestamp so the existing freshness check retries
        # after _PP_RETRY_AFTER_FAILURE seconds, serving stale meanwhile.
        _PP_CACHE = (now - _PP_CACHE_TTL + _PP_RETRY_AFTER_FAILURE, stale)
        if stale:
            log.warning("probable-pitcher fetch failed — serving previous "
                        f"snapshot ({len(stale)} entries), retrying in "
                        f"{_PP_RETRY_AFTER_FAILURE // 60} min")
        return stale
    out: dict[tuple[str, str], str] = {}
    for ev in data.get("events", []):
        if (ev.get("season") or {}).get("type") not in history.ESPN_MLB_SEASON_TYPES:
            continue  # spring training; All-Star has no probables anyway
        comps = ev.get("competitions") or []
        if not comps:
            continue
        c = comps[0]
        game_date = (c.get("date") or ev.get("date") or "")[:10]
        for comp in c.get("competitors") or []:
            name = (comp.get("team") or {}).get("displayName")
            probables = comp.get("probables") or []
            pid = ((probables[0].get("athlete") or {}).get("id")) if probables else None
            if name and pid and game_date:
                name = history.MLB_NAME_ALIASES.get(name, name)
                out[(game_date, name)] = str(pid)
    _PP_CACHE = (now, out)
    return out


def pitcher_for(team: str, game_date: date) -> str | None:
    """Probable starter for a team on a UTC date. Both sides of the lookup
    derive from the game's actual start instant in UTC (the market's
    gameStartTime and ESPN's competition date), so exact match is correct.
    No ±1-day fallback: with UTC-to-UTC keys a neighbouring day can only
    ever name a pitcher who is NOT starting this game (that was a statsapi
    artifact, when its officialDate was US-local)."""
    return probable_pitchers().get((game_date.isoformat(), team))


def fetch_games(start_year: int, end_year: int | None = None) -> list[dict]:
    end_year = end_year or date.today().year
    games = history.espn_mlb_games(date(start_year, 3, 1),
                                   min(date(end_year, 11, 15), date.today()))
    by_year: dict[str, int] = {}
    for g in games:
        by_year[g["date"][:4]] = by_year.get(g["date"][:4], 0) + 1
    for year, n in sorted(by_year.items()):
        log.info(f"MLB {year}: {n} finished games")
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
           season_regress: float = 1 / 3, feature_fn=None) -> tuple[EloEngine, list]:
    """Runs every game through the model in chronological order. With
    collect=True, also returns walk-forward (predicted_prob, home_won)
    pairs — each prediction made BEFORE that game updates any rating, i.e.
    with exactly the information the live bot would have had.

    feature_fn (optional): if given, the collected pairs are
    (feature_fn(engine, home, away, home_pitcher, away_pitcher), home_won)
    instead of (prob, home_won) — lets the XGBoost trainer harvest the full
    feature vector at each game's pre-update state, reusing this exact replay
    (pitcher seeding and all) so training can't drift from the live model."""
    engine = EloEngine(k_factor=p["k"])
    pitchers = engine.extras.setdefault("pitchers", {})
    predictions = []
    prev_year = None
    seeds: dict[str, float] = {}

    def _stat_seeds(season: int) -> dict[str, float]:
        """Stat-implied pitcher ratings from the PRIOR season's ERA — prior
        season only, so this is walk-forward honest (a rookie's eventual
        full-season ERA is never used to predict his own debut). Weighted by
        innings reliability: below ~30 IP an ERA is mostly noise.

        NOTE: inactive (pitcher_seed_scale=0, a settled dead end) and backed
        by statsapi.mlb.com, which blocks the droplet — and its ids are NOT
        ESPN ids. If ever revived, it needs an ESPN-id stats source first."""
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
            label = 1.0 if home_won else 0.0
            if feature_fn is not None:
                predictions.append((feature_fn(engine, home, away,
                                               g["home_pitcher"], g["away_pitcher"],
                                               g["date"]), label))
            else:
                predictions.append((exp_home, label))

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
