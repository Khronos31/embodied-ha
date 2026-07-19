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
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "set_selected_harness") as set_selected, \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value="/bin/claude"):
            self.assertTrue(daemon.harness_ready())
        set_selected.assert_called_once_with("claude")

    def test_missing_flag_without_claude_auth_waits_for_setup(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "set_selected_harness") as set_selected, \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=False):
            self.assertFalse(daemon.harness_ready())
        set_selected.assert_not_called()

    def test_migration_write_error_keeps_claude_ready_when_binary_exists(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "set_selected_harness", side_effect=OSError("read-only")), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value="/bin/claude"):
            self.assertTrue(daemon.harness_ready())

    def test_migration_write_error_waits_when_claude_binary_is_missing(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "set_selected_harness", side_effect=OSError("read-only")), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value=None):
            self.assertFalse(daemon.harness_ready())

    def test_invalid_flag_never_migrates_to_claude(self):
        for selection_state in ("invalid",):
            with self.subTest(selection_state=selection_state), \
                 mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
                 mock.patch.object(daemon.harness_state, "read_selection", return_value=(selection_state, None)), \
                 mock.patch.object(daemon.harness_state, "set_selected_harness") as set_selected, \
                 mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True):
                self.assertFalse(daemon.harness_ready())
            set_selected.assert_not_called()

    def test_empty_and_invalid_flag_files_never_migrate_to_claude(self):
        for contents in ("\n", "not-a-harness\n"):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as temp:
                flag = Path(temp) / "selected_harness"
                flag.write_text(contents, encoding="utf-8")
                with mock.patch.dict(os.environ, {"EHA_HARNESS_FLAG_FILE": str(flag)}, clear=False), \
                     mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
                     mock.patch.object(daemon.harness_state, "set_selected_harness") as set_selected:
                    self.assertFalse(daemon.harness_ready())
                set_selected.assert_not_called()

    def test_unreadable_flag_never_migrates_to_claude(self):
        with mock.patch.object(daemon.harness_state, "flag_path", return_value="/unreadable"), \
             mock.patch("builtins.open", side_effect=OSError("read failed")), \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.harness_state, "set_selected_harness") as set_selected:
            self.assertFalse(daemon.harness_ready())
        set_selected.assert_not_called()

    def test_missing_flag_still_migrates_to_claude(self):
        with mock.patch.object(daemon.harness_state, "get_selected_harness", return_value=None), \
             mock.patch.object(daemon.harness_state, "read_selection", return_value=("missing", None)), \
             mock.patch.object(daemon.harness_state, "set_selected_harness") as set_selected, \
             mock.patch.object(daemon.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(daemon.claude_setup, "resolve_claude_bin", return_value="/bin/claude"):
            self.assertTrue(daemon.harness_ready())
        set_selected.assert_called_once_with("claude")


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

    def test_claude_and_codex_install_handlers_record_selection_after_success(self):
        claude = types.SimpleNamespace(
            install=lambda progress: (progress("installed") or {"ok": True}),
        )
        codex = types.SimpleNamespace(
            install=lambda progress: (progress("installed") or {"ok": True}),
        )
        with mock.patch.object(server.threading, "Thread", self._ImmediateThread), \
             mock.patch.object(server.harness_state, "set_selected_harness") as set_selected, \
             mock.patch.object(server, "claude_setup", claude), \
             mock.patch.object(server, "codex_setup", codex):
            self._handler()._serve_setup_claude_install()
            self._handler()._serve_setup_codex_install()
        self.assertEqual(set_selected.call_args_list, [mock.call("claude"), mock.call("codex")])

    def test_antigravity_install_handler_records_selection_only_for_zero_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            agy = types.SimpleNamespace(
                home_dir=lambda: temp,
                bin_dir=lambda: os.path.join(temp, "bin"),
                fetch_install_script=lambda timeout: "exit 0\n",
                subprocess_env=lambda: {},
            )
            process = mock.MagicMock()
            process.stdin = mock.MagicMock()
            process.stdout = []
            process.wait.return_value = 0
            process.poll.return_value = 0
            with mock.patch.object(server.threading, "Thread", self._ImmediateThread), \
                 mock.patch.object(server.harness_state, "set_selected_harness") as set_selected, \
                 mock.patch.object(server, "antigravity_setup", agy), \
                 mock.patch("subprocess.Popen", return_value=process):
                self._handler()._serve_setup_antigravity_install()
        set_selected.assert_called_once_with("agy")

    def test_successful_install_records_each_harness_without_affecting_install(self):
        with mock.patch.object(server.harness_state, "set_selected_harness") as set_selected:
            for harness in harness_state.VALID_HARNESSES:
                server._record_selected_harness(harness)
        self.assertEqual(
            set_selected.call_args_list,
            [mock.call("claude"), mock.call("codex"), mock.call("agy")],
        )

    def test_selection_record_failure_is_best_effort(self):
        with mock.patch.object(server.harness_state, "set_selected_harness", side_effect=OSError):
            server._record_selected_harness("claude")


if __name__ == "__main__":
    unittest.main()
