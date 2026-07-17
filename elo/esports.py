"""Esports Elo adapter: Dota 2, CS2, League of Legends, Valorant.

Sources (all free, all verified live 2026-07-08 — none of them need the
Riot developer portal or a STRATZ key, which cover ranked/player data
rather than pro match results):

- DOTA2    — OpenDota /proMatches (keyless; ~100 matches/call, paginated
             back through history via less_than_match_id).
- CS2      — bo3.gg public API (keyless; real dates, team names via
             with=teams, tier labels; 70k+ finished matches available).
- LOL      — Leaguepedia (lol.fandom.com) Cargo API (keyless; the standard
             community source for pro LoL results; date-windowed queries).
- VALORANT — vlr.gg via the vlr.orlandomm.net mirror (keyless but FLAKY —
             scrape-backed, intermittent failures, only relative dates
             like "2w 2d", ~50 results/page reverse-chronological).

Because several of these only expose a sliding window of recent results,
each title keeps an ACCUMULATING local store (data/cache/esports_*.json,
matches keyed by source match id, merged on every build). History deepens
the longer the bot runs; a fresh install starts with whatever the source
exposes today and grows from there. Run build_ratings.py daily.

Model notes: match-level (series winner, not per-map), no home advantage
(LAN/online is not knowable cheaply), no margin-of-victory in v1, and a
higher K than traditional sports — esports rosters and game metas shift
fast, so recent results should dominate. Player-roster changes are the
big blind spot (an Elo follows the TEAM NAME, and a rebuilt roster keeps
the old tag's rating) — that argues for high K, thresholds derived from
the measured noise floor, and humility about this whole category.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config
from elo import history, params
from elo.engine import EloEngine

log = logging.getLogger("divergence_bot.elo.esports")

TITLES = ("dota2", "cs2", "lol", "valorant")

STORE_DIR = history.CACHE_DIR


# ------------------------------------------------------------------
# Accumulating store: {match_id: [iso_date, winner, loser]}
# ------------------------------------------------------------------

def _store_path(title: str) -> Path:
    return STORE_DIR / f"esports_{title}_store.json"


def _load_store(title: str) -> dict:
    try:
        with open(_store_path(title), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_store(title: str, store: dict):
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_store_path(title), "w", encoding="utf-8") as f:
        json.dump(store, f)


# Sidecar team-name -> source-team-id map, accumulated as results are
# ingested (dota proMatches, cs2/valorant result feeds all carry team ids).
# Used by elo/rosters.py to resolve a team to its roster endpoint without a
# separate name->id lookup. LoL is name-based and has no id map.

def _teams_path(title: str) -> Path:
    return STORE_DIR / f"esports_{title}_teams.json"


def load_team_ids(title: str) -> dict:
    try:
        with open(_teams_path(title), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_team_ids(title: str, teams: dict):
    if not teams:
        return
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_teams_path(title), "w", encoding="utf-8") as f:
        json.dump(teams, f)


# ------------------------------------------------------------------
# DOTA2 — OpenDota
# ------------------------------------------------------------------

OPENDOTA_URL = "https://api.opendota.com/api/proMatches"
DOTA_BACKFILL_DAYS = 540   # ~18 months (was 365; the 150-call cap was the real
                           # limiter, leaving only ~7 months — deeper history
                           # gives each team more games to stabilize its rating)
DOTA_MAX_CALLS = 450  # ~100 matches/call; free tier is 2000/day. On a deepening
                       # run the walk re-scans the known head (adds nothing, the
                       # added>0 early-stop guard keeps it going) then extends older.


def _fetch_dota2(store: dict, teams: dict | None = None) -> int:
    added, calls = 0, 0
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=DOTA_BACKFILL_DAYS)).timestamp()
    less_than = None
    consecutive_known = 0
    while calls < DOTA_MAX_CALLS:
        url = OPENDOTA_URL + (f"?less_than_match_id={less_than}" if less_than else "")
        # A single timeout must not abort the whole year's walk (it did on
        # 2026-07-08 — one read timeout left the store with 42 days of data).
        data = None
        for attempt in range(3):
            data = history._get_json(url, timeout=40)
            calls += 1
            if data:
                break
            time.sleep(3 * (attempt + 1))
        if not data:
            log.warning("OpenDota walk aborted after repeated failures — "
                        "store keeps whatever was fetched; rerun to deepen")
            break
        named_on_page, new_on_page = 0, 0
        for m in data:
            mid = str(m.get("match_id"))
            ts = m.get("start_time") or 0
            r_name, d_name = m.get("radiant_name"), m.get("dire_name")
            if not (mid and r_name and d_name) or ts < cutoff_ts:
                continue  # OpenDota rows without team names are common — not a stop signal
            named_on_page += 1
            if teams is not None:
                if m.get("radiant_team_id"):
                    teams[r_name] = m["radiant_team_id"]
                if m.get("dire_team_id"):
                    teams[d_name] = m["dire_team_id"]
            if mid not in store:
                new_on_page += 1
                winner, loser = (r_name, d_name) if m.get("radiant_win") else (d_name, r_name)
                store[mid] = [datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
                              winner, loser]
                added += 1
        oldest_ts = min((m.get("start_time") or 0) for m in data)
        less_than = min(m.get("match_id") for m in data)
        if oldest_ts < cutoff_ts:
            break
        # Incremental early stop — only on pages that actually HAD named
        # matches, all already stored, twice in a row. The first version
        # stopped on any page with nothing new, which included pages that
        # were 100% nameless rows — that silently truncated the backfill to
        # ~3 months (caught 2026-07-08: store had no Team Spirit/BetBoom).
        if named_on_page > 0 and new_on_page == 0:
            consecutive_known += 1
            if consecutive_known >= 2 and added > 0:
                break
        else:
            consecutive_known = 0
        time.sleep(1.1)  # keyless OpenDota allows 60 calls/min
    return added


# ------------------------------------------------------------------
# CS2 — bo3.gg
# ------------------------------------------------------------------

BO3_URL = ("https://api.bo3.gg/api/v1/matches?filter%5Bmatches.status%5D%5Beq%5D=finished"
           "&sort=-start_date&with=teams&page%5Blimit%5D=100&page%5Boffset%5D={offset}")
CS2_BACKFILL_DAYS = 365
CS2_MAX_CALLS = 200  # ~100 matches/call; a full year of tiers s-c runs ~15-18k matches
CS2_TIERS = {"s", "a", "b", "c"}  # exclude tier d — semi-am noise that floods
                                   # the rating pool with teams Polymarket never lists


def _fetch_cs2(store: dict, teams: dict | None = None) -> int:
    added, offset = 0, 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CS2_BACKFILL_DAYS)).date().isoformat()
    consecutive_known = 0
    for _ in range(CS2_MAX_CALLS):
        data = history._get_json(BO3_URL.format(offset=offset))
        results = (data or {}).get("results") or []
        if not results:
            break
        eligible_on_page, new_on_page = 0, 0
        reached_cutoff = False
        for m in results:
            start = (m.get("start_date") or "")[:10]
            if start and start < cutoff:
                reached_cutoff = True
                continue
            if str(m.get("tier") or "").lower() not in CS2_TIERS:
                continue  # filtered tiers are not a stop signal (a page can be 100% tier-d)
            mid = str(m.get("id"))
            t1, t2 = m.get("team1") or {}, m.get("team2") or {}
            n1, n2 = t1.get("name"), t2.get("name")
            wid = m.get("winner_team_id")
            if not (mid and n1 and n2 and wid):
                continue
            eligible_on_page += 1
            if teams is not None:
                if t1.get("id"):
                    teams[n1] = t1["id"]
                if t2.get("id"):
                    teams[n2] = t2["id"]
            if mid not in store:
                new_on_page += 1
                winner, loser = (n1, n2) if wid == t1.get("id") else (n2, n1)
                store[mid] = [start, winner, loser]
                added += 1
        if reached_cutoff:
            break
        if eligible_on_page > 0 and new_on_page == 0:
            consecutive_known += 1
            if consecutive_known >= 2 and added > 0:
                break
        else:
            consecutive_known = 0
        offset += 100
        time.sleep(0.3)
    return added


# ------------------------------------------------------------------
# LOL — Leaguepedia Cargo
# ------------------------------------------------------------------

LEAGUEPEDIA_URL = ("https://lol.fandom.com/api.php?action=cargoquery&format=json"
                   "&tables=MatchSchedule&fields=Team1,Team2,Winner,DateTime_UTC"
                   "&where={where}&limit=500&offset={offset}")
LOL_BACKFILL_DAYS = 365


def _lol_query(where: str, offset: int) -> list | None:
    """One Cargo query with rate-limit awareness: Fandom returns rate-limit
    errors as HTTP 200 with an {"error": ...} body (seen live 2026-07-08),
    so a naive .get('cargoquery') reads as 'no rows' and silently truncates
    the dataset. Detect it, back off, retry."""
    for attempt in range(3):
        data = history._get_json(LEAGUEPEDIA_URL.format(where=where, offset=offset))
        if data is None:
            return None
        err = data.get("error")
        if not err:
            return data.get("cargoquery") or []
        if "ratelimit" in str(err.get("code", "")).lower():
            wait = 30 * (attempt + 1)
            log.info(f"Leaguepedia rate-limited — waiting {wait}s")
            time.sleep(wait)
            continue
        log.warning(f"Leaguepedia error: {err}")
        return None
    return None


def _fetch_lol(store: dict, teams: dict | None = None) -> int:
    # LoL is name-based (Leaguepedia uses team names, no numeric id) — the
    # teams param is accepted for a uniform signature but not populated.
    added = 0
    start = date.today() - timedelta(days=LOL_BACKFILL_DAYS)
    for c_start, c_end in history._month_chunks(start, date.today()):
        offset = 0
        while True:
            where = urllib.parse.quote(
                f"DateTime_UTC >= '{c_start} 00:00:00' AND DateTime_UTC <= '{c_end} 23:59:59' "
                f"AND Winner IS NOT NULL AND Winner != ''"
            )
            rows = _lol_query(where, offset)
            if not rows:
                break
            for r in rows:
                t = r.get("title") or {}
                t1, t2, winner_no = t.get("Team1"), t.get("Team2"), t.get("Winner")
                dt = (t.get("DateTime UTC") or "")[:10]
                if not (t1 and t2 and winner_no in ("1", "2") and dt):
                    continue
                mid = f"{dt}|{t1}|{t2}|{t.get('DateTime UTC')}"
                if mid not in store:
                    winner, loser = (t1, t2) if winner_no == "1" else (t2, t1)
                    store[mid] = [dt, winner, loser]
                    added += 1
            if len(rows) < 500:
                break
            offset += 500
            time.sleep(2.0)  # Fandom rate-limits anonymous clients aggressively
        time.sleep(2.0)
    return added


# ------------------------------------------------------------------
# VALORANT — vlr.gg mirror (flaky: retries, tolerate failures, only
# relative dates — parsed to approximate ISO dates for ordering)
# ------------------------------------------------------------------

VLR_URL = "https://vlr.orlandomm.net/api/v1/results?page={page}"
VLR_MAX_PAGES = 85  # the vlr.orlandomm mirror serves ~85 pages (~13 months);
                     # page 90+ 404s. Was 40 (~5 months). A deepening run
                     # re-scans the known head then extends older (added>0 guard).
VLR_RETRIES = 3

_AGO_UNITS = {"m": 1 / 1440, "h": 1 / 24, "d": 1, "w": 7, "mo": 30, "yr": 365}


def _ago_to_date(ago: str) -> str:
    """'2w 2d' -> approximate ISO date. Ordering-quality only."""
    days = 0.0
    for num, unit in re.findall(r"(\d+)\s*(mo|yr|[mhdw])", str(ago or "")):
        days += int(num) * _AGO_UNITS.get(unit, 0)
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def _fetch_valorant_vlr(store: dict, team_ids: dict | None = None) -> int:
    added = 0
    consecutive_known = 0
    for page in range(1, VLR_MAX_PAGES + 1):
        data = None
        for attempt in range(VLR_RETRIES):
            data = history._get_json(VLR_URL.format(page=page), timeout=25)
            if data:
                break
            time.sleep(2 * (attempt + 1))
        if not data:
            log.warning(f"vlr.gg mirror: page {page} failed after {VLR_RETRIES} tries — continuing")
            continue
        page_added = 0
        for m in data.get("data") or []:
            mid = str(m.get("id"))
            match_teams = m.get("teams") or []
            if not mid or len(match_teams) != 2 or m.get("status") != "Completed":
                continue
            winner = next((t.get("name") for t in match_teams if t.get("won")), None)
            loser = next((t.get("name") for t in match_teams if not t.get("won")), None)
            if not (winner and loser):
                continue
            if team_ids is not None:
                for t in match_teams:
                    if t.get("name") and t.get("id"):
                        team_ids[t["name"]] = t["id"]
            if mid not in store:
                store[mid] = [_ago_to_date(m.get("ago")), winner, loser]
                added += 1
                page_added += 1
        consecutive_known = consecutive_known + 1 if page_added == 0 else 0
        if consecutive_known >= 3 and added > 0:
            break  # deep into already-stored territory on an incremental run
        time.sleep(1.0)
    return added


# ------------------------------------------------------------------
# PANDASCORE — one structured source, three titles. The free "Fixtures
# Only" token covers valorant, dota2 and csgo (=CS2) past matches at
# 1000 req/hr, with real begin_at dates and stable team ids (verified
# 2026-07-12: valorant ~18k matches to 2021, dota2 ~40k to 2015, csgo
# ~95k to 2016 — 3-6x deeper than the keyless sources). Which titles
# actually USE it is gated by config.PANDASCORE_TITLES: a title is cut
# over only after the deeper data measurably beat the old source in a
# walk-forward backtest, never on faith. Tokenless installs keep the
# keyless fallbacks (vlr mirror / OpenDota / bo3.gg) with zero change.
# ------------------------------------------------------------------

PANDASCORE_SLUGS = {"valorant": "valorant", "dota2": "dota2", "cs2": "csgo"}
PANDASCORE_URL = ("https://api.pandascore.co/{slug}/matches/past"
                  "?sort=-begin_at&per_page=100&page={page}")
# Page caps sized to each title's FULL PandaScore history (one-time deep
# backfill; incremental runs early-stop after ~2 known pages). A run that
# hits the hourly rate limit saves what it got and RESUMES on the next
# refresh cycle — the early-stop needs added>0, so re-walking known pages
# on resume never falsely terminates the deepening.
PANDASCORE_MAX_PAGES = {"valorant": 200, "dota2": 430, "cs2": 980}


def _match_bo(m: dict):
    """Best-of length (1/3/5...) from a PandaScore match, or None."""
    try:
        bo = int(m.get("number_of_games"))
        return bo if bo > 0 else None
    except (TypeError, ValueError):
        return None


def _match_tier(m: dict):
    """Tournament tier letter ('s','a','b','c','d') from a PandaScore match,
    or None. Tournament tier is the reliable field; serie.tier is often null."""
    for src in ("tournament", "serie"):
        t = (m.get(src) or {}).get("tier")
        if t:
            return str(t).lower()
    return None


def _fetch_pandascore(title: str, store: dict, teams: dict | None, token: str) -> int:
    """PandaScore past matches for one title into the store, keyed 'ps{id}'
    so they never collide with the keyless sources' numeric ids."""
    slug = PANDASCORE_SLUGS[title]
    added, consecutive_known = 0, 0
    for page in range(1, PANDASCORE_MAX_PAGES[title] + 1):
        req = urllib.request.Request(
            PANDASCORE_URL.format(slug=slug, page=page),
            headers={"User-Agent": "DivergenceBot/1.0",
                     "Authorization": f"Bearer {token}",
                     "Accept": "application/json"},
        )
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    log.warning("PandaScore: 401 Unauthorized — check PANDASCORE_TOKEN")
                    return added
                if e.code == 429:  # rate limited — back off and retry
                    time.sleep(5 * (attempt + 1))
                    continue
                log.warning(f"PandaScore {title} page {page}: HTTP {e.code}")
                time.sleep(2 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(2 * (attempt + 1))
        if data is None:
            log.warning(f"PandaScore {title}: page {page} failed after retries — stopping "
                        f"(store keeps {len(store)}; next refresh resumes the walk)")
            break
        if not isinstance(data, list) or not data:
            break  # empty page = paged past the last result (normal, not an error)
        page_added = 0
        for m in data:
            if m.get("status") != "finished":
                continue
            winner_id = m.get("winner_id")
            opps = m.get("opponents") or []
            if not winner_id or len(opps) != 2:
                continue
            names = {}
            for o in opps:
                t = o.get("opponent") or {}
                if t.get("id") and t.get("name"):
                    names[t["id"]] = t["name"]
            if len(names) != 2 or winner_id not in names:
                continue
            winner = names[winner_id]
            loser = next(n for tid, n in names.items() if tid != winner_id)
            begin = (m.get("begin_at") or m.get("end_at") or "")[:10]
            if not begin:
                continue
            if teams is not None:
                for tid, n in names.items():
                    teams[n] = tid
            mid = f"ps{m.get('id')}"
            if mid not in store:
                # 5-field entry: [date, winner, loser, bo_format, tier].
                # bo/tier feed the XGB context features (exp 3); readers
                # tolerate legacy 3-field entries (missing context -> NaN).
                store[mid] = [begin, winner, loser, _match_bo(m), _match_tier(m)]
                added += 1
                page_added += 1
        consecutive_known = consecutive_known + 1 if page_added == 0 else 0
        if consecutive_known >= 2 and added > 0:
            break  # incremental run reached already-stored territory
        time.sleep(0.4)
    return added


def _ctx_cursor_path(title: str):
    return STORE_DIR / f"esports_{title}_ctx_cursor.json"


def enrich_pandascore_context(title: str, max_pages: int | None = None) -> dict:
    """RESUMABLE historical walk that back-fills bo_format/tier onto existing
    store entries (and adds any matches the store is missing — this also
    completes valorant's never-finished deep backfill). Walks newest-first
    from a saved page cursor; a rate-limited/failed page saves progress and
    returns, so repeated runs converge. Returns the cursor state.

    Only needed once per title (then the fetcher writes 5-field entries for
    everything new). Server-side this matters only if a context model ever
    ships; until then it's a research-side tool."""
    import urllib.request as _rq
    token = getattr(config, "PANDASCORE_TOKEN", "").strip()
    if not token or title not in PANDASCORE_SLUGS:
        return {"done": False, "page": 0, "error": "no token/slug"}
    path = _ctx_cursor_path(title)
    try:
        cursor = json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cursor = {"page": 0, "done": False}
    if cursor.get("done"):
        return cursor
    store = _load_store(title)
    team_ids = load_team_ids(title)
    slug = PANDASCORE_SLUGS[title]
    limit = max_pages or PANDASCORE_MAX_PAGES[title]
    page = cursor["page"]
    updated = added = 0
    while page < limit:
        page += 1
        req = _rq.Request(
            PANDASCORE_URL.format(slug=slug, page=page),
            headers={"User-Agent": "DivergenceBot/1.0",
                     "Authorization": f"Bearer {token}",
                     "Accept": "application/json"})
        data = None
        for attempt in range(3):
            try:
                with _rq.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(2 * (attempt + 1))
        if data is None:
            page -= 1  # retry this page next run
            break
        if not isinstance(data, list) or not data:
            cursor["done"] = True
            break
        for m in data:
            if m.get("status") != "finished":
                continue
            winner_id = m.get("winner_id")
            opps = m.get("opponents") or []
            names = {(o.get("opponent") or {}).get("id"): (o.get("opponent") or {}).get("name")
                     for o in opps if (o.get("opponent") or {}).get("id")}
            if len(names) != 2 or winner_id not in names:
                continue
            begin = (m.get("begin_at") or m.get("end_at") or "")[:10]
            if not begin:
                continue
            winner = names[winner_id]
            loser = next(n for tid, n in names.items() if tid != winner_id)
            if team_ids is not None:
                for tid, n in names.items():
                    team_ids[n] = tid
            mid = f"ps{m.get('id')}"
            entry = [begin, winner, loser, _match_bo(m), _match_tier(m)]
            if mid not in store:
                added += 1
            elif len(store[mid]) < 5:
                updated += 1
            store[mid] = entry
        time.sleep(0.4)
    cursor["page"] = page
    _save_store(title, store)
    _save_team_ids(title, team_ids)
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cursor, f)
    log.info(f"{title} context enrichment: thru page {page}/{limit} "
             f"(+{added} new, {updated} enriched) {'DONE' if cursor.get('done') else '— resumable'}")
    return cursor


