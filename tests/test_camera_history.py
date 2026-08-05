import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "embodied_ha"))

import camera_history  # noqa: E402


JPEG_A = b"\xff\xd8" + (b"a" * 256) + b"\xff\xd9"
JPEG_B = b"\xff\xd8" + (b"b" * 256) + b"\xff\xd9"


class CameraHistorySettingsTests(unittest.TestCase):
    def test_disabled_by_default_and_retention_is_bounded(self):
        self.assertEqual(camera_history.history_settings({}), (False, 10))
        self.assertEqual(
            camera_history.history_settings(
                {"camera_history_enabled": True, "camera_history_minutes": -20}
            ),
            (True, 1),
        )
        self.assertEqual(
            camera_history.history_settings(
                {"camera_history_enabled": True, "camera_history_minutes": 999}
            ),
            (True, 60),
        )

    def test_camera_sources_support_ha_and_go2rtc_without_duplicates(self):
        prefs = {
            "cameras": [
                {"ha_entity": "camera.living", "source": "living_stream"},
                {"source": "study_capture"},
                {"entity": "camera.entry"},
                {"source": "study_capture"},
                {"label": "missing"},
            ]
        }
        self.assertEqual(
            camera_history.camera_sources(prefs),
            ["camera.living", "study_capture", "camera.entry"],
        )


class CameraHistoryStoreTests(unittest.TestCase):
    def test_store_is_atomic_and_source_names_never_become_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = camera_history.store_frame(
                tmp,
                "../../camera/秘密",
                JPEG_A,
                captured_at=100.25,
            )
            self.assertIsNotNone(record)
            self.assertEqual(record.captured_at, 100.25)
            self.assertEqual(record.path.parent.parent, Path(tmp))
            self.assertNotIn("秘密", str(record.path))
            self.assertNotIn("..", str(record.path.relative_to(tmp)))
            self.assertEqual(record.path.read_bytes(), JPEG_A)
            self.assertEqual(list(Path(tmp).rglob("*.tmp")), [])

    def test_non_jpeg_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = camera_history.store_frame(
                tmp, "camera.living", b"not-a-jpeg", captured_at=100.0
            )
            self.assertIsNone(record)
            self.assertEqual(list(Path(tmp).rglob("*")), [])

    def test_store_rejects_symlinked_history_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outside = base / "outside"
            outside.mkdir()
            root = base / "history"
            root.symlink_to(outside, target_is_directory=True)
            self.assertIsNone(
                camera_history.store_frame(root, "camera.living", JPEG_A)
            )
            self.assertEqual(list(outside.iterdir()), [])

            root.unlink()
            root.mkdir()
            camera_dir = camera_history.camera_directory(root, "camera.living")
            camera_dir.symlink_to(outside, target_is_directory=True)
            self.assertIsNone(
                camera_history.store_frame(root, "camera.living", JPEG_A)
            )
            self.assertEqual(list(outside.iterdir()), [])

    def test_prune_enforces_retention_per_camera_count_and_global_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera_history.store_frame(tmp, "camera.one", JPEG_A, captured_at=100.0)
            camera_history.store_frame(tmp, "camera.one", JPEG_A, captured_at=170.0)
            camera_history.store_frame(tmp, "camera.one", JPEG_B, captured_at=180.0)
            camera_history.store_frame(tmp, "camera.two", JPEG_B, captured_at=190.0)

            removed = camera_history.prune_history(
                tmp,
                retention_minutes=1,
                now=200.0,
                max_frames_per_camera=1,
                max_total_bytes=len(JPEG_B),
            )

            self.assertEqual(removed, 3)
            remaining = list(Path(tmp).rglob("*.jpg"))
            self.assertEqual(len(remaining), 1)
            self.assertIn("190000", remaining[0].name)

    def test_select_frames_spans_requested_range_and_ignores_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            for captured_at in (700.0, 800.0, 900.0, 990.0):
                camera_history.store_frame(
                    tmp, "camera.living", JPEG_A, captured_at=captured_at
                )
            camera_dir = camera_history.camera_directory(tmp, "camera.living")
            (camera_dir / "950000.jpg").symlink_to("/etc/passwd")

            frames = camera_history.select_frames(
                tmp,
                "camera.living",
                start_seconds_ago=300,
                end_seconds_ago=0,
                max_frames=3,
                retention_minutes=10,
                now=1000.0,
            )

            self.assertEqual([frame.captured_at for frame in frames], [700.0, 800.0, 990.0])
            self.assertTrue(all(not frame.path.is_symlink() for frame in frames))

    def test_read_frame_rejects_symlink_after_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = camera_history.store_frame(
                tmp, "camera.living", JPEG_A, captured_at=100.0
            )
            assert record is not None
            record.path.unlink()
            record.path.symlink_to("/etc/passwd")
            with self.assertRaises(OSError):
                camera_history.read_frame(record)


