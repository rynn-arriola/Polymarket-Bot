"""
DIVERGENCE BOT — Polymarket US, Elo-vs-market-price divergence entries

Commands:
    python divergence_bot.py            -> run the bot loop (dry-run or live per config.LIVE)
    python divergence_bot.py discover   -> print current candidate markets with model_prob/market_price/divergence
    python divergence_bot.py status     -> show open positions, results, win rate, P&L, avg divergence

Strategy: for each supported sport (config.SUPPORTED_SPORTS), compute our own
win probability from a pre-built Elo model (elo_ratings.json — build/refresh
with build_ratings.py) and compare it to Polymarket's current price. Enter
only when the two disagree by at least that sport's threshold
(config.DIVERGENCE_THRESHOLDS — derived from each model's noise floor), on
whichever side (favorite or underdog) is actually underpriced.

SAFETY: config.LIVE = False means NO real orders are ever sent.
"""

import json
import logging
import logging.handlers
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import config
import name_match
import reporting
import risk
import xgb_live
from db import db, db_init, day_open_balance, period_bounds_utc, today
from elo import basketball, esports, injuries, mlb, params, rosters, soccer, tennis
from elo.engine import EloEngine

# A second, WARNING-and-above log so problems (warnings/errors/CRITICAL untracked
# orders) are collected on their own — reviewable at a glance without wading
# through the INFO noise of every scan cycle. The main log still keeps
# everything.
ERROR_LOG = "divergence_bot.errors.log"
_error_handler = logging.handlers.RotatingFileHandler(
    ERROR_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
)
_error_handler.setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            "divergence_bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ),
        _error_handler,
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("divergence_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Ops alerts: WARNING+ records are ALSO pushed to a dedicated Discord webhook
# (config.DISCORD_ERRORS_WEBHOOK_URL) — deduped, batched, CRITICAL posts fast.
# Attached to the root logger so every module's problems are covered.
reporting.attach_discord_error_handler(logging.getLogger())

try:
    from polymarket_us import BadRequestError, NotFoundError, PolymarketUS
except ImportError:
    print("Missing SDK. Run:  pip install polymarket-us")
    sys.exit(1)


def make_client(authenticated: bool) -> "PolymarketUS":
    if authenticated:
        if "PASTE_YOUR" in config.KEY_ID or "PASTE_YOUR" in config.SECRET_KEY:
            log.error("API keys not set in config.py — live mode needs them.")
            sys.exit(1)
        return PolymarketUS(key_id=config.KEY_ID, secret_key=config.SECRET_KEY)
    return PolymarketUS()


# ------------------------------------------------------------------
# Market helpers (same defensive style as the existing bot.py — API
# schemas can shift, so never crash on a weird record, log and skip)
# ------------------------------------------------------------------

def get_meta(market: dict) -> dict:
    for key in ("metadata", "meta"):
        if isinstance(market.get(key), dict):
            return market[key]
    return market


def is_fullgame_moneyline(market: dict) -> bool:
    if market.get("closed") is True or market.get("archived") is True:
        return False
    stype = str(market.get("sportsMarketType") or "").lower()
    if stype == "moneyline":
        return True
    return stype.endswith(
        ("_full_game_winner", "_full_time_winner", "_match_winner", "_fight_winner")
    )


def market_slug(market: dict):
    return market.get("slug") or market.get("marketSlug")


def team_display_name(team: dict) -> str:
    """Polymarket's team.name is inconsistent across sports: for MLB/tennis
    it's already the full name (team.alias duplicates it), but for NBA/WNBA
    it's just the city ("Dallas") with the nickname in team.alias ("Wings")
    — verified live (2026-07-07) that using name alone fails every WNBA name
    match against ESPN's full "Dallas Wings"-style names. Combine the two
    when they're genuinely different; otherwise just use name."""
    name = (team.get("name") or "").strip()
    alias = (team.get("alias") or "").strip()
    if alias and alias != name and alias not in name:
        return f"{name} {alias}".strip()
    return name


def event_start_time(market: dict):
    ts = market.get("gameStartTime")
    if not ts:
        return None
    raw = str(ts).strip()
    try:
        s = raw.replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        m = re.search(r"([+-])(\d{2}):?(\d{2})?$", s)
        if m and ":" not in s[s.rfind(m.group(1)):]:
            s = s[: m.start()] + f"{m.group(1)}{m.group(2)}:{m.group(3) or '00'}"
        start = datetime.fromisoformat(s)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start
    except Exception as e:
        log.warning(f"Unparseable start time '{raw}': {e} — treating as started")
        return None


def _stored_utc_time(value):
    """Parse a stored ISO timestamp as aware UTC; invalid values return None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_started(market: dict) -> bool:
    start = event_start_time(market)
    if start is None:
        # Fail closed: an unknown/unparseable start time is treated as already
        # started (matching the warning in event_start_time), so a market with
        # a bad timestamp can never be traded in-play. Today this is belt-and-
        # suspenders — inside_entry_window also rejects unknown starts — but it
        # stops TRADE_BEFORE_START_MINUTES=0 from silently reopening the hole.
        return True
    return datetime.now(timezone.utc) >= start


def _start_iso(market: dict) -> str | None:
    """Event start as a UTC ISO string (for closing-line timing), or None."""
    start = event_start_time(market)
    return start.isoformat() if start else None


def inside_entry_window(market: dict) -> bool:
    minutes = getattr(config, "TRADE_BEFORE_START_MINUTES", 0)
    if not minutes:
        return True
    start = event_start_time(market)
    if start is None:
        return False
    now = datetime.now(timezone.utc)
    return now <= start <= now + timedelta(minutes=minutes)


def fetch_all_markets(client) -> list:
    markets, offset = [], 0
    while True:
        params = {"limit": 100, "offset": offset, "active": True, "closed": False}
        paginated = True
        try:
            page = client.markets.list(params)
        except TypeError:
            page = client.markets.list({"limit": 100, "active": True})
            paginated = False
        batch = page.get("markets", page) if isinstance(page, dict) else page
        if not batch:
            break
        markets.extend(batch)
        if not paginated:
            # The offset param was rejected — without it every iteration would
            # refetch this same first page, so stop after one.
            log.warning("markets.list rejected pagination params — only the first page was fetched")
            break
        if len(batch) < 100:
            break
        offset += 100
        if offset >= 20000:
            log.warning("Pagination cap hit at 20000 — market list may be incomplete")
            break
    log.info(f"Fetched {len(markets)} active markets")
    return markets


# ------------------------------------------------------------------
# Elo ratings (loaded once per run from elo_ratings.json — build/refresh
# with build_ratings.py; this process never hits the sport data sources)
# ------------------------------------------------------------------

RATINGS_FILE = "elo_ratings.json"
FRESHNESS_FILE = "elo_freshness.json"

# Per-sport freshness metadata written by build_ratings (last_built, latest
# game date, counts). Loaded alongside ratings and re-read on every hot-reload.
_freshness: dict = {}


def load_ratings() -> dict[str, EloEngine]:
    try:
        with open(RATINGS_FILE) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error(f"Could not load {RATINGS_FILE} ({e}) — run build_ratings.py first. "
                  f"No sport will have a model until it exists.")
        return {}
    engines = {}
    for sport_key, d in raw.items():
        engines[sport_key] = EloEngine.from_dict(d)
        log.info(f"Loaded {sport_key}: {len(engines[sport_key].ratings)} rated teams/players")
    load_freshness()
    return engines


def load_freshness() -> dict:
    """(Re)read the freshness sidecar into the module global. Fail-open: a
    missing/corrupt file leaves freshness empty, which never blocks trading."""
    global _freshness
    try:
        with open(FRESHNESS_FILE) as f:
            _freshness = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _freshness = {}
    stale = [s for s in _freshness if sport_stale(s)]
    if stale:
        log.warning(f"STALE ratings for {stale} — those sports are skipped until their next successful rebuild")
    return _freshness


def sport_stale(sport_key: str) -> str | None:
    """Reason string if this sport's ratings are too old to trust (no
    successful rebuild within RATINGS_STALE_HOURS), else None. Fail-open:
    unknown sport / missing timestamp / guard disabled -> never stale."""
    hours = getattr(config, "RATINGS_STALE_HOURS", 0)
    if not hours:
        return None
    meta = _freshness.get(sport_key)
    if not meta or not meta.get("last_built"):
        return None
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(meta["last_built"])).total_seconds() / 3600
    except (ValueError, TypeError):
        return None
    if age_h > hours:
        return f"ratings stale ({age_h:.0f}h since last rebuild, limit {hours}h)"
    return None


def divergence_threshold(sport_key: str) -> float:
    """Per-sport minimum divergence (config.DIVERGENCE_THRESHOLDS), derived
    from each model's measured noise floor — falls back to the flat
    DIVERGENCE_THRESHOLD for anything unlisted."""
    return getattr(config, "DIVERGENCE_THRESHOLDS", {}).get(
        sport_key, config.DIVERGENCE_THRESHOLD)


def _esports_roster_skip(sport_key: str, team_a: str, team_b: str, slug: str) -> bool:
    """True if either esports team should be skipped because its roster
    changed and the rating hasn't caught up (see elo/rosters.py). No-op for
    non-esports sports and for titles without a roster provider (e.g. cs2)."""
    if sport_key not in esports.TITLES:
        return False
    for team in (team_a, team_b):
        if not rosters.team_ok(sport_key, team):
            log.info(f"skip {slug}: roster changed for {team!r} — rating stale until lineup settles")
            return True
    return False


def _base_names(engine: EloEngine):
    """Rating keys that are actual team/player names. Tennis engines also
    hold per-surface keys like "Novak Djokovic|clay" — those must never be
    offered to the fuzzy name matcher or it can 'resolve' a player to his
    own surface rating."""
    return [k for k in engine.ratings if "|" not in k]


def _sport_probability(sport_key: str, engine: EloEngine, name_a: str, name_b: str,
                       m: dict) -> float | None:
    """RAW model P(name_a wins), before calibration. Dispatches to the right
    adapter with whatever game context that sport's model uses: probable
    pitchers for MLB, the game date (back-to-back check) for NBA/WNBA, the
    current tour surface for tennis, draw decomposition for FWC."""
    # Gated XGBoost hook: if this sport has a model that beat Elo out-of-sample
    # and is still fresh, it takes over; otherwise has_model is False and we
    # fall straight through to Elo below (the default — no model files ship).
    # The XGB prob is self-calibrated; the XGB-capable sports carry an identity
    # Elo Platt, so evaluate_market's apply_calibration is a no-op on top of it.
    if xgb_live.has_model(sport_key):
        start = event_start_time(m) or datetime.now(timezone.utc)
        ctx = {"game_date": start.date().isoformat()}
        if sport_key in ("atp", "wta"):
            ctx["surface"] = tennis.current_surface(sport_key)
        elif sport_key == "mlb":
            # Same probable-starter lookup the Elo path uses, so the XGB
            # pitcher features match live reality (name_a=home, name_b=away).
            ctx["home_pitcher"] = mlb.pitcher_for(name_a, start.date())
            ctx["away_pitcher"] = mlb.pitcher_for(name_b, start.date())
        xp = xgb_live.predict(sport_key, engine, name_a, name_b, ctx)
        if xp is not None:
            return xp
    if sport_key == "mlb":
        start = event_start_time(m)
        game_date = (start or datetime.now(timezone.utc)).date()
        return mlb.probability(
            engine, name_a, name_b,
            home_pitcher=mlb.pitcher_for(name_a, game_date),
            away_pitcher=mlb.pitcher_for(name_b, game_date),
        )
    if sport_key in ("nba", "wnba"):
        start = event_start_time(m)
        game_date = (start or datetime.now(timezone.utc)).date().isoformat()
        return basketball.probability(engine, name_a, name_b, league=sport_key,
                                      game_date=game_date)
    if sport_key in ("atp", "wta"):
        surface = tennis.current_surface(sport_key)
        return tennis.probability(engine, name_a, name_b, tour=sport_key, surface=surface)
    if sport_key == "itf":
        return tennis.probability(engine, name_a, name_b, tour="itf", surface=None)
    if sport_key == "fwc":
        return soccer.probability(engine, name_a, name_b)
    if sport_key == "lol":
        pp = _lol_player_prob(name_a, name_b)
        if pp is not None:
            return pp  # player-Elo (validated better); falls through to team-Elo if unavailable
    if sport_key in esports.TITLES:
        return esports.probability(engine, name_a, name_b, title=sport_key)
    return None


# LoL player-level model sidecar (built by build_ratings from Oracle's Elixir).
# Loaded once; used only when fresh enough (else team-Elo stays live).
_LOL_PLAYER_MODEL: dict | None = None
_LOL_PLAYER_MODEL_LOADED = False


def _reset_lol_player_model():
    """Force the LoL player sidecar to be re-read on next use — called after
    a data refresh rewrites lol_player_model.json."""
    global _LOL_PLAYER_MODEL, _LOL_PLAYER_MODEL_LOADED
    _LOL_PLAYER_MODEL, _LOL_PLAYER_MODEL_LOADED = None, False


def _load_lol_player_model() -> dict | None:
    global _LOL_PLAYER_MODEL, _LOL_PLAYER_MODEL_LOADED
    if _LOL_PLAYER_MODEL_LOADED:
        return _LOL_PLAYER_MODEL
    _LOL_PLAYER_MODEL_LOADED = True
    if not getattr(config, "LOL_PLAYER_ELO", True):
        return None
    try:
        with open("lol_player_model.json") as f:
            model = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    # Freshness gate: stale data carries stale lineups — don't deploy it over
    # the current-data team-Elo.
    try:
        latest = datetime.fromisoformat(model.get("latest_date", "2000-01-01"))
        age_days = (datetime.now() - latest).days
    except ValueError:
        return None
    if age_days > getattr(config, "LOL_PLAYER_FRESHNESS_DAYS", 45):
        log.info(f"LoL player model present but stale ({model.get('latest_date')}, "
                 f"{age_days}d old) — using team-Elo. Run fetch_oe.py + build_ratings to refresh.")
        return None
    log.info(f"LoL player-Elo ACTIVE (data through {model.get('latest_date')}, "
             f"{len(model.get('ratings', {}))} players)")
    _LOL_PLAYER_MODEL = model
    return model


def _lol_player_prob(team_a: str, team_b: str) -> float | None:
    """P(team_a wins) from the LoL player model: each team's current lineup
    (its most recent Oracle's Elixir game) -> mean player rating. None if the
    model is unavailable/stale or a lineup can't be resolved — caller then
    falls back to team-Elo."""
    from elo import lol_players
    model = _load_lol_player_model()
    if not model:
        return None
    lineups = model["team_lineups"]
    la = name_match.resolve(team_a, lineups.keys())
    lb = name_match.resolve(team_b, lineups.keys())
    if not la or not lb:
        return None
    return lol_players.probability_players(model["ratings"], lineups[la], lineups[lb])


# ------------------------------------------------------------------
# PAIRED single-team markets: some sports structure each game as one
# market per TEAM ("Will France win: Yes/No") instead of one head-to-head
# market. Verified live for FWC soccer (2026-07-07) and for part of the
# Dota 2 listings (2026-07-08: "atc-dota2-bb-re-...-bb" / "...-re" are two
# separate markets for the same match, both sides of each naming the SAME
# team). The opponent is found via the sibling market sharing the slug
# prefix (slug minus the trailing team abbreviation).
# ------------------------------------------------------------------

def _is_paired_single_team(m: dict) -> bool:
    """Both marketSides name the same team (Yes/No on one team) rather
    than two different teams."""
    sides = m.get("marketSides") or []
    if len(sides) != 2:
        return False
    names = {(s.get("team") or {}).get("name") for s in sides}
    return len(names) == 1 and None not in names


def _paired_opponent_map(markets: list) -> dict[str, dict[str, str]]:
    groups: dict[str, dict[str, str]] = {}
    for m in markets:
        if not is_fullgame_moneyline(m) or not _is_paired_single_team(m):
            continue
        team = (m.get("marketSides") or [{}])[0].get("team") or {}
        slug = market_slug(m)
        abbr = str(team.get("abbreviation") or "").lower()
        name = team_display_name(team)
        if not slug or not abbr or not name:
            continue
        parts = slug.split("-")
        if not parts or parts[-1] != abbr:
            continue
        prefix = "-".join(parts[:-1])
        groups.setdefault(prefix, {})[abbr] = name
    return groups


# ------------------------------------------------------------------
# Per-market evaluation: returns a candidate dict if this market clears the
# divergence threshold on some side, else None. Never raises on bad/missing
# data — logs and returns None instead.
# ------------------------------------------------------------------

def evaluate_market(pub, m: dict, engines: dict[str, EloEngine], fwc_groups: dict) -> dict | None:
    if not is_fullgame_moneyline(m):
        return None
    slug = market_slug(m)
    if not slug:
        return None
    low_slug = slug.lower()
    if any(x in low_slug for x in getattr(config, "EXCLUDE_MARKET_SLUGS", ())):
        return None  # excluded competition (e.g. NBA Summer League) — see config
    if event_started(m):
        return None  # pregame only — see config.PREGAME_ONLY note
    if not inside_entry_window(m):
        return None

    sides = m.get("marketSides") or []
    if len(sides) != 2:
        return None
    league = None
    for side in sides:
        team = side.get("team") or {}
        if team.get("league"):
            league = str(team["league"]).upper()
            break
    sport_key = config.SUPPORTED_SPORTS.get(league)
    if not sport_key:
        return None  # unsupported sport (includes all esports) — never fetched further

    engine = engines.get(sport_key)
    if engine is None or not engine.ratings:
        return None  # no model built for this sport yet

    stale = sport_stale(sport_key)
    if stale:
        return None  # ratings too old to trust — see startup/reload warning

    try:
        bbo = pub.markets.bbo(slug)
        data = bbo.get("marketData", bbo) if isinstance(bbo, dict) else {}
        ask_long = float((data.get("bestAsk") or {}).get("value"))
        bid_long = float((data.get("bestBid") or {}).get("value"))
        ask_short = float((data.get("shortQuote") or {}).get("value"))
        open_interest = float(data.get("openInterest") or 0)
        ask_depth = int(data.get("askDepth") or 0)
    except (TypeError, ValueError, AttributeError):
        return None

    min_oi = getattr(config, "MIN_OPEN_INTEREST", 100)
    if ask_depth < 1 or open_interest < min_oi:
        return None

    # Spread guard (config.MAX_SPREAD_USD): a wide bid-ask gap means a thin,
    # unreliable book — and since divergence is measured against the MID while
    # an entry fills at the ASK, a wide spread systematically overstates the
    # edge. A missing bid counts as bid=0 (the thinnest possible book).
    max_spread = getattr(config, "MAX_SPREAD_USD", 0)
    if max_spread and (ask_long - (bid_long or 0.0)) > max_spread:
        return None

    if _is_paired_single_team(m):
        team = (sides[0].get("team") or {})
        own_abbr = str(team.get("abbreviation") or "").lower()
        parts = slug.split("-")
        prefix = "-".join(parts[:-1]) if parts and parts[-1] == own_abbr else None
        group = fwc_groups.get(prefix, {})
        opponent_name = next((n for a, n in group.items() if a != own_abbr), None)
        team_name = team_display_name(team)
        if not opponent_name or not team_name:
            return None

        known = _base_names(engine)
        resolved_team = name_match.resolve(team_name, known)
        resolved_opp = name_match.resolve(opponent_name, known)
        if not resolved_team or not resolved_opp:
            return None
        if _esports_roster_skip(sport_key, resolved_team, resolved_opp, slug):
            return None
        model_prob = _sport_probability(sport_key, engine, resolved_team, resolved_opp, m)
        if model_prob is None:
            return None
        model_prob = params.apply_calibration(model_prob, sport_key)
        market_price = round((ask_long + bid_long) / 2, 4) if bid_long else ask_long
        divergence = model_prob - market_price
        if not (config.PRICE_FLOOR <= market_price <= config.PRICE_CEIL):
            return None
        if divergence < divergence_threshold(sport_key):
            return None
        if divergence > getattr(config, "MAX_DIVERGENCE", 1.0):
            log.info(f"skip {slug}: divergence {divergence:+.1%} too large — "
                     f"market likely knows something the model doesn't")
            return None
        # The order will fill at the ASK, not the mid — the edge must clear
        # the threshold at the price actually paid, or the spread eats it.
        if model_prob - ask_long < divergence_threshold(sport_key):
            log.info(f"skip {slug}: edge at the ask ({model_prob - ask_long:+.1%}) below "
                     f"threshold — the spread eats the mid-priced divergence")
            return None
        return {
            "slug": slug, "sport": league, "side": "long", "team": team_name,
            # Shared event id (the slug prefix both teams' markets share) so
            # the one-position-per-event guard can't buy Yes on BOTH teams
            # of the same match through their two separate market slugs.
            "event_id": prefix or slug,
            "matchup": f"{team_name} vs {opponent_name}", "ask": ask_long,
            "model_prob": model_prob, "market_price": market_price, "divergence": divergence,
            "game_start": _start_iso(m), "is_long": 1,  # paired markets are always the long/Yes side
        }

    # Two-team market (MLB/NBA/WNBA/tennis): both teams live in marketSides.
    long_side = next((s for s in sides if s.get("long")), None)
    short_side = next((s for s in sides if not s.get("long")), None)
    if not long_side or not short_side:
        return None
    long_team = team_display_name(long_side.get("team") or {}) or long_side.get("description")
    short_team = team_display_name(short_side.get("team") or {}) or short_side.get("description")
    if not long_team or not short_team:
        return None

    home_name, away_name = long_team, short_team
    for side in sides:
        t = side.get("team") or {}
        if t.get("ordering") == "home":
            home_name = team_display_name(t) or home_name
        elif t.get("ordering") == "away":
            away_name = team_display_name(t) or away_name

    known = _base_names(engine)
    resolved_home = name_match.resolve(home_name, known)
    resolved_away = name_match.resolve(away_name, known)
    if not resolved_home or not resolved_away:
        return None

    # Key-player injury filter: an Elo rating reflects the roster that
    # earned it, so if either team's top scorers are listed Out/Doubtful,
    # skip rather than trade a rating we know is stale (see config.py).
    if sport_key in ("nba", "wnba") and getattr(config, "INJURY_FILTER", True):
        outs = []
        for team in (resolved_home, resolved_away):
            outs += [f"{p} ({team})" for p in injuries.key_players_out(
                sport_key, team,
                top_n=getattr(config, "INJURY_TOP_N", 3),
                skip_statuses=tuple(getattr(config, "INJURY_SKIP_STATUSES", ("Out", "Doubtful"))),
            )]
        if outs:
            log.info(f"skip {slug}: key player(s) out — {', '.join(outs)}")
            return None

    if _esports_roster_skip(sport_key, resolved_home, resolved_away, slug):
        return None

    model_prob_home = _sport_probability(sport_key, engine, resolved_home, resolved_away, m)
    if model_prob_home is None:
        return None
    # Calibrate once on the home-side probability, then derive the other
    # side from the calibrated value — applying the (non-symmetric) Platt
    # correction independently to p and 1-p would make the two sides'
    # probabilities stop summing to 1.
    model_prob_home = params.apply_calibration(model_prob_home, sport_key)
    model_prob_long = model_prob_home if home_name == long_team else (1 - model_prob_home)
    model_prob_short = 1 - model_prob_long

    market_price_long = round((ask_long + bid_long) / 2, 4) if bid_long else ask_long
    market_price_short = ask_short  # no short-side bid exposed by the API — ask is the entry cost anyway

    div_long = model_prob_long - market_price_long
    div_short = model_prob_short - market_price_short

    max_div = getattr(config, "MAX_DIVERGENCE", 1.0)
    candidates = []
    min_div = divergence_threshold(sport_key)
    if config.PRICE_FLOOR <= market_price_long <= config.PRICE_CEIL and div_long >= min_div:
        if div_long > max_div:
            log.info(f"skip {slug} (long): divergence {div_long:+.1%} too large — "
                     f"market likely knows something the model doesn't")
        elif model_prob_long - ask_long < min_div:
            # A long entry fills at the ask, not the mid the divergence was
            # measured against — the edge must survive the price actually paid.
            # (The short side needs no such gate: div_short is already priced
            # at ask_short, the short entry cost.)
            log.info(f"skip {slug} (long): edge at the ask ({model_prob_long - ask_long:+.1%}) "
                     f"below threshold — the spread eats the mid-priced divergence")
        else:
            candidates.append(("long", long_team, ask_long, model_prob_long, market_price_long, div_long))
    if (not getattr(config, "LONG_ONLY", False)
            and config.PRICE_FLOOR <= market_price_short <= config.PRICE_CEIL and div_short >= min_div):
        if div_short > max_div:
            log.info(f"skip {slug} (short): divergence {div_short:+.1%} too large — "
                     f"market likely knows something the model doesn't")
        else:
            candidates.append(("short", short_team, ask_short, model_prob_short, market_price_short, div_short))
    if not candidates:
        return None
    # If both sides somehow cleared the bar (shouldn't happen with a
    # sane model — it would mean the market's two prices summed to well
    # under 1), take the larger edge rather than guess.
    side, team_name, ask, model_prob, market_price, divergence = max(candidates, key=lambda c: c[5])

    return {
        "slug": slug, "sport": league, "side": side, "team": team_name,
        "event_id": slug,
        "matchup": f"{long_team} vs {short_team}", "ask": ask,
        "model_prob": model_prob, "market_price": market_price, "divergence": divergence,
        "game_start": _start_iso(m), "is_long": 1 if side == "long" else 0,
    }


# ------------------------------------------------------------------
# Bankroll (same pattern as bot.py's fetch_balance/get_effective_bankroll)
# ------------------------------------------------------------------

_LAST_BALANCE = None


def fetch_balance(client) -> float | None:
    try:
        resp = client.account.balances()
    except Exception as e:
        log.warning(f"Could not fetch account balance: {e}")
        return None
    balances = resp.get("balances") if isinstance(resp, dict) else None
    if not balances:
        return None
    for b in balances:
        if str(b.get("currency") or "USD").upper() == "USD":
            try:
                return float(b.get("currentBalance"))
            except (TypeError, ValueError):
                return None
    return None


def get_effective_bankroll(client) -> float:
    """Sizing bankroll = the balance captured at the START OF TODAY, held FIXED
    all day (via day_open_balance). A day that opens at $700 sizes off $700 all
    day — a win doesn't upsize and a loss doesn't downsize mid-day; tomorrow
    re-snapshots (e.g. $750). Compounds day-to-day, steady intraday."""
    global _LAST_BALANCE
    if not config.LIVE:
        realized = db(
            "SELECT COALESCE(SUM(pnl),0) FROM positions WHERE pnl IS NOT NULL AND live=0", fetch=True
        )[0][0]
        return day_open_balance(getattr(config, "DRY_RUN_BANKROLL", 1000.0) + (realized or 0.0))

    bal = fetch_balance(client)
    if bal is not None:
        _LAST_BALANCE = bal
        return day_open_balance(bal)
    if _LAST_BALANCE is not None:
        log.warning(f"Balance fetch failed — using last known ${_LAST_BALANCE:.2f}")
        return day_open_balance(_LAST_BALANCE)
    # live=1 only: dry-run P&L must not leak into the live bankroll estimate
    # (the server carries a lot of simulated P&L that isn't real money).
    realized = db("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE pnl IS NOT NULL AND live=1", fetch=True)[0][0]
    fallback = config.BANKROLL + (realized or 0.0)
    log.warning(f"Balance API unavailable, no cached value yet — falling back to BANKROLL+live P&L (${fallback:.2f})")
    # persist=False: this is a GUESS, not a real balance — it must not get
    # snapshotted as the day-open, or a restart during an API outage would
    # lock the whole day's sizing/loss-limit baseline to config.BANKROLL.
    return day_open_balance(fallback, persist=False)


# ------------------------------------------------------------------
# Scan loop
# ------------------------------------------------------------------

def _as_int(v, default: int = 0) -> int:
    """Defensive int coercion — the API returns some integer quantities as
    strings (protobuf int64 convention)."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _recover_order(auth, slug: str, requested_qty: int):
    """After orders.create RAISED: find out whether the order actually reached
    the exchange anyway (a timeout fires after the request was sent, so the
    exchange may have accepted it). Returns (order_id, quantity, status) if the
    order is live on the exchange, or None if it verifiably is not. Raises if
    the verification itself fails — the caller must then treat the order as
    possibly live."""
    resp = auth.orders.list({"slugs": [slug]})
    orders = resp.get("orders") if isinstance(resp, dict) else resp
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        o_slug = o.get("marketSlug") or (o.get("marketMetadata") or {}).get("slug")
        if o_slug == slug:
            qty = _as_int(o.get("quantity"), requested_qty)
            return str(o.get("id") or "RECOVERED"), qty, "pending"
    # Not among open orders — it may have filled immediately (a filled order
    # leaves the open list). The dup guard guarantees we held no prior position
    # on this slug, so any held quantity can only be this order. Per-market
    # lookup (not the paginated full listing); an inconclusive lookup raises so
    # the caller treats the order as possibly live rather than absent.
    qty = _position_quantity(auth, slug)
    if qty is None:
        raise RuntimeError("portfolio position lookup failed")
    if qty > 0:
        return "RECOVERED", min(_as_int(qty), requested_qty) or requested_qty, "open"
    return None


def guard_untracked_exchange_state(auth):
    """LIVE only, once per cycle: any slug present on the exchange (open order
    or held position) but absent from positions.db is an untracked order —
    e.g. one placed in a previous run that died between send and DB insert
    (_UNTRACKED_SLUGS_THIS_RUN does not survive a restart). Block it from
    entry and escalate; a human must reconcile it. Fail-open on API errors —
    this is a safety net, not a gate."""
    if not config.LIVE:
        return
    exchange_slugs: set = set()
    try:
        _collect_slugs(auth.orders.list(), exchange_slugs)
        # NOTE: the unfiltered positions listing is paginated — beyond one
        # page this sweep's coverage degrades (it may MISS untracked slugs,
        # which is fail-open and safe; it can never wrongly flag one). The
        # decision paths that must be exact use _position_quantity instead.
        _collect_slugs(auth.portfolio.positions(), exchange_slugs)
    except Exception as e:
        log.warning(f"Could not sweep exchange state for untracked orders: {e}")
        return
    if not exchange_slugs:
        return
    known = {r[0] for r in db("SELECT market_slug FROM positions", fetch=True)}
    for slug in sorted(exchange_slugs - known):
        if slug not in risk._UNTRACKED_SLUGS_THIS_RUN:
            risk._UNTRACKED_SLUGS_THIS_RUN.add(slug)
            log.critical(f"UNTRACKED exchange position/order on {slug} — present on the "
                         f"exchange but not in positions.db. Blocking bot entries on it; "
                         f"reconcile it manually (cancel it or add a row to positions.db).")


def record_valid_signal(cand: dict, stake: float) -> None:
    """Record a paper signal before price/risk/order decisions.

    This ledger is deliberately independent of `positions`: a failed paper
    write cannot block or alter a real order, and its rows never participate
    in risk, bankroll, settlement-card, or real-P&L queries.
    """
    if not getattr(config, "TRACK_ALL_VALID_SIGNALS", True):
        return
    ask = cand["ask"]
    quantity = int(stake // ask)
    if quantity < 1:
        return
    try:
        db(
            """INSERT OR IGNORE INTO shadow_signals
               (created_at, market_slug, event_id, matchup, sport, side, price,
                quantity, stake, live, model_prob, market_price, divergence,
                game_start, is_long)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                cand["slug"], cand.get("event_id") or cand["slug"], cand["matchup"],
                cand["sport"], cand["team"], ask, quantity, round(quantity * ask, 2),
                int(config.LIVE), cand["model_prob"], cand["market_price"],
                cand["divergence"], cand.get("game_start"), cand.get("is_long"),
            ),
        )
    except Exception as e:
        # Analytics must fail open: an unavailable ledger never changes trade
        # eligibility or order handling.
        log.warning(f"Could not record paper signal for {cand['slug']}: {e}")


def set_signal_decision(slug: str, decision: str, reason: str | None = None) -> None:
    """Set the latest paper-ledger decision without ever touching positions."""
    if not getattr(config, "TRACK_ALL_VALID_SIGNALS", True):
        return
    try:
        db(
            """UPDATE shadow_signals SET decision=?, decision_reason=?
               WHERE market_slug=? AND decision != 'traded'""",
            (decision, reason, slug),
        )
    except Exception as e:
        log.warning(f"Could not record signal decision for {slug}: {e}")


def live_price_reason(cand: dict) -> str | None:
    """Live-only price policy; candidate evaluation stays wider for learning."""
    price = cand["ask"]  # actual limit-order price, not the midpoint
    floor = getattr(config, "LIVE_ENTRY_PRICE_FLOOR", config.PRICE_FLOOR)
    ceiling = getattr(config, "LIVE_ENTRY_PRICE_CEIL", config.PRICE_CEIL)
    if price < floor:
        return f"live price policy ({price:.0%} below {floor:.0%} floor)"
    if price > ceiling:
        return f"live price policy ({price:.0%} above {ceiling:.0%} ceiling)"
    return None


def scan_once(pub, auth, engines: dict[str, EloEngine]):
    guard_untracked_exchange_state(auth)
    markets = fetch_all_markets(pub)
    fwc_groups = _paired_opponent_map(markets)
    effective_bankroll = get_effective_bankroll(auth)
    base_stake = risk.stake_for(effective_bankroll)
    balance_label = "Balance" if config.LIVE else "Balance (simulated, dry-run)"
    log.info(f"{balance_label} ${effective_bankroll:.2f} -> base stake ${base_stake:.2f} this cycle")

    candidates_found = 0
    for m in markets:
        try:
            cand = evaluate_market(pub, m, engines, fwc_groups)
        except Exception as e:
            log.warning(f"error evaluating market {market_slug(m)}: {e}")
            continue
        if not cand:
            continue
        candidates_found += 1

        # Fractional-Kelly: stake scales with the model's edge on the side
        # being bought (falls back to flat sizing if KELLY_FRACTION is 0).
        stake = risk.kelly_stake(effective_bankroll, cand["ask"], cand["model_prob"])

        # Record every valid candidate before ANY live decision. This keeps a
        # complete, paper-only sample of price-policy and risk-gated signals.
        record_valid_signal(cand, stake)
        price_reason = live_price_reason(cand)
        if price_reason:
            set_signal_decision(cand["slug"], "not_traded", price_reason)
            log.info(f"paper-only {cand['slug']}: {price_reason}")
            continue

        event_id = cand.get("event_id") or cand["slug"]
        reason = risk.risk_check(cand["sport"], event_id, cand["slug"], effective_bankroll, stake)
        if reason:
            set_signal_decision(cand["slug"], "not_traded", reason)
            log.info(f"skip {cand['slug']}: {reason}")
            continue

        # The create call itself can raise AFTER the exchange accepted the
        # order (timeout, 5xx, connection drop mid-response) — without this
        # handling the exception would skip both the DB insert and the
        # re-entry block, and the next cycle would buy the market AGAIN.
        entry_status = "pending"
        try:
            order_id, quantity = risk.place_order(
                auth if config.LIVE else pub, cand["slug"], cand["side"], cand["ask"], stake
            )
        except Exception as e:
            if not config.LIVE:
                set_signal_decision(cand["slug"], "order_failed", "dry-run order simulation failed")
                log.warning(f"dry-run order simulation failed for {cand['slug']}: {e}")
                continue
            if isinstance(e, BadRequestError):
                # A clean 400 is a definitive rejection — the order does not
                # exist on the exchange. Safe to skip without blocking.
                set_signal_decision(cand["slug"], "order_failed", "exchange order rejected")
                log.warning(f"order rejected (400) for {cand['slug']}: {e} — nothing placed")
                continue
            # Ambiguous failure — the order MAY be live. Block first, verify second.
            risk._UNTRACKED_SLUGS_THIS_RUN.add(cand["slug"])
            try:
                recovered = _recover_order(auth, cand["slug"], int(stake // cand["ask"]))
            except Exception as verify_err:
                set_signal_decision(cand["slug"], "order_unknown", "ambiguous exchange order state")
                log.critical(f"POSSIBLE UNTRACKED LIVE ORDER on {cand['slug']}: create raised "
                             f"({e}) and verification also failed ({verify_err}). Blocking this "
                             f"market for this run — CHECK THE APP/EXCHANGE MANUALLY.")
                continue
            if recovered is None:
                risk._UNTRACKED_SLUGS_THIS_RUN.discard(cand["slug"])
                set_signal_decision(cand["slug"], "order_failed", "exchange order create failed")
                log.warning(f"order create failed for {cand['slug']} ({e}) — verified NOT on "
                            f"the exchange, safe to retry next cycle")
                continue
            order_id, quantity, entry_status = recovered
            risk._UNTRACKED_SLUGS_THIS_RUN.discard(cand["slug"])
            log.warning(f"order create raised ({e}) but the order DID reach the exchange "
                        f"(order {order_id}, {quantity} contract(s), {entry_status}) — recording it")

        # The order (real or dry-run) is already placed, so a DB-write failure
        # here must never look like "no entry happened" — that would let the
        # next cycle re-buy the same market. Retry transient failures (e.g. the
        # db briefly locked by a concurrent `status` read); if it still fails,
        # permanently block re-entry this run and escalate loudly for a LIVE
        # order (a real untracked position needs a human).
        inserted, last_err = False, None
        for attempt in range(3):
            try:
                db(
                    """INSERT INTO positions
                       (created_at, market_slug, event_id, matchup, sport, side, price,
                        quantity, stake, live, order_id, status, pnl, model_prob, market_price, divergence,
                        game_start, is_long)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?, NULL, ?,?,?,?,?)""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        cand["slug"], event_id, cand["matchup"], cand["sport"], cand["team"], cand["ask"],
                        quantity, round(quantity * cand["ask"], 2), int(config.LIVE), order_id,
                        entry_status,
                        cand["model_prob"], cand["market_price"], cand["divergence"],
                        cand.get("game_start"), cand.get("is_long"),
                    ),
                )
                inserted = True
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1)
        if not inserted:
            set_signal_decision(
                cand["slug"], "order_unknown" if config.LIVE else "order_failed",
                "position record failed after order" if config.LIVE else "dry-run position record failed",
            )
            risk._UNTRACKED_SLUGS_THIS_RUN.add(cand["slug"])
            if config.LIVE:
                log.critical(f"UNTRACKED LIVE ORDER: order {order_id} on {cand['slug']} was SENT "
                             f"but the DB insert failed after retries ({last_err}). CANCEL IT IN THE "
                             f"APP or add it to positions.db by hand. Blocking re-entry this run.")
            else:
                log.warning(f"DB insert failed for {cand['slug']} after retries ({last_err}) — "
                            f"blocking re-entry this run")
            continue

        set_signal_decision(cand["slug"], "traded")
        log.info(f"ENTERED {cand['team']} ({cand['side']}) @ {cand['ask']:.2f} stake ${stake:.2f} "
                 f"({cand['sport']}) model={cand['model_prob']:.1%} market={cand['market_price']:.1%} "
                 f"divergence={cand['divergence']:+.1%} [{'LIVE' if config.LIVE else 'DRY'}]")

    log.info(f"Cycle done: {candidates_found} candidate(s) cleared the divergence threshold")


