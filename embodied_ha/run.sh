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
# ON のときだけ loop に家電操作サーバー(ha-control)を繋ぐ＝物理的なゲート。
export EHA_AUTONOMOUS
EHA_AUTONOMOUS=$(python3 -c "import json; print('1' if json.load(open('/data/options.json')).get('autonomous_control', False) else '0')" 2>/dev/null || echo "0")
echo "[run] 自律操作: $([ "$EHA_AUTONOMOUS" = "1" ] && echo "ON（loop が家電操作可）" || echo "OFF（観察・提案のみ）")"

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
    for _pulse_sock in "/run/audio/pulse.sock" "/run/audio/native" "/run/pulse/native" "/var/run/pulse/native" "/run/user/0/pulse/native"; do
        if [ -S "$_pulse_sock" ]; then
            export PULSE_SERVER="unix://${_pulse_sock}"
            echo "[run] PulseAudio: PULSE_SERVER=unix://${_pulse_sock}"
            break
        fi
    done
    unset _pulse_sock
fi
# ソケットが見つからなかった時だけ、原因特定用の診断情報を出す
# （見つかった通常時はログを汚さない。HAOS更新でパスが変わって再び失敗した際に効く）
if [ -z "${PULSE_SERVER:-}" ]; then
    echo "[run] PulseAudio: ソケット見つからず（/run ls: $(find /run -maxdepth 1 -mindepth 1 -printf '%f ' 2>/dev/null)）"
    if [ -d /run/audio ]; then
        echo "[run] PulseAudio-diag: ls -la /run/audio"
        # shellcheck disable=SC2012  # Diagnostic output intentionally uses ls -la format.
        ls -la /run/audio 2>&1 | sed 's/^/[run] PulseAudio-diag: /'
    else
        echo "[run] PulseAudio-diag: /run/audio not found"
    fi
    if [ -f /etc/asound.conf ]; then
        echo "[run] PulseAudio-diag: cat /etc/asound.conf"
        sed 's/^/[run] PulseAudio-diag: /' /etc/asound.conf
    else
        echo "[run] PulseAudio-diag: /etc/asound.conf not found"
    fi
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
export EHA_BODY_STATE_FILE="${EHA_BODY_STATE_FILE:-$EHA_DATA_DIR/body_state.json}"
mkdir -p "$EHA_DATA_DIR/log"
if [ -f "$EHA_DATA_DIR/audio_log.jsonl" ] && [ ! -f "$EHA_DATA_DIR/log/audio_log.jsonl" ]; then
    mv "$EHA_DATA_DIR/audio_log.jsonl" "$EHA_DATA_DIR/log/audio_log.jsonl"
    echo "[run] migrated audio log to $EHA_DATA_DIR/log/audio_log.jsonl"
