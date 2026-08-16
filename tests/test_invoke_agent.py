import base64
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "embodied_ha" / "invoke-agent.sh"
MEMORY_ALLOWLIST = ",".join(
    f"mcp__memory__{name}"
    for name in [
        "recall",
        "remember",
        "loops_list",
        "loops_add",
        "loops_close",
        "record_episode",
        "record_counterfactual",
        "get_episode",
        "get_working_memory",
        "ingest_scene",
        "resolve_reference",
        "compare_recent_scenes",
        "list_episodes",
        "build_daybook",
        "get_daybook",
        "record_causal_chain",
        "get_causal_chain",
        "consolidate_memory",
    ]
)


def write_executable(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    path.chmod(0o755)


class InvokeAgentTests(unittest.TestCase):
    def run_wrapper(self, args, env, *, input_text=None):
        run_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            # run.sh always supplies HA_URL.  The Antigravity schema-manifest
            # preflight now starts selected MCP servers for tools/list.
            "HA_URL": "http://example.invalid",
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

    def write_project_fake_agy(self, tmpdir: Path) -> Path:
        fake = tmpdir / "agy"
        record_dir = tmpdir / "agy-records"
        record_dir.mkdir()
        write_executable(
            fake,
            f"""
            #!/usr/bin/env python3
            import fcntl
            import json
            import os
            import sys
            import time
            from pathlib import Path

            record_dir = Path({record_dir.as_posix()!r})
            args = sys.argv[1:]
            cwd = Path.cwd()
            site = cwd.name
            home = Path(os.environ["HOME"])
            projects_dir = home / ".gemini" / "config" / "projects"
            projects_dir.mkdir(parents=True, exist_ok=True)
            concurrency_path = record_dir / "concurrency.json"
            counter_lock = record_dir / "counter.lock"

            def update_counter(delta):
                counter_lock.touch()
                with counter_lock.open("r+") as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    try:
                        if concurrency_path.exists():
                            data = json.loads(concurrency_path.read_text(encoding="utf-8"))
                        else:
                            data = {{"active": 0, "max": 0}}
                        data["active"] += delta
                        data["max"] = max(data.get("max", 0), data["active"])
                        concurrency_path.write_text(json.dumps(data), encoding="utf-8")
                    finally:
                        fcntl.flock(fh, fcntl.LOCK_UN)

            project_id = None
            if "--new-project" in args:
                update_counter(1)
                try:
                    time.sleep(0.2)
                    project_id = f"{{site}}-{{os.getpid()}}"
                    (projects_dir / f"{{project_id}}.json").write_text(
                        json.dumps({{"folderUri": str(cwd)}}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                finally:
                    update_counter(-1)
            elif "--project" in args:
                project_id = args[args.index("--project") + 1]

            record = {{
                "args": args,
                "cwd": str(cwd),
                "home": str(home),
                "project_id": project_id,
            }}
            (record_dir / f"{{site}}-{{os.getpid()}}.json").write_text(
                json.dumps(record, ensure_ascii=False),
                encoding="utf-8",
            )
            print('{{"ok":true}}')
            """,
        )
        return fake

    def read_agy_records(self, tmpdir: Path):
        record_dir = tmpdir / "agy-records"
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(record_dir.glob("*.json"))
            if path.name != "concurrency.json"
        ]

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
                    "--allowed-builtins",
                    "Read",
                    "--mcp-servers",
                    "ha",
                    "--allowed-mcp-tools",
                    "mcp__ha__ha_get",
                    "--append-system-prompt",
                    "system prompt",
                    "--content-json",
                    content,
                    "ignored text when content-json is supplied",
                ],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                    "SUPERVISOR_TOKEN": "secret-token",
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
            self.assertEqual(args[args.index("--disallowedTools") + 1], "Bash")
            self.assertIn("--mcp-config", args)
            self.assertEqual(args[args.index("--append-system-prompt") + 1], "system prompt")
            message = json.loads(payload["stdin"])
            self.assertEqual(message["message"]["content"], json.loads(content))

    def test_claude_cwd_prefers_eha_agent_cwd_and_falls_back_to_eha_claude_cwd(self):
        # invoke-agent-caller-wiring-phase2-spec.md 増分1: EHA_AGENT_CWD/EHA_CLAUDE_CWD
        # 移行期間の二重export下で、claudeサブプロセスの実行cwdが両変数で
        # byte-identicalに解決されることを確認する（run.shは同一値を両方exportする前提）。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "claude.json"
            fake = tmpdir / "claude"
            write_executable(
                fake,
                f"""
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                sys.stdin.read()
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": sys.argv[1:], "pwd": os.environ.get("PWD")}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(json.dumps({{"type": "result", "result": "ok"}}, ensure_ascii=False))
                """,
            )
            shared_cwd = tmpdir / "workdir"
            shared_cwd.mkdir()
            other_cwd = tmpdir / "stale-claude-only-workdir"
            other_cwd.mkdir()

            # 移行期間の二重export想定: EHA_AGENT_CWDとEHA_CLAUDE_CWDが同じ値。
            both_set = self.run_wrapper(
                ["hello"],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": shared_cwd.as_posix(),
                    "EHA_CLAUDE_CWD": shared_cwd.as_posix(),
                },
            )
            self.assertEqual(both_set.returncode, 0, both_set.stderr)
            self.assertEqual(
                json.loads(record.read_text(encoding="utf-8"))["pwd"],
                shared_cwd.as_posix(),
            )

            # 優先順位そのものの検証(false positive防止): 値が食い違う場合、
            # EHA_AGENT_CWDが実際に勝つことを確認する(同値ケースだけでは
            # 旧優先順序のままでも偶然通ってしまう)。
            precedence = self.run_wrapper(
                ["hello"],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": shared_cwd.as_posix(),
                    "EHA_CLAUDE_CWD": other_cwd.as_posix(),
                },
            )
            self.assertEqual(precedence.returncode, 0, precedence.stderr)
            self.assertEqual(
                json.loads(record.read_text(encoding="utf-8"))["pwd"],
                shared_cwd.as_posix(),
            )

            # 増分1完了前（EHA_AGENT_CWD未export）の現行動作: EHA_CLAUDE_CWDのみで解決。
            legacy_only = self.run_wrapper(
                ["hello"],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                    "EHA_CLAUDE_CWD": shared_cwd.as_posix(),
                },
            )
            self.assertEqual(legacy_only.returncode, 0, legacy_only.stderr)
            self.assertEqual(
                json.loads(record.read_text(encoding="utf-8"))["pwd"],
                shared_cwd.as_posix(),
            )

            # 増分7完了後（EHA_CLAUDE_CWD未export）を先取りした動作: EHA_AGENT_CWDのみで解決。
            new_only = self.run_wrapper(
                ["hello"],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": shared_cwd.as_posix(),
                },
            )
            self.assertEqual(new_only.returncode, 0, new_only.stderr)
            self.assertEqual(
                json.loads(record.read_text(encoding="utf-8"))["pwd"],
                shared_cwd.as_posix(),
            )

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
                out_path = args[args.index("-o") + 1]
                prompt = args[-1]
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{
                        "args": args,
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
            self.assertEqual(args[args.index("--model") + 1], "gpt-5.6-luna")
            self.assertEqual(args[args.index("--config") + 1], "model_reasoning_effort=low")
            # 契約(F11-B1・2026-07-23): codex 既定の built-in 実行系/apps を明示 hardening する。
            # read-only sandbox + apps/shell_tool 等の feature 無効化。exec は残す(MCP 中核)。
            self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
            disabled = [args[i + 1] for i, a in enumerate(args) if a == "--disable"]
            for feature in (
                "apps",
                "shell_tool",
                "image_generation",
                "goals",
                "multi_agent",
                "tool_suggest",
            ):
                self.assertIn(feature, disabled)
            # --ignore-user-config は使わない: transient --profile ごと無視され MCP が全滅するため
            # (2026-07-23 実機 A/B 実証)。
            self.assertNotIn("--ignore-user-config", args)
            # 契約変更(F5・2026-07-23): codex は --output-schema(OpenAI strict)を使わず、agy 同様に schema を
            # prompt へ埋め込む(EHA の任意キー object は strict で表現不可)。
            self.assertNotIn("--output-schema", args)
            self.assertTrue(payload["prompt"].startswith("SYS\n\nhello"))
            self.assertIn(schema, payload["prompt"])
            self.assertTrue(payload["prompt"].endswith("JSON:\n"))

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
                if "--help" in args:
                    print("--output-format text|json|stream-json")
                    print("--json-schema JSON")
                    raise SystemExit(0)
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": args}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(json.dumps({{
                    "status": "SUCCESS",
                    "response": "ignored text",
                    "structured_output": {{"ok": True}},
                }}))
                """,
            )
            schema = '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}'

            result = self.run_wrapper(
                [
                    "--model", "default", "--json-schema", schema,
                    "--append-system-prompt", "SYS", "--agent-site", "daybook", "hello",
                ],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": (tmpdir / "agy-home").as_posix(),
                    "EHA_AGENT_CWD": (tmpdir / "workdir").as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"ok": True})
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertEqual(args[args.index("--model") + 1], "Gemini 3.5 Flash (Medium)")
            self.assertEqual(args[args.index("--output-format") + 1], "json")
            self.assertEqual(args[args.index("--json-schema") + 1], schema)
            prompt = args[args.index("-p") + 1]
            self.assertIn("あなたへの指示:\nSYS", prompt)
            self.assertIn(schema, prompt)
            self.assertTrue(prompt.endswith("JSON:\n"))

    def test_agy_legacy_cli_keeps_prompt_schema_fallback(self):
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
                if "--help" in args:
                    print("legacy help")
                    raise SystemExit(0)
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
                ["--json-schema", schema, "--agent-site", "daybook", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": (tmpdir / "agy-home").as_posix(),
                    "EHA_AGENT_CWD": (tmpdir / "workdir").as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"ok": True})
            args = json.loads(record.read_text(encoding="utf-8"))["args"]
            self.assertNotIn("--output-format", args)
            self.assertNotIn("--json-schema", args)
            prompt = args[args.index("-p") + 1]
            self.assertIn(schema, prompt)

    def test_agy_native_empty_response_does_not_expose_metadata_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake = tmpdir / "agy"
            write_executable(
                fake,
                """
                #!/usr/bin/env python3
                import json
                import sys

                if "--help" in sys.argv[1:]:
                    print("--output-format --json-schema")
                    raise SystemExit(0)
                print(json.dumps({
                    "conversation_id": "test",
                    "status": "SUCCESS",
                    "response": "",
                    "structured_output": None,
                }))
                """,
            )

            result = self.run_wrapper(
                [
                    "--json-schema", '{"type":"object"}',
                    "--agent-site", "daybook", "hello",
                ],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": (tmpdir / "agy-home").as_posix(),
                    "EHA_AGENT_CWD": (tmpdir / "workdir").as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_agy_native_error_envelope_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake = tmpdir / "agy"
            write_executable(
                fake,
                """
                #!/usr/bin/env python3
                import json
                import sys

                if "--help" in sys.argv[1:]:
                    print("--output-format --json-schema")
                    raise SystemExit(0)
                print(json.dumps({
                    "conversation_id": "test",
                    "status": "ERROR",
                    "error": "schema rejected",
                    "response": "",
                }))
                """,
            )

            result = self.run_wrapper(
                [
                    "--json-schema", '{"type":"object"}',
                    "--agent-site", "daybook", "hello",
                ],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": (tmpdir / "agy-home").as_posix(),
                    "EHA_AGENT_CWD": (tmpdir / "workdir").as_posix(),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("agy structured output failed: schema rejected", result.stderr)

    def test_agy_non_daybook_schema_keeps_prompt_fallback(self):
        """loop の各モードは prompt 埋め込みのまま（native へ広げない）。

        2026-08-14 実測: MCP サーバーを繋いだ状態では agy の `--output-format json` が
        `structured_output` を返さない。loop は MCP を繋ぐので、native 化すると応答が
        空になり invoke 失敗になる。daybook が成立しているのは MCP を繋がないため。
        """
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
                if "--help" in args:
                    print("--output-format --json-schema")
                    raise SystemExit(0)
                Path({record.as_posix()!r}).write_text(json.dumps(args), encoding="utf-8")
                print('{{"ok":true}}')
                """,
            )
            schema = '{"type":"object"}'

            result = self.run_wrapper(
                ["--json-schema", schema, "--agent-site", "observe", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": (tmpdir / "agy-home").as_posix(),
                    "EHA_AGENT_CWD": (tmpdir / "workdir").as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(record.read_text(encoding="utf-8"))
            self.assertNotIn("--output-format", args)
            self.assertNotIn("--json-schema", args)

    def test_claude_no_tools_uses_empty_builtin_set_and_strict_empty_mcp(self):
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

                sys.stdin.read()
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": sys.argv[1:]}}), encoding="utf-8"
                )
                print(json.dumps({{"type": "result", "result": "ok"}}))
                """,
            )

            result = self.run_wrapper(
                ["--no-tools", "hello"],
                {"EHA_AGENT_HARNESS": "claude", "EHA_CLAUDE_BIN": fake.as_posix()},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(record.read_text(encoding="utf-8"))["args"]
            self.assertEqual(args[args.index("--tools") + 1], "")
            self.assertIn("--strict-mcp-config", args)
            mcp_path = Path(args[args.index("--mcp-config") + 1])
            # wrapper終了時に一時ファイルは消えるため、引数と命名契約を確認する。
            self.assertTrue(mcp_path.name.startswith("eha-claude-no-tools."))
            self.assertNotIn("--allowedTools", args)

    def test_codex_no_tools_ignores_user_config_and_disables_execution_surfaces(self):
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
                out_path = args[args.index("-o") + 1]
                Path({record.as_posix()!r}).write_text(json.dumps({{"args": args}}), encoding="utf-8")
                Path(out_path).write_text('{{"ok":true}}', encoding="utf-8")
                """,
            )

            result = self.run_wrapper(
                ["--no-tools", "hello"],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": tmpdir.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(record.read_text(encoding="utf-8"))["args"]
            self.assertIn("--ignore-user-config", args)
            disabled = [args[i + 1] for i, arg in enumerate(args) if arg == "--disable"]
            for feature in (
                "apps",
                "shell_tool",
                "unified_exec",
                "code_mode_host",
                "computer_use",
                "browser_use",
                "browser_use_external",
                "browser_use_full_cdp_access",
            ):
                self.assertIn(feature, disabled)
            configs = [args[i + 1] for i, arg in enumerate(args) if arg == "--config"]
            self.assertIn("web_search=disabled", configs)
            self.assertNotIn("--profile", args)

    def test_agy_no_tools_uses_dedicated_site_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            workdir = tmpdir / "workdir"
            fake = self.write_project_fake_agy(tmpdir)

            result = self.run_wrapper(
                ["--no-tools", "--agent-site", "daybook", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": agy_home.as_posix(),
                    "EHA_AGENT_CWD": workdir.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads(
                (workdir / "daybook" / ".agents" / "settings.json").read_text(encoding="utf-8")
            )
            deny = settings["permissions"]["deny"]
            for rule in (
                "command(*)",
                "write_file(*)",
                "read_file(*)",
                "read_url(*)",
                "execute_url(*)",
                "browser(*)",
                "mcp(*)",
                "unsandboxed(*)",
                "escalate_admin(*)",
            ):
                self.assertIn(rule, deny)
            records = self.read_agy_records(tmpdir)
            self.assertEqual(records[0]["cwd"], str(workdir / "daybook"))

    def test_no_tools_rejects_capabilities_and_agy_requires_site(self):
        conflicting = self.run_wrapper(
            ["--no-tools", "--allowed-builtins", "Read", "hello"],
            {"EHA_AGENT_HARNESS": "claude"},
        )
        self.assertEqual(conflicting.returncode, 2)
        self.assertIn("cannot be combined", conflicting.stderr)

        missing_site = self.run_wrapper(
            ["--no-tools", "hello"],
            {"EHA_AGENT_HARNESS": "agy", "EHA_ANTIGRAVITY_BIN": "/bin/true"},
        )
        self.assertEqual(missing_site.returncode, 2)
        self.assertIn("--agent-site is required for agy --no-tools", missing_site.stderr)

    def test_claude_content_json_at_prefix_reads_from_file(self):
        # 2026-07-16発見: --content-jsonのinline JSONはLinuxの単一argv要素128KB上限
        # (MAX_ARG_STRLEN)に引っかかる(observeモードの実カメラ画像で確認)。
        # curl -d @file慣習で@<path>指定時はファイルから読むようにした。
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
                Path({record.as_posix()!r}).write_text(stdin, encoding="utf-8")
                print(json.dumps({{"type": "result", "structured_output": {{"ok": True}}}}, ensure_ascii=False))
                """,
            )
            # 128KBのinline argv上限を超えるペイロード(大きな画像相当)をファイル経由で渡す。
            big_text = "x" * 200_000
            content = json.dumps(
                [{"type": "text", "text": big_text}],
                ensure_ascii=False,
            )
            content_path = tmpdir / "content.json"
            content_path.write_text(content, encoding="utf-8")

            result = self.run_wrapper(
                ["--content-json", f"@{content_path}", "ignored"],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            envelope = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(envelope["message"]["content"], json.loads(content))

    def test_claude_content_json_at_prefix_missing_file_dies(self):
        result = self.run_wrapper(
            ["--content-json", "@/nonexistent/path.json", "hello"],
            {"EHA_AGENT_HARNESS": "claude"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--content-json file not found", result.stderr)

    def test_claude_writes_raw_stream_to_explicit_transcript_file(self):
        # loop.pyのfacts抽出(introspection_facts.extract_facts_from_stream_text)は
        # assistant/userイベント中のtool_use/tool_resultを必要とするが、stdoutは
        # extract_result_json()が最終resultイベントだけに絞ってしまう。run_codex()の
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            transcript = tmpdir / "transcript.jsonl"
            fake = tmpdir / "claude"
            write_executable(
                fake,
                """
                #!/usr/bin/env python3
                import json
                import sys
                sys.stdin.read()
                print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1", "name": "mcp__ha__ha_get", "input": {}}]}}, ensure_ascii=False))
                print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1"}]}}, ensure_ascii=False))
                print(json.dumps({"type": "result", "structured_output": {"ok": True}}, ensure_ascii=False))
                """,
            )

            result = self.run_wrapper(
                ["--transcript-file", transcript.as_posix(), "hello"],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"ok": True})
            self.assertEqual(result.stderr, "")
            events = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["type"] for e in events], ["assistant", "user", "result"])

    def test_claude_transcript_write_failure_keeps_model_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            write_executable(
                fake,
                """
                #!/usr/bin/env python3
                import json
                import sys
                sys.stdin.read()
                print(json.dumps({"type": "result", "structured_output": {"ok": True}}))
                """,
            )
            result = self.run_wrapper(
                ["--transcript-file", "/proc/eha-transcript.jsonl", "hello"],
                {"EHA_AGENT_HARNESS": "claude", "EHA_CLAUDE_BIN": fake.as_posix()},
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"ok": True})
        self.assertIn("failed to write transcript file", result.stderr)

    def test_claude_large_transcript_stays_out_of_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake = tmpdir / "claude"
            transcript = tmpdir / "transcript.jsonl"
            write_executable(
                fake,
                """
                #!/usr/bin/env python3
                import json
                import sys
                sys.stdin.read()
                print(json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "x" * (500 * 1024)}]},
                }))
                print(json.dumps({"type": "result", "structured_output": {"ok": True}}))
                """,
            )
            result = self.run_wrapper(
                ["--transcript-file", transcript.as_posix(), "hello"],
                {"EHA_AGENT_HARNESS": "claude", "EHA_CLAUDE_BIN": fake.as_posix()},
            )
            data = transcript.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True})
        self.assertEqual(result.stderr, "")
        self.assertGreater(len(data), 500 * 1024)

    def test_claude_system_prompt_uses_native_flag_distinct_from_append(self):
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

                sys.stdin.read()
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": sys.argv[1:]}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(json.dumps({{"type": "result", "result": "ok"}}, ensure_ascii=False))
                """,
            )

            result = self.run_wrapper(
                [
                    "--system-prompt", "MAIN",
                    "--append-system-prompt", "EXTRA",
                    "hello",
                ],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertEqual(args[args.index("--system-prompt") + 1], "MAIN")
            self.assertEqual(args[args.index("--append-system-prompt") + 1], "EXTRA")

    def test_codex_system_prompt_uses_model_instructions_file(self):
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
                out_path = args[args.index("-o") + 1]
                config_values = [
                    a for i, a in enumerate(args)
                    if args[i - 1] == "--config" and a.startswith("model_instructions_file=")
                ]
                instructions_content = None
                if config_values:
                    instructions_path = config_values[0].split("=", 1)[1].strip('"')
                    instructions_content = Path(instructions_path).read_text(encoding="utf-8")
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": args, "instructions_content": instructions_content}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                Path(out_path).write_text('{{"ok":true}}', encoding="utf-8")
                """,
            )

            result = self.run_wrapper(
                ["--system-prompt", "MAIN INSTRUCTION", "hello"],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": "/tmp",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(payload["instructions_content"], "MAIN INSTRUCTION")

    def test_agy_system_prompt_uses_system_instruction_prefix_format(self):
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
                print('{{"ok":true}}')
                """,
            )

            result = self.run_wrapper(
                ["--system-prompt", "MAIN", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            prompt = args[args.index("-p") + 1]
            self.assertIn("[System Instruction]\nMAIN\n\n[User Prompt]\nhello", prompt)


    def test_legacy_allowed_tools_option_is_removed(self):
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
            self.assertIn("unknown option: --allowed-tools", result.stderr)

    def test_sound_file_option_is_removed(self):
        result = self.run_wrapper(
            ["--sound-file", "/tmp/removed.wav", "hello"],
            {"EHA_AGENT_HARNESS": "agy"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown option: --sound-file", result.stderr)

    def test_codex_mcp_servers_use_temp_profile_and_delete_after_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "codex_profile.json"
            codex_home = tmpdir / "codex-home"
            codex_home.mkdir()
            fake = tmpdir / "codex"
            write_executable(
                fake,
                f"""
                #!/usr/bin/env python3
                import json
                import sys
                import tomllib
                from pathlib import Path

                args = sys.argv[1:]
                profile_name = args[args.index("--profile") + 1]
                profile_path = Path({codex_home.as_posix()!r}) / f"{{profile_name}}.config.toml"
                with profile_path.open("rb") as fh:
                    profile = tomllib.load(fh)
                out_path = args[args.index("-o") + 1]
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{
                        "args": args,
                        "profile_name": profile_name,
                        "profile_exists_during_call": profile_path.exists(),
                        "profile": profile,
                    }}, ensure_ascii=False),
                    encoding="utf-8",
                )
                Path(out_path).write_text('{{"ok":true}}', encoding="utf-8")
                """,
            )

            result = self.run_wrapper(
                [
                    "--mcp-servers",
                    "ha",
                    "--allowed-mcp-tools",
                    "mcp__ha__ha_get",
                    "hello",
                ],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": "/tmp",
                    "CODEX_HOME": codex_home.as_posix(),
                    "SUPERVISOR_TOKEN": "secret-token",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '{"ok":true}')
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertIn("--profile", args)
            self.assertTrue(payload["profile_exists_during_call"])
            profile_name = payload["profile_name"]
            self.assertFalse((codex_home / f"{profile_name}.config.toml").exists())
            ha_config = payload["profile"]["mcp_servers"]["ha"]
            self.assertEqual(ha_config["enabled_tools"], ["ha_get"])
            self.assertEqual(ha_config["env"]["SUPERVISOR_TOKEN"], "secret-token")

    def test_claude_mcp_servers_generate_config_and_combine_allowed_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "claude_mcp.json"
            fake = tmpdir / "claude"
            write_executable(
                fake,
                f"""
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                mcp_config_path = Path(args[args.index("--mcp-config") + 1])
                config = json.loads(mcp_config_path.read_text(encoding="utf-8"))
                stdin = sys.stdin.read()
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{
                        "args": args,
                        "stdin": stdin,
                        "mcp_config_path": str(mcp_config_path),
                        "mcp_config_exists_during_call": mcp_config_path.exists(),
                        "mcp_config": config,
                    }}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(json.dumps({{"type": "result", "structured_output": {{"ok": True}}}}, ensure_ascii=False))
                """,
            )

            result = self.run_wrapper(
                [
                    "--mcp-servers",
                    "ha memory",
                    "--allowed-builtins",
                    "Read,WebSearch",
                    "--allowed-mcp-tools",
                    "mcp__ha__ha_get," + MEMORY_ALLOWLIST,
                    "hello",
                ],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                    "SUPERVISOR_TOKEN": "secret-token",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertEqual(
                args[args.index("--allowedTools") + 1],
                "Read,WebSearch,mcp__ha__ha_get," + MEMORY_ALLOWLIST,
            )
            self.assertTrue(payload["mcp_config_exists_during_call"])
            self.assertFalse(Path(payload["mcp_config_path"]).exists())
            config = payload["mcp_config"]["mcpServers"]
            self.assertIn("ha", config)
            self.assertIn("memory", config)
            self.assertNotIn("includeTools", config["ha"])
            self.assertEqual(config["ha"]["env"]["SUPERVISOR_TOKEN"], "secret-token")

    def test_mcp_config_and_mcp_servers_are_mutually_exclusive(self):
        result = self.run_wrapper(
            ["--mcp-config", "/tmp/x.json", "--mcp-servers", "ha", "hello"],
            {"EHA_AGENT_HARNESS": "claude"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--mcp-config and --mcp-servers cannot be used together", result.stderr)

    def test_mcp_config_rejects_separate_allowlists(self):
        builtins = self.run_wrapper(
            ["--mcp-config", "/tmp/x.json", "--allowed-builtins", "Read", "hello"],
            {"EHA_AGENT_HARNESS": "claude"},
        )
        mcp_tools = self.run_wrapper(
            ["--mcp-config", "/tmp/x.json", "--allowed-mcp-tools", "mcp__ha__ha_get", "hello"],
            {"EHA_AGENT_HARNESS": "claude"},
        )

        self.assertNotEqual(builtins.returncode, 0)
        self.assertIn("--mcp-config cannot be used with --allowed-builtins or --allowed-mcp-tools", builtins.stderr)
        self.assertNotEqual(mcp_tools.returncode, 0)
        self.assertIn("--mcp-config cannot be used with --allowed-builtins or --allowed-mcp-tools", mcp_tools.stderr)

    def test_allowed_mcp_tools_requires_mcp_servers(self):
        result = self.run_wrapper(
            ["--allowed-mcp-tools", "mcp__ha__ha_get", "hello"],
            {"EHA_AGENT_HARNESS": "claude"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--allowed-mcp-tools requires --mcp-servers", result.stderr)

    def test_empty_allowed_builtins_is_invalid_when_specified(self):
        result = self.run_wrapper(
            ["--allowed-builtins", "", "hello"],
            {"EHA_AGENT_HARNESS": "claude"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--allowed-builtins contains an empty entry", result.stderr)

    def test_help_documents_hacontrol_server_list_safety_boundary(self):
        result = self.run_wrapper(["--help"], {})

        self.assertEqual(result.returncode, 0)
        help_text = result.stderr
        self.assertIn("--allowed-builtins", help_text)
        self.assertIn("--allowed-mcp-tools", help_text)
        self.assertIn("--mcp-servers", help_text)
        self.assertIn("Removed: --allowed-tools / --allowedTools", help_text)
        self.assertIn("hacontrol", help_text)
        self.assertIn("server-list is the", help_text)
        self.assertIn("not --allowed-mcp-tools", help_text)
        self.assertIn("Per-server partial allowlists", help_text)

    def test_codex_accepts_allowed_builtins_and_maps_web_search(self):
        # 契約変更(2026-07-23・F4): --allowed-builtins は全ハーネス共通の能力意図。codex では
        # die せず受理し、WebSearch 意図の有無を native web_search(live/disabled)へ翻訳する。
        # Read は files MCP が担うため codex へ raw フラグは渡さない。
        for builtins, expected_web in (
            ("Read", "web_search=disabled"),
            ("Read,WebSearch", "web_search=live"),
            ("Read, WebSearch", "web_search=live"),  # 空白付きCSVも正規化して判定(sol Med)
        ):
            with self.subTest(builtins=builtins):
                with tempfile.TemporaryDirectory() as tmp:
                    record = Path(tmp) / "args.json"
                    fake = Path(tmp) / "codex"
                    write_executable(
                        fake,
                        f"""
                        #!/usr/bin/env python3
                        import json
                        import sys
                        from pathlib import Path

                        args = sys.argv[1:]
                        out_path = args[args.index("-o") + 1]
                        Path({record.as_posix()!r}).write_text(
                            json.dumps(args), encoding="utf-8"
                        )
                        Path(out_path).write_text('{{"ok":true}}', encoding="utf-8")
                        """,
                    )

                    result = self.run_wrapper(
                        ["--allowed-builtins", builtins, "hello"],
                        {
                            "EHA_AGENT_HARNESS": "codex",
                            "EHA_CODEX_BIN": fake.as_posix(),
                        },
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn(
                        "--allowed-builtins is not supported for codex", result.stderr
                    )
                    args = json.loads(record.read_text(encoding="utf-8"))
                    self.assertIn(expected_web, args)
                    self.assertNotIn("--allowed-builtins", args)

    def test_codex_translates_content_json_to_ordered_prompt_and_images(self):
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
                images = [args[i + 1] for i, arg in enumerate(args) if arg == "--image"]
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{
                        "args": args,
                        "prompt": args[-1],
                        "images": images,
                        "images_exist": [Path(path).is_file() for path in images],
                    }}, ensure_ascii=False),
                    encoding="utf-8",
                )
                out_path = args[args.index("-o") + 1]
                Path(out_path).write_text('{{"ok":true}}', encoding="utf-8")
                """,
            )
            content_path = tmpdir / "content.json"
            content_path.write_text(
                json.dumps(
                    [
                        {"type": "text", "text": "台所:"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64.b64encode(b"\xff\xd8\xff\xe0fixture").decode(),
                            },
                        },
                        {"type": "text", "text": "状況を説明してください"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temp_root = tmpdir / "content-tmp"
            temp_root.mkdir()

            result = self.run_wrapper(
                ["--content-json", f"@{content_path}", "hello"],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": fake.as_posix(),
                    "EHA_TMP_DIR": temp_root.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(payload["images_exist"], [True])
            self.assertLess(payload["prompt"].index("台所:"), payload["prompt"].index("【画像1】"))
            self.assertLess(
                payload["prompt"].index("【画像1】"),
                payload["prompt"].index("状況を説明してください"),
            )
            self.assertFalse(Path(payload["images"][0]).exists())
            self.assertEqual(list(temp_root.glob("eha-content-*")), [])

    def test_agy_translates_content_json_to_ordered_at_path_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "agy.json"
            fake = tmpdir / "agy"
            write_executable(
                fake,
                f"""
                #!/usr/bin/env python3
                import json
                import re
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                prompt = args[args.index("-p") + 1]
                images = re.findall(r"@([^\\s]+image-\\d+\\.[a-z]+)", prompt)
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{
                        "prompt": prompt,
                        "images": images,
                        "images_exist": [Path(path).is_file() for path in images],
                    }}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print('{{"ok":true}}')
                """,
            )
            content = json.dumps(
                [
                    {"type": "text", "text": "玄関:"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode(),
                        },
                    },
                    {"type": "text", "text": "確認してください"},
                ],
                ensure_ascii=False,
            )
            temp_root = tmpdir / "content-tmp"
            temp_root.mkdir()

            result = self.run_wrapper(
                ["--content-json", content, "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_TMP_DIR": temp_root.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(payload["images_exist"], [True])
            self.assertLess(payload["prompt"].index("玄関:"), payload["prompt"].index("【画像1】"))
            self.assertLess(
                payload["prompt"].index("【画像1】"),
                payload["prompt"].index("確認してください"),
            )
            self.assertFalse(Path(payload["images"][0]).exists())
            self.assertEqual(list(temp_root.glob("eha-content-*")), [])

    def test_agy_first_use_writes_site_config_and_registers_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            workdir = tmpdir / "workdir"
            agy_home = tmpdir / "agy-home"
            global_config = agy_home / ".gemini" / "config" / "mcp_config.json"
            global_config.parent.mkdir(parents=True)
            global_config.write_text('{"global":true}', encoding="utf-8")
            fake = self.write_project_fake_agy(tmpdir)

            result = self.run_wrapper(
                [
                    "--agent-site",
                    "explore",
                    "--mcp-servers",
                    "ha",
                    "--allowed-mcp-tools",
                    "mcp__ha__ha_get",
                    "hello",
                ],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": agy_home.as_posix(),
                    "EHA_CLAUDE_CWD": workdir.as_posix(),
                    "SUPERVISOR_TOKEN": "secret-token",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            site_dir = workdir / "explore"
            config_path = site_dir / ".agents" / "mcp_config.json"
            config_text = config_path.read_text(encoding="utf-8")
            config = json.loads(config_text)
            self.assertEqual(config["mcpServers"]["ha"]["includeTools"], ["ha_get"])
            self.assertNotIn("SUPERVISOR_TOKEN", config_text)
            self.assertNotIn("secret-token", config_text)
            credential_path = Path(config["mcpServers"]["ha"]["args"][1])
            self.assertEqual(
                credential_path.parent,
                agy_home
                / ".gemini"
                / "antigravity-cli"
                / "eha-mcp-credentials",
            )
            self.assertEqual(credential_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertFalse(credential_path.exists())
            manifest_path = site_dir / ".eha-mcp-tool-schemas.json"
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(
                [(item["server"], item["name"]) for item in manifest["tools"]],
                [("ha", "ha_get")],
            )
            self.assertNotIn("SUPERVISOR_TOKEN", manifest_text)
            self.assertNotIn("secret-token", manifest_text)
            self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
            project_id = (site_dir / ".eha_project_id").read_text(encoding="utf-8").strip()
            self.assertTrue(project_id.startswith("explore-"))
            self.assertEqual(global_config.read_text(encoding="utf-8"), '{"global":true}')
            records = self.read_agy_records(tmpdir)
            self.assertEqual(len(records), 1)
            self.assertIn("--new-project", records[0]["args"])
            self.assertEqual(records[0]["cwd"], str(site_dir))
            prompt = records[0]["args"][-1]
            self.assertIn("【Antigravity headlessでのツール利用】", prompt)
            self.assertIn(f"@{manifest_path}", prompt)
            self.assertIn("required・enum・型を厳守", prompt)
            self.assertIn(".agents/mcp_config.json", prompt)
            self.assertIn("調査対象にしないでください", prompt)
            self.assertIn("native command、write_file、shell、terminal", prompt)
            self.assertIn("read_file、WebSearch等", prompt)
            self.assertIn("確認できない事実は推測で補わず", prompt)

    def test_agy_without_mcp_does_not_add_headless_tool_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake = self.write_project_fake_agy(tmpdir)

            result = self.run_wrapper(
                ["hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": (tmpdir / "agy-home").as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            records = self.read_agy_records(tmpdir)
            self.assertEqual(len(records), 1)
            self.assertNotIn(
                "【Antigravity headlessでのツール利用】",
                records[0]["args"][-1],
            )

    def test_agy_reuses_existing_project_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            workdir = tmpdir / "workdir"
            site_dir = workdir / "chat"
            site_dir.mkdir(parents=True)
            (site_dir / ".eha_project_id").write_text("saved-project-123\n", encoding="utf-8")
            agy_home = tmpdir / "agy-home"
            fake = self.write_project_fake_agy(tmpdir)

            result = self.run_wrapper(
                [
                    "--agent-site",
                    "chat",
                    "--mcp-servers",
                    "ha",
                    "--allowed-mcp-tools",
                    "mcp__ha__ha_get",
                    "hello",
                ],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": agy_home.as_posix(),
                    "EHA_CLAUDE_CWD": workdir.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            records = self.read_agy_records(tmpdir)
            self.assertEqual(len(records), 1)
            self.assertNotIn("--new-project", records[0]["args"])
            self.assertEqual(records[0]["args"][records[0]["args"].index("--project") + 1], "saved-project-123")

    def _run_agy_with_servers(self, tmpdir, agy_home, extra_args):
        workdir = tmpdir / "workdir"
        site_dir = workdir / "chat"
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / ".eha_project_id").write_text("saved-project-123\n", encoding="utf-8")
        fake = tmpdir / "agy"
        if not fake.exists():
            fake = self.write_project_fake_agy(tmpdir)
        return self.run_wrapper(
            ["--agent-site", "chat", *extra_args, "hello"],
            {
                "EHA_AGENT_HARNESS": "agy",
                "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                "EHA_ANTIGRAVITY_HOME": agy_home.as_posix(),
                "EHA_CLAUDE_CWD": workdir.as_posix(),
            },
        )

    def test_agy_writes_server_wildcard_permission_grants(self):
        # agy 1.1.3 headlessはconfig.jsonのglobalPermissionGrantsだけを実行承認に
        # 使う(settings.jsonのpermissions.allowは無視される。実機切り分け済み、
        # 2026-07-17)。グラントは接続サーバー単位のワイルドカードmcp(server/*)——
        # 完全一致だとモデルがグラント外ツール名を呼んだ時点でprintモードが
        # ターン全体を打ち切るため(実測)。--allowed-mcp-toolsの有無はグラントに
        # 影響しない(それはincludeTools=可視性側の入力)。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            config_path = agy_home / ".gemini" / "config" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                '{"userSettings": {"remoteControlHostname": "keep-me"}}',
                encoding="utf-8",
            )

            result = self._run_agy_with_servers(
                tmpdir, agy_home,
                ["--mcp-servers", "ha memory",
                 "--allowed-mcp-tools", "mcp__ha__ha_get,mcp__memory__recall"],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["userSettings"]["remoteControlHostname"], "keep-me")
            self.assertEqual(
                config["userSettings"]["globalPermissionGrants"]["allow"],
                ["mcp(ha/*)", "mcp(memory/*)"],
            )

    def test_agy_migrates_native_read_policy_and_preserves_future_user_grants(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            settings_path = agy_home / ".gemini" / "antigravity-cli" / "settings.json"
            config_path = agy_home / ".gemini" / "config" / "config.json"
            settings_path.parent.mkdir(parents=True)
            config_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps({
                    "allowNonWorkspaceAccess": True,
                    "permissions": {
                        "allow": ["read_file(*)", "browser(example.com)"],
                        "deny": ["read_file(*)", "browser(blocked.example)"],
                        "ask": ["browser(*)"],
                    },
                    "theme": "keep-me",
                }),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps({
                    "userSettings": {
                        "remoteControlHostname": "keep-me",
                        "globalPermissionGrants": {
                            "allow": ["read_file(*)", "mcp(existing/*)"],
                        },
                    },
                }),
                encoding="utf-8",
            )
            os.chmod(settings_path, 0o640)
            os.chmod(config_path, 0o640)
            fake = self.write_project_fake_agy(tmpdir)

            env = {
                "EHA_AGENT_HARNESS": "agy",
                "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                "EHA_ANTIGRAVITY_HOME": agy_home.as_posix(),
            }
            result = self.run_wrapper(["hello"], env)
            self.assertEqual(result.returncode, 0, result.stderr)

            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["theme"], "keep-me")
            self.assertFalse(settings["allowNonWorkspaceAccess"])
            self.assertEqual(
                settings["permissions"]["allow"],
                ["browser(example.com)"],
            )
            self.assertEqual(settings["permissions"]["ask"], ["browser(*)"])
            self.assertEqual(
                settings["permissions"]["deny"],
                [
                    "browser(blocked.example)",
                    "command(*)",
                    "write_file(*)",
                    "read_file(/config)",
                    "read_file(/data)",
                    "read_file(/proc)",
                    "read_file(/root)",
                    "read_file(/run/secrets)",
                ],
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["userSettings"]["remoteControlHostname"], "keep-me")
            self.assertEqual(
                config["userSettings"]["globalPermissionGrants"]["allow"],
                ["mcp(existing/*)"],
            )
            self.assertEqual(settings_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)
            marker = settings_path.parent / ".eha-native-read-policy-v1"
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

            # marker後にユーザーが明示的に同名grantを追加した場合は、EHA由来と
            # 区別できないため再削除しない。core denyは引き続き維持する。
            settings["permissions"]["allow"].append("read_file(*)")
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
            config["userSettings"]["globalPermissionGrants"]["allow"].append(
                "read_file(*)"
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result = self.run_wrapper(["hello again"], env)
            self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIn("read_file(*)", settings["permissions"]["allow"])
            self.assertIn(
                "read_file(*)",
                config["userSettings"]["globalPermissionGrants"]["allow"],
            )

    def test_agy_native_safety_policy_fails_closed_on_invalid_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            settings_path = agy_home / ".gemini" / "antigravity-cli" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            original = json.dumps({"permissions": {"deny": "command(*)"}})
            settings_path.write_text(original, encoding="utf-8")
            fake = self.write_project_fake_agy(tmpdir)

            result = self.run_wrapper(
                ["hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": agy_home.as_posix(),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("permissions.deny is not a list", result.stderr)
            self.assertEqual(settings_path.read_text(encoding="utf-8"), original)

    def test_agy_extracts_json_encoded_string_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake = tmpdir / "agy"
            write_executable(
                fake,
                """
                #!/usr/bin/env python3
                import json
                print(json.dumps('{"private":"考えたこと","speak":null}', ensure_ascii=False))
                """,
            )

            result = self.run_wrapper(
                ["hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": (tmpdir / "agy-home").as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {"private": "考えたこと", "speak": None},
            )

    def test_agy_does_not_grant_native_read_file_when_read_intended(self):
        # Read は files MCP へ写像し、native read_file は機密パス policy の迂回を防ぐため配布しない。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            config_path = agy_home / ".gemini" / "config" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"userSettings": {}}', encoding="utf-8")

            result = self._run_agy_with_servers(
                tmpdir, agy_home,
                ["--mcp-servers", "ha", "--allowed-builtins", "Read"],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            allow = config["userSettings"]["globalPermissionGrants"]["allow"]
            self.assertNotIn("read_file(*)", allow)
            self.assertIn("mcp(ha/*)", allow)

    def test_agy_does_not_grant_read_file_without_read_builtin(self):
        # Read 意図が無ければ read_file(*) は配らない(intent gate)。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            config_path = agy_home / ".gemini" / "config" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"userSettings": {}}', encoding="utf-8")

            result = self._run_agy_with_servers(
                tmpdir, agy_home, ["--mcp-servers", "ha"],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            allow = config["userSettings"]["globalPermissionGrants"]["allow"]
            self.assertNotIn("read_file(*)", allow)

    def test_agy_permission_grants_remove_legacy_read_once_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            config_path = agy_home / ".gemini" / "config" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"userSettings": {"globalPermissionGrants": {
                    "allow": ["mcp(ha/*)", "read_file(*)"]}}}),
                encoding="utf-8",
            )

            for _ in range(2):
                result = self._run_agy_with_servers(
                    tmpdir, agy_home,
                    ["--mcp-servers", "ha memory",
                     "--allowed-mcp-tools", "mcp__ha__ha_get,mcp__memory__recall"],
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                config["userSettings"]["globalPermissionGrants"]["allow"],
                ["mcp(ha/*)", "mcp(memory/*)"],
            )

    def test_agy_permission_grants_written_without_allowed_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"

            result = self._run_agy_with_servers(
                tmpdir, agy_home, ["--mcp-servers", "ha"],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config_path = agy_home / ".gemini" / "config" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                config["userSettings"]["globalPermissionGrants"]["allow"],
                ["mcp(ha/*)"],
            )

    def test_agy_permission_grants_die_on_corrupt_config_without_clobbering(self):
        # 壊れた既存config.jsonを黙って{}で全置換するとuserSettingsの他キーを
        # 失うため、fail-closedで停止しファイルへ触れないこと(sol review指摘)。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            config_path = agy_home / ".gemini" / "config" / "config.json"
            settings_path = agy_home / ".gemini" / "antigravity-cli" / "settings.json"
            config_path.parent.mkdir(parents=True)
            settings_path.parent.mkdir(parents=True)
            config_path.write_text("{broken json", encoding="utf-8")
            original_settings = json.dumps({"permissions": {"deny": ["read_file(*)"]}})
            settings_path.write_text(original_settings, encoding="utf-8")

            result = self._run_agy_with_servers(
                tmpdir, agy_home, ["--mcp-servers", "ha"],
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("native safety policy failed", result.stderr)
            self.assertEqual(config_path.read_text(encoding="utf-8"), "{broken json")
            self.assertEqual(settings_path.read_text(encoding="utf-8"), original_settings)

    def test_agy_permission_grants_die_on_invalid_nested_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            config_path = agy_home / ".gemini" / "config" / "config.json"
            config_path.parent.mkdir(parents=True)
            original = json.dumps({"userSettings": {"globalPermissionGrants": {"allow": "mcp(ha/*)"}}})
            config_path.write_text(original, encoding="utf-8")

            result = self._run_agy_with_servers(
                tmpdir, agy_home, ["--mcp-servers", "ha"],
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("allow is not a list", result.stderr)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_agy_permission_grants_preserve_file_mode_and_skip_rewrite_when_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            config_path = agy_home / ".gemini" / "config" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"userSettings": {"globalPermissionGrants": {"allow": ["mcp(ha/*)"]}}}),
                encoding="utf-8",
            )
            os.chmod(config_path, 0o600)

            result = self._run_agy_with_servers(
                tmpdir, agy_home, ["--mcp-servers", "ha"],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            # 既に必要なグラントが揃っている場合は書き換え自体が起きないこと
            mtime_before = config_path.stat().st_mtime_ns
            result = self._run_agy_with_servers(
                tmpdir, agy_home, ["--mcp-servers", "ha"],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config_path.stat().st_mtime_ns, mtime_before)

    def test_agy_mcp_requires_agent_site_and_accepts_allowed_builtins(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = self.write_project_fake_agy(Path(tmp))
            # hermetic: agent-site の作業場所を temp に固定し、repo 直下 chat/.agents を作らない(sol Med)。
            work = Path(tmp) / "work"
            work.mkdir()

            missing_site = self.run_wrapper(
                ["--mcp-servers", "ha", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": work.as_posix(),
                },
            )
            # 契約変更(2026-07-23・F4): agy も --allowed-builtins で die しない。Read は files MCP、
            # WebSearch(agy native)の grant 形式は 2.1.0(§8)。ここでは受理して無視することを確認。
            with_builtins = self.run_wrapper(
                ["--agent-site", "chat", "--mcp-servers", "ha", "--allowed-builtins", "Read", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": work.as_posix(),
                },
            )

            self.assertNotEqual(missing_site.returncode, 0)
            self.assertIn("--agent-site is required for agy MCP config", missing_site.stderr)
            self.assertEqual(with_builtins.returncode, 0, with_builtins.stderr)
            self.assertNotIn("--allowed-builtins is not supported for agy", with_builtins.stderr)

    def test_agent_site_is_ignored_by_codex_cwd_selection(self):
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
                out_path = args[args.index("-o") + 1]
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"args": args}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                Path(out_path).write_text('{{"ok":true}}', encoding="utf-8")
                """,
            )

            result = self.run_wrapper(
                ["--agent-site", "chat", "hello"],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": fake.as_posix(),
                    "EHA_AGENT_CWD": "/tmp/codex-cwd",
                    "EHA_CLAUDE_CWD": "/tmp/claude-sites",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(record.read_text(encoding="utf-8"))["args"]
            self.assertEqual(args[args.index("-C") + 1], "/tmp/codex-cwd")

    def test_agent_site_prefers_eha_agent_cwd_over_eha_claude_cwd(self):
        # invoke-agent-caller-wiring-phase2-spec.md 増分1: agyのsite_dir解決も
        # EHA_AGENT_CWDを優先するよう揃えた（旧: EHA_CLAUDE_CWD優先）。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy_home = tmpdir / "agy-home"
            fake = self.write_project_fake_agy(tmpdir)
            agent_workdir = tmpdir / "agent-workdir"
            claude_workdir = tmpdir / "claude-workdir"

            result = self.run_wrapper(
                ["--agent-site", "chat", "--mcp-servers", "ha", "--allowed-mcp-tools", "mcp__ha__ha_get", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                    "EHA_ANTIGRAVITY_HOME": agy_home.as_posix(),
                    "EHA_AGENT_CWD": agent_workdir.as_posix(),
                    "EHA_CLAUDE_CWD": claude_workdir.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((agent_workdir / "chat" / ".agents" / "mcp_config.json").exists())
            self.assertFalse((claude_workdir / "chat").exists())
            records = self.read_agy_records(tmpdir)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["cwd"], str(agent_workdir / "chat"))

    def test_agy_parallel_first_registration_is_serialized_by_global_flock(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            workdir = tmpdir / "workdir"
            agy_home = tmpdir / "agy-home"
            fake = self.write_project_fake_agy(tmpdir)
            base_env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "HA_URL": "http://example.invalid",
                "EHA_AGENT_HARNESS": "agy",
                "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                "EHA_ANTIGRAVITY_HOME": agy_home.as_posix(),
                "EHA_CLAUDE_CWD": workdir.as_posix(),
            }
            commands = [
                [SCRIPT.as_posix(), "--agent-site", "explore", "--mcp-servers", "ha", "hello"],
                [SCRIPT.as_posix(), "--agent-site", "chat", "--mcp-servers", "ha", "hello"],
            ]
            procs = [
                subprocess.Popen(
                    cmd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=ROOT,
                    env=base_env,
                )
                for cmd in commands
            ]
            results = [proc.communicate(timeout=10) + (proc.returncode,) for proc in procs]

            for stdout, stderr, returncode in results:
                self.assertEqual(returncode, 0, stderr)
                self.assertEqual(json.loads(stdout), {"ok": True})
            explore_id = (workdir / "explore" / ".eha_project_id").read_text(encoding="utf-8").strip()
            chat_id = (workdir / "chat" / ".eha_project_id").read_text(encoding="utf-8").strip()
            self.assertNotEqual(explore_id, chat_id)
            self.assertTrue(explore_id.startswith("explore-"))
            self.assertTrue(chat_id.startswith("chat-"))
            concurrency = json.loads((tmpdir / "agy-records" / "concurrency.json").read_text(encoding="utf-8"))
            self.assertEqual(concurrency["max"], 1)


if __name__ == "__main__":
    unittest.main()


class ClaudeSelfUpdateSuppressionTests(unittest.TestCase):
    # InvokeAgentTests を継承すると既存50件まで再実行されるので、必要な起動ヘルパだけ持つ。
    run_wrapper = InvokeAgentTests.run_wrapper

    def test_claude_runs_with_self_updates_disabled(self):
        """管理下の DIY バイナリが自分で入れ替わらないこと。

        `claude_setup.runtime_env()` は DISABLE_UPDATES=1 を宣言し、呼び出し側が 0 を
        渡しても 1 に上書きするテストまであるが、この実行経路からは呼ばれておらず
        環境をそのまま継承していた（2026-08-13 に判明）。宣言だけあって効いていない
        状態を、実際の起動側で固定する。
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "env.json"
            fake = tmpdir / "claude"
            write_executable(
                fake,
                f"""
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                sys.stdin.read()
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"disable_updates": os.environ.get("DISABLE_UPDATES")}}),
                    encoding="utf-8",
                )
                print(json.dumps({{"type": "result", "result": "{{}}"}}))
                """,
            )
            result = self.run_wrapper(
                ["--model", "lite", "prompt"],
                {"EHA_AGENT_HARNESS": "claude", "EHA_CLAUDE_BIN": fake.as_posix()},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(record.read_text(encoding="utf-8"))["disable_updates"], "1"
            )


class ExtractResultJsonTests(unittest.TestCase):
    """`extract_result_json` が「整形された JSON の配列要素」を最終応答と取り違えないこと。

    2026-08-14 に特定した実害: agy が JSON を整形して出すと、`scene_people` などの
    配列要素の行（例 `  "yuno"`）が単独で有効な JSON 文字列であるため、直前に読んだ
    オブジェクト全体を上書きしていた。配列を持つのは observe のスキーマだけなので、
    **observe だけ**が 8 日間で 44 回パース失敗として捨てられた（他モードは 0 件）。

    実データとの整合: 断片に emotion 値・真偽値・数値・改行入りが 1 件も無い。
    いずれも「配列要素として現れ得ない」ものであり、この機構と一致する。
    """

    SCRIPT = ROOT / "embodied_ha" / "invoke-agent.sh"

    @classmethod
    def setUpClass(cls):
        import re as _re

        source = cls.SCRIPT.read_text(encoding="utf-8")
        match = _re.search(r"extract_result_json\(\) \{\n  python3 -c '\n(.*?)\n'\n\}", source, _re.DOTALL)
        assert match, "extract_result_json を取り出せない"
        cls.extractor = match.group(1)

    def _extract(self, payload):
        return subprocess.run(
            [sys.executable, "-c", self.extractor],
            input=payload, capture_output=True, text=True, check=False,
        ).stdout

    OBSERVE = {
        "topic": "スタディの室温上昇", "speak": None, "private": "暑いな",
        "emotion": "concerned", "feature_presented": None, "proposal": None, "action": None,
        "scene_objects": ["エアコン"], "scene_people": ["yuno"],
        "scene_changes": ["pixel_9a_charging"],
    }

    def test_pretty_printed_object_with_arrays_survives(self):
        out = self._extract(json.dumps(self.OBSERVE, ensure_ascii=False, indent=2))
        self.assertEqual(json.loads(out), self.OBSERVE)

    def test_compact_object_survives(self):
        out = self._extract(json.dumps(self.OBSERVE, ensure_ascii=False))
        self.assertEqual(json.loads(out), self.OBSERVE)

    def test_double_encoded_object_is_still_accepted(self):
        # この分岐が元々存在する理由。壊していないことを固定する。
        inner = json.dumps(self.OBSERVE, ensure_ascii=False)
        out = self._extract(json.dumps(inner, ensure_ascii=False))
        self.assertEqual(json.loads(out), self.OBSERVE)

    def test_claude_stream_json_result_is_unaffected(self):
        out = self._extract(json.dumps({"type": "result", "structured_output": {"ok": True}}))
        self.assertEqual(json.loads(out), {"ok": True})

    def test_agy_envelope_is_unaffected(self):
        out = self._extract(json.dumps(
            {"conversation_id": "x", "status": "SUCCESS", "response": "",
             "structured_output": self.OBSERVE}))
        self.assertEqual(json.loads(out), self.OBSERVE)

    def test_prose_answer_still_passes_through(self):
        prose = "ユーザーさん、お疲れさま！\n報告するね。"
        self.assertIn("お疲れさま", self._extract(prose))
