"""speak.py のユニットテスト。"""
import importlib.util
import json
import os
import sys
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
            {"room": "kitchen", "type": "tcp", "host": "192.168.1.153", "port": 3334},
        ]
        result = self.speak._find_speaker(speakers, "kitchen")
        self.assertEqual(result["type"], "tcp")
        self.assertEqual(result["host"], "192.168.1.153")

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
                    "host": "192.168.1.153",
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
            "speakers": [{"room": "kitchen", "type": "tcp", "host": "192.168.1.153", "port": 3334}],
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
                {"room": "kitchen", "type": "tcp", "host": "192.168.1.153", "port": 3334}
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
                {"room": "kitchen", "type": "tcp", "host": "192.168.1.153", "port": 3334}
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
                {"room": "kitchen", "type": "tcp", "host": "192.168.1.153", "port": 3334}
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


if __name__ == "__main__":
    unittest.main()
