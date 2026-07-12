"""Key-player injury awareness for NBA/WNBA (ESPN public feeds, verified
live 2026-07-08).

Why this exists: an Elo rating reflects the roster that EARNED it. When a
team's star is out tonight, the market reprices but the rating doesn't —
the bot would see a fat "divergence" and confidently buy the wrong side.
That's adverse selection, and it's the single most likely way a divergence
strategy loses money. The honest fix for a team-level model isn't to guess
an adjustment (that needs player-value data we don't have) — it's to KNOW
we don't know, and skip the market.

Definition of "key player": one of the team's top INJURY_TOP_N scorers by
points per game (ESPN's per-team leaders endpoint). Injury status comes
from ESPN's league-wide injury report. A market is skipped when any key
player on either team has a status in INJURY_SKIP_STATUSES ("Out",
"Doubtful" by default — "Day-To-Day"/"Questionable" players usually play,
and skipping on those would kill most NBA markets, where every team always
lists somebody).

Sports this can't cover, and why that's OK:
- MLB: the one player who dominates a baseball game (probable starter) is
  already tracked directly, and a late scratch shows up automatically when
  the 30-minute probable-pitcher cache refreshes.
- Tennis: if the match starts, both players are playing; pregame pullouts
  void/replace the market itself.
- FWC/soccer: no free lineup feed found; mitigated by the late entry window
  (lineups are public ~1h before kickoff, when we trade) and the
  MAX_DIVERGENCE guard in config.py.
"""

import logging
import time
from datetime import date

from elo import history

log = logging.getLogger("divergence_bot.elo.injuries")

INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/{league}/injuries"
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/{league}/teams?limit=50"
LEADERS_URL = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/{league}"
               "/seasons/{year}/types/2/teams/{team_id}/leaders")

_INJ_CACHE: dict[str, tuple[float, dict]] = {}
_INJ_TTL = 30 * 60  # injury statuses move on game day — keep this short


def _season_years(league: str) -> list[int]:
    """ESPN labels a season by its ending year, so the NBA's Oct-Jun season
    needs year+1 once autumn starts; the WNBA's May-Oct season is just the
    calendar year. Both get a fallback in case the primary has no data yet
    (e.g. the first days of a new season)."""
    y = date.today().year
    if league == "nba" and date.today().month >= 10:
        return [y + 1, y]
    return [y, y - 1]


def injuries(league: str) -> dict[str, dict[str, str]]:
    """{team_display_name: {player_display_name: status}} from ESPN's
    league-wide injury report. Cached 30 minutes. Empty on failure — callers
    treat that as 'no injury info', not 'nobody is hurt'."""
    now = time.monotonic()
    cached = _INJ_CACHE.get(league)
    if cached and now - cached[0] < _INJ_TTL:
        return cached[1]
    data = history._get_json(INJURIES_URL.format(league=league))
    out: dict[str, dict[str, str]] = {}
    for team_block in (data or {}).get("injuries", []):
        team = team_block.get("displayName")
        if not team:
            continue
        players = {}
        for item in team_block.get("injuries", []) or []:
            name = (item.get("athlete") or {}).get("displayName")
            status = item.get("status")
            if name and status:
                players[name] = status
        out[team] = players
    _INJ_CACHE[league] = (now, out)
    return out


def _fetch_top_scorers(league: str, top_n: int) -> dict[str, list[str]]:
    teams_data = history._get_json(TEAMS_URL.format(league=league))
    try:
        teams = teams_data["sports"][0]["leagues"][0]["teams"]
    except (KeyError, IndexError, TypeError):
        return {}
    out: dict[str, list[str]] = {}
    for t in teams:
        team = t.get("team") or {}
        team_id, team_name = team.get("id"), team.get("displayName")
        if not team_id or not team_name:
            continue
        leaders = None
        for year in _season_years(league):
            data = history._get_json(LEADERS_URL.format(league=league, year=year, team_id=team_id))
            cats = (data or {}).get("categories") or []
            ppg = next((c for c in cats if c.get("name") == "pointsPerGame"), None)
            if ppg and ppg.get("leaders"):
                leaders = ppg["leaders"][:top_n]
                break
        if not leaders:
            continue
        names = []
        for l in leaders:
            ref = (l.get("athlete") or {}).get("$ref")
            if not ref:
                continue
            athlete = history._get_json(ref)
            name = (athlete or {}).get("displayName")
            if name:
                names.append(name)
            time.sleep(0.05)
        if names:
            out[team_name] = names
    return out


def top_scorers(league: str, top_n: int = 3) -> dict[str, list[str]]:
    """{team_display_name: [top_n scorers by PPG]}. ~4 ESPN calls per team,
    so cached for the day via the shared history cache (which stores
    whatever JSON the fetch returns — a dict here, a game list elsewhere)."""
    result = history.cached_chunk(f"key_players_{league}", date.today(),
                                  lambda: _fetch_top_scorers(league, top_n) or None)
    return result if isinstance(result, dict) else {}


def key_players_out(league: str, team: str, top_n: int = 3,
                    skip_statuses: tuple = ("Out", "Doubtful")) -> list[str]:
    """Names of this team's top scorers currently listed with a skip-worthy
    injury status. Empty list = safe to trade (or no data — the two are
    indistinguishable here, which is why this is a filter, not a guarantee)."""
    stars = top_scorers(league, top_n).get(team, [])
    if not stars:
        return []
    team_injuries = injuries(league).get(team, {})
    return [p for p in stars if team_injuries.get(p) in skip_statuses]
