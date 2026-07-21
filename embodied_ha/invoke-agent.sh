#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: invoke-agent.sh [options] [prompt]

Options:
  --model default|lite        Logical model tier (default: default)
  --json-schema JSON         Structured output schema JSON
  --sound-file PATH          Force Antigravity audio fallback and inject PATH into the prompt
  --system-prompt TXT        Replace the harness's main system instruction (Claude native
                             --system-prompt; Codex via model_instructions_file; Antigravity
                             via prompt prefix approximation)
  --append-system-prompt TXT Append to the harness/system prompt (Claude native
                             --append-system-prompt; Codex/Antigravity via prompt prefix)
  --allowed-builtins CSV     Built-in tool allow-list for Claude Code only
                             (currently: Read, WebSearch)
  --allowed-mcp-tools CSV    MCP tools as mcp__server__tool; must cover every
                             selected MCP server. Per-server partial allowlists
                             are allowed: Claude blocks unlisted tool execution,
                             but keeps connected tool schemas visible.
  --mcp-config PATH          Claude Code MCP config path
  --mcp-servers "NAMES"      Space-separated MCP server names; for hacontrol and
                             other single-tool servers, this server-list is the
                             safety boundary, not --allowed-mcp-tools
  --agent-site SITE          Antigravity site: observe/explore/reflect/web/social/chat/game
  --content-json JSON        Claude Code stream-json content blocks. Use
                             @PATH to read the JSON from a file instead of
                             inline (avoids the ~128KB argv element limit).
  -h, --help                 Show this help

Harness selection comes from EHA_AGENT_HARNESS unless --sound-file is present.
Removed: --allowed-tools / --allowedTools. Use --allowed-builtins and
--allowed-mcp-tools separately.
EOF
}

die() {
  echo "invoke-agent.sh: $*" >&2
  exit 2
}

TEMP_FILES=()
cleanup_temp_files() {
  local path
  for path in "${TEMP_FILES[@]}"; do
    [[ -n "$path" ]] && rm -f "$path"
  done
}
trap cleanup_temp_files EXIT

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

append_csv() {
  local base="$1"
  local extra="$2"
  if [[ -z "$extra" ]]; then
    printf '%s' "$base"
  elif [[ -z "$base" ]]; then
    printf '%s' "$extra"
  else
    printf '%s,%s' "$base" "$extra"
  fi
}

json_schema=""
logical_model="default"
sound_file=""
system_prompt=""
system_prompt_replace=""
allowed_builtins=""
allowed_builtins_set="false"
allowed_mcp_tools=""
allowed_mcp_tools_set="false"
mcp_config=""
mcp_servers=""
agent_site=""
content_json=""
content_json_file=""
content_json_set="false"
prompt_parts=()

