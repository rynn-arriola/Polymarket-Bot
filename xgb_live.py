"""Gated XGBoost inference — the future-proof hook.

Elo always runs. This layer lets a per-sport XGBoost model TAKE OVER a sport's
probability, but ONLY when a trained model exists that beat Elo out-of-sample
and is still fresh. Default state (no model files) -> every predict returns
None -> the caller falls back to Elo, so today nothing changes.

The point: the day a new data source appears (lineups, pace, injuries-as-
numbers), you add those as feature columns, retrain with train_xgb.py, and if
the model clears the gate its sport flips to XGBoost automatically — no other
code change. Same "activate only when it earns it" pattern as the LoL
player-model freshness gate.

Two rules keep this honest:
  * FEATURE BUILDERS ARE SHARED with training (xgb_features imports them), so
    the vector at inference is identical to the vector the model trained on.
  * Only ENGINE-RECONSTRUCTIBLE features are used — everything here is derived
    from the loaded Elo engine (+ live surface for tennis), so there is no
    hidden state to keep in sync.

Heavy deps (xgboost, numpy) are imported lazily, so a bot running pure Elo
never pays their memory cost.
"""

import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path

import config

log = logging.getLogger("divergence_bot.xgb_live")

MODELS_DIR = Path(__file__).resolve().parent / "xgb_models"

# Feature contracts (order matters — training and inference must agree).
#
# BASE_FEATURES is the universal Elo vector any two-outcome sport can produce
# from its engine alone — the "feed Elo to XGBoost" baseline that works
# everywhere. A sport's list is BASE_FEATURES + whatever EXTRA orthogonal
# signal it has data for (NBA adds rest/back-to-back, tennis adds surface).
# Adding a feature later = append to that sport's list here and to its
# extractor; the gate then decides whether it earns activation.
BASE_FEATURES = ["elo_exp", "elo_gap", "rating_a", "rating_b",
                 "games_a", "games_b"]

NBA_FEATURES = ["elo_gap", "elo_exp", "rating_home", "rating_away",
                "rest_home", "rest_away", "b2b_home", "b2b_away",
                "games_home", "games_away"]
TENNIS_FEATURES = ["elo_exp", "overall_gap", "surface_gap", "surf_known",
                   "rating_a", "rating_b", "surf_games_a", "surf_games_b",
                   "games_a", "games_b", "is_clay", "is_grass"]

# MLB decomposes its Elo signal into team vs. starting-pitcher components —
# elo_exp is the pitcher-aware home win prob the live model actually trades.
MLB_FEATURES = ["elo_exp", "team_gap", "pitcher_gap",
                "rating_home", "rating_away", "games_home", "games_away"]
# FWC (World Cup): elo_exp is the draw-decomposed P(home wins OUTRIGHT), the
# number the single-team markets settle on.
FWC_FEATURES = ["elo_exp", "elo_gap", "rating_home", "rating_away",
                "games_home", "games_away"]

# LoL: the gated player-blend (first gate clear, 2026-07-13 — beat player-Elo
# +0.0044 and team-Elo +0.0139 on test). ALL features come from OE-consistent
# state (the lol_player_model.json sidecar), never the live Leaguepedia match
# engine — mixing the two would be train/serve drift. Player aggregates are
# NaN when a lineup has <3 rated players (P6: NaN, never 0).
PLAYER_FEATURES = ["elo_exp", "elo_gap", "rating_a", "rating_b", "games_a", "games_b",
                   "p_exp", "p_gap", "p_min_gap", "p_spread_diff",
                   "p_experience_diff"]
LOL_FEATURES = PLAYER_FEATURES

# Esports (and any future title-based sport) use the universal base vector —
# no orthogonal data yet, so this is the honest Elo-only baseline until
# per-match lineups / series-format / tier features land (see XGBOOST_PLAN.md).
ESPORTS_TITLES = ("dota2", "cs2", "lol", "valorant")

FEATURES_FOR = {"nba": NBA_FEATURES, "wnba": NBA_FEATURES,
                "atp": TENNIS_FEATURES, "wta": TENNIS_FEATURES,
                "mlb": MLB_FEATURES, "fwc": FWC_FEATURES,
                **{t: BASE_FEATURES for t in ESPORTS_TITLES},
                "lol": PLAYER_FEATURES, "valorant": PLAYER_FEATURES}