# ------------------------------------------------------------------
# Fill confirmation, stale-order cancellation, settlement (same patterns as
# the existing bot.py, reimplemented against this project's own db.py)
# ------------------------------------------------------------------

def _collect_slugs(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("marketSlug", "slug") and isinstance(v, str):
                out.add(v)
            else:
                _collect_slugs(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_slugs(item, out)


def _position_quantity(client, slug: str) -> float | None:
    """Net position held on ONE market, asked with the API's market filter.
    Returns 0.0 for 'definitively no position', a positive count if held, or
    None when the lookup itself failed (callers must treat that as
    inconclusive, never as absence).

    This replaces 'list the whole portfolio and collect slugs': that listing
    is PAGINATED, and once the account held more positions than one page,
    everything past it looked absent — three FILLED orders were marked
    'cancelled' exactly that way (2026-07-13, ~$31 of settled losses
    invisible to the bot until reconciled by hand)."""
    try:
        resp = client.portfolio.positions({"market": slug})
    except NotFoundError:
        return 0.0  # filter matched nothing — no position on this market
    except Exception as e:
        log.warning(f"Position lookup failed for {slug}: {e}")
        return None
    positions = resp.get("positions") if isinstance(resp, dict) else None
    if positions is None:
        return None
    entries = (list(positions.values()) if isinstance(positions, dict)
               else positions if isinstance(positions, list) else [])
    total = 0.0
    for p in entries:
        if not isinstance(p, dict):
            continue
        try:
            total += float(p.get("netPosition") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _order_state(client, order_id: str) -> tuple[str, int] | None:
    """(state, cumQuantity filled) for an order, or None if it can't be
    fetched — callers then fall back to the coarser portfolio-presence check."""
    if not order_id or order_id in ("DRY_RUN", "RECOVERED"):
        return None
    try:
        resp = client.orders.retrieve(order_id)
        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        state = str(order.get("state") or "")
        if not state:
            return None
        return state, _as_int(order.get("cumQuantity"), 0)
    except NotFoundError:
        # The exchange has no such order — definitively gone (never rested, or
        # already purged). Distinct from a transient failure: callers resolve
        # it immediately instead of re-checking (and re-logging) every cycle.
        return "NOT_FOUND"
    except Exception as e:
        log.warning(f"Could not fetch order {order_id}: {e}")
        return None


def _mark_open(pid: int, slug: str, price: float, requested, filled: int):
    """Mark a position open at its ACTUAL filled size. A partial fill
    (cumQuantity < requested) corrects quantity and stake so settlement P&L
    and the committed-bankroll check run on real numbers, not the request."""
    if filled and filled != requested:
        db("UPDATE positions SET status='open', quantity=?, stake=? WHERE id=?",
           (filled, round(filled * price, 2), pid))
        log.warning(f"Fill confirmed on {slug} at {filled}/{requested} contract(s) "
                    f"(partial) — quantity and stake corrected")
    else:
        db("UPDATE positions SET status='open' WHERE id=?", (pid,))
        log.info(f"Fill confirmed on {slug}")


# Order states that mean "this order is finished and will never fill further".
_ORDER_DONE_STATES = ("ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED",
                      "ORDER_STATE_EXPIRED", "ORDER_STATE_REPLACED")


def confirm_fills(client):
    if not config.LIVE:
        db("UPDATE positions SET status='open' WHERE status='pending' AND live=0")
        return
    rows = db("SELECT id, market_slug, order_id, price, quantity FROM positions "
              "WHERE status='pending' AND live=1", fetch=True)
    if not rows:
        return
    for pid, slug, order_id, price, quantity in rows:
        st = _order_state(client, order_id)
        if isinstance(st, tuple):
            state, filled = st
            if state == "ORDER_STATE_FILLED":
                _mark_open(pid, slug, price, quantity, filled or quantity)
            elif state in _ORDER_DONE_STATES:
                # Finished without our cancel loop seeing it (external cancel,
                # late reject). A partial fill still leaves a real position.
                if filled > 0:
                    _mark_open(pid, slug, price, quantity, filled)
                else:
                    db("UPDATE positions SET status='cancelled' WHERE id=?", (pid,))
                    log.info(f"Order on {slug} ended {state} with no fill — marked cancelled")
            # NEW / PARTIALLY_FILLED / PENDING_* -> still working, stays pending
            # (cancel_stale_orders resolves it at the CANCEL_UNFILLED_AFTER_MIN mark).
            continue
        # st is "NOT_FOUND" or None (transient failure). Ask the portfolio
        # about THIS market specifically — the per-market filter can't be
        # fooled by pagination the way the full listing was. A visible
        # position confirms the fill; anything else leaves the row pending.
        # Absence NEVER cancels here — not even NOT_FOUND: a just-placed
        # order can be invisible to the exchange's read path for a moment
        # (three orders were mis-cancelled <1s after placement on 2026-07-14,
        # then filled and resolved unseen). Giving up on an order is solely
        # cancel_stale_orders' job: it waits CANCEL_UNFILLED_AFTER_MIN and
        # issues a definitive cancel before concluding anything.
        qty = _position_quantity(client, slug)
        if qty is not None and qty > 0:
            _mark_open(pid, slug, price, quantity,
                       min(_as_int(qty), _as_int(quantity)) or _as_int(quantity))


def cancel_stale_orders(client):
    if not config.LIVE:
        return
    rows = db(
        "SELECT id, order_id, created_at, market_slug, price, quantity "
        "FROM positions WHERE status='pending' AND live=1",
        fetch=True,
    )
    stale = []
    for pid, order_id, created_at, slug, price, quantity in rows:
        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).total_seconds() / 60
        if age_min >= config.CANCEL_UNFILLED_AFTER_MIN:
            stale.append((pid, order_id, slug, age_min, price, quantity))
    for pid, order_id, slug, age_min, price, quantity in stale:
        try:
            # CancelOrderParams requires the market slug (SDK 0.1.2) — a
            # single-arg cancel raises TypeError and never cancels.
            client.orders.cancel(order_id, {"marketSlug": slug})
        except NotFoundError:
            # The exchange doesn't know this order (sync reject, or already
            # purged). At CANCEL_UNFILLED_AFTER_MIN old that's definitive —
            # read-path lag is a seconds-scale phenomenon — so fall through
            # to the fill checks below and resolve the row. Without this,
            # a never-rested order would stay pending forever, since
            # confirm_fills deliberately never cancels on absence.
            pass
        except Exception as e:
            log.warning(f"Cancel failed for {slug}: {e}")
            continue
        # The order itself is the authority on what filled before the cancel
        # landed — cumQuantity catches PARTIAL fills, which the portfolio
        # presence check below cannot size. A "NOT_FOUND" string (order already
        # gone) falls through to the portfolio check like a transient miss —
        # but here we've just cancelled it, so an absent slug means cancelled.
        st = _order_state(client, order_id)
        if isinstance(st, tuple):
            state, filled = st
            if filled > 0:
                _mark_open(pid, slug, price, quantity, filled)
                log.info(f"Order on {slug} filled {filled} contract(s) before cancel — tracked as open")
            else:
                db("UPDATE positions SET status='cancelled' WHERE id=?", (pid,))
                log.info(f"Cancelled unfilled order on {slug} after {age_min:.0f} min")
            continue
        qty = _position_quantity(client, slug)
        if qty is None:
            log.warning(f"Post-cancel fill check inconclusive for {slug} — leaving as pending")
            continue
        if qty > 0:
            _mark_open(pid, slug, price, quantity,
                       min(_as_int(qty), _as_int(quantity)) or _as_int(quantity))
            log.info(f"Order on {slug} FILLED just before cancel — tracked as open")
        else:
            db("UPDATE positions SET status='cancelled' WHERE id=?", (pid,))
            log.info(f"Cancelled unfilled order on {slug} after {age_min:.0f} min")


def verify_cancelled_rows(auth):
    """Self-heal mis-cancelled rows: cross-check every live 'cancelled' row
    against the exchange ONCE, and restore it if the exchange disagrees.

    Both mis-cancel incidents (2026-07-13 pagination, 2026-07-14 read-path
    lag) had the same shape: the DB said cancelled while the exchange held a
    real position — settled losses invisible to reporting until repaired by
    hand. The exchange is the source of truth, so verify against it:

      - position still held        -> restore to 'open' (normal settlement
                                      flow takes it from there)
      - POSITION_RESOLUTION posted -> settle with the exchange's own
                                      fee-inclusive P&L
      - neither                    -> genuinely cancelled; flag verified so
                                      the row is never checked again

    Checks wait until the row is a few minutes old so the exchange's read
    path has certainly indexed any fill, and each row is verified at most
    once, so the steady-state API cost is zero. Known blind spot: a
    MANUAL position on the same market would make a correctly-cancelled bot
    row look filled — acceptable; the loud ops log makes any heal visible.
    """
    if not config.LIVE:
        return
    rows = db(
        "SELECT id, market_slug, price, quantity, created_at FROM positions "
        "WHERE live=1 AND status='cancelled' AND COALESCE(cancel_verified,0)=0",
        fetch=True,
    )
    grace = 5  # minutes; read-path lag is seconds-scale, this is ample
    for pid, slug, price, quantity, created_at in rows:
        try:
            age_min = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(created_at)).total_seconds() / 60
        except (TypeError, ValueError):
            age_min = None
        if age_min is not None and age_min < grace:
            continue  # too young for the exchange read path to be trustworthy
        qty = _position_quantity(auth, slug)
        if qty is None:
            continue  # lookup failed — retry next cycle
        if qty > 0:
            _mark_open(pid, slug, price, quantity,
                       min(_as_int(qty), _as_int(quantity)) or _as_int(quantity))
            db("UPDATE positions SET cancel_verified=1 WHERE id=?", (pid,))
            log.warning(f"MIS-CANCEL healed: {slug} was marked cancelled but the "
                        f"exchange holds {qty:.0f} contract(s) — restored to open")
            continue
        res = _resolution_pnl(auth, slug)
        if isinstance(res, tuple):
            pnl, stable = res
            status = "won" if pnl > 0 else ("lost" if pnl < 0 else "push")
            db("UPDATE positions SET status=?, pnl=?, settled_at=?, "
               "pnl_reconciled=?, cancel_verified=1 WHERE id=?",
               (status, pnl, datetime.now(timezone.utc).isoformat(),
                1 if stable else 0, pid))
            log.warning(f"MIS-CANCEL healed: {slug} was marked cancelled but "
                        f"resolved on the exchange — settled {status.upper()} "
                        f"P&L {pnl:+.2f}")
            continue
        if res is RESOLUTION_CHECK_FAILED:
            continue  # couldn't check — retry next cycle, conclude NOTHING
        # res is None: the feed answered — no position, no resolution.
        # Only this definitive answer marks the cancel verified.
        db("UPDATE positions SET cancel_verified=1 WHERE id=?", (pid,))
    return


def mark_rescheduled_positions(pub) -> int:
    """Mark LIVE open positions whose match the exchange has RESCHEDULED.

    The trigger is deliberately NOT elapsed time: once a position is
    RESCHEDULE_MARK_AFTER_HOURS past its ORIGINAL start, we re-read the
    market's own gameStartTime. Only a start moved INTO THE FUTURE — the
    exchange's own word that the match was postponed — triggers a mark; a
    long game keeps its past start time and is always left alone, and a
    short delay is already in the past by the time we look. The entry
    stays open to avoid paying an early-exit fee. The risk gate gives each
    marked open position one extra slot, so a postponed match does not block
    the normal MAX_OPEN_POSITIONS budget.

    `game_start` is updated to the exchange's new start so closing-line capture
    still runs near the actual game. `original_game_start` preserves the first
    start we bought against. Fail-open everywhere: lookup/update failure ->
    retry next cycle. Positions whose rescheduled market carries NO date yet
    (TBD) are left alone until the exchange sets one. Dry-run positions are
    skipped (nothing real to protect from fees)."""
    hours = getattr(config, "RESCHEDULE_MARK_AFTER_HOURS",
                    getattr(config, "RESCHEDULE_EXIT_AFTER_HOURS", 0))
    if not hours or not config.LIVE:
        return 0
    now = datetime.now(timezone.utc)
    rows = db("SELECT id, market_slug, game_start, original_game_start, rescheduled_start "
              "FROM positions "
              "WHERE live=1 AND status='open' AND game_start IS NOT NULL", fetch=True)
    marked = 0
    for pid, slug, gstart, original_start, old_rescheduled_start in rows:
        try:
            start = datetime.fromisoformat(original_start or gstart)
        except (TypeError, ValueError):
            continue
        if now < start + timedelta(hours=hours):
            continue  # not overdue — normal in-play window
        try:
            resp = pub.markets.retrieve_by_slug(slug)
            market = resp.get("market", resp) if isinstance(resp, dict) else {}
        except Exception as e:
            log.warning(f"Reschedule check failed for {slug}: {e}")
            continue
        new_start = event_start_time(market)
        if new_start is None or new_start <= now:
            continue  # still in the past (long game) or TBD — leave alone
        new_start_iso = new_start.isoformat()
        if old_rescheduled_start == new_start_iso:
            continue
        db("""UPDATE positions
              SET original_game_start=COALESCE(original_game_start, game_start),
                  game_start=?,
                  rescheduled_start=?,
                  rescheduled_at=?
              WHERE id=?""",
           (new_start_iso, new_start_iso, now.isoformat(), pid))
        marked += 1
        log.warning(f"RESCHEDULED {slug}: match moved to {new_start.isoformat()[:16]} — "
                    f"keeping position open; open-position cap gets one extra slot")
    return marked


def _market_mid(pub, slug: str, is_long: bool) -> float | None:
    """Current market price for one side of a market, priced exactly the way
    evaluate_market prices an entry: mid (ask+bid)/2 for the long side, the
    short quote for the short side. None on any bad/missing data."""
    try:
        bbo = pub.markets.bbo(slug)
        data = bbo.get("marketData", bbo) if isinstance(bbo, dict) else {}
        if is_long:
            ask = float((data.get("bestAsk") or {}).get("value"))
            bid = float((data.get("bestBid") or {}).get("value"))
            return round((ask + bid) / 2, 4) if bid else round(ask, 4)
        return round(float((data.get("shortQuote") or {}).get("value")), 4)
    except (TypeError, ValueError, AttributeError):
        return None


def capture_closing_lines(pub):
    """Closing-line value (CLV): snapshot the market price for our side near
    tip-off so we can later ask whether the market moved TOWARD our bets.
    Inside the final CLOSING_CAPTURE_MINUTES we re-snapshot every cycle, so
    the last value before start is the true closing line; if we only observed
    the position after start and never captured, we grab one post-start price
    as a fallback. Priced per side (is_long) so CLV compares like with like."""
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=getattr(config, "CLOSING_CAPTURE_MINUTES", 5))
    rows = db(
        "SELECT id, market_slug, game_start, is_long, closing_price FROM positions "
        "WHERE status IN ('pending','open') AND game_start IS NOT NULL",
        fetch=True,
    )
    for pid, slug, gstart, is_long, existing in rows:
        try:
            start = datetime.fromisoformat(gstart)
        except (TypeError, ValueError):
            continue
        in_window = start - window <= now <= start
        fallback = existing is None and start < now <= start + timedelta(minutes=30)
        if not (in_window or fallback):
            continue
        mid = _market_mid(pub, slug, bool(is_long))
        if mid is not None:
            db("UPDATE positions SET closing_price=?, closing_captured_at=? WHERE id=?",
               (mid, now.isoformat(), pid))
            log.info(f"CLV close {mid:.3f} captured for {slug}")


