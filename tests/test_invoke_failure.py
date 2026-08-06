"""`invoke_failure` の単体テスト。

守っている性質は「認知ループが黙って止まらない」。
2026-07-27 の認証失効では invoke-agent が毎回即死しているのに検知がゼロで、
21時間後の人間の日次チェックまで誰も気づかなかった。
- 失敗は永続化されるか（アドオンログのリングバッファに頼らない）
- stderr の**先頭**が残るか（末尾だけだと原因の先頭が落ちる）
- 連続失敗が数えられ、しきい値で1度だけ通知されるか
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = str(ROOT / "embodied_ha")
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import invoke_failure


class ClipStderrTests(unittest.TestCase):
    def test_short_stderr_is_kept_whole(self):
        self.assertEqual(invoke_failure.clip_stderr("  boom  "), "boom")

    def test_long_stderr_keeps_head_and_tail(self):
        head = "A" * invoke_failure.STDERR_HEAD_CHARS
        tail = "Z" * invoke_failure.STDERR_TAIL_CHARS
        clipped = invoke_failure.clip_stderr(head + "M" * 5000 + tail)
        self.assertTrue(clipped.startswith("A"), "先頭が落ちている")
        self.assertTrue(clipped.endswith("Z"), "末尾が落ちている")
        self.assertIn("中略", clipped, "落としたことが分からない")

    def test_empty_stderr(self):
        self.assertEqual(invoke_failure.clip_stderr(""), "")
        self.assertEqual(invoke_failure.clip_stderr(None), "")


class RecordFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self):
        path = Path(self.log_dir) / invoke_failure.FAILURES_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_failure_is_persisted(self):
        invoke_failure.record_failure(
            self.log_dir, source="loop", mode="explore",
            returncode=1, stdout_empty=True, stderr="Invalid refresh token",
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "loop")
        self.assertEqual(rows[0]["mode"], "explore")
        self.assertEqual(rows[0]["returncode"], 1)
        self.assertTrue(rows[0]["stdout_empty"])
        self.assertIn("refresh token", rows[0]["stderr"])
        self.assertTrue(rows[0]["timestamp"])

    def test_failures_are_bounded(self):
        for i in range(invoke_failure.MAX_FAILURE_LINES + 20):
            invoke_failure.record_failure(self.log_dir, source="loop", stderr=f"e{i}")
        rows = self._rows()
        self.assertEqual(len(rows), invoke_failure.MAX_FAILURE_LINES)
        # 落ちるのは古い方。直近が残る。
        self.assertEqual(rows[-1]["stderr"], f"e{invoke_failure.MAX_FAILURE_LINES + 19}")

    def test_record_failure_never_raises(self):
        # 書けない場所を渡しても呼び出し元を巻き込まない
        invoke_failure.record_failure("/proc/nonexistent-dir", source="loop", stderr="x")

    def test_latest_failure_skips_malformed_rows_and_filters_source(self):
        invoke_failure.record_failure(
            self.log_dir, source="chat", mode="text", returncode=2, harness="agy",
        )
        invoke_failure.record_failure(
            self.log_dir, source="loop", mode="reflect", returncode=1,
            stdout_empty=True, stderr="private diagnostic", harness="codex",
        )
        path = Path(self.log_dir) / invoke_failure.FAILURES_FILE
        with path.open("a", encoding="utf-8") as f:
            f.write("not-json\n")

        latest = invoke_failure.read_latest_failure(self.log_dir, source="loop")
        self.assertEqual(latest["mode"], "reflect")
        self.assertEqual(latest["harness"], "codex")
        self.assertEqual(
            invoke_failure.read_latest_failure(self.log_dir, source="chat")["mode"],
            "text",
        )
        self.assertEqual(invoke_failure.read_latest_failure(self.log_dir, source="missing"), {})
        self.assertEqual(
            invoke_failure.read_latest_failure(
                self.log_dir,
                source="loop",
                since="2999-01-01T00:00:00+09:00",
            ),
            {},
            "以前の失敗ストリークを通知へ再利用している",
        )


class ConsecutiveCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_up_and_resets_on_success(self):
        first = invoke_failure.mark_failure(self.log_dir, source="loop")
        self.assertEqual(first["consecutive"], 1)
        second = invoke_failure.mark_failure(self.log_dir, source="loop")
        self.assertEqual(second["consecutive"], 2)
        self.assertEqual(second["first_failed_at"], first["first_failed_at"], "最初の失敗時刻が動いている")

        invoke_failure.mark_success(self.log_dir)
        success = invoke_failure.read_state(self.log_dir)
        self.assertEqual(success["consecutive"], 0)
        self.assertTrue(success["last_success_at"])

        after = invoke_failure.mark_failure(self.log_dir, source="loop")
        self.assertEqual(after["consecutive"], 1, "成功後に数え直していない")

    def test_mark_success_is_safe_when_no_state(self):
        invoke_failure.mark_success(self.log_dir)  # 例外を投げず成功時刻を作る
        self.assertTrue(invoke_failure.read_state(self.log_dir)["last_success_at"])

    def test_failure_preserves_last_success_for_time_based_alert(self):
        invoke_failure.mark_success(self.log_dir)
        last_success = invoke_failure.read_state(self.log_dir)["last_success_at"]
        failed = invoke_failure.mark_failure(self.log_dir, source="loop")
        self.assertEqual(failed["last_success_at"], last_success)

    def test_alerts_after_silence_only_during_an_active_failure_streak(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        healthy = {
            "consecutive": 0,
            "last_success_at": (now - timedelta(hours=8)).isoformat(),
            "alerted_at": "",
        }
        self.assertFalse(invoke_failure.should_alert(
            healthy, threshold=3, max_silence_seconds=4 * 3600, now=now,
        ))

        failed = {
            **healthy,
            "consecutive": 1,
            "first_failed_at": (now - timedelta(minutes=5)).isoformat(),
        }
        self.assertTrue(invoke_failure.should_alert(
            failed, threshold=3, max_silence_seconds=4 * 3600, now=now,
        ))

    def test_first_failure_is_time_reference_when_success_is_unknown(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        failed = {
            "consecutive": 1,
            "first_failed_at": (now - timedelta(hours=4)).isoformat(),
            "last_success_at": "",
            "alerted_at": "",
        }
        self.assertTrue(invoke_failure.should_alert(
            failed, threshold=3, max_silence_seconds=4 * 3600, now=now,
        ))

    def test_alert_fires_once_per_streak(self):
        threshold = 3
        states = [invoke_failure.mark_failure(self.log_dir, source="loop") for _ in range(threshold)]
        self.assertFalse(invoke_failure.should_alert(states[0], threshold=threshold))
        self.assertTrue(invoke_failure.should_alert(states[-1], threshold=threshold))

        invoke_failure.mark_alerted(self.log_dir)
        again = invoke_failure.mark_failure(self.log_dir, source="loop")
        self.assertFalse(invoke_failure.should_alert(again, threshold=threshold), "同じ連続失敗で再通知している")

        # 成功をはさめば、次の連続失敗ではまた通知される
        invoke_failure.mark_success(self.log_dir)
        fresh = [invoke_failure.mark_failure(self.log_dir, source="loop") for _ in range(threshold)]
        self.assertTrue(invoke_failure.should_alert(fresh[-1], threshold=threshold))

    def test_threshold_from_env(self):
        self.assertEqual(invoke_failure.alert_threshold({}), invoke_failure.DEFAULT_ALERT_THRESHOLD)
        self.assertEqual(invoke_failure.alert_threshold({"EHA_INVOKE_FAILURE_ALERT_THRESHOLD": "5"}), 5)
        self.assertEqual(invoke_failure.alert_threshold({"EHA_INVOKE_FAILURE_ALERT_THRESHOLD": "0"}), 1)
        self.assertEqual(
            invoke_failure.alert_threshold({"EHA_INVOKE_FAILURE_ALERT_THRESHOLD": "not-a-number"}),
            invoke_failure.DEFAULT_ALERT_THRESHOLD,
        )

    def test_alert_message_uses_safe_failure_summary(self):
        state = invoke_failure.mark_failure(
            self.log_dir,
            source="loop",
            detail="定期実行（15分間隔）\n二行目",
        )
        message = invoke_failure.alert_message(state, failure={
            "harness": "codex",
            "mode": "explore",
            "returncode": 1,
            "stdout_empty": True,
            "stderr": "Invalid refresh token: secret-value",
        })
        self.assertIn("再ログイン", message)
        self.assertIn("1回", message)
        self.assertIn("ハーネス=codex", message)
        self.assertIn("モード=explore", message)
        self.assertIn("終了コード=1", message)
        self.assertIn("標準出力=空", message)
        self.assertIn("起動理由: 定期実行（15分間隔） 二行目", message)
        self.assertNotIn("直近のエラー", message)
        self.assertNotIn("secret-value", message)

    def test_alert_message_works_without_failure_record(self):
        state = invoke_failure.mark_failure(self.log_dir, source="loop")
        message = invoke_failure.alert_message(state)
        self.assertIn("実行基盤", message)
        self.assertNotIn("直近の失敗:", message)
        self.assertNotIn("起動理由:", message)




class LoopWiringTests(unittest.TestCase):
    """loop.py が実際に失敗記録を書くか（配線の裏付け）。

    2026-07-27 の停止では、失敗そのものは起きていたのに永続記録がどこにも残らず、
    翌日に原因を辿れなかった。ここで「落ちたら残る」を固定する。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_invoke_agent_failure_is_recorded(self):
        import loop

        class Result:
            stdout = ""
            stderr = "A" * 6000 + "Invalid refresh token"
            returncode = 1

        def fake_run(cmd, **kwargs):
            return Result()

        with self.assertRaises(loop.InvokeAgentError):
            loop.invoke_loop_claude(
                user_prompt="u", system_prompt="s", mode="explore",
                allowed_tools="", mcp_servers=[],
                environ={
                    "SCRIPT_DIR": str(ROOT / "embodied_ha"),
                    "EHA_DATA_DIR": self.tmp.name,
                    "EHA_LOG_DIR": self.tmp.name,
                },
                run=fake_run,
            )

        path = Path(self.tmp.name) / invoke_failure.FAILURES_FILE
        self.assertTrue(path.exists(), "失敗が永続化されていない")
        row = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(row["source"], "loop")
        self.assertEqual(row["mode"], "explore")
        self.assertEqual(row["returncode"], 1)
        self.assertTrue(row["stdout_empty"])
        # 末尾だけ残す実装では原因（末尾）は拾えるが先頭が落ちる。両方あることを確認する。
        self.assertTrue(row["stderr"].startswith("A"), "stderr の先頭が落ちている")
        self.assertIn("Invalid refresh token", row["stderr"], "stderr の末尾が落ちている")


if __name__ == "__main__":
    unittest.main()
