"""Cached historical-results fetchers, shared by build_ratings.py,
backtest.py, and tune.py.

Why this exists: hyperparameter tuning re-runs the walk-forward backtest
many times per sport. Without a local cache that would mean re-fetching the
same thousands of games from MLB/ESPN on every candidate parameter value —
minutes per run and thousands of pointless requests. Fetchers here write
normalized game lists to data/cache/*.json; a cache chunk whose date range
ended more than CACHE_IMMUTABLE_AFTER_DAYS ago is treated as immutable and
never refetched (finished games don't change), while recent chunks are
refetched at most once per day.

ESPN note (verified live 2026-07-08): the scoreboard endpoints accept a
date RANGE (dates=YYYYMMDD-YYYYMMDD) plus a limit param — a month of NBA
comes back in one call (232 events with limit=400, vs a default cap of 100
without it). Fetchers chunk by month with limit=900 rather than walking
day by day like the first version of this project did.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("divergence_bot.elo.history")

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
CACHE_IMMUTABLE_AFTER_DAYS = 3
ESPN_LIMIT = 900


def _get_json(url: str, timeout: int = 30, headers: dict | None = None) -> dict | None:
    # Explicit User-Agent: several sources (OpenDota, bo3.gg, Fandom/
    # Leaguepedia) 403 the default "Python-urllib/x.y" UA outright —
    # verified live 2026-07-08; the same URLs work with any custom UA.
    # `headers` merges on top (e.g. a PandaScore Authorization bearer).
    req = urllib.request.Request(
        url, headers={"User-Agent": "DivergenceBot/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning(f"fetch failed: {url}: {e}")
        return None


def cached_chunk(key: str, chunk_end: date, fetch_fn) -> list:
    """Returns fetch_fn()'s result, cached under data/cache/{key}.json.
    Chunks that ended comfortably in the past never refetch; chunks that
    include recent days refetch at most once per calendar day.

    A FAILED refetch serves the stale cache instead of an empty list. The
    original behavior (return []) silently erased everything the chunk
    already knew: when statsapi.mlb.com started blocking the droplet's IP
    (~2026-07-10), the season-sized mlb_2026 chunk came back empty on every
    6h rebuild and MLB Elo quietly regressed to end-of-2025 ratings for a
    week — while the freshness guard showed 'fresh', because last_built
    kept updating. Stale-by-days beats missing-a-season, for every source."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    today = date.today()
    cached = None
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            fetched_on = cached.get("fetched_on", "")
            immutable = chunk_end < today - timedelta(days=CACHE_IMMUTABLE_AFTER_DAYS)
            if immutable or fetched_on == today.isoformat():
                return cached.get("games", [])
        except (json.JSONDecodeError, OSError):
            cached = None
    games = fetch_fn()
    if games is None:
        if cached is not None:
            stale_games = cached.get("games", [])
            log.warning(f"{key}: refetch failed — serving stale cache from "
                        f"{cached.get('fetched_on') or 'unknown date'} "
                        f"({len(stale_games)} games)")
            return stale_games
        return []
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_on": today.isoformat(), "games": games}, f)
    except OSError as e:
        log.warning(f"cache write failed for {key}: {e}")
    return games


def _month_chunks(start: date, end: date):
    """Yields (chunk_start, chunk_end) month-aligned chunks covering [start, end]."""
    cur = start
    while cur <= end:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


# ------------------------------------------------------------------
# MLB (statsapi.mlb.com) — one call per season, with probable pitchers
# ------------------------------------------------------------------

MLB_SCHEDULE = ("https://statsapi.mlb.com/api/v1/schedule?sportId=1"
                "&startDate={start}&endDate={end}&hydrate=probablePitcher")
# Regular season + postseason only; excludes spring training ("S"), other
# exhibitions ("E"), and All-Star ("A") — see elo/mlb.py for why.
MLB_REAL_GAME_TYPES = {"R", "F", "D", "L", "W"}
MLB_NAME_ALIASES = {"Oakland Athletics": "Athletics"}


