import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import loop  # noqa: E402


class LoopPyModeSelectionTests(unittest.TestCase):
    def test_choose_mode_respects_explicit_mode(self):
        self.assertEqual(loop.choose_mode({"MODE": "reflect"}), "reflect")

    def test_compute_mode_weights_applies_anomaly_urgency(self):
        normal = loop.compute_mode_weights({}, anomaly_urgency=0, github_app_exists=True)
        urgent = loop.compute_mode_weights({}, anomaly_urgency=10, github_app_exists=True)

        self.assertGreater(urgent["observe"], normal["observe"])
        self.assertGreater(urgent["explore"], normal["explore"])
        self.assertEqual(urgent["reflect"], normal["reflect"])

    def test_choose_mode_uses_env_anomaly_urgency_and_disables_social_without_github_app(self):
        captured = {}

        def fake_choices(modes, weights, k):
            captured["modes"] = modes
            captured["weights"] = weights
            captured["k"] = k
            return ["explore"]

        with tempfile.TemporaryDirectory() as tmpdir:
            mode = loop.choose_mode(
                {
                    "EHA_BODY_STATE": json.dumps({"curiosity": 0.5, "energy": 0.5, "stress": 0.0}),
                    "ANOMALY_URGENCY": "10",
                    "EHA_GITHUB_APP_PEM": str(Path(tmpdir) / "missing.pem"),
                },
                choices=fake_choices,
            )

        self.assertEqual(mode, "explore")
        weights = dict(zip(captured["modes"], captured["weights"]))
        self.assertEqual(captured["k"], 1)
        self.assertEqual(weights["social"], 0)
        self.assertEqual(weights["observe"], 38)
        self.assertEqual(weights["explore"], 47)