def matches_with_context(title: str) -> list[tuple]:
    """(date, winner, loser, bo_format, tier) chronological from the store —
    bo/tier are None for legacy 3-field entries (features go NaN, per P6).
    Store-only (no fetch): the exp-3 training reader."""
    store = _load_store(title)
    out = []
    for m in store.values():
        bo = m[3] if len(m) > 3 else None
        tier = m[4] if len(m) > 4 else None
        out.append((m[0], m[1], m[2], bo, tier))
    out.sort(key=lambda r: r[0])
    return out


def pandascore_enabled(title: str) -> bool:
    """True when this title should use PandaScore: token present AND the
    title has been promoted into config.PANDASCORE_TITLES (i.e. the deeper
    data won its backtest). Rosters key off this too, so match team-ids and
    the roster provider always agree on WHOSE ids are in the sidecar."""
    token = getattr(config, "PANDASCORE_TOKEN", "").strip()
    return bool(token) and title in getattr(config, "PANDASCORE_TITLES", ("valorant",))


def _pandascore_or_fallback(title: str, store: dict, team_ids: dict | None,
                            fallback) -> int:
    """Dispatcher: PandaScore when enabled for this title, else the keyless
    fallback. On the first PandaScore run, drops the fallback source's
    entries (plain-numeric ids) so two sources can't double-count the same
    match under different ids — and drops the fallback's team-id sidecar
    entries, which belong to a different id-space entirely."""
    if not pandascore_enabled(title):
        return fallback(store, team_ids)
    stale = [k for k in store if not str(k).startswith("ps")]
    if stale:
        log.info(f"{title}: cutting over to PandaScore — dropping {len(stale)} "
                 f"old-source entries to avoid cross-source double-count")
        for k in stale:
            del store[k]
        if team_ids is not None:
            team_ids.clear()  # old source's team ids — wrong id-space for PandaScore
    added = _fetch_pandascore(title, store, team_ids,
                              getattr(config, "PANDASCORE_TOKEN", "").strip())
    if added == 0 and not store:
        log.info(f"PandaScore returned nothing and {title} store empty — falling back")
        return fallback(store, team_ids)
    return added


