"""chat_context.py（chat.py移植 増分2）の単体テスト。

各関数がchat.shの元コード（heredocブロック1-7）と同じ入出力になることを
fixtureベースで検証する。
"""
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import chat_context  # type: ignore  # noqa: E402


def _write_lines(path, dicts):
    with open(path, "w", encoding="utf-8") as f:
        for d in dicts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


class RecentActivityTests(unittest.TestCase):
    def test_merges_and_sorts_both_logs_by_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = Path(tmp) / "observations.jsonl"
            exp = Path(tmp) / "explore.jsonl"
            _write_lines(obs, [
                {"timestamp": "2026-07-10T10:00:00+09:00", "emotion": "calm", "private": "観察ノート"},
            ])
            _write_lines(exp, [
                {"timestamp": "2026-07-10T11:00:00+09:00", "emotion": "curious", "topic": "探索テーマ"},
            ])
            result = chat_context.build_recent_activity(str(obs), str(exp))
            lines = result.split("\n")
            self.assertEqual(len(lines), 2)
            self.assertIn("観察", lines[0])
            self.assertIn("探索", lines[1])

    def test_missing_files_return_nashi(self):
        result = chat_context.build_recent_activity("/no/such/file.jsonl", "/no/such/other.jsonl")
        self.assertEqual(result, "なし")

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = Path(tmp) / "observations.jsonl"
            with open(obs, "w", encoding="utf-8") as f:
                f.write("not json\n")
                f.write(json.dumps({"timestamp": "2026-07-10T10:00:00", "private": "ok"}, ensure_ascii=False) + "\n")
            result = chat_context.build_recent_activity(str(obs), "/no/such/file.jsonl")
            self.assertIn("ok", result)

    def test_only_last_8_lines_considered_per_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = Path(tmp) / "observations.jsonl"
            entries = [{"timestamp": f"2026-07-10T{h:02d}:00:00", "private": f"entry{h}"} for h in range(10)]
            _write_lines(obs, entries)
            result = chat_context.build_recent_activity(str(obs), "/no/such/file.jsonl")
            self.assertNotIn("entry0", result)
            self.assertNotIn("entry1", result)
            self.assertIn("entry9", result)


