"""Step4 増分3-redo: §14.7 2経路 export のテスト。

経路1(メモリ) = fail-closed 解決・md丸ごと・黙ったトリム禁止(値スキャン非適用)。
経路2(/data生ダンプ) = 3 home allowlist・認証name-net・値ベースcontent scan・loud-skip。
共通 = secure walk(symlink/hardlink/TOCTOU)・サイズcap・endpoint guard/認可。

旧v1(カテゴリallowlist)のテスト契約は §14.7 の作り直し決定で supersede
([[embodied-ha-agent-setup-step4-spec]] §13.4/§14.7、記録メモ参照)。
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

SUP_TOKEN = "sup-token-abcdef123456"
API_KEY = "sk-ant-test-abcdef123456"
CLAUDE_OAUTH = "claude-oauth-secret-value-9999"
CODEX_TOKEN = "codex-access-token-value-8888"
AGY_TOKEN = "agy-oauth-token-value-7777"


class _Env:
    """Isolated homes (claude/codex/gemini) + options.json + flag file."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.claude = base / "claude-home"
        self.codex = base / "codex-home"
        self.agyhome = base / "agy-home"
        self.gemini = self.agyhome / ".gemini"
        self.data_dir = base / "eha-data"
        self.options = base / "options.json"
        self.flag = base / "selected_harness"
        for d in (self.claude, self.codex, self.gemini, self.data_dir):
            d.mkdir(parents=True)
        self.options.write_text(json.dumps({"claude_api_key": API_KEY}), encoding="utf-8")
        self._patch = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude),
            "EHA_CODEX_HOME": str(self.codex),
            "EHA_ANTIGRAVITY_HOME": str(self.agyhome),
            "EHA_DATA_DIR": str(self.data_dir),
            "EHA_OPTIONS_JSON": str(self.options),
            "EHA_HARNESS_FLAG_FILE": str(self.flag),
            "SUPERVISOR_TOKEN": SUP_TOKEN,
            "ANTHROPIC_API_KEY": API_KEY,
        }, clear=False)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._tmp.cleanup()

    def write(self, home: Path, rel: str, content: str = "x"):
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def seed_credentials(self):
        """Place all three harness credential stores with known secret values."""
        self.write(self.claude, ".credentials.json", json.dumps({"token": CLAUDE_OAUTH}))
        self.write(self.claude, ".claude.json", json.dumps({"userID": "u-123456789"}))
        self.write(self.codex, "auth.json", json.dumps({"access_token": CODEX_TOKEN}))
        self.write(self.gemini, "antigravity-cli/antigravity-oauth-token", AGY_TOKEN)
        self.write(self.gemini, "eha-auth-ok", "ok")

    def seed_memory(self, project="-data-workdir"):
        mem = self.claude / "projects" / project / "memory"
        (mem).mkdir(parents=True, exist_ok=True)
        (mem / "MEMORY.md").write_text("# index\n", encoding="utf-8")
        (mem / "note.md").write_text("a note\n", encoding="utf-8")
        return mem


def _extract(bundle_bytes):
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        for m in tar.getmembers():
            entries[m.name] = tar.extractfile(m).read()
    manifest = json.loads(entries["manifest.json"])
    return manifest, entries


def _build(kind):
    buf = io.BytesIO()
    if kind == "memory":
        manifest = export_manifest.build_memory_bundle(buf)
    else:
        manifest = export_manifest.build_data_dump(buf)
    return manifest, buf.getvalue()


def _paths(manifest):
    return {e["path"] for e in manifest["entries"]}


def _skip_reasons(manifest):
    return {r["path"]: r["reason"] for r in manifest["skipped"]}


