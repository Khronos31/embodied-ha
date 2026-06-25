import importlib.util
import json
import os
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
    spec.loader.exec_module(module)
    return module


class AudioMcpTests(unittest.TestCase):
    def setUp(self):
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
        cmd = self.audio_mcp.build_record_command("alsa", 7)
        self.assertEqual(cmd[:5], ["ffmpeg", "-f", "alsa", "-i", "default"])
        self.assertIn("7", cmd)

    def test_listen_returns_ffmpeg_missing_error(self):
        with mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value=None):
            payload = self._json(self.audio_mcp.listen({}))
        self.assertEqual(payload["error"], "ffmpeg not found")

    def test_listen_go2rtc_without_stt(self):
        responses = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr="mean_volume: -28.1 dB\nmax_volume: -12.3 dB\n"),
        ]
        with mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), \
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
        with mock.patch.object(self.audio_mcp, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(self.audio_mcp.subprocess, "run", side_effect=responses) as run_mock:
            payload = self._json(self.audio_mcp.listen({"source": "alsa", "duration": 3}))

        first_cmd = run_mock.call_args_list[0].args[0]
        self.assertEqual(first_cmd[:5], ["/usr/bin/ffmpeg", "-f", "alsa", "-i", "default"])
        self.assertFalse(payload["has_sound"])

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


if __name__ == "__main__":
    unittest.main()
