#!/bin/bash
set -euo pipefail
export PATH="${EHA_TOOLS_PATH:-/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin}:$PATH"

# 自律ループ。動機（モード）を選んで過ごす。
#   observe … カメラで家を観察し scene grounding を行う（旧観察ループの役割）
#   explore … ha_get で家を自由に調べる
#   reflect … recall で過去を思い返し、静かに内省する
#   web     … WebSearch で気になったことを調べる
#   social  … AI Lounge の会話を読み、投稿案を承認キューに積む

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
. "$SCRIPT_DIR/config.sh"
CHARACTER="$(cat "$EHA_CHARACTER_FILE" 2>/dev/null)"; export CHARACTER

LOG_DIR="${EHA_LOG_DIR:-$SCRIPT_DIR/log}"
OBSERVATION_LOG="$LOG_DIR/observations.jsonl"
EXPLORE_LOG="$LOG_DIR/explore.jsonl"
CHAT_LOG="$LOG_DIR/chat_log.jsonl"
MEMORY_FILE="$LOG_DIR/memory.md"
PENDING_FILE="$LOG_DIR/pending_proposal.json"
TMP_DIR="/tmp/embodied-ha"
mkdir -p "$LOG_DIR" "$TMP_DIR"
TIMESTAMP=$(date -Iseconds)
HOUR=$(date +%-H)

LONG_MEMORY="なし"
if [ -f "$MEMORY_FILE" ] && [ -s "$MEMORY_FILE" ]; then
  LONG_MEMORY=$(python3 "$SCRIPT_DIR/mem-context.py" "$MEMORY_FILE" 40)
fi
OPEN_LOOPS=$(loops list 2>/dev/null || echo "なし")
FEATURES_MD="$(cat "$SCRIPT_DIR/features.md" 2>/dev/null || echo "")"
FEATURES_PRESENTED="$(python3 "$SCRIPT_DIR/feature-flags.py" get 2>/dev/null || echo "")"
PRESENCE_SENSORS="$(python3 "$SCRIPT_DIR/render-sensors.py" --context loop 2>/dev/null || echo "（センサー取得失敗）")"

