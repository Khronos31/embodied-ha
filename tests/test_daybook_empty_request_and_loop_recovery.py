"""空のdaybook生成要求を入口で拒否することと、ループ回復時に通知を消すこと。

空の日誌ができると、その日の観察は「日誌あり」とみなされて要約されないまま捨てられる。
夜間rollup側でも空スタブを上書きするようにしてあるが、観察が0件の日は対象日に選ばれず
永久に残るため、作らせない側で塞ぐ必要がある。

ループ失敗の通知はHAの永続通知で、Core再起動でしか消えない。復旧しても警告が残ると
通知そのものが信用されなくなる。
"""
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


def load_memory_mcp():
    path = EHA_DIR / "memory-mcp.py"
    spec = importlib.util.spec_from_file_location("memory_mcp_empty_daybook_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_daemon(name: str):
    source = (EHA_DIR / "daemon.py").read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType(name)
    module.__file__ = str(EHA_DIR / "daemon.py")
    with mock.patch.dict(os.environ, {"HA_URL": "http://supervisor/core/api"}, clear=False):
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


class EmptyDaybookRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mcp = load_memory_mcp()
        self.mcp.LOG_DIR = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, args):
        return self.mcp.build_daybook(args)

    def _is_error(self, result):
        return isinstance(result, tuple) and result[1] is True

    def test_empty_request_is_rejected(self):
        result = self._call({"date": "2026-08-18"})
        self.assertTrue(self._is_error(result))
        self.assertFalse(self.mcp.ms.daybook_exists(self.tmp.name, "2026-08-18"))

    def test_rejection_message_points_at_the_alternative(self):
        payload, is_error = self._call({"date": "2026-08-18"})
        text = payload[0]["text"]
        self.assertIn("summary", text)
        self.assertIn("夜間", text, "自動生成されることを伝えないと手で作ろうとし続ける")
        self.assertIn("get_daybook", text, "読むだけの代替手段を示す")

    def test_blank_strings_and_empty_lists_do_not_count_as_content(self):
        result = self._call({
            "date": "2026-08-18",
            "summary": "   ",
            "episodes": [],
            "episode_ids": [],
            "themes": [],
            "highlights": [],
            "open_questions": [],
        })
        self.assertTrue(self._is_error(result))

    def test_request_with_summary_is_accepted(self):
        result = self._call({"date": "2026-08-18", "summary": "静かな一日だった。"})
        self.assertFalse(self._is_error(result))
        self.assertTrue(self.mcp.ms.daybook_exists(self.tmp.name, "2026-08-18"))

    def test_request_with_episode_ids_is_accepted(self):
        result = self._call({"date": "2026-08-18", "episode_ids": ["ep-1"]})
        self.assertFalse(self._is_error(result))

    def test_reading_back_an_existing_daybook_still_works(self):
        """既存の日誌を引くだけの空呼び出しは通す（この関数は既存があれば返す契約）。"""
        self.mcp.build_daybook({"date": "2026-08-18", "summary": "既にある日誌。"})
        result = self._call({"date": "2026-08-18"})
        self.assertFalse(self._is_error(result))
        self.assertEqual(json.loads(result[0]["text"])["summary"], "既にある日誌。")


class LoopFailureNotificationRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daemon = load_daemon("daemon_loop_recovery_test")

    def test_success_after_an_alert_dismisses_the_notification(self):
        with (
            mock.patch.object(self.daemon.invoke_failure, "read_state",
                              return_value={"alerted_at": "2026-08-17T17:42:00+09:00"}),
            mock.patch.object(self.daemon.invoke_failure, "mark_success") as mark_success,
            mock.patch.object(self.daemon, "dismiss_loop_failure_notification",
                              return_value=True) as dismiss,
        ):
            self.daemon.track_loop_outcome(True, trigger_reason="定期実行")
        mark_success.assert_called_once()
        dismiss.assert_called_once_with()

    def test_success_without_a_prior_alert_does_not_call_ha(self):
        with (
            mock.patch.object(self.daemon.invoke_failure, "read_state", return_value={"alerted_at": ""}),
            mock.patch.object(self.daemon.invoke_failure, "mark_success"),
            mock.patch.object(self.daemon, "dismiss_loop_failure_notification") as dismiss,
        ):
            self.daemon.track_loop_outcome(True, trigger_reason="定期実行")
        dismiss.assert_not_called()

    def test_dismissal_targets_the_same_id_the_alert_uses(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(self.daemon.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertTrue(self.daemon.dismiss_loop_failure_notification())
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["notification_id"], self.daemon._LOOP_FAILURE_NOTIFICATION_ID)


if __name__ == "__main__":
    unittest.main()
