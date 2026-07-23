#!/usr/bin/env python3
"""mcp-config.py [--format claude|codex|agy] <output_path> <server>...

各ループ（loop/chat）が必要なMCPサーバーだけを指定して設定を書き出す。
サーバーはエージェントハーネスの子プロセスとして起動されるため、必要な
環境変数を明示的に env ブロックへ注入する（env継承に依存しない）。

--mcp-servers に hacontrol などの単一tool serverを含めるかどうかが、
そのserverの安全性境界である。--allowed-mcp-tools は多tool server内の
実行を絞り込めるが、Claude Codeでは未許可toolの一覧・スキーマも可視のまま
である。hacontrol のような単一tool serverの安全性は tool allow-list だけに
依存せず、server-list 接続可否と boundary.py 側ゲートで多層に守る。

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
from dataclasses import dataclass
import json
import os
import re
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
    "EHA_TOOLS_PATH", "EHA_ACTOR", "EHA_MQTT_PREFIX", "PATH",
)
COMMON_ENV = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}

# game-mcp は CPU 戦(WordVec)で invoke-agent.sh 経由に選択ハーネスを再起動する唯一の MCP
# サーバー。MCP サーバーは COMMON_ENV(明示 env)からのみ起動され親環境を継承しないため、
# nested invoke に要る「選択ハーネス + その CLI パス/ホーム/認証/cwd」を game 限定で明示注入する
# (Step4増分1b・sol H3)。ANTHROPIC_API_KEY 等の認証情報は全 MCP へ広げず、first-party の
# game サーバーだけに限定する(存在するものだけ渡す)。
_GAME_NESTED_ENV_KEYS = (
    "EHA_AGENT_HARNESS", "EHA_AGENT_CWD",
    # claude: bin 解決(EHA_CLAUDE_BIN>CLAUDE_BIN>DIY) + cwd + 認証(config dir / API key)
    "EHA_CLAUDE_BIN", "CLAUDE_BIN", "EHA_CLAUDE_CWD", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY",
    # codex: bin + home(認証)
    "EHA_CODEX_BIN", "CODEX_HOME",
    # agy: bin + home(認証)
    "EHA_ANTIGRAVITY_BIN", "EHA_ANTIGRAVITY_BIN_DIR", "EHA_ANTIGRAVITY_HOME",
)
GAME_NESTED_ENV = {k: os.environ[k] for k in _GAME_NESTED_ENV_KEYS if k in os.environ}


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


# files MCP のような「HA へアクセスしない」サーバー向けの最小 env。COMMON_ENV(SUPERVISOR_TOKEN 等の
# 秘密を含む)を渡さない=最小権限。read_file が万一 /proc/self/environ 相当を読んでも秘密が無い
# (本命の防御は files-mcp.py 側の /proc・/sys 拒否+NUL 検出。これはその二重化)。
MINIMAL_ENV = {"PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")}


def _server(script, extra_args=None, extra_env=None, base_env=None):
    env = dict(COMMON_ENV if base_env is None else base_env)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    return {
        "command": "python3",
        "args": [os.path.join(DIR, script)] + (extra_args or []),
        "env": env,
    }


@dataclass(frozen=True)
class ServerSpec:
    command: object
    tools: object

    def build(self):
        return self.command()

    def active_tools(self):
        tools = self.tools() if callable(self.tools) else self.tools
        return tuple(tools)


def _http_tools():
    tools = ["http_get"]
    if prefs.get("http_post_enabled"):
        tools.append("http_post")
    return tuple(tools)


SERVER_SPECS = {
    "audio": ServerSpec(lambda: _server("audio-mcp.py"), (
        "listen",
        "listen_media",
        "read_audio_log",
        "read_heard_audio_log",
        "read_active_listen_log",
        "read_non_speech_audio_events",
        "read_audio_event_tags",
        "speak",
        "use_device_speaker",
        "use_device_microphone",
        "concentrate_hearing",
    )),
    "body": ServerSpec(lambda: _server("body-mcp.py"), (
        "get_location",
        "move_to",
        "enter_cyberspace",
        "move_cyber",
        "return_to_body",
        "estimate_move_cost",
        "get_room_graph",
    )),
    "camera": ServerSpec(lambda: _server("camera-mcp.py", [
        "--ha-url",     os.environ["HA_URL"],
        "--go2rtc-url", os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984"),
    ]), (
        "use_device_camera",
        "watch_media",
    )),
    "ha": ServerSpec(lambda: _server("ha-mcp.py"), ("ha_get",)),  # 読み取り専用
    "hacontrol": ServerSpec(lambda: _server("ha-control-mcp.py"), ("ha_call_service",)),  # 家電操作
    # codex/agy は本環境の bwrap 制約でシェル経由 Read が不可。Claude の組み込み Read 相当を
    # シェルなしで最小権限提供する(2026-07-22)。claude は native Read を使うので通常は不要だが
    # ハーネス非依存で持たせておく。
    "files": ServerSpec(lambda: _server("files-mcp.py", base_env=MINIMAL_ENV), ("read_file",)),  # ファイル読み取り(read-anything+secure-read・最小env)
    # http_post は preferences.json の http_post_enabled(Web UI「高度な設定」タブのトグル)が
    # true のときだけ、http-mcp.py側のゲート用env(EHA_HTTP_ALLOW_POST)を注入する。
    # Claude Codeの--allowedToolsは実行時にMCPツール単位で拒否できるが、tools/listの可視性は
    # 絞らない。tools/listに載せないことは引き続き有効な防御層(http-mcp.py側のコメント参照)。
    # うちだけの外部デバイス連携用の抜け道であり、
    # デフォルトは無効。
    "http": ServerSpec(lambda: _server("http-mcp.py", extra_env={
        "EHA_HTTP_ALLOW_POST": "1" if prefs.get("http_post_enabled") else None,
    }), _http_tools),
    "lounge": ServerSpec(lambda: _server("lounge-mcp.py", extra_env={
        "LOUNGE_APP_ID": prefs.get("ai_lounge", {}).get("app_id", "") if isinstance(prefs.get("ai_lounge"), dict) else "",
        "LOUNGE_INSTALLATION_ID": prefs.get("ai_lounge", {}).get("installation_id", "") if isinstance(prefs.get("ai_lounge"), dict) else "",
    }), (
        "read_lounge_discussions",
        "read_lounge_discussion",
        "enqueue_lounge_post",
        "read_lounge_queue",
        "read_lounge_log",
    )),
    "memory": ServerSpec(lambda: _server("memory-mcp.py"), (
        "recall",
        "remember",
        "loops_list",
        "loops_add",
        "loops_close",
        "record_episode",
        "record_counterfactual",
        "get_episode",
        "get_working_memory",
        "ingest_scene",
        "resolve_reference",
        "compare_recent_scenes",
        "list_episodes",
        "build_daybook",
        "get_daybook",
        "record_causal_chain",
        "get_causal_chain",
        "consolidate_memory",
    )),
    "sensors": ServerSpec(lambda: _server("sensors-mcp.py"), ("get_sensors",)),
    "sociality": ServerSpec(lambda: _server("sociality-mcp.py"), (
        "get_relationship",
        "update_relationship",
        "get_narrative",
        "append_narrative",
        "get_social_state",
        "update_social_state",
        "get_shared_focus",
        "set_shared_focus",
        "get_person_model",
        "record_boundary",
        "record_consent",
        "should_interrupt",
        "get_turn_taking_state",
        "ingest_interaction",
    )),
    "game": ServerSpec(lambda: _server("game-mcp.py", extra_env=GAME_NESTED_ENV), (
        "game_wiki6_start",
        "game_wiki6_getlinks",
        "game_wordvec_race_start",
        "game_wordvec_race_cpu_move",
        "game_wordvec_race_submit",
        "game_wordvec_race_hint",
        "game_wiki6_solve",
    )),
    "song": ServerSpec(lambda: _server("song-mcp.py"), ("record",)),
}


_MCP_TOOL_RE = re.compile(r"^mcp__([A-Za-z0-9_-]+)__([A-Za-z0-9_-]+)$")


def _fail(message):
    print(f"[mcp-config] {message}", file=sys.stderr)
    raise SystemExit(2)


def _parse_allowed_mcp_tools(csv, selected_servers):
    if csv is None:
        return {}
    selected = set(selected_servers)
    allowed = {}
    seen = set()
    for raw_item in csv.split(","):
        item = raw_item.strip()
        if not item:
            _fail("--allowed-mcp-tools contains an empty entry")
        match = _MCP_TOOL_RE.fullmatch(item)
        if not match:
            _fail(f"invalid MCP tool allowlist entry: {item}")
        if item in seen:
            _fail(f"duplicate MCP tool allowlist entry: {item}")
        seen.add(item)
        server, tool = match.groups()
        if server not in SERVER_SPECS:
            _fail(f"unknown MCP server in allowlist: {server}")
        if server not in selected:
            _fail(f"MCP server is not selected by --mcp-servers: {server}")
        active_tools = set(SERVER_SPECS[server].active_tools())
        if tool not in active_tools:
            _fail(f"unknown MCP tool for server {server}: {tool}")
        if tool in allowed.setdefault(server, []):
            _fail(f"duplicate MCP tool for server {server}: {tool}")
        allowed[server].append(tool)
    missing = selected - set(allowed)
    if missing:
        _fail("--allowed-mcp-tools must cover every selected server; missing: " + ", ".join(sorted(missing)))
    return allowed


def _validate_claude_allowed_tools(allowed_tools):
    """Accept Claude per-server partial allowlists.

    `_parse_allowed_mcp_tools` retains the fail-closed syntax, selected-server,
    active-tool, and duplicate checks. Claude Code enforces the resulting
    `--allowedTools` entries at execution time, although all connected MCP tool
    schemas remain visible to the model.
    """


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
    if "files" in servers:
        # Codex 0.145.0 + gpt-5.6-terra では、通常 chat の長い user prompt 内に
        # read_file の利用方法を書くだけでは tool を「利用不能」と誤判定する一方、
        # 同一 profile の短い prompt では呼べることを実機確認した。これは tool の
        # 接続契約なので user prompt ではなく追加 developer instruction に置く。
        # model_instructions_file は built-in base instructions を置換するため使わない。
        lines.append(
            "developer_instructions = "
            + _toml_string(
                "When the user asks to read a file, use the files MCP server's read_file tool. "
                "Pass an absolute or relative regular-file path. It does not list directories. "
                "Do not infer current tool availability from prior conversation claims; call the tool "
                "and report an error only if the current call fails."
            )
        )
        lines.append("")
    for name, server in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        lines.append(f"command = {_toml_string(server['command'])}")
        if server.get("args"):
            lines.append(f"args = {_toml_array(server['args'])}")
        if allowed_tools.get(name):
            lines.append(f"enabled_tools = {_toml_array(allowed_tools[name])}")
        # Codex の非対話実行は approval_policy=never のため、既定で承認を求める
        # 注釈なし MCP tool は "user cancelled MCP tool call" として拒否される。
        # files は read_file だけを公開する first-party の読み取り専用 server なので、
        # この server に限って自動承認する。他 server の承認境界は変更しない。
        if name == "files":
            lines.append('default_tools_approval_mode = "approve"')
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
    parser.add_argument("--allowed-mcp-tools")
    parser.add_argument("output_path")
    parser.add_argument("servers", nargs="*")
    args = parser.parse_args()
    out = args.output_path
    names = args.servers

    duplicate_names = sorted({n for n in names if names.count(n) > 1})
    if duplicate_names:
        _fail("duplicate MCP server name: " + ", ".join(duplicate_names))

    for n in names:
        if n not in SERVER_SPECS:
            _fail(f"unknown MCP server: {n}")

    allowed_tools = _parse_allowed_mcp_tools(args.allowed_mcp_tools, names)
    if args.format == "claude" and allowed_tools:
        _validate_claude_allowed_tools(allowed_tools)

    servers = {n: SERVER_SPECS[n].build() for n in names}
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if args.format == "codex":
        _write_codex_profile(out, servers, allowed_tools)
    else:
        config = _json_config(servers, allowed_tools, include_tools=args.format == "agy")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