def _fetch_valorant(store: dict, team_ids: dict | None = None) -> int:
    return _pandascore_or_fallback("valorant", store, team_ids, _fetch_valorant_vlr)


def _fetch_dota2_dispatch(store: dict, team_ids: dict | None = None) -> int:
    return _pandascore_or_fallback("dota2", store, team_ids, _fetch_dota2)


def _fetch_cs2_dispatch(store: dict, team_ids: dict | None = None) -> int:
    return _pandascore_or_fallback("cs2", store, team_ids, _fetch_cs2)


# ------------------------------------------------------------------
# Shared build / replay / probability
# ------------------------------------------------------------------

_FETCHERS = {"dota2": _fetch_dota2_dispatch, "cs2": _fetch_cs2_dispatch,
             "lol": _fetch_lol, "valorant": _fetch_valorant}


def fetch_matches(title: str) -> list[tuple[str, str, str]]:
    """(iso_date, winner, loser) chronological, from the accumulating store
    after merging in whatever the source exposes right now. Also refreshes
    the team-name -> source-id sidecar map used by the roster guard."""
    store = _load_store(title)
    team_ids = load_team_ids(title)
    before = len(store)
    try:
        added = _FETCHERS[title](store, team_ids)
    except Exception as e:
        log.warning(f"{title}: fetch failed ({e}) — using {before} stored matches")
        added = 0
    if added:
        _save_store(title, store)
    _save_team_ids(title, team_ids)
    matches = sorted(store.values(), key=lambda m: m[0])
    log.info(f"{title.upper()}: {len(matches)} matches in store (+{added} new this run)")
    return [(m[0], m[1], m[2]) for m in matches]


