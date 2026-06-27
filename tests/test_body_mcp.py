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

    def _write_prefs(self, tmpdir, projection_targets):
        prefs_path = Path(tmpdir) / "preferences.json"
        prefs_path.write_text(json.dumps({"projection_targets": projection_targets}, ensure_ascii=False), encoding="utf-8")
        return prefs_path

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
        self.assertIsNone(state["projected_room"])
        self.assertEqual(rows[0]["kind"], "body_move")
        self.assertEqual(rows[0]["reason"], "テレビを見る")
        self.assertEqual(rows[0]["action_mode"], "physical_move")
        self.assertEqual(rows[0]["action_cost"], 2.0)

    def test_project_to_keeps_body_room_and_sets_projection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            log_path = Path(tmpdir) / "body_location_log.jsonl"
            with mock.patch.dict(os.environ, {
                "EHA_ROOM_GRAPH_FILE": str(graph_path),
                "EHA_BODY_LOCATION_FILE": str(state_path),
                "EHA_BODY_LOCATION_LOG_FILE": str(log_path),
            }, clear=False):
                payload = self._json(self.body_mcp.project_to({"room": "キッチン", "host": "camera.kitchen", "reason": "様子を見る"}))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(payload["state"]["current_room"], "study")
        self.assertEqual(payload["state"]["projected_room"], "kitchen")
        self.assertEqual(state["current_room"], "study")
        self.assertEqual(state["projected_room"], "kitchen")
        self.assertEqual(state["projected_host"], "camera.kitchen")
        self.assertEqual(rows[0]["kind"], "body_project")
        self.assertEqual(rows[0]["action_mode"], "remote_avatar")
        self.assertEqual(rows[0]["action_cost"], 0.35)
        self.assertEqual(rows[0]["projection_mode"], "enter_remote")
        self.assertEqual(rows[0]["target_host"], "camera.kitchen")

    def test_project_to_is_zero_cost_while_already_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            log_path = Path(tmpdir) / "body_location_log.jsonl"
            state_path.write_text(json.dumps({
                "current_room": "study",
                "projected_room": "kitchen",
                "projected_host": "camera.kitchen"
            }, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "EHA_ROOM_GRAPH_FILE": str(graph_path),
                "EHA_BODY_LOCATION_FILE": str(state_path),
                "EHA_BODY_LOCATION_LOG_FILE": str(log_path),
            }, clear=False):
                payload = self._json(self.body_mcp.project_to({"room": "living_room", "host": "camera.living_room", "reason": "見直す"}))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(payload["state"]["current_room"], "study")
        self.assertEqual(payload["state"]["projected_room"], "living_room")
        self.assertEqual(state["projected_room"], "living_room")
        self.assertEqual(rows[0]["action_cost"], 0.0)
        self.assertEqual(rows[0]["projection_mode"], "remote_move")
        self.assertEqual(rows[0]["cost"], 2.0)

    def test_enter_cyberspace_uses_external_projection_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            prefs_path = self._write_prefs(tmpdir, [{"id": "external://astrolabe", "room": "study"}])
            state_path = Path(tmpdir) / "body_location.json"
            log_path = Path(tmpdir) / "body_location_log.jsonl"
            state_path.write_text(json.dumps({"current_room": "study"}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "EHA_ROOM_GRAPH_FILE": str(graph_path),
                "EHA_PREFS_FILE": str(prefs_path),
                "EHA_BODY_LOCATION_FILE": str(state_path),
                "EHA_BODY_LOCATION_LOG_FILE": str(log_path),
            }, clear=False):
                payload = self._json(self.body_mcp.enter_cyberspace({"entity": "external://astrolabe", "reason": "見守る"}))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(payload["state"]["projected_room"], "study")
        self.assertEqual(state["projected_host"], "external://astrolabe")
        self.assertEqual(rows[0]["kind"], "body_project")
        self.assertEqual(rows[0]["projection_mode"], "enter_remote")
        self.assertEqual(rows[0]["action_cost"], 0.35)

    def test_move_cyber_keeps_projection_when_room_unresolved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            log_path = Path(tmpdir) / "body_location_log.jsonl"
            state_path.write_text(json.dumps({
                "current_room": "study",
                "projected_room": "kitchen",
                "projected_host": "camera.kitchen"
            }, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "EHA_ROOM_GRAPH_FILE": str(graph_path),
                "EHA_BODY_LOCATION_FILE": str(state_path),
                "EHA_BODY_LOCATION_LOG_FILE": str(log_path),
            }, clear=False):
                payload = self._json(self.body_mcp.move_cyber({"entity": "camera.living_room", "reason": "見直す"}))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(payload["state"]["projected_room"], "kitchen")
        self.assertEqual(state["projected_host"], "camera.living_room")
        self.assertEqual(rows[0]["projection_mode"], "remote_move")
        self.assertEqual(rows[0]["action_cost"], 0.0)

    def test_return_to_body_clears_projection(self):
        # 物理体と同室のエンティティに投射中 → 帰還成功
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            log_path = Path(tmpdir) / "body_location_log.jsonl"
            state_path.write_text(json.dumps({
                "current_room": "living_room",
                "projected_room": "living_room",
                "projected_host": "camera.living_room"
            }, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "EHA_ROOM_GRAPH_FILE": str(graph_path),
                "EHA_BODY_LOCATION_FILE": str(state_path),
                "EHA_BODY_LOCATION_LOG_FILE": str(log_path),
            }, clear=False):
                payload = self._json(self.body_mcp.return_to_body({"host": "alsa://living_room", "reason": "戻る"}))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(payload["state"]["current_room"], "living_room")
        self.assertIsNone(payload["state"]["projected_room"])
        self.assertEqual(state["current_room"], "living_room")
        self.assertIsNone(state["projected_room"])
        self.assertEqual(state["projected_host"], "")
        self.assertEqual(rows[0]["kind"], "body_return")
        self.assertEqual(rows[0]["action_mode"], "direct_in_room")

    def test_return_to_body_blocked_from_different_room(self):
        # 物理体と別室のエンティティに投射中 → 帰還不可
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            log_path = Path(tmpdir) / "body_location_log.jsonl"
            state_path.write_text(json.dumps({
                "current_room": "living_room",
                "projected_room": "kitchen",
                "projected_host": "camera.kitchen"
            }, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "EHA_ROOM_GRAPH_FILE": str(graph_path),
                "EHA_BODY_LOCATION_FILE": str(state_path),
                "EHA_BODY_LOCATION_LOG_FILE": str(log_path),
            }, clear=False):
                result, is_error = self.body_mcp.return_to_body({"reason": "帰る"})
        self.assertTrue(is_error)
        payload = json.loads(result[0]["text"])
        self.assertIn("error", payload)
        self.assertEqual(payload["physical_room"], "living_room")
        self.assertEqual(payload["projected_room"], "kitchen")

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
