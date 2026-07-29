"""daybook のマーカー進行のテスト。

守っている性質は「**マーカーだけ進んで日誌ができない状態にならない**」。

2026-07-05〜07-29 の25日間、実際にそうなっていた。`.last_daybook` は毎日 `today` へ進む一方、
日誌ファイルは `2026-07-04.json` が最後だった。しかも生存確認（`daemon.py`）が同じマーカーの
経過日数だけを見ていたため、**gap が常に0で警告が原理的に出なかった**。
"""
import datetime as dt
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "embodied_ha" / "daybook_rollup.py"


def _run_rollup(env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": os.environ.get("PATH", ""), **env_extra}
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


class MarkerAdvanceTests(unittest.TestCase):
    """`main()` の「やることが無い」分岐がマーカーをどこへ進めるか。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)
        (self.log_dir / "memory").mkdir()
        self.marker = self.log_dir / ".last_daybook"
        self.observation_log = self.log_dir / "observations.jsonl"
        self.observation_log.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, today: str, last: str) -> dict:
        return {
            "LOG_FILE": str(self.observation_log),
            "MEMORY_FILE": str(self.log_dir / "memory.md"),
            "TODAY": today,
            "DAYBOOK_MARKER": str(self.marker),
            "LAST_DAYBOOK": last,
            "SCRIPT_DIR": str(ROOT / "embodied_ha"),
            "CHARACTER": "",
            "RESIDENT": "ゆの",
        }

    def test_nothing_to_do_marks_yesterday_not_today(self):
        """昨日まで済んでいるとき、マーカーは昨日のまま。today を書くと自己永続化する。"""
        _run_rollup(self._env(today="2026-07-29", last="2026-07-28"))
        self.assertEqual(self.marker.read_text(encoding="utf-8").strip(), "2026-07-28")

    def test_marker_does_not_run_away_over_days(self):
        """何日連続で空振りしてもマーカーが未来へ走らないこと（これが25日間の空振りの正体）。"""
        last = "2026-07-28"
        for offset in range(5):
            today = (dt.date(2026, 7, 29) + dt.timedelta(days=offset)).isoformat()
            _run_rollup(self._env(today=today, last=last))
            written = self.marker.read_text(encoding="utf-8").strip()
            expected = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
            self.assertEqual(written, expected, f"{today} 時点でマーカーが {written} になっている")
            self.assertLess(written, today, "マーカーが今日以降を指している")
            last = written

    def test_marker_never_points_at_the_future(self):
        _run_rollup(self._env(today="2026-07-29", last="2026-07-30"))
        written = self.marker.read_text(encoding="utf-8").strip()
        self.assertLess(written, "2026-07-29", "マーカーが未来を指した")


class LivenessCheckTests(unittest.TestCase):
    """生存確認が「マーカーの経過日数」だけでなく「日誌ファイルの実在」も見ること。"""

    def _source(self) -> str:
        return (ROOT / "embodied_ha" / "daemon.py").read_text(encoding="utf-8")

    def test_detector_checks_the_artifact_not_only_the_marker(self):
        src = self._source()
        marker_block = src[src.index("保守パイプラインの生存確認"):]
        marker_block = marker_block[: marker_block.index("メインスレッドを生かし続ける")]
        self.assertIn("daybooks", marker_block,
                      "マーカーの経過日数しか見ておらず、マーカーだけ進む故障を検知できない")
        self.assertIn("os.path.exists", marker_block)


if __name__ == "__main__":
    unittest.main()
