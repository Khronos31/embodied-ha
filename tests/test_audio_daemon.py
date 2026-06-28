import importlib.util
import json
import math
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
        self.audio_daemon._non_speech_cache.clear()

    def test_default_audio_log_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_daemon.default_audio_log_path(),
                "/config/embodied-ha/log/audio_log.jsonl",
            )

    def test_default_background_audio_log_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_daemon.default_background_audio_log_path(),
                "/config/embodied-ha/log/background_audio_log.jsonl",
            )

    def test_default_non_speech_audio_events_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_daemon.default_non_speech_audio_events_path(),
                "/config/embodied-ha/log/non_speech_audio_events.jsonl",
            )

    def test_default_audio_wav_dir_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_daemon.default_audio_wav_dir(),
                "/config/embodied-ha/wav",
            )

    def test_default_auditory_events_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            from auditory_context import default_auditory_events_path

            self.assertEqual(
                default_auditory_events_path(),
                "/config/embodied-ha/log/auditory_events.jsonl",
            )

    def _pcm_sine(self, freq_hz: float = 440.0, seconds: float = 1.0, amplitude: float = 0.5) -> bytes:
        samples = int(self.audio_daemon.SAMPLE_RATE * seconds)
        values = bytearray()
        for idx in range(samples):
            value = int(32767 * amplitude * math.sin(2 * math.pi * freq_hz * idx / self.audio_daemon.SAMPLE_RATE))
            values.extend(value.to_bytes(2, byteorder="little", signed=True))
        return bytes(values)

    def test_summarize_chunk_levels_ignores_non_finite_values(self):
        peak_db, mean_db = self.audio_daemon.summarize_chunk_levels(
            [float("-inf"), -33.24, -12.05]
        )
        self.assertEqual(peak_db, -12.1)
        self.assertEqual(mean_db, -22.6)

    def test_build_acoustic_features_estimates_sine_frequency(self):
        features = self.audio_daemon.build_acoustic_features(
            self._pcm_sine(440.0),
            {"peak_db": -12.0, "mean_db": -24.0, "speech_ratio": 0.0},
        )
        self.assertEqual(features["duration_sec"], 1.0)
        self.assertEqual(features["peak_db"], -12.0)
        self.assertEqual(features["dominant_band"], "low")
        self.assertGreater(features["zero_crossing_rate_hz"], 800.0)
        self.assertLess(features["zero_crossing_rate_hz"], 960.0)
        self.assertGreater(features["spectral_centroid_hz"], 400.0)
        self.assertLess(features["spectral_centroid_hz"], 480.0)

    def test_should_transcribe_segment_allows_non_fallback(self):
        allowed, reason = self.audio_daemon.should_transcribe_segment(
            "silero",
            {"speech_ratio": 0.01, "peak_db": -80.0},
        )
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_should_transcribe_segment_rejects_fallback_noise(self):
        allowed, reason = self.audio_daemon.should_transcribe_segment(
            "fallback",
            {"speech_ratio": 0.07, "peak_db": -48.9},
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "fallback_gate_low_speech_ratio")

    def test_should_transcribe_segment_allows_strong_fallback_segment(self):
        allowed, reason = self.audio_daemon.should_transcribe_segment(
            "fallback",
            {"speech_ratio": 0.22, "peak_db": -39.5},
        )
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_should_record_non_speech_event_rejects_weak_empty_transcription(self):
        should_record = self.audio_daemon.should_record_non_speech_event(
            "empty_transcription",
            {
                "duration_sec": 1.28,
                "peak_db": -38.5,
                "speech_ratio": 0.05,
                "high_energy": 0.2,
                "mid_energy": 0.4,
                "transient": True,
            },
        )
        self.assertFalse(should_record)

    def test_non_speech_importance_score_prefers_stronger_events(self):
        weak = self.audio_daemon.non_speech_importance_score(
            "empty_transcription",
            {
                "duration_sec": 1.28,
                "peak_db": -38.5,
                "speech_ratio": 0.05,
                "high_energy": 0.2,
                "mid_energy": 0.4,
                "transient": False,
                "periodic": False,
            },
        )
        strong = self.audio_daemon.non_speech_importance_score(
            "empty_transcription",
            {
                "duration_sec": 4.5,
                "peak_db": -27.0,
                "speech_ratio": 0.35,
                "high_energy": 0.0,
                "mid_energy": 1.0,
                "transient": True,
                "periodic": False,
            },
        )
        self.assertLess(weak, self.audio_daemon.NON_SPEECH_EMPTY_TRANSCRIPTION_THRESHOLD)
        self.assertGreaterEqual(strong, self.audio_daemon.NON_SPEECH_EMPTY_TRANSCRIPTION_THRESHOLD)
        self.assertLess(weak, strong)

    def test_load_enabled_audio_sources_filters_and_normalizes(self):
        prefs = {
            "audio_sources": [
                {"source": "alsa://default", "label": "Desk", "room": "study", "stt_enabled": True, "wake_word_enabled": True},
                {"source": "rtsp://example", "label": "TV", "room": "living", "stt_enabled": False},
            ]
        }
        sources = self.audio_daemon.load_enabled_audio_sources(prefs)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source, "alsa://default")
        self.assertEqual(sources[0].label, "Desk")
        self.assertTrue(sources[0].wake_word_enabled)
        self.assertEqual(sources[0].retention_hours, 60)

    def test_load_enabled_audio_sources_keeps_zero_retention_as_background_by_default(self):
        prefs = {
            "audio_sources": [
                {"source": "alsa://default", "label": "Desk", "room": "study", "stt_enabled": True, "stt_retention_hours": 0},
                {"source": "rtsp://example", "label": "TV", "room": "living", "stt_enabled": True, "stt_retention_hours": 1},
            ]
        }
        sources = self.audio_daemon.load_enabled_audio_sources(prefs)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0].label, "Desk")
        self.assertTrue(sources[0].background_only)
        self.assertEqual(sources[0].retention_hours, 24)
        self.assertEqual(sources[1].label, "TV")
        self.assertFalse(sources[1].background_only)

    def test_load_enabled_audio_sources_skips_zero_retention_when_background_disabled(self):
        prefs = {
            "audio_sources": [
                {
                    "source": "rtsp://example/recorder",
                    "label": "Recorder",
                    "room": "study",
                    "stt_enabled": True,
                    "stt_retention_hours": 0,
                    "background_hearing_enabled": False,
                },
                {
                    "source": "rtsp://example/google-tv",
                    "label": "Google TV",
                    "room": "study",
                    "stt_enabled": True,
                    "stt_retention_hours": 0,
                    "background_hearing_enabled": True,
                },
            ]
        }
        sources = self.audio_daemon.load_enabled_audio_sources(prefs)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].label, "Google TV")
        self.assertTrue(sources[0].background_only)

    def test_load_enabled_audio_sources_parses_tcp_pull_source(self):
        prefs = {
            "audio_sources": [
                {
                    "source": "tcp://192.168.1.31:3333",
                    "label": "Hallway VoiceS3R",
                    "room": "hallway",
                    "sample_rate": 16000,
                    "channels": 1,
                    "format": "s16le",
                    "stt_enabled": True,
                }
            ]
        }
        sources = self.audio_daemon.load_enabled_audio_sources(prefs)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].transport, "tcp_pull")
        self.assertEqual(sources[0].source, "tcp://192.168.1.31:3333")
        self.assertEqual(sources[0].host, "192.168.1.31")
        self.assertEqual(sources[0].port, 3333)
        self.assertEqual(sources[0].room, "hallway")
        self.assertEqual(sources[0].sample_rate, 16000)
        self.assertEqual(sources[0].channels, 1)
        self.assertEqual(sources[0].audio_format, "s16le")

    def test_load_enabled_audio_sources_rejects_invalid_tcp_pull_source(self):
        prefs = {
            "audio_sources": [
                {
                    "source": "tcp://192.168.1.31:3333",
                    "label": "Hallway VoiceS3R",
                    "room": "hallway",
                    "sample_rate": 48000,
                    "channels": 1,
                    "format": "s16le",
                    "stt_enabled": True,
                }
            ]
        }
        sources = self.audio_daemon.load_enabled_audio_sources(prefs)
        self.assertEqual(sources, [])

    def test_load_enabled_audio_sources_rejects_missing_room(self):
        prefs = {
            "audio_sources": [
                {
                    "source": "alsa://default",
                    "label": "Desk",
                    "stt_enabled": True,
                }
            ]
        }
        sources = self.audio_daemon.load_enabled_audio_sources(prefs)
        self.assertEqual(sources, [])

    def test_parse_tcp_port_accepts_valid_range(self):
        self.assertEqual(self.audio_daemon.parse_tcp_port(3333), 3333)
        self.assertEqual(self.audio_daemon.parse_tcp_port("65535"), 65535)
        self.assertIsNone(self.audio_daemon.parse_tcp_port(0))
        self.assertIsNone(self.audio_daemon.parse_tcp_port(65536))

    def test_load_runtime_settings_uses_latest_global_and_source_values(self):
        base_config = self.audio_daemon.AudioSourceConfig("alsa://default", "Desk", 24, False, room="study")
        settings = self.audio_daemon.load_runtime_settings(
            base_config,
            {
                "stt_provider": "stt.home_assistant_cloud",
                "stt_language": "ja-JP",
                "wake_words": ["あかねちゃん"],
                "audio_sources": [
                    {
                        "source": "alsa://default",
                        "label": "Desk",
                        "room": "study",
                        "stt_enabled": True,
                        "stt_retention_hours": 10,
                        "wake_word_enabled": True,
                    }
                ],
            },
        )
        self.assertEqual(settings.provider, "stt.home_assistant_cloud")
        self.assertEqual(settings.language, "ja-JP")
        self.assertEqual(settings.wake_words, ["あかねちゃん"])
        self.assertTrue(settings.stt_enabled)
        self.assertEqual(settings.config.retention_hours, 10)
        self.assertTrue(settings.config.wake_word_enabled)

    def test_load_runtime_settings_detects_disabled_source(self):
        base_config = self.audio_daemon.AudioSourceConfig("alsa://default", "Desk", 24, True, room="study")
        settings = self.audio_daemon.load_runtime_settings(
            base_config,
            {
                "stt_provider": "stt.home_assistant_cloud",
                "audio_sources": [
                    {
                        "source": "alsa://default",
                        "label": "Desk",
                        "room": "study",
                        "stt_enabled": False,
                        "wake_word_enabled": False,
                    }
                ],
            },
        )
        self.assertFalse(settings.stt_enabled)
        self.assertFalse(settings.config.wake_word_enabled)

    def test_load_runtime_settings_uses_zero_retention_as_background_by_default(self):
        base_config = self.audio_daemon.AudioSourceConfig("alsa://default", "Desk", 24, True, room="study")
        settings = self.audio_daemon.load_runtime_settings(
            base_config,
            {
                "stt_provider": "stt.home_assistant_cloud",
                "audio_sources": [
                    {
                        "source": "alsa://default",
                        "label": "Desk",
                        "room": "study",
                        "stt_enabled": True,
                        "stt_retention_hours": 0,
                        "wake_word_enabled": True,
                    }
                ],
            },
        )
        self.assertFalse(settings.stt_enabled)
        self.assertTrue(settings.config.background_only)
        self.assertEqual(settings.config.retention_hours, 24)

    def test_load_runtime_settings_disables_background_when_requested(self):
        base_config = self.audio_daemon.AudioSourceConfig("rtsp://example", "Recorder", 24, False, background_only=True, room="study")
        settings = self.audio_daemon.load_runtime_settings(
            base_config,
            {
                "stt_provider": "stt.home_assistant_cloud",
                "audio_sources": [
                    {
                        "source": "rtsp://example",
                        "label": "Recorder",
                        "room": "study",
                        "stt_enabled": True,
                        "stt_retention_hours": 0,
                        "background_hearing_enabled": False,
                    }
                ],
            },
        )
        self.assertFalse(settings.stt_enabled)
        self.assertFalse(settings.config.background_only)

    def test_append_background_audio_log_prunes_only_matching_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "background_audio_log.jsonl"
            log_path.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-06-20T10:00:00+09:00", "source": "Desk", "kind": "background_audio"}, ensure_ascii=False),
                    json.dumps({"timestamp": "2026-06-20T10:00:00+09:00", "source": "TV", "kind": "background_audio"}, ensure_ascii=False),
                ]) + "\n",
                encoding="utf-8",
            )
            entry = {"timestamp": "2026-06-25T10:00:00+09:00", "source": "Desk", "kind": "background_audio"}
            with mock.patch.dict(os.environ, {"EHA_BACKGROUND_AUDIO_LOG_FILE": str(log_path)}, clear=False), \
                 mock.patch.object(self.audio_daemon, "now", return_value=self.audio_daemon.parse_ts("2026-06-25T10:00:00+09:00")):
                self.audio_daemon.append_background_audio_log(entry, retention_hours=24, source_label="Desk")

            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([row["source"] for row in rows], ["TV", "Desk"])

    def test_maybe_record_background_audio_rate_limits(self):
        config = self.audio_daemon.AudioSourceConfig("rtsp://example", "TV", 24, False, background_only=True)
        entries: list[dict] = []

        def capture(entry, retention_hours=None, source_label=None):
            entries.append(entry)

        with mock.patch.object(self.audio_daemon, "append_background_audio_log", side_effect=capture), \
             mock.patch.object(self.audio_daemon.time, "monotonic", return_value=1000.0):
            first = self.audio_daemon.maybe_record_background_audio(
                config,
                b"\x00\x01" * int(self.audio_daemon.SAMPLE_RATE),
                "fallback",
                {"speech_ratio": 0.2, "peak_db": -31.0, "mean_db": -45.0},
                0.0,
            )
            second = self.audio_daemon.maybe_record_background_audio(
                config,
                b"\x00\x01" * int(self.audio_daemon.SAMPLE_RATE),
                "fallback",
                {"speech_ratio": 0.2, "peak_db": -31.0, "mean_db": -45.0},
                first,
            )

        self.assertEqual(first, 1000.0)
        self.assertEqual(second, first)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "background_audio")
        self.assertEqual(entries[0]["awareness"], "background")
        self.assertIsNone(entries[0]["transcript"])
        self.assertFalse(entries[0]["stt_requested"])

    def test_should_trigger_wake_word_is_prefix_case_insensitive(self):
        self.assertTrue(self.audio_daemon.should_trigger_wake_word("AkAnE, listen", ["akane"]))
        self.assertFalse(self.audio_daemon.should_trigger_wake_word("HELLO AKANE", ["akane"]))  # prefix only
        self.assertFalse(self.audio_daemon.should_trigger_wake_word("こんにちは", ["akane"]))

    def test_update_current_room_from_audio_source_updates_body_location(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs_path = Path(tmpdir) / "preferences.json"
            state_path = Path(tmpdir) / "body_location.json"
            prefs_path.write_text(
                json.dumps({"audio_sources": [{"source": "default", "label": "Desk", "room": "kitchen"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"current_room": "study", "previous_room": "living_room", "last_move_cost": 3}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = self.audio_daemon.AudioSourceConfig("alsa://default", "Desk", 24, True, room="study")
            with mock.patch.dict(os.environ, {"EHA_PREFS_FILE": str(prefs_path), "EHA_BODY_LOCATION_FILE": str(state_path)}, clear=False),                  mock.patch("builtins.print") as print_mock:
                self.audio_daemon.update_current_room_from_audio_source(config)

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["current_room"], "kitchen")
            self.assertEqual(payload["previous_room"], "living_room")
            self.assertEqual(payload["last_move_cost"], 3)
            print_mock.assert_called_once_with("[audio] wake word: current_room → kitchen")

    def test_record_non_speech_audio_event_suppresses_repeated_noise(self):
        config = self.audio_daemon.AudioSourceConfig(
            "rtsp://localhost:8554/capture_tv",
            "TV・レコーダー",
            24,
            False,
            room="study",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "log" / "non_speech_audio_events.jsonl"
            wav_dir = Path(tmpdir) / "wav"
            fixed_now = self.audio_daemon.parse_ts("2026-06-26T10:00:00+09:00")
            with mock.patch.dict(os.environ, {
                "EHA_NON_SPEECH_AUDIO_EVENTS_FILE": str(events_path),
                "EHA_AUDIO_WAV_DIR": str(wav_dir),
            }, clear=False),                  mock.patch.object(self.audio_daemon, "now", return_value=fixed_now),                  mock.patch.object(self.audio_daemon.time, "monotonic", return_value=1000.0):
                first = self.audio_daemon.record_non_speech_audio_event(
                    config,
                    self._pcm_sine(4000.0),
                    "2026-06-26T10:00:00+09:00",
                    "empty_transcription",
                    diagnostics={"peak_db": -20.0, "mean_db": -19.0, "speech_ratio": 0.35, "vad_mode": "fallback"},
                    error="empty transcription",
                )
                second = self.audio_daemon.record_non_speech_audio_event(
                    config,
                    self._pcm_sine(4000.0),
                    "2026-06-26T10:00:00+09:00",
                    "empty_transcription",
                    diagnostics={"peak_db": -20.0, "mean_db": -19.0, "speech_ratio": 0.35, "vad_mode": "fallback"},
                    error="empty transcription",
                )

            self.assertIsNotNone(first)
            self.assertIsNone(second)
            rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], "empty_transcription")

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
        config = self.audio_daemon.AudioSourceConfig("alsa://default", "Desk", 24, False, room="study")
        logged_entries: list[dict] = []

        def capture_entry(entry, retention_hours, source_label):
            logged_entries.append(entry)

        with mock.patch.object(self.audio_daemon, "write_wav"), \
             mock.patch.object(self.audio_daemon, "transcribe_wav", side_effect=RuntimeError("empty transcription")), \
             mock.patch.object(self.audio_daemon, "append_audio_log", side_effect=capture_entry), \
             mock.patch.object(self.audio_daemon, "record_non_speech_audio_event") as non_speech_mock:
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
        non_speech_mock.assert_called_once()
        self.assertEqual(non_speech_mock.call_args.args[3], "empty_transcription")

    def test_process_segment_writes_auditory_event_on_success(self):
        config = self.audio_daemon.AudioSourceConfig("alsa://default", "Desk", 24, True, room="study")
        logged_entries: list[dict] = []
        auditory_events: list[dict] = []

        def capture_log(entry, retention_hours, source_label):
            logged_entries.append(entry)

        def capture_event(entry, retention_hours=None, source_label=None):
            auditory_events.append((entry, retention_hours, source_label))

        with mock.patch.object(self.audio_daemon, "write_wav"), \
             mock.patch.object(self.audio_daemon, "transcribe_wav", return_value="こんにちは。聞こえますか？"), \
             mock.patch.object(self.audio_daemon, "append_audio_log", side_effect=capture_log), \
             mock.patch.object(self.audio_daemon, "append_auditory_event", side_effect=capture_event):
            self.audio_daemon.process_segment(
                config,
                b"\x00\x01" * int(self.audio_daemon.SAMPLE_RATE),
                "stt.home_assistant_cloud",
                "ja-JP",
                "token",
                [],
                diagnostics={
                    "vad_mode": "fallback",
                    "speech_ratio": 0.42,
                    "peak_db": -36.8,
                    "mean_db": -49.7,
                },
            )

        self.assertEqual(len(logged_entries), 1)
        self.assertEqual(logged_entries[0]["text"], "こんにちは。聞こえますか？")
        self.assertEqual(len(auditory_events), 1)
        event, retention_hours, source_label = auditory_events[0]
        self.assertEqual(retention_hours, 24)
        self.assertEqual(source_label, "Desk")
        self.assertEqual(event["modality"], "auditory")
        self.assertEqual(event["transcript"], "こんにちは。聞こえますか？")
        self.assertEqual(event["source"], "Desk")
        self.assertEqual(event["origin"], "alsa://default")
        self.assertEqual(event["speaker_hint"], "user")
        self.assertEqual(event["duration_sec"], 1.0)
        self.assertEqual(event["stt_provider"], "stt.home_assistant_cloud")
        self.assertEqual(event["stt_language"], "ja-JP")
        self.assertEqual(event["vad_mode"], "fallback")
        self.assertEqual(event["speech_ratio"], 0.42)
        self.assertEqual(event["peak_db"], -36.8)
        self.assertEqual(event["mean_db"], -49.7)
        self.assertIsNone(event["confidence"])
        self.assertIsNone(event["raw_audio_ref"])

    def test_process_segment_skips_fallback_noise_before_stt(self):
        config = self.audio_daemon.AudioSourceConfig("alsa://default", "Desk", 24, False, room="study")
        logged_entries: list[dict] = []
        auditory_events: list[dict] = []

        def capture_entry(entry, retention_hours, source_label):
            logged_entries.append(entry)

        def capture_event(entry, retention_hours=None, source_label=None):
            auditory_events.append((entry, retention_hours, source_label))

        with mock.patch.object(self.audio_daemon, "transcribe_wav") as transcribe_mock, \
             mock.patch.object(self.audio_daemon, "append_audio_log", side_effect=capture_entry), \
             mock.patch.object(self.audio_daemon, "append_auditory_event", side_effect=capture_event):
            self.audio_daemon.process_segment(
                config,
                b"\x00\x01" * int(self.audio_daemon.SAMPLE_RATE),
                "stt.google_ai_stt",
                "ja-JP",
                "token",
                [],
                diagnostics={
                    "vad_mode": "fallback",
                    "speech_ratio": 0.05,
                    "peak_db": -48.8,
                    "mean_db": -53.0,
                },
            )

        transcribe_mock.assert_not_called()
        self.assertEqual(len(logged_entries), 1)
        entry = logged_entries[0]
        self.assertTrue(entry["skipped"])
        self.assertEqual(entry["skip_reason"], "fallback_gate_low_speech_ratio")
        self.assertEqual(auditory_events, [])

    def test_process_segment_records_non_speech_for_skipped_strong_audio(self):
        config = self.audio_daemon.AudioSourceConfig("rtsp://example", "TV", 24, False)
        with mock.patch.object(self.audio_daemon, "transcribe_wav") as transcribe_mock, \
             mock.patch.object(self.audio_daemon, "append_audio_log"), \
             mock.patch.object(self.audio_daemon, "record_non_speech_audio_event") as non_speech_mock:
            self.audio_daemon.process_segment(
                config,
                self._pcm_sine(4000.0),
                "stt.home_assistant_cloud",
                "ja-JP",
                "token",
                [],
                diagnostics={
                    "vad_mode": "fallback",
                    "speech_ratio": 0.05,
                    "peak_db": -37.0,
                    "mean_db": -50.0,
                },
            )

        transcribe_mock.assert_not_called()
        non_speech_mock.assert_called_once()
        self.assertEqual(non_speech_mock.call_args.args[3], "fallback_gate_low_speech_ratio")

    def test_record_non_speech_audio_event_writes_event_and_wav_ref(self):
        config = self.audio_daemon.AudioSourceConfig(
            "rtsp://localhost:8554/capture_tv",
            "TV・レコーダー",
            24,
            False,
            room="study",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "log" / "non_speech_audio_events.jsonl"
            wav_dir = Path(tmpdir) / "wav"
            fixed_now = self.audio_daemon.parse_ts("2026-06-26T10:00:00+09:00")
            with mock.patch.dict(os.environ, {
                "EHA_NON_SPEECH_AUDIO_EVENTS_FILE": str(events_path),
                "EHA_AUDIO_WAV_DIR": str(wav_dir),
            }, clear=False), \
                 mock.patch.object(self.audio_daemon, "now", return_value=fixed_now):
                entry = self.audio_daemon.record_non_speech_audio_event(
                    config,
                    self._pcm_sine(4000.0),
                    "2026-06-26T10:00:00+09:00",
                    "empty_transcription",
                    diagnostics={"peak_db": -20.0, "mean_db": -19.0, "speech_ratio": 0.35, "vad_mode": "fallback"},
                    error="empty transcription",
                )

            self.assertIsNotNone(entry)
            rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["kind"], "non_speech_audio_event")
            self.assertEqual(row["source"], "TV・レコーダー")
            self.assertEqual(row["reason"], "empty_transcription")
            self.assertEqual(row["stt_error"], "empty transcription")
            self.assertIsNone(row["transcript"])
            self.assertTrue(row["wav_ref"].endswith(".wav"))
            self.assertTrue(Path(row["wav_ref"]).exists())
            self.assertEqual(row["acoustic_features"]["dominant_band"], "high")
            self.assertEqual(row["situational_context"]["source_room"], "study")

    def test_build_non_speech_situational_context_expands_phase_2b_fields(self):
        config = self.audio_daemon.AudioSourceConfig("rtsp://localhost:8554/capture_tv", "TV・レコーダー", 24, False, room="living_room")
        sensory = {
            "body_room": "study",
            "source_room": "living_room",
            "sensory_origin": "remote",
            "move_cost": 2.0,
        }
        recent_motion = {
            "window_minutes": 20,
            "events": [{"room": "living_room", "label": "リビング人感", "minutes_ago": 2.0, "timestamp": "2026-06-26T09:58:00+09:00"}],
        }
        recent_visual = {
            "scene_id": "scene_1",
            "source": "camera.living_room",
            "room": "living_room",
            "timestamp": "2026-06-26T09:57:00+09:00",
            "objects": ["mug"],
            "people": [],
            "changes": ["ソファの前にマグカップ"],
        }
        related = [{
            "entity_id": "binary_sensor.living_motion",
            "label": "リビング",
            "group": "人感センサー",
            "state": "on",
            "changed_minutes_ago": 2.0,
        }]
        with mock.patch.object(self.audio_daemon, "load_preferences", return_value={}), \
             mock.patch.object(self.audio_daemon, "get_current_ha_states", return_value=[]), \
             mock.patch.object(self.audio_daemon, "build_recent_motion_context", return_value=recent_motion), \
             mock.patch.object(self.audio_daemon, "build_recent_visual_context", return_value=recent_visual), \
             mock.patch.object(self.audio_daemon, "build_related_ha_state_context", return_value=related):
            payload = self.audio_daemon.build_non_speech_situational_context(config, "2026-06-26T10:00:00+09:00", sensory)

        self.assertEqual(payload["body_room"], "study")
        self.assertEqual(payload["source_room"], "living_room")
        self.assertEqual(payload["recent_motion"], recent_motion)
        self.assertEqual(payload["recent_visual_context"], recent_visual)
        self.assertEqual(payload["related_ha_state"], related)
        self.assertIsInstance(payload["location_prior"], dict)
        self.assertEqual(payload["location_prior"]["best_room"], "living_room")
        self.assertTrue(any(item["room"] == "living_room" for item in payload["location_prior"]["candidate_rooms"]))

    def test_format_recent_auditory_prompt_is_voice_specific(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "auditory_events.jsonl"
            rows = [
                {
                    "timestamp": "2026-06-25T09:59:00+09:00",
                    "modality": "auditory",
                    "origin": "alsa://default",
                    "source": "Desk",
                    "speaker_hint": "unknown",
                    "transcript": "別の発話",
                    "duration_sec": 1.1,
                    "peak_db": -30.4,
                    "speech_ratio": 0.31,
                },
                {
                    "timestamp": "2026-06-25T10:00:00+09:00",
                    "modality": "auditory",
                    "origin": "alsa://default",
                    "source": "Desk",
                    "speaker_hint": "user",
                    "transcript": "こんにちは",
                    "duration_sec": 2.4,
                    "peak_db": -36.8,
                    "speech_ratio": 0.42,
                },
            ]
            events_path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"EHA_AUDITORY_EVENTS_FILE": str(events_path)}, clear=False):
                from auditory_context import format_recent_auditory_prompt

                prompt = format_recent_auditory_prompt("こんにちは")

        self.assertIn("# 直近の聴覚入力", prompt)
        self.assertIn("これはテキストチャットではなく、部屋の音声入力からSTTされた発話です。", prompt)
        self.assertIn("時刻: 2026-06-25T10:00:00+09:00", prompt)
        self.assertIn("音源: Desk (alsa://default)", prompt)
        self.assertIn("話者推定: user", prompt)
        self.assertIn("内容: 「こんにちは」", prompt)
        self.assertIn("duration=2.4s, peak=-36.8dB, speech_ratio=0.42", prompt)
        self.assertNotIn("別の発話", prompt)

    def test_append_auditory_event_prunes_only_matching_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "auditory_events.jsonl"
            events_path.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-06-25T08:00:00+09:00", "source": "Desk", "transcript": "old desk"}, ensure_ascii=False),
                    json.dumps({"timestamp": "2026-06-25T08:00:00+09:00", "source": "TV", "transcript": "old tv"}, ensure_ascii=False),
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"EHA_AUDITORY_EVENTS_FILE": str(events_path)}, clear=False), \
                 mock.patch("auditory_context.now", return_value=self.audio_daemon.parse_ts("2026-06-25T10:00:00+09:00")):
                from auditory_context import append_auditory_event

                append_auditory_event(
                    {"timestamp": "2026-06-25T10:00:00+09:00", "source": "Desk", "transcript": "new desk"},
                    retention_hours=1,
                    source_label="Desk",
                )

            rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["transcript"] for row in rows], ["old tv", "new desk"])


if __name__ == "__main__":
    unittest.main()
