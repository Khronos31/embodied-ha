import fcntl
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "embodied_ha"))

from loop_shadow_harness import (  # noqa: E402
    RUNTIME_FILES,
    capture_runtime_side_effects,
    comparable_wiring_trace,
    make_runtime,
    run_shadow_command,
    summarize_wiring_delta,
)

import loop  # noqa: E402
import introspection_facts  # noqa: E402


class LoopMigrationSafetyTests(unittest.TestCase):
    def test_daemon_now_invokes_loop_py(self):
        daemon = (ROOT / "embodied_ha" / "daemon.py").read_text(encoding="utf-8")

        self.assertIn('LOOP_PY = os.path.join(_SCRIPT_DIR, "loop.py")', daemon)
        self.assertIn('subprocess.run(["python3", LOOP_PY]', daemon)

    def test_loop_py_main_accepts_forced_mode_without_daemon_wiring(self):
        calls = []
        original_run = loop.run
        try:
            def fake_run(env):
                calls.append(env)
                return {"mode": env.get("MODE")}

            loop.run = fake_run
            loop.main(["--mode", "reflect"])
        finally:
            loop.run = original_run

        self.assertEqual(calls[0]["MODE"], "reflect")

    def test_runtime_contract_doc_covers_shadow_files_and_cutover_blocker(self):
        doc = (ROOT / "docs" / "loop-runtime-contracts.md").read_text(encoding="utf-8")

        for name in RUNTIME_FILES:
            self.assertIn(name, doc)
        self.assertIn("EHA_SESSION_BIN", doc)
        self.assertIn("invoke-agent.sh", doc)
        self.assertIn("not cutover-ready", doc)

    def test_loop_py_no_longer_blocks_agy_after_invoke_agent_cutover(self):
        # 仕様変更(2026-07-17、#14増分6): EHA_SESSION_BIN=agyのSystemExitガードは
        # invoke-agent.sh --sound-file経由のAntigravity音声サポート実装に伴い撤去した。
        # このテストは「もう落ちない」ことを確認する形へ更新する(loop.pyのソースに
        # 撤去済みのSystemExit文字列が残っていないことを直接確認)。
        source = (ROOT / "embodied_ha" / "loop.py").read_text(encoding="utf-8")
        self.assertNotIn("EHA_SESSION_BIN", source)
        self.assertNotIn("does not implement EHA_SESSION_BIN=agy", source)

    def test_side_effect_snapshot_normalizes_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "observations.jsonl").write_text(
                json.dumps({"timestamp": "t", "emotion": "calm", "private": "見た"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (root / "pending_proposal.json").write_text(
                json.dumps(
                    {
                        "timestamp": "t",
                        "proposal": "消しましょうか",
                        "action": {"domain": "light", "service": "turn_off", "entity_id": "light.x"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            snapshot = capture_runtime_side_effects(root)

        self.assertEqual(snapshot.files["observations.jsonl"][0]["private"], "見た")
        self.assertEqual(snapshot.files["pending_proposal.json"]["action"]["entity_id"], "light.x")
        self.assertEqual(snapshot.files["explore.jsonl"], [])
        self.assertEqual(snapshot.files["loop_parse_errors.jsonl"], [])
        self.assertEqual(snapshot.files["chat_log.jsonl"], [])


class LoopShadowProcessParityTests(unittest.TestCase):
    maxDiff = None
    modes = ("observe", "explore", "reflect", "web", "social")
    timestamp = "2026-07-15T12:00:00+09:00"
    today = "2026-07-15"
    production_anomaly_state_file = Path("/config/embodied-ha/log/anomaly_state.json")
    shared_tmp_dir = Path("/tmp/embodied-ha")
    shared_tmp_lock_file = Path("/tmp/embodied-ha-shadow-parity.lock")
    shared_tmp_known_files = (
        "mcp.json",
        "anomaly_context.txt",
        "anomaly_urgency.txt",
        *(f"{mode}_facts.json" for mode in modes),
        *(f"{mode}_parsed.json" for mode in modes),
    )

    def assert_fixture_anomaly_state_file(self, env: dict[str, str]) -> None:
        anomaly_file = Path(env["EHA_ANOMALY_STATE_FILE"])

        self.assertEqual(anomaly_file.parent, Path(env["EHA_LOG_DIR"]))
        self.assertNotEqual(anomaly_file, self.production_anomaly_state_file)

    def snapshot_shared_tmp_known_files(self) -> dict[str, bytes | None]:
        self.shared_tmp_dir.mkdir(parents=True, exist_ok=True)
        snapshot: dict[str, bytes | None] = {}
        for name in self.shared_tmp_known_files:
            path = self.shared_tmp_dir / name
            if path.exists() and path.is_file():
                snapshot[name] = path.read_bytes()
            else:
                snapshot[name] = None
        return snapshot

    def restore_shared_tmp_known_files(self, snapshot: dict[str, bytes | None]) -> None:
        for name, content in snapshot.items():
            path = self.shared_tmp_dir / name
            if content is None:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                continue

            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.rmtree(path)
            path.write_bytes(content)

    def run_with_shared_tmp_guard(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess:
        """Run one parity process without blanket-removing loop.sh's fixed /tmp dir.

        loop.sh still hardcodes /tmp/embodied-ha in this phase, so the test cannot
        fully isolate itself from a live production loop. The flock prevents this
        unittest from racing with another copy of itself, and the snapshot restores
        only the fixed filenames the harness is known to create. Unknown files are
        intentionally left alone; the dummy-file regression test below proves that
        we no longer delete the shared directory wholesale.
        """
        with self.shared_tmp_lock_file.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            snapshot = self.snapshot_shared_tmp_known_files()
            try:
                return subprocess.run(
                    cmd,
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            finally:
                self.restore_shared_tmp_known_files(snapshot)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def run_loop_py(self, env: dict[str, str], mode: str, cwd: Path) -> subprocess.CompletedProcess:
        return run_shadow_command(
            ["python3", str(ROOT / "embodied_ha" / "loop.py"), "--mode", mode],
            cwd=cwd,
            env=env,
            runner=self.run_with_shared_tmp_guard,
        )

    def run_direct_invoke_agent_from_loop_trace(
        self,
        env: dict[str, str],
        trace: dict,
        *,
        cwd: Path,
        drop_tool: str | None = None,
    ) -> subprocess.CompletedProcess:
        direct_call = trace["claude_calls"][0]
        direct_argv = direct_call["argv"]
        allowed_tools = self.flag_value(direct_argv, "--allowedTools")
        builtins, mcp_tools = self.split_allowed_tools(allowed_tools)
        if drop_tool is not None:
            builtins = [item for item in builtins if item != drop_tool]
            mcp_tools = [item for item in mcp_tools if item != drop_tool]

        cmd = [
            "bash",
            str(ROOT / "embodied_ha" / "invoke-agent.sh"),
            "--model",
            "default",
            "--append-system-prompt",
            self.flag_value(direct_argv, "--append-system-prompt"),
            "--json-schema",
            self.flag_value(direct_argv, "--json-schema"),
        ]
        if builtins:
            cmd += ["--allowed-builtins", ",".join(builtins)]
        if mcp_tools:
            cmd += ["--allowed-mcp-tools", ",".join(mcp_tools)]
            cmd += ["--mcp-servers", " ".join(trace["mcp_config_calls"][0]["servers"])]
        cmd.append("fixture prompt")

        return run_shadow_command(
            cmd,
            cwd=cwd,
            env=env,
            extra_env={
                "EHA_ACTOR": direct_call.get("actor") or "loop",
                "EHA_CLAUDE_MODEL_DEFAULT": direct_call.get("model") or "opus",
            },
        )

    def flag_value(self, argv: list, flag: str) -> str:
        try:
            return argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            return ""

    def split_allowed_tools(self, allowed_tools: str) -> tuple[list[str], list[str]]:
        items = [item for item in allowed_tools.split(",") if item]
        builtins = [item for item in items if not item.startswith("mcp__")]
        mcp_tools = [item for item in items if item.startswith("mcp__")]
        return builtins, mcp_tools

    def allowed_tool_tokens(self, call: dict) -> set[str]:
        allowed_tools = call.get("allowed_tools") or self.flag_value(call.get("argv", []), "--allowedTools")
        return {item for item in allowed_tools.split(",") if item}

    def assert_allowed_tools_token_sets_match(self, old_trace: dict, new_trace: dict) -> None:
        try:
            old_call = old_trace["claude_calls"][0]
            new_call = new_trace["claude_calls"][0]
        except IndexError as exc:
            raise AssertionError(summarize_wiring_delta(old_trace, new_trace)) from exc

        old_tokens = self.allowed_tool_tokens(old_call)
        new_tokens = self.allowed_tool_tokens(new_call)
        if old_tokens != new_tokens:
            raise AssertionError(
                json.dumps(
                    {
                        "allowed_tools": [sorted(old_tokens), sorted(new_tokens)],
                        "trace_delta": summarize_wiring_delta(old_trace, new_trace),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

    def loop_facts(self, env: dict[str, str], mode: str) -> dict:
        facts = introspection_facts.load_facts_file(str(Path(env["EHA_TMP_DIR"]) / f"{mode}_facts.json"))
        self.assertIsInstance(facts, dict)
        return facts

    def test_invoke_agent_direct_shadow_harness_detects_matching_allowed_tools(self):
        """Self-test for future caller cutovers: the generalized harness can compare final Claude tool sets.

        This does not prove loop.py is wired through invoke-agent.sh; it only
        proves a manually assembled invoke-agent.sh call can produce the same
        claude-fixture allowedTools token set as the current direct loop.py path.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _, direct_env = make_runtime(root, "direct-loop-py")
            _, invoke_env = make_runtime(root, "direct-invoke-agent")

            direct = self.run_loop_py(direct_env, "web", root)
            self.assertEqual(direct.returncode, 0, direct.stderr)
            direct_trace = comparable_wiring_trace(direct_env)

            invoked = self.run_direct_invoke_agent_from_loop_trace(invoke_env, direct_trace, cwd=root)
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            invoke_trace = comparable_wiring_trace(invoke_env)

            self.assert_allowed_tools_token_sets_match(direct_trace, invoke_trace)

    def test_invoke_agent_direct_shadow_harness_detects_broken_allowed_tools(self):
        """Self-test for future caller cutovers: the generalized harness fails on meaningful tool drift.

        This intentionally removes WebSearch from the manually assembled
        invoke-agent.sh call. The expected AssertionError proves the comparison
        is not a vacuous always-green check.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _, direct_env = make_runtime(root, "direct-loop-py")
            _, invoke_env = make_runtime(root, "direct-invoke-agent-broken")

            direct = self.run_loop_py(direct_env, "web", root)
            self.assertEqual(direct.returncode, 0, direct.stderr)
            direct_trace = comparable_wiring_trace(direct_env)
            # This test's premise depends on WebSearch actually being part of
            # web mode's allowed_tools; assert it explicitly so a future change
            # to loop.py's mode_config() can't silently turn this into a
            # vacuous always-green test (sol review, 2026-07-16).
            self.assertIn("WebSearch", self.allowed_tool_tokens(direct_trace["claude_calls"][0]))

            invoked = self.run_direct_invoke_agent_from_loop_trace(
                invoke_env,
                direct_trace,
                cwd=root,
                drop_tool="WebSearch",
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            invoke_trace = comparable_wiring_trace(invoke_env)

            with self.assertRaises(AssertionError):
                self.assert_allowed_tools_token_sets_match(direct_trace, invoke_trace)


class LoopPyCutoverRegressionTests(unittest.TestCase):
    class Result:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def make_env(self, tmp: Path) -> dict[str, str]:
        prefs = tmp / "preferences.json"
        prefs.write_text(json.dumps({"speakers": [{"room": "living"}], "cameras": []}), encoding="utf-8")
        character = tmp / "character.md"
        character.write_text("# character\n", encoding="utf-8")
        body_location = tmp / "body_location.json"
        body_location.write_text(json.dumps({"current_entity": ""}), encoding="utf-8")
        workdir = tmp / "workdir"
        workdir.mkdir()
        return {
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

    def fake_run(self, cmd, **kwargs):
        if cmd[:2] == ["loops", "list"]:
            return self.Result("なし\n")
        if cmd[:2] == ["loops", "list-json"]:
            return self.Result("[]\n")
        if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("render-sensors.py"):
            return self.Result("# sensors\nfixture\n")
        if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("body-context.py"):
            return self.Result("# 身体位置\nfixture\n")
        if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("boundary.py"):
            return self.Result('{"allowed": false}\n')
        if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("feature-flags.py"):
            return self.Result("\n")
        if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("mcp-config.py"):
            Path(cmd[2]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[2]).write_text("{}", encoding="utf-8")
            return self.Result()
        if cmd and cmd[0] == "curl":
            return self.Result()
        if cmd and cmd[0] == "/bin/claude":
            payload = {
                "type": "result",
                "structured_output": {
                    "topic": "fixture",
                    "private": "fresh anomaly",
                    "emotion": "calm",
                    "speak": None,
                    "proposal": None,
                    "feature_presented": None,
                },
            }
            return self.Result(json.dumps(payload, ensure_ascii=False))
        return self.Result()

    def test_run_auto_mode_selection_uses_fresh_anomaly_urgency(self):
        captured = []

        def fake_choose(environ=None, **_kwargs):
            env = dict(environ or {})
            captured.append(env)
            if env.get("MODE"):
                return str(env["MODE"])
            return "explore" if env.get("ANOMALY_URGENCY") == "99" else "reflect"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with mock.patch.object(loop, "choose_mode", side_effect=fake_choose), \
                    mock.patch.object(loop, "update_anomaly_context", return_value=("# anomaly\nfresh", "99")):
                result = loop.run(self.make_env(tmp), run_subprocess=self.fake_run)

        self.assertEqual(result["mode"], "explore")
        self.assertTrue(
            any(call.get("ANOMALY_URGENCY") == "99" and not call.get("MODE") for call in captured),
            captured,
        )

    def test_postprocess_removes_queued_file_before_recording_presented_features(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            queued = tmp / "queued.wav"
            queued.write_bytes(b"queued")
            prefs = tmp / "preferences.json"
            prefs.write_text(json.dumps({"speakers": [{"room": "living"}]}), encoding="utf-8")
            paths = loop.LoopPaths(
                log_dir=str(tmp),
                observation_log=str(tmp / "observations.jsonl"),
                explore_log=str(tmp / "explore.jsonl"),
                chat_log=str(tmp / "chat_log.jsonl"),
                memory_file=str(tmp / "memory.md"),
                pending_file=str(tmp / "pending_proposal.json"),
                daybook_marker=str(tmp / ".last_daybook"),
                tmp_dir=str(tmp / "tmp"),
            )
            feature_call_saw_file = []

            def fake_run(cmd, **_kwargs):
                if len(cmd) >= 3 and cmd[1].endswith("feature-flags.py") and cmd[2] == "add":
                    feature_call_saw_file.append(queued.exists())
                return self.Result()

            loop.postprocess_loop_response(
                {
                    "_parse_ok": True,
                    "topic": "fixture",
                    "private": "順序確認",
                    "emotion": "calm",
                    "speak": None,
                    "proposal": None,
                    "feature_presented": "feature-x",
                },
                "{}",
                {"mode": "explore", "cfg": {"EHA_PREFS_FILE": str(prefs)}, "queued_listen_file": str(queued)},
                paths,
                "2026-07-15T12:00:00+09:00",
                run=fake_run,
            )

        self.assertEqual(feature_call_saw_file, [False])


if __name__ == "__main__":
    unittest.main()
