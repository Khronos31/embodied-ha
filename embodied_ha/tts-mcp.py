#!/usr/bin/env python3
"""TTS / 通知 MCP サーバー（embodied-ha 用）。

ツール:
  speak … preferences.json の speakers 設定に従って部屋へ発話（TTS）または通知する

実体は speak.py をサブプロセスで呼ぶ（stdout を JSON-RPC と分離するため）。
env: EHA_PREFS_FILE, HA_URL, SUPERVISOR_TOKEN
"""
import os
import subprocess

from mcp_lib import serve, text, log

SPEAK_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speak.py")


def speak(args):
    room = (args.get("room") or "").strip()
    message = (args.get("message") or "").strip()
    if not room or not message:
        return [text("room と message の両方が必要です")], True
    r = subprocess.run(
        ["python3", SPEAK_PY, room, message],
        capture_output=True, text=True, timeout=15
    )
    detail = (r.stdout or "").strip() or (r.stderr or "").strip()
    if r.returncode != 0:
        return [text(f"発話できませんでした（room={room}）: {detail}")], True
    log(f"[tts-mcp] speak {room}: {message[:40]}")
    return [text(f"発話しました（{room}）")]


serve("tts-mcp", "1.0", {
    "speak": {
        "spec": {
            "name": "speak",
            "description": (
                "指定した部屋で声を出す（TTS）か通知を送る。\n"
                "room は preferences.json の speakers に登録された部屋名"
                "（例: living, study）。\n"
                "登録されていない room を指定すると失敗する。"
                "どの部屋が使えるかは preferences.json または長期記憶を参照。\n"
                "観察・探索中に独り言ではなく家人に伝えたいことがあるときに使う。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "room": {"type": "string", "description": "発話先の部屋名（speakersのキー）"},
                    "message": {"type": "string", "description": "話す内容"},
                },
                "required": ["room", "message"],
            },
        },
        "handler": speak,
    },
})
