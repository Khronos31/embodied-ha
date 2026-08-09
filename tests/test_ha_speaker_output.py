"""HA media_player-only speaker output contract tests."""

import importlib.util
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_speak():
    spec = importlib.util.spec_from_file_location(
        "speak_ha_output", ROOT / "embodied_ha" / "speak.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HaSpeakerOutputTests(unittest.TestCase):
    def setUp(self):
        self.speak = _load_speak()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefs_path = self.root / "preferences.json"
        self.media_dir = self.root / "media" / "embodied-ha"

    def _write_prefs(self, speaker: dict, **extra) -> None:
        prefs = {
            "tts_entity": "tts.voicevox_tts_tsumugi",
            # Legacy input may remain in an existing file but must not override
            # the selected HA TTS entity's configured defaults.
            "tts_options": {"speaker": 8, "volume": 2, "pitch": 0, "speed": 1},
            "speakers": [speaker],
            **extra,
        }
        self.prefs_path.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")

    def _env(self) -> dict[str, str]:
        return {
            "HA_URL": "http://supervisor/core/api",
            "SUPERVISOR_TOKEN": "test-token",
            "EHA_PREFS_FILE": str(self.prefs_path),
            "EHA_MEDIA_SOURCE_DIR": str(self.media_dir),
        }

    def test_tts_uses_global_entity_media_source_and_disables_cache(self):
        self._write_prefs({
            "room": "study",
            "entity": "media_player.study",
            "tts_entity": "tts.must_not_be_used",
        })
        payloads = []
        with mock.patch.dict(os.environ, self._env(), clear=False), \
             mock.patch.object(
                 self.speak,
                 "curl_post",
                 side_effect=lambda url, payload, token: payloads.append(
                     (url, json.loads(payload), token)
                 ) or True,
             ):
            ok = self.speak.speak("study", "テスト")

        self.assertTrue(ok)
        url, payload, token = payloads[0]
        self.assertEqual(url, "http://supervisor/core/api/services/media_player/play_media")
        self.assertEqual(token, "test-token")
        self.assertEqual(payload["entity_id"], "media_player.study")
        self.assertEqual(payload["media"]["media_content_type"], "music")
        parsed = urlparse(payload["media"]["media_content_id"])
        self.assertEqual(parsed.path, "/tts.voicevox_tts_tsumugi")
        self.assertEqual(parse_qs(parsed.query), {
            "message": ["テスト"],
            "cache": ["false"],
        })

    def test_legacy_tcp_and_local_speaker_types_are_rejected(self):
        for legacy_type in ("tcp", "local"):
            with self.subTest(legacy_type=legacy_type):
                self._write_prefs({
                    "room": "study",
                    "type": legacy_type,
                    "entity": "media_player.study",
                    "host": "192.0.2.1",
                })
                with mock.patch.dict(os.environ, self._env(), clear=False), \
                     mock.patch.object(self.speak, "curl_post") as post:
                    self.assertFalse(self.speak.speak("study", "テスト"))
                post.assert_not_called()

    def test_file_path_is_persisted_and_played_through_media_source(self):
        self._write_prefs({"room": "study", "entity": "media_player.study"})
        source = self.root / "song.wav"
        with wave.open(str(source), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x01\x02" * 160)

        payloads = []
        with mock.patch.dict(os.environ, self._env(), clear=False), \
             mock.patch.object(
                 self.speak,
                 "curl_post",
                 side_effect=lambda url, payload, token: payloads.append(
                     (url, json.loads(payload), token)
                 ) or True,
             ):
            self.assertTrue(self.speak.play_audio_file("study", str(source)))
            self.assertTrue(self.speak.play_audio_file("study", str(source)))

        persisted = list(self.media_dir.glob("*.wav"))
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].read_bytes(), source.read_bytes())
        self.assertEqual(len(payloads), 2)
        url, payload, token = payloads[0]
        self.assertEqual(url, "http://supervisor/core/api/services/media_player/play_media")
        self.assertEqual(token, "test-token")
        self.assertEqual(payload["entity_id"], "media_player.study")
        self.assertEqual(payload["media_content_type"], "audio/wav")
        self.assertTrue(payload["media_content_id"].startswith(
            "media-source://media_source/local/embodied-ha/"
        ))

    def test_raw_pcm_is_persisted_as_wav_for_media_source(self):
        source = self.root / "sample.pcm"
        source.write_bytes(b"\x01\x02" * 160)

        with mock.patch.dict(os.environ, self._env(), clear=False):
            staged_path, media_uri, media_type = self.speak._stage_media_source(str(source))

        self.assertEqual(staged_path.suffix, ".wav")
        self.assertEqual(media_type, "audio/wav")
        self.assertTrue(media_uri.endswith(".wav"))
        with wave.open(str(staged_path), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 16000)
            self.assertEqual(wav_file.readframes(160), source.read_bytes())

    def test_media_staging_rejects_oversized_input_before_creating_output(self):
        source = self.root / "large.wav"
        source.write_bytes(b"x")
        oversized = mock.Mock(st_mode=0o100644, st_size=self.speak.MAX_AUDIO_INPUT_BYTES + 1)

        with mock.patch.dict(os.environ, self._env(), clear=False), \
             mock.patch.object(self.speak.os, "stat", return_value=oversized):
            with self.assertRaises(RuntimeError):
                self.speak._stage_media_source(str(source))

        self.assertFalse(self.media_dir.exists())

    def test_addon_uses_media_mount_without_host_audio_access(self):
        manifest = (ROOT / "embodied_ha" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("  - media:rw\n", manifest)
        self.assertNotIn("\naudio: true\n", manifest)


if __name__ == "__main__":
    unittest.main()
