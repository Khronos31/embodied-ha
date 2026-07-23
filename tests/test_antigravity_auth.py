import os
import queue
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha" / "web"))

os.environ.setdefault("HA_URL", "http://example.invalid")
os.environ.setdefault("SUPERVISOR_TOKEN", "test-token")

import server  # noqa: E402


class AntigravityAuthTests(unittest.TestCase):
    def test_login_autoresponder_drives_expected_steps(self):
        q = queue.Queue()
        state = {"sent_method": False, "url_found": False, "sent_code_wait": False}

        with mock.patch.object(server.os, "write") as write:
            server._antigravity_login_handle_line("1. Google OAuth", state, 11, q)
            # raw モード TUI では Enter は CR(\r)。LF(\n)だと選択が登録されず agy がハングする
            # (2026-07-23 実機確認・ゆの指摘)。option 1 はハイライト済なので CR で確定。
            write.assert_called_once_with(11, b"\r")
            self.assertTrue(state["sent_method"])

            server._antigravity_login_handle_line(
                "Open https://example.com/auth now",
                state,
                11,
                q,
            )
            self.assertEqual(q.get_nowait(), ("url", {"url": "https://example.com/auth"}))
            self.assertTrue(state["url_found"])

            server._antigravity_login_handle_line("Enter authorization code:", state, 11, q)
            self.assertEqual(q.get_nowait(), ("waiting_code", {}))
            self.assertTrue(state["sent_code_wait"])

            server._antigravity_login_handle_line("color scheme", state, 11, q)
            server._antigravity_login_handle_line("Terms of Service", state, 11, q)
            server._antigravity_login_handle_line("Do you trust this workspace?", state, 11, q)

            write.assert_has_calls(
                [
                    mock.call(11, b"\r"),
                    mock.call(11, b"\r"),
                    mock.call(11, b"\x1b[B\x1b[C\r"),
                    mock.call(11, b"\r"),
                ],
                any_order=False,
            )
            self.assertTrue(q.empty())

    def test_login_autoresponder_ignores_lines_before_url(self):
        q = queue.Queue()
        state = {"sent_method": False, "url_found": False, "sent_code_wait": False}

        with mock.patch.object(server.os, "write") as write:
            server._antigravity_login_handle_line("some unrelated text", state, 11, q)
            write.assert_not_called()
            self.assertTrue(q.empty())
            self.assertFalse(state["url_found"])


if __name__ == "__main__":
    unittest.main()