class CameraHistoryWorkerTests(unittest.TestCase):
    def _write_prefs(self, path: Path, data: dict):
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_disabled_cycle_removes_existing_history_without_fetching(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            self._write_prefs(prefs_path, {"camera_history_enabled": False})
            camera_history.store_frame(
                history_root, "camera.living", JPEG_A, captured_at=100.0
            )
            fetched = []
            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(prefs_path),
                history_root=str(history_root),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=lambda source, **kwargs: fetched.append(source) or JPEG_A,
            )

            result = worker.run_cycle(now=200.0)

            self.assertFalse(result["enabled"])
            self.assertEqual(fetched, [])
            self.assertFalse(history_root.exists())

    def test_enabled_cycle_captures_ha_and_go2rtc_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            self._write_prefs(
                prefs_path,
                {
                    "camera_history_enabled": True,
                    "camera_history_minutes": 5,
                    "cameras": [
                        {"ha_entity": "camera.living"},
                        {"source": "study_capture"},
                    ],
                },
            )
            fetched = []
            lock = threading.Lock()

            def fetch(source, **kwargs):
                with lock:
                    fetched.append((source, kwargs))
                return JPEG_A

            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(prefs_path),
                history_root=str(history_root),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=fetch,
            )

            result = worker.run_cycle(now=1000.0)

            self.assertTrue(result["enabled"])
            self.assertEqual(result["captured"], 2)
            self.assertEqual({item[0] for item in fetched}, {"camera.living", "study_capture"})
            self.assertTrue(
                all(item[1]["ha_url"] == "http://ha/api" for item in fetched)
            )
            self.assertTrue(
                all(item[1]["timeout_seconds"] == 4 for item in fetched)
            )
            self.assertEqual(len(list(history_root.rglob("*.jpg"))), 2)

    def test_retry_recovers_one_fetch_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            self._write_prefs(
                prefs_path,
                {"camera_history_enabled": True, "cameras": [{"source": "study"}]},
            )
            responses = iter([None, JPEG_A])
            fetched = []

            def fetch(source, **kwargs):
                fetched.append((source, kwargs))
                return next(responses)

            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(prefs_path),
                history_root=str(history_root),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=fetch,
                retry_delay=0,
            )

            result = worker.run_cycle(now=1000.0)

            self.assertEqual(result["captured"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(len(fetched), 2)
            self.assertEqual(result["failure_events"], [])
            self.assertEqual(
                camera_history.list_frames(history_root, "study")[0].captured_at,
                1000.0,
            )

    def test_runtime_capture_timestamp_is_retrieval_completion_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(Path(tmp) / "unused.json"),
                history_root=str(Path(tmp) / "history"),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=lambda source, **kwargs: JPEG_A,
                retry_delay=0,
            )

            with mock.patch.object(camera_history.time, "time", return_value=1002.5):
                result = worker._capture_source(
                    "study",
                    None,
                    allow_retry=True,
                )

            self.assertEqual(result.status, "captured")
            self.assertEqual(result.captured_at, 1002.5)

    def test_fetch_failures_are_counted_and_recovery_hides_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            source = "camera.secret_room"
            self._write_prefs(
                prefs_path,
                {"camera_history_enabled": True, "cameras": [{"source": source}]},
            )
            responses = iter([None, None, None, None, JPEG_A])
            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(prefs_path),
                history_root=str(history_root),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=lambda _source, **kwargs: next(responses),
                retry_delay=0,
            )

            first = worker.run_cycle(now=1000.0)
            second = worker.run_cycle(now=1010.0)
            recovered = worker.run_cycle(now=1020.0)
            output = io.StringIO()
            with redirect_stdout(output):
                worker._log_cycle_events(first)
                worker._log_cycle_events(second)
                worker._log_cycle_events(recovered)

            self.assertEqual(first["failure_events"][0]["consecutive"], 1)
            self.assertEqual(first["failure_events"][0]["attempts"], 2)
            self.assertEqual(second["failure_events"][0]["consecutive"], 2)
            self.assertEqual(
                recovered["recovery_events"],
                [
                    {
                        "source_index": 1,
                        "reason": "fetch_unavailable",
                        "previous_consecutive": 2,
                    }
                ],
            )
            self.assertIn("source_index=1", output.getvalue())
            self.assertIn("previous_consecutive=2", output.getvalue())
            self.assertNotIn(source, output.getvalue())

    def test_invalid_frame_and_storage_failure_are_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            self._write_prefs(
                prefs_path,
                {"camera_history_enabled": True, "cameras": [{"source": "study"}]},
            )
            fetched = []
            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(prefs_path),
                history_root=str(history_root),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=lambda source, **kwargs: fetched.append(source) or b"not-jpeg",
                retry_delay=0,
            )

            invalid = worker.run_cycle(now=1000.0)
            self.assertEqual(len(fetched), 1)
            self.assertEqual(invalid["failure_events"][0]["reason"], "invalid_frame")

            fetched.clear()
            worker.fetch = lambda source, **kwargs: fetched.append(source) or JPEG_A
            with mock.patch.object(camera_history, "store_frame", return_value=None):
                storage = worker.run_cycle(now=1010.0)
            self.assertEqual(len(fetched), 1)
            self.assertEqual(storage["failure_events"][0]["reason"], "store_failed")

    def test_more_than_four_sources_do_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            sources = [f"camera-{index}" for index in range(5)]
            self._write_prefs(
                prefs_path,
                {
                    "camera_history_enabled": True,
                    "cameras": [{"source": source} for source in sources],
                },
            )
            fetched = []
            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(prefs_path),
                history_root=str(history_root),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=lambda source, **kwargs: fetched.append(source) or None,
                retry_delay=0,
            )

            result = worker.run_cycle(now=1000.0)

            self.assertEqual(result["failed"], 5)
            self.assertEqual(sorted(fetched), sorted(sources))
            self.assertTrue(
                all(event["attempts"] == 1 for event in result["failure_events"])
            )

    def test_tracking_reset_is_reported_on_disable_and_source_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            self._write_prefs(
                prefs_path,
                {"camera_history_enabled": True, "cameras": [{"source": "one"}]},
            )
            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(prefs_path),
                history_root=str(history_root),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=lambda source, **kwargs: None,
                retry_delay=0,
            )

            initial = worker.run_cycle(now=1000.0)
            self.assertEqual(initial["tracking_reset"], "worker_start")
            self._write_prefs(
                prefs_path,
                {"camera_history_enabled": True, "cameras": [{"source": "two"}]},
            )
            changed = worker.run_cycle(now=1010.0)
            self.assertEqual(changed["tracking_reset"], "source_set_changed")
            self._write_prefs(prefs_path, {"camera_history_enabled": False})
            disabled = worker.run_cycle(now=1020.0)
            self.assertEqual(disabled["tracking_reset"], "disabled")

    def test_failure_logging_is_rate_limited_after_gate_boundary(self):
        self.assertTrue(
            all(
                camera_history.CameraHistoryWorker._should_log_failure(count)
                for count in range(1, 7)
            )
        )
        self.assertFalse(
            camera_history.CameraHistoryWorker._should_log_failure(7)
        )
        self.assertTrue(
            camera_history.CameraHistoryWorker._should_log_failure(30)
        )

    def test_unreadable_preferences_clear_existing_history_and_skip_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            prefs_path.write_text("{broken", encoding="utf-8")
            camera_history.store_frame(
                history_root, "camera.living", JPEG_A, captured_at=100.0
            )
            fetched = []
            worker = camera_history.CameraHistoryWorker(
                prefs_file=str(prefs_path),
                history_root=str(history_root),
                ha_url="http://ha/api",
                go2rtc_url="http://go2rtc",
                token="token",
                fetch=lambda source, **kwargs: fetched.append(source) or JPEG_A,
            )

            result = worker.run_cycle(now=200.0)

            self.assertEqual(result["status"], "preferences_unavailable")
            self.assertFalse(result["enabled"])
            self.assertEqual(result["removed"], 1)
            self.assertEqual(fetched, [])
            self.assertFalse(history_root.exists())

    def test_run_from_environment_clears_history_before_worker_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            history_root = Path(tmp) / "history"
            self._write_prefs(prefs_path, {"camera_history_enabled": True})
            camera_history.store_frame(
                history_root, "camera.living", JPEG_A, captured_at=100.0
            )

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "EHA_PREFS_FILE": str(prefs_path),
                        "EHA_CAMERA_HISTORY_DIR": str(history_root),
                        "HA_URL": "http://ha/api",
                        "GO2RTC_BASE": "http://go2rtc",
                        "SUPERVISOR_TOKEN": "token",
                    },
                ),
                mock.patch.object(
                    camera_history.CameraHistoryWorker, "run_forever"
                ) as run_forever,
            ):
                camera_history.run_from_environment()

            self.assertFalse(history_root.exists())
            run_forever.assert_called_once_with()

    def test_daemon_starts_camera_history_with_runtime(self):
        source = (ROOT / "embodied_ha" / "daemon.py").read_text(encoding="utf-8")
        runtime_block = source[
            source.index("def start_runtime_threads"):source.index(
                "def boot_runtime_when_ready"
            )
        ]
        self.assertIn(
            "target=camera_history.run_from_environment",
            runtime_block,
        )


if __name__ == "__main__":
    unittest.main()
