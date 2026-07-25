"""chat_log.jsonlのagent移行とWeb API互換性。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))
sys.path.insert(0, str(EHA_DIR / "web"))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import server  # noqa: E402


class ChatLogCompatibilityTests(unittest.TestCase):
    def test_web_api_normalizes_old_records_without_adding_legacy_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "chat_log.jsonl"
            rows = [
                {"timestamp": "old", "claude": "旧形式"},
                {"timestamp": "new", "agent": "新形式"},
            ]
            path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            with mock.patch.object(server, "CHAT_LOG", str(path)):
                messages = server.get_chat_messages()

        self.assertEqual(messages[0]["agent"], "旧形式")
        self.assertEqual(messages[0]["claude"], "旧形式")
        self.assertEqual(messages[1]["agent"], "新形式")
        self.assertNotIn("claude", messages[1])


if __name__ == "__main__":
    unittest.main()
