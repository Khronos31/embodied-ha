#!/bin/bash
set -euo pipefail
export PATH="${EHA_TOOLS_PATH:-/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin}:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
. "$SCRIPT_DIR/config.sh"
# キャラクター定義（Markdown）を読み込む。EHA_CHARACTER_FILE は config.sh / run.sh が設定。
CHARACTER="$(cat "$EHA_CHARACTER_FILE" 2>/dev/null)"; export CHARACTER
LOG_DIR="${EHA_LOG_DIR:-$SCRIPT_DIR/log}"
LOG_FILE="$LOG_DIR/observations.jsonl"
TMP_DIR="/tmp/embodied-ha"

mkdir -p "$LOG_DIR" "$TMP_DIR"

# --- Web UI ステータス通知 ---
_web_idle() { curl -sf -X POST "http://localhost:${INGRESS_PORT:-8099}/api/status" -H "Content-Type: application/json" -d '{"status":"idle","source":null}' >/dev/null 2>&1 || true; }
curl -sf -X POST "http://localhost:${INGRESS_PORT:-8099}/api/status" -H "Content-Type: application/json" -d '{"status":"thinking","source":"watch"}' >/dev/null 2>&1 || true
trap '_web_idle' EXIT

# --- 計測ヘルパー（EHA_TIMING=1 のときだけ有効。EPOCHREALTIMEでsubprocessなし）---
if [ "${EHA_TIMING:-0}" = "1" ]; then
  _t_start=${EPOCHREALTIME/[.,]/}
  _t_last=$_t_start
  _timing_log="$LOG_DIR/timing.log"
  echo "===== watch.sh $(date '+%H:%M:%S') TRIGGER=${TRIGGER_REASON:-定期実行} =====" >> "$_timing_log"
  tlog() {
    local now=${EPOCHREALTIME/[.,]/}
    printf '[TIMING] %-26s +%6dms  (累計 %7dms)\n' "$1" "$(( (now - _t_last) / 1000 ))" "$(( (now - _t_start) / 1000 ))" >> "$_timing_log"
    _t_last=$now
  }
else
  tlog() { :; }
fi

TIMESTAMP=$(date -Iseconds)
HOUR=$(date +%-H)
DAYBOOK_MARKER="$LOG_DIR/.last_daybook"
TODAY=$(date +%Y-%m-%d)

HA_TOKEN="${SUPERVISOR_TOKEN:-}"

# --- 1. センサー状態取得（preferences.json の sensors マニフェストを Template API で描画）---
SENSORS=$(python3 "$SCRIPT_DIR/render-sensors.py" --context watch 2>/dev/null || echo "（センサー取得失敗）")

tlog "1.センサー描画(render-sensors.py)"

# --- 2a. 人感センサー履歴（直近15分。HA recorder から直接取得）---
# 人感センサー履歴は HA History API（recorder）から実行時に直接組み立てる。
RECENT_MOTION=$(python3 "$SCRIPT_DIR/motion-history.py" 15 2>/dev/null || echo "なし")
[ -z "$RECENT_MOTION" ] && RECENT_MOTION="なし"

tlog "2a.人感センサー履歴"

# --- 2b. Google TV の最前面アプリは HA の androidtv 統合（media_player の source/app_name）を
#         preferences.sensors の template item としておもなデバイスに入れると SENSORS に乗る。---

# --- 2c. 開いたループ（やりかけ・約束。発話で蒸し返せる）---
OPEN_LOOPS=$(loops list 2>/dev/null || echo "なし")

# --- features.md（アドオンの機能一覧。LLMが文脈次第で自然に紹介する）---
FEATURES_MD="$(cat "$SCRIPT_DIR/features.md" 2>/dev/null || echo "")"
FEATURES_PRESENTED="$(python3 "$SCRIPT_DIR/feature-flags.py" get 2>/dev/null || echo "")"
tlog "2c.loops list"



