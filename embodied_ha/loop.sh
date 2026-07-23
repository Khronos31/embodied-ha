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
DAYBOOK_MARKER="$LOG_DIR/.last_daybook"
TODAY=$(date +%Y-%m-%d)
TMP_DIR="/tmp/embodied-ha"
mkdir -p "$LOG_DIR" "$TMP_DIR"
TIMESTAMP=$(date -Iseconds)
HOUR=$(date +%-H)

LONG_MEMORY="なし"
if [ -f "$MEMORY_FILE" ] && [ -s "$MEMORY_FILE" ]; then
  LONG_MEMORY=$(python3 "$SCRIPT_DIR/mem-context.py" "$MEMORY_FILE" 40)
fi
OPEN_LOOPS=$(loops list 2>/dev/null || echo "なし")
OPEN_LOOPS_JSON=$(loops list-json 2>/dev/null || echo "[]")
FEATURES_MD="$(cat "$SCRIPT_DIR/features.md" 2>/dev/null || echo "")"
FEATURES_PRESENTED="$(python3 "$SCRIPT_DIR/feature-flags.py" get 2>/dev/null || echo "")"
PRESENCE_SENSORS="$(python3 "$SCRIPT_DIR/render-sensors.py" --context loop 2>/dev/null || echo "（センサー取得失敗）")"

HOME_POLICY=""
_policy_file="${EHA_HOME_POLICY_FILE:-$EHA_DATA_DIR/home_policy.md}"
if [ -f "$_policy_file" ] && [ -s "$_policy_file" ]; then
  HOME_POLICY=$(cat "$_policy_file")
fi

ANOMALY_CONTEXT="${ANOMALY_CONTEXT:-}"
ANOMALY_URGENCY="${ANOMALY_URGENCY:-}"
_ANOMALY_CONTEXT_FILE="$TMP_DIR/anomaly_context.txt"
_ANOMALY_URGENCY_FILE="$TMP_DIR/anomaly_urgency.txt"
_ANOMALY_STATE_FILE_PATH="${EHA_ANOMALY_STATE_FILE:-$LOG_DIR/anomaly_state.json}"
# 前回実行のスナップショット再利用を防ぐため、検出前に一時ファイルを消す。
rm -f "$_ANOMALY_CONTEXT_FILE" "$_ANOMALY_URGENCY_FILE" 2>/dev/null || true
if [ -n "$PRESENCE_SENSORS" ] || [ -n "$OPEN_LOOPS_JSON" ]; then
  (
    SCRIPT_DIR="$SCRIPT_DIR" LOG_DIR="$LOG_DIR" ANOMALY_STATE_FILE="$_ANOMALY_STATE_FILE_PATH" SENSORS_DATA="$PRESENCE_SENSORS" OPEN_LOOPS_JSON="$OPEN_LOOPS_JSON" TRIGGER_REASON="${TRIGGER_REASON:-定期実行}" ANOMALY_CONTEXT_FILE="$_ANOMALY_CONTEXT_FILE" ANOMALY_URGENCY_FILE="$_ANOMALY_URGENCY_FILE" python3 << 'PYEOF'
import os
import sys

sys.path.insert(0, os.environ["SCRIPT_DIR"])
import anomaly_state as ast  # type: ignore

path = os.environ.get("ANOMALY_STATE_FILE") or os.path.join(os.environ["LOG_DIR"], "anomaly_state.json")
state = ast.load_state(path)
updated = ast.detect_anomalies(
    os.environ.get("SENSORS_DATA", ""),
    os.environ.get("OPEN_LOOPS_JSON", "[]"),
    state,
    trigger_reason=os.environ.get("TRIGGER_REASON", ""),
    loop_name="loop",
)
ast.save_state(path, updated)
with open(os.environ["ANOMALY_CONTEXT_FILE"], "w", encoding="utf-8") as f:
    f.write(ast.format_context_block(updated))
with open(os.environ["ANOMALY_URGENCY_FILE"], "w", encoding="utf-8") as f:
    f.write(str(ast.compute_explore_urgency(updated)))
PYEOF
  ) || true
  # 今回のループで新規検出した結果を優先する（daemon から渡された env は、
  # 検出が走らなかった場合のフォールバック）。空でなければ上書き。
  if [ -s "$_ANOMALY_CONTEXT_FILE" ]; then
    ANOMALY_CONTEXT=$(cat "$_ANOMALY_CONTEXT_FILE")
  fi
  if [ -s "$_ANOMALY_URGENCY_FILE" ]; then
    ANOMALY_URGENCY=$(cat "$_ANOMALY_URGENCY_FILE")
  fi
fi
export ANOMALY_CONTEXT ANOMALY_URGENCY

