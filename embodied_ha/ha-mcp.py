#!/usr/bin/env python3
"""HA REST API MCP サーバー（embodied-ha 用・読み取り専用）。

ツール:
  ha_get … 読み取り専用 GET（states / services / history 等）

家電操作（ha_call_service）は別サーバー ha-control-mcp.py に分離した。
「読みサーバーを繋いでも操作はできない」という物理的な分離のため
（自律操作 OFF のループに ha-control を繋がなければ操作ツール自体が無い）。

トークンはサーバー内に隠蔽し、Claude には渡さない。
env: HA_URL, SUPERVISOR_TOKEN
"""
import os
import subprocess

from mcp_lib import serve, text

HA_URL = os.environ["HA_URL"].rstrip("/")


def _token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def ha_get(args):
    path = (args.get("path") or "").strip().lstrip("/")
    if not path:
        return [text("path が空です（例: states, states/climate.xxx, services）")], True
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "15",
         "-H", f"Authorization: Bearer {_token()}",
         f"{HA_URL}/{path}"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return [text(f"GET 失敗（path={path} returncode={r.returncode}）")], True
    return [text(r.stdout if r.stdout else "(空のレスポンス)")]


if __name__ == "__main__":
    serve("ha-mcp", "1.0", {
        "ha_get": {
            "spec": {
                "name": "ha_get",
                "description": (
                    "Home Assistant の状態を読み取る（GET専用・操作不可）。\n"
                    "  states                         … 全エンティティの状態\n"
                    "  states/<entity_id>             … 個別エンティティの詳細\n"
                    "  history/period?filter_entity_id=<id> … 履歴\n"
                    "  services                       … 利用可能なサービス一覧\n"
                    "出力は JSON。大量になる場合があるので必要な path を絞って呼ぶ。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "API パス（例: states/climate.living）"}
                    },
                    "required": ["path"],
                },
            },
            "handler": ha_get,
        },
    })