def mlb_season(year: int) -> list[dict]:
    """Every finished real MLB game in `year`, chronological:
    {date, home, away, hs, as_, home_pitcher, away_pitcher} — pitcher fields
    are MLB player IDs (ints) or None. Verified live that
    hydrate=probablePitcher populates the scheduled starter on historical
    games too."""
    season_start = date(year, 3, 1)
    season_end = min(date(year, 11, 15), date.today())
    if season_start > date.today():
        return []

    def fetch():
        url = MLB_SCHEDULE.format(start=season_start.isoformat(), end=season_end.isoformat())
        data = _get_json(url)
        if data is None:
            return None
        games = []
        for d in data.get("dates", []):
            for g in d.get("games", []):
                if g.get("gameType") not in MLB_REAL_GAME_TYPES:
                    continue
                if (g.get("status") or {}).get("detailedState") != "Final":
                    continue
                teams = g.get("teams") or {}
                home_t = teams.get("home") or {}
                away_t = teams.get("away") or {}
                home = (home_t.get("team") or {}).get("name")
                away = (away_t.get("team") or {}).get("name")
                hs, as_ = home_t.get("score"), away_t.get("score")
                if not (home and away) or hs is None or as_ is None:
                    continue
                games.append({
                    "date": d.get("date"),
                    "home": MLB_NAME_ALIASES.get(home, home),
                    "away": MLB_NAME_ALIASES.get(away, away),
                    "hs": int(hs),
                    "as_": int(as_),
                    "home_pitcher": (home_t.get("probablePitcher") or {}).get("id"),
                    "away_pitcher": (away_t.get("probablePitcher") or {}).get("id"),
                })
        games.sort(key=lambda g: g["date"])
        return games

    return cached_chunk(f"mlb_{year}", season_end, fetch)


# ------------------------------------------------------------------
# MLB via ESPN (the CURRENT source, since 2026-07-18) — statsapi.mlb.com
# blocks the droplet's IP at the origin (406, all header variants, since
# ~2026-07-10), so results + probable starters now come from the same ESPN
# scoreboard family that already powers NBA/WNBA/WTA/FWC. Pitcher IDs are
# ESPN athlete ids — a different id space from statsapi, consistent across
# history and live probables, which is all the model needs. The statsapi
# fetchers above are kept for reference / in case the block ever lifts;
# nothing calls them.
# ------------------------------------------------------------------

ESPN_MLB_PATH = "baseball/mlb"
# season.type: 1 = spring training (excluded — different rosters/effort),
# 2 = regular, 3 = postseason. The All-Star game arrives as type 2, so the
# real-teams whitelist (same trick as NBA exhibitions) is what excludes it.
ESPN_MLB_SEASON_TYPES = {2, 3}


def _parse_espn_mlb_events(data: dict, real_teams: set[str] | None) -> list[dict]:
    """Finished MLB games in the shape replay() expects:
    {date, home, away, hs, as_, home_pitcher, away_pitcher} — pitcher ids
    are ESPN athlete ids (str) or None when ESPN lists no probable."""
    out = []
    for ev in (data or {}).get("events", []):
        if (ev.get("season") or {}).get("type") not in ESPN_MLB_SEASON_TYPES:
            continue
        comps = ev.get("competitions") or []
        if not comps:
            continue
        c = comps[0]
        if not ((c.get("status") or {}).get("type") or {}).get("completed"):
            continue
        by_side = {}
        for comp in c.get("competitors") or []:
            name = (comp.get("team") or {}).get("displayName")
            score = comp.get("score")
            side = comp.get("homeAway")
            if name is None or score is None or side not in ("home", "away"):
                continue
            # Era-accurate names must be unified BEFORE the whitelist check:
            # ESPN says "Oakland Athletics" on 2022-24 games and "Athletics"
            # from 2025 — without the alias the whitelist (current names)
            # silently drops every pre-move A's game (162/season).
            name = MLB_NAME_ALIASES.get(name, name)
            pid = None
            probables = comp.get("probables") or []
            if probables:
                pid = ((probables[0].get("athlete") or {}).get("id"))
            try:
                by_side[side] = (name, int(float(score)), str(pid) if pid else None)
            except (TypeError, ValueError):
                continue
        if "home" not in by_side or "away" not in by_side:
            continue
        h, a = by_side["home"], by_side["away"]
        if real_teams and (h[0] not in real_teams or a[0] not in real_teams):
            continue  # All-Star squads, exhibitions
        out.append({"date": (c.get("date") or ev.get("date") or "")[:10],
                    "home": h[0], "away": a[0], "hs": h[1], "as_": a[1],
                    "home_pitcher": h[2], "away_pitcher": a[2]})
    return out