def store_latest_date(title: str) -> str | None:
    """Most recent match date in a title's accumulating store (ISO), or None.
    Used by the freshness tracker to show how current each esport's data is."""
    store = _load_store(title)
    return max((m[0] for m in store.values()), default=None)


def recent_match_dates(title: str, team_name: str) -> list[str]:
    """ISO dates (ascending) of stored matches involving team_name — used by
    the roster guard to count how many games a team has played since a
    detected roster change (i.e. whether the Elo has re-equilibrated)."""
    store = _load_store(title)
    out = [m[0] for m in store.values() if team_name in (m[1], m[2])]
    out.sort()
    return out


DOTA_LINEUP_CALLS_PER_RUN = 300  # OpenDota free tier is 2000/day; the id walk
                                  # plus 4 refresh runs/day of lineups stays under it


def deepen_dota_player_data():
    """Data collection for a future Dota player model (the LoL player blend —
    the one model that ever beat Elo here — needs per-match lineups, and its
    approach is the template). PandaScore's free tier carries no players and
    its paid stats plans are restricted to non-betting usage, so lineups come
    from keyless OpenDota instead: walk proMatches into a SEPARATE
    OpenDota-id store (the live match store is PandaScore ids — a different
    id-space, deliberately never joined), then fetch each match's lineup.
    The OD store carries its own dates/results, so the future model trains
    entirely on OpenDota data. Collection only — nothing live reads it."""
    if pandascore_enabled("dota2"):
        store = _load_store("dota2_od")
        before = len(store)
        try:
            # teams=None: OpenDota team ids must never leak into the
            # PandaScore team-id sidecar (different id-space).
            added = _fetch_dota2(store, None)
        except Exception as e:
            log.warning(f"dota2_od walk failed ({e}) — keeping {before} stored matches")
            added = 0
        if added:
            _save_store("dota2_od", store)
        log.info(f"DOTA2 player-data walk: {len(store)} OpenDota matches (+{added} new)")
    else:
        store = _load_store("dota2")  # tokenless install: the main store IS OpenDota ids
    capture_dota_lineups(store)


