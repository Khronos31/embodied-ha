#!/usr/bin/env python3
"""センサー MCP サーバー（embodied-ha 用）。

ツール:
  get_sensors … preferences.json の sensors マニフェスト（おもなデバイス）を
                現在値つきで描画して返す

実体は render-sensors.py をサブプロセスで呼ぶ。
おもなデバイス以外のセンサーは ha_get（ha-mcp）でいつでも取れる。
env: EHA_PREFS_FILE, HA_URL, SUPERVISOR_TOKEN
"""
import os
import subprocess

from mcp_lib import serve, text

RENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render-sensors.py")


def get_sensors(args):
    context = (args.get("context") or "watch").strip()
    if context not in ("watch", "chat"):
        context = "watch"
    r = subprocess.run(
        ["python3", RENDER, "--context", context],
        capture_output=True, text=True, timeout=20
    )
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        return [text(f"センサー取得に失敗しました: {(r.stderr or '').strip()}")], True
    return [text(out if out else "（おもなデバイスは未設定です）")]


serve("sensors-mcp", "1.0", {
    "get_sensors": {
        "spec": {
            "name": "get_sensors",
            "description": (
                "家のおもなデバイスの現在値をまとめて取得する。\n"
                "人感・在宅・温湿度など、preferences.json に登録された"
                "観察用センサーの現在状態が返る。\n"
                "おもなデバイス以外の個別エンティティは ha_get で取得できる。\n"
                "context=watch（観察向け・既定）/ chat（会話向け）でフィルタ。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "context": {"type": "string",
                                "description": "watch（既定）または chat"},
                },
            },
        },
        "handler": get_sensors,
    },
})
