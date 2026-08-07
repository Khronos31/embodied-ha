import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "embodied_ha" / "mcp-schema-manifest.py"


class McpSchemaManifestTests(unittest.TestCase):
    def _write_fake_server(self, directory: Path) -> Path:
        path = directory / "fake-mcp.py"
        path.write_text(
            """#!/usr/bin/env python3
import json
import sys

request = json.loads(sys.stdin.readline())
tools = [
    {
        "name": "allowed_tool",
        "description": "Allowed description",
        "inputSchema": {
            "type": "object",
            "properties": {"intent": {"type": "string", "enum": ["a", "b"]}},
            "required": ["intent"],
        },
    },
    {
        "name": "hidden_tool",
        "description": "Must not be copied",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": tools}}))
""",
            encoding="utf-8",
        )
        os.chmod(path, 0o755)
        return path

    def test_writes_only_allowed_tool_schema_without_server_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            server = self._write_fake_server(directory)
            config_path = directory / "mcp_config.json"
            output_path = directory / "manifest.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "fixture": {
                                "command": sys.executable,
                                "args": [str(server)],
                                "env": {
                                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                    "SUPERVISOR_TOKEN": "must-not-appear",
                                },
                                "includeTools": ["allowed_tool"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(config_path), str(output_path)],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest_text = output_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(len(manifest["tools"]), 1)
            self.assertEqual(manifest["tools"][0]["server"], "fixture")
            self.assertEqual(manifest["tools"][0]["name"], "allowed_tool")
            self.assertEqual(
                manifest["tools"][0]["inputSchema"]["required"],
                ["intent"],
            )
            self.assertNotIn("hidden_tool", manifest_text)
            self.assertNotIn("SUPERVISOR_TOKEN", manifest_text)
            self.assertNotIn("must-not-appear", manifest_text)
            self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)

    def test_fails_closed_if_allowed_tool_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            server = self._write_fake_server(directory)
            config_path = directory / "mcp_config.json"
            output_path = directory / "manifest.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "fixture": {
                                "command": sys.executable,
                                "args": [str(server)],
                                "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                                "includeTools": ["missing_tool"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(config_path), str(output_path)],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tools/list omitted allowed tools", result.stderr)
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
