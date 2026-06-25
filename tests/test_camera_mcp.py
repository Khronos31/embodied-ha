import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_camera_mcp_module():
    path = ROOT / "embodied_ha" / "camera-mcp.py"
    spec = importlib.util.spec_from_file_location("camera_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CameraMcpTests(unittest.TestCase):
    def test_camera_context_uses_preferences_metadata(self):
        camera_mcp = load_camera_mcp_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(
                json.dumps(
                    {
                        "cameras": [
                            {
                                "source": "camera.living",
                                "label": "リビング",
                                "room": "living",
                                "preset": "sofa",
                                "direction": "left",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old = os.environ.get("EHA_PREFS_FILE")
            os.environ["EHA_PREFS_FILE"] = str(prefs)
            try:
                context = camera_mcp.camera_context("camera.living")
            finally:
                if old is None:
                    os.environ.pop("EHA_PREFS_FILE", None)
                else:
                    os.environ["EHA_PREFS_FILE"] = old
        self.assertEqual(context["source"], "camera.living")
        self.assertEqual(context["room"], "living")
        self.assertEqual(context["preset"], "sofa")
        self.assertEqual(context["direction"], "left")
        self.assertTrue(context["timestamp"])


if __name__ == "__main__":
    unittest.main()
