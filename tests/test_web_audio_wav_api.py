"""録音WAV配信APIのパス境界テスト。"""
import io
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

import server


class AudioWavApiTests(unittest.TestCase):
    def _handler(self, event_id: str):
        handler = object.__new__(server.Handler)
        handler.path = f"/api/audio-events/{event_id}/wav"
        handler.client_address = ("172.30.32.2", 0)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.send_error = mock.Mock()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        return handler

    def test_serves_regular_wav_inside_wav_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_dir = Path(tmpdir) / "wav"
            wav_dir.mkdir()
            wav_path = wav_dir / "event-ok.wav"
            wav_path.write_bytes(b"RIFF-safe-wav")
            handler = self._handler("event-ok")

            with mock.patch.object(server, "WAV_DIR", str(wav_dir)), mock.patch.object(
                server,
                "read_jsonl",
                return_value=[{"event_id": "event-ok", "wav_ref": str(wav_path)}],
            ):
                handler.do_GET()

        handler.send_error.assert_not_called()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), b"RIFF-safe-wav")

    def test_rejects_symlink_that_resolves_outside_wav_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wav_dir = root / "wav"
            wav_dir.mkdir()
            outside = root / "outside.json"
            outside.write_text('{"secret": true}', encoding="utf-8")
            link = wav_dir / "event-link.wav"
            link.symlink_to(outside)
            handler = self._handler("event-link")

            with mock.patch.object(server, "WAV_DIR", str(wav_dir)), mock.patch.object(
                server,
                "read_jsonl",
                return_value=[{"event_id": "event-link", "wav_ref": str(link)}],
            ):
                handler.do_GET()

        handler.send_error.assert_called_once_with(403, "Forbidden")
        handler.send_response.assert_not_called()
        self.assertEqual(handler.wfile.getvalue(), b"")


if __name__ == "__main__":
    unittest.main()
