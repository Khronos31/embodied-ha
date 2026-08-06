"""Periodic liveness checks must not depend on the autonomous-loop interval."""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")


def _load_daemon():
    path = EHA_DIR / "daemon.py"
    source = path.read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType("daemon_maintenance_test")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


class MaintenanceCheckTests(unittest.TestCase):
    def setUp(self):
        self.daemon = _load_daemon()
        self.daemon._last_daybook_liveness_check = None
        self.daemon._last_daybook_liveness_observed_warning = None
        self.daemon._last_daybook_liveness_notified_warning = None
        self.daemon._daybook_liveness_reconciled = False

    def test_loop_check_runs_each_keepalive_but_daybook_is_throttled(self):
        with mock.patch.object(self.daemon, "check_loop_failure_liveness") as loop_check, \
             mock.patch.object(self.daemon, "daybook_liveness_warning", return_value=None) as daybook, \
             mock.patch.object(
                 self.daemon, "dismiss_daybook_liveness_notification", return_value=True,
             ) as dismiss:
            for now in (0, 60, 899, 900):
                self.daemon.run_maintenance_checks(now_monotonic=now)
        self.assertEqual(loop_check.call_count, 4)
        self.assertEqual(daybook.call_count, 2)
        dismiss.assert_called_once_with()

    def test_healthy_first_check_dismisses_warning_left_by_previous_process(self):
        with mock.patch.object(self.daemon, "check_loop_failure_liveness"), \
             mock.patch.object(self.daemon, "daybook_liveness_warning", return_value=None), \
             mock.patch.object(
                 self.daemon, "dismiss_daybook_liveness_notification", return_value=True,
             ) as dismiss:
            self.daemon.run_maintenance_checks(now_monotonic=0)
            self.daemon.run_maintenance_checks(now_monotonic=900)
        dismiss.assert_called_once_with()

    def test_daybook_notification_is_deduplicated_recovered_and_rearmed(self):
        warning = "[daemon] 警告: daybook が 3 日更新されていません（保守パイプライン停止の疑い）"
        with mock.patch.object(self.daemon, "check_loop_failure_liveness"), \
             mock.patch.object(
                 self.daemon, "daybook_liveness_warning",
                 side_effect=[warning, warning, None, warning],
             ), \
             mock.patch.object(self.daemon, "notify_daybook_liveness", return_value=True) as notify, \
             mock.patch.object(
                 self.daemon, "dismiss_daybook_liveness_notification", return_value=True,
             ) as dismiss:
            for now in (0, 900, 1800, 2700):
                self.daemon.run_maintenance_checks(now_monotonic=now)
        self.assertEqual(notify.call_count, 2)
        dismiss.assert_called_once_with()

    def test_failed_daybook_notification_retries_on_next_check(self):
        warning = "[daemon] 警告: synthetic"
        with mock.patch.object(self.daemon, "check_loop_failure_liveness"), \
             mock.patch.object(self.daemon, "daybook_liveness_warning", return_value=warning), \
             mock.patch.object(
                 self.daemon, "notify_daybook_liveness", side_effect=[False, True],
             ) as notify:
            self.daemon.run_maintenance_checks(now_monotonic=0)
            self.daemon.run_maintenance_checks(now_monotonic=900)
        self.assertEqual(notify.call_count, 2)

    def test_daybook_notification_payload_is_japanese_and_fixed_id(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        warning = "[daemon] 警告: daybook が 3 日更新されていません（保守パイプライン停止の疑い）"
        with mock.patch.object(self.daemon.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertTrue(self.daemon.notify_daybook_liveness(warning))
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["notification_id"], self.daemon._DAYBOOK_LIVENESS_NOTIFICATION_ID)
        self.assertIn("日誌生成", payload["title"])
        self.assertIn("保守パイプライン停止", payload["message"])
        self.assertNotIn("[daemon]", payload["message"])

    def test_main_keepalive_calls_maintenance_checks(self):
        source = (EHA_DIR / "daemon.py").read_text(encoding="utf-8")
        tail = source.rsplit("# メインスレッドの既存keepalive", 1)[1]
        self.assertIn("run_maintenance_checks()", tail)
        self.assertIn("time.sleep(60)", tail)


if __name__ == "__main__":
    unittest.main()