def capture_dota_lineups(store: dict):
    """Accumulates per-match LINEUPS for OpenDota-id matches (newest first,
    capped per run — OpenDota serves lineups one match per call). Entries
    mirror lol_players' game shape so the future training harness is
    source-agnostic: {match_id: {date, teams: {name: [account_ids]},
    winner}}. A valid match response is always recorded, even with a partial
    lineup, so it's never refetched (training filters thin lineups); a
    failed fetch is skipped and retried on a later run."""
    path = STORE_DIR / "esports_dota2_lineups.json"
    try:
        with open(path, encoding="utf-8") as f:
            lineups = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        lineups = {}
    todo = sorted((mid for mid in store if mid not in lineups and str(mid).isdigit()),
                  key=lambda mid: store[mid][0], reverse=True)[:DOTA_LINEUP_CALLS_PER_RUN]
    fetched = 0
    for mid in todo:
        data = history._get_json(f"https://api.opendota.com/api/matches/{mid}", timeout=25)
        time.sleep(1.1)  # also on failure — a dead endpoint must not get hammered
        if not data or data.get("radiant_win") is None:
            continue  # transient failure / unresolved match — retry next run
        radiant = [str(pl["account_id"]) for pl in data.get("players", [])
                   if pl.get("isRadiant") and pl.get("account_id")]
        dire = [str(pl["account_id"]) for pl in data.get("players", [])
                if not pl.get("isRadiant") and pl.get("account_id")]
        game_date, winner, loser = store[mid][0], store[mid][1], store[mid][2]
        win_lu, lose_lu = (radiant, dire) if data["radiant_win"] else (dire, radiant)
        lineups[mid] = {"date": game_date, "teams": {winner: win_lu, loser: lose_lu},
                        "winner": winner}
        fetched += 1
    if fetched:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lineups, f)
    log.info(f"dota2 lineup capture: +{fetched} this run, {len(lineups)} total "
             f"({len(store) - len(lineups)} still uncaptured)")


