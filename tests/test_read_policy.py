import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import read_policy


class ReadPolicyTests(unittest.TestCase):
    def test_claude_settings_merge_preserves_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.local.json"
            path.write_text(json.dumps({
                "theme": "keep",
                "permissions": {"allow": ["Read(/config/public/**)"], "deny": ["Bash(*)"]},
            }), encoding="utf-8")
            read_policy.merge_claude_settings(str(path))
            settings = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(settings["theme"], "keep")
            self.assertEqual(settings["permissions"]["allow"], ["Read(/config/public/**)"])
            self.assertIn("Bash(*)", settings["permissions"]["deny"])
            for rule in read_policy.CLAUDE_DENY_RULES:
                self.assertIn(rule, settings["permissions"]["deny"])

    def test_invalid_existing_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.local.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                read_policy.merge_claude_settings(str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