while (($#)); do
  case "$1" in
    --model)
      (($# >= 2)) || die "--model requires a value"
      logical_model="$2"
      shift 2
      ;;
    --json-schema)
      (($# >= 2)) || die "--json-schema requires a value"
      json_schema="$2"
      shift 2
      ;;
    --sound-file)
      (($# >= 2)) || die "--sound-file requires a value"
      sound_file="$2"
      shift 2
      ;;
    --system-prompt)
      (($# >= 2)) || die "--system-prompt requires a value"
      system_prompt_replace="$2"
      shift 2
      ;;
    --append-system-prompt)
      (($# >= 2)) || die "--append-system-prompt requires a value"
      system_prompt="$2"
      shift 2
      ;;
    --allowed-builtins)
      (($# >= 2)) || die "--allowed-builtins requires a value"
      allowed_builtins="$2"
      allowed_builtins_set="true"
      shift 2
      ;;
    --allowed-mcp-tools)
      (($# >= 2)) || die "--allowed-mcp-tools requires a value"
      allowed_mcp_tools="$2"
      allowed_mcp_tools_set="true"
      shift 2
      ;;
    --mcp-config)
      (($# >= 2)) || die "--mcp-config requires a value"
      mcp_config="$2"
      shift 2
      ;;
    --mcp-servers)
      (($# >= 2)) || die "--mcp-servers requires a value"
      mcp_servers="$2"
      shift 2
      ;;
    --agent-site)
      (($# >= 2)) || die "--agent-site requires a value"
      agent_site="$2"
      shift 2
      ;;
    --content-json)
      (($# >= 2)) || die "--content-json requires a value"
      content_json="$2"
      content_json_set="true"
      if [[ "$content_json" == @* ]]; then
        content_json_file="${content_json#@}"
        [[ -f "$content_json_file" ]] || die "--content-json file not found: $content_json_file"
        content_json=""
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      prompt_parts+=("$@")
      break
      ;;
    --*)
      die "unknown option: $1"
      ;;
    *)
      prompt_parts+=("$1")
      shift
      ;;
  esac
done

if [[ -n "$mcp_config" && -n "$mcp_servers" ]]; then
  die "--mcp-config and --mcp-servers cannot be used together"
fi
if [[ -n "$mcp_config" && ( "$allowed_mcp_tools_set" == "true" || "$allowed_builtins_set" == "true" ) ]]; then
  die "--mcp-config cannot be used with --allowed-builtins or --allowed-mcp-tools; use --mcp-servers"
fi
if [[ "$allowed_mcp_tools_set" == "true" && -z "$mcp_servers" ]]; then
  die "--allowed-mcp-tools requires --mcp-servers"
fi

case "$logical_model" in
  default|lite) ;;
  *) die "--model must be 'default' or 'lite'" ;;
esac

if ((${#prompt_parts[@]})); then
  prompt="${prompt_parts[*]}"
else
  prompt="$(cat)"
fi

if [[ -n "$sound_file" ]]; then
  [[ -f "$sound_file" ]] || die "--sound-file not found: $sound_file"
  # Antigravity(agy)側のGo content-sniffがWAV/MP3/FLACのMIMEを誤判定し(WAV->audio/wave,
  # MP3->audio/mpeg, FLAC->application/octet-stream)、いずれもGemini APIに拒否される
  # (実機検証済み、2026-07-17)。音声のみのWebM(opus)だけがクライアント/サーバー双方に
  # 受理されるため、ここで変換する。これはAntigravity側のバグに対する暫定ワークアラウンドで
  # あり、Antigravity側でWAV等のMIME判定が修正されたら不要になる。詳細:
  # embodied_ha_agy_audio_mime_investigation_2026-07-17 メモリ参照。
  sound_file_webm="$(mktemp "${TMPDIR:-/tmp}/eha-agy-sound.XXXXXX.webm")"
  TEMP_FILES+=("$sound_file_webm")
  ffmpeg -y -loglevel error -i "$sound_file" -vn -c:a libopus "$sound_file_webm" \
    || die "failed to convert --sound-file to webm for Antigravity: $sound_file"
  # ツール/スクリプト利用を明示的に禁止する指示。実機検証(2026-07-17)により、この指示が
  # あれば--dangerously-skip-permissions無しでも安全にview_fileへ直行できることを確認済み
  # (指示が無いと、モデルがls/file等のcommand権限ツールを試みてheadlessモードで自動拒否
  # されるか、Pythonスクリプトを自前で書いて外部STT APIへ投げる誤動作を起こす)。
  prompt="${prompt}"$'\n\n'"【いま聞こえた音】"$'\n'"view_fileで下記の音声ファイルを読み込んで内容を理解してください"$'\n'"command/shell/Pythonなどの実行ツールや外部スクリプトによる解析は禁止です"$'\n'"@${sound_file_webm}"
fi

selected_harness="${EHA_AGENT_HARNESS:-claude}"
harness_was_agy="false"
case "$(lower "$selected_harness")" in
  agy|antigravity|gemini) harness_was_agy="true" ;;
esac
if [[ -n "$sound_file" ]]; then
  selected_harness="agy"
fi

case "$(lower "$selected_harness")" in
  claude|claude-code) harness="claude" ;;
  codex) harness="codex" ;;
  agy|antigravity|gemini) harness="agy" ;;
  *) die "unknown EHA_AGENT_HARNESS: $selected_harness" ;;
esac

model=""
effort=""
case "$harness:$logical_model" in
  claude:default)
    model="${EHA_CLAUDE_MODEL_DEFAULT:-sonnet}"
    effort="${EHA_CLAUDE_EFFORT_DEFAULT:-medium}"
    ;;
  claude:lite)
    model="${EHA_CLAUDE_MODEL_LITE:-haiku}"
    effort="${EHA_CLAUDE_EFFORT_LITE:-low}"
    ;;
  codex:default)
    model="${EHA_CODEX_MODEL_DEFAULT:-gpt-5.6-terra}"
    effort="${EHA_CODEX_REASONING_EFFORT_DEFAULT:-medium}"
    ;;
  codex:lite)
    model="${EHA_CODEX_MODEL_LITE:-gpt-5.6-luna}"
    effort="${EHA_CODEX_REASONING_EFFORT_LITE:-low}"
    ;;
  agy:default)
    model="${EHA_AGY_MODEL_DEFAULT:-Gemini 3.5 Flash (Medium)}"
    ;;
  agy:lite)
    model="${EHA_AGY_MODEL_LITE:-Gemini 3.5 Flash (Low)}"
    ;;
esac
if [[ "$harness" == "agy" && -n "$sound_file" ]]; then
  # 音声モデルの優先順位(sol Med3): (1)明示された EHA_SESSION_MODEL を最優先。深聴き音声
  # セッション(listen_queue)はこれを音声既定Highに設定するため、選択ハーネスが agy で default
  # ティア prefs(EHA_AGY_MODEL_DEFAULT)を Low にしても STT には波及しない。(2)EHA_SESSION_MODEL
  # 未設定かつ元々 agy 選択でない(あかね等)なら音声専用既定へ。(3)元々 agy 選択かつ session
  # モデル未指定なら default ティアのまま(既存の意図的挙動を保持)。
  if [[ -n "${EHA_SESSION_MODEL:-}" ]]; then
    model="$EHA_SESSION_MODEL"
  elif [[ "$harness_was_agy" != "true" ]]; then
    model="${EHA_AGY_AUDIO_MODEL:-Gemini 3.5 Flash (High)}"
  fi
fi

extract_result_json() {
  python3 -c '
import json, re, sys
raw = sys.stdin.read().strip()
result = ""
for line in raw.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except Exception:
        continue
    if event.get("type") == "result":
        structured = event.get("structured_output")
        result = json.dumps(structured, ensure_ascii=False) if structured is not None else event.get("result", "")
if not result:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    result = m.group(0) if m else raw
print(result, end="")
'
}

validate_allowed_builtins() {
  [[ "$allowed_builtins_set" == "true" ]] || return 0
  local IFS=,
  local item
  local seen=","
  local -a builtin_items=()
  read -r -a builtin_items <<< "$allowed_builtins" || true
  ((${#builtin_items[@]} > 0)) || die "--allowed-builtins contains an empty entry"
  for item in "${builtin_items[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [[ -n "$item" ]] || die "--allowed-builtins contains an empty entry"
    case "$item" in
      Read|WebSearch) ;;
      *) die "unknown built-in tool in --allowed-builtins: $item" ;;
    esac
    if [[ "$seen" == *",$item,"* ]]; then
      die "duplicate built-in tool in --allowed-builtins: $item"
    fi
    seen+="$item,"
  done
}

validate_allowed_builtins

# agy 1.1.3のheadless(-p)モードは、MCPツール実行の確認をsettings.jsonの
# permissions.allowではなく、config.jsonのuserSettings.globalPermissionGrants
# (grant store)で判定する(settings.json側はエラーメッセージの案内に反して
# 無視される。実機切り分け済み、2026-07-17)。ここで必要なグラントを
# add-onlyマージで反映しないと、モデルがMCPツールを呼んだ時点でターン全体が
# 空応答になる。グラントは呼び出し後も残す(EHA専用identityへの恒久配置。
# 呼び出し単位のツール制限はmcp_configのincludeTools側が担う)。
ensure_agy_permission_grants() {
  local agy_home="$1"
  local grants="$2"
  local config_dir="$agy_home/.gemini/config"
  mkdir -p "$config_dir"
  local lock_file="$config_dir/.eha-grants.lock"
  (
    flock -x 200
    AGY_CONFIG_JSON="$config_dir/config.json" EHA_GRANTS="$grants" python3 - <<'PY'
import json
import os
import sys
import tempfile

path = os.environ["AGY_CONFIG_JSON"]
wanted = [g for g in os.environ["EHA_GRANTS"].split("\n") if g]


def fail(message):
    # 壊れた/型不正の既存config.jsonを黙って全置換すると、userSettingsの
    # 他のキーを失う。fail-closedで止めて診断に乗せる(sol review、2026-07-17)。
    print(f"invoke-agent.sh: agy config.json grants merge failed: {message} ({path})", file=sys.stderr)
    sys.exit(1)


file_existed = os.path.exists(path)
config = {}
if file_existed:
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except ValueError as e:
        fail(f"existing file is not valid JSON: {e}")
    if not isinstance(config, dict):
        fail("existing JSON root is not an object")
user_settings = config.setdefault("userSettings", {})
if not isinstance(user_settings, dict):
    fail("userSettings is not an object")
grants = user_settings.setdefault("globalPermissionGrants", {})
if not isinstance(grants, dict):
    fail("userSettings.globalPermissionGrants is not an object")
allow = grants.setdefault("allow", [])
if not isinstance(allow, list):
    fail("userSettings.globalPermissionGrants.allow is not a list")
changed = False
for grant in wanted:
    if grant not in allow:
        allow.append(grant)
        changed = True
if changed:
    mode = os.stat(path).st_mode & 0o777 if file_existed else 0o600
    fd, tmp = tempfile.mkstemp(prefix=".config.json.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
PY
  ) 200>"$lock_file"
}

detect_new_agy_project_id() {
  local projects_dir="$1"
  local before_file="$2"
  local site_dir="$3"
  PROJECTS_DIR="$projects_dir" BEFORE_FILE="$before_file" SITE_DIR="$site_dir" python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

projects_dir = Path(os.environ["PROJECTS_DIR"])
before_file = Path(os.environ["BEFORE_FILE"])
site_dir = str(Path(os.environ["SITE_DIR"]))
before = set(before_file.read_text(encoding="utf-8").splitlines()) if before_file.exists() else set()
candidates = []
new_files = []
for path in projects_dir.iterdir() if projects_dir.exists() else []:
    if path.name in before or path.name == ".eha-registration.lock" or not path.is_file():
        continue
    new_files.append(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    folder = data.get("folderUri") or data.get("folderPath") or data.get("path")
    if folder == site_dir:
        candidates.append(path.stem if path.suffix == ".json" else path.name)
if not candidates and len(new_files) == 1:
    only = new_files[0]
    candidates.append(only.stem if only.suffix == ".json" else only.name)
if len(candidates) != 1:
    print(f"expected exactly one new agy project for {site_dir}, got {candidates}", file=sys.stderr)
    sys.exit(1)
print(candidates[0], end="")
PY
}

claude_message() {
  # content_json_file (from --content-json @PATH) is read via normal file I/O,
  # not via argv/envp, to avoid Linux's ~128KB single-element limit
  # (MAX_ARG_STRLEN) that large inline content (e.g. camera images) would hit.
  PROMPT_TEXT="$prompt" CONTENT_JSON="$content_json" CONTENT_JSON_FILE="$content_json_file" python3 -c '
import json, os, sys
content_json_file = os.environ.get("CONTENT_JSON_FILE", "")
if content_json_file:
    with open(content_json_file, encoding="utf-8") as fh:
        content_json = fh.read()
else:
    content_json = os.environ.get("CONTENT_JSON", "")
if content_json:
    content = json.loads(content_json)
else:
    content = [{"type": "text", "text": os.environ.get("PROMPT_TEXT", "")}]
if not isinstance(content, list):
    raise SystemExit("--content-json must be a JSON array")
print(json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False), end="")
'
}

run_claude() {
  # 同梱廃止(増分5a): claudeバイナリを実在確認しながら解決する。優先順位は
  # resolve_claude_bin()(ready判定・login・game-mcpと同じ)と一致させる:
  # EHA_CLAUDE_BIN(実在) > CLAUDE_BIN(実在) > 既知DIYパス(実在) > PATHのclaude。
  # env値が存在しないパスを指していても(run.shは未配置時に将来のDIYパスを指す)、実在確認で
  # 読み飛ばしてDIY/PATHへフォールバックするため、readinessが見る実体と食い違わない。
  # 実在確認は resolve_claude_bin() の isfile+X_OK と一致させる(-f で実行可能ディレクトリを弾く)。
  # DIY 既知パスは EHA_CLAUDE_INSTALL_ROOT を尊重(run.sh/claude_setup.install_root と同じ既定)。
  local bin="" _cand
  local _diy="${EHA_CLAUDE_INSTALL_ROOT:-/data/claude-cli}/bin/claude"
  for _cand in "${EHA_CLAUDE_BIN:-}" "${CLAUDE_BIN:-}" "$_diy"; do
    if [[ -n "$_cand" && -f "$_cand" && -x "$_cand" ]]; then bin="$_cand"; break; fi
  done
  [[ -n "$bin" ]] || bin="claude"
  local cwd="${EHA_AGENT_CWD:-${EHA_CLAUDE_CWD:-$PWD}}"
  local stdout
  local mcp_config_arg="$mcp_config"
  local effective_allowed_tools="$allowed_builtins"
  effective_allowed_tools="$(append_csv "$effective_allowed_tools" "$allowed_mcp_tools")"
  if [[ -n "$mcp_servers" ]]; then
    mcp_config_arg="$(mktemp "${TMPDIR:-/tmp}/eha-claude-mcp.XXXXXX.json")"
    TEMP_FILES+=("$mcp_config_arg")
    local server_args=()
    read -r -a server_args <<< "$mcp_servers"
    local gen_cmd=(python3 "$(dirname "${BASH_SOURCE[0]}")/mcp-config.py" --format claude)
    if [[ "$allowed_mcp_tools_set" == "true" ]]; then
      gen_cmd+=(--allowed-mcp-tools "$allowed_mcp_tools")
    fi
    gen_cmd+=("$mcp_config_arg" "${server_args[@]}")
    "${gen_cmd[@]}"
  fi
  local cmd=("$bin" "-p" "--model" "$model" "--effort" "$effort"
             "--input-format" "stream-json" "--output-format" "stream-json" "--verbose")
  if [[ -n "$system_prompt_replace" ]]; then
    cmd+=("--system-prompt" "$system_prompt_replace")
  fi
  if [[ -n "$system_prompt" ]]; then
    cmd+=("--append-system-prompt" "$system_prompt")
  fi
  if [[ -n "$json_schema" ]]; then
    cmd+=("--json-schema" "$json_schema")
  fi
  if [[ -n "$effective_allowed_tools" ]]; then
    cmd+=("--allowedTools" "$effective_allowed_tools")
  fi
  if [[ -n "$mcp_config_arg" ]]; then
    cmd+=("--mcp-config" "$mcp_config_arg")
  fi
  stdout="$(claude_message | (cd "$cwd" && "${cmd[@]}"))"
  # Mirror run_codex()'s contract: full raw stream-json transcript goes to
  # stderr (callers that need tool_use/tool_result events, e.g. loop.py's
  # facts extraction, read it from there), extracted structured payload
  # goes to stdout.
  printf '%s\n' "$stdout" >&2
  printf '%s' "$stdout" | extract_result_json
}

run_codex() {
  [[ "$allowed_builtins_set" != "true" ]] || die "--allowed-builtins is not supported for codex in invoke-agent.sh yet"
  [[ -z "$mcp_config" ]] || die "--mcp-config is not supported for codex in invoke-agent.sh; use --mcp-servers"
  [[ "$content_json_set" != "true" ]] || die "--content-json is not supported for codex in invoke-agent.sh yet"

  local bin="${EHA_CODEX_BIN:-${CODEX_BIN:-}}"
  if [[ -z "$bin" ]]; then
    if [[ -x /data/codex-cli/bin/codex ]]; then
      bin="/data/codex-cli/bin/codex"
    else
      bin="codex"
    fi
  fi
  local cwd="${EHA_AGENT_CWD:-${EHA_CODEX_CWD:-$PWD}}"
  local full_prompt="$prompt"
  local profile_name=""
  if [[ -n "$system_prompt" ]]; then
    full_prompt="${system_prompt}"$'\n\n'"${full_prompt}"
  fi

  local cmd=("$bin" "exec" "--skip-git-repo-check" "-C" "$cwd"
             "--model" "$model" "--config" "model_reasoning_effort=$effort")
  if [[ -n "$system_prompt_replace" ]]; then
    local instructions_path
    instructions_path="$(mktemp "${TMPDIR:-/tmp}/eha-codex-system-prompt.XXXXXX.md")"
    TEMP_FILES+=("$instructions_path")
    printf '%s' "$system_prompt_replace" > "$instructions_path"
    cmd+=("--config" "model_instructions_file=\"$instructions_path\"")
  fi
  if [[ -n "$mcp_servers" ]]; then
    local codex_home="${CODEX_HOME:-${HOME:-/data}/.codex}"
    mkdir -p "$codex_home"
    profile_name="eha-mcp-$RANDOM-$$-$(date +%s%N)"
    local profile_path="$codex_home/$profile_name.config.toml"
    local server_args=()
    read -r -a server_args <<< "$mcp_servers"
    local gen_cmd=(python3 "$(dirname "${BASH_SOURCE[0]}")/mcp-config.py" --format codex)
    if [[ "$allowed_mcp_tools_set" == "true" ]]; then
      gen_cmd+=(--allowed-mcp-tools "$allowed_mcp_tools")
    fi
    gen_cmd+=("$profile_path" "${server_args[@]}")
    "${gen_cmd[@]}"
    TEMP_FILES+=("$profile_path")
    cmd+=("--profile" "$profile_name")
  fi
  if [[ -n "$json_schema" ]]; then
    # Keep process substitution here intentionally: this is the contract the
    # wrapper exists to hide from callers, and it was verified from a Bash file.
    "${cmd[@]}" --output-schema <(printf '%s' "$json_schema") -o >(cat) "$full_prompt" 1>&2
  else
    "${cmd[@]}" -o >(cat) "$full_prompt" 1>&2
  fi
}

run_agy() {
  [[ "$allowed_builtins_set" != "true" ]] || die "--allowed-builtins is not supported for agy in invoke-agent.sh yet"
  [[ -z "$mcp_config" ]] || die "--mcp-config is not supported for agy in invoke-agent.sh yet"
  [[ "$content_json_set" != "true" ]] || die "--content-json is not supported for agy in invoke-agent.sh yet"

  local bin="${EHA_ANTIGRAVITY_BIN:-${AGY_BIN:-agy}}"
  local agy_home="${EHA_ANTIGRAVITY_HOME:-${HOME:-/data/}}"
  local site_dir=""
  local project_arg=()
  if [[ -n "$mcp_servers" && -z "$agent_site" ]]; then
    die "--agent-site is required for agy MCP config"
  fi
  if [[ -n "$agent_site" ]]; then
    case "$agent_site" in
      observe|explore|reflect|web|social|chat|game) ;;
      *) die "--agent-site must be one of observe/explore/reflect/web/social/chat/game" ;;
    esac
    local base_cwd="${EHA_AGENT_CWD:-${EHA_CLAUDE_CWD:-$PWD}}"
    site_dir="$base_cwd/$agent_site"
    mkdir -p "$site_dir/.agents"
  fi
  if [[ -n "$mcp_servers" ]]; then
    local server_args=()
    read -r -a server_args <<< "$mcp_servers"
    local gen_cmd=(python3 "$(dirname "${BASH_SOURCE[0]}")/mcp-config.py" --format agy)
    if [[ "$allowed_mcp_tools_set" == "true" ]]; then
      gen_cmd+=(--allowed-mcp-tools "$allowed_mcp_tools")
    fi
    gen_cmd+=("$site_dir/.agents/mcp_config.json" "${server_args[@]}")
    "${gen_cmd[@]}"

    # headless実行の実行承認グラントを、接続サーバー単位のワイルドカード
    # mcp(server/*)で導出する。完全一致(mcp(server/tool))にしない理由:
    # agy 1.1.3はincludeToolsの可視性制限を実効させておらず、モデルがグラント外の
    # ツール名を1回でも呼ぶとprintモードがターン全体を打ち切る(実測、2026-07-17)。
    # ワイルドカードなら未知ツール名はMCPサーバー側の「未知のツール」エラーとして
    # 返り、モデルは続行できる。ツール単位の安全境界はサーバー側ゲート
    # (http_postのtools/list掲載制御・hacontrolのquiet gate等)が担う。
    # includeTools強制と「拒否=ターン死」挙動がagy側で修正されたら、完全一致への
    # 引き締めを再検討する(この粒度はワークアラウンド。グラント配布という行為
    # 自体はagyのheadless権限モデルが要求する恒久処理)。
    local grants=""
    local server
    for server in "${server_args[@]}"; do
      grants+="mcp(${server}/*)"$'\n'
    done
    ensure_agy_permission_grants "$agy_home" "$grants"
  fi
  local full_prompt="$prompt"
  local stdout
  if [[ -n "$system_prompt_replace" ]]; then
    full_prompt="[System Instruction]"$'\n'"${system_prompt_replace}"$'\n\n'"[User Prompt]"$'\n'"${full_prompt}"
  fi
  if [[ -n "$system_prompt" ]]; then
    full_prompt="あなたへの指示:"$'\n'"${system_prompt}"$'\n\n'"${full_prompt}"
  fi
  if [[ -n "$json_schema" ]]; then
    full_prompt="${full_prompt}"$'\n\n'"出力は次のJSON Schemaに厳密に従ってください。JSON以外は一切含めないでください。"$'\n'"${json_schema}"$'\nJSON:\n'
  fi
  if [[ -n "$mcp_servers" ]]; then
    local project_id_file="$site_dir/.eha_project_id"
    local project_id=""
    if [[ -s "$project_id_file" ]]; then
      project_id="$(head -n 1 "$project_id_file" | tr -d '[:space:]')"
      project_arg=(--project "$project_id")
      stdout="$(cd "$site_dir" && HOME="$agy_home" "$bin" "${project_arg[@]}" --model "$model" -p "$full_prompt")"
    else
      local projects_dir="$agy_home/.gemini/config/projects"
      mkdir -p "$projects_dir"
      local before_file
      before_file="$(mktemp "${TMPDIR:-/tmp}/eha-agy-projects-before.XXXXXX")"
      TEMP_FILES+=("$before_file")
      local lock_file="$projects_dir/.eha-registration.lock"
      stdout="$(
        (
          flock -x 200
          if [[ -s "$project_id_file" ]]; then
            project_id="$(head -n 1 "$project_id_file" | tr -d '[:space:]')"
            cd "$site_dir" && HOME="$agy_home" "$bin" --project "$project_id" --model "$model" -p "$full_prompt"
          else
            find "$projects_dir" -maxdepth 1 -type f -printf '%f\n' | sort > "$before_file"
            cd "$site_dir" && HOME="$agy_home" "$bin" --new-project --model "$model" -p "$full_prompt"
            project_id="$(detect_new_agy_project_id "$projects_dir" "$before_file" "$site_dir")"
            printf '%s\n' "$project_id" > "$project_id_file.tmp.$$"
            mv "$project_id_file.tmp.$$" "$project_id_file"
          fi
        ) 200>"$lock_file"
      )"
    fi
  elif [[ -n "$site_dir" ]]; then
    stdout="$(cd "$site_dir" && HOME="$agy_home" "$bin" --model "$model" -p "$full_prompt")"
  else
    stdout="$(HOME="$agy_home" "$bin" --model "$model" -p "$full_prompt")"
  fi
  printf '%s' "$stdout" | extract_result_json
}

case "$harness" in
  claude) run_claude ;;
  codex) run_codex ;;
  agy) run_agy ;;
esac
