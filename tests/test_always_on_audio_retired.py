import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAEMON_PATH = ROOT / "embodied_ha" / "daemon.py"
DOCKERFILE_PATH = ROOT / "embodied_ha" / "Dockerfile"


class AlwaysOnAudioRetiredTests(unittest.TestCase):
    def test_daemon_does_not_spawn_removed_audio_process(self):
        source = DAEMON_PATH.read_text(encoding="utf-8")
        self.assertNotIn("audio_daemon.py", source)
        self.assertNotIn("audio_daemon_watchdog", source)
        self.assertNotIn("_AUDIO_FAILURE_NOTIFICATION_ID", source)

    def test_active_hearing_cleanup_has_a_persistent_owner(self):
        source = DAEMON_PATH.read_text(encoding="utf-8")
        self.assertIn("import concentrate_hearing_files", source)
        self.assertIn("target=concentrate_hearing_files.cleanup_forever", source)
        self.assertIn('name="concentrate-hearing-cleanup"', source)

    def test_removed_vad_dependency_is_not_installed(self):
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("pysilero-vad", dockerfile)
        self.assertNotIn("SileroVoiceActivityDetector", dockerfile)


if __name__ == "__main__":
    unittest.main()