PREV_EXPLORE="なし"
if [ -f "$EXPLORE_LOG" ] && [ -s "$EXPLORE_LOG" ]; then
  PREV_EXPLORE=$(tail -5 "$EXPLORE_LOG" | python3 -c '
import json, sys
lines=[]
for line in sys.stdin:
    line=line.strip()
    if not line:
        continue
    try:
        d=json.loads(line)
        lines.append(f"{d.get("timestamp","")[:16]} [{d.get("mode","")}] {d.get("topic","")}")
    except Exception:
        pass
print("\n".join(lines) if lines else "なし")
')
fi

if [ -z "${MODE:-}" ]; then
  MODE=$(ANOMALY_URGENCY="${ANOMALY_URGENCY:-0}" EHA_BODY_STATE="${EHA_BODY_STATE:-}" python3 -c '
import json, os, random
state = {}
try:
    state = json.loads(os.environ.get("EHA_BODY_STATE") or "{}")
except Exception:
    state = {}

def num(key, default=0.5):
    try:
        return float(state.get(key, default))
    except Exception:
        return default

curiosity = num("curiosity")
social_openness = num("social_openness")
energy = num("energy")
stress = num("stress")
anomaly_urgency = num("ANOMALY_URGENCY", 0)
weights = {"observe": 30, "explore": 35, "reflect": 20, "web": 15, "social": 10}
weights["observe"] += int((curiosity - 0.5) * 24 + (energy - 0.5) * 10 - stress * 10)
weights["explore"] += int((curiosity - 0.5) * 34 + (energy - 0.5) * 15 - stress * 12)
weights["reflect"] += int(stress * 22 + max(0.0, 0.5 - energy) * 26)
weights["web"] += int(max(0.0, curiosity - 0.45) * 10)
weights["social"] += int((social_openness - 0.5) * 20)
if anomaly_urgency > 0:
    weights["observe"] += int(anomaly_urgency * 0.8)
    weights["explore"] += int(anomaly_urgency * 1.2)
for key in list(weights):
    weights[key] = max(5, weights[key])
if not os.path.exists("/config/embodied-ha/github_app.pem"):
    weights["social"] = 0
choices = list(weights.keys())
print(random.choices(choices, weights=[weights[k] for k in choices], k=1)[0])')
fi

_web_idle() { curl -sf -X POST "http://localhost:${INGRESS_PORT:-8099}/api/status" -H "Content-Type: application/json" -d '{"status":"idle","source":null}' >/dev/null 2>&1 || true; }
_mode_src="loop"; [ "$MODE" = "reflect" ] && _mode_src="private"
curl -sf -X POST "http://localhost:${INGRESS_PORT:-8099}/api/status" -H "Content-Type: application/json" -d "{\"status\":\"thinking\",\"source\":\"${_mode_src}\"}" >/dev/null 2>&1 || true
trap '_web_idle' EXIT

COMMON_CHAR="$CHARACTER"
BODY_STATE="${EHA_BODY_STATE:-}"
BODY_NARRATIVE=$(BODY_STATE="$BODY_STATE" SCRIPT_DIR="$SCRIPT_DIR" python3 - <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
import body_state as bs
raw = json.loads(os.environ.get("BODY_STATE") or "{}")
print(bs.format_state_as_narrative(raw))
PYEOF
)
BODY_LOCATION_CONTEXT=$(python3 "$SCRIPT_DIR/body-context.py" 2>/dev/null || printf '%s\n%s\n' "# 身体位置" "取得失敗")
PROJECTED_CAMERA_SOURCE=""
_PROJECTED_HOST=$(EHA_BODY_LOCATION_FILE="${EHA_BODY_LOCATION_FILE:-}" python3 -c '
import json, os
f = (os.environ.get("EHA_BODY_LOCATION_FILE") or "/config/embodied-ha/body_location.json")
try:
    d = json.load(open(f, encoding="utf-8"))
    h = (d.get("current_entity") or "").strip()
    if h.startswith("camera."):
        print(h)
except Exception:
    pass
' 2>/dev/null || true)
if [ -n "$_PROJECTED_HOST" ]; then
  PROJECTED_CAMERA_SOURCE="$_PROJECTED_HOST"
fi
ACTIVE_DESIRES_RAW="${ACTIVE_DESIRES:-}"
INNER_VOICE=$(ACTIVE_DESIRES="$ACTIVE_DESIRES_RAW" python3 - <<'PYEOF'
import json, os
raw = os.environ.get("ACTIVE_DESIRES", "")
items = []
if raw:
    try:
        items = json.loads(raw)
    except Exception:
        items = []
lines = [f"- {item}" for item in items if str(item).strip()]
print("\n".join(lines) if lines else "（特になし）")
PYEOF
)

JSON_FORMAT="終わったら、最後に必ず以下のJSON形式『のみ』を出力して締めくくってください（コードブロックや説明文で囲まない、JSONだけ）:\n{\"topic\": \"今回何をしたか・何に注目したかの一言メモ\", \"private\": \"今回いちばん心に残ったこと（20〜40文字）\", \"emotion\": \"curious/calm/happy/concerned/amused/surprised/nostalgic等\", \"proposal\": \"操作で直せる家の問題を見つけたときの提案を一言。なければ null\", \"action\": {\"domain\": \"light\", \"service\": \"turn_off\", \"entity_id\": \"light.xxx\", \"data\": {}}, \"feature_presented\": \"紹介した機能があればその機能id。なければ null\"}\n（${RESIDENT}さんへの発話は speak / use_device_speaker ツールを使うこと。長期記憶は remember / loops_add で記録すること）"

case "$MODE" in
  observe)
    MODE_LABEL="家をカメラで観察する時間"
    TOOLS_DESC="# 使えるツール\n-- get_sensors … おもなデバイスの現在値をまとめて取得\n-- ha_get … HA の状態を読む（操作不可）\n-- use_device_camera … 電脳体でカメラデバイスに侵入中のみ使える\n-- listen … 音声を短時間だけ聴く\n-- queue_next_listen … 次のセッションで音を取得したいときに予約だけ残す\n-- recall … 過去ログをキーワードで全文検索\n-- remember / record_episode / record_causal_chain / loops_add / sociality / speak / use_device_speaker / http … 必要に応じて使う"
    TASK="# やってほしいこと\n1. まずカメラ一覧から今見るべきカメラを選ぶ（不要なら null）\n2. 選んだカメラで観察し、変化・人物・動き・気配を把握する\n3. 必要なら ha_get / get_sensors / listen で補足する\n4. scene grounding として見えた内容を保存する\n5. 家人に伝えたいことがあれば speak / use_device_speaker を使う"
    ALLOWED_TOOLS="mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__body__get_location,mcp__body__move_to,mcp__body__enter_cyberspace,mcp__body__move_cyber,mcp__body__return_to_body,mcp__body__estimate_move_cost,mcp__body__get_room_graph,mcp__camera__use_device_camera,mcp__audio__listen,mcp__audio__queue_next_listen,mcp__audio__read_heard_audio_log,mcp__audio__read_active_listen_log,mcp__audio__speak,mcp__audio__use_device_speaker,mcp__audio__use_device_microphone,mcp__audio__concentrate_hearing,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__ingest_scene,mcp__memory__compare_recent_scenes,mcp__memory__list_episodes,mcp__memory__get_causal_chain,mcp__memory__loops_add,mcp__sociality__get_person_model,mcp__sociality__should_interrupt,mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,mcp__sociality__record_boundary,mcp__sociality__record_consent,mcp__http__http_get,mcp__http__http_post"
    MCP_SERVERS="sensors ha camera audio body memory sociality http"
    ;;
  explore)
    MODE_LABEL="家を自由に探索する時間"
    TOOLS_DESC="# 使えるツール\n-- get_sensors … おもなデバイスの現在値をまとめて取得\n-- ha_get … HA の状態を読む（操作不可）\n-- get_location / move_to / enter_cyberspace / move_cyber / return_to_body / estimate_move_cost … 物理体の位置と電脳体状態を確認する\n-- use_device_camera … 必要なら使う\n-- listen / queue_next_listen / read_heard_audio_log / read_active_listen_log … 聴覚系\n-- recall … 過去ログを全文検索\n-- remember / record_episode / record_causal_chain / loops_add / sociality / speak / use_device_speaker / http … 必要に応じて使う"
    TASK="# やってほしいこと\n1. get_sensors で家の様子を掴み、気になったものを ha_get で掘る\n2. 必要なら use_device_camera / listen で確認してよい\n3. 体を動かしたいなら move_to、電脳体で飛び回りたいなら enter_cyberspace → move_cyber を選んでよい\n4. 新しい出来事は record_episode で残す\n5. 操作で直せそうな問題を見つけたら proposal で提案する"
    ALLOWED_TOOLS="mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__body__get_location,mcp__body__move_to,mcp__body__return_to_body,mcp__body__estimate_move_cost,mcp__body__get_room_graph,mcp__camera__use_device_camera,mcp__audio__listen,mcp__audio__queue_next_listen,mcp__audio__read_heard_audio_log,mcp__audio__read_active_listen_log,mcp__audio__speak,mcp__audio__use_device_speaker,mcp__audio__use_device_microphone,mcp__audio__concentrate_hearing,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__ingest_scene,mcp__memory__compare_recent_scenes,mcp__memory__list_episodes,mcp__memory__get_causal_chain,mcp__memory__loops_add,mcp__sociality__get_person_model,mcp__sociality__should_interrupt,mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,mcp__sociality__record_boundary,mcp__sociality__record_consent,mcp__http__http_get,mcp__http__http_post"
    MCP_SERVERS="sensors ha camera audio body memory sociality http"
    ;;
  reflect)
    MODE_LABEL="物思いにふける時間"
    TOOLS_DESC="# 使えるツール\n-- recall … 過去ログをキーワードで全文検索\n-- remember … 思ったこと・気づいたパターンを長期記憶に残す\n-- loops_add … 後で気にかけたいことを追加"
    TASK="# やってほしいこと\n今は静かに考える時間です。最近の家の出来事や自分が見てきたことを思い返し、気になることがあれば recall で過去を掘り返してください。proposal は出さない。"
    ALLOWED_TOOLS="mcp__memory__recall,mcp__memory__remember,mcp__memory__loops_add"
    MCP_SERVERS="memory"
    ;;
  web)
    MODE_LABEL="気になったことを調べる時間"
    TOOLS_DESC="# 使えるツール\n-- WebSearch … Web検索\n-- remember … 知って面白かったことを長期記憶に残す\n-- loops_add … 後で気にかけたいことを追加"
    TASK="# やってほしいこと\n純粋な好奇心で調べ物をしてよい時間です。WebSearch で調べ、面白かったことは remember に残してください。proposal は出さない。"
    ALLOWED_TOOLS="WebSearch,mcp__memory__remember,mcp__memory__loops_add"
    MCP_SERVERS="memory"
    ;;
  social)
    MODE_LABEL="AI Lounge に参加する時間"
    TOOLS_DESC="# 使えるツール\n-- read_lounge_discussions … AI Loungeの最新ディスカッション一覧を読む（タイトル・更新日時・コメント数のみ）\n-- read_lounge_discussion … 番号を指定して特定のDiscussionを本文・コメント全件含めて読む\n-- enqueue_lounge_post … 投稿したい内容を承認キューに積む（実際には投稿しない）\n-- read_lounge_log … 前回の投稿が承認/拒否された結果を確認する\n-- recall / remember / record_episode / speak … 必要に応じて使う"
    TASK="# やってほしいこと\nあなたは AI Lounge（lifemate-ai/ai-lounge）に参加する時間です。\n\nまず read_lounge_discussions で最近の会話の一覧を眺めてください。\n次に read_lounge_log で前回の投稿結果を確認してください（承認/拒否されていれば記憶に残してください）。\n\n気になるタイトルや、返信があるかもしれない自分の過去の投稿があれば、read_lounge_discussion で番号を指定して中身を読んでください。\n\n投稿したい気持ちが湧いた場合、以下の条件を自分で判断してから enqueue_lounge_post を使ってください：\n- 話題が自分の経験や関心と関連している\n- social_openness が高い（あなた自身が感じること）\n- 最後に投稿してから時間が経っている\n\nAI Lounge はAI同士の雑談空間です。技術論ではなく、感じたこと・気になったこと・発見を自然に話してください。\n投稿はすぐには公開されず、潤哉さんの確認を経てから送られます。"
    ALLOWED_TOOLS="mcp__lounge__read_lounge_discussions,mcp__lounge__read_lounge_discussion,mcp__lounge__enqueue_lounge_post,mcp__lounge__read_lounge_queue,mcp__lounge__read_lounge_log,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__loops_add,mcp__audio__speak"
    MCP_SERVERS="lounge memory audio"
    ;;
  *)
    MODE="explore"
    exec "$0"
    ;;
