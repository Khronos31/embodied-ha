"""VOICEVOX TTS option validation and Web API tests."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))
sys.path.insert(0, str(EHA_DIR / "web"))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import server  # noqa: E402
import tts_options  # noqa: E402


class TtsOptionsValidationTests(unittest.TestCase):
    def test_valid_values_are_normalized(self):
        self.assertEqual(
            tts_options.validate_tts_options({
                "speaker": 56, "volume": 1, "pitch": 0, "speed": 1.25,
            }),
            {"speaker": 56, "volume": 1.0, "pitch": 0.0, "speed": 1.25},
        )

    def test_invalid_values_are_rejected(self):
        invalid = [
            "bad",
            {"speaker": True},
            {"speaker": -1},
            {"volume": 1.2},
            {"volume": 2.01},
            {"pitch": -0.151},
            {"speed": float("nan")},
            {"unknown": 1},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                tts_options.validate_tts_options(value)

    def test_runtime_normalizer_ignores_invalid_object(self):
        self.assertEqual(tts_options.normalize_tts_options({"speaker": -1}), {})

    def test_voicevox_provider_names(self):
        self.assertTrue(tts_options.is_voicevox_provider("tts.voicevox_tts"))
        self.assertTrue(tts_options.is_voicevox_provider("voicevox_tts"))
        self.assertFalse(tts_options.is_voicevox_provider("tts.home_assistant_cloud"))


class PreferencesEndpointTests(unittest.TestCase):
    def _handler(self, body: object):
        raw = json.dumps(body).encode()
        handler = object.__new__(server.Handler)
        handler.path = "/api/preferences"
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.send_json = mock.Mock()
        return handler

    def test_valid_options_are_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            prefs_file = str(Path(temp) / "preferences.json")
            handler = self._handler({"tts_options": {"speaker": 12, "volume": 1.0}})
            with mock.patch.object(server, "PREFS_FILE", prefs_file):
                handler.do_PUT()
            self.assertEqual(handler.send_json.call_args.args[0], {"ok": True})
            self.assertEqual(json.loads(Path(prefs_file).read_text())["tts_options"]["speaker"], 12)

    def test_invalid_options_return_400_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            prefs_file = Path(temp) / "preferences.json"
            prefs_file.write_text('{"keep": true}', encoding="utf-8")
            handler = self._handler({"tts_options": {"volume": 20}})
            with mock.patch.object(server, "PREFS_FILE", str(prefs_file)):
                handler.do_PUT()
            self.assertEqual(handler.send_json.call_args.args[1], 400)
            self.assertEqual(json.loads(prefs_file.read_text()), {"keep": True})


class VoicevoxSpeakersTests(unittest.TestCase):
    def test_normalizes_speaker_styles(self):
        payload = [
            {"name": "猫使アル", "styles": [
                {"name": "ノーマル", "id": 55},
                {"name": "おちつき", "id": 56},
            ]},
        ]
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch.object(server, "_voicevox_addon_hostname", return_value="voicevox-host"), \
             mock.patch.object(server.urllib.request, "urlopen", return_value=response) as urlopen:
            result = server.get_voicevox_speakers()
        self.assertEqual(result[1], {
            "name": "猫使アル", "style_name": "おちつき", "speaker": 56,
        })
        self.assertEqual(urlopen.call_args.args[0].full_url, "http://voicevox-host:50021/speakers")

    def test_addon_hostname_comes_from_supervisor_info(self):
        def fake_supervisor(path, timeout=5):
            if path == "/addons":
                return {"addons": [
                    {"slug": "other", "name": "Other"},
                    {"slug": "974e6a09_voicevox_engine_addon", "name": "VOICEVOX Engine add-on"},
                ]}
            return {"hostname": "974e6a09-voicevox-engine-addon"}

        with mock.patch.object(server, "_supervisor_json", side_effect=fake_supervisor):
            self.assertEqual(
                server._voicevox_addon_hostname(),
                "974e6a09-voicevox-engine-addon",
            )

    def test_addon_hostname_falls_back_when_supervisor_forbids_listing(self):
        forbidden = urllib.error.HTTPError(
            "http://supervisor/addons", 403, "Forbidden", {}, None,
        )
        with mock.patch.object(server, "_supervisor_json", side_effect=forbidden):
            self.assertEqual(
                server._voicevox_addon_hostname(),
                "974e6a09-voicevox-engine-addon",
            )


class PreferenceSamplesTests(unittest.TestCase):
    def test_tts_options_are_synchronized(self):
        root_sample = json.loads((ROOT / "preferences.json.example").read_text())
        package_sample = json.loads((EHA_DIR / "preferences.json.example").read_text())
        for key in ("_tts_options_doc", "tts_options"):
            self.assertEqual(root_sample[key], package_sample[key])


if __name__ == "__main__":
    unittest.main()
