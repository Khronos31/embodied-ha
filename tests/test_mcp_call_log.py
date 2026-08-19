import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "embodied_ha"))

import mcp_call_log  # noqa: E402
import mcp_lib  # noqa: E402


def _rows(log_dir):
    path = os.path.join(log_dir, "mcp_tool_calls.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _run(tools, requests):
    """serve() を1往復ぶん回し、stdout に出た JSON-RPC 応答を返す。"""
    lines = [json.dumps(r) + "\n" for r in requests]
    buf = io.StringIO()
    stdin = sys.stdin
    sys.stdin = io.StringIO("".join(lines))
    try:
        with redirect_stdout(buf):
            mcp_lib.serve("probe-mcp", "1.0", tools)
    finally:
        sys.stdin = stdin
    return buf.getvalue()


def _tool(handler):
    return {
        "spec": {"name": "probe", "description": "", "inputSchema": {"type": "object", "properties": {}}},
        "handler": handler,
    }


def _call(name="probe"):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": {"secret": "住人の観察"}}}


class McpCallLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = self._tmp.name
        self._prev = os.environ.get("EHA_LOG_DIR")
        os.environ["EHA_LOG_DIR"] = self.log_dir
        importlib.reload(mcp_call_log)
        importlib.reload(mcp_lib)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("EHA_LOG_DIR", None)
        else:
            os.environ["EHA_LOG_DIR"] = self._prev
        self._tmp.cleanup()

    def test_successful_call_is_recorded_without_argument_values(self):
        out = _run({"probe": _tool(lambda args: [mcp_lib.text("ok")])}, [_call()])
        rows = [r for r in _rows(self.log_dir) if r.get("reason") != "server_start"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["server"], "probe-mcp")
        self.assertEqual(rows[0]["tool"], "probe")
        self.assertTrue(rows[0]["ok"])
        self.assertNotIn("住人の観察", json.dumps(rows[0], ensure_ascii=False))
        self.assertNotIn("secret", json.dumps(rows[0]))
        self.assertIn('"result"', out)

    def test_handler_error_tuple_is_recorded_as_failure(self):
        _run({"probe": _tool(lambda args: ([mcp_lib.text("だめ")], True))}, [_call()])
        rows = [r for r in _rows(self.log_dir) if r.get("reason") != "server_start"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])

    def test_handler_exception_and_unknown_tool_are_recorded(self):
        def boom(args):
            raise RuntimeError("boom")

        _run({"probe": _tool(boom)}, [_call(), _call("nope")])
        rows = [r for r in _rows(self.log_dir) if r.get("reason") != "server_start"]
        self.assertEqual([(r["tool"], r["ok"], r.get("reason")) for r in rows], [
            ("probe", False, "handler_exception"),
            ("nope", False, "unknown_tool"),
        ])

    def test_logging_failure_does_not_break_the_tool_call(self):
        blocker = os.path.join(self.log_dir, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        os.environ["EHA_LOG_DIR"] = os.path.join(blocker, "under-a-file")
        importlib.reload(mcp_call_log)
        importlib.reload(mcp_lib)
        out = _run({"probe": _tool(lambda args: [mcp_lib.text("ok")])}, [_call()])
        self.assertIn('"result"', out)
        self.assertNotIn('"error"', out)

    def test_stdout_carries_only_json_rpc(self):
        out = _run({"probe": _tool(lambda args: [mcp_lib.text("ok")])}, [_call()])
        for line in out.splitlines():
            if not line.strip():
                continue
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")

    def test_server_start_is_recorded_before_any_call(self):
        _run({"probe": _tool(lambda args: [mcp_lib.text("ok")])}, [])
        rows = _rows(self.log_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "server_start")
        self.assertEqual(rows[0]["server"], "probe-mcp")

    def test_invalid_params_are_recorded(self):
        bad = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "not-a-dict"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": 7}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "probe", "arguments": "住人の観察"}},
        ]
        _run({"probe": _tool(lambda args: [mcp_lib.text("ok")])}, bad)
        rows = [r for r in _rows(self.log_dir) if r.get("reason") != "server_start"]
        self.assertEqual([r["reason"] for r in rows], ["invalid_params"] * 3)
        self.assertEqual([r["tool"] for r in rows], ["", "", "probe"])
        self.assertNotIn("住人の観察", json.dumps(rows, ensure_ascii=False))

    def test_no_log_dir_means_no_file_and_no_error(self):
        os.environ.pop("EHA_LOG_DIR", None)
        importlib.reload(mcp_call_log)
        importlib.reload(mcp_lib)
        out = _run({"probe": _tool(lambda args: [mcp_lib.text("ok")])}, [_call()])
        self.assertIn('"result"', out)
        self.assertEqual(_rows(self.log_dir), [])


if __name__ == "__main__":
    unittest.main()
