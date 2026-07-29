#!/usr/bin/env python3
"""HA REST URL へ連結する前の入力検証。

`ha-mcp.py` と `ha-control-mcp.py` は、エージェントから受け取った文字列を
`HA_URL`（= `http://supervisor/core/api`）へそのまま連結して `curl` に渡す。
curl は送信前にパスの `..` を正規化するため、検証しないと**連結元より上へ抜けて
Supervisor API へ到達できる**（`--path-as-is` を付けない限りこの正規化は避けられない）。

- `ha_get` は `path` が自由文字列 → 読み取りの越境（他アドオンの options 等）
- `ha_call_service` は `service` が無検査 → **POST での越境**（`domain` は allowlist 済み）

どちらも「連結してよい形か」を入口で判定して閉じる。URL を組み立て直したり
正規化して黙って別のパスへ送るのではなく、**拒否して理由を返す**——
エージェントが「なぜ届かないか」を読めるようにするため。

サーバー側で percent-decode されうるので、デコード後の文字列も同じ基準で見る。
"""
from urllib.parse import unquote

# 多重エンコード（%252e 等）を追う回数。実用上2回で足りるが余裕を持たせる。
_MAX_DECODE_ROUNDS = 3

# service 片に許す文字。HA のサービス名・スクリプト名はスラッグなので
# ドットもスラッシュも本来含まない。ドットを許さないことで `..` も原理的に作れない。
_SERVICE_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_-"
)


def _decoded_variants(value: str) -> list[str]:
    """元の文字列と、percent-decode を繰り返した各段階を返す。"""
    variants = [value]
    current = value
    for _ in range(_MAX_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
        variants.append(current)
    return variants


def api_path_error(path: str) -> str:
    """`HA_URL` へ連結してよい API パスか調べる。

    駄目なら**エージェントへ返す日本語の理由**、良ければ空文字を返す。
    クエリ（`?` 以降）とフラグメント（`#` 以降）はパスではないので検証しない。
    """
    raw = (path or "").strip()
    if not raw:
        return "path が空です（例: states, states/climate.xxx, services）"
    path_part = raw.split("?", 1)[0].split("#", 1)[0]
    for candidate in _decoded_variants(path_part):
        if "\\" in candidate:
            return "path にバックスラッシュは使えません"
        if "://" in candidate:
            return "path に URL は渡せません（API パスだけを指定してください）"
        if any(segment == ".." for segment in candidate.split("/")):
            return "path に '..' は使えません（API ルートの外へは出られません）"
    return ""


def service_name_error(service: str) -> str:
    """`{domain}/{service}` の service 片として URL に載せてよいか調べる。

    駄目なら理由、良ければ空文字を返す。`domain` 側は呼び出し元が
    `ALLOWED_DOMAINS` で検査済みである前提。
    """
    raw = (service or "").strip()
    if not raw:
        return "service が空です"
    if not set(raw) <= _SERVICE_ALLOWED:
        return (
            "service に使えない文字が含まれています"
            "（英数字・アンダースコア・ハイフンのみ）"
        )
    return ""
