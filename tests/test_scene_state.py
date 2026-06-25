import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import scene_state  # type: ignore  # noqa: E402


class SceneStateTests(unittest.TestCase):
    def test_ingest_resolve_and_compare_recent_scenes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = scene_state.ingest_scene_parse(
                "camera.living",
                {"preset": "sofa", "direction": "left"},
                [{"id": "obj_mug_1", "label": "青いマグ", "location": "テーブル右", "confidence": 0.7}],
                [],
                [],
                log_dir=tmpdir,
            )
            second = scene_state.ingest_scene_parse(
                "camera.living",
                {"preset": "sofa", "direction": "left"},
                [
                    {"id": "obj_mug_1", "label": "青いマグ", "location": "テーブル右", "confidence": 0.7},
                    {"id": "obj_book_1", "label": "本", "location": "ソファ", "confidence": 0.8},
                ],
                [],
                ["ソファに本が増えている"],
                log_dir=tmpdir,
            )
            self.assertNotEqual(first, second)
            resolved = scene_state.resolve_reference("それ", {"object_id": "obj_mug_1"}, log_dir=tmpdir)
            self.assertEqual(resolved["candidate"]["id"], "obj_mug_1")
            compared = scene_state.compare_recent_scenes("camera.living", log_dir=tmpdir)
            self.assertEqual(compared["status"], "ok")
            self.assertIn("本", " ".join(compared["changes"]))


if __name__ == "__main__":
    unittest.main()