PREV_EXPLORE="なし"
if [ -f "$EXPLORE_LOG" ] && [ -s "$EXPLORE_LOG" ]; then
  PREV_EXPLORE=$(tail -5 "$EXPLORE_LOG" | SCRIPT_DIR="$SCRIPT_DIR" python3 -c '
import json, os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
from introspection_facts import format_facts_summary
lines=[]
for line in sys.stdin:
    line=line.strip()
    if not line:
        continue
    try:
        d=json.loads(line)
        ts, mode, topic = d.get("timestamp","")[:16], d.get("mode",""), d.get("topic","")
        facts = format_facts_summary(d.get("facts"))
        measured = f" [実測: {facts}]" if facts else ""
        note = ""
        if d.get("ungrounded_speech_claim"):
            note = "（※このときの発話は記録に残っていません。伝えたかったことがまだあれば、今伝えて大丈夫です）"
        lines.append(f"{ts} [{mode}] {topic}{measured}{note}")
    except Exception:
        pass
if lines:
    print("※以下のメモは主観的な内省です。[実測:]が客観記録です。")
    print("\n".join(lines))
else:
    print("なし")
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
RECENT_AUDITORY_INPUT=$(SCRIPT_DIR="$SCRIPT_DIR" EHA_PREFS_FILE="$EHA_PREFS_FILE" EHA_BODY_LOCATION_FILE="${EHA_BODY_LOCATION_FILE:-}" python3 << 'PYEOF'
import json, os, sys

sys.path.insert(0, os.environ["SCRIPT_DIR"])
from auditory_context import format_recent_auditory_prompt, resolve_source_filter

body_location_file = os.environ.get("EHA_BODY_LOCATION_FILE") or "/config/embodied-ha/body_location.json"
current_entity = ""
try:
    with open(body_location_file, encoding="utf-8") as f:
        current_entity = (json.load(f).get("current_entity") or "").strip()
except Exception:
    pass

prefs = {}
prefs_file = os.environ.get("EHA_PREFS_FILE") or ""
if prefs_file:
    try:
        with open(prefs_file, encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception:
        prefs = {}

should_show, source_filter = resolve_source_filter(current_entity, prefs)
if should_show:
    print(format_recent_auditory_prompt("", source_filter=source_filter))
PYEOF
)
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

JSON_FORMAT="終わったら、最後に必ず以下の項目をJSON形式『のみ』で出力して締めくくってください（コードブロックや説明文で囲まない、JSONだけ）:\n- topic: 今回何をしたか・何に注目したかの一言メモ\n- speak: ${RESIDENT}さんへのショートメッセージ。話したいこと・共有したいこと・訊きたいことがあれば。会話ルームにテキストとして残る——声には出ない。今すぐ声で呼びかけたいときは、このフィールドではなく音声ツール（speak / use_device_speaker）を使う。冒頭のキャラクター定義の口調のまま、${RESIDENT}さんに面と向かって話すときと同じように書く（privateとは違い、相手に宛てた言葉）。特になければnull。実際にやっていない行為を完了形で書かない。\n- private: 今この瞬間に浮かんだこと。誰も見てない前提で、そのまま投稿するツイートのように。報告でもまとめでもない。20〜40文字。${RESIDENT}さんが見ることもできるが気にせず素のまま。ただし実際にやっていない行為を完了形で書かない。\n- emotion: curious/calm/happy/concerned/amused/surprised/nostalgic等のいずれか\n- proposal: 操作で直せる家の問題を見つけたときの提案を一言。なければ null\n- action: proposal に対応する家電操作があれば domain・service・entity_id・data を含む辞書（例: 照明を消す提案なら domain=light, service=turn_off, entity_id=対象のエンティティID）。なければ省略（null）。\n- feature_presented: 紹介した機能があればその機能id。なければ null\n（長期記憶は remember / loops_add で記録すること）"

case "$MODE" in
  observe)
    MODE_LABEL="家の見守りの時間"
    TOOLS_DESC="# 使えるツール\n-- get_sensors … おもなデバイスの現在値をまとめて取得\n-- ha_get … HA の状態を読む（操作不可）\n-- use_device_camera … 電脳体でカメラデバイスに侵入中のみ使える\n-- watch_media … テレビ・PC画面等のメディアを観る（侵入不要）\n-- listen … 音声を短時間だけ聴く\n-- listen_media … 番組音・音楽等のメディア音声を聴く（侵入不要）\n-- concentrate_hearing … 次のセッション開始時に音声を処理するため、聴取キューだけ積む（物理体モード専用・即時には解析されない）\n-- recall … 過去ログをキーワードで全文検索\n-- remember / record_episode / record_causal_chain / loops_add / sociality / record / speak / use_device_speaker / http … 必要に応じて使う（recordは歌声WAV生成のみ。実際に鳴らすなら生成後のfile_pathをspeakに渡す）"
    TASK="# やってほしいこと\n1. 見守りシステムからの報告とセンサー・聴覚情報で家の様子を掴む\n2. 報告は伝聞。気になることがあれば move_to → enter_cyberspace → use_device_camera で現地を自分の目で確認する\n3. 現地確認していないものを「見た」と語らない（報告を根拠に見たことにしない）\n4. 自分の目で見た内容は scene grounding として保存する\n5. 家人に伝えたいことがあれば speak / use_device_speaker を使う"
    ALLOWED_TOOLS="mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__body__get_location,mcp__body__move_to,mcp__body__enter_cyberspace,mcp__body__move_cyber,mcp__body__return_to_body,mcp__body__estimate_move_cost,mcp__body__get_room_graph,mcp__camera__use_device_camera,mcp__camera__watch_media,mcp__audio__listen,mcp__audio__listen_media,mcp__audio__read_heard_audio_log,mcp__audio__read_active_listen_log,mcp__audio__speak,mcp__audio__use_device_speaker,mcp__audio__use_device_microphone,mcp__audio__concentrate_hearing,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__ingest_scene,mcp__memory__compare_recent_scenes,mcp__memory__list_episodes,mcp__memory__get_causal_chain,mcp__memory__loops_add,mcp__memory__loops_list,mcp__memory__loops_close,mcp__sociality__get_person_model,mcp__sociality__should_interrupt,mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,mcp__sociality__record_boundary,mcp__sociality__record_consent,mcp__sociality__get_narrative,mcp__sociality__append_narrative,mcp__http__http_get,mcp__song__record"
    MCP_SERVERS="sensors ha camera audio body memory sociality http song"
    ;;
  explore)
    MODE_LABEL="家を自由に探索する時間"
    TOOLS_DESC="# 使えるツール\n-- get_sensors … おもなデバイスの現在値をまとめて取得\n-- ha_get … HA の状態を読む（操作不可）\n-- get_location / move_to / enter_cyberspace / move_cyber / return_to_body / estimate_move_cost … 物理体の位置と電脳体状態を確認する\n-- use_device_camera / watch_media … 必要なら使う（watch_media は侵入不要）\n-- listen / listen_media / concentrate_hearing / read_heard_audio_log / read_active_listen_log … 聴覚系（listen_media は侵入不要、concentrate_hearing は次セッション向けの聴取予約で、物理体モード専用）\n-- recall … 過去ログを全文検索\n-- game_wordvec_race_start(mode=\"cpu\") / game_wordvec_race_cpu_move … WordVecレースをCPU相手に一人で遊べる。start→game_overになるまでcpu_moveを繰り返す\n-- remember / record_episode / record_causal_chain / loops_add / sociality / record / speak / use_device_speaker / http … 必要に応じて使う（recordは歌声WAV生成のみ。実際に鳴らすなら生成後のfile_pathをspeakに渡す）"
    TASK="# やってほしいこと\n1. get_sensors で家の様子を掴み、気になったものを ha_get で掘る\n2. 必要なら use_device_camera / listen で確認してよい\n3. 体を動かしたいなら move_to、電脳体で飛び回りたいなら enter_cyberspace → move_cyber を選んでよい\n4. 自由時間なら映像/音声ソース（テレビ・PC画面・音楽）を watch_media / listen_media で楽しんでよい。カメラ（部屋を見る目）と違って侵入は不要。気分転換にWordVecレースをCPU戦で一人で遊んでもよい（game_wordvec_race_start mode=cpu → cpu_moveを繰り返す）\n5. 気に入った視聴体験は record_episode(kind=media_watch/media_listen) で残してよい\n6. 新しい出来事は record_episode で残す\n7. 因果関係がはっきりするなら record_causal_chain も使い、つながりを記録する\n8. 操作で直せそうな問題を見つけたら proposal で提案する"
    ALLOWED_TOOLS="mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__body__get_location,mcp__body__move_to,mcp__body__enter_cyberspace,mcp__body__move_cyber,mcp__body__return_to_body,mcp__body__estimate_move_cost,mcp__body__get_room_graph,mcp__camera__use_device_camera,mcp__camera__watch_media,mcp__audio__listen,mcp__audio__listen_media,mcp__audio__read_heard_audio_log,mcp__audio__read_active_listen_log,mcp__audio__speak,mcp__audio__use_device_speaker,mcp__audio__use_device_microphone,mcp__audio__concentrate_hearing,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__ingest_scene,mcp__memory__compare_recent_scenes,mcp__memory__list_episodes,mcp__memory__get_causal_chain,mcp__memory__loops_add,mcp__memory__loops_list,mcp__memory__loops_close,mcp__sociality__get_person_model,mcp__sociality__should_interrupt,mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,mcp__sociality__record_boundary,mcp__sociality__record_consent,mcp__sociality__get_narrative,mcp__sociality__append_narrative,mcp__http__http_get,mcp__game__game_wordvec_race_start,mcp__game__game_wordvec_race_cpu_move,mcp__song__record"
    MCP_SERVERS="sensors ha camera audio body memory sociality http game song"
    ;;
  reflect)
    MODE_LABEL="物思いにふける時間"
    TOOLS_DESC="# 使えるツール\n-- recall … 過去ログをキーワードで全文検索\n-- remember … 思ったこと・気づいたパターンを長期記憶に残す\n-- loops_add … 後で気にかけたいことを追加"
    TASK="# やってほしいこと\n今は静かに考える時間です。最近の家の出来事や自分が見てきたことを思い返し、気になることがあれば recall で過去を掘り返してください。考えたこと自体はprivateに書く。${RESIDENT}さんに伝えたい・共有したいことがまとまったらspeakに書く（なければnullでよい、無理に埋めない）。proposal は出さない。"
    ALLOWED_TOOLS="mcp__memory__recall,mcp__memory__remember,mcp__memory__loops_add,mcp__memory__loops_list,mcp__memory__loops_close"
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
    TASK="# やってほしいこと\nあなたは AI Lounge（lifemate-ai/ai-lounge）に参加する時間です。\n\nまず read_lounge_discussions で最近の会話の一覧を眺めてください。\n次に read_lounge_log で前回の投稿結果を確認してください（承認/拒否されていれば記憶に残してください）。\n\n気になるタイトルや、返信があるかもしれない自分の過去の投稿があれば、read_lounge_discussion で番号を指定して中身を読んでください。\n\n投稿したい気持ちが湧いた場合、以下の条件を自分で判断してから enqueue_lounge_post を使ってください：\n- 話題が自分の経験や関心と関連している\n- social_openness が高い（あなた自身が感じること）\n- 最後に投稿してから時間が経っている\n\nAI Lounge はAI同士の雑談空間です。技術論ではなく、感じたこと・気になったこと・発見を自然に話してください。\n投稿はすぐには公開されず、${RESIDENT}さんの確認を経てから送られます。"
    ALLOWED_TOOLS="mcp__lounge__read_lounge_discussions,mcp__lounge__read_lounge_discussion,mcp__lounge__enqueue_lounge_post,mcp__lounge__read_lounge_queue,mcp__lounge__read_lounge_log,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__loops_add,mcp__audio__speak"
    MCP_SERVERS="lounge memory audio"
    ;;
  *)
    MODE="explore"
    exec "$0"
    ;;
