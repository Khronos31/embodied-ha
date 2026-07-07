import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import observe_context  # noqa: E402


class ObserveContextTests(unittest.TestCase):
    def test_projected_camera_injects_caption_and_image(self):
        prefs = {"cameras": [{"source": "camera.living", "label": "リビング"}]}

        blocks = observe_context.build_projected_camera_blocks(
            "camera.living",
            prefs,
            fetch_frame=lambda *args, **kwargs: b"jpeg-bytes" * 20,
            ha_url="http://ha",
            go2rtc_url="http://go2rtc",
            token="token",
        )

        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("リビング", blocks[0]["text"])
        self.assertIn("camera.living", blocks[0]["text"])
        self.assertEqual(blocks[1]["type"], "image")

    def test_non_projected_camera_injects_nothing(self):
        blocks = observe_context.build_projected_camera_blocks(
            "",
            {"cameras": [{"source": "camera.living", "label": "リビング"}]},
            fetch_frame=lambda *args, **kwargs: b"jpeg-bytes" * 20,
            ha_url="http://ha",
            go2rtc_url="http://go2rtc",
            token="token",
        )

        self.assertEqual(blocks, [])

    def test_unregistered_projected_camera_uses_entity_id_caption(self):
        blocks = observe_context.build_projected_camera_blocks(
            "camera.unknown",
            {"cameras": []},
            fetch_frame=lambda *args, **kwargs: b"jpeg-bytes" * 20,
            ha_url="http://ha",
            go2rtc_url="http://go2rtc",
            token="token",
        )

        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("camera.unknown", blocks[0]["text"])
        self.assertNotIn("（camera.unknown）", blocks[0]["text"])
        self.assertEqual(blocks[1]["type"], "image")


if __name__ == "__main__":
    unittest.main()
