import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import loop  # noqa: E402


class LoopPyModeSelectionTests(unittest.TestCase):
    def test_choose_mode_respects_explicit_mode(self):
        self.assertEqual(loop.choose_mode({"MODE": "reflect"}), "reflect")

    def test_compute_mode_weights_applies_anomaly_urgency(self):
        normal = loop.compute_mode_weights({}, anomaly_urgency=0, github_app_exists=True)
        urgent = loop.compute_mode_weights({}, anomaly_urgency=10, github_app_exists=True)

        self.assertGreater(urgent["observe"], normal["observe"])
        self.assertGreater(urgent["explore"], normal["explore"])
        self.assertEqual(urgent["reflect"], normal["reflect"])

    def test_choose_mode_uses_env_anomaly_urgency_and_disables_social_without_github_app(self):
        captured = {}

        def fake_choices(modes, weights, k):
            captured["modes"] = modes
            captured["weights"] = weights
            captured["k"] = k
            return ["explore"]

        with tempfile.TemporaryDirectory() as tmpdir:
            mode = loop.choose_mode(
                {
                    "EHA_BODY_STATE": json.dumps({"curiosity": 0.5, "energy": 0.5, "stress": 0.0}),
                    "ANOMALY_URGENCY": "10",
                    "EHA_GITHUB_APP_PEM": str(Path(tmpdir) / "missing.pem"),
                },
                choices=fake_choices,
            )

        self.assertEqual(mode, "explore")
        weights = dict(zip(captured["modes"], captured["weights"]))
        self.assertEqual(captured["k"], 1)
        self.assertEqual(weights["social"], 0)
        self.assertEqual(weights["observe"], 38)
        self.assertEqual(weights["explore"], 47)


