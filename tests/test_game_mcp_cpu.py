import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "embodied_ha" / "game-mcp.py"
EMBODIED_HA = ROOT / "embodied_ha"
if str(EMBODIED_HA) not in sys.path:
    sys.path.insert(0, str(EMBODIED_HA))


class FakeKv(dict):
    def similarity(self, word, start):
        return {
            "基準": 1.0,
            "相手一": 0.8,
            "CPU一": 0.6,
            "相手二": 0.4,
            "CPU二": 0.2,
            "近い": 0.9,
        }[word]


def load_game_module():
    name = f"game_mcp_cpu_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._PLUGINS = {"wordvec_race": True, "wiki6": True}
    module._get_kv = lambda: FakeKv({
        "基準": None,
        "相手一": None,
        "CPU一": None,
        "相手二": None,
        "CPU二": None,
        "近い": None,
    })
    return module


def result_json(result):
    return json.loads(result[0][0]["text"])


class WordVecCpuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tempdir = Path(self.temp.name)
        self.record = self.tempdir / "invoke-record.jsonl"
        self.script = self.tempdir / "invoke-agent.sh"
        self.script.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            record = Path(os.environ["CPU_RECORD"])
            with record.open("a", encoding="utf-8") as f:
                f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
            words = os.environ.get("CPU_WORDS", "CPU一").split("・")
            print(words[len(record.read_text(encoding="utf-8").splitlines()) - 1])
            """), encoding="utf-8")
        self.script.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {
            "EHA_DATA_DIR": str(self.tempdir / "data"),
            "EHA_AGENT_HARNESS": "codex",
            "CPU_RECORD": str(self.record),
            "CPU_WORDS": "CPU一・CPU二",
            "PATH": "/usr/bin:/bin",
        }, clear=False)
        self.env.start()
        self.module = load_game_module()
        self.module._SCRIPT_DIR = str(self.tempdir)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def start_cpu_game(self):
        result = result_json(self.module.game_wordvec_race_start({"base": "基準", "mode": "cpu"}))
        self.assertEqual(result["mode"], "cpu")
        return result

    def cpu_move(self, session_id, last, answer, move_count=0):
        return result_json(self.module.game_wordvec_race_cpu_move({
            "cpu_session_id": session_id,
            "start": "基準",
            "last": last,
            "answer": answer,
            "move_count": move_count,
        }))

    def recorded_argv(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def test_codex_cpu_move_uses_invoke_agent_with_lite_rules_and_no_start_cli(self):
        game = self.start_cpu_game()
        self.assertEqual(self.recorded_argv(), [])

        original_run = self.module.subprocess.run
        with mock.patch.object(self.module.subprocess, "run", wraps=original_run) as run:
            response = self.cpu_move(game["cpu_session_id"], "基準", "相手一")

        self.assertFalse(response["game_over"])
        self.assertEqual(response["cpu_move"]["word"], "CPU一")
        self.assertEqual(run.call_args.args[0][0], str(self.script))
        argv = self.recorded_argv()[0]
        self.assertEqual(argv[:4], ["--model", "lite", "--system-prompt", self.module._CPU_RULES])

    def test_prompt_and_persisted_trajectory_survive_module_reimport(self):
        game = self.start_cpu_game()
        session_id = game["cpu_session_id"]
        self.cpu_move(session_id, "基準", "相手一")
        state_path = Path(self.module._cpu_session_path(session_id))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["trajectory"],
            [
                {"word": "相手一", "sim": 0.8, "by": "player"},
                {"word": "CPU一", "sim": 0.6, "by": "cpu"},
            ],
        )

        restarted = load_game_module()
        restarted._SCRIPT_DIR = str(self.tempdir)
        self.module = restarted
        self.cpu_move(session_id, "CPU一", "相手二", move_count=2)

        argv = self.recorded_argv()[1]
        message = argv[-1]
        self.assertIn("1. 相手「相手一」(0.8000)", message)
        self.assertIn("2. あなた(CPU)「CPU一」(0.6000)", message)
        self.assertIn("既出単語(再使用禁止): 相手一・CPU一・相手二", message)
        self.assertIn("直近のバー: 「相手二」(0.4000)", message)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual([move["word"] for move in state["trajectory"]], ["相手一", "CPU一", "相手二", "CPU二"])
        self.assertEqual(state["move_count"], 2)

    def test_terminal_game_deletes_persisted_state(self):
        game = self.start_cpu_game()
        session_id = game["cpu_session_id"]
        self.cpu_move(session_id, "基準", "相手一")
        state_path = Path(self.module._cpu_session_path(session_id))
        self.assertTrue(state_path.exists())

        response = self.cpu_move(session_id, "CPU一", "近い")
        self.assertTrue(response["game_over"])
        self.assertEqual(response["winner"], "cpu")
        self.assertFalse(state_path.exists())

    def test_invalid_session_id_rejected_before_any_file_access(self):
        sentinel = self.tempdir / "data" / "evil.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("{}", encoding="utf-8")

        response = self.cpu_move("../evil", "基準", "近い")

        self.assertIn("cpu_session_id が不正", response["error"])
        self.assertTrue(sentinel.exists())
        self.assertEqual(self.recorded_argv(), [])

    def test_missing_state_file_falls_back_to_args(self):
        game = self.start_cpu_game()
        session_id = game["cpu_session_id"]
        state_path = Path(self.module._cpu_session_path(session_id))
        state_path.unlink()

        response = self.cpu_move(session_id, "基準", "相手一")

        self.assertFalse(response["game_over"])
        self.assertEqual(response["cpu_move"]["word"], "CPU一")
        message = self.recorded_argv()[0][-1]
        self.assertIn("既出単語(再使用禁止): 相手一", message)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual([move["word"] for move in state["trajectory"]], ["相手一", "CPU一"])

    def test_corrupt_state_file_recovers_instead_of_sticking(self):
        game = self.start_cpu_game()
        session_id = game["cpu_session_id"]
        state_path = Path(self.module._cpu_session_path(session_id))
        state_path.write_text("{broken json", encoding="utf-8")

        response = self.cpu_move(session_id, "基準", "相手一")

        self.assertFalse(response.get("game_over"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual([move["word"] for move in state["trajectory"]], ["相手一", "CPU一"])

    def test_replay_of_same_move_does_not_double_append(self):
        game = self.start_cpu_game()
        session_id = game["cpu_session_id"]
        self.cpu_move(session_id, "基準", "相手一")
        self.cpu_move(session_id, "基準", "相手一")

        state = json.loads(
            Path(self.module._cpu_session_path(session_id)).read_text(encoding="utf-8")
        )
        words = [move["word"] for move in state["trajectory"]]
        self.assertEqual(words.count("相手一"), 1)

    def test_no_session_or_resume_flags_in_any_cli_call(self):
        game = self.start_cpu_game()
        session_id = game["cpu_session_id"]
        self.cpu_move(session_id, "基準", "相手一")
        self.cpu_move(session_id, "CPU一", "相手二", move_count=2)

        flags = [arg for argv in self.recorded_argv() for arg in argv]
        self.assertNotIn("--session-id", flags)
        self.assertNotIn("--resume", flags)

    def test_unsupported_harness_returns_polite_error(self):
        with mock.patch.dict(os.environ, {"EHA_AGENT_HARNESS": "unsupported-harness"}):
            result = result_json(self.module.game_wordvec_race_start({"base": "基準", "mode": "cpu"}))

        self.assertEqual(result["error"], "cpu_unsupported_harness")
        self.assertIn("unsupported-harness", result["message"])
        self.assertIn("人間対戦", result["message"])
        self.assertEqual(self.recorded_argv(), [])


if __name__ == "__main__":
    unittest.main()
