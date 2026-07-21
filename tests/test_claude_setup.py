import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
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
    # §13.9: claude_config_dir オプションを撤去。resolve_config_dir は data_dir だけを取り、
    # 旧既定 grandfather → 新既定 の2段で解決する（option 分岐のテストは削除・契約変更メモ参照）。
    def test_resolve_config_dir_grandfathers_legacy_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = os.path.join(temp, "data")
            legacy_dir = os.path.join(data_dir, ".claude")
            os.makedirs(legacy_dir)
            Path(legacy_dir, ".credentials.json").write_text("token", encoding="utf-8")
            self.assertEqual(claude_setup.resolve_config_dir(data_dir), legacy_dir)

    def test_resolve_config_dir_grandfathers_legacy_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = os.path.join(temp, "data")
            legacy_dir = os.path.join(data_dir, ".claude")
            os.makedirs(os.path.join(legacy_dir, "projects"))
            self.assertEqual(claude_setup.resolve_config_dir(data_dir), legacy_dir)

    def test_resolve_config_dir_uses_new_default_for_empty_legacy_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = os.path.join(temp, "data")
            os.makedirs(os.path.join(data_dir, ".claude"))
            self.assertEqual(
                claude_setup.resolve_config_dir(data_dir),
                claude_setup.NEW_DEFAULT_CONFIG_DIR,
            )

    def test_resolve_config_dir_uses_new_default_for_missing_legacy_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = os.path.join(temp, "data")
            self.assertEqual(
                claude_setup.resolve_config_dir(data_dir),
                claude_setup.NEW_DEFAULT_CONFIG_DIR,
            )

    def test_paths_auth_state_and_clear_auth_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": temp, "EHA_CLAUDE_INSTALL_ROOT": root},
            clear=False,
        ):
            primary, legacy = claude_setup.credentials_paths()
            self.assertEqual(primary, os.path.join(temp, ".credentials.json"))
            self.assertEqual(legacy, os.path.join(temp, "credentials.json"))
            # is_installed() は DIY 配置バイナリの実チェック(増分1で定数True→実チェックへ)。
            self.assertFalse(claude_setup.is_installed())
            self.assertFalse(claude_setup.is_authenticated())
            self.assertEqual(claude_setup.clear_auth(), {"removed_files": []})

            # バイナリを配置すると is_installed() が True になる。
            os.makedirs(claude_setup.bin_dir(), exist_ok=True)
            Path(claude_setup.binary_path()).write_text("#!/bin/true\n", encoding="utf-8")
            os.chmod(claude_setup.binary_path(), 0o755)
            self.assertTrue(claude_setup.is_installed())

            Path(primary).write_text("token", encoding="utf-8")
            Path(legacy).write_text("token", encoding="utf-8")
            self.assertTrue(claude_setup.is_authenticated())
            self.assertEqual(
                claude_setup.state(),
                {
                    "installed": True,
                    "authenticated": True,
                    "install_root": claude_setup.install_root(),
                    "bin_dir": claude_setup.bin_dir(),
                    "binary_path": claude_setup.binary_path(),
                    "checksum_source": "Claude release manifest checksum",
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

    def test_platform_target_and_manifest_resolution_use_glibc_releases(self):
        self.assertEqual(claude_setup.platform_target("x86_64"), "linux-x64")
        self.assertEqual(claude_setup.platform_target("amd64"), "linux-x64")
        self.assertEqual(claude_setup.platform_target("aarch64"), "linux-arm64")
        self.assertEqual(claude_setup.platform_target("arm64"), "linux-arm64")
        with self.assertRaisesRegex(RuntimeError, "Unsupported architecture"):
            claude_setup.platform_target("riscv64")

        manifest = {"version": "2.1.205", "platforms": {"linux-x64": {}}}
        with mock.patch.object(
            claude_setup,
            "_read_url",
            side_effect=[b"2.1.205", json.dumps(manifest).encode()],
        ):
            self.assertEqual(
                claude_setup.resolve_manifest(),
                ("2.1.205", manifest),
            )

    def test_checksum_and_download_size_limits_are_enforced(self):
        binary = b"\x7fELFmock-claude"
        digest = hashlib.sha256(binary).hexdigest()
        claude_setup.verify_sha256(binary, digest)
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            claude_setup.verify_sha256(binary, "0" * 64)

        asset = {
            "binary": "claude",
            "checksum": digest,
            "size": claude_setup.MAX_DOWNLOAD_BYTES + 1,
        }
        with self.assertRaisesRegex(RuntimeError, "size limit"):
            claude_setup._platform_asset({"platforms": {"linux-x64": asset}}, "linux-x64")

        class Response:
            headers = {"Content-Length": "5"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b"12345"

        with mock.patch.object(claude_setup, "MAX_DOWNLOAD_BYTES", 4), \
             mock.patch.object(claude_setup, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                claude_setup._read_url("https://example.invalid/claude")

    def test_platform_asset_rejects_unsafe_binary_names_and_empty_binary(self):
        digest = hashlib.sha256(b"binary").hexdigest()
        for binary in (".", "..", "claude-old", "not-claude"):
            asset = {"binary": binary, "checksum": digest, "size": 1}
            with self.subTest(binary=binary), self.assertRaisesRegex(RuntimeError, "unsafe binary name"):
                claude_setup._platform_asset({"platforms": {"linux-x64": asset}}, "linux-x64")

        asset = {"binary": "claude", "checksum": digest, "size": 0}
        with self.assertRaisesRegex(RuntimeError, "invalid binary size"):
            claude_setup._platform_asset({"platforms": {"linux-x64": asset}}, "linux-x64")

    def test_install_verifies_manifest_and_atomically_installs_binary(self):
        binary = b"\x7fELFmock-claude"
        digest = hashlib.sha256(binary).hexdigest()
        manifest = {
            "version": "2.1.205",
            "platforms": {
                "linux-x64": {"binary": "claude", "checksum": digest, "size": len(binary)},
            },
        }
        messages = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "claude-cli"
            with mock.patch.dict(os.environ, {"EHA_CLAUDE_INSTALL_ROOT": str(root)}, clear=False), \
                 mock.patch.object(claude_setup, "platform_target", return_value="linux-x64"), \
                 mock.patch.object(
                     claude_setup,
                     "_read_url",
                     side_effect=[b"2.1.205", json.dumps(manifest).encode(), binary],
                 ):
                result = claude_setup.install(progress=messages.append)
            installed_binary = root / "bin" / "claude"
            self.assertEqual(installed_binary.read_bytes(), binary)
            self.assertTrue(os.access(installed_binary, os.X_OK))
            self.assertEqual(
                result,
                {
                    "version": "2.1.205",
                    "platform": "linux-x64",
                    "checksum_verified": True,
                    "binary_path": str(installed_binary),
                },
            )
        self.assertEqual(messages[-1], "Claude installation complete")

    def test_install_failure_preserves_existing_binary(self):
        replacement = b"\x7fELFbad-claude"
        manifest = {
            "version": "2.1.205",
            "platforms": {
                "linux-x64": {
                    "binary": "claude",
                    "checksum": "0" * 64,
                    "size": len(replacement),
                },
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "claude-cli"
            old_binary = root / "bin" / "claude"
            old_binary.parent.mkdir(parents=True)
            old_binary.write_bytes(b"old")
            old_binary.chmod(0o755)
            with mock.patch.dict(os.environ, {"EHA_CLAUDE_INSTALL_ROOT": str(root)}, clear=False), \
                 mock.patch.object(claude_setup, "platform_target", return_value="linux-x64"), \
                 mock.patch.object(
                     claude_setup,
                     "_read_url",
                     side_effect=[b"2.1.205", json.dumps(manifest).encode(), replacement],
                 ):
                with self.assertRaisesRegex(RuntimeError, "checksum"):
                    claude_setup.install()
            self.assertEqual(old_binary.read_bytes(), b"old")

    def test_install_replace_failure_restores_existing_binary(self):
        binary = b"\x7fELFmock-claude"
        digest = hashlib.sha256(binary).hexdigest()
        manifest = {
            "version": "2.1.205",
            "platforms": {
                "linux-x64": {"binary": "claude", "checksum": digest, "size": len(binary)},
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "claude-cli"
            old_binary = root / "bin" / "claude"
            old_binary.parent.mkdir(parents=True)
            old_binary.write_bytes(b"old")
            old_binary.chmod(0o755)
            original_replace = os.replace

            def fail_new_install(source, destination):
                if source.endswith("/claude-cli") and destination == str(root):
                    raise OSError("replace failed")
                return original_replace(source, destination)

            with mock.patch.dict(os.environ, {"EHA_CLAUDE_INSTALL_ROOT": str(root)}, clear=False), \
                 mock.patch.object(claude_setup, "platform_target", return_value="linux-x64"), \
                 mock.patch.object(
                     claude_setup,
                     "_read_url",
                     side_effect=[b"2.1.205", json.dumps(manifest).encode(), binary],
                 ), \
                 mock.patch.object(claude_setup.os, "replace", side_effect=fail_new_install):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    claude_setup.install()
            self.assertEqual(old_binary.read_bytes(), b"old")

    def test_uninstall_keeps_credentials_and_rejects_filesystem_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "claude-cli"
            binary = root / "bin" / "claude"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"binary")
            credentials = Path(temp) / ".credentials.json"
            credentials.write_text("token", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"EHA_CLAUDE_INSTALL_ROOT": str(root), "CLAUDE_CONFIG_DIR": temp},
                clear=False,
            ):
                self.assertEqual(claude_setup.uninstall(), {"removed_files": [str(root)]})
                self.assertFalse(root.exists())
                self.assertTrue(credentials.exists())
                self.assertEqual(claude_setup.uninstall(), {"removed_files": []})
            with mock.patch.dict(os.environ, {"EHA_CLAUDE_INSTALL_ROOT": os.path.sep}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "filesystem root"):
                    claude_setup.uninstall()

    def test_uninstall_rejects_unsafe_install_roots_without_removal(self):
        for root in ("/", "//", "/./", "/tmp/..", "", ".", "relative/path"):
            with self.subTest(root=root), \
                 mock.patch.dict(os.environ, {"EHA_CLAUDE_INSTALL_ROOT": root}, clear=False), \
                 mock.patch.object(claude_setup.shutil, "rmtree") as rmtree:
                with self.assertRaises(RuntimeError):
                    claude_setup.uninstall()
                rmtree.assert_not_called()

    def test_is_installed_checks_diy_binary_and_runtime_env_has_no_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "claude-cli"
            binary = root / "bin" / "claude"
            with mock.patch.dict(os.environ, {
                "EHA_CLAUDE_INSTALL_ROOT": str(root),
                "SUPERVISOR_TOKEN": "secret",
                "ANTHROPIC_API_KEY": "secret",
            }, clear=False):
                self.assertFalse(claude_setup.is_installed())
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"binary")
                binary.chmod(0o755)
                self.assertTrue(claude_setup.is_installed())
                env = claude_setup.runtime_env({"DISABLE_UPDATES": "0"})
                self.assertEqual(env["DISABLE_UPDATES"], "1")
                self.assertNotIn("SUPERVISOR_TOKEN", env)
                self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_state_reports_diy_install_locations_and_manifest_checksum_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "claude-cli"
            with mock.patch.dict(os.environ, {"EHA_CLAUDE_INSTALL_ROOT": str(root)}, clear=False):
                state = claude_setup.state()
            self.assertEqual(state["install_root"], str(root))
            self.assertEqual(state["bin_dir"], str(root / "bin"))
            self.assertEqual(state["binary_path"], str(root / "bin" / "claude"))
            self.assertEqual(state["checksum_source"], "Claude release manifest checksum")


class ClaudeSetupEndpointTests(unittest.TestCase):
    def setUp(self):
        self.setup_guard_env = mock.patch.dict(
            os.environ, {"EHA_SETUP_GUARD": "off"}, clear=False
        )
        self.setup_guard_env.start()
        self.harness_flag_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.harness_flag_dir)
        self.harness_flag_env = mock.patch.dict(
            os.environ,
            {"EHA_HARNESS_FLAG_FILE": os.path.join(self.harness_flag_dir, "selected_harness")},
            clear=False,
        )
        self.harness_flag_env.start()
        self.addCleanup(self.harness_flag_env.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()
        self.setup_guard_env.stop()

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

    def test_claude_install_sse_and_uninstall_dispatch(self):
        result = {
            "version": "2.1.205",
            "platform": "linux-x64",
            "checksum_verified": True,
            "binary_path": "/tmp/claude",
        }
        fake = mock.Mock(uninstall=lambda: {"removed_files": ["/tmp/claude-cli"]})

        def install(*, progress):
            progress("Resolving Claude release")
            return result

        fake.install.side_effect = install
        request = urllib.request.Request(
            self.base_url + "/api/setup/claude/install", data=b"{}", method="POST"
        )
        with mock.patch.object(server, "claude_setup", fake):
            with urllib.request.urlopen(request) as response:
                body = "".join(response.readline().decode("utf-8") for _ in range(6))
            self.assertIn("event: line", body)
            self.assertIn("Resolving Claude release", body)
            self.assertIn('event: done\ndata: {"version": "2.1.205"', body)
            for _ in range(100):
                if not server._CLAUDE_MUTATION_LOCK.locked():
                    break
                time.sleep(0.01)
            self.assertFalse(server._CLAUDE_MUTATION_LOCK.locked())
            # §13.2: claude uninstall is refused while claude is the effective harness;
            # select codex so this test still exercises the dispatch path
            # ([[embodied-ha-step4-uninstall-guard-test-contract-change]]).
            Path(self.harness_flag_dir, "selected_harness").write_text("codex\n", encoding="utf-8")
            self.assertEqual(
                self._post_json("/api/setup/claude/uninstall"),
                {"ok": True, "removed_files": ["/tmp/claude-cli"]},
            )


if __name__ == "__main__":
    unittest.main()
