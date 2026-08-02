import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import migrate_antigravity_structured_output as migration  # noqa: E402


class F157AntigravityUpgradeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "agy-home"
        self.bin_dir = self.home / "bin"
        self.binary = self.bin_dir / "agy"
        self.token = self.home / ".gemini/antigravity-cli/antigravity-oauth-token"
        self.bin_dir.mkdir(parents=True)
        self.token.parent.mkdir(parents=True)
        self.binary.write_bytes(b"old-binary")
        self.binary.chmod(0o755)
        self.token.write_bytes(b"oauth-secret")
        self._env = mock.patch.dict(
            os.environ,
            {
                "EHA_ANTIGRAVITY_HOME": str(self.home),
                "EHA_ANTIGRAVITY_BIN_DIR": str(self.bin_dir),
                "EHA_ANTIGRAVITY_BIN": str(self.binary),
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def selected_agy(self):
        return mock.patch.object(
            migration.harness_state,
            "read_selection",
            return_value=("valid", "agy"),
        )

    def freeze_mocks(self):
        return (
            mock.patch.object(migration.agy_update_freeze, "add_hosts_redirect", return_value=True),
            mock.patch.object(migration.agy_update_freeze, "remove_hosts_redirect", return_value=True),
        )

    def test_non_antigravity_selection_never_probes_or_updates(self):
        with mock.patch.object(
            migration.harness_state,
            "read_selection",
            return_value=("valid", "codex"),
        ), mock.patch.object(migration, "_supports_structured_output") as supports, \
             mock.patch.object(migration, "_run_official_installer") as installer:
            result = migration.migrate()
        self.assertEqual(result, {"status": "skipped", "reason": "antigravity_not_selected"})
        supports.assert_not_called()
        installer.assert_not_called()

    def test_supported_cli_is_left_unchanged_and_frozen(self):
        add_patch, remove_patch = self.freeze_mocks()
        with self.selected_agy(), add_patch as add, remove_patch as remove, \
             mock.patch.object(migration, "_supports_structured_output", return_value=True), \
             mock.patch.object(migration, "_cli_version", return_value="1.1.9"), \
             mock.patch.object(migration, "_run_official_installer") as installer:
            result = migration.migrate()
        self.assertEqual(result["reason"], "structured_output_supported")
        self.assertEqual(result["version"], "1.1.9")
        installer.assert_not_called()
        remove.assert_not_called()
        self.assertGreaterEqual(add.call_count, 1)
        self.assertEqual(self.binary.read_bytes(), b"old-binary")

    def test_old_cli_updates_preserves_auth_and_removes_rollback_binary(self):
        def install():
            self.assertFalse(self.binary.exists())
            self.binary.write_bytes(b"new-binary")
            self.binary.chmod(0o755)

        add_patch, remove_patch = self.freeze_mocks()
        with self.selected_agy(), add_patch as add, remove_patch as remove, \
             mock.patch.object(migration, "_supports_structured_output", side_effect=[False, True]), \
             mock.patch.object(migration, "_cli_version", side_effect=["1.1.6", "1.1.9"]), \
             mock.patch.object(migration, "_run_official_installer", side_effect=install):
            result = migration.migrate()

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["old_version"], "1.1.6")
        self.assertEqual(result["new_version"], "1.1.9")
        self.assertTrue(result["auth_preserved"])
        self.assertEqual(self.binary.read_bytes(), b"new-binary")
        self.assertEqual(self.token.read_bytes(), b"oauth-secret")
        self.assertFalse(migration._binary_backup_path().exists())
        remove.assert_called_once_with()
        self.assertGreaterEqual(add.call_count, 2)

    def test_installer_failure_restores_binary_and_auth(self):
        def fail_install():
            self.binary.write_bytes(b"partial-binary")
            self.token.write_bytes(b"changed-token")
            raise RuntimeError("download failed")

        add_patch, remove_patch = self.freeze_mocks()
        with self.selected_agy(), add_patch, remove_patch, \
             mock.patch.object(migration, "_supports_structured_output", return_value=False), \
             mock.patch.object(migration, "_cli_version", return_value="1.1.6"), \
             mock.patch.object(migration, "_run_official_installer", side_effect=fail_install), \
             self.assertRaisesRegex(RuntimeError, "download failed"):
            migration.migrate()

        self.assertEqual(self.binary.read_bytes(), b"old-binary")
        self.assertEqual(self.token.read_bytes(), b"oauth-secret")
        self.assertFalse(migration._binary_backup_path().exists())
        self.assertTrue(self.binary.stat().st_mode & stat.S_IXUSR)

    def test_new_binary_without_required_flags_is_rolled_back(self):
        def install():
            self.binary.write_bytes(b"unsupported-new-binary")
            self.binary.chmod(0o755)

        add_patch, remove_patch = self.freeze_mocks()
        with self.selected_agy(), add_patch, remove_patch, \
             mock.patch.object(migration, "_supports_structured_output", side_effect=[False, False]), \
             mock.patch.object(migration, "_cli_version", return_value="1.1.6"), \
             mock.patch.object(migration, "_run_official_installer", side_effect=install), \
             self.assertRaisesRegex(RuntimeError, "does not support"):
            migration.migrate()
        self.assertEqual(self.binary.read_bytes(), b"old-binary")
        self.assertEqual(self.token.read_bytes(), b"oauth-secret")

    def test_auth_mode_change_is_rejected_and_rolled_back(self):
        self.token.chmod(0o600)

        def install():
            self.binary.write_bytes(b"new-binary")
            self.binary.chmod(0o755)
            self.token.chmod(0o644)

        add_patch, remove_patch = self.freeze_mocks()
        with self.selected_agy(), add_patch, remove_patch, \
             mock.patch.object(migration, "_supports_structured_output", side_effect=[False, True]), \
             mock.patch.object(migration, "_cli_version", return_value="1.1.6"), \
             mock.patch.object(migration, "_run_official_installer", side_effect=install), \
             self.assertRaisesRegex(RuntimeError, "authentication changed"):
            migration.migrate()
        self.assertEqual(self.binary.read_bytes(), b"old-binary")
        self.assertEqual(self.token.read_bytes(), b"oauth-secret")
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)

    def test_auth_rollback_is_attempted_even_when_binary_rollback_fails(self):
        def fail_install():
            self.binary.write_bytes(b"partial-binary")
            self.token.write_bytes(b"changed-token")
            raise RuntimeError("install failed")

        add_patch, remove_patch = self.freeze_mocks()
        with self.selected_agy(), add_patch, remove_patch, \
             mock.patch.object(migration, "_supports_structured_output", return_value=False), \
             mock.patch.object(migration, "_cli_version", return_value="1.1.6"), \
             mock.patch.object(migration, "_run_official_installer", side_effect=fail_install), \
             mock.patch.object(migration, "_restore_binary_backup", side_effect=OSError("disk error")), \
             self.assertRaisesRegex(RuntimeError, "rollback incomplete"):
            migration.migrate()
        self.assertEqual(self.token.read_bytes(), b"oauth-secret")

    def test_interrupted_update_backup_is_restored_before_retry(self):
        backup = migration._binary_backup_path()
        backup.write_bytes(b"known-good-old-binary")
        backup.chmod(0o755)
        self.binary.write_bytes(b"partial-binary")

        with mock.patch.object(migration, "_supports_structured_output", return_value=False):
            recovered = migration._recover_interrupted_update()

        self.assertTrue(recovered)
        self.assertEqual(self.binary.read_bytes(), b"known-good-old-binary")
        self.assertFalse(backup.exists())

    def test_verified_new_binary_discards_stale_backup_without_restoring_it(self):
        backup = migration._binary_backup_path()
        backup.write_bytes(b"old-binary")
        backup.chmod(0o755)
        self.binary.write_bytes(b"verified-new-binary")

        with mock.patch.object(migration, "_supports_structured_output", return_value=True):
            recovered = migration._recover_interrupted_update()

        self.assertFalse(recovered)
        self.assertEqual(self.binary.read_bytes(), b"verified-new-binary")
        self.assertFalse(backup.exists())

    def test_main_reports_failure_in_english_without_raising(self):
        stdout = io.StringIO()
        with mock.patch.object(migration, "migrate", side_effect=RuntimeError("network down")), \
             mock.patch("sys.stdout", stdout):
            result = migration.main()
        self.assertEqual(result, 1)
        self.assertEqual(
            stdout.getvalue(),
            "[f157-agy-upgrade] failed: RuntimeError: network down\n",
        )

    def test_main_reports_success_in_english(self):
        stdout = io.StringIO()
        result_value = {
            "status": "updated",
            "old_version": "1.1.6",
            "new_version": "1.1.9",
            "auth_preserved": True,
        }
        with mock.patch.object(migration, "migrate", return_value=result_value), \
             mock.patch("sys.stdout", stdout):
            result = migration.main()
        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue(),
            "[f157-agy-upgrade] updated: 1.1.6 -> 1.1.9 "
            "auth_preserved=yes rollback_backup=removed\n",
        )

    def test_run_sh_orders_upgrade_between_cleanup_and_freeze(self):
        source = (ROOT / "embodied_ha/run.sh").read_text(encoding="utf-8")
        cleanup = source.index('migrate_remove_unused_antigravity.py')
        upgrade = source.index('migrate_antigravity_structured_output.py')
        freeze = source.index('agy_update_freeze.py" add')
        self.assertLess(cleanup, upgrade)
        self.assertLess(upgrade, freeze)

    def test_install_contract_rejects_changed_installer_before_manifest_fetch(self):
        with mock.patch.object(
            migration.antigravity_setup,
            "fetch_install_script",
            return_value="changed installer",
        ), mock.patch.object(migration, "_fetch_manifest") as fetch_manifest:
            with self.assertRaisesRegex(RuntimeError, "pinned SHA-256"):
                migration._pinned_install_contract()
        fetch_manifest.assert_not_called()

    def test_install_contract_rejects_changed_manifest(self):
        script = "pinned installer"
        installer_hash = migration.hashlib.sha256(script.encode()).hexdigest()
        with mock.patch.object(migration, "INSTALLER_SHA256", installer_hash), \
             mock.patch.object(
                 migration.antigravity_setup,
                 "fetch_install_script",
                 return_value=script,
             ), mock.patch.object(migration, "_release_platform", return_value="linux_amd64"), \
             mock.patch.object(migration, "_fetch_manifest", return_value=b"{}"):
            with self.assertRaisesRegex(RuntimeError, "manifest does not match"):
                migration._pinned_install_contract()

    def test_official_installer_rejects_wrong_installed_binary_digest(self):
        pinned = {
            "version": "1.1.9",
            "binary_sha512": "expected",
        }
        proc = mock.Mock(returncode=0, stdout="installed")
        with mock.patch.object(
            migration,
            "_pinned_install_contract",
            return_value=("exit 0\n", pinned),
        ), mock.patch.object(migration.subprocess, "run", return_value=proc), \
             mock.patch.object(migration, "_supports_structured_output", return_value=True), \
             mock.patch.object(migration, "_cli_version", return_value="1.1.9"), \
             mock.patch.object(migration, "_binary_sha512", return_value="different"), \
             self.assertRaisesRegex(RuntimeError, "pinned SHA-512"):
            migration._run_official_installer()


if __name__ == "__main__":
    unittest.main()
