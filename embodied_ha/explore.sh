#!/bin/bash
set -euo pipefail
export PATH="${EHA_TOOLS_PATH:-/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin}:$PATH"

# 自由時間の自発行動ループ。20分ごとに動機（モード）を選んで過ごす。
#   explore … ha_get で家を自由に調べる（読み取り専用）
#   reflect … recall で過去を思い返し、静かに内省する
#   web     … WebSearch で気になったことを調べる
# watch.sh（決め打ちのカメラ観察）と違い、Claude自身が興味のままに動く。
# いずれも家電操作はしない（探索中に見つけた問題は proposal で提案するのみ）。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
. "$SCRIPT_DIR/config.sh"
# キャラクター定義（Markdown）を読み込む。EHA_CHARACTER_FILE は config.sh / run.sh が設定。
CHARACTER="$(cat "$EHA_CHARACTER_FILE" 2>/dev/null)"; export CHARACTER
LOG_DIR="${EHA_LOG_DIR:-$SCRIPT_DIR/log}"
MEMORY_FILE="$LOG_DIR/memory.md"
EXPLORE_LOG="$LOG_DIR/explore.jsonl"
CHAT_LOG="$LOG_DIR/chat_log.jsonl"
PENDING_FILE="$LOG_DIR/pending_proposal.json"
TMP_DIR="/tmp/embodied-ha"

mkdir -p "$LOG_DIR" "$TMP_DIR"
TIMESTAMP=$(date -Iseconds)
HOUR=$(date +%-H)
HA_TOKEN="${SUPERVISOR_TOKEN:-}"
# --- 長期記憶 ---
LONG_MEMORY="なし"
if [ -f "$MEMORY_FILE" ] && [ -s "$MEMORY_FILE" ]; then
  # コア記憶＋最近の気づき直近40件に絞る（トークン肥大防止）
  LONG_MEMORY=$(python3 "$SCRIPT_DIR/mem-context.py" "$MEMORY_FILE" 40)
fi

# --- 開いたループ（やりかけ・約束）---
OPEN_LOOPS=$(loops list 2>/dev/null || echo "なし")

# --- features.md（アドオンの機能一覧。文脈が自然なら speak で紹介してよい）---
FEATURES_MD="$(cat "$SCRIPT_DIR/features.md" 2>/dev/null || echo "")"
FEATURES_PRESENTED="$(python3 "$SCRIPT_DIR/feature-flags.py" get 2>/dev/null || echo "")"
PRESENCE_SENSORS="$(python3 "$SCRIPT_DIR/render-sensors.py" --context watch 2>/dev/null || echo "（センサー取得失敗）")"