class MemoryResolveTests(unittest.TestCase):
    def test_exactly_one_project_memory_resolves(self):
        with _Env() as env:
            mem = env.seed_memory()
            self.assertEqual(export_manifest.resolve_memory_dir(), str(mem))

    def test_zero_memory_dirs_fails_closed(self):
        with _Env():
            with self.assertRaises(export_manifest.ExportError):
                export_manifest.resolve_memory_dir()

    def test_multiple_memory_dirs_fail_closed(self):
        with _Env() as env:
            env.seed_memory("-p1")
            env.seed_memory("-p2")
            with self.assertRaises(export_manifest.ExportError):
                export_manifest.resolve_memory_dir()

    def test_pinned_auto_memory_directory_wins(self):
        with _Env() as env:
            env.seed_memory("-p1")
            pinned = env.claude / "pinned-memory"
            pinned.mkdir()
            (env.claude / "settings.json").write_text(
                json.dumps({"autoMemoryDirectory": str(pinned)}), encoding="utf-8")
            self.assertEqual(export_manifest.resolve_memory_dir(), str(pinned))

    def test_pinned_relative_path_rejected(self):
        with _Env() as env:
            env.seed_memory()
            (env.claude / "settings.json").write_text(
                json.dumps({"autoMemoryDirectory": "relative/path"}), encoding="utf-8")
            with self.assertRaises(export_manifest.ExportError):
                export_manifest.resolve_memory_dir()


class MemoryBundleTests(unittest.TestCase):
    def test_memory_bundle_contains_all_md(self):
        with _Env() as env:
            env.seed_memory()
            manifest, bundle = _build("memory")
            self.assertEqual(manifest["kind"], "memory")
            self.assertEqual(_paths(manifest), {"memory/MEMORY.md", "memory/note.md"})
            _, entries = _extract(bundle)
            self.assertEqual(entries["data/memory/note.md"], b"a note\n")

    def test_memory_with_secret_is_NOT_trimmed(self):
        """黙ったトリム禁止: メモリに実トークンが写り込んでいても丸ごと出す。"""
        with _Env() as env:
            mem = env.seed_memory()
            env.seed_credentials()
            (mem / "secretish.md").write_text(f"note contains {SUP_TOKEN}\n", encoding="utf-8")
            manifest, _ = _build("memory")
            self.assertIn("memory/secretish.md", _paths(manifest))

    def test_non_markdown_is_loud_skipped(self):
        with _Env() as env:
            mem = env.seed_memory()
            (mem / "junk.bin").write_bytes(b"\x00\x01")
            manifest, _ = _build("memory")
            self.assertNotIn("memory/junk.bin", _paths(manifest))
            self.assertEqual(_skip_reasons(manifest).get("memory/junk.bin"), "not-markdown")

    def test_empty_memory_dir_raises_instead_of_empty_tar(self):
        with _Env() as env:
            mem = env.claude / "projects" / "-p" / "memory"
            mem.mkdir(parents=True)
            with self.assertRaises(export_manifest.ExportError):
                _build("memory")

    def test_symlink_in_memory_not_followed(self):
        with _Env() as env:
            mem = env.seed_memory()
            secret = env.codex / "auth.json"
            env.seed_credentials()
            os.symlink(secret, mem / "sneaky.md")
            manifest, _ = _build("memory")
            self.assertNotIn("memory/sneaky.md", _paths(manifest))
            self.assertEqual(_skip_reasons(manifest).get("memory/sneaky.md"), "symlink")


