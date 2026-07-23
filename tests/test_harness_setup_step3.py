import json
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
sys.path.insert(0, str(ROOT / "embodied_ha" / "web"))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import claude_setup  # noqa: E402
import harness_state  # noqa: E402
import server  # noqa: E402


def _load_daemon_without_boot():
    path = ROOT / "embodied_ha" / "daemon.py"
    source = path.read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType("daemon_harness_setup_test")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


daemon = _load_daemon_without_boot()


def _cas_worker(args):
    """別プロセスで compare_and_set を呼ぶ(並行 install の直列化テスト用・module-level 必須)。"""
    flag, harness = args
    os.environ["EHA_HARNESS_FLAG_FILE"] = flag
    return harness_state.compare_and_set(harness)


class HarnessStateTests(unittest.TestCase):
    def test_get_set_round_trip_uses_atomic_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            flag = Path(temp) / "state" / "selected_harness"
            with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": str(flag)}, clear=False), \
                 mock.patch.object(harness_state.os, "replace", wraps=os.replace) as replace:
                harness_state.set_selected_harness("codex")
                self.assertEqual(harness_state.get_selected_harness(), "codex")
            replace.assert_called_once()
            source, destination = replace.call_args.args
            self.assertEqual(destination, str(flag))
            self.assertEqual(Path(source).parent, flag.parent)
            self.assertEqual(flag.read_text(encoding="utf-8"), "codex\n")

    def test_get_returns_none_for_missing_empty_and_invalid_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            flag = Path(temp) / "selected_harness"
            with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": str(flag)}, clear=False):
                self.assertIsNone(harness_state.get_selected_harness())
                flag.write_text("\n", encoding="utf-8")
                self.assertIsNone(harness_state.get_selected_harness())
                flag.write_text("unknown\n", encoding="utf-8")
                self.assertIsNone(harness_state.get_selected_harness())
                flag.write_bytes(b"\xff")
                self.assertIsNone(harness_state.get_selected_harness())

    def test_read_selection_distinguishes_missing_from_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            flag = Path(temp) / "selected_harness"
            with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": str(flag)}, clear=False):
                self.assertEqual(harness_state.read_selection(), ("missing", None))
                flag.write_text("\n", encoding="utf-8")
                self.assertEqual(harness_state.read_selection(), ("invalid", None))
                flag.write_text("unknown\n", encoding="utf-8")
                self.assertEqual(harness_state.read_selection(), ("invalid", None))
                flag.write_text("codex\n", encoding="utf-8")
                self.assertEqual(harness_state.read_selection(), ("valid", "codex"))

    def test_read_selection_treats_read_error_as_invalid(self):
        with mock.patch.object(harness_state, "flag_path", return_value="/unreadable"), \
             mock.patch("builtins.open", side_effect=OSError("read failed")):
            self.assertEqual(harness_state.read_selection(), ("invalid", None))

    def test_set_rejects_invalid_harness(self):
        with self.assertRaises(ValueError):
            harness_state.set_selected_harness("unknown")

    def test_compare_and_set_sets_when_missing_or_invalid(self):
        # Step4増分1c(sol H1): 未選択/不正なら確定する。
        with tempfile.TemporaryDirectory() as temp:
            flag = Path(temp) / "state" / "selected_harness"
            with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": str(flag)}, clear=False):
                self.assertEqual(harness_state.compare_and_set("codex"), ("set", "codex"))
                self.assertEqual(harness_state.get_selected_harness(), "codex")
                # invalid からも確定できる。
                flag.write_text("garbage\n", encoding="utf-8")
                self.assertEqual(harness_state.compare_and_set("agy"), ("set", "agy"))
                self.assertEqual(harness_state.get_selected_harness(), "agy")

    def test_compare_and_set_is_idempotent_for_same_harness(self):
        with tempfile.TemporaryDirectory() as temp:
            flag = Path(temp) / "selected_harness"
            with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": str(flag)}, clear=False):
                harness_state.compare_and_set("claude")
                self.assertEqual(harness_state.compare_and_set("claude"), ("unchanged", "claude"))
                self.assertEqual(harness_state.get_selected_harness(), "claude")

    def test_compare_and_set_rejects_different_valid_selection(self):
        # 初回固定: 別の valid 選択があれば conflict、既存は不変。
        with tempfile.TemporaryDirectory() as temp:
            flag = Path(temp) / "selected_harness"
            with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": str(flag)}, clear=False):
                harness_state.compare_and_set("claude")
                self.assertEqual(harness_state.compare_and_set("codex"), ("conflict", "claude"))
                self.assertEqual(harness_state.get_selected_harness(), "claude")

    def test_compare_and_set_rejects_invalid_harness_argument(self):
        with self.assertRaises(ValueError):
            harness_state.compare_and_set("unknown")

    def test_compare_and_set_serialises_concurrent_processes(self):
        # sol 1b/1c Med2: 並行 install が最後勝ちしない。異なるハーネスで同時に CAS しても
        # flock により確定は1件のみ、残りは conflict、最終選択は確定したハーネス。
        import multiprocessing as mp
        with tempfile.TemporaryDirectory() as temp:
            flag = os.path.join(temp, "selected_harness")
            jobs = [(flag, h) for h in ("claude", "codex", "agy", "codex", "agy")]
            with mp.get_context("fork").Pool(len(jobs)) as pool:
                results = pool.map(_cas_worker, jobs)
            outcomes = [outcome for outcome, _ in results]
            self.assertEqual(outcomes.count("set"), 1)
            setter = next(h for (_, h), (o, _) in zip(jobs, results) if o == "set")
            with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": flag}, clear=False):
                self.assertEqual(harness_state.get_selected_harness(), setter)
            for (_, h), (outcome, current) in zip(jobs, results):
                if outcome == "set":
                    continue
                if h == setter:
                    self.assertEqual(outcome, "unchanged")  # 同一ハーネスの後続は冪等
                else:
                    self.assertEqual(outcome, "conflict")  # 別ハーネスは拒否(最後勝ちしない)
                    self.assertEqual(current, setter)


