import json
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import body_state  # type: ignore  # noqa: E402


class BodyStateTests(unittest.TestCase):
    def test_load_state_returns_defaults_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "body_state.json"
            state = body_state.load_state(str(path))
            self.assertEqual(state["curiosity"], 0.52)
            self.assertEqual(state["energy"], 0.68)
            self.assertEqual(state["stress"], 0.24)
            self.assertEqual(state["confidence"], 0.56)
            self.assertEqual(state["social_openness"], 0.50)
            self.assertEqual(state["updated_at"], "")

    def test_save_state_round_trips_through_atomic_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "body_state.json"
            body_state.save_state(
                str(path),
                {
                    "curiosity": 0.9,
                    "energy": 0.1,
                    "stress": 0.8,
                    "confidence": 0.7,
                    "social_openness": 0.6,
                    "updated_at": "2026-06-24T12:00:00+09:00",
                    "last_loop": "watch",
                },
            )
            self.assertTrue(path.exists())
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["curiosity"], 0.9)
            self.assertEqual(loaded["last_loop"], "watch")

    def test_tick_and_feedback_change_expected_axes(self):
        state = body_state.advance_tick(
            body_state.normalize_state(None),
            loop_name="watch",
            trigger_reason="定期実行（20分間隔）",
            active_desires=["気になること"],
        )
        self.assertGreater(state["curiosity"], 0.52)
        self.assertGreaterEqual(state["stress"], 0.22)
        self.assertEqual(state["last_loop"], "watch")
        self.assertEqual(state["last_result"], "tick")

        after = body_state.apply_feedback(
            state,
            loop_name="watch",
            success=True,
            duration_seconds=120.0,
            spoke=False,
        )
        self.assertLess(after["energy"], state["energy"])
        self.assertLess(after["curiosity"], state["curiosity"])
        self.assertEqual(after["last_result"], "success")

    def test_compute_run_chance_reflects_state(self):
        calm = {
            "curiosity": 0.9,
            "energy": 0.9,
            "stress": 0.1,
            "confidence": 0.8,
            "social_openness": 0.7,
        }
        tense = {
            "curiosity": 0.2,
            "energy": 0.2,
            "stress": 0.9,
            "confidence": 0.3,
            "social_openness": 0.2,
        }
        self.assertGreater(body_state.compute_run_chance(50, calm, "explore"), 50)
        self.assertLess(body_state.compute_run_chance(50, tense, "explore"), 50)

    def test_format_log_line_has_body_state_prefix(self):
        line = body_state.format_log_line("tick/watch", body_state.normalize_state(None), reason="定期実行")
        self.assertTrue(line.startswith("[body_state]"))
        self.assertIn("curiosity=", line)
        self.assertIn("reason=定期実行", line)


if __name__ == "__main__":
    unittest.main()
