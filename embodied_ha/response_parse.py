"""claudeのstream-json応答からのJSON抽出・防御的unwrapロジック。

chat.sh / loop.sh に埋め込まれていた python3 -c ワンライナーを、chat.py移植
（[[embodied-ha-pythonize-chat-loop-design-2026-07-09]]）に伴い、importできる
モジュールとして切り出した。ロジック自体はchat.sh/loop.shの既存heredocと
意図的に同一（tests/test_json_fallback.pyが検証する契約と一致させること）。

- 抽出失敗時のフォールバック挙動（chat: reply へ生テキスト格納 /
  loop: private のみへ生テキスト格納・speak には流さない）
- stream-json result イベントの structured_output を result 文字列より優先する処理
- 二重包み（フィールドの値が同じキーを持つJSON文字列になっている）を
  最大3段まで再帰的に剥がす防御的unwrap
"""
import json
import re


def unwrap(value, key, max_depth=3):
    """値が {"<key>": ...} 形式のJSON文字列に見えたら再帰的に剥がす（二重包み対策の保険）。"""
    depth = 0
    while isinstance(value, str) and depth < max_depth:
        s = value.strip()
        if not (s.startswith("{") and ('"' + key + '"') in s):
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


def stream_result_payload(stream):
    """claude -pのstream-json出力から最後のresultイベントのペイロードを取り出す。

    structured_output があればそちらを優先し、無ければ result 文字列を使う。
    """
    result_text = ""
    for line in stream.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "result":
            structured = d.get("structured_output")
            result_text = (
                json.dumps(structured, ensure_ascii=False)
                if structured is not None
                else d.get("result", "")
            )
    return result_text


def chat_extract(text):
    """chat.shのJSON抽出＋フォールバック＋unwrap処理と同一ロジック。"""
    stripped = re.sub(r"```(?:json)?\s*|```", "", text)
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    result = {}
    if m:
        try:
            result = json.loads(m.group())
        except Exception:
            pass
    if not result:
        fallback_text = stripped.strip()[:4000]
        if fallback_text:
            result = {"reply": fallback_text}
    if "reply" in result:
        result["reply"] = unwrap(result["reply"], "reply")
    return result


def _extract_last_json_object(value):
    """loop.shのextract_last_json_object()と同一ロジック。"""
    decoder = json.JSONDecoder()
    best = None
    for match in re.finditer(r"\{", value):
        try:
            obj, end = decoder.raw_decode(value, match.start())
        except Exception:
            continue
        if isinstance(obj, dict) and (
            best is None or end > best[0] or (end == best[0] and match.start() > best[1])
        ):
            best = (end, match.start(), obj)
    return best[2] if best else None


def loop_extract(text):
    """loop.shの抽出＋フォールバック＋unwrap処理と同一ロジック。"""
    result = _extract_last_json_object(text)
    parse_ok = isinstance(result, dict)
    if not parse_ok:
        fallback_text = text.strip()[:4000]
        result = {"private": fallback_text} if fallback_text else {}
    for k in ("speak", "private"):
        if k in result:
            result[k] = unwrap(result[k], k)
    result["_parse_ok"] = parse_ok
    return result
