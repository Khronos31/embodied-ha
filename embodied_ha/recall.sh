#!/bin/bash
set -uo pipefail

# 記憶の全文検索ツール（読み取り専用）。
# embodied-haの過去ログをキーワードで横断検索する。chat.sh から Claude が使う。
# 使い方: recall <キーワード1> [キーワード2] ...
#   - 複数キーワードは OR 検索（どれかにマッチした行を返す）
#   - 類義語を一緒に渡すと取りこぼしが減る（例: recall エアコン 冷房 設定温度）
#
# 検索対象: observations.jsonl（観察）/ explore.jsonl（探索）/ chat_log.jsonl（会話）/ memory.md（長期記憶）

# symlink(/config/.tools/bin/recall 等)経由でも実体ディレクトリ基準で log を引く。
# 実行時は run.sh / config.sh が EHA_LOG_DIR を設定するのでそちらが優先される。
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
LOG_DIR="${EHA_LOG_DIR:-$SCRIPT_DIR/log}"

if [ "$#" -eq 0 ]; then
  echo "使い方: recall <キーワード> [キーワード...]"
  exit 0
fi

LOG_DIR="$LOG_DIR" RESIDENT="${RESIDENT:-ユーザー}" python3 - "$@" << 'PYEOF'
import sys, json, os

log_dir = os.environ["LOG_DIR"]
resident = os.environ.get("RESIDENT", "ユーザー")
keywords = [k.lower() for k in sys.argv[1:] if k.strip()]
if not keywords:
    print("（キーワードが空です）")
    raise SystemExit(0)

def match(blob):
    b = blob.lower()
    return any(k in b for k in keywords)

hits = []

# --- jsonl形式のログ ---
jsonl_sources = [
    ("observations.jsonl",   "観察", lambda d: d.get("private", "")),
    ("explore.jsonl",        "探索", lambda d: f"{d.get('topic','')} {d.get('private','')}".strip()),
    ("chat_log.jsonl",       "会話", lambda d: f"{resident}「{d.get('user','')}」/ Claude「{d.get('claude','')}」"),
]
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
            hits.append((ts, f"{ts} [{label}] {text}"))

# --- memory.md（行単位）---
mpath = os.path.join(log_dir, "memory.md")
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
            hits.append((ts, f"[記憶] {l}"))

if not hits:
    print(f"（「{' / '.join(keywords)}」に一致する記憶は見つかりませんでした）")
    raise SystemExit(0)

# 時系列ソート（タイムスタンプ空は末尾）。多すぎる場合は新しい順に40件。
hits.sort(key=lambda h: h[0] or "0")
MAX = 40
shown = hits[-MAX:] if len(hits) > MAX else hits
if len(hits) > MAX:
    print(f"（{len(hits)}件ヒット、新しい{MAX}件を表示）")
print("\n".join(line for _, line in shown))
PYEOF
