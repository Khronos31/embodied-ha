#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: invoke-agent.sh [options] [prompt]

Options:
  --model default|lite        Logical model tier (default: default)
  --json-schema JSON         Structured output schema JSON
  --system-prompt TXT        Replace the harness's main system instruction (Claude native
                             --system-prompt; Codex via model_instructions_file; Antigravity
                             via prompt prefix approximation)
  --append-system-prompt TXT Append to the harness/system prompt (Claude native
                             --append-system-prompt; Codex/Antigravity via prompt prefix)
  --allowed-builtins CSV     Harness-agnostic capability intent (currently: Read,
                             WebSearch). claude=native --allowed-builtins; codex=WebSearch
                             gates native web_search, Read is served by the files MCP;
                             agy=Read via files MCP, WebSearch grant is 2.1.0 (§8).
  --no-tools                 Disable all built-in and MCP tool execution. Mutually
                             exclusive with capability/MCP options. Antigravity
                             requires a dedicated --agent-site for project policy.
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
  --transcript-file PATH     Write Claude's raw stream-json transcript to PATH
                             instead of stderr (best effort)
  -h, --help                 Show this help

Harness selection comes from EHA_AGENT_HARNESS.
Removed: --allowed-tools / --allowedTools. Use --allowed-builtins and
--allowed-mcp-tools separately.
EOF
}

die() {
  echo "invoke-agent.sh: $*" >&2
  exit 2
}