class ResolveClaudeBinTests(unittest.TestCase):
    def test_prefers_executable_diy_binary(self):
        with mock.patch.object(claude_setup, "binary_path", return_value="/managed/claude"), \
             mock.patch.object(claude_setup.os.path, "isfile", return_value=True), \
             mock.patch.object(claude_setup.os, "access", return_value=True), \
             mock.patch.object(claude_setup.shutil, "which") as which:
            self.assertEqual(claude_setup.resolve_claude_bin(), "/managed/claude")
            which.assert_not_called()

    def test_falls_back_to_path_and_returns_none_when_unavailable(self):
        with mock.patch.object(claude_setup, "binary_path", return_value="/managed/claude"), \
             mock.patch.object(claude_setup.os.path, "isfile", return_value=False), \
             mock.patch.object(claude_setup.shutil, "which", return_value="/usr/bin/claude") as which:
            self.assertEqual(claude_setup.resolve_claude_bin(), "/usr/bin/claude")
            which.assert_called_once_with("claude")
        with mock.patch.object(claude_setup, "binary_path", return_value="/managed/claude"), \
             mock.patch.object(claude_setup.os.path, "isfile", return_value=False), \
             mock.patch.object(claude_setup.shutil, "which", return_value=None):
            self.assertIsNone(claude_setup.resolve_claude_bin())


