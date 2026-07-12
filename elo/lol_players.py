"""LoL PLAYER-level Elo pilot — the first model here that rates players,
not team names, so 'who is actually playing tonight' is the model input.

Data: Leaguepedia ScoreboardPlayers (one row per player per GAME, with
Link/Team/TeamVs/PlayerWin/GameId/DateTime_UTC). Heavily rate-limited — a
sustained anonymous backfill stalls (verified 2026-07-09), so this path is
only viable in small resumable chunks (fetch_lol_players.py, major leagues
only, months cached immutably). BETTER PATH (not yet wired): Oracle's Elixir
(oracleselixir.com) publishes yearly bulk CSVs, 12 rows/game = 2 teams + 5
players each, no rate limiting — the standard free LoL analytics dataset.
Its old S3 bucket (oracleselixir-downloadable-match-data) is dead as of
2026-07; the current download URL must be pulled from their JS-rendered
downloads page or a recent pipeline repo. Once obtained, feed its rows
through rows_to_games() (same shape) and the comparison below runs
unchanged.

Model: each player carries a rating; a team's strength for a game is the
MEAN of the ratings of the players who actually played it (mean, not sum,
so a 4-player row or a stand-in doesn't break the scale). Prediction is the
standard Elo logistic on the strength gap; after the game every player on
the winning lineup gains K*(1-exp) and every loser loses the same — the
attribution is deliberately naive (no per-player stats), which means a weak
player on a strong team gets carried upward. That noise is the known cost;
the payoff is that roster changes price in automatically.

Verdict rule (honest): replay_team() runs plain team-name Elo over the SAME
games with the SAME prediction-gating, and whichever wins the walk-forward
Brier is what the live bot should use. No 'fancier must be better'.
"""

import logging
import time
import urllib.parse
from datetime import date, timedelta

from elo import history
from elo.engine import EloEngine

log = logging.getLogger("divergence_bot.elo.lol_players")

SP_URL = ("https://lol.fandom.com/api.php?action=cargoquery&format=json"
          "&tables=ScoreboardPlayers&fields=Link,Team,TeamVs,PlayerWin,GameId,DateTime_UTC"
          "&where={where}&limit=500&offset={offset}")
BACKFILL_DAYS = 365
DEFAULT_RATING = 1500.0

# Major circuits only. Two reasons: (1) that's what Polymarket lists (its
# LoL slugs are LCK/LPL-tier teams), and (2) Leaguepedia throttles sustained
# anonymous pulls hard enough that the ~80% of rows coming from minor
# regional leagues cost more in rate-limit stalls than they add in signal.
# OverviewPage LIKE patterns; names per the 2025-26 circuit reorg
# (LTA absorbed LCS, LCP is the APAC league, First Stand is the new
# international event).
MAJOR_LEAGUE_LIKES = (
    "LCK%", "LPL%", "LEC%", "LTA%", "LCS%", "LCP%",
    "%Worlds%", "%Mid-Season Invitational%", "%First Stand%",
)


def _league_filter() -> str:
    ors = " OR ".join(f"OverviewPage LIKE '{p}'" for p in MAJOR_LEAGUE_LIKES)
    return f"({ors})"


def _query(where: str, offset: int, max_retries: int = 12) -> list | None:
    """One cargoquery with patient rate-limit backoff (anonymous Fandom
    clients get throttled hard; waiting works, hammering doesn't)."""
    url = SP_URL.format(where=urllib.parse.quote(where), offset=offset)
    wait = 45
    for _ in range(max_retries):
        data = history._get_json(url, timeout=40)
        if data is None:
            time.sleep(wait)
            wait = min(wait * 2, 120)
            continue
        if data.get("error"):
            if data["error"].get("code") == "ratelimited":
                log.info(f"Leaguepedia rate-limited — waiting {wait}s")
                time.sleep(wait)
                wait = min(wait * 2, 120)
                continue
            log.warning(f"Leaguepedia error: {data['error']}")
            return None
        return data.get("cargoquery") or []
    return None