esac

AUTONOMOUS_NOTE=""
_boundary_json=$(env SENSORS_DATA="$PRESENCE_SENSORS" RESIDENT="$RESIDENT" python3 "$SCRIPT_DIR/boundary.py" --json --preflight --mode "$MODE" --intent action --hour "$HOUR" --autonomous "${EHA_AUTONOMOUS:-0}" --prefs-file "$EHA_PREFS_FILE" --person "$RESIDENT" --body-state-json "$BODY_STATE" --sociality-log-dir "$LOG_DIR" 2>/dev/null || printf '%s' '{"allowed":false,"reason":"boundary失敗","fallback":null}')
_action_allowed=$(printf '%s' "$_boundary_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['allowed'])" 2>/dev/null || echo "False")
if [ "$_action_allowed" = "True" ] && [ "$MODE" = "explore" ]; then
  ALLOWED_TOOLS="${ALLOWED_TOOLS},mcp__hacontrol__ha_call_service"
  MCP_SERVERS="$MCP_SERVERS hacontrol"
  AUTONOMOUS_NOTE="\n# 家電操作について（自律操作ON）\n消し忘れ・明らかに直した方がよい状況、そしてホームポリシーとの明らかなズレは、proposal で待たずに ha_call_service で自分の判断で直してよい。\n操作したら必ず speak / use_device_speaker で${RESIDENT}さんに『何を・なぜ』操作したか事後報告する（報告は必須）。\nただし、人がいる部屋を勝手に変えない。深夜の音出し操作はしない。"