# ------------------------------------------------------------------
# Feature builders — shared by training (walk-forward, engine at each game's
# state) and live inference (engine at current state). Identical either way.
# ------------------------------------------------------------------

def _rest_days(last_played: dict, team: str, game_date: str) -> int:
    prev = last_played.get(team)
    if not prev:
        return 7
    try:
        return min((date.fromisoformat(game_date) - date.fromisoformat(prev)).days, 14)
    except (ValueError, TypeError):
        return 7


def base_features(engine, a: str, b: str) -> dict:
    """Universal Elo feature vector for a two-outcome match, from the engine
    alone — shared by training and inference so they can never drift. `a`/`b`
    are in the sport's prediction order (esports: alphabetical, predicts
    P(a beats b)). Extend a sport by adding features ALONGSIDE these, never by
    changing these."""
    gap = engine.get_rating(a) - engine.get_rating(b)
    return {
        "elo_exp": engine.probability(a, b),
        "elo_gap": gap,
        "rating_a": engine.get_rating(a),
        "rating_b": engine.get_rating(b),
        "games_a": engine.games(a),
        "games_b": engine.games(b),
    }


def mlb_features(engine, home: str, away: str, home_pitcher=None, away_pitcher=None) -> dict:
    """MLB feature vector, home perspective (predicts P(home wins)). elo_exp is
    the SAME pitcher-aware probability mlb.probability produces live; team_gap
    and pitcher_gap expose the two components separately so the trees can weight
    them. Shared by training (via the replay callback) and inference."""
    from elo import mlb, params
    p = params.get("mlb")
    pitchers = engine.extras.get("pitchers", {})
    team_gap = engine.get_rating(home) - engine.get_rating(away)
    ph = mlb._pitcher_rating(pitchers, home_pitcher)
    pa = mlb._pitcher_rating(pitchers, away_pitcher)
    eff_gap = team_gap + p["home_adv"] + p["pitcher_weight"] * (ph - pa)
    return {
        "elo_exp": 1.0 / (1.0 + 10 ** (-eff_gap / 400.0)),
        "team_gap": team_gap,
        "pitcher_gap": ph - pa,
        "rating_home": engine.get_rating(home),
        "rating_away": engine.get_rating(away),
        "games_home": engine.games(home),
        "games_away": engine.games(away),
    }


def fwc_features(engine, home: str, away: str) -> dict:
    """World Cup feature vector, home perspective. elo_exp is P(home wins
    OUTRIGHT) after draw decomposition — the number the single-team markets
    settle on — matching soccer.probability."""
    from elo import params
    from elo.engine import decompose_win_draw_loss
    p = params.get("fwc")
    gap = engine.get_rating(home) - engine.get_rating(away)
    raw = 1.0 / (1.0 + 10 ** (-gap / 400.0))
    win_home, _draw, _win_away = decompose_win_draw_loss(raw, p["draw_rate"])
    return {
        "elo_exp": win_home,
        "elo_gap": gap,
        "rating_home": engine.get_rating(home),
        "rating_away": engine.get_rating(away),
        "games_home": engine.games(home),
        "games_away": engine.games(away),
    }


PLAYER_MIN_KNOWN = 3  # <3 rated players per lineup -> player aggregates are NaN
LOL_MIN_KNOWN = PLAYER_MIN_KNOWN


