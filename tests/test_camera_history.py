import json
import os
import tempfile
import threading
import unittest
from pathlib import Path


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
            self.assertEqual(len(list(history_root.rglob("*.jpg"))), 2)

    def test_unreadable_preferences_preserve_existing_history_and_skip_capture(self):
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
            self.assertEqual(fetched, [])
            self.assertEqual(len(list(history_root.rglob("*.jpg"))), 1)

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