fi
export EHA_AUDIO_LOG_FILE="${EHA_AUDIO_LOG_FILE:-$EHA_DATA_DIR/log/audio_log.jsonl}"
export EHA_ACTIVE_LISTEN_LOG_FILE="${EHA_ACTIVE_LISTEN_LOG_FILE:-$EHA_DATA_DIR/log/active_listen_log.jsonl}"
export EHA_BACKGROUND_AUDIO_LOG_FILE="${EHA_BACKGROUND_AUDIO_LOG_FILE:-$EHA_DATA_DIR/log/background_audio_log.jsonl}"
export EHA_NON_SPEECH_AUDIO_EVENTS_FILE="${EHA_NON_SPEECH_AUDIO_EVENTS_FILE:-$EHA_DATA_DIR/log/non_speech_audio_events.jsonl}"
export EHA_AUDIO_EVENT_TAGS_FILE="${EHA_AUDIO_EVENT_TAGS_FILE:-$EHA_DATA_DIR/log/audio_event_tags.jsonl}"
export EHA_AUDIO_WAV_DIR="${EHA_AUDIO_WAV_DIR:-$EHA_DATA_DIR/wav}"
export EHA_NEXT_LISTEN_REQUEST_FILE="${EHA_NEXT_LISTEN_REQUEST_FILE:-$EHA_DATA_DIR/runtime/next_listen_request.json}"
export EHA_NEXT_LISTEN_LOG_FILE="${EHA_NEXT_LISTEN_LOG_FILE:-$EHA_DATA_DIR/log/next_listen_log.jsonl}"
export EHA_AUDIO_SESSION_BIN="${EHA_AUDIO_SESSION_BIN:-agy}"
export EHA_AUDIO_SESSION_MODEL="${EHA_AUDIO_SESSION_MODEL:-Gemini 3.5 Flash (High)}"
export EHA_ROOM_GRAPH_FILE="${EHA_ROOM_GRAPH_FILE:-$EHA_DATA_DIR/floorplan_room_graph_draft.json}"
export EHA_BODY_LOCATION_FILE="${EHA_BODY_LOCATION_FILE:-$EHA_DATA_DIR/body_location.json}"
export EHA_BODY_LOCATION_LOG_FILE="${EHA_BODY_LOCATION_LOG_FILE:-$EHA_DATA_DIR/log/body_location_log.jsonl}"
mkdir -p "$EHA_AUDIO_WAV_DIR" "$EHA_DATA_DIR/runtime"
echo "[run] audio log: ${EHA_AUDIO_LOG_FILE}"
echo "[run] active listen log: ${EHA_ACTIVE_LISTEN_LOG_FILE}"
echo "[run] background audio log: ${EHA_BACKGROUND_AUDIO_LOG_FILE}"
echo "[run] non-speech audio events: ${EHA_NON_SPEECH_AUDIO_EVENTS_FILE}"
echo "[run] audio event tags: ${EHA_AUDIO_EVENT_TAGS_FILE}"
echo "[run] audio wav dir: ${EHA_AUDIO_WAV_DIR}"
echo "[run] room graph: ${EHA_ROOM_GRAPH_FILE}"
echo "[run] body location: ${EHA_BODY_LOCATION_FILE}"

# --- Antigravity CLI（任意・ユーザー導入）---
# Web UI から後で install / auth を分けて扱えるよう、永続ホームを先に決めておく。
export EHA_ANTIGRAVITY_HOME="${EHA_ANTIGRAVITY_HOME:-/data/}"
export EHA_ANTIGRAVITY_BIN_DIR="${EHA_ANTIGRAVITY_BIN_DIR:-$EHA_ANTIGRAVITY_HOME/bin}"
export EHA_ANTIGRAVITY_BIN="${EHA_ANTIGRAVITY_BIN:-$EHA_ANTIGRAVITY_BIN_DIR/agy}"
mkdir -p "$EHA_ANTIGRAVITY_HOME" "$EHA_ANTIGRAVITY_BIN_DIR"
echo "[run] Antigravity home: ${EHA_ANTIGRAVITY_HOME}"
echo "[run] Antigravity bin: ${EHA_ANTIGRAVITY_BIN}"

# --- agy 用 MCP config 生成（Antigravityが音声解析セッションで使うMCPサーバー設定）---
python3 -c "
import os, sys
sys.path.insert(0, os.environ.get('SCRIPT_DIR', '/app'))
import antigravity_setup
result = antigravity_setup.write_mcp_config(
    os.environ.get('SCRIPT_DIR', '/app'),
    servers=('audio', 'memory', 'ha', 'sensors', 'body'),
)
if result:
    print(f'[run] agy MCP config: {result}')
else:
    print('[run] agy MCP config: 生成スキップ（mcp-config.py なし or エラー）', file=sys.stderr)
" 2>&1 || true

# --- Claude 設定ディレクトリ ---
# デフォルトは EHA_DATA_DIR/.claude（/config/embodied-ha/.claude）。
# アンインストール時に /data/ が消えても記憶・認証が /config/ 側に残る。
# options で claude_config_dir を指定した場合はそちらを優先。
export CLAUDE_CONFIG_DIR="${_OPT_CONFIG_DIR:-${EHA_DATA_DIR}/.claude}"
unset _OPT_CONFIG_DIR
echo "[run] CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}"

