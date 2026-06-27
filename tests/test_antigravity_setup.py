import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import antigravity_setup  # noqa: E402


class AntigravitySetupTests(unittest.TestCase):
    def test_default_paths_use_data_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(antigravity_setup.home_dir(), "/data/")
            self.assertEqual(antigravity_setup.bin_dir(), "/data/bin")
            self.assertEqual(antigravity_setup.binary_path(), "/data/bin/agy")
            self.assertEqual(
                antigravity_setup.oauth_token_path(),
                "/data/.gemini/antigravity-cli/antigravity-oauth-token",
            )

    def test_env_overrides_are_respected(self):
        with mock.patch.dict(
            os.environ,
            {
                "EHA_ANTIGRAVITY_HOME": "/tmp/agy-home",
                "EHA_ANTIGRAVITY_BIN_DIR": "/tmp/agy-bin",
                "EHA_ANTIGRAVITY_BIN": "/tmp/agy-bin/agy-custom",
            },
            clear=False,
        ):
            self.assertEqual(antigravity_setup.home_dir(), "/tmp/agy-home")
            self.assertEqual(antigravity_setup.bin_dir(), "/tmp/agy-bin")
            self.assertEqual(antigravity_setup.binary_path(), "/tmp/agy-bin/agy-custom")

    def test_install_and_auth_state_follow_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bin_dir = home / "bin"
            bin_dir.mkdir(parents=True)
            binary = bin_dir / "agy"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            token_path = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token_path.parent.mkdir(parents=True)
            token_path.write_text("token", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {
                    "EHA_ANTIGRAVITY_HOME": str(home),
                    "EHA_ANTIGRAVITY_BIN_DIR": str(bin_dir),
                },
                clear=False,
            ):
                self.assertTrue(antigravity_setup.is_installed())
                self.assertTrue(antigravity_setup.is_authenticated())
                state = antigravity_setup.state()
                self.assertTrue(state["installed"])
                self.assertTrue(state["authenticated"])
                self.assertEqual(state["home_dir"], str(home))
                self.assertEqual(state["bin_dir"], str(bin_dir))
                self.assertEqual(state["binary_path"], str(binary))
                self.assertEqual(state["oauth_token_path"], str(token_path))

    def test_uninstall_deletes_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bin_dir = home / "bin"
            bin_dir.mkdir(parents=True)
            binary = bin_dir / "agy"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            with mock.patch.dict(
                os.environ,
                {
                    "EHA_ANTIGRAVITY_HOME": str(home),
                    "EHA_ANTIGRAVITY_BIN_DIR": str(bin_dir),
                },
                clear=False,
            ):
                self.assertTrue(antigravity_setup.is_installed())
                self.assertTrue(antigravity_setup.uninstall()["removed_files"])
                self.assertFalse(antigravity_setup.is_installed())
                self.assertEqual(antigravity_setup.uninstall()["removed_files"], [])

    def test_clear_auth_deletes_token_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            token_path = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text("token", encoding="utf-8")
            marker_path = home / ".gemini" / "eha-auth-ok"
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text("ok", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {
                    "EHA_ANTIGRAVITY_HOME": str(home),
                },
                clear=False,
            ):
                self.assertTrue(antigravity_setup.is_authenticated())
                self.assertTrue(antigravity_setup.clear_auth()["removed_files"])
                self.assertFalse(antigravity_setup.is_authenticated())
                self.assertEqual(antigravity_setup.clear_auth()["removed_files"], [])


if __name__ == "__main__":
    unittest.main()
