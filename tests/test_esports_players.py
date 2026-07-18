import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None

import fetch_esports_players
from elo import esports, esports_players


class HistoricalLoaderTests(unittest.TestCase):
    @staticmethod
    def _write_valorant_archive(path: Path, labels=("Alpha", "Wrong Team")):
        def csv_bytes(fields, rows):
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            return output.getvalue()

        teams = [{"Team": "Alpha", "Team ID": "1"},
                 {"Team": "Bravo", "Team ID": "2"}]
        players = [{"Player": f"A{i}", "Player ID": str(10 + i)} for i in range(1, 6)]
        players += [{"Player": f"B{i}", "Player ID": str(20 + i)} for i in range(1, 6)]
        ids, scores, overview = [], [], []
        for match_id, alpha_score, bravo_score in ((200, 5, 13), (100, 13, 5)):
            common = {"Tournament": "VCT", "Stage": "Main", "Match Type": "Round",
                      "Match Name": "Alpha vs Bravo", "Map": f"Map{match_id}"}
            ids.append({**common, "Tournament ID": "1", "Stage ID": "2",
                        "Match ID": str(match_id), "Game ID": str(match_id * 10)})
            scores.append({**common, "Team A": labels[0], "Team A Score": str(alpha_score),
                           "Team B": labels[1], "Team B Score": str(bravo_score)})
            for team, prefix in zip(labels, ("A", "B")):
                for slot in range(1, 6):
                    overview.append({**common, "Player": f"{prefix}{slot}",
                                     "Team": team, "Side": "both"})
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr("vct_2026/ids/teams_ids.csv",
                            csv_bytes(["Team", "Team ID"], teams))
            bundle.writestr("vct_2026/ids/players_ids.csv",
                            csv_bytes(["Player", "Player ID"], players))
            bundle.writestr("vct_2026/ids/tournaments_stages_matches_games_ids.csv",
                            csv_bytes(["Tournament", "Tournament ID", "Stage", "Stage ID",
                                       "Match Type", "Match Name", "Match ID", "Map", "Game ID"],
                                      ids))
            bundle.writestr("vct_2026/matches/maps_scores.csv",
                            csv_bytes(["Tournament", "Stage", "Match Type", "Match Name", "Map",
                                       "Team A", "Team A Score", "Team B", "Team B Score"], scores))
            bundle.writestr("vct_2026/matches/overview.csv",
                            csv_bytes(["Tournament", "Stage", "Match Type", "Match Name", "Map",
                                       "Player", "Team", "Side"], overview))

    @unittest.skipUnless(pa is not None, "pyarrow is required")
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

    @unittest.skipUnless(pa is not None, "pyarrow is required")
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

    def test_valorant_uses_id_anchor_and_match_id_chronology(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "valorant.zip"
            self._write_valorant_archive(path)
            games = esports_players.load_valorant_history(path)
        self.assertEqual(["100:1000", "200:2000"],
                         [game["source_id"] for game in games])
        self.assertEqual("valorant-team:1", games[0]["winner"])
        self.assertEqual("valorant-team:2", games[1]["winner"])
        self.assertTrue(all(len(lineup) == 5 for game in games
                            for lineup in game["teams"].values()))
        audit = esports_players.audit_games(games)
        self.assertIsNone(audit["earliest_date"])
        self.assertEqual((100, 200),
                         (audit["earliest_sequence"], audit["latest_sequence"]))

    def test_valorant_rejects_rows_without_a_team_id_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "valorant.zip"
            self._write_valorant_archive(path, labels=("Wrong A", "Wrong B"))
            self.assertEqual([], esports_players.load_valorant_history(path))


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

    def test_cs2_collector_prunes_unresolved_legacy_rows(self):
        rows = {
            "old": {"date": "2026-01-01", "teams": {"A": ["1", "2", "3"],
                                                       "B": ["4", "5", "6"]},
                    "winner": "A"},
            "new": {"date": "2026-01-02", "teams": {"A": ["cs2:a", "cs2:b", "cs2:c"],
                                                       "B": ["cs2:d", "cs2:e", "cs2:f"]},
                    "winner": "B"},
            "partial": {"date": "2026-01-03", "teams": {"A": ["cs2:a"],
                                                           "B": ["cs2:d"]},
                        "winner": "A"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store_dir = Path(temporary)
            path = store_dir / "esports_cs2_lineups.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            with (patch.object(esports, "STORE_DIR", store_dir),
                  patch.object(esports.history, "_get_json", return_value={"results": []})):
                esports.deepen_cs2_player_data()
            cleaned = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(["new"], list(cleaned))

    def test_audit_reports_largest_gap(self):
        games = [{"date": "2026-01-01", "source": "x"},
                 {"date": "2026-01-04", "source": "x"}]
        self.assertEqual(3, esports_players.audit_games(games)["largest_gap_days"])

    def test_valorant_model_reports_sequence_without_fake_date(self):
        games = [{"date": "vlr-match:000000100", "sequence": 100,
                  "teams": {"valorant-team:1": ["valorant:1", "valorant:2", "valorant:3"],
                            "valorant-team:2": ["valorant:4", "valorant:5", "valorant:6"]},
                  "winner": "valorant-team:1", "source": "valorant-vct-kaggle"}]
        source = {"last_updated": "2026-06-26T06:01:26.74Z", "version": 47}
        with (patch.object(esports_players, "load_games", return_value=games),
              patch.object(esports_players, "valorant_source_metadata", return_value=source)):
            model = esports_players.build_model("valorant", 32.0)
        self.assertEqual("2026-06-26", model["latest_date"])
        self.assertEqual(47, model["source_version"])
        self.assertEqual(100, model["latest_sequence"])

    def test_valorant_model_uses_latest_lineup_and_unique_display_names(self):
        games = []
        for sequence, lineup in ((1, ["valorant:1", "valorant:2", "valorant:3"]),
                                 (2, ["valorant:1", "valorant:2", "valorant:4"])):
            games.append({
                "date": f"vlr-match:{sequence:09d}", "sequence": sequence,
                "teams": {"valorant-team:1": lineup,
                          "valorant-team:2": ["valorant:6", "valorant:7", "valorant:8"]},
                "team_names": {"valorant-team:1": "Shared",
                               "valorant-team:2": "Shared"},
                "winner": "valorant-team:1", "source": "valorant-vct-kaggle",
            })
        with (patch.object(esports_players, "load_games", return_value=games),
              patch.object(esports_players, "valorant_source_metadata",
                           return_value={"last_updated": "2026-06-26", "version": 47})):
            model = esports_players.build_valorant_live_model()
        self.assertEqual(["valorant:1", "valorant:2", "valorant:4"],
                         model["team_lineups"]["valorant-team:1"])
        self.assertEqual({}, model["team_lookup"])

    def test_valorant_model_keeps_unambiguous_historical_alias(self):
        games = []
        for sequence, name in ((1, "Old Alpha"), (2, "Alpha")):
            games.append({
                "date": f"vlr-match:{sequence:09d}", "sequence": sequence,
                "teams": {"valorant-team:1": ["valorant:1", "valorant:2", "valorant:3"],
                          "valorant-team:2": ["valorant:6", "valorant:7", "valorant:8"]},
                "team_names": {"valorant-team:1": name, "valorant-team:2": "Bravo"},
                "winner": "valorant-team:1", "source": "valorant-vct-kaggle",
            })
        with (patch.object(esports_players, "load_games", return_value=games),
              patch.object(esports_players, "valorant_source_metadata",
                           return_value={"last_updated": "2026-06-26", "version": 47})):
            model = esports_players.build_valorant_live_model()
        self.assertEqual("Alpha", model["team_names"]["valorant-team:1"])
        self.assertEqual("valorant-team:1", model["team_lookup"]["Old Alpha"])

    def test_valorant_fetch_downloads_when_unmanifested_archive_is_older(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / fetch_esports_players.DATASETS["valorant"]["archive"]
            archive.write_bytes(b"old")
            metadata = {"last_updated": "2026-07-15T00:00:00Z", "version": 48}
            with (patch.dict(esports_players.DATA_DIRS, {"valorant": directory}),
                  patch.object(fetch_esports_players, "_valorant_metadata",
                               return_value=metadata),
                  patch.object(fetch_esports_players, "_download") as download):
                fetch_esports_players.fetch_valorant(audit=False)
            download.assert_called_once()
            manifest = json.loads((directory / "source.json").read_text(encoding="utf-8"))
            self.assertEqual(48, manifest["version"])


if __name__ == "__main__":
    unittest.main()
