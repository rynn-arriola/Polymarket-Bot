import json
import unittest
from datetime import date, timedelta
from unittest.mock import mock_open, patch

import xgb_live


def sidecar(latest_date=None):
    return {
        "ratings": {f"valorant:{player}": 1500.0 + player
                    for player in range(1, 11)},
        "played": {f"valorant:{player}": 10 for player in range(1, 11)},
        "team_elo": {"valorant-team:1": 1550.0, "valorant-team:2": 1450.0},
        "team_games": {"valorant-team:1": 20, "valorant-team:2": 18},
        "team_lineups": {
            "valorant-team:1": [f"valorant:{player}" for player in range(1, 6)],
            "valorant-team:2": [f"valorant:{player}" for player in range(6, 11)],
        },
        "team_lookup": {"Alpha Gaming": "valorant-team:1",
                        "Bravo": "valorant-team:2"},
        "latest_date": latest_date or date.today().isoformat(),
    }


class ValorantLiveTests(unittest.TestCase):
    def tearDown(self):
        xgb_live.reset_valorant_sidecar()

    def test_stale_sidecar_falls_back(self):
        stale = sidecar((date.today() - timedelta(days=46)).isoformat())
        with patch("builtins.open", mock_open(read_data=json.dumps(stale))):
            self.assertIsNone(xgb_live._valorant_sidecar())

    def test_live_names_resolve_to_stable_ids(self):
        xgb_live._VALORANT_SIDECAR = sidecar()
        xgb_live._VALORANT_SIDECAR_LOADED = True
        built = xgb_live._valorant_live_features("Alpha", "Bravo")
        self.assertIsNotNone(built)
        features, flipped = built
        self.assertFalse(flipped)
        self.assertGreater(features["elo_exp"], 0.5)
        self.assertFalse(features["p_exp"] != features["p_exp"])

    def test_unknown_team_falls_back(self):
        xgb_live._VALORANT_SIDECAR = sidecar()
        xgb_live._VALORANT_SIDECAR_LOADED = True
        self.assertIsNone(xgb_live._valorant_live_features("Unknown", "Bravo"))


if __name__ == "__main__":
    unittest.main()
