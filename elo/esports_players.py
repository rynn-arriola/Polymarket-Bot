"""Historical player-lineup bootstraps for Dota 2 and CS2.

The bulk datasets are used only for immutable match facts: date, two teams,
winner, and player identities. Provider ratings and aggregate statistics are
deliberately ignored. Games are returned in the same shape as the LoL player
model so rating and evaluation code can remain source-agnostic.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from elo import params

log = logging.getLogger("divergence_bot.elo.esports_players")

DEFAULT_RATING = 1500.0
DATA_DIRS = {
    "dota2": Path("data/dota2_pro"),
    "cs2": Path("data/cs2_demo"),
}
HISTORICAL_FILES = {
    "dota2": DATA_DIRS["dota2"] / "dota2_matches.parquet",
    "cs2": DATA_DIRS["cs2"] / "manifest.json",
}
FORWARD_FILES = {
    "dota2": Path("data/cache/esports_dota2_lineups.json"),
    "cs2": Path("data/cache/esports_cs2_lineups.json"),
}


def cs2_player_key(value) -> str:
    """Stable-enough CS2 identity shared by parsed demos and bo3.gg."""
    raw = str(value or "").strip()
    if raw.startswith("cs2:"):
        return raw
    name = re.sub(r"\s+", " ", raw).casefold()
    return f"cs2:{name}" if name else ""


def dota_player_key(value) -> str:
    if value is None or str(value).strip() in ("", "0", "nan"):
        return ""
    text = str(value).strip()
    if text.startswith("dota2:"):
        return text
    if text.endswith(".0"):
        text = text[:-2]
    return f"dota2:{text}"


def _valid_game(game: dict) -> bool:
    teams = game.get("teams") or {}
    return (len(teams) == 2 and game.get("winner") in teams
            and all(len(set(players)) >= 3 for players in teams.values()))


def load_dota_history(path: str | Path = HISTORICAL_FILES["dota2"]) -> list[dict]:
    """Read the exact columns needed from the Dota parquet bootstrap."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("Dota bootstrap requires pyarrow; install requirements.txt") from exc

    columns = [
        "match_id", "match_start_date_time",
        "radiant_team_id", "radiant_team_name", "dire_team_id", "dire_team_name",
        "winner_id",
        *[f"{side}_player_{slot}_id"
          for side in ("radiant", "dire") for slot in range(1, 6)],
    ]
    data = parquet.read_table(path, columns=columns).to_pydict()
    games_by_id: dict[str, dict] = {}
    for row in range(len(data["match_id"])):
        radiant_id, dire_id = data["radiant_team_id"][row], data["dire_team_id"][row]
        winner_id = data["winner_id"][row]
        if winner_id not in (radiant_id, dire_id):
            continue
        radiant_name = str(data["radiant_team_name"][row] or "").strip()
        dire_name = str(data["dire_team_name"][row] or "").strip()
        if not radiant_name or not dire_name or radiant_name == dire_name:
            continue
        radiant = [dota_player_key(data[f"radiant_player_{slot}_id"][row])
                   for slot in range(1, 6)]
        dire = [dota_player_key(data[f"dire_player_{slot}_id"][row])
                for slot in range(1, 6)]
        radiant, dire = [p for p in radiant if p], [p for p in dire if p]
        winner = radiant_name if winner_id == radiant_id else dire_name
        stamp = data["match_start_date_time"][row]
        date = stamp.isoformat() if isinstance(stamp, datetime) else str(stamp or "")
        game = {"date": date, "teams": {radiant_name: radiant, dire_name: dire},
                "winner": winner, "source": "dota2-kaggle",
                "source_id": str(data["match_id"][row])}
        if _valid_game(game):
            games_by_id[str(data["match_id"][row])] = game
    games = sorted(games_by_id.values(), key=lambda game: game["date"])
    log.info("Dota bootstrap: %s valid games from %s", len(games), path)
    return games


