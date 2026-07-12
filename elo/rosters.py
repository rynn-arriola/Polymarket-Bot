"""Roster-change guard for esports.

The problem it solves: an Elo rating is earned by a specific five-player
roster, but the rating follows the TEAM NAME. When a team swaps players,
the rating is stale — it describes a lineup that no longer plays. In a
divergence strategy this is dangerous: the market reprices instantly on a
roster move, our results-only model doesn't, and the gap looks like a fat
"edge" that is really the market being right. This is the single biggest
structural weakness of team-level esports Elo (flagged throughout).

What this does — a prospective guard, not a historical player model
(free data can't support full historical player-Elo across titles; see
README):

  1. Snapshot each team's current roster the first time we see it.
  2. On each check, compare the live roster to the snapshot. If it has
     changed by >= ROSTER_CHANGE_MIN_DIFF players, the rating is stale:
     mark the team "changed" and SKIP its markets.
  3. Re-accept once the team has played ROSTER_REACCEPT_MATCHES games since
     the change (the Elo has re-equilibrated under the new lineup) — then
     re-snapshot to the current roster and resume trading.

Per-title roster providers (each title, its own best free source — the
"separate API per title" design):
  - dota2    : OpenDota /teams/{id}/players (current members)
  - valorant : vlr.gg mirror /teams/{id} (players list)
  - lol      : Leaguepedia Cargo (rate-limited; cached hard)
  - cs2      : NO free roster source found (bo3 team-filter is broken,
               HLTV blocks) — provider returns None, so the guard no-ops
               for CS2 and it's protected only by its divergence threshold.

Fail-open: if a roster can't be fetched (flaky endpoints, rate limits), the
guard returns "ok" rather than blocking — the divergence thresholds are the
baseline protection, and failing closed would halt most esports trading on
every API hiccup. Every fetch failure and every skip is logged.
"""

import json
import logging
import time
import urllib.parse
from datetime import date
from pathlib import Path

import config
from elo import esports, history

log = logging.getLogger("divergence_bot.elo.rosters")

SNAPSHOT_DIR = history.CACHE_DIR
_ROSTER_TTL = 12 * 3600  # cache a fetched roster this long (game-day granularity is plenty)
_roster_cache: dict[tuple[str, str], tuple[float, frozenset | None]] = {}


# ------------------------------------------------------------------
# Snapshot store: {team_name: {"ids": [...], "status": "stable"|"changed",
#                              "changed_on": "YYYY-MM-DD"}}
# ------------------------------------------------------------------

def _snap_path(title: str) -> Path:
    return SNAPSHOT_DIR / f"roster_snap_{title}.json"


def _load_snaps(title: str) -> dict:
    try:
        with open(_snap_path(title), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_snaps(title: str, snaps: dict):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_snap_path(title), "w", encoding="utf-8") as f:
        json.dump(snaps, f)


# ------------------------------------------------------------------
# Per-title roster providers -> frozenset[str] of player identifiers, or
# None if unavailable (fail-open).
# ------------------------------------------------------------------

def _dota_roster(team_name: str, team_id) -> frozenset | None:
    if not team_id:
        return None
    data = None
    for attempt in range(3):  # OpenDota's roster endpoint is genuinely flaky
        data = history._get_json(f"https://api.opendota.com/api/teams/{team_id}/players", timeout=25)
        if data is not None:
            break
        time.sleep(2 * (attempt + 1))
    if not isinstance(data, list):
        return None
    ids = {str(p.get("account_id")) for p in data
           if p.get("is_current_team_member") and p.get("account_id")}
    return frozenset(ids) or None


def _valorant_roster(team_name: str, team_id) -> frozenset | None:
    if not team_id:
        return None
    data = history._get_json(f"https://vlr.orlandomm.net/api/v1/teams/{team_id}", timeout=25)
    d = (data or {}).get("data") or {}
    players = d.get("players") or []
    ids = {str(p.get("id") or p.get("user")) for p in players if (p.get("id") or p.get("user"))}
    return frozenset(ids) or None


