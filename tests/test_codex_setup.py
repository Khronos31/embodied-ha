import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
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


class CodexSetupTests(unittest.TestCase):
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
    def test_install_post_dispatches_sse(self):
        fake = SimpleNamespace(
            install=lambda progress: (progress("downloaded") or {"checksum_verified": True}),
            state=lambda: {"installed": False, "authenticated": False},
            uninstall=lambda: {"removed_files": []},
            clear_auth=lambda: {"removed_files": []},
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        with mock.patch.object(server, "codex_setup", fake):
            thread.start()
            try:
                url = f"http://127.0.0.1:{httpd.server_address[1]}/api/setup/codex/install"
                request = urllib.request.Request(url, data=b"", method="POST")
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = "".join(response.readline().decode("utf-8") for _ in range(6))
                self.assertIn("event: line", body)
                self.assertIn("event: done", body)
                self.assertIn("checksum_verified", body)
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