TEMP_FILES=()
TEMP_DIRS=()
cleanup_temp_files() {
  local path
  for path in "${TEMP_FILES[@]}"; do
    [[ -n "$path" ]] && rm -f "$path"
  done
  for path in "${TEMP_DIRS[@]}"; do
    if [[ -n "$path" && "$(basename "$path")" == eha-content-* ]]; then
      rm -rf -- "$path"
    fi
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
system_prompt=""
system_prompt_replace=""
allowed_builtins=""
allowed_builtins_set="false"
no_tools="false"
allowed_mcp_tools=""
allowed_mcp_tools_set="false"
mcp_config=""
mcp_servers=""
agent_site=""
content_json=""
content_json_file=""
content_json_set="false"
transcript_file=""
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
    --no-tools)
      no_tools="true"
      shift
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
    --transcript-file)
      (($# >= 2)) || die "--transcript-file requires a value"
      transcript_file="$2"
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
if [[ "$no_tools" == "true" && ( "$allowed_builtins_set" == "true" || "$allowed_mcp_tools_set" == "true" || -n "$mcp_config" || -n "$mcp_servers" ) ]]; then
  die "--no-tools cannot be combined with capability or MCP options"
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

selected_harness="${EHA_AGENT_HARNESS:-claude}"

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
extract_result_json() {
  python3 -c '
import json, re, sys
raw = sys.stdin.read().strip()
result = ""
recognized_envelope = False
for line in raw.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except Exception:
        continue
    is_agy_envelope = (
        isinstance(event, dict)
        and "conversation_id" in event
        and "status" in event
        and "response" in event
    )
    if is_agy_envelope and event.get("status") != "SUCCESS":
        detail = event.get("error") or event.get("status") or "unknown error"
        print(f"invoke-agent.sh: agy structured output failed: {detail}", file=sys.stderr)
        sys.exit(1)
    if isinstance(event, dict) and event.get("structured_output") is not None:
        # Claude stream-json and Antigravity 1.1.8+ JSON output both expose
        # the schema-validated payload here. The Antigravity JSON envelope has
        # no `type: result`, so key off the payload itself first.
        recognized_envelope = True
        structured = event.get("structured_output")
        result = json.dumps(structured, ensure_ascii=False)
    elif isinstance(event, dict) and event.get("type") == "result":
        recognized_envelope = True
        result = event.get("result", "")
    elif is_agy_envelope:
        # Antigravity native JSON without a structured payload. Preserve its
        # final text response, but do not mistake the outer metadata envelope
        # for the requested JSON when the response is empty.
        recognized_envelope = True
        result = event.get("response", "")
    elif isinstance(event, str) and event.lstrip()[:1] in ("{", "["):
        # agy may JSON-encode its schema-shaped final response as a string.
        # ⚠️ 中身が JSON のときだけ拾う。素の文字列まで拾うと、**整形された JSON の
        # 配列要素の行**が単独で有効な JSON 文字列であるために、直前に読んだオブジェクト
        # 全体を上書きしてしまう。
        #
        #   {                                ← この行でオブジェクトを得るが
        #     "topic": "...",
        #     "tags": [
        #       "example"                    ← この行が文字列として拾われ、上書きされる
        #     ]
        #   }
        #
        # 配列を持つのは observe のスキーマだけなので、この取り違えは observe にだけ出る。
        result = event
if not result and not recognized_envelope:
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

# agy 1.1.4以降のheadless(-p)モードはsettings.jsonのpermissionsを反映する。
# native command/write_file は恒久denyする。native read_file は原則 files MCP に寄せるが、
# agy 1.1.6+ が標準許可する system temp の view_file は concentrate_hearing の同一ターン
# 音声確認に必要なため残す。MCP config/認証/環境変数を守る /config・/data・/proc 等は
# 明示denyし、その他のnon-workspace readはheadlessで確認不能→自動拒否に戻す。
# 2.0.14が追加した read_file(*) deny と、それ以前にEHAが自動配布した同名allowは
# v1 marker以前の一回だけ安全な順序で除去する(F-141 live canary)。
ensure_agy_native_safety_policy() {
  local agy_home="$1"
  local settings_dir="$agy_home/.gemini/antigravity-cli"
  local config_dir="$agy_home/.gemini/config"
  mkdir -p "$settings_dir" "$config_dir"
  local lock_file="$agy_home/.gemini/.eha-native-safety-policy.lock"
  (
    flock -x 200
    AGY_SETTINGS_JSON="$settings_dir/settings.json" \
      AGY_CONFIG_JSON="$config_dir/config.json" \
      AGY_POLICY_MARKER="$settings_dir/.eha-native-read-policy-v1" \
      python3 - <<'PY'
import json
import os
import sys
import tempfile

settings_path = os.environ["AGY_SETTINGS_JSON"]
config_path = os.environ["AGY_CONFIG_JSON"]
marker_path = os.environ["AGY_POLICY_MARKER"]
legacy_rule = "read_file(*)"
required_denies = (
    "command(*)",
    "write_file(*)",
    "read_file(/config)",
    "read_file(/data)",
    "read_file(/proc)",
    "read_file(/root)",
    "read_file(/run/secrets)",
)


def fail(message, path):
    print(
        f"invoke-agent.sh: agy native safety policy failed: {message} ({path})",
        file=sys.stderr,
    )
    sys.exit(1)


def load_object(path, label):
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except ValueError as e:
        fail(f"existing {label} is not valid JSON: {e}", path)
    if not isinstance(value, dict):
        fail(f"existing {label} root is not an object", path)
    return value, True


def write_json_atomic(path, value, existed, default_mode=0o600):
    mode = os.stat(path).st_mode & 0o777 if existed else default_mode
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


settings, settings_existed = load_object(settings_path, "settings.json")
permissions = settings.setdefault("permissions", {})
if not isinstance(permissions, dict):
    fail("permissions is not an object", settings_path)
deny = permissions.setdefault("deny", [])
if not isinstance(deny, list):
    fail("permissions.deny is not a list", settings_path)

first_migration = not os.path.exists(marker_path)
config = None
config_existed = False
config_changed = False
if first_migration:
    # 両ファイルを検証し終えるまで一切書かない。config側の旧allowを先に消し、
    # settings側の旧denyを最後に消すので、途中終了しても unrestricted read にならない。
    config, config_existed = load_object(config_path, "config.json")
    user_settings = config.get("userSettings")
    if user_settings is not None and not isinstance(user_settings, dict):
        fail("userSettings is not an object", config_path)
    grants = user_settings.get("globalPermissionGrants") if user_settings else None
    if grants is not None and not isinstance(grants, dict):
        fail("userSettings.globalPermissionGrants is not an object", config_path)
    config_allow = grants.get("allow") if grants else None
    if config_allow is not None and not isinstance(config_allow, list):
        fail("userSettings.globalPermissionGrants.allow is not a list", config_path)
    settings_allow = permissions.get("allow")
    if settings_allow is not None and not isinstance(settings_allow, list):
        fail("permissions.allow is not a list", settings_path)
    if config_allow is not None and legacy_rule in config_allow:
        grants["allow"] = [rule for rule in config_allow if rule != legacy_rule]
        config_changed = True

changed = False
if first_migration:
    settings_allow = permissions.get("allow")
    if settings_allow is not None and legacy_rule in settings_allow:
        permissions["allow"] = [rule for rule in settings_allow if rule != legacy_rule]
        changed = True
    if legacy_rule in deny:
        deny[:] = [rule for rule in deny if rule != legacy_rule]
        changed = True
if settings.get("allowNonWorkspaceAccess") is not False:
    settings["allowNonWorkspaceAccess"] = False
    changed = True
for rule in required_denies:
    if rule not in deny:
        deny.append(rule)
        changed = True

if config_changed:
    write_json_atomic(config_path, config, config_existed)
if changed:
    write_json_atomic(settings_path, settings, settings_existed)
if first_migration:
    write_json_atomic(
        marker_path,
        {
            "version": 1,
            "reason": "F-141 scoped native read policy",
        },
        False,
    )
PY
  ) 200>"$lock_file"
}

# no-tools呼び出しは通常siteの権限を狭めない。専用siteのproject policyだけへ
# denyをadd-onlyで置き、global grantが残っていてもdaybook等の純粋な生成処理から
# native/MCP操作を実行できないようにする。
ensure_agy_no_tools_policy() {
  local settings_path="$1"
  mkdir -p "$(dirname "$settings_path")"
  local lock_file="${settings_path}.lock"
  (
    flock -x 200
    AGY_SITE_SETTINGS_JSON="$settings_path" python3 - <<'PY'
import json
import os
import tempfile

path = os.environ["AGY_SITE_SETTINGS_JSON"]
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)
    if not isinstance(settings, dict):
        raise SystemExit(f"invoke-agent.sh: agy site settings root is not an object ({path})")
else:
    settings = {}

permissions = settings.setdefault("permissions", {})
if not isinstance(permissions, dict):
    raise SystemExit(f"invoke-agent.sh: agy site permissions is not an object ({path})")
deny = permissions.setdefault("deny", [])
if not isinstance(deny, list):
    raise SystemExit(f"invoke-agent.sh: agy site permissions.deny is not a list ({path})")

required = (
    "command(*)",
    "write_file(*)",
    "read_file(*)",
    "read_url(*)",
    "execute_url(*)",
    "browser(*)",
    "mcp(*)",
    "unsandboxed(*)",
    "escalate_admin(*)",
)
changed = False
for rule in required:
    if rule not in deny:
        deny.append(rule)
        changed = True

if changed or not os.path.exists(path):
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
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

# agy 1.1.3のheadless(-p)モードは、MCPツール実行の確認をsettings.jsonの
# permissions.allowではなく、config.jsonのuserSettings.globalPermissionGrants
# (grant store)で判定する(settings.json側はエラーメッセージの案内に反して
# 無視される。実機切り分け済み)。ここで必要なグラントを
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
    # 他のキーを失う。fail-closedで止めて診断に乗せる。
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

prepare_nonclaude_content() {
  local root="${EHA_TMP_DIR:-${TMPDIR:-/tmp}}"
  local helper
  helper="$(dirname "${BASH_SOURCE[0]}")/content_blocks.py"
  mkdir -p "$root"
  python3 "$helper" --cleanup-stale "$root"
  content_work_dir="$(mktemp -d "$root/eha-content-XXXXXX")"
  TEMP_DIRS+=("$content_work_dir")
  if [[ -n "$content_json_file" ]]; then
    python3 "$helper" "$content_json_file" "$content_work_dir"
  else
    printf '%s' "$content_json" | python3 "$helper" - "$content_work_dir"
  fi
  mapfile -t content_image_paths < <(
    find "$content_work_dir" -maxdepth 1 -type f -name 'image-*' -print | sort
  )
}

validate_nonclaude_prompt_size() {
  local size
  size="$(printf '%s' "$1" | wc -c)"
  ((size <= 98304)) || die "expanded non-Claude prompt exceeds 96 KiB"
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
             "--input-format" "stream-json" "--output-format" "stream-json" "--verbose"
             "--disallowedTools" "Bash")
  if [[ "$no_tools" == "true" ]]; then
    local empty_mcp_config
    empty_mcp_config="$(mktemp "${TMPDIR:-/tmp}/eha-claude-no-tools.XXXXXX.json")"
    TEMP_FILES+=("$empty_mcp_config")
    printf '%s\n' '{"mcpServers":{}}' >"$empty_mcp_config"
    cmd+=("--tools" "" "--strict-mcp-config" "--mcp-config" "$empty_mcp_config")
  fi
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
  # DISABLE_UPDATES=1: 管理下の DIY バイナリが自分で入れ替わらないようにする。
  # claude_setup.runtime_env() が同じ保証を宣言し(呼び出し側が 0 を渡しても 1 に
  # 上書きする)テストもあるが、この実行経路からは呼ばれておらず環境をそのまま
  # 継承していた——宣言だけあって効いていない状態だった。ここで実際に適用する。
  # これが無いと、ピン記録は「入れたはずの版」を指したまま実物だけが進みうる。
  stdout="$(claude_message | (cd "$cwd" && DISABLE_UPDATES=1 "${cmd[@]}"))"
  if [[ -n "$transcript_file" ]]; then
    if ! printf '%s\n' "$stdout" >"$transcript_file"; then
      rm -f -- "$transcript_file"
      echo "invoke-agent.sh: warning: failed to write transcript file: $transcript_file" >&2
    fi
  fi
  printf '%s' "$stdout" | extract_result_json
}

run_codex() {
  # --allowed-builtins は全ハーネス共通の能力意図(Read/WebSearch)。codex では Read は files MCP が
  # 担うため no-op、WebSearch は下で codex native の web_search を制御する(die しない)。
  [[ -z "$mcp_config" ]] || die "--mcp-config is not supported for codex in invoke-agent.sh; use --mcp-servers"
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
  local content_work_dir=""
  local content_image_paths=()
  local profile_name=""
  if [[ "$content_json_set" == "true" ]]; then
    prepare_nonclaude_content
    full_prompt="$(cat "$content_work_dir/codex-prompt.txt")"
  fi
  if [[ -n "$system_prompt" ]]; then
    full_prompt="${system_prompt}"$'\n\n'"${full_prompt}"
  fi

  local cmd=("$bin" "exec" "--skip-git-repo-check" "-C" "$cwd"
             "--model" "$model" "--config" "model_reasoning_effort=$effort")
  local image_path
  for image_path in "${content_image_paths[@]}"; do
    cmd+=("--image" "$image_path")
  done
  # F11-B1:
  # codex 既定は built-in 実行系(exec_command/apply_patch/write_stdin)と ChatGPT apps(codex_apps・
  # ユーザーの CODEX_HOME 認証由来)を露出する。住み込み個体には不要かつ危険なので明示 hardening する。
  #   --sandbox read-only    : default/global config のドリフトに依存せず書込みを塞ぐ(exec は残るが write 不可)
  #   --disable apps         : mcp__codex_apps__* を除去(feature フラグ由来なので user-config 非依存)
  #   --disable shell_tool   : exec_command / write_stdin を除去
  #   --disable image_generation/goals/multi_agent/tool_suggest : chat に不要な surface を削減
  # exec(code-mode の MCP 呼出し中核)と apply_patch は残るが、read-only sandbox で書込みは拒否される。
  # flag 実在は codex 0.144.4 の --help / features list で確認済。
  # ★--ignore-user-config は「使わない」: EHA は MCP を CODEX_HOME 内の transient --profile で渡すため、
  #   --ignore-user-config を付けると profile ごと無視され MCP tool も developer_instruction も全滅する
  #   (付けると read_file 呼び出しが消え、外すと読取に成功する)。
  #   addon の CODEX_HOME は DIY で個人 global 設定を持たないため
  #   継承リスクは元々低く、codex_apps 除去は --disable apps が担う。厳密な user-config 隔離が要る場合は
  #   MCP を --profile でなく inline -c で渡す別実装が要る(将来課題・レポート§8/B-1)。
  cmd+=("--sandbox" "read-only"
        "--disable" "apps" "--disable" "shell_tool"
        "--disable" "image_generation" "--disable" "goals"
        "--disable" "multi_agent" "--disable" "tool_suggest")
  if [[ "$no_tools" == "true" ]]; then
    # no-tools経路にはMCP profileが無いので、通常経路では使えない
    # --ignore-user-configを安全に使える。built-in実行・閲覧系もfeature単位で落とす。
    cmd+=("--ignore-user-config"
          "--disable" "unified_exec" "--disable" "code_mode_host"
          "--disable" "computer_use" "--disable" "browser_use"
          "--disable" "browser_use_external" "--disable" "browser_use_full_cdp_access")
  fi
  # WebSearch 意図があれば codex native の live web_search を有効化、無ければ無効化して
  # claude chat(WebSearch 非許可)とのパリティを取る。--allowed-builtins 未指定時は codex 既定に任せる。
  # validate_allowed_builtins が要素を trim して受理する("Read, WebSearch"等)ため、判定前に空白を除去して
  # 正規化する(生CSV部分一致だと空白付き要素を取りこぼす)。
  if [[ "$no_tools" == "true" ]]; then
    cmd+=("--config" "web_search=disabled")
  elif [[ "$allowed_builtins_set" == "true" ]]; then
    local _ab_norm="${allowed_builtins//[[:space:]]/}"
    if [[ ",$_ab_norm," == *",WebSearch,"* ]]; then
      cmd+=("--config" "web_search=live")
    else
      cmd+=("--config" "web_search=disabled")
    fi
  fi
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
  # json_schema は --output-schema(OpenAI strict response_format)ではなく prompt へ埋め込む(F5)。
  # EHA のスキーマは任意キーの object(cameras_add 等の {"type":"object"})を含み、OpenAI strict モード
  # (全 object に additionalProperties:false 必須=任意キー不可)では表現できないため。agy と同じ
  # prompt-injection にし、最終メッセージは -o で回収する(claude は native --json-schema のまま)。
  if [[ -n "$json_schema" ]]; then
    full_prompt="${full_prompt}"$'\n\n'"出力は次のJSON Schemaに厳密に従ってください。JSON以外は一切含めないでください。"$'\n'"${json_schema}"$'\nJSON:\n'
  fi
  validate_nonclaude_prompt_size "$full_prompt"
  # -o >(cat) の process substitution は wrapper が隠す契約(Bash ファイルから検証済み)。
  "${cmd[@]}" -o >(cat) "$full_prompt" 1>&2
}

run_agy() {
  # --allowed-builtins Read は呼び出し側で files MCP に写像する。agy native read_file は
  # settings.json で deny し、機密パス policy を迂回させない。
  # WebSearch(agy native)の headless 許可形式は未確定(§8)で 2.1.0。ここでは die せず受理する。
  [[ -z "$mcp_config" ]] || die "--mcp-config is not supported for agy in invoke-agent.sh yet"
  local bin="${EHA_ANTIGRAVITY_BIN:-${AGY_BIN:-agy}}"
  local agy_home="${EHA_ANTIGRAVITY_HOME:-${HOME:-/data/}}"
  local structured_args=()
  # Antigravity 1.1.8 added native structured output. Use it for the F-51
  # daybook path whose exact schema is live-verified.
  #
  # ⚠️ **loop の各モードへは広げられない。** MCP サーバーを繋いだ状態では
  # `--output-format json` が `structured_output` を返さない
  # （MCP 無し=返る / MCP 有りはツールの成否によらず返らない）。
  # loop は MCP を繋ぐ（`loop.py` の `--mcp-servers`）ので、native 化すると
  # 応答が空になり invoke 失敗になる。daybook が成立しているのは MCP を繋がないため。
  #
  # 旧コメントは「loop schemas の nullable type union を 1.1.9 が拒否する」と説明していたが、
  # これは誤り（1.1.9 / 1.1.12 とも union は通る。拒否されるのは enum 内 null だけ）。
  # 結論（loop は prompt 埋め込みのまま）は正しく、理由が違っていた。
  if [[ "$agent_site" == "daybook" && -n "$json_schema" ]]; then
    local agy_help
    agy_help="$("$bin" --help 2>&1 || true)"
    if grep -q -- '--output-format' <<< "$agy_help" \
        && grep -q -- '--json-schema' <<< "$agy_help"; then
      structured_args=(--output-format json --json-schema "$json_schema")
    fi
  fi
  ensure_agy_native_safety_policy "$agy_home"
  local site_dir=""
  local schema_manifest_path=""
  local project_arg=()
  if [[ -n "$mcp_servers" && -z "$agent_site" ]]; then
    die "--agent-site is required for agy MCP config"
  fi
  if [[ -n "$agent_site" ]]; then
    case "$agent_site" in
      observe|explore|reflect|web|social|chat|game|daybook) ;;
      *) die "--agent-site must be one of observe/explore/reflect/web/social/chat/game/daybook" ;;
    esac
    local base_cwd="${EHA_AGENT_CWD:-${EHA_CLAUDE_CWD:-$PWD}}"
    site_dir="$base_cwd/$agent_site"
    mkdir -p "$site_dir/.agents"
  fi
  if [[ "$no_tools" == "true" ]]; then
    [[ -n "$site_dir" ]] || die "--agent-site is required for agy --no-tools"
    ensure_agy_no_tools_policy "$site_dir/.agents/settings.json"
  fi
  if [[ -n "$mcp_servers" ]]; then
    local server_args=()
    read -r -a server_args <<< "$mcp_servers"
    local gen_cmd=(python3 "$(dirname "${BASH_SOURCE[0]}")/mcp-config.py" --format agy)
    local credential_dir="$agy_home/.gemini/antigravity-cli/eha-mcp-credentials"
    mkdir -p "$credential_dir"
    chmod 700 "$credential_dir"
    local credential_file
    credential_file="$(mktemp "$credential_dir/${agent_site}.XXXXXX.json")"
    TEMP_FILES+=("$credential_file")
    gen_cmd+=(--credential-file "$credential_file")
    if [[ "$allowed_mcp_tools_set" == "true" ]]; then
      gen_cmd+=(--allowed-mcp-tools "$allowed_mcp_tools")
    fi
    gen_cmd+=("$site_dir/.agents/mcp_config.json" "${server_args[@]}")
    "${gen_cmd[@]}"
    schema_manifest_path="$site_dir/.eha-mcp-tool-schemas.json"
    python3 "$(dirname "${BASH_SOURCE[0]}")/mcp-schema-manifest.py" \
      "$site_dir/.agents/mcp_config.json" \
      "$schema_manifest_path"

    # headless実行の実行承認グラントを、接続サーバー単位のワイルドカード
    # mcp(server/*)で導出する。完全一致(mcp(server/tool))にしない理由:
    # agy 1.1.3はincludeToolsの可視性制限を実効させておらず、モデルがグラント外の
    # ツール名を1回でも呼ぶとprintモードがターン全体を打ち切る。
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
  local content_work_dir=""
  local content_image_paths=()
  if [[ "$content_json_set" == "true" ]]; then
    prepare_nonclaude_content
    full_prompt="$(cat "$content_work_dir/agy-prompt.txt")"
  fi
  local stdout
  if [[ -n "$system_prompt_replace" ]]; then
    full_prompt="[System Instruction]"$'\n'"${system_prompt_replace}"$'\n\n'"[User Prompt]"$'\n'"${full_prompt}"
  fi
  if [[ -n "$system_prompt" ]]; then
    full_prompt="あなたへの指示:"$'\n'"${system_prompt}"$'\n\n'"${full_prompt}"
  fi
  if [[ -n "$mcp_servers" ]]; then
    # agy headless は未承認の native command/write_file をモデルが選ぶと、確認を出せず
    # ターン全体を空応答で終了する。接続済みMCPへ直行させ、許可済みの
    # read_file/WebSearch等まで禁止しない。ツール失敗時の補完も防ぐ。
    full_prompt="${full_prompt}"$'\n\n'"【Antigravity headlessでのツール利用】"$'\n'"接続済みMCPツールの正規description/inputSchemaは、秘密を除いた次のmanifestにあります: @${schema_manifest_path}"$'\n'"MCPツールを呼ぶ前にmanifestの該当項目を確認し、required・enum・型を厳守してください。.agents/mcp_config.jsonはserver起動配線でありSchemaの正本ではないため、調査対象にしないでください。必要な操作には、manifestに掲載された接続済みMCPツール、またはこのターンで明示的に許可された組み込みツール（read_file、WebSearch等）を直接使用してください。native command、write_file、shell、terminal、またはPythonスクリプトで代替してはいけません。利用可能なツールで確認できない事実は推測で補わず、確認できた範囲だけで処理を続けて、必ず指定された出力形式で最終応答を返してください。"
  fi
  if [[ -n "$json_schema" ]]; then
    full_prompt="${full_prompt}"$'\n\n'"出力は次のJSON Schemaに厳密に従ってください。JSON以外は一切含めないでください。"$'\n'"${json_schema}"$'\nJSON:\n'
  fi
  validate_nonclaude_prompt_size "$full_prompt"
  if [[ -n "$mcp_servers" ]]; then
    local project_id_file="$site_dir/.eha_project_id"
    local project_id=""
    if [[ -s "$project_id_file" ]]; then
      project_id="$(head -n 1 "$project_id_file" | tr -d '[:space:]')"
      project_arg=(--project "$project_id")
      stdout="$(cd "$site_dir" && HOME="$agy_home" "$bin" "${project_arg[@]}" --model "$model" "${structured_args[@]}" -p "$full_prompt")"
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
            cd "$site_dir" && HOME="$agy_home" "$bin" --project "$project_id" --model "$model" "${structured_args[@]}" -p "$full_prompt"
          else
            find "$projects_dir" -maxdepth 1 -type f -printf '%f\n' | sort > "$before_file"
            cd "$site_dir" && HOME="$agy_home" "$bin" --new-project --model "$model" "${structured_args[@]}" -p "$full_prompt"
            project_id="$(detect_new_agy_project_id "$projects_dir" "$before_file" "$site_dir")"
            printf '%s\n' "$project_id" > "$project_id_file.tmp.$$"
            mv "$project_id_file.tmp.$$" "$project_id_file"
          fi
        ) 200>"$lock_file"
      )"
    fi
  elif [[ -n "$site_dir" ]]; then
    stdout="$(cd "$site_dir" && HOME="$agy_home" "$bin" --model "$model" "${structured_args[@]}" -p "$full_prompt")"
  else
    stdout="$(HOME="$agy_home" "$bin" --model "$model" "${structured_args[@]}" -p "$full_prompt")"
  fi
  printf '%s' "$stdout" | extract_result_json
}

case "$harness" in
  claude) run_claude ;;
  codex) run_codex ;;
  agy) run_agy ;;
esac