class CurrentMoodTests(unittest.TestCase):
    def test_returns_last_nonempty_emotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = Path(tmp) / "observations.jsonl"
            _write_lines(obs, [
                {"emotion": "calm"},
                {"emotion": ""},
                {"emotion": "excited"},
            ])
            self.assertEqual(chat_context.build_current_mood(str(obs)), "excited")

    def test_missing_file_defaults_to_odayaka(self):
        self.assertEqual(chat_context.build_current_mood("/no/such/file.jsonl"), "おだやか")

    def test_all_empty_emotions_default_to_odayaka(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = Path(tmp) / "observations.jsonl"
            _write_lines(obs, [{"emotion": ""}, {"emotion": ""}])
            self.assertEqual(chat_context.build_current_mood(str(obs)), "おだやか")


class PendingProposalTests(unittest.TestCase):
    def test_missing_file_returns_nashi(self):
        self.assertEqual(chat_context.build_pending_proposal("/no/such/file.json"), "なし")

    def test_recent_proposal_is_returned_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pending_proposal.json"
            now = datetime.datetime.now(datetime.timezone.utc).astimezone()
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"timestamp": now.isoformat(), "proposal": "電気を消しますか？", "action": "light_off"}, fh)
            result = chat_context.build_pending_proposal(str(p))
            parsed = json.loads(result)
            self.assertEqual(parsed["提案文"], "電気を消しますか？")
            self.assertEqual(parsed["action"], "light_off")

    def test_stale_proposal_over_2h_returns_nashi(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pending_proposal.json"
            old = datetime.datetime.now(datetime.timezone.utc).astimezone() - datetime.timedelta(hours=3)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"timestamp": old.isoformat(), "proposal": "古い提案", "action": "x"}, fh)
            self.assertEqual(chat_context.build_pending_proposal(str(p)), "なし")

    def test_empty_file_returns_nashi(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pending_proposal.json"
            p.touch()
            self.assertEqual(chat_context.build_pending_proposal(str(p)), "なし")

    def test_malformed_json_returns_nashi(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pending_proposal.json"
            p.write_text("not json", encoding="utf-8")
            self.assertEqual(chat_context.build_pending_proposal(str(p)), "なし")


class EntityTableTests(unittest.TestCase):
    def test_renders_markdown_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = Path(tmp) / "preferences.json"
            with open(prefs, "w", encoding="utf-8") as fh:
                json.dump({"entities": [{"name": "リビングのライト", "entity_id": "light.living", "note": "備考"}]}, fh)
            result = chat_context.build_entity_table(str(prefs))
            self.assertIn("| 名前 | entity_id | 備考 |", result)
            self.assertIn("light.living", result)

    def test_no_entities_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = Path(tmp) / "preferences.json"
            with open(prefs, "w", encoding="utf-8") as fh:
                json.dump({"entities": []}, fh)
            self.assertEqual(chat_context.build_entity_table(str(prefs)), "")

    def test_missing_file_returns_empty_string(self):
        self.assertEqual(chat_context.build_entity_table("/no/such/preferences.json"), "")

    def test_entities_without_entity_id_are_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = Path(tmp) / "preferences.json"
            with open(prefs, "w", encoding="utf-8") as fh:
                json.dump({"entities": [{"name": "壊れた行"}]}, fh)
            self.assertEqual(chat_context.build_entity_table(str(prefs)), "")


class ChatHistoryTests(unittest.TestCase):
    def test_formats_dialogue_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "chat_log.jsonl"
            _write_lines(log, [{"user": "こんにちは", "claude": "こんにちは！"}])
            result = chat_context.build_chat_history(str(log), "ゆの")
            self.assertEqual(result, "ゆのさん: こんにちは\nClaude: こんにちは！")

    def test_missing_file_returns_nashi(self):
        self.assertEqual(chat_context.build_chat_history("/no/such/chat_log.jsonl", "ゆの"), "なし")

    def test_empty_file_returns_nashi(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "chat_log.jsonl"
            log.touch()
            self.assertEqual(chat_context.build_chat_history(str(log), "ゆの"), "なし")

    def test_only_last_10_lines_considered(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "chat_log.jsonl"
            entries = [{"user": f"msg{i}", "claude": f"reply{i}"} for i in range(15)]
            _write_lines(log, entries)
            result = chat_context.build_chat_history(str(log), "ゆの")
            self.assertNotIn("msg0", result)
            self.assertIn("msg14", result)


class TurnTakingStateTests(unittest.TestCase):
    def test_returns_valid_json_with_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = chat_context.build_turn_taking_state(tmp, "ゆの")
            parsed = json.loads(result)
            self.assertIn("turn_taking", parsed)
            self.assertIn("quiet_window", parsed)


class ProjectedCameraEntityTests(unittest.TestCase):
    def test_camera_entity_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "body_location.json"
            with open(f, "w", encoding="utf-8") as fh:
                json.dump({"current_entity": "camera.living"}, fh)
            self.assertEqual(chat_context.resolve_projected_camera_entity(str(f)), "camera.living")

    def test_non_camera_entity_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "body_location.json"
            with open(f, "w", encoding="utf-8") as fh:
                json.dump({"current_entity": "light.living"}, fh)
            self.assertEqual(chat_context.resolve_projected_camera_entity(str(f)), "")

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "does_not_exist.json"
            self.assertEqual(chat_context.resolve_projected_camera_entity(str(f)), "")

    def test_no_argument_falls_back_to_production_path_literal(self):
        # 本番ファイルには一切触れず、開こうとしたパスだけを検証する
        # （chat.shの既存フォールバック文字列と同一であることの確認）
        with patch("builtins.open", side_effect=FileNotFoundError) as mock_open:
            result = chat_context.resolve_projected_camera_entity(None)
        mock_open.assert_called_once_with("/config/embodied-ha/body_location.json", encoding="utf-8")
        self.assertEqual(result, "")


class RecentAuditoryInputTests(unittest.TestCase):
    def test_non_voice_returns_empty_without_touching_files(self):
        # chat_source != "voice" の場合、body_location/eventsファイルには一切アクセスしない
        with patch("builtins.open", side_effect=AssertionError("開いてはいけない")):
            result = chat_context.build_recent_auditory_input("chat", "こんにちは", "/no/such/prefs.json")
        self.assertEqual(result, "")

    def test_voice_with_no_events_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "auditory_events.jsonl"  # 存在しない
            bl_file = Path(tmp) / "body_location.json"
            with open(bl_file, "w", encoding="utf-8") as fh:
                json.dump({"current_entity": ""}, fh)
            with patch.dict("os.environ", {"EHA_AUDITORY_EVENTS_FILE": str(events_file)}):
                result = chat_context.build_recent_auditory_input(
                    "voice", "さっきの音は何？", None, str(bl_file)
                )
            self.assertEqual(result, "")

    def test_voice_missing_body_location_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "auditory_events.jsonl"
            missing_bl = Path(tmp) / "does_not_exist.json"
            with patch.dict("os.environ", {"EHA_AUDITORY_EVENTS_FILE": str(events_file)}):
                result = chat_context.build_recent_auditory_input(
                    "voice", "さっきの音は何？", None, str(missing_bl)
                )
            self.assertEqual(result, "")

    def test_voice_with_recent_event_includes_it_in_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "auditory_events.jsonl"
            bl_file = Path(tmp) / "body_location.json"
            with open(bl_file, "w", encoding="utf-8") as fh:
                json.dump({"current_entity": ""}, fh)
            now = datetime.datetime.now().astimezone()
            with open(events_file, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "timestamp": now.isoformat(),
                    "transcript": "ピンポンと鳴った",
                    "source": "リビング",
                }, ensure_ascii=False) + "\n")
            with patch.dict("os.environ", {"EHA_AUDITORY_EVENTS_FILE": str(events_file)}):
                result = chat_context.build_recent_auditory_input(
                    "voice", "さっきの音は何？", None, str(bl_file)
                )
            self.assertIn("ピンポン", result)


class QueuedListenContextTests(unittest.TestCase):
    def test_no_pending_request_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_file = Path(tmp) / "next_listen_request.json"  # 存在しない = 予約無し
            with patch.dict("os.environ", {"EHA_NEXT_LISTEN_REQUEST_FILE": str(request_file)}):
                result = chat_context.resolve_queued_listen_context("chat")
            self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
