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


if __name__ == "__main__":
    unittest.main()