class HarnessReadyTests(unittest.TestCase):
    def test_selected_harness_readiness_branches(self):
        cases = (
            ("claude", True, True),
            ("codex", True, True),
            ("agy", True, True),
        )
        for selected, installed, authenticated in cases:
            with self.subTest(selected=selected), \
                 mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=selected), \
                 mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=authenticated), \
                 mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value="/bin/claude"), \
                 mock.patch.object(daemon.codex_setup, "is_installed", return_value=installed), \
                 mock.patch.object(daemon.codex_setup, "is_authenticated", return_value=authenticated), \
                 mock.patch.object(daemon.antigravity_setup, "is_installed", return_value=installed), \
                 mock.patch.object(daemon.antigravity_setup, "is_authenticated", return_value=authenticated):
                self.assertTrue(daemon.harness_ready())

    def test_selected_harness_requires_its_own_readiness(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value="claude"), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value=None):
            self.assertFalse(daemon.harness_ready())
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value="codex"), \
             mock.patch.object(daemon.codex_setup, "is_installed", return_value=False), \
             mock.patch.object(daemon.codex_setup, "is_authenticated", return_value=True):
            self.assertFalse(daemon.harness_ready())
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value="agy"), \
             mock.patch.object(daemon.antigravity_setup, "is_installed", return_value=True), \
             mock.patch.object(daemon.antigravity_setup, "is_authenticated", return_value=False):
            self.assertFalse(daemon.harness_ready())

    def test_missing_flag_migrates_authenticated_claude_once(self):
        # Step4増分1c(sol 1b/1c High): migration も compare_and_set 経由に統一。
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "compare_and_set", return_value=("set", "claude")) as cas, \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value="/bin/claude"):
            self.assertTrue(daemon.harness_ready())
        cas.assert_called_once_with("claude")

    def test_missing_flag_migration_yields_to_concurrent_selection(self):
        # sol 1b/1c High: migration 中に別ハーネスが CAS 確定していたら claude を決め打ちせず
        # conflict の current を採用する(既に選択済みを上書きしない)。
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "compare_and_set", return_value=("conflict", "codex")), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.codex_setup, "is_installed", return_value=True), \
             mock.patch.object(daemon.codex_setup, "is_authenticated", return_value=True):
            # 実効ハーネスは codex。codex が ready なので True(claude 判定に落ちない)。
            self.assertTrue(daemon.harness_ready())

    def test_missing_flag_without_claude_auth_waits_for_setup(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "compare_and_set") as cas, \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=False):
            self.assertFalse(daemon.harness_ready())
        cas.assert_not_called()

    def test_migration_write_error_keeps_claude_ready_when_binary_exists(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "compare_and_set", side_effect=OSError("read-only")), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value="/bin/claude"):
            self.assertTrue(daemon.harness_ready())

    def test_migration_write_error_waits_when_claude_binary_is_missing(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "compare_and_set", side_effect=OSError("read-only")), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value=None):
            self.assertFalse(daemon.harness_ready())

    def test_invalid_flag_never_migrates_to_claude(self):
        for selection_state in ("invalid",):
            with self.subTest(selection_state=selection_state), \
                 mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
                 mock.patch.object(daemon.harness_state, "read_selection", return_value=(selection_state, None)), \
                 mock.patch.object(daemon.harness_state, "compare_and_set") as cas, \
                 mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True):
                self.assertFalse(daemon.harness_ready())
            cas.assert_not_called()

    def test_empty_and_invalid_flag_files_never_migrate_to_claude(self):
        for contents in ("\n", "not-a-harness\n"):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as temp:
                flag = Path(temp) / "selected_harness"
                flag.write_text(contents, encoding="utf-8")
                with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": str(flag)}, clear=False), \
                     mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
                     mock.patch.object(daemon.harness_state, "compare_and_set") as cas:
                    self.assertFalse(daemon.harness_ready())
                cas.assert_not_called()

    def test_unreadable_flag_never_migrates_to_claude(self):
        with mock.patch.object(daemon.harness_state, "flag_path", return_value="/unreadable"), \
             mock.patch("builtins.open", side_effect=OSError("read failed")), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.harness_state, "compare_and_set") as cas:
            self.assertFalse(daemon.harness_ready())
        cas.assert_not_called()

    def test_missing_flag_still_migrates_to_claude(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "compare_and_set", return_value=("set", "claude")) as cas, \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value="/bin/claude"):
            self.assertTrue(daemon.harness_ready())
        cas.assert_called_once_with("claude")


