import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import concentrate_hearing_files


class ConcentrateHearingFileLifecycleTests(unittest.TestCase):
    def test_prune_removes_expired_tool_files_without_another_recording(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            stale = directory / "eha-concentrate-hearing-stale.webm"
            fresh = directory / "eha-concentrate-hearing-fresh.webm"
            unrelated = directory / "keep.webm"
            for path in (stale, fresh, unrelated):
                path.write_bytes(b"x")
            os.utime(stale, (100, 100))
            os.utime(fresh, (1000, 1000))

            removed = concentrate_hearing_files.prune_stale_files(
                1000 + concentrate_hearing_files.CONCENTRATE_HEARING_FILE_TTL_SECONDS - 1,
                directory=directory,
            )

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_housekeeping_prunes_before_sleeping(self):
        sleep = mock.Mock(side_effect=RuntimeError("stop"))
        with mock.patch.object(
            concentrate_hearing_files,
            "prune_stale_files",
        ) as prune, self.assertRaises(RuntimeError):
            concentrate_hearing_files.cleanup_forever(sleep_fn=sleep)
        prune.assert_called_once_with()
        sleep.assert_called_once_with(
            concentrate_hearing_files.CONCENTRATE_HEARING_CLEANUP_INTERVAL_SECONDS
        )

    def test_housekeeping_removes_crash_leftover_without_next_recording(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            stale = directory / "eha-concentrate-hearing-crash-leftover.webm"
            stale.write_bytes(b"audio")
            expired = time.time() - (
                concentrate_hearing_files.CONCENTRATE_HEARING_FILE_TTL_SECONDS + 1
            )
            os.utime(stale, (expired, expired))
            with mock.patch.object(
                concentrate_hearing_files,
                "CONCENTRATE_HEARING_DIR",
                directory,
            ), self.assertRaisesRegex(RuntimeError, "stop after first housekeeping pass"):
                concentrate_hearing_files.cleanup_forever(
                    sleep_fn=mock.Mock(
                        side_effect=RuntimeError("stop after first housekeeping pass")
                    )
                )
            self.assertFalse(stale.exists())
