"""ATP/WTA/ITF tennis Elo adapter.

ATP and WTA are built from ESPN's public tennis scoreboard via
elo/history.py's cached fetcher (ITF is NOT on ESPN — checked several
league-slug variants live, all 404/400; it falls back to local CSVs, see
below).

Upgrade over plain Elo: SURFACE-SPECIFIC ratings. Clay/grass/hard
specialists are a real, large effect in tennis (the canonical example being
peak Nadal on clay), so each player carries an overall rating plus one per
surface, and predictions blend them: (1-w)*overall + w*surface. ESPN's feed
doesn't expose a surface field (verified live — only the tournament name),
so surface comes from a curated tournament-name keyword map below; the tour
calendar is stable year to year, and anything unmapped falls back to "hard",
the majority surface. Both ratings update after every match; the surface
rating only participates in a prediction once both players have
surface_min_games matches on that surface.

For LIVE prediction the upcoming match's surface isn't knowable from
Polymarket data (markets don't name the tournament), so current_surface()
checks which tournaments are running on ESPN today and returns the majority
surface — the tour is almost always on one surface in any given week.
"""

import csv
import logging
import time
from datetime import date
from pathlib import Path

from elo import history, params
from elo.engine import EloEngine

log = logging.getLogger("divergence_bot.elo.tennis")

GROUPING_SLUG = {"atp": "mens-singles", "wta": "womens-singles"}

# Curated tournament-name keywords -> surface. Lowercase substring match.
# Only tournaments whose surface is unambiguous are listed; default is hard.
CLAY_KEYWORDS = (
    "roland garros", "french open", "monte carlo", "monte-carlo", "madrid",
    "rome", "italian open", "barcelona", "hamburg", "gstaad", "bastad",
    "kitzbuhel", "umag", "buenos aires", "rio open", "santiago", "estoril",
    "munich", "geneva", "lyon", "marrakech", "houston", "bucharest",
    "palermo", "rabat", "strasbourg", "charleston", "warsaw", "budapest",
)
GRASS_KEYWORDS = (
    "wimbledon", "halle", "queen", "hertogenbosch", "libema", "rosmalen",
    "mallorca", "eastbourne", "newport", "berlin", "bad homburg",
    "nottingham", "birmingham", "boss open",
)


def surface_of(tournament: str) -> str:
    t = (tournament or "").lower()
    for kw in CLAY_KEYWORDS:
        if kw in t:
            return "clay"
    for kw in GRASS_KEYWORDS:
        if kw in t:
            return "grass"
    return "hard"


def fetch_matches(tour: str, start: date, end: date) -> list[dict]:
    matches = history.espn_tennis_matches(tour, GROUPING_SLUG[tour], start, end)
    log.info(f"{tour.upper()}: {len(matches)} completed singles matches ({start}..{end})")
    return matches


def fetch_matches_for(tour: str, start: date, end: date, tml_start_year: int) -> list[dict]:
    """Route each tour to its best source: ATP -> TML-Database (deep history,
    real surfaces), WTA -> ESPN (no live TML mirror exists for it). One entry
    point so build_ratings / backtest / tune all stay in sync."""
    if tour == "atp":
        return fetch_matches_tml(tml_start_year, end.year)
    return fetch_matches(tour, start, end)


def _skey(player: str, surface: str) -> str:
    return f"{player}|{surface}"


def _blended_prob(engine: EloEngine, p: dict, a: str, b: str, surface: str | None) -> float:
    """P(a beats b): overall Elo, blended with surface-specific Elo when the
    surface is known and both players have enough history on it."""
    gap = engine.get_rating(a) - engine.get_rating(b)
    w = p.get("surface_weight", 0.0)
    if surface and w > 0:
        min_s = p.get("surface_min_games", 5)
        if (engine.games(_skey(a, surface)) >= min_s
                and engine.games(_skey(b, surface)) >= min_s):
            s_gap = engine.get_rating(_skey(a, surface)) - engine.get_rating(_skey(b, surface))
            gap = (1 - w) * gap + w * s_gap
    return 1.0 / (1.0 + 10 ** (-gap / 400.0))


def replay(matches: list[dict], p: dict, collect: bool = False) -> tuple[EloEngine, list]:
    """Chronological replay; with collect=True also returns walk-forward
    (predicted_prob, actual) pairs. Player order in each prediction is
    alphabetical — deliberately outcome-independent, because ordering by
    winner would make every calibration bucket trivially 100% (a bug the
    first version of this backtest actually had)."""
    engine = EloEngine(k_factor=p["k"])
    predictions = []
    for m in matches:
        winner, loser = m["winner"], m["loser"]
        # Prefer a real surface label (TML CSV has one) over keyword-guessing
        # from the tournament name (ESPN, which exposes no surface field).
        surface = m.get("surface") or surface_of(m.get("tournament", ""))
        if collect and engine.games(winner) >= p["min_games"] and engine.games(loser) >= p["min_games"]:
            name_a, name_b = sorted((winner, loser))
            prob_a = _blended_prob(engine, p, name_a, name_b, surface)
            predictions.append((prob_a, 1.0 if name_a == winner else 0.0))
        engine.record_result(winner, loser, 1.0)
        engine.record_result(_skey(winner, surface), _skey(loser, surface), 1.0)
    return engine, predictions


