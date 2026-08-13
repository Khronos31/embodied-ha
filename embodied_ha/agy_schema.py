#!/usr/bin/env python3
"""JSON Schema を Antigravity CLI が受け付ける形へ整える。

Antigravity の `--json-schema` は **`enum` の要素に `null` が含まれるスキーマを
モデル実行前に拒否する**。返るのは
`{"status":"ERROR","error":"Agent execution terminated due to error."}` だけで、
何が悪いかは言わない（実測: 0 トークン・0.3〜0.5 秒で即エラー）。

実測で切り分けた対応表（1.1.9 と 1.1.12 で同一。2026-08-13）:

| 構文 | 結果 |
|---|---|
| `{"type": "string"}` | 通る |
| `{"type": ["string", "null"]}` | **通る** |
| `{"anyOf": [{"type": "string"}, {"type": "null"}]}` | 通る |
| **`{"type": "string", "enum": ["x", null]}`** | **拒否** |
| `required` に載せない | 通る |

⚠️ `invoke-agent.sh` には長らく「loop schemas は **nullable type union** を使っており
1.1.9 がそれを拒否する」というコメントがあったが、上のとおり**union は通る**。
当初からの誤診で、そのせいで observe/explore/... は prompt 埋め込みのままだった
（ソラの observe が 1 日 4〜6 回 JSON にならず捨てられていた原因）。

ここでの変換は **agy へ渡す直前だけ**に効かせる。正本のスキーマ
（`json_schemas.py`）は変えない——claude は native `--json-schema` で、codex は
prompt 埋め込みで、それぞれ現状のまま通っているため。
"""
from __future__ import annotations

import json
import sys


def sanitize(node):
    """`enum` に `null` を含む部分を `anyOf` へ書き換えた新しいスキーマを返す。

    元のオブジェクトは変更しない。`enum` から `null` を抜いた本体と
    `{"type": "null"}` の 2 択にするだけなので、**受け入れる値の集合は変わらない**
    ——Antigravity が読める書き方に移すだけの変換である。
    """
    if isinstance(node, list):
        return [sanitize(item) for item in node]
    if not isinstance(node, dict):
        return node

    converted = {key: sanitize(value) for key, value in node.items()}

    enum = converted.get("enum")
    if not isinstance(enum, list) or not any(item is None for item in enum):
        return converted

    without_null = [item for item in enum if item is not None]
    # `description` や `title` は分岐の外に残す。中へ入れると UI/モデルから見た
    # 説明が anyOf の片側だけの説明になってしまう。
    outer_keys = {"description", "title", "default", "examples"}
    outer = {k: v for k, v in converted.items() if k in outer_keys}
    inner = {k: v for k, v in converted.items() if k not in outer_keys and k != "enum"}

    if without_null:
        inner["enum"] = without_null
        branches = [inner, {"type": "null"}]
    else:
        # enum が null だけだった場合。null 以外を許さない意図なので、そのまま維持する。
        branches = [{"type": "null"}]

    # `type` が union で null を含んでいたなら、分岐側で表現するので落とす。
    branch_type = inner.get("type")
    if isinstance(branch_type, list):
        remaining = [t for t in branch_type if t != "null"]
        if remaining:
            inner["type"] = remaining[0] if len(remaining) == 1 else remaining
        else:
            inner.pop("type", None)

    outer["anyOf"] = branches
    return outer


def main(argv: list[str]) -> int:
    """stdin のスキーマを整えて stdout へ出す。壊れた入力はそのまま素通しする。

    素通しにするのは、ここで落とすと呼び出し側が「スキーマ無し」ではなく
    「呼び出し失敗」になるため。整形できなければ元のままでも従来と同じ挙動になる。
    """
    raw = sys.stdin.read()
    try:
        schema = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        sys.stdout.write(raw)
        return 0
    converted = sanitize(schema)
    if converted == schema:
        # 変換が要らないなら**元の文字列をそのまま**返す。再シリアライズすると
        # 意味は同じでも字面が変わり、実地検証済みのスキーマ（daybook）が
        # 「検証したものと同じ文字列」でなくなる。直す必要のないものは触らない。
        sys.stdout.write(raw)
        return 0
    json.dump(converted, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
