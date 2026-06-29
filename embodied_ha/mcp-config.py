#!/usr/bin/env python3
"""mcp-config.py <output_path> <server>...  — claude の --mcp-config 用 JSON を生成。

各ループ（watch/explore/chat）が必要なMCPサーバーだけを指定して設定を書き出す。
サーバーは claude の子プロセスとして起動されるため、必要な環境変数を
明示的に env ブロックへ注入する（env継承に依存しない）。

サーバー名: audio / body / camera / ha / hacontrol / http / memory / sensors / sociality
env: HA_URL, GO2RTC_BASE, SUPERVISOR_TOKEN,
     EHA_PREFS_FILE, EHA_LOG_DIR, EHA_DATA_DIR, EHA_AUDIO_LOG_FILE,
     EHA_AUDITORY_EVENTS_FILE, EHA_ACTIVE_LISTEN_LOG_FILE,
     EHA_ACTIVE_LISTEN_RETENTION_HOURS, EHA_BACKGROUND_AUDIO_LOG_FILE,
     EHA_NON_SPEECH_AUDIO_EVENTS_FILE, EHA_AUDIO_EVENT_TAGS_FILE, EHA_AUDIO_WAV_DIR,
     EHA_ROOM_GRAPH_FILE, EHA_BODY_LOCATION_FILE, EHA_BODY_LOCATION_LOG_FILE,
     EHA_TOOLS_PATH, PATH
"""
import sys
import os
import json

DIR = os.path.dirname(os.path.abspath(__file__))

# MCPサーバーへ引き継ぐ環境変数（存在するものだけ）
_ENV_KEYS = (
    "HA_URL", "GO2RTC_BASE", "SUPERVISOR_TOKEN",
    "EHA_PREFS_FILE", "EHA_LOG_DIR", "EHA_DATA_DIR", "EHA_AUDIO_LOG_FILE",
    "EHA_AUDITORY_EVENTS_FILE", "EHA_ACTIVE_LISTEN_LOG_FILE",
    "EHA_ACTIVE_LISTEN_RETENTION_HOURS", "EHA_BACKGROUND_AUDIO_LOG_FILE",
    "EHA_NON_SPEECH_AUDIO_EVENTS_FILE", "EHA_AUDIO_EVENT_TAGS_FILE", "EHA_AUDIO_WAV_DIR",
    "EHA_ROOM_GRAPH_FILE", "EHA_BODY_LOCATION_FILE", "EHA_BODY_LOCATION_LOG_FILE",
    "EHA_TOOLS_PATH", "EHA_ACTOR", "PATH",
)
COMMON_ENV = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}


def _server(script, extra_args=None):
    return {
        "command": "python3",
        "args": [os.path.join(DIR, script)] + (extra_args or []),
        "env": COMMON_ENV,
    }


REGISTRY = {
    "audio":   lambda: _server("audio-mcp.py"),
    "body":    lambda: _server("body-mcp.py"),
    "camera": lambda: _server("camera-mcp.py", [
        "--ha-url",     os.environ["HA_URL"],
        "--go2rtc-url", os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984"),
    ]),
    "ha":        lambda: _server("ha-mcp.py"),          # 読み取り専用（ha_get）
    "hacontrol": lambda: _server("ha-control-mcp.py"),  # 家電操作（ha_call_service）
    "http":    lambda: _server("http-mcp.py"),
    "memory":  lambda: _server("memory-mcp.py"),
    "sensors": lambda: _server("sensors-mcp.py"),
    "sociality": lambda: _server("sociality-mcp.py"),
}


def main():
    if len(sys.argv) < 2:
        print("usage: mcp-config.py <output_path> <server>...", file=sys.stderr)
        sys.exit(1)
    out = sys.argv[1]
    names = sys.argv[2:]

    servers = {}
    for n in names:
        if n in REGISTRY:
            servers[n] = REGISTRY[n]()
        else:
            print(f"[mcp-config] 未知のサーバー: {n}（スキップ）", file=sys.stderr)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": servers}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