class SetupWaitNotificationTests(unittest.TestCase):
    def setUp(self):
        daemon._setup_wait_notification_sent = False

    def _notify(self, authenticated):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=authenticated), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value=None), \
             mock.patch.object(daemon.urllib.request, "urlopen", return_value=response) as urlopen, \
             mock.patch.object(daemon, "get_ha_token", return_value="test-token"), \
             mock.patch.object(daemon, "HA_URL", "http://supervisor/core/api"):
            daemon.notify_setup_waiting()
            daemon.notify_setup_waiting()
        self.assertEqual(urlopen.call_count, 1)
        return urlopen.call_args.args[0]

    def test_notification_is_once_and_mentions_reinstall_for_authenticated_claude(self):
        request = self._notify(authenticated=True)
        body = json.loads(request.data.decode("utf-8"))
        self.assertIn("記憶は保持されています", body["message"])
        self.assertIn("再インストール", body["message"])
        self.assertEqual(body["notification_id"], daemon._SETUP_WAIT_NOTIFICATION_ID)
        self.assertEqual(request.full_url, "http://supervisor/core/api/services/persistent_notification/create")

    def test_notification_mentions_harness_choice_for_new_setup(self):
        request = self._notify(authenticated=False)
        body = json.loads(request.data.decode("utf-8"))
        self.assertIn("ハーネスを選んでインストール", body["message"])

    def test_polling_does_not_repeat_notification(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=False), \
             mock.patch.object(daemon.urllib.request, "urlopen", return_value=response) as urlopen, \
             mock.patch.object(daemon, "harness_ready", side_effect=[False, False, True]), \
             mock.patch.object(daemon, "start_runtime_threads"), \
             mock.patch.object(daemon.time, "sleep") as sleep:
            daemon.boot_runtime_when_ready()
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(sleep.call_count, 2)

    def test_failed_notification_retries_until_a_successful_post(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=False), \
             mock.patch.object(daemon.urllib.request, "urlopen", side_effect=[OSError("offline"), response]) as urlopen:
            daemon.notify_setup_waiting()
            daemon.notify_setup_waiting()
            daemon.notify_setup_waiting()
        self.assertEqual(urlopen.call_count, 2)

    def test_notification_message_follows_selection_and_setup_state(self):
        cases = (
            ("claude", False, False, "Claude Codeをインストール"),
            ("claude", True, False, "Claude Codeにログイン"),
            ("codex", False, False, "Codexをインストール"),
            ("codex", True, False, "Codexにログイン"),
            ("agy", False, False, "Antigravityをインストール"),
            ("agy", True, False, "Antigravityにログイン"),
        )
        for selected, installed, authenticated, expected in cases:
            with self.subTest(selected=selected, installed=installed, authenticated=authenticated):
                daemon._setup_wait_notification_sent = False
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.__exit__.return_value = False
                with mock.patch.object(daemon.harness_state, "read_selection", return_value=("valid", selected)), \
                     mock.patch.object(daemon.claude_setup, "is_installed", return_value=installed), \
                     mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=authenticated), \
                     mock.patch.object(daemon.codex_setup, "is_installed", return_value=installed), \
                     mock.patch.object(daemon.codex_setup, "is_authenticated", return_value=authenticated), \
                     mock.patch.object(daemon.antigravity_setup, "is_installed", return_value=installed), \
                     mock.patch.object(daemon.antigravity_setup, "is_authenticated", return_value=authenticated), \
                     mock.patch.object(daemon.urllib.request, "urlopen", return_value=response) as urlopen:
                    daemon.notify_setup_waiting()
                body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
                self.assertIn(expected, body["message"])

    def test_notification_message_handles_grandfather_and_new_setup(self):
        cases = (
            (True, None, "記憶は保持されています。Web UIでClaude Codeを再インストール"),
            (False, None, "ハーネスを選んでインストール"),
        )
        for authenticated, claude_binary, expected in cases:
            with self.subTest(authenticated=authenticated):
                daemon._setup_wait_notification_sent = False
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.__exit__.return_value = False
                with mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
                     mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=authenticated), \
                     mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value=claude_binary), \
                     mock.patch.object(daemon.urllib.request, "urlopen", return_value=response) as urlopen:
                    daemon.notify_setup_waiting()
                body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
                self.assertIn(expected, body["message"])


