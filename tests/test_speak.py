"""speak.py のユニットテスト。"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
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


class TtsMediaSourceUriTests(unittest.TestCase):
    def setUp(self):
        self.speak = _load_speak()

    def test_builds_provider_neutral_uri_without_options(self):
        uri = self.speak._tts_media_source_uri(
            "tts.voicevox_tts_tsumugi", "こんにちは、世界。"
        )
        parsed = urlparse(uri)
        self.assertEqual(parsed.scheme, "media-source")
        self.assertEqual(parsed.netloc, "tts")
        self.assertEqual(parsed.path, "/tts.voicevox_tts_tsumugi")
        self.assertEqual(
            parse_qs(parsed.query),
            {"message": ["こんにちは、世界。"], "cache": ["false"]},
        )
        self.assertNotIn("tts_options", parsed.query)
        self.assertNotIn("language", parsed.query)

    def test_rejects_non_tts_entity(self):
        with self.assertRaisesRegex(ValueError, "tts_entity"):
            self.speak._tts_media_source_uri("voicevox_tts", "hello")

    def test_adds_only_language_and_voice(self):
        uri = self.speak._tts_media_source_uri(
            "tts.google_ai_tts", "hello", "ja-JP", "zephyr"
        )
        self.assertEqual(
            parse_qs(urlparse(uri).query),
            {
                "message": ["hello"],
                "cache": ["false"],
                "language": ["ja-JP"],
                "voice": ["zephyr"],
            },
        )

    def test_ignores_voice_without_language(self):
        uri = self.speak._tts_media_source_uri(
            "tts.google_ai_tts", "hello", "", "zephyr"
        )
        self.assertNotIn("voice", parse_qs(urlparse(uri).query))


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

    def test_tts_uses_media_source_and_entity_defaults(self):
        prefs = {
            "tts_entity": "tts.voicevox_tts_aru",
            "tts_options": {"speaker": 56, "volume": 1.1, "pitch": 0.02, "speed": 1.2},
            "speakers": [{"room": "study", "type": "tts", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        payloads = []
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(
                     self.speak, "curl_post",
                     side_effect=lambda url, payload, _token: payloads.append((url, json.loads(payload))) or True,
                 ):
                ok = self.speak.speak("study", "こんにちは")
        finally:
            os.unlink(prefs_path)
        self.assertTrue(ok)
        url, payload = payloads[0]
        self.assertEqual(url, "http://supervisor/core/api/services/media_player/play_media")
        self.assertEqual(payload["entity_id"], "media_player.study")
        self.assertEqual(payload["media"]["media_content_type"], "music")
        parsed = urlparse(payload["media"]["media_content_id"])
        self.assertEqual(parsed.path, "/tts.voicevox_tts_aru")
        self.assertEqual(parse_qs(parsed.query), {
            "message": ["こんにちは"],
            "cache": ["false"],
        })
        self.assertNotIn("tts_options", payload)

    def test_non_tts_entity_returns_false_without_post(self):
        prefs = {
            "tts_entity": "voicevox_tts",
            "speakers": [{"room": "study", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(self.speak, "curl_post") as post:
                self.assertFalse(self.speak.speak("study", "hello"))
                post.assert_not_called()
        finally:
            os.unlink(prefs_path)

    def test_tts_uses_selection_for_active_entity_only(self):
        prefs = {
            "tts_entity": "tts.google_ai_tts",
            "tts_selections": {
                "tts.google_ai_tts": {"language": "ja-JP", "voice": "zephyr"},
                "tts.home_assistant_cloud": {
                    "language": "en-US",
                    "voice": "AvaMultilingualNeural",
                },
            },
            "speakers": [{"room": "study", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        payloads = []
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                self.speak,
                "curl_post",
                side_effect=lambda _url, payload, _token: payloads.append(
                    json.loads(payload)
                ) or True,
            ):
                self.assertTrue(self.speak.speak("study", "こんにちは"))
        finally:
            os.unlink(prefs_path)
        query = parse_qs(urlparse(payloads[0]["media"]["media_content_id"]).query)
        self.assertEqual(query["language"], ["ja-JP"])
        self.assertEqual(query["voice"], ["zephyr"])
        self.assertNotIn("en-US", str(query))

    def test_option_failure_retries_exact_legacy_uri_once(self):
        prefs = {
            "tts_entity": "tts.google_ai_tts",
            "tts_selections": {
                "tts.google_ai_tts": {"language": "ja-JP", "voice": "stale"}
            },
            "speakers": [{"room": "study", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        payloads = []
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                self.speak,
                "curl_post",
                side_effect=lambda _url, payload, _token: payloads.append(
                    json.loads(payload)
                ) or len(payloads) == 2,
            ):
                self.assertTrue(self.speak.speak("study", "hello"))
        finally:
            os.unlink(prefs_path)
        self.assertEqual(len(payloads), 2)
        first = parse_qs(urlparse(payloads[0]["media"]["media_content_id"]).query)
        second = parse_qs(urlparse(payloads[1]["media"]["media_content_id"]).query)
        self.assertEqual(first["voice"], ["stale"])
        self.assertEqual(second, {"message": ["hello"], "cache": ["false"]})

    def test_default_uri_failure_is_not_retried(self):
        prefs = {
            "tts_entity": "tts.voicevox_tts_aru",
            "speakers": [{"room": "study", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                self.speak, "curl_post", return_value=False
            ) as post:
                self.assertFalse(self.speak.speak("study", "hello"))
        finally:
            os.unlink(prefs_path)
        post.assert_called_once()


class AudioFileValidationTests(unittest.TestCase):
    def setUp(self):
        self.speak = _load_speak()

    def test_non_regular_audio_path_is_rejected_before_open_or_ffmpeg(self):
        fifo_stat = mock.Mock(st_mode=self.speak.stat.S_IFIFO, st_size=100)
        with mock.patch.object(self.speak.os, "stat", return_value=fifo_stat), \
             mock.patch("builtins.open") as open_mock, \
             mock.patch.object(self.speak.subprocess, "Popen") as popen_mock:
            with self.assertRaisesRegex(RuntimeError, "audio file read failed"):
                self.speak._pcm_bytes_from_file("/tmp/audio.webm")

        open_mock.assert_not_called()
        popen_mock.assert_not_called()

    def test_oversized_source_is_rejected_before_ffmpeg(self):
        audio = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        audio.write(b"large")
        audio.close()
        try:
            with mock.patch.object(self.speak, "MAX_AUDIO_INPUT_BYTES", 4), \
                 mock.patch.object(self.speak.subprocess, "Popen") as popen_mock:
                with self.assertRaisesRegex(RuntimeError, "audio file read failed"):
                    self.speak._pcm_bytes_from_file(audio.name)
        finally:
            os.unlink(audio.name)

        popen_mock.assert_not_called()

if __name__ == "__main__":
    unittest.main()
