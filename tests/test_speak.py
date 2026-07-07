"""speak.py のユニットテスト。"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_speak():
    spec = importlib.util.spec_from_file_location(
        "speak_module", ROOT / "embodied_ha" / "speak.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FindSpeakerTests(unittest.TestCase):
    def setUp(self):
        self.speak = _load_speak()

    def test_list_finds_by_room(self):
        speakers = [
            {"room": "study", "type": "tts"},
            {"room": "kitchen", "type": "tcp", "host": "192.168.1.100", "port": 3334},
        ]
        result = self.speak._find_speaker(speakers, "kitchen")
        self.assertEqual(result["type"], "tcp")
        self.assertEqual(result["host"], "192.168.1.100")

    def test_list_first_match_wins(self):
        speakers = [
            {"room": "study", "type": "tts"},
            {"room": "study", "type": "notify"},
        ]
        self.assertEqual(self.speak._find_speaker(speakers, "study")["type"], "tts")

    def test_list_unknown_room_returns_empty(self):
        speakers = [{"room": "study", "type": "tts"}]
        self.assertEqual(self.speak._find_speaker(speakers, "kitchen"), {})

    def test_empty_list_returns_empty(self):
        self.assertEqual(self.speak._find_speaker([], "study"), {})

    def test_dict_backward_compat(self):
        speakers = {"study": {"type": "tts", "label": "書斎"}}
        result = self.speak._find_speaker(speakers, "study")
        self.assertEqual(result["type"], "tts")

    def test_dict_unknown_room_returns_empty(self):
        speakers = {"study": {"type": "tts"}}
        self.assertEqual(self.speak._find_speaker(speakers, "kitchen"), {})


class RewriteTtsUrlTests(unittest.TestCase):
    def setUp(self):
        self.speak = _load_speak()

    def test_supervisor_rewrite(self):
        ha_url = "http://supervisor/core/api"
        tts_url = "http://homeassistant.local:8123/api/tts_proxy/xxxxx_16000Hz.mp3"
        result = self.speak._rewrite_tts_url(tts_url, ha_url)
        self.assertEqual(result, "http://supervisor/core/api/tts_proxy/xxxxx_16000Hz.mp3")

    def test_preserves_query_string(self):
        ha_url = "http://supervisor/core/api"
        tts_url = "http://homeassistant.local:8123/api/tts_proxy/xxx.mp3?token=abc"
        result = self.speak._rewrite_tts_url(tts_url, ha_url)
        self.assertEqual(result, "http://supervisor/core/api/tts_proxy/xxx.mp3?token=abc")

    def test_ha_url_with_trailing_slash(self):
        ha_url = "http://supervisor/core/api/"
        tts_url = "http://external:8123/api/tts_proxy/xxx.mp3"
        result = self.speak._rewrite_tts_url(tts_url, ha_url)
        self.assertEqual(result, "http://supervisor/core/api/tts_proxy/xxx.mp3")


class SpeakTcpTests(unittest.TestCase):
    _ENV = {
        "HA_URL": "http://supervisor/core/api",
        "SUPERVISOR_TOKEN": "test-token",
    }

    def setUp(self):
        self.speak = _load_speak()

    def _write_prefs(self, prefs: dict) -> str:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        )
        json.dump(prefs, f, ensure_ascii=False)
        f.close()
        return f.name

    def test_tcp_success(self):
        prefs = {
            "tts_provider": "tts.home_assistant_cloud",
            "stt_language": "ja-JP",
            "speakers": [
                {
                    "room": "kitchen",
                    "label": "台所（VoiceS3R）",
                    "type": "tcp",
                    "host": "192.168.1.100",
                    "port": 3334,
                    "note": "",
                }
            ],
        }
        pcm_data = b"\x00\x01" * 800

        mock_sock = mock.MagicMock()
        sent_data = []
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = lambda s, *a: None
        mock_sock.sendall.side_effect = lambda d: sent_data.append(d)

        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(self.speak, "_fetch_pcm_for_message", return_value=pcm_data), \
                 mock.patch("socket.create_connection", return_value=mock_sock):
                ok = self.speak.speak("kitchen", "こんにちは")
        finally:
            os.unlink(prefs_path)

        self.assertTrue(ok)
        self.assertEqual(sent_data, [pcm_data])

    def test_local_success_plays_to_sink(self):
        prefs = {
            "tts_provider": "tts.home_assistant_cloud",
            "stt_language": "ja-JP",
            "speakers": [
                {
                    "room": "本体",
                    "label": "本体（M720q内蔵）",
                    "type": "local",
                    "sink": "alsa_output.pci-0000_00_1f.3.analog-stereo",
                }
            ],
        }
        pcm_data = b"\x00\x01" * 800
        played = []

        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(self.speak, "_fetch_pcm_for_message", return_value=pcm_data), \
                 mock.patch.object(self.speak, "_play_pcm_local",
                                   side_effect=lambda pcm, sink="", **kw: played.append((pcm, sink))):
                ok = self.speak.speak("本体", "こんにちは")
        finally:
            os.unlink(prefs_path)

        self.assertTrue(ok)
        self.assertEqual(played, [(pcm_data, "alsa_output.pci-0000_00_1f.3.analog-stereo")])

    def test_tcp_missing_host_returns_false(self):
        prefs = {
            "tts_provider": "tts.home_assistant_cloud",
            "speakers": [{"room": "kitchen", "type": "tcp", "port": 3334}],
        }
        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env):
                ok = self.speak.speak("kitchen", "こんにちは")
        finally:
            os.unlink(prefs_path)
        self.assertFalse(ok)

    def test_tcp_missing_tts_provider_returns_false(self):
        prefs = {
            "speakers": [{"room": "kitchen", "type": "tcp", "host": "192.168.1.100", "port": 3334}],
        }
        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env):
                ok = self.speak.speak("kitchen", "こんにちは")
        finally:
            os.unlink(prefs_path)
        self.assertFalse(ok)

    def test_tcp_inherits_global_tts_provider(self):
        prefs = {
            "tts_provider": "tts.global_provider",
            "speakers": [
                {"room": "kitchen", "type": "tcp", "host": "192.168.1.100", "port": 3334}
            ],
        }
        pcm_data = b"\x00" * 100
        mock_sock = mock.MagicMock()
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = lambda s, *a: None

        prefs_path = self._write_prefs(prefs)
        captured = {}
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            def fake_fetch(message, ha_url, ha_token, tts_provider, tts_language):
                captured["tts_provider"] = tts_provider
                return pcm_data
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(self.speak, "_fetch_pcm_for_message", side_effect=fake_fetch), \
                 mock.patch("socket.create_connection", return_value=mock_sock):
                self.speak.speak("kitchen", "テスト")
        finally:
            os.unlink(prefs_path)
        self.assertEqual(captured.get("tts_provider"), "tts.global_provider")

    def test_tcp_tts_fetch_failure_returns_false(self):
        prefs = {
            "tts_provider": "tts.home_assistant_cloud",
            "speakers": [
                {"room": "kitchen", "type": "tcp", "host": "192.168.1.100", "port": 3334}
            ],
        }
        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(
                     self.speak, "_fetch_pcm_for_message",
                     side_effect=RuntimeError("TTS failed")
                 ):
                ok = self.speak.speak("kitchen", "こんにちは")
        finally:
            os.unlink(prefs_path)
        self.assertFalse(ok)

    def test_tcp_socket_error_returns_false(self):
        prefs = {
            "tts_provider": "tts.home_assistant_cloud",
            "speakers": [
                {"room": "kitchen", "type": "tcp", "host": "192.168.1.100", "port": 3334}
            ],
        }
        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(self.speak, "_fetch_pcm_for_message", return_value=b"\x00" * 100), \
                 mock.patch("socket.create_connection", side_effect=ConnectionRefusedError("refused")):
                ok = self.speak.speak("kitchen", "こんにちは")
        finally:
            os.unlink(prefs_path)
        self.assertFalse(ok)


class SpeakGeneralTests(unittest.TestCase):
    _ENV = {
        "HA_URL": "http://supervisor/core/api",
        "SUPERVISOR_TOKEN": "test-token",
        "EHA_PREFS_FILE": "",
    }

    def setUp(self):
        self.speak = _load_speak()

    def _write_prefs(self, prefs: dict) -> str:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        )
        json.dump(prefs, f, ensure_ascii=False)
        f.close()
        return f.name

    def test_unknown_room_returns_false(self):
        prefs = {"speakers": [{"room": "study", "type": "tts"}]}
        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env):
                ok = self.speak.speak("kitchen", "hello")
        finally:
            os.unlink(prefs_path)
        self.assertFalse(ok)

    def test_unknown_type_returns_false(self):
        prefs = {"speakers": [{"room": "study", "type": "magic"}]}
        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env):
                ok = self.speak.speak("study", "hello")
        finally:
            os.unlink(prefs_path)
        self.assertFalse(ok)

    def test_no_prefs_file_returns_false(self):
        env = {**self._ENV, "EHA_PREFS_FILE": "/nonexistent/path.json"}
        with mock.patch.dict(os.environ, env):
            ok = self.speak.speak("study", "hello")
        self.assertFalse(ok)


class PlayPcmFileTests(unittest.TestCase):
    def setUp(self):
        self.speak = _load_speak()

    def _write_prefs(self, prefs: dict) -> str:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        )
        json.dump(prefs, f, ensure_ascii=False)
        f.close()
        return f.name

    def test_play_pcm_file_supports_local_speaker(self):
        prefs = {
            "speakers": [
                {"room": "本体", "type": "local", "sink": "alsa_output.test"}
            ]
        }
        prefs_path = self._write_prefs(prefs)
        audio = tempfile.NamedTemporaryFile(delete=False)
        audio.write(b"\x01\x02" * 100)
        audio.close()
        played = []
        try:
            with mock.patch.dict(os.environ, {"EHA_PREFS_FILE": prefs_path}, clear=False), \
                 mock.patch.object(self.speak, "_play_pcm_local", side_effect=lambda pcm, sink="", **kw: played.append((pcm, sink))):
                ok = self.speak.play_pcm_file("本体", audio.name)
        finally:
            os.unlink(prefs_path)
            os.unlink(audio.name)

        self.assertTrue(ok)
        self.assertEqual(played, [(b"\x01\x02" * 100, "alsa_output.test")])

    def test_play_pcm_file_converts_wav_before_local_playback(self):
        prefs = {"speakers": [{"room": "本体", "type": "local"}]}
        prefs_path = self._write_prefs(prefs)
        wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        wav.write(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32)
        wav.close()
        converted = b"\x00\x01" * 80
        proc = mock.Mock()
        proc.communicate.return_value = (converted, b"")
        proc.returncode = 0
        played = []
        try:
            with mock.patch.dict(os.environ, {"EHA_PREFS_FILE": prefs_path}, clear=False), \
                 mock.patch.object(self.speak.subprocess, "Popen", return_value=proc) as popen_mock, \
                 mock.patch.object(self.speak, "_play_pcm_local", side_effect=lambda pcm, sink="", **kw: played.append((pcm, sink))):
                ok = self.speak.play_pcm_file("本体", wav.name)
        finally:
            os.unlink(prefs_path)
            os.unlink(wav.name)

        self.assertTrue(ok)
        self.assertEqual(played, [(converted, "")])
        cmd = popen_mock.call_args.args[0]
        self.assertEqual(cmd[:4], ["ffmpeg", "-loglevel", "error", "-i"])
        self.assertEqual(cmd[4], wav.name)
        self.assertIn("s16le", cmd)


if __name__ == "__main__":
    unittest.main()