def espn_mlb_games(start: date, end: date, sleep_sec: float = 0.1) -> list[dict]:
    """Every finished real MLB game in [start, end] from ESPN, chronological,
    with probable-starter ids. Month-chunked and cached like every other
    ESPN sport; a failed refetch serves the stale chunk (see cached_chunk)."""
    real_teams = espn_real_teams(ESPN_MLB_PATH)
    games = []
    for c_start, c_end in _month_chunks(start, min(end, date.today())):
        def fetch(c_start=c_start, c_end=c_end):
            url = ESPN_SCOREBOARD.format(path=ESPN_MLB_PATH,
                                         d1=c_start.strftime("%Y%m%d"),
                                         d2=c_end.strftime("%Y%m%d"), limit=ESPN_LIMIT)
            data = _get_json(url)
            if data is None:
                return None
            if sleep_sec:
                time.sleep(sleep_sec)
            return _parse_espn_mlb_events(data, real_teams)
        games.extend(cached_chunk(f"mlb_espn_{c_start.strftime('%Y%m')}", c_end, fetch))
    games.sort(key=lambda g: g["date"])
    return games


MLB_PITCHING = ("https://statsapi.mlb.com/api/v1/stats?stats=season&group=pitching"
                "&season={year}&sportId=1&limit=1100")


def mlb_pitching(year: int) -> dict:
    """{pitcher_id(str): {"era": float, "ip": float}} for a season — one bulk
    call (873 qualifying pitchers in 2025), cached. Used to SEED pitcher
    ratings from real prior-season quality instead of a flat 1500."""
    def fetch():
        data = _get_json(MLB_PITCHING.format(year=year))
        if data is None:
            return None
        out = {}
        for s in (data.get("stats") or [{}])[0].get("splits", []):
            pid = str((s.get("player") or {}).get("id") or "")
            st = s.get("stat") or {}
            try:
                era = float(st.get("era"))
                ip = float(st.get("inningsPitched"))
            except (TypeError, ValueError):
                continue
            if pid:
                out[pid] = {"era": era, "ip": ip}
        return out
    result = cached_chunk(f"mlb_pitching_{year}", date(year, 11, 15), fetch)
    return result if isinstance(result, dict) else {}


# ------------------------------------------------------------------
# ESPN team sports (NBA / WNBA / soccer) — month-range calls
# ------------------------------------------------------------------

ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
                   "?dates={d1}-{d2}&limit={limit}")
ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/{path}/teams?limit=50"


def espn_real_teams(path: str) -> set[str]:
    """Current official team list for a league (e.g. basketball/nba) — used
    to filter out All-Star exhibitions and international friendlies. Empty
    set means 'no whitelist available', callers should pass filtering."""
    data = _get_json(ESPN_TEAMS.format(path=path))
    try:
        teams = data["sports"][0]["leagues"][0]["teams"]
    except (KeyError, IndexError, TypeError):
        return set()
    return {t["team"]["displayName"] for t in teams if t.get("team", {}).get("displayName")}