class ServerHarnessPersistenceTests(unittest.TestCase):
    class _ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    @staticmethod
    def _handler():
        handler = object.__new__(server.Handler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        return handler

    def setUp(self):
        # 選択フラグを実ファイルで隔離(Step4増分1cで _record→_commit/CAS 化したため、
        # モックした set_selected ではなく実 compare_and_set の結果を検証する)。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        flag = os.path.join(self._tmp.name, "selected_harness")
        patcher = mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": flag}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_install_handler_commits_selection_and_second_harness_conflicts(self):
        # Step4増分1c(sol H1): 初回 install が選択を確定し、異なるハーネスの後続 install は
        # 「初回固定」で上書きせず conflict になる(選択は最初のまま)。
        claude = types.SimpleNamespace(
            install=lambda progress: (progress("installed") or {"ok": True}),
            is_installed=lambda: True,
        )
        codex = types.SimpleNamespace(
            install=lambda progress: (progress("installed") or {"ok": True}),
            is_installed=lambda: True,
        )
        with mock.patch.object(server.threading, "Thread", self._ImmediateThread), \
             mock.patch.object(server, "claude_setup", claude), \
             mock.patch.object(server, "codex_setup", codex):
            self._handler()._serve_setup_claude_install()
            self.assertEqual(harness_state.get_selected_harness(), "claude")
            self._handler()._serve_setup_codex_install()
        # codex install は走るが初回固定で選択は claude のまま(偽の上書きなし)。
        self.assertEqual(harness_state.get_selected_harness(), "claude")

    def test_antigravity_install_commits_only_with_zero_exit_and_binary(self):
        # sol H9: rc==0 かつ実 binary あり → 選択確定。
        with tempfile.TemporaryDirectory() as temp:
            agy = types.SimpleNamespace(
                home_dir=lambda: temp,
                bin_dir=lambda: os.path.join(temp, "bin"),
                fetch_install_script=lambda timeout: "exit 0\n",
                subprocess_env=lambda: {},
                is_installed=lambda: True,
            )
            process = mock.MagicMock()
            process.stdin = mock.MagicMock()
            process.stdout = []
            process.wait.return_value = 0
            process.poll.return_value = 0
            with mock.patch.object(server.threading, "Thread", self._ImmediateThread), \
                 mock.patch.object(server, "antigravity_setup", agy), \
                 mock.patch("subprocess.Popen", return_value=process):
                self._handler()._serve_setup_antigravity_install()
        self.assertEqual(harness_state.get_selected_harness(), "agy")

    def test_antigravity_install_does_not_commit_when_binary_missing(self):
        # sol H9: rc==0 でも実 binary が無ければ偽成功にしない=選択を確定しない。
        with tempfile.TemporaryDirectory() as temp:
            agy = types.SimpleNamespace(
                home_dir=lambda: temp,
                bin_dir=lambda: os.path.join(temp, "bin"),
                fetch_install_script=lambda timeout: "exit 0\n",
                subprocess_env=lambda: {},
                is_installed=lambda: False,
            )
            process = mock.MagicMock()
            process.stdin = mock.MagicMock()
            process.stdout = []
            process.wait.return_value = 0
            process.poll.return_value = 0
            with mock.patch.object(server.threading, "Thread", self._ImmediateThread), \
                 mock.patch.object(server, "antigravity_setup", agy), \
                 mock.patch("subprocess.Popen", return_value=process):
                self._handler()._serve_setup_antigravity_install()
        self.assertIsNone(harness_state.get_selected_harness())

    def test_commit_selected_harness_sets_when_binary_present(self):
        with mock.patch.object(server, "claude_setup", types.SimpleNamespace(is_installed=lambda: True)):
            server._commit_selected_harness("claude")
        self.assertEqual(harness_state.get_selected_harness(), "claude")

    def test_commit_raises_and_does_not_write_when_binary_missing(self):
        # sol H1/H9: install 完了扱いでも binary が無ければ raise(偽成功禁止)、フラグ未書込。
        with mock.patch.object(server, "claude_setup", types.SimpleNamespace(is_installed=lambda: False)):
            with self.assertRaises(RuntimeError):
                server._commit_selected_harness("claude")
        self.assertIsNone(harness_state.get_selected_harness())

    def test_commit_raises_on_conflicting_selection(self):
        # sol H1: 別の valid 選択が既にあれば raise、既存選択は不変。
        harness_state.set_selected_harness("claude")
        with mock.patch.object(server, "codex_setup", types.SimpleNamespace(is_installed=lambda: True)):
            with self.assertRaises(RuntimeError):
                server._commit_selected_harness("codex")
        self.assertEqual(harness_state.get_selected_harness(), "claude")

    def test_commit_is_idempotent_for_same_harness(self):
        with mock.patch.object(server, "claude_setup", types.SimpleNamespace(is_installed=lambda: True)):
            server._commit_selected_harness("claude")
            server._commit_selected_harness("claude")  # unchanged, 例外なし
        self.assertEqual(harness_state.get_selected_harness(), "claude")

    def test_install_handler_emits_error_and_no_done_on_conflict(self):
        # sol Med2: 別ハーネス選択済みでの install は error SSE を出し done を出さない(偽成功禁止)。
        harness_state.set_selected_harness("claude")
        codex = types.SimpleNamespace(
            install=lambda progress: (progress("installed") or {"ok": True}),
            is_installed=lambda: True,
        )
        handler = self._handler()
        with mock.patch.object(server.threading, "Thread", self._ImmediateThread), \
             mock.patch.object(server, "codex_setup", codex):
            handler._serve_setup_codex_install()
        body = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("event: error", body)
        self.assertNotIn("event: done", body)
        self.assertEqual(harness_state.get_selected_harness(), "claude")

    def test_install_handler_emits_error_and_no_done_when_binary_missing(self):
        # sol Med2/H9: rc相当は成功でも binary 欠落なら error SSE、done なし、選択未確定。
        claude = types.SimpleNamespace(
            install=lambda progress: (progress("installed") or {"ok": True}),
            is_installed=lambda: False,
        )
        handler = self._handler()
        with mock.patch.object(server.threading, "Thread", self._ImmediateThread), \
             mock.patch.object(server, "claude_setup", claude):
            handler._serve_setup_claude_install()
        body = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("event: error", body)
        self.assertNotIn("event: done", body)
        self.assertIsNone(harness_state.get_selected_harness())


if __name__ == "__main__":
    unittest.main()