# --- 4. 過去ログ（直近20件）---
PREV_LOG="なし"
if [ -f "$LOG_FILE" ] && [ -s "$LOG_FILE" ]; then
  PREV_LOG=$(tail -20 "$LOG_FILE" | python3 -c "
import json, sys
lines = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        ts = d.get('timestamp', '')[:16]
        obs = d.get('private', '')
        emo = d.get('emotion', '')
        lines.append(f'{ts} [{emo}] {obs}')
    except: pass
print('\n'.join(lines) if lines else 'なし')
")
fi
tlog "4.過去ログ(直近20件)"

# --- 4b. 長期記憶ファイル（2層構造: コア記憶 --- 最近の気づき）---
MEMORY_FILE="$LOG_DIR/memory.md"
# 無ければ初期化
if [ ! -f "$MEMORY_FILE" ] || [ ! -s "$MEMORY_FILE" ]; then
  printf '## コア記憶\n\n（まだ蓄積されていません）\n\n---\n\n## 最近の気づき\n\n' > "$MEMORY_FILE"
fi
LONG_MEMORY="なし"
if [ -f "$MEMORY_FILE" ] && [ -s "$MEMORY_FILE" ]; then
  # フルではなくコア記憶＋最近の気づき直近40件に絞って送る（トークン肥大防止）
  LONG_MEMORY=$(python3 "$SCRIPT_DIR/mem-context.py" "$MEMORY_FILE" 40)
fi
tlog "4b.長期記憶読み込み"

# --- 4c. 内なる衝動（経過時間ベース）---
URGES=$(LOG_FILE="$LOG_FILE" python3 << 'PYEOF'
import json, os, datetime
now = datetime.datetime.now(datetime.timezone.utc).astimezone()
log = os.environ["LOG_FILE"]
resident = os.environ.get("RESIDENT", "ユーザー")
last_obs = last_speak = last_mem = None
if os.path.exists(log):
    with open(log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                ts = d.get("timestamp")
                if not ts: continue
                last_obs = ts
                if d.get("speak"): last_speak = ts
            except: pass

def mins(ts):
    try:
        t = datetime.datetime.fromisoformat(ts)
        return int((now - t).total_seconds() / 60)
    except: return None

urges = []
m = mins(last_speak)
if m is not None and m >= 60:
    urges.append(f"{resident}さんと最後に言葉を交わしてから約{m}分。そろそろ何か声をかけたい気もする。")
m2 = mins(last_obs)
if m2 is not None and m2 >= 30:
    urges.append(f"前回ちゃんと家を見てから{m2}分経っている。久しぶりに見る感覚。")
print("\n".join(f"- {u}" for u in urges) if urges else "（特になし）")
PYEOF
)
tlog "4c.内なる衝動(経過時間)"

# --- 4d. 直近の会話（ユーザーと交わした言葉。観察を会話と地続きにする）---
CHAT_LOG="$LOG_DIR/chat_log.jsonl"
RECENT_CHAT="なし"
if [ -f "$CHAT_LOG" ] && [ -s "$CHAT_LOG" ]; then
  RECENT_CHAT=$(tail -4 "$CHAT_LOG" | python3 -c "
import json, sys, os
resident = os.environ.get('RESIDENT', 'ユーザー')
lines = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        ts = d.get('timestamp','')[:16]
        lines.append(f\"{ts} {resident}さん「{d.get('user','')}」/ 自分「{d.get('claude','')}」\")
    except: pass
print('\n'.join(lines) if lines else 'なし')
")
fi
tlog "4d.直近の会話"

# --- 4. Claude呼び出し（2フェーズ: カメラ選択 → 観察）---
RESPONSE=$(SENSORS_DATA="$SENSORS" PREV_DATA="$PREV_LOG" LONG_MEMORY="$LONG_MEMORY" URGES_DATA="$URGES" CHAT_DATA="$RECENT_CHAT" OPEN_LOOPS_DATA="$OPEN_LOOPS" HOUR="$HOUR" RECENT_MOTION_DATA="$RECENT_MOTION" CHARACTER="$CHARACTER" FEATURES_MD="$FEATURES_MD" FEATURES_PRESENTED="$FEATURES_PRESENTED" EXTRA_CONTEXT="$EXTRA_CONTEXT" SCRIPT_DIR="$SCRIPT_DIR" EHA_TIMING="${EHA_TIMING:-0}" EHA_TIMING_LOG="${_timing_log:-/dev/stderr}" python3 << 'PYEOF'
import base64, json, os, subprocess, urllib.request, time

def _ptime(label):
    if os.environ.get("EHA_TIMING") == "1":
        now = time.perf_counter()
        with open(os.environ.get("EHA_TIMING_LOG", "/dev/stderr"), "a", encoding="utf-8") as f:
            f.write(f'[TIMING-PY] {label:<22} +{(now - _ptime.last)*1000:6.0f}ms\n')
        _ptime.last = now
_ptime.last = time.perf_counter()

CLAUDE = os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")
CLAUDE_ENV = {**os.environ,
              "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
              "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")}
GO2RTC = os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984") + "/api/frame.jpeg?src"
HA_URL  = os.environ["HA_URL"]
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# --- preferences.json 読み込み → カメラ定義を動的に構築 ---
_prefs = {}
try:
    _prefs_path = os.environ.get("EHA_PREFS_FILE", "")
    if _prefs_path:
        _prefs = json.load(open(_prefs_path, encoding="utf-8"))
except Exception:
    pass

def _src_to_slug(s):
    return s.replace(".", "_").replace("/", "_")

CAMERA_FETCH = {}
CAMERA_PATHS = {}
CAMERA_LABELS = {}
for _c in _prefs.get("cameras", []):
    _src = _c.get("source", "").strip()
    if not _src:
        continue
    _slug = _src_to_slug(_src)
    _note = _c.get("note", "")
    CAMERA_LABELS[_slug] = f"{_c.get('label', _src)}（{_note}）" if _note else _c.get("label", _src)
    CAMERA_PATHS[_slug] = f"/tmp/embodied-ha/{_slug}.jpg"
    if "." in _src:
        CAMERA_FETCH[_slug] = ["curl", "-sf", "--max-time", "5",
                                "-H", f"Authorization: Bearer {HA_TOKEN}",
                                f"{HA_URL}/camera_proxy/{_src}"]
    else:
        CAMERA_FETCH[_slug] = ["curl", "-sf", "--max-time", "5", f"{GO2RTC}={_src}"]

def fetch_cameras(names):
    procs = {}
    for name in names:
        cmd = CAMERA_FETCH.get(name)
        if cmd:
            procs[name] = subprocess.Popen(cmd, stdout=open(CAMERA_PATHS[name], "wb"), stderr=subprocess.DEVNULL)
    for p in procs.values():
        p.wait()

def call_claude(content_blocks, model="sonnet", allowed_tools=None, mcp_config=None):
    import sys
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": content_blocks}})
    cmd = [CLAUDE, "-p", "--model", model, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config]
    r = subprocess.run(
        cmd,
        input=msg, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or os.environ.get("SCRIPT_DIR", "/app"), env=CLAUDE_ENV)
    result = ""
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line: continue
        try:
            d = json.loads(line)
            t = d.get("type")
            if t == "assistant":
                # 自発的に叩いたツールを stderr に残す（explore.sh と同じ可視化）
                for blk in d.get("message", {}).get("content", []):
                    if blk.get("type") == "tool_use":
                        inp = blk.get("input", {})
                        detail = inp.get("path") or inp.get("source") or inp.get("query") or json.dumps(inp, ensure_ascii=False)[:80]
                        print(f"[watch][tool] {blk.get('name','')}: {detail}", file=sys.stderr)
            elif t == "result":
                result = d.get("result", "")
        except:
            pass
    return result

def load_image(path):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except:
        return None

sensors     = os.environ.get("SENSORS_DATA", "")
prev        = os.environ.get("PREV_DATA", "なし")
trigger     = os.environ.get("TRIGGER_REASON", "定期実行")
# 定期/手動以外＝HAオートメーション由来の具体的な経緯。その状況に対応するよう促す。
trigger_note = ""
if trigger not in ("定期実行", "手動実行"):
    trigger_note = "\n（これは「何か起きたサイン」。この経緯に関係する場所をカメラ/センサーで確かめ、必要なら触れる）"
long_memory = os.environ.get("LONG_MEMORY", "なし")
urges       = os.environ.get("URGES_DATA", "（特になし）")
recent_chat = os.environ.get("CHAT_DATA", "なし")
open_loops    = os.environ.get("OPEN_LOOPS_DATA", "なし")
hour          = int(os.environ.get("HOUR", "12"))
recent_motion = os.environ.get("RECENT_MOTION_DATA", "なし")
character     = os.environ.get("CHARACTER", "")
resident      = os.environ.get("RESIDENT", "ユーザー")
features_md   = os.environ.get("FEATURES_MD", "")
features_presented = os.environ.get("FEATURES_PRESENTED", "")
extra_context = os.environ.get("EXTRA_CONTEXT", "")

# カメラリスト説明（context / phase1 共用）
if CAMERA_LABELS:
    _cam_list = "\n".join(f"- {k}: {v}" for k, v in CAMERA_LABELS.items())
else:
    _cam_list = "（カメラ未設定。preferences.json の cameras に登録してください）"

# 発話先の部屋（preferences.speakers のキー）。speak_room はこの中から選ぶ。
_speaker_rooms = "、".join(_prefs.get("speakers", {}).keys()) or "（スピーカー未設定）"

active_desires_raw = os.environ.get("ACTIVE_DESIRES", "")
active_desires = []
if active_desires_raw:
    try:
        active_desires = json.loads(active_desires_raw)
    except Exception:
        pass

inner_voice_parts = []
if urges and urges.strip() != "（特になし）":
    inner_voice_parts.append(urges.strip())
for d in active_desires:
    inner_voice_parts.append(f"- {d}")
inner_voice = "\n".join(inner_voice_parts) if inner_voice_parts else "（特になし）"

# 時間帯ルール
if 1 <= hour <= 6:
    time_rule = f"今は深夜{hour}時台。みんな寝ているかもしれない。声で話しかける(speak)のは避けて、静かに観察するだけにする。speak は基本 null。"
elif 7 <= hour <= 9:
    time_rule = f"今は朝{hour}時台。一日の始まり。"
elif 22 <= hour or hour == 0:
    time_rule = f"今は夜{hour}時台。そろそろ静かな時間。声をかけるなら控えめに。"
else:
    time_rule = f"今は{hour}時台。"


context = f"""# あなた自身について

{character}

# 今の時間帯
{time_rule}

# 利用可能なカメラ
{_cam_list}

【今回のトリガー】{trigger}{trigger_note}
【内なる衝動】
{inner_voice}
【気にかけていること（やりかけ・約束。関係する変化があれば speak で触れてよい）】
{open_loops}
【センサー状態】
{sensors}
【直近15分の人感センサー履歴（部屋の移動の流れ）】
{recent_motion}
【長期記憶】
{long_memory}
【直近の会話（{resident}さんと交わした言葉。さっき自分が話したこと）】
{recent_chat}
【過去の観察（直近20件）】
{prev}"""

if features_md.strip():
    _presented_note = (f"既に伝えた機能: {features_presented}（繰り返し紹介しなくてよい）\n"
                       if features_presented.strip() else "")
    context += f"""

【このアドオンでできること】
文脈が自然なら speak でさりげなく一つ紹介してよい。毎回しなくてよい。紹介したら下のJSONの feature_presented に見出し末尾の [id] を入れる。
{_presented_note}{features_md}"""

if extra_context.strip():
    context += f"""

【追加コンテキスト】
{extra_context.strip()}"""

# --- Phase 1: どのカメラを見るか判断 ---
# カメラ選択に必要な情報だけを渡す（長期記憶・git・観察履歴は不要）。
# フルcontextを渡すと入力が肥大しhaikuが極端に遅くなるため、ここはスリムに保つ。
_example_cams = list(CAMERA_LABELS.keys())[:2] if CAMERA_LABELS else []
_example_json = json.dumps({"cameras": _example_cams}, ensure_ascii=False)

phase1_prompt = f"""今、家のどのカメラを確認すべきか判断してください。

# 利用可能なカメラ
{_cam_list}
（不要なカメラは選ばない。カメラが設定されていない or 確認不要なら [] で返答可）

【センサー状態】
{sensors}
【直近15分の人感センサー履歴（部屋の移動の流れ）】
{recent_motion}

センサーと履歴を踏まえて、今回確認すべきカメラを選んでください。
以下のJSON形式のみで返答:
{_example_json}"""

if CAMERA_FETCH:
    phase1_result = call_claude([{"type": "text", "text": phase1_prompt}], model="haiku")
    _ptime("phase1(haiku)カメラ選択")
    try:
        import re
        m = re.search(r'\{.*?\}', phase1_result, re.DOTALL)
        selected = json.loads(m.group()).get("cameras", []) if m else []
    except:
        selected = _example_cams  # フォールバック
    fetch_cameras(selected)
    _ptime(f"カメラ取得({len(selected)}台)")
else:
    _ptime("phase1スキップ(カメラ未設定)")
    selected = []

# --- Phase 2: 選択したカメラで本観察 ---
# 自律操作 ON のときだけ家電操作を許可する文面に切り替える（OFF時はツール自体が無い）。
if os.environ.get("EHA_AUTONOMOUS") == "1":
    _action_note = (f"消し忘れ・明らかに直した方がよい状況なら、ha_call_service で自分の判断で家電を操作してよい。"
                    f"操作したら必ず speak で{resident}さんに「何を・なぜ」操作したか事後報告する（報告は必須）。")
else:
    _action_note = "家電操作はしない（見守るだけ）。"

phase2_prompt = context + f"""

選択されたカメラの画像を添付します（{', '.join(selected) if selected else 'なし'}）。

# 使えるツール
読み取り（必要なときだけ）:
- ha_get … センサー欄に無い個別エンティティの現在値・履歴を読む（気になったときだけ）
- get_sensors … 主要センサーをまとめて取り直す
- camera_get … 指定カメラのスナップショットを追加で見る（source は go2rtcストリーム名 or camera.xxx）
記録（あれば呼ぶ。下のJSONには書かない）:
- remember … 長期記憶に残したい気づき・パターンがあれば note に一文で記録する。一時的な観察は残さない
- loops_add … 「後で気にかけておきたい」こと（消し忘れ・植物の世話・{resident}さんの作業の続き等）があれば text に一言、source="watch" で追加。既に【気にかけていること】にある内容は繰り返さない
無理に使う必要はない。観察は手早く。{_action_note}

最後に以下のJSON形式のみで返答してください。マークダウンや余分な説明は不要です。

{{
  "private": "今この瞬間に浮かんだこと。誰も見てない前提で、何も考えずそのまま投稿するツイートのように。報告でもまとめでもない。20〜40文字程度。{resident}さんが見ることもできるが気にせず素のまま。",
  "emotion": "今の気分を一語で。curious / calm / happy / concerned / amused / nostalgic / bored / surprised のどれか。",
  "speak": "ユーザーへのショートメッセージ。センサートリガー時は積極的に。定期実行時は明確な変化があるときだけ。それ以外は null。",
  "speak_room": "発話先の部屋。speak が null なら null。speak が null でなければ **必ずこの中から1つ選ぶ**（null 禁止）。利用可能な部屋: {_speaker_rooms}。{resident}さんが今いる部屋（人感センサー履歴・在室から判断）を選ぶ。外出中で away があれば away。居場所が不明なら先頭の部屋を選ぶ。",
  "schedule": "スケジュールを変更したい場合のみオブジェクトで。変更不要なら null。変更可能フィールド: watch_interval(秒,300-3600), explore_interval(秒,600-7200), day_probability(%,10-100), late_probability(%,0-50), night_probability(%,0-30)。例: {{\"watch_interval\": 600}}",
  "feature_presented": "speak でアドオンの機能を紹介したなら、その機能id（features の見出し [id]）。紹介していなければ null。"
}}"""

content = []
for cam in selected:
    path = CAMERA_PATHS.get(cam)
    if path:
        b64 = load_image(path)
        if b64:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
content.append({"type": "text", "text": phase2_prompt})

# --- Phase 2 用 MCP 設定生成（観察中に能動的にHAを掘れるよう・読み取り中心）---
# 自律操作 ON のときだけ操作サーバー(hacontrol)を繋ぐ＝物理ゲート。OFF ならツール自体が無い。
_sd = os.environ.get("SCRIPT_DIR", "")
_boundary_proc = subprocess.run(
    ["python3", os.path.join(_sd, "boundary.py"), "--json",
     "--mode", "watch", "--intent", "action", "--hour", str(hour),
     "--autonomous", os.environ.get("EHA_AUTONOMOUS", "0"),
     "--prefs-file", os.environ.get("EHA_PREFS_FILE", "")],
    capture_output=True, text=True,
    env={**CLAUDE_ENV, "SENSORS_DATA": sensors, "RESIDENT": resident,
         "EHA_PREFS_FILE": os.environ.get("EHA_PREFS_FILE", "")},
)
_boundary = {}
try:
    _boundary = json.loads(_boundary_proc.stdout or "{}")
except Exception:
    pass
_autonomous = bool(_boundary.get("allowed"))
_mcp_path = None
_allowed = None
if _sd:
    _mcp_path = "/tmp/embodied-ha/mcp_watch.json"
    _servers = ["sensors", "ha", "camera", "memory"]
    if _autonomous:
        _servers.append("hacontrol")
    subprocess.run(["python3", os.path.join(_sd, "mcp-config.py"), _mcp_path] + _servers,
                   env={**CLAUDE_ENV, "EHA_ACTOR": "watch"}, check=False)
    if os.path.exists(_mcp_path):
        _allowed = ("mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__camera__camera_get,"
                    "mcp__memory__remember,mcp__memory__loops_add")
        if _autonomous:
            _allowed += ",mcp__hacontrol__ha_call_service"
    else:
        _mcp_path = None

_resp = call_claude(content, allowed_tools=_allowed, mcp_config=_mcp_path)
_ptime("phase2(sonnet)観察")
print(_resp)
PYEOF
)
tlog "5.Claude呼び出し(全体)"

# --- 5. JSON抽出・パース（tempファイル経由で確実に）---
PARSED_FILE="$TMP_DIR/parsed.json"
printf '%s' "$RESPONSE" | python3 -c "
import sys, re, json
text = sys.stdin.read()
text = re.sub(r'\`\`\`(?:json)?\s*|\`\`\`', '', text)
m = re.search(r'\{.*\}', text, re.DOTALL)
result = {}
if m:
    try:
        result = json.loads(m.group())
    except: pass
with open('$PARSED_FILE', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False)
"
tlog "5b.JSON抽出"

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
# shlex.quote でシェル安全に変換し eval で代入する。
# 万一 python が出力ゼロで死んでも set -u で落ちないよう先に空で初期化しておく。
PRIVATE=""; SPEAK=""; SPEAK_ROOM=""; EMOTION=""; SPEAK_JSON="null"; PRIVATE_JSON='""'
eval "$(python3 -c "
import json, shlex
try:
    d = json.load(open('$PARSED_FILE', encoding='utf-8'))
except Exception:
    d = {}
private = d.get('private', '') or ''
speak_v = d.get('speak')
speak   = speak_v if speak_v else ''
pairs = {
    'PRIVATE':      private,
    'SPEAK':        speak,
    'SPEAK_ROOM':   d.get('speak_room') or '',
    'EMOTION':      d.get('emotion', '') or '',
    'SPEAK_JSON':   json.dumps(speak_v, ensure_ascii=False),
    'PRIVATE_JSON': json.dumps(private, ensure_ascii=False),
}
for k, v in pairs.items():
    print(f'{k}={shlex.quote(v)}')
")"
tlog "6.parsed.json一括抽出(python1回)"

# speak があるのに speak_room が空の場合、preferences.speakers の先頭キーにフォールバック
if [ -n "$SPEAK" ] && [ -z "$SPEAK_ROOM" ]; then
  SPEAK_ROOM=$(EHA_PREFS_FILE="$EHA_PREFS_FILE" python3 -c "
import json, os
try:
    prefs = json.load(open(os.environ['EHA_PREFS_FILE'], encoding='utf-8'))
    keys = list(prefs.get('speakers', {}).keys())
    print(keys[0] if keys else '')
except: print('')
" 2>/dev/null)
  [ -n "$SPEAK_ROOM" ] && echo "[watch] speak_room fallback: $SPEAK_ROOM"
fi

# --- 6. ログ追記 ---
echo "{\"timestamp\":\"$TIMESTAMP\",\"emotion\":\"$EMOTION\",\"private\":$PRIVATE_JSON,\"speak\":$SPEAK_JSON}" >> "$LOG_FILE"
echo "[$TIMESTAMP] $PRIVATE"

# --- 6b. 長期記憶・開いたループは MCP ツール（remember / loops_add）で記録する。---

# --- 6b3. スケジュール自己更新 ---
python3 -c "
import json, os, sys
try:
    d = json.load(open('$PARSED_FILE', encoding='utf-8'))
    sched = d.get('schedule')
    if not sched or not isinstance(sched, dict):
        raise SystemExit(0)
    sched_file = '$SCRIPT_DIR/schedule.json'
    try:
        current = json.load(open(sched_file, encoding='utf-8'))
    except Exception:
        current = {}
    limits = {
        'watch_interval':   (300, 3600),
        'explore_interval': (600, 7200),
        'day_probability':  (10, 100),
        'late_probability': (0, 50),
        'night_probability':(0, 30),
    }
    updated = {}
    for k, v in sched.items():
        if k in limits and isinstance(v, (int, float)):
            lo, hi = limits[k]
            current[k] = max(lo, min(hi, int(v)))
            updated[k] = current[k]
    if updated:
        # アトミック書き込み（daemonが毎ループ読むため、中途半端な状態を見せない）
        tmp = sched_file + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        os.replace(tmp, sched_file)
        print(f'[watch][schedule] 更新: {updated}')
except SystemExit:
    pass
except Exception as e:
    print(f'[watch][schedule] エラー: {e}')
" 2>/dev/null || true

# --- 7. エンティティ更新（MQTT優先、なければ input_text REST フォールバック）---
HA_URL="$HA_URL" HA_TOKEN="$HA_TOKEN" PARSED_FILE="$PARSED_FILE" python3 << 'PYEOF'
import json, subprocess, os

d       = json.load(open(os.environ["PARSED_FILE"], encoding="utf-8"))
obs     = d.get("private", "") or ""
speak   = d.get("speak") or ""
emotion = d.get("emotion", "") or ""

mqtt_host = os.environ.get("MQTT_HOST", "")
mqtt_port = os.environ.get("MQTT_PORT", "1883")
mqtt_user = os.environ.get("MQTT_USER", "")
mqtt_pass = os.environ.get("MQTT_PASS", "")

if mqtt_host:
    def mqtt_pub(topic, payload):
        # -r（retain）: 最後の値をブローカーに残し、HA/ブローカー再起動後も
        # エンティティが unknown に戻らず即座に最新値を表示できるようにする。
        subprocess.run(
            ["mosquitto_pub", "-h", mqtt_host, "-p", mqtt_port,
             "-u", mqtt_user, "-P", mqtt_pass, "-r", "-t", topic, "-m", payload],
            capture_output=True, timeout=5
        )
    mqtt_pub("embodied_ha/observation/state", obs[:255])
    mqtt_pub("embodied_ha/last_speak/state",  speak[:255] if speak else "（なし）")
    mqtt_pub("embodied_ha/emotion/state",     emotion)
PYEOF

# --- 8. TTS発火（部屋別ルーティング）---

_speak_boundary_json=$(SENSORS_DATA="$SENSORS" RESIDENT="$RESIDENT"   python3 "$SCRIPT_DIR/boundary.py" --json     --mode watch --intent speak --hour "$HOUR"     --autonomous "${EHA_AUTONOMOUS:-0}" --prefs-file "$EHA_PREFS_FILE"     --metadata-json "$(python3 -c "import json, os; print(json.dumps({'room': os.environ.get('SPEAK_ROOM', '')}, ensure_ascii=False))")"   2>/dev/null || printf '%s' '{"allowed":false,"reason":"boundary失敗","fallback":null}')
_speak_allowed=$(printf '%s' "$_speak_boundary_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['allowed'])" 2>/dev/null || echo "False")
if [ "$_speak_allowed" != "True" ]; then
  SPEAK=""
fi

if [ -n "$SPEAK" ]; then
  echo "[SPEAK:$SPEAK_ROOM] $SPEAK"
  python3 "$SCRIPT_DIR/speak.py" "$SPEAK_ROOM" "$SPEAK" || true
  python3 -c "
import json, sys
with open('$CHAT_LOG', 'a', encoding='utf-8') as f:
    f.write(json.dumps({'timestamp':'$TIMESTAMP','source':'watch','claude':sys.argv[1],'user':None}, ensure_ascii=False) + '\n')
" "$SPEAK" 2>/dev/null || true
fi
tlog "7.後処理(記憶/loop/entity/TTS)"

# --- 9. デイリーサマリー（日付が変わった最初の実行で前日分を要約）---
LAST_DAYBOOK=""
[ -f "$DAYBOOK_MARKER" ] && LAST_DAYBOOK=$(cat "$DAYBOOK_MARKER")

if [ "$LAST_DAYBOOK" != "$TODAY" ] && [ -f "$LOG_FILE" ] && [ -s "$LOG_FILE" ]; then
  echo "[DAYBOOK] 前日分を要約中..."
  LOG_FILE="$LOG_FILE" MEMORY_FILE="$MEMORY_FILE" TODAY="$TODAY" DAYBOOK_MARKER="$DAYBOOK_MARKER" LAST_DAYBOOK="$LAST_DAYBOOK" python3 << 'PYEOF'
import json, os, datetime, subprocess

log = os.environ["LOG_FILE"]
resident = os.environ.get("RESIDENT", "ユーザー")
character = os.environ.get("CHARACTER", "")
memory_file = os.environ["MEMORY_FILE"]
today = os.environ["TODAY"]
daybook_marker = os.environ["DAYBOOK_MARKER"]
last_daybook = os.environ.get("LAST_DAYBOOK", "").strip()

today_d = datetime.date.fromisoformat(today)
yesterday_d = today_d - datetime.timedelta(days=1)

# 要約対象: 前回処理日の翌日 〜 昨日（複数日ダウンしても中間日を取りこぼさない）
if last_daybook:
    try:
        start_d = datetime.date.fromisoformat(last_daybook) + datetime.timedelta(days=1)
    except Exception:
        start_d = yesterday_d
else:
    start_d = yesterday_d
if start_d > yesterday_d:
    raise SystemExit(0)  # 当日内の再実行など、対象なし

# 長期ダウン時の暴発防止（最大7日。古い分はスキップ）
MAX_DAYS = 7
span = (yesterday_d - start_d).days + 1
if span > MAX_DAYS:
    print(f"[DAYBOOK] {span - MAX_DAYS}日分が古すぎるためスキップ")
    start_d = yesterday_d - datetime.timedelta(days=MAX_DAYS - 1)

target_dates = []
dd = start_d
while dd <= yesterday_d:
    target_dates.append(dd.isoformat())
    dd += datetime.timedelta(days=1)

# 観察ログを日付ごとに1パスで集める
by_date = {d: [] for d in target_dates}
with open(log, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try:
            d = json.loads(line)
            ts = d.get("timestamp", "")
            day = ts[:10]
            if day in by_date:
                emo = d.get("emotion", "")
                obs = d.get("private", "")
                spk = d.get("speak") or ""
                by_date[day].append(f"{ts[11:16]} [{emo}] {obs}" + (f" → 発話: {spk}" if spk else ""))
        except: pass

CLAUDE = os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")
env = {**os.environ,
       "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
       "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")}

def summarize(day, entries):
    # キャラクター定義（character.md）を冒頭に注入し、日記も同じ人格・口調で書かせる。
    head = (character.strip() + "\n\n") if character.strip() else ""
    prompt = head + f"""一日の終わりに、{day}一日の観察を振り返って、日記のように短くまとめてください。

その日の観察ログ:
""" + "\n".join(entries) + f"""

3〜4文程度で、その日の{resident}さんや家の雰囲気、自分が感じたことを振り返ってください。
事実の羅列ではなく、一日を振り返る視点で。JSON等は不要、文章のみで。"""
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
    r = subprocess.run([CLAUDE, "-p", "--model", "sonnet", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
                       input=msg, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or os.environ.get("SCRIPT_DIR", "/app"), env=env)
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line: continue
        try:
            d = json.loads(line)
            if d.get("type") == "result":
                return d.get("result", "").strip()
        except: pass
    return ""

# 1回の watch 実行につき最も古い1日だけ要約する。複数日分を連続で Claude 呼び出しすると
# WATCH_TIMEOUT(600s) を超えて kill され、しかもマーカーが進まず毎回やり直しになるため。
# 最古の「要約対象がある日」を1日だけ処理する。
target_day = next((d for d in target_dates if by_date.get(d)), None)
if target_day is None:
    # 対象期間に要約すべき日が無い → 今日までマーカーを進める。
    new_marker = today
else:
    summary = summarize(target_day, by_date[target_day])
    if summary:
        compact = " ".join(summary.split())
        with open(memory_file, "a", encoding="utf-8") as f:
            f.write(f"- {target_day} | 【日記】{compact}\n")
        print(f"[DAYBOOK] {target_day} 記録完了: {compact[:40]}...")
        new_marker = target_day      # この日まで進める（残りは次サイクル）
    else:
        # 要約が空＝失敗。マーカーを進めず次サイクルで同じ日を再試行（取りこぼし防止）。
        print(f"[DAYBOOK] {target_day} 要約が空。マーカーを進めず次回再試行")
        new_marker = None

if new_marker:
    with open(daybook_marker, "w") as f:
        f.write(new_marker)
PYEOF
fi

# --- 10. ロールアップ（件数ベース。最近の気づきが上限を超えたら古い分をコア記憶へ昇格）---
# 件数キャップ方式。CAP件を超えたら古い超過分をコア記憶へ要約し、直近KEEP件を保持。
MEMORY_ROLLUP_CAP=120   # 最近の気づきがこの件数を超えたらロールアップ発火
MEMORY_ROLLUP_KEEP=60   # ロールアップ後に保持する直近件数

if [ -f "$MEMORY_FILE" ] && [ -s "$MEMORY_FILE" ]; then
  MEMORY_FILE="$MEMORY_FILE" ROLLUP_CAP="$MEMORY_ROLLUP_CAP" ROLLUP_KEEP="$MEMORY_ROLLUP_KEEP" python3 << 'PYEOF'
import json, os, subprocess

memory_file = os.environ["MEMORY_FILE"]
CAP  = int(os.environ["ROLLUP_CAP"])
KEEP = int(os.environ["ROLLUP_KEEP"])
CLAUDE = os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")

content = open(memory_file, encoding="utf-8").read()
# 境界は「## 最近の気づき」見出しで判定（--- はコア本文にも現れうる）
MARKER = "## 最近の気づき"
if MARKER not in content:
    raise SystemExit(0)
core_part, recent_part = content.split(MARKER, 1)
core_section = core_part.rstrip()
if core_section.endswith("---"):
    core_section = core_section[:-3].rstrip()
# 最近の気づきはエントリ行（"- "始まり）だけ対象にする
recent_lines = [l for l in recent_part.splitlines() if l.strip().startswith("-")]

# 上限以下なら何もしない（毎サイクルこのチェックだけ走る。安いので問題なし）
if len(recent_lines) <= CAP:
    raise SystemExit(0)

old_entries  = recent_lines[:-KEEP]   # 古い超過分 → コア記憶へ昇格
keep_entries = recent_lines[-KEEP:]   # 直近KEEP件は保持
print(f"[ROLLUP] 最近の気づき {len(recent_lines)}件 > 上限{CAP}件 → 古い{len(old_entries)}件をコア記憶へ昇格...")

prompt = f"""{core_section}

---

上記が現在のコア記憶です。以下の「古くなった気づき」の中から長期的に価値あるものを選び、コア記憶を更新してください。

## 古くなった気づき
{chr(10).join(old_entries)}

ルール:
- コア記憶は「この家の構造的な理解」。一時的な出来事は含めない
- 既存コア記憶と似た内容は統合・更新（重複させない）
- 重要でなければ捨ててよい
- 出力は `## コア記憶` の見出しから始め、内容をMarkdownで書いてください
- 前置き・後置きの説明文は書かない。区切り線（---）も使わない。コア記憶本文だけを出力する"""

env = {**os.environ,
       "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
       "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")}
msg = json.dumps({"type":"user","message":{"role":"user","content":[{"type":"text","text":prompt}]}})
r = subprocess.run(
    [CLAUDE, "-p", "--model", "sonnet",
     "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
    input=msg, capture_output=True, text=True, cwd=os.environ.get("EHA_CLAUDE_CWD") or os.environ.get("SCRIPT_DIR", "/app"), env=env)

new_core = ""
for line in r.stdout.splitlines():
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        if d.get("type") == "result":
            new_core = d.get("result", "").strip()
            break
    except: pass

# Claude応答が空なら何もしない（エントリを失わない。次サイクルで再試行）
if not new_core:
    print("[ROLLUP] Claude応答なし、スキップ（エントリは保持）")
    raise SystemExit(0)

# 出力の堅牢化: モデルが前置きや余計な区切り線(---)を出すことがある。
# --- は層の区切り専用なので、コア記憶本体だけを安全に取り出す。
idx = new_core.rfind("## コア記憶")   # 前置きがある場合、本体は最後の見出しから
if idx != -1:
    new_core = new_core[idx:]
else:
    new_core = "## コア記憶\n\n" + new_core
# コア記憶内に紛れた区切り線(---)を除去（誤分割を防ぐ）
new_core = "\n".join(l for l in new_core.splitlines() if l.strip() != "---").strip()

new_content = new_core.rstrip() + "\n\n---\n\n## 最近の気づき\n\n"
new_content += "\n".join(keep_entries) + "\n"

# アトミック書き込み（長いClaude呼び出しを挟むので、途中クラッシュでmemory.mdを壊さない）
tmp = memory_file + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(new_content)
os.replace(tmp, memory_file)
print(f"[ROLLUP] 完了。{len(old_entries)}件を昇格、{len(keep_entries)}件を保持")
PYEOF
fi