class LoopPyInvocationTests(unittest.TestCase):
    def test_build_loop_claude_command_uses_schema_and_mcp_config(self):
        cmd = loop.build_loop_claude_command(
            claude_bin="/bin/claude",
            model="sonnet",
            mode="reflect",
            allowed_tools="mcp__memory__recall",
            system_prompt="system",
            mcp_config="/tmp/mcp.json",
        )

        self.assertEqual(cmd[:4], ["/bin/claude", "-p", "--model", "sonnet"])
        self.assertIn("--json-schema", cmd)
        self.assertIn("--mcp-config", cmd)
        self.assertIn("/tmp/mcp.json", cmd)
        self.assertIn("mcp__memory__recall", cmd)

    def test_invoke_loop_claude_is_claude_only_and_returns_structured_output(self):
        calls = []

        class Result:
            def __init__(self, stdout=""):
                self.stdout = stdout

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[0] == "python3":
                Path(cmd[2]).parent.mkdir(parents=True, exist_ok=True)
                Path(cmd[2]).write_text("{}", encoding="utf-8")
                return Result()
            payload = {
                "type": "result",
                "structured_output": {"private": "静かに考えた", "emotion": "calm", "speak": None},
            }
            return Result(json.dumps(payload, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmpdir:
            response = loop.invoke_loop_claude(
                user_prompt="user",
                system_prompt="system",
                mode="reflect",
                allowed_tools="mcp__memory__recall",
                mcp_servers=["memory"],
                environ={
                    "SCRIPT_DIR": str(ROOT / "embodied_ha"),
                    "CLAUDE_BIN": "/bin/claude",
                    "EHA_SESSION_BIN": "agy",
                    "EHA_DATA_DIR": tmpdir,
                },
                run=fake_run,
            )

        self.assertEqual(json.loads(response)["private"], "静かに考えた")
        claude_calls = [call for call in calls if call[0][0] == "/bin/claude"]
        self.assertEqual(len(claude_calls), 1)
        claude_cmd, claude_kwargs = claude_calls[0]
        self.assertNotIn("agy", claude_cmd)
        self.assertIn("--mcp-config", claude_cmd)
        self.assertEqual(claude_kwargs["cwd"], str(Path(tmpdir) / "workdir"))
        envelope = json.loads(claude_kwargs["input"])
        self.assertEqual(envelope["type"], "user")
        self.assertEqual(envelope["message"]["content"][0]["text"], "user")


class LoopPyPostprocessTests(unittest.TestCase):
    def test_pending_proposal_requires_action_triplet(self):
        payload = loop.pending_proposal_payload(
            {
                "proposal": "電気を消しましょうか",
                "action": {"domain": "light", "service": "turn_off", "entity_id": "light.living"},
            },
            timestamp="2026-07-15T12:00:00+09:00",
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["proposal"], "電気を消しましょうか")

        self.assertIsNone(loop.pending_proposal_payload({"proposal": "x", "action": {"domain": "light"}}, timestamp="t"))

    def test_write_pending_proposal_and_speak_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pending_proposal.json"
            payload = {
                "timestamp": "t",
                "proposal": "電気を消しましょうか",
                "action": {"domain": "light", "service": "turn_off", "entity_id": "light.living"},
            }
            self.assertTrue(loop.write_pending_proposal(path, payload))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["proposal"], "電気を消しましょうか")

        plan = loop.loop_speak_plan({"_parse_ok": True, "speak": "あとで見ます"}, payload)
        self.assertEqual(plan["tts"], "電気を消しましょうか")
        self.assertEqual(plan["say"], "あとで見ます")

    def test_first_speaker_room_supports_list_and_dict_shapes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(json.dumps({"speakers": [{"room": "study"}]}), encoding="utf-8")
            self.assertEqual(loop.first_speaker_room(prefs), "study")
            prefs.write_text(json.dumps({"speakers": {"living": {"type": "tcp"}}}), encoding="utf-8")
            self.assertEqual(loop.first_speaker_room(prefs), "living")

    def test_append_loop_chat_log_uses_loop_jsonl_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "chat_log.jsonl"
            loop.append_loop_chat_log(path, timestamp="t", source="reflect", claude="考えています")
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(rows, [{"timestamp": "t", "source": "reflect", "claude": "考えています", "user": None}])


class LoopPyPersistenceTests(unittest.TestCase):
    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_parse_failure_records_error_but_skips_observation_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed = loop.parse_loop_response("plain raw failure")

            should_skip = loop.record_parse_skip_if_needed(
                parsed=parsed,
                response="plain raw failure",
                log_dir=tmp,
                timestamp="2026-07-15T12:00:00+09:00",
                mode="observe",
            )
            persisted = loop.persist_loop_introspection(
                parsed=parsed,
                mode="observe",
                timestamp="2026-07-15T12:00:00+09:00",
                observation_log=tmp / "observations.jsonl",
                explore_log=tmp / "explore.jsonl",
            )

            self.assertTrue(should_skip)
            self.assertFalse(persisted)
            self.assertEqual(self.read_jsonl(tmp / "observations.jsonl"), [])
            errors = self.read_jsonl(tmp / "loop_parse_errors.jsonl")
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "json_parse_failed")
            self.assertEqual(errors[0]["raw"], "plain raw failure")

    def test_valid_observe_introspection_persists_observation_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed = {
                "_parse_ok": True,
                "private": "静かに見守っている",
                "emotion": "calm",
                "topic": "watch",
            }

            should_skip = loop.record_parse_skip_if_needed(
                parsed=parsed,
                response=json.dumps(parsed, ensure_ascii=False),
                log_dir=tmp,
                timestamp="2026-07-15T12:00:00+09:00",
                mode="observe",
            )
            persisted = loop.persist_loop_introspection(
                parsed=parsed,
                mode="observe",
                timestamp="2026-07-15T12:00:00+09:00",
                observation_log=tmp / "observations.jsonl",
                explore_log=tmp / "explore.jsonl",
            )

            self.assertFalse(should_skip)
            self.assertTrue(persisted)
            rows = self.read_jsonl(tmp / "observations.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["private"], "静かに見守っている")
            self.assertEqual(rows[0]["emotion"], "calm")
            self.assertEqual(self.read_jsonl(tmp / "loop_parse_errors.jsonl"), [])

    def test_valid_non_observe_introspection_persists_explore_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed = {
                "_parse_ok": True,
                "private": "記憶の連なりを見ている",
                "emotion": "thoughtful",
                "topic": "memory",
            }

            persisted = loop.persist_loop_introspection(
                parsed=parsed,
                mode="reflect",
                timestamp="2026-07-15T12:00:00+09:00",
                observation_log=tmp / "observations.jsonl",
                explore_log=tmp / "explore.jsonl",
            )

            self.assertTrue(persisted)
            rows = self.read_jsonl(tmp / "explore.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["mode"], "reflect")
            self.assertEqual(rows[0]["topic"], "memory")
            self.assertEqual(rows[0]["private"], "記憶の連なりを見ている")

    def test_empty_introspection_records_skip_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed = {"_parse_ok": True, "private": "", "emotion": ""}

            should_skip = loop.record_parse_skip_if_needed(
                parsed=parsed,
                response="{}",
                log_dir=tmp,
                timestamp="2026-07-15T12:00:00+09:00",
                mode="explore",
            )

            self.assertTrue(should_skip)
            errors = self.read_jsonl(tmp / "loop_parse_errors.jsonl")
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "empty_introspection")


class LoopPyStandaloneRunTests(unittest.TestCase):
    class Result:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def fake_run_factory(self, calls):
        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:2] == ["loops", "list"]:
                return self.Result("なし\n")
            if cmd[:2] == ["loops", "list-json"]:
                return self.Result("[]\n")
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("render-sensors.py"):
                return self.Result("# sensors\n異常なし\n")
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("body-context.py"):
                return self.Result("# 身体位置\nリビング\n")
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("boundary.py"):
                return self.Result('{"allowed": false}\n')
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("feature-flags.py"):
                return self.Result("\n")
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("mcp-config.py"):
                Path(cmd[2]).parent.mkdir(parents=True, exist_ok=True)
                Path(cmd[2]).write_text("{}", encoding="utf-8")
                return self.Result()
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("speak.py"):
                return self.Result()
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("daybook_rollup.py"):
                return self.Result()
            if cmd and cmd[0] == "curl":
                return self.Result()
            if cmd and cmd[0] == "/bin/claude":
                payload = {
                    "type": "result",
                    "structured_output": {
                        "topic": "fixture",
                        "private": "静かに確認している",
                        "emotion": "calm",
                        "speak": "あとで見ます",
                        "proposal": None,
                        "feature_presented": None,
                    },
                }
                return self.Result(json.dumps(payload, ensure_ascii=False) + "\n")
            return self.Result()
        return fake_run

    def make_env(self, tmp: Path, mode: str) -> dict[str, str]:
        prefs = tmp / "preferences.json"
        prefs.write_text(json.dumps({"speakers": [{"room": "living"}], "cameras": []}), encoding="utf-8")
        character = tmp / "character.md"
        character.write_text("# character\n", encoding="utf-8")
        body_location = tmp / "body_location.json"
        body_location.write_text(json.dumps({"current_entity": ""}), encoding="utf-8")
        workdir = tmp / "workdir"
        workdir.mkdir()
        return {
            "MODE": mode,
            "CLAUDE_BIN": "/bin/claude",
            "EHA_LOG_DIR": str(tmp / "log"),
            "EHA_TMP_DIR": str(tmp / "tmp"),
            "EHA_PREFS_FILE": str(prefs),
            "EHA_CHARACTER_FILE": str(character),
            "EHA_BODY_LOCATION_FILE": str(body_location),
            "EHA_DATA_DIR": str(tmp),
            "EHA_CLAUDE_CWD": str(workdir),
            "EHA_TEST_TIMESTAMP": "2026-07-15T12:00:00+09:00",
            "EHA_TEST_HOUR": "12",
        }

    def test_run_all_modes_with_mocked_external_commands(self):
        for mode in ["observe", "explore", "reflect", "web", "social"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                calls = []
                result = loop.run(self.make_env(tmp, mode), run_subprocess=self.fake_run_factory(calls))

                self.assertEqual(result["mode"], mode)
                self.assertTrue(result["parsed"].get("_parse_ok"))
                claude_calls = [call for call in calls if call[0] and call[0][0] == "/bin/claude"]
                self.assertTrue(claude_calls)
                claude_cmd, claude_kwargs = claude_calls[-1]
                self.assertIn("--allowedTools", claude_cmd)
                self.assertIn(result["context"]["allowed_tools"], claude_cmd)
                self.assertIn("--append-system-prompt", claude_cmd)
                self.assertIn(result["context"]["sys_prompt"], claude_cmd)
                envelope = json.loads(claude_kwargs["input"])
                self.assertEqual(envelope["message"]["content"][-1]["text"], result["context"]["user_prompt"])
                mcp_calls = [call for call in calls if len(call[0]) >= 2 and call[0][0] == "python3" and call[0][1].endswith("mcp-config.py")]
                self.assertTrue(mcp_calls)
                self.assertEqual(tuple(mcp_calls[-1][0][3:]), tuple(result["context"]["mcp_servers"]))
                rows = self.read_jsonl(tmp / "log" / ("observations.jsonl" if mode == "observe" else "explore.jsonl"))
                self.assertEqual(rows[0]["private"], "静かに確認している")
                chat_rows = self.read_jsonl(tmp / "log" / "chat_log.jsonl")
                self.assertEqual(chat_rows[-1]["source"], mode)

    def test_mode_config_matches_loop_sh_mcp_and_allowed_tools(self):
        text = (ROOT / "embodied_ha" / "loop.sh").read_text(encoding="utf-8")
        for mode in ["observe", "explore", "reflect", "web", "social"]:
            with self.subTest(mode=mode):
                start = text.index(f"  {mode})")
                end = text.index("    ;;", start)
                block = text[start:end]
                cfg = loop.mode_config(mode)
                def shell_literal(value):
                    return str(value).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")

                self.assertIn(f'MODE_LABEL="{shell_literal(cfg.label)}"', block)
                self.assertIn(f'TOOLS_DESC="{shell_literal(cfg.tools_desc)}"', block)
                self.assertIn(f'TASK="{shell_literal(cfg.task)}"', block)
                self.assertIn(f'ALLOWED_TOOLS="{cfg.allowed_tools}"', block)
                self.assertIn(f'MCP_SERVERS="{" ".join(cfg.mcp_servers)}"', block)


if __name__ == "__main__":
    unittest.main()