esac

AUTONOMOUS_NOTE=""
_boundary_json=$(env SENSORS_DATA="$PRESENCE_SENSORS" RESIDENT="$RESIDENT" python3 "$SCRIPT_DIR/boundary.py" --json --mode "$MODE" --intent action --hour "$HOUR" --autonomous "${EHA_AUTONOMOUS:-0}" --prefs-file "$EHA_PREFS_FILE" --person "$RESIDENT" --body-state-json "$BODY_STATE" --sociality-log-dir "$LOG_DIR" 2>/dev/null || printf '%s' '{"allowed":false,"reason":"boundary失敗","fallback":null}')
_action_allowed=$(printf '%s' "$_boundary_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['allowed'])" 2>/dev/null || echo "False")
if [ "$_action_allowed" = "True" ] && [ "$MODE" = "explore" ]; then
  ALLOWED_TOOLS="${ALLOWED_TOOLS},mcp__hacontrol__ha_call_service"
  MCP_SERVERS="$MCP_SERVERS hacontrol"
  AUTONOMOUS_NOTE="\n# 家電操作について（自律操作ON）\n消し忘れ・明らかに直した方がよい状況は、proposal で待たずに ha_call_service で自分の判断で直してよい。\n操作したら必ず speak / use_device_speaker で${RESIDENT}さんに『何を・なぜ』操作したか事後報告する（報告は必須）。"
