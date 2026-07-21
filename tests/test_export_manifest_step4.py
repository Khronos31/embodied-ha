"""Step4 増分3: secure export bundle のテスト。

security 系(認証除外・symlink拒否・サイズ上限)を最重点に、カテゴリ選択・namespace・
manifest・endpoint(guard/stream)を検証する。
"""
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))
sys.path.insert(0, str(EHA_DIR / "web"))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import export_manifest  # noqa: E402
import server  # noqa: E402


class _Env:
    """Temp EHA_DATA_DIR (+ /data) with helpers to seed files."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.eha = base / "eha"
        self.addon = base / "addon"
        (self.eha / "log" / "memory" / "episodes").mkdir(parents=True)
        (self.eha / "log" / "memory" / "daybooks").mkdir(parents=True)
        self.addon.mkdir(parents=True)
        self._patch = mock.patch.dict(os.environ, {
            "EHA_DATA_DIR": str(self.eha),
            "EHA_ADDON_DATA_DIR": str(self.addon),
        }, clear=False)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._tmp.cleanup()

    def write(self, rel, content="x", root="eha"):
        target = (self.eha if root == "eha" else self.addon) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target


def _extract(bundle_bytes):
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        for m in tar.getmembers():
            entries[m.name] = tar.extractfile(m).read()
    manifest = json.loads(entries["manifest.json"])
    return manifest, entries


def _build(categories):
    buf = io.BytesIO()
    manifest = export_manifest.build_bundle(categories, buf)
    return manifest, buf.getvalue()


def _build_and_extract(categories):
    manifest, bundle = _build(categories)
    _, entries = _extract(bundle)
    return manifest, entries


class NormalizeTests(unittest.TestCase):
    def test_default_is_core(self):
        self.assertEqual(export_manifest.normalize_categories(None), ["identity", "memory"])

    def test_unknown_category_rejected(self):
        with self.assertRaises(export_manifest.ExportError):
            export_manifest.normalize_categories(["identity", "nope"])

    def test_dedup(self):
        self.assertEqual(export_manifest.normalize_categories(["memory", "memory"]), ["memory"])


class BundleContentTests(unittest.TestCase):
    def test_core_includes_identity_and_memory(self):
        with _Env() as env:
            env.write("character.md", "akane character")
            env.write("desires.json", "{}")
            env.write("log/memory.md", "long-term memory")
            env.write("log/memory/episodes/ep1.json", '{"id":"ep1"}')
            manifest, entries = _build_and_extract(None)
        paths = {e["path"] for e in manifest["entries"]}
        self.assertIn("eha/character.md", paths)
        self.assertIn("eha/log/memory.md", paths)
        self.assertIn("eha/log/memory/episodes/ep1.json", paths)
        self.assertIn(b"akane character", entries["data/eha/character.md"])
        # manifest entries carry size + sha256
        import hashlib
        entry = next(e for e in manifest["entries"] if e["path"] == "eha/character.md")
        self.assertEqual(entry["sha256"], hashlib.sha256(b"akane character").hexdigest())
        self.assertEqual(entry["category"], "identity")

    def test_only_requested_categories(self):
        with _Env() as env:
            env.write("character.md")
            env.write("preferences.json", "{}")
            env.write("floorplan_room_graph_draft.json", "{}")
            manifest, _ = _build(["identity"])
        paths = {e["path"] for e in manifest["entries"]}
        self.assertIn("eha/character.md", paths)
        self.assertNotIn("eha/preferences.json", paths)
        self.assertNotIn("eha/floorplan_room_graph_draft.json", paths)

    def test_agent_prefs_addon_namespace(self):
        with _Env() as env:
            env.write("agent_prefs.json", "{}", root="addon")
            manifest, _ = _build(["agent_prefs"])
        self.assertEqual(manifest["entries"][0]["path"], "addon/agent_prefs.json")

    def test_missing_files_are_skipped(self):
        with _Env():  # nothing written
            manifest, _ = _build(None)
        self.assertEqual(manifest["entries"], [])

    def test_manifest_version_is_int_one(self):
        with _Env() as env:
            env.write("character.md")
            manifest, _ = _build(["identity"])
        self.assertIs(type(manifest["version"]), int)
        self.assertEqual(manifest["version"], 1)


class SecurityTests(unittest.TestCase):
    def test_auth_files_never_exported_even_when_present(self):
        # grandfather .claude(認証+transcript)を EHA_DATA_DIR 内に置いても、どのカテゴリにも
        # 入らない(allowlist 列挙なので構造的に到達不能)。
        with _Env() as env:
            env.write("character.md")
            env.write(".claude/.credentials.json", '{"token":"secret"}')
            env.write(".claude/.claude.json", '{"userId":"x"}')
            env.write("log/memory.md", "mem")
            manifest, entries = _build_and_extract(list(export_manifest.VALID_CATEGORIES))
        for e in manifest["entries"]:
            self.assertNotIn(".claude", e["path"])
            self.assertNotIn("credentials", e["path"])
        for name in entries:
            self.assertNotIn(".credentials.json", name)
            self.assertNotIn(".claude.json", name)

    def test_symlinked_file_is_not_followed(self):
        # character.md を秘密ファイルへの symlink にしても中身は export されない(sol H2)。
        with _Env() as env:
            secret = env.write("secret_outside.txt", "TOP SECRET")
            link = env.eha / "character.md"
            os.symlink(secret, link)
            env.write("home_policy.md", "policy")  # 正常ファイルは入る
            manifest, entries = _build_and_extract(["identity"])
        paths = {e["path"] for e in manifest["entries"]}
        self.assertNotIn("eha/character.md", paths)  # symlink は拒否
        self.assertIn("eha/home_policy.md", paths)
        for data in entries.values():
            self.assertNotIn(b"TOP SECRET", data)

    def test_symlinked_dir_entry_is_not_followed(self):
        # memory/episodes/ 内の symlink エントリも拾わない。
        with _Env() as env:
            secret = env.write("secret.json", '{"s":"x"}')
            os.symlink(secret, env.eha / "log" / "memory" / "episodes" / "evil.json")
            env.write("log/memory/episodes/ok.json", '{"id":"ok"}')
            manifest, _ = _build(["memory"])
        paths = {e["path"] for e in manifest["entries"]}
        self.assertIn("eha/log/memory/episodes/ok.json", paths)
        self.assertNotIn("eha/log/memory/episodes/evil.json", paths)

    def test_intermediate_dir_symlink_not_followed(self):
        # sol H1 の再現: log/memory/episodes を .claude(認証)への symlink dir にしても
        # 認証ファイルは入らない(中間dir symlink を辿らない)。
        with _Env() as env:
            env.write(".claude/.credentials.json", '{"token":"SECRET"}')
            # 既定の episodes ディレクトリを消し、.claude への symlink に差し替える。
            import shutil
            shutil.rmtree(env.eha / "log" / "memory" / "episodes")
            os.symlink(env.eha / ".claude", env.eha / "log" / "memory" / "episodes")
            env.write("log/memory.md", "mem")
            manifest, entries = _build_and_extract(["memory"])
        for e in manifest["entries"]:
            self.assertNotIn("credentials", e["path"])
            self.assertNotIn("episodes", e["path"])  # symlink dir 経由の中身は出ない
        for data in entries.values():
            self.assertNotIn(b"SECRET", data)

    def test_hardlink_to_external_secret_not_exported(self):
        # sol H2 の再現: allowlist 名を root 外の秘密への hardlink にしても export されない
        # (st_nlink != 1 で拒否)。
        with _Env() as env:
            outside = Path(env._tmp.name) / "outside_secret.txt"
            outside.write_text("HARDLINK SECRET", encoding="utf-8")
            try:
                os.link(outside, env.eha / "character.md")  # hardlink
            except OSError:
                self.skipTest("hardlink not supported on this filesystem")
            env.write("home_policy.md", "policy")
            manifest, entries = _build_and_extract(["identity"])
        paths = {e["path"] for e in manifest["entries"]}
        self.assertNotIn("eha/character.md", paths)
        self.assertIn("eha/home_policy.md", paths)
        for data in entries.values():
            self.assertNotIn(b"HARDLINK SECRET", data)

    def test_fifo_named_file_is_skipped_without_hanging(self):
        # sol Med の再現: allowlist 名が FIFO でも open で hang せず(O_NONBLOCK)、
        # 非regular として skip。
        with _Env() as env:
            try:
                os.mkfifo(env.eha / "character.md")
            except (OSError, AttributeError):
                self.skipTest("mkfifo not supported")
            env.write("home_policy.md", "policy")
            manifest, _ = _build(["identity"])  # must return, not hang
        paths = {e["path"] for e in manifest["entries"]}
        self.assertNotIn("eha/character.md", paths)
        self.assertIn("eha/home_policy.md", paths)

    def test_secure_read_discards_when_inode_changes_during_read(self):
        # sol refix High(hardlink/unlink TOCTOU): 読み取り後に name→inode の結び付きが
        # 変わっていたら bytes を破棄する。after-check の os.stat を差し替えて競合を模擬。
        from types import SimpleNamespace
        with _Env() as env:
            env.write("character.md", "content")
            parent_fd = os.open(str(env.eha), os.O_RDONLY | os.O_DIRECTORY)
            real_stat = os.stat
            calls = {"n": 0}

            def fake_stat(*a, **k):
                st = real_stat(*a, **k)
                calls["n"] += 1
                if calls["n"] >= 2:  # after-read stat → 別 inode を装う
                    return SimpleNamespace(
                        st_mode=st.st_mode, st_nlink=1, st_dev=st.st_dev, st_ino=st.st_ino + 1)
                return st

            try:
                with mock.patch("os.stat", side_effect=fake_stat):
                    data = export_manifest._secure_read(parent_fd, "character.md", "character.md")
                self.assertIsNone(data)
            finally:
                os.close(parent_fd)

    def test_oversized_file_raises(self):
        with _Env() as env:
            big = env.eha / "character.md"
            big.write_bytes(b"a" * 16)
            with mock.patch.object(export_manifest, "MAX_FILE_BYTES", 8):
                with self.assertRaises(export_manifest.ExportError):
                    _build(["identity"])

    def test_total_size_cap_raises(self):
        with _Env() as env:
            env.write("character.md", "a" * 100)
            env.write("home_policy.md", "b" * 100)
            with mock.patch.object(export_manifest, "MAX_TOTAL_BYTES", 150):
                with self.assertRaises(export_manifest.ExportError):
                    _build(["identity"])


class ExportEndpointTests(unittest.TestCase):
    def setUp(self):
        self._env = _Env().__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        self._env.write("character.md", "akane")
        self._env.write("log/memory.md", "mem")
        self.enterContext(mock.patch.dict(os.environ, {"EHA_SETUP_GUARD": "off"}, clear=False))

    def _handler(self, path, body_bytes=b"", client=("127.0.0.1", 0)):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.client_address = client
        handler.headers = {"Content-Length": str(len(body_bytes))}
        handler.rfile = io.BytesIO(body_bytes)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_json = mock.Mock()
        return handler

    def test_export_streams_targz_with_manifest(self):
        handler = self._handler("/api/setup/export", json.dumps({"categories": ["identity"]}).encode())
        handler.do_POST()
        handler.send_response.assert_called_with(200)
        headers = {c.args[0]: c.args[1] for c in handler.send_header.call_args_list}
        self.assertEqual(headers.get("Content-Type"), "application/gzip")
        self.assertEqual(headers.get("Cache-Control"), "no-store, private")
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        manifest, _ = _extract(handler.wfile.getvalue())
        self.assertIn("eha/character.md", {e["path"] for e in manifest["entries"]})

    def test_export_unknown_category_is_400(self):
        handler = self._handler("/api/setup/export", json.dumps({"categories": ["nope"]}).encode())
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[1], 400)


class ExportGuardTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("EHA_SETUP_GUARD", None)

    def test_export_post_guarded_from_loopback(self):
        handler = object.__new__(server.Handler)
        handler.path = "/api/setup/export"
        handler.client_address = ("127.0.0.1", 0)  # 非ingress
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.send_json = mock.Mock()
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[1], 403)


if __name__ == "__main__":
    unittest.main()
