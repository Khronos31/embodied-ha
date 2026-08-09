"""Preferences Web API validation tests."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))
sys.path.insert(0, str(EHA_DIR / "web"))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import server  # noqa: E402


class PreferencesEndpointTests(unittest.TestCase):
    def _handler(self, body: object):
        raw = json.dumps(body).encode()
        handler = object.__new__(server.Handler)
        handler.path = "/api/preferences"
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.send_json = mock.Mock()
        return handler

    def test_legacy_tts_options_are_preserved_as_unmanaged_data(self):
        with tempfile.TemporaryDirectory() as temp:
            prefs_file = Path(temp) / "preferences.json"
            prefs_file.write_text(
                '{"tts_options":{"speaker":12},"keep":true}', encoding="utf-8"
            )
            handler = self._handler({"tts_entity": "tts.voicevox_tts_kotarou"})
            with mock.patch.object(server, "PREFS_FILE", str(prefs_file)):
                handler.do_PUT()

            self.assertEqual(handler.send_json.call_args.args[0], {"ok": True})
            saved = json.loads(prefs_file.read_text())
            self.assertEqual(saved["tts_entity"], "tts.voicevox_tts_kotarou")
            self.assertEqual(saved["tts_options"], {"speaker": 12})
            self.assertTrue(saved["keep"])

    def test_camera_history_options_are_validated_before_save(self):
        with tempfile.TemporaryDirectory() as temp:
            prefs_file = Path(temp) / "preferences.json"
            prefs_file.write_text('{"keep": true}', encoding="utf-8")
            valid = self._handler({
                "camera_history_enabled": True,
                "camera_history_minutes": 15,
            })
            with mock.patch.object(server, "PREFS_FILE", str(prefs_file)):
                valid.do_PUT()
            self.assertEqual(valid.send_json.call_args.args[0], {"ok": True})

            for invalid_value in (0, 61, True, "10"):
                with self.subTest(value=invalid_value):
                    invalid = self._handler({
                        "camera_history_enabled": True,
                        "camera_history_minutes": invalid_value,
                    })
                    with mock.patch.object(server, "PREFS_FILE", str(prefs_file)):
                        invalid.do_PUT()
                    self.assertEqual(invalid.send_json.call_args.args[1], 400)


if __name__ == "__main__":
    unittest.main()