def _parse_espn_team_events(data: dict, real_teams: set[str] | None) -> list[dict]:
    out = []
    for ev in (data or {}).get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        c = comps[0]
        if not ((c.get("status") or {}).get("type") or {}).get("completed"):
            continue
        by_side = {}
        for comp in c.get("competitors") or []:
            name = (comp.get("team") or {}).get("displayName")
            score = comp.get("score")
            side = comp.get("homeAway")
            if name is None or score is None or side not in ("home", "away"):
                continue
            try:
                by_side[side] = (name, float(score))
            except (TypeError, ValueError):
                continue
        if "home" not in by_side or "away" not in by_side:
            continue
        h, a = by_side["home"], by_side["away"]
        if real_teams and (h[0] not in real_teams or a[0] not in real_teams):
            continue
        out.append({"date": (c.get("date") or ev.get("date") or "")[:10],
                    "home": h[0], "away": a[0], "hs": h[1], "as_": a[1]})
    return out


def espn_team_games(path: str, cache_prefix: str, start: date, end: date,
                    real_teams: set[str] | None = None, sleep_sec: float = 0.1) -> list[dict]:
    """Every finished game for an ESPN team-sport league in [start, end]:
    {date, home, away, hs, as_}, chronological."""
    games = []
    for c_start, c_end in _month_chunks(start, min(end, date.today())):
        def fetch(c_start=c_start, c_end=c_end):
            url = ESPN_SCOREBOARD.format(path=path, d1=c_start.strftime("%Y%m%d"),
                                         d2=c_end.strftime("%Y%m%d"), limit=ESPN_LIMIT)
            data = _get_json(url)
            if data is None:
                return None
            if sleep_sec:
                time.sleep(sleep_sec)
            return _parse_espn_team_events(data, real_teams)
        games.extend(cached_chunk(f"{cache_prefix}_{c_start.strftime('%Y%m')}", c_end, fetch))
    games.sort(key=lambda g: g["date"])
    return games


# ------------------------------------------------------------------
# ESPN tennis — month-range calls, deduped by competition id
# ------------------------------------------------------------------

def _parse_espn_tennis(data: dict, grouping_slug: str) -> dict[str, dict]:
    out = {}
    for ev in (data or {}).get("events", []):
        tournament = ev.get("name") or ""
        for grouping in ev.get("groupings", []) or []:
            if (grouping.get("grouping") or {}).get("slug") != grouping_slug:
                continue
            for c in grouping.get("competitions", []) or []:
                if not ((c.get("status") or {}).get("type") or {}).get("completed"):
                    continue
                comp_id, comp_date = c.get("id"), c.get("date")
                winner = loser = None
                for comp in c.get("competitors") or []:
                    name = (comp.get("athlete") or {}).get("displayName")
                    if name is None:
                        continue
                    if comp.get("winner"):
                        winner = name
                    else:
                        loser = name
                if comp_id and comp_date and winner and loser:
                    out[comp_id] = {"date": comp_date, "winner": winner,
                                    "loser": loser, "tournament": tournament}
    return out


def espn_tennis_matches(tour: str, grouping_slug: str, start: date, end: date,
                        sleep_sec: float = 0.1) -> list[dict]:
    """Every completed singles match for tour ("atp"/"wta") in [start, end]:
    {date, winner, loser, tournament}, chronological. ESPN returns a
    tournament's ENTIRE match list for any date inside its span, so matches
    are deduped by competition id across chunks."""
    matches: dict[str, dict] = {}
    for c_start, c_end in _month_chunks(start, min(end, date.today())):
        def fetch(c_start=c_start, c_end=c_end):
            url = ESPN_SCOREBOARD.format(path=f"tennis/{tour}", d1=c_start.strftime("%Y%m%d"),
                                         d2=c_end.strftime("%Y%m%d"), limit=ESPN_LIMIT)
            data = _get_json(url)
            if data is None:
                return None
            if sleep_sec:
                time.sleep(sleep_sec)
            return list(_parse_espn_tennis(data, grouping_slug).values())
        for m in cached_chunk(f"tennis_{tour}_{c_start.strftime('%Y%m')}", c_end, fetch):
            # dedupe across chunks: same match appears in every chunk that
            # overlaps its tournament's span, so key on (date, winner, loser)
            matches[(m["date"], m["winner"], m["loser"])] = m
    return sorted(matches.values(), key=lambda m: m["date"])