def _fetch_month(c_start: date, c_end: date) -> list | None:
    rows_out = []
    offset = 0
    while True:
        where = (f"DateTime_UTC >= '{c_start} 00:00:00' AND "
                 f"DateTime_UTC <= '{c_end} 23:59:59' AND {_league_filter()}")
        rows = _query(where, offset)
        if rows is None:
            return None  # don't cache a partial month
        for r in rows:
            t = r.get("title") or {}
            if all(t.get(k) for k in ("Link", "Team", "GameId", "DateTime UTC")):
                rows_out.append([t["DateTime UTC"], t["GameId"], t["Team"],
                                 t["Link"], t.get("PlayerWin") or ""])
        if len(rows) < 500:
            return rows_out
        offset += 500
        time.sleep(8.0)  # gentle: sustained anonymous pulls get throttled hard


def fetch_player_rows() -> list:
    """[[datetime, game_id, team, player_link, player_win], ...] across the
    backfill window, month-cached (immutable once a month is in the past)."""
    rows = []
    start = date.today() - timedelta(days=BACKFILL_DAYS)
    for c_start, c_end in history._month_chunks(start, date.today()):
        got = history.cached_chunk(f"lol_players_{c_start.strftime('%Y%m')}", c_end,
                                   lambda cs=c_start, ce=c_end: _fetch_month(cs, ce))
        rows.extend(got)
        time.sleep(1.0)
    return rows