def _lol_roster(team_name: str, team_id) -> frozenset | None:
    # Leaguepedia: current roster from the TeamRosters table (rate-limited,
    # so this is cached 12h like every provider). Name-based, no numeric id.
    where = urllib.parse.quote(f"Team = '{team_name}' AND IsCurrent = '1'")
    url = ("https://lol.fandom.com/api.php?action=cargoquery&format=json"
           f"&tables=TeamRosters&fields=Player&where={where}&limit=20")
    data = history._get_json(url)
    if not data or data.get("error"):
        return None
    rows = data.get("cargoquery") or []
    ids = {(r.get("title") or {}).get("Player") for r in rows}
    ids.discard(None)
    return frozenset(ids) or None


_PROVIDERS = {
    "dota2": _dota_roster,
    "valorant": _valorant_roster,
    "lol": _lol_roster,
    # cs2: intentionally absent — no free roster source (see module docstring)
}


def current_roster(title: str, team_name: str) -> frozenset | None:
    """Live roster for a team, cached _ROSTER_TTL. None = unavailable."""
    provider = _PROVIDERS.get(title)
    if provider is None:
        return None
    key = (title, team_name)
    now = time.monotonic()
    hit = _roster_cache.get(key)
    if hit and now - hit[0] < _ROSTER_TTL:
        return hit[1]
    team_id = esports.load_team_ids(title).get(team_name)
    try:
        roster = provider(team_name, team_id)
    except Exception as e:
        log.warning(f"{title} roster fetch failed for {team_name!r}: {e}")
        roster = None
    _roster_cache[key] = (now, roster)
    return roster


# ------------------------------------------------------------------
# The guard
# ------------------------------------------------------------------

def team_ok(title: str, team_name: str) -> bool:
    """False = skip this team's markets (roster changed and the rating hasn't
    caught up yet). True = safe to trade, OR the guard couldn't run (fail-open
    — the divergence threshold is the fallback protection)."""
    if not getattr(config, "ROSTER_GUARD", True):
        return True
    if title not in _PROVIDERS:
        return True  # no provider for this title (e.g. cs2) — threshold only

    roster = current_roster(title, team_name)
    if roster is None:
        return True  # fail-open: unavailable roster must not halt trading

    min_diff = getattr(config, "ROSTER_CHANGE_MIN_DIFF", 2)
    reaccept = getattr(config, "ROSTER_REACCEPT_MATCHES", 15)
    snaps = _load_snaps(title)
    snap = snaps.get(team_name)
    today = date.today().isoformat()

    if snap is None:
        # First sighting — establish a baseline, trade normally.
        snaps[team_name] = {"ids": sorted(roster), "status": "stable", "changed_on": today}
        _save_snaps(title, snaps)
        return True

    snap_ids = frozenset(snap.get("ids", []))
    diff = len(roster.symmetric_difference(snap_ids))

    if snap.get("status") == "stable":
        if diff >= min_diff:
            snaps[team_name] = {"ids": sorted(roster), "status": "changed", "changed_on": today}
            _save_snaps(title, snaps)
            log.info(f"{title}: roster change for {team_name!r} ({diff} players different) — "
                     f"skipping until {reaccept} games played under the new lineup")
            return False
        # Unchanged (or minor) — refresh baseline so ids stay current.
        if diff:
            snap["ids"] = sorted(roster)
            _save_snaps(title, snaps)
        return True

    # status == "changed": has the Elo re-equilibrated yet?
    if diff >= min_diff:
        # Roster shifted again vs the changed snapshot — still in flux, reset clock.
        snaps[team_name] = {"ids": sorted(roster), "status": "changed", "changed_on": today}
        _save_snaps(title, snaps)
        return False
    games_since = sum(1 for d in esports.recent_match_dates(title, team_name)
                      if d >= snap.get("changed_on", today))
    if games_since >= reaccept:
        snaps[team_name] = {"ids": sorted(roster), "status": "stable", "changed_on": today}
        _save_snaps(title, snaps)
        log.info(f"{title}: {team_name!r} re-accepted after {games_since} games on the new roster")
        return True
    return False
