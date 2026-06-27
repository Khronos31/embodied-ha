import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import antigravity_setup  # noqa: E402


class AgyHarnessTests(unittest.TestCase):
    def test_is_agy_bin_recognizes_executable_name(self):
        self.assertTrue(antigravity_setup.is_agy_bin("agy"))
        self.assertTrue(antigravity_setup.is_agy_bin("/path/to/agy"))
        self.assertFalse(antigravity_setup.is_agy_bin("claude"))

    def test_agy_prompt_text_converts_blocks_and_appends_json_suffix(self):
        prompt = antigravity_setup.agy_prompt_text(
            [
                {"type": "text", "text": "first"},
                {"type": "image", "source": "camera"},
                {"type": "text", "text": "last"},
            ]
        )
        self.assertEqual(prompt, "first\n[カメラ画像]\nlast\nJSON:\n")

    def test_write_mcp_config_returns_empty_string_for_missing_generator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = antigravity_setup.write_mcp_config(Path(tmpdir).as_posix())
            self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
