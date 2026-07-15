#!/usr/bin/env python3
"""mcp-config.py [--format claude|codex|agy] <output_path> <server>...

各ループ（loop/chat）が必要なMCPサーバーだけを指定して設定を書き出す。
サーバーはエージェントハーネスの子プロセスとして起動されるため、必要な
環境変数を明示的に env ブロックへ注入する（env継承に依存しない）。

--mcp-servers に hacontrol などの単一tool serverを含めるかどうかが、
そのserverの安全性境界である。--allowed-mcp-tools は多tool server内の
補助的な絞り込みであり、hacontrol のような単一tool serverの安全性は
tool allow-listではなく server-list 接続可否と boundary.py 側ゲートに依存する。

サーバー名: audio / body / camera / ha / hacontrol / http / lounge / memory / sensors / sociality / song
env: HA_URL, GO2RTC_BASE, SUPERVISOR_TOKEN,
     EHA_PREFS_FILE, EHA_LOG_DIR, EHA_DATA_DIR, EHA_AUDIO_LOG_FILE,
     EHA_AUDITORY_EVENTS_FILE, EHA_ACTIVE_LISTEN_LOG_FILE,
     EHA_ACTIVE_LISTEN_RETENTION_HOURS, EHA_BACKGROUND_AUDIO_LOG_FILE,
     EHA_NON_SPEECH_AUDIO_EVENTS_FILE, EHA_AUDIO_EVENT_TAGS_FILE, EHA_AUDIO_WAV_DIR,
     EHA_ROOM_GRAPH_FILE, EHA_BODY_LOCATION_FILE, EHA_BODY_LOCATION_LOG_FILE, EHA_ANOMALY_STATE_FILE,
     EHA_TOOLS_PATH, PATH, LOUNGE_APP_ID, LOUNGE_INSTALLATION_ID
"""
import argparse
import json
import os
import sys

DIR = os.path.dirname(os.path.abspath(__file__))

# MCPサーバーへ引き継ぐ環境変数（存在するものだけ）
_ENV_KEYS = (
    "HA_URL", "GO2RTC_BASE", "SUPERVISOR_TOKEN",
    "EHA_PREFS_FILE", "EHA_LOG_DIR", "EHA_DATA_DIR", "EHA_AUDIO_LOG_FILE",
    "EHA_AUDITORY_EVENTS_FILE", "EHA_ACTIVE_LISTEN_LOG_FILE",
    "EHA_ACTIVE_LISTEN_RETENTION_HOURS", "EHA_BACKGROUND_AUDIO_LOG_FILE",
    "EHA_NON_SPEECH_AUDIO_EVENTS_FILE", "EHA_AUDIO_EVENT_TAGS_FILE", "EHA_AUDIO_WAV_DIR",
    "EHA_ROOM_GRAPH_FILE", "EHA_BODY_LOCATION_FILE", "EHA_BODY_LOCATION_LOG_FILE", "EHA_ANOMALY_STATE_FILE",
    "EHA_TOOLS_PATH", "EHA_ACTOR", "PATH",
)
COMMON_ENV = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}


def _load_prefs():
    path = os.environ.get("EHA_PREFS_FILE", "")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


prefs = _load_prefs()


def _server(script, extra_args=None, extra_env=None):
    env = dict(COMMON_ENV)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    return {
        "command": "python3",
        "args": [os.path.join(DIR, script)] + (extra_args or []),
        "env": env,
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
    # http_post は preferences.json の http_post_enabled(Web UI「高度な設定」タブのトグル)が
    # true のときだけ、http-mcp.py側のゲート用env(EHA_HTTP_ALLOW_POST)を注入する。
    # --allowedTools ではMCPツール単位の絞り込みができないため、tools/listに載せるかどうかが
    # 唯一の制御点(http-mcp.py側のコメント参照)。うちだけの外部デバイス連携用の抜け道であり、
    # デフォルトは無効。
    "http":    lambda: _server("http-mcp.py", extra_env={
        "EHA_HTTP_ALLOW_POST": "1" if prefs.get("http_post_enabled") else None,
    }),
    "lounge": lambda: _server("lounge-mcp.py", extra_env={
        "LOUNGE_APP_ID": prefs.get("ai_lounge", {}).get("app_id", "") if isinstance(prefs.get("ai_lounge"), dict) else "",
        "LOUNGE_INSTALLATION_ID": prefs.get("ai_lounge", {}).get("installation_id", "") if isinstance(prefs.get("ai_lounge"), dict) else "",
    }),
    "memory":  lambda: _server("memory-mcp.py"),
    "sensors": lambda: _server("sensors-mcp.py"),
    "sociality": lambda: _server("sociality-mcp.py"),
    "game":     lambda: _server("game-mcp.py"),
    "song":     lambda: _server("song-mcp.py"),
}


def _parse_allowed_mcp_tools(csv):
    allowed = {}
    for item in (csv or "").split(","):
        item = item.strip()
        if not item:
            continue
        if not item.startswith("mcp__") or "__" not in item[5:]:
            continue
        server, tool = item[5:].split("__", 1)
        if server and tool:
            allowed.setdefault(server, []).append(tool)
    return allowed


def _json_config(servers, allowed_tools=None, *, include_tools=False):
    config = {"mcpServers": servers}
    if include_tools and allowed_tools:
        for name, tools in allowed_tools.items():
            if name in servers and tools:
                servers[name]["includeTools"] = tools
    return config


def _toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values):
    return "[" + ", ".join(_toml_string(v) for v in values) + "]"


def _write_codex_profile(path, servers, allowed_tools):
    lines = []
    for name, server in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        lines.append(f"command = {_toml_string(server['command'])}")
        if server.get("args"):
            lines.append(f"args = {_toml_array(server['args'])}")
        if allowed_tools.get(name):
            lines.append(f"enabled_tools = {_toml_array(allowed_tools[name])}")
        env = server.get("env") or {}
        if env:
            lines.append("")
            lines.append(f"[mcp_servers.{name}.env]")
            for key in sorted(env):
                lines.append(f"{key} = {_toml_string(env[key])}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(
        description="Generate MCP server config for Claude, Codex, or Antigravity.",
    )
    parser.add_argument("--format", choices=("claude", "codex", "agy"), default="claude")
    parser.add_argument("--allowed-mcp-tools", default="")
    parser.add_argument("output_path")
    parser.add_argument("servers", nargs="*")
    args = parser.parse_args()
    out = args.output_path
    names = args.servers

    servers = {}
    for n in names:
        if n in REGISTRY:
            servers[n] = REGISTRY[n]()
        else:
            print(f"[mcp-config] 未知のサーバー: {n}（スキップ）", file=sys.stderr)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    allowed_tools = _parse_allowed_mcp_tools(args.allowed_mcp_tools)
    if args.format == "codex":
        _write_codex_profile(out, servers, allowed_tools)
    else:
        config = _json_config(servers, allowed_tools, include_tools=args.format == "agy")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
