"""`scripts/check_repo_hygiene.py` の契約テスト。

⚠️ このファイル自身も検査対象になるため、秘密のサンプルは**文字列を組み立てて**書く。
リテラルで置くと、自分の検査に自分で引っかかる。
"""
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_repo_hygiene as hygiene  # noqa: E402

ANTHROPIC_KEY = "sk-" + "ant-" + "A" * 32
JWT = "ey" + "J" + "a" * 20 + ".ey" + "J" + "b" * 20
REAL_PEM = "-----BEGIN PRIVATE KEY-----\n" + ("QUJDREVG" * 20) + "\n-----END PRIVATE KEY-----"
DUMMY_PEM = "-----BEGIN PRIVATE KEY-----\\ntest\\n-----END PRIVATE KEY-----"


class RepoHygieneTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.repo])
        self.env = dict(
            os.environ,
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_SYSTEM="/dev/null",
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        subprocess.run(["git", "-C", self.repo, "init", "-q"], env=self.env, check=True)

    def _track(self, relative: str, content: str = "x\n"):
        path = Path(self.repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "-C", self.repo, "add", "-f", relative], env=self.env, check=True
        )

    def _paths(self):
        return hygiene.tracked_files(self.repo)

    def _problems(self):
        paths = self._paths()
        persona, _ = hygiene.persona_violations(self.repo, paths)
        return (
            hygiene.path_violations(paths)
            + hygiene.content_violations(self.repo, paths)
            + persona
        )

    def test_personal_data_directory_is_rejected(self):
        self._track("personal_data/preferences.json", "{}\n")
        self.assertTrue(any("personal_data" in p for p in self._problems()))

    def test_real_personal_config_is_rejected_but_the_example_is_not(self):
        self._track("preferences.json", "{}\n")
        self.assertEqual(len(self._problems()), 1, self._problems())

        other = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", other])
        subprocess.run(["git", "-C", other, "init", "-q"], env=self.env, check=True)
        example = Path(other) / "preferences.json.example"
        example.write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "-C", other, "add", "-f", "preferences.json.example"],
                       env=self.env, check=True)
        self.assertEqual(hygiene.path_violations(hygiene.tracked_files(other)), [])

    def test_key_files_are_rejected(self):
        self._track("embodied_ha/github_app.pem", "x\n")
        self.assertTrue(any("鍵ファイル" in p for p in self._problems()))

    def test_api_keys_and_tokens_are_detected(self):
        self._track("embodied_ha/leak.py", f'KEY = "{ANTHROPIC_KEY}"\n')
        self._track("embodied_ha/token.py", f'TOKEN = "{JWT}"\n')
        problems = self._problems()
        self.assertEqual(len(problems), 2, problems)

    def test_real_private_key_is_rejected_and_a_test_dummy_is_not(self):
        self._track("embodied_ha/real.txt", REAL_PEM)
        self.assertTrue(any("秘密鍵" in p for p in self._problems()))

        other = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", other])
        subprocess.run(["git", "-C", other, "init", "-q"], env=self.env, check=True)
        dummy = Path(other) / "tests/test_api.py"
        dummy.parent.mkdir(parents=True)
        dummy.write_text(f'body = "{DUMMY_PEM}"\n', encoding="utf-8")
        subprocess.run(["git", "-C", other, "add", "-f", "tests/test_api.py"],
                       env=self.env, check=True)
        paths = hygiene.tracked_files(other)
        self.assertEqual(hygiene.content_violations(other, paths), [])

    def test_persona_names_are_checked_only_when_the_local_list_exists(self):
        self._track("tests/test_thing.py", 'label = "山田太郎"\n')
        problems, checked = hygiene.persona_violations(self.repo, self._paths())
        self.assertFalse(checked)
        self.assertEqual(problems, [])

        self._track("tests/persona_names.local", "# 実名の一覧\n山田太郎\n")
        problems, checked = hygiene.persona_violations(self.repo, self._paths())
        self.assertTrue(checked)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tests/test_thing.py", problems[0])

    def test_the_name_list_can_live_outside_the_worktree(self):
        """worktreeごとに置き直さずに済むこと（新しい作業コピーだけ無防備、を防ぐ）。"""
        self._track("tests/test_thing.py", 'label = "山田太郎"\n')
        shared = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(shared)])
        (shared / "names.local").write_text("山田太郎\n", encoding="utf-8")

        with unittest.mock.patch.dict(
            os.environ, {hygiene.PERSONA_NAMES_ENV: str(shared / "names.local")}
        ):
            problems, checked = hygiene.persona_violations(self.repo, self._paths())
        self.assertTrue(checked)
        self.assertEqual(len(problems), 1, problems)

    def test_a_clean_repository_passes(self):
        self._track("embodied_ha/daemon.py", "print('hello')\n")
        self._track("preferences.json.example", "{}\n")
        self.assertEqual(self._problems(), [])


if __name__ == "__main__":
    unittest.main()
