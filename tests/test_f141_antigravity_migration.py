import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import migrate_remove_unused_antigravity as migration


class F141AntigravityMigrationTests(unittest.TestCase):
    def test_valid_non_antigravity_selection_uninstalls_and_unfreezes(self):
        for selected in ("claude", "codex"):
            with self.subTest(selected=selected), \
                 mock.patch.object(migration.harness_state, "read_selection", return_value=("valid", selected)), \
                 mock.patch.object(
                     migration.antigravity_setup,
                     "uninstall",
                     return_value={"removed_files": ["binary", "token"]},
                 ) as uninstall, \
                 mock.patch.object(migration.agy_update_freeze, "remove_hosts_redirect", return_value=True) as unfreeze:
                result = migration.migrate()
            self.assertEqual(result["status"], "removed")
            self.assertEqual(result["selected"], selected)
            self.assertEqual(result["removed_file_count"], 2)
            self.assertEqual(result["failed_steps"], [])
            uninstall.assert_called_once_with()
            unfreeze.assert_called_once_with()

    def test_antigravity_selection_is_untouched(self):
        with mock.patch.object(migration.harness_state, "read_selection", return_value=("valid", "agy")), \
             mock.patch.object(migration.antigravity_setup, "uninstall") as uninstall, \
             mock.patch.object(migration.agy_update_freeze, "remove_hosts_redirect") as unfreeze:
            result = migration.migrate()
        self.assertEqual(result, {"status": "skipped", "reason": "antigravity_selected"})
        uninstall.assert_not_called()
        unfreeze.assert_not_called()

    def test_missing_and_invalid_selection_are_untouched(self):
        for state in ("missing", "invalid"):
            with self.subTest(state=state), \
                 mock.patch.object(migration.harness_state, "read_selection", return_value=(state, None)), \
                 mock.patch.object(migration.antigravity_setup, "uninstall") as uninstall, \
                 mock.patch.object(migration.agy_update_freeze, "remove_hosts_redirect") as unfreeze:
                result = migration.migrate()
            self.assertEqual(result, {"status": "skipped", "reason": f"selection_{state}"})
            uninstall.assert_not_called()
            unfreeze.assert_not_called()

    def test_selection_read_failure_cannot_reach_destructive_calls(self):
        with mock.patch.object(migration.harness_state, "read_selection", side_effect=OSError("unreadable")), \
             mock.patch.object(migration.antigravity_setup, "uninstall") as uninstall, \
             mock.patch.object(migration.agy_update_freeze, "remove_hosts_redirect") as unfreeze, \
             self.assertRaises(OSError):
            migration.migrate()
        uninstall.assert_not_called()
        unfreeze.assert_not_called()

    def test_real_uninstall_removes_binary_and_auth_but_retains_brain_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "agy-home"
            bin_dir = home / "bin"
            binary = bin_dir / "agy"
            token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            marker = home / ".gemini" / "eha-auth-ok"
            brain = home / ".gemini" / "antigravity-cli" / "brain" / "memory.md"
            flag = root / "selected_harness"
            bin_dir.mkdir(parents=True)
            token.parent.mkdir(parents=True)
            marker.parent.mkdir(parents=True, exist_ok=True)
            brain.parent.mkdir(parents=True)
            binary.write_text("binary", encoding="utf-8")
            binary.chmod(0o755)
            token.write_text("token", encoding="utf-8")
            marker.write_text("ok", encoding="utf-8")
            brain.write_text("retained", encoding="utf-8")
            flag.write_text("codex\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_HARNESS_FLAG_FILE": str(flag),
                    "EHA_ANTIGRAVITY_HOME": str(home),
                    "EHA_ANTIGRAVITY_BIN_DIR": str(bin_dir),
                    "EHA_ANTIGRAVITY_BIN": str(binary),
                },
                clear=False,
            ), mock.patch.object(
                migration.agy_update_freeze,
                "remove_hosts_redirect",
                return_value=False,
            ):
                result = migration.migrate()
            self.assertEqual(result["status"], "removed")
            self.assertFalse(binary.exists())
            self.assertFalse(token.exists())
            self.assertFalse(marker.exists())
            self.assertEqual(brain.read_text(encoding="utf-8"), "retained")

    def test_partial_cleanup_reports_stage_and_next_run_converges(self):
        with mock.patch.object(
            migration.harness_state,
            "read_selection",
            return_value=("valid", "codex"),
        ), mock.patch.object(
            migration.antigravity_setup,
            "uninstall",
            side_effect=({"removed_files": ["binary", "token"]}, {"removed_files": []}),
        ), mock.patch.object(
            migration.agy_update_freeze,
            "remove_hosts_redirect",
            side_effect=(OSError("read-only"), True),
        ):
            first = migration.migrate()
            second = migration.migrate()

        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["removed_file_count"], 2)
        self.assertEqual(first["failed_steps"], ["freeze_redirect:OSError"])
        self.assertEqual(second["status"], "removed")
        self.assertEqual(second["removed_file_count"], 0)
        self.assertTrue(second["redirect_removed"])
        self.assertEqual(second["failed_steps"], [])

    def test_run_sh_wires_cleanup_before_freeze_and_removes_global_audio_fallback_config(self):
        source = (ROOT / "embodied_ha" / "run.sh").read_text(encoding="utf-8")
        cleanup_index = source.index('migrate_remove_unused_antigravity.py')
        freeze_index = source.index('agy_update_freeze.py" add')
        self.assertLess(cleanup_index, freeze_index)
        self.assertNotIn("write_mcp_config", source)
        self.assertNotIn("音声解析セッション", source)


if __name__ == "__main__":
    unittest.main()
