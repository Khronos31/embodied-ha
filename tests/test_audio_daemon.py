import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_audio_daemon_module():
    path = ROOT / "embodied_ha" / "audio_daemon.py"
    import sys

    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("audio_daemon_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AudioDaemonTests(unittest.TestCase):
    def setUp(self):
        self.audio_daemon = load_audio_daemon_module()

    def test_default_audio_log_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_daemon.default_audio_log_path(),
                "/config/embodied-ha/audio_log.jsonl",
            )

    def test_summarize_chunk_levels_ignores_non_finite_values(self):
        peak_db, mean_db = self.audio_daemon.summarize_chunk_levels(
            [float("-inf"), -33.24, -12.05]
        )
        self.assertEqual(peak_db, -12.1)
        self.assertEqual(mean_db, -22.6)

    def test_load_enabled_audio_sources_filters_and_normalizes(self):
        prefs = {
            "audio_sources": [
                {"source": "alsa", "label": "Desk", "stt_enabled": True, "wake_word_enabled": True},
                {"source": "rtsp://example", "label": "TV", "stt_enabled": False},
            ]
        }
        sources = self.audio_daemon.load_enabled_audio_sources(prefs)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source, "default")
        self.assertEqual(sources[0].label, "Desk")
        self.assertTrue(sources[0].wake_word_enabled)
        self.assertEqual(sources[0].retention_hours, 60)

    def test_should_trigger_wake_word_is_case_insensitive(self):
        self.assertTrue(self.audio_daemon.should_trigger_wake_word("AkAnE, listen", ["akane"]))
        self.assertTrue(self.audio_daemon.should_trigger_wake_word("HELLO AKANE", ["akane"]))
        self.assertFalse(self.audio_daemon.should_trigger_wake_word("こんにちは", ["akane"]))

    def test_append_audio_log_prunes_only_matching_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audio_log.jsonl"
            log_path.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-06-20T10:00:00+09:00", "source": "Desk", "text": "old desk"}, ensure_ascii=False),
                    json.dumps({"timestamp": "2026-06-20T10:00:00+09:00", "source": "TV", "text": "old tv"}, ensure_ascii=False),
                ]) + "\n",
                encoding="utf-8",
            )
            entry = {"timestamp": "2026-06-25T10:00:00+09:00", "source": "Desk", "text": "new desk", "duration_sec": 1.2}
            with mock.patch.dict(os.environ, {"EHA_AUDIO_LOG_FILE": str(log_path)}, clear=False), \
                 mock.patch.object(self.audio_daemon, "now", return_value=self.audio_daemon.parse_ts("2026-06-25T10:00:00+09:00")):
                self.audio_daemon.append_audio_log(entry, retention_hours=24, source_label="Desk")

            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([row["text"] for row in rows], ["old tv", "new desk"])

    def test_process_segment_records_diagnostics_on_empty_transcription(self):
        config = self.audio_daemon.AudioSourceConfig("default", "Desk", 24, False)
        logged_entries: list[dict] = []

        def capture_entry(entry, retention_hours, source_label):
            logged_entries.append(entry)

        with mock.patch.object(self.audio_daemon, "write_wav"), \
             mock.patch.object(self.audio_daemon, "transcribe_wav", side_effect=RuntimeError("empty transcription")), \
             mock.patch.object(self.audio_daemon, "append_audio_log", side_effect=capture_entry):
            self.audio_daemon.process_segment(
                config,
                b"\x00\x01" * int(self.audio_daemon.SAMPLE_RATE),
                "stt.google_ai_stt",
                "ja-JP",
                "token",
                [],
                diagnostics={
                    "vad_mode": "fallback",
                    "speech_ratio": 0.625,
                    "peak_db": -17.4,
                    "mean_db": -34.8,
                },
            )

        self.assertEqual(len(logged_entries), 1)
        entry = logged_entries[0]
        self.assertEqual(entry["error"], "empty transcription")
        self.assertEqual(entry["vad_mode"], "fallback")
        self.assertEqual(entry["speech_ratio"], 0.625)
        self.assertEqual(entry["peak_db"], -17.4)
        self.assertEqual(entry["mean_db"], -34.8)


if __name__ == "__main__":
    unittest.main()
