"""chat_postprocess.py（chat.py移植 増分5）の単体テスト。

append_chat_log/publish_private_to_mqttのフォルトインジェクション観点は
tests/test_fault_injection.py に分離してある。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import chat_postprocess  # type: ignore  # noqa: E402


class RecordPresentedFeaturesTests(unittest.TestCase):
    def test_calls_feature_flags_add_with_ids(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd

        chat_postprocess.record_presented_features(
            {"feature_presented": "viewing_reservation"}, "/some/script_dir", run=fake_run
        )
        self.assertIn("viewing_reservation", captured["cmd"])
        self.assertIn("add", captured["cmd"])

    def test_list_of_ids_all_passed(self):
        captured = {}
        chat_postprocess.record_presented_features(
            {"feature_presented": ["a", "b"]}, "/x", run=lambda cmd, **k: captured.setdefault("cmd", cmd)
        )
        self.assertIn("a", captured["cmd"])
        self.assertIn("b", captured["cmd"])

    def test_null_string_and_falsy_values_filtered_out(self):
        captured = {"called": False}
        chat_postprocess.record_presented_features(
            {"feature_presented": ["null", "", None]}, "/x", run=lambda cmd, **k: captured.update(called=True)
        )
        self.assertFalse(captured["called"])

    def test_missing_key_does_not_call_run(self):
        captured = {"called": False}
        chat_postprocess.record_presented_features({}, "/x", run=lambda cmd, **k: captured.update(called=True))
        self.assertFalse(captured["called"])

    def test_run_exception_is_swallowed(self):
        def failing_run(cmd, **kwargs):
            raise RuntimeError("boom")

        # 例外を投げないことそのものが検証内容
        chat_postprocess.record_presented_features({"feature_presented": "x"}, "/x", run=failing_run)


class ConsumePendingProposalTests(unittest.TestCase):
    def test_resolved_true_removes_file_and_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending_proposal.json"
            pending.write_text("{}", encoding="utf-8")
            printed = []
            chat_postprocess.consume_pending_proposal(
                {"proposal_resolved": True}, str(pending), print_fn=printed.append
            )
            self.assertFalse(pending.exists())
            self.assertEqual(len(printed), 1)

    def test_resolved_false_keeps_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending_proposal.json"
            pending.write_text("{}", encoding="utf-8")
            chat_postprocess.consume_pending_proposal({"proposal_resolved": False}, str(pending))
            self.assertTrue(pending.exists())

    def test_resolved_true_but_file_missing_does_not_crash(self):
        chat_postprocess.consume_pending_proposal({"proposal_resolved": True}, "/no/such/pending.json")

    def test_malformed_parsed_dict_is_swallowed(self):
        # .get を持たない値を渡しても例外を投げない
        chat_postprocess.consume_pending_proposal(None, "/no/such/pending.json")


class PublishPrivateToMqttTests(unittest.TestCase):
    def test_publishes_when_private_and_host_present(self):
        captured = {}
        chat_postprocess.publish_private_to_mqtt(
            {"private": "考え事"}, "192.168.1.10", run=lambda cmd, **k: captured.setdefault("cmd", cmd)
        )
        self.assertIn("mosquitto_pub", captured["cmd"])
        self.assertIn("考え事", captured["cmd"])
        self.assertIn("192.168.1.10", captured["cmd"])

    def test_no_private_does_not_publish(self):
        captured = {"called": False}
        chat_postprocess.publish_private_to_mqtt(
            {}, "192.168.1.10", run=lambda cmd, **k: captured.update(called=True)
        )
        self.assertFalse(captured["called"])

    def test_no_mqtt_host_does_not_publish(self):
        captured = {"called": False}
        chat_postprocess.publish_private_to_mqtt(
            {"private": "考え事"}, "", run=lambda cmd, **k: captured.update(called=True)
        )
        self.assertFalse(captured["called"])

    def test_private_text_truncated_to_255_chars(self):
        captured = {}
        long_text = "あ" * 500
        chat_postprocess.publish_private_to_mqtt(
            {"private": long_text}, "192.168.1.10", run=lambda cmd, **k: captured.setdefault("cmd", cmd)
        )
        msg_index = captured["cmd"].index("-m") + 1
        self.assertEqual(len(captured["cmd"][msg_index]), 255)

    def test_run_exception_is_swallowed_even_without_original_try_except(self):
        # chat.sh側はpythonコード自体にtry/exceptが無いが、外側のbashが
        # `2>/dev/null || true` で包んでいたため観測可能な挙動としては
        # 常にクラッシュしない。ここでも同じ契約を守ることを確認する。
        def failing_run(cmd, **kwargs):
            raise RuntimeError("boom")

        chat_postprocess.publish_private_to_mqtt({"private": "x"}, "192.168.1.10", run=failing_run)


if __name__ == "__main__":
    unittest.main()