# --- Claude の作業ディレクトリ ---
# 空の場合は EHA_DATA_DIR/workdir（永続・監査可能・ハーネス非依存の既定パス）。
# cwdの祖先ディレクトリにあたる /config/CLAUDE.md（SCS運用者向け・無関係な内容）が
# Claude Codeのプロジェクトメモリとして誤って読み込まれないよう、直下に
# .claude/settings.local.json（claudeMdExcludes）を配置する。既存ファイルは上書きしない。
_OPT_CWD=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('claude_cwd',''))" 2>/dev/null || echo "")
export EHA_CLAUDE_CWD="${_OPT_CWD:-$EHA_DATA_DIR/workdir}"
# invoke-agent.sh caller配線(#14)移行期間中の二重export。EHA_AGENT_CWDが3ハーネス共通の
# 正式変数になる予定だが、daybook_rollup.py・loop.sh/chat.sh（ロールバック経路）は
# EHA_CLAUDE_CWDのみを読み続けるため、旧経路コードを削除する増分まで両方exportする
# （docs/invoke-agent-caller-wiring-phase2-spec.md 増分1・7参照）。
export EHA_AGENT_CWD="$EHA_CLAUDE_CWD"
unset _OPT_CWD
echo "[run] EHA_CLAUDE_CWD=${EHA_CLAUDE_CWD} EHA_AGENT_CWD=${EHA_AGENT_CWD}"

mkdir -p "$EHA_CLAUDE_CWD/.claude"
for _eha_agent_site in observe explore reflect web social chat game; do
    mkdir -p "$EHA_CLAUDE_CWD/$_eha_agent_site"
done
unset _eha_agent_site
if [ ! -f "$EHA_CLAUDE_CWD/.claude/settings.local.json" ]; then
    cat > "$EHA_CLAUDE_CWD/.claude/settings.local.json" << 'JSONEOF'
{
  "claudeMdExcludes": ["/config/CLAUDE.md", "/config/CLAUDE.local.md"]
}
JSONEOF
    echo "[run] ${EHA_CLAUDE_CWD}/.claude/settings.local.json を初期化（/config/CLAUDE.md除外設定）"
fi

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

