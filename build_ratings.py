"""CLI: builds/refreshes every sport's Elo ratings and caches them to
elo_ratings.json (read by divergence_bot.py and backtest.py).

Ratings go stale as new games are played, so re-run this periodically (e.g.
daily or weekly via cron / Windows Task Scheduler) — divergence_bot.py itself
never hits the sport data sources, it only reads the cached JSON.

Usage:
    python build_ratings.py            # rebuild every sport
    python build_ratings.py mlb nba    # rebuild just these (merges into the
                                        # existing file, doesn't wipe the rest)
"""

import json
import logging
import sys
from datetime import date, datetime, timezone

import config
from elo import basketball, esports, mlb, params, soccer, tennis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_ratings")

RATINGS_FILE = "elo_ratings.json"
FRESHNESS_FILE = "elo_freshness.json"


def _sanity_check(name: str, engine, n_games: int):
    if n_games == 0:
        log.warning(f"{name}: 0 games processed — ratings will be empty, model unusable for this sport until fixed")
        return
    ratings = list(engine.ratings.values())
    spread = max(ratings) - min(ratings) if ratings else 0
    log.info(f"{name}: {n_games} games, {len(ratings)} teams/players rated, rating spread {spread:.0f}")
    if spread < 50 and n_games > 20:
        log.warning(f"{name}: rating spread is suspiciously small ({spread:.0f}) for {n_games} games — check ingestion for a bug")


def _record(freshness: dict, name: str, engine, n_games: int, latest_date: str | None):
    """Log the sanity check AND capture freshness metadata so the live bot can
    tell whether a sport's ratings actually refreshed (and how current the
    underlying games are), instead of silently trading on stale data."""
    _sanity_check(name, engine, n_games)
    freshness[name] = {
        "last_built": datetime.now(timezone.utc).isoformat(),
        "n_games": n_games,
        "n_rated": len(engine.ratings),
        "latest_game_date": (latest_date or "")[:10] or None,
    }


def build_all(sports: list[str] | None = None):
    sports = sports or ["mlb", "nba", "wnba", "fwc", "atp", "wta", "itf",
                        "dota2", "cs2", "lol", "valorant"]
    out = {}
    freshness = {}

    for title in esports.TITLES:
        if title in sports:
            engine, n = esports.build_engine(title)
            out[title] = engine.to_dict()
            _record(freshness, title, engine, n, esports.store_latest_date(title))
            if title == "dota2":
                # accumulate OpenDota match ids + per-match lineups for the
                # future player model (collection only; nothing live reads it)
                try:
                    esports.deepen_dota_player_data()
                except Exception as e:
                    log.warning(f"dota2 player-data collection skipped: {e}")
            if title == "cs2":
                # forward-only bo3.gg lineup collection (same purpose as dota2)
                try:
                    esports.deepen_cs2_player_data()
                except Exception as e:
                    log.warning(f"cs2 player-data collection skipped: {e}")
            if title == "lol":
                # LoL player-level model sidecar (Oracle's Elixir); goes live
                # for LoL automatically once its data is fresh (see config).
                try:
                    from elo import lol_players
                    model = lol_players.build_live_model()
                    if model:
                        with open("lol_player_model.json", "w") as f:
                            json.dump(model, f)
                        log.info(f"Wrote lol_player_model.json (through {model['latest_date']}, "
                                 f"{len(model['ratings'])} players)")
                except Exception as e:
                    log.warning(f"LoL player-model build skipped: {e}")

    if "mlb" in sports:
        games = mlb.fetch_games(config.MLB_START_YEAR)
        engine, _ = mlb.replay(games, params.get("mlb"))
        out["mlb"] = engine.to_dict()
        _record(freshness, "mlb", engine, len(games),
                max((g["date"] for g in games), default=None))

    for league, start in (("nba", config.NBA_START_DATE), ("wnba", config.WNBA_START_DATE)):
        if league in sports:
            engine, n = basketball.build_engine(league, start, date.today())
            latest = max(engine.extras.get("last_played", {}).values(), default=None)
            out[league] = engine.to_dict()
            _record(freshness, league, engine, n, latest)

    if "fwc" in sports:
        games = soccer.fetch_games()
        engine, _ = soccer.replay(games, params.get("fwc"))
        out["fwc"] = engine.to_dict()
        _record(freshness, "fwc", engine, len(games),
                max((g["date"] for g in games), default=None))

    for tour in ("atp", "wta"):
        if tour in sports:
            matches = tennis.fetch_matches_for(tour, config.TENNIS_START_DATE, date.today(),
                                               config.TENNIS_TML_START_YEAR)
            engine, _ = tennis.replay(matches, params.get(tour))
            out[tour] = engine.to_dict()
            _record(freshness, tour, engine, len(matches),
                    max((m["date"] for m in matches), default=None))

    if "itf" in sports:
        engine, n = tennis.build_engine_csv("data/tennis/itf")
        out["itf"] = engine.to_dict()
        _record(freshness, "itf", engine, n, None)

    # Merge into the existing files so a partial rebuild (e.g. just `mlb`)
    # doesn't wipe out other sports' already-cached ratings or freshness.
    _merge_write(RATINGS_FILE, out)
    _merge_write(FRESHNESS_FILE, freshness)
    log.info(f"Wrote {RATINGS_FILE} + {FRESHNESS_FILE} ({', '.join(out)})")


def _merge_write(path: str, new: dict):
    existing = {}
    try:
        with open(path) as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    existing.update(new)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


if __name__ == "__main__":
    requested = [s.lower() for s in sys.argv[1:]] or None
    build_all(requested)
