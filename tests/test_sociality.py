import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_update_relationship_audit_contains_metadata_without_values(self):
        with mock.patch.object(self.sociality, "log") as log_mock:
            result, is_error = self.sociality.update_relationship({
                "person": "alice",
                "text": "古い引数名",
                "secret-value-as-key": "token-value",
            })

        self.assertTrue(is_error)
        self.assertEqual(result[0]["text"], "note が空です")
        log_line = log_mock.call_args.args[0]
        self.assertIn("update_relationship invalid args", log_line)
        self.assertIn("missing_note", log_line)
        self.assertIn("person", log_line)
        self.assertIn("text", log_line)
        self.assertNotIn("alice", log_line)
        self.assertNotIn("古い引数名", log_line)
        self.assertNotIn("secret-value-as-key", log_line)
        audit_path = Path(self.tmpdir.name) / "sociality_tool_errors.jsonl"
        audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(audit["tool"], "update_relationship")
        self.assertEqual(audit["reason"], "missing_note")
        self.assertEqual(audit["arg_keys"], ["person", "text"])
        self.assertEqual(audit["arg_types"], {"person": "string", "text": "string"})
        self.assertEqual(audit["unknown_arg_count"], 1)
        persisted = audit_path.read_text(encoding="utf-8")
        self.assertNotIn("alice", persisted)
        self.assertNotIn("古い引数名", persisted)
        self.assertNotIn("secret-value-as-key", persisted)
        self.assertNotIn("token-value", persisted)

    def test_get_narrative_returns_empty_string_when_missing(self):
        self.assertEqual(self._text(self.sociality.get_narrative({})), "")

    def test_append_narrative_appends_a_bullet_text(self):
        self.sociality.append_narrative({"text": "今日は会話の流れが少し落ち着いていた"})
        narrative = self._text(self.sociality.get_narrative({}))
        self.assertIn("今日は会話の流れが少し落ち着いていた", narrative)
        self.assertIn("- ", narrative)

    def test_corrupt_relationships_are_not_replaced(self):
        path = Path(self.tmpdir.name) / "relationships.json"
        path.write_text("{broken", encoding="utf-8")
        result, is_error = self.sociality.update_relationship(
            {"person": "alice", "note": "失われてはいけない"}
        )
        self.assertTrue(is_error)
        self.assertIn("更新を中止", result[0]["text"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_unreadable_narrative_is_not_replaced(self):
        path = Path(self.tmpdir.name) / "self_narrative.md"
        path.write_bytes(b"\xff\xfe")
        result, is_error = self.sociality.append_narrative({"text": "追記"})
        self.assertTrue(is_error)
        self.assertIn("更新を中止", result[0]["text"])
        self.assertEqual(path.read_bytes(), b"\xff\xfe")

    def test_get_social_state_returns_defaults_when_missing(self):
        payload = json.loads(self._text(self.sociality.get_social_state({})))
        self.assertEqual(payload["mode"], "idle")
        self.assertEqual(payload["last_event"], "")
        self.assertEqual(payload["last_event_ts"], "")
        self.assertEqual(payload["last_interaction_ts"], "")
        self.assertIsNone(payload["elapsed_since_last_interaction_seconds"])

    def test_update_social_state_audit_is_bounded(self):
        with mock.patch.object(self.sociality, "log") as log_mock:
            for index in range(self.sociality._AUDIT_MAX_ENTRIES + 5):
                result, is_error = self.sociality.update_social_state({"text": f"古い引数名{index}"})

        self.assertTrue(is_error)
        self.assertEqual(result[0]["text"], "event が空です")
        log_line = log_mock.call_args.args[0]
        self.assertIn("update_social_state invalid args", log_line)
        self.assertIn("missing_event", log_line)
        self.assertIn("text", log_line)
        self.assertNotIn("古い引数名", log_line)
        audit_path = Path(self.tmpdir.name) / "sociality_tool_errors.jsonl"
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), self.sociality._AUDIT_MAX_ENTRIES)
        self.assertNotIn("古い引数名", audit_path.read_text(encoding="utf-8"))

    def test_valid_updates_do_not_create_audit_log(self):
        self.sociality.update_relationship({"person": "alice", "note": "猫が好き"})
        self.sociality.update_social_state({"event": "会話を開始"})
        self.assertFalse((Path(self.tmpdir.name) / "sociality_tool_errors.jsonl").exists())

    def test_corrupt_audit_file_does_not_replace_validation_error(self):
        audit_path = Path(self.tmpdir.name) / "sociality_tool_errors.jsonl"
        audit_path.write_bytes(b"\xff\xfe\x00broken")
        with mock.patch.object(self.sociality, "log") as log_mock:
            result, is_error = self.sociality.update_relationship({"person": "alice"})

        self.assertTrue(is_error)
        self.assertEqual(result[0]["text"], "note が空です")
        row = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(row["tool"], "update_relationship")
        self.assertEqual(row["reason"], "missing_note")
        self.assertNotIn("alice", audit_path.read_text(encoding="utf-8"))
        self.assertNotIn("alice", log_mock.call_args.args[0])

    def test_set_shared_focus_is_read_back_by_get_shared_focus(self):
        self.sociality.set_shared_focus({
            "topic": "猫の話",
            "context": "今は会話の焦点",
            "object_id": "obj_mug_1",
            "scene_source": "camera.living",
            "last_seen_at": "2026-06-25T10:00:00+09:00",
        })
        payload = json.loads(self._text(self.sociality.get_shared_focus({})))
        self.assertEqual(payload["topic"], "猫の話")
        self.assertEqual(payload["context"], "今は会話の焦点")
        self.assertEqual(payload["object_id"], "obj_mug_1")
        self.assertEqual(payload["scene_source"], "camera.living")
        self.assertEqual(payload["last_seen_at"], "2026-06-25T10:00:00+09:00")
        self.assertTrue(payload["updated_at"])

    def test_get_person_model_returns_defaults_for_empty_and_unknown_person(self):
        empty_payload = json.loads(self._text(self.sociality.get_person_model({"person": ""})))
        alice_payload = json.loads(self._text(self.sociality.get_person_model({"person": "alice"})))
        for payload, expected_person in ((empty_payload, ""), (alice_payload, "alice")):
            self.assertEqual(payload["person"], expected_person)
            self.assertFalse(payload["boundary"]["quiet_window"]["active"])
            self.assertTrue(payload["boundary"]["consent"]["speak"])
            self.assertTrue(payload["boundary"]["consent"]["action"])
            self.assertEqual(payload["boundary"]["turn_taking"]["state"], "open")
            self.assertEqual(payload["shared_focus"]["topic"], "")

    def test_record_boundary_persists_quiet_window_and_turn_taking(self):
        self.sociality.record_boundary(
            {
                "person": "alice",
                "quiet_window": {"active": True, "start": "22:00", "end": "07:00"},
                "turn_taking": {"state": "waiting", "awaiting_reply": True},
            }
        )
        payload = json.loads(self._text(self.sociality.get_person_model({"person": "alice"})))
        self.assertTrue(payload["boundary"]["quiet_window"]["active"])
        self.assertEqual(payload["boundary"]["quiet_window"]["start"], "22:00")
        self.assertEqual(payload["boundary"]["turn_taking"]["state"], "waiting")
        self.assertTrue(payload["boundary"]["turn_taking"]["awaiting_reply"])

    def test_record_consent_reenables_interrupt_after_rejection(self):
        self.sociality.record_boundary(
            {
                "person": "alice",
                "consent": {"speak": False, "action": True},
            }
        )
        denied = json.loads(
            self._text(
                self.sociality.should_interrupt(
                    {"person": "alice", "mode": "watch", "intent": "speak", "hour": 12}
                )
            )
        )
        self.assertFalse(denied["allowed"])
        self.assertIn("consent", denied["reason"])

        self.sociality.record_consent({"person": "alice", "kind": "speak", "granted": True, "note": "OK"})
        allowed = json.loads(
            self._text(
                self.sociality.should_interrupt(
                    {"person": "alice", "mode": "watch", "intent": "speak", "hour": 12}
                )
            )
        )
        self.assertTrue(allowed["allowed"])

    def test_should_interrupt_schema_requires_explicit_intent_enum(self):
        with mock.patch.object(self.sociality, "serve") as serve_mock:
            self.sociality.main()

        tools = serve_mock.call_args.args[2]
        spec = tools["should_interrupt"]["spec"]
        schema = spec["inputSchema"]
        self.assertEqual(schema["required"], ["intent"])
        self.assertEqual(
            schema["properties"]["intent"]["enum"],
            ["speak", "action"],
        )
        self.assertIn("発話", schema["properties"]["intent"]["description"])
        self.assertIn("家電操作", schema["properties"]["intent"]["description"])
        self.assertIn("intent=speak", spec["description"])
        self.assertIn("intent=action", spec["description"])

    def test_should_interrupt_missing_intent_fails_closed(self):
        payload = json.loads(
            self._text(
                self.sociality.should_interrupt(
                    {"person": "alice", "mode": "watch", "hour": 12}
                )
            )
        )

        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["reason"], "未知のintent: （空）")

    def test_should_interrupt_keeps_speak_and_action_consent_separate(self):
        self.sociality.record_boundary(
            {
                "person": "alice",
                "consent": {"speak": True, "action": False},
            }
        )

        speak = json.loads(
            self._text(
                self.sociality.should_interrupt(
                    {"person": "alice", "mode": "watch", "intent": "speak", "hour": 12}
                )
            )
        )
        action = json.loads(
            self._text(
                self.sociality.should_interrupt(
                    {"person": "alice", "mode": "watch", "intent": "action", "hour": 12}
                )
            )
        )

        self.assertTrue(speak["allowed"])
        self.assertFalse(action["allowed"])
        self.assertIn("consent", action["reason"])

    def test_ingest_interaction_updates_turn_taking_state(self):
        self.sociality.ingest_interaction(
            {
                "person": "alice",
                "speaker": "resident",
                "kind": "question",
                "text": "今いい?",
            }
        )
        payload = json.loads(self._text(self.sociality.get_turn_taking_state({"person": "alice"})))
        self.assertEqual(payload["turn_taking"]["state"], "awaiting_reply")
        self.assertTrue(payload["turn_taking"]["awaiting_reply"])
        self.assertEqual(payload["turn_taking"]["last_speaker"], "resident")
        self.assertEqual(payload["turn_taking"]["last_text"], "今いい?")


if __name__ == "__main__":
    unittest.main()
