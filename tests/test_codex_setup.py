import hashlib
import io
import json
import os
import sys
import tarfile
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
sys.path.insert(0, str(ROOT / "embodied_ha" / "web"))
os.environ.setdefault("HA_URL", "http://example.invalid")

import codex_setup  # noqa: E402
import server  # noqa: E402


def archive_bytes(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith("/bin/codex") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def linked_archive_bytes(kind: bytes) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type = kind
        info.linkname = "target"
        tar.addfile(info)
    return out.getvalue()


class FakeUrlResponse:
    def __init__(self, data: bytes, content_length: str | None = None):
        self.data = data
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        return self.data


class CodexSetupTests(unittest.TestCase):
    def test_device_auth_values_extract_only_strict_url_and_code(self):
        url = "\x1b[1;36mOpen https://auth.openai.com/codex/device?flow=1\x1b[0m diagnostic"
        code = "\x1b[1mYour code is ABCD-12345\x1b[0m extra diagnostic"
        self.assertEqual(
            codex_setup.device_auth_values(url),
            ["https://auth.openai.com/codex/device?flow=1"],
        )
        self.assertEqual(codex_setup.device_auth_values(code), ["ABCD-12345"])
        self.assertEqual(codex_setup.device_auth_values("Waiting for approval..."), [])
        self.assertEqual(codex_setup.device_auth_values("https://auth.openai.com/codex/deviceevil"), [])

    def test_platform_assets_and_release_resolution(self):
        self.assertEqual(codex_setup.platform_target("x86_64"), "x86_64-unknown-linux-musl")
        self.assertEqual(codex_setup.platform_target("aarch64"), "aarch64-unknown-linux-musl")
        self.assertEqual(
            codex_setup.package_asset_name("x86_64-unknown-linux-musl"),
            "codex-package-x86_64-unknown-linux-musl.tar.gz",
        )
        metadata = {"tag_name": "rust-v1.2.3", "assets": []}
        with mock.patch.object(codex_setup, "_read_url", return_value=json.dumps(metadata).encode()):
            self.assertEqual(codex_setup.resolve_release()["version"], "1.2.3")

    def test_checksum_success_and_mismatch(self):
        data = b"archive"
        digest = hashlib.sha256(data).hexdigest()
        manifest = f"{digest}  codex-package-x.tar.gz\n".encode()
        self.assertEqual(codex_setup.expected_sha256(manifest, "codex-package-x.tar.gz"), digest)
        codex_setup.verify_sha256(data, digest)
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            codex_setup.verify_sha256(data, "0" * 64)

    def test_tar_path_traversal_is_rejected(self):
        malicious = archive_bytes({"../outside": b"no"})
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "Unsafe path"):
                codex_setup._safe_extract(malicious, temp)
            self.assertFalse((Path(temp).parent / "outside").exists())

    def test_tar_limits_reject_member_and_total_size(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(codex_setup, "MAX_ARCHIVE_MEMBER_BYTES", 4):
            with self.assertRaisesRegex(RuntimeError, "member exceeds"):
                codex_setup._safe_extract(archive_bytes({"large": b"12345"}), temp)
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(codex_setup, "MAX_ARCHIVE_TOTAL_BYTES", 5):
            with self.assertRaisesRegex(RuntimeError, "total extracted"):
                codex_setup._safe_extract(archive_bytes({"one": b"123", "two": b"456"}), temp)

    def test_tar_member_count_and_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(codex_setup, "MAX_ARCHIVE_MEMBERS", 1):
            with self.assertRaisesRegex(RuntimeError, "member count"):
                codex_setup._safe_extract(archive_bytes({"one": b"", "two": b""}), temp)
        for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(RuntimeError, "Unsafe path"):
                    codex_setup._safe_extract(linked_archive_bytes(link_type), temp)

    def test_safe_extract_works_without_pep706_filter_and_strips_setuid(self):
        # 本番コンテナ(bookworm python3.11.2)はextractallにfilter引数が無い。
        # hasattr(tarfile, "data_filter")フォールバックの動作と、
        # フォールバック経路でもsetuid/setgidが剥がれることを固定する。
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            info = tarfile.TarInfo("bin")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
            info = tarfile.TarInfo("bin/codex")
            payload = b"#!/bin/sh\n"
            info.size = len(payload)
            info.mode = 0o4755  # setuid付き
            tar.addfile(info, io.BytesIO(payload))
        archive = out.getvalue()
        for simulate_old_python in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                if simulate_old_python:
                    original = tarfile.data_filter
                    del tarfile.data_filter
                    try:
                        codex_setup._safe_extract(archive, temp)
                    finally:
                        tarfile.data_filter = original
                else:
                    codex_setup._safe_extract(archive, temp)
                mode = os.stat(Path(temp) / "bin" / "codex").st_mode & 0o7777
                self.assertEqual(mode, 0o755, f"simulate_old_python={simulate_old_python}")

    def test_read_url_enforces_advertised_and_actual_sizes(self):
        with mock.patch.object(codex_setup, "MAX_DOWNLOAD_BYTES", 4), \
             mock.patch.object(codex_setup, "urlopen", return_value=FakeUrlResponse(b"ok", "5")):
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                codex_setup._read_url("https://example.invalid")
        with mock.patch.object(codex_setup, "MAX_DOWNLOAD_BYTES", 4), \
             mock.patch.object(codex_setup, "urlopen", return_value=FakeUrlResponse(b"12345")):
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                codex_setup._read_url("https://example.invalid")

    def test_replace_install_root_preserves_backup_when_restore_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex-cli"
            root.mkdir()
            staged = Path(temp) / "staged"
            staged.mkdir()
            original_replace = os.replace

            def fail_new_install(source, destination):
                if source == str(staged) or destination == str(root):
                    raise OSError("replace failed")
                return original_replace(source, destination)

            with mock.patch.object(codex_setup.os, "replace", side_effect=fail_new_install):
                with self.assertRaisesRegex(RuntimeError, "backup remains at") as raised:
                    codex_setup._replace_install_root(str(staged), str(root))
            backup = Path(str(raised.exception).split("backup remains at ", 1)[1].split(": ", 1)[0])
            self.assertTrue(backup.is_dir())
            self.assertFalse(root.exists())

    def test_install_is_atomic_when_staging_fails(self):
        target = "x86_64-unknown-linux-musl"
        name = codex_setup.package_asset_name(target)
        bad_archive = archive_bytes({"unexpected/file": b"bad"})
        digest = hashlib.sha256(bad_archive).hexdigest()
        release = {
            "version": "1.2.3",
            "assets": [
                {"name": name, "browser_download_url": "https://example.invalid/archive"},
                {"name": codex_setup.checksum_asset_name(), "browser_download_url": "https://example.invalid/sums"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex-cli"
            old = root / "bin" / "codex"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            old.chmod(0o755)
            with mock.patch.dict(os.environ, {"EHA_CODEX_INSTALL_ROOT": str(root)}, clear=False), \
                 mock.patch.object(codex_setup, "resolve_release", return_value=release), \
                 mock.patch.object(codex_setup, "platform_target", return_value=target), \
                 mock.patch.object(codex_setup, "_read_url", side_effect=[f"{digest}  {name}\n".encode(), bad_archive]):
                with self.assertRaisesRegex(RuntimeError, "release directory"):
                    codex_setup.install()
            self.assertEqual(old.read_bytes(), b"old")

    def test_install_succeeds_with_real_package_layout(self):
        # 実配布(rust-v0.144.5実物確認)はアーカイブ直下がbin/codex。
        target = "x86_64-unknown-linux-musl"
        name = codex_setup.package_asset_name(target)
        archive = archive_bytes({
            "bin/codex": b"#!/bin/sh\nexit 0\n",
            "codex-package.json": b"{}",
        })
        digest = hashlib.sha256(archive).hexdigest()
        release = {
            "version": "1.2.3",
            "assets": [
                {"name": name, "browser_download_url": "https://example.invalid/archive"},
                {"name": codex_setup.checksum_asset_name(), "browser_download_url": "https://example.invalid/sums"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex-cli"
            with mock.patch.dict(os.environ, {"EHA_CODEX_INSTALL_ROOT": str(root)}, clear=False), \
                 mock.patch.object(codex_setup, "resolve_release", return_value=release), \
                 mock.patch.object(codex_setup, "platform_target", return_value=target), \
                 mock.patch.object(codex_setup, "_read_url", side_effect=[f"{digest}  {name}\n".encode(), archive]):
                result = codex_setup.install()
            self.assertTrue(result["checksum_verified"])
            self.assertEqual(result["version"], "1.2.3")
            binary = root / "bin" / "codex"
            self.assertTrue(binary.is_file())
            self.assertTrue(os.access(binary, os.X_OK))
            self.assertTrue((root / "codex-package.json").is_file())

    def test_state_cleanup_and_secret_free_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "cli"
            home = Path(temp) / "home"
            binary = root / "bin" / "codex"
            binary.parent.mkdir(parents=True)
            binary.write_text("#! /bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            auth = home / "auth.json"
            auth.parent.mkdir()
            auth.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "EHA_CODEX_INSTALL_ROOT": str(root), "EHA_CODEX_HOME": str(home),
                "SUPERVISOR_TOKEN": "secret", "ANTHROPIC_API_KEY": "secret",
            }, clear=False):
                state = codex_setup.state()
                self.assertTrue(state["installed"])
                self.assertTrue(state["authenticated"])
                env = codex_setup.subprocess_env()
                self.assertNotIn("SUPERVISOR_TOKEN", env)
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertTrue(codex_setup.clear_auth()["removed_files"])
                self.assertEqual(codex_setup.clear_auth()["removed_files"], [])
                auth.write_text("{}", encoding="utf-8")
                self.assertTrue(codex_setup.uninstall()["removed_files"])
                self.assertEqual(codex_setup.uninstall()["removed_files"], [])


class CodexSetupEndpointTests(unittest.TestCase):
    def setUp(self):
        self.setup_guard_env = mock.patch.dict(
            os.environ, {"EHA_SETUP_GUARD": "off"}, clear=False
        )
        self.setup_guard_env.start()

    def tearDown(self):
        self.setup_guard_env.stop()

    def _with_server(self, fake, assertion, expect_lock_released=True):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        with mock.patch.object(server, "codex_setup", fake):
            thread.start()
            try:
                assertion(f"http://127.0.0.1:{httpd.server_address[1]}")
                if expect_lock_released:
                    for _ in range(50):
                        if not server._CODEX_INSTALL_LOCK.locked():
                            break
                        time.sleep(0.01)
                    self.assertFalse(server._CODEX_INSTALL_LOCK.locked())
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()

    @staticmethod
    def _post(url):
        request = urllib.request.Request(url, data=b"", method="POST")
        return urllib.request.urlopen(request, timeout=5)

    @staticmethod
    def _sse_body(response):
        lines = []
        while True:
            line = response.readline().decode("utf-8")
            if not line:
                break
            lines.append(line)
            if line.startswith("event: done") or line.startswith("event: error"):
                lines.append(response.readline().decode("utf-8"))
                break
        return "".join(lines)

    @staticmethod
    def _sse_until(response, predicate):
        lines = []
        while True:
            line = response.readline().decode("utf-8")
            if not line:
                raise AssertionError("SSE stream ended before the expected event")
            lines.append(line)
            body = "".join(lines)
            if predicate(body):
                return body

    @staticmethod
    def _wait_for_path(path):
        for _ in range(100):
            if path.exists():
                return
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {path}")

    @staticmethod
    def _fake_codex(path, program):
        path.write_text("#!/bin/sh\n" + program, encoding="utf-8")
        path.chmod(0o755)

    def test_codex_login_success_streams_device_details_and_done(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = Path(temp) / "cli", Path(temp) / "home"
            binary = root / "bin" / "codex"
            binary.parent.mkdir(parents=True)
            self._fake_codex(binary, "printf '\\033[36mOpen https://auth.openai.com/codex/device?flow=1 diagnostic-only\\033]0;private-title\\007\\033[0m\\n'\nprintf '\\033[1mEnter code ABCD-12345 internal-detail\\033[0m\\n'\nwhile [ ! -e \"$CODEX_HOME/approve\" ]; do sleep 0.01; done\ntouch \"$CODEX_HOME/auth.json\"\nwhile :; do sleep 1; done\n")
            with mock.patch.dict(os.environ, {
                "EHA_CODEX_INSTALL_ROOT": str(root), "EHA_CODEX_HOME": str(home),
            }, clear=False):
                def assertion(base_url):
                    with self._post(f"{base_url}/api/setup/codex/login") as response:
                        body = self._sse_until(
                            response,
                            lambda value: "https://auth.openai.com/codex/device?flow=1" in value
                            and "ABCD-12345" in value,
                        )
                        (home / "approve").touch()
                        body += self._sse_body(response)
                    self.assertIn("event: line", body)
                    self.assertIn("https://auth.openai.com/codex/device?flow=1", body)
                    self.assertIn("ABCD-12345", body)
                    self.assertNotIn("diagnostic-only", body)
                    self.assertNotIn("internal-detail", body)
                    self.assertNotIn("private-title", body)
                    self.assertIn('event: done\ndata: {"authenticated": true}', body)
                self._with_server(codex_setup, assertion)

    def test_codex_login_reader_error_streams_error_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = Path(temp) / "cli", Path(temp) / "home"
            binary = root / "bin" / "codex"
            terminated = Path(temp) / "terminated"
            binary.parent.mkdir(parents=True)
            self._fake_codex(binary, f"trap 'touch {terminated}; exit 0' TERM\necho line\nwhile :; do sleep 1; done\n")
            with mock.patch.dict(os.environ, {
                "EHA_CODEX_INSTALL_ROOT": str(root), "EHA_CODEX_HOME": str(home),
            }, clear=False), mock.patch.object(
                codex_setup, "device_auth_values", side_effect=ValueError("decode broke")
            ):
                def assertion(base_url):
                    with self._post(f"{base_url}/api/setup/codex/login") as response:
                        body = self._sse_body(response)
                    self.assertIn("event: error", body)
                    self.assertIn("output reader failed: decode broke", body)
                self._with_server(codex_setup, assertion)
            self._wait_for_path(terminated)

    def test_codex_login_full_queue_disconnect_stops_reader(self):
        class DisconnectingWriter:
            def write(self, _data):
                # Let the child fill the bounded queue before simulating a
                # client disconnect while the main loop is writing an SSE line.
                time.sleep(0.1)
                raise BrokenPipeError("client disconnected")

            def flush(self):
                pass

        with tempfile.TemporaryDirectory() as temp:
            root, home = Path(temp) / "cli", Path(temp) / "home"
            binary = root / "bin" / "codex"
            terminated = Path(temp) / "terminated"
            binary.parent.mkdir(parents=True)
            self._fake_codex(binary, f"trap 'touch {terminated}; exit 0' TERM\ni=0\nwhile [ $i -lt 500 ]; do printf 'ABCD-%05d\\n' $i; i=$((i + 1)); done\nwhile :; do sleep 1; done\n")
            handler = object.__new__(server.Handler)
            handler.send_response = lambda *_args: None
            handler.send_header = lambda *_args: None
            handler.end_headers = lambda: None
            handler.wfile = DisconnectingWriter()
            with mock.patch.dict(os.environ, {
                "EHA_CODEX_INSTALL_ROOT": str(root), "EHA_CODEX_HOME": str(home),
            }, clear=False):
                handler._serve_setup_codex_login()
            self.assertFalse(server._CODEX_INSTALL_LOCK.locked())
            self._wait_for_path(terminated)
            self.assertFalse(any(thread.name == "codex-login-reader" and thread.is_alive()
                                 for thread in threading.enumerate()))

    def test_codex_login_done_when_process_exits_after_writing_auth(self):
        # 実挙動(実ブラウザ承認E2E 2026-07-18): 承認成功時、codexはauth.jsonを
        # 書いた直後にstatus 0で自ら終了する。死亡判定が認証完了チェックより
        # 先に成立してもdoneを配信すること(誤errorの回帰防止)。
        with tempfile.TemporaryDirectory() as temp:
            root, home = Path(temp) / "cli", Path(temp) / "home"
            binary = root / "bin" / "codex"
            binary.parent.mkdir(parents=True)
            self._fake_codex(binary, "echo 'Open https://auth.openai.com/codex/device'\necho 'Enter code ABCD-12345'\ntouch \"$CODEX_HOME/auth.json\"\nexit 0\n")
            with mock.patch.dict(os.environ, {
                "EHA_CODEX_INSTALL_ROOT": str(root), "EHA_CODEX_HOME": str(home),
            }, clear=False):
                def assertion(base_url):
                    with self._post(f"{base_url}/api/setup/codex/login") as response:
                        body = self._sse_body(response)
                    self.assertIn('event: done\ndata: {"authenticated": true}', body)
                    self.assertNotIn("event: error", body)
                self._with_server(codex_setup, assertion)

    def test_codex_login_errors_when_not_installed(self):
        fake = SimpleNamespace(is_installed=lambda: False)

        def assertion(base_url):
            with self._post(f"{base_url}/api/setup/codex/login") as response:
                body = self._sse_body(response)
            self.assertIn("event: error", body)
            self.assertIn("not installed", body)

        self._with_server(fake, assertion)

    def test_codex_login_reports_busy_mutation_lock(self):
        def assertion(base_url):
            with self._post(f"{base_url}/api/setup/codex/login") as response:
                body = self._sse_body(response)
            self.assertIn("event: error", body)
            self.assertIn("Codex install is running", body)

        self.assertTrue(server._acquire_codex_mutation("install"))
        try:
            self._with_server(SimpleNamespace(), assertion, expect_lock_released=False)
        finally:
            server._release_codex_mutation()

    def test_codex_login_timeout_and_exit_release_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = Path(temp) / "cli", Path(temp) / "home"
            binary = root / "bin" / "codex"
            binary.parent.mkdir(parents=True)
            self._fake_codex(binary, "while :; do sleep 1; done\n")
            with mock.patch.dict(os.environ, {
                "EHA_CODEX_INSTALL_ROOT": str(root), "EHA_CODEX_HOME": str(home),
            }, clear=False), mock.patch.object(server, "_CODEX_LOGIN_TIMEOUT", 0.05), \
                 mock.patch.object(server, "_CODEX_LOGIN_POLL_INTERVAL", 0.01):
                def timeout_assertion(base_url):
                    with self._post(f"{base_url}/api/setup/codex/login") as response:
                        body = self._sse_body(response)
                    self.assertIn("event: error", body)
                    self.assertIn("timed out", body)
                self._with_server(codex_setup, timeout_assertion)

            self._fake_codex(binary, "exit 7\n")
            with mock.patch.dict(os.environ, {
                "EHA_CODEX_INSTALL_ROOT": str(root), "EHA_CODEX_HOME": str(home),
            }, clear=False), mock.patch.object(server, "_CODEX_LOGIN_POLL_INTERVAL", 0.01):
                def exit_assertion(base_url):
                    with self._post(f"{base_url}/api/setup/codex/login") as response:
                        body = self._sse_body(response)
                    self.assertIn("event: error", body)
                    self.assertIn("exited before authentication", body)
                self._with_server(codex_setup, exit_assertion)

    def test_codex_login_child_environment_has_no_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = Path(temp) / "cli", Path(temp) / "home"
            binary = root / "bin" / "codex"
            binary.parent.mkdir(parents=True)
            self._fake_codex(binary, "if [ -n \"$SUPERVISOR_TOKEN$ANTHROPIC_API_KEY\" ]; then\n  echo secret-leaked\n  exit 9\nfi\necho 'Visit https://auth.openai.com/codex/device with ABCD-12345'\ntouch \"$CODEX_HOME/auth.json\"\nwhile :; do sleep 1; done\n")
            with mock.patch.dict(os.environ, {
                "EHA_CODEX_INSTALL_ROOT": str(root), "EHA_CODEX_HOME": str(home),
                "SUPERVISOR_TOKEN": "secret", "ANTHROPIC_API_KEY": "secret",
            }, clear=False):
                def assertion(base_url):
                    with self._post(f"{base_url}/api/setup/codex/login") as response:
                        body = self._sse_body(response)
                    self.assertIn('event: done\ndata: {"authenticated": true}', body)
                    self.assertNotIn("secret-leaked", body)
                self._with_server(codex_setup, assertion)

    def test_install_post_dispatches_sse(self):
        fake = SimpleNamespace(
            install=lambda progress: (progress("downloaded") or {"checksum_verified": True}),
            state=lambda: {"installed": False, "authenticated": False},
            uninstall=lambda: {"removed_files": []},
            clear_auth=lambda: {"removed_files": []},
        )
        def assertion(base_url):
            with self._post(f"{base_url}/api/setup/codex/install") as response:
                body = "".join(response.readline().decode("utf-8") for _ in range(6))
            self.assertIn("event: line", body)
            self.assertIn("event: done", body)
            self.assertIn("checksum_verified", body)

        self._with_server(fake, assertion)

    def test_uninstall_and_clear_auth_dispatch(self):
        fake = SimpleNamespace(
            uninstall=lambda: {"removed_files": ["install"]},
            clear_auth=lambda: {"removed_files": ["auth"]},
        )

        def assertion(base_url):
            with self._post(f"{base_url}/api/setup/codex/uninstall") as response:
                self.assertEqual(json.loads(response.read()), {"ok": True, "removed_files": ["install"]})
            with self._post(f"{base_url}/api/setup/codex/clear-auth") as response:
                self.assertEqual(json.loads(response.read()), {"ok": True, "removed_files": ["auth"]})

        self._with_server(fake, assertion)

    def test_mutations_return_409_while_install_is_running(self):
        fake = SimpleNamespace(uninstall=lambda: {}, clear_auth=lambda: {})

        def assertion(base_url):
            for endpoint in ("uninstall", "clear-auth"):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self._post(f"{base_url}/api/setup/codex/{endpoint}")
                self.assertEqual(raised.exception.code, 409)
                self.assertEqual(json.loads(raised.exception.read()), {"error": "Codex install is running"})

        self.assertTrue(server._acquire_codex_mutation("install"))
        try:
            self._with_server(fake, assertion, expect_lock_released=False)
        finally:
            server._release_codex_mutation()

    def test_second_install_returns_sse_error(self):
        fake = SimpleNamespace()

        def assertion(base_url):
            with self._post(f"{base_url}/api/setup/codex/install") as response:
                body = "".join(response.readline().decode("utf-8") for _ in range(2))
            self.assertIn("event: error", body)
            self.assertIn("Codex install is running", body)

        self.assertTrue(server._acquire_codex_mutation("install"))
        try:
            self._with_server(fake, assertion, expect_lock_released=False)
        finally:
            server._release_codex_mutation()

    def test_install_failure_is_sent_as_sse_error(self):
        fake = SimpleNamespace(install=mock.Mock(side_effect=RuntimeError("download failed")))

        def assertion(base_url):
            with self._post(f"{base_url}/api/setup/codex/install") as response:
                body = "".join(response.readline().decode("utf-8") for _ in range(2))
            self.assertIn("event: error", body)
            self.assertIn("download failed", body)

        self._with_server(fake, assertion)

    def test_install_releases_lock_when_sse_headers_fail(self):
        handler = object.__new__(server.Handler)
        with mock.patch.object(handler, "send_response", side_effect=BrokenPipeError):
            handler._serve_setup_codex_install()
        self.assertTrue(server._CODEX_INSTALL_LOCK.acquire(blocking=False))
        server._CODEX_INSTALL_LOCK.release()


if __name__ == "__main__":
    unittest.main()
