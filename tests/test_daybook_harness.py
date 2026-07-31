"""F-51: daybook生成が選択ハーネス契約を使い、失敗時に状態を進めない。"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "embodied_ha" / "daybook_rollup.py"
SPEC = importlib.util.spec_from_file_location("daybook_rollup_f51", SCRIPT)
assert SPEC and SPEC.loader
daybook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daybook)
sys.path.insert(0, str(ROOT / "embodied_ha"))
import loop as loop_module  # noqa: E402


def valid_draft() -> dict:
    return {
        "summary": "静かな一日だった。",
        "themes": ["観察"],
        "highlights": [
            {
                "summary": "窓辺を見た",
                "detail": "午後に窓辺を確認した。",
                "tags": ["日常"],
                "importance": 0.4,
            }
        ],
        "open_questions": [],
        "episodes": [
            {
                "timestamp": "2026-07-30T12:00:00+09:00",
                "kind": "observation",
                "source": "loop",
                "summary": "窓辺を見た",
                "detail": "午後に窓辺を確認した。",
                "tags": ["日常"],
                "entities": [],
                "actors": [],
                "importance": 0.4,
                "evidence": [
                    {
                        "timestamp": "2026-07-30T12:00:00+09:00",
                        "private": "明るい。",
                    }
                ],
                "status": "canonical",
                "links": {"causes": [], "effects": []},
            }
        ],
    }


class Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class DaybookHarnessTests(unittest.TestCase):
    def rollup_env(self, log_dir: Path, *, today: str, last: str) -> dict[str, str]:
        return {
            "LOG_FILE": str(log_dir / "observations.jsonl"),
            "MEMORY_FILE": str(log_dir / "memory.md"),
            "TODAY": today,
            "DAYBOOK_MARKER": str(log_dir / ".last_daybook"),
            "LAST_DAYBOOK": last,
            "CONSOLIDATE_MEMORY": "0",
            "CHARACTER": "",
            "RESIDENT": "住人",
        }

    def test_selected_harness_wrapper_contract_preserves_prompt(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return Result(json.dumps(valid_draft(), ensure_ascii=False))

        entries = [
            {
                "timestamp": "2026-07-30T12:00:00+09:00",
                "emotion": "calm",
                "private": "明るい。",
                "speak": "",
            }
        ]
        with mock.patch.dict(
            os.environ,
            {"EHA_AGENT_CWD": "/tmp", "EHA_AGENT_HARNESS": "codex", "RESIDENT": "住人"},
            clear=False,
        ):
            result = daybook._summarize_with_agent("2026-07-30", entries, run=fake_run)

        self.assertEqual(result, valid_draft())
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[:2], ["bash", str(ROOT / "embodied_ha" / "invoke-agent.sh")])
        self.assertEqual(cmd[cmd.index("--model") + 1], "default")
        self.assertIn("--no-tools", cmd)
        self.assertEqual(cmd[cmd.index("--agent-site") + 1], "daybook")
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), daybook.daybook_schema())
        self.assertNotIn("--mcp-servers", cmd)
        self.assertNotIn("--allowed-builtins", cmd)
        self.assertEqual(kwargs["cwd"], "/tmp")
        self.assertIn("2026-07-30 の観察ログ", kwargs["input"])
        self.assertIn("対象の一日は 住人 さん", kwargs["input"])
        self.assertIn("12:00 [calm] 明るい。", kwargs["input"])

    def test_nonzero_empty_and_schema_mismatch_are_errors(self):
        entries = [{"timestamp": "2026-07-30T12:00:00+09:00", "private": "明るい。"}]
        cases = [
            Result(stderr="authentication failed", returncode=1),
            Result(stdout="", returncode=0),
            Result(
                stdout=json.dumps(
                    {
                        "summary": "x",
                        "themes": [],
                        "highlights": [],
                        "open_questions": [],
                        "episodes": [{}],
                    }
                ),
                returncode=0,
            ),
        ]
        for result in cases:
            with self.subTest(result=result.__dict__), self.assertRaises(daybook.DaybookAgentError):
                daybook._summarize_with_agent(
                    "2026-07-30", entries, run=lambda *args, _result=result, **kwargs: _result
                )

    def test_agent_failure_writes_no_daybook_state_or_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            log_dir = data_dir / "log"
            log_dir.mkdir()
            observations = log_dir / "observations.jsonl"
            observations.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-30T12:00:00+09:00",
                        "emotion": "calm",
                        "private": "明るい。",
                        "speak": "",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            marker = log_dir / ".last_daybook"
            marker.write_text("2026-07-29", encoding="utf-8")
            memory = log_dir / "memory.md"
            env = {
                "LOG_FILE": str(observations),
                "MEMORY_FILE": str(memory),
                "TODAY": "2026-07-31",
                "DAYBOOK_MARKER": str(marker),
                "LAST_DAYBOOK": "2026-07-29",
                "CONSOLIDATE_MEMORY": "1",
                "CHARACTER": "",
                "RESIDENT": "住人",
            }

            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(
                    daybook,
                    "_summarize_with_agent",
                    side_effect=daybook.DaybookAgentError("failed"),
                ),
                self.assertRaises(daybook.DaybookAgentError),
            ):
                daybook.main()

            self.assertEqual(marker.read_text(encoding="utf-8"), "2026-07-29")
            self.assertFalse(memory.exists())
            self.assertFalse((log_dir / "memory" / "daybooks").exists())
            self.assertFalse((log_dir / "memory" / "episodes").exists())

    def test_more_than_seven_days_skips_old_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "log"
            log_dir.mkdir()
            rows = [
                {"timestamp": "2026-07-20T12:00:00+09:00", "private": "古い観察"},
                {"timestamp": "2026-07-30T12:00:00+09:00", "private": "新しい観察"},
            ]
            (log_dir / "observations.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            marker = log_dir / ".last_daybook"
            marker.write_text("2026-07-19", encoding="utf-8")
            called_days = []

            def summarize(day, entries):
                called_days.append(day)
                return valid_draft()

            with (
                mock.patch.dict(
                    os.environ,
                    self.rollup_env(log_dir, today="2026-08-01", last="2026-07-19"),
                    clear=False,
                ),
                mock.patch.object(daybook, "_summarize_with_agent", side_effect=summarize),
            ):
                daybook.main()

            self.assertEqual(called_days, ["2026-07-30"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "2026-07-30")
            self.assertFalse(daybook.ms.daybook_exists(str(log_dir), "2026-07-20"))
            self.assertTrue(daybook.ms.daybook_exists(str(log_dir), "2026-07-30"))

    def test_partial_write_reuses_staged_draft_without_duplicate_episodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "log"
            log_dir.mkdir()
            (log_dir / "observations.jsonl").write_text(
                json.dumps(
                    {"timestamp": "2026-07-30T12:00:00+09:00", "private": "明るい。"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            marker = log_dir / ".last_daybook"
            marker.write_text("2026-07-29", encoding="utf-8")
            env = self.rollup_env(log_dir, today="2026-07-31", last="2026-07-29")
            original_write_daybook = daybook._write_daybook

            def fail_after_episodes(log_dir_arg, memory_file, target_day, draft, entries):
                daybook._save_episodes(log_dir_arg, target_day, draft, entries)
                raise OSError("injected after episodes")

            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(daybook, "_summarize_with_agent", return_value=valid_draft()),
                mock.patch.object(daybook, "_write_daybook", side_effect=fail_after_episodes),
                self.assertRaises(OSError),
            ):
                daybook.main()

            stage = Path(daybook._draft_stage_path(str(log_dir), "2026-07-30"))
            self.assertTrue(stage.exists())
            first_episode_paths = sorted((log_dir / "memory" / "episodes").glob("*.json"))
            self.assertEqual(len(first_episode_paths), 1)
            self.assertEqual(marker.read_text(encoding="utf-8"), "2026-07-29")

            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(
                    daybook,
                    "_summarize_with_agent",
                    side_effect=AssertionError("staged draft should be reused"),
                ),
                mock.patch.object(daybook, "_write_daybook", wraps=original_write_daybook),
            ):
                daybook.main()

            second_episode_paths = sorted((log_dir / "memory" / "episodes").glob("*.json"))
            self.assertEqual(second_episode_paths, first_episode_paths)
            self.assertFalse(stage.exists())
            self.assertTrue(daybook.ms.daybook_exists(str(log_dir), "2026-07-30"))
            self.assertEqual(marker.read_text(encoding="utf-8"), "2026-07-30")

    def test_loop_records_daybook_failure_without_marking_artifact_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "log"
            log_dir.mkdir()
            observations = log_dir / "observations.jsonl"
            observations.write_text('{"timestamp":"2026-07-30T12:00:00+09:00"}\n', encoding="utf-8")
            marker = log_dir / ".last_daybook"
            marker.write_text("2026-07-29", encoding="utf-8")
            paths = loop_module.LoopPaths(
                log_dir=str(log_dir),
                observation_log=str(observations),
                explore_log=str(log_dir / "explore.jsonl"),
                chat_log=str(log_dir / "chat.jsonl"),
                memory_file=str(log_dir / "memory.md"),
                pending_file=str(log_dir / "pending.json"),
                daybook_marker=str(marker),
                tmp_dir=str(Path(tmp) / "tmp"),
            )

            with (
                mock.patch.object(loop_module.invoke_failure, "record_failure") as record_failure,
                mock.patch.object(loop_module, "_selected_harness", return_value="codex"),
            ):
                success = loop_module.maybe_run_daybook(
                    paths,
                    {"EHA_LOG_DIR": str(log_dir)},
                    "2026-07-31",
                    run=lambda *args, **kwargs: Result(stderr="schema mismatch", returncode=1),
                )

            self.assertFalse(success)
            self.assertEqual(marker.read_text(encoding="utf-8"), "2026-07-29")
            record_failure.assert_called_once()
            kwargs = record_failure.call_args.kwargs
            self.assertEqual(kwargs["source"], "daybook")
            self.assertEqual(kwargs["mode"], "rollup")
            self.assertEqual(kwargs["harness"], "codex")
            self.assertEqual(kwargs["returncode"], 1)


if __name__ == "__main__":
    unittest.main()