def capture_signal_closing_lines(pub):
    """Capture CLV for paper signals without involving real positions."""
    if not getattr(config, "TRACK_ALL_VALID_SIGNALS", True):
        return
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=getattr(config, "CLOSING_CAPTURE_MINUTES", 5))
    rows = db(
        """SELECT id, market_slug, game_start, is_long, closing_price
           FROM shadow_signals WHERE status='open' AND game_start IS NOT NULL""",
        fetch=True,
    )
    for sid, slug, gstart, is_long, existing in rows:
        start = _stored_utc_time(gstart)
        if start is None:
            continue
        in_window = start - window <= now <= start
        fallback = existing is None and start < now <= start + timedelta(minutes=30)
        if not (in_window or fallback):
            continue
        mid = _market_mid(pub, slug, bool(is_long))
        if mid is not None:
            db("UPDATE shadow_signals SET closing_price=?, closing_captured_at=? WHERE id=?",
               (mid, now.isoformat(), sid))
            log.info(f"Paper-signal CLV close {mid:.3f} captured for {slug}")


def _refresh_rescheduled_signal_start(client, sid: int, slug: str,
                                      current_start: datetime | None,
                                      now: datetime) -> bool:
    """Move a paper signal to a later exchange-confirmed start, if present."""
    try:
        resp = client.markets.retrieve_by_slug(slug)
        market = resp.get("market", resp) if isinstance(resp, dict) else {}
    except Exception:
        return False
    new_start = event_start_time(market)
    if new_start is None or new_start <= now:
        return False
    if current_start is not None and new_start <= current_start:
        return False
    db(
        """UPDATE shadow_signals
           SET game_start=?, closing_price=NULL, closing_captured_at=NULL
           WHERE id=?""",
        (new_start.isoformat(), sid),
    )
    log.info(f"Paper signal {slug} rescheduled to {new_start.isoformat()[:16]}")
    return True


