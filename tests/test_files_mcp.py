"""files-mcp.py の read_file ツールのテスト。

policy-read + secure-read の契約:
  - 通常ファイルはそのまま読み、認証・機密パスは共通 policy で拒否。
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

    def test_relative_path_is_rejected(self):
        text, err = self._call("relative.txt")
        self.assertTrue(err)
        self.assertIn("絶対パス", text)

    def test_well_known_sensitive_paths_are_rejected(self):
        for path in (
            "/config/secrets.yaml",
            "/config/.storage/auth",
            "/config/.ssh/id_ed25519",
            "/data/options.json",
            "/data/claude-home/.credentials.json",
            "/data/codex-home/auth.json",
            "/data/.gemini/antigravity-cli/token",
            "/config/embodied-ha/github_app.pem",
        ):
            with self.subTest(path=path):
                _text, err = self._call(path)
                self.assertTrue(err)

    def test_intermediate_symlink_cannot_alias_sensitive_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_dir = root / ".ssh"
            secret_dir.mkdir()
            (secret_dir / "key").write_text("secret", encoding="utf-8")
            alias = root / "alias"
            alias.symlink_to(secret_dir, target_is_directory=True)
            text, err = self._call(str(alias / "key"))
            self.assertTrue(err)
            self.assertIn("認証・機密", text)

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


class FilesMcpViewImageTests(unittest.TestCase):
    """view_image: 画像は画像として返し、機密境界は read_file と同一に保つ。"""

    PNG = bytes.fromhex("89504e470d0a1a0a") + b"rest-of-png"
    JPEG = b"\xff\xd8\xff\xe0" + b"rest-of-jpeg"
    GIF = b"GIF89a" + b"rest-of-gif"
    WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"rest"

    def setUp(self):
        self.mod = load_files_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _call(self, path):
        out = self.mod.view_image({"path": path})
        if isinstance(out, tuple):
            return out
        return out, False

    def _write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return str(path)

    def test_returns_each_supported_format_as_an_image_block(self):
        import base64

        for name, data, mime in (
            ("a.png", self.PNG, "image/png"),
            ("a.jpg", self.JPEG, "image/jpeg"),
            ("a.gif", self.GIF, "image/gif"),
            ("a.webp", self.WEBP, "image/webp"),
        ):
            with self.subTest(name=name):
                content, is_error = self._call(self._write(name, data))
                self.assertFalse(is_error)
                self.assertEqual(content[0]["type"], "image")
                self.assertEqual(content[0]["mimeType"], mime)
                self.assertEqual(base64.b64decode(content[0]["data"]), data)

    def test_format_is_decided_by_content_not_extension(self):
        # 拡張子を信じると、名前を変えただけの非画像を画像として返してしまう。
        content, is_error = self._call(self._write("liar.png", b"just text, not an image"))
        self.assertTrue(is_error)
        self.assertIn("画像として認識できません", content[0]["text"])

    def test_a_png_named_as_text_is_still_served(self):
        content, is_error = self._call(self._write("actually.txt", self.PNG))
        self.assertFalse(is_error)
        self.assertEqual(content[0]["mimeType"], "image/png")

    def test_oversized_image_is_refused_not_truncated(self):
        # 切り詰めた画像は壊れたバイト列にしかならないので、拒否する方が正しい。
        self.mod.MAX_IMAGE_BYTES = 32
        content, is_error = self._call(self._write("big.png", self.PNG + b"x" * 128))
        self.assertTrue(is_error)
        self.assertIn("大きすぎます", content[0]["text"])

    def test_secret_paths_are_refused_like_read_file(self):
        for name in ("secrets.yaml", "key.pem"):
            with self.subTest(name=name):
                content, is_error = self._call(self._write(name, self.PNG))
                self.assertTrue(is_error)
                self.assertTrue(content[0]["text"].startswith("view_image: "))

    def test_relative_path_and_missing_file_are_refused(self):
        content, is_error = self._call("relative/a.png")
        self.assertTrue(is_error)
        self.assertIn("絶対パス", content[0]["text"])
        content, is_error = self._call(str(self.dir / "nope.png"))
        self.assertTrue(is_error)
        self.assertIn("見つかりません", content[0]["text"])

    def test_directory_and_symlink_are_refused(self):
        content, is_error = self._call(str(self.dir))
        self.assertTrue(is_error)
        target = self._write("real.png", self.PNG)
        link = self.dir / "link.png"
        link.symlink_to(target)
        content, is_error = self._call(str(link))
        self.assertTrue(is_error)
        self.assertIn("symlink", content[0]["text"])