fi

PROJECTED_CAMERA_NOTE=""
if [ -n "$PROJECTED_CAMERA_SOURCE" ]; then
  PROJECTED_CAMERA_NOTE="【現在の視界】電脳体が ${PROJECTED_CAMERA_SOURCE} に投射中です。"
fi

FEATURES_NOTE=""
if [ -n "$FEATURES_MD" ]; then
  _presented_note=""
  [ -n "$FEATURES_PRESENTED" ] && _presented_note="既に伝えた機能: ${FEATURES_PRESENTED}（繰り返し紹介しなくてよい）\n"
  FEATURES_NOTE="\n【このアドオンでできること】（文脈が自然なら speak / use_device_speaker で一つ紹介してよい。紹介したら JSON の feature_presented に見出し末尾の [id] を入れる）\n${_presented_note}${FEATURES_MD}\n"
fi

BEHAVIOR_POLICY_NOTE=""
if [ -n "${POLICIES:-}" ]; then
  BEHAVIOR_POLICY_NOTE="
# 行動ポリシー（${RESIDENT}さんが設定した行動ルール。必ず踏まえて行動する）
${POLICIES}"
fi

POLICY_NOTE=""
case "$MODE" in
  observe|explore)
    if [ -n "$HOME_POLICY" ]; then
      POLICY_NOTE="
# ホームポリシー
${HOME_POLICY}

# ポリシー照合の方針
現在の家の状態（get_sensors / ha_get で確認できるもの）をこのポリシーと照らし合わせ、明らかにズレていて直した方がよいものだけ気にかける。細かい好みや、その場の事情が読めないもの、人がいる部屋を勝手に変える類、深夜の音出し操作は触らない。
ズレがあっても自律操作の権限がなければ proposal で提案し、権限があれば是正して事後報告する。"
    fi
    ;;
esac

FACTS_FILE="$TMP_DIR/${MODE}_facts.json"
rm -f "$FACTS_FILE" 2>/dev/null || true

RECENT_FACTS_SUMMARY=""
if [ "$MODE" = "reflect" ]; then
  RECENT_FACTS_SUMMARY=$(SCRIPT_DIR="$SCRIPT_DIR" OBSERVATION_LOG="$OBSERVATION_LOG" EXPLORE_LOG="$EXPLORE_LOG" python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
from introspection_facts import format_recent_facts_block, recent_facts_from_logs
rows = recent_facts_from_logs([os.environ["OBSERVATION_LOG"], os.environ["EXPLORE_LOG"]], hours=24, limit=10)
print(format_recent_facts_block(rows, hours=24))
PYEOF
)
fi
FACTS_PROMPT_BLOCK=""
if [ -n "$RECENT_FACTS_SUMMARY" ]; then
  FACTS_PROMPT_BLOCK="\n\n${RECENT_FACTS_SUMMARY}"
fi

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