def check_signal_settlements(client) -> int:
    """Settle paper signals from public market data, never account activity.

    `paper_pnl` is an estimated result using the configured per-contract fee,
    so it is intentionally separate from the exchange-exact P&L in positions.
    """
    if not getattr(config, "TRACK_ALL_VALID_SIGNALS", True):
        return 0
    now = datetime.now(timezone.utc)
    interval = timedelta(minutes=getattr(config, "SIGNAL_SETTLEMENT_CHECK_INTERVAL_MIN", 10))
    rows = db(
        """SELECT id, market_slug, price, quantity, is_long, game_start, last_settlement_check_at
           FROM shadow_signals WHERE status='open'""",
        fetch=True,
    )
    settled = 0
    for sid, slug, price, quantity, is_long, game_start, checked_at in rows:
        start = _stored_utc_time(game_start)
        if start is not None and start > now:
            continue
        last_checked = _stored_utc_time(checked_at)
        if last_checked is not None and now - last_checked < interval:
            continue
        try:
            settlement = client.markets.settlement(slug)
        except Exception:
            _refresh_rescheduled_signal_start(client, sid, slug, start, now)
            db("UPDATE shadow_signals SET last_settlement_check_at=? WHERE id=?",
               (now.isoformat(), sid))
            continue
        if not settlement:
            _refresh_rescheduled_signal_start(client, sid, slug, start, now)
            db("UPDATE shadow_signals SET last_settlement_check_at=? WHERE id=?",
               (now.isoformat(), sid))
            continue
        settle_price = None
        if isinstance(settlement, dict):
            for key in ("settlement", "settlementPrice", "price", "value"):
                value = settlement.get(key)
                if isinstance(value, dict):
                    value = value.get("value")
                if value is not None:
                    try:
                        settle_price = float(value)
                        break
                    except (TypeError, ValueError):
                        pass
        if settle_price is None:
            log.warning(f"Unrecognized paper-signal settlement format for {slug}")
            db("UPDATE shadow_signals SET last_settlement_check_at=? WHERE id=?",
               (now.isoformat(), sid))
            continue
        long_side = is_long is None or bool(is_long)
        payout = settle_price if long_side else (1.0 - settle_price)
        gross_pnl = quantity * (payout - price)
        fee = quantity * getattr(config, "SIGNAL_PAPER_FEE_PER_CONTRACT", 0.012)
        paper_pnl = round(gross_pnl - fee, 2)
        status = "won" if gross_pnl > 0 else ("lost" if gross_pnl < 0 else "push")
        db(
            """UPDATE shadow_signals
               SET status=?, settlement_price=?, estimated_fee=?, paper_pnl=?,
                   settled_at=?, last_settlement_check_at=?
               WHERE id=?""",
            (status, settle_price, round(fee, 2), paper_pnl,
             now.isoformat(), now.isoformat(), sid),
        )
        settled += 1
        log.info(f"PAPER SIGNAL SETTLED {status.upper()} {slug}  estimated P&L {paper_pnl:+.2f}")
    return settled


