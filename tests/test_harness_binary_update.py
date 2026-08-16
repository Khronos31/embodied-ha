import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import struct
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import harness_binary_update as updater  # noqa: E402
import harness_pin  # noqa: E402


def _archive_with(payload: bytes) -> bytes:
    """Build a vendor-shaped tar.gz carrying a single `antigravity` member."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(updater.ARCHIVE_MEMBER)
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class HarnessPinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pin_file = Path(self._tmp.name) / "harness_pin.json"
        env = mock.patch.dict(
            os.environ, {"EHA_HARNESS_PIN_FILE": str(self.pin_file)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)

    def test_absent_record_is_missing_not_invalid(self):
        state, record = harness_pin.read_record()
        self.assertEqual(state, "missing")
        self.assertEqual(record["harnesses"], {})
        self.assertIsNone(harness_pin.read_pin("agy"))

    def test_recorded_install_is_readable(self):
        harness_pin.record_install("agy", "1.1.9", url="https://x/y", binary_sha512="ab")
        pin = harness_pin.read_pin("agy")
        self.assertEqual(pin["version"], "1.1.9")
        self.assertEqual(pin["binary_sha512"], "ab")
        self.assertEqual(pin["source"], "install")
        self.assertTrue(pin["installed_at"])

    def test_corrupt_record_is_invalid_so_callers_fail_closed(self):
        self.pin_file.write_text("{not json", encoding="utf-8")
        state, _ = harness_pin.read_record()
        self.assertEqual(state, "invalid")
        self.assertIsNone(harness_pin.read_pin("agy"))

    def test_unknown_schema_version_is_invalid(self):
        self.pin_file.write_text(
            json.dumps({"schema_version": 99, "harnesses": {}}), encoding="utf-8"
        )
        self.assertEqual(harness_pin.read_record()[0], "invalid")

    def test_install_preserves_retained_builds(self):
        harness_pin.add_retained("agy", "1.1.9", "/data/bin/agy-1.1.9")
        harness_pin.record_install("agy", "1.1.12")
        retained = harness_pin.retained_builds("agy")
        self.assertEqual([item["version"] for item in retained], ["1.1.9"])

    def test_re_retaining_a_version_does_not_duplicate_it(self):
        harness_pin.add_retained("agy", "1.1.9", "/data/bin/agy-1.1.9")
        harness_pin.add_retained("agy", "1.1.9", "/data/bin/agy-1.1.9")
        self.assertEqual(len(harness_pin.retained_builds("agy")), 1)

    def test_dropped_retention_disappears(self):
        harness_pin.add_retained("agy", "1.1.9", "/data/bin/agy-1.1.9")
        harness_pin.drop_retained("agy", "1.1.9")
        self.assertEqual(harness_pin.retained_builds("agy"), [])

    def test_unknown_harness_is_rejected(self):
        with self.assertRaises(ValueError):
            harness_pin.record_install("gemini", "1.0.0")


class AntigravityUpdateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "agy-home"
        self.bin_dir = self.home / "bin"
        self.binary = self.bin_dir / "agy"
        self.token = self.home / ".gemini/antigravity-cli/antigravity-oauth-token"
        self.bin_dir.mkdir(parents=True)
        self.token.parent.mkdir(parents=True)
        self.binary.write_bytes(b"old-binary")
        self.binary.chmod(0o755)
        self.token.write_bytes(b"oauth-secret")

        env = mock.patch.dict(
            os.environ,
            {
                "EHA_ANTIGRAVITY_HOME": str(self.home),
                "EHA_ANTIGRAVITY_BIN_DIR": str(self.bin_dir),
                "EHA_ANTIGRAVITY_BIN": str(self.binary),
                "EHA_HARNESS_PIN_FILE": str(root / "harness_pin.json"),
                "EHA_HARNESS_UPDATE_JOURNAL": str(root / "journal.json"),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

        # 実機の /etc/hosts を絶対に触らせない。フォールバック経路は本物の
        # remove/add_hosts_redirect を呼ぶため、これが無いとテスト実行が
        # ホストの凍結状態を書き換える（2026-08-13 に実際に消えた）。
        # 個々のテストは self.frozen() でこのモックの属性を上書きして観測する。
        freeze = mock.patch.object(updater, "agy_update_freeze", mock.MagicMock())
        freeze.start()
        self.addCleanup(freeze.stop)

        self.new_payload = b"new-binary-contents"
        self.archive = _archive_with(self.new_payload)
        self.manifest = {
            "version": "1.1.12",
            "url": "https://example.invalid/agy.tar.gz",
            "sha512": hashlib.sha512(self.archive).hexdigest(),
        }

    # -- helpers ---------------------------------------------------------

    @contextlib.contextmanager
    def urlopen_serving_archive(self):
        """Serve both transports offline.

        manifest は hosts を迂回して実IPから取る経路が本線になったので、そちらも
        差し替える。塞がないとテストが本物のベンダーへ出ていく（実際に出て、本物の
        manifest を掴んで SHA-512 不一致で落ちた）。
        """

        def fake_urlopen(url, timeout=None):
            if "manifests" in url:
                return _FakeResponse(json.dumps(self.manifest).encode("utf-8"))
            return _FakeResponse(self.archive)

        def fake_pinned(url, timeout, max_bytes):
            return json.dumps(self.manifest).encode("utf-8")

        with mock.patch.object(updater, "urlopen", side_effect=fake_urlopen), mock.patch.object(
            updater, "_read_via_pinned_ip", side_effect=fake_pinned
        ):
            yield

    def frozen(self):
        return (
            mock.patch.object(updater.agy_update_freeze, "is_redirect_active", return_value=True),
            mock.patch.object(updater.agy_update_freeze, "add_hosts_redirect", return_value=True),
            mock.patch.object(
                updater.agy_update_freeze, "remove_hosts_redirect", return_value=True
            ),
        )

    def versions(self, sequence):
        """Report versions in call order: the CLI answers differently after a swap."""
        return mock.patch.object(updater, "installed_version", side_effect=list(sequence))

    # -- observation -----------------------------------------------------

    def test_manifest_without_https_url_is_rejected(self):
        self.manifest["url"] = "http://example.invalid/agy.tar.gz"
        with self.urlopen_serving_archive(), self.assertRaises(updater.UpdateError):
            updater.fetch_manifest()

    def test_manifest_with_short_digest_is_rejected(self):
        self.manifest["sha512"] = "deadbeef"
        with self.urlopen_serving_archive(), self.assertRaises(updater.UpdateError):
            updater.fetch_manifest()

    def test_check_never_lowers_the_freeze(self):
        """確認のたびに凍結を解かないこと。

        当初は「解いて取り、必ず戻す」だった。manifest を hosts 迂回で取れると
        実測できたので（2026-08-13）、**窓を狭めるのではなく開けない**方へ変えた。
        凍結対象は manifest のホスト1つだけで、アーカイブ本体は別ホストにあるため、
        これで更新経路から解除窓そのものが消える。定期確認を将来足すなら必須の性質。
        """
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove as remove_mock, \
                self.versions(["1.1.9"]):
            report = updater.check_for_update()
        self.assertTrue(report["update_available"])
        self.assertEqual(report["available_version"], "1.1.12")
        remove_mock.assert_not_called()

    def test_manifest_falls_back_to_lifting_the_freeze_when_the_bypass_fails(self):
        """迂回できない環境（nameserverが読めない等）でも確認は成立すること。"""
        active, add, remove = self.frozen()

        def fake_urlopen(url, timeout=None):
            return _FakeResponse(json.dumps(self.manifest).encode("utf-8"))

        with active, add as add_mock, remove as remove_mock, \
                mock.patch.object(updater, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(
                    updater, "_read_via_pinned_ip", side_effect=updater.UpdateError("no nameserver")
                ):
            manifest = updater.fetch_manifest()
        self.assertEqual(manifest["version"], "1.1.12")
        remove_mock.assert_called_once()
        add_mock.assert_called_once()

    def test_pinned_fetch_refuses_a_non_https_url(self):
        with self.assertRaises(updater.UpdateError):
            updater._read_via_pinned_ip("http://example.invalid/x.json", 5, 1024)

    # -- update ----------------------------------------------------------

    def test_update_swaps_binary_and_records_the_pin(self):
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, \
                self.versions(["1.1.9", "1.1.12"]), \
                mock.patch.object(updater, "supports_structured_output", return_value=True):
            result = updater.update()
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["version"], "1.1.12")
        self.assertEqual(self.binary.read_bytes(), self.new_payload)
        self.assertEqual(harness_pin.read_pin("agy")["version"], "1.1.12")
        self.assertIsNone(updater.read_journal())

    def test_update_retains_the_previous_build_for_rollback(self):
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, \
                self.versions(["1.1.9", "1.1.12"]), \
                mock.patch.object(updater, "supports_structured_output", return_value=True):
            updater.update()
        retained = harness_pin.retained_builds("agy")
        self.assertEqual([item["version"] for item in retained], ["1.1.9"])
        kept = Path(retained[0]["path"])
        self.assertTrue(kept.exists())
        self.assertEqual(kept.read_bytes(), b"old-binary")

    def test_binary_path_never_becomes_absent_during_an_update(self):
        """The whole reason updates do not reuse the vendor installer."""
        observed = []
        real_replace = os.replace
        real_unlink = os.unlink
        target = str(self.binary)

        # `updater.os` is the shared module object, so these wrappers also see
        # unrelated calls (shutil's cleanup passes dir_fd); accept anything and
        # only record the ones aimed at the live binary.
        def watching_replace(src, dst, *args, **kwargs):
            if str(dst) == target:
                observed.append(("replace", os.path.exists(target)))
            return real_replace(src, dst, *args, **kwargs)

        def watching_unlink(path, *args, **kwargs):
            if str(path) == target:
                observed.append(("unlink", True))
            return real_unlink(path, *args, **kwargs)

        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, \
                self.versions(["1.1.9", "1.1.12"]), \
                mock.patch.object(updater, "supports_structured_output", return_value=True), \
                mock.patch.object(updater.os, "replace", side_effect=watching_replace), \
                mock.patch.object(updater.os, "unlink", side_effect=watching_unlink):
            updater.update()

        self.assertNotIn("unlink", [kind for kind, _ in observed])
        self.assertTrue(all(existed for _, existed in observed))

    def test_failure_between_journal_and_swap_leaves_no_journal(self):
        """A journal that outlives its transaction would trigger a bogus reconcile."""
        real_replace = os.replace
        target = str(self.binary)

        def refuse_the_swap(src, dst, *args, **kwargs):
            if str(dst) == target:
                raise OSError("disk went away")
            return real_replace(src, dst, *args, **kwargs)

        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, self.versions(["1.1.9"]), \
                mock.patch.object(updater.os, "replace", side_effect=refuse_the_swap), \
                self.assertRaises(OSError):
            updater.update()
        # Retention happens immediately before the journal write, so its presence
        # proves the transaction really reached the point this test is about.
        self.assertEqual([item["version"] for item in harness_pin.retained_builds("agy")], ["1.1.9"])
        self.assertIsNone(updater.read_journal())
        self.assertEqual(self.binary.read_bytes(), b"old-binary")

    def test_corrupt_download_is_refused_before_any_swap(self):
        self.manifest["sha512"] = "0" * 128
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, self.versions(["1.1.9"]), \
                self.assertRaises(updater.UpdateError):
            updater.update()
        self.assertEqual(self.binary.read_bytes(), b"old-binary")

    def test_failed_verification_rolls_the_binary_back(self):
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, \
                self.versions(["1.1.9", "9.9.9"]), \
                mock.patch.object(updater, "supports_structured_output", return_value=True), \
                self.assertRaises(updater.UpdateError):
            updater.update()
        self.assertEqual(self.binary.read_bytes(), b"old-binary")

    def test_lost_structured_output_support_rolls_back(self):
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, \
                self.versions(["1.1.9", "1.1.12"]), \
                mock.patch.object(updater, "supports_structured_output", return_value=False), \
                self.assertRaises(updater.UpdateError):
            updater.update()
        self.assertEqual(self.binary.read_bytes(), b"old-binary")

    def test_changed_authentication_rolls_back(self):
        active, add, remove = self.frozen()

        def clobber_auth(*_args, **_kwargs):
            self.token.write_bytes(b"different-token")
            return True

        with self.urlopen_serving_archive(), active, add, remove, \
                self.versions(["1.1.9", "1.1.12"]), \
                mock.patch.object(updater, "supports_structured_output", side_effect=clobber_auth), \
                self.assertRaises(updater.UpdateError):
            updater.update()
        self.assertEqual(self.binary.read_bytes(), b"old-binary")

    def test_already_current_is_a_no_op(self):
        self.manifest["version"] = "1.1.9"
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, self.versions(["1.1.9"]):
            result = updater.update()
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(self.binary.read_bytes(), b"old-binary")

    def test_update_refuses_when_nothing_is_installed(self):
        self.binary.unlink()
        with self.assertRaises(updater.UpdateError):
            updater.update()

    def test_freeze_is_restored_even_when_the_update_fails(self):
        self.manifest["sha512"] = "0" * 128
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add as add_mock, remove, \
                self.versions(["1.1.9"]), self.assertRaises(updater.UpdateError):
            updater.update()
        add_mock.assert_called_once()

    # -- rollback --------------------------------------------------------

    def test_rollback_restores_the_retained_build(self):
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, \
                self.versions(["1.1.9", "1.1.12"]), \
                mock.patch.object(updater, "supports_structured_output", return_value=True):
            updater.update()
        with self.versions(["1.1.12", "1.1.9"]):
            result = updater.rollback()
        self.assertEqual(result["version"], "1.1.9")
        self.assertEqual(self.binary.read_bytes(), b"old-binary")
        self.assertEqual(harness_pin.read_pin("agy")["version"], "1.1.9")
        self.assertEqual(harness_pin.read_pin("agy")["source"], "rollback")

    def test_rollback_refuses_a_tampered_retained_binary(self):
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add, remove, \
                self.versions(["1.1.9", "1.1.12"]), \
                mock.patch.object(updater, "supports_structured_output", return_value=True):
            updater.update()
        Path(harness_pin.retained_builds("agy")[0]["path"]).write_bytes(b"tampered")
        with self.versions(["1.1.12"]), self.assertRaises(updater.UpdateError):
            updater.rollback()
        self.assertEqual(self.binary.read_bytes(), self.new_payload)

    def test_rollback_to_a_deleted_retained_build_reports_it_plainly(self):
        """消えた保管版で素の FileNotFoundError を出さないこと。"""
        harness_pin.add_retained("agy", "1.1.6", str(self.bin_dir / "agy-1.1.6"), binary_sha512="ab")
        # 第1引数は harness になったので版はキーワードで渡す（3ハーネス共通化に伴う変更）。
        with self.versions(["1.1.9"]), self.assertRaises(updater.UpdateError) as raised:
            updater.rollback(version="1.1.6")
        self.assertIn("missing", str(raised.exception))
        self.assertEqual(self.binary.read_bytes(), b"old-binary")

    def test_rollback_without_any_retained_build_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.rollback()

    # -- reconcile -------------------------------------------------------

    def test_reconcile_without_a_journal_is_clean(self):
        self.assertEqual(updater.reconcile()["status"], "clean")

    def test_reconcile_makes_the_pin_agree_with_the_disk(self):
        harness_pin.record_install("agy", "1.1.9")
        updater._write_journal(
            {"harness": "agy", "from_version": "1.1.9", "to_version": "1.1.12", "phase": "prepared"}
        )
        with self.versions(["1.1.12"]):
            result = updater.reconcile()
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(harness_pin.read_pin("agy")["version"], "1.1.12")
        self.assertIsNone(updater.read_journal())

    def test_reconcile_leaves_the_journal_when_the_version_is_unreadable(self):
        updater._write_journal({"harness": "agy", "phase": "prepared"})
        with self.versions([None]):
            result = updater.reconcile()
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNotNone(updater.read_journal())


if __name__ == "__main__":
    unittest.main()


class _FakeSetupModule:
    """A claude/codex-shaped installer backed by real directories.

    Mirrors the contract this module relies on: an install root containing
    ``bin/<name>``, an installer that replaces that root atomically, and a
    resolver for the current release.
    """

    def __init__(self, name, root, latest="2.0.0"):
        self.name = name
        self.root = root
        self.latest = latest
        self.install_calls = []
        self.installed_payload = None

    # -- accessors the updater uses ---------------------------------------
    def _resolved_install_root(self):
        return str(self.root)

    def binary_path(self):
        return str(Path(self.root) / "bin" / self.name)

    def is_installed(self):
        return os.path.isfile(self.binary_path())

    def resolve_version(self, timeout=60):
        return self.latest

    def resolve_release(self, timeout=60):
        return {"version": self.latest}

    # -- the installer -----------------------------------------------------
    def install(self, version="latest", timeout=60, progress=None):
        self.install_calls.append(version)
        if progress:
            progress(f"Downloading {self.name} {version}")
        staged = Path(str(self.root) + ".staged")
        if staged.exists():
            shutil.rmtree(staged)
        (staged / "bin").mkdir(parents=True)
        (staged / "bin" / self.name).write_text(
            self.installed_payload or f"{self.name}-{version}", encoding="utf-8"
        )
        (staged / "bin" / self.name).chmod(0o755)
        if os.path.lexists(self.root):
            shutil.rmtree(self.root)
        os.replace(staged, self.root)
        return {"version": version}


class RootHarnessUpdateTests(unittest.TestCase):
    """claude/codex: the unit swapped is the whole install root."""

    HARNESS = "claude"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.install_root = root / f"{self.HARNESS}-cli"
        (self.install_root / "bin").mkdir(parents=True)
        self.binary = self.install_root / "bin" / self.HARNESS
        self.binary.write_text("old-build", encoding="utf-8")
        self.binary.chmod(0o755)

        env = mock.patch.dict(
            os.environ,
            {
                "EHA_HARNESS_PIN_FILE": str(root / "harness_pin.json"),
                "EHA_HARNESS_UPDATE_JOURNAL": str(root / "journal.json"),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

        self.module = _FakeSetupModule(self.HARNESS, self.install_root, latest="2.0.0")
        patch = mock.patch.object(updater, f"{self.HARNESS}_setup", self.module)
        patch.start()
        self.addCleanup(patch.stop)

    def versions(self, sequence):
        return mock.patch.object(updater, "installed_version", side_effect=list(sequence))

    def test_update_drives_the_existing_installer_and_records_the_pin(self):
        with self.versions(["1.0.0", "2.0.0"]):
            result = updater.update(self.HARNESS)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["version"], "2.0.0")
        # 既存インストーラをそのまま適用段として使うこと（DL処理を再実装しない）。
        self.assertEqual(self.module.install_calls, ["2.0.0"])
        self.assertEqual(self.binary.read_text(encoding="utf-8"), f"{self.HARNESS}-2.0.0")
        self.assertEqual(harness_pin.read_pin(self.HARNESS)["version"], "2.0.0")
        self.assertIsNone(updater.read_journal())

    def test_update_retains_the_previous_install_root(self):
        with self.versions(["1.0.0", "2.0.0"]):
            updater.update(self.HARNESS)
        retained = harness_pin.retained_builds(self.HARNESS)
        self.assertEqual([item["version"] for item in retained], ["1.0.0"])
        kept = Path(retained[0]["path"]) / "bin" / self.HARNESS
        self.assertTrue(kept.is_file())
        self.assertEqual(kept.read_text(encoding="utf-8"), "old-build")

    def test_wrong_version_after_install_rolls_the_root_back(self):
        with self.versions(["1.0.0", "9.9.9"]), self.assertRaises(updater.UpdateError):
            updater.update(self.HARNESS)
        self.assertEqual(self.binary.read_text(encoding="utf-8"), "old-build")

    def test_installer_failure_rolls_the_root_back(self):
        self.module.install = mock.Mock(side_effect=RuntimeError("network down"))
        with self.versions(["1.0.0"]), self.assertRaises(RuntimeError):
            updater.update(self.HARNESS)
        self.assertEqual(self.binary.read_text(encoding="utf-8"), "old-build")
        self.assertIsNone(updater.read_journal())

    def test_rollback_restores_the_retained_root(self):
        with self.versions(["1.0.0", "2.0.0"]):
            updater.update(self.HARNESS)
        with self.versions(["2.0.0", "1.0.0"]):
            result = updater.rollback(self.HARNESS)
        self.assertEqual(result["version"], "1.0.0")
        self.assertEqual(self.binary.read_text(encoding="utf-8"), "old-build")
        self.assertEqual(harness_pin.read_pin(self.HARNESS)["source"], "rollback")

    def test_already_current_does_not_touch_the_installer(self):
        self.module.latest = "1.0.0"
        with self.versions(["1.0.0"]):
            result = updater.update(self.HARNESS)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(self.module.install_calls, [])

    def test_check_reports_the_resolver_version(self):
        with self.versions(["1.0.0"]):
            report = updater.check_for_update(self.HARNESS)
        self.assertEqual(report["harness"], self.HARNESS)
        self.assertEqual(report["available_version"], "2.0.0")
        self.assertTrue(report["update_available"])

    def test_retention_is_capped_so_backups_do_not_grow_without_bound(self):
        for old, new in (("1.0.0", "2.0.0"), ("2.0.0", "3.0.0")):
            self.module.latest = new
            with self.versions([old, new]):
                updater.update(self.HARNESS)
        retained = harness_pin.retained_builds(self.HARNESS)
        self.assertEqual(len(retained), updater.RETAINED_GENERATIONS)
        self.assertEqual(retained[-1]["version"], "2.0.0")
        # 記録から外した世代は実体も消えていること（/data と HA バックアップが膨らむため）。
        self.assertFalse(Path(str(self.install_root) + "-1.0.0").exists())


class CodexRootHarnessUpdateTests(RootHarnessUpdateTests):
    HARNESS = "codex"


class UnknownHarnessTests(unittest.TestCase):
    def test_update_rejects_an_unknown_harness(self):
        with self.assertRaises(updater.UpdateError):
            updater.update("gemini")

    def test_rollback_rejects_an_unknown_harness(self):
        with self.assertRaises(updater.UpdateError):
            updater.rollback("gemini")


class DnsBypassTests(unittest.TestCase):
    """手書きのDNS応答パーサ。依存を増やさない代わりに、壊れ方を自分で持つ部分。"""

    QUERY_ID = 0x4548

    def _response(self, *, flags=0x8180, answers=(), query_id=None):
        header = struct.pack(
            ">HHHHHH", self.QUERY_ID if query_id is None else query_id, flags, 1, len(answers), 0, 0
        )
        question = b"\x03www\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
        body = b""
        for record_type, payload in answers:
            body += b"\xc0\x0c" + struct.pack(">HHIH", record_type, 1, 60, len(payload)) + payload
        return header + question + body

    def test_extracts_the_a_record(self):
        packet = self._response(answers=[(1, socket.inet_aton("203.0.113.7"))])
        self.assertEqual(updater._first_a_record(packet, self.QUERY_ID), "203.0.113.7")

    def test_follows_past_a_cname_to_the_a_record(self):
        packet = self._response(
            answers=[(5, b"\x03cdn\x07example\x03com\x00"), (1, socket.inet_aton("198.51.100.9"))]
        )
        self.assertEqual(updater._first_a_record(packet, self.QUERY_ID), "198.51.100.9")

    def test_mismatched_transaction_id_is_refused(self):
        packet = self._response(answers=[(1, socket.inet_aton("203.0.113.7"))], query_id=0x1234)
        with self.assertRaises(updater.UpdateError):
            updater._first_a_record(packet, self.QUERY_ID)

    def test_truncated_response_is_refused(self):
        # TC ビットが立っていたら、切れた答えを信用せず失敗させる。
        packet = self._response(flags=0x8380, answers=[(1, socket.inet_aton("203.0.113.7"))])
        with self.assertRaises(updater.UpdateError):
            updater._first_a_record(packet, self.QUERY_ID)

    def test_server_failure_rcode_is_refused(self):
        packet = self._response(flags=0x8182)  # SERVFAIL
        with self.assertRaises(updater.UpdateError):
            updater._first_a_record(packet, self.QUERY_ID)

    def test_answer_without_an_a_record_is_refused(self):
        packet = self._response(answers=[(28, b"\x00" * 16)])  # AAAA のみ
        with self.assertRaises(updater.UpdateError):
            updater._first_a_record(packet, self.QUERY_ID)

    def test_short_packet_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater._first_a_record(b"\x00\x01", self.QUERY_ID)

    def test_resolution_without_a_nameserver_is_refused(self):
        with mock.patch.object(updater, "_nameservers", return_value=[]), \
                self.assertRaises(updater.UpdateError):
            updater._resolve_bypassing_hosts("example.invalid")
