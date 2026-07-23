import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
os.environ.setdefault("HA_URL", "http://homeassistant.invalid")

from web import server  # noqa: E402


class LogoutStep3Tests(unittest.TestCase):
    def setUp(self):
        self._harness_dir = tempfile.TemporaryDirectory()
        self.harness_flag = Path(self._harness_dir.name) / "selected_harness"
        self._harness_env = mock.patch.dict(
            os.environ, {"EHA_HARNESS_FLAG_FILE": str(self.harness_flag)}, clear=False
        )
        self._harness_env.start()
        self._reset_self_restart_latch()

    def tearDown(self):
        self._reset_self_restart_latch()
        self._harness_env.stop()
        self._harness_dir.cleanup()

    @staticmethod
    def _reset_self_restart_latch():
        with server._self_restart_lock:
            server._self_restart_scheduled = False

    def _select_harness(self, harness):
        self.harness_flag.write_text(f"{harness}\n", encoding="utf-8")

    @staticmethod
    def _wait_for_latch(value):
        for _ in range(100):
            with server._self_restart_lock:
                if server._self_restart_scheduled is value:
                    return
            threading.Event().wait(0.01)
        raise AssertionError(f"self-restart latch did not become {value}")

    def _handler(self):
        handler = object.__new__(server.Handler)
        handler.send_json = mock.Mock()
        return handler

    def _assert_successful_logout(self, harness):
        handler = self._handler()
        with mock.patch.object(server, "_schedule_self_restart") as schedule_restart:
            handler._serve_setup_logout(harness)
        schedule_restart.assert_called_once_with()
        response, status = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertTrue(response["restarting"])
        self.assertIn("セットアップ待ち", response["message"])

    def test_claude_logout_removes_credentials_and_keeps_projects(self):
        self._select_harness("claude")
        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "claude"
            projects = config_dir / "projects"
            projects.mkdir(parents=True)
            session = projects / "session.json"
            session.write_text("memory", encoding="utf-8")

            with mock.patch.dict(
                os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}, clear=False
            ), mock.patch.object(server.claude_setup, "resolve_claude_bin", return_value="claude"):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                credentials = [Path(path) for path in server.claude_setup.credentials_paths()]
                for credential in credentials:
                    credential.write_text("auth", encoding="utf-8")
                self._assert_successful_logout("claude")

            self.assertTrue(all(not credential.exists() for credential in credentials))
            self.assertTrue(session.exists())

    def test_codex_logout_removes_credentials_and_keeps_projects(self):
        self._select_harness("codex")
        with tempfile.TemporaryDirectory() as tempdir:
            home_dir = Path(tempdir) / "codex"
            projects = home_dir / "projects"
            projects.mkdir(parents=True)
            session = projects / "session.json"
            session.write_text("memory", encoding="utf-8")

            with mock.patch.dict(os.environ, {"EHA_CODEX_HOME": str(home_dir)}, clear=False), \
                 mock.patch.object(server.codex_setup, "is_installed", return_value=True):
                auth_path = Path(server.codex_setup.auth_path())
                auth_path.write_text("auth", encoding="utf-8")
                self._assert_successful_logout("codex")

            self.assertFalse(auth_path.exists())
            self.assertTrue(session.exists())

    def test_antigravity_logout_removes_credentials_and_keeps_projects(self):
        self._select_harness("agy")
        with tempfile.TemporaryDirectory() as tempdir:
            home_dir = Path(tempdir) / "antigravity"
            projects = home_dir / "projects"
            projects.mkdir(parents=True)
            session = projects / "session.json"
            session.write_text("memory", encoding="utf-8")

            with mock.patch.dict(
                os.environ, {"EHA_ANTIGRAVITY_HOME": str(home_dir)}, clear=False
            ), mock.patch.object(server.antigravity_setup, "is_installed", return_value=True):
                marker_path = Path(server.antigravity_setup.auth_marker_path())
                token_path = Path(server.antigravity_setup.oauth_token_path())
                marker_path.parent.mkdir(parents=True)
                token_path.parent.mkdir(parents=True)
                marker_path.write_text("auth", encoding="utf-8")
                token_path.write_text("auth", encoding="utf-8")
                self._assert_successful_logout("agy")

            self.assertFalse(marker_path.exists())
            self.assertFalse(token_path.exists())
            self.assertTrue(session.exists())

    def test_claude_partial_clear_failure_does_not_restart(self):
        handler = self._handler()
        result = {"removed_files": ["/tmp/credential"], "errors": ["permission denied"]}
        with mock.patch.object(server.claude_setup, "clear_auth", return_value=result), \
             mock.patch.object(server, "_schedule_self_restart") as schedule_restart:
            handler._serve_setup_logout("claude")

        handler.send_json.assert_called_once_with({"ok": False, **result}, 500)
        schedule_restart.assert_not_called()

    def test_claude_api_key_logout_succeeds_without_restart(self):
        self._select_harness("claude")
        handler = self._handler()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False), \
             mock.patch.object(
                 server.claude_setup, "clear_auth", return_value={"removed_files": []}
             ) as clear_auth, \
             mock.patch.object(server.claude_setup, "resolve_claude_bin", return_value="claude"), \
             mock.patch.object(server, "_schedule_self_restart") as schedule_restart:
            handler._serve_setup_logout("claude")

        clear_auth.assert_called_once_with()
        schedule_restart.assert_not_called()
        response, status = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertFalse(response["restarting"])
        self.assertIn("構成タブ", response["message"])
        self.assertNotIn("セットアップ待ち", response["message"])

    def test_nonselected_harness_logout_does_not_restart(self):
        self._select_harness("codex")
        handler = self._handler()
        with mock.patch.object(server.codex_setup, "is_installed", return_value=True), \
             mock.patch.object(server.codex_setup, "is_authenticated", return_value=True), \
             mock.patch.object(
                 server.antigravity_setup, "clear_auth", return_value={"removed_files": []}
             ) as clear_auth, \
             mock.patch.object(server, "_schedule_self_restart") as schedule_restart:
            handler._serve_setup_logout("agy")

        clear_auth.assert_called_once_with()
        schedule_restart.assert_not_called()
        response, status = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertFalse(response["restarting"])

    def test_successful_logout_schedules_restart_before_single_send_json(self):
        self._select_harness("claude")
        handler = self._handler()
        handler.send_json.side_effect = BrokenPipeError("client disconnected")
        with mock.patch.object(server.claude_setup, "clear_auth", return_value={"removed_files": []}), \
             mock.patch.object(server.claude_setup, "is_authenticated", return_value=False), \
             mock.patch.object(server, "_schedule_self_restart") as schedule_restart:
            with self.assertRaises(BrokenPipeError):
                handler._serve_setup_logout("claude")

        schedule_restart.assert_called_once_with()
        self.assertEqual(handler.send_json.call_count, 1)

    def test_self_restart_request_uses_supervisor_url_and_bearer_token(self):
        response = mock.MagicMock()
        with mock.patch.dict(
            os.environ, {"EHA_SUPERVISOR_URL": "http://supervisor/"}, clear=False
        ), mock.patch.object(server, "HA_TOKEN", "test-token"), \
             mock.patch.object(urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertTrue(server._request_addon_self_restart())

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://supervisor/addons/self/restart")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)

    def test_self_restart_request_retries_transient_failures(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("offline")) as urlopen, \
             mock.patch.object(server.time, "sleep") as sleep:
            self.assertFalse(server._request_addon_self_restart())

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_duplicate_self_restart_schedules_one_request(self):
        restart_called = threading.Event()

        def request_restart():
            restart_called.set()
            return True

        with mock.patch.object(server, "_SELF_RESTART_DELAY_SECONDS", 0), \
             mock.patch.object(server, "_request_addon_self_restart", side_effect=request_restart) as restart:
            server._schedule_self_restart()
            server._schedule_self_restart()
            self.assertTrue(restart_called.wait(timeout=1))

        restart.assert_called_once_with()

    def test_failed_self_restart_request_releases_latch_for_retry(self):
        restart_called = threading.Event()

        def request_restart():
            restart_called.set()
            return False

        with mock.patch.object(server, "_SELF_RESTART_DELAY_SECONDS", 0), \
             mock.patch.object(server, "_request_addon_self_restart", side_effect=request_restart) as restart:
            server._schedule_self_restart()
            self.assertTrue(restart_called.wait(timeout=1))
            self._wait_for_latch(False)
            restart_called.clear()
            server._schedule_self_restart()
            self.assertTrue(restart_called.wait(timeout=1))
            self._wait_for_latch(False)

        self.assertEqual(restart.call_count, 2)

    def test_thread_start_failure_releases_latch(self):
        thread = mock.Mock()
        thread.start.side_effect = RuntimeError("thread unavailable")
        with mock.patch.object(server.threading, "Thread", return_value=thread):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                server._schedule_self_restart()

        with server._self_restart_lock:
            self.assertFalse(server._self_restart_scheduled)

    def test_self_restart_worker_exception_releases_latch_for_retry(self):
        # helperが例外送出で終わっても、workerのtry/finallyでlatchを戻し再予約できる。
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("supervisor url malformed")

        with mock.patch.object(server, "_SELF_RESTART_DELAY_SECONDS", 0), \
             mock.patch.object(server, "_request_addon_self_restart", side_effect=boom):
            server._schedule_self_restart()
            self._wait_for_latch(False)
            server._schedule_self_restart()
            self._wait_for_latch(False)

        self.assertEqual(len(calls), 2)

    def test_self_restart_worker_releases_latch_even_if_logging_fails(self):
        # helper例外 かつ 内側の失敗ログ(print)も例外でも、外側finallyでlatchは戻る(airtight)。
        import builtins
        real_print = builtins.print

        def selective_print(*args, **kwargs):
            if args and "self-restart worker failed" in str(args[0]):
                raise OSError("stdout broken")
            return real_print(*args, **kwargs)

        with mock.patch.object(server, "_SELF_RESTART_DELAY_SECONDS", 0), \
             mock.patch.object(
                 server, "_request_addon_self_restart", side_effect=RuntimeError("helper failed")
             ), \
             mock.patch("builtins.print", side_effect=selective_print):
            server._schedule_self_restart()
            self._wait_for_latch(False)

    def test_request_addon_self_restart_returns_false_on_bad_url(self):
        # 不正なEHA_SUPERVISOR_URLでRequest生成が失敗しても例外を投げずFalseを返す。
        with mock.patch.dict(os.environ, {"EHA_SUPERVISOR_URL": "http://["}, clear=False), \
             mock.patch.object(server.time, "sleep"):
            self.assertFalse(server._request_addon_self_restart())

    def test_selected_harness_ready_missing_flag_grandfathers_claude_without_writing(self):
        # フラグ未作成(missing)+claude認証あり → True。かつフラグファイルを書かない(読み取り専用)。
        self.assertFalse(self.harness_flag.exists())
        with mock.patch.object(server.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(server.claude_setup, "resolve_claude_bin", return_value="claude"):
            self.assertTrue(server._selected_harness_ready())
        self.assertFalse(self.harness_flag.exists())

    def test_selected_harness_ready_missing_flag_no_claude_auth_is_false(self):
        with mock.patch.object(server.claude_setup, "is_authenticated", return_value=False):
            self.assertFalse(server._selected_harness_ready())

    def test_selected_harness_ready_invalid_flag_is_false(self):
        self.harness_flag.write_text("garbage\n", encoding="utf-8")
        with mock.patch.object(server.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(server.claude_setup, "resolve_claude_bin", return_value="claude"):
            self.assertFalse(server._selected_harness_ready())

    def test_selected_harness_ready_valid_claude_without_binary_is_false(self):
        self._select_harness("claude")
        with mock.patch.object(server.claude_setup, "is_authenticated", return_value=True), \
             mock.patch.object(server.claude_setup, "resolve_claude_bin", return_value=None):
            self.assertFalse(server._selected_harness_ready())

    def test_selected_harness_ready_valid_codex_not_installed_is_false(self):
        self._select_harness("codex")
        with mock.patch.object(server.codex_setup, "is_installed", return_value=False), \
             mock.patch.object(server.codex_setup, "is_authenticated", return_value=True):
            self.assertFalse(server._selected_harness_ready())


if __name__ == "__main__":
    unittest.main()
