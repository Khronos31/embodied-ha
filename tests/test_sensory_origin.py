import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_sensory_origin_module():
    import sys

    path = ROOT / "embodied_ha" / "sensory_origin.py"
    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("sensory_origin_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SensoryOriginTests(unittest.TestCase):
    def setUp(self):
        self.sensory_origin = load_sensory_origin_module()

    def _write_graph(self, tmpdir):
        graph = {
            "rooms": {
                "study": {"display_name": "スタディ", "tags": ["study"]},
                "living_room": {"display_name": "リビング", "tags": ["living"]},
                "kitchen": {"display_name": "台所", "tags": ["kitchen"]},
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

    def _write_location(self, tmpdir, room="study"):
        state_path = Path(tmpdir) / "body_location.json"
        state_path.write_text(json.dumps({"current_room": room}, ensure_ascii=False), encoding="utf-8")
        return state_path

    def test_classifies_same_room_as_direct(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = self._write_location(tmpdir, "study")
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(graph_path), "EHA_BODY_LOCATION_FILE": str(state_path)}, clear=False):
                payload = self.sensory_origin.classify_sensory_origin(label="スタディマイク", modality="auditory")
        self.assertEqual(payload["body_room"], "study")
        self.assertEqual(payload["source_room"], "study")
        self.assertEqual(payload["sensory_origin"], "direct")
        self.assertEqual(payload["move_cost"], 0.0)

    def test_classifies_other_room_as_remote_with_cost(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = self._write_location(tmpdir, "study")
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(graph_path), "EHA_BODY_LOCATION_FILE": str(state_path)}, clear=False):
                payload = self.sensory_origin.classify_sensory_origin(label="リビングカメラ", modality="visual")
        self.assertEqual(payload["body_room"], "study")
        self.assertEqual(payload["source_room"], "living_room")
        self.assertEqual(payload["source_room_label"], "リビング")
        self.assertEqual(payload["sensory_origin"], "remote")
        self.assertEqual(payload["move_cost"], 2.0)
        self.assertEqual(payload["move_path"], ["study", "living_room"])

    def test_unknown_source_room_is_home_assistant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = self._write_graph(tmpdir)
            state_path = self._write_location(tmpdir, "study")
            with mock.patch.dict(os.environ, {"EHA_ROOM_GRAPH_FILE": str(graph_path), "EHA_BODY_LOCATION_FILE": str(state_path)}, clear=False):
                payload = self.sensory_origin.classify_sensory_origin(source="sensor.unknown", label="外部API")
        self.assertIsNone(payload["source_room"])
        self.assertEqual(payload["sensory_origin"], "home_assistant")
        self.assertIsNone(payload["move_cost"])


if __name__ == "__main__":
    unittest.main()
