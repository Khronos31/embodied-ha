import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
os.environ.setdefault("HA_URL", "http://example.invalid")

import anomaly_state  # type: ignore  # noqa: E402
import body_state  # type: ignore  # noqa: E402


def _load_daemon_without_boot():
    path = ROOT / "embodied_ha" / "daemon.py"
    source = path.read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType("daemon_trigger_kind_test")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


class DaemonTriggerKindTests(unittest.TestCase):
    def setUp(self):
        self.daemon = _load_daemon_without_boot()

    def test_loop_trigger_classifies_button_and_legacy_custom_text(self):
        for payload in ("", "LOOP"):
            with self.subTest(payload=payload), mock.patch.object(self.daemon, "run_loop") as run_loop:
                self.daemon.on_loop_trigger(payload)
                run_loop.assert_called_once_with(
                    "手動実行",
                    mode="observe",
                    trigger_kind=body_state.TriggerKind.MANUAL,
                )

        with mock.patch.object(self.daemon, "run_loop") as run_loop:
            self.daemon.on_loop_trigger("玄関のドアが開いた")
            run_loop.assert_called_once_with(
                "玄関のドアが開いた",
                mode="observe",
                trigger_kind=body_state.TriggerKind.EXTERNAL,
            )

    def test_tick_body_state_forwards_kind_without_changing_log_reason(self):
        def update_state(_path, updater):
            return updater(body_state.normalize_state(None))

        with (
            mock.patch.object(self.daemon.body_state, "update_state", side_effect=update_state),
            mock.patch.object(self.daemon, "_log_body_state") as log_body_state,
        ):
            self.daemon.tick_body_state(
                "loop",
                "定期実行（30分間隔）",
                [],
                trigger_kind=body_state.TriggerKind.SCHEDULED,
            )

        log_body_state.assert_called_once()
        self.assertEqual(log_body_state.call_args.kwargs["trigger_kind"], "scheduled")
        self.assertEqual(log_body_state.call_args.kwargs["reason"], "定期実行（30分間隔）")

    def test_chat_is_an_explicit_user_trigger(self):
        state = body_state.normalize_state(None)
        with (
            mock.patch.object(self.daemon, "mqtt_pub"),
            mock.patch.object(self.daemon, "_load_body_state", return_value=state),
            mock.patch.object(self.daemon, "tick_desires", return_value=([], 0.0)),
            mock.patch.object(self.daemon, "tick_body_state", return_value=state) as tick_body_state,
            mock.patch.object(self.daemon.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
            mock.patch.object(self.daemon, "finish_body_state"),
        ):
            self.daemon.run_chat("確認して")

        tick_body_state.assert_called_once_with(
            "chat",
            "会話:確認して",
            [],
            trigger_kind=body_state.TriggerKind.USER,
        )

    def test_scheduler_marks_pre_run_body_tick_as_scheduled(self):
        schedule = {"loop_interval": 1800}
        body = body_state.normalize_state(None)
        anomaly = anomaly_state.normalize_state(None)
        with (
            mock.patch.object(self.daemon, "load_schedule", return_value=schedule),
            mock.patch.object(self.daemon.time, "sleep", side_effect=[None, StopIteration]),
            mock.patch.object(self.daemon, "_load_body_state", return_value=body),
            mock.patch.object(self.daemon, "_load_anomaly_state", return_value=anomaly),
            mock.patch.object(self.daemon, "tick_desires", return_value=([], 0.0)),
            mock.patch.object(self.daemon, "tick_body_state", return_value=body) as tick_body_state,
            mock.patch.object(self.daemon, "run_chance", return_value=0),
            mock.patch.object(self.daemon.random, "randint", return_value=100),
        ):
            with self.assertRaises(StopIteration):
                self.daemon.loop_scheduler()

        tick_body_state.assert_called_once_with(
            "loop",
            "定期実行（30分間隔）",
            [],
            trigger_kind=body_state.TriggerKind.SCHEDULED,
        )

    def test_run_loop_preserves_reason_for_subprocess_and_failure_tracking(self):
        reason = "玄関のドアが開いた"
        body = body_state.normalize_state(None)
        anomaly = anomaly_state.normalize_state(None)
        with (
            mock.patch.object(self.daemon, "_log_body_state"),
            mock.patch.object(self.daemon, "_load_anomaly_state", return_value=anomaly),
            mock.patch.object(
                self.daemon.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
            mock.patch.object(self.daemon, "track_loop_outcome") as track_loop_outcome,
            mock.patch.object(self.daemon, "finish_body_state"),
        ):
            self.daemon.run_loop(
                reason,
                body_state_snapshot=body,
                trigger_kind=body_state.TriggerKind.EXTERNAL,
            )

        self.assertEqual(run.call_args.kwargs["env"]["TRIGGER_REASON"], reason)
        track_loop_outcome.assert_called_once_with(True, trigger_reason=reason)


if __name__ == "__main__":
    unittest.main()