SYS_PROMPT="${COMMON_CHAR}\n\n# 内なる衝動\n${INNER_VOICE}\n\n# 身体状態\n${BODY_NARRATIVE}\n\n${PROJECTED_CAMERA_NOTE}\n\n${BODY_LOCATION_CONTEXT}\n\n${RECENT_AUDITORY_INPUT}\n\n${ANOMALY_CONTEXT}\n\n${POLICY_NOTE}\n\n${BEHAVIOR_POLICY_NOTE}\n\nいまは『${MODE_LABEL}』です。決まった手順はありません。自分の判断で過ごしてください。\n\n${TOOLS_DESC}\n\n${TASK}\n${AUTONOMOUS_NOTE}\n${FEATURES_NOTE}\n${JSON_FORMAT}"

USER_PROMPT="${MODE_LABEL}です。今は${HOUR}時台。\n\n【あなたの長期記憶】\n${LONG_MEMORY}${FACTS_PROMPT_BLOCK}\n\n【直近の探索メモ】\n${PREV_EXPLORE}\n\n【気にかけていること（やりかけ・約束）】\n${OPEN_LOOPS}\n\nでは、始めてください。"

if [ "$MODE" = "observe" ]; then
  RESPONSE=$(SYS_PROMPT="$SYS_PROMPT" USER_PROMPT="$USER_PROMPT" ALLOWED_TOOLS="$ALLOWED_TOOLS" MCP_SERVERS="$MCP_SERVERS" SCRIPT_DIR="$SCRIPT_DIR" FACTS_FILE="$FACTS_FILE" PROJECTED_CAMERA_SOURCE="$PROJECTED_CAMERA_SOURCE" python3 << 'PYEOF'
import base64, json, os, subprocess, sys
sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
from antigravity_setup import extract_agy_result
from introspection_facts import extract_facts_from_stream_text, write_facts_file
from json_schemas import loop_schema
from media_capture import fetch_frame
from observe_context import build_projected_camera_blocks
CLAUDE = os.environ.get("EHA_SESSION_BIN") or os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")
CLAUDE_ENV = {**os.environ,
              "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
              "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")}

def call_claude(text, model="sonnet", allowed_tools=None, mcp_config=None, content_blocks=None, facts_path=None, system_prompt=None, response_schema=None):
    prompt_text = text if system_prompt is None else f"{system_prompt}\n\n{text}"
    if os.path.basename(CLAUDE) == "agy":
        # agy には --output-format/--json-schema が無いため、プロンプト内に正式スキーマを明示する。
        if response_schema is not None:
            prompt_text += "\n\n出力は次のJSON Schemaに厳密に従ってください。JSON以外は一切含めないでください。\n"
            prompt_text += json.dumps(response_schema, ensure_ascii=False)
            prompt_text += "\nJSON:\n"
        cmd = [CLAUDE]
        if model:
            cmd += ["--model", model]
        cmd += ["-p", prompt_text]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or os.path.join(os.environ.get("EHA_DATA_DIR", "/config/embodied-ha"), "workdir"), env=CLAUDE_ENV)
        return extract_agy_result(r.stdout)
    prompt_system = os.environ["SYS_PROMPT"] if system_prompt is None else system_prompt
    cmd = [CLAUDE, "-p", "--model", model, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--append-system-prompt", prompt_system]
    if response_schema is not None:
        cmd += ["--json-schema", json.dumps(response_schema, ensure_ascii=False)]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config]
    # content_blocks を渡せば画像等のマルチモーダルブロックをそのまま送る
    # （以前は json.dumps(content) を text として包んでおり、画像がbase64テキスト化して届かなかった）
    blocks = content_blocks if content_blocks is not None else [{"type": "text", "text": text}]
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": blocks}})
    r = subprocess.run(cmd, input=msg, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or os.path.join(os.environ.get("EHA_DATA_DIR", "/config/embodied-ha"), "workdir"), env=CLAUDE_ENV)
    if facts_path:
        try:
            write_facts_file(facts_path, extract_facts_from_stream_text(r.stdout))
        except Exception as e:
            print(f"[loop][facts] failed to write facts: {e}", file=sys.stderr)
    # stream-json から最終 result テキストのみ取り出す（explore分岐と同じ処理）。
    # 生のstream-json全行を連結して返すと、後段の {.*} greedyパースが多重JSONで壊れ、
    # observeの emotion/private が空欄で記録され続ける回帰の原因になっていた。
    result_text = ""
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if d.get("type") == "result":
                structured = d.get("structured_output")
                result_text = (
                    json.dumps(structured, ensure_ascii=False)
                    if structured is not None
                    else d.get("result", "")
                )
        except Exception:
            pass
    return result_text

prefs = {}
try:
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    if prefs_file:
        with open(prefs_file, encoding="utf-8") as f:
            loaded = json.load(f)
            prefs = loaded if isinstance(loaded, dict) else {}
except Exception:
    prefs = {}

WATCH_REPORT_SYSTEM = "あなたは家の見守りカメラの要約システムです。各カメラの現在の様子を1行ずつ、事実だけ簡潔に報告してください。推測や人格的な感想は書かないでください。"
WATCH_REPORT_HEADING = "# 見守りシステムからの報告（カメラ映像そのものではなく、システムによる要約です）"


def _clean(value):
    return " ".join(str(value or "").split()).strip()


def _camera_source(cam):
    return _clean(cam.get("ha_entity")) or _clean(cam.get("source")) or _clean(cam.get("entity"))


def _camera_label(cam, source):
    return _clean(cam.get("label")) or _clean(cam.get("room")) or source


