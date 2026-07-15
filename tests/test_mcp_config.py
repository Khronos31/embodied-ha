"""mcp-config.pyのhttp_post許可トグル(preferences.jsonのhttp_post_enabled)テスト。

http-mcp.py自身はEHA_HTTP_ALLOW_POST環境変数でhttp_postツールの
tools/list掲載有無を切り替える(--allowedToolsではMCPツール単位の
絞り込みができないため、これが唯一の制御点)。mcp-config.pyはその
env変数を、preferences.jsonのhttp_post_enabledフィールド(Web UI
「高度な設定」タブのトグル)から動的に注入する役割を持つ。

subprocessとして実際に起動し、生成されるmcp設定JSONのenvを検証する
(mcp-config.pyはモジュールレベルでos.environを読むため、import経由の
単体テストより実行経路に忠実なsubprocess呼び出しの方が適している)。
"""
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "embodied_ha" / "mcp-config.py"

# Official key names fixed from primary docs:
# - Codex manual config reference: MCP server tool allow-list is `enabled_tools`.
# - Gemini CLI MCP server docs: MCP server tool allow-list is `includeTools`.
CODEX_OFFICIAL_MCP_ALLOWLIST_KEY = "enabled_tools"
GEMINI_OFFICIAL_MCP_ALLOWLIST_KEY = "includeTools"


def _run_mcp_config(prefs_content, tmp):
    prefs_file = Path(tmp) / "preferences.json"
    prefs_file.write_text(json.dumps(prefs_content, ensure_ascii=False), encoding="utf-8")
    out_path = Path(tmp) / "mcp_config.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), str(out_path), "http"],
        env={"EHA_PREFS_FILE": str(prefs_file), "PATH": "/usr/bin:/bin"},
        check=True, capture_output=True, text=True,
    )
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh)


class HttpPostToggleTests(unittest.TestCase):
    def test_enabled_injects_allow_post_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _run_mcp_config({"http_post_enabled": True}, tmp)
        http_env = config["mcpServers"]["http"]["env"]
        self.assertEqual(http_env.get("EHA_HTTP_ALLOW_POST"), "1")

    def test_disabled_omits_allow_post_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _run_mcp_config({"http_post_enabled": False}, tmp)
        http_env = config["mcpServers"]["http"]["env"]
        self.assertNotIn("EHA_HTTP_ALLOW_POST", http_env)

    def test_missing_field_defaults_to_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _run_mcp_config({}, tmp)
        http_env = config["mcpServers"]["http"]["env"]
        self.assertNotIn("EHA_HTTP_ALLOW_POST", http_env)


class McpConfigFormatTests(unittest.TestCase):
    def run_config(self, args, env=None):
        run_env = {"PATH": "/usr/bin:/bin", **(env or {})}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=run_env,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_format_claude_matches_legacy_output_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "legacy.json"
            explicit = Path(tmp) / "explicit.json"

            self.run_config([str(legacy), "ha"])
            self.run_config(["--format", "claude", str(explicit), "ha"])

            self.assertEqual(legacy.read_bytes(), explicit.read_bytes())

    def test_format_agy_adds_include_tools_for_matching_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "mcp_config.json"

            self.run_config(
                [
                    "--format",
                    "agy",
                    "--allowed-mcp-tools",
                    "mcp__ha__ha_get",
                    str(out_path),
                    "ha",
                    "memory",
                ]
            )

            config = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(
                config["mcpServers"]["ha"][GEMINI_OFFICIAL_MCP_ALLOWLIST_KEY],
                ["ha_get"],
            )
            self.assertNotIn(GEMINI_OFFICIAL_MCP_ALLOWLIST_KEY, config["mcpServers"]["memory"])

    def test_format_codex_writes_profile_toml_with_enabled_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "profile.config.toml"

            self.run_config(
                [
                    "--format",
                    "codex",
                    "--allowed-mcp-tools",
                    "mcp__ha__ha_get",
                    str(out_path),
                    "ha",
                ],
                env={"SUPERVISOR_TOKEN": "secret-token"},
            )

            with out_path.open("rb") as fh:
                profile = tomllib.load(fh)
            ha_config = profile["mcp_servers"]["ha"]
            self.assertEqual(ha_config["command"], "python3")
            self.assertEqual(ha_config["args"], [str(ROOT / "embodied_ha" / "ha-mcp.py")])
            self.assertEqual(ha_config["env"]["SUPERVISOR_TOKEN"], "secret-token")
            self.assertEqual(ha_config[CODEX_OFFICIAL_MCP_ALLOWLIST_KEY], ["ha_get"])

    def test_official_allowlist_key_names_are_not_typoed(self):
        self.assertEqual(CODEX_OFFICIAL_MCP_ALLOWLIST_KEY, "enabled_tools")
        self.assertEqual(GEMINI_OFFICIAL_MCP_ALLOWLIST_KEY, "includeTools")

    def test_docstring_names_server_list_as_hacontrol_safety_boundary(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("hacontrol", text)
        self.assertIn("--mcp-servers", text)
        self.assertIn("--allowed-mcp-tools", text)
        self.assertIn("server-list", text)


if __name__ == "__main__":
    unittest.main()