# --- home_policy.md（家のいい感じの状態。ユーザーが Markdown で編集可能）---
export EHA_HOME_POLICY_FILE="${EHA_HOME_POLICY_FILE:-$EHA_DATA_DIR/home_policy.md}"
if [ ! -f "$EHA_HOME_POLICY_FILE" ]; then
    if cp "$SCRIPT_DIR/home_policy.md" "$EHA_HOME_POLICY_FILE" 2>/dev/null; then
        echo "[run] home_policy.md を同梱デフォルトから初期化（$EHA_HOME_POLICY_FILE）"
    else
        echo "[run] home_policy.md 初期化失敗（同梱デフォルトを使用）"
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
# discover.py は sensors/speakers を空のときだけ seed する（既存設定は非破壊）。
# speakers 未設定のみの既存ユーザーにも seed が届くよう OR 条件にする（seed自体が非破壊なので毎起動でも安全）。
NEED_DISCOVER=$(python3 -c "
import json
try:
    d = json.load(open('$EHA_PREFS_FILE'))
except Exception:
    d = {}
sensors = sum(len(g.get('items', [])) for g in d.get('sensors', {}).get('groups', []))
speakers = len(d.get('speakers') or [])
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
    echo "[run] sensors 未設定。discover.py で自動発見します..."
    EHA_PREFS_FILE="$EHA_PREFS_FILE" HA_URL="$HA_URL" RESIDENT="$RESIDENT" \
        python3 "$SCRIPT_DIR/discover.py" --write \
        && echo "[run] discover.py 完了" \
        || echo "[run] discover.py 失敗（スキップ。起動は続ける）"
fi

# --- 起動時のソーススキーマ移行（旧2キー→新4キー）---
# 旧2キー構成なら新4キーへ一度だけ移行（冪等・バックアップ+アトミック）
python3 "$SCRIPT_DIR/migrate_source_schema.py" --apply "$EHA_PREFS_FILE" 2>&1 | sed 's/^/[run][migrate] /' || true

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

    # 内省ログ（loop/observe ループが書き込む）
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

    CHARACTER_LABEL=$(python3 - <<'PYEOF'
import json, os
path = os.environ.get("EHA_PREFS_FILE", "")
name = "Claude"
try:
    with open(path, encoding="utf-8") as f:
        prefs = json.load(f)
    if isinstance(prefs, dict) and isinstance(prefs.get("character_name"), str) and prefs.get("character_name").strip():
        name = prefs.get("character_name").strip()
except Exception:
    pass
print(name)
PYEOF
)
    export CHARACTER_LABEL

    # 観察トリガーボタン
    _pub -t "homeassistant/button/embodied_ha_observe/config" -m \
        '{"name":"Embodied HA ループ","unique_id":"embodied_ha_loop","command_topic":"embodied_ha/loop/trigger","icon":"mdi:eye","payload_press":"LOOP"}'

    _pub -t "homeassistant/sensor/embodied_ha_body_physical_room/config" -m \
        "$(python3 - <<'PYEOF'
import json, os
name = os.environ.get('CHARACTER_LABEL', 'Claude')
payload = {
  'name': f'Embodied HA {name}の身体がある場所',
  'unique_id': 'embodied_ha_body_physical_room',
  'state_topic': 'embodied_ha/body/physical_room/state',
  'icon': 'mdi:map-marker',
}
print(json.dumps(payload, ensure_ascii=False))
PYEOF
)"

    _pub -t "homeassistant/sensor/embodied_ha_body_current_place/config" -m \
        "$(python3 - <<'PYEOF'
import json, os
name = os.environ.get('CHARACTER_LABEL', 'Claude')
payload = {
  'name': f'Embodied HA {name}のいる場所',
  'unique_id': 'embodied_ha_body_current_place',
  'state_topic': 'embodied_ha/body/current_place/state',
  'icon': 'mdi:radar',
}
print(json.dumps(payload, ensure_ascii=False))
PYEOF
)"

    export MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS
    echo "[run] MQTT discovery 完了（7 エンティティ登録）"
else
    echo "[run] 警告: MQTT 未取得（services:mqtt 未提供または MQTT 統合未登録）。チャット/観察トリガー・状態publishが無効になります（MQTT統合・Mosquitto を導入してください）。"
fi

# --- ログディレクトリ ---
# 一元管理。recall.sh / loops.sh もこれを参照する。
# EHA_DATA_DIR（既定 /config/embodied-ha）配下に置き、再ビルド・更新でも
# ログ・記憶（memory.md等）が永続化される。
export EHA_LOG_DIR="${EHA_LOG_DIR:-$EHA_DATA_DIR/log}"
mkdir -p "$EHA_LOG_DIR"
export EHA_ANOMALY_STATE_FILE="${EHA_ANOMALY_STATE_FILE:-$EHA_LOG_DIR/anomaly_state.json}"

# --- FTS5インデックス初期化（既存エピソード・memory.md を初回インデックス化）---
EHA_LOG_DIR="$EHA_LOG_DIR" python3 "$SCRIPT_DIR/init_fts.py" \
    && echo "[run] fts_index 初期化完了" \
    || echo "[run] fts_index 初期化失敗（スキップ。起動は続ける）"

# --- daemon.py 起動（web / loop / chat watchdog を管理）---
echo "[run] daemon.py 起動（web + watchdog）"
python3 "$SCRIPT_DIR/daemon.py" &
DAEMON_PID=$!

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

# --- daemon.py を監視し続ける ---
wait "$DAEMON_PID"
