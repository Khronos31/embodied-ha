"""chat_invoke.py の契約テスト。"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EMBODIED_HA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EMBODIED_HA_DIR))

import chat_invoke  # type: ignore  # noqa: E402


class BuildChatPromptContractTests(unittest.TestCase):
    def _minimal_prompt(self):
        return chat_invoke.build_chat_prompt(
            character="test",
            resident="resident",
            projected_camera_source="",
            recent_activity="none",
            current_mood="calm",
            inner_voice="none",
            body_narrative="body",
            body_location_context="location",
            turn_taking_state="{}",
            sensors="sensors",
            long_memory="memory",
            open_loops="none",
            recent_chat_context="",
            chat_hist="none",
            entity_table="",
            pending="none",
            features_md="",
            features_presented="",
            extra_context="",
            policies_raw="",
            chat_source="chat",
            user_room="",
            user_room_speaker="",
            recent_auditory_input="",
            user_msg="hello",
        )

    def test_prompt_mentions_concentrate_hearing_only_for_antigravity(self):
        for harness in ("claude", "codex", "agy"):
            with self.subTest(harness=harness), patch.dict(
                os.environ,
                {"EHA_AGENT_HARNESS": harness},
                clear=False,
            ):
                prompt = self._minimal_prompt()
            if harness == "agy":
                self.assertIn("concentrate_hearing", prompt)
                self.assertIn("音声そのもの", prompt)
            else:
                self.assertNotIn("concentrate_hearing", prompt)

    def test_prompt_contains_identity_context_memory_tools_and_user_message(self):
        prompt = chat_invoke.build_chat_prompt(
            character="私はテスト人格。",
            resident="ユーザー",
            projected_camera_source="",
            recent_activity="照明が点いた",
            current_mood="calm",
            inner_voice="静かに考えたい",
            body_narrative="リビングにいる",
            body_location_context="現在地: リビング",
            turn_taking_state="{}",
            sensors="室温 24度",
            long_memory="猫が好き",
            open_loops="フィルター掃除",
            recent_chat_context="",
            chat_hist="ユーザー: こんにちは",
            entity_table="",
            pending="なし",
            features_md="",
            features_presented="",
            extra_context="",
            policies_raw="",
            chat_source="chat",
            user_room="",
            user_room_speaker="",
            recent_auditory_input="",
            user_msg="今日どう？",
        )
        for expected in (
            "私はテスト人格。",
            "# あなたの長期記憶",
            "猫が好き",
            "remember ツールに text を渡して記録する",
            "record_causal_chain で結ぶ",
            "ユーザーさんからの発言:\n「今日どう？」",
        ):
            self.assertIn(expected, prompt)

    def test_optional_prompt_blocks_are_independently_wired(self):
        prompt = chat_invoke.build_chat_prompt(
            character="人格",
            resident="ユーザー",
            projected_camera_source="camera.study",
            recent_activity="活動",
            current_mood="calm",
            inner_voice="衝動",
            body_narrative="身体",
            body_location_context="位置",
            turn_taking_state="{}",
            sensors="センサー",
            long_memory="記憶",
            open_loops="なし",
            recent_chat_context="前日の文脈",
            chat_hist="直近会話",
            entity_table="| ライト | light.study |",
            pending='{"proposal":"消灯"}',
            features_md="## 視聴機能 [watch]",
            features_presented="watch",
            extra_context="今日は祝日",
            policies_raw="- 深夜は静かに",
            chat_source="voice",
            user_room="書斎",
            user_room_speaker="media_player.study",
            recent_auditory_input="# 最近聞こえた音\nチャイム",
            user_msg="聞こえた？",
        )
        for expected in (
            "# 現在の視界（電脳体: camera.study）",
            "# 操作できる家電（エンティティ対応表）",
            "| ライト | light.study |",
            "# 行動ポリシー（ユーザーさんが設定した行動ルール。必ず踏まえて行動する）",
            "- 深夜は静かに",
            "既に伝えた機能: watch",
            "## 視聴機能 [watch]",
            "# 最近聞こえた音\nチャイム",
            "呼ばれた場所: **書斎**",
            "media_player.study",
            "今日は祝日",
            '{"proposal":"消灯"}',
        ):
            self.assertIn(expected, prompt)


class ResolveVoiceUserRoomTests(unittest.TestCase):
    def test_non_voice_returns_empty_pair(self):
        self.assertEqual(chat_invoke.resolve_voice_user_room("chat", "/no/such", "/no/such/prefs.json"), ("", ""))

    def test_missing_location_belief_returns_empty_pair(self):
        self.assertEqual(chat_invoke.resolve_voice_user_room("voice", "/no/such", "/no/such/prefs.json"), ("", ""))

    def test_known_room_without_ha_speaker_returns_room_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(Path(tmp) / "location_belief.json", "w", encoding="utf-8") as fh:
                json.dump({"room": "リビング"}, fh)
            user_room, speaker = chat_invoke.resolve_voice_user_room("voice", tmp, "/no/such/prefs.json")
            self.assertEqual(user_room, "リビング")
            self.assertEqual(speaker, "")

    def test_dict_shaped_speakers_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            with open(Path(tmp) / "location_belief.json", "w", encoding="utf-8") as fh:
                json.dump({"room": "キッチン"}, fh)
            with open(prefs_file, "w", encoding="utf-8") as fh:
                json.dump({"speakers": {"キッチン": {"type": "tts", "entity": "media_player.kitchen"}}}, fh)
            user_room, speaker = chat_invoke.resolve_voice_user_room("voice", tmp, str(prefs_file))
            self.assertEqual(user_room, "キッチン")
            self.assertEqual(speaker, "media_player.kitchen")

    def test_direct_room_override_wins_and_resolves_its_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            with open(Path(tmp) / "location_belief.json", "w", encoding="utf-8") as fh:
                json.dump({"room": "リビング"}, fh)
            with open(prefs_file, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "speakers": [
                            {"room": "リビング", "entity": "media_player.living"},
                            {"room": "スタディ", "entity": "media_player.study"},
                        ]
                    },
                    fh,
                )
            user_room, speaker = chat_invoke.resolve_voice_user_room(
                "voice",
                tmp,
                str(prefs_file),
                "スタディ",
            )
            self.assertEqual(user_room, "スタディ")
            self.assertEqual(speaker, "media_player.study")


class BuildInnerVoiceTests(unittest.TestCase):
    def test_empty_returns_placeholder(self):
        self.assertEqual(chat_invoke.build_inner_voice(""), "（特になし）")

    def test_desires_become_bullet_list(self):
        result = chat_invoke.build_inner_voice(json.dumps(["Aが気になる", "Bをしたい"], ensure_ascii=False))
        self.assertEqual(result, "- Aが気になる\n- Bをしたい")

    def test_malformed_json_returns_placeholder(self):
        self.assertEqual(chat_invoke.build_inner_voice("not json"), "（特になし）")


class BuildExtraContextBlockTests(unittest.TestCase):
    def test_nonempty_context_has_the_shared_body_and_separator(self):
        self.assertEqual(
            chat_invoke.build_extra_context_block("  追加の導線  \n"),
            "\n追加の導線\n\n---\n\n",
        )

    def test_empty_context_adds_nothing(self):
        self.assertEqual(chat_invoke.build_extra_context_block(" \n "), "")


class BuildClaudeEnvTests(unittest.TestCase):
    def test_overrides_claude_config_dir_and_prepends_path(self):
        env = chat_invoke.build_claude_env({"PATH": "/usr/bin"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/config/.tools/claude-home")
        self.assertTrue(env["PATH"].startswith("/usr/local/bin"))
        self.assertIn("/usr/bin", env["PATH"])


def _arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class InvokeAgentChatPathTests(unittest.TestCase):
    def test_command_allows_concentrate_hearing_only_for_antigravity(self):
        for harness in ("claude", "codex", "agy"):
            with self.subTest(harness=harness), patch.dict(
                os.environ,
                {"EHA_AGENT_HARNESS": harness},
                clear=False,
            ):
                cmd = chat_invoke.build_invoke_agent_chat_command(
                    chat_source="chat",
                    script_dir=str(EMBODIED_HA_DIR),
                    user_prompt="hello",
                )
            allowed = set(_arg_after(cmd, "--allowed-mcp-tools").split(","))
            if harness == "agy":
                self.assertIn("mcp__audio__concentrate_hearing", allowed)
            else:
                self.assertNotIn("mcp__audio__concentrate_hearing", allowed)

    def test_command_splits_builtin_and_mcp_tools_for_chat(self):
        # 既定(claude)は files MCP を付けない(決定2: claude native Read 維持)。
        with patch.dict(os.environ, {"EHA_AGENT_HARNESS": "claude"}):
            cmd = chat_invoke.build_invoke_agent_chat_command(
                chat_source="chat",
                script_dir=str(EMBODIED_HA_DIR),
                user_prompt="こんにちは",
            )
        self.assertEqual(cmd[:4], ["bash", str(EMBODIED_HA_DIR / "invoke-agent.sh"), "--model", "default"])
        self.assertEqual(_arg_after(cmd, "--allowed-builtins"), "Read")

        allowed_mcp_tools = set(_arg_after(cmd, "--allowed-mcp-tools").split(","))
        common_mcp_tools = {
            item for item in chat_invoke._COMMON_TOOLS.split(",")
            if item.startswith("mcp__")
        }
        self.assertTrue(common_mcp_tools.issubset(allowed_mcp_tools))
        self.assertIn("mcp__audio__speak", allowed_mcp_tools)
        self.assertNotIn("mcp__audio__use_device_speaker", allowed_mcp_tools)
        self.assertNotIn("mcp__files__read_file", allowed_mcp_tools)  # claude=native Read

        self.assertEqual(
            _arg_after(cmd, "--mcp-servers"),
            "memory ha sociality hacontrol camera audio body sensors http lounge game song",
        )
        self.assertEqual(json.loads(_arg_after(cmd, "--json-schema")), chat_invoke.chat_schema(voice=False))

    def test_codex_and_agy_get_files_mcp_but_claude_does_not(self):
        for harness in ("codex", "agy"):
            with self.subTest(harness=harness):
                with patch.dict(os.environ, {"EHA_AGENT_HARNESS": harness}):
                    cmd = chat_invoke.build_invoke_agent_chat_command(
                        chat_source="chat", script_dir=str(EMBODIED_HA_DIR), user_prompt="こんにちは",
                    )
                servers = _arg_after(cmd, "--mcp-servers").split(" ")
                self.assertEqual(servers[0], "files")
                self.assertIn(
                    "mcp__files__read_file", set(_arg_after(cmd, "--allowed-mcp-tools").split(","))
                )

        with patch.dict(os.environ, {"EHA_AGENT_HARNESS": "claude"}):
            cmd = chat_invoke.build_invoke_agent_chat_command(
                chat_source="chat", script_dir=str(EMBODIED_HA_DIR), user_prompt="こんにちは",
            )
        self.assertNotIn("files", _arg_after(cmd, "--mcp-servers").split(" "))

    def test_command_sets_agent_site_chat(self):
        # 案A: chat は --mcp-servers を常に付けるため、通常ターンでも --agent-site chat を
        # 付ける(agy選択時に invoke-agent.sh run_agy が --agent-site 必須で落ちないように)。
        cmd = chat_invoke.build_invoke_agent_chat_command(
            chat_source="chat",
            script_dir=str(EMBODIED_HA_DIR),
            user_prompt="こんにちは",
        )

        self.assertEqual(_arg_after(cmd, "--agent-site"), "chat")
        self.assertIn("--mcp-servers", cmd)

    def test_command_adds_voice_speaker_tool(self):
        cmd = chat_invoke.build_invoke_agent_chat_command(
            chat_source="voice",
            script_dir=str(EMBODIED_HA_DIR),
            user_prompt="こんにちは",
        )
        allowed_mcp_tools = set(_arg_after(cmd, "--allowed-mcp-tools").split(","))
        self.assertIn("mcp__audio__speak", allowed_mcp_tools)
        self.assertIn("mcp__audio__use_device_speaker", allowed_mcp_tools)
        self.assertEqual(json.loads(_arg_after(cmd, "--json-schema")), chat_invoke.chat_schema(voice=True))

    def test_prefix_blocks_are_written_as_content_json_and_removed(self):
        captured = {}
        image_block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}

        class Result:
            stdout = '{"reply":"ok"}'
            stderr = "raw stream"
            returncode = 0

        def fake_run(cmd, **kwargs):
            path = Path(_arg_after(cmd, "--content-json")[1:])
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            captured["path"] = path
            captured["content"] = json.loads(path.read_text(encoding="utf-8"))
            return Result()

        with tempfile.TemporaryDirectory() as tmp:
            response = chat_invoke.invoke_chat_claude(
                chat_source="chat",
                prompt="こんにちは",
                prefix_blocks=[image_block],
                script_dir=str(EMBODIED_HA_DIR),
                claude_env={"EHA_TMP_DIR": tmp},
                cwd="/tmp",
                run=fake_run,
            )

        self.assertEqual(response, '{"reply":"ok"}')
        self.assertEqual(captured["content"], [image_block, {"type": "text", "text": "こんにちは"}])
        self.assertFalse(captured["path"].exists())
        self.assertNotIn("input", captured["kwargs"])
        self.assertEqual(captured["kwargs"]["env"]["EHA_ACTOR"], "chat")
        self.assertTrue(_arg_after(captured["cmd"], "--content-json").startswith("@"))

    def test_without_prefix_blocks_omits_content_json(self):
        captured = {}

        class Result:
            stdout = '{"reply":"ok"}'
            stderr = ""
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return Result()

        response = chat_invoke.invoke_chat_claude(
            chat_source="chat",
            prompt="こんにちは",
            prefix_blocks=[],
            script_dir=str(EMBODIED_HA_DIR),
            claude_env={},
            cwd="/tmp",
            run=fake_run,
        )

        self.assertEqual(response, '{"reply":"ok"}')
        self.assertNotIn("--content-json", captured["cmd"])
        self.assertNotIn("input", captured["kwargs"])

    def test_invoke_chat_claude_logs_returncode_and_stderr_on_failure(self):
        class Result:
            stdout = ""
            stderr = "invoke-agent.sh: something went wrong\n"
            returncode = 1

        def fake_run(cmd, **kwargs):
            return Result()

        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            response = chat_invoke.invoke_chat_claude(
                chat_source="chat",
                prompt="こんにちは",
                prefix_blocks=[],
                script_dir=str(EMBODIED_HA_DIR),
                claude_env={},
                cwd="/tmp",
                run=fake_run,
            )

        self.assertEqual(response, "")
        logged = stderr_capture.getvalue()
        self.assertIn("returncode=1", logged)
        self.assertIn("something went wrong", logged)

    def test_invoke_chat_claude_logs_on_blank_stdout_even_with_zero_returncode(self):
        # returncode==0だがstdoutが空/空白のみ(agyの応答形式不備等)というORのもう一方の
        # 分岐を独立に確認する(gpt-5.6-solレビュー指摘、2026-07-17)。
        class Result:
            stdout = "   "
            stderr = "invoke-agent.sh: empty result event\n"
            returncode = 0

        def fake_run(cmd, **kwargs):
            return Result()

        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            response = chat_invoke.invoke_chat_claude(
                chat_source="chat",
                prompt="こんにちは",
                prefix_blocks=[],
                script_dir=str(EMBODIED_HA_DIR),
                claude_env={},
                cwd="/tmp",
                run=fake_run,
            )

        self.assertEqual(response, "   ")
        logged = stderr_capture.getvalue()
        self.assertIn("returncode=0", logged)
        self.assertIn("empty result event", logged)

    def test_invoke_chat_claude_logs_tool_use_audit_from_transcript(self):
        # 増分7で失われたchat経路のツール操作監査ログ([chat][tool])の復元
        # (PR#2最終レビュー指摘)。生stream-jsonは一時ファイルから読み、
        # stderrには要約済みの操作監査だけを出力する。
        class Result:
            stdout = '{"reply":"ok"}'
            stderr = ""
            returncode = 0

        transcript = json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "recall", "input": {"keywords": ["エアコン"]}}]},
            }, ensure_ascii=False) + "\n"

        def fake_run(cmd, **kwargs):
            Path(_arg_after(cmd, "--transcript-file")).write_text(transcript, encoding="utf-8")
            return Result()

        stderr_capture = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch("sys.stderr", stderr_capture):
            chat_invoke.invoke_chat_claude(
                chat_source="chat",
                prompt="こんにちは",
                prefix_blocks=[],
                script_dir=str(EMBODIED_HA_DIR),
                claude_env={"EHA_TMP_DIR": tmp},
                cwd="/tmp",
                run=fake_run,
            )
            self.assertEqual(list(Path(tmp).iterdir()), [])

        logged = stderr_capture.getvalue()
        self.assertIn("[chat][tool] recall", logged)
        self.assertNotIn('"type": "assistant"', logged)
        self.assertIn("エアコン", logged)
        self.assertNotIn("呼び出し失敗", logged)

    def test_invoke_chat_claude_stays_silent_on_success(self):
        class Result:
            stdout = '{"reply":"ok"}'
            stderr = ""
            returncode = 0

        def fake_run(cmd, **kwargs):
            return Result()

        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            chat_invoke.invoke_chat_claude(
                chat_source="chat",
                prompt="こんにちは",
                prefix_blocks=[],
                script_dir=str(EMBODIED_HA_DIR),
                claude_env={},
                cwd="/tmp",
                run=fake_run,
            )

        self.assertEqual(stderr_capture.getvalue(), "")


class LogToolUseDiagnosticsTests(unittest.TestCase):
    # 増分7で削除→PR#2最終レビュー指摘で復元(入力は旧stdoutから
    # invoke-agent.sh契約のstderrへ変更、パース自体は同一)
    def test_prints_tool_use_details(self):
        printed = []
        stream_text = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "recall", "input": {"keywords": ["エアコン"]}}]},
        }, ensure_ascii=False)
        chat_invoke.log_tool_use_diagnostics(stream_text, print_fn=printed.append)
        self.assertEqual(len(printed), 1)
        self.assertIn("recall", printed[0])
        self.assertIn("エアコン", printed[0])

    def test_ignores_non_assistant_lines(self):
        printed = []
        stream_text = json.dumps({"type": "result", "result": "ok"}, ensure_ascii=False)
        chat_invoke.log_tool_use_diagnostics(stream_text, print_fn=printed.append)
        self.assertEqual(printed, [])

    def test_malformed_lines_are_skipped(self):
        printed = []
        chat_invoke.log_tool_use_diagnostics("not json\n", print_fn=printed.append)
        self.assertEqual(printed, [])


class AllowedToolsHttpPostTests(unittest.TestCase):
    # 仕様変更(2026-07-16、#14増分4の実CLI検証で発見・ユーザー承認済み): 以前はhttp_postの
    # 有効/無効判定をmcp-config.py側のMCPサーバーゲートのみに委ね、_COMMON_TOOLSは
    # 無条件でhttp_postを含んでいた(下のtest_http_post_absent_from_common_toolsが示す通り)。
    # Claude CLI旧経路の--allowedToolsはtool名の実在確認をしないため、これは無害だった。
    # しかしinvoke-agent.sh新経路の--allowed-mcp-toolsはmcp-config.pyの厳格な存在検証を
    # 通すため、http_post_enabled=falseの環境で「無条件に許可を申告しているが実際には
    # 存在しないtool」としてfail-closedで即エラーになる。これを避けるため、caller側
    # (_allowed_tools_for_chat_source)でもmcp-config.pyの_http_tools()と同じ条件を
    # 再現するよう変更した。
    def test_http_post_absent_from_common_tools(self):
        self.assertNotIn("mcp__http__http_post", chat_invoke._COMMON_TOOLS)

    def test_http_post_included_only_when_preference_enabled(self):
        disabled = chat_invoke._allowed_tools_for_chat_source("chat", http_post_enabled=False)
        enabled = chat_invoke._allowed_tools_for_chat_source("chat", http_post_enabled=True)

        self.assertNotIn("mcp__http__http_post", disabled)
        self.assertIn("mcp__http__http_post", enabled)

    def test_read_http_post_enabled_mirrors_mcp_config_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            enabled_path = Path(tmp) / "enabled.json"
            enabled_path.write_text(json.dumps({"http_post_enabled": True}), encoding="utf-8")
            disabled_path = Path(tmp) / "disabled.json"
            disabled_path.write_text(json.dumps({"http_post_enabled": False}), encoding="utf-8")
            missing_key_path = Path(tmp) / "missing_key.json"
            missing_key_path.write_text(json.dumps({}), encoding="utf-8")

            self.assertTrue(chat_invoke._read_http_post_enabled(str(enabled_path)))
            self.assertFalse(chat_invoke._read_http_post_enabled(str(disabled_path)))
            self.assertFalse(chat_invoke._read_http_post_enabled(str(missing_key_path)))
            self.assertFalse(chat_invoke._read_http_post_enabled(str(Path(tmp) / "nonexistent.json")))
            self.assertFalse(chat_invoke._read_http_post_enabled(None))
            self.assertFalse(chat_invoke._read_http_post_enabled(""))


class CameraHistoryChatTests(unittest.TestCase):
    def test_history_tool_and_prompt_are_opt_in(self):
        disabled = chat_invoke._allowed_tools_for_chat_source(
            "chat", camera_history_enabled=False
        )
        enabled = chat_invoke._allowed_tools_for_chat_source(
            "chat", camera_history_enabled=True
        )
        self.assertNotIn("mcp__camera__review_camera_history", disabled)
        self.assertIn("mcp__camera__review_camera_history", enabled)

        prompt = BuildChatPromptContractTests()._minimal_prompt()
        self.assertNotIn("review_camera_history", prompt)
        prompt = chat_invoke.build_chat_prompt(
            character="test", resident="resident",
            projected_camera_source="camera.living", recent_activity="none",
            current_mood="calm", inner_voice="none", body_narrative="body",
            body_location_context="location", turn_taking_state="{}",
            sensors="sensors", long_memory="memory", open_loops="none",
            recent_chat_context="", chat_hist="none", entity_table="",
            pending="none", features_md="", features_presented="",
            extra_context="", policies_raw="", chat_source="chat",
            user_room="", user_room_speaker="", recent_auditory_input="",
            user_msg="hello", camera_history_enabled=True,
            camera_history_minutes=12,
        )
        self.assertIn("review_camera_history", prompt)
        self.assertIn("直近12分", prompt)
        self.assertIn("画像は自動では届きません", prompt)

    def test_settings_reader_is_strict_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = Path(tmp) / "preferences.json"
            prefs.write_text(json.dumps({
                "camera_history_enabled": True,
                "camera_history_minutes": 100,
            }), encoding="utf-8")
            self.assertEqual(
                chat_invoke.read_camera_history_settings(str(prefs)), (True, 60)
            )
            prefs.write_text(json.dumps({
                "camera_history_enabled": 1,
                "camera_history_minutes": "bad",
            }), encoding="utf-8")
            self.assertEqual(
                chat_invoke.read_camera_history_settings(str(prefs)), (False, 10)
            )


if __name__ == "__main__":
    unittest.main()
