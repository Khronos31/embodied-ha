"""JSON抽出フォールバック・防御的unwrapのロジック検証。

ロジック本体は embodied_ha/response_parse.py（chat.py移植の増分1で
response_parse.py へ昇格済み）。以前はここに loop.sh 側と意図的に同一の
コードを複製して保持していたが、response_parse.py が実体になったことで
このファイルは import して検証するだけになった。

- 抽出失敗時のフォールバック挙動（chat: reply へ生テキスト格納 /
  loop: private のみへ生テキスト格納・speak には流さない）
- stream-json result イベントの structured_output を result 文字列より優先する処理
- 二重包み（フィールドの値が同じキーを持つJSON文字列になっている）を
  最大3段まで再帰的に剥がす防御的unwrap

loop.sh 側（まだheredoc埋め込みのまま）のロジックを変更した場合は、
response_parse.py の loop_extract 系関数も同期して更新すること。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

from response_parse import (  # type: ignore  # noqa: E402
    chat_extract,
    loop_extract,
    stream_result_payload,
    unwrap,
)


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
