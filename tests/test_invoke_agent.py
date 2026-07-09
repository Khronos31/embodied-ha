import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "embodied_ha" / "invoke-agent.sh"


def write_executable(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    path.chmod(0o755)


class InvokeAgentTests(unittest.TestCase):
    def run_wrapper(self, args, env, *, input_text=None):
        run_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            **env,
        }
        return subprocess.run(
            [SCRIPT.as_posix(), *args],
            input=input_text,
            text=True,
            capture_output=True,
            cwd=ROOT,
            env=run_env,
            check=False,
        )

    def test_claude_lite_maps_model_effort_schema_tools_mcp_and_content_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "claude.json"
            fake = tmpdir / "claude"
            write_executable(
                fake,
                f"""
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                stdin = sys.stdin.read()
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": sys.argv[1:], "stdin": stdin}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(json.dumps({{"type": "result", "structured_output": {{"ok": True}}}}, ensure_ascii=False))
                """,
            )
            schema = '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}'
            content = json.dumps(
                [
                    {"type": "text", "text": "observe prompt"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"}},
                ],
                ensure_ascii=False,
            )

            result = self.run_wrapper(
                [
                    "--model",
                    "lite",
                    "--json-schema",
                    schema,
                    "--allowed-tools",
                    "Read,mcp__ha__ha_get",
                    "--mcp-config",
                    "/tmp/mcp.json",
                    "--append-system-prompt",
                    "system prompt",
                    "--content-json",
                    content,
                    "ignored text when content-json is supplied",
                ],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"ok": True})
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertIn("-p", args)
            self.assertEqual(args[args.index("--model") + 1], "haiku")
            self.assertEqual(args[args.index("--effort") + 1], "low")
            self.assertEqual(args[args.index("--json-schema") + 1], schema)
            self.assertEqual(args[args.index("--allowedTools") + 1], "Read,mcp__ha__ha_get")
            self.assertEqual(args[args.index("--mcp-config") + 1], "/tmp/mcp.json")
            self.assertEqual(args[args.index("--append-system-prompt") + 1], "system prompt")
            message = json.loads(payload["stdin"])
            self.assertEqual(message["message"]["content"], json.loads(content))

    def test_codex_lite_uses_process_substitution_contract_and_stdout_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "codex.json"
            fake = tmpdir / "codex"
            write_executable(
                fake,
                f"""
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                schema_path = args[args.index("--output-schema") + 1]
                out_path = args[args.index("-o") + 1]
                prompt = args[-1]
                schema = Path(schema_path).read_text(encoding="utf-8")
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{
                        "args": args,
                        "schema": schema,
                        "prompt": prompt,
                    }}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print("codex transcript on stdout")
                Path(out_path).write_text('{{"ok":true}}', encoding="utf-8")
                """,
            )
            schema = '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}'

            result = self.run_wrapper(
                ["--model", "lite", "--json-schema", schema, "--append-system-prompt", "SYS", "hello"],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": "/tmp",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '{"ok":true}')
            self.assertIn("codex transcript on stdout", result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertEqual(args[:2], ["exec", "--skip-git-repo-check"])
            self.assertEqual(args[args.index("--model") + 1], "gpt-5.4-mini")
            self.assertEqual(args[args.index("--config") + 1], "model_reasoning_effort=low")
            self.assertEqual(payload["schema"], schema)
            self.assertEqual(payload["prompt"], "SYS\n\nhello")

    def test_agy_appends_schema_to_prompt_and_extracts_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "agy.json"
            fake = tmpdir / "agy"
            write_executable(
                fake,
                f"""
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": args}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print("prefix")
                print('{{"ok":true}}')
                """,
            )
            schema = '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}'

            result = self.run_wrapper(
                ["--model", "default", "--json-schema", schema, "--append-system-prompt", "SYS", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"ok": True})
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertEqual(args[args.index("--model") + 1], "Gemini 3.5 Flash (Medium)")
            prompt = args[args.index("-p") + 1]
            self.assertIn("あなたへの指示:\nSYS", prompt)
            self.assertIn(schema, prompt)
            self.assertTrue(prompt.endswith("JSON:\n"))

    def test_sound_file_forces_agy_high_even_when_harness_is_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "agy.json"
            agy = tmpdir / "agy"
            codex = tmpdir / "codex"
            write_executable(
                agy,
                f"""
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": sys.argv[1:]}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print('{{"ok":true}}')
                """,
            )
            write_executable(
                codex,
                """
                #!/usr/bin/env bash
                echo "codex must not be called" >&2
                exit 99
                """,
            )

            result = self.run_wrapper(
                ["--model", "lite", "--sound-file", "/tmp/input.wav", "listen"],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": codex.as_posix(),
                    "EHA_ANTIGRAVITY_BIN": agy.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"ok": True})
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertEqual(args[args.index("--model") + 1], "Gemini 3.5 Flash (High)")
            prompt = args[args.index("-p") + 1]
            self.assertIn("【いま聞こえた音】\n/tmp/input.wav", prompt)

    def test_codex_rejects_claude_only_tool_contract_instead_of_dropping_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "codex"
            write_executable(
                fake,
                """
                #!/usr/bin/env bash
                echo "codex must not be called" >&2
                exit 99
                """,
            )

            result = self.run_wrapper(
                ["--allowed-tools", "Read", "hello"],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": fake.as_posix(),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--allowed-tools is not supported for codex", result.stderr)


if __name__ == "__main__":
    unittest.main()