def player_features(state: dict, t1: str, t2: str,
                    lineup1: list[str], lineup2: list[str]) -> dict:
    """Player-blend row for alphabetically ordered stable team keys.

    State contains team Elo/games plus player ratings/games. The walk-forward
    extractor and live sidecars call this exact builder.
    """
    import math
    nan = float("nan")
    te, tg = state.get("team_elo", {}), state.get("team_games", {})
    ratings, played = state.get("ratings", {}), state.get("played", {})
    r1, r2 = te.get(t1, 1500.0), te.get(t2, 1500.0)
    row = {
        "elo_exp": 1.0 / (1.0 + 10 ** (-(r1 - r2) / 400.0)),
        "elo_gap": r1 - r2,
        "rating_a": r1, "rating_b": r2,
        "games_a": tg.get(t1, 0), "games_b": tg.get(t2, 0),
        "p_exp": nan, "p_gap": nan, "p_min_gap": nan,
        "p_spread_diff": nan, "p_experience_diff": nan,
    }
    k1 = [ratings[x] for x in lineup1 if x in ratings]
    k2 = [ratings[x] for x in lineup2 if x in ratings]
    if len(k1) >= PLAYER_MIN_KNOWN and len(k2) >= PLAYER_MIN_KNOWN:
        m1, m2 = sum(k1) / len(k1), sum(k2) / len(k2)
        sd = lambda v, m: math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
        row["p_exp"] = 1.0 / (1.0 + 10 ** (-(m1 - m2) / 400.0))
        row["p_gap"] = m1 - m2
        row["p_min_gap"] = min(k1) - min(k2)
        row["p_spread_diff"] = sd(k1, m1) - sd(k2, m2)
        e1 = sum(played.get(x, 0) for x in lineup1) / len(lineup1)
        e2 = sum(played.get(x, 0) for x in lineup2) / len(lineup2)
        row["p_experience_diff"] = e1 - e2
    return row


def lol_features(state: dict, t1: str, t2: str,
                 lineup1: list[str], lineup2: list[str]) -> dict:
    """Compatibility wrapper for the original LoL feature contract."""
    return player_features(state, t1, t2, lineup1, lineup2)


_LOL_SIDECAR: dict | None = None
_LOL_SIDECAR_LOADED = False
_VALORANT_SIDECAR: dict | None = None
_VALORANT_SIDECAR_LOADED = False


def reset_lol_sidecar():
    """Re-read lol_player_model.json on next use (ratings hot-reload path)."""
    global _LOL_SIDECAR, _LOL_SIDECAR_LOADED
    _LOL_SIDECAR, _LOL_SIDECAR_LOADED = None, False


def _lol_sidecar() -> dict | None:
    """The OE sidecar, freshness-gated exactly like the live player-Elo path
    (config.LOL_PLAYER_FRESHNESS_DAYS): stale lineups/state -> None -> the
    caller falls back to Elo. Requires the team_elo extension (older sidecar
    files without it are unusable for the blend)."""
    global _LOL_SIDECAR, _LOL_SIDECAR_LOADED
    if _LOL_SIDECAR_LOADED:
        return _LOL_SIDECAR
    _LOL_SIDECAR_LOADED = True
    try:
        with open("lol_player_model.json") as f:
            model = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not model.get("team_elo"):
        log.info("LoL sidecar predates the team_elo extension — rebuild via "
                 "build_ratings before the XGB blend can run; Elo stays live")
        return None
    try:
        latest = datetime.fromisoformat(model.get("latest_date", "2000-01-01"))
        age_days = (datetime.now() - latest).days
    except ValueError:
        return None
    if age_days > getattr(config, "LOL_PLAYER_FRESHNESS_DAYS", 45):
        return None  # stale lineups — the player-Elo path logs this already
    _LOL_SIDECAR = model
    return model


def reset_valorant_sidecar():
    """Re-read valorant_player_model.json on next use."""
    global _VALORANT_SIDECAR, _VALORANT_SIDECAR_LOADED
    _VALORANT_SIDECAR, _VALORANT_SIDECAR_LOADED = None, False


def _valorant_sidecar() -> dict | None:
    """Return fresh, complete VCT state; otherwise signal team-Elo fallback."""
    global _VALORANT_SIDECAR, _VALORANT_SIDECAR_LOADED
    if _VALORANT_SIDECAR_LOADED:
        return _VALORANT_SIDECAR
    _VALORANT_SIDECAR_LOADED = True
    try:
        with open("valorant_player_model.json") as handle:
            model = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    dict_fields = ("ratings", "played", "team_elo", "team_games",
                   "team_lineups", "team_lookup")
    if any(not isinstance(model.get(key), dict) for key in dict_fields):
        return None
    if not model["team_elo"] or not model["team_lineups"] or not model["team_lookup"]:
        return None
    try:
        age_days = (date.today()
                    - date.fromisoformat(str(model.get("latest_date", ""))[:10])).days
    except (TypeError, ValueError):
        return None
    if age_days < 0 or age_days > getattr(config, "VALORANT_PLAYER_FRESHNESS_DAYS", 45):
        return None
    _VALORANT_SIDECAR = model
    return model


