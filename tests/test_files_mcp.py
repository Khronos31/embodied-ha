"""files-mcp.py の read_file ツールのテスト。

read-anything + secure-read の契約(2026-07-23・F4/ゆの案):
  - 通常ファイルはそのまま読む(パス制限なし=Claude native Read パリティ)。
  - symlink / ディレクトリ / fifo・デバイス(非通常ファイル)は拒否。
  - size cap 超過は切り詰めて注記。非 UTF-8 は内容を出さず要約のみ。
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "embodied_ha" / "files-mcp.py"
EMBODIED_HA = ROOT / "embodied_ha"
if str(EMBODIED_HA) not in sys.path:
    sys.path.insert(0, str(EMBODIED_HA))


def load_files_module():
    spec = importlib.util.spec_from_file_location("files_mcp_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FilesMcpReadFileTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_files_module()

    def _call(self, path):
        out = self.mod.read_file({"path": path})
        if isinstance(out, tuple):
            content, is_error = out
        else:
            content, is_error = out, False
        return content[0]["text"], is_error

    def test_reads_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.txt"
            p.write_text("hello-世界\n", encoding="utf-8")
            text, err = self._call(str(p))
            self.assertFalse(err)
            self.assertEqual(text, "hello-世界\n")

    def test_empty_path_is_error(self):
        _text, err = self._call("")
        self.assertTrue(err)

    def test_not_found_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            text, err = self._call(str(Path(tmp) / "nope"))
            self.assertTrue(err)
            self.assertIn("見つかりません", text)

    def test_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _text, err = self._call(tmp)
            self.assertTrue(err)

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.txt"
            target.write_text("secret", encoding="utf-8")
            link = Path(tmp) / "link.txt"
            link.symlink_to(target)
            text, err = self._call(str(link))
            self.assertTrue(err)
            self.assertIn("symlink", text)

    def test_fifo_is_rejected_without_hanging(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "pipe"
            os.mkfifo(fifo)
            # O_NONBLOCK により open がブロックせず、fstat で通常ファイルでないと弾く。
            _text, err = self._call(str(fifo))
            self.assertTrue(err)

    def test_non_utf8_is_not_dumped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bin"
            p.write_bytes(b"\xff\xfe\xfd\xfc")  # 無効 UTF-8(NUL なし)→ decode 経路で拒否
            text, err = self._call(str(p))
            self.assertTrue(err)
            self.assertIn("バイナリ", text)

    def test_nul_bytes_rejected_as_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nul.bin"
            p.write_bytes(b"hello\x00world")  # NUL 区切り(environ 相当)は内容を出さない
            text, err = self._call(str(p))
            self.assertTrue(err)
            self.assertIn("NUL", text)

    def test_proc_environ_is_rejected(self):
        # /proc/self/environ はプロセスの環境変数(SUPERVISOR_TOKEN 等)を返すため拒否(Claude Read パリティ)。
        text, err = self._call("/proc/self/environ")
        self.assertTrue(err)
        self.assertIn("仮想ファイルシステム", text)

    def test_sys_is_rejected(self):
        # sysfs のファイルは S_ISREG を通る(NUL も無い)ため、realpath prefix 判定でしか
        # 弾けない。/proc と同じ _DENY_REALPATH_PREFIXES 経路が /sys にも効くことを検証(2026-07-23)。
        candidates = (
            "/sys/kernel/ostype",
            "/sys/devices/system/cpu/online",
            "/sys/class/dmi/id/sys_vendor",
        )
        path = next((p for p in candidates if os.path.exists(p)), None)
        if path is None:
            self.skipTest("読める /sys ファイルが無い環境")
        text, err = self._call(path)
        self.assertTrue(err)
        self.assertIn("仮想ファイルシステム", text)

    def test_unix_socket_is_rejected(self):
        # ソケットは open(O_RDONLY) で ENXIO になるか、開けても S_ISREG を通らず弾かれる。
        # いずれにせよ内容は返さない(fifo と同じ非通常ファイル拒否・2026-07-23)。
        import socket

        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "s.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                srv.bind(sock_path)
                _text, err = self._call(sock_path)
                self.assertTrue(err)
            finally:
                srv.close()

    def test_proc_text_file_rejected_by_realpath_not_only_nul(self):
        # /proc/self/status はテキスト(NUL 無し)。NUL 拒否では捕まらないので realpath-reject を独立にピンする。
        text, err = self._call("/proc/self/status")
        self.assertTrue(err)
        self.assertIn("仮想ファイルシステム", text)

    def test_nul_just_past_cap_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "capnul.bin"
            # cap 直後の 1 byte が NUL。truncation プローブがこれを NUL 判定に含める。
            p.write_bytes(b"a" * self.mod.MAX_READ_BYTES + b"\x00tail")
            text, err = self._call(str(p))
            self.assertTrue(err)
            self.assertIn("NUL", text)

    def test_short_read_returns_full_content(self):
        # os.read が短く返しても(pipe 的 short read)ループで全文を組み立てることを固定。
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.txt"
            payload = "".join(f"line{i}\n" for i in range(200))
            p.write_text(payload, encoding="utf-8")
            real_read = os.read

            def short_read(fd, n):
                return real_read(fd, min(n, 7))  # 1 回最大 7 byte に制限

            with mock.patch.object(self.mod.os, "read", side_effect=short_read):
                text, err = self._call(str(p))
            self.assertFalse(err)
            self.assertEqual(text, payload)

    def test_size_cap_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "big.txt"
            p.write_text("a" * (self.mod.MAX_READ_BYTES + 100), encoding="utf-8")
            text, err = self._call(str(p))
            self.assertFalse(err)
            self.assertIn("切り詰め", text)

    def test_truncation_at_multibyte_boundary_not_misread_as_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "big.txt"
            # cap 境界でマルチバイト文字(あ=3byte)が分断されるよう配置。分断末尾を落として正常デコードする。
            filler = "a" * (self.mod.MAX_READ_BYTES - 1)
            p.write_text(filler + "あ" + "b" * 10, encoding="utf-8")
            text, err = self._call(str(p))
            self.assertFalse(err)
            self.assertIn("切り詰め", text)


if __name__ == "__main__":
    unittest.main()