_STUCK_SETTLEMENT_WARNED: dict = {}  # pid -> monotonic time of last "stuck" warning


# Sentinel: the resolution check itself FAILED (feed down, rate-limited,
# unrecognized shape). Distinct from None = the feed answered and there is
# definitively no resolution. verify_cancelled_rows marked a mis-cancelled
# row (ica-dnt, 2026-07-14) verified-forever off one transient failure
# because the two cases were conflated — callers must retry on this value
# and never conclude anything from it.
RESOLUTION_CHECK_FAILED = object()


def _resolution_pnl(auth, slug: str):
    """(P&L, stable) from the account's POSITION_RESOLUTION activity for this
    market; None when the feed answered and shows NO resolution (definitive);
    RESOLUTION_CHECK_FAILED when the check itself failed — retry later.
    Callers should branch with isinstance(res, tuple) for the settled case.

    `stable` is False while the activity is younger than
    RESOLUTION_STABLE_MINUTES: the exchange RESTATES the cost basis (rolls
    fees in) shortly after posting the resolution — 16 positions audited on
    2026-07-13 carried P&L read too early, overstating the book ~$10.6.
    Callers may act on an unstable figure (it's close, and Discord shouldn't
    wait) but must not mark it final (pnl_reconciled) until stable.

    This feed is the same one the app's History tab shows, and it publishes
    well BEFORE the public /markets/{slug}/settlement record — polling only
    the latter made settlement messages lag by hours (reported 2026-07-13)."""
    try:
        resp = auth.portfolio.activities({
            "marketSlug": slug,
            "types": ["ACTIVITY_TYPE_POSITION_RESOLUTION"],
            "limit": 1,
        })
    except Exception as e:
        log.warning(f"Resolution-activity check failed for {slug}: {e}")
        return RESOLUTION_CHECK_FAILED
    if not isinstance(resp, dict) or "activities" not in resp:
        return RESOLUTION_CHECK_FAILED  # unexpected shape is NOT an empty feed
    if not resp["activities"]:
        return None
    activities = resp["activities"]
    pr = activities[0].get("positionResolution") or {}
    before = pr.get("beforePosition") or {}
    try:
        cost = float((before.get("cost") or {}).get("value"))
        cash_value = float((before.get("cashValue") or {}).get("value"))
    except (TypeError, ValueError):
        log.warning(f"Unrecognized resolution activity format for {slug}")
        return RESOLUTION_CHECK_FAILED
    stable = True  # unknown age -> assume stable (pre-updateTime API shapes)
    raw_ts = pr.get("updateTime") or activities[0].get("updateTime")
    if raw_ts:
        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            stable = age_min >= getattr(config, "RESOLUTION_STABLE_MINUTES", 45)
        except (ValueError, TypeError):
            pass
    return round(cash_value - cost, 2), stable


