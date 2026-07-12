"""FIFA World Cup national-team Elo adapter (Polymarket league code "FWC"),
backed by ESPN's public scoreboard via elo/history.py's cached fetcher.

Sample-thickness upgrade: the first build used World Cup FINALS matches only
(~64 per tournament — Brier 0.245, mostly noise). This version also ingests
WORLD CUP QUALIFIERS from every confederation (ESPN league slugs verified
live 2026-07-08: fifa.worldq.uefa/.conmebol/.concacaf/.afc/.caf/.ofc),
which multiplies the per-team sample severalfold with competitive (not
friendly) matches. Friendlies are deliberately excluded — teams experiment
with lineups and effort varies too much for the results to be reliable
rating signal.

Polymarket structural note (verified live): FWC full-time-winner markets
are single-team binary ("Will France win this match: Yes/No") — a draw
settles No for BOTH teams' markets. So the model needs P(win outright),
not the plain Elo expected score: see elo.engine.decompose_win_draw_loss.
"""

import logging
from datetime import date

from elo import history, params
from elo.engine import EloEngine, decompose_win_draw_loss, mov_multiplier

log = logging.getLogger("divergence_bot.elo.soccer")

SPORT = "fwc"

# World Cup finals windows (finals-only league slug fifa.world).
TOURNAMENT_WINDOWS = [
    (date(2014, 6, 12), date(2014, 7, 13)),
    (date(2018, 6, 14), date(2018, 7, 15)),
    (date(2022, 11, 20), date(2022, 12, 18)),
    (date(2026, 6, 11), date(2026, 7, 19)),
]

# Qualifier league slugs + the current cycle's window. Earlier cycles'
# qualifiers are left out on purpose: pre-2023 form says little about 2026
# squads, and season regression would mostly erase it anyway.
QUALIFIER_SLUGS = [
    "fifa.worldq.uefa", "fifa.worldq.conmebol", "fifa.worldq.concacaf",
    "fifa.worldq.afc", "fifa.worldq.caf", "fifa.worldq.ofc",
]
QUALIFIER_WINDOW = (date(2023, 1, 1), date(2026, 6, 10))


def fetch_games() -> list[dict]:
    games = []
    for slug in QUALIFIER_SLUGS:
        start, end = QUALIFIER_WINDOW
        chunk = history.espn_team_games(f"soccer/{slug}", f"soccer_{slug}", start, end)
        log.info(f"{slug}: {len(chunk)} finished qualifiers")
        games.extend(chunk)
    for start, end in TOURNAMENT_WINDOWS:
        if start > date.today():
            continue
        chunk = history.espn_team_games("soccer/fifa.world", f"soccer_wc_{start.year}",
                                        start, min(end, date.today()))
        log.info(f"World Cup {start.year}: {len(chunk)} finished matches")
        games.extend(chunk)
    games.sort(key=lambda g: g["date"])
    return games


def replay(games: list[dict], p: dict, collect: bool = False) -> tuple[EloEngine, list]:
    """Chronological replay; with collect=True also returns walk-forward
    (predicted_win_prob_home, home_won_outright) pairs — win probability
    after draw decomposition, since that's the number the bot actually
    trades on."""
    engine = EloEngine(k_factor=p["k"])
    predictions = []
    for g in games:
        home, away = g["home"], g["away"]
        gap = engine.get_rating(home) - engine.get_rating(away)
        exp_home = 1.0 / (1.0 + 10 ** (-gap / 400.0))
        home_won = g["hs"] > g["as_"]

        if collect and engine.games(home) >= p["min_games"] and engine.games(away) >= p["min_games"]:
            win_home, _draw, _win_away = decompose_win_draw_loss(exp_home, p["draw_rate"])
            predictions.append((win_home, 1.0 if home_won else 0.0))

        margin = abs(g["hs"] - g["as_"])
        diff_winner = gap if home_won else -gap
        mult = mov_multiplier(margin, diff_winner) if p.get("mov") else 1.0
        actual = 1.0 if home_won else (0.0 if g["hs"] < g["as_"] else 0.5)
        engine.record_result(home, away, actual, k_multiplier=mult)
    return engine, predictions


def build_engine() -> tuple[EloEngine, int]:
    games = fetch_games()
    engine, _ = replay(games, params.get(SPORT))
    return engine, len(games)


def probability(engine: EloEngine, team_a: str, team_b: str) -> float | None:
    """P(team_a wins OUTRIGHT — a draw does not count), or None if either
    team lacks enough history to trust its rating."""
    p = params.get(SPORT)
    if engine.games(team_a) < p["min_games"] or engine.games(team_b) < p["min_games"]:
        return None
    raw = engine.probability(team_a, team_b)
    win_a, _draw, _win_b = decompose_win_draw_loss(raw, p["draw_rate"])
    return win_a
