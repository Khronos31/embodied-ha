"""Step4 増分2: default ティアの model/effort 永続(agent_prefs)のテスト。

- module: load/save/env_overrides/validate/set_default_tier。
- run.sh: 実ブロックを抽出して EHA_<H>_MODEL_DEFAULT/EFFORT が配線されること・prefs 不在なら未 export。
- endpoint: POST /api/setup/agent-prefs が保存+self-restart、GET が現在値を返す、検証エラーは 400。
"""
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))
sys.path.insert(0, str(EHA_DIR / "web"))
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import agent_prefs  # noqa: E402
import server  # noqa: E402


class AgentPrefsModuleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(self.enterContext(_TempDir()))
        self._file = self._tmp / "agent_prefs.json"
        self.enterContext(mock.patch.dict(
            os.environ, {"EHA_AGENT_PREFS_FILE": str(self._file)}, clear=False))

    def test_load_missing_returns_empty(self):
        self.assertEqual(agent_prefs.load(), {})

    def test_save_load_round_trip(self):
        prefs = agent_prefs.set_default_tier("claude", model="opus", effort="high")
        agent_prefs.save(prefs)
        self.assertEqual(agent_prefs.load()["default_tier"]["claude"], {"model": "opus", "effort": "high"})

    def test_env_overrides_claude_model_and_effort(self):
        agent_prefs.save(agent_prefs.set_default_tier("claude", model="opus", effort="high"))
        self.assertEqual(agent_prefs.env_overrides("claude"), {
            "EHA_CLAUDE_MODEL_DEFAULT": "opus",
            "EHA_CLAUDE_EFFORT_DEFAULT": "high",
        })

    def test_env_overrides_codex_uses_reasoning_effort_key(self):
        agent_prefs.save(agent_prefs.set_default_tier("codex", model="gpt-5.6-sol", effort="low"))
        self.assertEqual(agent_prefs.env_overrides("codex"), {
            "EHA_CODEX_MODEL_DEFAULT": "gpt-5.6-sol",
            "EHA_CODEX_REASONING_EFFORT_DEFAULT": "low",
        })

    def test_env_overrides_agy_model_only(self):
        agent_prefs.save(agent_prefs.set_default_tier("agy", model="Gemini 3.5 Flash (Low)"))
        self.assertEqual(agent_prefs.env_overrides("agy"), {
            "EHA_AGY_MODEL_DEFAULT": "Gemini 3.5 Flash (Low)",
        })

    def test_env_overrides_empty_when_absent(self):
        self.assertEqual(agent_prefs.env_overrides("claude"), {})
        self.assertEqual(agent_prefs.env_overrides("unknown-harness"), {})

    def test_validate_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            agent_prefs.validate_entry("nope", "m", None)
        with self.assertRaises(ValueError):
            agent_prefs.validate_entry("claude", "", None)
        with self.assertRaises(ValueError):
            agent_prefs.validate_entry("claude", "opus", "extreme")
        with self.assertRaises(ValueError):
            agent_prefs.validate_entry("agy", "m", "high")  # agy に effort は不可

    def test_validate_rejects_control_chars_in_model(self):
        # sol High: 改行/タブ等を含む model を拒否(run.sh の TSV/改行注入を防ぐ)。
        for bad in ("safe\nEHA_EVIL=x", "a\tb", "x\r", "y\x00z"):
            with self.assertRaises(ValueError):
                agent_prefs.validate_entry("claude", bad, None)

    def test_env_overrides_skips_control_char_value(self):
        # 手編集された prefs に制御文字値が入っていても env_overrides は落とす(二層目)。
        agent_prefs.save({"default_tier": {"claude": {"model": "ok\nEHA_EVIL=x"}}})
        self.assertEqual(agent_prefs.env_overrides("claude"), {})

    def test_update_default_tier_merges_and_persists(self):
        agent_prefs.update_default_tier("claude", model="opus")
        agent_prefs.update_default_tier("claude", effort="high")
        self.assertEqual(agent_prefs.load()["default_tier"]["claude"], {"model": "opus", "effort": "high"})

    def test_set_default_tier_merges_without_dropping_other_harness(self):
        prefs = agent_prefs.set_default_tier("claude", model="opus")
        prefs = agent_prefs.set_default_tier("codex", model="gpt-5.6-terra", effort="medium", prefs=prefs)
        self.assertEqual(prefs["default_tier"]["claude"], {"model": "opus"})
        self.assertEqual(prefs["default_tier"]["codex"], {"model": "gpt-5.6-terra", "effort": "medium"})