def check_settlements(client, auth=None):
    rows = db(
        "SELECT id, market_slug, price, quantity, is_long, created_at, live "
        "FROM positions WHERE status='open'",
        fetch=True,
    )
    settled_ids = []
    for pid, slug, price, quantity, is_long, created_at, live in rows:
        # FAST PATH (live positions): the account's own resolution activity —
        # available as soon as the exchange resolves the position (what the
        # History tab shows), and it carries the REAL fee-inclusive P&L, so
        # the settlement message posts the true figure immediately instead of
        # an estimate that reconcile_live_pnl corrects later.
        if live and auth is not None:
            res = _resolution_pnl(auth, slug)
            if isinstance(res, tuple):
                pnl, stable = res
                status = "won" if pnl > 0 else ("lost" if pnl < 0 else "push")
                # Settle NOW (Discord shouldn't wait), but only stamp the P&L
                # final once the activity is old enough for the exchange's
                # fee restatement — else reconcile_live_pnl refreshes it.
                db("UPDATE positions SET status=?, pnl=?, settled_at=?, pnl_reconciled=? WHERE id=?",
                   (status, pnl, datetime.now(timezone.utc).isoformat(), 1 if stable else 0, pid))
                settled_ids.append(pid)
                _STUCK_SETTLEMENT_WARNED.pop(pid, None)
                log.info(f"SETTLED {status.upper()} {slug} (via resolution activity)  P&L {pnl:+.2f}"
                         + ("" if stable else "  (provisional — final figure reconciles shortly)"))
                continue
        # SLOW PATH: the public settlement record — the only option for
        # dry-run positions, and the fallback when the activity feed is
        # quiet or unavailable.
        try:
            settlement = client.markets.settlement(slug)
        except Exception as e:
            # Usually just "not settled yet". But a systemic endpoint failure
            # (auth expiry, schema change) looks identical and would silently
            # never settle anything — so once a position has been open unusually
            # long, surface it (throttled per position, not every cycle).
            try:
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).total_seconds() / 3600
            except (TypeError, ValueError):
                age_h = 0
            stuck_h = getattr(config, "SETTLEMENT_STUCK_WARNING_DAYS", 14) * 24
            if age_h > stuck_h and time.monotonic() - _STUCK_SETTLEMENT_WARNED.get(pid, 0) > 4 * 3600:
                log.warning(f"{slug} still open after {age_h:.0f}h and settlement checks keep failing "
                            f"({e}) — verify the settlement endpoint/auth is healthy")
                _STUCK_SETTLEMENT_WARNED[pid] = time.monotonic()
            continue
        if not settlement:
            continue
        settle_price = None
        if isinstance(settlement, dict):
            for k in ("settlement", "settlementPrice", "price", "value"):
                v = settlement.get(k)
                if isinstance(v, dict):
                    v = v.get("value")
                if v is not None:
                    try:
                        settle_price = float(v)
                        break
                    except (TypeError, ValueError):
                        pass
        if settle_price is None:
            log.warning(f"Unrecognized settlement format for {slug}")
            continue
        # settle_price is the market's LONG-side outcome. A long position is
        # worth settle_price per contract; a SHORT position is worth
        # (1 - settle_price) — it pays out when the long side loses. is_long is
        # NULL only for pre-migration rows, which were long, so default to long.
        long_side = is_long is None or bool(is_long)
        payout = settle_price if long_side else (1.0 - settle_price)
        pnl = round(quantity * (payout - price), 2)
        status = "won" if pnl > 0 else ("lost" if pnl < 0 else "push")
        db(
            "UPDATE positions SET status=?, pnl=?, settled_at=? WHERE id=?",
            (status, pnl, datetime.now(timezone.utc).isoformat(), pid),
        )
        settled_ids.append(pid)
        _STUCK_SETTLEMENT_WARNED.pop(pid, None)
        log.info(f"SETTLED {status.upper()} {slug} ({'long' if long_side else 'short'})  P&L {pnl:+.2f}")
    return settled_ids


