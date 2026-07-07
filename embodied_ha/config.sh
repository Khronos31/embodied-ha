# shellcheck shell=bash
# config.sh — 環境固有設定（IP・URL・パス）。各スクリプトが source する。
# アドオンでは options から環境変数で注入し、ここのデフォルトを上書きする。
# 個人環境では下記デフォルトでそのまま動く（${VAR:-default} 形式で env 優先）。

# --- 居住者名（キャラクター定義・プロンプト内で使う）---
export RESIDENT="${RESIDENT:-ユーザー}"

# --- HA API ---
export HA_URL="${HA_URL:-http://supervisor/core/api}"

# --- カメラ（go2rtc） ---
export GO2RTC_BASE="${GO2RTC_BASE:-http://homeassistant.local:1984}"

# --- Claude Code CLI / ツール ---
export CLAUDE_BIN="${CLAUDE_BIN:-claude}"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/config/.tools/claude-home}"
export EHA_TOOLS_PATH="${EHA_TOOLS_PATH:-/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin}"

# --- Antigravity CLI（任意・後からユーザーが導入する）---
export EHA_ANTIGRAVITY_HOME="${EHA_ANTIGRAVITY_HOME:-/data/}"
export EHA_ANTIGRAVITY_BIN_DIR="${EHA_ANTIGRAVITY_BIN_DIR:-$EHA_ANTIGRAVITY_HOME/bin}"
export EHA_ANTIGRAVITY_BIN="${EHA_ANTIGRAVITY_BIN:-$EHA_ANTIGRAVITY_BIN_DIR/agy}"

# --- 音声の次回聴取予約（次回セッションに音声コンテキストを注入するための準備） ---
export EHA_NEXT_LISTEN_REQUEST_FILE="${EHA_NEXT_LISTEN_REQUEST_FILE:-${EHA_DATA_DIR:-/config/embodied-ha}/runtime/next_listen_request.json}"
export EHA_NEXT_LISTEN_LOG_FILE="${EHA_NEXT_LISTEN_LOG_FILE:-${EHA_DATA_DIR:-/config/embodied-ha}/log/next_listen_log.jsonl}"
export EHA_AUDIO_SESSION_BIN="${EHA_AUDIO_SESSION_BIN:-agy}"
export EHA_AUDIO_SESSION_MODEL="${EHA_AUDIO_SESSION_MODEL:-Gemini 3.5 Flash (High)}"

# --- メモリディレクトリ ---
export EHA_MEMORY_DIR="${EHA_MEMORY_DIR:-/config/.tools/claude-home/projects/-config/memory}"

# --- preferences.json（会話で育てる設定 / スピーカー・カメラ・在宅エンティティ等）---
_EHA_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export EHA_PREFS_FILE="${EHA_PREFS_FILE:-$_EHA_SCRIPT_DIR/preferences.json}"

# --- character.md（キャラクター定義。loop/chat/explore が cat して $CHARACTER に）---
# アドオンでは run.sh が EHA_DATA_DIR 配下のユーザー編集版を指す。dev/直接実行時は同梱版。
export EHA_CHARACTER_FILE="${EHA_CHARACTER_FILE:-$_EHA_SCRIPT_DIR/character.md}"

# --- extra_context.conf（1行1コマンド。# はコメント。出力が毎回プロンプトに追加される）---
_extra_conf="${EHA_DATA_DIR:+$EHA_DATA_DIR/extra_context.conf}"
if [ -n "$_extra_conf" ] && [ -f "$_extra_conf" ]; then
  EXTRA_CONTEXT=$(grep -v '^\s*#' "$_extra_conf" | grep -v '^\s*$' | while IFS= read -r _line; do
    bash -c "$_line" 2>/dev/null
    echo
  done)
else
  EXTRA_CONTEXT=""
fi
export EXTRA_CONTEXT
unset _extra_conf

# --- 行動ポリシー（preferences.json の policies。会話/Web UIで追記される自由記述の行動ルール）---
if [ -f "$EHA_PREFS_FILE" ]; then
  POLICIES=$(EHA_PREFS_FILE="$EHA_PREFS_FILE" python3 -c '
import json, os
try:
    with open(os.environ["EHA_PREFS_FILE"], encoding="utf-8") as f:
        prefs = json.load(f)
    lines = [f"- {p.strip()}" for p in prefs.get("policies", []) if isinstance(p, str) and p.strip()]
    print("\n".join(lines))
except Exception:
    pass
' 2>/dev/null)
else
  POLICIES=""
fi
export POLICIES
