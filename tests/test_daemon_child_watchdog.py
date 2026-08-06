"""Repeated audio/Web child exits must become visible without slowing recovery."""
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
    module = types.ModuleType("daemon_child_watchdog_test")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


class ChildWatchdogFailuresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daemon = _load_daemon()

    def test_alerts_on_fifth_short_failure_once_per_streak(self):
        tracker = self.daemon.ChildWatchdogFailures(threshold=5, stable_reset_seconds=600)
        decisions = [tracker.record_failure(30) for _ in range(5)]
        self.assertEqual([count for count, _ in decisions], [1, 2, 3, 4, 5])
        self.assertEqual([alert for _, alert in decisions], [False, False, False, False, True])
        tracker.mark_alerted()
        self.assertEqual(tracker.record_failure(30), (6, False))

    def test_stable_runtime_resets_streak_and_notification(self):
        tracker = self.daemon.ChildWatchdogFailures(threshold=2, stable_reset_seconds=600)
        self.assertEqual(tracker.record_failure(1), (1, False))
        self.assertEqual(tracker.record_failure(1), (2, True))
        tracker.mark_alerted()
        self.assertEqual(tracker.record_failure(600), (1, False))
        self.assertEqual(tracker.record_failure(1), (2, True))

    def test_failed_notification_can_be_retried(self):
        tracker = self.daemon.ChildWatchdogFailures(threshold=2, stable_reset_seconds=600)
        tracker.record_failure(1)
        self.assertEqual(tracker.record_failure(1), (2, True))
        self.assertEqual(tracker.record_failure(1), (3, True))

    def test_notification_is_japanese_and_uses_fixed_id(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(self.daemon.urllib.request, "urlopen", return_value=response) as urlopen:
            sent = self.daemon.notify_child_watchdog_failing(
                "音声デーモン", self.daemon._AUDIO_FAILURE_NOTIFICATION_ID, 5,
            )
        self.assertTrue(sent)
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["notification_id"], self.daemon._AUDIO_FAILURE_NOTIFICATION_ID)
        self.assertIn("5回続けて停止", payload["message"])
        self.assertIn("自動再起動", payload["message"])

    def test_watchdogs_keep_the_existing_restart_delay(self):
        self.assertEqual(self.daemon.CHILD_WATCHDOG_RESTART_DELAY, 60)

    def test_audio_watchdog_notifies_after_five_real_restart_iterations(self):
        class StopWatchdog(BaseException):
            pass

        proc = mock.MagicMock()
        proc.wait.return_value = 1
        proc.poll.return_value = 1
        monotonic = []
        for iteration in range(5):
            monotonic.extend((iteration * 10, iteration * 10 + 1))

        sleeps = []

        def stop_after_five(delay):
            sleeps.append(delay)
            if len(sleeps) == 5:
                raise StopWatchdog

        with mock.patch.object(self.daemon.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(self.daemon.time, "monotonic", side_effect=monotonic), \
             mock.patch.object(self.daemon.time, "sleep", side_effect=stop_after_five), \
             mock.patch.object(
                 self.daemon, "notify_child_watchdog_failing", return_value=True,
             ) as notify:
            with self.assertRaises(StopWatchdog):
                self.daemon.audio_daemon_watchdog()

        self.assertEqual(popen.call_count, 5)
        self.assertEqual(sleeps, [60] * 5)
        notify.assert_called_once_with(
            "音声デーモン", self.daemon._AUDIO_FAILURE_NOTIFICATION_ID, 5,
        )


if __name__ == "__main__":
    unittest.main()
