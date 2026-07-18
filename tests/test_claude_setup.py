import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from http.server import ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
os.environ.setdefault("HA_URL", "http://homeassistant.invalid")

import claude_setup  # noqa: E402
from web import server  # noqa: E402


class ClaudeSetupTests(unittest.TestCase):
    def test_paths_auth_state_and_clear_auth_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": temp}, clear=False
        ):
            primary, legacy = claude_setup.credentials_paths()
            self.assertEqual(primary, os.path.join(temp, ".credentials.json"))
            self.assertEqual(legacy, os.path.join(temp, "credentials.json"))
            self.assertTrue(claude_setup.is_installed())
            self.assertFalse(claude_setup.is_authenticated())
            self.assertEqual(claude_setup.clear_auth(), {"removed_files": []})

            Path(primary).write_text("token", encoding="utf-8")
            Path(legacy).write_text("token", encoding="utf-8")
            self.assertTrue(claude_setup.is_authenticated())
            self.assertEqual(
                claude_setup.state(),
                {
                    "installed": True,
                    "authenticated": True,
                    "config_dir": temp,
                    "credentials_paths": [primary, legacy],
                },
            )
            self.assertEqual(claude_setup.clear_auth(), {"removed_files": [primary, legacy]})
            self.assertFalse(claude_setup.is_authenticated())
            self.assertEqual(claude_setup.clear_auth(), {"removed_files": []})

    def test_api_key_auth_is_preserved_when_credentials_are_cleared(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": temp, "ANTHROPIC_API_KEY": "test-key"},
            clear=False,
        ):
            Path(claude_setup.credentials_paths()[0]).write_text("token", encoding="utf-8")
            self.assertTrue(claude_setup.is_authenticated())
            claude_setup.clear_auth()
            self.assertTrue(claude_setup.is_authenticated())


class ClaudeSetupEndpointTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()

    def _get_json(self, path):
        with urllib.request.urlopen(self.base_url + path) as response:
            return json.loads(response.read())

    def _post_json(self, path, body=None):
        data = json.dumps(body or {}).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method="POST")
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def test_old_and_new_status_shapes_and_login_dispatch(self):
        fake = mock.Mock()
        fake.is_authenticated.return_value = True
        fake.state.return_value = {"installed": True, "authenticated": True, "config_dir": "/tmp/claude"}
        with mock.patch.object(server, "claude_setup", fake), \
             mock.patch.object(server, "antigravity_status", return_value={"installed": False}):
            old_status = self._get_json("/api/setup/status")
            self.assertEqual(old_status, {"authenticated": True, "antigravity": {"installed": False}})
            self.assertEqual(self._get_json("/api/setup/claude/status"), fake.state.return_value)

            login_calls = []

            def served_login(handler):
                login_calls.append(handler.path)
                handler.send_json({"ok": True})

            with mock.patch.object(server.Handler, "_serve_setup_login", served_login):
                self._get_json("/api/setup/login")
                self._get_json("/api/setup/claude/login")
            self.assertEqual(login_calls, ["/api/setup/login", "/api/setup/claude/login"])

    def test_old_and_new_login_code_dispatch_and_clear_auth(self):
        fake = mock.Mock(clear_auth=lambda: {"removed_files": ["/tmp/.credentials.json"]})
        with mock.patch.object(server, "claude_setup", fake), \
             mock.patch.object(server, "_login_pty_fd", [123]), \
             mock.patch.object(server.os, "write") as write:
            self.assertEqual(self._post_json("/api/setup/login-code", {"code": "old"}), {"ok": True})
            self.assertEqual(self._post_json("/api/setup/claude/login-code", {"code": "new"}), {"ok": True})
            self.assertEqual(write.call_args_list, [mock.call(123, b"old\r"), mock.call(123, b"new\r")])
            self.assertEqual(
                self._post_json("/api/setup/claude/clear-auth"),
                {"ok": True, "removed_files": ["/tmp/.credentials.json"]},
            )


if __name__ == "__main__":
    unittest.main()
