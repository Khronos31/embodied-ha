import json
import os
import subprocess
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


def write_silent_wav(path: Path, *, duration_sec: float = 0.2) -> None:
    import wave

    frame_rate = 16000
    n_frames = int(frame_rate * duration_sec)
    with wave.open(path.as_posix(), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(frame_rate)
        fh.writeframes(b"\x00\x00" * n_frames)


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
            self.assertEqual(args[args.index("--model") + 1], "gpt-5.6-luna")
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

    def test_claude_emits_raw_stream_on_stderr_for_tool_use_extraction(self):
        # loop.pyのfacts抽出(introspection_facts.extract_facts_from_stream_text)は
        # assistant/userイベント中のtool_use/tool_resultを必要とするが、stdoutは
        # extract_result_json()が最終resultイベントだけに絞ってしまう。run_codex()の
        # 「生transcriptはstderr、構造化結果はstdout」契約をrun_claude()にも揃え、
        # callerがstderrから生ストリームを読めるようにする(2026-07-16)。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake = tmpdir / "claude"
            write_executable(
                fake,
                """
                #!/usr/bin/env python3
                import json
                print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1", "name": "mcp__ha__ha_get", "input": {}}]}}, ensure_ascii=False))
                print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1"}]}}, ensure_ascii=False))
                print(json.dumps({"type": "result", "structured_output": {"ok": True}}, ensure_ascii=False))
                """,
            )

            result = self.run_wrapper(
                ["hello"],
                {
                    "EHA_AGENT_HARNESS": "claude",
                    "EHA_CLAUDE_BIN": fake.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"ok": True})
            stderr_events = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
            self.assertEqual([e["type"] for e in stderr_events], ["assistant", "user", "result"])

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

    def test_sound_file_forces_agy_high_even_when_harness_is_codex(self):
        # 仕様変更(2026-07-17): --sound-fileは実機検証(agyのGo content-sniffがWAVを
        # audio/waveと誤判定しGemini APIに拒否される)により、WAVをwebmへffmpeg変換した
        # 上でview_file専用の明示指示付きプロンプトを組み立てるようになった。従来の
        # 「@<元のwavパス>がそのままプロンプトに載る」という前提は成立しなくなったため、
        # 実在するWAVフィクスチャを使い、webm変換後のパス参照とツール利用禁止指示を
        # 検証する形にアサーションを更新した。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "agy.json"
            agy = tmpdir / "agy"
            codex = tmpdir / "codex"
            wav_path = tmpdir / "input.wav"
            write_silent_wav(wav_path)
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
                ["--model", "lite", "--sound-file", wav_path.as_posix(), "listen"],
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
            self.assertIn("view_fileで下記の音声ファイルを読み込んで内容を理解してください", prompt)
            self.assertIn("command/shell/Pythonなどの実行ツールや外部スクリプトによる解析は禁止です", prompt)
            self.assertNotIn(wav_path.as_posix(), prompt)
            self.assertRegex(prompt, r"@\S+\.webm")

    def test_sound_file_does_not_force_high_when_harness_already_agy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "agy.json"
            agy = tmpdir / "agy"
            wav_path = tmpdir / "input.wav"
            write_silent_wav(wav_path)
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

            result = self.run_wrapper(
                ["--model", "lite", "--sound-file", wav_path.as_posix(), "listen"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": agy.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            args = payload["args"]
            self.assertEqual(args[args.index("--model") + 1], "Gemini 3.5 Flash (Low)")
            prompt = args[args.index("-p") + 1]
            self.assertIn("command/shell/Pythonなどの実行ツールや外部スクリプトによる解析は禁止です", prompt)
            self.assertRegex(prompt, r"@\S+\.webm")

    def test_sound_file_uses_session_model_over_default_tier_for_agy(self):
        # sol Med3: agy 選択でも深聴き(EHA_SESSION_MODEL 指定)は default ティア prefs より
        # session モデルを優先し、STT 品質を prefs で劣化させない。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "agy.json"
            agy = tmpdir / "agy"
            wav_path = tmpdir / "input.wav"
            write_silent_wav(wav_path)
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
            result = self.run_wrapper(
                ["--model", "lite", "--sound-file", wav_path.as_posix(), "listen"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": agy.as_posix(),
                    "EHA_AGY_MODEL_DEFAULT": "Gemini 3.5 Flash (Low)",  # prefs 相当の低モデル
                    "EHA_SESSION_MODEL": "Gemini 3.5 Flash (High)",     # 深聴きが指定する音声モデル
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(record.read_text(encoding="utf-8"))["args"]
            self.assertEqual(args[args.index("--model") + 1], "Gemini 3.5 Flash (High)")

    def test_sound_file_missing_dies(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            agy = tmpdir / "agy"
            write_executable(
                agy,
                """
                #!/usr/bin/env bash
                echo "agy must not be called" >&2
                exit 99
                """,
            )

            result = self.run_wrapper(
                ["--model", "lite", "--sound-file", (tmpdir / "missing.wav").as_posix(), "listen"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": agy.as_posix(),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--sound-file not found", result.stderr)

    def test_sound_file_webm_conversion_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            record = tmpdir / "agy.json"
            agy = tmpdir / "agy"
            wav_path = tmpdir / "input.wav"
            write_silent_wav(wav_path)
            write_executable(
                agy,
                f"""
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                prompt = args[args.index("-p") + 1]
                webm_path = prompt.split("@", 1)[1].strip()
                Path({record.as_posix()!r}).write_text(
                    json.dumps({{"webm_path": webm_path, "exists": Path(webm_path).exists()}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                print('{{"ok":true}}')
                """,
            )

            result = self.run_wrapper(
                ["--model", "lite", "--sound-file", wav_path.as_posix(), "listen"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": agy.as_posix(),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(record.read_text(encoding="utf-8"))
            self.assertTrue(payload["exists"], "webm temp file must exist while agy runs")
            self.assertFalse(Path(payload["webm_path"]).exists(), "webm temp file must be cleaned up after run")

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

    def test_codex_rejects_allowed_builtins(self):
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
                ["--mcp-servers", "ha", "--allowed-builtins", "Read", "hello"],
                {
                    "EHA_AGENT_HARNESS": "codex",
                    "EHA_CODEX_BIN": fake.as_posix(),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--allowed-builtins is not supported for codex", result.stderr)

    def test_codex_rejects_content_json_including_at_path_form(self):
        # content_json_set(2026-07-16の@<path>対応)がinline/@path両方の指定を
        # 正しく検出し、codexへ回さずに拒否できることを確認する。
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake = tmpdir / "codex"
            write_executable(
                fake,
                """
                #!/usr/bin/env bash
                echo "codex must not be called" >&2
                exit 99
                """,
            )
            content_path = tmpdir / "content.json"
            content_path.write_text("[]", encoding="utf-8")

            inline = self.run_wrapper(
                ["--content-json", "[]", "hello"],
                {"EHA_AGENT_HARNESS": "codex", "EHA_CODEX_BIN": fake.as_posix()},
            )
            at_path = self.run_wrapper(
                ["--content-json", f"@{content_path}", "hello"],
                {"EHA_AGENT_HARNESS": "codex", "EHA_CODEX_BIN": fake.as_posix()},
            )

            self.assertNotEqual(inline.returncode, 0)
            self.assertIn("--content-json is not supported for codex", inline.stderr)
            self.assertNotEqual(at_path.returncode, 0)
            self.assertIn("--content-json is not supported for codex", at_path.stderr)

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
            config = json.loads((site_dir / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["mcpServers"]["ha"]["includeTools"], ["ha_get"])
            self.assertEqual(config["mcpServers"]["ha"]["env"]["SUPERVISOR_TOKEN"], "secret-token")
            project_id = (site_dir / ".eha_project_id").read_text(encoding="utf-8").strip()
            self.assertTrue(project_id.startswith("explore-"))
            self.assertEqual(global_config.read_text(encoding="utf-8"), '{"global":true}')
            records = self.read_agy_records(tmpdir)
            self.assertEqual(len(records), 1)
            self.assertIn("--new-project", records[0]["args"])
            self.assertEqual(records[0]["cwd"], str(site_dir))

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

    def test_agy_permission_grants_merge_is_add_only_and_dedupes(self):
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
                ["mcp(ha/*)", "read_file(*)", "mcp(memory/*)"],
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
            config_path.parent.mkdir(parents=True)
            config_path.write_text("{broken json", encoding="utf-8")

            result = self._run_agy_with_servers(
                tmpdir, agy_home, ["--mcp-servers", "ha"],
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("grants merge failed", result.stderr)
            self.assertEqual(config_path.read_text(encoding="utf-8"), "{broken json")

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

    def test_agy_mcp_requires_agent_site_and_rejects_allowed_builtins(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = self.write_project_fake_agy(Path(tmp))

            missing_site = self.run_wrapper(
                ["--mcp-servers", "ha", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                },
            )
            bad_builtins = self.run_wrapper(
                ["--agent-site", "chat", "--mcp-servers", "ha", "--allowed-builtins", "Read", "hello"],
                {
                    "EHA_AGENT_HARNESS": "agy",
                    "EHA_ANTIGRAVITY_BIN": fake.as_posix(),
                },
            )

            self.assertNotEqual(missing_site.returncode, 0)
            self.assertIn("--agent-site is required for agy MCP config", missing_site.stderr)
            self.assertNotEqual(bad_builtins.returncode, 0)
            self.assertIn("--allowed-builtins is not supported for agy", bad_builtins.stderr)

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
