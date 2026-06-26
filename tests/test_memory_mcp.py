import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))


def load_memory_mcp_module():
    path = ROOT / "embodied_ha" / "memory-mcp.py"
    spec = importlib.util.spec_from_file_location("memory_mcp_audio_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MemoryMcpRecallAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmpdir.name)
        self.memory_mcp = load_memory_mcp_module()
        self.memory_mcp.LOG_DIR = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def _text(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["type"], "text")
        return result[0]["text"]

    def test_recall_finds_auditory_events_transcript(self):
        (self.log_dir / "auditory_events.jsonl").write_text(
            '{"timestamp":"2026-06-26T09:30:00+09:00","source":"スタディマイク","origin":"alsa://default","speaker_hint":"user","transcript":"こんにちは、朝ごはんできたよ"}\n',
            encoding="utf-8",
        )
        recall = self._text(self.memory_mcp.recall({"keywords": ["朝ごはん"]}))
        self.assertIn("[audio:heard]", recall)
        self.assertIn("スタディマイク", recall)
        self.assertIn("こんにちは、朝ごはんできたよ", recall)

    def test_recall_finds_active_listen_transcript(self):
        (self.log_dir / "active_listen_log.jsonl").write_text(
            '{"timestamp":"2026-06-26T09:35:00+09:00","actor":"explore","source":"rtsp://localhost:8554/capture_tv","source_label":"スタディ（レコーダー）","transcript":"ニュースの音声です","transcribe_requested":true}\n',
            encoding="utf-8",
        )
        recall = self._text(self.memory_mcp.recall({"keywords": ["ニュース"]}))
        self.assertIn("[audio:listened]", recall)
        self.assertIn("explore / スタディ（レコーダー）", recall)
        self.assertIn("ニュースの音声です", recall)

    def test_recall_distinguishes_heard_vs_listened_audio(self):
        (self.log_dir / "auditory_events.jsonl").write_text(
            '{"timestamp":"2026-06-26T09:30:00+09:00","source":"スタディマイク","origin":"alsa://default","speaker_hint":"user","transcript":"同じキーワードを含む声"}\n',
            encoding="utf-8",
        )
        (self.log_dir / "active_listen_log.jsonl").write_text(
            '{"timestamp":"2026-06-26T09:35:00+09:00","actor":"watch","source":"alsa://default","source_label":"スタディマイク","transcript":"同じキーワードを含む録音"}\n',
            encoding="utf-8",
        )
        recall = self._text(self.memory_mcp.recall({"keywords": ["キーワード"]}))
        self.assertIn("[audio:heard]", recall)
        self.assertIn("[audio:listened]", recall)

    def test_recall_finds_background_audio_log(self):
        (self.log_dir / "background_audio_log.jsonl").write_text(
            '{"timestamp":"2026-06-26T09:40:00+09:00","kind":"background_audio","source":"スタディ（レコーダー）","origin":"rtsp://localhost:8554/capture_tv","awareness":"background","peak_db":-28.5,"speech_ratio":0.4}\n',
            encoding="utf-8",
        )
        recall = self._text(self.memory_mcp.recall({"keywords": ["レコーダー"]}))
        self.assertIn("[audio:background]", recall)
        self.assertIn("スタディ（レコーダー）", recall)
        self.assertIn("背景音あり", recall)

    def test_memory_mcp_prefers_eha_data_dir_for_default_log_dir(self):
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": "/config/embodied-ha", "EHA_LOG_DIR": ""}, clear=False):
            module = load_memory_mcp_module()
        self.assertEqual(module.LOG_DIR, "/config/embodied-ha/log")


if __name__ == "__main__":
    unittest.main()
