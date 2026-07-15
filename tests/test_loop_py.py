import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import loop  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