def reconcile_live_pnl(client):
    """Replace the estimated P&L on settled LIVE positions with the exchange's
    own realized figure (cost basis and proceeds, both fee-inclusive) from the
    activity feed. Our settlement estimate ignores trading fees, so it's
    optimistic; this is what actually hit the balance, and it's side-agnostic
    (works for long and short alike). No-op in dry-run."""
    if not config.LIVE:
        return
    rows = db(
        """SELECT id, market_slug FROM positions
           WHERE live=1 AND pnl_reconciled=0 AND status IN ('won','lost','push')""",
        fetch=True,
    )
    for pid, slug in rows:
        res = _resolution_pnl(client, slug)
        if not isinstance(res, tuple):
            continue  # not posted yet / check failed — retry next cycle
        real_pnl, stable = res
        if not stable:
            # The exchange restates cost basis (fees) shortly after posting
            # the resolution; a figure read too early sticks WRONG forever
            # (16 drifted rows, 2026-07-13 audit). Wait for it to settle.
            continue
        status = "won" if real_pnl > 0 else ("lost" if real_pnl < 0 else "push")
        db("UPDATE positions SET pnl=?, status=?, pnl_reconciled=1 WHERE id=?",
           (real_pnl, status, pid))
        log.info(f"Reconciled {slug}: actual P&L {real_pnl:+.2f} (fees included)")


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------

