import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "embodied_ha"))

from loop_shadow_harness import RUNTIME_FILES, capture_runtime_side_effects  # noqa: E402

import loop  # noqa: E402


class LoopMigrationSafetyTests(unittest.TestCase):
    def test_daemon_still_invokes_loop_sh(self):
        daemon = (ROOT / "embodied_ha" / "daemon.py").read_text(encoding="utf-8")

        self.assertIn('LOOP_SH = os.path.join(_SCRIPT_DIR, "loop.sh")', daemon)
        self.assertIn('subprocess.run(["bash", LOOP_SH]', daemon)

    def test_loop_py_main_accepts_forced_mode_without_daemon_wiring(self):
        calls = []
        original_run = loop.run
        try:
            def fake_run(env):
                calls.append(env)
                return {"mode": env.get("MODE")}

            loop.run = fake_run
            loop.main(["--mode", "reflect"])
        finally:
            loop.run = original_run

        self.assertEqual(calls[0]["MODE"], "reflect")

    def test_runtime_contract_doc_covers_shadow_files_and_cutover_blocker(self):
        doc = (ROOT / "docs" / "loop-runtime-contracts.md").read_text(encoding="utf-8")

        for name in RUNTIME_FILES:
            self.assertIn(name, doc)
        self.assertIn("EHA_SESSION_BIN", doc)
        self.assertIn("invoke-agent.sh", doc)
        self.assertIn("not cutover-ready", doc)

    def test_loop_py_blocks_agy_until_invoke_agent_cutover(self):
        with self.assertRaises(SystemExit) as caught:
            loop.run({"EHA_SESSION_BIN": "/data/bin/agy"})

        self.assertIn("EHA_SESSION_BIN=agy", str(caught.exception))
        self.assertIn("invoke-agent.sh", str(caught.exception))

    def test_side_effect_snapshot_normalizes_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "observations.jsonl").write_text(
                json.dumps({"timestamp": "t", "emotion": "calm", "private": "見た"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (root / "pending_proposal.json").write_text(
                json.dumps(
                    {
                        "timestamp": "t",
                        "proposal": "消しましょうか",
                        "action": {"domain": "light", "service": "turn_off", "entity_id": "light.x"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            snapshot = capture_runtime_side_effects(root)

        self.assertEqual(snapshot.files["observations.jsonl"][0]["private"], "見た")
        self.assertEqual(snapshot.files["pending_proposal.json"]["action"]["entity_id"], "light.x")
        self.assertEqual(snapshot.files["explore.jsonl"], [])
        self.assertEqual(snapshot.files["loop_parse_errors.jsonl"], [])
        self.assertEqual(snapshot.files["chat_log.jsonl"], [])


if __name__ == "__main__":
    unittest.main()