def dota_historical_ids(path: str | Path = HISTORICAL_FILES["dota2"]) -> set[str]:
    """OpenDota match ids already supplied by the bulk archive."""
    path = Path(path)
    if not path.exists():
        return set()
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("Dota bootstrap requires pyarrow; install requirements.txt") from exc
    return {str(match_id) for match_id in parquet.read_table(path, columns=["match_id"])
            .column("match_id").to_pylist() if match_id is not None}


def _cs2_paths(path: str | Path) -> list[Path]:
    path = Path(path)
    if path.is_file():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            return [path.parent / name for name in manifest.get("files", [])
                    if (path.parent / name).exists()]
        except (json.JSONDecodeError, OSError):
            return []
    if path.is_dir():
        return sorted(path.glob("*.parquet"))
    return []


def _roster_index(lineup: list[str], anchors: list[set[str]]) -> int | None:
    overlaps = [len(set(lineup) & anchor) for anchor in anchors]
    if max(overlaps, default=0) < 3 or overlaps[0] == overlaps[1]:
        return None
    return 0 if overlaps[0] > overlaps[1] else 1


def load_cs2_history(path: str | Path = HISTORICAL_FILES["cs2"]) -> list[dict]:
    """Load replay-grounded CS2 map lineups from Hugging Face metadata.

    The demo rows identify starting CT/T rosters rather than team1/team2. We
    cluster rosters by player overlap across a series, then accept the mapping
    only when its map-win totals exactly reproduce the published series score.
    """
    paths = _cs2_paths(path)
    if not paths:
        return []
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("CS2 bootstrap requires pyarrow; install requirements.txt") from exc
    columns = ["match_id", "match_date", "team1", "team2", "score1", "score2",
               "map_index", "winner_side", "teams"]
    rows_by_match: dict[str, list[dict]] = defaultdict(list)
    seen_maps = set()
    for row in parquet.read_table(paths, columns=columns).to_pylist():
        key = (str(row.get("match_id")), row.get("map_index"))
        if key not in seen_maps:
            seen_maps.add(key)
            rows_by_match[key[0]].append(row)

    games = []
    for match_id, rows in rows_by_match.items():
        rows.sort(key=lambda row: row.get("map_index") or 0)
        first_sides = rows[0].get("teams") or []
        if len(first_sides) != 2:
            continue
        anchors = []
        for side in first_sides:
            anchors.append({cs2_player_key(player.get("name"))
                            for player in side.get("players") or [] if player.get("name")})
        if min(map(len, anchors)) < 3:
            continue
        prepared, roster_wins = [], [0, 0]
        valid = True
        for row in rows:
            sides, side_to_roster = {}, {}
            for side in row.get("teams") or []:
                lineup = [cs2_player_key(player.get("name"))
                          for player in side.get("players") or [] if player.get("name")]
                roster = _roster_index(lineup, anchors)
                start_side = (side.get("side_start") or "").lower()
                if roster is None or start_side not in ("ct", "t") or roster in sides:
                    valid = False
                    break
                sides[roster] = lineup
                side_to_roster[start_side] = roster
            winner_side = (row.get("winner_side") or "").lower()
            winner_roster = side_to_roster.get(winner_side)
            if not valid or len(sides) != 2 or winner_roster is None:
                valid = False
                break
            roster_wins[winner_roster] += 1
            prepared.append((row, sides, winner_roster))
        if not valid:
            continue
        score1, score2 = rows[0].get("score1"), rows[0].get("score2")
        if roster_wins == [score1, score2]:
            roster_to_team = {0: rows[0].get("team1"), 1: rows[0].get("team2")}
        elif roster_wins == [score2, score1]:
            roster_to_team = {0: rows[0].get("team2"), 1: rows[0].get("team1")}
        else:
            continue
        if not all(roster_to_team.values()) or roster_to_team[0] == roster_to_team[1]:
            continue
        for row, sides, winner_roster in prepared:
            stamp = row.get("match_date")
            date = stamp.isoformat() if isinstance(stamp, datetime) else str(stamp or "")
            game = {"date": date,
                    "teams": {roster_to_team[0]: sides[0], roster_to_team[1]: sides[1]},
                    "winner": roster_to_team[winner_roster], "source": "cs2-demo",
                    "source_id": f"{match_id}:{row.get('map_index') or 0}",
                    "sort_key": (date, match_id, row.get("map_index") or 0)}
            if _valid_game(game):
                games.append(game)
    games.sort(key=lambda game: game["sort_key"])
    for game in games:
        game.pop("sort_key", None)
    log.info("CS2 demo bootstrap: %s valid maps from %s matches", len(games), len(rows_by_match))
    return games


