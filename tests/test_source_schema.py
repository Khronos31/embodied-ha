import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import discover  # type: ignore  # noqa: E402
import migrate_source_schema as migration  # type: ignore  # noqa: E402


class SourceSchemaMigrationTests(unittest.TestCase):
    def _sample_prefs(self):
        return {
            "cameras": [
                {
                    "entity": "camera.living_room_entity_id",
                    "source": "camera.living_room_entity_id",
                    "room": "living",
                    "label": "リビング",
                },
                {
                    "entity": "camera_tv",
                    "source": "capture_tv",
                    "room": "living",
                    "label": "テレビ",
                },
                {
                    "entity": "camera.study_webcam",
                    "source": "camera.study_webcam",
                    "room": "study",
                    "label": "書斎",
                },
            ],
            "audio_sources": [
                {
                    "entity": "mic_alsa",
                    "source": "alsa://default",
                    "room": "study",
                    "label": "書斎マイク",
                    "stt_enabled": True,
                },
                {
                    "entity": "mic_tv",
                    "source": "capture_tv",
                    "room": "living",
                    "label": "テレビ音声",
                    "stt_enabled": False,
                },
                {
                    "entity": "mic_node_hallway",
                    "source": "tcp://192.168.1.5:3333",
                    "room": "hallway",
                    "label": "VoiceS3R",
                    "stt_enabled": True,
                },
                {
                    "entity": "mic_ambiguous",
                    "source": "tv_mic_only",
                    "room": "study",
                    "label": "曖昧",
                    "stt_enabled": True,
                },
            ],
            "presence": {"entity": "input_boolean.resident_home"},
        }

    def test_classify_source_is_shared_between_modules(self):
        self.assertIs(discover.classify_source, migration.classify_source)

    def test_strong_media_overrides_sensor_tokens(self):
        # screenshot/screen は camera.* エンティティ型でもメディア（画面）
        self.assertEqual(
            migration.classify_source("camera", {"entity": "camera.home_pc_screenshot", "source": "camera.home_pc_screenshot"}),
            "video_media",
        )
        # rtsp:// の音声は mic_only を含んでもメディア（キャプチャ箱/AVフィード）
        self.assertEqual(
            migration.classify_source("audio", {"entity": "mic_tv", "source": "rtsp://192.168.1.130:8558/mic_only"}),
            "audio_media",
        )
        # 実マイク（alsa/tcp）はセンサー側のまま
        self.assertEqual(migration.classify_source("audio", {"source": "alsa://default"}), "mics")
        self.assertEqual(migration.classify_source("audio", {"source": "tcp://192.168.1.153:3333"}), "mics")

    def test_build_source_draft_from_preferences_keeps_new_buckets(self):
        draft, warnings = discover.build_source_draft_from_preferences(
            {
                "cameras": [
                    {"source": "camera.living_room_entity_id", "room": "living", "label": "リビング"},
                    {"source": "capture_tv", "room": "living", "label": "テレビ"},
                ],
                "mics": [
                    {"source": "alsa://default", "room": "study", "label": "書斎マイク"},
                    {"source": "tcp://192.168.1.5:3333", "room": "hallway", "label": "VoiceS3R"},
                ],
                "video_media": [
                    {"source": "camera.home_pc_screenshot", "room": "study", "label": "PC画面"},
                ],
                "audio_media": [
                    {"source": "rtsp://192.168.1.130:8558/mic_only", "room": "living", "label": "テレビ音声"},
                ],
            }
        )
        self.assertEqual(len(draft["cameras"]), 2)
        self.assertEqual(len(draft["video_media"]), 1)
        self.assertEqual(len(draft["mics"]), 2)
        self.assertEqual(len(draft["audio_media"]), 1)
        self.assertEqual(warnings, [])

    def test_migration_dry_run_and_apply_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs_path = Path(tmpdir) / "preferences.json"
            prefs_path.write_text(
                json.dumps(self._sample_prefs(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = migration.main([str(prefs_path), "--dry-run"])
            self.assertEqual(code, 0)
            report = stdout.getvalue()
            self.assertIn("video_media (1)", report)
            self.assertIn("audio_media (1)", report)
            self.assertIn("summary: cameras=2, mics=3, video_media=1, audio_media=1", report)
            self.assertIn("[warn]", report)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = migration.main([str(prefs_path), "--apply"])
            self.assertEqual(code, 0)
            applied = json.loads(prefs_path.read_text(encoding="utf-8"))
            self.assertIn("cameras", applied)
            self.assertIn("mics", applied)
            self.assertIn("video_media", applied)
            self.assertIn("audio_media", applied)
            self.assertNotIn("audio_sources", applied)

            backups = list(tmpdir_path for tmpdir_path in Path(tmpdir).glob("preferences.json.*.bak"))
            self.assertEqual(len(backups), 1)

            before = prefs_path.read_text(encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = migration.main([str(prefs_path), "--apply"])
            self.assertEqual(code, 0)
            self.assertEqual(before, prefs_path.read_text(encoding="utf-8"))
            self.assertIn("already migrated", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
