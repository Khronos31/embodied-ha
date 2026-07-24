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

    def test_voicevox_tts_passes_options(self):
        prefs = {
            "tts_entity": "tts.voicevox_tts",
            "tts_options": {"speaker": 56, "volume": 1.1, "pitch": 0.02, "speed": 1.2},
            "speakers": [{"room": "study", "type": "tts", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        payloads = []
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(self.speak, "_request_tts_url", return_value="/api/tts_proxy/test"), \
                 mock.patch.object(self.speak, "_fetch_tts_audio", return_value=b"audio"), \
                 mock.patch.object(
                     self.speak, "curl_post",
                     side_effect=lambda _url, payload, _token: payloads.append(json.loads(payload)) or True,
                 ):
                ok = self.speak.speak("study", "hello")
        finally:
            os.unlink(prefs_path)
        self.assertTrue(ok)
        self.assertEqual(payloads[0]["options"], prefs["tts_options"])
        self.assertEqual(payloads[0]["language"], "ja-JP")

    def test_non_voicevox_tts_does_not_pass_options(self):
        prefs = {
            "tts_entity": "tts.home_assistant_cloud",
            "tts_options": {"speaker": 56},
            "speakers": [{"room": "study", "type": "tts", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        payloads = []
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(
                     self.speak, "curl_post",
                     side_effect=lambda _url, payload, _token: payloads.append(json.loads(payload)) or True,
                 ):
                ok = self.speak.speak("study", "hello")
        finally:
            os.unlink(prefs_path)
        self.assertTrue(ok)
        self.assertNotIn("options", payloads[0])

    def test_voicevox_tts_preflight_failure_uses_defaults_once(self):
        prefs = {
            "tts_entity": "tts.voicevox_tts",
            "tts_options": {"speaker": 999999},
            "speakers": [{"room": "study", "type": "tts", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        payloads = []
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(
                     self.speak, "_request_tts_url",
                     side_effect=RuntimeError("stale speaker"),
                 ), \
                 mock.patch.object(
                     self.speak, "curl_post",
                     side_effect=lambda _url, payload, _token: payloads.append(json.loads(payload)) or True,
                 ):
                ok = self.speak.speak("study", "hello")
        finally:
            os.unlink(prefs_path)
        self.assertTrue(ok)
        self.assertEqual(len(payloads), 1)
        self.assertNotIn("options", payloads[0])

    def test_voicevox_delayed_synthesis_failure_uses_defaults_once(self):
        prefs = {
            "tts_entity": "tts.voicevox_tts",
            "tts_options": {"speaker": 999999},
            "speakers": [{"room": "study", "type": "tts", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        payloads = []
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(self.speak, "_request_tts_url", return_value="/api/tts_proxy/test"), \
                 mock.patch.object(
                     self.speak, "_fetch_tts_audio",
                     side_effect=RuntimeError("VOICEVOX synthesis failed"),
                 ), \
                 mock.patch.object(
                     self.speak, "curl_post",
                     side_effect=lambda _url, payload, _token: payloads.append(json.loads(payload)) or True,
                 ):
                ok = self.speak.speak("study", "hello")
        finally:
            os.unlink(prefs_path)
        self.assertTrue(ok)
        self.assertEqual(len(payloads), 1)
        self.assertNotIn("options", payloads[0])

    def test_invalid_runtime_options_are_ignored(self):
        prefs = {
            "tts_entity": "tts.voicevox_tts",
            "tts_options": {"speaker": -1},
            "speakers": [{"room": "study", "type": "tts", "entity": "media_player.study"}],
        }
        prefs_path = self._write_prefs(prefs)
        payloads = []
        try:
            env = {**self._ENV, "EHA_PREFS_FILE": prefs_path}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(
                     self.speak, "curl_post",
                     side_effect=lambda _url, payload, _token: payloads.append(json.loads(payload)) or True,
                 ):
                ok = self.speak.speak("study", "hello")
        finally:
            os.unlink(prefs_path)
        self.assertTrue(ok)
        self.assertNotIn("options", payloads[0])


class VoicevoxPcmTests(unittest.TestCase):
    def setUp(self):
        self.speak = _load_speak()

    def test_pcm_fetch_retries_without_options(self):
        options = {"speaker": 12, "volume": 1.0}
        calls = []

        def fake_fetch(*args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("stale speaker")
            return b"pcm"

        with mock.patch.object(self.speak, "_fetch_pcm_for_message", side_effect=fake_fetch):
            result = self.speak._fetch_pcm_with_fallback(
                "hello", "http://ha", "token", "voicevox_tts", "ja-JP", options
            )
        self.assertEqual(result, b"pcm")
        self.assertEqual(calls[0][-1], options)
        self.assertEqual(len(calls[1]), 5)

    def test_fetch_uses_current_ha_engine_id_contract(self):
        tts_response = mock.MagicMock()
        tts_response.__enter__.return_value = tts_response
        tts_response.read.return_value = json.dumps({
            "url": "http://homeassistant.local:8123/api/tts_proxy/test.wav"
        }).encode()
        audio_response = mock.MagicMock()
        audio_response.__enter__.return_value = audio_response
        audio_response.read.return_value = b"wav"
        proc = mock.Mock()
        proc.communicate.return_value = (b"pcm", b"")
        proc.returncode = 0
        requests = []

        def fake_urlopen(request, timeout=15):
            requests.append(request)
            return tts_response if len(requests) == 1 else audio_response

        with mock.patch.object(self.speak.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(self.speak.subprocess, "Popen", return_value=proc):
            result = self.speak._fetch_pcm_for_message(
                "hello", "http://supervisor/core/api", "token",
                "voicevox_tts", "ja-JP", {"speaker": 56},
            )

        payload = json.loads(requests[0].data)
        self.assertEqual(payload["engine_id"], "tts.voicevox_tts")
        self.assertNotIn("platform", payload)
        self.assertEqual(payload["options"], {"speaker": 56})
        self.assertEqual(result, b"pcm")


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


class LocalPlaybackGainTests(unittest.TestCase):
    def setUp(self):
        self.speak = _load_speak()

    def test_local_playback_uses_fixed_gain_and_limiter_before_paplay(self):
        boosted = b"\x03\x04" * 80
        ffmpeg = mock.Mock()
        ffmpeg.communicate.return_value = (boosted, b"")
        ffmpeg.returncode = 0
        paplay = mock.Mock()
        paplay.communicate.return_value = (b"", b"")
        paplay.returncode = 0

        with mock.patch.object(
            self.speak.subprocess, "Popen", side_effect=[ffmpeg, paplay]
        ) as popen:
            self.speak._play_pcm_local(
                b"\x01\x02" * 80,
                sink="alsa_output.test",
                sample_rate=16000,
                channels=1,
            )

        ffmpeg_cmd = popen.call_args_list[0].args[0]
        self.assertIn(
            "volume=1.5,alimiter=limit=0.95:level=false", ffmpeg_cmd
        )
        paplay_cmd = popen.call_args_list[1].args[0]
        self.assertEqual(paplay_cmd[0], "paplay")
        self.assertIn("--device=alsa_output.test", paplay_cmd)
        paplay.communicate.assert_called_once_with(input=boosted, timeout=30)


if __name__ == "__main__":
    unittest.main()
