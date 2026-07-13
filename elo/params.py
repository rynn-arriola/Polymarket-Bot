"""Per-sport model hyperparameters.

Defaults here are hand-set from domain knowledge / published Elo literature.
tune.py grid-searches them against the walk-forward backtest's Brier score
and writes winners to model_params.json, which — when present — overrides
these defaults. Adapters always go through get() so tuned values take
effect everywhere (build, backtest, live bot) without editing code.

"calibration" is a per-sport Platt-scaling correction {a, b} fitted by
tune.py on walk-forward predictions: p' = sigmoid(a * logit(p) + b).
Identity (a=1, b=0) until fitted. Applied by apply_calibration(), which
divergence_bot.py calls on every model probability before comparing it to
the market price.
"""

import json
import math
from pathlib import Path

PARAMS_FILE = Path(__file__).resolve().parent.parent / "model_params.json"

DEFAULTS: dict[str, dict] = {
    "mlb": {
        "k": 20.0,
        "home_adv": 28.0,        # Elo pts; from MLB's long-run ~54% home win rate
        "mov": True,             # margin-of-victory K scaling
        "pitcher_k": 12.0,       # how fast a starter's own rating moves per start
        "pitcher_seed_scale": 0.0,  # Elo pts per ERA point vs league avg, seeding from PRIOR season (0 = off)
        "pitcher_weight": 0.5,   # how much of the starters' rating gap adjusts the team gap
        "min_games": 10,
        "calibration": {"a": 1.0, "b": 0.0},
    },
    "nba": {
        "k": 20.0,
        "home_adv": 65.0,
        "mov": True,
        "b2b_penalty": 35.0,     # Elo pts docked when a team played yesterday
        "min_games": 10,
        "calibration": {"a": 1.0, "b": 0.0},
    },
    "wnba": {
        "k": 20.0,
        "home_adv": 55.0,
        "mov": True,
        "b2b_penalty": 35.0,
        "min_games": 10,
        "calibration": {"a": 1.0, "b": 0.0},
    },
    "fwc": {
        "k": 30.0,
        "mov": True,
        "draw_rate": 0.24,       # long-run share of international matches drawn
        "min_games": 3,
        "calibration": {"a": 1.0, "b": 0.0},
    },
    "atp": {
        "k": 32.0,
        "surface_weight": 0.3,   # blend: (1-w)*overall + w*surface-specific rating
        "surface_min_games": 5,  # below this many matches on the surface, use overall only
        "min_games": 10,
        "calibration": {"a": 1.0, "b": 0.0},
    },
    "wta": {
        "k": 32.0,
        "surface_weight": 0.3,
        "surface_min_games": 5,
        "min_games": 10,
        "calibration": {"a": 1.0, "b": 0.0},
    },
    "itf": {
        "k": 32.0,
        "surface_weight": 0.0,   # CSV fallback data may lack surface info
        "surface_min_games": 5,
        "min_games": 10,
        "calibration": {"a": 1.0, "b": 0.0},
    },
    # Esports: higher K than traditional sports — rosters and game metas
    # shift fast, so recent results should dominate. min_games modest since
    # several sources only expose a sliding recent window until the
    # accumulating store deepens.
    # inactivity_days/regress: one-shot regression toward the mean when a
    # team returns from a long idle spell (roster/meta have usually moved
    # on; the stale rating otherwise reads as a fat fake divergence live).
    # dota2/cs2 only — measured win there (2026-07-13: dormant-game Brier
    # 0.2257->0.2213 / 0.2288->0.2222); no benefit measured for valorant
    # (n=69, noise) and LoL's 12-month window barely has dormancy.
    "dota2": {"k": 40.0, "min_games": 8, "inactivity_days": 90, "inactivity_regress": 0.35,
              "calibration": {"a": 1.0, "b": 0.0}},
    "cs2": {"k": 40.0, "min_games": 8, "inactivity_days": 90, "inactivity_regress": 0.35,
            "calibration": {"a": 1.0, "b": 0.0}},
    "lol": {"k": 40.0, "min_games": 8, "calibration": {"a": 1.0, "b": 0.0}},
    "valorant": {"k": 40.0, "min_games": 8, "calibration": {"a": 1.0, "b": 0.0}},
}

_tuned_cache: dict | None = None


def _tuned() -> dict:
    global _tuned_cache
    if _tuned_cache is None:
        try:
            with open(PARAMS_FILE, encoding="utf-8") as f:
                _tuned_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _tuned_cache = {}
    return _tuned_cache


def get(sport: str) -> dict:
    """Defaults overridden by any tuned values from model_params.json."""
    merged = dict(DEFAULTS.get(sport, {}))
    merged.update(_tuned().get(sport, {}))
    return merged


def save(all_params: dict):
    """Write tuned params (tune.py only). Merges over the existing file so
    tuning one sport doesn't wipe another's results."""
    global _tuned_cache
    existing = _tuned()
    existing.update(all_params)
    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    _tuned_cache = existing


def apply_calibration(p: float, sport: str) -> float:
    """Platt scaling: p' = sigmoid(a*logit(p) + b). Identity until tuned."""
    cal = get(sport).get("calibration") or {}
    a, b = cal.get("a", 1.0), cal.get("b", 0.0)
    if a == 1.0 and b == 0.0:
        return p
    p = min(max(p, 1e-9), 1 - 1e-9)
    z = a * math.log(p / (1 - p)) + b
    return 1.0 / (1.0 + math.exp(-z))
