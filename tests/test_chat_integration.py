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


def _fake_claude_run(cmd, **kwargs):
    """claude -p 呼び出しを模したstream-json応答を返す。"""
    class _Result:
        stdout = json.dumps({
            "type": "result",
            "result": json.dumps({"reply": "こんにちは、元気ですよ", "private": "テスト内省"}, ensure_ascii=False),
        }, ensure_ascii=False)
        stderr = ""
        returncode = 0
    return _Result()


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
    next_listen_request_file = Path(tmp) / "next_listen_request.json"  # 存在しない=予約無し

    env = {
        "CHAT_MESSAGE": "こんにちは",
        "CHAT_SOURCE": "chat",
        "RESIDENT": "ゆの",
        "EHA_LOG_DIR": str(log_dir),
        "EHA_PREFS_FILE": str(prefs_file),
        "EHA_CHARACTER_FILE": str(character_file),
        "EHA_BODY_LOCATION_FILE": str(body_location_file),
        "EHA_DATA_DIR": str(tmp),
        "EHA_NEXT_LISTEN_REQUEST_FILE": str(next_listen_request_file),
        "CLAUDE_CONFIG_DIR": str(Path(tmp) / "claude-home"),
        "CLAUDE_BIN": "claude",
        "MQTT_HOST": "",  # 空=publish無し(実MQTTブローカーに触れない)
        "INGRESS_PORT": "0",  # Web UI呼び出し先も隔離(実際のcurlは後述でモック)
    }
    env.update(overrides)
    return env, log_dir, prefs_file


class ChatRunIntegrationTests(unittest.TestCase):
    def test_full_turn_chat_mode_writes_chat_log_and_web_ui_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp)
            web_ui_calls = []

            with patch.object(chat, "_web_ui_status", side_effect=lambda status, source, port: web_ui_calls.append(status)), \
                 patch.object(chat.chat_invoke, "invoke_claude", side_effect=lambda cmd, msg, cwd, env: _fake_claude_run(cmd)), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""), \
                 patch.object(chat.chat_invoke, "build_claude_command", return_value=["claude", "-p"]):
                chat.run(env)

            self.assertEqual(web_ui_calls, ["thinking", "idle"])

            chat_log = log_dir / "chat_log.jsonl"
            self.assertTrue(chat_log.exists())
            record = json.loads(chat_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["claude"], "こんにちは、元気ですよ")
            self.assertEqual(record["private"], "テスト内省")
            self.assertEqual(record["user"], "こんにちは")

    def test_voice_mode_does_not_write_chat_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp, CHAT_SOURCE="voice")

            with patch.object(chat, "_web_ui_status"), \
                 patch.object(chat.chat_invoke, "invoke_claude", side_effect=lambda cmd, msg, cwd, env: _fake_claude_run(cmd)), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""), \
                 patch.object(chat.chat_invoke, "build_claude_command", return_value=["claude", "-p"]):
                chat.run(env)

            chat_log = log_dir / "chat_log.jsonl"
            self.assertFalse(chat_log.exists())

    def test_empty_message_exits_early_without_web_ui_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _log_dir, _prefs_file = _make_isolated_env(tmp, CHAT_MESSAGE="")
            with patch.object(chat, "_web_ui_status") as mock_status:
                chat.run(env)
            mock_status.assert_not_called()

    def test_preferences_update_from_response_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp)

            def fake_claude_with_prefs_update(cmd, msg, cwd, claude_env):
                class _Result:
                    stdout = json.dumps({
                        "type": "result",
                        "result": json.dumps({
                            "reply": "覚えました",
                            "preferences_update": {"policies_add": ["静かに"]},
                        }, ensure_ascii=False),
                    }, ensure_ascii=False)
                    stderr = ""
                    returncode = 0
                return _Result()

            with patch.object(chat, "_web_ui_status"), \
                 patch.object(chat.chat_invoke, "invoke_claude", side_effect=fake_claude_with_prefs_update), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""), \
                 patch.object(chat.chat_invoke, "build_claude_command", return_value=["claude", "-p"]):
                chat.run(env)

            with open(prefs_file, encoding="utf-8") as fh:
                prefs = json.load(fh)
            self.assertIn("静かに", prefs["policies"])

    def test_projected_camera_entity_injects_image_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp)
            body_location_file = Path(env["EHA_BODY_LOCATION_FILE"])
            body_location_file.write_text(json.dumps({"current_entity": "camera.living"}), encoding="utf-8")

            captured_msgs = []

            def capture_invoke_claude(cmd, msg, cwd, claude_env):
                captured_msgs.append(msg)
                return _fake_claude_run(cmd)

            with patch.object(chat, "_web_ui_status"), \
                 patch.object(chat.chat_invoke, "invoke_claude", side_effect=capture_invoke_claude), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""), \
                 patch.object(chat, "fetch_frame", return_value=b"FAKE_JPEG_BYTES"), \
                 patch.object(chat.chat_invoke, "build_claude_command", return_value=["claude", "-p"]):
                chat.run(env)

            self.assertEqual(len(captured_msgs), 1)
            content = json.loads(captured_msgs[0])["message"]["content"]
            self.assertGreater(len(content), 1)
            image_blocks = [b for b in content if b.get("type") == "image"]
            self.assertEqual(len(image_blocks), 1)
            self.assertEqual(image_blocks[0]["source"]["data"], __import__("base64").b64encode(b"FAKE_JPEG_BYTES").decode("ascii"))
            # 画像ブロックはユーザープロンプト本文(最後のtextブロック)より前に来る
            self.assertEqual(content[-1]["type"], "text")

    def test_camera_fetch_failure_does_not_crash_and_omits_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log_dir, prefs_file = _make_isolated_env(tmp)
            body_location_file = Path(env["EHA_BODY_LOCATION_FILE"])
            body_location_file.write_text(json.dumps({"current_entity": "camera.living"}), encoding="utf-8")

            with patch.object(chat, "_web_ui_status"), \
                 patch.object(chat.chat_invoke, "invoke_claude", side_effect=lambda cmd, msg, cwd, env: _fake_claude_run(cmd)), \
                 patch.object(chat, "_build_long_memory", return_value="なし"), \
                 patch.object(chat, "_build_recent_chat_context", return_value=""), \
                 patch.object(chat, "_build_open_loops", return_value="なし"), \
                 patch.object(chat, "_build_sensors", return_value=""), \
                 patch.object(chat, "_build_body_location_context", return_value=""), \
                 patch.object(chat, "_build_features_presented", return_value=""), \
                 patch.object(chat, "fetch_frame", side_effect=RuntimeError("network down")), \
                 patch.object(chat.chat_invoke, "build_claude_command", return_value=["claude", "-p"]):
                chat.run(env)  # 例外を投げずに完走することの確認

            chat_log = log_dir / "chat_log.jsonl"
            self.assertTrue(chat_log.exists())


if __name__ == "__main__":
    unittest.main()
