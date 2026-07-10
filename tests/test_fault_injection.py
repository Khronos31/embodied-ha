"""chat.sh移植で意図的にガード無しのまま残した4箇所のフォルトインジェクションテスト。

red-team判定（[[project_embodied_ha_todo]]・
/config/.tools/claude-home/red-team/20260710-chat-py-port-plan.md）の
必須修正事項2への対応。以下4箇所は、chat.shの元コードにもエラー
ハンドリングが無く、失敗時はスクリプト全体を中断させる設計になっている
（意図的か見落としかは元コードからは判別できないが、chat.py移植では
「同じように壊れる」ことを優先し、静かに握りつぶす方向へ揃えない）:

1. chat_context.build_turn_taking_state（chat.sh:146-152）
2. chat_context.build_recent_auditory_input（chat.sh:186-213、voiceモード時のみ）
3. chat_postprocess.append_chat_log（chat.sh:857-878）
4. chat._build_long_memory（chat.sh:72-77、mem-context.py呼び出し。Codexレビューで
   発見された4箇所目。当初は誤ってfallback="なし"で握りつぶしていた）

各テストは、依存先を意図的に例外送出させ、対象関数がそれを握りつぶさず
そのまま伝播させることを確認する。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import chat  # type: ignore  # noqa: E402
import chat_context  # type: ignore  # noqa: E402
import chat_postprocess  # type: ignore  # noqa: E402


class TurnTakingStateFaultInjectionTests(unittest.TestCase):
    def test_exception_from_sociality_state_propagates(self):
        with patch.object(chat_context.ss, "get_turn_taking_state", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                chat_context.build_turn_taking_state("/some/log_dir", "ゆの")


class RecentAuditoryInputFaultInjectionTests(unittest.TestCase):
    def test_exception_from_resolve_source_filter_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            bl_file = Path(tmp) / "body_location.json"
            bl_file.write_text('{"current_entity": ""}', encoding="utf-8")
            with patch.object(chat_context, "resolve_source_filter", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    chat_context.build_recent_auditory_input("voice", "こんにちは", None, str(bl_file))

    def test_exception_from_format_recent_auditory_prompt_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            bl_file = Path(tmp) / "body_location.json"
            bl_file.write_text('{"current_entity": ""}', encoding="utf-8")
            with patch.object(chat_context, "format_recent_auditory_prompt", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    chat_context.build_recent_auditory_input("voice", "こんにちは", None, str(bl_file))

    def test_non_voice_never_reaches_the_fragile_call(self):
        # ガード無しなのはvoice分岐のみ。非voiceなら関数の先頭で早期returnし、
        # 依存先が壊れていても一切影響を受けない（chat.shのif分岐と同一）。
        with patch.object(chat_context, "resolve_source_filter", side_effect=RuntimeError("boom")):
            result = chat_context.build_recent_auditory_input("chat", "こんにちは", None, "/no/such/file.json")
        self.assertEqual(result, "")


class AppendChatLogFaultInjectionTests(unittest.TestCase):
    def test_unwritable_path_propagates_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 親ディレクトリが存在しないパス = open(..., "a") が必ず失敗する
            bad_path = Path(tmp) / "no_such_subdir" / "chat_log.jsonl"
            with self.assertRaises(OSError):
                chat_postprocess.append_chat_log(
                    {"reply": "こんにちは"}, "こんにちは", "やあ", "chat", "2026-07-10T00:00:00", str(bad_path)
                )

    def test_normal_case_still_appends_successfully(self):
        # ガード無し＝壊れやすい、だけでなく正常系がちゃんと動くことも確認しておく
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "chat_log.jsonl"
            chat_postprocess.append_chat_log(
                {"reply": "こんにちは", "private": "内緒"}, "フォールバック用reply", "やあ", "chat",
                "2026-07-10T00:00:00", str(log_file),
            )
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("こんにちは", content)
            self.assertIn("内緒", content)


class LongMemoryFaultInjectionTests(unittest.TestCase):
    def test_mem_context_failure_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_file = Path(tmp) / "memory.md"
            memory_file.write_text("何か長期記憶", encoding="utf-8")

            def fake_run(cmd, **kwargs):
                raise subprocess.CalledProcessError(1, cmd)

            with self.assertRaises(subprocess.CalledProcessError):
                chat._build_long_memory(str(memory_file), "/some/script_dir", run=fake_run)

    def test_empty_or_missing_memory_file_short_circuits_without_calling_run(self):
        # メモリファイルが無い/空のときはchat.sh同様run自体を呼ばない
        def fake_run(cmd, **kwargs):
            raise AssertionError("memory_fileが空/無いときはrunを呼んではいけない")

        result = chat._build_long_memory("/no/such/file.md", "/some/script_dir", run=fake_run)
        self.assertEqual(result, "なし")


if __name__ == "__main__":
    unittest.main()
