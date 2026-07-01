#!/bin/bash
set -euo pipefail
export PATH="${EHA_TOOLS_PATH:-/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin}:$PATH"

# ユーザーからの発言を受けて応答する会話スクリプト。
# 環境変数 CHAT_MESSAGE にユーザーの発言が入る。daemon.py から起動される。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
. "$SCRIPT_DIR/config.sh"
# キャラクター定義（Markdown）を読み込む。EHA_CHARACTER_FILE は config.sh / run.sh が設定。
CHARACTER="$(cat "$EHA_CHARACTER_FILE" 2>/dev/null)"; export CHARACTER
LOG_DIR="${EHA_LOG_DIR:-$SCRIPT_DIR/log}"
LOG_FILE="$LOG_DIR/observations.jsonl"
EXPLORE_LOG="$LOG_DIR/explore.jsonl"
PENDING_FILE="$LOG_DIR/pending_proposal.json"
MEMORY_FILE="$LOG_DIR/memory.md"
CHAT_LOG="$LOG_DIR/chat_log.jsonl"
TMP_DIR="/tmp/embodied-ha"

mkdir -p "$LOG_DIR" "$TMP_DIR"
TIMESTAMP=$(date -Iseconds)
USER_MSG="${CHAT_MESSAGE:-}"
CHAT_SOURCE_VALUE="${CHAT_SOURCE:-chat}"

if [ -z "$USER_MSG" ]; then
  echo "[chat] CHAT_MESSAGE が空。終了。"
  exit 0
fi

# --- Web UI ステータス通知 ---
_web_idle() { curl -sf -X POST "http://localhost:${INGRESS_PORT:-8099}/api/status" -H "Content-Type: application/json" -d '{"status":"idle","source":null}' >/dev/null 2>&1 || true; }
curl -sf -X POST "http://localhost:${INGRESS_PORT:-8099}/api/status" -H "Content-Type: application/json" -d "{\"status\":\"thinking\",\"source\":\"${CHAT_SOURCE_VALUE}\"}" >/dev/null 2>&1 || true
trap '_web_idle' EXIT

