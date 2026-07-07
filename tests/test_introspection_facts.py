import json
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import introspection_facts as facts  # noqa: E402


class IntrospectionFactsTest(unittest.TestCase):
    def test_extract_facts_from_stream_json(self):
        stream = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "確認します"},
                        {"type": "tool_use", "id": "toolu_1", "name": "mcp__sensors__get_sensors"},
                        {"type": "tool_use", "id": "toolu_2", "name": "mcp__audio__speak"},
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok", "is_error": False},
                        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "spoken", "is_error": False},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "toolu_3", "name": "mcp__hacontrol__ha_call_service"},
                        {"type": "tool_use", "id": "toolu_4", "name": "mcp__camera__use_device_camera"},
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_3", "content": "done", "is_error": False},
                        {"type": "tool_result", "tool_use_id": "toolu_4", "content": "timeout", "is_error": True},
                    ]
                },
            },
            {"type": "result", "result": "{}"},
            "not json",
        ]
        text = "\n".join(json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item for item in stream)

        extracted = facts.extract_facts_from_stream_text(text)

        self.assertEqual(extracted["tool_calls"], 4)
        self.assertEqual(extracted["tool_errors"], 1)
        self.assertEqual(
            extracted["tools_used"],
            {
                "mcp__audio__speak": 1,
                "mcp__camera__use_device_camera": 1,
                "mcp__hacontrol__ha_call_service": 1,
                "mcp__sensors__get_sensors": 1,
            },
        )
        self.assertEqual(extracted["error_tools"], ["mcp__camera__use_device_camera"])
        self.assertEqual(extracted["speak_ok"], 1)
        self.assertEqual(extracted["action_ok"], 1)

    def test_ungrounded_speech_claim_requires_completed_claim_and_no_speak(self):
        self.assertTrue(
            facts.should_flag_ungrounded_speech_claim(
                private="照明のことは伝えた。",
                facts={"speak_ok": 0},
                proposal=None,
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_speech_claim(
                private="照明のことは伝えた。",
                facts={"speak_ok": 1},
                proposal=None,
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_speech_claim(
                private="照明のことは伝えた。",
                facts={"speak_ok": 0},
                proposal="照明を確認してほしい",
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_speech_claim(
                private="照明のことを伝えたい。",
                facts={"speak_ok": 0},
                proposal=None,
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_speech_claim(
                private="照明のことを伝えようと思った。",
                facts={"speak_ok": 0},
                proposal=None,
            )
        )
        # 仮定形（〜たら）・並列（〜たり）は完了クレームではない
        self.assertFalse(
            facts.should_flag_ungrounded_speech_claim(
                private="明日伝えたらいいかな。",
                facts={"speak_ok": 0},
                proposal=None,
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_speech_claim(
                private="ゆのさんに言ったらどうなるか考えた。",
                facts={"speak_ok": 0},
                proposal=None,
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_speech_claim(
                private="夜に話したりした時間を思い返した。",
                facts={"speak_ok": 0},
                proposal=None,
            )
        )
        # facts が無い（agy等でstream不明）なら判定しない
        self.assertFalse(
            facts.should_flag_ungrounded_speech_claim(
                private="照明のことは伝えた。",
                facts=None,
                proposal=None,
            )
        )

    def test_ungrounded_visual_claim_requires_visual_text_and_no_camera_grounding(self):
        self.assertTrue(
            facts.should_flag_ungrounded_visual_claim(
                private="リビングに人が見えた。",
                facts={"tools_used": {}},
                current_entity="",
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_visual_claim(
                private="リビングに人が見えた。",
                facts={"tools_used": {"mcp__camera__use_device_camera": 1}},
                current_entity="",
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_visual_claim(
                private="リビングが気になる。",
                facts={"tools_used": {}},
                current_entity="",
            )
        )
        self.assertFalse(
            facts.should_flag_ungrounded_visual_claim(
                private="視界に明かりが映っている。",
                facts={"tools_used": {}},
                current_entity="camera.living",
            )
        )



if __name__ == "__main__":
    unittest.main()