def load_dota_games() -> list[dict]:
    """Captured Dota games in lol_players' game shape ({date, teams, winner}),
    filtered to full two-sided lineups (>=3 known players a side — the same
    rule as the LoL OE loader): the future player model's training reader."""
    try:
        with open(STORE_DIR / "esports_dota2_lineups.json", encoding="utf-8") as f:
            lineups = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    games = [g for g in lineups.values()
             if len(g.get("teams", {})) == 2 and g.get("winner") in g.get("teams", {})
             and all(len(v) >= 3 for v in g["teams"].values())]
    games.sort(key=lambda g: g["date"])
    return games


def _apply_inactivity_decay(engine: EloEngine, team: str, game_day, last_played: dict, p: dict):
    """One-shot regression toward the mean when a team RETURNS after a long
    idle spell. An Elo rating is earned by a specific roster in a specific
    meta; after months dormant both have usually changed, and the stale
    rating shows up live as a fat fake divergence the market (which knows)
    happily takes the other side of. Measured 2026-07-13 on dormant-team
    games (90d idle): dota2 Brier 0.2257->0.2213, cs2 0.2288->0.2222.
    No-op for titles without inactivity params (lol, valorant — no measured
    benefit there)."""
    days = p.get("inactivity_days")
    frac = p.get("inactivity_regress", 0.0)
    if not days or not frac:
        return
    prev = last_played.get(team)
    if prev is not None and (game_day - prev).days > days and team in engine.ratings:
        engine.ratings[team] += (1500.0 - engine.ratings[team]) * frac


