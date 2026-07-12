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
NBA_FEATURES = ["elo_gap", "elo_exp", "rating_home", "rating_away",
                "rest_home", "rest_away", "b2b_home", "b2b_away",
                "games_home", "games_away"]
TENNIS_FEATURES = ["elo_exp", "overall_gap", "surface_gap", "surf_known",
                   "rating_a", "rating_b", "surf_games_a", "surf_games_b",
                   "games_a", "games_b", "is_clay", "is_grass"]

FEATURES_FOR = {"nba": NBA_FEATURES, "wnba": NBA_FEATURES,
                "atp": TENNIS_FEATURES, "wta": TENNIS_FEATURES}


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
    on the next use (call from the ratings hot-reload)."""
    _CACHE.clear()


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
    elif sport in ("atp", "wta"):
        a, b = sorted((name_a, name_b))
        feats = tennis_features(engine, sport, a, b, ctx.get("surface"))
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
