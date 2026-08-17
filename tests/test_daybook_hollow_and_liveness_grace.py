"""daybook空スタブの上書きと、liveness監視の深夜猶予窓のテスト。

背景（2026-08-14〜15の実測）:
- エージェントがMCPの build_daybook を当日日付・内容なしで呼ぶと空スタブができ、
  夜間rollupが「既存daybookあり」として再利用し、その日の実エントリが要約されず失われる
  （ある個体で6日分の発生を実測）。
- liveness監視は健全時でも日付が変わった直後（夜間rollup完了前）に gap==2 となり、
  毎晩誤警告のHA通知を出していた（3個体すべてで実測）。
"""
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))

SPEC = importlib.util.spec_from_file_location("daybook_rollup_hollow_test", EHA_DIR / "daybook_rollup.py")
assert SPEC and SPEC.loader
daybook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daybook)


def _load_daemon(name: str):
    source = (EHA_DIR / "daemon.py").read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType(name)
    module.__file__ = str(EHA_DIR / "daemon.py")
    with mock.patch.dict(os.environ, {"HA_URL": "http://supervisor/core/api"}, clear=False):
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def load_memory_mcp_module():
    path = EHA_DIR / "memory-mcp.py"
    spec = importlib.util.spec_from_file_location("memory_mcp_daybook_source_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def valid_draft() -> dict:
    return {
        "summary": "静かな一日だった。",
        "themes": ["観察"],
        "highlights": [
            {
                "summary": "窓辺を見た",
                "detail": "午後に窓辺を確認した。",
                "tags": ["日常"],
                "importance": 0.4,
            }
        ],
        "open_questions": [],
        "episodes": [],
    }


def hollow_stub(date: str) -> dict:
    """実運用で観測された空スタブと同型のdaybook。"""
    return {
        "date": date,
        "generated_at": f"{date}T17:21:02+09:00",
        "source": "loop",
        "episode_ids": [],
        "summary": "",
        "themes": [],
        "highlights": [],
        "open_questions": [],
        "importance_cutoff": 0.65,
        "raw_entry_count": 0,
        "episode_count": 0,
    }


class HollowDaybookRollupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name) / "log"
        (self.log_dir / "memory" / "daybooks").mkdir(parents=True)
        (self.log_dir / "observations.jsonl").write_text(
            json.dumps(
                {"timestamp": "2026-07-30T12:00:00+09:00", "private": "明るい。"},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.marker = self.log_dir / ".last_daybook"
        self.marker.write_text("2026-07-29", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self) -> dict:
        return {
            "LOG_FILE": str(self.log_dir / "observations.jsonl"),
            "MEMORY_FILE": str(self.log_dir / "memory.md"),
            "TODAY": "2026-07-31",
            "DAYBOOK_MARKER": str(self.marker),
            "LAST_DAYBOOK": "2026-07-29",
            "CONSOLIDATE_MEMORY": "0",
            "CHARACTER": "",
            "RESIDENT": "住人",
        }

    def test_hollow_stub_is_overwritten_by_real_summary(self):
        stub_path = self.log_dir / "memory" / "daybooks" / "2026-07-30.json"
        stub_path.write_text(json.dumps(hollow_stub("2026-07-30"), ensure_ascii=False), encoding="utf-8")
        with (
            mock.patch.dict(os.environ, self._env(), clear=False),
            mock.patch.object(daybook, "_summarize_with_agent", return_value=valid_draft()) as summarize,
        ):
            daybook.main()
        self.assertEqual(summarize.call_count, 1)
        written = json.loads(stub_path.read_text(encoding="utf-8"))
        self.assertEqual(written["summary"], "静かな一日だった。")
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "2026-07-30")

    def test_real_existing_daybook_is_still_reused(self):
        real = hollow_stub("2026-07-30")
        real["summary"] = "既にある正規の日誌。"
        path = self.log_dir / "memory" / "daybooks" / "2026-07-30.json"
        path.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
        with (
            mock.patch.dict(os.environ, self._env(), clear=False),
            mock.patch.object(
                daybook, "_summarize_with_agent",
                side_effect=AssertionError("existing daybook must be reused"),
            ),
        ):
            daybook.main()
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["summary"], "既にある正規の日誌。")
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "2026-07-30")

    def test_concurrent_real_daybook_is_not_clobbered(self):
        """要約生成中に別経路が正規の日誌を書いたら、それを壊さない。

        `_chat_lock`と`_loop_lock`は別ロックなので、rollupが`_summarize_with_agent`
        （数分）を回している間にチャット側のエージェントが同じ日の日誌を書きうる。
        置き換え判定はファイルロックの内側で行われるため、古い観測に基づいて
        上書きしてはいけない（red-team反論2・2026-08-16）。
        """
        stub_path = self.log_dir / "memory" / "daybooks" / "2026-07-30.json"
        stub_path.write_text(json.dumps(hollow_stub("2026-07-30"), ensure_ascii=False), encoding="utf-8")

        def summarize_then_someone_else_writes(day, entries):
            # 要約中に別経路が中身のある日誌を書いた状況を再現する。
            rival = hollow_stub(day)
            rival["summary"] = "別経路が書いた正規の日誌。"
            stub_path.write_text(json.dumps(rival, ensure_ascii=False), encoding="utf-8")
            return valid_draft()

        with (
            mock.patch.dict(os.environ, self._env(), clear=False),
            mock.patch.object(daybook, "_summarize_with_agent", side_effect=summarize_then_someone_else_writes),
        ):
            daybook.main()

        written = json.loads(stub_path.read_text(encoding="utf-8"))
        self.assertEqual(written["summary"], "別経路が書いた正規の日誌。")

    def test_hollow_detector_boundaries(self):
        self.assertTrue(daybook._daybook_is_hollow(hollow_stub("2026-07-30")))
        for key, value in (
            ("summary", "何かあった"),
            ("episode_ids", ["ep-1"]),
            ("highlights", [{"summary": "h"}]),
            ("themes", ["t"]),
            ("open_questions", ["q"]),
        ):
            data = hollow_stub("2026-07-30")
            data[key] = value
            self.assertFalse(daybook._daybook_is_hollow(data), key)


