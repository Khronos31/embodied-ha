import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
os.environ.setdefault("HA_URL", "http://homeassistant.invalid")

from web import server  # noqa: E402


class SetupMutationLockTests(unittest.TestCase):
    def setUp(self):
        self.setup_guard_env = mock.patch.dict(
            os.environ, {"EHA_SETUP_GUARD": "off"}, clear=False
        )
        self.setup_guard_env.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()
        self.setup_guard_env.stop()

    def _post_json(self, path, body=None):
        request = urllib.request.Request(
            self.base_url + path, data=json.dumps(body or {}).encode(), method="POST"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_antigravity_destructive_operations_wait_for_install_or_login(self):
        fake = SimpleNamespace(
            uninstall=mock.Mock(return_value={"removed_files": ["bin"]}),
            clear_auth=mock.Mock(return_value={"removed_files": ["auth"]}),
        )
        with mock.patch.object(server, "antigravity_setup", fake):
            for lock in (
                server._ANTIGRAVITY_INSTALL_LOCK,
                server._ANTIGRAVITY_LOGIN_SESSION_LOCK,
            ):
                self.assertTrue(lock.acquire(blocking=False))
                try:
                    for endpoint in ("uninstall", "clear-auth"):
                        with self.subTest(lock=lock, endpoint=endpoint), self.assertRaises(
                            urllib.error.HTTPError
                        ) as raised:
                            self._post_json(f"/api/setup/antigravity/{endpoint}")
                        self.assertEqual(raised.exception.code, 409)
                        self.assertEqual(
                            json.loads(raised.exception.read()),
                            {"error": "Antigravity setup is busy"},
                        )
                finally:
                    lock.release()

            # 応答返却とハンドラのfinally(ロック解放)は非同期。連続する成功
            # リクエストの間でも解放前に次が始まると409で落ちるため、
            # 各成功応答の後に毎回解放を待つ(solフレーク指摘の完全対応)
            def wait_for_release():
                for _ in range(100):
                    if not (server._ANTIGRAVITY_INSTALL_LOCK.locked()
                            or server._ANTIGRAVITY_LOGIN_SESSION_LOCK.locked()):
                        return
                    time.sleep(0.01)

            self.assertEqual(
                self._post_json("/api/setup/antigravity/uninstall"),
                {"ok": True, "removed_files": ["bin"]},
            )
            wait_for_release()
            self.assertEqual(
                self._post_json("/api/setup/antigravity/clear-auth"),
                {"ok": True, "removed_files": ["auth"]},
            )
            wait_for_release()
        self.assertFalse(server._ANTIGRAVITY_INSTALL_LOCK.locked())
        self.assertFalse(server._ANTIGRAVITY_LOGIN_SESSION_LOCK.locked())

    def test_codex_status_reports_actual_mutation_and_busy_error(self):
        fake = SimpleNamespace(
            state=lambda: {"installed": True, "authenticated": False},
            uninstall=lambda: {"removed_files": []},
        )
        with mock.patch.object(server, "codex_setup", fake):
            self.assertTrue(server._acquire_codex_mutation("login"))
            try:
                status = server.codex_status()
                self.assertFalse(status["installing"])
                self.assertEqual(status["active_operation"], "login")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self._post_json("/api/setup/codex/uninstall")
                self.assertEqual(raised.exception.code, 409)
                self.assertEqual(
                    json.loads(raised.exception.read()), {"error": "Codex login is running"}
                )
            finally:
                server._release_codex_mutation()

            self.assertTrue(server._acquire_codex_mutation("install"))
            try:
                status = server.codex_status()
                self.assertTrue(status["installing"])
                self.assertEqual(status["active_operation"], "install")
            finally:
                server._release_codex_mutation()

    def test_claude_login_owns_lock_until_session_ends(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            release_path = temp_path / "release-login"
            binary = temp_path / "fake-claude"
            binary.write_text(
                "#!/bin/sh\n"
                "echo 'https://claude.example.invalid/login'\n"
                f"while [ ! -e '{release_path}' ]; do sleep 0.01; done\n"
                "exit 0\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            fake_claude_setup = mock.Mock()
            fake_claude_setup.is_authenticated.return_value = False

            with mock.patch.object(server, "CLAUDE_BIN", str(binary)), \
                 mock.patch.object(server, "claude_setup", fake_claude_setup):
                first_request = urllib.request.Request(self.base_url + "/api/setup/claude/login")
                with urllib.request.urlopen(first_request, timeout=5) as first_response:
                    for _ in range(100):
                        if server._CLAUDE_MUTATION_LOCK.locked():
                            break
                        time.sleep(0.01)
                    self.assertTrue(server._CLAUDE_MUTATION_LOCK.locked())

                    with urllib.request.urlopen(
                        urllib.request.Request(self.base_url + "/api/setup/login"), timeout=5
                    ) as busy_response:
                        self.assertEqual(busy_response.readline().decode().strip(), "event: error")
                        self.assertIn("Claude login is busy", busy_response.readline().decode())

                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        self._post_json("/api/setup/claude/clear-auth")
                    self.assertEqual(raised.exception.code, 409)
                    self.assertEqual(
                        json.loads(raised.exception.read()), {"error": "Claude login is busy"}
                    )
                    fake_claude_setup.clear_auth.assert_not_called()

                    release_path.touch()
                    events = []
                    for _ in range(10):
                        line = first_response.readline().decode()
                        events.append(line)
                        if line == "event: done\n":
                            break
                    self.assertIn("event: done\n", events)

            for _ in range(100):
                if not server._CLAUDE_MUTATION_LOCK.locked():
                    break
                time.sleep(0.01)
            self.assertTrue(server._CLAUDE_MUTATION_LOCK.acquire(blocking=False))
            server._CLAUDE_MUTATION_LOCK.release()


if __name__ == "__main__":
    unittest.main()
