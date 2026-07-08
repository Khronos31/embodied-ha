import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "embodied_ha" / "json_schemas.py"


def load_json_schemas_module():
    module_name = "json_schemas_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_no_additional_properties_anywhere(testcase, schema, path="$"):
    """schema 内の全 object 型（サブスキーマ含む）が additionalProperties: false を持つことを検証。

    ただし properties を持たない（意図的に緩い passthrough として設計された）
    object 型は対象外とする — 明示的に形を定義した箇所だけ厳格化する方針のため。
    """
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object" and "properties" in schema:
        testcase.assertEqual(
            schema.get("additionalProperties"), False, f"{path} is missing additionalProperties: false"
        )
        for key, subschema in schema.get("properties", {}).items():
            _assert_no_additional_properties_anywhere(testcase, subschema, f"{path}.{key}")
    if schema.get("type") == "array":
        _assert_no_additional_properties_anywhere(testcase, schema.get("items", {}), f"{path}[]")


class LoopSchemaTests(unittest.TestCase):
    def setUp(self):
        self.js = load_json_schemas_module()

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            self.js.loop_schema("bogus")

    def test_all_modes_have_core_fields(self):
        for mode in ("observe", "explore", "reflect", "web", "social"):
            schema = self.js.loop_schema(mode)
            for field in ("topic", "speak", "private", "emotion", "feature_presented"):
                self.assertIn(field, schema["properties"], f"{mode} missing core field {field}")
                self.assertIn(field, schema["required"], f"{mode} core field {field} not required")

    def test_only_observe_and_explore_have_proposal_action(self):
        for mode in ("observe", "explore"):
            schema = self.js.loop_schema(mode)
            self.assertIn("proposal", schema["properties"])
            self.assertIn("action", schema["properties"])
        for mode in ("reflect", "web", "social"):
            schema = self.js.loop_schema(mode)
            self.assertNotIn("proposal", schema["properties"])
            self.assertNotIn("action", schema["properties"])

    def test_only_observe_has_scene_fields(self):
        observe_schema = self.js.loop_schema("observe")
        for field in ("scene_objects", "scene_people", "scene_changes"):
            self.assertIn(field, observe_schema["properties"])
        for mode in ("explore", "reflect", "web", "social"):
            schema = self.js.loop_schema(mode)
            for field in ("scene_objects", "scene_people", "scene_changes"):
                self.assertNotIn(field, schema["properties"])

    def test_additional_properties_false_everywhere(self):
        for mode in ("observe", "explore", "reflect", "web", "social"):
            _assert_no_additional_properties_anywhere(self, self.js.loop_schema(mode), path=mode)

    def test_required_matches_properties_exactly(self):
        for mode in ("observe", "explore", "reflect", "web", "social"):
            schema = self.js.loop_schema(mode)
            self.assertEqual(set(schema["required"]), set(schema["properties"].keys()), mode)


class ChatSchemaTests(unittest.TestCase):
    def setUp(self):
        self.js = load_json_schemas_module()

    def test_chat_variant_has_reply(self):
        schema = self.js.chat_schema(voice=False)
        self.assertIn("reply", schema["properties"])
        self.assertIn("reply", schema["required"])

    def test_voice_variant_has_no_reply(self):
        schema = self.js.chat_schema(voice=True)
        self.assertNotIn("reply", schema["properties"])

    def test_both_variants_share_core_fields(self):
        for schema in (self.js.chat_schema(voice=False), self.js.chat_schema(voice=True)):
            for field in ("private", "proposal_resolved", "preferences_update", "feature_presented"):
                self.assertIn(field, schema["properties"])
                self.assertIn(field, schema["required"])

    def test_additional_properties_false_everywhere(self):
        _assert_no_additional_properties_anywhere(self, self.js.chat_schema(voice=False), path="chat")
        _assert_no_additional_properties_anywhere(self, self.js.chat_schema(voice=True), path="voice")


class DaybookSchemaTests(unittest.TestCase):
    def setUp(self):
        self.js = load_json_schemas_module()

    def test_top_level_fields(self):
        schema = self.js.daybook_schema()
        for field in ("summary", "themes", "highlights", "open_questions", "episodes"):
            self.assertIn(field, schema["properties"])
            self.assertIn(field, schema["required"])

    def test_additional_properties_false_everywhere(self):
        _assert_no_additional_properties_anywhere(self, self.js.daybook_schema(), path="daybook")


if __name__ == "__main__":
    unittest.main()
