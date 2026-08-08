import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "scripts" / "f46_matched_pcm_watchdog.sh"


class F46MatchedPcmWatchdogTests(unittest.TestCase):
    def make_fake_commands(self, directory: Path, canary_state: str, emit_marker: bool):
        counter = directory / "log-count"
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
                  if [ "{1 if emit_marker else 0}" = 1 ] && [ "$count" -ge 2 ]; then
                    echo 'F46_MATCHED_CAPTURE_COMPLETE {{"bytes": 1}}'
                  fi
                  exit 0
                fi
                if [ "$1 $2 $3" = "addons info canary" ]; then
                  echo 'state: {canary_state}'
                  exit 0
                fi
                if [ "$1 $2 $3" = "addons info resident" ]; then
                  echo 'state: started'
                  echo 'ip_address: 127.0.0.1'
                  exit 0
                fi
                if [ "$1 $2 $3" = "addons logs resident" ]; then
                  for label in one two three four five; do
                    echo "tcp pull state=streaming label=$label generation=1"
                  done
                  exit 0
                fi
                if [ "$1 $2 $3" = "addons start resident" ]; then
                  echo started > "{directory / 'resident-started'}"
                  exit 0
                fi
                exit 0
                """
            ),
            encoding="utf-8",
        )
        ha.chmod(0o755)
        curl = directory / "curl"
        curl.write_text("#!/bin/sh\nprintf 200\n", encoding="utf-8")
        curl.chmod(0o755)
        return ha, curl

    def run_watchdog(
        self,
        canary_state: str,
        emit_marker: bool,
        *,
        restore_not_before_epoch: int = 0,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ha, curl = self.make_fake_commands(directory, canary_state, emit_marker)
            receipt = directory / "receipt.log"
            environment = {
                **os.environ,
                "HA_BIN": str(ha),
                "CURL_BIN": str(curl),
                "START_GRACE_SECONDS": "0",
                "RESTORE_TIMEOUT_SECONDS": "2",
                "POLL_SECONDS": "0",
                "RESTORE_NOT_BEFORE_EPOCH": str(restore_not_before_epoch),
            }
            result = subprocess.run(
                [str(WATCHDOG), "canary", "resident", "2", str(receipt)],
                check=False,
                text=True,
                capture_output=True,
                env=environment,
                timeout=5,
            )
            return result, receipt.read_text(encoding="utf-8"), (
                directory / "resident-started"
            ).exists()

    def test_capture_marker_restores_resident(self):
        result, receipt, resident_started = self.run_watchdog("started", True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(resident_started)
        self.assertIn("RESTORE_TRIGGER reason=capture_complete", receipt)
        self.assertIn("RESTORE_PASS state=started web=200 tcp_sources=5", receipt)

    def test_canary_start_failure_still_restores_resident(self):
        result, receipt, resident_started = self.run_watchdog("error", False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(resident_started)
        self.assertIn("RESTORE_TRIGGER reason=canary_never_started", receipt)
        self.assertIn("RESTORE_PASS state=started web=200 tcp_sources=5", receipt)

    def test_restore_can_be_deferred_after_capture(self):
        restore_at = int(time.time()) + 1
        result, receipt, resident_started = self.run_watchdog(
            "started",
            True,
            restore_not_before_epoch=restore_at,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(resident_started)
        self.assertIn(f"RESTORE_DEFERRED until_epoch={restore_at}", receipt)
        self.assertIn("RESTORE_PASS state=started web=200 tcp_sources=5", receipt)


if __name__ == "__main__":
    unittest.main()