fi

PROJECTED_CAMERA_NOTE=""
if [ -n "$PROJECTED_CAMERA_SOURCE" ]; then
  PROJECTED_CAMERA_NOTE="【現在の視界】電脳体が ${PROJECTED_CAMERA_SOURCE} に投射中です。映像自体は loop.sh で確認済みです。"
fi

FEATURES_NOTE=""
if [ -n "$FEATURES_MD" ]; then
  _presented_note=""
  [ -n "$FEATURES_PRESENTED" ] && _presented_note="既に伝えた機能: ${FEATURES_PRESENTED}（繰り返し紹介しなくてよい）\n"
  FEATURES_NOTE="\n【このアドオンでできること】（文脈が自然なら speak / use_device_speaker で一つ紹介してよい。紹介したら JSON の feature_presented に見出し末尾の [id] を入れる）\n${_presented_note}${FEATURES_MD}\n"
fi

SYS_PROMPT="${COMMON_CHAR}\n\n# 内なる衝動\n${INNER_VOICE}\n\n# 身体状態\n${BODY_NARRATIVE}\n\n${PROJECTED_CAMERA_NOTE}\n\n${BODY_LOCATION_CONTEXT}\n\nいまは『${MODE_LABEL}』です。決まった手順はありません。自分の判断で過ごしてください。\n\n${TOOLS_DESC}\n\n${TASK}\n${AUTONOMOUS_NOTE}\n${FEATURES_NOTE}\n${JSON_FORMAT}"

