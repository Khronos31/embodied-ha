import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha" / "web"))
os.environ.setdefault("HA_URL", "http://homeassistant.invalid")

import server  # noqa: E402


class SetupGuardTests(unittest.TestCase):
    # Deliberately independent from server._SETUP_MUTATION_PATHS: adding a
    # dispatch route but forgetting the guard set must fail this test.
    MUTATION_ROUTES = (
        ("GET", "/api/setup/login"),
        ("POST", "/api/setup/login-code"),
        ("GET", "/api/setup/claude/login"),
        ("POST", "/api/setup/claude/login-code"),
        ("POST", "/api/setup/claude/install"),
        ("POST", "/api/setup/claude/uninstall"),
        ("POST", "/api/setup/claude/clear-auth"),
        ("POST", "/api/setup/claude/logout"),
        ("GET", "/api/setup/antigravity/install"),
        ("GET", "/api/setup/antigravity/login"),
        ("POST", "/api/setup/antigravity/input"),
        ("POST", "/api/setup/antigravity/login-code"),
        ("POST", "/api/setup/antigravity/uninstall"),
        ("POST", "/api/setup/antigravity/clear-auth"),
        ("POST", "/api/setup/antigravity/logout"),
        # Binary swaps, plus the status read whose check=1 form reaches the
        # vendor (for agy that also lifts the update freeze).
        ("GET", "/api/setup/antigravity/update"),
        ("GET", "/api/setup/antigravity/rollback"),
        ("GET", "/api/setup/antigravity/update-status"),
        ("GET", "/api/setup/claude/update"),
        ("GET", "/api/setup/claude/rollback"),
        ("GET", "/api/setup/claude/update-status"),
        ("GET", "/api/setup/codex/update"),
        ("GET", "/api/setup/codex/rollback"),
        ("GET", "/api/setup/codex/update-status"),
        ("POST", "/api/setup/codex/install"),
        ("POST", "/api/setup/codex/login"),
        ("POST", "/api/setup/codex/uninstall"),
        ("POST", "/api/setup/codex/clear-auth"),
        ("POST", "/api/setup/codex/logout"),
        ("POST", "/api/setup/terms"),
    )

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()

    def _request(self, method, path):
        data = None if method == "GET" else b"{}"
        return urllib.request.urlopen(
            urllib.request.Request(self.base_url + path, data=data, method=method), timeout=3
        )

    def test_loopback_rejects_every_setup_mutation_alias(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for method, path in self.MUTATION_ROUTES:
                with self.subTest(method=method, path=path), self.assertRaises(urllib.error.HTTPError) as raised:
                    self._request(method, path)
                self.assertEqual(raised.exception.code, 403)
                self.assertEqual(
                    json.loads(raised.exception.read()), {"error": server._SETUP_GUARD_ERROR}
                )

    def test_frontend_install_methods_match_backend_routing(self):
        """app.js の install method 宣言が backend の verb と一致すること。

        2026-07-23 の回帰ガード: app.js が claude install を GET と宣言していたが backend は
        POST(do_POST + MUTATION_ROUTES)で dispatch するため、GET が 404 へフォールスルーし、
        ピッカーが install handler に到達しないまま汎用の「インストールに失敗しました」を出していた。
        フロント/バックの method 契約を誰もクロスチェックしていなかったのが原因。
        """
        import re

        app_js = (ROOT / "embodied_ha" / "web" / "app.js").read_text(encoding="utf-8")
        for method, path in self.MUTATION_ROUTES:
            if not path.endswith("/install"):
                continue
            m = re.search(
                r"install:\s*\{\s*method:\s*'(\w+)',\s*url:\s*'" + re.escape(path) + r"'",
                app_js,
            )
            self.assertIsNotNone(m, f"app.js に {path} の install エントリが無い")
            self.assertEqual(
                m.group(1),
                method,
                f"frontend install method for {path} ({m.group(1)}) != backend verb ({method})",
            )

    def test_frontend_binary_update_calls_use_the_backend_verb(self):
        """更新/ロールバックの SSE 呼び出しが GET で宣言されていること。

        install で一度起きた失敗（フロントが POST と宣言し、backend の GET dispatch に
        届かず 404 へフォールスルーして汎用エラーだけが出る）を、あとから増えた
        バイナリ差し替え経路でも防ぐ。app.js は経路ごとに harnessStreamSSE を直接
        呼ぶ形なので、宣言テーブルではなく呼び出しそのものを読む。
        """
        import re

        app_js = (ROOT / "embodied_ha" / "web" / "app.js").read_text(encoding="utf-8")
        # パスはハーネス名を差し込むテンプレートリテラルなので、末尾の操作名で照合する。
        # backend は3ハーネスとも同じ verb で dispatch するため、期待値は1つに定まる。
        expected = {method for method, path in self.MUTATION_ROUTES if path.endswith("/update")}
        self.assertEqual(expected, {"GET"}, "backend の update verb が分岐している")
        for operation in ("update", "rollback"):
            calls = re.findall(
                r"harnessStreamSSE\(\s*'(\w+)'\s*,\s*[`'][^`']*/" + operation + r"[`$]",
                app_js,
            )
            self.assertTrue(calls, f"app.js に /{operation} を呼ぶ harnessStreamSSE が無い")
            for method in calls:
                self.assertEqual(
                    method, "GET", f"frontend method for /{operation} ({method}) != backend verb GET"
                )

    def test_frontend_checks_the_vendor_when_the_screen_opens(self):
        """バージョン画面を開いたら最新版を確認しにいくこと。

        当初は逆の契約だった——`?check=1` が agy では更新の凍結を一時解除するため、
        押されるまで問い合わせない設計にしていた。ゆの指摘で覆した（2026-08-13）:
        「バージョン管理の画面を開いておいて『まだ確認していません』と出るのは、
        こちらの都合を利用者に説明しているだけ」。**決定が変わったのでテストの意図ごと
        差し替えている**（実装に合わせて期待値を緩めたのではない）。

        問い合わせ口は1箇所に保つ。増えると、どこから外へ出るのかを追えなくなる。
        """
        app_js = (ROOT / "embodied_ha" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("checkVendor = true", app_js)
        self.assertEqual(app_js.count("update-status?check=1"), 1)

    def test_only_ingress_source_is_allowed_unless_overridden_or_disabled(self):
        handler = object.__new__(server.Handler)
        handler.send_json = mock.Mock()

        with mock.patch.dict(os.environ, {}, clear=True):
            handler.client_address = ("172.30.32.2", 12345)
            self.assertFalse(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))
            self.assertTrue(server.setup_guard(handler.client_address))

            handler.client_address = ("172.30.33.7", 12345)
            self.assertTrue(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))

            handler.client_address = ("127.0.0.1", 12345)
            self.assertTrue(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))
            self.assertEqual(handler.send_json.call_count, 2)

        handler.send_json.reset_mock()
        with mock.patch.dict(os.environ, {"EHA_INGRESS_SOURCE": "10.0.0.1, 2001:db8::1"}, clear=True):
            handler.client_address = ("10.0.0.1", 12345)
            self.assertFalse(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))
            handler.client_address = ("2001:db8::1", 12345)
            self.assertTrue(server.setup_guard(handler.client_address))
            handler.client_address = ("172.30.32.2", 12345)
            self.assertTrue(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))

        handler.send_json.reset_mock()
        with mock.patch.dict(os.environ, {"EHA_SETUP_GUARD": "off"}, clear=True):
            handler.client_address = ("127.0.0.1", 12345)
            self.assertFalse(handler._block_loopback_setup_mutation("/api/setup/codex/uninstall"))
            self.assertTrue(server.setup_guard(handler.client_address))
            handler.send_json.assert_not_called()

    def test_status_routes_are_not_guarded(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.send_json = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=True):
            for path in (
                "/api/setup/status", "/api/setup/antigravity/status",
                "/api/setup/codex/status", "/api/setup/claude/status",
            ):
                self.assertFalse(handler._block_loopback_setup_mutation(path))
        handler.send_json.assert_not_called()


class AntigravityInstallEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.harness_flag_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.harness_flag_dir)
        self.harness_flag_env = mock.patch.dict(
            os.environ,
            {"EHA_HARNESS_FLAG_FILE": os.path.join(self.harness_flag_dir, "selected_harness")},
            clear=False,
        )
        self.harness_flag_env.start()
        self.addCleanup(self.harness_flag_env.stop)

    def test_install_script_child_env_excludes_secrets(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = iter(())

            def wait(self):
                return 0

            def poll(self):
                return 0

            def terminate(self):
                pass

        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        handler = object.__new__(server.Handler)
        handler.send_response = lambda *_args: None
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        process = FakeProcess()
        with mock.patch.dict(os.environ, {
            "SUPERVISOR_TOKEN": "supervisor-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
        }, clear=False), mock.patch.object(
            server.antigravity_setup, "fetch_install_script", return_value="exit 0\n"
        ), mock.patch.object(server.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
            server.threading, "Thread", InlineThread
        ):
            handler._serve_setup_antigravity_install()

        env = popen.call_args.kwargs["env"]
        self.assertEqual(set(env), {"HOME", "PATH", "LANG"})
        self.assertNotIn("SUPERVISOR_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)


if __name__ == "__main__":
    unittest.main()
