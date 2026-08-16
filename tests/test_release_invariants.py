"""`scripts/check_release_invariants.py` の契約テスト。

検査対象の不変条件はリリース手順書側にあり、ここではその機械検査が
「落とすべきものを落とし、通すべきものを通す」ことだけを見る。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_release_invariants as chk  # noqa: E402

CONFIG_TEMPLATE = 'name: Embodied HA\nversion: "{version}"\nslug: embodied_ha\n'


class ReleaseInvariantTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.repo])
        # 実機のgit設定を持ち込まない（テストは走らせた機械に依存しない）。
        self.env = dict(
            os.environ,
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_SYSTEM="/dev/null",
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        self._git("init", "-q", "-b", "main")

    def _git(self, *args):
        result = subprocess.run(
            ["git", "-C", self.repo, *args],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _write(self, relative: str, content: str):
        path = Path(self.repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _commit(self, message: str, *, version: str | None = None,
                addon_body: str | None = None, other_body: str | None = None) -> str:
        if version is not None:
            self._write("embodied_ha/config.yaml", CONFIG_TEMPLATE.format(version=version))
        if addon_body is not None:
            self._write("embodied_ha/daemon.py", addon_body)
        if other_body is not None:
            self._write("README.md", other_body)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _released(self, version: str, body: str) -> str:
        return self._commit(f"release {version}", version=version, addon_body=body)

    def test_addon_change_without_version_bump_is_rejected(self):
        base = self._released("1.0.0", "first")
        head = self._commit("hotfix", addon_body="second")
        errors, _ = chk.check(self.repo, base, head)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("version が 1.0.0 のまま", errors[0])

    def test_addon_change_with_version_bump_is_accepted(self):
        base = self._released("1.0.0", "first")
        head = self._released("1.0.1", "second")
        errors, _ = chk.check(self.repo, base, head)
        self.assertEqual(errors, [])

    def test_change_outside_the_addon_needs_no_version_bump(self):
        # CI設定やドキュメントはアドオンイメージに入らないので版番号を動かさない。
        base = self._released("1.0.0", "first")
        head = self._commit("docs", other_body="hello")
        errors, _ = chk.check(self.repo, base, head)
        self.assertEqual(errors, [])

    def test_reusing_a_published_version_is_rejected(self):
        self._released("1.0.0", "first")
        self._released("1.0.1", "second")
        base = self._released("1.0.2", "third")
        head = self._released("1.0.1", "fourth")
        errors, _ = chk.check(self.repo, base, head)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("既にmainへ公開済み", errors[0])

    def test_republishing_the_same_number_with_different_content_is_rejected(self):
        """2026-07-26に実際に起きた形: 同じ2.0.8が別内容で二度公開された。"""
        base = self._released("2.0.8", "first")
        head = self._commit("republish", addon_body="different")
        errors, _ = chk.check(self.repo, base, head)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("同じ版番号で中身が違う", errors[0])

    def test_missing_config_at_head_is_an_error_not_a_pass(self):
        base = self._released("1.0.0", "first")
        self._git("rm", "-q", "embodied_ha/config.yaml")
        self._git("commit", "-q", "-m", "drop manifest")
        head = self._git("rev-parse", "HEAD")
        with self.assertRaises(chk.CheckError):
            chk.check(self.repo, base, head)

    def test_lower_version_is_reported_but_not_fatal(self):
        self._released("1.0.0", "first")
        base = self._released("2.0.0", "second")
        head = self._released("1.9.0", "third")
        errors, notes = chk.check(self.repo, base, head)
        self.assertEqual(errors, [])
        self.assertTrue(any("より大きくない" in note for note in notes), notes)


if __name__ == "__main__":
    unittest.main()
