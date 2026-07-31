"""ファイル読み取り能力のハーネス別配線のテスト。

守っている性質は「**3ハーネスのどれで動いていても、ループがファイルを読める**」。

以前は loop がどのハーネスでも Read を要求していなかったが、実態は揃っていなかった:
Claude はnative Read、CodexとAntigravityは同じdeny policyを持つfiles MCPを使う。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import file_read_capability  # noqa: E402


class GrantFileReadTests(unittest.TestCase):
    def test_claude_gets_read_builtin_and_no_files_server(self):
        allowed, servers = file_read_capability.grant_file_read(
            "mcp__memory__recall", ("memory",), "claude"
        )
        self.assertIn("Read", allowed.split(","))
        self.assertNotIn("files", servers)
        self.assertNotIn("mcp__files__read_file", allowed.split(","))

    def test_agy_gets_files_mcp_and_not_native_read(self):
        allowed, servers = file_read_capability.grant_file_read(
            "mcp__memory__recall", ("memory",), "agy"
        )
        self.assertNotIn("Read", allowed.split(","))
        self.assertIn("mcp__files__read_file", allowed.split(","))
        self.assertEqual(servers[0], "files")

    def test_codex_gets_files_mcp_at_the_front(self):
        allowed, servers = file_read_capability.grant_file_read(
            "mcp__memory__recall", ("memory",), "codex"
        )
        self.assertIn("mcp__files__read_file", allowed.split(","))
        self.assertEqual(servers[0], "files", "codex のtool選別で落ちないよう先頭に置く")
        self.assertIn("memory", servers)

    def test_idempotent(self):
        """呼び出し側が二重に呼んでも増殖しない。"""
        first = file_read_capability.grant_file_read("mcp__memory__recall", ("memory",), "codex")
        second = file_read_capability.grant_file_read(first[0], first[1], "codex")
        self.assertEqual(first, second)
        self.assertEqual(first[0].split(",").count("Read"), 0)
        self.assertEqual(first[0].split(",").count("mcp__files__read_file"), 1)

    def test_existing_read_is_not_duplicated(self):
        allowed, _ = file_read_capability.grant_file_read("Read,mcp__memory__recall", ("memory",), "claude")
        self.assertEqual(allowed.split(",").count("Read"), 1)

    def test_substring_does_not_count_as_present(self):
        """`ReadFile` のような別ツールがあっても `Read` が入っていると誤判定しない。"""
        allowed, _ = file_read_capability.grant_file_read("ReadSomethingElse", (), "claude")
        self.assertIn("Read", allowed.split(","))

    def test_unknown_harness_fails_closed_without_read(self):
        for harness in ("", "  ", "unknown"):
            with self.subTest(harness=harness):
                allowed, servers = file_read_capability.grant_file_read("mcp__memory__recall", ("memory",), harness)
                self.assertNotIn("Read", allowed.split(","))
                self.assertNotIn("files", servers)

    def test_empty_allowed_tools(self):
        allowed, _ = file_read_capability.grant_file_read("", (), "claude")
        self.assertEqual(allowed, "Read")


if __name__ == "__main__":
    unittest.main()