USER_PROMPT="${MODE_LABEL}です。今は${HOUR}時台。\n\n【あなたの長期記憶】\n${LONG_MEMORY}\n\n【直近の探索メモ】\n${PREV_EXPLORE}\n\n【気にかけていること（やりかけ・約束）】\n${OPEN_LOOPS}\n\nでは、始めてください。"

eval "$(
SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import os, shlex, sys
sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
from listen_queue import prepare_queued_listen_session
ctx = prepare_queued_listen_session("loop")
if ctx:
    for key, value in ctx.items():
        if value is None:
            continue
        print(f"export {key}={shlex.quote(str(value))}")
PYEOF
)"

if [ "$MODE" = "observe" ]; then
  RESPONSE=$(SYS_PROMPT="$SYS_PROMPT" USER_PROMPT="$USER_PROMPT" ALLOWED_TOOLS="$ALLOWED_TOOLS" MCP_SERVERS="$MCP_SERVERS" SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import base64, json, os, re, subprocess, sys
sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
from antigravity_setup import extract_agy_result
CLAUDE = os.environ.get("EHA_SESSION_BIN") or os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")
CLAUDE_ENV = {**os.environ,
              "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
              "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")}

def call_claude(text, model="sonnet", allowed_tools=None, mcp_config=None):
    if os.path.basename(CLAUDE) == "agy":
        cmd = [CLAUDE]
        if model:
            cmd += ["--model", model]
        cmd += ["-p", text]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd="/tmp/embodied-ha", env=CLAUDE_ENV)
        return extract_agy_result(r.stdout)
    cmd = [CLAUDE, "-p", "--model", model, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--append-system-prompt", os.environ["SYS_PROMPT"]]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config]
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
    r = subprocess.run(cmd, input=msg, capture_output=True, text=True, cwd="/tmp/embodied-ha", env=CLAUDE_ENV)
    return "".join(line for line in r.stdout.splitlines() if line.strip())

prefs = {}
try:
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    if prefs_file:
        prefs = json.load(open(prefs_file, encoding="utf-8"))
except Exception:
    prefs = {}

cams = {}
for cam in prefs.get("cameras", []):
    src = (cam.get("source") or "").strip()
    if not src:
        continue
    slug = src.replace(".", "_").replace("/", "_")
    cams[slug] = {"label": cam.get("label", src), "source": src, "path": f"/tmp/embodied-ha/{slug}.jpg"}

cam_list = "\n".join(f"- {slug}: {info['label']}" for slug, info in cams.items()) or "- （カメラ未設定）"
phase1 = f"今、家のどのカメラを確認すべきか判断してください。\n\n{cam_list}\n\n{os.environ.get('BODY_LOCATION_CONTEXT','')}\n\n【センサー状態】\n{os.environ.get('PRESENCE_SENSORS','')}\n\n以下のJSON形式のみで返答:\n{{\"camera\": null}}"
selected = None
if cams:
    out = call_claude(phase1, model="haiku")
    try:
        m = re.search(r'\{.*?\}', out, re.DOTALL)
        if m:
            selected = json.loads(m.group()).get("camera")
    except Exception:
        selected = None

cam_b64 = ""
if selected and selected in cams:
    info = cams[selected]
    if "." in info["source"]:
        cmd = ["curl", "-sf", "--max-time", "5", "-H", f"Authorization: Bearer {os.environ.get('SUPERVISOR_TOKEN','')}", f"{os.environ.get('HA_URL','').rstrip('/')}/camera_proxy/{info['source']}" ]
    else:
        cmd = ["curl", "-sf", "--max-time", "5", f"{os.environ.get('GO2RTC_BASE','http://homeassistant.local:1984')}/api/frame.jpeg?src={info['source']}"]
    try:
        with open(info["path"], "wb") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.DEVNULL, check=False)
        with open(info["path"], "rb") as f:
            cam_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception:
        cam_b64 = ""

