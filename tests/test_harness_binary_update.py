import hashlib
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

        self.new_payload = b"new-binary-contents"
        self.archive = _archive_with(self.new_payload)
        self.manifest = {
            "version": "1.1.12",
            "url": "https://example.invalid/agy.tar.gz",
            "sha512": hashlib.sha512(self.archive).hexdigest(),
        }

    # -- helpers ---------------------------------------------------------

    def urlopen_serving_archive(self):
        def fake_urlopen(url, timeout=None):
            if "manifests" in url:
                return _FakeResponse(json.dumps(self.manifest).encode("utf-8"))
            return _FakeResponse(self.archive)

        return mock.patch.object(updater, "urlopen", side_effect=fake_urlopen)

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

    def test_check_restores_the_freeze_it_lifted(self):
        active, add, remove = self.frozen()
        with self.urlopen_serving_archive(), active, add as add_mock, remove as remove_mock, \
                self.versions(["1.1.9"]):
            report = updater.check_for_update()
        self.assertTrue(report["update_available"])
        self.assertEqual(report["available_version"], "1.1.12")
        remove_mock.assert_called_once()
        add_mock.assert_called_once()

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
