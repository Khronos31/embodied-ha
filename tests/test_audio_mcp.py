import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_audio_mcp_module():
    path = ROOT / "embodied_ha" / "audio-mcp.py"
    import sys

    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("audio_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AudioMcpTests(unittest.TestCase):
    def setUp(self):
        self._body_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._body_tmp.cleanup)
        body_path = Path(self._body_tmp.name) / "body_location.json"
        body_path.write_text(json.dumps({"current_entity": "", "current_room": ""}), encoding="utf-8")
        env_patch = mock.patch.dict(os.environ, {"EHA_BODY_LOCATION_FILE": str(body_path)}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.audio_mcp = load_audio_mcp_module()

    def _json(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["type"], "text")
        return json.loads(result[0]["text"])

    def test_parse_volumedetect(self):
        peak, mean = self.audio_mcp.parse_volumedetect(
            "[Parsed_volumedetect_0] mean_volume: -28.1 dB\n"
            "[Parsed_volumedetect_0] max_volume: -12.3 dB\n"
        )
        self.assertEqual(peak, -12.3)
        self.assertEqual(mean, -28.1)

    def test_build_record_command_go2rtc(self):
        cmd = self.audio_mcp.build_record_command("rtsp://localhost:8554/capture_tv", 5)
        self.assertEqual(cmd[:4], ["ffmpeg", "-rtsp_transport", "tcp", "-i"])
        self.assertIn("rtsp://localhost:8554/capture_tv", cmd)

    def test_build_record_command_alsa(self):
        cmd = self.audio_mcp.build_record_command("alsa://default", 7)
        self.assertEqual(cmd[:5], ["ffmpeg", "-f", "alsa", "-i", "default"])
        self.assertIn("7", cmd)

    def test_build_record_command_rejects_tcp(self):
        with self.assertRaises(ValueError):
            self.audio_mcp.build_record_command("tcp://192.168.1.100:3333", 5)

    def test_speaker_file_path_contract_names_raw_format_and_conversion(self):
        for tool in (
            self.audio_mcp.TOOL_SPEAK,
            self.audio_mcp.TOOL_USE_DEVICE_SPEAKER,
        ):
            description = tool["inputSchema"]["properties"]["file_path"]["description"]
            self.assertIn(".pcm", description)
            self.assertIn("mono s16le/16kHz", description)
            self.assertIn("ffmpeg", description)
            self.assertIn("最長10分", description)

    def test_concentrate_hearing_spec_requires_view_file_without_execution_tools(self):
        description = self.audio_mcp.TOOL_CONCENTRATE_HEARING["description"]
        self.assertIn("view_file", description)
        self.assertIn("デフォルト5秒、最大30秒", description)
        self.assertNotIn("WebM", description)
        self.assertNotIn("Antigravity個体専用", description)
        duration_spec = self.audio_mcp.TOOL_CONCENTRATE_HEARING["inputSchema"]["properties"]["duration"]
        self.assertEqual(duration_spec["type"], "integer")
        self.assertEqual(duration_spec["minimum"], 1)
        self.assertEqual(duration_spec["maximum"], 30)
        self.assertEqual(duration_spec["default"], 5)
        self.assertIn("デフォルト 5、最大 30", duration_spec["description"])
        for forbidden in ("command", "shell", "terminal", "Python", "外部スクリプト", "外部STT"):
            self.assertIn(forbidden, description)

    def test_concentrate_hearing_is_registered_only_for_antigravity(self):
        with mock.patch.dict(os.environ, {"EHA_AGENT_HARNESS": "claude"}, clear=False):
            self.assertNotIn("concentrate_hearing", self.audio_mcp._audio_tools())
        with mock.patch.dict(os.environ, {"EHA_AGENT_HARNESS": "codex"}, clear=False):
            self.assertNotIn("concentrate_hearing", self.audio_mcp._audio_tools())
        with mock.patch.dict(os.environ, {"EHA_AGENT_HARNESS": "agy"}, clear=False):
            self.assertIn("concentrate_hearing", self.audio_mcp._audio_tools())

    def test_concentrate_hearing_rejects_non_antigravity_before_recording(self):
        with mock.patch.dict(os.environ, {"EHA_AGENT_HARNESS": "claude"}, clear=False), \
             mock.patch.object(self.audio_mcp, "_record_concentrate_hearing_webm") as record_mock:
            content, is_error = self.audio_mcp.concentrate_hearing({})
        self.assertTrue(is_error)
        self.assertIn("現在のハーネス", content[0]["text"])
        self.assertNotIn("Antigravity個体専用", content[0]["text"])
        record_mock.assert_not_called()

    def test_record_concentrate_hearing_converts_to_audio_only_webm_and_keeps_result(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "libopus" in command:
                Path(command[-1]).write_bytes(b"webm-opus")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(self.audio_mcp, "CONCENTRATE_HEARING_DIR", Path(tmpdir)), \
             mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(self.audio_mcp.subprocess, "run", side_effect=fake_run):
            result_path = self.audio_mcp._record_concentrate_hearing_webm(
                "rtsp://localhost:8554/room",
                5,
            )
            result = Path(result_path)
            self.assertTrue(result.exists())
            self.assertEqual(result.read_bytes(), b"webm-opus")
            self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
            self.assertFalse(any(Path(tmpdir).glob("*.wav")))
            self.assertIn("-vn", commands[1])
            self.assertEqual(commands[1][commands[1].index("-c:a") + 1], "libopus")
            self.audio_mcp._cleanup_created_concentrate_hearing_files()
            self.assertFalse(result.exists())

    def test_concentrate_hearing_returns_webm_path_then_enforces_cooldown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            prefs = tmp / "preferences.json"
            state = tmp / "concentrate-state.json"
            webm = tmp / "heard.webm"
            prefs.write_text(
                json.dumps({"mics": [{"source": "rtsp://localhost:8554/room", "label": "Room"}]}),
                encoding="utf-8",
            )
            webm.write_bytes(b"webm")
            fake_body_state = mock.Mock(
                update_body_state=lambda updater: updater({}),
                on_audio_session=lambda current: current,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_CONCENTRATE_HEARING_STATE_FILE": str(state),
                    "EHA_CONCENTRATE_HEARING_COOLDOWN_SECONDS": "60",
                },
                clear=False,
            ), mock.patch.object(
                self.audio_mcp,
                "_record_concentrate_hearing_webm",
                return_value=str(webm),
            ) as record_mock, mock.patch.object(
                self.audio_mcp,
                "record_active_listen",
            ) as log_mock, mock.patch.dict(
                "sys.modules",
                {"body_state": fake_body_state},
                clear=False,
            ):
                payload = self._json(self.audio_mcp.concentrate_hearing({"duration": 17}))
                second_content, second_is_error = self.audio_mcp.concentrate_hearing({})

            self.assertEqual(payload["file_path"], str(webm))
            self.assertEqual(payload["media_type"], "audio/webm")
            self.assertEqual(payload["audio_codec"], "opus")
            self.assertEqual(payload["duration_sec"], 17)
            self.assertIs(payload["available_for_current_turn"], True)
            self.assertIn("view_file", payload["instruction"])
            self.assertNotIn("transcript", payload)
            self.assertTrue(second_is_error)
            self.assertIn("クールダウン中", second_content[0]["text"])
            record_mock.assert_called_once_with("rtsp://localhost:8554/room", 17)
            log_mock.assert_called_once()
            self.assertEqual(log_mock.call_args.args[0]["duration"], 17)
            self.assertEqual(log_mock.call_args.args[0]["duration_sec"], 17)

    def test_concentrate_hearing_rejects_invalid_duration_before_recording(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(
                json.dumps({"mics": [{"source": "rtsp://localhost:8554/room"}]}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"EHA_AGENT_HARNESS": "agy", "EHA_PREFS_FILE": str(prefs)},
                clear=False,
            ), mock.patch.object(
                self.audio_mcp,
                "_record_concentrate_hearing_webm",
            ) as record_mock:
                for invalid in (0, -1, 31, "3", True):
                    with self.subTest(duration=invalid):
                        content, is_error = self.audio_mcp.concentrate_hearing({"duration": invalid})
                        self.assertTrue(is_error)
                        self.assertIn("duration は1〜30の整数", content[0]["text"])
            record_mock.assert_not_called()

    def test_failed_concentrate_hearing_does_not_start_cooldown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            prefs = tmp / "preferences.json"
            state = tmp / "concentrate-state.json"
            prefs.write_text(
                json.dumps({"mics": [{"source": "rtsp://localhost:8554/room"}]}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_CONCENTRATE_HEARING_STATE_FILE": str(state),
                },
                clear=False,
            ), mock.patch.object(
                self.audio_mcp,
                "_record_concentrate_hearing_webm",
                side_effect=RuntimeError("capture failed"),
            ) as record_mock:
                first_content, first_is_error = self.audio_mcp.concentrate_hearing({})
                second_content, second_is_error = self.audio_mcp.concentrate_hearing({})

            self.assertTrue(first_is_error)
            self.assertTrue(second_is_error)
            self.assertIn("capture failed", first_content[0]["text"])
            self.assertIn("capture failed", second_content[0]["text"])
            self.assertEqual(record_mock.call_count, 2)
            record_mock.assert_has_calls([
                mock.call("rtsp://localhost:8554/room", 5),
                mock.call("rtsp://localhost:8554/room", 5),
            ])
            self.assertFalse(state.exists())

    def test_concentrate_hearing_rejects_cyber_body(self):
        with mock.patch.dict(os.environ, {"EHA_AGENT_HARNESS": "agy"}, clear=False), \
             mock.patch.object(self.audio_mcp, "_current_body_state", return_value=({}, "camera.room", "room", "room")), \
             mock.patch.object(self.audio_mcp, "_record_concentrate_hearing_webm") as record_mock:
            content, is_error = self.audio_mcp.concentrate_hearing({})
        self.assertTrue(is_error)
        self.assertIn("電脳体モード", content[0]["text"])
        record_mock.assert_not_called()

    def test_prune_concentrate_hearing_removes_only_expired_webm(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(self.audio_mcp, "CONCENTRATE_HEARING_DIR", Path(tmpdir)):
            stale = Path(tmpdir) / "eha-concentrate-hearing-stale.webm"
            fresh = Path(tmpdir) / "eha-concentrate-hearing-fresh.webm"
            unrelated = Path(tmpdir) / "keep.webm"
            for path in (stale, fresh, unrelated):
                path.write_bytes(b"x")
            os.utime(stale, (100, 100))
            os.utime(fresh, (1000, 1000))
            self.audio_mcp._prune_stale_concentrate_hearing_files(
                1000 + self.audio_mcp.CONCENTRATE_HEARING_FILE_TTL_SECONDS - 1
            )
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_default_audio_log_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_mcp.default_audio_log_path(),
                "/config/embodied-ha/log/audio_log.jsonl",
            )


    def test_default_active_listen_log_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_mcp.default_active_listen_log_path(),
                "/config/embodied-ha/log/active_listen_log.jsonl",
            )

    def test_default_auditory_events_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_mcp.default_auditory_events_path(),
                "/config/embodied-ha/log/auditory_events.jsonl",
            )

    def test_default_non_speech_audio_events_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_mcp.default_non_speech_audio_events_path(),
                "/config/embodied-ha/log/non_speech_audio_events.jsonl",
            )

    def test_default_audio_event_tags_path_prefers_eha_data_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha"}, clear=False):
            self.assertEqual(
                self.audio_mcp.default_audio_event_tags_path(),
                "/config/embodied-ha/log/audio_event_tags.jsonl",
            )

    def test_listen_defaults_to_first_configured_audio_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(
                json.dumps({"mics": [{"source": "rtsp://example.local/tv", "label": "TV"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"EHA_PREFS_FILE": str(prefs)}, clear=False):
                self.assertEqual(self.audio_mcp.default_listen_source(), "rtsp://example.local/tv")

    def test_listen_defaults_to_current_room_audio_source_for_physical_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            body = Path(tmpdir) / "body_location.json"
            prefs.write_text(
                json.dumps({"mics": [
                    {"source": "rtsp://example.local/living", "room": "living", "label": "Living"},
                    {"source": "rtsp://example.local/study", "room": "study", "label": "Study"},
                ]}, ensure_ascii=False),
                encoding="utf-8",
            )
            body.write_text(json.dumps({"current_entity": "", "current_room": "study"}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"EHA_PREFS_FILE": str(prefs), "EHA_BODY_LOCATION_FILE": str(body)}, clear=False):
                self.assertEqual(self.audio_mcp.default_listen_source(), "rtsp://example.local/study")

    def test_listen_returns_ffmpeg_missing_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "active_listen_log.jsonl"
            with mock.patch.object(self.audio_mcp, "ACTIVE_LISTEN_LOG_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value=None):
                result = self.audio_mcp.listen({"source": "alsa://default"})
        self.assertTrue(result[1])
        payload = self._json(result[0])
        self.assertEqual(payload["error"], "ffmpeg not found")

    def test_listen_without_registered_microphone_is_explicitly_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            body = Path(tmpdir) / "body_location.json"
            prefs.write_text("{}", encoding="utf-8")
            body.write_text('{"current_entity": "", "current_room": "study"}', encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"EHA_PREFS_FILE": str(prefs), "EHA_BODY_LOCATION_FILE": str(body)},
                clear=False,
            ):
                result, is_error = self.audio_mcp.listen({})
        self.assertTrue(is_error)
        self.assertIn("マイクが登録されていません", result[0]["text"])

    def test_listen_go2rtc_without_stt(self):
        responses = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr="mean_volume: -28.1 dB\nmax_volume: -12.3 dB\n"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "active_listen_log.jsonl"
            with mock.patch.object(self.audio_mcp, "ACTIVE_LISTEN_LOG_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(self.audio_mcp.subprocess, "run", side_effect=responses) as run_mock:
                payload = self._json(self.audio_mcp.listen({"source": "rtsp://localhost:8554/capture_tv", "duration": 5}))

        self.assertEqual(payload["source"], "rtsp://localhost:8554/capture_tv")
        self.assertEqual(payload["duration"], 5)
        self.assertTrue(payload["has_sound"])
        self.assertEqual(payload["peak_db"], -12.3)
        self.assertEqual(payload["mean_db"], -28.1)
        self.assertIsNone(payload["transcript"])
        first_cmd = run_mock.call_args_list[0].args[0]
        self.assertEqual(first_cmd[:4], ["/usr/bin/ffmpeg", "-rtsp_transport", "tcp", "-i"])
        self.assertIn("rtsp://localhost:8554/capture_tv", first_cmd)


    def test_listen_alsa_branch(self):
        responses = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr="mean_volume: -80.0 dB\nmax_volume: -70.0 dB\n"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "active_listen_log.jsonl"
            with mock.patch.object(self.audio_mcp, "ACTIVE_LISTEN_LOG_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(self.audio_mcp.subprocess, "run", side_effect=responses) as run_mock:
                payload = self._json(self.audio_mcp.listen({"source": "alsa://default", "duration": 3}))

        first_cmd = run_mock.call_args_list[0].args[0]
        self.assertEqual(first_cmd[:5], ["/usr/bin/ffmpeg", "-f", "alsa", "-i", "default"])
        self.assertFalse(payload["has_sound"])


    def test_listen_tcp_branch_records_audio(self):
        recorded_entries = []
        with mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value="/usr/bin/ffmpeg"),              mock.patch.object(self.audio_mcp, "request_daemon_capture_to_wav") as tcp_mock,              mock.patch.object(self.audio_mcp, "analyze_volume", return_value=(-18.0, -29.0)),              mock.patch.object(self.audio_mcp, "record_active_listen", side_effect=lambda entry, source: recorded_entries.append((entry, source))):
            payload = self._json(self.audio_mcp.listen({"source": "tcp://192.168.1.100:3333", "duration": 4}))

        tcp_mock.assert_called_once()
        self.assertEqual(payload["source"], "tcp://192.168.1.100:3333")
        self.assertTrue(payload["has_sound"])
        self.assertEqual(recorded_entries[-1][1], "tcp://192.168.1.100:3333")
        self.assertEqual(recorded_entries[-1][0]["source"], "tcp://192.168.1.100:3333")

    def test_listen_tcp_timeout_is_logged(self):
        recorded_entries = []
        with mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value="/usr/bin/ffmpeg"),              mock.patch.object(self.audio_mcp, "request_daemon_capture_to_wav", side_effect=TimeoutError("timed out")),              mock.patch.object(self.audio_mcp, "record_active_listen", side_effect=lambda entry, source: recorded_entries.append((entry, source))):
            result = self.audio_mcp.listen({"source": "tcp://192.168.1.100:3333", "duration": 4})

        payload = json.loads(result[0][0]["text"])
        self.assertIn("timed out", payload["error"])
        self.assertEqual(payload["source"], "tcp://192.168.1.100:3333")
        self.assertEqual(recorded_entries[-1][1], "tcp://192.168.1.100:3333")
        self.assertIn("timed out", recorded_entries[-1][0]["error"])

    def test_listen_records_active_log_with_transcript(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "active_listen_log.jsonl"
            prefs_path = Path(tmpdir) / "preferences.json"
            prefs_path.write_text(
                json.dumps({"mics": [{"source": "rtsp://localhost:8554/capture_tv", "label": "TV・レコーダー"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            responses = [mock.Mock(returncode=0, stdout="", stderr="")]
            fixed_now = self.audio_mcp.parse_ts("2026-06-26T10:00:00+09:00")
            with mock.patch.object(self.audio_mcp, "ACTIVE_LISTEN_LOG_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(self.audio_mcp.subprocess, "run", side_effect=responses), \
                 mock.patch.object(self.audio_mcp, "analyze_volume", return_value=(-11.0, -24.0)), \
                 mock.patch.object(self.audio_mcp, "transcribe_audio", return_value="聞こえました"), \
                 mock.patch.object(self.audio_mcp, "now", return_value=fixed_now), \
                 mock.patch.dict(os.environ, {"EHA_ACTOR": "explore", "EHA_PREFS_FILE": str(prefs_path)}, clear=False):
                payload = self._json(self.audio_mcp.listen({"source": "rtsp://localhost:8554/capture_tv", "duration": 5, "transcribe": True}))

            self.assertEqual(payload["transcript"], "聞こえました")
            self.assertIn("audio_context", payload)
            self.assertEqual(payload["audio_context"]["type"], "active_listen")
            self.assertEqual(payload["audio_context"]["actor"], "explore")
            self.assertEqual(payload["audio_context"]["source"], "rtsp://localhost:8554/capture_tv")
            self.assertEqual(payload["audio_context"]["source_label"], "TV・レコーダー")
            self.assertEqual(payload["audio_context"]["duration_sec"], 5)
            self.assertTrue(payload["audio_context"]["has_sound"])
            self.assertEqual(payload["audio_context"]["transcript"], "聞こえました")
            self.assertEqual(payload["audio_context"]["log_ref"]["file"], "active_listen_log.jsonl")
            self.assertEqual(payload["audio_context"]["log_ref"]["timestamp"], payload["timestamp"])
            entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["kind"], "active_listen")
            self.assertEqual(entries[0]["type"], "active_listen")
            self.assertEqual(entries[0]["actor"], "explore")
            self.assertEqual(entries[0]["source_label"], "TV・レコーダー")
            self.assertEqual(entries[0]["duration_sec"], 5)
            self.assertTrue(entries[0]["transcribe_requested"])
            self.assertEqual(entries[0]["transcript"], "聞こえました")

    def test_read_active_listen_log_filters_recent_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "active_listen_log.jsonl"
            log_path.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-06-26T09:00:00+09:00", "source": "A", "transcript": "old"}, ensure_ascii=False),
                    json.dumps({"timestamp": "2026-06-26T09:55:00+09:00", "source": "A", "transcript": "recent"}, ensure_ascii=False),
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(self.audio_mcp, "ACTIVE_LISTEN_LOG_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "now", return_value=self.audio_mcp.parse_ts("2026-06-26T10:00:00+09:00")):
                payload = self._json(self.audio_mcp.read_active_listen_log({"limit": 5, "since_minutes": 10}))
        self.assertEqual([entry["transcript"] for entry in payload], ["recent"])

    def test_read_audio_log_filters_recent_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audio_log.jsonl"
            log_path.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-06-25T10:00:00+09:00", "source": "A", "text": "old"}, ensure_ascii=False),
                    json.dumps({"timestamp": "2026-06-25T10:55:00+09:00", "source": "A", "text": "recent"}, ensure_ascii=False),
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(self.audio_mcp, "AUDIO_LOG_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "now", return_value=self.audio_mcp.parse_ts("2026-06-25T11:00:00+09:00")):
                payload = self._json(self.audio_mcp.read_audio_log({"limit": 5, "since_minutes": 10}))
        self.assertEqual([entry["text"] for entry in payload], ["recent"])

    def test_read_heard_audio_log_filters_recent_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "auditory_events.jsonl"
            log_path.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-06-25T10:00:00+09:00", "source": "A", "transcript": "old"}, ensure_ascii=False),
                    json.dumps({"timestamp": "2026-06-25T10:55:00+09:00", "source": "A", "transcript": "recent"}, ensure_ascii=False),
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(self.audio_mcp, "AUDITORY_EVENTS_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "now", return_value=self.audio_mcp.parse_ts("2026-06-25T11:00:00+09:00")):
                payload = self._json(self.audio_mcp.read_heard_audio_log({"limit": 5, "since_minutes": 10}))
        self.assertEqual([entry["transcript"] for entry in payload], ["recent"])

    def test_read_non_speech_audio_events_filters_recent_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "non_speech_audio_events.jsonl"
            log_path.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-06-25T10:00:00+09:00", "event_id": "old"}, ensure_ascii=False),
                    json.dumps({"timestamp": "2026-06-25T10:55:00+09:00", "event_id": "recent"}, ensure_ascii=False),
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(self.audio_mcp, "NON_SPEECH_AUDIO_EVENTS_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "now", return_value=self.audio_mcp.parse_ts("2026-06-25T11:00:00+09:00")):
                payload = self._json(self.audio_mcp.read_non_speech_audio_events({"limit": 5, "since_minutes": 10}))
        self.assertEqual([entry["event_id"] for entry in payload], ["recent"])

    def test_read_audio_event_tags_filters_recent_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audio_event_tags.jsonl"
            log_path.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-06-25T10:00:00+09:00", "event_id": "a", "label": "old"}, ensure_ascii=False),
                    json.dumps({"timestamp": "2026-06-25T10:55:00+09:00", "event_id": "b", "label": "recent"}, ensure_ascii=False),
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(self.audio_mcp, "AUDIO_EVENT_TAGS_FILE", str(log_path)), \
                 mock.patch.object(self.audio_mcp, "now", return_value=self.audio_mcp.parse_ts("2026-06-25T11:00:00+09:00")):
                payload = self._json(self.audio_mcp.read_audio_event_tags({"limit": 5, "since_minutes": 10}))
        self.assertEqual([entry["label"] for entry in payload], ["recent"])

    def test_transcribe_routes_to_ha_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(json.dumps({"stt_provider": "wyoming"}, ensure_ascii=False), encoding="utf-8")
            old = os.environ.get("EHA_PREFS_FILE")
            os.environ["EHA_PREFS_FILE"] = str(prefs)
            try:
                with mock.patch.object(self.audio_mcp, "transcribe_via_ha", return_value="こんにちは") as ha_mock, \
                     mock.patch.object(self.audio_mcp, "transcribe_via_local", return_value="ローカル") as local_mock:
                    result = self.audio_mcp.transcribe_audio("/tmp/example.wav")
            finally:
                if old is None:
                    os.environ.pop("EHA_PREFS_FILE", None)
                else:
                    os.environ["EHA_PREFS_FILE"] = old
        self.assertEqual(result, "こんにちは")
        ha_mock.assert_called_once_with("/tmp/example.wav", "wyoming")
        local_mock.assert_not_called()

    def test_transcribe_routes_to_local_when_provider_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text("{}", encoding="utf-8")
            old = os.environ.get("EHA_PREFS_FILE")
            os.environ["EHA_PREFS_FILE"] = str(prefs)
            try:
                with mock.patch.object(self.audio_mcp, "transcribe_via_ha", return_value="こんにちは") as ha_mock, \
                     mock.patch.object(self.audio_mcp, "transcribe_via_local", return_value="ローカル") as local_mock:
                    result = self.audio_mcp.transcribe_audio("/tmp/example.wav")
            finally:
                if old is None:
                    os.environ.pop("EHA_PREFS_FILE", None)
                else:
                    os.environ["EHA_PREFS_FILE"] = old
        self.assertEqual(result, "ローカル")
        ha_mock.assert_not_called()
        local_mock.assert_called_once_with("/tmp/example.wav")



    def test_listen_media_resolves_audio_media_to_recordable_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(
                json.dumps({"audio_media": [{"id": "capture_tv_audio", "source": "capture_tv", "label": "テレビ音声", "room": "living"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"EHA_PREFS_FILE": str(prefs)}, clear=False),                  mock.patch.object(self.audio_mcp, "_current_body_state", return_value=({}, "", "", "")),                  mock.patch.object(self.audio_mcp, "_audio_listen_from_source", return_value=([{"type": "text", "text": "ok"}], False)) as listen_mock:
                result = self.audio_mcp.listen_media({"source": "capture_tv_audio", "duration": 7, "transcribe": True})
        self.assertEqual(result, ([{"type": "text", "text": "ok"}], False))
        listen_mock.assert_called_once()
        args, kwargs = listen_mock.call_args
        self.assertEqual(args[0], "rtsp://localhost:8554/capture_tv")
        self.assertEqual(args[1], 7)
        self.assertTrue(args[2])
        self.assertEqual(kwargs["source_label_override"], "テレビ音声")
        self.assertEqual(kwargs["extra_payload"]["media_context"]["media_id"], "capture_tv_audio")
        self.assertEqual(kwargs["extra_payload"]["media_context"]["label"], "テレビ音声")
        self.assertEqual(kwargs["media_kind_hint"], '聴いた内容を残すなら record_episode(kind="media_listen", ...) を使ってよい。')

    def test_listen_media_errors_for_unknown_source(self):
        with mock.patch.object(self.audio_mcp, "_current_body_state", return_value=({}, "", "", "")),              mock.patch.object(self.audio_mcp, "_resolve_audio_media_item", return_value=({}, "")):
            result = self.audio_mcp.listen_media({"source": "missing"})
        self.assertTrue(result[1])
        self.assertIn("未登録", result[0][0]["text"])

if __name__ == "__main__":
    unittest.main()
