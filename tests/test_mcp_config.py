"""mcp-config.pyのhttp_post許可トグル(preferences.jsonのhttp_post_enabled)テスト。

http-mcp.py自身はEHA_HTTP_ALLOW_POST環境変数でhttp_postツールの
tools/list掲載有無を切り替える。Claude Codeの--allowedToolsはMCPツールを
実行時に絞り込めるが、tools/listの可視性は絞らないため、非掲載も防御層として残る。mcp-config.pyはその
env変数を、preferences.jsonのhttp_post_enabledフィールド(Web UI
「高度な設定」タブのトグル)から動的に注入する役割を持つ。

subprocessとして実際に起動し、生成されるmcp設定JSONのenvを検証する
(mcp-config.pyはモジュールレベルでos.environを読むため、import経由の
単体テストより実行経路に忠実なsubprocess呼び出しの方が適している)。
"""
import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
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

    def run_config_no_check(self, args, env=None):
        run_env = {"PATH": "/usr/bin:/bin", **(env or {})}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=run_env,
            check=False,
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
                ]
            )

            config = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(
                config["mcpServers"]["ha"][GEMINI_OFFICIAL_MCP_ALLOWLIST_KEY],
                ["ha_get"],
            )
            self.assertEqual(set(config["mcpServers"]), {"ha"})

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

    def test_format_codex_auto_approves_all_first_party_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "profile.config.toml"

            self.run_config(
                [
                    "--format",
                    "codex",
                    "--allowed-mcp-tools",
                    "mcp__files__read_file,mcp__ha__ha_get",
                    str(out_path),
                    "files",
                    "ha",
                ],
            )

            with out_path.open("rb") as fh:
                profile = tomllib.load(fh)
            self.assertEqual(
                profile["mcp_servers"]["files"]["default_tools_approval_mode"],
                "approve",
            )
            # F11-A1(2026-07-23): developer_instruction を全 MCP 向けに一般化(code-mode の
            # ALL_TOOLS registry を照合させて confabulation を抑える)。files 提示時は read_file
            # ガイドも併記される。
            self.assertIn("ALL_TOOLS", profile["developer_instructions"])
            self.assertIn(
                "do not rely on earlier conversation claims",
                profile["developer_instructions"],
            )
            self.assertIn("files MCP server's read_file tool", profile["developer_instructions"])
            # F10(2026-07-23): 承認は files 限定ではなく全 first-party server に付与する。
            # files 限定だと codex chat が ha/memory/body/game 等を "user cancelled" で
            # 一切呼べない（実機E2Eで判明）。ha も approve されること。
            self.assertEqual(
                profile["mcp_servers"]["ha"]["default_tools_approval_mode"],
                "approve",
            )

            ha_only_path = Path(tmp) / "ha-only.config.toml"
            self.run_config(
                [
                    "--format",
                    "codex",
                    "--allowed-mcp-tools",
                    "mcp__ha__ha_get",
                    str(ha_only_path),
                    "ha",
                ],
            )
            with ha_only_path.open("rb") as fh:
                ha_only_profile = tomllib.load(fh)
            # F11-A1: 一般 code-mode instruction は MCP server がある限り付く(files 非同席でも)。
            self.assertIn("ALL_TOOLS", ha_only_profile["developer_instructions"])
            # ただし read_file 固有ガイドは files 提示時だけなので付かない。
            self.assertNotIn(
                "files MCP server's read_file tool",
                ha_only_profile["developer_instructions"],
            )
            # 承認は first-party 一律なので files 非同席でも ha は approve される（F10）。
            self.assertEqual(
                ha_only_profile["mcp_servers"]["ha"]["default_tools_approval_mode"],
                "approve",
            )

    def test_official_allowlist_key_names_are_not_typoed(self):
        self.assertEqual(CODEX_OFFICIAL_MCP_ALLOWLIST_KEY, "enabled_tools")
        self.assertEqual(GEMINI_OFFICIAL_MCP_ALLOWLIST_KEY, "includeTools")

    def test_docstring_names_server_list_as_hacontrol_safety_boundary(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("hacontrol", text)
        self.assertIn("--mcp-servers", text)
        self.assertIn("--allowed-mcp-tools", text)
        self.assertIn("server-list", text)

    def test_bare_filename_output_path_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "mcp_config.json", "memory"],
                cwd=tmp,
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(tmp) / "mcp_config.json").exists())

    def assert_rejects_without_output(self, args, expected_stderr):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "mcp_config.json"
            result = self.run_config_no_check([*args, str(out_path), "ha"])

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_stderr, result.stderr)
            self.assertFalse(out_path.exists())

    def test_unknown_selected_server_is_error_and_does_not_write_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "mcp_config.json"
            result = self.run_config_no_check([str(out_path), "unknown"])

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unknown MCP server: unknown", result.stderr)
            self.assertFalse(out_path.exists())

    def test_allowed_mcp_tools_rejects_unknown_server_and_does_not_write_output(self):
        self.assert_rejects_without_output(
            ["--allowed-mcp-tools", "mcp__unknown__ha_get"],
            "unknown MCP server in allowlist: unknown",
        )

    def test_allowed_mcp_tools_rejects_unselected_server_and_does_not_write_output(self):
        self.assert_rejects_without_output(
            ["--allowed-mcp-tools", "mcp__memory__recall"],
            "MCP server is not selected by --mcp-servers: memory",
        )

    def test_allowed_mcp_tools_rejects_unknown_tool_and_does_not_write_output(self):
        self.assert_rejects_without_output(
            ["--allowed-mcp-tools", "mcp__ha__typo"],
            "unknown MCP tool for server ha: typo",
        )

    def test_allowed_mcp_tools_rejects_duplicate_and_does_not_write_output(self):
        self.assert_rejects_without_output(
            ["--allowed-mcp-tools", "mcp__ha__ha_get,mcp__ha__ha_get"],
            "duplicate MCP tool allowlist entry: mcp__ha__ha_get",
        )

    def test_allowed_mcp_tools_rejects_empty_entry_and_does_not_write_output(self):
        self.assert_rejects_without_output(
            ["--allowed-mcp-tools", "mcp__ha__ha_get,"],
            "--allowed-mcp-tools contains an empty entry",
        )

    def test_allowed_mcp_tools_must_cover_all_selected_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "mcp_config.json"
            result = self.run_config_no_check(
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

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must cover every selected server", result.stderr)
            self.assertFalse(out_path.exists())

    def test_claude_allows_server_internal_partial_allowlist(self):
        """Specification change: Claude now accepts a per-server tool subset."""
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "mcp_config.json"
            self.run_config(
                [
                    "--format",
                    "claude",
                    "--allowed-mcp-tools",
                    "mcp__memory__recall",
                    str(out_path),
                    "memory",
                ]
            )

            config = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(set(config["mcpServers"]), {"memory"})
            self.assertNotIn("includeTools", config["mcpServers"]["memory"])


class ServerSpecsTests(unittest.TestCase):
    def load_module(self, tmp, prefs_content=None):
        prefs_file = Path(tmp) / "preferences.json"
        prefs_file.write_text(json.dumps(prefs_content or {}, ensure_ascii=False), encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HA_URL": "http://example.invalid",
            "SUPERVISOR_TOKEN": "test-token",
            "EHA_PREFS_FILE": str(prefs_file),
            "EHA_DATA_DIR": str(Path(tmp) / "data"),
            "EHA_LOG_DIR": str(Path(tmp) / "log"),
        }
        Path(env["EHA_DATA_DIR"]).mkdir()
        Path(env["EHA_LOG_DIR"]).mkdir()
        spec = importlib.util.spec_from_file_location("mcp_config_for_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, env, clear=False):
            spec.loader.exec_module(module)
        return module, env

    def list_runtime_tools(self, server, env):
        proc = subprocess.Popen(
            [server["command"], *server.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**env, **(server.get("env") or {})},
            cwd=ROOT,
        )
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}) + "\n"
        stdout, stderr = proc.communicate(request, timeout=5)
        self.assertEqual(proc.returncode, 0, stderr)
        response = json.loads(stdout)
        return [tool["name"] for tool in response["result"]["tools"]]

    def assert_server_specs_match_runtime_tools(self, prefs_content=None):
        with tempfile.TemporaryDirectory() as tmp:
            module, env = self.load_module(tmp, prefs_content)
            with mock.patch.dict(os.environ, env, clear=False):
                for name, spec in module.SERVER_SPECS.items():
                    server = spec.build()
                    self.assertEqual(self.list_runtime_tools(server, env), list(spec.active_tools()), name)

    def test_server_specs_match_runtime_tools_with_default_preferences(self):
        self.assert_server_specs_match_runtime_tools()

    def test_files_server_env_excludes_secrets(self):
        # files MCP は HA へアクセスしないため COMMON_ENV(SUPERVISOR_TOKEN 等の秘密)を渡さない=最小権限。
        # (本命の防御は files-mcp.py の /proc・/sys 拒否+NUL 検出。これはその二重化。)
        with tempfile.TemporaryDirectory() as tmp:
            module, env = self.load_module(tmp)
            with mock.patch.dict(os.environ, env, clear=False):
                files = module.SERVER_SPECS["files"].build()
        self.assertNotIn("SUPERVISOR_TOKEN", files["env"])
        self.assertNotIn("HA_URL", files["env"])
        self.assertIn("PATH", files["env"])

    def test_server_specs_match_runtime_tools_when_http_post_enabled(self):
        self.assert_server_specs_match_runtime_tools({"http_post_enabled": True})


if __name__ == "__main__":
    unittest.main()
