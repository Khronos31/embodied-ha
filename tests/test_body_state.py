import json
import datetime
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
        self.assertEqual(after["session_count"], state["session_count"] + 1)

    def test_audio_session_cost_updates_energy_and_stress(self):
        state = body_state.normalize_state({"energy": 0.9, "stress": 0.5})
        after = body_state.on_audio_session(state)
        self.assertEqual(after["energy"], 0.82)
        self.assertEqual(after["stress"], 0.53)
        self.assertEqual(after["last_event"], "audio_session")

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

    def test_serialize_state_hides_private_embodiment_fields(self):
        state = body_state.normalize_state({
            "confidence": 0.5,
            "embodiment_tension": 0.4,
            "return_to_body_pressure": 0.3,
            "remote_mode": "remote_avatar",
            "remote_room": "washroom",
        })
        payload = json.loads(body_state.serialize_state(state))
        self.assertIn("confidence", payload)
        self.assertNotIn("embodiment_tension", payload)
        self.assertNotIn("remote_room", payload)
        self.assertNotIn("physical_anchor_host", payload)

    def test_remote_avatar_action_and_tick_create_small_drift(self):
        initial = body_state.normalize_state(None)
        after_action = body_state.apply_action_effect(
            initial,
            action_mode="remote_avatar",
            action_cost=0.45,
            target_room="washroom",
            target_host="camera.washroom",
            move_cost=3.0,
        )
        self.assertGreater(after_action["stress"], initial["stress"])
        self.assertLess(after_action["confidence"], initial["confidence"])
        self.assertEqual(after_action["remote_mode"], "remote_avatar")
        self.assertEqual(after_action["remote_avatar_host"], "camera.washroom")
        self.assertEqual(after_action["physical_anchor_host"], "")

        drifted = body_state.advance_tick(
            after_action,
            loop_name="watch",
            trigger_reason="定期実行",
            now=body_state._now() + datetime.timedelta(minutes=30),
        )
        self.assertGreaterEqual(drifted["embodiment_tension"], after_action["embodiment_tension"])
        self.assertGreaterEqual(drifted["return_to_body_pressure"], after_action["return_to_body_pressure"])

    def test_direct_in_room_clears_remote_presence_and_settles(self):
        remote = body_state.normalize_state({
            "stress": 0.35,
            "confidence": 0.50,
            "embodiment_tension": 0.4,
            "return_to_body_pressure": 0.5,
            "remote_mode": "remote_avatar",
            "remote_room": "washroom",
            "remote_move_cost": 3.0,
        })
        settled = body_state.apply_action_effect(remote, action_mode="direct_in_room", action_cost=0.05, target_room="study", target_host="alsa://default")
        self.assertEqual(settled["remote_mode"], "")
        self.assertEqual(settled["remote_room"], "")
        self.assertEqual(settled["current_device_host"], "alsa://default")
        self.assertEqual(settled["physical_anchor_host"], "alsa://default")
        self.assertLess(settled["stress"], remote["stress"])
        self.assertGreater(settled["confidence"], remote["confidence"])

    def test_physical_move_clears_physical_anchor_host(self):
        state = body_state.normalize_state({
            "physical_anchor_host": "alsa://study",
            "current_device_host": "alsa://study",
            "remote_avatar_host": "camera.kitchen",
            "remote_mode": "remote_avatar",
            "remote_room": "kitchen",
        })
        moved = body_state.apply_action_effect(state, action_mode="physical_move", action_cost=2.0, target_room="living_room", move_cost=2.0)
        self.assertEqual(moved["current_device_host"], "")
        self.assertEqual(moved["physical_anchor_host"], "")
        self.assertEqual(moved["remote_avatar_host"], "")

if __name__ == "__main__":
    unittest.main()
