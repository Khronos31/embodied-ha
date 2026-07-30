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

    def test_soliloquy_merges_voice_private_without_exposing_turn_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            voice_path = Path(tmpdir) / "voice_introspection.jsonl"
            voice_path.write_text(
                json.dumps({
                    "timestamp": "2026-07-30T12:00:00+09:00",
                    "source": "voice",
                    "private": "声の内省",
                    "user": "公開しない入力",
                    "agent": "公開しない返答",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            empty = str(Path(tmpdir) / "missing.jsonl")
            with mock.patch.object(server, "VOICE_INTROSPECTION_LOG", str(voice_path)), \
                 mock.patch.object(server, "CHAT_LOG", empty), \
                 mock.patch.object(server, "OBS_LOG", empty), \
                 mock.patch.object(server, "OBS_RECOVERED_LOG", empty), \
                 mock.patch.object(server, "EXP_LOG", empty):
                messages = server.get_soliloquy_messages()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["source"], "voice")
        self.assertEqual(messages[0]["private"], "声の内省")
        self.assertNotIn("user", messages[0])
        self.assertNotIn("agent", messages[0])

    def test_voice_introspection_log_notifies_soliloquy_room(self):
        watched = dict(server.watched_log_rooms())
        self.assertEqual(watched[server.VOICE_INTROSPECTION_LOG], ["soliloquy"])
        mtimes = {server.VOICE_INTROSPECTION_LOG: None}
        self.assertTrue(
            server.watched_file_changed(mtimes, server.VOICE_INTROSPECTION_LOG, 1.0)
        )
        self.assertFalse(
            server.watched_file_changed(mtimes, server.VOICE_INTROSPECTION_LOG, 1.0)
        )


if __name__ == "__main__":
    unittest.main()
