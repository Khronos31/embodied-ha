"""chat.py（オーケストレーター本体、増分7）の統合テスト。

claude CLI・mcp-config.py・mem-context.py等の外部プロセス呼び出しは
全てモック化し、隔離した一時ディレクトリのfixtureのみを使う。本番の
/config/embodied-ha配下・/tmp/embodied-ha配下には一切書き込まない
（red-team必須修正1: 隔離環境の環境変数を完全リスト化する対応）。

観点:
- 正常系一気通貫（chatモード）で chat_log.jsonl・preferences.json が
  期待通り更新されること
- voiceモードでは chat_log.jsonl に追記されないこと
- 空メッセージなら何もせず早期終了すること（Web UIステータスも打たない）
- Web UIステータスが thinking → idle の順で必ず呼ばれること
  （早期終了時は呼ばれない = chat.shのtrap登録タイミングと同一）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EMBODIED_HA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EMBODIED_HA_DIR))

import chat  # type: ignore  # noqa: E402


def _fake_chat_response(**_kwargs):
    """invoke-agent.sh呼び出し後のchat JSON本文を返す。"""
    return json.dumps({"reply": "こんにちは、元気ですよ", "private": "テスト内省"}, ensure_ascii=False)


def _make_isolated_env(tmp, **overrides):
    log_dir = Path(tmp) / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    prefs_file = Path(tmp) / "preferences.json"
    with open(prefs_file, "w", encoding="utf-8") as fh:
        json.dump({"cameras": [], "speakers": [], "presence": {}, "policies": [], "entities": []}, fh)
    character_file = Path(tmp) / "character.md"
    character_file.write_text("私はあかね。", encoding="utf-8")
    body_location_file = Path(tmp) / "body_location.json"
    body_location_file.write_text(json.dumps({"current_entity": ""}), encoding="utf-8")
    env = {
        "CHAT_MESSAGE": "こんにちは",
        "CHAT_SOURCE": "chat",
        "RESIDENT": "ユーザー",
        "EHA_LOG_DIR": str(log_dir),
        "EHA_PREFS_FILE": str(prefs_file),
        "EHA_CHARACTER_FILE": str(character_file),
        "EHA_BODY_LOCATION_FILE": str(body_location_file),
        "EHA_DATA_DIR": str(tmp),
        "CLAUDE_CONFIG_DIR": str(Path(tmp) / "claude-home"),
        "CLAUDE_BIN": "claude",
        "MQTT_HOST": "",  # 空=publish無し(実MQTTブローカーに触れない)
        "INGRESS_PORT": "0",  # Web UI呼び出し先も隔離(実際のcurlは後述でモック)
    }
    env.update(overrides)
    return env, log_dir, prefs_file


class ChatRunIntegrationTests(unittest.TestCase):
    def test_extra_context_shell_gets_chat_kind_and_source_and_prompt_gets_output_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _log_dir, _prefs_file = _make_isolated_env(tmp, CHAT_SOURCE=" Voice ")
            Path(tmp, "extra_context.conf").write_text(
                "printf 'contract kind=%s source=%s' \"$EHA_EXTRA_CONTEXT_KIND\" \"$EHA_EXTRA_CONTEXT_SOURCE\"\n",
                encoding="utf-8",
            )
            captured_calls = []

            with patch.object(chat, "_web_ui_status"), \
                 patch.object(chat.chat_invoke, "invoke_chat_claude", side_effect=lambda **kwargs: captured_calls.append(kwargs) or _fake_chat_response()), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""):
                chat.run(env)

            self.assertEqual(len(captured_calls), 1)
            block = chat.chat_invoke.build_extra_context_block("contract kind=chat source=voice")
            self.assertEqual(captured_calls[0]["prompt"].count(block), 1)
            self.assertIn("# 声で呼ばれた", captured_calls[0]["prompt"])

    def test_full_turn_chat_mode_writes_chat_log_and_web_ui_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp)
            web_ui_calls = []

            with patch.object(chat, "_web_ui_status", side_effect=lambda status, source, port: web_ui_calls.append(status)), \
                 patch.object(chat.chat_invoke, "invoke_chat_claude", side_effect=_fake_chat_response), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""):
                chat.run(env)

            self.assertEqual(web_ui_calls, ["thinking", "idle"])

            chat_log = log_dir / "chat_log.jsonl"
            self.assertTrue(chat_log.exists())
            record = json.loads(chat_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["agent"], "こんにちは、元気ですよ")
            self.assertNotIn("claude", record)
            self.assertEqual(record["private"], "テスト内省")
            self.assertEqual(record["user"], "こんにちは")

    def test_voice_mode_does_not_write_chat_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp, CHAT_SOURCE="voice")

            with patch.object(chat, "_web_ui_status"), \
                 patch.object(chat.chat_invoke, "invoke_chat_claude", side_effect=_fake_chat_response), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""):
                chat.run(env)

            chat_log = log_dir / "chat_log.jsonl"
            self.assertFalse(chat_log.exists())
            voice_log = log_dir / "voice_introspection.jsonl"
            record = json.loads(voice_log.read_text(encoding="utf-8"))
            self.assertEqual(record["source"], "voice")
            self.assertEqual(record["private"], "テスト内省")
            self.assertNotIn("user", record)
            self.assertNotIn("agent", record)

    def test_empty_message_exits_early_without_web_ui_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _log_dir, _prefs_file = _make_isolated_env(tmp, CHAT_MESSAGE="")
            with patch.object(chat, "_web_ui_status") as mock_status:
                chat.run(env)
            mock_status.assert_not_called()

    def test_preferences_update_from_response_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp)

            def fake_chat_with_prefs_update(**_kwargs):
                return json.dumps({
                    "reply": "覚えました",
                    "preferences_update": {"policies_add": ["静かに"]},
                }, ensure_ascii=False)

            with patch.object(chat, "_web_ui_status"), \
                 patch.object(chat.chat_invoke, "invoke_chat_claude", side_effect=fake_chat_with_prefs_update), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""):
                chat.run(env)

            with open(prefs_file, encoding="utf-8") as fh:
                prefs = json.load(fh)
            self.assertIn("静かに", prefs["policies"])

    def test_projected_camera_is_not_passively_injected(self):
        for source in ("camera.living", "living_stream"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                env, _log_dir, prefs_file = _make_isolated_env(tmp)
                prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
                prefs["cameras"] = [{"source": source, "label": "リビング"}]
                prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
                Path(env["EHA_BODY_LOCATION_FILE"]).write_text(
                    json.dumps({"current_entity": source}), encoding="utf-8"
                )
                captured_calls = []

                with patch.object(chat, "_web_ui_status"), \
                     patch.object(chat.chat_invoke, "invoke_chat_claude", side_effect=lambda **kwargs: captured_calls.append(kwargs) or _fake_chat_response()), \
                     patch.object(chat, "_build_long_memory", return_value="なし"), \
                     patch.object(chat, "_build_recent_chat_context", return_value=""), \
                     patch.object(chat, "_build_open_loops", return_value="なし"), \
                     patch.object(chat, "_build_sensors", return_value=""), \
                     patch.object(chat, "_build_body_location_context", return_value=""), \
                     patch.object(chat, "_build_features_presented", return_value=""):
                    chat.run(env)

                self.assertEqual(len(captured_calls), 1)
                self.assertNotIn("prefix_blocks", captured_calls[0])
                self.assertIn("画像は自動では届きません", captured_calls[0]["prompt"])

    def test_character_file_content_flows_into_prompt(self):
        # Codexレビューで発見: eha_config.pyはEHA_CHARACTER_FILEのパスを解決するだけで
        # 内容を読んでおらず、chat.py側の読み取りが欠落していた(全会話でキャラクター
        # 定義が空文字列になる回帰)。character.mdの実内容がプロンプトに乗ることを確認する。
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp)
            character_file = Path(env["EHA_CHARACTER_FILE"])
            character_file.write_text("私はテスト用のあかね。特徴的な一文。", encoding="utf-8")

            captured_calls = []

            def capture_invoke_chat(**kwargs):
                captured_calls.append(kwargs)
                return _fake_chat_response()

            with patch.object(chat, "_web_ui_status"), \
                 patch.object(chat.chat_invoke, "invoke_chat_claude", side_effect=capture_invoke_chat), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""):
                chat.run(env)

            self.assertEqual(len(captured_calls), 1)
            prompt_text = captured_calls[0]["prompt"]
            self.assertIn("私はテスト用のあかね。特徴的な一文。", prompt_text)

if __name__ == "__main__":
    unittest.main()
