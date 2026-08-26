import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import read_policy


AGY_BRAIN = "/data/.gemini/antigravity-cli/brain/1c0e-0d29/.system_generated"
# 開発機側の作業ディレクトリ。同じ形の枝があり、非公開の資料から組み立てた
# プロンプト全文が入る。アドオンは config を map しているので届いてしまう。
DEV_BRAIN = ("/config/.tools/antigravity-home/.gemini/antigravity-cli"
             "/brain/1c0e-0d29/.system_generated")


class SystemGeneratedExceptionTests(unittest.TestCase):
    """退避されたツール結果を読めるようにした例外の範囲を固定する。

    Antigravity は大きい MCP ツール結果を .system_generated 配下へ退避する。
    そこが読めないと、ツールを呼べても結果を受け取れない（出力が大きいほど届かない）。
    認証情報は別の枝にあるので、.gemini だけが拒否理由のときに限って開ける。
    """

    def test_spilled_tool_output_is_readable(self):
        self.assertEqual(read_policy.read_deny_reason(f"{AGY_BRAIN}/steps/10/output.txt"), "")

    def test_transcript_in_the_same_branch_is_readable(self):
        # ⚠️ ここには注入したプロンプト全文とモデルの思考過程が入る。
        # 「小さければ提示されていたもの」ではなく、開示は増える。枝ごと開ける判断。
        self.assertEqual(
            read_policy.read_deny_reason(f"{AGY_BRAIN}/logs/transcript_full.jsonl"), "")

    def test_credentials_and_config_stay_denied(self):
        for path in (
            "/data/.gemini/antigravity-cli/eha-mcp-credentials/observe.json",
            "/data/.gemini/antigravity-cli/antigravity-oauth-token",
            "/data/.gemini/antigravity-cli/settings.json",
            "/data/.gemini/config/config.json",
            "/data/.gemini/config/projects/abc.json",
        ):
            with self.subTest(path=path):
                self.assertNotEqual(read_policy.read_deny_reason(path), "")

    def test_sibling_directories_stay_denied(self):
        for leaf in ("scratch/note.txt", ".user_uploaded/photo.png"):
            path = f"/data/.gemini/antigravity-cli/brain/1c0e-0d29/{leaf}"
            with self.subTest(path=path):
                self.assertNotEqual(read_policy.read_deny_reason(path), "")

    def test_only_the_addon_data_directory_is_opened(self):
        for leaf in ("steps/1/output.txt", "logs/transcript_full.jsonl", "messages/1.json"):
            with self.subTest(leaf=leaf):
                self.assertEqual(read_policy.read_deny_reason(f"{AGY_BRAIN}/{leaf}"), "")
                self.assertNotEqual(read_policy.read_deny_reason(f"{DEV_BRAIN}/{leaf}"), "")

    def test_marker_anywhere_in_the_path_does_not_open_credentials(self):
        # 構成要素の集合で判定すると、順序を入れ替えるだけで資格情報が開く。
        for path in (
            "/data/.system_generated/.gemini/antigravity-cli/eha-mcp-credentials/observe.json",
            f"{AGY_BRAIN}/steps/1/../../../eha-mcp-credentials/observe.json",
        ):
            with self.subTest(path=path):
                self.assertNotEqual(read_policy.read_deny_reason(path), "")

    def test_other_denied_directories_are_not_opened_by_the_marker(self):
        # 別の拒否理由が重なっている場合は開けない。
        for path in ("/config/.ssh/.system_generated/id_rsa",
                     "/config/.storage/.system_generated/auth",
                     "/config/.tools/claude-home/.system_generated/x"):
            with self.subTest(path=path):
                self.assertNotEqual(read_policy.read_deny_reason(path), "")

    def test_name_based_denials_still_win_inside_the_exception(self):
        for leaf in ("steps/1/key.pem", "steps/1/secrets.yaml",
                     "steps/1/eha-mcp-x.config.toml"):
            with self.subTest(leaf=leaf):
                self.assertNotEqual(read_policy.read_deny_reason(f"{AGY_BRAIN}/{leaf}"), "")

    def test_unrelated_paths_are_unaffected(self):
        self.assertEqual(read_policy.read_deny_reason("/config/embodied-ha-sora/preferences.json"), "")
        self.assertNotEqual(read_policy.read_deny_reason("/config/secrets.yaml"), "")


class ReadPolicyTests(unittest.TestCase):
    def test_claude_settings_merge_preserves_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.local.json"
            path.write_text(json.dumps({
                "theme": "keep",
                "permissions": {"allow": ["Read(/config/public/**)"], "deny": ["Bash(*)"]},
            }), encoding="utf-8")
            read_policy.merge_claude_settings(str(path))
            settings = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(settings["theme"], "keep")
            self.assertEqual(settings["permissions"]["allow"], ["Read(/config/public/**)"])
            self.assertIn("Bash(*)", settings["permissions"]["deny"])
            for rule in read_policy.CLAUDE_DENY_RULES:
                self.assertIn(rule, settings["permissions"]["deny"])

    def test_invalid_existing_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.local.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                read_policy.merge_claude_settings(str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
