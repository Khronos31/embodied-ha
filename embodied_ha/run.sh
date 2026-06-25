#!/bin/bash
# Embodied HA アドオン エントリポイント
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SCRIPT_DIR

echo "========================================"
echo "[run] Embodied HA 起動"
echo "========================================"

# --- アドオン options 読み込み ---
export RESIDENT
RESIDENT=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('resident_name','ユーザー'))" 2>/dev/null || echo "ユーザー")
echo "[run] RESIDENT=${RESIDENT}"

# --- 自律操作ゲート（デフォルトOFF）---
# ON のときだけ watch/explore に家電操作サーバー(ha-control)を繋ぐ＝物理的なゲート。
export EHA_AUTONOMOUS
EHA_AUTONOMOUS=$(python3 -c "import json; print('1' if json.load(open('/data/options.json')).get('autonomous_control', False) else '0')" 2>/dev/null || echo "0")
echo "[run] 自律操作: $([ "$EHA_AUTONOMOUS" = "1" ] && echo "ON（watch/explore が家電操作可）" || echo "OFF（観察・提案のみ）")"

# --- Claude 認証 ---
# 優先順位: 1) options.json の claude_api_key → 2) サブスク認証（.credentials.json）
_OPT_KEY=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('claude_api_key',''))" 2>/dev/null || echo "")
if [ -n "$_OPT_KEY" ]; then
    export ANTHROPIC_API_KEY="$_OPT_KEY"
    echo "[run] Claude: APIキー認証モード"
fi
unset _OPT_KEY

# --- HA API ---
# SUPERVISOR_TOKEN はコンテナ自動注入（HAOS アドオン）。各スクリプトはこれを直接使う。
# Core API は Supervisor プロキシ経由（http://supervisor/core/api）で叩く。
# これには config.yaml の homeassistant_api: true が必要（SUPERVISOR_TOKEN が Core API に通る）。
export HA_URL="${HA_URL:-http://supervisor/core/api}"

# --- Claude CLI ---
_OPT_CONFIG_DIR=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('claude_config_dir',''))" 2>/dev/null || echo "")
export CLAUDE_BIN="${CLAUDE_BIN:-claude}"
export EHA_TOOLS_PATH="${EHA_TOOLS_PATH:-/usr/local/bin}"
export PATH="${EHA_TOOLS_PATH}:${PATH}"

# --- PulseAudio（audio: true で注入されるソケット）---
# HAOS は PULSE_SERVER を自動セットしないため、ソケットが存在する場合は手動で設定する。
# libasound2-plugins の ALSA→Pulse ブリッジはこの変数を参照する。
# ソケットパスは HAOS バージョンで変わる可能性があるため複数パスを試す。
if [ -z "${PULSE_SERVER:-}" ]; then
    for _pulse_sock in "/run/audio/native" "/run/pulse/native" "/var/run/pulse/native" "/run/user/0/pulse/native"; do
        if [ -S "$_pulse_sock" ]; then
            export PULSE_SERVER="unix://${_pulse_sock}"
            echo "[run] PulseAudio: PULSE_SERVER=unix://${_pulse_sock}"
            break
        fi
    done
    unset _pulse_sock
fi
if [ -z "${PULSE_SERVER:-}" ]; then
    echo "[run] PulseAudio: ソケット見つからず（/run ls: $(find /run -maxdepth 1 -mindepth 1 -printf '%f ' 2>/dev/null)）"
fi

mkdir -p /data/embodied-ha

# --- 永続データの置き場（/config/embodied-ha/）---
# HA設定ディレクトリ配下に置くことで、Studio Code Server / Samba / File Editor
# から記憶・ログ・設定を直接閲覧・編集でき、HAバックアップにも含まれる。
# （config:rw マウント前提。未マウント環境では /data にフォールバック）
export EHA_DATA_DIR="${EHA_DATA_DIR:-/config/embodied-ha}"
if ! mkdir -p "$EHA_DATA_DIR" 2>/dev/null; then
    EHA_DATA_DIR="/data/embodied-ha"
    mkdir -p "$EHA_DATA_DIR"
    echo "[run] /config/embodied-ha が使えないため /data/embodied-ha にフォールバック"
