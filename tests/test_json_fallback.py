"""JSON抽出フォールバック・防御的unwrapのロジック検証。

chat.sh / loop.sh の JSON 抽出処理は bash に埋め込まれた python3 -c ワンライナー
であり、python モジュールとして import できない。このテストは両スクリプトの
抽出ロジックと **意図的に同一のコード** を保持し、以下を検証する:

- 抽出失敗時のフォールバック挙動（chat.sh: reply へ生テキスト格納 /
  loop.sh: private のみへ生テキスト格納・speak には流さない）
- stream-json result イベントの structured_output を result 文字列より優先する処理
- 二重包み（フィールドの値が同じキーを持つJSON文字列になっている）を
  最大3段まで再帰的に剥がす防御的unwrap

chat.sh / loop.sh 側のロジックを変更した場合は、このファイルの関数も同期して
更新すること。
"""
import json
import re
import unittest


def unwrap(value, key, max_depth=3):
    """chat.sh / loop.sh 共通の防御的unwrapロジックと同一。"""
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
    """chat.sh / loop.sh / daybook_rollup.py の stream-json result 抽出処理と同一。"""
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
    """chat.sh の JSON 抽出＋フォールバック＋unwrap処理と同一ロジック。"""
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
    """loop.sh の extract_last_json_object() と同一ロジック。"""
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
    """loop.sh の抽出＋フォールバック＋unwrap処理と同一ロジック。"""
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


class StreamJsonResultTests(unittest.TestCase):
    def test_structured_output_is_preferred_over_result(self):
        event = {
            "type": "result",
            "result": "plain fallback",
            "structured_output": {"reply": "構造化された返事"},
        }
        payload = stream_result_payload(json.dumps(event, ensure_ascii=False))
        self.assertEqual(payload, '{"reply": "構造化された返事"}')
        self.assertEqual(chat_extract(payload), {"reply": "構造化された返事"})

    def test_result_string_is_used_when_structured_output_missing(self):
        event = {"type": "result", "result": '{"reply": "従来経路"}'}
        payload = stream_result_payload(json.dumps(event, ensure_ascii=False))
        self.assertEqual(payload, '{"reply": "従来経路"}')
        self.assertEqual(chat_extract(payload), {"reply": "従来経路"})

    def test_structured_output_flows_into_loop_extractor(self):
        event = {
            "type": "result",
            "result": "not json",
            "structured_output": {"speak": None, "private": "見守り中", "emotion": "calm"},
        }
        payload = stream_result_payload(json.dumps(event, ensure_ascii=False))
        result = loop_extract(payload)
        self.assertTrue(result["_parse_ok"])
        self.assertEqual(result["private"], "見守り中")


class ChatFallbackTests(unittest.TestCase):
    def test_valid_json_parses_normally(self):
        result = chat_extract('{"reply": "こんにちは"}')
        self.assertEqual(result, {"reply": "こんにちは"})

    def test_code_fenced_json_parses_normally(self):
        result = chat_extract('```json\n{"reply": "ok"}\n```')
        self.assertEqual(result, {"reply": "ok"})

    def test_plain_text_falls_back_to_reply(self):
        result = chat_extract("ツールの実行が完了しました。特に問題ありませんでした。")
        self.assertEqual(result, {"reply": "ツールの実行が完了しました。特に問題ありませんでした。"})

    def test_empty_response_stays_empty(self):
        result = chat_extract("")
        self.assertEqual(result, {})

    def test_whitespace_only_response_stays_empty(self):
        result = chat_extract("   \n  ")
        self.assertEqual(result, {})

    def test_fallback_text_is_truncated(self):
        result = chat_extract("あ" * 5000)
        self.assertEqual(len(result["reply"]), 4000)


class LoopFallbackTests(unittest.TestCase):
    def test_valid_json_parses_with_parse_ok_true(self):
        result = loop_extract('{"speak": null, "private": "考え中", "emotion": "curious"}')
        self.assertTrue(result["_parse_ok"])
        self.assertEqual(result["private"], "考え中")

    def test_plain_text_falls_back_to_private_only(self):
        text = "カメラで人影を検知したので確認しています。"
        result = loop_extract(text)
        self.assertFalse(result["_parse_ok"])
        self.assertEqual(result["private"], text)
        # speak には絶対に流さない（会話ルームへの不自然な独白を防ぐため）
        self.assertNotIn("speak", result)

    def test_malformed_json_falls_back_to_private_only(self):
        result = loop_extract('{"speak": "hi", "private": ')
        self.assertFalse(result["_parse_ok"])
        self.assertIn("private", result)
        self.assertNotIn("speak", result)

    def test_empty_response_has_no_private_but_parse_ok_false(self):
        result = loop_extract("   ")
        self.assertEqual(result, {"_parse_ok": False})

    def test_fallback_text_is_truncated(self):
        result = loop_extract("あ" * 5000)
        self.assertEqual(len(result["private"]), 4000)


class UnwrapTests(unittest.TestCase):
    def test_plain_text_untouched(self):
        self.assertEqual(unwrap("こんにちは", "reply"), "こんにちは")

    def test_single_wrap_is_unwrapped(self):
        wrapped = json.dumps({"reply": "こんにちは"})
        self.assertEqual(unwrap(wrapped, "reply"), "こんにちは")

    def test_double_wrap_is_unwrapped(self):
        wrapped = json.dumps({"reply": json.dumps({"reply": "こんにちは"})})
        self.assertEqual(unwrap(wrapped, "reply"), "こんにちは")

    def test_none_untouched(self):
        self.assertIsNone(unwrap(None, "reply"))

    def test_json_like_but_different_key_untouched(self):
        value = '{"other": "x"}'
        self.assertEqual(unwrap(value, "reply"), value)

    def test_four_levels_of_wrap_stops_at_three(self):
        wrapped = json.dumps(
            {"reply": json.dumps({"reply": json.dumps({"reply": json.dumps({"reply": "深すぎ"})})})}
        )
        result = unwrap(wrapped, "reply")
        self.assertNotEqual(result, "深すぎ")

    def test_chat_extract_unwraps_double_wrapped_reply(self):
        double_wrapped_reply = json.dumps({"reply": "こんにちは"})
        text = json.dumps({"reply": double_wrapped_reply})
        result = chat_extract(text)
        self.assertEqual(result["reply"], "こんにちは")

    def test_loop_extract_unwraps_double_wrapped_speak(self):
        double_wrapped_speak = json.dumps({"speak": "やあ"})
        text = json.dumps({"speak": double_wrapped_speak, "private": "考え中"})
        result = loop_extract(text)
        self.assertEqual(result["speak"], "やあ")


if __name__ == "__main__":
    unittest.main()
