import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "f46_matched_pcm_replay_guard.sh"


class F46MatchedPcmReplayGuardTests(unittest.TestCase):
    def run_guard(self, *, emit_result: bool, replay_seconds: int):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            counter = directory / "log-count"
            stopped = directory / "stopped"
            ha = directory / "ha"
            ha.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    if [ "$1 $2 $3" = "addons logs canary" ]; then
                      count=0
                      [ ! -f "{counter}" ] || count=$(cat "{counter}")
                      count=$((count + 1))
                      echo "$count" > "{counter}"
                      [ "$count" -lt 3 ] || echo 'F46_MATCHED_CAPTURE_COMPLETE {{}}'
                      if [ "{1 if emit_result else 0}" = 1 ] && [ "$count" -ge 4 ]; then
                        echo 'F46_MATCHED_RESULT {{}}'
                      fi
                      exit 0
                    fi
                    if [ "$1 $2 $3" = "addons info canary" ]; then
                      echo 'state: started'
                      exit 0
                    fi
                    if [ "$1 $2 $3" = "addons stop canary" ]; then
                      echo stopped > "{stopped}"
                      exit 0
                    fi
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            ha.chmod(0o755)
            receipt = directory / "receipt.log"
            result = subprocess.run(
                [str(GUARD), "canary", "2", str(replay_seconds), str(receipt)],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
                env={**os.environ, "HA_BIN": str(ha), "POLL_SECONDS": "0"},
            )
            return result, receipt.read_text(encoding="utf-8"), stopped.exists()

    def test_result_finishes_without_stopping_canary(self):
        result, receipt, stopped = self.run_guard(emit_result=True, replay_seconds=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stopped)
        self.assertIn("REPLAY_GUARD_CAPTURED", receipt)
        self.assertIn("REPLAY_GUARD_RESULT", receipt)

    def test_external_timeout_stops_canary(self):
        result, receipt, stopped = self.run_guard(emit_result=False, replay_seconds=0)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(stopped)
        self.assertIn("REPLAY_GUARD_REPLAY_TIMEOUT", receipt)


if __name__ == "__main__":
    unittest.main()
