import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_body_context_module():
    import sys

    path = ROOT / "embodied_ha" / "body-context.py"
    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("body_context_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BodyContextTests(unittest.TestCase):
    def setUp(self):
        self.body_context = load_body_context_module()

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
            "aliases_pending": {"living_room": ["居間"]},
        }
        graph_path = Path(tmpdir) / "floorplan_room_graph_draft.json"
        graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
        return graph_path

    def test_format_body_context_defaults_to_study(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            body_state_path = Path(tmpdir) / "body_state.json"
            body_state_path.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(graph_path), "EHA_BODY_LOCATION_FILE": str(Path(tmpdir) / "missing.json"), "EHA_BODY_STATE_FILE": str(body_state_path)}, clear=False):
                output = self.body_context.format_body_context()
        self.assertIn("# 今いる場所", output)
        self.assertIn("スタディにいる。", output)
        self.assertIn("近くへ移動: リビング(2) / 台所(3)", output)
        self.assertIn("enter_cyberspace", output)
        self.assertIn("move_to", output)
        self.assertNotIn("感覚の扱い", output)

    def test_format_body_context_uses_saved_location_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            body_state_path = Path(tmpdir) / "body_state.json"
            state_path.write_text(json.dumps({"current_room": "居間", "previous_room": "study", "last_move_cost": 2}, ensure_ascii=False), encoding="utf-8")
            body_state_path.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(graph_path), "EHA_BODY_LOCATION_FILE": str(state_path), "EHA_BODY_STATE_FILE": str(body_state_path)}, clear=False):
                output = self.body_context.format_body_context()
        self.assertIn("# 今いる場所", output)
        self.assertIn("リビングにいる。", output)
        self.assertIn("直前の物理移動: スタディ (`study`) から来た", output)
        self.assertIn("直前の物理移動コスト: 2", output)

    def test_format_body_context_shows_projected_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = Path(tmpdir) / "body_location.json"
            body_state_path = Path(tmpdir) / "body_state.json"
            state_path.write_text(json.dumps({
                "current_room": "study",
                "projected_room": "kitchen",
                "current_entity": "camera.kitchen"
            }, ensure_ascii=False), encoding="utf-8")
            body_state_path.write_text(json.dumps({"remote_avatar_host": "camera.kitchen"}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "EHA_ROOM_GRAPH_FILE": str(graph_path),
                "EHA_BODY_LOCATION_FILE": str(state_path),
                "EHA_BODY_STATE_FILE": str(body_state_path),
            }, clear=False):
                output = self.body_context.format_body_context()
        self.assertIn("# 今いる場所", output)
        self.assertIn("台所 の `camera.kitchen` から見ている（電脳体）。身体は スタディ にある。", output)
        self.assertIn("move_cyber", output)
        self.assertIn("return_to_body", output)

    def test_format_body_context_handles_missing_graph(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(Path(tmpdir) / "missing.json")}, clear=False):
                output = self.body_context.format_body_context()
        self.assertIn("部屋グラフが未設定", output)


if __name__ == "__main__":
    unittest.main()
