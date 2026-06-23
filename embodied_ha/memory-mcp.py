#!/usr/bin/env python3
"""記憶 MCP サーバー（embodied-ha 用）。

ツール:
  recall       … 過去ログ（観察・探索・会話・記憶）を全文検索（読み取り専用）
  remember     … 長期記憶 memory.md に一文追記
  loops_list   … 開いたループ（やりかけ・約束）一覧
  loops_add    … 新しいループを追加
  loops_close  … ループをクローズ

recall / loops は既存の recall.sh / loops.sh をサブプロセスで呼ぶ。
env: EHA_LOG_DIR, EHA_TOOLS_PATH
"""
import os
import datetime
import subprocess

from mcp_lib import serve, text, log

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_DIR, "log"))
MEMORY_FILE = os.path.join(LOG_DIR, "memory.md")
RECALL = os.path.join(_DIR, "recall.sh")
LOOPS = os.path.join(_DIR, "loops.sh")


def recall(args):
    kw = args.get("keywords") or []
    if isinstance(kw, str):
        kw = kw.split()
    kw = [str(k).strip() for k in kw if str(k).strip()]
    if not kw:
        return [text("keywords が空です（例: [\"エアコン\", \"冷房\"]）")], True
    r = subprocess.run(["bash", RECALL, *kw], capture_output=True, text=True, timeout=20)
    out = (r.stdout or "").strip()
    return [text(out if out else "（ヒットなし）")]


def remember(args):
    note = (args.get("note") or "").strip()
    if not note:
        return [text("note が空です")], True
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"- {ts} | {note}\n")
    except Exception as e:
        return [text(f"記憶の追記に失敗: {e}")], True
    log(f"[memory-mcp] remember: {note[:40]}")
    return [text("記憶に残しました")]


def loops_list(args):
    r = subprocess.run(["bash", LOOPS, "list"], capture_output=True, text=True, timeout=10)
    out = (r.stdout or "").strip()
    return [text(out if out else "（開いているループはありません）")]


def loops_add(args):
    note = (args.get("text") or "").strip()
    source = (args.get("source") or "explore").strip()
    if not note:
        return [text("text が空です")], True
    r = subprocess.run(["bash", LOOPS, "add", source, note],
                       capture_output=True, text=True, timeout=10)
    new_id = (r.stdout or "").strip()
    if r.returncode != 0:
        return [text(f"ループ追加に失敗: {r.stderr.strip()}")], True
    return [text(f"ループを追加しました（id={new_id}）")]


def loops_close(args):
    loop_id = (args.get("id") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not loop_id:
        return [text("id が必要です")], True
    cmd = ["bash", LOOPS, "close", loop_id]
    if reason:
        cmd.append(reason)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return [text(f"クローズに失敗: {r.stderr.strip()}")], True
    return [text(f"ループ {loop_id} をクローズしました")]


serve("memory-mcp", "1.0", {
    "recall": {
        "spec": {
            "name": "recall",
            "description": (
                "過去ログ（観察・探索・会話・長期記憶）をキーワードで全文検索する。\n"
                "長期記憶や直近の会話に無い昔のことを思い出したいときに使う。\n"
                "複数キーワードは OR 検索。類義語も一緒に渡すと取りこぼしが減る。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "array", "items": {"type": "string"},
                                 "description": "検索キーワード（OR検索）"},
                },
                "required": ["keywords"],
            },
        },
        "handler": recall,
    },
    "remember": {
        "spec": {
            "name": "remember",
            "description": (
                "長期記憶（memory.md）に一文を追記する。\n"
                "家の構造・家人の好み・繰り返し気づいたパターンなど、"
                "後々まで覚えておきたいことを残す。一時的な観察は残さない。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "記憶に残す一文"},
                },
                "required": ["note"],
            },
        },
        "handler": remember,
    },
    "loops_list": {
        "spec": {
            "name": "loops_list",
            "description": "開いたループ（やりかけ・家人との約束）の一覧を見る。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        "handler": loops_list,
    },
    "loops_add": {
        "spec": {
            "name": "loops_add",
            "description": (
                "新しいループ（やりかけ・約束・後で気にかけたいこと）を追加する。\n"
                "例: 「金曜にフィルター掃除をする約束をした」"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "ループの内容"},
                    "source": {"type": "string", "description": "watch/explore/chat のいずれか（既定 explore）"},
                },
                "required": ["text"],
            },
        },
        "handler": loops_add,
    },
    "loops_close": {
        "spec": {
            "name": "loops_close",
            "description": "完了した・不要になったループを id 指定でクローズする。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "ループのid（loops_listで確認）"},
                    "reason": {"type": "string", "description": "クローズ理由（任意）"},
                },
                "required": ["id"],
            },
        },
        "handler": loops_close,
    },
})
