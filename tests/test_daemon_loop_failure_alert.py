"""daemon 側の「連続失敗を数えて通知する」配線のテスト。

`invoke_failure` の単体テストは `tests/test_invoke_failure.py`。ここは
**daemon がループの成否をそこへ流しているか**と、**同じ連続失敗で通知を繰り返さないか**を見る。
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import invoke_failure


def _load_daemon_without_boot(log_dir):
    path = ROOT / "embodied_ha" / "daemon.py"
    source = path.read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType("daemon_loop_failure_test")
    module.__file__ = str(path)
    previous = os.environ.get("EHA_LOG_DIR")
    os.environ["EHA_LOG_DIR"] = log_dir
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        if previous is None:
            os.environ.pop("EHA_LOG_DIR", None)
        else:
            os.environ["EHA_LOG_DIR"] = previous
    return module


class TrackLoopOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = self.tmp.name
        self.daemon = _load_daemon_without_boot(self.log_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_clears_the_streak(self):
        self.daemon.track_loop_outcome(False, trigger_reason="定期実行")
        self.assertEqual(invoke_failure.read_state(self.log_dir).get("consecutive"), 1)
        self.daemon.track_loop_outcome(True, trigger_reason="定期実行")
        self.assertEqual(invoke_failure.read_state(self.log_dir), {})

    def test_notifies_once_after_threshold(self):
        threshold = invoke_failure.alert_threshold()
        with mock.patch.object(self.daemon, "notify_loop_failing", return_value=True) as notify:
            for _ in range(threshold + 3):
                self.daemon.track_loop_outcome(False, trigger_reason="定期実行")
        self.assertEqual(notify.call_count, 1, "同じ連続失敗で通知を繰り返している")

    def test_notifies_again_after_recovery_and_new_streak(self):
        threshold = invoke_failure.alert_threshold()
        with mock.patch.object(self.daemon, "notify_loop_failing", return_value=True) as notify:
            for _ in range(threshold):
                self.daemon.track_loop_outcome(False, trigger_reason="定期実行")
            self.daemon.track_loop_outcome(True, trigger_reason="定期実行")
            for _ in range(threshold):
                self.daemon.track_loop_outcome(False, trigger_reason="定期実行")
        self.assertEqual(notify.call_count, 2, "復旧後の新しい連続失敗で通知していない")

    def test_alert_is_retried_when_notification_fails(self):
        # 通知の送信に失敗したら「通知済み」にしない——次の失敗でまた試す
        threshold = invoke_failure.alert_threshold()
        with mock.patch.object(self.daemon, "notify_loop_failing", return_value=False) as notify:
            for _ in range(threshold + 1):
                self.daemon.track_loop_outcome(False, trigger_reason="定期実行")
        self.assertEqual(notify.call_count, 2)

    def test_tracking_error_does_not_propagate(self):
        with mock.patch.object(self.daemon.invoke_failure, "mark_failure", side_effect=OSError("disk full")):
            self.daemon.track_loop_outcome(False, trigger_reason="定期実行")  # 例外を投げない

    def test_notification_uses_latest_structured_failure_without_stderr(self):
        invoke_failure.record_failure(
            self.log_dir,
            source="loop",
            mode="observe",
            returncode=1,
            stdout_empty=True,
            stderr="Invalid refresh token: secret-value",
            harness="claude",
        )
        state = invoke_failure.mark_failure(
            self.log_dir,
            source="loop",
            detail="定期実行（15分間隔）",
        )

        with mock.patch.object(self.daemon.urllib.request, "urlopen") as urlopen:
            self.assertTrue(self.daemon.notify_loop_failing(state))

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        message = payload["message"]
        self.assertIn("ハーネス=claude", message)
        self.assertIn("モード=observe", message)
        self.assertIn("終了コード=1", message)
        self.assertIn("標準出力=空", message)
        self.assertIn("起動理由: 定期実行（15分間隔）", message)
        self.assertNotIn("secret-value", message)
        self.assertNotIn("直近のエラー", message)


if __name__ == "__main__":
    unittest.main()