def load_oe_games(csv_dir: str = "data/oe") -> list[dict]:
    """Oracle's Elixir CSVs -> game dicts {date, teams: {name: [players]},
    winner}, the same shape rows_to_games() produces, so the comparison
    harness is source-agnostic. OE lays each game out as 12 rows: 10 player
    rows (position in top/jng/mid/bot/sup) plus 2 team-summary rows
    (position == 'team', skipped here). `result` is 1 for the winning side.
    Bulk CSV download — no rate limiting, unlike Leaguepedia."""
    import csv as _csv
    import glob
    from pathlib import Path

    by_game: dict[str, dict] = {}
    for path in sorted(glob.glob(str(Path(csv_dir) / "*.csv"))):
        with open(path, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if (row.get("position") or "").lower() == "team":
                    continue
                gid = row.get("gameid")
                player = row.get("playername")
                team = row.get("teamname")
                if not (gid and player and team):
                    continue
                g = by_game.setdefault(gid, {"date": row.get("date") or "", "teams": {}, "winner": None})
                g["teams"].setdefault(team, []).append(player)
                if str(row.get("result")) == "1":
                    g["winner"] = team
    games = [g for g in by_game.values()
             if len(g["teams"]) == 2 and g["winner"] in g["teams"]
             and all(len(v) >= 3 for v in g["teams"].values())]  # drop malformed/partial rows
    games.sort(key=lambda g: g["date"])
    log.info(f"Oracle's Elixir: {len(games)} games loaded from {csv_dir}")
    return games


def rows_to_games(rows: list) -> list[dict]:
    """Groups player rows into games: {date, teams: {name: [players]},
    winner}. Skips games without exactly two teams or a clear winner."""
    by_game: dict[str, dict] = {}
    for dt, gid, team, link, win in rows:
        g = by_game.setdefault(gid, {"date": dt, "teams": {}, "winner": None})
        g["teams"].setdefault(team, []).append(link)
        if win == "Yes":
            g["winner"] = team
    games = []
    for g in by_game.values():
        if len(g["teams"]) == 2 and g["winner"] in g["teams"]:
            games.append(g)
    games.sort(key=lambda g: g["date"])
    return games


def _mean(vals):
    return sum(vals) / len(vals)


def replay_players(games: list[dict], k: float = 24.0, min_lineup_games: float = 5.0,
                   collect: bool = True) -> tuple[dict, list]:
    """Player-Elo walk-forward. Prediction gating: each lineup's AVERAGE
    player experience (games in our data) must be >= min_lineup_games —
    the player-level analogue of the team model's min_games."""
    ratings: dict[str, float] = {}
    played: dict[str, int] = {}
    predictions = []
    for g in games:
        (t1, p1), (t2, p2) = sorted(g["teams"].items())
        r1 = _mean([ratings.get(x, DEFAULT_RATING) for x in p1])
        r2 = _mean([ratings.get(x, DEFAULT_RATING) for x in p2])
        exp1 = 1.0 / (1.0 + 10 ** ((r2 - r1) / 400.0))
        if collect:
            e1 = _mean([played.get(x, 0) for x in p1])
            e2 = _mean([played.get(x, 0) for x in p2])
            if e1 >= min_lineup_games and e2 >= min_lineup_games:
                predictions.append((exp1, 1.0 if g["winner"] == t1 else 0.0))
        actual1 = 1.0 if g["winner"] == t1 else 0.0
        delta = k * (actual1 - exp1)
        for x in p1:
            ratings[x] = ratings.get(x, DEFAULT_RATING) + delta
            played[x] = played.get(x, 0) + 1
        for x in p2:
            ratings[x] = ratings.get(x, DEFAULT_RATING) - delta
            played[x] = played.get(x, 0) + 1
    return ratings, predictions


def build_player_ratings(csv_dir: str = "data/oe", k: float = 48.0) -> tuple[dict, dict, str]:
    """Production build: final player ratings + games-played from all OE CSVs
    in csv_dir (walk-forward isn't needed here — this is the current-state
    rating after replaying all history). Returns (ratings, played, latest_date).
    k=48 was the backtest-winning value (Brier 0.2253 vs team 0.2300)."""
    games = load_oe_games(csv_dir)
    if not games:
        return {}, {}, ""
    ratings, played = {}, {}
    for g in games:
        (t1, p1), (t2, p2) = sorted(g["teams"].items())
        r1 = _mean([ratings.get(x, DEFAULT_RATING) for x in p1])
        r2 = _mean([ratings.get(x, DEFAULT_RATING) for x in p2])
        exp1 = 1.0 / (1.0 + 10 ** ((r2 - r1) / 400.0))
        delta = k * ((1.0 if g["winner"] == t1 else 0.0) - exp1)
        for x in p1:
            ratings[x] = ratings.get(x, DEFAULT_RATING) + delta
            played[x] = played.get(x, 0) + 1
        for x in p2:
            ratings[x] = ratings.get(x, DEFAULT_RATING) - delta
            played[x] = played.get(x, 0) + 1
    latest = games[-1]["date"][:10]
    log.info(f"LoL player ratings: {len(ratings)} players from {len(games)} games "
             f"(latest {latest})")
    return ratings, played, latest


def build_live_model(csv_dir: str = "data/oe", k: float = 48.0) -> dict:
    """Full live LoL player model saved as a sidecar: player ratings +
    each team's MOST RECENT lineup (the live 'who plays' proxy, straight
    from OE — no Leaguepedia rate limits) + the latest data date (for the
    freshness gate). Empty dict if no OE data present."""
    games = load_oe_games(csv_dir)
    if not games:
        return {}
    ratings, played, latest = build_player_ratings(csv_dir, k)
    team_lineups: dict[str, list[str]] = {}
    for g in games:  # chronological, so the last write per team is its newest lineup
        for team, players in g["teams"].items():
            team_lineups[team] = players
    return {"ratings": ratings, "played": played, "team_lineups": team_lineups,
            "latest_date": latest, "k": k}


def team_strength(ratings: dict, players: list[str]) -> float | None:
    """Mean rating of a lineup's players. None if none are rated."""
    known = [ratings[x] for x in players if x in ratings]
    return _mean(known) if known else None


def probability_players(ratings: dict, lineup_a: list[str], lineup_b: list[str],
                        min_known: int = 3) -> float | None:
    """P(lineup_a beats lineup_b) from player ratings. None unless at least
    min_known of each five are rated (else the mean is unreliable)."""
    ka = [ratings[x] for x in lineup_a if x in ratings]
    kb = [ratings[x] for x in lineup_b if x in ratings]
    if len(ka) < min_known or len(kb) < min_known:
        return None
    gap = _mean(ka) - _mean(kb)
    return 1.0 / (1.0 + 10 ** (-gap / 400.0))


def replay_team(games: list[dict], k: float = 40.0, min_lineup_games: float = 5.0) -> list:
    """Team-name Elo over the SAME games with the SAME gating rule, so the
    two models' Briers are compared on an identical prediction set."""
    engine = EloEngine(k_factor=k)
    played: dict[str, int] = {}
    predictions = []
    for g in games:
        (t1, p1), (t2, p2) = sorted(g["teams"].items())
        exp1 = engine.probability(t1, t2)
        e1 = _mean([played.get(x, 0) for x in p1])
        e2 = _mean([played.get(x, 0) for x in p2])
        if e1 >= min_lineup_games and e2 >= min_lineup_games:
            predictions.append((exp1, 1.0 if g["winner"] == t1 else 0.0))
        engine.record_result(t1, t2, 1.0 if g["winner"] == t1 else 0.0)
        for x in p1 + p2:
            played[x] = played.get(x, 0) + 1
    return predictions
