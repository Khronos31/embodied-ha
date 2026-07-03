import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_camera_mcp_module():
    import sys

    path = ROOT / "embodied_ha" / "camera-mcp.py"
    sys.path.insert(0, str(ROOT / "embodied_ha"))
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
                                "ptz": {
                                    "left": "button.example_pan_left",
                                    "right": "button.example_pan_right",
                                    "up": "button.example_tilt_up",
                                    "down": "button.example_tilt_down",
                                },
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

    def test_handle_ptz_uses_camera_specific_button_mapping(self):
        camera_mcp = load_camera_mcp_module()
        camera = {
            "ptz": {
                "left": "button.example_pan_left",
                "right": "button.example_pan_right",
                "up": "button.example_tilt_up",
                "down": "button.example_tilt_down",
            }
        }
        sent = []
        with mock.patch.object(camera_mcp, "press_button", return_value=True) as press_mock, \
             mock.patch.object(camera_mcp, "send", side_effect=sent.append):
            camera_mcp._handle_ptz(camera, "camera.living", "http://supervisor/core/api", "left", 99)
        press_mock.assert_called_once_with("button.example_pan_left", "http://supervisor/core/api")
        self.assertEqual(sent[-1]["result"]["content"][0]["text"], "カメラをleftに向けました")
        self.assertFalse(sent[-1]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
