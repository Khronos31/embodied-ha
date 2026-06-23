#!/bin/bash
set -euo pipefail
# 開いたループ（やりかけ・約束ごと）の管理。追記専用ログ log/open_loops.jsonl。
# add / close のイベントを追記するだけ（上書きしない）。読むときに集約してopen状態を出す。
#
# 使い方:
#   loops list            … open なループを人間可読で一覧
#   loops list-json       … open なループをJSON配列で
#   loops add <source> <text...>  … 新規ループを追加し、採番したidを出力
#   loops close <id> [reason]      … ループをクローズ
#
# source: chat / watch / explore のいずれか（どこで生まれたやりかけ・約束か）

# symlink(/config/.tools/bin/loops 等)経由でも実体ディレクトリ基準で log を引く。
# 実行時は run.sh / config.sh が EHA_LOG_DIR を設定するのでそちらが優先される。
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
LOG="${EHA_LOG_DIR:-$SCRIPT_DIR/log}/open_loops.jsonl"
mkdir -p "$(dirname "$LOG")"

cmd="${1:-list}"
case "$cmd" in
  list|list-json)
    if [ ! -f "$LOG" ] || [ ! -s "$LOG" ]; then
      [ "$cmd" = "list-json" ] && echo "[]" || echo "なし"
      exit 0
    fi
    FORMAT="$cmd" LOG="$LOG" python3 -c "
import json, os, sys
adds, closed = {}, set()
for line in open(os.environ['LOG'], encoding='utf-8'):
    line = line.strip()
    if not line: continue
    try: d = json.loads(line)
    except: continue
    if d.get('action') == 'add' and d.get('id'):
        adds[d['id']] = d
    elif d.get('action') == 'close' and d.get('id'):
        closed.add(d['id'])
opens = [d for i, d in adds.items() if i not in closed]
opens.sort(key=lambda d: d.get('created', ''))
if os.environ['FORMAT'] == 'list-json':
    print(json.dumps([{'id': d['id'], 'source': d.get('source',''),
                       'text': d.get('text',''), 'created': d.get('created','')}
                      for d in opens], ensure_ascii=False))
else:
    if not opens:
        print('なし'); sys.exit(0)
    for d in opens:
        print(f\"{d['id']} | {d.get('created','')[:16]} [{d.get('source','')}] {d.get('text','')}\")
"
    ;;
  add)
    src="${2:-unknown}"; text="${*:3}"   # 3番目以降が本文（shift失敗でsrcがtextになるバグ回避）
    if [ -z "$text" ]; then echo "usage: loops add <source> <text>"; exit 1; fi
    ID="ol_$(date +%s%N)"   # フルのナノ秒精度（16桁切り詰めはID衝突しうるため）
    TS=$(date -Iseconds)
    ID="$ID" TS="$TS" SRC="$src" TEXT="$text" LOG="$LOG" python3 -c "
import json, os
d = {'action':'add','id':os.environ['ID'],'created':os.environ['TS'],
     'source':os.environ['SRC'],'text':os.environ['TEXT']}
open(os.environ['LOG'],'a',encoding='utf-8').write(json.dumps(d,ensure_ascii=False)+'\n')
print(os.environ['ID'])
"
    ;;
  close)
    ID="${2:-}"; reason="${3:-}"
    if [ -z "$ID" ]; then echo "usage: loops close <id> [reason]"; exit 1; fi
    TS=$(date -Iseconds)
    ID="$ID" TS="$TS" REASON="$reason" LOG="$LOG" python3 -c "
import json, os
d = {'action':'close','id':os.environ['ID'],'closed':os.environ['TS'],
     'reason':os.environ.get('REASON','')}
open(os.environ['LOG'],'a',encoding='utf-8').write(json.dumps(d,ensure_ascii=False)+'\n')
print('closed', os.environ['ID'])
"
    ;;
  *)
    echo "usage: loops {list|list-json|add <source> <text>|close <id> [reason]}"
    exit 1
    ;;
esac