def load_forward_games(title: str, path: str | Path | None = None) -> list[dict]:
    path = Path(path) if path else FORWARD_FILES[title]
    try:
        with open(path, encoding="utf-8") as handle:
            rows = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    games = []
    for source_id, raw in rows.items():
        raw_players = [str(player) for players in (raw.get("teams") or {}).values()
                       for player in players]
        if title == "cs2" and any(not player.startswith("cs2:") for player in raw_players):
            continue  # legacy numeric ids; the collector refetches these by nickname
        teams = {}
        for team, players in (raw.get("teams") or {}).items():
            key_fn = dota_player_key if title == "dota2" else cs2_player_key
            normalized = [key_fn(player) for player in players]
            teams[team] = [player for player in normalized if player]
        game = {"date": raw.get("date") or "", "teams": teams,
                "winner": raw.get("winner"), "source": f"{title}-forward",
                "source_id": str(source_id)}
        if _valid_game(game):
            games.append(game)
    return sorted(games, key=lambda game: game["date"])


def merge_games(title: str, historical: list[dict], forward: list[dict]) -> list[dict]:
    """Prefer bulk records when the forward collector overlaps them."""
    if title == "dota2":
        historical_ids = {game.get("source_id") for game in historical}
        forward = [game for game in forward if game.get("source_id") not in historical_ids]
    elif title == "cs2":
        # Demo rows are maps while bo3 rows are series and use unrelated ids.
        # Prefer the exact demo maps for an overlapping day/team matchup.
        demo_matchups = {(game["date"][:10], frozenset(game["teams"]))
                         for game in historical}
        forward = [game for game in forward
                   if (game["date"][:10], frozenset(game["teams"])) not in demo_matchups]
    else:
        raise ValueError(f"unsupported player bootstrap: {title}")
    seen, games = set(), []
    for game in sorted(historical + forward, key=lambda item: item["date"]):
        signature = (game.get("source"), game.get("source_id"))
        if signature not in seen:
            seen.add(signature)
            games.append(game)
    return games


def load_games(title: str) -> list[dict]:
    if title == "dota2":
        historical = load_dota_history()
    elif title == "cs2":
        historical = load_cs2_history()
    else:
        raise ValueError(f"unsupported player bootstrap: {title}")
    return merge_games(title, historical, load_forward_games(title))


def audit_games(games: list[dict]) -> dict:
    dates = sorted({game["date"][:10] for game in games if game.get("date")})
    largest_gap = 0
    gap_after = None
    for before, after in zip(dates, dates[1:]):
        try:
            gap = (datetime.fromisoformat(after) - datetime.fromisoformat(before)).days
        except ValueError:
            continue
        if gap > largest_gap:
            largest_gap, gap_after = gap, before
    return {
        "n_games": len(games),
        "earliest_date": dates[0] if dates else None,
        "latest_date": dates[-1] if dates else None,
        "largest_gap_days": largest_gap,
        "largest_gap_after": gap_after,
        "sources": sorted({game.get("source") for game in games if game.get("source")}),
    }


