#!/usr/bin/env python3
"""MCP ツール呼び出しの記録。

なぜサーバー側で記録するのか: ループの `facts.tools_used` は Claude の stream-json を
パースして作られるため、Codex と agy では常に空になる。呼び出しの有無をログから
判定できるのが1ハーネスだけ、という状態を解消する。

記録するのはツール名と成否だけで、引数の値は書かない。引数には住人の観察が入る。

既知の限界（この台帳に「行が無い」ことを「呼んでいない」と読んではいけない4類型）:
  1. ハーネスの権限層で止められた呼び出しは、サーバーへ届かないので残らない。
     ツールがそもそも見えていない場合も同じ。
  2. 組み込みツール（Read / WebSearch / ToolSearch 等）は MCP を経由しないので対象外。
  3. `files` サーバーは最小 env で起動され `EHA_LOG_DIR` を持たないため、恒久的に0行。
  4. 記録は失敗しても無言で諦める（ツール呼び出しを巻き込まないため）。

サーバー起動時にも1行（`reason=server_start`）を残す。「呼び出し0件」と
「そもそもそのサーバーが起動していない／配線が切れている」を区別するため。
"""
import datetime as _dt
import json
import os
import sys

_MAX_BYTES = 2 * 1024 * 1024


def _now_ts():
    """記録用の時刻。この機器が使っている時間帯で書く。

    時間帯は `TZ` から決まる。アドオン本体のログも同じ決め方なので、
    そちらと突き合わせられる。⚠️ コンテナの `/etc/localtime` は UTC で、
    現地時刻になっているのは `TZ` のおかげ——`/etc` を見に行ってはいけない。

    `TZ` は `mcp-config.py` が各サーバーへ明示的に渡す。継承に頼ると、
    エージェントCLIによっては付かず、同じログの中でオフセットが混ざる
    （実測: 同一分内に +00:00 と +09:00 の行が並んだ）。混ざると、行を
    文字列として並べ替えたときに順序が狂う。
    """
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


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
        "timestamp": _now_ts(),
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
