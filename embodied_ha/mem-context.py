#!/usr/bin/env python3
"""memory.md を「コア記憶（全文）＋最近の気づき直近N件」に絞って出力する。

LLMへ毎回フルのmemory.mdを送るとトークンが肥大するため、送信用に整形する。
コア記憶はキュレート済みで小さいので全文、最近の気づきは直近N件だけ。

使い方: mem-context.py <memory.md path> [N=40]
"""
import sys

path = sys.argv[1] if len(sys.argv) > 1 else ""
n = int(sys.argv[2]) if len(sys.argv) > 2 else 40

try:
    content = open(path, encoding="utf-8").read()
except Exception:
    print("なし")
    sys.exit(0)

# 2層構造（コア記憶 --- 最近の気づき）。境界は「## 最近の気づき」見出しで判定する
# （--- はコア記憶本文中にも現れうるため、それで分割すると誤分割する）。
MARKER = "## 最近の気づき"
if MARKER in content:
    core, recent = content.split(MARKER, 1)
else:
    core, recent = content, ""
core = core.rstrip()
if core.endswith("---"):       # コアと最近の気づきの間の区切り線を落とす
    core = core[:-3].rstrip()

# 最近の気づきはエントリ行（"- "始まり）だけ数えて直近N件を保持
recent_entries = [ln for ln in recent.splitlines() if ln.strip().startswith("-")]
kept = recent_entries[-n:] if len(recent_entries) > n else recent_entries

out = core.rstrip()
if kept:
    omitted = len(recent_entries) - len(kept)
    note = f"（古い{omitted}件は省略。コア記憶に要約済み）\n" if omitted > 0 else ""
    out += "\n\n---\n\n## 最近の気づき\n\n" + note + "\n".join(kept)

print(out if out.strip() else "なし")
