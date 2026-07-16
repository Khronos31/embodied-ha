"""chat_invoke.py（chat.py移植 増分4）の単体テスト＋ゴールデン比較。

ゴールデン比較テストは、chat.sh自身のプロンプト構築コード（234-510行目、
prompt = f\"\"\"...\"\"\" の代入まで）を実際に読み取り、環境変数を制御した
状態で exec() 実行し、その結果の `prompt` 変数と
chat_invoke.build_chat_prompt() の出力を直接比較する。副作用（subprocess
呼び出し・ファイル書き込み）はこの行範囲に一切無いため、モック無しで
安全に実行できる（body_state.py/json_schemas.py の import と、
try/exceptで包まれたlocation_belief.json/preferences.jsonの読み取りのみ）。
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

import chat_invoke  # type: ignore  # noqa: E402

CHAT_SH = EMBODIED_HA_DIR / "chat.sh"
_PROMPT_SOURCE_START_LINE = 234  # import json, os, subprocess, sys
_PROMPT_SOURCE_END_LINE = 510    # {json_format_block}"""  (prompt f-string の終端)


def _extract_chat_sh_prompt_source():
    lines = CHAT_SH.read_text(encoding="utf-8").splitlines()
    # 行番号は1始まり・両端含む
    snippet = lines[_PROMPT_SOURCE_START_LINE - 1:_PROMPT_SOURCE_END_LINE]
    return "\n".join(snippet)


_CHAT_SH_PROMPT_SOURCE = _extract_chat_sh_prompt_source()


def _run_chat_sh_prompt(env):
    """chat.shの実プロンプト構築コードを、指定env varsの下でexec実行し`prompt`を返す。"""
    full_env = {"SCRIPT_DIR": str(EMBODIED_HA_DIR), **env}
    with patch.dict("os.environ", full_env, clear=True):
        namespace = {}
        exec(_CHAT_SH_PROMPT_SOURCE, namespace)  # noqa: S102
    return namespace["prompt"]


def _default_env(**overrides):
    env = {
        "USER_MSG": "こんにちは",
        "CHAT_SOURCE_VALUE": "chat",
        "RECENT_ACTIVITY": "なし",
        "CURRENT_MOOD": "おだやか",
        "LONG_MEMORY": "なし",
        "CHAT_HISTORY": "なし",
        "RECENT_CHAT_CONTEXT": "",
        "SENSORS": "",
        "BODY_LOCATION_CONTEXT": "",
        "PROJECTED_CAMERA_SOURCE": "",
        "ENTITY_TABLE": "",
        "POLICIES": "",
        "EXTRA_CONTEXT": "",
        "FEATURES_MD": "",
        "FEATURES_PRESENTED": "",
        "PENDING_PROPOSAL": "なし",
        "OPEN_LOOPS": "なし",
        "TURN_TAKING_STATE": "{}",
        "CHARACTER": "私はあかね。",
        "RECENT_AUDITORY_INPUT": "",
        "ACTIVE_DESIRES": "",
        "RESIDENT": "ゆの",
        "EHA_BODY_STATE": "{}",
        "EHA_DATA_DIR": "/no/such/data_dir",
        "EHA_PREFS_FILE": "/no/such/preferences.json",
    }
    env.update(overrides)
    return env


def _build_prompt_via_chat_invoke(env):
    """envと同じ入力値からchat_invoke.build_chat_promptを呼ぶ（golden比較の対抗馬）。"""
    chat_source = env["CHAT_SOURCE_VALUE"]
    user_room, user_room_speaker = chat_invoke.resolve_voice_user_room(
        chat_source, env["EHA_DATA_DIR"], env["EHA_PREFS_FILE"]
    )
    inner_voice = chat_invoke.build_inner_voice(env["ACTIVE_DESIRES"])
    body_narrative = chat_invoke.build_body_narrative(env["EHA_BODY_STATE"])
    return chat_invoke.build_chat_prompt(
        character=env["CHARACTER"],
        resident=env["RESIDENT"],
        projected_camera_source=env["PROJECTED_CAMERA_SOURCE"],
        recent_activity=env["RECENT_ACTIVITY"],
        current_mood=env["CURRENT_MOOD"],
        inner_voice=inner_voice,
        body_narrative=body_narrative,
        body_location_context=env["BODY_LOCATION_CONTEXT"],
        turn_taking_state=env["TURN_TAKING_STATE"],
        sensors=env["SENSORS"],
        long_memory=env["LONG_MEMORY"],
        open_loops=env["OPEN_LOOPS"],
        recent_chat_context=env["RECENT_CHAT_CONTEXT"],
        chat_hist=env["CHAT_HISTORY"],
        entity_table=env["ENTITY_TABLE"],
        pending=env["PENDING_PROPOSAL"],
        features_md=env["FEATURES_MD"],
        features_presented=env["FEATURES_PRESENTED"],
        extra_context=env["EXTRA_CONTEXT"],
        policies_raw=env["POLICIES"],
        chat_source=chat_source,
        user_room=user_room,
        user_room_speaker=user_room_speaker,
        recent_auditory_input=env["RECENT_AUDITORY_INPUT"],
        user_msg=env["USER_MSG"],
    )