def replay(matches: list, p: dict, collect: bool = False) -> tuple[EloEngine, list]:
    """Chronological replay, same walk-forward contract as every other
    adapter. Alphabetical player-A ordering for collected predictions —
    outcome-independent, so calibration buckets stay honest. Tracks each
    team's last-played date (engine.extras — the live dormancy check needs
    it) and applies the on-return inactivity decay before predicting."""
    from datetime import date as _date
    engine = EloEngine(k_factor=p["k"])
    last_played: dict = {}
    predictions = []
    for d, winner, loser in matches:
        try:
            game_day = _date.fromisoformat(str(d)[:10])
        except ValueError:
            game_day = None
        if game_day is not None:
            _apply_inactivity_decay(engine, winner, game_day, last_played, p)
            _apply_inactivity_decay(engine, loser, game_day, last_played, p)
        if collect and engine.games(winner) >= p["min_games"] and engine.games(loser) >= p["min_games"]:
            name_a, name_b = sorted((winner, loser))
            prob_a = engine.probability(name_a, name_b)
            predictions.append((prob_a, 1.0 if name_a == winner else 0.0))
        engine.record_result(winner, loser, 1.0)
        if game_day is not None:
            last_played[winner] = game_day
            last_played[loser] = game_day
    engine.extras["last_played"] = {t: d.isoformat() for t, d in last_played.items()}
    return engine, predictions