fi
echo "[run] 永続データ: ${EHA_DATA_DIR}"
mkdir -p "$EHA_DATA_DIR/log"
if [ -f "$EHA_DATA_DIR/audio_log.jsonl" ] && [ ! -f "$EHA_DATA_DIR/log/audio_log.jsonl" ]; then
    mv "$EHA_DATA_DIR/audio_log.jsonl" "$EHA_DATA_DIR/log/audio_log.jsonl"
    echo "[run] migrated audio log to $EHA_DATA_DIR/log/audio_log.jsonl"
fi
export EHA_AUDIO_LOG_FILE="${EHA_AUDIO_LOG_FILE:-$EHA_DATA_DIR/log/audio_log.jsonl}"
export EHA_ACTIVE_LISTEN_LOG_FILE="${EHA_ACTIVE_LISTEN_LOG_FILE:-$EHA_DATA_DIR/log/active_listen_log.jsonl}"
echo "[run] audio log: ${EHA_AUDIO_LOG_FILE}"
echo "[run] active listen log: ${EHA_ACTIVE_LISTEN_LOG_FILE}"

# --- Claude 設定ディレクトリ ---
# デフォルトは EHA_DATA_DIR/.claude（/config/embodied-ha/.claude）。
# アンインストール時に /data/ が消えても記憶・認証が /config/ 側に残る。
# options で claude_config_dir を指定した場合はそちらを優先。
export CLAUDE_CONFIG_DIR="${_OPT_CONFIG_DIR:-${EHA_DATA_DIR}/.claude}"
unset _OPT_CONFIG_DIR
echo "[run] CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}"

# --- Claude の作業ディレクトリ ---
# 空の場合はスクリプトディレクトリ（/app）。
# /config を指定し claude_config_dir=/config/.tools/claude-home と組み合わせると
# Studio Code Server 版の Claude Code とメモリを共有できる。
_OPT_CWD=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('claude_cwd',''))" 2>/dev/null || echo "")
export EHA_CLAUDE_CWD="${_OPT_CWD:-}"
unset _OPT_CWD
[ -n "$EHA_CLAUDE_CWD" ] && echo "[run] EHA_CLAUDE_CWD=${EHA_CLAUDE_CWD}"

# --- preferences.json ---
# 会話で育てる設定（スピーカー・カメラ・在宅判定・センサー（おもなデバイス）等）。
# example はイメージ同梱なので初期化元に使う。
export EHA_PREFS_FILE="${EHA_PREFS_FILE:-$EHA_DATA_DIR/preferences.json}"

if [ ! -f "$EHA_PREFS_FILE" ]; then
    if [ -f "$SCRIPT_DIR/preferences.json.example" ]; then
        cp "$SCRIPT_DIR/preferences.json.example" "$EHA_PREFS_FILE"
        echo "[run] preferences.json を example から初期化"
    else
        echo '{}' > "$EHA_PREFS_FILE"
        echo "[run] preferences.json を空で初期化"
    fi
fi

# --- desires.json（欲求システム定義。ユーザーが JSON で編集可能）---
# 同梱デフォルト（$SCRIPT_DIR/desires.json）を初期化元に、EHA_DATA_DIR 配下へ seed-once。
# 初期化時の正規化は desire_state.py に寄せる。既存ファイルは上書きしない。
export EHA_DESIRES_FILE="${EHA_DESIRES_FILE:-$EHA_DATA_DIR/desires.json}"
if [ ! -f "$EHA_DESIRES_FILE" ]; then
    if SCRIPT_DIR="$SCRIPT_DIR" EHA_DESIRES_FILE="$EHA_DESIRES_FILE" python3 - <<'PY'
import os
import sys

script_dir = os.environ["SCRIPT_DIR"]
dest = os.environ["EHA_DESIRES_FILE"]
sys.path.insert(0, script_dir)
import desire_state  # type: ignore

desire_state.seed_catalog(os.path.join(script_dir, "desires.json"), dest)
PY
    then
        echo "[run] desires.json を desire_state.py 経由で初期化（$EHA_DESIRES_FILE）"
    else
        echo "[run] desires.json 初期化失敗（同梱デフォルトを使用）"
        export EHA_DESIRES_FILE="$SCRIPT_DIR/desires.json"
    fi
