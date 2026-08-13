"""agy_schema.py: Antigravity が拒否する enum 内 null を anyOf へ移す変換。

契約:
  - 受け入れる値の集合を変えない（表現だけ移す）
  - Antigravity が通す構文（nullable union・anyOf・optional）には触らない
  - 壊れた入力は素通しする（呼び出し側を失敗させない）
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import agy_schema  # noqa: E402
import json_schemas  # noqa: E402


def _has_null_in_enum(node) -> bool:
    if isinstance(node, list):
        return any(_has_null_in_enum(item) for item in node)
    if not isinstance(node, dict):
        return False
    enum = node.get("enum")
    if isinstance(enum, list) and any(item is None for item in enum):
        return True
    return any(_has_null_in_enum(value) for value in node.values())


class SanitizeTests(unittest.TestCase):
    def test_enum_with_null_becomes_anyof(self):
        out = agy_schema.sanitize({"type": "string", "enum": ["a", "b", None]})
        self.assertEqual(
            out, {"anyOf": [{"type": "string", "enum": ["a", "b"]}, {"type": "null"}]}
        )

    def test_nullable_type_union_is_left_alone(self):
        # union は Antigravity で通ることを実測済み（1.1.9 / 1.1.12）。触る理由がない。
        node = {"type": ["string", "null"], "description": "x"}
        self.assertEqual(agy_schema.sanitize(node), node)

    def test_existing_anyof_is_left_alone(self):
        node = {"anyOf": [{"type": "string"}, {"type": "null"}]}
        self.assertEqual(agy_schema.sanitize(node), node)

    def test_enum_without_null_is_left_alone(self):
        node = {"type": "string", "enum": ["a", "b"]}
        self.assertEqual(agy_schema.sanitize(node), node)

    def test_description_stays_outside_the_branches(self):
        # 中へ入れると、説明が anyOf の片側だけの説明になってしまう。
        out = agy_schema.sanitize(
            {"type": "string", "enum": ["a", None], "description": "気分"}
        )
        self.assertEqual(out["description"], "気分")
        self.assertNotIn("description", out["anyOf"][0])

    def test_union_type_loses_null_because_the_branch_expresses_it(self):
        out = agy_schema.sanitize({"type": ["string", "null"], "enum": ["a", None]})
        self.assertEqual(out["anyOf"][0]["type"], "string")
        self.assertEqual(out["anyOf"][1], {"type": "null"})

    def test_enum_of_only_null_is_kept_as_null_only(self):
        out = agy_schema.sanitize({"enum": [None]})
        self.assertEqual(out, {"anyOf": [{"type": "null"}]})

    def test_nested_and_listed_schemas_are_reached(self):
        out = agy_schema.sanitize(
            {
                "type": "object",
                "properties": {"a": {"type": "string", "enum": ["x", None]}},
                "oneOf": [{"type": "string", "enum": ["y", None]}],
            }
        )
        self.assertFalse(_has_null_in_enum(out))

    def test_input_is_not_mutated(self):
        node = {"type": "string", "enum": ["a", None]}
        before = json.dumps(node, sort_keys=True)
        agy_schema.sanitize(node)
        self.assertEqual(json.dumps(node, sort_keys=True), before)


class RealLoopSchemaTests(unittest.TestCase):
    """正本のループスキーマが、変換後に Antigravity の拒否条件を満たさないこと。"""

    def test_every_loop_mode_has_no_enum_null_after_conversion(self):
        for mode in ("observe", "explore", "reflect", "web", "social"):
            with self.subTest(mode=mode):
                schema = json_schemas.loop_schema(mode)
                self.assertTrue(_has_null_in_enum(schema), "変換前は該当があるはず")
                self.assertFalse(_has_null_in_enum(agy_schema.sanitize(schema)))

    def test_conversion_only_touches_emotion(self):
        schema = json_schemas.loop_schema("observe")
        converted = agy_schema.sanitize(schema)
        for key, value in schema["properties"].items():
            if key == "emotion":
                self.assertIn("anyOf", converted["properties"][key])
            else:
                self.assertEqual(converted["properties"][key], value, key)

    def test_chat_and_daybook_schemas_are_unchanged(self):
        for name, schema in (
            ("chat", json_schemas.chat_schema()),
            ("chat_voice", json_schemas.chat_schema(voice=True)),
            ("daybook", json_schemas.daybook_schema()),
        ):
            with self.subTest(name=name):
                self.assertEqual(agy_schema.sanitize(schema), schema)


class CliTests(unittest.TestCase):
    def _run(self, text):
        return subprocess.run(
            [sys.executable, str(ROOT / "embodied_ha" / "agy_schema.py")],
            input=text,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_stdin_to_stdout_conversion(self):
        result = self._run(json.dumps({"type": "string", "enum": ["a", None]}))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(result.stdout),
            {"anyOf": [{"type": "string", "enum": ["a"]}, {"type": "null"}]},
        )

    def test_broken_input_passes_through_untouched(self):
        # ここで失敗させると、呼び出し側が「スキーマ無し」ではなく「呼び出し失敗」になる。
        result = self._run("{not json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "{not json")


class PassThroughTests(unittest.TestCase):
    def test_unchanged_schema_keeps_its_exact_bytes(self):
        """変換不要なら字面を変えない。

        daybook のスキーマは実地検証済みで、native へ渡す文字列が変わると
        「検証したものと同じ」と言えなくなる。直す必要のないものは触らない。
        """
        raw = '{"type": "object", "properties": {"ok": {"type": "boolean"}}}'
        result = subprocess.run(
            [sys.executable, str(ROOT / "embodied_ha" / "agy_schema.py")],
            input=raw, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.stdout, raw)


if __name__ == "__main__":
    unittest.main()
