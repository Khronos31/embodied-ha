#!/usr/bin/env python3
"""提示済み機能フラグの記録。features.md の機能id（見出し末尾の [id]）を既出セットに記録/取得する。

エージェントが機能をユーザーに紹介したら、その id を記録しておき、プロンプトに
「既に伝えた機能」として渡すことで繰り返しを減らす。紹介するか/どれを/いつかは
LLM の判断に委ねる（構造的な強制はしない）。

使い方:
  feature-flags.py get          … 既出idをカンマ区切りで出力（無ければ空）
  feature-flags.py add <id>...  … idを既出セットに追加（未知idは無視しない＝そのまま記録）

ファイル: $EHA_LOG_DIR/features_presented.json （idの配列）
"""
import sys
import os
import json

LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "log"))
FLAGS_FILE = os.path.join(LOG_DIR, "features_presented.json")


def load():
    try:
        with open(FLAGS_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save(ids):
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = FLAGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False)
    os.replace(tmp, FLAGS_FILE)


def main():
    if len(sys.argv) < 2:
        return
    cmd = sys.argv[1]
    if cmd == "get":
        print(",".join(sorted(load())))
    elif cmd == "add":
        ids = load()
        added = {a.strip() for a in sys.argv[2:] if a.strip()}
        if added:
            ids |= added
            save(ids)


if __name__ == "__main__":
    main()
