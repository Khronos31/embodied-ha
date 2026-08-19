#!/usr/bin/env python3
"""embodied-ha MCP サーバー共通ライブラリ。

stdio JSON-RPC (MCP) のボイラープレートをまとめ、各サーバーは
「ツール定義（spec）＋ハンドラ」だけを書けばよいようにする。

使い方:
    from mcp_lib import serve, text, image

    def my_handler(args):
        return [text("結果")]          # content のリストを返す
        # またはエラー時: return [text("失敗")], True

    serve("my-mcp", "1.0", {
        "my_tool": {
            "spec": {
                "name": "my_tool",
                "description": "...",
                "inputSchema": {"type": "object", "properties": {...}, "required": [...]},
            },
            "handler": my_handler,
        },
    })

重要: ハンドラ内では絶対に print() で標準出力に書かないこと
（stdout は JSON-RPC 専用。ログは stderr へ）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_call_log  # noqa: E402


def text(s):
    """テキスト content ブロックを作る。"""
    return {"type": "text", "text": str(s)}


def image(b64, mime="image/jpeg"):
    """画像 content ブロックを作る（base64）。"""
    return {"type": "image", "data": b64, "mimeType": mime}


def log(msg):
    """stderr へログ出力（stdout は JSON-RPC 専用なので汚さない）。"""
    print(msg, file=sys.stderr, flush=True)


def _send(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _send_result(id_, content, is_error=False):
    result = {"content": content}
    if is_error:
        result["isError"] = True
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _send_error(id_, code, message):
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def serve(name, version, tools):
    """stdio JSON-RPC ループを回す。

    tools: {tool_name: {"spec": <MCP tool schema>, "handler": fn(args)->content|（content, is_error）}}
    """
    specs = [t["spec"] for t in tools.values()]
    mcp_call_log.record(name, "", True, "server_start")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue

        method = req.get("method", "")
        id_ = req.get("id")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": name, "version": version},
            }})

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": id_, "result": {"tools": specs}})

        elif method == "tools/call":
            params = req.get("params", {})
            if not isinstance(params, dict):
                mcp_call_log.record(name, "", False, "invalid_params")
                if id_ is not None:
                    _send_error(id_, -32602, "Invalid params")
                continue
            tool_name = params.get("name", "")
            if not isinstance(tool_name, str):
                mcp_call_log.record(name, "", False, "invalid_params")
                if id_ is not None:
                    _send_error(id_, -32602, "Invalid params")
                continue
            call_args = params.get("arguments", {})
            if call_args is None:
                call_args = {}
            elif not isinstance(call_args, dict):
                mcp_call_log.record(name, tool_name, False, "invalid_params")
                if id_ is not None:
                    _send_error(id_, -32602, "Invalid params")
                continue
            tool = tools.get(tool_name)
            if not tool:
                mcp_call_log.record(name, tool_name, False, "unknown_tool")
                _send_result(id_, [text(f"未知のツール: {tool_name}")], True)
                continue
            try:
                out = tool["handler"](call_args)
                if isinstance(out, tuple):
                    content, is_error = out
                else:
                    content, is_error = out, False
                mcp_call_log.record(name, tool_name, not is_error)
                _send_result(id_, content, is_error)
            except Exception as e:
                mcp_call_log.record(name, tool_name, False, "handler_exception")
                log(f"Tool handler failed ({tool_name}): {type(e).__name__}: {e}")
                _send_result(id_, [text(f"ツール実行エラー（{tool_name}）")], True)

        elif id_ is not None:
            # 未対応メソッドには JSON-RPC エラーを返す（通知にはなにもしない）
            _send_error(id_, -32601, f"Method not found: {method}")
