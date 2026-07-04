import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import boundary  # type: ignore  # noqa: E402
import sociality_state as ss  # type: ignore  # noqa: E402


class BoundaryTests(unittest.TestCase):
    def test_quiet_hours_speak_is_blocked(self):
        result = boundary.check(
            mode="watch",
            intent="speak",
            hour=2,
            is_autonomous=True,
            presence={"resident": True, "guest": False},
            policies=["深夜1〜6時は発話しない"],
            metadata={},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "深夜帯（1-6時）のため発話抑制")
        self.assertIsNone(result["fallback"])

    def test_quiet_hours_action_is_allowed_if_autonomous_and_home(self):
        result = boundary.check(
            mode="explore",
            intent="action",
            hour=2,
            is_autonomous=True,
            presence={"resident": True},
            policies=[],
            metadata={},
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "許可")
        self.assertIsNone(result["fallback"])

    def test_normal_time_speak_is_allowed(self):
        result = boundary.check(
            mode="chat",
            intent="speak",
            hour=10,
            is_autonomous=False,
            presence={"resident": False},
            policies=[],
            metadata={},
        )
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["fallback"])

    def test_unknown_policies_do_not_block(self):
        result = boundary.check(
            mode="watch",
            intent="speak",
            hour=10,
            is_autonomous=True,
            presence={"resident": True},
            policies=["カスタムルール: 玄関では元気に挨拶する"],
            metadata={"room": "study"},
        )
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["fallback"])

    def test_action_is_blocked_when_not_autonomous(self):
        result = boundary.check(
            mode="explore",
            intent="action",
            hour=10,
            is_autonomous=False,
            presence={"resident": True},
            policies=[],
            metadata={},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "自律操作OFFのため家電操作しない")

    def test_action_is_blocked_when_no_one_home(self):
        result = boundary.check(
            mode="explore",
            intent="action",
            hour=10,
            is_autonomous=True,
            presence={"resident": False},
            policies=[],
            metadata={},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "不在のため家電操作を抑制")

    def test_policies_can_be_loaded_from_prefs_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(
                json.dumps(
                    {
                        "presence": {"entity": "input_boolean.resident_home"},
                        "policies": ["深夜は発話しない"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            policies = boundary._load_policies(str(prefs))
            self.assertEqual(policies, ["深夜は発話しない"])

    def _write_presence_prefs(self, tmpdir: str, include_sensor_label: bool = True) -> Path:
        prefs = Path(tmpdir) / "preferences.json"
        data = {"presence": {"entity": "input_boolean.junya_home"}}
        if include_sensor_label:
            data["sensors"] = {
                "groups": [
                    {
                        "items": [
                            {"label": "潤哉", "entity": "input_boolean.junya_home"},
                        ],
                    },
                ],
            }
        prefs.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return prefs

    def _action_decision_from_prefs(self, prefs: Path, sensors_text: str, sociality_log_dir: str) -> dict:
        args = boundary.parse_args(
            [
                "--mode",
                "explore",
                "--intent",
                "action",
                "--hour",
                "14",
                "--autonomous",
                "1",
                "--prefs-file",
                str(prefs),
                "--sensors-text",
                sensors_text,
                "--person",
                "ゆの",
                "--body-state-json",
                "{}",
                "--sociality-log-dir",
                sociality_log_dir,
            ]
        )
        loaded_prefs = boundary._load_prefs(args.prefs_file)
        presence = boundary._load_presence(args, loaded_prefs)
        return boundary.check(
            mode=args.mode,
            intent=args.intent,
            hour=args.hour,
            is_autonomous=args.autonomous,
            presence=presence,
            policies=[],
            metadata={},
            person=args.person,
            body_state={},
            sociality_log_dir=args.sociality_log_dir,
        )

    def test_presence_entity_label_allows_action_when_sensor_label_is_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = self._write_presence_prefs(tmpdir)
            with patch.dict(os.environ, {"RESIDENT": "ゆの", "SENSORS_DATA": ""}):
                result = self._action_decision_from_prefs(
                    prefs,
                    "## 在宅状態\n潤哉: on\nまどか: off",
                    tmpdir,
                )
            self.assertTrue(result["allowed"])
            self.assertEqual(result["reason"], "許可")

    def test_presence_entity_label_blocks_action_when_sensor_label_is_away(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = self._write_presence_prefs(tmpdir)
            with patch.dict(os.environ, {"RESIDENT": "ゆの", "SENSORS_DATA": ""}):
                result = self._action_decision_from_prefs(
                    prefs,
                    "## 在宅状態\n潤哉: off\nまどか: off",
                    tmpdir,
                )
            self.assertFalse(result["allowed"])
            self.assertEqual(result["reason"], "不在のため家電操作を抑制")

    def test_presence_entity_pointer_without_live_state_is_not_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = self._write_presence_prefs(tmpdir, include_sensor_label=False)
            with patch.dict(os.environ, {"RESIDENT": "ゆの", "SENSORS_DATA": ""}):
                result = self._action_decision_from_prefs(prefs, "", tmpdir)
            self.assertFalse(result["allowed"])
            self.assertEqual(result["reason"], "不在のため家電操作を抑制")

    def test_quiet_window_blocks_spontaneous_speak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ss.record_boundary(
                tmpdir,
                "alice",
                {"quiet_window": {"active": True, "start": "22:00", "end": "07:00"}},
            )
            result = boundary.check(
                mode="watch",
                intent="speak",
                hour=23,
                is_autonomous=True,
                presence={"resident": True},
                policies=[],
                metadata={},
                person="alice",
                sociality_log_dir=tmpdir,
            )
            self.assertFalse(result["allowed"])
            self.assertEqual(result["reason"], "quiet_window")

    def test_direct_call_overrides_quiet_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ss.record_boundary(
                tmpdir,
                "alice",
                {"quiet_window": {"active": True, "start": "22:00", "end": "07:00"}},
            )
            result = boundary.check(
                mode="watch",
                intent="speak",
                hour=23,
                is_autonomous=True,
                presence={"resident": True},
                policies=[],
                metadata={"direct": True},
                person="alice",
                sociality_log_dir=tmpdir,
            )
            self.assertTrue(result["allowed"])
            self.assertEqual(result["reason"], "direct_override")

    def test_urgent_override_overrides_quiet_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ss.record_boundary(
                tmpdir,
                "alice",
                {"quiet_window": {"active": True, "start": "22:00", "end": "07:00"}},
            )
            result = boundary.check(
                mode="explore",
                intent="action",
                hour=23,
                is_autonomous=True,
                presence={"resident": True},
                policies=[],
                metadata={"urgent": True},
                person="alice",
                sociality_log_dir=tmpdir,
            )
            self.assertTrue(result["allowed"])
            self.assertEqual(result["reason"], "urgent_override")

    def test_turn_taking_blocks_background_speak_until_updated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ss.record_boundary(
                tmpdir,
                "alice",
                {"turn_taking": {"state": "waiting", "awaiting_reply": True, "cooldown_seconds": 120}},
            )
            blocked = boundary.check(
                mode="watch",
                intent="speak",
                hour=12,
                is_autonomous=True,
                presence={"resident": True},
                policies=[],
                metadata={},
                person="alice",
                sociality_log_dir=tmpdir,
            )
            self.assertFalse(blocked["allowed"])
            self.assertEqual(blocked["reason"], "turn_taking")

            ss.update_turn_taking(tmpdir, "alice", speaker="resident", kind="question", text="今いい?")
            refreshed = boundary.check(
                mode="watch",
                intent="speak",
                hour=12,
                is_autonomous=True,
                presence={"resident": True},
                policies=[],
                metadata={"direct": True},
                person="alice",
                sociality_log_dir=tmpdir,
            )
            self.assertTrue(refreshed["allowed"])


if __name__ == "__main__":
    unittest.main()
