import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import state_utils  # type: ignore  # noqa: E402


def load_memory_mcp_module():
    path = ROOT / "embodied_ha" / "memory-mcp.py"
    spec = importlib.util.spec_from_file_location("memory_mcp_lock_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StateUtilsTests(unittest.TestCase):
    def test_camera_capability_supports_manifest_stream_and_legacy_entity(self):
        prefs = {"cameras": [{"source": "living_stream", "label": "リビング"}]}

        stream = state_utils.get_device_capabilities("living_stream", prefs)
        legacy = state_utils.get_device_capabilities("camera.unregistered", prefs)
        other = state_utils.get_device_capabilities("media_player.living", prefs)

        self.assertTrue(stream["is_camera"])
        self.assertEqual(stream["camera"]["label"], "リビング")
        self.assertTrue(legacy["is_camera"])
        self.assertIsNone(legacy["camera"])
        self.assertFalse(other["is_camera"])

    def test_microphone_capability_exposes_canonical_room(self):
        prefs = {
            "mics": [
                {
                    "entity": "study_mic",
                    "source": "rtsp://example.invalid/study",
                    "label": "Study microphone",
                    "room": "study",
                }
            ]
        }

        capabilities = state_utils.get_device_capabilities("study_mic", prefs)

        self.assertTrue(capabilities["is_mic"])
        self.assertEqual(capabilities["mic_room"], "study")
        self.assertEqual(capabilities["mic"]["label"], "Study microphone")

    def test_file_lock_blocks_same_path_but_not_other_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a = Path(tmpdir) / "state-a.json"
            path_b = Path(tmpdir) / "state-b.json"

            same_started = threading.Event()
            same_acquired = threading.Event()
            other_acquired = threading.Event()

            def lock_same_path():
                same_started.set()
                with state_utils.file_lock(str(path_a)):
                    same_acquired.set()

            def lock_other_path():
                with state_utils.file_lock(str(path_b)):
                    other_acquired.set()

            with state_utils.file_lock(str(path_a)):
                same_thread = threading.Thread(target=lock_same_path, daemon=True)
                other_thread = threading.Thread(target=lock_other_path, daemon=True)
                same_thread.start()
                other_thread.start()

                self.assertTrue(same_started.wait(1))
                self.assertTrue(other_acquired.wait(1))
                self.assertFalse(same_acquired.wait(0.2))

            self.assertTrue(same_acquired.wait(1))

    def test_memory_md_appends_survive_concurrent_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_mcp = load_memory_mcp_module()
            memory_mcp.LOG_DIR = tmpdir

            start = threading.Event()
            errors: list[BaseException] = []

            def append_line(line: str):
                try:
                    start.wait(1)
                    memory_mcp._append_memory_line(line)
                except BaseException as exc:  # pragma: no cover - debug aid only
                    errors.append(exc)

            t1 = threading.Thread(target=append_line, args=("- one",), daemon=True)
            t2 = threading.Thread(target=append_line, args=("- two",), daemon=True)
            t1.start()
            t2.start()
            start.set()
            t1.join(2)
            t2.join(2)

            self.assertEqual(errors, [])
            memory_md = Path(tmpdir) / "memory.md"
            content = memory_md.read_text(encoding="utf-8")
            self.assertIn("- one", content)
            self.assertIn("- two", content)


if __name__ == "__main__":
    unittest.main()
