import json
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import boundary  # type: ignore  # noqa: E402


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

    def test_quiet_hours_action_is_blocked_even_if_autonomous(self):
        result = boundary.check(
            mode="explore",
            intent="action",
            hour=2,
            is_autonomous=True,
            presence={"resident": True},
            policies=[],
            metadata={},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "深夜帯（1-6時）のため自律操作抑制")
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
            mode="watch",
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


if __name__ == "__main__":
    unittest.main()
