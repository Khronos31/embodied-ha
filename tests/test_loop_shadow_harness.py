import fcntl
import json
import os
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

from loop_shadow_harness import RUNTIME_FILES, assert_same_side_effects, capture_runtime_side_effects  # noqa: E402

import loop  # noqa: E402


class LoopMigrationSafetyTests(unittest.TestCase):
    def test_daemon_still_invokes_loop_sh(self):
        daemon = (ROOT / "embodied_ha" / "daemon.py").read_text(encoding="utf-8")

        self.assertIn('LOOP_SH = os.path.join(_SCRIPT_DIR, "loop.sh")', daemon)
        self.assertIn('subprocess.run(["bash", LOOP_SH]', daemon)

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

    def test_loop_py_blocks_agy_until_invoke_agent_cutover(self):
        with self.assertRaises(SystemExit) as caught:
            loop.run({"EHA_SESSION_BIN": "/data/bin/agy"})

        self.assertIn("EHA_SESSION_BIN=agy", str(caught.exception))
        self.assertIn("invoke-agent.sh", str(caught.exception))

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

    def write_executable(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def install_fixture_bins(self, bin_dir: Path) -> None:
        self.write_executable(
            bin_dir / "date",
            """#!/usr/bin/env python3
import sys
arg = sys.argv[1] if len(sys.argv) > 1 else ""
if arg == "-Iseconds":
    print("2026-07-15T12:00:00+09:00")
elif arg == "+%-H":
    print("12")
elif arg == "+%Y-%m-%d":
    print("2026-07-15")
else:
    print("2026-07-15T12:00:00+09:00")
""",
        )
        self.write_executable(
            bin_dir / "loops",
            """#!/usr/bin/env python3
import sys
if len(sys.argv) > 1 and sys.argv[1] == "list-json":
    print("[]")
else:
    print("なし")
""",
        )
        self.write_executable(
            bin_dir / "curl",
            """#!/usr/bin/env python3
import os
import sys
args = sys.argv[1:]
target = args[-1] if args else ""
if "/api/status" in target:
    sys.exit(0)
if "/api/camera_proxy/" in target or "/api/frame.jpeg" in target:
    os.write(1, b"JPEGFIXTURE" * 20)
    sys.exit(0)
if target.endswith("/template"):
    print("# sensors\\nfixture sensor: on")
    sys.exit(0)
sys.exit(0)
""",
        )
        self.write_executable(
            bin_dir / "claude-fixture",
            """#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
stdin = sys.stdin.read()

def value_after(flag, default=""):
    try:
        return argv[argv.index(flag) + 1]
    except Exception:
        return default

model = value_after("--model", "")
has_schema = "--json-schema" in argv
has_tools = "--allowedTools" in argv
actor = os.environ.get("EHA_ACTOR", "")
mode = os.environ.get("MODE", "")
if has_schema:
    try:
        schema = json.loads(value_after("--json-schema", "{}"))
        mode = schema.get("title", mode).replace("loop_", "").replace("_response", "") or mode
    except Exception:
        pass

if model == "haiku":
    text = f"watch model={model};schema={int(has_schema)};tools={int(has_tools)};actor={actor or 'unset'}"
    print(json.dumps({"type": "result", "result": text}, ensure_ascii=False))
    sys.exit(0)

watch = ""
try:
    envelope = json.loads(stdin or "{}")
    blocks = envelope.get("message", {}).get("content", [])
    texts = [str(block.get("text", "")) for block in blocks if isinstance(block, dict)]
    joined = "\\n".join(texts)
    marker = "watch model="
    if marker in joined:
        watch = joined[joined.index(marker):].splitlines()[0]
except Exception:
    pass

private = (
    f"mode={mode};model={model};actor={actor or 'unset'};"
    f"schema={int(has_schema)};tools={int(has_tools)};watch={watch or 'none'}"
)
payload = {
    "type": "result",
    "structured_output": {
        "topic": "fixture",
        "private": private,
        "emotion": "calm",
        "speak": f"say {mode}",
        "proposal": None,
        "feature_presented": None,
    },
}
print(json.dumps(payload, ensure_ascii=False))
""",
        )

    def make_runtime(self, root: Path, name: str) -> tuple[Path, dict[str, str]]:
        run_root = root / name
        data_dir = run_root / "data"
        log_dir = run_root / "log"
        tmp_dir = run_root / "tmp"
        workdir = run_root / "workdir"
        home = run_root / "home"
        bin_dir = run_root / "bin"
        for path in (data_dir, log_dir, tmp_dir, workdir, home, bin_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.install_fixture_bins(bin_dir)

        prefs = data_dir / "preferences.json"
        prefs.write_text(
            json.dumps(
                {
                    "speakers": [{"room": "living"}],
                    "cameras": [{"ha_entity": "camera.fixture", "label": "Fixture"}],
                    "sensors": {"groups": []},
                    "policies": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        character = data_dir / "character.md"
        character.write_text("# character\n", encoding="utf-8")
        body_location = data_dir / "body_location.json"
        body_location.write_text(json.dumps({"current_entity": ""}), encoding="utf-8")
        (log_dir / ".last_daybook").write_text(self.today, encoding="utf-8")

        path = f"{bin_dir}:{os.environ.get('PATH', '')}"
        env = {
            **os.environ,
            "PATH": path,
            "HOME": str(home),
            "EHA_TOOLS_PATH": str(bin_dir),
            "CLAUDE_BIN": str(bin_dir / "claude-fixture"),
            "CLAUDE_CONFIG_DIR": str(home / "claude"),
            "EHA_DATA_DIR": str(data_dir),
            "EHA_LOG_DIR": str(log_dir),
            "EHA_TMP_DIR": str(tmp_dir),
            "EHA_ANOMALY_STATE_FILE": str(log_dir / "anomaly_state.json"),
            "EHA_PREFS_FILE": str(prefs),
            "EHA_CHARACTER_FILE": str(character),
            "EHA_BODY_LOCATION_FILE": str(body_location),
            "EHA_CLAUDE_CWD": str(workdir),
            "EHA_NEXT_LISTEN_REQUEST_FILE": str(data_dir / "runtime" / "next_listen_request.json"),
            "EHA_NEXT_LISTEN_LOG_FILE": str(log_dir / "next_listen_log.jsonl"),
            "EHA_ACTIVE_LISTEN_LOG_FILE": str(log_dir / "active_listen_log.jsonl"),
            "EHA_TEST_TIMESTAMP": self.timestamp,
            "EHA_TEST_HOUR": "12",
            "EHA_SESSION_MODEL": "opus",
            "HA_URL": "http://fixture.local/api",
            "SUPERVISOR_TOKEN": "fixture-token",
            "INGRESS_PORT": "18099",
            "RESIDENT": "ユーザー",
        }
        env.pop("EHA_SESSION_BIN", None)
        return log_dir, env

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

    def run_loop_sh(self, env: dict[str, str], mode: str, cwd: Path) -> subprocess.CompletedProcess:
        run_env = {**env, "MODE": mode}
        return self.run_with_shared_tmp_guard(
            ["bash", str(ROOT / "embodied_ha" / "loop.sh")],
            cwd=cwd,
            env=run_env,
        )

    def run_loop_py(self, env: dict[str, str], mode: str, cwd: Path) -> subprocess.CompletedProcess:
        return self.run_with_shared_tmp_guard(
            ["python3", str(ROOT / "embodied_ha" / "loop.py"), "--mode", mode],
            cwd=cwd,
            env=env,
        )

    def test_loop_sh_and_loop_py_side_effects_match_for_all_modes(self):
        for mode in self.modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                sh_log, sh_env = self.make_runtime(root, "loop-sh")
                py_log, py_env = self.make_runtime(root, "loop-py")
                self.assert_fixture_anomaly_state_file(sh_env)
                self.assert_fixture_anomaly_state_file(py_env)

                sh = self.run_loop_sh(sh_env, mode, root)
                py = self.run_loop_py(py_env, mode, root)

                self.assertEqual(sh.returncode, 0, sh.stderr)
                self.assertEqual(py.returncode, 0, py.stderr)
                assert_same_side_effects(
                    self,
                    capture_runtime_side_effects(sh_log),
                    capture_runtime_side_effects(py_log),
                )

    def test_shared_tmp_dummy_file_survives_loop_process_runs(self):
        self.shared_tmp_dir.mkdir(parents=True, exist_ok=True)
        dummy = self.shared_tmp_dir / f"shadow_parity_preserve_{os.getpid()}.txt"
        dummy.write_text("preserve\n", encoding="utf-8")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _, sh_env = self.make_runtime(root, "loop-sh")
                _, py_env = self.make_runtime(root, "loop-py")

                sh = self.run_loop_sh(sh_env, "reflect", root)
                self.assertEqual(sh.returncode, 0, sh.stderr)
                self.assertEqual(dummy.read_text(encoding="utf-8"), "preserve\n")

                py = self.run_loop_py(py_env, "reflect", root)
                self.assertEqual(py.returncode, 0, py.stderr)
                self.assertEqual(dummy.read_text(encoding="utf-8"), "preserve\n")
        finally:
            dummy.unlink(missing_ok=True)


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