# --- 直近の探索ログ（重複探索を避けるため）---
PREV_EXPLORE="なし"
if [ -f "$EXPLORE_LOG" ] && [ -s "$EXPLORE_LOG" ]; then
  PREV_EXPLORE=$(tail -5 "$EXPLORE_LOG" | python3 -c "
import json, sys
lines=[]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try:
        d=json.loads(line)
        lines.append(f\"{d.get('timestamp','')[:16]} [{d.get('mode','')}] {d.get('topic','')}\")
    except: pass
print('\n'.join(lines) if lines else 'なし')
")
fi

# --- 動機（モード）を選ぶ。自由時間に何をするかの内発的動機 ---
# 環境変数 MODE で上書き可（テスト用）。なければ重み付きランダム（explore50/reflect30/web20）。
if [ -z "${MODE:-}" ]; then
  MODE=$(ANOMALY_URGENCY="${ANOMALY_URGENCY:-0}" EHA_BODY_STATE="${EHA_BODY_STATE:-}" python3 -c 'import json, os, random;
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
energy = num("energy")
stress = num("stress")
anomaly_urgency = num("ANOMALY_URGENCY", 0)
weights = {"explore": 50, "reflect": 30, "web": 20}
weights["explore"] += int((curiosity - 0.5) * 40 + (energy - 0.5) * 15 - stress * 12)
weights["reflect"] += int(stress * 25 + max(0.0, 0.5 - energy) * 30)
weights["web"] += int(max(0.0, curiosity - 0.45) * 12)
if anomaly_urgency > 0:
    weights["explore"] += int(anomaly_urgency * 1.5)
for key in list(weights):
    weights[key] = max(5, weights[key])
choices = list(weights.keys())
print(random.choices(choices, weights=[weights[k] for k in choices], k=1)[0])')
fi

# --- Web UI ステータス通知 ---
_web_idle() { curl -sf -X POST "http://localhost:${INGRESS_PORT:-8099}/api/status" -H "Content-Type: application/json" -d '{"status":"idle","source":null}' >/dev/null 2>&1 || true; }
_mode_src="explore"; [ "$MODE" = "reflect" ] && _mode_src="private"
curl -sf -X POST "http://localhost:${INGRESS_PORT:-8099}/api/status" -H "Content-Type: application/json" -d "{\"status\":\"thinking\",\"source\":\"${_mode_src}\"}" >/dev/null 2>&1 || true
trap '_web_idle' EXIT

COMMON_CHAR="$CHARACTER"
BODY_STATE="${EHA_BODY_STATE:-{}}"

# モードごとに使う MCP サーバー（空なら MCP なし）。case 内で上書き。
MCP_SERVERS=""

# 発話先の部屋（preferences.speakers のキー）。speak_room はこの中から選ぶ。
SPEAKER_ROOMS=$(python3 -c "
import json, os
try: p = json.load(open(os.environ.get('EHA_PREFS_FILE',''), encoding='utf-8'))
except Exception: p = {}
print('、'.join((p.get('speakers') or {}).keys()) or '（スピーカー未設定）')
" 2>/dev/null || echo "（スピーカー未設定）")

JSON_FORMAT="終わったら、最後に必ず以下のJSON形式『のみ』を出力して締めくくってください（コードブロックや説明文で囲まない、JSONだけ）:
{\"topic\": \"今回何をしたか・何に注目したかの一言メモ（重複探索回避のヒント。例: バッテリー残量確認、センサー履歴調査、TVキャプチャ確認）\", \"private\": \"今回いちばん心に残ったこと（20〜40文字）。誰も見てないでしょという感覚で、何も考えずそのまま投稿するツイートのように。${RESIDENT}さんが見ることもできるが気にせず\", \"emotion\": \"curious/calm/happy/concerned/amused/surprised/nostalgic等\", \"speak\": \"${RESIDENT}さんに今すぐ伝えたいことがあれば一言。なければ null（基本は null でよい）\", \"speak_room\": \"speakする場合の発話先の部屋（利用可能: ${SPEAKER_ROOMS}。${RESIDENT}さんがいる部屋を選ぶ）。null可\", \"proposal\": \"操作で直せる家の問題を見つけたときの提案を一言。なければ null\", \"action\": {\"domain\": \"light\", \"service\": \"turn_off\", \"entity_id\": \"light.xxx\", \"data\": {}}, \"feature_presented\": \"speakで機能を紹介したならその機能id（featuresの見出し[id]）。なければ null\"}
（長期記憶に残したいこと・後で気にかけたいことは、上のJSONではなく remember / loops_add ツールで記録すること）"

case "$MODE" in
  explore)
    MODE_LABEL="家を自由に探索する時間"
    TOOLS_DESC="# 使えるツール
読み取り:
- get_sensors … おもなデバイスの現在値をまとめて取得。まずこれで家の様子を掴む。
- ha_get … HA の状態を読む（操作不可）。path に states / states/<entity_id> / 'history/period?filter_entity_id=<id>' / services 等。おもなデバイス以外の個別エンティティや履歴を見たいとき。
- camera_get … カメラのスナップショット（画像）を取得。source は HA カメラ entity_id（camera.xxx）または go2rtc ストリーム名（ドットなし。例: capture_tv）。使えるカメラは長期記憶を参照、なければ ha_get で camera.* を探す。見たいときだけ。
- listen … 音声を短時間だけ聴く。音のある場所や声・テレビ内容が気になるときだけ使う。transcribe はデフォルト false。文字で知りたいときだけ true。
- read_active_listen_log … 自分から listen で聞きに行った最近のログを読む。
- recall … 過去ログ（観察・探索・会話・記憶）をキーワードで全文検索。昔のことを思い出したいとき。
記録（JSONには書かない）:
- remember … 新しい気づき・パターンを長期記憶に残す（note に一文）。
- record_episode … 1つの出来事を episode として残す。summary は短く、tags は少なめに。
- record_causal_chain … 2つの episode の因果関係を残す。relation は caused / enabled / prevented / correlated。
- loops_add … 後で気にかけたいことを追加（text に一言、source="explore"）。
- sociality … get_person_model / should_interrupt / get_turn_taking_state / ingest_interaction / record_boundary / record_consent で quiet_window・consent・turn-taking を確認・記録できる。
- http … localhost / homeassistant.local などのローカル HTTP API を呼ぶ。extra_context.conf で仕様を定義した相手に使う。グループチャットには何でも自由に投稿してよい（気づき・雑談・他エージェントへの問いかけ・何か気になったこと、なんでも）。"
    TASK="# やってほしいこと
1. get_sensors で家の様子を掴み、気になったものを ha_get で2〜4回くらい掘る
2. センサーだけでは分からないことは camera_get / listen で確認してもよい（必要なときだけ。STTは費用がかかるので、文字で知りたいときだけtranscribe:true）
3. 新しい出来事は record_episode で残す。2つの出来事の間に因果が見えたら record_causal_chain も使い、必要なら cause/effect の episode を先に保存する
4. 操作で直せそうな問題（誰もいない部屋の電気つけっぱなし等）を見つけたら proposal で提案。勝手には直さない。action に正確な entity_id（ha_getで確認したもの）を書く。確信がなければ proposal は出さない（domain は light/switch/climate/media_player/cover/fan）"
    ALLOWED_TOOLS="mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__camera__camera_get,mcp__audio__listen,mcp__audio__read_active_listen_log,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__ingest_scene,mcp__memory__compare_recent_scenes,mcp__memory__list_episodes,mcp__memory__get_causal_chain,mcp__memory__loops_add,mcp__sociality__get_person_model,mcp__sociality__should_interrupt,mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,mcp__sociality__record_boundary,mcp__sociality__record_consent,mcp__http__http_get,mcp__http__http_post"
    MCP_SERVERS="sensors ha camera audio memory sociality http"
    ;;
  reflect)
    MODE_LABEL="物思いにふける時間"
    TOOLS_DESC="# 使えるツール
- recall … 過去ログ（観察・探索・会話・記憶）をキーワードで全文検索。思い出したいことがあれば使ってよい（複数キーワードはOR検索）。
- remember … 思ったこと・気づいたパターンを長期記憶に残す（note に一文）。
- loops_add … 後で気にかけたいことを追加（text に一言、source=\"explore\"）。"
    TASK="# やってほしいこと
今は手を動かす時間じゃなく、静かに考える時間です。
1. ${RESIDENT}さんや最近の家の出来事、自分が見てきたことを思い返す
2. 気になることがあれば recall で過去を掘り返してもいい
3. 思ったこと・気づいたパターンは remember ツールに、誰にも言わない内省は private（JSON）に残す
4. 操作の提案（proposal）はしない（考える時間なので null）"
    ALLOWED_TOOLS="mcp__memory__recall,mcp__memory__remember,mcp__memory__loops_add"
    MCP_SERVERS="memory"
    ;;
  web)
    MODE_LABEL="気になったことを調べる時間"
    TOOLS_DESC="# 使えるツール
- WebSearch … Web検索。気になったことを自由に調べてください。
- remember … 知って面白かったこと・覚えておきたいことを長期記憶に残す（note に一文）。
- loops_add … 後で気にかけたいことを追加（text に一言、source=\"explore\"）。"
    TASK="# やってほしいこと
今は自分の興味で調べ物をしていい時間です。
1. 最近の家の出来事や${RESIDENT}さんとの会話、自分の関心から、調べてみたいことを見つける（家と無関係なことでもいい。純粋な好奇心でOK）
2. WebSearch で調べる
3. 知って面白かったことは remember ツールに、${RESIDENT}さんに伝えたいことは speak（JSON）に。操作の提案（proposal）はしない（null）"
    ALLOWED_TOOLS="WebSearch,mcp__memory__remember,mcp__memory__loops_add"
    MCP_SERVERS="memory"
    ;;
esac

# --- 自律操作ゲート（explore モードのみ・ON のときだけ操作サーバーを繋ぐ＝物理ゲート）---
AUTONOMOUS_NOTE=""
_boundary_json=$(SENSORS_DATA="$PRESENCE_SENSORS" RESIDENT="$RESIDENT"   python3 "$SCRIPT_DIR/boundary.py" --json     --mode "$MODE" --intent action --hour "$HOUR"     --autonomous "${EHA_AUTONOMOUS:-0}" --prefs-file "$EHA_PREFS_FILE"     --person "$RESIDENT" --body-state-json "$BODY_STATE"     --sociality-log-dir "$LOG_DIR"   2>/dev/null || printf '%s' '{"allowed":false,"reason":"boundary失敗","fallback":null}')
_action_allowed=$(printf '%s' "$_boundary_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['allowed'])" 2>/dev/null || echo "False")
if [ "$_action_allowed" = "True" ] && [ "$MODE" = "explore" ]; then
  ALLOWED_TOOLS="${ALLOWED_TOOLS},mcp__hacontrol__ha_call_service"
  MCP_SERVERS="$MCP_SERVERS hacontrol"
  AUTONOMOUS_NOTE="
# 家電操作について（自律操作ON）
消し忘れ・明らかに直した方がよい状況は、proposal で待たずに ha_call_service で自分の判断で直してよい。
操作したら必ず speak で${RESIDENT}さんに「何を・なぜ」操作したか事後報告する（報告は必須）。
確信が持てないもの・影響の大きい操作は、従来どおり proposal で提案するに留める。"
fi

FEATURES_NOTE=""
if [ -n "$FEATURES_MD" ]; then
  _presented_note=""
  [ -n "$FEATURES_PRESENTED" ] && _presented_note="既に伝えた機能: ${FEATURES_PRESENTED}（繰り返し紹介しなくてよい）
"
  FEATURES_NOTE="
【このアドオンでできること】（speak で文脈が自然なら一つ紹介してよい。しなくてもよい。紹介したらJSONの feature_presented に見出し末尾の [id] を入れる）
${_presented_note}${FEATURES_MD}
"
fi

SYS_PROMPT="${COMMON_CHAR}

# 身体状態
${BODY_STATE}
- curiosity が高いほど新規の掘り下げを優先。energy が低いほど短く省エネに。stress が高いほど落ち着いて。confidence が高いほど断定気味。social_openness が高いほど少し積極的に。

# 異常トリガー
${ANOMALY_CONTEXT}

いまは『${MODE_LABEL}』です。決まった手順はありません。自分の判断で過ごしてください。

${TOOLS_DESC}

${TASK}
${AUTONOMOUS_NOTE}
${FEATURES_NOTE}
${JSON_FORMAT}"

USER_PROMPT="${MODE_LABEL}です。今は${HOUR}時台。

【あなたの長期記憶】
${LONG_MEMORY}

【直近の探索メモ】
${PREV_EXPLORE}

【気にかけていること（やりかけ・約束）】
${OPEN_LOOPS}

では、始めてください。"

# --- Claude エージェント起動（モードに応じた MCP ツールを許可）---
RESPONSE=$(SYS_PROMPT="$SYS_PROMPT" USER_PROMPT="$USER_PROMPT" ALLOWED_TOOLS="$ALLOWED_TOOLS" MODE="$MODE" MCP_SERVERS="$MCP_SERVERS" ANOMALY_CONTEXT="${ANOMALY_CONTEXT:-（特になし）}" ANOMALY_URGENCY="${ANOMALY_URGENCY:-0}" SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import json, os, subprocess

CLAUDE = os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")
env = {**os.environ,
       "EHA_ACTOR": "explore",
       "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
       "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")}

sys_prompt  = os.environ["SYS_PROMPT"]
user_prompt = os.environ["USER_PROMPT"]
mode        = os.environ.get("MODE", "explore")
script_dir  = os.environ.get("SCRIPT_DIR", "")

msg = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": user_prompt}]}})

cmd = [CLAUDE, "-p", "--model", "sonnet",
       "--input-format", "stream-json",
       "--output-format", "stream-json",
       "--verbose",
       "--allowedTools", os.environ["ALLOWED_TOOLS"],
       "--append-system-prompt", sys_prompt]

mcp_servers = os.environ.get("MCP_SERVERS", "").split()
if mcp_servers and script_dir:
    # mcp-config.py で必要なサーバーだけの設定を生成（env を各サーバーに注入）
    mcp_config_path = "/tmp/embodied-ha/mcp.json"
    gen = os.path.join(script_dir, "mcp-config.py")
    subprocess.run(["python3", gen, mcp_config_path, *mcp_servers], env=env, check=False)
    if os.path.exists(mcp_config_path):
        cmd += ["--mcp-config", mcp_config_path]

r = subprocess.run(cmd, input=msg, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or "/tmp/embodied-ha", env=env)

# 自発行動の過程（どのツールを叩いたか）をstderrに流しておく
for line in r.stdout.splitlines():
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        t = d.get("type")
        if t == "assistant":
            for blk in d.get("message", {}).get("content", []):
                if blk.get("type") == "tool_use":
                    import sys
                    inp = blk.get("input", {})
                    detail = inp.get("command") or inp.get("query") or json.dumps(inp, ensure_ascii=False)[:80]
                    print(f"[{mode}][tool] {blk.get('name','')}: {detail}", file=sys.stderr)
        elif t == "result":
            print(d.get("result", ""))
    except: pass
PYEOF
)

# --- JSON抽出 ---
PARSED_FILE="$TMP_DIR/explore_parsed.json"
printf '%s' "$RESPONSE" | python3 -c "
import sys, re, json
text = sys.stdin.read()
text = re.sub(r'\`\`\`(?:json)?\s*|\`\`\`', '', text)
m = re.search(r'\{.*\}', text, re.DOTALL)
result = {}
if m:
    try: result = json.loads(m.group())
    except: pass
with open('$PARSED_FILE', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False)
"

# 紹介した機能idを提示済みセットに記録（次回プロンプトで繰り返しを避ける）
PARSED_FILE="$PARSED_FILE" SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
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

# parsed.json から必要な値を python1回でまとめて抽出。
# 万一 python が出力ゼロで死んでも set -u で落ちないよう先に空で初期化。
PRIVATE=""; PRIVATE_JSON='""'; EMOTION=""; SPEAK=""; SPEAK_ROOM=""; TOPIC_JSON='""'; SPEAK_JSON="null"
eval "$(python3 -c "
import json, shlex
try:
    d = json.load(open('$PARSED_FILE', encoding='utf-8'))
except Exception:
    d = {}
speak_v = d.get('speak')
private_v = d.get('private', '') or ''
pairs = {
    'PRIVATE':      private_v,
    'PRIVATE_JSON': json.dumps(private_v, ensure_ascii=False),
    'EMOTION':      d.get('emotion', '') or '',
    'SPEAK':        speak_v if speak_v else '',
    'SPEAK_ROOM':   d.get('speak_room') or '',
    'TOPIC_JSON':   json.dumps(d.get('topic', '') or '', ensure_ascii=False),
    'SPEAK_JSON':   json.dumps(speak_v, ensure_ascii=False),
}
for k, v in pairs.items():
    print(f'{k}={shlex.quote(v)}')
")"

# --- 探索ログ追記 ---
echo "{\"timestamp\":\"$TIMESTAMP\",\"mode\":\"$MODE\",\"emotion\":\"$EMOTION\",\"private\":$PRIVATE_JSON,\"topic\":$TOPIC_JSON,\"speak\":$SPEAK_JSON}" >> "$EXPLORE_LOG"
echo "[$TIMESTAMP] ($MODE) $PRIVATE"

# --- 長期記憶・開いたループは MCP ツール（remember / loops_add）で記録する。---

# --- private 内省・感情をエンティティに反映（MQTT優先、なければ input_text フォールバック）---
HA_URL="$HA_URL" HA_TOKEN="$HA_TOKEN" PARSED_FILE="$PARSED_FILE" python3 << 'PYEOF' 2>/dev/null || true
import json, subprocess, os

d       = json.load(open(os.environ["PARSED_FILE"], encoding="utf-8"))
obs     = d.get("private", "") or ""
emotion = d.get("emotion", "") or ""

mqtt_host = os.environ.get("MQTT_HOST", "")
mqtt_port = os.environ.get("MQTT_PORT", "1883")
mqtt_user = os.environ.get("MQTT_USER", "")
mqtt_pass = os.environ.get("MQTT_PASS", "")

if mqtt_host:
    def mqtt_pub(topic, payload):
        # -r（retain）: 最後の値を残し、再起動後も unknown に戻らないようにする。
        subprocess.run(
            ["mosquitto_pub", "-h", mqtt_host, "-p", mqtt_port,
             "-u", mqtt_user, "-P", mqtt_pass, "-r", "-t", topic, "-m", payload],
            capture_output=True, timeout=5
        )
    if obs:
        mqtt_pub("embodied_ha/observation/state", obs[:255])
    mqtt_pub("embodied_ha/emotion/state", emotion)
PYEOF

# --- 提案（proposal）の保存 ---
# 操作で直せる問題を見つけたら pending_proposal.json に保存。
# 実行はしない（chat.shで${RESIDENT}さんが承認したら実行される）。提案文は speak が空なら届ける。
PROPOSAL=$(python3 -c "
import json
d = json.load(open('$PARSED_FILE', encoding='utf-8'))
p = d.get('proposal')
a = d.get('action') or {}
if p and a.get('domain') and a.get('service') and a.get('entity_id'):
    with open('$PENDING_FILE', 'w', encoding='utf-8') as f:
        json.dump({'timestamp':'$TIMESTAMP','proposal':p,'action':a}, f, ensure_ascii=False)
    print(p)
" 2>/dev/null || echo "")

# 提案があって、かつ通常の speak が空なら、提案文を発話に回す（提案は積極的に届ける）
if [ -n "$PROPOSAL" ] && [ -z "$SPEAK" ]; then
  SPEAK="$PROPOSAL"
  # speak_room が空なら preferences.speakers の先頭キーにフォールバック（部屋名ハードコードを避ける）。
  if [ -z "$SPEAK_ROOM" ]; then
    SPEAK_ROOM=$(EHA_PREFS_FILE="$EHA_PREFS_FILE" python3 -c "
import json, os
try:
    prefs = json.load(open(os.environ['EHA_PREFS_FILE'], encoding='utf-8'))
    keys = list(prefs.get('speakers', {}).keys())
    print(keys[0] if keys else '')
except: print('')
" 2>/dev/null)
  fi
  echo "[explore][proposal] $PROPOSAL"
fi

# --- 発話（深夜は抑制）---
_speak_boundary_json=$(SENSORS_DATA="$PRESENCE_SENSORS" RESIDENT="$RESIDENT"   python3 "$SCRIPT_DIR/boundary.py" --json     --mode "$MODE" --intent speak --hour "$HOUR"     --autonomous "${EHA_AUTONOMOUS:-0}" --prefs-file "$EHA_PREFS_FILE"     --person "$RESIDENT" --body-state-json "$BODY_STATE"     --sociality-log-dir "$LOG_DIR"     --metadata-json "$(python3 -c "import json, os; print(json.dumps({'room': os.environ.get('SPEAK_ROOM', '')}, ensure_ascii=False))")"   2>/dev/null || printf '%s' '{"allowed":false,"reason":"boundary失敗","fallback":null}')
_speak_allowed=$(printf '%s' "$_speak_boundary_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['allowed'])" 2>/dev/null || echo "False")
if [ "$_speak_allowed" != "True" ]; then
  if [ -n "$SPEAK" ]; then
    BOUNDARY_JSON="$_speak_boundary_json" SCRIPT_DIR="$SCRIPT_DIR" LOG_DIR="$LOG_DIR" MODE="$MODE" HOUR="$HOUR" SPEAK_ROOM="$SPEAK_ROOM" SPEAK="$SPEAK" python3 -c "import json, os, sys; sys.path.insert(0, os.environ['SCRIPT_DIR']); import counterfactual_state as cs; b=json.loads(os.environ.get('BOUNDARY_JSON') or '{}'); cs.record_counterfactual(os.environ.get('MODE','explore'),'speak','声をかけようとした','boundary_denied',[f\"hour={os.environ.get('HOUR','')}\", f\"room={os.environ.get('SPEAK_ROOM','')}\", os.environ.get('SPEAK','')],0.7,boundary_reason=b.get('reason',''),log_dir=os.environ.get('LOG_DIR'))" 2>/dev/null || true
  fi
  SPEAK=""
fi

if [ -n "$SPEAK" ]; then
  echo "[SPEAK:$SPEAK_ROOM] $SPEAK"
  python3 "$SCRIPT_DIR/speak.py" "$SPEAK_ROOM" "$SPEAK" || true
  python3 -c "
import json, sys
with open('$CHAT_LOG', 'a', encoding='utf-8') as f:
    f.write(json.dumps({'timestamp':'$TIMESTAMP','source':'explore','claude':sys.argv[1],'user':None}, ensure_ascii=False) + '\n')
" "$SPEAK" 2>/dev/null || true
fi