class PromptGoldenComparisonTests(unittest.TestCase):
    """chat.sh実物のプロンプト構築コードとchat_invoke.build_chat_promptの出力一致を検証。"""

    def _assert_golden_match(self, env):
        expected = _run_chat_sh_prompt(env)
        actual = _build_prompt_via_chat_invoke(env)
        self.assertEqual(actual, expected)

    def test_plain_chat_minimal(self):
        self._assert_golden_match(_default_env())

    def test_chat_with_entity_table_and_features(self):
        self._assert_golden_match(_default_env(
            ENTITY_TABLE="| 名前 | entity_id | 備考 |\n|------|-----------|------|\n| リビングのライト | light.living | |",
            FEATURES_MD="## 視聴予約 [viewing_reservation]\n番組を予約できます。",
            FEATURES_PRESENTED="viewing_reservation",
        ))

    def test_chat_with_pending_proposal_and_policies(self):
        self._assert_golden_match(_default_env(
            PENDING_PROPOSAL=json.dumps({"提案文": "電気消しますか？", "action": "light_off"}, ensure_ascii=False),
            POLICIES="- 集中してるときは静かに\n- 21時以降は控えめに",
            EXTRA_CONTEXT="今日は祝日です。",
        ))

    def test_chat_with_recent_chat_context_and_active_desires(self):
        self._assert_golden_match(_default_env(
            RECENT_CHAT_CONTEXT="ゆのさん: 昨日の話の続きだけど\nClaude: はい、覚えてます",
            ACTIVE_DESIRES=json.dumps(["ゆのさんの様子が気になる", "最近のコミットを見たい"], ensure_ascii=False),
            PROJECTED_CAMERA_SOURCE="camera.living",
        ))

    def test_voice_with_known_room_and_tcp_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            prefs_file = data_dir / "preferences.json"
            with open(data_dir / "location_belief.json", "w", encoding="utf-8") as fh:
                json.dump({"room": "スタディ"}, fh)
            with open(prefs_file, "w", encoding="utf-8") as fh:
                json.dump({"speakers": [{"room": "スタディ", "type": "tcp", "host": "192.168.1.139", "port": 3334}]}, fh)
            self._assert_golden_match(_default_env(
                CHAT_SOURCE_VALUE="voice",
                EHA_DATA_DIR=str(data_dir),
                EHA_PREFS_FILE=str(prefs_file),
            ))

    def test_voice_with_unknown_room(self):
        self._assert_golden_match(_default_env(CHAT_SOURCE_VALUE="voice"))

    def test_chat_with_auditory_input_block(self):
        self._assert_golden_match(_default_env(
            RECENT_AUDITORY_INPUT="# 最近聞こえた音\n玄関でチャイムが鳴りました。",
        ))


class ResolveVoiceUserRoomTests(unittest.TestCase):
    def test_non_voice_returns_empty_pair(self):
        self.assertEqual(chat_invoke.resolve_voice_user_room("chat", "/no/such", "/no/such/prefs.json"), ("", ""))

    def test_missing_location_belief_returns_empty_pair(self):
        self.assertEqual(chat_invoke.resolve_voice_user_room("voice", "/no/such", "/no/such/prefs.json"), ("", ""))

    def test_known_room_without_tcp_speaker_returns_room_only(self):
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
                json.dump({"speakers": {"キッチン": {"type": "tcp", "host": "192.168.1.100", "port": 3334}}}, fh)
            user_room, speaker = chat_invoke.resolve_voice_user_room("voice", tmp, str(prefs_file))
            self.assertEqual(user_room, "キッチン")
            self.assertEqual(speaker, "tcp://192.168.1.100:3334")


