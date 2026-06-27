import json
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import embodied_action  # type: ignore  # noqa: E402
import body_state  # type: ignore  # noqa: E402


class EmbodiedActionTests(unittest.TestCase):
    def test_action_cost_tiers(self):
        self.assertEqual(embodied_action.action_cost_for_mode("direct_in_room", 2), 0.05)
        self.assertEqual(embodied_action.action_cost_for_mode("physical_move", 2), 2.0)
        self.assertGreater(embodied_action.action_cost_for_mode("remote_avatar", 3), 0.35)

    def test_apply_action_to_body_state_persists_remote_presence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "body_state.json"
            body_state.save_state(str(state_path), body_state.normalize_state(None))
            old = embodied_action.body_state_path
            try:
                embodied_action.body_state_path = lambda: str(state_path)
                embodied_action.apply_action_to_body_state(
                    action_mode="remote_avatar",
                    action_cost=0.45,
                    target_room="washroom",
                    target_host="camera.washroom",
                    move_cost=3.0,
                )
            finally:
                embodied_action.body_state_path = old
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["remote_mode"], "remote_avatar")
        self.assertEqual(saved["remote_room"], "washroom")
        self.assertEqual(saved["remote_avatar_host"], "camera.washroom")
        self.assertGreater(saved["embodiment_tension"], 0.0)
