"""Step4 §13.2: uninstall エンドポイントは選択中(effective)ハーネスを拒否する。

grandfather 個体(フラグ missing → effective=claude)の claude uninstall も拒否されること
(=稼働中ランタイムの足元を消させない)を含めて固定する。
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
sys.path.insert(0, str(ROOT / "embodied_ha" / "web"))
os.environ.setdefault("HA_URL", "http://homeassistant.invalid")

from web import server  # noqa: E402


class UninstallGuardTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.flag = Path(self._dir.name) / "selected_harness"
        self.enterContext(mock.patch.dict(
            os.environ, {"EHA_HARNESS_FLAG_FILE": str(self.flag)}, clear=False))

    def _select(self, harness):
        self.flag.write_text(f"{harness}\n", encoding="utf-8")

    def _handler(self):
        h = object.__new__(server.Handler)
        h.send_json = mock.Mock()
        return h

    # --- guard predicate ---------------------------------------------------------

    def test_blocks_effective_harness(self):
        self._select("codex")
        h = self._handler()
        self.assertTrue(h._uninstall_blocked_for_effective("codex"))
        self.assertEqual(h.send_json.call_args.args[1], 409)

    def test_allows_non_effective_harness(self):
        self._select("codex")
        h = self._handler()
        self.assertFalse(h._uninstall_blocked_for_effective("claude"))
        self.assertFalse(h._uninstall_blocked_for_effective("agy"))
        h.send_json.assert_not_called()

    def test_grandfather_missing_flag_blocks_claude(self):
        # No flag written → snapshot effective resolves to claude; claude uninstall blocked.
        h = self._handler()
        self.assertTrue(h._uninstall_blocked_for_effective("claude"))
        self.assertFalse(h._uninstall_blocked_for_effective("codex"))

    # --- wired into the POST handlers --------------------------------------------

    def _post(self, path):
        h = object.__new__(server.Handler)
        h.path = path
        h.client_address = ("127.0.0.1", 0)
        h.headers = {"Content-Length": "0"}
        h.rfile = io.BytesIO(b"")
        h.wfile = io.BytesIO()
        h.send_json = mock.Mock()
        return h

    def test_codex_uninstall_endpoint_blocked_when_selected(self):
        self._select("codex")
        with mock.patch.dict(os.environ, {"EHA_SETUP_GUARD": "off"}, clear=False), \
             mock.patch.object(server, "codex_setup") as codex_setup:
            h = self._post("/api/setup/codex/uninstall")
            h.do_POST()
            self.assertEqual(h.send_json.call_args.args[1], 409)
            codex_setup.uninstall.assert_not_called()

    def test_claude_uninstall_endpoint_allowed_when_codex_selected(self):
        self._select("codex")
        with mock.patch.dict(os.environ, {"EHA_SETUP_GUARD": "off"}, clear=False), \
             mock.patch.object(server, "claude_setup") as claude_setup:
            claude_setup.uninstall.return_value = {"removed": []}
            h = self._post("/api/setup/claude/uninstall")
            h.do_POST()
            claude_setup.uninstall.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
