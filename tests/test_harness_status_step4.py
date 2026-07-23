"""Step4 増分1a: 選択ハーネスのランタイム配線と集約 readiness snapshot のテスト。

- harness_status.readiness/snapshot が単一定義であること(daemon と web overview の共有)。
- daemon.harness_ready() が snapshot と同じ readiness を返すこと(sol R5: 二重定義ドリフト防止)。
- daemon.start_runtime_threads() が選択ハーネスを EHA_AGENT_HARNESS へ配線すること
  (初回選択=再起動なしでも子プロセスが継承・sol H2/§3.1)。
- server._selected_harness_ready() が snapshot へ委譲すること。
- run.sh の export ブロック相当が valid フラグのみ EHA_AGENT_HARNESS を設定すること。
"""
import contextlib
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))

import harness_status  # noqa: E402


def load_daemon(name: str):
    """daemon.py の関数定義部だけを exec する(末尾の flock/thread 起動は走らせない)。"""
    source = (EHA_DIR / "daemon.py").read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType(name)
    module.__file__ = str(EHA_DIR / "daemon.py")
    with mock.patch.dict(os.environ, {"HA_URL": "http://supervisor/core/api"}, clear=False):
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def load_server(name: str):
    import importlib.util

    env = {
        "HA_URL": "http://supervisor/core/api",
        "SUPERVISOR_TOKEN": "test-token",
        "EHA_LOG_DIR": "/tmp",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location(name, EHA_DIR / "web" / "server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _harness_setup(installed: bool, authed: bool):
    """全 setup モジュールを installed/authed 揃いにモックする。"""
    with contextlib.ExitStack() as stack:
        for mod, extra in (
            (harness_status.claude_setup, True),
            (harness_status.codex_setup, False),
            (harness_status.antigravity_setup, False),
        ):
            stack.enter_context(mock.patch.object(mod, "is_installed", return_value=installed))
            stack.enter_context(mock.patch.object(mod, "is_authenticated", return_value=authed))
        stack.enter_context(mock.patch.object(
            harness_status.claude_setup, "resolve_claude_bin",
            return_value="/bin/claude" if authed else None,
        ))
        yield


class ReadinessTests(unittest.TestCase):
    def test_claude_requires_auth_and_binary(self):
        with mock.patch.object(harness_status.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(harness_status.claude_setup, "resolve_claude_bin", return_value="/bin/claude"):
            self.assertTrue(harness_status.readiness("claude"))
        with mock.patch.object(harness_status.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(harness_status.claude_setup, "resolve_claude_bin", return_value=None):
            self.assertFalse(harness_status.readiness("claude"))

    def test_codex_and_agy_require_install_and_auth(self):
        with mock.patch.object(harness_status.codex_setup, "is_installed", return_value=True), \
             mock.patch.object(harness_status.codex_setup, "is_authenticated", return_value=True):
            self.assertTrue(harness_status.readiness("codex"))
        with mock.patch.object(harness_status.antigravity_setup, "is_installed", return_value=True), \
             mock.patch.object(harness_status.antigravity_setup, "is_authenticated", return_value=False):
            self.assertFalse(harness_status.readiness("agy"))

    def test_unknown_harness_is_never_ready(self):
        self.assertFalse(harness_status.readiness(None))
        self.assertFalse(harness_status.readiness("nope"))


class SnapshotTests(unittest.TestCase):
    # snapshot() は read_selection() を1回だけ読む(単一capture・sol Med2)ので
    # read_selection をモックする。

    def test_valid_selection_reports_selected_effective_ready(self):
        with mock.patch.object(harness_status.harness_state, "read_selection", return_value=("valid", "codex")), \
             _harness_setup(installed=True, authed=True):
            snap = harness_status.snapshot()
        self.assertEqual(snap["selection_state"], "valid")
        self.assertEqual(snap["selected"], "codex")
        self.assertEqual(snap["effective"], "codex")
        self.assertTrue(snap["ready"])
        self.assertEqual(
            snap["harnesses"]["codex"], {"installed": True, "authenticated": True}
        )

    def test_valid_but_not_ready(self):
        with mock.patch.object(harness_status.harness_state, "read_selection", return_value=("valid", "codex")), \
             _harness_setup(installed=False, authed=False):
            snap = harness_status.snapshot()
        self.assertEqual(snap["selection_state"], "valid")
        self.assertFalse(snap["ready"])

    def test_missing_flag_with_authenticated_claude_is_effective_claude_ready(self):
        with mock.patch.object(harness_status.harness_state, "read_selection", return_value=("missing", None)), \
             _harness_setup(installed=True, authed=True):
            snap = harness_status.snapshot()
        self.assertEqual(snap["selection_state"], "missing")
        self.assertIsNone(snap["selected"])
        self.assertEqual(snap["effective"], "claude")
        self.assertTrue(snap["ready"])  # sol H8/Med8: 稼働中を通常モードで報告

    def test_missing_flag_without_claude_auth_is_not_ready(self):
        with mock.patch.object(harness_status.harness_state, "read_selection", return_value=("missing", None)), \
             _harness_setup(installed=False, authed=False):
            snap = harness_status.snapshot()
        self.assertFalse(snap["ready"])

    def test_invalid_flag_fails_closed(self):
        with mock.patch.object(harness_status.harness_state, "read_selection", return_value=("invalid", None)), \
             _harness_setup(installed=True, authed=True):
            snap = harness_status.snapshot()
        self.assertEqual(snap["selection_state"], "invalid")
        self.assertIsNone(snap["selected"])
        self.assertFalse(snap["ready"])

    def test_snapshot_is_internally_consistent_single_read(self):
        # sol Med2: ready と harnesses が同一 capture から作られること。codex を
        # ready かつ installed/authenticated=True で一貫報告する(矛盾しない)。
        with mock.patch.object(harness_status.harness_state, "read_selection", return_value=("valid", "codex")), \
             _harness_setup(installed=True, authed=True):
            snap = harness_status.snapshot()
        self.assertTrue(snap["ready"])
        self.assertTrue(snap["harnesses"]["codex"]["installed"])
        self.assertTrue(snap["harnesses"]["codex"]["authenticated"])

    def test_public_schema_has_no_paths_or_tokens(self):
        with mock.patch.object(harness_status.harness_state, "read_selection", return_value=("valid", "claude")), \
             _harness_setup(installed=True, authed=True):
            snap = harness_status.snapshot()
        self.assertEqual(
            set(snap), {"selection_state", "selected", "effective", "ready", "harnesses"}
        )
        for state in snap["harnesses"].values():
            self.assertEqual(set(state), {"installed", "authenticated"})


class DaemonParityTests(unittest.TestCase):
    def test_harness_ready_matches_snapshot_ready_for_valid_selection(self):
        # daemon.harness_ready() は get_selected_harness を先に読む(既存Step3契約)、
        # snapshot() は read_selection を読む。valid選択では両者一致するので、
        # 同じ選択・同じ setup 状態で ready が一致することを確認(sol R5: ドリフト防止)。
        daemon = load_daemon("daemon_parity")
        for harness in ("claude", "codex", "agy"):
            for ready in (True, False):
                with self.subTest(harness=harness, ready=ready), \
                     mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=harness), \
                     mock.patch.object(harness_status.harness_state, "read_selection", return_value=("valid", harness)), \
                     _harness_setup(installed=ready, authed=ready):
                    self.assertEqual(daemon.harness_ready(), harness_status.snapshot()["ready"])
                    self.assertEqual(daemon.harness_ready(), ready)


class RuntimeWiringTests(unittest.TestCase):
    @contextlib.contextmanager
    def _clean_env(self):
        saved = os.environ.pop("EHA_AGENT_HARNESS", None)
        try:
            yield
        finally:
            if saved is None:
                os.environ.pop("EHA_AGENT_HARNESS", None)
            else:
                os.environ["EHA_AGENT_HARNESS"] = saved

    def test_start_runtime_threads_exports_effective_from_ready_snapshot(self):
        # sol 1a-review High/Med3: judge した値=export する値を同一 snapshot に固定。
        daemon = load_daemon("daemon_rtwire")
        daemon._runtime_started.clear()
        ready_snap = {
            "selection_state": "valid", "selected": "codex", "effective": "codex",
            "ready": True, "harnesses": {},
        }
        with self._clean_env():
            with mock.patch.object(daemon.harness_status, "snapshot", return_value=ready_snap), \
                 mock.patch.object(daemon, "threading", mock.MagicMock()), \
                 mock.patch.object(daemon, "MQTT_HOST", ""), \
                 mock.patch.object(daemon, "load_enabled_mics", return_value=[]):
                daemon.start_runtime_threads()
                self.assertEqual(os.environ.get("EHA_AGENT_HARNESS"), "codex")
                self.assertTrue(daemon._runtime_started.is_set())
        daemon._runtime_started.clear()

    def test_missing_flag_exports_effective_claude_overriding_stale_env(self):
        # sol Med3: 継承 env=agy でも effective=claude を明示上書き。
        daemon = load_daemon("daemon_rtwire_claude")
        daemon._runtime_started.clear()
        snap = {
            "selection_state": "missing", "selected": None, "effective": "claude",
            "ready": True, "harnesses": {},
        }
        with self._clean_env():
            os.environ["EHA_AGENT_HARNESS"] = "agy"  # 古い継承値
            with mock.patch.object(daemon.harness_status, "snapshot", return_value=snap), \
                 mock.patch.object(daemon, "threading", mock.MagicMock()), \
                 mock.patch.object(daemon, "MQTT_HOST", ""), \
                 mock.patch.object(daemon, "load_enabled_mics", return_value=[]):
                daemon.start_runtime_threads()
                self.assertEqual(os.environ.get("EHA_AGENT_HARNESS"), "claude")
        daemon._runtime_started.clear()

    def test_start_runtime_threads_declines_when_snapshot_not_ready(self):
        # sol 1a-review High: 起動直前に未準備へ変わったら壊れたハーネスで起動しない。
        daemon = load_daemon("daemon_rtwire_notready")
        daemon._runtime_started.clear()
        snap = {
            "selection_state": "valid", "selected": "codex", "effective": "codex",
            "ready": False, "harnesses": {},
        }
        with self._clean_env():
            with mock.patch.object(daemon.harness_status, "snapshot", return_value=snap), \
                 mock.patch.object(daemon, "threading", mock.MagicMock()) as Thread, \
                 mock.patch.object(daemon, "MQTT_HOST", ""), \
                 mock.patch.object(daemon, "load_enabled_mics", return_value=[]):
                daemon.start_runtime_threads()
                self.assertIsNone(os.environ.get("EHA_AGENT_HARNESS"))
                self.assertFalse(daemon._runtime_started.is_set())
                Thread.Thread.assert_not_called()
        daemon._runtime_started.clear()


class ServerMirrorTests(unittest.TestCase):
    def test_selected_harness_ready_delegates_to_snapshot(self):
        server = load_server("server_mirror_step4")
        with mock.patch.object(server.harness_status, "snapshot", return_value={"ready": True}):
            self.assertTrue(server._selected_harness_ready())
        with mock.patch.object(server.harness_status, "snapshot", return_value={"ready": False}):
            self.assertFalse(server._selected_harness_ready())


class RunShExportTests(unittest.TestCase):
    """run.sh の実ブロックを切り出して実行し、production の順序/引用/条件を固定する。

    sol 1a-review Low4: テスト内に複製せず、run.sh の該当ブロックそのものを抽出して回す。
    ブロックの引用や条件が壊れたら本テストが落ちる。
    """

    RUN_SH = EHA_DIR / "run.sh"
    START = "# --- 選択ハーネスを実行時ハーネスへ配線"
    END = "# --- PulseAudio"

    def _extract_block(self) -> str:
        source = self.RUN_SH.read_text(encoding="utf-8")
        start = source.index(self.START)
        end = source.index(self.END, start)
        return source[start:end]

    def _run_block(self, flag_value: str | None) -> str:
        block = self._extract_block()
        with tempfile.TemporaryDirectory() as d:
            env_line = ""
            if flag_value is not None:
                flag = Path(d) / "selected_harness"
                flag.write_text(f"{flag_value}\n", encoding="utf-8")
                env_line = f"export EHA_HARNESS_FLAG_FILE={flag}"
            script = "\n".join([
                "set -euo pipefail",
                f"SCRIPT_DIR={EHA_DIR}",
                env_line,
                block,
                'echo "RESULT:${EHA_AGENT_HARNESS:-UNSET}"',
            ])
            out = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, check=True
            ).stdout
        for line in out.splitlines():
            if line.startswith("RESULT:"):
                return line[len("RESULT:"):]
        raise AssertionError(f"RESULT line missing in: {out!r}")

    def test_valid_flag_exports_harness(self):
        self.assertEqual(self._run_block("codex"), "codex")
        self.assertEqual(self._run_block("agy"), "agy")

    def test_missing_flag_leaves_unset(self):
        self.assertEqual(self._run_block(None), "UNSET")

    def test_invalid_flag_leaves_unset(self):
        self.assertEqual(self._run_block("not-a-harness"), "UNSET")

    def test_stale_inherited_env_is_cleared_when_flag_not_valid(self):
        # sol Med3: valid フラグが無ければ継承された EHA_AGENT_HARNESS を残さない。
        block = self._extract_block()
        script = "\n".join([
            "set -euo pipefail",
            f"SCRIPT_DIR={EHA_DIR}",
            "export EHA_AGENT_HARNESS=agy",  # 古い継承値
            block,
            'echo "RESULT:${EHA_AGENT_HARNESS:-UNSET}"',
        ])
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
        result = next(line[len("RESULT:"):] for line in out.splitlines() if line.startswith("RESULT:"))
        self.assertEqual(result, "UNSET")


if __name__ == "__main__":
    unittest.main()