fi

# --- character.md（キャラクター定義。ユーザーが Markdown で編集可能）---
# 同梱デフォルト（$SCRIPT_DIR/character.md）を初期化元に、ユーザー編集用コピーを
# EHA_DATA_DIR 配下に置く。File Editor / VS Code / Samba から編集できる。
# 既存ユーザーファイルは上書きしない（アップデートで編集が消えないよう seed-once）。
export EHA_CHARACTER_FILE="${EHA_CHARACTER_FILE:-$EHA_DATA_DIR/character.md}"
if [ ! -f "$EHA_CHARACTER_FILE" ]; then
    if cp "$SCRIPT_DIR/character.md" "$EHA_CHARACTER_FILE" 2>/dev/null; then
        echo "[run] character.md を同梱デフォルトから初期化（$EHA_CHARACTER_FILE）"
    else
        echo "[run] character.md 初期化失敗（同梱デフォルトを使用）"
    fi
fi

# --- personal.inc（個人向け設定。なければ example から seed-once）---
# EHA_DATA_DIR 配下に置くことで File Editor から編集可能になり再ビルドでも消えない。
export EHA_PERSONAL_INC="${EHA_PERSONAL_INC:-$EHA_DATA_DIR/personal.inc}"
if [ ! -f "$EHA_PERSONAL_INC" ]; then
    if cp "$SCRIPT_DIR/personal.inc.example" "$EHA_PERSONAL_INC" 2>/dev/null; then
        echo "[run] personal.inc を example から初期化（$EHA_PERSONAL_INC）"
    fi
fi