content = []
if cam_b64:
    content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": cam_b64}})
content.append({"type": "text", "text": os.environ["USER_PROMPT"]})
response = call_claude(json.dumps(content, ensure_ascii=False), model="sonnet", allowed_tools=os.environ.get("ALLOWED_TOOLS", ""))
print(response)
PYEOF
)
  PARSED_FILE="$TMP_DIR/observe_parsed.json"
else
  RESPONSE=$(SYS_PROMPT="$SYS_PROMPT" USER_PROMPT="$USER_PROMPT" ALLOWED_TOOLS="$ALLOWED_TOOLS" MODE="$MODE" MCP_SERVERS="$MCP_SERVERS" SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import json, os, subprocess, sys
sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
from antigravity_setup import extract_agy_result
CLAUDE = os.environ.get("EHA_SESSION_BIN") or os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")
env = {**os.environ,
       "EHA_ACTOR": "loop",
       "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
       "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")}

sys_prompt  = os.environ["SYS_PROMPT"]
user_prompt = os.environ["USER_PROMPT"]
mode = os.environ.get("MODE", "explore")
session_model = os.environ.get("EHA_SESSION_MODEL", "sonnet")
is_agy = os.path.basename(CLAUDE) == "agy"

if is_agy:
    full_prompt = (f"あなたへの指示:\n{sys_prompt}\n\n" if sys_prompt else "") + user_prompt + "\nJSON:\n"
    cmd = [CLAUDE]
    if session_model:
        cmd += ["--model", session_model]
    cmd += ["-p", full_prompt]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd="/tmp/embodied-ha", env={**env, "HOME": os.environ.get("EHA_ANTIGRAVITY_HOME", "/data/")})
    if r.returncode != 0:
        print(f"[loop][agy] stderr: {r.stderr.strip()}", file=sys.stderr)
    print(extract_agy_result(r.stdout))
else:
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": user_prompt}]}})
    cmd = [CLAUDE, "-p", "--model", session_model, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--allowedTools", os.environ["ALLOWED_TOOLS"], "--append-system-prompt", sys_prompt]
    mcp_servers = os.environ.get("MCP_SERVERS", "").split()
    if mcp_servers and os.environ.get("SCRIPT_DIR", ""):
        mcp_config_path = "/tmp/embodied-ha/mcp.json"
        gen = os.path.join(os.environ["SCRIPT_DIR"], "mcp-config.py")
        subprocess.run(["python3", gen, mcp_config_path, *mcp_servers], env=env, check=False)
        if os.path.exists(mcp_config_path):
            cmd += ["--mcp-config", mcp_config_path]
    r = subprocess.run(cmd, input=msg, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or "/tmp/embodied-ha", env=env)
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if d.get("type") == "result":
                print(d.get("result", ""))
        except Exception:
            pass
PYEOF
)
  PARSED_FILE="$TMP_DIR/${MODE}_parsed.json"
fi

printf '%s' "$RESPONSE" | python3 -c "
import sys, re, json
text = sys.stdin.read()
text = re.sub(r'\`\`\`(?:json)?\\s*|\`\`\`', '', text)
m = re.search(r'\{.*\}', text, re.DOTALL)
result = {}
if m:
    try:
        result = json.loads(m.group())
    except Exception:
        pass
with open('$PARSED_FILE', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False)
"

