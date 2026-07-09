"""eha_config.py（chat.py移植 増分7）の単体テスト。

config.sh（bash）の環境変数デフォルト解決・EXTRA_CONTEXT/POLICIES構築
ロジックとの一致を検証する。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import eha_config  # type: ignore  # noqa: E402


class LoadConfigDefaultsTests(unittest.TestCase):
    def test_empty_environ_gets_all_defaults(self):
        resolved = eha_config.load_config(script_dir="/some/script_dir", environ={})
        self.assertEqual(resolved["RESIDENT"], "ユーザー")
        self.assertEqual(resolved["HA_URL"], "http://supervisor/core/api")
        self.assertEqual(resolved["CLAUDE_BIN"], "claude")
        self.assertEqual(resolved["CLAUDE_CONFIG_DIR"], "/config/.tools/claude-home")
        self.assertEqual(resolved["EHA_PREFS_FILE"], "/some/script_dir/preferences.json")
        self.assertEqual(resolved["EHA_CHARACTER_FILE"], "/some/script_dir/character.md")

    def test_existing_nonempty_values_are_preserved(self):
        resolved = eha_config.load_config(
            script_dir="/some/script_dir", environ={"RESIDENT": "ゆの", "CLAUDE_BIN": "/custom/claude"}
        )
        self.assertEqual(resolved["RESIDENT"], "ゆの")
        self.assertEqual(resolved["CLAUDE_BIN"], "/custom/claude")

    def test_empty_string_value_is_treated_as_unset_like_bash(self):
        # bashの${VAR:-default}は空文字列でも既定値を使う。dict.setdefaultとは違う挙動。
        resolved = eha_config.load_config(script_dir="/some/script_dir", environ={"RESIDENT": ""})
        self.assertEqual(resolved["RESIDENT"], "ユーザー")

    def test_antigravity_bin_dir_derives_from_home(self):
        resolved = eha_config.load_config(
            script_dir="/x", environ={"EHA_ANTIGRAVITY_HOME": "/custom/agy"}
        )
        self.assertEqual(resolved["EHA_ANTIGRAVITY_BIN_DIR"], "/custom/agy/bin")
        self.assertEqual(resolved["EHA_ANTIGRAVITY_BIN"], "/custom/agy/bin/agy")

    def test_original_environ_dict_is_not_mutated(self):
        original = {}
        eha_config.load_config(script_dir="/x", environ=original)
        self.assertEqual(original, {})


class BuildExtraContextTests(unittest.TestCase):
    def test_no_data_dir_returns_empty(self):
        self.assertEqual(eha_config._build_extra_context(None), "")

    def test_missing_conf_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(eha_config._build_extra_context(tmp), "")

    def test_runs_each_line_as_shell_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "extra_context.conf"
            conf.write_text("echo line1\necho line2\n", encoding="utf-8")
            result = eha_config._build_extra_context(tmp)
            self.assertIn("line1", result)
            self.assertIn("line2", result)

    def test_comments_and_blank_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "extra_context.conf"
            conf.write_text("# comment\n\necho only_this\n", encoding="utf-8")
            result = eha_config._build_extra_context(tmp)
            self.assertEqual(result.strip(), "only_this")

    def test_failing_command_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "extra_context.conf"
            conf.write_text("this_command_does_not_exist_xyz\necho after\n", encoding="utf-8")
            result = eha_config._build_extra_context(tmp)
            self.assertIn("after", result)


class BuildPoliciesTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(eha_config._build_policies("/no/such/preferences.json"), "")

    def test_renders_policies_as_bullets(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            with open(prefs_file, "w", encoding="utf-8") as fh:
                json.dump({"policies": ["静かに", "21時以降は控えめに"]}, fh)
            result = eha_config._build_policies(str(prefs_file))
            self.assertEqual(result, "- 静かに\n- 21時以降は控えめに")

    def test_non_string_or_empty_policies_are_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            with open(prefs_file, "w", encoding="utf-8") as fh:
                json.dump({"policies": ["有効", "", None, 123]}, fh)
            result = eha_config._build_policies(str(prefs_file))
            self.assertEqual(result, "- 有効")

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            prefs_file.write_text("not json", encoding="utf-8")
            self.assertEqual(eha_config._build_policies(str(prefs_file)), "")


if __name__ == "__main__":
    unittest.main()