# --- センサー・スピーカー初回自動発見 ---
# sensors か speakers のどちらかが未設定なら discover.py を走らせる。
# discover.py は sensors を下書きで置き換え、speakers は未設定のときだけ補う。
NEED_DISCOVER=$(python3 -c "
import json
try:
    d = json.load(open('$EHA_PREFS_FILE'))
except Exception:
    d = {}
sensors = sum(len(g.get('items', [])) for g in d.get('sensors', {}).get('groups', []))
speakers = len(d.get('speakers', {}) or {})
print('1' if (sensors == 0 or speakers == 0) else '0')
" 2>/dev/null || echo "1")

# --- キャラクター名（Web UIの設定画面 → preferences.json から読む）---
export EHA_CHARACTER_NAME
EHA_CHARACTER_NAME=$(python3 -c "
import json, os
try:
    d = json.load(open('$EHA_PREFS_FILE', encoding='utf-8'))
    print(d.get('character_name','') or 'Claude')
except Exception:
    print('Claude')
" 2>/dev/null || echo "Claude")
echo "[run] キャラクター名: ${EHA_CHARACTER_NAME}"

if [ "$NEED_DISCOVER" = "1" ]; then
    echo "[run] sensors/speakers 未設定。discover.py で自動発見します..."
    EHA_PREFS_FILE="$EHA_PREFS_FILE" HA_URL="$HA_URL" RESIDENT="$RESIDENT" \
        python3 "$SCRIPT_DIR/discover.py" --write \
        && echo "[run] discover.py 完了" \
        || echo "[run] discover.py 失敗（スキップ。起動は続ける）"
fi

# --- MQTT discovery（HA にエンティティを生やす）---
MQTT=$(curl -sf -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
    http://supervisor/services/mqtt 2>/dev/null || echo "")

if [ -n "$MQTT" ]; then
    read -r MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS < <(echo "$MQTT" | python3 -c '
import sys, json
d = json.load(sys.stdin)["data"]
print(d.get("host",""), d.get("port", 1883), d.get("username",""), d.get("password",""))')

    echo "[run] MQTT broker: ${MQTT_HOST}:${MQTT_PORT}"

    _pub() {
        mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" \
            -u "$MQTT_USER" -P "$MQTT_PASS" -r "$@"
    }

    # 内省ログ（watch/explore ループが書き込む）
    _pub -t "homeassistant/sensor/embodied_ha_observation/config" -m \
        '{"name":"Embodied HA 内省","unique_id":"embodied_ha_observation","state_topic":"embodied_ha/observation/state","icon":"mdi:thought-bubble","entity_category":"diagnostic"}'

    # 直近の発話
    _pub -t "homeassistant/sensor/embodied_ha_last_speak/config" -m \
        '{"name":"Embodied HA 発話","unique_id":"embodied_ha_last_speak","state_topic":"embodied_ha/last_speak/state","icon":"mdi:message-text"}'

    # 感情（照明の色変え等の自動化に使える）
    _pub -t "homeassistant/sensor/embodied_ha_emotion/config" -m \
        '{"name":"Embodied HA 感情","unique_id":"embodied_ha_emotion","state_topic":"embodied_ha/emotion/state","icon":"mdi:heart"}'

    # チャット入力（HA UI → アドオン）
    _pub -t "homeassistant/text/embodied_ha_chat/config" -m \
        '{"name":"Embodied HA チャット入力","unique_id":"embodied_ha_chat","command_topic":"embodied_ha/chat/set","state_topic":"embodied_ha/chat/state","icon":"mdi:chat","max":500}'

    # 観察トリガーボタン
    _pub -t "homeassistant/button/embodied_ha_observe/config" -m \
        '{"name":"Embodied HA 観察","unique_id":"embodied_ha_observe","command_topic":"embodied_ha/observe/trigger","icon":"mdi:eye","payload_press":"OBSERVE"}'

    export MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS
    echo "[run] MQTT discovery 完了（5 エンティティ登録）"
else
    echo "[run] 警告: MQTT 未取得（services:mqtt 未提供または MQTT 統合未登録）。チャット/観察トリガー・状態publishが無効になります（MQTT統合・Mosquitto を導入してください）。"
fi

# --- ログディレクトリ ---
# 一元管理。recall.sh / loops.sh もこれを参照する。
# EHA_DATA_DIR（既定 /config/embodied-ha）配下に置き、再ビルド・更新でも
# ログ・記憶（memory.md等）が永続化される。
export EHA_LOG_DIR="${EHA_LOG_DIR:-$EHA_DATA_DIR/log}"
mkdir -p "$EHA_LOG_DIR"

# --- FTS5インデックス初期化（既存エピソード・memory.md を初回インデックス化）---
EHA_LOG_DIR="$EHA_LOG_DIR" python3 "$SCRIPT_DIR/init_fts.py" \
    && echo "[run] fts_index 初期化完了" \
    || echo "[run] fts_index 初期化失敗（スキップ。起動は続ける）"

# --- Web UI サーバー（認証前でも起動。セットアップ画面を出すため）---
echo "[run] web server 起動（ポート ${INGRESS_PORT:-8099}）"
python3 "$SCRIPT_DIR/web/server.py" &

# --- 認証確認（未設定なら Web UI セットアップ完了まで待機）---
_auth_ok() {
    [ -n "${ANTHROPIC_API_KEY:-}" ] && return 0
    # サブスク認証は OAuthトークン本体の有無で判定する。
    # .claude.json の userID はログイン記録であって認証実体ではない（トークンが
    # 無ければ claude は "Not logged in" になる）ので判定に使わない。
    [ -f "${CLAUDE_CONFIG_DIR}/.credentials.json" ] && return 0
    [ -f "${CLAUDE_CONFIG_DIR}/credentials.json" ] && return 0
    return 1
}
if ! _auth_ok; then
    echo "[run] Claude 未認証。Web UI でセットアップしてください（ポート ${INGRESS_PORT:-8099}）..."
    until _auth_ok; do
        sleep 5
        echo "[auth] CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR} / files: $(find "${CLAUDE_CONFIG_DIR}" -maxdepth 1 -printf '%f ' 2>/dev/null || echo '(ディレクトリなし)')"
        if [ -f "${CLAUDE_CONFIG_DIR}/.credentials.json" ]; then
            echo "[auth] .credentials.json: あり（認証実体OK）"
        else
            echo "[auth] .credentials.json: なし（未認証。.claude.jsonのuserIDは認証実体ではない）"
        fi
    done
    echo "[run] 認証完了。daemon 起動..."
fi

# --- daemon.py 起動（watch / explore / chat ループを管理）---
echo "[run] daemon.py 起動"
exec python3 "$SCRIPT_DIR/daemon.py"
