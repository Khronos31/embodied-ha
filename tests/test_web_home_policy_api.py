import importlib.util
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "embodied_ha" / "web" / "server.py"
DEFAULT_HOME_POLICY = ROOT / "embodied_ha" / "home_policy.md"


def load_server_module(home_policy_file: str):
    module_name = "web_server_home_policy_test"
    sys.modules.pop(module_name, None)
    env = {
        "HA_URL": "http://127.0.0.1:8123",
        "SUPERVISOR_TOKEN": "test-token",
        "EHA_HOME_POLICY_FILE": home_policy_file,
        "EHA_DATA_DIR": str(ROOT / "embodied_ha"),
        "EHA_LOG_DIR": tempfile.gettempdir(),
    }
    original = os.environ.copy()
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(original)


class HomePolicyApiTests(unittest.TestCase):
    def _start_server(self, module):
        server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_get_and_put_home_policy_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home_policy_path = os.path.join(tmpdir, "home_policy.md")
            module = load_server_module(home_policy_path)
            server = self._start_server(module)
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/home-policy") as res:
                    default_body = res.read().decode("utf-8")
                self.assertEqual(default_body, DEFAULT_HOME_POLICY.read_text(encoding="utf-8"))

                new_policy = """# テスト用ポリシー
- 在宅中はエアコンを使ってよい
- 深夜に音は出さない
"""
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/home-policy",
                    data=new_policy.encode("utf-8"),
                    method="PUT",
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
                with urllib.request.urlopen(req) as res:
                    self.assertEqual(res.status, 200)
                    self.assertEqual(res.read().decode("utf-8"), '{"ok": true}')

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/home-policy") as res:
                    updated_body = res.read().decode("utf-8")
                self.assertEqual(updated_body, new_policy)
                self.assertEqual(Path(home_policy_path).read_text(encoding="utf-8"), new_policy)
            finally:
                server.shutdown()
                server.server_close()

    def test_empty_home_policy_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home_policy_path = os.path.join(tmpdir, "home_policy.md")
            module = load_server_module(home_policy_path)
            server = self._start_server(module)
            try:
                port = server.server_address[1]
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/home-policy",
                    data=b"",
                    method="PUT",
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
                with urllib.request.urlopen(req) as res:
                    self.assertEqual(res.status, 200)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/home-policy") as res:
                    self.assertEqual(res.read().decode("utf-8"), "")
                self.assertEqual(Path(home_policy_path).read_text(encoding="utf-8"), "")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