def _valorant_live_features(name_a: str, name_b: str) -> tuple[dict, bool] | None:
    """Resolve display names to stable VLR team IDs and build one live row."""
    import name_match
    sidecar = _valorant_sidecar()
    if not sidecar:
        return None
    lookup = sidecar["team_lookup"]
    resolved_a = name_match.resolve(name_a, lookup.keys())
    resolved_b = name_match.resolve(name_b, lookup.keys())
    if not resolved_a or not resolved_b:
        return None
    team_a, team_b = lookup[resolved_a], lookup[resolved_b]
    lineups = sidecar["team_lineups"]
    if team_a == team_b or team_a not in lineups or team_b not in lineups:
        return None
    first, second = sorted((team_a, team_b))
    row = player_features(sidecar, first, second, lineups[first], lineups[second])
    return row, first != team_a


def basketball_features(engine, league: str, home: str, away: str, game_date: str) -> dict:
    from elo import basketball, params
    p = params.get(league)
    last_played = engine.extras.get("last_played", {})
    gap = basketball._gap(engine, p, home, away, game_date)
    return {
        "elo_gap": gap,
        "elo_exp": 1.0 / (1.0 + 10 ** (-gap / 400.0)),
        "rating_home": engine.get_rating(home),
        "rating_away": engine.get_rating(away),
        "rest_home": _rest_days(last_played, home, game_date),
        "rest_away": _rest_days(last_played, away, game_date),
        "b2b_home": int(basketball._b2b(last_played, home, game_date)),
        "b2b_away": int(basketball._b2b(last_played, away, game_date)),
        "games_home": engine.games(home),
        "games_away": engine.games(away),
    }


def tennis_features(engine, tour: str, a: str, b: str, surface: str | None) -> dict:
    from elo import tennis, params
    p = params.get(tour)
    min_s = p.get("surface_min_games", 5)
    sa, sb = tennis._skey(a, surface), tennis._skey(b, surface)
    surf_known = int(engine.games(sa) >= min_s and engine.games(sb) >= min_s)
    return {
        "elo_exp": tennis._blended_prob(engine, p, a, b, surface),
        "overall_gap": engine.get_rating(a) - engine.get_rating(b),
        "surface_gap": (engine.get_rating(sa) - engine.get_rating(sb)) if surf_known else 0.0,
        "surf_known": surf_known,
        "rating_a": engine.get_rating(a), "rating_b": engine.get_rating(b),
        "surf_games_a": engine.games(sa), "surf_games_b": engine.games(sb),
        "games_a": engine.games(a), "games_b": engine.games(b),
        "is_clay": int(surface == "clay"), "is_grass": int(surface == "grass"),
    }


# ------------------------------------------------------------------
# Model registry + gate. A sport's model activates only if it beat Elo and is
# fresh; otherwise (or if absent/disabled) load_model returns None -> Elo.
# ------------------------------------------------------------------

_CACHE: dict[str, dict | None] = {}


def reset_cache():
    """Drop cached models so a freshly-trained/dropped-in model is picked up
    on the next use (call from the ratings hot-reload). Also re-reads the LoL
    sidecar — the refresh rebuilds it on the same cadence."""
    _CACHE.clear()
    reset_lol_sidecar()
    reset_valorant_sidecar()


def load_model(sport: str):
    """Returns a usable model bundle for `sport`, or None to signal 'use Elo'.
    Cached. None whenever XGB is disabled, no model file exists, the model did
    not beat Elo, it's stale, or it fails to load (fail-safe to Elo)."""
    if sport in _CACHE:
        return _CACHE[sport]
    bundle = None
    if getattr(config, "XGB_ENABLED", True) and sport in FEATURES_FOR:
        meta_path = MODELS_DIR / f"{sport}.meta.json"
        model_path = MODELS_DIR / f"{sport}.ubj"
        if meta_path.exists() and model_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                bundle = _gate(sport, meta, model_path)
            except Exception as e:
                log.warning(f"XGB {sport}: could not load model ({e}) — staying on Elo")
    _CACHE[sport] = bundle
    return bundle


