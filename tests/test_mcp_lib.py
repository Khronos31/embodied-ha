import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import mcp_lib  # noqa: E402


def run_server(requests, handler=None):
    if handler is None:
        handler = lambda arguments: [mcp_lib.text("ok")]
    tools = {
        "probe": {
            "spec": {
                "name": "probe",
                "description": "test tool",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "handler": handler,
        }
    }
    stdin = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(sys, "stdin", stdin), redirect_stdout(stdout), redirect_stderr(stderr):
        mcp_lib.serve("test-mcp", "1.0", tools)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    return replies, stderr.getvalue()


class McpLibTests(unittest.TestCase):
    def test_non_object_requests_are_ignored_and_server_continues(self):
        replies, _ = run_server([
            None,
            [],
            "not-an-object",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        ])

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(replies[0]["result"]["tools"][0]["name"], "probe")

    def test_invalid_params_types_return_error_and_server_continues(self):
        replies, _ = run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": []},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "probe", "arguments": []},
            },
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        ])

        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        for reply in replies[:3]:
            self.assertEqual(reply["error"], {"code": -32602, "message": "Invalid params"})
        self.assertIn("tools", replies[3]["result"])

    def test_handler_exception_is_sanitized_and_server_continues(self):
        sentinel = "secret-like-/config/private/path"

        def failing_handler(arguments):
            raise RuntimeError(sentinel)

        replies, stderr = run_server([
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "probe", "arguments": {}},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ], handler=failing_handler)

        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertEqual(replies[0]["result"]["content"][0]["text"], "ツール実行エラー（probe）")
        self.assertNotIn(sentinel, json.dumps(replies[0], ensure_ascii=False))
        self.assertIn(sentinel, stderr)
        self.assertIn("tools", replies[1]["result"])


if __name__ == "__main__":
    unittest.main()