class DataDumpTests(unittest.TestCase):
    def test_includes_normal_files_from_all_homes(self):
        with _Env() as env:
            env.seed_credentials()
            env.write(env.claude, "projects/-p/transcript.jsonl", '{"x":1}\n')
            env.write(env.codex, "sessions/s1.jsonl", '{"y":2}\n')
            env.write(env.gemini, "brain/log.jsonl", '{"z":3}\n')
            manifest, _ = _build("data-dump")
            paths = _paths(manifest)
            self.assertIn("claude/projects/-p/transcript.jsonl", paths)
            self.assertIn("codex/sessions/s1.jsonl", paths)
            self.assertIn("gemini/brain/log.jsonl", paths)
            self.assertEqual(manifest["kind"], "data-dump")

    def test_credential_files_excluded_by_name(self):
        with _Env() as env:
            env.seed_credentials()
            manifest, _ = _build("data-dump")
            paths = _paths(manifest)
            for bad in ("claude/.credentials.json", "claude/.claude.json",
                        "codex/auth.json",
                        "gemini/antigravity-cli/antigravity-oauth-token",
                        "gemini/eha-auth-ok"):
                self.assertNotIn(bad, paths, bad)
            reasons = _skip_reasons(manifest)
            self.assertEqual(reasons.get("claude/.credentials.json"), "excluded-name")
            self.assertEqual(reasons.get("gemini/antigravity-cli"), "excluded-dir")

    def test_value_scan_drops_innocuous_named_file_with_live_secret(self):
        """§14.7①: 名前が無害でも live 秘密値を含むファイルは丸ごと除外。"""
        with _Env() as env:
            env.seed_credentials()
            env.write(env.gemini, "some/notes.txt", f"embedded {SUP_TOKEN} here")
            env.write(env.codex, "config.toml", f'key = "{CODEX_TOKEN}"')
            env.write(env.claude, "harmless.json", json.dumps({"k": CLAUDE_OAUTH}))
            manifest, _ = _build("data-dump")
            paths = _paths(manifest)
            self.assertNotIn("gemini/some/notes.txt", paths)
            self.assertNotIn("codex/config.toml", paths)
            self.assertNotIn("claude/harmless.json", paths)
            reasons = _skip_reasons(manifest)
            self.assertEqual(reasons.get("gemini/some/notes.txt"), "contains-live-secret")
            self.assertEqual(reasons.get("codex/config.toml"), "contains-live-secret")
            self.assertEqual(reasons.get("claude/harmless.json"), "contains-live-secret")

    def test_value_scan_covers_api_key_from_options(self):
        with _Env() as env:
            env.write(env.claude, "leaked.txt", f"key={API_KEY}")
            manifest, _ = _build("data-dump")
            self.assertNotIn("claude/leaked.txt", _paths(manifest))

    def test_secret_carrier_configs_excluded(self):
        with _Env() as env:
            env.write(env.gemini, "config/mcp_config.json", "{}")
            env.write(env.claude, "workdir/.agents/mcp_config.json", "{}")
            env.write(env.codex, "abc123.config.toml", "profile")
            manifest, _ = _build("data-dump")
            paths = _paths(manifest)
            self.assertFalse(any("mcp_config.json" in p for p in paths))
            self.assertFalse(any(".agents" in p for p in paths))
            self.assertFalse(any(p.endswith(".config.toml") for p in paths))
            reasons = _skip_reasons(manifest)
            self.assertEqual(reasons.get("gemini/config"), "excluded-dir")
            self.assertEqual(reasons.get("claude/workdir/.agents"), "excluded-dir")

    def test_symlink_and_hardlink_loud_skipped(self):
        with _Env() as env:
            outside = Path(env._tmp.name) / "outside-secret"
            outside.write_text("outside", encoding="utf-8")
            os.symlink(outside, env.codex / "sym.jsonl")
            env.write(env.codex, "normal.jsonl", "ok")
            os.link(outside, env.codex / "hard.jsonl")
            manifest, _ = _build("data-dump")
            paths = _paths(manifest)
            self.assertIn("codex/normal.jsonl", paths)
            self.assertNotIn("codex/sym.jsonl", paths)
            self.assertNotIn("codex/hard.jsonl", paths)
            reasons = _skip_reasons(manifest)
            self.assertEqual(reasons.get("codex/sym.jsonl"), "symlink")
            self.assertEqual(reasons.get("codex/hard.jsonl"), "not-regular-or-hardlinked")

    def test_value_scan_covers_mqtt_pass_and_pem(self):
        with _Env() as env:
            pem = Path(env._tmp.name) / "gh.pem"
            pem.write_text("-----BEGIN RSA PRIVATE KEY-----\nAAAABBBBCCCC\n-----END RSA PRIVATE KEY-----\n",
                           encoding="utf-8")
            env.write(env.claude, "mqtt.txt", "password is mqtt-secret-pass-1234")
            env.write(env.codex, "gh.txt", "-----BEGIN RSA PRIVATE KEY-----\nAAAABBBBCCCC\n-----END RSA PRIVATE KEY-----\n")
            with mock.patch.dict(os.environ, {"MQTT_PASS": "mqtt-secret-pass-1234",
                                              "EHA_GITHUB_APP_PEM": str(pem)}, clear=False):
                manifest, _ = _build("data-dump")
            paths = _paths(manifest)
            self.assertNotIn("claude/mqtt.txt", paths)
            self.assertNotIn("codex/gh.txt", paths)

    def test_opaque_gzip_file_excluded(self):
        with _Env() as env:
            env.write(env.claude, "readable.jsonl", "ok")
            (env.claude / "blob.gz").write_bytes(b"\x1f\x8b\x08\x00secretishcompressed")
            (env.claude / "bin.dat").write_bytes(b"abc\x00def")
            manifest, _ = _build("data-dump")
            paths = _paths(manifest)
            self.assertIn("claude/readable.jsonl", paths)
            self.assertNotIn("claude/blob.gz", paths)
            self.assertNotIn("claude/bin.dat", paths)
            reasons = _skip_reasons(manifest)
            self.assertEqual(reasons.get("claude/blob.gz"), "opaque-not-scannable")
            self.assertEqual(reasons.get("claude/bin.dat"), "opaque-not-scannable")

    def test_broad_root_is_skipped_not_dumped(self):
        with _Env() as env:
            env.write(env.claude, "a.txt", "ok")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/config"}, clear=False):
                manifest, _ = _build("data-dump")
            paths = _paths(manifest)
            self.assertFalse(any(p.startswith("claude/") for p in paths))
            self.assertEqual(_skip_reasons(manifest).get("claude"), "root-too-broad")

    def test_overlapping_roots_skipped(self):
        with _Env() as env:
            with mock.patch.dict(os.environ, {"EHA_CODEX_HOME": str(env.claude / "sub")},
                                 clear=False):
                (env.claude / "sub").mkdir()
                manifest, _ = _build("data-dump")
            reasons = _skip_reasons(manifest)
            self.assertEqual(reasons.get("codex"), "root-overlaps-another")

    def test_unreadable_credential_store_fails_closed(self):
        with _Env() as env:
            # options.json present but not valid JSON → abort rather than scan-with-gap.
            env.options.write_text("{not json", encoding="utf-8")
            with self.assertRaises(export_manifest.ExportError):
                _build("data-dump")

    def test_missing_home_is_recorded_not_fatal(self):
        with _Env() as env:
            import shutil
            shutil.rmtree(env.codex)
            env.write(env.claude, "a.txt", "ok")
            manifest, _ = _build("data-dump")
            self.assertIn("claude/a.txt", _paths(manifest))
            self.assertEqual(_skip_reasons(manifest).get("codex"), "home-missing")

    def test_oversized_file_raises(self):
        with _Env() as env:
            env.write(env.claude, "big.txt", "x")
            with mock.patch.object(export_manifest, "MAX_FILE_BYTES", 0):
                with self.assertRaises(export_manifest.ExportError):
                    _build("data-dump")

    def test_archive_cap_raises(self):
        with _Env() as env:
            env.write(env.claude, "a.txt", "x" * 10000)
            with mock.patch.object(export_manifest, "MAX_ARCHIVE_BYTES", 16):
                with self.assertRaises(export_manifest.ExportError):
                    _build("data-dump")

    def test_temp_space_admission(self):
        with _Env() as env:
            env.seed_memory()
            fake = os.statvfs_result((4096, 4096, 1000, 1, 1, 0, 0, 0, 0, 255))
            with mock.patch.object(os, "statvfs", return_value=fake):
                with self.assertRaises(export_manifest.ExportError):
                    export_manifest.build_to_tempfile("memory")


