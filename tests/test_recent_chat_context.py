import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import recent_chat_context as rcc  # type: ignore  # noqa: E402


class RecentChatContextTests(unittest.TestCase):
    def _write_log(self, tmpdir: str, rows: list[object]) -> Path:
        path = Path(tmpdir) / "chat_log.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        return path

    def test_returns_empty_when_today_entries_are_within_tail_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            today = date.today().isoformat()
            rows = [
                {"timestamp": f"{today}T08:00:00+09:00", "user": "朝のあいさつ", "claude": "おはよう"},
                {"timestamp": f"{today}T08:10:00+09:00", "user": "朝ごはん", "claude": "わかった"},
            ]
            log_path = self._write_log(tmpdir, rows)

            self.assertEqual(rcc.format_earlier_today_chat(str(log_path), "潤哉"), "")

    def test_formats_entries_before_tail_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            today = date.today().isoformat()
            rows = []
            for index in range(12):
                rows.append(
                    {
                        "timestamp": f"{today}T08:{index:02d}:00+09:00",
                        "user": f"発言{index}",
                        "claude": f"返答{index}",
                    }
                )
            log_path = self._write_log(tmpdir, rows)

            result = rcc.format_earlier_today_chat(str(log_path), "潤哉")

            self.assertTrue(result.startswith("（今日の会話・それ以前）"))
            self.assertIn('08:00 潤哉さん: 「発言0」', result)
            self.assertIn("08:00 あかね: 返答0", result)
            self.assertIn('08:01 潤哉さん: 「発言1」', result)
            self.assertIn("08:01 あかね: 返答1", result)
            self.assertNotIn("発言10", result)
            self.assertNotIn("返答10", result)

    def test_ignores_invalid_timestamp_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            today = date.today().isoformat()
            rows = [
                {"timestamp": "not-a-timestamp", "user": "無視される", "claude": "これも無視"},
            ]
            for index in range(11):
                rows.append(
                    {
                        "timestamp": f"{today}T09:{index:02d}:00+09:00",
                        "user": f"有効{index}",
                        "claude": f"応答{index}",
                    }
                )
            log_path = self._write_log(tmpdir, rows)

            result = rcc.format_earlier_today_chat(str(log_path), "潤哉")

            self.assertNotIn("無視される", result)
            self.assertIn('09:00 潤哉さん: 「有効0」', result)
            self.assertIn("09:00 あかね: 応答0", result)

    def test_missing_file_returns_empty_string(self):
        self.assertEqual(rcc.format_earlier_today_chat("/tmp/does-not-exist.jsonl", "潤哉"), "")


if __name__ == "__main__":
    unittest.main()