def build_engine(title: str) -> tuple[EloEngine, int]:
    matches = fetch_matches(title)
    engine, _ = replay(matches, params.get(title))
    return engine, len(matches)


def _effective_rating(engine: EloEngine, team: str, p: dict) -> float:
    """Rating for a LIVE prediction: if the team is dormant right now
    (idle past inactivity_days as of today), view its rating through the
    same one-shot regression the replay applies on return — WITHOUT
    mutating stored state, so the check is idempotent across cycles and
    the replay's own on-return decay stays the single source of truth
    once the team actually plays."""
    from datetime import date as _date
    r = engine.get_rating(team)
    days = p.get("inactivity_days")
    frac = p.get("inactivity_regress", 0.0)
    lp = (engine.extras.get("last_played") or {}).get(team)
    if days and frac and lp:
        try:
            idle = (_date.today() - _date.fromisoformat(str(lp)[:10])).days
        except ValueError:
            return r
        if idle > days:
            r += (1500.0 - r) * frac
    return r


def probability(engine: EloEngine, team_a: str, team_b: str, title: str) -> float | None:
    p = params.get(title)
    if engine.games(team_a) < p["min_games"] or engine.games(team_b) < p["min_games"]:
        return None
    gap = _effective_rating(engine, team_a, p) - _effective_rating(engine, team_b, p)
    return 1.0 / (1.0 + 10 ** (-gap / 400.0))