class BuildInnerVoiceTests(unittest.TestCase):
    def test_empty_returns_placeholder(self):
        self.assertEqual(chat_invoke.build_inner_voice(""), "（特になし）")

    def test_desires_become_bullet_list(self):
        result = chat_invoke.build_inner_voice(json.dumps(["Aが気になる", "Bをしたい"], ensure_ascii=False))
        self.assertEqual(result, "- Aが気になる\n- Bをしたい")

    def test_malformed_json_returns_placeholder(self):
        self.assertEqual(chat_invoke.build_inner_voice("not json"), "（特になし）")


class BuildClaudeEnvTests(unittest.TestCase):
    def test_overrides_claude_config_dir_and_prepends_path(self):
        env = chat_invoke.build_claude_env({"PATH": "/usr/bin"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/config/.tools/claude-home")
        self.assertTrue(env["PATH"].startswith("/config/.tools/bin"))
        self.assertIn("/usr/bin", env["PATH"])


class BuildClaudeCommandTests(unittest.TestCase):
    def test_no_script_dir_omits_tool_flags(self):
        cmd = chat_invoke.build_claude_command(
            chat_source="chat", script_dir="", claude_env={}, run_mcp_config=lambda *a, **k: None,
        )
        self.assertNotIn("--allowedTools", cmd)
        self.assertNotIn("--mcp-config", cmd)

    def test_mcp_config_missing_after_run_omits_tool_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "mcp_chat.json"  # run_mcp_configが何も作らない想定
            cmd = chat_invoke.build_claude_command(
                chat_source="chat", script_dir=str(EMBODIED_HA_DIR), claude_env={},
                mcp_config_path=str(missing_path), run_mcp_config=lambda *a, **k: None,
            )
            self.assertNotIn("--allowedTools", cmd)

    def test_voice_adds_speaker_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            mcp_path = Path(tmp) / "mcp_chat.json"
            mcp_path.write_text("{}", encoding="utf-8")
            cmd = chat_invoke.build_claude_command(
                chat_source="voice", script_dir=str(EMBODIED_HA_DIR), claude_env={},
                mcp_config_path=str(mcp_path), run_mcp_config=lambda *a, **k: None,
            )
            idx = cmd.index("--allowedTools")
            self.assertIn("mcp__audio__use_device_speaker", cmd[idx + 1])

    def test_chat_omits_use_device_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            mcp_path = Path(tmp) / "mcp_chat.json"
            mcp_path.write_text("{}", encoding="utf-8")
            cmd = chat_invoke.build_claude_command(
                chat_source="chat", script_dir=str(EMBODIED_HA_DIR), claude_env={},
                mcp_config_path=str(mcp_path), run_mcp_config=lambda *a, **k: None,
            )
            idx = cmd.index("--allowedTools")
            self.assertNotIn("mcp__audio__use_device_speaker", cmd[idx + 1])
            self.assertIn("mcp__audio__speak", cmd[idx + 1])


def _arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class InvokeAgentChatPathTests(unittest.TestCase):
    def test_default_migrates_normal_path(self):
        self.assertEqual(chat_invoke.invoke_agent_migrated_chat_paths({}), {"normal"})

    def test_empty_override_disables_migration(self):
        self.assertEqual(chat_invoke.invoke_agent_migrated_chat_paths({"EHA_INVOKE_AGENT_CHAT_PATHS": ""}), set())

    def test_command_splits_builtin_and_mcp_tools_for_chat(self):
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

        self.assertEqual(
            _arg_after(cmd, "--mcp-servers"),
            "memory ha sociality hacontrol camera audio body sensors http lounge game song",
        )
        self.assertEqual(json.loads(_arg_after(cmd, "--json-schema")), chat_invoke.chat_schema(voice=False))

    def test_queued_listen_turn_stays_on_direct_path_by_default(self):
        # sol reviewの指摘(2026-07-16): is_queued_listen=Trueのとき、"normal"が
        # デフォルトで移行済みでもqueued listenは巻き込まれず旧経路(直接claude呼び出し)
        # のままであることを確認する(--sound-file転送は増分5でまだ未実装のため)。
        calls = []

        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            payload = {"type": "result", "structured_output": {"reply": "queued reply"}}
            return Result(stdout=json.dumps(payload, ensure_ascii=False))

        response = chat_invoke.invoke_chat_claude(
            chat_source="chat",
            prompt="こんにちは",
            prefix_blocks=None,
            script_dir=str(EMBODIED_HA_DIR),
            claude_env={},
            cwd="/tmp",
            claude_bin="/bin/claude",
            is_queued_listen=True,
            run=fake_run,
        )

        self.assertEqual(json.loads(response)["reply"], "queued reply")
        self.assertEqual(len(calls), 1)
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[0], "/bin/claude")
        self.assertNotIn("invoke-agent.sh", cmd[0] if cmd else "")
        self.assertIn("input", kwargs)

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


class BuildMessageEnvelopeTests(unittest.TestCase):
    def test_no_prefix_blocks_yields_single_text_block(self):
        envelope = json.loads(chat_invoke.build_message_envelope("こんにちは"))
        self.assertEqual(envelope["message"]["content"], [{"type": "text", "text": "こんにちは"}])

    def test_prefix_blocks_come_before_prompt_text(self):
        image_block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}
        envelope = json.loads(chat_invoke.build_message_envelope("こんにちは", prefix_blocks=[image_block]))
        content = envelope["message"]["content"]
        self.assertEqual(content[0], image_block)
        self.assertEqual(content[-1], {"type": "text", "text": "こんにちは"})

    def test_empty_prefix_blocks_list_behaves_like_none(self):
        envelope = json.loads(chat_invoke.build_message_envelope("こんにちは", prefix_blocks=[]))
        self.assertEqual(envelope["message"]["content"], [{"type": "text", "text": "こんにちは"}])


