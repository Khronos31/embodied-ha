import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "embodied_ha"
LOOP_SH = SCRIPT_DIR / "loop.sh"


def _loop_persistence_branch() -> str:
    text = LOOP_SH.read_text(encoding="utf-8")
    start = text.index('SPEAK=""; SPEAK_ROOM')
    end = text.index("\n\nPROPOSAL=", start)
    return text[start:end]


class LoopPersistenceGateTests(unittest.TestCase):
    def run_branch(self, *, mode: str, parsed: dict, response: str = "agent raw output") -> Path:
        tmp = Path(tempfile.mkdtemp())
        parsed_file = tmp / "parsed.json"
        parsed_file.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
        harness = tmp / "harness.sh"
        harness.write_text(
            "\n".join(
                [
                    "#!/bin/bash",
                    "set -euo pipefail",
                    f"SCRIPT_DIR={json.dumps(str(SCRIPT_DIR))}",
                    f"LOG_DIR={json.dumps(str(tmp))}",
                    f"PARSED_FILE={json.dumps(str(parsed_file))}",
                    f"RESPONSE={json.dumps(response)}",
                    f"MODE={json.dumps(mode)}",
                    "TIMESTAMP=2026-07-15T12:00:00+09:00",
                    f"OBSERVATION_LOG={json.dumps(str(tmp / 'observations.jsonl'))}",
                    f"EXPLORE_LOG={json.dumps(str(tmp / 'explore.jsonl'))}",
                    f"FACTS_FILE={json.dumps(str(tmp / 'facts.json'))}",
                    "PROJECTED_CAMERA_SOURCE=",
                    _loop_persistence_branch(),
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(["bash", str(harness)], check=True, cwd=str(ROOT))
        return tmp

    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_parse_failure_observe_writes_only_parse_error(self):
        tmp = self.run_branch(
            mode="observe",
            parsed={"_parse_ok": False, "private": "raw fallback text"},
            response="not json from external agent",
        )

        self.assertEqual(self.read_jsonl(tmp / "observations.jsonl"), [])
        errors = self.read_jsonl(tmp / "loop_parse_errors.jsonl")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "json_parse_failed")
        self.assertEqual(errors[0]["raw"], "not json from external agent")

    def test_parse_failure_non_observe_writes_only_parse_error(self):
        tmp = self.run_branch(
            mode="explore",
            parsed={"_parse_ok": False, "private": "raw fallback text"},
            response="tool error raw text",
        )

        self.assertEqual(self.read_jsonl(tmp / "explore.jsonl"), [])
        errors = self.read_jsonl(tmp / "loop_parse_errors.jsonl")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "json_parse_failed")
        self.assertEqual(errors[0]["raw"], "tool error raw text")

    def test_valid_observe_introspection_is_still_persisted(self):
        tmp = self.run_branch(
            mode="observe",
            parsed={"_parse_ok": True, "private": "静かに観察している", "emotion": "calm"},
        )

        rows = self.read_jsonl(tmp / "observations.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["private"], "静かに観察している")
        self.assertEqual(rows[0]["emotion"], "calm")
        self.assertEqual(self.read_jsonl(tmp / "loop_parse_errors.jsonl"), [])

    def test_valid_non_observe_introspection_is_still_persisted(self):
        tmp = self.run_branch(
            mode="reflect",
            parsed={"_parse_ok": True, "private": "記憶を整理している", "emotion": "thoughtful", "topic": "memory"},
        )

        rows = self.read_jsonl(tmp / "explore.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "reflect")
        self.assertEqual(rows[0]["private"], "記憶を整理している")
        self.assertEqual(rows[0]["topic"], "memory")
        self.assertEqual(self.read_jsonl(tmp / "loop_parse_errors.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
