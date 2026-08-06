"""daybook のマーカー進行のテスト。

守っている性質は「**マーカーだけ進んで日誌ができない状態にならない**」。

2026-07-05〜07-29 の25日間、実際にそうなっていた。`.last_daybook` は毎日 `today` へ進む一方、
日誌ファイルは `2026-07-04.json` が最後だった。しかも生存確認（`daemon.py`）が同じマーカーの
経過日数だけを見ていたため、**gap が常に0で警告が原理的に出なかった**。
"""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
SCRIPT = EHA_DIR / "daybook_rollup.py"
sys.path.insert(0, str(EHA_DIR))


def _run_rollup(env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": os.environ.get("PATH", ""), **env_extra}
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


def _load_daemon(name: str):
    source = (EHA_DIR / "daemon.py").read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType(name)
    module.__file__ = str(EHA_DIR / "daemon.py")
    with mock.patch.dict(os.environ, {"HA_URL": "http://supervisor/core/api"}, clear=False):
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


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
    """生存確認が、正常な空日と観察を失った異常なmarker先行を区別すること。"""

    @classmethod
    def setUpClass(cls):
        cls.daemon = _load_daemon("daemon_daybook_liveness_test")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)
        (self.log_dir / "memory" / "daybooks").mkdir(parents=True)
        (self.log_dir / ".last_daybook").write_text("2026-08-05", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def warning(self):
        return self.daemon.daybook_liveness_warning(self.log_dir, today=dt.date(2026, 8, 6))

    def write_observation(self, filename: str, day: str):
        (self.log_dir / filename).write_text(
            json.dumps({"timestamp": f"{day}T12:00:00+09:00", "private": "fixture"}) + "\n",
            encoding="utf-8",
        )

    def test_zero_observation_day_does_not_warn(self):
        self.assertIsNone(self.warning())

    def test_observation_without_daybook_warns(self):
        self.write_observation("observations.jsonl", "2026-08-05")
        warning = self.warning()
        self.assertIsNotNone(warning)
        self.assertIn("マーカーだけが進んでいる疑い", warning)

    def test_recovered_observation_without_daybook_warns(self):
        self.write_observation("observations_recovered.jsonl", "2026-08-05")
        self.assertIsNotNone(self.warning())

    def test_observation_on_another_day_does_not_require_marker_day_artifact(self):
        self.write_observation("observations.jsonl", "2026-08-02")
        self.assertIsNone(self.warning())

    def test_existing_daybook_suppresses_missing_artifact_warning(self):
        self.write_observation("observations.jsonl", "2026-08-05")
        (self.log_dir / "memory" / "daybooks" / "2026-08-05.json").write_text("{}\n", encoding="utf-8")
        self.assertIsNone(self.warning())

    def test_stale_marker_warning_is_unchanged_for_empty_day(self):
        warning = self.daemon.daybook_liveness_warning(self.log_dir, today=dt.date(2026, 8, 8))
        self.assertEqual(
            warning,
            "[daemon] 警告: daybook が 3 日更新されていません（保守パイプライン停止の疑い）",
        )


if __name__ == "__main__":
    unittest.main()
