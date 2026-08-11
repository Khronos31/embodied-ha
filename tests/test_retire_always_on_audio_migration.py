import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import migrate_retire_always_on_audio as migration


class RetireAlwaysOnAudioMigrationTests(unittest.TestCase):
    def test_removes_only_retired_audio_settings(self):
        migrated, removed = migration.retire_always_on_audio_preferences(
            {
                "wake_words": ["sample"],
                "wake_ack": {"enabled": True},
                "stt_provider": "stt.example",
                "stt_language": "ja-JP",
                "mics": [
                    {
                        "source": "rtsp://example/study",
                        "room": "study",
                        "stt_enabled": True,
                        "stt_retention_hours": 24,
                        "wake_word_enabled": True,
                        "background_hearing_enabled": True,
                    }
                ],
            }
        )

        self.assertEqual(migrated["stt_provider"], "stt.example")
        self.assertEqual(migrated["stt_language"], "ja-JP")
        self.assertEqual(
            migrated["mics"],
            [{"source": "rtsp://example/study", "room": "study"}],
        )
        self.assertEqual(
            removed,
            [
                "wake_words",
                "wake_ack",
                "mics[0].stt_enabled",
                "mics[0].stt_retention_hours",
                "mics[0].wake_word_enabled",
                "mics[0].background_hearing_enabled",
            ],
        )

    def test_apply_is_atomic_backed_up_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "preferences.json"
            original = {
                "wake_words": ["sample"],
                "stt_provider": "stt.example",
                "mics": [{"source": "rtsp://example", "stt_enabled": True}],
            }
            path.write_text(
                json.dumps(original, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(migration.main([str(path), "--apply"]), 0)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("wake_words", saved)
            self.assertNotIn("stt_enabled", saved["mics"][0])
            self.assertEqual(saved["stt_provider"], "stt.example")

            backups = list(Path(tmpdir).glob("*.always-on-audio.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                json.loads(backups[0].read_text(encoding="utf-8")), original
            )

            before = path.read_bytes()
            self.assertEqual(migration.main([str(path), "--apply"]), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                len(list(Path(tmpdir).glob("*.always-on-audio.bak"))), 1
            )

    def test_rollback_cannot_restart_legacy_listener_without_opt_in(self):
        migrated, _ = migration.retire_always_on_audio_preferences(
            {"mics": [{"source": "rtsp://example", "stt_enabled": True}]}
        )
        enabled = [
            mic
            for mic in migrated["mics"]
            if isinstance(mic, dict) and mic.get("stt_enabled") is True
        ]
        self.assertEqual(enabled, [])


if __name__ == "__main__":
    unittest.main()
