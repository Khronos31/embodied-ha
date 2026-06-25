import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_body_mcp_module():
    import sys

    path = ROOT / "embodied_ha" / "body-mcp.py"
    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("body_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BodyMcpTests(unittest.TestCase):
    def setUp(self):
        self.body_mcp = load_body_mcp_module()

    def _json(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["type"], "text")
        return json.loads(result[0]["text"])

    def _write_graph(self, tmpdir):
        graph = {
            "rooms": {
                "study": {"display_name": "スタディ"},
                "living_room": {"display_name": "リビング"},
                "kitchen": {"display_name": "台所"},
            },
            "edges": [
                {"from": "study", "to": "living_room", "cost": 2},
                {"from": "living_room", "to": "kitchen", "cost": 1},
            ],
            "aliases_pending": {
                "living_room": ["居間"],
                "kitchen": ["キッチン"],
            },
            "assumptions": ["test graph"],
        }
        graph_path = Path(tmpdir) / "floorplan_room_graph_draft.json"
        graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
        return graph_path

    def test_defaults_use_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(self.body_mcp.room_graph_path(), "/config/embodied-ha/floorplan_room_graph_draft.json")
            self.assertEqual(self.body_mcp.body_location_path(), "/config/embodied-ha/body_location.json")
            self.assertEqual(self.body_mcp.body_location_log_path(), "/config/embodied-ha/log/body_location_log.jsonl")

    def test_get_location_defaults_to_study(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(graph_path), "EHA_BODY_LOCATION_FILE": str(state_path)}, clear=False):
                payload = self._json(self.body_mcp.get_location({}))
        self.assertEqual(payload["current_room"], "study")
        self.assertEqual(payload["display_name"], "スタディ")
        self.assertIn("available_rooms", payload)

    def test_estimate_move_cost_uses_aliases_and_shortest_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            state_path.write_text(json.dumps({"current_room": "study"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(graph_path), "EHA_BODY_LOCATION_FILE": str(state_path)}, clear=False):
                payload = self._json(self.body_mcp.estimate_move_cost({"to": "キッチン"}))
        self.assertEqual(payload["from"], "study")
        self.assertEqual(payload["to"], "kitchen")
        self.assertEqual(payload["cost"], 3.0)
        self.assertEqual(payload["path"], ["study", "living_room", "kitchen"])

    def test_move_to_persists_state_and_appends_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            log_path = Path(tmpdir) / "body_location_log.jsonl"
            with mock.patch.dict(os.environ, {
                "EHA_ROOM_GRAPH_FILE": str(graph_path),
                "EHA_BODY_LOCATION_FILE": str(state_path),
                "EHA_BODY_LOCATION_LOG_FILE": str(log_path),
            }, clear=False):
                payload = self._json(self.body_mcp.move_to({"room": "居間", "reason": "テレビを見る"}))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(payload["state"]["current_room"], "living_room")
        self.assertEqual(state["current_room"], "living_room")
        self.assertEqual(rows[0]["kind"], "body_move")
        self.assertEqual(rows[0]["reason"], "テレビを見る")

    def test_unknown_room_returns_error_tuple(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(graph_path)}, clear=False):
                content, is_error = self.body_mcp.estimate_move_cost({"to": "屋根裏"})
        self.assertTrue(is_error)
        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["error"], "unknown room")


if __name__ == "__main__":
    unittest.main()
