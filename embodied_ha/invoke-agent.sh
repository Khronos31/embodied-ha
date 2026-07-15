#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: invoke-agent.sh [options] [prompt]

Options:
  --model default|lite        Logical model tier (default: default)
  --json-schema JSON         Structured output schema JSON
  --sound-file PATH          Force Antigravity audio fallback and inject PATH into the prompt
  --append-system-prompt TXT System prompt text
  --allowed-tools TOOLS      Claude Code allowed tools list
  --mcp-config PATH          Claude Code MCP config path
  --content-json JSON        Claude Code stream-json content blocks
  -h, --help                 Show this help

Harness selection comes from EHA_AGENT_HARNESS unless --sound-file is present.
EOF
}

die() {
  echo "invoke-agent.sh: $*" >&2
  exit 2
}

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

json_schema=""
logical_model="default"
sound_file=""
system_prompt=""
allowed_tools=""
mcp_config=""
content_json=""
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
    --append-system-prompt)
      (($# >= 2)) || die "--append-system-prompt requires a value"
      system_prompt="$2"
      shift 2
      ;;
    --allowed-tools|--allowedTools)
      (($# >= 2)) || die "$1 requires a value"
      allowed_tools="$2"
      shift 2
      ;;
    --mcp-config)
      (($# >= 2)) || die "--mcp-config requires a value"
      mcp_config="$2"
      shift 2
      ;;
    --content-json)
      (($# >= 2)) || die "--content-json requires a value"
      content_json="$2"
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
  # TODO: This is path injection only. The Web UI should tell users that audio
  # multimodal processing falls back to Antigravity, and future work should
  # replace this with a real audio attachment path if the harness supports it.
  prompt="${prompt}"$'\n\n'"【いま聞こえた音】"$'\n'"${sound_file}"
fi

selected_harness="${EHA_AGENT_HARNESS:-}"
if [[ -z "$selected_harness" && -n "${EHA_SESSION_BIN:-}" ]]; then
  case "$(basename "$EHA_SESSION_BIN")" in
    claude) selected_harness="claude" ;;
    codex) selected_harness="codex" ;;
    agy|agy.real) selected_harness="agy" ;;
  esac
fi
selected_harness="${selected_harness:-claude}"
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
  model="${EHA_AGY_AUDIO_MODEL:-Gemini 3.5 Flash (High)}"
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

claude_message() {
  PROMPT_TEXT="$prompt" CONTENT_JSON="$content_json" python3 -c '
import json, os, sys
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
  local bin="${EHA_CLAUDE_BIN:-${CLAUDE_BIN:-claude}}"
  local stdout
  local cmd=("$bin" "-p" "--model" "$model" "--effort" "$effort"
             "--input-format" "stream-json" "--output-format" "stream-json" "--verbose")
  if [[ -n "$system_prompt" ]]; then
    cmd+=("--append-system-prompt" "$system_prompt")
  fi
  if [[ -n "$json_schema" ]]; then
    cmd+=("--json-schema" "$json_schema")
  fi
  if [[ -n "$allowed_tools" ]]; then
    cmd+=("--allowedTools" "$allowed_tools")
  fi
  if [[ -n "$mcp_config" ]]; then
    cmd+=("--mcp-config" "$mcp_config")
  fi
  stdout="$(claude_message | "${cmd[@]}")"
  printf '%s' "$stdout" | extract_result_json
}

run_codex() {
  [[ -z "$allowed_tools" ]] || die "--allowed-tools is not supported for codex in invoke-agent.sh yet"
  [[ -z "$mcp_config" ]] || die "--mcp-config is not supported for codex in invoke-agent.sh yet"
  [[ -z "$content_json" ]] || die "--content-json is not supported for codex in invoke-agent.sh yet"

  local bin="${EHA_CODEX_BIN:-${CODEX_BIN:-codex}}"
  local cwd="${EHA_AGENT_CWD:-${EHA_CODEX_CWD:-$PWD}}"
  local full_prompt="$prompt"
  if [[ -n "$system_prompt" ]]; then
    full_prompt="${system_prompt}"$'\n\n'"${full_prompt}"
  fi

  local cmd=("$bin" "exec" "--skip-git-repo-check" "-C" "$cwd"
             "--model" "$model" "--config" "model_reasoning_effort=$effort")
  if [[ -n "$json_schema" ]]; then
    # Keep process substitution here intentionally: this is the contract the
    # wrapper exists to hide from callers, and it was verified from a Bash file.
    "${cmd[@]}" --output-schema <(printf '%s' "$json_schema") -o >(cat) "$full_prompt" 1>&2
  else
    "${cmd[@]}" -o >(cat) "$full_prompt" 1>&2
  fi
}

run_agy() {
  [[ -z "$allowed_tools" ]] || die "--allowed-tools is not supported for agy in invoke-agent.sh yet"
  [[ -z "$mcp_config" ]] || die "--mcp-config is not supported for agy in invoke-agent.sh yet"
  [[ -z "$content_json" ]] || die "--content-json is not supported for agy in invoke-agent.sh yet"

  local bin="${EHA_ANTIGRAVITY_BIN:-${AGY_BIN:-agy}}"
  local full_prompt="$prompt"
  local stdout
  if [[ -n "$system_prompt" ]]; then
    full_prompt="あなたへの指示:"$'\n'"${system_prompt}"$'\n\n'"${full_prompt}"
  fi
  if [[ -n "$json_schema" ]]; then
    full_prompt="${full_prompt}"$'\n\n'"出力は次のJSON Schemaに厳密に従ってください。JSON以外は一切含めないでください。"$'\n'"${json_schema}"$'\nJSON:\n'
  fi
  stdout="$(HOME="${EHA_ANTIGRAVITY_HOME:-${HOME:-/data/}}" "$bin" --model "$model" -p "$full_prompt")"
  printf '%s' "$stdout" | extract_result_json
}

case "$harness" in
  claude) run_claude ;;
  codex) run_codex ;;
  agy) run_agy ;;
esac
