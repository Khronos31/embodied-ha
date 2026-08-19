#!/usr/bin/env python3
"""MCP ツール呼び出しの記録。

なぜサーバー側で記録するのか: ループの `facts.tools_used` は Claude の stream-json を
パースして作られるため、Codex と agy では常に空になる。呼び出しの有無をログから
判定できるのが1ハーネスだけ、という状態を解消する。

記録するのはツール名と成否だけで、引数の値は書かない。引数には住人の観察が入る。

既知の限界:
  - ここに残るのは**サーバーへ到達した呼び出しだけ**。ハーネスの権限層で止められた
    呼び出しや、そもそもツールが見えていない場合は現れない。
  - 組み込みツール（Read / WebSearch 等）は MCP サーバーを通らないので対象外。
"""
import json
import os
import sys
import time

_MAX_BYTES = 2 * 1024 * 1024


def _log_path():
    log_dir = os.environ.get("EHA_LOG_DIR", "").strip()
    if not log_dir:
        return ""
    return os.path.join(log_dir, "mcp_tool_calls.jsonl")


def _rotate_if_needed(path):
    try:
        if os.path.getsize(path) <= _MAX_BYTES:
            return
    except OSError:
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from state_utils import file_lock

    with file_lock(path):
        try:
            if os.path.getsize(path) > _MAX_BYTES:
                os.replace(path, f"{path}.1")
        except OSError:
            pass


def record(server, tool, ok, reason=""):
    """1件の呼び出しを追記する。失敗しても呼び出し側へは伝播させない。"""
    path = _log_path()
    if not path:
        return
    row = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": str(server),
        "tool": str(tool),
        "ok": bool(ok),
    }
    if reason:
        row["reason"] = str(reason)
    try:
        line = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    except Exception:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        _rotate_if_needed(path)
    except Exception:
        return