class RunShAgentPrefsWiringTests(unittest.TestCase):
    RUN_SH = EHA_DIR / "run.sh"
    START = "# 選択ハーネス(未選択時は claude 既定)の default ティア"
    END = "# --- PulseAudio"

    def _extract_block(self) -> str:
        source = self.RUN_SH.read_text(encoding="utf-8")
        start = source.index(self.START)
        end = source.index(self.END, start)
        return source[start:end]

    def _run(self, selected_harness: str, prefs: dict | None) -> dict:
        block = self._extract_block()
        with _TempDir() as temp:
            env_lines = [f"_SELECTED_HARNESS={selected_harness}"]
            if prefs is not None:
                pf = Path(temp) / "agent_prefs.json"
                pf.write_text(json.dumps(prefs), encoding="utf-8")
                env_lines.append(f"export EHA_AGENT_PREFS_FILE={pf}")
            script = "\n".join([
                "set -euo pipefail",
                f"SCRIPT_DIR={EHA_DIR}",
                *env_lines,
                block,
                # 対象キーだけを RESULT: で出力(空白を含む値も1行で拾えるよう key=value 形式)。
                'for _k in EHA_CLAUDE_MODEL_DEFAULT EHA_CLAUDE_EFFORT_DEFAULT '
                'EHA_CODEX_MODEL_DEFAULT EHA_CODEX_REASONING_EFFORT_DEFAULT EHA_AGY_MODEL_DEFAULT; do '
                'if [ -n "${!_k:-}" ]; then echo "RESULT:${_k}=${!_k}"; fi; done',
            ])
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
        result = {}
        for line in out.splitlines():
            if line.startswith("RESULT:"):
                key, _, value = line[len("RESULT:"):].partition("=")
                result[key] = value
        return result

    def test_no_prefs_exports_nothing(self):
        self.assertEqual(self._run("claude", None), {})

    def test_claude_prefs_exported(self):
        prefs = {"default_tier": {"claude": {"model": "opus", "effort": "high"}}}
        self.assertEqual(self._run("claude", prefs), {
            "EHA_CLAUDE_MODEL_DEFAULT": "opus", "EHA_CLAUDE_EFFORT_DEFAULT": "high",
        })

    def test_agy_model_with_spaces_exported(self):
        prefs = {"default_tier": {"agy": {"model": "Gemini 3.5 Flash (Low)"}}}
        self.assertEqual(self._run("agy", prefs), {
            "EHA_AGY_MODEL_DEFAULT": "Gemini 3.5 Flash (Low)",
        })

    def test_only_selected_harness_exported(self):
        # codex 選択なら claude の prefs は export されない(選択ハーネスのみ)。
        prefs = {"default_tier": {"claude": {"model": "opus"}, "codex": {"model": "gpt-5.6-terra"}}}
        self.assertEqual(self._run("codex", prefs), {"EHA_CODEX_MODEL_DEFAULT": "gpt-5.6-terra"})

    def test_injected_control_char_value_does_not_export_extra_env(self):
        # sol High: 改行注入した model があっても、env_overrides が値ごと落とすため
        # EHA_EVIL 等の余計な env は export されない(既知キーも出ない)。
        prefs = {"default_tier": {"claude": {"model": "safe\nEHA_EVIL=pwned"}}}
        result = self._run("claude", prefs)
        self.assertEqual(result, {})
        self.assertNotIn("EHA_EVIL", result)


class AgentPrefsEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(self.enterContext(_TempDir()))
        self.enterContext(mock.patch.dict(os.environ, {
            "EHA_AGENT_PREFS_FILE": str(self._tmp / "agent_prefs.json"),
            "EHA_SETUP_GUARD": "off",  # ingress ガードをテストで無効化
        }, clear=False))

    def _handler(self, path, body_bytes=b""):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.client_address = ("127.0.0.1", 0)
        handler.headers = {"Content-Length": str(len(body_bytes))}
        handler.rfile = io.BytesIO(body_bytes)
        handler.send_json = mock.Mock()
        return handler

    def test_post_saves_and_self_restarts(self):
        body = json.dumps({"harness": "claude", "model": "opus", "effort": "high"}).encode()
        handler = self._handler("/api/setup/agent-prefs", body)
        with mock.patch.object(server, "_schedule_self_restart") as restart:
            handler.do_POST()
        restart.assert_called_once_with()
        response = handler.send_json.call_args.args[0]
        self.assertTrue(response["ok"])
        self.assertTrue(response["restarting"])
        self.assertEqual(agent_prefs.load()["default_tier"]["claude"], {"model": "opus", "effort": "high"})

    def test_post_validation_error_is_400_and_no_restart(self):
        body = json.dumps({"harness": "agy", "model": "m", "effort": "high"}).encode()
        handler = self._handler("/api/setup/agent-prefs", body)
        with mock.patch.object(server, "_schedule_self_restart") as restart:
            handler.do_POST()
        restart.assert_not_called()
        args = handler.send_json.call_args.args
        self.assertEqual(args[1], 400)

    def test_get_returns_default_tier(self):
        agent_prefs.save(agent_prefs.set_default_tier("codex", model="gpt-5.6-terra", effort="medium"))
        handler = self._handler("/api/setup/agent-prefs")
        handler.do_GET()
        response = handler.send_json.call_args.args[0]
        self.assertEqual(response["default_tier"]["codex"], {"model": "gpt-5.6-terra", "effort": "medium"})

    def test_post_non_dict_body_is_400(self):
        # sol Low: object 以外の JSON は 400(500 にしない)。
        handler = self._handler("/api/setup/agent-prefs", json.dumps([1, 2]).encode())
        with mock.patch.object(server, "_schedule_self_restart") as restart:
            handler.do_POST()
        restart.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[1], 400)

    def test_post_control_char_model_is_400(self):
        # sol High: 制御文字を含む model は endpoint で 400。
        body = json.dumps({"harness": "claude", "model": "x\nEHA_EVIL=1"}).encode()
        handler = self._handler("/api/setup/agent-prefs", body)
        with mock.patch.object(server, "_schedule_self_restart") as restart:
            handler.do_POST()
        restart.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[1], 400)


class AgentPrefsGuardTests(unittest.TestCase):
    """sol Med: GET /api/setup/agent-prefs は read なので loopback でも通り、POST は guard される。"""

    def setUp(self):
        self._tmp = Path(self.enterContext(_TempDir()))
        # guard を有効にする(off にしない)。ingress 元は 172.30.32.2 なので loopback は非ingress。
        env = {"EHA_AGENT_PREFS_FILE": str(self._tmp / "agent_prefs.json")}
        env.pop("EHA_SETUP_GUARD", None)
        self.enterContext(mock.patch.dict(os.environ, env, clear=False))
        os.environ.pop("EHA_SETUP_GUARD", None)

    def _handler(self, path, body_bytes=b""):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.client_address = ("127.0.0.1", 0)  # 非ingress(loopback)
        handler.headers = {"Content-Length": str(len(body_bytes))}
        handler.rfile = io.BytesIO(body_bytes)
        handler.send_json = mock.Mock()
        return handler

    def test_get_agent_prefs_is_reachable_from_loopback(self):
        handler = self._handler("/api/setup/agent-prefs")
        handler.do_GET()
        response = handler.send_json.call_args.args[0]
        self.assertIn("default_tier", response)  # 403 でなく実データが返る

    def test_post_agent_prefs_is_guarded_from_loopback(self):
        body = json.dumps({"harness": "claude", "model": "opus"}).encode()
        handler = self._handler("/api/setup/agent-prefs", body)
        with mock.patch.object(server, "_schedule_self_restart") as restart:
            handler.do_POST()
        restart.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[1], 403)


class _TempDir:
    def __enter__(self):
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        return self._d.name

    def __exit__(self, *exc):
        self._d.cleanup()


if __name__ == "__main__":
    unittest.main()
