import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import agy_update_freeze  # noqa: E402


class AgyUpdateFreezeHostsTests(unittest.TestCase):
    def setUp(self):
        fd, self.hosts = tempfile.mkstemp(prefix="eha-hosts-")
        os.close(fd)
        with open(self.hosts, "w", encoding="utf-8") as f:
            f.write("127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost\n")

    def tearDown(self):
        try:
            os.remove(self.hosts)
        except OSError:
            pass

    def _content(self) -> str:
        with open(self.hosts, encoding="utf-8") as f:
            return f.read()

    def test_add_is_idempotent_and_detected(self):
        self.assertFalse(agy_update_freeze.is_redirect_active(self.hosts))
        self.assertTrue(agy_update_freeze.add_hosts_redirect(self.hosts))
        self.assertTrue(agy_update_freeze.is_redirect_active(self.hosts))
        # 2回目は変更なし（冪等）
        self.assertFalse(agy_update_freeze.add_hosts_redirect(self.hosts))
        # マーカー行がちょうど1本
        marker_lines = [ln for ln in self._content().splitlines() if agy_update_freeze.MARKER in ln]
        self.assertEqual(len(marker_lines), 1)
        self.assertIn(agy_update_freeze.UPDATE_HOST, marker_lines[0])
        self.assertIn(agy_update_freeze.REDIRECT_IP, marker_lines[0])

    def test_add_preserves_existing_lines(self):
        agy_update_freeze.add_hosts_redirect(self.hosts)
        content = self._content()
        self.assertIn("127.0.0.1\tlocalhost", content)
        self.assertIn("::1\tlocalhost ip6-localhost", content)

    def test_remove_only_strips_marker_lines(self):
        agy_update_freeze.add_hosts_redirect(self.hosts)
        self.assertTrue(agy_update_freeze.remove_hosts_redirect(self.hosts))
        self.assertFalse(agy_update_freeze.is_redirect_active(self.hosts))
        content = self._content()
        self.assertIn("127.0.0.1\tlocalhost", content)
        self.assertIn("::1\tlocalhost ip6-localhost", content)
        self.assertNotIn(agy_update_freeze.UPDATE_HOST, content)

    def test_remove_noop_when_absent(self):
        self.assertFalse(agy_update_freeze.remove_hosts_redirect(self.hosts))

    def test_missing_file_is_safe(self):
        missing = self.hosts + ".nope"
        self.assertFalse(agy_update_freeze.is_redirect_active(missing))
        self.assertFalse(agy_update_freeze.remove_hosts_redirect(missing))

    def test_add_when_no_trailing_newline_does_not_concatenate(self):
        with open(self.hosts, "w", encoding="utf-8") as f:
            f.write("172.30.33.2\tmyhost")  # 末尾改行なし
        self.assertTrue(agy_update_freeze.add_hosts_redirect(self.hosts))
        lines = self._content().splitlines()
        self.assertIn("172.30.33.2\tmyhost", lines)  # 直前行が壊れていない
        marker_lines = [ln for ln in lines if agy_update_freeze.MARKER in ln]
        self.assertEqual(len(marker_lines), 1)
        self.assertTrue(marker_lines[0].startswith(agy_update_freeze.REDIRECT_IP))

    def test_marker_only_comment_not_treated_as_active(self):
        # マーカー文字列を含むが更新ホストを伴わない行は「凍結有効」とみなさない
        with open(self.hosts, "a", encoding="utf-8") as f:
            f.write(f"# unrelated {agy_update_freeze.MARKER} note\n")
        self.assertFalse(agy_update_freeze.is_redirect_active(self.hosts))
        # → add は本物のリダイレクト行を追加できる
        self.assertTrue(agy_update_freeze.add_hosts_redirect(self.hosts))
        self.assertTrue(agy_update_freeze.is_redirect_active(self.hosts))

    def test_reconcile_installed_adds_and_uninstalled_removes(self):
        self.assertTrue(agy_update_freeze.reconcile(True, self.hosts))
        self.assertTrue(agy_update_freeze.is_redirect_active(self.hosts))
        # 冪等: 既に有効なら変更なし
        self.assertFalse(agy_update_freeze.reconcile(True, self.hosts))
        # 未インストール → 撤去
        self.assertTrue(agy_update_freeze.reconcile(False, self.hosts))
        self.assertFalse(agy_update_freeze.is_redirect_active(self.hosts))


if __name__ == "__main__":
    unittest.main()