class ExportEndpointTests(unittest.TestCase):
    def setUp(self):
        self._env = _Env().__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        self._env.seed_memory()
        self.enterContext(mock.patch.dict(os.environ, {"EHA_SETUP_GUARD": "off"}, clear=False))
        # server.HA_TOKEN was captured at import; blank it so the identity check is
        # exercised explicitly in its own test below.
        self.enterContext(mock.patch.object(server, "HA_TOKEN", ""))

    def _handler(self, path, headers=None, client=("127.0.0.1", 0)):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.client_address = client
        handler.headers = {"Content-Length": "0", **(headers or {})}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_json = mock.Mock()
        return handler

    def test_memory_export_streams_targz(self):
        handler = self._handler("/api/setup/export/memory")
        handler.do_POST()
        handler.send_response.assert_called_with(200)
        headers = {c.args[0]: c.args[1] for c in handler.send_header.call_args_list}
        self.assertEqual(headers.get("Content-Type"), "application/gzip")
        self.assertEqual(headers.get("Cache-Control"), "no-store, private")
        disposition = headers.get("Content-Disposition", "")
        self.assertIn("embodied-ha-memory-unknown-", disposition)
        manifest, _ = _extract(handler.wfile.getvalue())
        self.assertIn("memory/MEMORY.md", _paths(manifest))

    def test_filename_uses_validated_harness(self):
        self._env.flag.write_text("codex", encoding="utf-8")
        handler = self._handler("/api/setup/export/memory")
        handler.do_POST()
        headers = {c.args[0]: c.args[1] for c in handler.send_header.call_args_list}
        self.assertIn("embodied-ha-memory-codex-", headers.get("Content-Disposition", ""))

    def test_data_dump_requires_developer_mode(self):
        handler = self._handler("/api/setup/export/data-dump")
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[1], 403)

    def test_data_dump_with_developer_mode(self):
        self._env.write(self._env.claude, "diag.txt", "ok")
        with mock.patch.dict(os.environ, {"EHA_DEVELOPER_MODE": "true"}, clear=False):
            handler = self._handler("/api/setup/export/data-dump")
            handler.do_POST()
        handler.send_response.assert_called_with(200)
        manifest, _ = _extract(handler.wfile.getvalue())
        self.assertIn("claude/diag.txt", _paths(manifest))

    def test_requires_ingress_user_when_supervised(self):
        with mock.patch.object(server, "HA_TOKEN", "sup"):
            handler = self._handler("/api/setup/export/memory")
            handler.do_POST()
            self.assertEqual(handler.send_json.call_args.args[1], 403)
            handler = self._handler("/api/setup/export/memory",
                                    headers={"X-Remote-User-Id": "u1"})
            handler.do_POST()
            handler.send_response.assert_called_with(200)

    def test_memory_resolution_failure_is_400(self):
        import shutil
        shutil.rmtree(self._env.claude / "projects")
        handler = self._handler("/api/setup/export/memory")
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[1], 400)


class ExportGuardTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("EHA_SETUP_GUARD", None)

    def test_export_posts_guarded_from_loopback(self):
        for path in ("/api/setup/export/memory", "/api/setup/export/data-dump"):
            handler = object.__new__(server.Handler)
            handler.path = path
            handler.client_address = ("127.0.0.1", 0)  # 非ingress
            handler.headers = {"Content-Length": "0"}
            handler.rfile = io.BytesIO(b"")
            handler.send_json = mock.Mock()
            handler.do_POST()
            self.assertEqual(handler.send_json.call_args.args[1], 403, path)


if __name__ == "__main__":
    unittest.main()
