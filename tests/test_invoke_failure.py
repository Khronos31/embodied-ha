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
        self.assertEqual(invoke_failure.read_state(self.log_dir), {})

        after = invoke_failure.mark_failure(self.log_dir, source="loop")
        self.assertEqual(after["consecutive"], 1, "成功後に数え直していない")

    def test_mark_success_is_safe_when_no_state(self):
        invoke_failure.mark_success(self.log_dir)  # 例外を投げない

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

    def test_alert_message_mentions_relogin(self):
        state = invoke_failure.mark_failure(self.log_dir, source="loop", detail="定期実行")
        message = invoke_failure.alert_message(state)
        self.assertIn("再ログイン", message)
        self.assertIn("1回", message)




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