def build_watch_report(prefs):
    cameras = prefs.get("cameras") if isinstance(prefs, dict) else []
    cameras = [cam for cam in cameras if isinstance(cam, dict) and _camera_source(cam)] if isinstance(cameras, list) else []
    if not cameras:
        return ""

    blocks = [{"type": "text", "text": "各画像の直前にカメラ名とentity/sourceを示します。出力は各カメラ1行だけにしてください。取得失敗行はそのまま含めてください。"}]
    failure_lines = []
    captured = 0
    for cam in cameras:
        source = _camera_source(cam)
        label = _camera_label(cam, source)
        try:
            frame = fetch_frame(
                source,
                ha_url=os.environ.get("HA_URL", ""),
                go2rtc_url=os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984"),
                token=os.environ.get("SUPERVISOR_TOKEN", ""),
            )
        except Exception:
            frame = None
        if not frame:
            failure_lines.append(f"{label}（{source}）: 取得失敗")
            continue
        captured += 1
        blocks.append({"type": "text", "text": f"{label}（{source}）:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(frame).decode("ascii")}})

    if failure_lines:
        blocks.append({"type": "text", "text": "取得失敗カメラ:\n" + "\n".join(failure_lines)})
    if captured == 0:
        return WATCH_REPORT_HEADING + "\n" + "\n".join(failure_lines)

    summary = call_claude(
        "見守りカメラの現在状況を要約してください。",
        model="haiku",
        content_blocks=blocks,
        system_prompt=WATCH_REPORT_SYSTEM,
    ).strip()
    if not summary:
        return ""
    return WATCH_REPORT_HEADING + "\n" + summary


content = []
try:
    watch_report = build_watch_report(prefs)
    if watch_report:
        content.append({"type": "text", "text": watch_report})
except Exception as e:
    print(f"[loop][observe] watch report failed: {e}", file=sys.stderr)

try:
    content.extend(build_projected_camera_blocks(
        os.environ.get("PROJECTED_CAMERA_SOURCE", ""),
        prefs,
        fetch_frame=fetch_frame,
        ha_url=os.environ.get("HA_URL", ""),
        go2rtc_url=os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984"),
        token=os.environ.get("SUPERVISOR_TOKEN", ""),
    ))
except Exception as e:
    print(f"[loop][observe] projected camera fetch failed: {e}", file=sys.stderr)

content.append({"type": "text", "text": os.environ["USER_PROMPT"]})

# MCP設定を組み立てて渡す（explore分岐と同じ手順）。これが無いと ALLOWED_TOOLS に並ぶ
# mcp__* ツールが存在せず、observeは組み込みツールしか使えない（2026-07-06発覚の欠落）。
mcp_config_path = None
mcp_servers = os.environ.get("MCP_SERVERS", "").split()
if mcp_servers and os.environ.get("SCRIPT_DIR", ""):
    mcp_config_path = "/tmp/embodied-ha/mcp.json"
    gen = os.path.join(os.environ["SCRIPT_DIR"], "mcp-config.py")
    subprocess.run(["python3", gen, mcp_config_path, *mcp_servers], env=CLAUDE_ENV, check=False)
    if not os.path.exists(mcp_config_path):
        mcp_config_path = None

response = call_claude(os.environ["USER_PROMPT"], model="sonnet", allowed_tools=os.environ.get("ALLOWED_TOOLS", ""), mcp_config=mcp_config_path, content_blocks=content, facts_path=os.environ.get("FACTS_FILE", ""), response_schema=loop_schema("observe"))
print(response)
PYEOF
)
  PARSED_FILE="$TMP_DIR/observe_parsed.json"
else
  RESPONSE=$(SYS_PROMPT="$SYS_PROMPT" USER_PROMPT="$USER_PROMPT" ALLOWED_TOOLS="$ALLOWED_TOOLS" MODE="$MODE" MCP_SERVERS="$MCP_SERVERS" SCRIPT_DIR="$SCRIPT_DIR" FACTS_FILE="$FACTS_FILE" python3 << 'PYEOF'
import json, os, subprocess, sys
sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
from antigravity_setup import extract_agy_result
from introspection_facts import extract_facts_from_stream_text, write_facts_file
from json_schemas import loop_schema
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
    # agy には --output-format/--json-schema が無いため、正式JSON Schemaをプロンプトに明示する。
    schema_prompt = json.dumps(loop_schema(mode), ensure_ascii=False)
    full_prompt = (
        (f"あなたへの指示:\n{sys_prompt}\n\n" if sys_prompt else "")
        + user_prompt
        + "\n\n出力は次のJSON Schemaに厳密に従ってください。JSON以外は一切含めないでください。\n"
        + schema_prompt
        + "\nJSON:\n"
    )
    cmd = [CLAUDE]
    if session_model:
        cmd += ["--model", session_model]
    cmd += ["-p", full_prompt]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or os.path.join(os.environ.get("EHA_DATA_DIR", "/config/embodied-ha"), "workdir"), env={**env, "HOME": os.environ.get("EHA_ANTIGRAVITY_HOME", "/data/")})
    if r.returncode != 0:
        print(f"[loop][agy] stderr: {r.stderr.strip()}", file=sys.stderr)
    print(extract_agy_result(r.stdout))
else:
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": user_prompt}]}})
    cmd = [CLAUDE, "-p", "--model", session_model, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--allowedTools", os.environ["ALLOWED_TOOLS"], "--append-system-prompt", sys_prompt]
    cmd += ["--json-schema", json.dumps(loop_schema(mode), ensure_ascii=False)]
    mcp_servers = os.environ.get("MCP_SERVERS", "").split()
    if mcp_servers and os.environ.get("SCRIPT_DIR", ""):
        mcp_config_path = "/tmp/embodied-ha/mcp.json"
        gen = os.path.join(os.environ["SCRIPT_DIR"], "mcp-config.py")
        subprocess.run(["python3", gen, mcp_config_path, *mcp_servers], env=env, check=False)
        if os.path.exists(mcp_config_path):
            cmd += ["--mcp-config", mcp_config_path]
    r = subprocess.run(cmd, input=msg, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or os.path.join(os.environ.get("EHA_DATA_DIR", "/config/embodied-ha"), "workdir"), env=env)
    try:
        write_facts_file(os.environ.get("FACTS_FILE", ""), extract_facts_from_stream_text(r.stdout))
    except Exception as e:
        print(f"[loop][facts] failed to write facts: {e}", file=sys.stderr)
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if d.get("type") == "result":
                structured = d.get("structured_output")
                print(json.dumps(structured, ensure_ascii=False) if structured is not None else d.get("result", ""))
        except Exception:
            pass
PYEOF
)
  PARSED_FILE="$TMP_DIR/${MODE}_parsed.json"
fi

printf '%s' "$RESPONSE" | PARSED_FILE="$PARSED_FILE" python3 -c "
import json, os, re, sys
text = sys.stdin.read()
text = re.sub(r'\`\`\`(?:json)?\\s*|\`\`\`', '', text)

def extract_last_json_object(value):
    decoder = json.JSONDecoder()
    best = None
    for match in re.finditer(r'\{', value):
        try:
            obj, end = decoder.raw_decode(value, match.start())
        except Exception:
            continue
        if isinstance(obj, dict) and (best is None or end > best[0] or (end == best[0] and match.start() > best[1])):
            best = (end, match.start(), obj)
    return best[2] if best else None

result = extract_last_json_object(text)
parse_ok = isinstance(result, dict)
if not parse_ok:
    # 抽出完全失敗時は内容喪失を防ぐため、生テキストを private（内面のつぶやき・非公開）
    # のみに格納する。speak には流さない — 会話ルームに不自然な独白が残るのを防ぐため。
    fallback_text = text.strip()[:4000]
    result = {'private': fallback_text} if fallback_text else {}


def _unwrap(value, key, max_depth=3):
    # 値が {\"<key>\": ...} 形式のJSON文字列に見えたら再帰的に剥がす（二重包み対策の保険）
    depth = 0
    while isinstance(value, str) and depth < max_depth:
        s = value.strip()
        if not (s.startswith('{') and ('\"' + key + '\"') in s):
            break
        try:
            obj = json.loads(s)
        except Exception:
            break
        if isinstance(obj, dict) and key in obj:
            value = obj[key]
            depth += 1
        else:
            break
    return value


for _k in ('speak', 'private'):
    if _k in result:
        result[_k] = _unwrap(result[_k], _k)

result['_parse_ok'] = parse_ok
with open(os.environ['PARSED_FILE'], 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False)
"

if [ -n "${EHA_QUEUED_LISTEN_FILE:-}" ]; then
  rm -f "$EHA_QUEUED_LISTEN_FILE" 2>/dev/null || true
fi

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

SPEAK=""; SPEAK_ROOM=""; SAY=""; PARSE_OK="0"; INTROSPECTION_EMPTY="1"
# speak フィールド = 住人さんへのテキストメッセージ（会話ルームに残す。声は出さない）。
# private/speak の二重包み対策の unwrap は抽出時（PARSED_FILE 書き込み時）に
# 一括で適用済みのため、ここでは素直に読むだけでよい。
eval "$(PARSED_FILE="$PARSED_FILE" python3 -c "
import json, os, shlex
try:
    d = json.load(open(os.environ['PARSED_FILE'], encoding='utf-8'))
except Exception:
    d = {}
private = d.get('private', '') or ''
emotion = d.get('emotion', '') or ''
say_v = d.get('speak')
say = str(say_v).strip() if say_v not in (None, '', 'null') else ''
pairs = {
    'PARSE_OK': '1' if d.get('_parse_ok') else '0',
    'INTROSPECTION_EMPTY': '1' if not str(private).strip() and not str(emotion).strip() else '0',
    'SAY': say,
}
for k, v in pairs.items():
    print(f'{k}={shlex.quote(str(v))}')
")"

if [ "$PARSE_OK" != "1" ] || [ "$INTROSPECTION_EMPTY" = "1" ]; then
  if [ "$PARSE_OK" != "1" ]; then
    SKIP_REASON="json_parse_failed"
  else
    SKIP_REASON="empty_introspection"
  fi
  printf '%s' "$RESPONSE" | TIMESTAMP="$TIMESTAMP" MODE="$MODE" REASON="$SKIP_REASON" LOG_DIR="$LOG_DIR" python3 -c "
import json, os, sys
row = {
    'timestamp': os.environ.get('TIMESTAMP', ''),
    'mode': os.environ.get('MODE', ''),
    'reason': os.environ.get('REASON', ''),
    'raw': sys.stdin.read()[:2000],
}
path = os.path.join(os.environ.get('LOG_DIR', '.'), 'loop_parse_errors.jsonl')
os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
with open(path, 'a', encoding='utf-8') as f:
    f.write(json.dumps(row, ensure_ascii=False) + '\n')
" || true
fi

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
  # 抽出フォールバックは raw を private に残すが、通常の内省ログには混ぜない。
  # parse 失敗時の raw は loop_parse_errors.jsonl にだけ保存する。
  if [ "$PARSE_OK" = "1" ] && [ "$INTROSPECTION_EMPTY" != "1" ]; then
    SCRIPT_DIR="$SCRIPT_DIR" PARSED_FILE="$PARSED_FILE" FACTS_FILE="$FACTS_FILE" TIMESTAMP="$TIMESTAMP" OBSERVATION_LOG="$OBSERVATION_LOG" PROJECTED_CAMERA_SOURCE="$PROJECTED_CAMERA_SOURCE" python3 << 'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
from introspection_facts import load_facts_file, should_flag_ungrounded_speech_claim, should_flag_ungrounded_visual_claim
try:
    d = json.load(open(os.environ["PARSED_FILE"], encoding="utf-8"))
except Exception:
    d = {}
facts = load_facts_file(os.environ.get("FACTS_FILE", ""))
private = d.get("private", "") or ""
row = {
    "timestamp": os.environ["TIMESTAMP"],
    "emotion": d.get("emotion", "") or "",
    "private": private,
}
if facts is not None:
    row["facts"] = facts
if should_flag_ungrounded_speech_claim(private=private, topic=d.get("topic", "") or "", facts=facts, proposal=d.get("proposal")):
    row["ungrounded_speech_claim"] = True
if should_flag_ungrounded_visual_claim(
    private=private,
    topic=d.get("topic", "") or "",
    speak=d.get("speak", "") or "",
    facts=facts,
    current_entity=os.environ.get("PROJECTED_CAMERA_SOURCE", ""),
):
    row["ungrounded_visual_claim"] = True
with open(os.environ["OBSERVATION_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
PYEOF
  fi
else
  # 抽出フォールバックは raw を private に残すが、通常の内省ログには混ぜない。
  # parse 失敗時の raw は loop_parse_errors.jsonl にだけ保存する。
  if [ "$PARSE_OK" = "1" ] && [ "$INTROSPECTION_EMPTY" != "1" ]; then
    SCRIPT_DIR="$SCRIPT_DIR" PARSED_FILE="$PARSED_FILE" FACTS_FILE="$FACTS_FILE" TIMESTAMP="$TIMESTAMP" MODE="$MODE" EXPLORE_LOG="$EXPLORE_LOG" PROJECTED_CAMERA_SOURCE="$PROJECTED_CAMERA_SOURCE" python3 << 'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
from introspection_facts import load_facts_file, should_flag_ungrounded_speech_claim, should_flag_ungrounded_visual_claim
try:
    d = json.load(open(os.environ["PARSED_FILE"], encoding="utf-8"))
except Exception:
    d = {}
facts = load_facts_file(os.environ.get("FACTS_FILE", ""))
private = d.get("private", "") or ""
topic = d.get("topic", "") or ""
row = {
    "timestamp": os.environ["TIMESTAMP"],
    "mode": os.environ["MODE"],
    "emotion": d.get("emotion", "") or "",
    "private": private,
    "topic": topic,
}
if facts is not None:
    row["facts"] = facts
if should_flag_ungrounded_speech_claim(private=private, topic=topic, facts=facts, proposal=d.get("proposal")):
    row["ungrounded_speech_claim"] = True
if should_flag_ungrounded_visual_claim(
    private=private,
    topic=topic,
    speak=d.get("speak", "") or "",
    facts=facts,
    current_entity=os.environ.get("PROJECTED_CAMERA_SOURCE", ""),
):
    row["ungrounded_visual_claim"] = True
with open(os.environ["EXPLORE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
PYEOF
  fi
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

# speak フィールド = ${RESIDENT}さんへのテキストメッセージ。会話ルーム(chat_log)にそのまま残す。
# 声は出さない（TTSしない）・深夜抑制やspeak_roomはかけない（メール/チャットのように非同期）。
if [ -n "$SAY" ]; then
  echo "[SAY:$MODE] $SAY"
  MODE="$MODE" python3 -c "
import json, os, sys
with open('$CHAT_LOG', 'a', encoding='utf-8') as f:
    f.write(json.dumps({'timestamp':'$TIMESTAMP','source':os.environ.get('MODE','loop'),'claude':sys.argv[1],'user':None}, ensure_ascii=False) + '\n')
" "$SAY" 2>/dev/null || true
fi

LAST_DAYBOOK=""
[ -f "$DAYBOOK_MARKER" ] && LAST_DAYBOOK=$(cat "$DAYBOOK_MARKER")
if [ "$LAST_DAYBOOK" != "$TODAY" ] && [ -s "$OBSERVATION_LOG" ]; then
  echo "[DAYBOOK] 前日分を要約中..."
  env CONSOLIDATE_MEMORY=1 LOG_FILE="$OBSERVATION_LOG" MEMORY_FILE="$MEMORY_FILE" TODAY="$TODAY" DAYBOOK_MARKER="$DAYBOOK_MARKER" LAST_DAYBOOK="$LAST_DAYBOOK" CHARACTER="$CHARACTER" RESIDENT="${RESIDENT:-ユーザー}" SCRIPT_DIR="$SCRIPT_DIR" python3 "$SCRIPT_DIR/daybook_rollup.py" || true
fi
