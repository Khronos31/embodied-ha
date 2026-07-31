"""ループのプロンプト組み立てでファイル読み取りが配線されることのテスト。

`file_read_capability` の単体テストは別ファイル。ここは **loop がそれを実際に呼んでいる**
ことを、`build_loop_prompt_context()` の戻り値で確かめる（配線忘れを検知する）。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import loop  # noqa: E402


class _Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _fake_run(cmd, **kwargs):
    """外部プロセスは呼ばせない。中身は本テストの関心外なので空を返す。"""
    return _Result(stdout="")


class LoopFileReadWiringTests(unittest.TestCase):
    def _context(self, harness: str, mode: str = "reflect") -> dict:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = {
                "RESIDENT": "ゆの",
                "EHA_LOG_DIR": tmpdir,
                "EHA_DATA_DIR": tmpdir,
                "EHA_TMP_DIR": tmpdir,
                "EHA_TEST_HOUR": "12",
                "EHA_BODY_LOCATION_FILE": str(Path(tmpdir) / "body_location.json"),
            }
            paths = loop.resolve_paths(cfg)
            with patch.object(loop, "_selected_harness", return_value=harness):
                return loop.build_loop_prompt_context(cfg, mode, paths, run=_fake_run)

    def test_claude_loop_gets_read(self):
        ctx = self._context("claude")
        self.assertIn("Read", ctx["allowed_tools"].split(","))
        self.assertNotIn("files", ctx["mcp_servers"])

    def test_agy_loop_gets_read(self):
        ctx = self._context("agy")
        self.assertIn("mcp__files__read_file", ctx["allowed_tools"].split(","))
        self.assertEqual(ctx["mcp_servers"][0], "files")

    def test_codex_loop_gets_files_mcp(self):
        # codex は組み込みのファイル読み取りが無いので files MCP で代替する。
        ctx = self._context("codex")
        self.assertIn("mcp__files__read_file", ctx["allowed_tools"].split(","))
        self.assertEqual(ctx["mcp_servers"][0], "files")

    def test_every_mode_gets_read(self):
        """特定モードだけ読めない、という取りこぼしを防ぐ。"""
        for mode in ("observe", "explore", "reflect", "web", "social"):
            with self.subTest(mode=mode):
                ctx = self._context("claude", mode=mode)
                self.assertIn("Read", ctx["allowed_tools"].split(","))


if __name__ == "__main__":
    unittest.main()
