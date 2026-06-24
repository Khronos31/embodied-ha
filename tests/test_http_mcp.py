import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))


def load_http_mcp_module():
    path = ROOT / "embodied_ha" / "http-mcp.py"
    spec = importlib.util.spec_from_file_location("http_mcp_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Handler(BaseHTTPRequestHandler):
    last_request = {}

    def log_message(self, format, *args):  # noqa: A003
        return

    def _record(self, body: str):
        self.__class__.last_request = {
            "method": self.command,
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body,
        }

    def do_GET(self):
        self._record("")
        response = "GET OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(response.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(response.encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self._record(body)
        response = json.dumps(
            {
                "received": body,
                "content_type": self.headers.get("Content-Type", ""),
            },
            ensure_ascii=False,
        )
        encoded = response.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class _RedirectHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://example.com/")
        self.end_headers()


class HttpMcpTests(unittest.TestCase):
    def setUp(self):
        self.mcp = load_http_mcp_module()

    def _start_server(self, handler_cls=_Handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def _json_text(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["type"], "text")
        return result[0]["text"]

    def _call(self, result):
        if isinstance(result, tuple):
            content, is_error = result
        else:
            content, is_error = result, False
        return content, is_error

    def test_http_get_and_post_round_trip(self):
        server = self._start_server()
        try:
            port = server.server_address[1]
            get_result, get_error = self._call(self.mcp.http_get({
                "url": f"http://127.0.0.1:{port}/hello?x=1",
                "headers": {"X-Test": "yes"},
            }))
            self.assertFalse(get_error)
            self.assertEqual(self._json_text(get_result), "GET OK")
            self.assertEqual(_Handler.last_request["method"], "GET")
            self.assertEqual(_Handler.last_request["path"], "/hello?x=1")
            self.assertEqual(_Handler.last_request["headers"].get("X-Test"), "yes")

            payload = json.dumps({"hello": "world"}, ensure_ascii=False)
            post_result, post_error = self._call(self.mcp.http_post({
                "url": f"http://127.0.0.1:{port}/submit",
                "body": payload,
                "headers": {"X-Auth": "token"},
            }))
            self.assertFalse(post_error)
            body = json.loads(self._json_text(post_result))
            self.assertEqual(body["received"], payload)
            self.assertEqual(body["content_type"], "application/json")
            self.assertEqual(_Handler.last_request["method"], "POST")
            self.assertEqual(_Handler.last_request["body"], payload)
            self.assertEqual(_Handler.last_request["headers"].get("X-Auth"), "token")
            self.assertEqual(_Handler.last_request["headers"].get("Content-Type"), "application/json")
        finally:
            server.shutdown()
            server.server_close()

    def test_redirects_are_blocked(self):
        server = self._start_server(_RedirectHandler)
        try:
            port = server.server_address[1]
            result, is_error = self._call(self.mcp.http_get({"url": f"http://127.0.0.1:{port}/redirect"}))
            self.assertTrue(is_error)
            error = json.loads(self._json_text(result))
            self.assertIn("redirect blocked", error["error"])
        finally:
            server.shutdown()
            server.server_close()

    def test_non_local_urls_return_json_error(self):
        result, is_error = self._call(self.mcp.http_get({"url": "http://example.com/"}))
        self.assertTrue(is_error)
        error = json.loads(self._json_text(result))
        self.assertIn("local network only", error["error"])

    def test_mcp_config_registers_http_server(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "mcp.json"
            subprocess.run(
                [sys.executable, str(ROOT / "embodied_ha" / "mcp-config.py"), str(out), "http"],
                check=True,
                capture_output=True,
                text=True,
            )
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("http", data["mcpServers"])
        self.assertTrue(data["mcpServers"]["http"]["args"][-1].endswith("http-mcp.py"))


if __name__ == "__main__":
    unittest.main()
