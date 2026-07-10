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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "embodied_ha" / "mcp-config.py"


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


if __name__ == "__main__":
    unittest.main()
