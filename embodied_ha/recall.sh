#!/bin/bash
set -uo pipefail

# 記憶の全文検索ツール（読み取り専用）。
# embodied-haの過去ログをキーワードで横断検索する。chat.sh から Claude が使う。
# 使い方: recall <キーワード1> [キーワード2] ...
#   - 複数キーワードは OR 検索（どれかにマッチした行を返す）
#   - 類義語を一緒に渡すと取りこぼしが減る（例: recall エアコン 冷房 設定温度）
#
# 検索対象: daybooks / canonical episodes / conflict episodes / causal_chains + observations.jsonl（観察）/ explore.jsonl（探索）/ chat_log.jsonl（会話）/ memory.md（長期記憶）

# symlink(/config/.tools/bin/recall 等)経由でも実体ディレクトリ基準で log を引く。
# 実行時は run.sh / config.sh が EHA_LOG_DIR を設定するのでそちらが優先される。
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
if [ -n "${EHA_LOG_DIR:-}" ]; then
  LOG_DIR="$EHA_LOG_DIR"
elif [ -n "${EHA_DATA_DIR:-}" ]; then
  LOG_DIR="$EHA_DATA_DIR/log"
else
  LOG_DIR="$SCRIPT_DIR/log"
fi

if [ "$#" -eq 0 ]; then
  echo "使い方: recall <キーワード> [キーワード...]"
  exit 0
fi

SCRIPT_DIR="$SCRIPT_DIR" LOG_DIR="$LOG_DIR" RESIDENT="${RESIDENT:-ユーザー}" python3 - "$@" << 'PYEOF'
import json, os, sys

script_dir = os.environ["SCRIPT_DIR"]
sys.path.insert(0, script_dir)
import memory_state as ms  # type: ignore

log_dir = os.environ["LOG_DIR"]
resident = os.environ.get("RESIDENT", "ユーザー")
keywords = [k.lower() for k in sys.argv[1:] if k.strip()]
if not keywords:
    print("（キーワードが空です）")
    raise SystemExit(0)


def match(blob):
    return any(k in blob.lower() for k in keywords)


def flatten_text(value):
    parts: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif node is not None:
            text = str(node).strip()
            if text:
                parts.append(text)

    walk(value)
    return " ".join(parts)


def add_hit(bucket_hits, bucket, ts, line):
    bucket_hits[bucket].append((ts or "", line))


bucket_hits = {-1: [], 0: [], 1: [], 2: [], 3: [], 4: []}

# --- SQLite FTS5 index (fast semantic-ish full-text pass) ---
for hit in ms.search_fts(log_dir, keywords, limit=5):
    episode_id = hit.get("episode_id", "")
    brief = ""
    if episode_id.startswith("ep_"):
        episode = ms.load_episode(log_dir, episode_id)
        brief = ms.episode_brief(episode) if episode.get("id") else ""
    if not brief:
        text_value = hit.get("text", "")
        brief = f"- {(hit.get('timestamp') or '')[:16]} | 【FTS:{hit.get('kind') or 'memory'}】{text_value[:120]}"
    matched = " / ".join(hit.get("matched_terms") or [])
    suffix = f" | episode_id={episode_id} | score={hit.get('score', 0)} | matched_terms={matched} | source=fts5"
    add_hit(bucket_hits, -1, (hit.get("timestamp") or "")[:16], brief + suffix)

# --- structured memory: daybooks / causal chains / episodes ---
for daybook in ms.list_daybooks(log_dir, reverse=True):
    if match(flatten_text(daybook)):
        ts = (daybook.get("generated_at") or daybook.get("date") or "")[:16]
        add_hit(bucket_hits, 0, ts, ms.daybook_brief(daybook))

for chain in ms.list_causal_chains(log_dir, reverse=True):
    if match(flatten_text(chain)):
        ts = (chain.get("created_at") or chain.get("day") or "")[:16]
        add_hit(bucket_hits, 1, ts, ms.causal_chain_brief(chain))

for episode in ms.list_episodes(log_dir, status="canonical", reverse=True):
    if match(flatten_text(episode)):
        ts = (episode.get("timestamp") or episode.get("day") or "")[:16]
        add_hit(bucket_hits, 2, ts, ms.episode_brief(episode))

for episode in ms.list_episodes(log_dir, status="conflict", reverse=True):
    if match(flatten_text(episode)):
        ts = (episode.get("timestamp") or episode.get("day") or "")[:16]
        add_hit(bucket_hits, 2, ts, ms.episode_brief(episode))

# --- jsonl形式のログ ---
jsonl_sources = [
    ("observations.jsonl",   "観察", lambda d: d.get("private", "")),
    ("observations_recovered.jsonl", "観察(復元)", lambda d: d.get("private", "")),
    ("explore.jsonl",        "探索", lambda d: f"{d.get('topic','')} {d.get('private','')}".strip()),
    ("chat_log.jsonl",       "会話", lambda d: f"{resident}「{d.get('user','')}」/ Claude「{d.get('claude','')}」"),
]
raw_hits = []
for fname, label, extract in jsonl_sources:
    path = os.path.join(log_dir, fname)
    if not os.path.exists(path):
        continue
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        text = extract(d)
        # 本文＋全フィールドの「値」を検索対象に（emotion等の値でも引っかかるように）。
        # json.dumps だとキー名(timestamp/user/speak等)にも誤ヒットするため値だけを連結する。
        values_blob = " ".join(str(v) for v in d.values())
        if match(text + " " + values_blob):
            ts = (d.get("timestamp", "") or "")[:16]
            raw_hits.append((ts, f"{ts} [{label}] {text}"))