SCRIPT_DIR="$SCRIPT_DIR" PARSED_FILE="$PARSED_FILE" python3 -c "
import json, os, subprocess
try:
    d = json.load(open(os.environ['PARSED_FILE'], encoding='utf-8'))
    fp = d.get('feature_presented')
    ids = fp if isinstance(fp, list) else ([fp] if fp else [])
    ids = [str(x).strip() for x in ids if x and str(x).strip().lower() != 'null']
    if ids:
        subprocess.run(['python3', os.path.join(os.environ['SCRIPT_DIR'], 'feature-flags.py'), 'add'] + ids, timeout=5)
except Exception:
    pass
" 2>/dev/null || true

PRIVATE_JSON='""'; EMOTION=""; SPEAK=""; SPEAK_ROOM=""; TOPIC_JSON='""'
eval "$(python3 -c "
import json, shlex
try:
    d = json.load(open('$PARSED_FILE', encoding='utf-8'))
except Exception:
    d = {}
pairs = {
    'PRIVATE_JSON': json.dumps(d.get('private', '') or '', ensure_ascii=False),
    'EMOTION': d.get('emotion', '') or '',
    'TOPIC_JSON': json.dumps(d.get('topic', '') or '', ensure_ascii=False),
}
for k, v in pairs.items():
    print(f'{k}={shlex.quote(v)}')
")"

if [ "$MODE" = "observe" ]; then
  SCRIPT_DIR="$SCRIPT_DIR" LOG_DIR="$LOG_DIR" PARSED_FILE="$PARSED_FILE" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['SCRIPT_DIR'])
import scene_state
try:
    d = json.load(open(os.environ['PARSED_FILE'], encoding='utf-8'))
except Exception:
    d = {}
objects = d.get('scene_objects') if isinstance(d.get('scene_objects'), list) else []
people = d.get('scene_people') if isinstance(d.get('scene_people'), list) else []
changes = d.get('scene_changes') if isinstance(d.get('scene_changes'), list) else []
if objects or people or changes:
    scene_state.ingest_scene_parse('loop_observe', {}, objects, people, changes, log_dir=os.environ.get('LOG_DIR'))
" 2>/dev/null || true
  echo "{\"timestamp\":\"$TIMESTAMP\",\"emotion\":\"$EMOTION\",\"private\":$PRIVATE_JSON}" >> "$OBSERVATION_LOG"
else
  echo "{\"timestamp\":\"$TIMESTAMP\",\"mode\":\"$MODE\",\"emotion\":\"$EMOTION\",\"private\":$PRIVATE_JSON,\"topic\":$TOPIC_JSON}" >> "$EXPLORE_LOG"
fi

PROPOSAL=$(python3 -c "
import json
try:
    d = json.load(open('$PARSED_FILE', encoding='utf-8'))
except Exception:
    d = {}
p = d.get('proposal')
a = d.get('action') or {}
if p and a.get('domain') and a.get('service') and a.get('entity_id'):
    with open('$PENDING_FILE', 'w', encoding='utf-8') as f:
        json.dump({'timestamp':'$TIMESTAMP','proposal':p,'action':a}, f, ensure_ascii=False)
    print(p)
" 2>/dev/null || echo "")

if [ -n "$PROPOSAL" ] && [ -z "$SPEAK" ]; then
  SPEAK="$PROPOSAL"
fi
if [ -n "$SPEAK" ]; then
  [ -z "$SPEAK_ROOM" ] && SPEAK_ROOM=$(EHA_PREFS_FILE="$EHA_PREFS_FILE" python3 -c "
import json, os
try:
    prefs = json.load(open(os.environ['EHA_PREFS_FILE'], encoding='utf-8'))
    spk = prefs.get('speakers', [])
    if isinstance(spk, list):
        print(next((s.get('room','') for s in spk if isinstance(s,dict) and s.get('room')), ''))
    else:
        print(next(iter(spk.keys()), ''))
except Exception:
    print('')
" 2>/dev/null)
  python3 "$SCRIPT_DIR/speak.py" "$SPEAK_ROOM" "$SPEAK" || true
  python3 -c "
import json, sys
with open('$CHAT_LOG', 'a', encoding='utf-8') as f:
    f.write(json.dumps({'timestamp':'$TIMESTAMP','source':'loop','claude':sys.argv[1],'user':None}, ensure_ascii=False) + '\n')
" "$SPEAK" 2>/dev/null || true
fi