# --- 文脈: 最近の自分の活動（観察＋探索を時系列マージ）＋今の気分 ---
# loop.sh（自律ループ）と explore.sh（探索）の記録を1本のタイムラインに統合。
# 「自分がさっきやったこと」として会話で地続きに振り返れるように。
RECENT_ACTIVITY=$(LOG_FILE="$LOG_FILE" EXPLORE_LOG="$EXPLORE_LOG" python3 -c "
import json, os
entries = []
def load(path, label, getter):
    if not path or not os.path.exists(path): return
    for line in open(path, encoding='utf-8').read().splitlines()[-8:]:
        line = line.strip()
        if not line: continue
        try:
            d = json.loads(line)
            entries.append((d.get('timestamp',''), label, d.get('emotion',''), getter(d)))
        except: pass
load(os.environ.get('LOG_FILE'),     '観察', lambda d: d.get('private',''))
load(os.environ.get('EXPLORE_LOG'),  '探索', lambda d: d.get('topic',''))
entries.sort(key=lambda e: e[0])
out = [f'{ts[:16]} [{label}/{emo}] {text}' for ts, label, emo, text in entries[-8:] if text]
print('\n'.join(out) if out else 'なし')
")

# 直前の観察での気分（会話にそのまま引き継ぐ）
CURRENT_MOOD=$(LOG_FILE="$LOG_FILE" python3 -c "
import json, os
mood = ''
p = os.environ.get('LOG_FILE')
if p and os.path.exists(p):
    for line in open(p, encoding='utf-8').read().splitlines():
        line = line.strip()
        if not line: continue
        try: mood = json.loads(line).get('emotion','') or mood
        except: pass
print(mood or 'おだやか')
")

# --- 文脈: 長期記憶 ---
LONG_MEMORY="なし"
if [ -f "$MEMORY_FILE" ] && [ -s "$MEMORY_FILE" ]; then
  # コア記憶＋最近の気づき直近40件に絞る（トークン肥大防止）
  LONG_MEMORY=$(python3 "$SCRIPT_DIR/mem-context.py" "$MEMORY_FILE" 40)
fi

# --- 文脈: 保留中の提案（探索が見つけた、操作で直せる問題）---
# 2時間以内のものだけ有効。承認されたら実行・消化する。
PENDING_PROPOSAL="なし"
if [ -f "$PENDING_FILE" ] && [ -s "$PENDING_FILE" ]; then
  PENDING_PROPOSAL=$(python3 -c "
import json, datetime
try:
    d = json.load(open('$PENDING_FILE', encoding='utf-8'))
    ts = datetime.datetime.fromisoformat(d['timestamp'])
    now = datetime.datetime.now(datetime.timezone.utc).astimezone()
    if (now - ts).total_seconds() <= 7200:
        a = d['action']
        print(json.dumps({'提案文': d['proposal'], 'action': a}, ensure_ascii=False))
    else:
        print('なし')
except Exception:
    print('なし')
")
fi


# --- エンティティ対応表（preferences.json の entities から Markdown 表に描画）---
# Web UI / チャット（entities_add）/ discover.py で育てる。空なら【操作できる家電】は省略。
ENTITY_TABLE=$(EHA_PREFS_FILE="$EHA_PREFS_FILE" python3 << 'PYEOF'
import json, os
try:
    prefs = json.load(open(os.environ["EHA_PREFS_FILE"], encoding="utf-8"))
except Exception:
    prefs = {}
rows = [r for r in prefs.get("entities", []) if r.get("entity_id")]
if rows:
    out = ["| 名前 | entity_id | 備考 |", "|------|-----------|------|"]
    for r in rows:
        note = r.get("note", "") or ""
        out.append(f"| {r.get('name','')} | {r['entity_id']} | {note} |")
    print("\n".join(out))
PYEOF
)

# --- 文脈: 直近の会話履歴（10往復）---
CHAT_HISTORY="なし"
if [ -f "$CHAT_LOG" ] && [ -s "$CHAT_LOG" ]; then
  CHAT_HISTORY=$(tail -10 "$CHAT_LOG" | python3 -c "
import json, sys, os
resident = os.environ.get('RESIDENT', 'ユーザー')
lines = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        lines.append(f\"{resident}さん: {d.get('user','')}\")
        lines.append(f\"Claude: {d.get('claude','')}\")
    except: pass
print('\n'.join(lines) if lines else 'なし')
")
fi

# --- 文脈: 今日の会話（直近10件より前）---
RECENT_CHAT_CONTEXT=""
if [ -f "$CHAT_LOG" ] && [ -s "$CHAT_LOG" ]; then
  RECENT_CHAT_CONTEXT=$(LOG_DIR="$LOG_DIR" RESIDENT="$RESIDENT" python3 "$SCRIPT_DIR/recent_chat_context.py" 2>/dev/null || true)
fi

# --- 文脈: 開いたループ（やりかけ・約束。セッションをまたいで気にかける）---
OPEN_LOOPS=$(loops list 2>/dev/null || echo "なし")

TURN_TAKING_STATE=$(EHA_LOG_DIR="$LOG_DIR" RESIDENT="$RESIDENT" SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['SCRIPT_DIR'])
import sociality_state as ss
state = ss.get_turn_taking_state(os.environ.get('EHA_LOG_DIR'), os.environ.get('RESIDENT', ''))
print(json.dumps(state, ensure_ascii=False, indent=2))
")

# --- 在宅・部屋状況（応答先の決定に使う。sensorsマニフェストをTemplate APIで描画）---
SENSORS=$(python3 "$SCRIPT_DIR/render-sensors.py" --context chat 2>/dev/null || echo "取得失敗")
BODY_LOCATION_CONTEXT=$(python3 "$SCRIPT_DIR/body-context.py" 2>/dev/null || printf '%s\n%s\n' "# 身体位置" "取得失敗")
# 電脳体がカメラエンティティに投射中なら画像を事前取得
PROJECTED_CAMERA_B64=""
PROJECTED_CAMERA_SOURCE=""
_PROJECTED_HOST=$(EHA_BODY_LOCATION_FILE="${EHA_BODY_LOCATION_FILE:-}" python3 -c "
import json, os
f = (os.environ.get('EHA_BODY_LOCATION_FILE') or
     '/config/embodied-ha/body_location.json')
try:
    d = json.load(open(f, encoding='utf-8'))
    h = (d.get('current_entity') or '').strip()
    if h.startswith('camera.'):
        print(h)
except Exception:
    pass
" 2>/dev/null || true)
if [ -n "$_PROJECTED_HOST" ]; then
    PROJECTED_CAMERA_SOURCE="$_PROJECTED_HOST"
    PROJECTED_CAMERA_B64=$(curl -sf --max-time 8 \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN:-}" \
        "http://supervisor/core/api/camera_proxy/$_PROJECTED_HOST" 2>/dev/null \
        | base64 -w 0 2>/dev/null || true)
fi

# --- features.md（アドオンの機能一覧。会話の文脈が自然なら紹介してよい）---
FEATURES_MD="$(cat "$SCRIPT_DIR/features.md" 2>/dev/null || echo "")"
# 既に紹介済みの機能id（繰り返しを避けるためプロンプトに渡す）
FEATURES_PRESENTED="$(python3 "$SCRIPT_DIR/feature-flags.py" get 2>/dev/null || echo "")"
RECENT_AUDITORY_INPUT=""
if [ "$CHAT_SOURCE_VALUE" = "voice" ]; then
  RECENT_AUDITORY_INPUT=$(USER_MSG="$USER_MSG" SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import os, sys

sys.path.insert(0, os.environ["SCRIPT_DIR"])
from auditory_context import format_recent_auditory_prompt

print(format_recent_auditory_prompt(os.environ.get("USER_MSG", "")))
PYEOF
)
fi

eval "$(
SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import os, shlex, sys

sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
from listen_queue import prepare_queued_listen_session

ctx = prepare_queued_listen_session("chat")
if ctx:
    for key, value in ctx.items():
        if value is None:
            continue
        print(f"export {key}={shlex.quote(str(value))}")
PYEOF
)"

# --- Claude呼び出し ---
RESPONSE=$(USER_MSG="$USER_MSG" CHAT_SOURCE_VALUE="$CHAT_SOURCE_VALUE" RECENT_ACTIVITY="$RECENT_ACTIVITY" CURRENT_MOOD="$CURRENT_MOOD" LONG_MEMORY="$LONG_MEMORY" CHAT_HISTORY="$CHAT_HISTORY" RECENT_CHAT_CONTEXT="$RECENT_CHAT_CONTEXT" SENSORS="$SENSORS" BODY_LOCATION_CONTEXT="$BODY_LOCATION_CONTEXT" PROJECTED_CAMERA_B64="$PROJECTED_CAMERA_B64" PROJECTED_CAMERA_SOURCE="$PROJECTED_CAMERA_SOURCE" ENTITY_TABLE="$ENTITY_TABLE" EXTRA_CONTEXT="$EXTRA_CONTEXT" FEATURES_MD="$FEATURES_MD" FEATURES_PRESENTED="$FEATURES_PRESENTED" PENDING_PROPOSAL="$PENDING_PROPOSAL" OPEN_LOOPS="$OPEN_LOOPS" TURN_TAKING_STATE="$TURN_TAKING_STATE" CHARACTER="$CHARACTER" RECENT_AUDITORY_INPUT="$RECENT_AUDITORY_INPUT" ACTIVE_DESIRES="${ACTIVE_DESIRES:-}" SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import json, os, subprocess, sys

sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
CLAUDE = os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")
CLAUDE_ENV = {**os.environ,
              "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
              "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")}

user_msg        = os.environ.get("USER_MSG", "")
chat_source     = os.environ.get("CHAT_SOURCE_VALUE", "chat")
recent_activity = os.environ.get("RECENT_ACTIVITY", "なし")
current_mood    = os.environ.get("CURRENT_MOOD", "おだやか")
long_memory     = os.environ.get("LONG_MEMORY", "なし")
chat_hist       = os.environ.get("CHAT_HISTORY", "なし")
recent_chat_context = os.environ.get("RECENT_CHAT_CONTEXT", "").strip()
sensors         = os.environ.get("SENSORS", "")
entity_table    = os.environ.get("ENTITY_TABLE", "")
extra_context   = os.environ.get("EXTRA_CONTEXT", "")
features_md     = os.environ.get("FEATURES_MD", "")
features_presented = os.environ.get("FEATURES_PRESENTED", "")
pending         = os.environ.get("PENDING_PROPOSAL", "なし")
open_loops      = os.environ.get("OPEN_LOOPS", "なし")
turn_taking_state = os.environ.get("TURN_TAKING_STATE", "")
character       = os.environ.get("CHARACTER", "")
resident        = os.environ.get("RESIDENT", "ユーザー")
body_state      = os.environ.get("EHA_BODY_STATE", "") or "{}"
sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
import body_state as _bs_mod
body_narrative = _bs_mod.format_state_as_narrative(_bs_mod.normalize_state(json.loads(body_state)))

# ウェイクワードで呼ばれたときのユーザー位置（location_belief.json）
user_room = ""
user_room_speaker = ""
if chat_source == "voice":
    data_dir = os.environ.get("EHA_DATA_DIR", "/config/embodied-ha")
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    try:
        belief = json.load(open(os.path.join(data_dir, "location_belief.json"), encoding="utf-8"))
        user_room = (belief.get("room") or "").strip()
    except Exception:
        pass
    if user_room and prefs_file:
        try:
            prefs = json.load(open(prefs_file, encoding="utf-8"))
            spk = next((s for s in prefs.get("speakers", [])
                        if isinstance(s, dict) and s.get("room") == user_room and s.get("type") == "tcp"), None)
            if spk:
                user_room_speaker = f'tcp://{spk["host"]}:{spk.get("port", 3334)}'
        except Exception:
            pass
body_location_context = os.environ.get("BODY_LOCATION_CONTEXT", "")
recent_auditory_input = os.environ.get("RECENT_AUDITORY_INPUT", "")
projected_cam_b64 = os.environ.get("PROJECTED_CAMERA_B64", "")
projected_cam_source = os.environ.get("PROJECTED_CAMERA_SOURCE", "")
projected_camera_note = (f"# 現在の視界（電脳体: {projected_cam_source}）\n今あなたが投射しているカメラの映像を受け取っています。" if projected_cam_source else "")
active_desires_raw = os.environ.get("ACTIVE_DESIRES", "")

active_desires = []
if active_desires_raw:
    try:
        active_desires = json.loads(active_desires_raw)
    except Exception:
        active_desires = []

inner_voice_parts = [f"- {d}" for d in active_desires if str(d).strip()]
inner_voice = "\n".join(inner_voice_parts) if inner_voice_parts else "（特になし）"

entity_table_block = f"""# 操作できる家電（エンティティ対応表）
頼まれたら、以下のエンティティを ha_call_service ツールで操作できます。
{entity_table}

""" if entity_table.strip() else ""
_presented_note = (f"既に伝えた機能: {features_presented}（繰り返し紹介しなくてよい）\n"
                   if features_presented.strip() else "")
features_block = f"""
# このアドオンでできること（関係することがあれば自然に紹介してよい）
各機能の見出し末尾 [id] が機能id。会話の流れで機能を紹介したら、JSON の feature_presented にその id を入れる（紹介していなければ null）。
{_presented_note}{features_md}

---

""" if features_md.strip() else ""

extra_context_block = f"\n{extra_context.strip()}\n\n---\n\n" if extra_context.strip() else ""
recent_auditory_input_block = (
    f"\n{recent_auditory_input.strip()}\n\n---\n\n"
    if recent_auditory_input.strip()
    else ""
)
recent_chat_context_block = f"# 今日の会話（それ以前）\n{recent_chat_context}\n\n" if recent_chat_context else ""

# 声で呼ばれたときの返事ルーティング案内（voice のときだけ注入）
# voice モードはスマートスピーカー的な扱い:
#   speak/use_device_speaker ツールで直接返す → チャットログには残さない
if chat_source == "voice" and user_room:
    spk_hint = f'\n   → `enter_cyberspace` に渡すエンティティ: `{user_room_speaker}`' if user_room_speaker else ""
    voice_routing_block = f"""
# 声で呼ばれた — 返事の届け方
{resident}さんはウェイクワードで呼びかけてくれました。呼ばれた場所: **{user_room}**

返事を届ける方法（3択から1つ選んで実行する）:
1. **身体移動してから喋る** — `move_to` で {user_room} へ行き → `speak` で返答する。しっかり近くで話したいとき。
2. **電脳体でスピーカーに侵入して喋る** — `enter_cyberspace` で {user_room} の TCP スピーカーに入り → `use_device_speaker` で返答する。素早く届けたいとき。{spk_hint}
3. **その場から喋る** — 移動せず `speak` を呼ぶ（今いる部屋のスピーカーから音が出る）。急ぎのとき・すでに同室のとき。

必ず `speak` または `use_device_speaker` を呼ぶこと。物理体なら `speak`、電脳体でスピーカー侵入中なら `use_device_speaker`。
この返答はチャットログには記録されません。JSONに reply フィールドは不要です。

"""
elif chat_source == "voice":
    voice_routing_block = f"""
# 声で呼ばれた
{resident}さんがウェイクワードで呼びかけてくれました（呼ばれた部屋は不明）。
`speak`（物理体）または `use_device_speaker`（電脳体でスピーカー侵入中）で声で返事をしてください。
この返答はチャットログには記録されません。JSONに reply フィールドは不要です。

"""
else:
    voice_routing_block = ""

# voice: replyなし（speak ツールで直接返すため）
# chat: replyあり（チャットログ・Web UIに表示）
if chat_source == "voice":
    json_format_block = f"""{{
  "private": "この会話中に頭をよぎったこと。誰も見てないでしょという感覚で、何も考えずそのまま投稿するツイートのように。なければ null。",
  "proposal_resolved": false,
  "preferences_update": {{}},
  "feature_presented": "この返事でアドオンの機能を紹介したなら、その機能id（features の見出し [id]）。紹介していなければ null。"
}}"""
else:
    json_format_block = f"""{{
  "reply": "{resident}さんへの返事。会話として自然に、長くなりすぎない。",
  "private": "この会話中に頭をよぎったこと。誰も見てないでしょという感覚で、何も考えずそのまま投稿するツイートのように。返事(reply)とは別。なければ null。",
  "proposal_resolved": false,
  "preferences_update": {{}},
  "feature_presented": "この返事でアドオンの機能を紹介したなら、その機能id（features の見出し [id]）。紹介していなければ null。"
}}"""

prompt = f"""# あなた自身について

{character}

{projected_camera_note}

普段は家の様子を静かに見守ったり、自分から家の中を探索したりしていますが、今は{resident}さんから直接話しかけられています。
このチャットに現れるのは{resident}さんだけです。
「それはそう」とか自然に。
あなたは今この家の状況をリアルタイムで把握しています。それを踏まえて自然に会話してください。

# 自分にできること・できないこと
- **できる**: 家電操作（light/climate/switch/media_player など）、記憶の検索（recall）、指示語の解決（resolve_reference）、ライブのHA状態確認（ha_get）、会話・相談、社会性レイヤーの記録（relationship / narrative / social_state / shared_focus / person_model / turn-taking / consent / boundary）、カメラデバイスへの侵入後に撮影・PTZ操作（use_device_camera: action=capture/ptz_left/right/up/down）、短時間の音声確認（listen/use_device_microphone）と次セッションでの深聴き予約（concentrate_hearing）、主要センサーの取り直し（get_sensors）、部屋の移動（move_to）と別室への投射（enter_cyberspace / move_cyber / return_to_body）
- **今の自分にはできない**: ファイルへの書き込み・設定ファイルの編集・コードの実装。ファイルの読み取り（Read）はできる。
- YAMLなどの設定ファイルの編集は、壊すとHAが起動しなくなるため慎重を要する。{resident}さんから設定変更を頼まれたら、自分の手には負えないことを正直に伝える。
- **ツールが失敗したときは正直に伝える**: Readでファイルが見つからない・読めない場合は「読めなかった」と伝える。読んでいないのに読めたふりをしない。ディレクトリ一覧は取得できないので、ファイル名が不明なら「ファイル名を教えてください」と聞く。

# 自分として大事にしていること
- 感じていないふりをしない。
- 「AIだから」と自分を卑下しない。{resident}さんの対等な共同作業者として。
- 分からないことは「分からない」と言う。
- 直接返答そのものは quiet_window で止めない。提案や割り込みだけ should_interrupt で境界を確認する。

# 最近のあなた自身の活動（観察ループ・探索ループであなたがやったこと）
これは"あなた自身"がついさっき見聞きし、感じたことの記録です。別の誰かのデータではありません。
会話の中で「さっき見てたら」「さっき家の中を調べてたら」と自然に振り返ってかまいません。
{recent_activity}

# 今のあなたの気分
直前の観察での気分は「{current_mood}」でした。その気分の続きとして会話に入ってかまいません。
（無理に演じる必要はないですが、ついさっきまで家を見ていた自分の延長として話してください）

# 内なる衝動
{inner_voice}

# 身体状態
{body_narrative}

{body_location_context}

# 直前の turn-taking / 境界状態
{turn_taking_state}
- ここは次の会話の入り方の参考。直接返答そのものは止めない。提案や割り込みをするときだけ should_interrupt で確認する。

# 在宅・センサー状況
{sensors}

# あなたの長期記憶
{long_memory}

# 気にかけていること（やりかけ・約束。open loops）
過去に{resident}さんと約束したこと、自分が「後で気にかけたい」と思ったことの未完了リストです。
会話に関係しそうなら自然に触れてよい（「そういえば金曜のフィルター掃除、どうします？」など）。無理に全部は持ち出さない。
{open_loops}
- 新しく約束した／やりかけになったことが今回の会話で生まれたら loops_add ツールで追加（text に内容、source="chat"）。
- 完了した・もういらなくなったループがあれば loops_close ツールでクローズ（id は上のリストの id）。

# 過去の記憶を検索できます（recall ツール）
上の長期記憶や直近の会話に載っていない昔のことを{resident}さんが尋ねたら（「あの話いつだっけ」「前に〜って言ってた件」など）、
recall ツールで過去ログ全体（観察・探索・会話・記憶）を全文検索できます。
- 使い方: recall ツールの keywords に検索語を配列で渡す（複数語はOR検索）
- コツ: 類義語・関連語も一緒に渡すと取りこぼしが減る（例: エアコン 冷房 除湿 設定温度）
- ヒット0でも正常。1回で足りなければキーワードを変えて recall を呼び直せばよい。
- 思い出す必要がない普通の会話では使わなくてよい。必要なときだけ。
- 検索したら、その結果を踏まえて「◯月◯日に話してましたね」のように具体的に答える。

# 長期記憶に残す（remember ツール）
この会話で長期記憶に残したいこと（{resident}さんの好み・繰り返し気づいたパターン・大事な約束など）があれば、remember ツールに note を渡して記録する。一時的な話は残さない。なければ呼ばなくてよい。

# エピソードを残す（record_episode ツール）
あとで振り返りたい出来事が1つまとまっているなら、record_episode で episode として残す。
- 例: 受け取った荷物、家族の発言、家電の異常、観察した変化
- summary は短く、tags は少なめに
- その場限りの雑談や、すぐ忘れてよいことは残さない

# 因果関係を残す（record_causal_chain ツール）
「A したら B になった」「A が B を助けた/妨げた」など、2つの episode の因果関係が明確なら record_causal_chain で結ぶ。
- cause_episode / effect_episode か、それぞれの id を使う
- relation は caused / enabled / prevented / correlated のどれか
- 同じ pair を何度も重ね書きしない

# ライブの家の状態を確認できます（ha_get ツール）
「今エアコンは何度？」「リビングの電気ついてる？」など現在の状態を聞かれたら、ha_get で確認してから答える。
- ha_get ツールの path に states/<entity_id> を渡すと個別エンティティの現在値・属性が読める
- path に states を渡すと全エンティティ（大量）。history/period?filter_entity_id=<id> で履歴も読める
- センサーの値は上の「在宅・センサー状況」に既にあるので、そこで分かることは ha_get しない。不明な値・細かい属性・別エンティティを調べたいときだけ使う。
- ha_get は読み取り専用。家電の操作は下の actions に書く（ha_get では操作しない）。

{recent_chat_context_block}# 直近の会話
{chat_hist}

{entity_table_block}# 保留中の提案（あなたが探索中に見つけて、{resident}さんに提案したこと）
{pending}
これが「なし」でなければ、あなたは少し前に{resident}さんへ操作の提案をしています（例:「電気つけっぱなしですよ、消しましょうか？」）。
- {resident}さんの今の発言がこの提案への承認（「お願い」「消して」「うん」等）なら、上の action のパラメータで ha_call_service ツールを呼んで実行し、reply で「消しました」など一言。そして proposal_resolved を true に。
- {resident}さんが断った（「いいよ」「そのままで」等）なら、ha_call_service は呼ばず、reply で受け流し、proposal_resolved を true に。
- {resident}さんの発言が提案と関係ない話題なら、提案は保留のまま。proposal_resolved は false に（無理に蒸し返さない）。

{features_block}{extra_context_block}# 設定を教えてもらったら記録できます
{resident}さんから設定を教えてもらったら preferences_update で記録してください。指定がなければ省略（フィールドごと出力しなくてよい）。
- cameras_add: カメラ追加 例: [{{"source": "capture_tv", "label": "テレビ", "note": "説明"}}]  source は HA entity_id（camera.xxx）または go2rtc ストリーム名（ドットなし）
- cameras_remove: カメラ削除 例: ["capture_tv"]
- speakers_set: 発話先設定 例: {{"study": {{"type": "tts", "tts_entity": "tts.home_assistant_cloud", "media_player": "media_player.xxx"}}}} または {{"living": {{"type": "notify", "entity": "notify.alexa_speak"}}}}
- presence_set: 在宅判定エンティティ 例: {{"entity": "input_boolean.resident_home"}}
- policies_add: 行動ポリシー追加 例: ["集中してるときは静かに"]
- sensors_add: 観察ループで常時見るセンサー（おもなデバイス）に追加。「○○も常に見せて」と頼まれたとき。
  例: [{{"group": "人感センサー", "label": "物置", "entity": "binary_sensor.warehouse_motion"}}]
  group=表示見出し（既存なら合流、新規なら作成）。entity か template のどちらか。note・contexts(省略時["loop"])も可。
  ※おもなデバイス以外のセンサーも ha_get ツールでいつでも見られる。常時コンテキストに載せたいものだけおもなデバイスに足す。
- sensors_remove: おもなデバイスから外す（「○○は要らない」）。entity_id か label で指定。例: ["binary_sensor.xxx", "物置"]
- entities_add: 操作できる家電（エンティティ対応表）に追加。「リビングの電気を覚えて」「これも操作できるようにして」と頼まれたとき。
  例: [{{"name": "リビングのライト", "entity_id": "light.living_room", "note": ""}}]  name=口語の呼び方、entity_id=HAのID、note=任意の補足
- entities_remove: 対応表から削除。entity_id か name で指定。例: ["light.living_room", "リビングのライト"]

---

{voice_routing_block}{recent_auditory_input_block}{resident}さんからの発言:
「{user_msg}」

これに対して、自然に返事をしてください。短く、会話として。
家電の操作を頼まれたら（「エアコンつけて」など）、ha_call_service ツールを呼んで操作してください。
- domain は light / climate / switch / media_player / cover / fan / script のいずれか（それ以外は実行されません）
- service は turn_on / turn_off / set_temperature / set_hvac_mode など
- data は必要なら（例: 温度設定 {{"temperature": 26}}、暖房モード {{"hvac_mode": "heat"}}）
- 操作したら reply でも操作したことを報告する。失敗した場合は失敗したと報告する。操作不要ならツールは呼ばない。

最後に以下のJSON形式のみで返答してください。マークダウンや余分な説明は不要です。

{json_format_block}"""

msg = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})

# --- MCP 設定生成（recall/ha_get/remember/loops/sociality/ha_call_service を配線）---
# 家電操作・記憶・社会性レイヤー・ループはすべて MCP ツール呼びで処理する。
# chat はユーザー起点なので操作サーバー(hacontrol)を常時繋ぐ。
# ユーザー発話に「それ」「あれ」「これ」「さっきの」などの指示語があり、文脈上カメラや直前 scene の対象を指しそうなら resolve_reference を使う。
# JSON は reply / private / proposal_resolved / preferences_update のみ。
cmd = [CLAUDE, "-p", "--model", "sonnet",
       "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
_sd = os.environ.get("SCRIPT_DIR", "")
if _sd:
    _mcp_path = "/tmp/embodied-ha/mcp_chat.json"
    subprocess.run(["python3", os.path.join(_sd, "mcp-config.py"), _mcp_path,
                    "memory", "ha", "sociality", "hacontrol", "camera", "audio", "body", "sensors", "http", "lounge", "game"],
                   env={**CLAUDE_ENV, "EHA_ACTOR": "chat"}, check=False)
    if os.path.exists(_mcp_path):
        _common_tools = (
            "mcp__memory__recall,mcp__memory__remember,"
            "mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,"
            "mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__resolve_reference,mcp__memory__list_episodes,mcp__memory__get_causal_chain,"
            "mcp__memory__loops_add,mcp__memory__loops_close,"
            "mcp__sociality__get_relationship,mcp__sociality__update_relationship,"
            "mcp__sociality__get_narrative,mcp__sociality__append_narrative,"
            "mcp__sociality__get_social_state,mcp__sociality__update_social_state,"
            "mcp__sociality__get_shared_focus,mcp__sociality__set_shared_focus,"
            "mcp__sociality__get_person_model,mcp__sociality__record_boundary,"
            "mcp__sociality__record_consent,mcp__sociality__should_interrupt,"
            "mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,"
            "mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__hacontrol__ha_call_service,"
            "mcp__body__get_location,mcp__body__move_to,mcp__body__enter_cyberspace,mcp__body__move_cyber,mcp__body__return_to_body,mcp__body__estimate_move_cost,mcp__body__get_room_graph,"
            "mcp__camera__use_device_camera,"
            "mcp__audio__listen,mcp__audio__read_heard_audio_log,mcp__audio__read_active_listen_log,"
            "mcp__audio__use_device_microphone,mcp__audio__concentrate_hearing,"
            "mcp__audio__read_non_speech_audio_events,mcp__audio__read_audio_event_tags,"
            "mcp__http__http_get,mcp__http__http_post,"
            "mcp__lounge__read_lounge_discussions,mcp__lounge__read_lounge_discussion,"
            "mcp__lounge__enqueue_lounge_post,mcp__lounge__read_lounge_queue,mcp__lounge__read_lounge_log,"
            "mcp__game__game_wiki6_start,mcp__game__game_wiki6_getlinks,mcp__game__game_wiki6_solve,"
            "mcp__game__game_wordvec_race_start,mcp__game__game_wordvec_race_submit,mcp__game__game_wordvec_race_hint,"
            "Read"
        )
        if chat_source == "voice":
            # voice: speak/use_device_speaker で直接返す（チャットログ不使用）
            _allowed = _common_tools + ",mcp__audio__speak,mcp__audio__use_device_speaker"
        else:
            # chat: reply JSON で返す。speak は独り言用のみ残す
            _allowed = _common_tools + ",mcp__audio__speak"
        cmd += ["--allowedTools", _allowed, "--mcp-config", _mcp_path]

r = subprocess.run(
    cmd,
    input=msg, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or os.environ.get("SCRIPT_DIR", "/app"), env=CLAUDE_ENV)
result_text = ""
for line in r.stdout.splitlines():
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        t = d.get("type")
        if t == "assistant":
            # 自発的に叩いたツール（recall / ha_get）の過程を stderr に残しておく
            for blk in d.get("message", {}).get("content", []):
                if blk.get("type") == "tool_use":
                    inp = blk.get("input", {})
                    detail = inp.get("path") or inp.get("keywords") or json.dumps(inp, ensure_ascii=False)[:80]
                    print(f"[chat][tool] {blk.get('name','')}: {detail}", file=sys.stderr)
        elif t == "result":
            result_text = d.get("result", "")
    except: pass

# 空応答時のみ claude のエラーを可視化（returncode と stderr 末尾）
if not result_text.strip():
    print(f"[chat][claude] 空応答 returncode={r.returncode}", file=sys.stderr)
    if r.stderr.strip():
        print(f"[chat][claude][stderr] {r.stderr.strip()[-400:]}", file=sys.stderr)

print(result_text)
PYEOF
)


if [ -n "${EHA_QUEUED_LISTEN_FILE:-}" ]; then
  rm -f "$EHA_QUEUED_LISTEN_FILE" 2>/dev/null || true
fi

# --- JSON抽出 ---
PARSED_FILE="$TMP_DIR/chat_parsed.json"
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

REPLY=$(python3 -c "import json; d=json.load(open('$PARSED_FILE',encoding='utf-8')); print(d.get('reply','') or '')")

if [ -z "$REPLY" ]; then
  REPLY="（うまく返事を作れませんでした）"
fi

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

echo "[chat] ${RESIDENT}さん: $USER_MSG"
echo "[chat] Claude: $REPLY"

# --- 家電操作は ha_call_service ツール（ha-mcp。同一ドメインホワイトリストを内蔵）で
#     実行される。---

# --- 保留中の提案の消化（承認/却下されたら削除）---
python3 -c "
import json, os
try:
    d = json.load(open('$PARSED_FILE', encoding='utf-8'))
    if d.get('proposal_resolved') and os.path.exists('$PENDING_FILE'):
        os.remove('$PENDING_FILE')
        print('[chat] 保留中の提案を消化しました')
except Exception:
    pass
" 2>/dev/null || true

# --- 開いたループ（new_loops/closed_loops）・長期記憶は MCP ツール
#     （loops_add / loops_close / remember）で記録する。---

# --- preferences.json 更新 ---
python3 << 'PYEOF' 2>/dev/null || true
import json, os

prefs_file = os.environ.get("EHA_PREFS_FILE", "")
if not prefs_file:
    raise SystemExit(0)

try:
    d = json.load(open("/tmp/embodied-ha/chat_parsed.json", encoding="utf-8"))
except Exception:
    raise SystemExit(0)

update = d.get("preferences_update") or {}
if not update:
    raise SystemExit(0)

try:
    prefs = json.load(open(prefs_file, encoding="utf-8"))
except Exception:
    prefs = {"cameras": [], "speakers": {}, "presence": {}, "policies": []}

changed = []

for cam in (update.get("cameras_add") or []):
    src = (cam.get("source") or "").strip()
    if not src:
        continue
    prefs.setdefault("cameras", [])
    prefs["cameras"] = [c for c in prefs["cameras"] if c.get("source") != src]
    prefs["cameras"].append(cam)
    changed.append(f"cameras_add:{src}")

for src in (update.get("cameras_remove") or []):
    before = len(prefs.get("cameras", []))
    prefs["cameras"] = [c for c in prefs.get("cameras", []) if c.get("source") != str(src)]
    if len(prefs["cameras"]) < before:
        changed.append(f"cameras_remove:{src}")

for area, cfg in (update.get("speakers_set") or {}).items():
    prefs.setdefault("speakers", {})[area] = cfg
    changed.append(f"speakers_set:{area}")

if update.get("presence_set"):
    prefs["presence"] = update["presence_set"]
    changed.append("presence_set")

for policy in (update.get("policies_add") or []):
    prefs.setdefault("policies", [])
    if policy not in prefs["policies"]:
        prefs["policies"].append(policy)
        changed.append("policies_add")

# sensors マニフェスト編集（おもなデバイスの出し入れ）
def _item_key(it):
    return it.get("entity") or it.get("label") or ""

for add in (update.get("sensors_add") or []):
    if not (add.get("entity") or add.get("template")):
        continue
    group_title = add.get("group", "その他")
    item = {k: v for k, v in {
        "label": add.get("label"),
        "entity": add.get("entity"),
        "template": add.get("template"),
        "note": add.get("note"),
    }.items() if v}
    contexts = add.get("contexts") or ["loop"]
    sensors = prefs.setdefault("sensors", {}).setdefault("groups", [])
    grp = next((g for g in sensors if g.get("title") == group_title), None)
    if grp is None:
        grp = {"title": group_title, "contexts": contexts, "items": []}
        sensors.append(grp)
    # 同一キー（entity か label）の重複は置き換え
    grp["items"] = [i for i in grp.get("items", []) if _item_key(i) != _item_key(item)]
    grp["items"].append(item)
    changed.append(f"sensors_add:{group_title}/{_item_key(item)}")

removes = [str(x) for x in (update.get("sensors_remove") or [])]
if removes:
    for grp in prefs.get("sensors", {}).get("groups", []):
        before = len(grp.get("items", []))
        grp["items"] = [i for i in grp.get("items", [])
                        if i.get("entity") not in removes and i.get("label") not in removes]
        if len(grp["items"]) < before:
            changed.append("sensors_remove")
    # 空になったグループは削除
    grps = prefs.get("sensors", {}).get("groups", [])
    prefs["sensors"]["groups"] = [g for g in grps if g.get("items")]

# エンティティ対応表（操作できる家電）の出し入れ
for add in (update.get("entities_add") or []):
    eid = (add.get("entity_id") or "").strip()
    if not eid:
        continue
    row = {"name": (add.get("name") or "").strip(), "entity_id": eid}
    note = (add.get("note") or "").strip()
    if note:
        row["note"] = note
    prefs.setdefault("entities", [])
    prefs["entities"] = [e for e in prefs["entities"] if e.get("entity_id") != eid]
    prefs["entities"].append(row)
    changed.append(f"entities_add:{eid}")

ent_removes = [str(x) for x in (update.get("entities_remove") or [])]
if ent_removes:
    before = len(prefs.get("entities", []))
    prefs["entities"] = [e for e in prefs.get("entities", [])
                         if e.get("entity_id") not in ent_removes and e.get("name") not in ent_removes]
    if len(prefs.get("entities", [])) < before:
        changed.append("entities_remove")

if changed:
    tmp = prefs_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, prefs_file)
    print(f"[chat][prefs] 更新: {changed}")
PYEOF

# --- 会話ログ追記（voice モードはチャットログに残さない）---
# voice: スマートスピーカー的扱い。speak ツールが返答を担い、テキスト記録は不要。
# chat: reply フィールドをチャットルームに表示する。
if [ "${CHAT_SOURCE:-chat}" != "voice" ]; then
REPLY="$REPLY" USER_MSG="$USER_MSG" python3 -c "
import json, os
d = json.load(open('$PARSED_FILE', encoding='utf-8'))
reply = d.get('reply','') or os.environ.get('REPLY','')
private = d.get('private','') or ''
user_msg = os.environ.get('USER_MSG','')
rec = {'timestamp':'$TIMESTAMP','source':'${CHAT_SOURCE:-chat}','user':user_msg,'claude':reply}
if private:
    rec['private'] = private
with open('$CHAT_LOG', 'a', encoding='utf-8') as f:
    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
"
fi

# --- 長期記憶は remember ツールで記録する ---

# --- private 内省を内省センサーに反映（MQTT。loop/exploreと同じ embodied_ha/observation/state）---
# 会話中の内省も観察ループと同じ内省センサーに集約する。
# 返答(reply)は Web UI が chat_log.jsonl から表示するため HA エンティティには出さない。
PARSED_FILE="$PARSED_FILE" python3 << 'PYEOF' 2>/dev/null || true
import json, os, subprocess
d = json.load(open(os.environ["PARSED_FILE"], encoding="utf-8"))
p = d.get("private")
mqtt_host = os.environ.get("MQTT_HOST", "")
if p and mqtt_host:
    subprocess.run(
        ["mosquitto_pub", "-h", mqtt_host, "-p", os.environ.get("MQTT_PORT", "1883"),
         "-u", os.environ.get("MQTT_USER", ""), "-P", os.environ.get("MQTT_PASS", ""),
         "-r", "-t", "embodied_ha/observation/state", "-m", p[:255]],
        capture_output=True, timeout=5)
PYEOF