class InvokeClaudeTests(unittest.TestCase):
    def test_delegates_to_run_with_expected_kwargs(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return "FAKE_RESULT"

        result = chat_invoke.invoke_claude(["claude", "-p"], "msg", "/some/cwd", {"A": "1"}, run=fake_run)
        self.assertEqual(result, "FAKE_RESULT")
        self.assertEqual(captured["cmd"], ["claude", "-p"])
        self.assertEqual(captured["kwargs"]["input"], "msg")
        self.assertEqual(captured["kwargs"]["cwd"], "/some/cwd")
        self.assertEqual(captured["kwargs"]["env"], {"A": "1"})
        self.assertTrue(captured["kwargs"]["capture_output"])
        self.assertTrue(captured["kwargs"]["text"])


class LogToolUseDiagnosticsTests(unittest.TestCase):
    def test_prints_tool_use_details(self):
        printed = []
        stdout = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "recall", "input": {"keywords": ["エアコン"]}}]},
        }, ensure_ascii=False)
        chat_invoke.log_tool_use_diagnostics(stdout, print_fn=printed.append)
        self.assertEqual(len(printed), 1)
        self.assertIn("recall", printed[0])
        self.assertIn("エアコン", printed[0])

    def test_ignores_non_assistant_lines(self):
        printed = []
        stdout = json.dumps({"type": "result", "result": "ok"}, ensure_ascii=False)
        chat_invoke.log_tool_use_diagnostics(stdout, print_fn=printed.append)
        self.assertEqual(printed, [])

    def test_malformed_lines_are_skipped(self):
        printed = []
        chat_invoke.log_tool_use_diagnostics("not json\n", print_fn=printed.append)
        self.assertEqual(printed, [])


class ExtractResponseTextTests(unittest.TestCase):
    def test_normal_result_returns_text_without_diagnostics(self):
        printed = []
        stdout = json.dumps({"type": "result", "result": '{"reply": "こんにちは"}'}, ensure_ascii=False)
        result = chat_invoke.extract_response_text(stdout, "", 0, print_fn=printed.append)
        self.assertEqual(result, '{"reply": "こんにちは"}')
        self.assertEqual(printed, [])

    def test_empty_response_prints_diagnostics(self):
        printed = []
        result = chat_invoke.extract_response_text("", "some claude stderr", 1, print_fn=printed.append)
        self.assertEqual(result, "")
        self.assertTrue(any("returncode=1" in p for p in printed))
        self.assertTrue(any("some claude stderr" in p for p in printed))

    def test_empty_response_with_no_stderr_only_prints_returncode_line(self):
        printed = []
        chat_invoke.extract_response_text("", "", 0, print_fn=printed.append)
        self.assertEqual(len(printed), 1)


class AllowedToolsHttpPostTests(unittest.TestCase):
    # 仕様変更(2026-07-16、#14増分4の実CLI検証で発見・ゆの承認済み): 以前はhttp_postの
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


if __name__ == "__main__":
    unittest.main()
