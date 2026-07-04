import importlib.util
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_media_capture_module():
    path = ROOT / "embodied_ha" / "media_capture.py"
    import sys

    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("media_capture_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MediaCaptureTests(unittest.TestCase):
    def setUp(self):
        self.media_capture = load_media_capture_module()

    def test_fetch_frame_uses_camera_proxy_for_ha_entities(self):
        with mock.patch.object(self.media_capture.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"x" * 120)) as run_mock:
            data = self.media_capture.fetch_frame(
                "camera.kitchen",
                ha_url="http://supervisor/core/api",
                go2rtc_url="http://homeassistant.local:1984",
                token="token123",
            )
        self.assertEqual(data, b"x" * 120)
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[:4], ["curl", "-sf", "--max-time", "8"])
        self.assertIn("-H", cmd)
        self.assertIn("Authorization: Bearer token123", cmd)
        self.assertIn("http://supervisor/core/api/camera_proxy/camera.kitchen", cmd)

    def test_fetch_frame_uses_go2rtc_for_stream_names(self):
        with mock.patch.object(self.media_capture.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"x" * 120)) as run_mock:
            data = self.media_capture.fetch_frame(
                "capture_tv",
                ha_url="http://supervisor/core/api",
                go2rtc_url="http://homeassistant.local:1984",
                token="token123",
            )
        self.assertEqual(data, b"x" * 120)
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd, ["curl", "-sf", "--max-time", "8", "http://homeassistant.local:1984/api/frame.jpeg?src=capture_tv"])


if __name__ == "__main__":
    unittest.main()