def cmd_discover():
    pub = make_client(False)
    engines = load_ratings()
    if not engines:
        print("No elo_ratings.json found — run `python build_ratings.py` first.")
        return
    markets = fetch_all_markets(pub)
    fwc_groups = _paired_opponent_map(markets)
    shown = 0
    for m in markets:
        if not is_fullgame_moneyline(m):
            continue
        league = None
        for side in m.get("marketSides") or []:
            team = side.get("team") or {}
            if team.get("league"):
                league = str(team["league"]).upper()
                break
        if league not in config.SUPPORTED_SPORTS:
            continue
        try:
            cand = evaluate_market(pub, m, engines, fwc_groups)
        except Exception as e:
            print(f"{market_slug(m)}: error evaluating ({e})")
            continue
        slug = market_slug(m)
        if cand:
            print(f"[CANDIDATE] {slug} | {cand['matchup']} | side={cand['side']} "
                  f"model={cand['model_prob']:.1%} market={cand['market_price']:.1%} "
                  f"divergence={cand['divergence']:+.1%}")
        else:
            print(f"[skip]      {slug} ({league})")
        shown += 1
        if shown >= 40:
            break
    if shown == 0:
        print("No supported-sport moneyline markets found in the current active list.")
    pub.close()


def cmd_status():
    db_init()
    print(reporting.format_summary_text(reporting.summary_snapshot()))
    print(reporting.format_divergence_table())
    print(reporting.format_freshness())


def cmd_errors(n: int = 60):
    """Print the most recent logged problems (WARNING and above) so they can be
    reviewed and fixed. These accumulate in ERROR_LOG while the bot runs."""
    try:
        with open(ERROR_LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"No {ERROR_LOG} yet — nothing logged (no warnings or errors).")
        return
    if not lines:
        print("No warnings or errors logged. All clean.")
        return
    tail = lines[-n:]
    print(f"=== last {len(tail)} of {len(lines)} logged problems ({ERROR_LOG}) ===\n")
    print("".join(tail), end="")


def cmd_run():
    db_init()
    engines = load_ratings()
    if not engines:
        log.error("No elo_ratings.json — run `python build_ratings.py` before starting the loop.")
        sys.exit(1)
    mode = "LIVE — REAL MONEY" if config.LIVE else "DRY-RUN — no real orders"
    thresholds = getattr(config, "DIVERGENCE_THRESHOLDS", {})
    threshold_desc = ", ".join(f"{k}:{v:.1%}" for k, v in sorted(thresholds.items())) or f"{config.DIVERGENCE_THRESHOLD:.0%}"
    log.info(f"Divergence bot starting | {mode} | thresholds {threshold_desc} | "
             f"sports {list(engines.keys())}")
    log.info(f"Ratings age: {reporting.freshness_oneline()}")
    pub = make_client(False)
    auth = make_client(True)
    reporting.post_discord_summary("Bot started")
    reporting.post_discord_paper_summary("Bot started")
    reporting.post_discord_clv("Bot started")
    reporting.post_discord_paper_clv("Bot started")
    last_discord_status = time.monotonic()
    last_clv_report = time.monotonic()

    # Self-refresh: rebuild all ratings every DATA_REFRESH_HOURS in a
    # background process and hot-reload, so a 24/7 bot never drifts stale.
    from refresh import DataRefresher
    refresher = DataRefresher()

    # `engines` is mutated in place on reload so scan_once keeps seeing the
    # live dict (it's captured by reference here and passed in each cycle).
    def _reload_ratings() -> bool:
        fresh = load_ratings()
        if not fresh:
            return False
        engines.clear()
        engines.update(fresh)
        _reset_lol_player_model()  # re-read the rebuilt LoL sidecar next use
        xgb_live.reset_cache()     # pick up any newly-trained/dropped-in model
        return True

    while True:
        try:
            scan_once(pub, auth, engines)
            confirm_fills(auth)
            capture_closing_lines(pub)
            capture_signal_closing_lines(pub)
            cancel_stale_orders(auth)
            verify_cancelled_rows(auth)
            mark_rescheduled_positions(pub)
            settled_ids = check_settlements(pub, auth if config.LIVE else None)
            check_signal_settlements(pub)
            if config.LIVE:
                reconcile_live_pnl(auth)
            if settled_ids:
                reporting.post_discord_settlements(settled_ids)
                reporting.post_discord_summary(f"{len(settled_ids)} position(s) settled")
                reporting.post_discord_paper_summary(f"{len(settled_ids)} position(s) settled")
                last_discord_status = time.monotonic()
            interval = getattr(config, "DISCORD_STATUS_INTERVAL_MIN", 30) * 60
            if interval > 0 and time.monotonic() - last_discord_status >= interval:
                reporting.post_discord_summary("Scheduled status update")
                reporting.post_discord_paper_summary("Scheduled status update")
                last_discord_status = time.monotonic()
            # CLV report N times a day (default 4 = every 6h) on its own webhook
            per_day = getattr(config, "CLV_REPORT_TIMES_PER_DAY", 0)
            if per_day > 0 and time.monotonic() - last_clv_report >= 24 / per_day * 3600:
                reporting.post_discord_clv("Scheduled CLV report")
                reporting.post_discord_paper_clv("Scheduled CLV report")
                last_clv_report = time.monotonic()
            reporting.maybe_post_daily_digest()
            reporting.maybe_post_ops_digest()
            refresher.tick(_reload_ratings)
        except KeyboardInterrupt:
            log.info("Shutting down (Ctrl+C)")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
        time.sleep(getattr(config, "SCAN_INTERVAL_SECONDS", 60))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "discover":
        cmd_discover()
    elif cmd == "status":
        cmd_status()
    elif cmd == "errors":
        cmd_errors()
    else:
        cmd_run()