def walk_forward(games: list[dict], player_k: float, team_k: float,
                 min_team_games: int = 8, min_player_games: float = 5.0) -> list[dict]:
    """Emit player and team Elo predictions strictly before each update."""
    ratings, played, team_ratings, team_played = {}, {}, {}, {}
    predictions = []
    for index, game in enumerate(games):
        (team1, lineup1), (team2, lineup2) = sorted(game["teams"].items())
        player1 = sum(ratings.get(p, DEFAULT_RATING) for p in lineup1) / len(lineup1)
        player2 = sum(ratings.get(p, DEFAULT_RATING) for p in lineup2) / len(lineup2)
        player_exp = 1.0 / (1.0 + 10 ** ((player2 - player1) / 400.0))
        team1_rating = team_ratings.get(team1, DEFAULT_RATING)
        team2_rating = team_ratings.get(team2, DEFAULT_RATING)
        team_exp = 1.0 / (1.0 + 10 ** ((team2_rating - team1_rating) / 400.0))
        experience1 = sum(played.get(p, 0) for p in lineup1) / len(lineup1)
        experience2 = sum(played.get(p, 0) for p in lineup2) / len(lineup2)
        if (team_played.get(team1, 0) >= min_team_games
                and team_played.get(team2, 0) >= min_team_games
                and experience1 >= min_player_games and experience2 >= min_player_games):
            predictions.append({"index": index, "date": game["date"],
                                "player": player_exp, "team": team_exp,
                                "actual": 1.0 if game["winner"] == team1 else 0.0})
        actual = 1.0 if game["winner"] == team1 else 0.0
        player_delta = player_k * (actual - player_exp)
        for player in lineup1:
            ratings[player] = ratings.get(player, DEFAULT_RATING) + player_delta
            played[player] = played.get(player, 0) + 1
        for player in lineup2:
            ratings[player] = ratings.get(player, DEFAULT_RATING) - player_delta
            played[player] = played.get(player, 0) + 1
        team_delta = team_k * (actual - team_exp)
        team_ratings[team1], team_ratings[team2] = team1_rating + team_delta, team2_rating - team_delta
        team_played[team1] = team_played.get(team1, 0) + 1
        team_played[team2] = team_played.get(team2, 0) + 1
    return predictions


def build_model(title: str, player_k: float) -> dict:
    """Build research state only. No live prediction path reads this model."""
    games = load_games(title)
    if not games:
        return {}
    team_k = params.get(title)["k"]
    ratings, played, team_ratings, team_played, lineups = {}, {}, {}, {}, {}
    for game in games:
        (team1, lineup1), (team2, lineup2) = sorted(game["teams"].items())
        player1 = sum(ratings.get(p, DEFAULT_RATING) for p in lineup1) / len(lineup1)
        player2 = sum(ratings.get(p, DEFAULT_RATING) for p in lineup2) / len(lineup2)
        player_exp = 1.0 / (1.0 + 10 ** ((player2 - player1) / 400.0))
        actual = 1.0 if game["winner"] == team1 else 0.0
        player_delta = player_k * (actual - player_exp)
        for player in lineup1:
            ratings[player] = ratings.get(player, DEFAULT_RATING) + player_delta
            played[player] = played.get(player, 0) + 1
        for player in lineup2:
            ratings[player] = ratings.get(player, DEFAULT_RATING) - player_delta
            played[player] = played.get(player, 0) + 1
        rating1, rating2 = team_ratings.get(team1, DEFAULT_RATING), team_ratings.get(team2, DEFAULT_RATING)
        team_exp = 1.0 / (1.0 + 10 ** ((rating2 - rating1) / 400.0))
        team_delta = team_k * (actual - team_exp)
        team_ratings[team1], team_ratings[team2] = rating1 + team_delta, rating2 - team_delta
        team_played[team1] = team_played.get(team1, 0) + 1
        team_played[team2] = team_played.get(team2, 0) + 1
        for team, lineup in game["teams"].items():
            lineups[team] = lineup
    return {"title": title, "ratings": ratings, "played": played,
            "team_lineups": lineups, "team_elo": team_ratings, "team_games": team_played,
            "latest_date": games[-1]["date"][:10], "player_k": player_k,
            "team_k": team_k, "audit": audit_games(games), "eligible_for_live": False}