raw_hits.sort(key=lambda h: h[0] or "")
raw_hits.reverse()
for ts, line in raw_hits:
    add_hit(bucket_hits, 3, ts, line)


def format_audio_heard(d):
    timestamp = (d.get("timestamp", "") or "")[:19]
    source = str(d.get("source") or d.get("origin") or "不明").strip()
    transcript = str(d.get("transcript") or "").strip()
    speaker_hint = str(d.get("speaker_hint") or "").strip()
    prefix = f"[audio:heard] {timestamp}"
    if source:
        prefix += f" {source}"
    if speaker_hint and speaker_hint != "unknown":
        prefix += f" ({speaker_hint})"
    return f"{prefix}: {transcript}".rstrip()


def audio_heard_values(d):
    parts = [
        d.get("transcript", ""),
        d.get("source", ""),
        d.get("origin", ""),
        d.get("speaker_hint", ""),
        d.get("timestamp", ""),
    ]
    return " ".join(str(part) for part in parts if part)


def format_audio_listened(d):
    timestamp = (d.get("timestamp", "") or "")[:19]
    actor = str(d.get("actor") or "unknown").strip()
    source_label = str(d.get("source_label") or d.get("source") or "不明").strip()
    transcript = str(d.get("transcript") or d.get("error") or "").strip()
    prefix = f"[audio:listened] {timestamp} {actor} / {source_label}"
    return f"{prefix}: {transcript}".rstrip()


def audio_listened_values(d):
    parts = [
        d.get("transcript", ""),
        d.get("source_label", ""),
        d.get("source", ""),
        d.get("actor", ""),
        d.get("timestamp", ""),
        d.get("error", ""),
    ]
    return " ".join(str(part) for part in parts if part)


def format_audio_background(d):
    timestamp = (d.get("timestamp", "") or "")[:19]
    source = str(d.get("source") or d.get("origin") or "不明").strip()
    peak = d.get("peak_db")
    speech_ratio = d.get("speech_ratio")
    parts = [f"peak={peak}dB" if peak is not None else "", f"speech_ratio={speech_ratio}" if speech_ratio is not None else ""]
    suffix = ", ".join(part for part in parts if part)
    return f"[audio:background] {timestamp} {source}: 背景音あり" + (f" ({suffix})" if suffix else "")


def audio_background_values(d):
    parts = [
        d.get("source", ""),
        d.get("origin", ""),
        d.get("awareness", ""),
        d.get("kind", ""),
        d.get("timestamp", ""),
    ]
    return " ".join(str(part) for part in parts if part)


audio_log_sources = [
    ("auditory_events.jsonl", format_audio_heard, audio_heard_values),
    ("active_listen_log.jsonl", format_audio_listened, audio_listened_values),
    ("background_audio_log.jsonl", format_audio_background, audio_background_values),
]
audio_hits = []
for fname, formatter, values_for_search in audio_log_sources:
    path = os.path.join(log_dir, fname)
    if not os.path.exists(path):
        continue
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if match(values_for_search(d)):
            ts = (d.get("timestamp", "") or "")[:16]
            audio_hits.append((ts, formatter(d)))
audio_hits.sort(key=lambda h: h[0] or "")
audio_hits.reverse()
for ts, line in audio_hits:
    add_hit(bucket_hits, 3, ts, line)

# --- memory.md（行単位）---
mpath = os.path.join(log_dir, "memory.md")
memory_hits = []
if os.path.exists(mpath):
    for line in open(mpath, encoding="utf-8"):
        l = line.strip()
        if not l or l.startswith("#") or l.startswith("---"):
            continue
        if match(l):
            # 行頭の "- 2026-06-19 | " からタイムスタンプを拾えれば時系列ソートに使う
            ts = ""
            if l.startswith("- ") and "|" in l:
                head = l[2:].split("|", 1)[0].strip()
                ts = head[:16]
            memory_hits.append((ts, f"[記憶] {l}"))
memory_hits.sort(key=lambda h: h[0] or "")
memory_hits.reverse()
for ts, line in memory_hits:
    add_hit(bucket_hits, 4, ts, line)

hits = []
seen_lines = set()
for bucket in sorted(bucket_hits):
    for item in bucket_hits[bucket]:
        line = item[1]
        dedupe_key = line.split(" | score=", 1)[0]
        if dedupe_key in seen_lines:
            continue
        seen_lines.add(dedupe_key)
        hits.append(item)

if not hits:
    print(f"（「{' / '.join(keywords)}」に一致する記憶は見つかりませんでした）")
    raise SystemExit(0)

MAX = 40
shown = hits[:MAX]
if len(hits) > MAX:
    print(f"（{len(hits)}件ヒット、優先順で新しい{MAX}件を表示）")
print("\n".join(line for _, line in shown))

PYEOF
