"""Generic, sport-agnostic Elo rating engine.

Each sport adapter (elo/mlb.py, elo/basketball.py, ...) feeds this engine
historical results in chronological order and uses it to produce a win
probability for a future matchup. Nothing sport-specific lives here.
"""

import math

DEFAULT_RATING = 1500.0


class EloEngine:
    def __init__(self, k_factor: float = 20.0, default_rating: float = DEFAULT_RATING):
        self.k_factor = k_factor
        self.default_rating = default_rating
        self.ratings: dict[str, float] = {}
        self.games_played: dict[str, int] = {}
        # Adapter-specific state that must survive the cache round-trip
        # (pitcher ratings, per-team last-played dates, ...). The engine
        # itself never reads this.
        self.extras: dict = {}

    def get_rating(self, name: str) -> float:
        return self.ratings.get(name, self.default_rating)

    def games(self, name: str) -> int:
        return self.games_played.get(name, 0)

    @staticmethod
    def expected_score(rating_a: float, rating_b: float) -> float:
        """Standard Elo expected score for A: 1.0 = certain win, 0.5 = a coin flip."""
        return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))

    def probability(self, name_a: str, name_b: str, adjustment_a: float = 0.0) -> float:
        """Elo expected score for name_a beating name_b, with an optional rating
        bonus (e.g. home advantage) applied to name_a only for this calculation."""
        ra = self.get_rating(name_a) + adjustment_a
        rb = self.get_rating(name_b)
        return self.expected_score(ra, rb)

    def record_result(self, name_a: str, name_b: str, score_a: float,
                      k_override: float | None = None, k_multiplier: float = 1.0):
        """score_a: 1.0 = A won, 0.0 = A lost, 0.5 = draw. k_multiplier lets
        adapters scale the update per game (margin-of-victory weighting)."""
        ra = self.get_rating(name_a)
        rb = self.get_rating(name_b)
        exp_a = self.expected_score(ra, rb)
        k = (self.k_factor if k_override is None else k_override) * k_multiplier
        delta = k * (score_a - exp_a)
        self.ratings[name_a] = ra + delta
        self.ratings[name_b] = rb - delta
        self.games_played[name_a] = self.games_played.get(name_a, 0) + 1
        self.games_played[name_b] = self.games_played.get(name_b, 0) + 1

    def regress_to_mean(self, fraction: float = 1 / 3):
        """Call at season boundaries: pulls every rating partway back toward
        the default so a team's rating from three years ago doesn't carry
        full weight into a new season with a different roster."""
        for name in list(self.ratings):
            self.ratings[name] += (self.default_rating - self.ratings[name]) * fraction

    def to_dict(self) -> dict:
        return {
            "k_factor": self.k_factor,
            "default_rating": self.default_rating,
            "ratings": self.ratings,
            "games_played": self.games_played,
            "extras": self.extras,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EloEngine":
        eng = cls(
            k_factor=d.get("k_factor", 20.0),
            default_rating=d.get("default_rating", DEFAULT_RATING),
        )
        eng.ratings = dict(d.get("ratings", {}))
        eng.games_played = dict(d.get("games_played", {}))
        eng.extras = dict(d.get("extras", {}))
        return eng


def mov_multiplier(margin: float, elo_diff_winner: float) -> float:
    """FiveThirtyEight-style margin-of-victory K multiplier: blowouts move
    ratings more than squeakers, damped when the winner was already heavily
    favored (the autocorrelation correction — good teams win big often, and
    without the damping their ratings would inflate). margin 0 (a draw)
    returns 1.0 so draw results still update normally."""
    if margin <= 0:
        return 1.0
    return math.log(margin + 1) * (2.2 / (elo_diff_winner * 0.001 + 2.2))


def decompose_win_draw_loss(p: float, base_draw_rate: float) -> tuple[float, float, float]:
    """Split an Elo expected-score p (which, by construction of training on
    win=1/draw=0.5/loss=0, already equals P(win) + 0.5*P(draw)) into separate
    P(win)/P(draw)/P(loss) for sports where draws are a real outcome (soccer).

    Draw probability peaks at base_draw_rate when the two sides are evenly
    matched (p=0.5) and shrinks toward 0 as the gap widens (4*p*(1-p) is the
    simplest curve with that shape, 0 at p=0/1, 1 at p=0.5). Clamped so
    neither win probability goes negative for lopsided matchups.
    """
    draw = base_draw_rate * 4 * p * (1 - p)
    draw = min(draw, 2 * min(p, 1 - p))
    win_a = p - draw / 2
    win_b = (1 - p) - draw / 2
    return win_a, draw, win_b