class LivenessGraceWindowTests(unittest.TestCase):
    """gap==2 は夜間rollup完了前の正常状態でもあるため、猶予時刻前は警告しない。"""

    @classmethod
    def setUpClass(cls):
        cls.daemon = _load_daemon("daemon_liveness_grace_test")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)
        (self.log_dir / "memory" / "daybooks").mkdir(parents=True)
        (self.log_dir / "observations.jsonl").write_text("", encoding="utf-8")
        (self.log_dir / ".last_daybook").write_text("2026-08-13", encoding="utf-8")
        (self.log_dir / "memory" / "daybooks" / "2026-08-13.json").write_text("{}\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def warning(self, now: dt.datetime):
        return self.daemon.daybook_liveness_warning(self.log_dir, now=now)

    def test_gap_two_just_after_midnight_does_not_warn(self):
        self.assertIsNone(self.warning(dt.datetime(2026, 8, 15, 0, 12)))

    def test_gap_two_before_grace_hour_does_not_warn(self):
        grace = self.daemon.DAYBOOK_LIVENESS_GRACE_HOUR
        self.assertIsNone(self.warning(dt.datetime(2026, 8, 15, grace - 1, 59)))

    def test_grace_hour_covers_observed_rollup_times_without_a_long_blind_spot(self):
        """猶予窓は実測のrollup完了時刻を覆いつつ、盲点を必要以上に広げない。

        3個体の実測は00:01〜00:48、最悪の外れ値が02:03（2026-08-16調査）。
        長すぎる猶予は本物の停止に気づくのを遅らせる（red-team反論5）。
        """
        grace = self.daemon.DAYBOOK_LIVENESS_GRACE_HOUR
        self.assertGreaterEqual(grace, 3, "実測02:03の外れ値を覆えない")
        self.assertLessEqual(grace, 6, "盲点が広すぎる")

    def test_gap_two_after_grace_hour_warns(self):
        grace = self.daemon.DAYBOOK_LIVENESS_GRACE_HOUR
        warning = self.warning(dt.datetime(2026, 8, 15, grace, 0))
        self.assertIsNotNone(warning)
        self.assertIn("2 日", warning)

    def test_gap_three_warns_even_at_midnight(self):
        warning = self.warning(dt.datetime(2026, 8, 16, 0, 5))
        self.assertIsNotNone(warning)
        self.assertIn("3 日", warning)

    def test_healthy_yesterday_marker_never_warns(self):
        self.assertIsNone(self.warning(dt.datetime(2026, 8, 14, 23, 59)))

    def test_today_only_call_treats_gap_two_as_end_of_day(self):
        """today だけ渡す既存経路は「その日の終わり」として評価され、gap==2 で警告する。"""
        warning = self.daemon.daybook_liveness_warning(self.log_dir, today=dt.date(2026, 8, 15))
        self.assertIsNotNone(warning)
        self.assertIn("2 日", warning)


class McpDaybookSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.memory_mcp = load_memory_mcp_module()
        self.memory_mcp.LOG_DIR = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def _daybook(self, result) -> dict:
        return json.loads(result[0]["text"])

    def test_default_source_is_mcp(self):
        result = self.memory_mcp.build_daybook({"date": "2026-08-14"})
        self.assertEqual(self._daybook(result)["source"], "mcp")

    def test_explicit_source_is_preserved(self):
        result = self.memory_mcp.build_daybook({"date": "2026-08-15", "source": "loop"})
        self.assertEqual(self._daybook(result)["source"], "loop")


if __name__ == "__main__":
    unittest.main()
