import math
import unittest

import exp_esports_players


class EsportsPlayerExperimentTests(unittest.TestCase):
    @staticmethod
    def _games(count, future_winner="valorant-team:1"):
        games = []
        for index in range(count):
            winner = "valorant-team:1" if index < 10 else future_winner
            games.append({
                "date": f"vlr-match:{index:09d}",
                "sequence": index,
                "teams": {
                    "valorant-team:1": [f"valorant:{player}" for player in range(1, 6)],
                    "valorant-team:2": [f"valorant:{player}" for player in range(6, 11)],
                },
                "winner": winner,
            })
        return games

    def test_future_results_do_not_change_prior_features(self):
        prefix_rows, prefix_outcomes, prefix_order = exp_esports_players.extract(
            self._games(10), "valorant", 32.0)
        full_rows, full_outcomes, full_order = exp_esports_players.extract(
            self._games(20, future_winner="valorant-team:2"), "valorant", 32.0)
        self.assertEqual(prefix_outcomes, full_outcomes[:len(prefix_outcomes)])
        self.assertEqual(prefix_order, full_order[:len(prefix_order)])
        for expected, actual in zip(prefix_rows, full_rows):
            for feature in exp_esports_players.FEATURES:
                if math.isnan(expected[feature]):
                    self.assertTrue(math.isnan(actual[feature]))
                else:
                    self.assertAlmostEqual(expected[feature], actual[feature])


if __name__ == "__main__":
    unittest.main()
