import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None

from elo import esports, esports_players


@unittest.skipUnless(pa is not None, "pyarrow is required")
class HistoricalLoaderTests(unittest.TestCase):
    def test_dota_keeps_distinct_same_day_maps(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dota.parquet"
            rows = []
            for match_id, winner_id in ((101, 1), (102, 2)):
                row = {
                    "match_id": match_id,
                    "match_start_date_time": datetime(2024, 1, 1, 12, 0),
                    "radiant_team_id": 1, "radiant_team_name": "Alpha",
                    "dire_team_id": 2, "dire_team_name": "Beta", "winner_id": winner_id,
                }
                for side, start in (("radiant", 10), ("dire", 20)):
                    for slot in range(1, 6):
                        row[f"{side}_player_{slot}_id"] = start + slot
                rows.append(row)
            pq.write_table(pa.Table.from_pylist(rows), path)
            games = esports_players.load_dota_history(path)
            self.assertEqual(2, len(games))
            self.assertEqual({"101", "102"}, {game["source_id"] for game in games})

    def test_cs2_maps_rosters_only_when_series_score_proves_assignment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "0.parquet"
            alpha = [f"Alpha{i}" for i in range(5)]
            beta = [f"Beta{i}" for i in range(5)]

            def side(name, players):
                return {"side_start": name,
                        "players": [{"name": player, "steamid": 100 + i}
                                    for i, player in enumerate(players)]}

            rows = [
                {"match_id": "m1", "match_date": datetime(2026, 1, 1),
                 "team1": "Alpha", "team2": "Beta", "score1": 2, "score2": 1,
                 "map_index": 1, "winner_side": "ct",
                 "teams": [side("ct", alpha), side("t", beta)]},
                {"match_id": "m1", "match_date": datetime(2026, 1, 1),
                 "team1": "Alpha", "team2": "Beta", "score1": 2, "score2": 1,
                 "map_index": 2, "winner_side": "ct",
                 "teams": [side("ct", beta), side("t", alpha)]},
                {"match_id": "m1", "match_date": datetime(2026, 1, 1),
                 "team1": "Alpha", "team2": "Beta", "score1": 2, "score2": 1,
                 "map_index": 3, "winner_side": "t",
                 "teams": [side("ct", beta), side("t", alpha)]},
            ]
            pq.write_table(pa.Table.from_pylist(rows), path)
            (directory / "manifest.json").write_text(
                json.dumps({"files": [path.name]}), encoding="utf-8")
            games = esports_players.load_cs2_history(directory / "manifest.json")
            self.assertEqual(["Alpha", "Beta", "Alpha"],
                             [game["winner"] for game in games])
            self.assertEqual({"Alpha", "Beta"}, set(games[0]["teams"]))

            rows[0]["score1"], rows[0]["score2"] = 2, 0
            rows[1]["score1"], rows[1]["score2"] = 2, 0
            rows[2]["score1"], rows[2]["score2"] = 2, 0
            pq.write_table(pa.Table.from_pylist(rows), path)
            self.assertEqual([], esports_players.load_cs2_history(directory / "manifest.json"))


class ForwardLoaderTests(unittest.TestCase):
    def test_completed_dota_store_stops_known_head_walk(self):
        now = int(datetime.now().timestamp())
        page = [{"match_id": 100, "start_time": now, "radiant_name": "A",
                 "dire_name": "B", "radiant_win": True}]
        store = {"100": ["2024-01-01", "A", "B"]}
        with (patch.object(esports.history, "_get_json", return_value=page) as get_json,
              patch.object(esports.time, "sleep")):
            added = esports._fetch_dota2(store)
        self.assertEqual(0, added)
        self.assertEqual(2, get_json.call_count)

    def test_dota_bulk_match_wins_cross_source_dedup(self):
        historical = [{"date": "2026-01-01", "teams": {"A": ["a"], "B": ["b"]},
                       "winner": "A", "source": "dota2-kaggle", "source_id": "123"}]
        forward = [{"date": "2026-01-01", "teams": {"A": ["a"], "B": ["b"]},
                    "winner": "A", "source": "dota2-forward", "source_id": "123"}]
        games = esports_players.merge_games("dota2", historical, forward)
        self.assertEqual(1, len(games))
        self.assertEqual("dota2-kaggle", games[0]["source"])

    def test_cs2_demo_matchup_wins_cross_source_dedup(self):
        historical = [{"date": "2026-01-01T12:00:00", "teams": {"A": ["a"], "B": ["b"]},
                       "winner": "A", "source": "cs2-demo", "source_id": "h:1"}]
        forward = [{"date": "2026-01-01", "teams": {"B": ["b"], "A": ["a"]},
                    "winner": "A", "source": "cs2-forward", "source_id": "f1"}]
        games = esports_players.merge_games("cs2", historical, forward)
        self.assertEqual(1, len(games))
        self.assertEqual("cs2-demo", games[0]["source"])

    def test_cs2_rejects_legacy_numeric_player_ids(self):
        rows = {
            "old": {"date": "2026-01-01", "teams": {"A": ["1", "2", "3"],
                                                       "B": ["4", "5", "6"]},
                    "winner": "A"},
            "new": {"date": "2026-01-02", "teams": {"A": ["cs2:a", "cs2:b", "cs2:c"],
                                                       "B": ["cs2:d", "cs2:e", "cs2:f"]},
                    "winner": "B"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "forward.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            games = esports_players.load_forward_games("cs2", path)
        self.assertEqual(1, len(games))
        self.assertEqual("new", games[0]["source_id"])

    def test_audit_reports_largest_gap(self):
        games = [{"date": "2026-01-01", "source": "x"},
                 {"date": "2026-01-04", "source": "x"}]
        self.assertEqual(3, esports_players.audit_games(games)["largest_gap_days"])


if __name__ == "__main__":
    unittest.main()
