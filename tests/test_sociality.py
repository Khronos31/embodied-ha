import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))


def load_sociality_module():
    path = ROOT / "embodied_ha" / "sociality-mcp.py"
    spec = importlib.util.spec_from_file_location("sociality_mcp", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SocialityTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sociality = load_sociality_module()
        self.sociality.LOG_DIR = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def _text(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["type"], "text")
        return result[0]["text"]

    def test_get_relationship_returns_empty_profile_for_missing_person(self):
        payload = json.loads(self._text(self.sociality.get_relationship({"person": "alice"})))
        self.assertEqual(payload["person"], "alice")
        self.assertEqual(payload["notes"], [])
        self.assertEqual(payload["interaction_count"], 0)
        self.assertEqual(payload["last_seen"], "")

    def test_update_relationship_is_read_back_by_get_relationship(self):
        self.sociality.update_relationship({"person": "alice", "note": "好きな話題は猫"})
        payload = json.loads(self._text(self.sociality.get_relationship({"person": "alice"})))
        self.assertEqual(payload["person"], "alice")
        self.assertEqual(payload["notes"], ["好きな話題は猫"])
        self.assertEqual(payload["interaction_count"], 1)
        self.assertTrue(payload["last_seen"])

    def test_get_narrative_returns_empty_string_when_missing(self):
        self.assertEqual(self._text(self.sociality.get_narrative({})), "")

    def test_append_narrative_appends_a_bullet_entry(self):
        self.sociality.append_narrative({"entry": "今日は会話の流れが少し落ち着いていた"})
        narrative = self._text(self.sociality.get_narrative({}))
        self.assertIn("今日は会話の流れが少し落ち着いていた", narrative)
        self.assertIn("- ", narrative)

    def test_get_social_state_returns_defaults_when_missing(self):
        payload = json.loads(self._text(self.sociality.get_social_state({})))
        self.assertEqual(payload["mode"], "idle")
        self.assertEqual(payload["last_event"], "")
        self.assertEqual(payload["last_event_ts"], "")
        self.assertEqual(payload["last_interaction_ts"], "")
        self.assertIsNone(payload["elapsed_since_last_interaction_seconds"])

    def test_set_shared_focus_is_read_back_by_get_shared_focus(self):
        self.sociality.set_shared_focus({"topic": "猫の話", "context": "今は会話の焦点"})
        payload = json.loads(self._text(self.sociality.get_shared_focus({})))
        self.assertEqual(payload["topic"], "猫の話")
        self.assertEqual(payload["context"], "今は会話の焦点")
        self.assertTrue(payload["updated_at"])


if __name__ == "__main__":
    unittest.main()