def _gate(sport: str, meta: dict, model_path: Path):
    if not meta.get("beats_elo"):
        log.info(f"XGB {sport}: model present but did NOT beat Elo out-of-sample — staying on Elo")
        return None
    try:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(meta["trained_at"])).days
    except (KeyError, ValueError, TypeError):
        age_days = 0
    if age_days > getattr(config, "XGB_STALE_DAYS", 45):
        log.info(f"XGB {sport}: model stale ({age_days}d old) — staying on Elo until retrained")
        return None
    import xgboost as xgb
    bst = xgb.Booster()
    bst.load_model(str(model_path))
    log.info(f"XGB {sport} ACTIVE — beat Elo out-of-sample "
             f"(Brier {meta.get('elo_brier')} -> {meta.get('xgb_brier')}, trained {str(meta.get('trained_at'))[:10]})")
    return {"bst": bst, "features": meta["features"],
            "cal": meta.get("calibration", {"a": 1.0, "b": 0.0}),
            "order": meta.get("order", "home"),
            "best": meta.get("best_iteration")}


def has_model(sport: str) -> bool:
    """Cheap check (does an ACTIVE model exist?) so callers avoid building
    expensive context (e.g. live surface) when XGB is off for a sport."""
    return load_model(sport) is not None


def _apply_platt(p: float, cal: dict) -> float:
    a, b = cal.get("a", 1.0), cal.get("b", 0.0)
    if a == 1.0 and b == 0.0:
        return p
    p = min(max(p, 1e-9), 1 - 1e-9)
    return 1.0 / (1.0 + math.exp(-(a * math.log(p / (1 - p)) + b)))


def predict(sport: str, engine, name_a: str, name_b: str, ctx: dict) -> float | None:
    """P(name_a beats name_b) from the sport's XGBoost model, or None to fall
    back to Elo. Convention matches training: basketball name_a=home (model
    predicts P(home)); tennis is alphabetical (model predicts P(first), flipped
    back to name_a's perspective here)."""
    bundle = load_model(sport)
    if not bundle:
        return None
    import numpy as np
    import xgboost as xgb

    flip = False
    if sport in ("nba", "wnba"):
        feats = basketball_features(engine, sport, name_a, name_b, ctx.get("game_date"))
    elif sport == "mlb":
        feats = mlb_features(engine, name_a, name_b,
                             ctx.get("home_pitcher"), ctx.get("away_pitcher"))
    elif sport == "fwc":
        feats = fwc_features(engine, name_a, name_b)
    elif sport in ("atp", "wta"):
        a, b = sorted((name_a, name_b))
        feats = tennis_features(engine, sport, a, b, ctx.get("surface"))
        flip = a != name_a  # model gives P(alphabetical-first); flip to name_a
    elif sport == "lol":
        # The blend's ENTIRE feature row comes from the OE sidecar (the live
        # Leaguepedia engine is a different rating space — never mix). The
        # caller's names are Leaguepedia-resolved, so re-resolve against the
        # sidecar's own team universe; any gap -> None -> Elo fallback.
        import name_match
        sc = _lol_sidecar()
        if not sc:
            return None
        lineups = sc["team_lineups"]
        ra = name_match.resolve(name_a, lineups.keys())
        rb = name_match.resolve(name_b, lineups.keys())
        if not ra or not rb or ra == rb:
            return None
        a, b = sorted((ra, rb))
        feats = lol_features(sc, a, b, lineups[a], lineups[b])
        flip = a != ra  # model gives P(alphabetical-first); flip to name_a's team
    elif sport == "valorant":
        built = _valorant_live_features(name_a, name_b)
        if not built:
            return None
        feats, flip = built
    elif sport in ESPORTS_TITLES:
        a, b = sorted((name_a, name_b))
        feats = base_features(engine, a, b)
        flip = a != name_a  # model gives P(alphabetical-first); flip to name_a
    else:
        return None

    cols = bundle["features"]
    vec = np.array([[feats[c] for c in cols]], dtype=float)
    dm = xgb.DMatrix(vec, feature_names=cols)
    # Use the same tree count training evaluated on (best_iteration), since
    # save_model doesn't persist it.
    it = bundle.get("best")
    # `is not None`, not truthiness: best_iteration=0 is a real value (a
    # one-tree model) and must still bound the prediction range.
    raw = float((bundle["bst"].predict(dm, iteration_range=(0, it + 1)) if it is not None
                 else bundle["bst"].predict(dm))[0])
    prob = _apply_platt(raw, bundle["cal"])
    return (1.0 - prob) if flip else prob
