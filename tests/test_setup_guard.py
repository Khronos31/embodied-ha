import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha" / "web"))
os.environ.setdefault("HA_URL", "http://homeassistant.invalid")

import server  # noqa: E402


class SetupGuardTests(unittest.TestCase):
    # Deliberately independent from server._SETUP_MUTATION_PATHS: adding a
    # dispatch route but forgetting the guard set must fail this test.
    MUTATION_ROUTES = (
        ("GET", "/api/setup/login"),
        ("POST", "/api/setup/login-code"),
        ("GET", "/api/setup/claude/login"),
        ("POST", "/api/setup/claude/login-code"),
        ("POST", "/api/setup/claude/install"),
        ("POST", "/api/setup/claude/uninstall"),
        ("POST", "/api/setup/claude/clear-auth"),
        ("GET", "/api/setup/antigravity/install"),
        ("GET", "/api/setup/antigravity/login"),
        ("POST", "/api/setup/antigravity/input"),
        ("POST", "/api/setup/antigravity/login-code"),
        ("POST", "/api/setup/antigravity/uninstall"),
        ("POST", "/api/setup/antigravity/clear-auth"),
        ("POST", "/api/setup/codex/install"),
        ("POST", "/api/setup/codex/login"),
        ("POST", "/api/setup/codex/uninstall"),
        ("POST", "/api/setup/codex/clear-auth"),
    )

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()

    def _request(self, method, path):
        data = None if method == "GET" else b"{}"
        return urllib.request.urlopen(
            urllib.request.Request(self.base_url + path, data=data, method=method), timeout=3
        )

    def test_loopback_rejects_every_setup_mutation_alias(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for method, path in self.MUTATION_ROUTES:
                with self.subTest(method=method, path=path), self.assertRaises(urllib.error.HTTPError) as raised:
                    self._request(method, path)
                self.assertEqual(raised.exception.code, 403)
                self.assertEqual(
                    json.loads(raised.exception.read()), {"error": server._SETUP_GUARD_ERROR}
                )

    def test_only_ingress_source_is_allowed_unless_overridden_or_disabled(self):
        handler = object.__new__(server.Handler)
        handler.send_json = mock.Mock()

        with mock.patch.dict(os.environ, {}, clear=True):
            handler.client_address = ("172.30.32.2", 12345)
            self.assertFalse(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))
            self.assertTrue(server.setup_guard(handler.client_address))

            handler.client_address = ("172.30.33.7", 12345)
            self.assertTrue(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))

            handler.client_address = ("127.0.0.1", 12345)
            self.assertTrue(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))
            self.assertEqual(handler.send_json.call_count, 2)

        handler.send_json.reset_mock()
        with mock.patch.dict(os.environ, {"EHA_INGRESS_SOURCE": "10.0.0.1, 2001:db8::1"}, clear=True):
            handler.client_address = ("10.0.0.1", 12345)
            self.assertFalse(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))
            handler.client_address = ("2001:db8::1", 12345)
            self.assertTrue(server.setup_guard(handler.client_address))
            handler.client_address = ("172.30.32.2", 12345)
            self.assertTrue(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))

        handler.send_json.reset_mock()
        with mock.patch.dict(os.environ, {"EHA_SETUP_GUARD": "off"}, clear=True):
            handler.client_address = ("127.0.0.1", 12345)
            self.assertFalse(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))
            self.assertTrue(server.setup_guard(handler.client_address))
            handler.send_json.assert_not_called()

    def test_status_routes_are_not_guarded(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.send_json = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=True):
            for path in (
                "/api/setup/status", "/api/setup/antigravity/status",
                "/api/setup/codex/status", "/api/setup/claude/status",
            ):
                self.assertFalse(handler._block_loopback_setup_mutation(path))
        handler.send_json.assert_not_called()


class AntigravityInstallEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.harness_flag_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.harness_flag_dir)
        self.harness_flag_env = mock.patch.dict(
            os.environ,
            {"EHA_HARNESS_FLAG_FILE": os.path.join(self.harness_flag_dir, "selected_harness")},
            clear=False,
        )
        self.harness_flag_env.start()
        self.addCleanup(self.harness_flag_env.stop)

    def test_install_script_child_env_excludes_secrets(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = iter(())

            def wait(self):
                return 0

            def poll(self):
                return 0

            def terminate(self):
                pass

        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        handler = object.__new__(server.Handler)
        handler.send_response = lambda *_args: None
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        process = FakeProcess()
        with mock.patch.dict(os.environ, {
            "SUPERVISOR_TOKEN": "supervisor-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
        }, clear=False), mock.patch.object(
            server.antigravity_setup, "fetch_install_script", return_value="exit 0\n"
        ), mock.patch.object(server.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
            server.threading, "Thread", InlineThread
        ):
            handler._serve_setup_antigravity_install()

        env = popen.call_args.kwargs["env"]
        self.assertEqual(set(env), {"HOME", "PATH", "LANG"})
        self.assertNotIn("SUPERVISOR_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)


if __name__ == "__main__":
    unittest.main()