def build_engine_espn(tour: str, start: date, end: date) -> tuple[EloEngine, int]:
    matches = fetch_matches(tour, start, end)
    engine, _ = replay(matches, params.get(tour))
    return engine, len(matches)


# ------------------------------------------------------------------
# ATP via TML-Database (github.com/Tennismylife/TML-Database) — the live
# successor to Jeff Sackmann's tennis_atp (which 404s as of 2026-07). Full
# Sackmann schema, one CSV per year 1968-present, and crucially a REAL
# `surface` column (Hard/Clay/Grass/Carpet) instead of the keyword-guessing
# ESPN forces. Plain HTTPS CSV — no rate limits, past years immutable — so
# it caches cleanly via history.cached_chunk. WTA/ITF have no equivalent
# live mirror found, so they stay on ESPN / local CSV respectively.
# ------------------------------------------------------------------

TML_URL = "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/{year}.csv"


def _fetch_tml_year(year: int) -> list[dict]:
    def fetch():
        import urllib.request
        req = urllib.request.Request(TML_URL.format(year=year),
                                     headers={"User-Agent": "DivergenceBot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as e:
            log.warning(f"TML {year} fetch failed: {e}")
            return None
        rows = []
        reader = csv.DictReader(text.splitlines())
        for r in reader:
            d, w, l = r.get("tourney_date"), r.get("winner_name"), r.get("loser_name")
            if not (d and w and l):
                continue
            # tourney_date is YYYYMMDD -> ISO for chronological sorting
            iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
            surf = (r.get("surface") or "").strip().lower() or None
            rows.append({"date": iso, "winner": w, "loser": l, "surface": surf})
        return rows

    # Past years never change; the current year refetches at most daily.
    chunk_end = date(year, 12, 31)
    return history.cached_chunk(f"tennis_tml_atp_{year}", chunk_end, fetch)


def fetch_matches_tml(start_year: int, end_year: int | None = None) -> list[dict]:
    end_year = end_year or date.today().year
    matches = []
    for year in range(start_year, end_year + 1):
        matches.extend(_fetch_tml_year(year))
    matches.sort(key=lambda m: m["date"])
    surfaced = sum(1 for m in matches if m.get("surface"))
    log.info(f"ATP (TML): {len(matches)} matches {start_year}-{end_year} "
             f"({surfaced} with a real surface label)")
    return matches


def build_engine_tml(start_year: int, end_year: int | None = None) -> tuple[EloEngine, int]:
    matches = fetch_matches_tml(start_year, end_year)
    engine, _ = replay(matches, params.get("atp"))
    return engine, len(matches)


# ------------------------------------------------------------------
# Live-time surface: which surface is the tour on this week?
# ------------------------------------------------------------------

_SURFACE_CACHE: dict[str, tuple[float, str | None]] = {}
_SURFACE_CACHE_TTL = 6 * 3600


def current_surface(tour: str) -> str | None:
    """Majority surface among tournaments running today on this tour, or
    None if that can't be determined (predictions then use overall only)."""
    now = time.monotonic()
    cached = _SURFACE_CACHE.get(tour)
    if cached and now - cached[0] < _SURFACE_CACHE_TTL:
        return cached[1]
    data = history._get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard")
    surface = None
    if data:
        counts: dict[str, int] = {}
        for ev in data.get("events", []):
            s = surface_of(ev.get("name") or "")
            counts[s] = counts.get(s, 0) + 1
        if counts:
            surface = max(counts, key=counts.get)
    _SURFACE_CACHE[tour] = (now, surface)
    return surface


def probability(engine: EloEngine, player_a: str, player_b: str,
                tour: str = "atp", surface: str | None = None) -> float | None:
    """P(player_a wins), or None if either player doesn't have enough match
    history. No home-advantage term — there's no real home-court equivalent
    in tour tennis."""
    p = params.get(tour)
    if engine.games(player_a) < p["min_games"] or engine.games(player_b) < p["min_games"]:
        return None
    return _blended_prob(engine, p, player_a, player_b, surface)


# ------------------------------------------------------------------
# ITF fallback: local CSVs (Sackmann schema). ESPN has no ITF endpoint and
# the classic public source (JeffSackmann/tennis_itf on GitHub) was
# unreachable when this was built — see README.md.
# ------------------------------------------------------------------

def _iter_csv_matches(csv_dir: Path) -> list[tuple[str, str, str]]:
    rows = []
    for path in sorted(csv_dir.glob("*.csv")):
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    d, w, l = row.get("tourney_date"), row.get("winner_name"), row.get("loser_name")
                    if d and w and l:
                        rows.append((d, w, l))
        except OSError as e:
            log.warning(f"Could not read {path}: {e}")
    rows.sort(key=lambda r: r[0])
    return rows


def build_engine_csv(csv_dir: str | Path) -> tuple[EloEngine, int]:
    csv_dir = Path(csv_dir)
    p = params.get("itf")
    engine = EloEngine(k_factor=p["k"])
    if not csv_dir.is_dir():
        log.warning(f"Tennis data directory not found: {csv_dir} — see README.md for setup")
        return engine, 0
    total = 0
    for _d, winner, loser in _iter_csv_matches(csv_dir):
        engine.record_result(winner, loser, 1.0)
        total += 1
    log.info(f"ITF (local CSVs): processed {total} matches")
    return engine, total