class LoopPyInvocationTests(unittest.TestCase):
    def test_invoke_loop_claude_explore_with_boundary_gate_hacontrol_uses_invoke_agent(self):
        # apply_boundary_gate()がexploreモードで許可時にallowed_tools/mcp_serversへ
        # hacontrol/mcp__hacontrol__ha_call_serviceを動的追加する(loop.py:676-700)。
        # invoke_loop_claude()の新経路(build_invoke_agent_loop_command)は追加変換なしに
        # mcp__ prefixで分割するだけなので、拡張済みの値がそのまま正しく渡ることを確認する
        # (#18で決定済みの設計の実配線側の裏付け)。
        calls = []

        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            # invoke-agent.sh's own extract_result_json already unwraps the
            # stream-json "result" event before returning stdout to the caller
            # (unlike the old direct-claude path, whose raw stdout still needs
            # stream_result_payload()).
            payload = {"private": "見回った", "emotion": "calm", "speak": None}
            return Result(stdout=json.dumps(payload, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmpdir:
            response = loop.invoke_loop_claude(
                user_prompt="user",
                system_prompt="system",
                mode="explore",
                allowed_tools="mcp__sensors__get_sensors,mcp__hacontrol__ha_call_service",
                mcp_servers=["sensors", "hacontrol"],
                environ={
                    "SCRIPT_DIR": str(ROOT / "embodied_ha"),
                    "EHA_DATA_DIR": tmpdir,
                },
                run=fake_run,
            )

        self.assertEqual(json.loads(response)["private"], "見回った")
        invoke_calls = [call for call in calls if "invoke-agent.sh" in call[0][1]]
        self.assertEqual(len(invoke_calls), 1)
        cmd, _kwargs = invoke_calls[0]
        self.assertEqual(cmd[cmd.index("--allowed-mcp-tools") + 1], "mcp__sensors__get_sensors,mcp__hacontrol__ha_call_service")
        self.assertEqual(cmd[cmd.index("--mcp-servers") + 1], "sensors hacontrol")
        self.assertNotIn("--allowed-builtins", cmd)

    def test_invoke_loop_claude_observe_haiku_uses_lite_model_and_content_json_file(self):
        calls = []
        seen_content_path = []
        seen_content = []

        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            content_arg = cmd[cmd.index("--content-json") + 1]
            self.assertTrue(content_arg.startswith("@"))
            content_path = content_arg[1:]
            self.assertTrue(os.path.exists(content_path))
            seen_content_path.append(content_path)
            seen_content.append(json.loads(Path(content_path).read_text(encoding="utf-8")))
            payload = "Fixture: clear"
            return Result(stdout=payload)

        content_blocks = [
            {"type": "text", "text": "Fixture camera:"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"}},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            response = loop.invoke_loop_claude(
                user_prompt="watch",
                system_prompt="system",
                mode="observe",
                allowed_tools="",
                mcp_servers=[],
                environ={
                    "SCRIPT_DIR": str(ROOT / "embodied_ha"),
                    "EHA_DATA_DIR": tmpdir,
                    "EHA_TMP_DIR": str(tmp / "tmp"),
                },
                model="haiku",
                content_blocks=content_blocks,
                response_schema=None,
                run=fake_run,
            )

            self.assertEqual(response, "Fixture: clear")
            self.assertEqual(seen_content, [content_blocks])
            self.assertFalse(os.path.exists(seen_content_path[0]))

        cmd, kwargs = calls[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "lite")
        self.assertNotIn("--json-schema", cmd)
        self.assertNotIn("--allowed-builtins", cmd)
        self.assertNotIn("--allowed-mcp-tools", cmd)
        self.assertNotIn("EHA_CLAUDE_MODEL_DEFAULT", kwargs["env"])

    def test_invoke_loop_claude_observe_sonnet_uses_default_model_and_content_json_file(self):
        calls = []
        seen_content_path = []
        seen_content = []

        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            content_arg = cmd[cmd.index("--content-json") + 1]
            self.assertTrue(content_arg.startswith("@"))
            content_path = content_arg[1:]
            self.assertTrue(os.path.exists(content_path))
            seen_content_path.append(content_path)
            seen_content.append(json.loads(Path(content_path).read_text(encoding="utf-8")))
            payload = {"private": "見守った", "emotion": "calm", "speak": None}
            return Result(stdout=json.dumps(payload, ensure_ascii=False))

        content_blocks = [
            {"type": "text", "text": "summary"},
            {"type": "text", "text": "observe prompt"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            response = loop.invoke_loop_claude(
                user_prompt="observe prompt",
                system_prompt="system",
                mode="observe",
                allowed_tools="mcp__sensors__get_sensors",
                mcp_servers=["sensors"],
                environ={
                    "SCRIPT_DIR": str(ROOT / "embodied_ha"),
                    "EHA_DATA_DIR": tmpdir,
                    "EHA_TMP_DIR": str(tmp / "tmp"),
                },
                model="sonnet",
                content_blocks=content_blocks,
                response_schema=loop.loop_schema("observe"),
                run=fake_run,
            )

            self.assertEqual(json.loads(response)["private"], "見守った")
            self.assertEqual(seen_content, [content_blocks])
            self.assertFalse(os.path.exists(seen_content_path[0]))

        cmd, kwargs = calls[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "default")
        self.assertIn("--json-schema", cmd)
        self.assertEqual(cmd[cmd.index("--content-json") + 1], f"@{seen_content_path[0]}")
        self.assertEqual(cmd[cmd.index("--allowed-mcp-tools") + 1], "mcp__sensors__get_sensors")
        self.assertEqual(cmd[cmd.index("--mcp-servers") + 1], "sensors")
        self.assertNotIn("EHA_CLAUDE_MODEL_DEFAULT", kwargs["env"])

    def test_invoke_loop_claude_sound_file_forwards_agent_site_and_drops_content_json(self):
        # #14増分6: queued listen(sound_file)がobserveの投射カメラ画像content_blocksと
        # 同時に発生しても、agyは--content-json/--allowed-builtinsで即死するため
        # 黙って落とす(chat_invoke.pyと同じ既知のトレードオフ)。
        calls = []

        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return Result(stdout="音が聞こえました")

        content_blocks = [{"type": "text", "text": "camera frame note"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            response = loop.invoke_loop_claude(
                user_prompt="observe prompt",
                system_prompt="system",
                mode="observe",
                allowed_tools="Read,mcp__sensors__get_sensors",
                mcp_servers=["sensors"],
                environ={
                    "SCRIPT_DIR": str(ROOT / "embodied_ha"),
                    "EHA_DATA_DIR": tmpdir,
                    "EHA_TMP_DIR": str(tmp / "tmp"),
                },
                model="sonnet",
                content_blocks=content_blocks,
                sound_file="/tmp/queued.wav",
                response_schema=None,
                run=fake_run,
            )

            self.assertEqual(response, "音が聞こえました")

        cmd, kwargs = calls[0]
        self.assertEqual(cmd[cmd.index("--sound-file") + 1], "/tmp/queued.wav")
        self.assertEqual(cmd[cmd.index("--agent-site") + 1], "observe")
        self.assertNotIn("--allowed-builtins", cmd)
        self.assertNotIn("--content-json", cmd)
        self.assertEqual(cmd[cmd.index("--allowed-mcp-tools") + 1], "mcp__sensors__get_sensors")

    def test_command_without_sound_file_sets_agent_site_for_agy(self):
        # 案A: 通常ターン(非sound_file)でも --mcp-servers があれば --agent-site を付ける。
        # agy選択時に invoke-agent.sh run_agy が --agent-site 必須で落ちるのを防ぐ
        # ([[embodied_ha_agent_site_missing_for_normal_agy_turns_2026-07-17]])。
        cmd = loop.build_invoke_agent_loop_command(
            script_dir=str(ROOT / "embodied_ha"),
            mode="observe",
            model_tier="default",
            allowed_tools="mcp__sensors__get_sensors",
            mcp_servers=["sensors"],
            system_prompt="system",
            user_prompt="observe prompt",
            response_schema=None,
        )
        self.assertNotIn("--sound-file", cmd)
        self.assertEqual(cmd[cmd.index("--agent-site") + 1], "observe")
        self.assertEqual(cmd[cmd.index("--mcp-servers") + 1], "sensors")

    def test_invoke_loop_claude_logs_returncode_and_stderr_on_failure(self):
        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            return Result(stderr="invoke-agent.sh: something went wrong\n", returncode=1)

        stderr_capture = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("sys.stderr", stderr_capture):
            response = loop.invoke_loop_claude(
                user_prompt="user",
                system_prompt="system",
                mode="observe",
                allowed_tools="mcp__sensors__get_sensors",
                mcp_servers=["sensors"],
                environ={"SCRIPT_DIR": str(ROOT / "embodied_ha"), "EHA_DATA_DIR": tmpdir},
                run=fake_run,
            )

        self.assertEqual(response, "")
        logged = stderr_capture.getvalue()
        self.assertIn("returncode=1", logged)
        self.assertIn("something went wrong", logged)

    def test_invoke_loop_claude_logs_on_blank_stdout_even_with_zero_returncode(self):
        # returncode==0だがstdoutが空/空白のみというORのもう一方の分岐を独立に確認する
        # (gpt-5.6-solレビュー指摘、2026-07-17)。
        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            return Result(stdout="   ", stderr="invoke-agent.sh: empty result event\n", returncode=0)

        stderr_capture = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("sys.stderr", stderr_capture):
            response = loop.invoke_loop_claude(
                user_prompt="user",
                system_prompt="system",
                mode="observe",
                allowed_tools="mcp__sensors__get_sensors",
                mcp_servers=["sensors"],
                environ={"SCRIPT_DIR": str(ROOT / "embodied_ha"), "EHA_DATA_DIR": tmpdir},
                run=fake_run,
            )

        self.assertEqual(response, "   ")
        logged = stderr_capture.getvalue()
        self.assertIn("returncode=0", logged)
        self.assertIn("empty result event", logged)

    def test_invoke_loop_claude_stays_silent_on_success(self):
        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            return Result(stdout=json.dumps({"private": "ok"}, ensure_ascii=False))

        stderr_capture = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("sys.stderr", stderr_capture):
            loop.invoke_loop_claude(
                user_prompt="user",
                system_prompt="system",
                mode="observe",
                allowed_tools="mcp__sensors__get_sensors",
                mcp_servers=["sensors"],
                environ={"SCRIPT_DIR": str(ROOT / "embodied_ha"), "EHA_DATA_DIR": tmpdir},
                run=fake_run,
            )

        self.assertEqual(stderr_capture.getvalue(), "")

    def test_observe_invocation_uses_sonnet_and_does_not_set_actor(self):
        calls = []

        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            payload = {"private": "見守った", "emotion": "calm", "speak": None}
            return Result(json.dumps(payload, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmpdir:
            response = loop.invoke_loop_claude(
                user_prompt="user",
                system_prompt="system",
                mode="observe",
                allowed_tools="mcp__sensors__get_sensors",
                mcp_servers=["sensors"],
                environ={
                    "SCRIPT_DIR": str(ROOT / "embodied_ha"),
                    "CLAUDE_BIN": "/bin/claude",
                    "EHA_SESSION_MODEL": "opus",
                    "EHA_DATA_DIR": tmpdir,
                },
                model="sonnet",
                run=fake_run,
            )

        self.assertEqual(json.loads(response)["private"], "見守った")
        invoke_cmd, invoke_kwargs = [call for call in calls if call[0][:2] == ["bash", str(ROOT / "embodied_ha" / "invoke-agent.sh")]][0]
        self.assertEqual(invoke_cmd[invoke_cmd.index("--model") + 1], "default")
        self.assertNotIn("input", invoke_kwargs)
        self.assertNotIn("EHA_ACTOR", invoke_kwargs["env"])

    def test_watch_summary_invocation_uses_haiku_without_tools_or_schema(self):
        calls = []

        class Result:
            def __init__(self, stdout="", stderr="", returncode=0):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:2] == ["bash", str(ROOT / "embodied_ha" / "invoke-agent.sh")]:
                return Result("Fixture: clear")
            return Result()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            prefs = tmp / "preferences.json"
            prefs.write_text(
                json.dumps({"cameras": [{"ha_entity": "camera.fixture", "label": "Fixture"}]}),
                encoding="utf-8",
            )
            context = {
                "cfg": {
                    "SCRIPT_DIR": str(ROOT / "embodied_ha"),
                    "CLAUDE_BIN": "/bin/claude",
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_SESSION_MODEL": "opus",
                    "EHA_DATA_DIR": tmpdir,
                },
                "projected_camera_source": "",
                "user_prompt": "observe prompt",
            }
            original_fetch_frame = loop.fetch_frame
            try:
                loop.fetch_frame = lambda *_args, **_kwargs: b"JPEGFIXTURE" * 20
                blocks = loop.build_observe_content_blocks(
                    context,
                    loop.LoopPaths(
                        log_dir=str(tmp),
                        observation_log=str(tmp / "observations.jsonl"),
                        explore_log=str(tmp / "explore.jsonl"),
                        chat_log=str(tmp / "chat_log.jsonl"),
                        memory_file=str(tmp / "memory.md"),
                        pending_file=str(tmp / "pending_proposal.json"),
                        daybook_marker=str(tmp / ".last_daybook"),
                        tmp_dir=str(tmp / "tmp"),
                    ),
                    run=fake_run,
                )
            finally:
                loop.fetch_frame = original_fetch_frame

        self.assertIn("Fixture: clear", blocks[0]["text"])
        invoke_cmd, invoke_kwargs = [call for call in calls if call[0][:2] == ["bash", str(ROOT / "embodied_ha" / "invoke-agent.sh")]][0]
        self.assertEqual(invoke_cmd[invoke_cmd.index("--model") + 1], "lite")
        self.assertNotIn("--allowed-builtins", invoke_cmd)
        self.assertNotIn("--allowed-mcp-tools", invoke_cmd)
        self.assertNotIn("--json-schema", invoke_cmd)
        self.assertNotIn("EHA_ACTOR", invoke_kwargs["env"])


class LoopPyPostprocessTests(unittest.TestCase):
    def test_pending_proposal_requires_action_triplet(self):
        payload = loop.pending_proposal_payload(
            {
                "proposal": "電気を消しましょうか",
                "action": {"domain": "light", "service": "turn_off", "entity_id": "light.living"},
            },
            timestamp="2026-07-15T12:00:00+09:00",
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["proposal"], "電気を消しましょうか")

        self.assertIsNone(loop.pending_proposal_payload({"proposal": "x", "action": {"domain": "light"}}, timestamp="t"))

    def test_write_pending_proposal_and_speak_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pending_proposal.json"
            payload = {
                "timestamp": "t",
                "proposal": "電気を消しましょうか",
                "action": {"domain": "light", "service": "turn_off", "entity_id": "light.living"},
            }
            self.assertTrue(loop.write_pending_proposal(path, payload))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["proposal"], "電気を消しましょうか")

        plan = loop.loop_speak_plan({"_parse_ok": True, "speak": "あとで見ます"}, payload)
        self.assertEqual(plan["tts"], "電気を消しましょうか")
        self.assertEqual(plan["say"], "あとで見ます")

    def test_first_speaker_room_supports_list_and_dict_shapes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(json.dumps({"speakers": [{"room": "study"}]}), encoding="utf-8")
            self.assertEqual(loop.first_speaker_room(prefs), "study")
            prefs.write_text(json.dumps({"speakers": {"living": {"type": "tcp"}}}), encoding="utf-8")
            self.assertEqual(loop.first_speaker_room(prefs), "living")

    def test_append_loop_chat_log_uses_loop_jsonl_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "chat_log.jsonl"
            loop.append_loop_chat_log(path, timestamp="t", source="reflect", claude="考えています")
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(rows, [{"timestamp": "t", "source": "reflect", "claude": "考えています", "user": None}])


class LoopPyPersistenceTests(unittest.TestCase):
    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_parse_failure_records_error_but_skips_observation_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed = loop.parse_loop_response("plain raw failure")

            should_skip = loop.record_parse_skip_if_needed(
                parsed=parsed,
                response="plain raw failure",
                log_dir=tmp,
                timestamp="2026-07-15T12:00:00+09:00",
                mode="observe",
            )
            persisted = loop.persist_loop_introspection(
                parsed=parsed,
                mode="observe",
                timestamp="2026-07-15T12:00:00+09:00",
                observation_log=tmp / "observations.jsonl",
                explore_log=tmp / "explore.jsonl",
            )

            self.assertTrue(should_skip)
            self.assertFalse(persisted)
            self.assertEqual(self.read_jsonl(tmp / "observations.jsonl"), [])
            errors = self.read_jsonl(tmp / "loop_parse_errors.jsonl")
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "json_parse_failed")
            self.assertEqual(errors[0]["raw"], "plain raw failure")

    def test_valid_observe_introspection_persists_observation_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed = {
                "_parse_ok": True,
                "private": "静かに見守っている",
                "emotion": "calm",
                "topic": "watch",
            }

            should_skip = loop.record_parse_skip_if_needed(
                parsed=parsed,
                response=json.dumps(parsed, ensure_ascii=False),
                log_dir=tmp,
                timestamp="2026-07-15T12:00:00+09:00",
                mode="observe",
            )
            persisted = loop.persist_loop_introspection(
                parsed=parsed,
                mode="observe",
                timestamp="2026-07-15T12:00:00+09:00",
                observation_log=tmp / "observations.jsonl",
                explore_log=tmp / "explore.jsonl",
            )

            self.assertFalse(should_skip)
            self.assertTrue(persisted)
            rows = self.read_jsonl(tmp / "observations.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["private"], "静かに見守っている")
            self.assertEqual(rows[0]["emotion"], "calm")
            self.assertEqual(self.read_jsonl(tmp / "loop_parse_errors.jsonl"), [])

    def test_valid_non_observe_introspection_persists_explore_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed = {
                "_parse_ok": True,
                "private": "記憶の連なりを見ている",
                "emotion": "thoughtful",
                "topic": "memory",
            }

            persisted = loop.persist_loop_introspection(
                parsed=parsed,
                mode="reflect",
                timestamp="2026-07-15T12:00:00+09:00",
                observation_log=tmp / "observations.jsonl",
                explore_log=tmp / "explore.jsonl",
            )

            self.assertTrue(persisted)
            rows = self.read_jsonl(tmp / "explore.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["mode"], "reflect")
            self.assertEqual(rows[0]["topic"], "memory")
            self.assertEqual(rows[0]["private"], "記憶の連なりを見ている")

    def test_empty_introspection_records_skip_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed = {"_parse_ok": True, "private": "", "emotion": ""}

            should_skip = loop.record_parse_skip_if_needed(
                parsed=parsed,
                response="{}",
                log_dir=tmp,
                timestamp="2026-07-15T12:00:00+09:00",
                mode="explore",
            )

            self.assertTrue(should_skip)
            errors = self.read_jsonl(tmp / "loop_parse_errors.jsonl")
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "empty_introspection")


class LoopPyStandaloneRunTests(unittest.TestCase):
    class Result:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def fake_run_factory(self, calls):
        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:2] == ["loops", "list"]:
                return self.Result("なし\n")
            if cmd[:2] == ["loops", "list-json"]:
                return self.Result("[]\n")
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("render-sensors.py"):
                return self.Result("# sensors\n異常なし\n")
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("body-context.py"):
                return self.Result("# 身体位置\nリビング\n")
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("boundary.py"):
                return self.Result('{"allowed": false}\n')
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("feature-flags.py"):
                return self.Result("\n")
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("mcp-config.py"):
                Path(cmd[2]).parent.mkdir(parents=True, exist_ok=True)
                Path(cmd[2]).write_text("{}", encoding="utf-8")
                return self.Result()
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("speak.py"):
                return self.Result()
            if len(cmd) >= 2 and cmd[0] == "python3" and cmd[1].endswith("daybook_rollup.py"):
                return self.Result()
            if cmd and cmd[0] == "curl":
                return self.Result()
            if len(cmd) >= 2 and cmd[0] == "bash" and cmd[1].endswith("invoke-agent.sh"):
                payload = {
                    "topic": "fixture",
                    "private": "静かに確認している",
                    "emotion": "calm",
                    "speak": "あとで見ます",
                    "proposal": None,
                    "feature_presented": None,
                }
                return self.Result(json.dumps(payload, ensure_ascii=False) + "\n")
            return self.Result()
        return fake_run

    def make_env(self, tmp: Path, mode: str) -> dict[str, str]:
        prefs = tmp / "preferences.json"
        prefs.write_text(json.dumps({"speakers": [{"room": "living"}], "cameras": []}), encoding="utf-8")
        character = tmp / "character.md"
        character.write_text("# character\n", encoding="utf-8")
        body_location = tmp / "body_location.json"
        body_location.write_text(json.dumps({"current_entity": ""}), encoding="utf-8")
        workdir = tmp / "workdir"
        workdir.mkdir()
        return {
            "MODE": mode,
            "CLAUDE_BIN": "/bin/claude",
            "EHA_LOG_DIR": str(tmp / "log"),
            "EHA_TMP_DIR": str(tmp / "tmp"),
            "EHA_PREFS_FILE": str(prefs),
            "EHA_CHARACTER_FILE": str(character),
            "EHA_BODY_LOCATION_FILE": str(body_location),
            "EHA_DATA_DIR": str(tmp),
            "EHA_CLAUDE_CWD": str(workdir),
            "EHA_TEST_TIMESTAMP": "2026-07-15T12:00:00+09:00",
            "EHA_TEST_HOUR": "12",
        }

    def test_run_all_modes_with_mocked_external_commands(self):
        for mode in ["observe", "explore", "reflect", "web", "social"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                calls = []
                result = loop.run(self.make_env(tmp, mode), run_subprocess=self.fake_run_factory(calls))

                self.assertEqual(result["mode"], mode)
                self.assertTrue(result["parsed"].get("_parse_ok"))
                invoke_calls = [call for call in calls if len(call[0]) >= 2 and call[0][0] == "bash" and call[0][1].endswith("invoke-agent.sh")]
                self.assertTrue(invoke_calls)
                invoke_cmd, invoke_kwargs = invoke_calls[-1]
                if result["context"]["allowed_tools"]:
                    expected_builtins, expected_mcp_tools = loop._split_allowed_tools_for_invoke_agent(result["context"]["allowed_tools"])
                    if expected_builtins:
                        self.assertEqual(invoke_cmd[invoke_cmd.index("--allowed-builtins") + 1], expected_builtins)
                    if expected_mcp_tools:
                        self.assertEqual(invoke_cmd[invoke_cmd.index("--allowed-mcp-tools") + 1], expected_mcp_tools)
                self.assertIn("--append-system-prompt", invoke_cmd)
                self.assertIn(result["context"]["sys_prompt"], invoke_cmd)
                self.assertNotIn("input", invoke_kwargs)
                self.assertEqual(invoke_cmd[-1], result["context"]["user_prompt"])
                self.assertEqual(invoke_cmd[invoke_cmd.index("--mcp-servers") + 1], " ".join(result["context"]["mcp_servers"]))
                rows = self.read_jsonl(tmp / "log" / ("observations.jsonl" if mode == "observe" else "explore.jsonl"))
                self.assertEqual(rows[0]["private"], "静かに確認している")
                chat_rows = self.read_jsonl(tmp / "log" / "chat_log.jsonl")
                self.assertEqual(chat_rows[-1]["source"], mode)

    def test_eha_session_bin_agy_no_longer_blocks_run(self):
        # #14増分6: EHA_SESSION_BIN=agyでも(既にレガシー変数として無視されるだけで)
        # run()がSystemExitしないことを確認する。
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            calls = []
            env = self.make_env(tmp, "reflect")
            env["EHA_SESSION_BIN"] = "/data/bin/agy"
            result = loop.run(env, run_subprocess=self.fake_run_factory(calls))

            self.assertEqual(result["mode"], "reflect")

    def test_mode_config_matches_loop_sh_mcp_and_allowed_tools(self):
        text = (ROOT / "embodied_ha" / "loop.sh").read_text(encoding="utf-8")
        for mode in ["observe", "explore", "reflect", "web", "social"]:
            with self.subTest(mode=mode):
                start = text.index(f"  {mode})")
                end = text.index("    ;;", start)
                block = text[start:end]
                cfg = loop.mode_config(mode)
                def shell_literal(value):
                    return str(value).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")

                self.assertIn(f'MODE_LABEL="{shell_literal(cfg.label)}"', block)
                self.assertIn(f'TOOLS_DESC="{shell_literal(cfg.tools_desc)}"', block)
                self.assertIn(f'TASK="{shell_literal(cfg.task)}"', block)
                self.assertIn(f'ALLOWED_TOOLS="{cfg.allowed_tools}"', block)
                self.assertIn(f'MCP_SERVERS="{" ".join(cfg.mcp_servers)}"', block)


if __name__ == "__main__":
    unittest.main()
