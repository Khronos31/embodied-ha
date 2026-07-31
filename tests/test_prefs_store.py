import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import prefs_store


def _increment_many(path: str, count: int) -> None:
    for _ in range(count):
        def increment(prefs):
            prefs["count"] = int(prefs.get("count", 0)) + 1
            return prefs

        prefs_store.update(path, increment)


class PreferencesStoreTests(unittest.TestCase):
    def test_corrupt_existing_file_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "preferences.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(prefs_store.PreferencesReadError):
                prefs_store.update(str(path), lambda _prefs: {"replacement": True})
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_process_concurrent_updates_are_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "preferences.json"
            path.write_text('{"count": 0}', encoding="utf-8")
            processes = [
                multiprocessing.Process(target=_increment_many, args=(str(path), 25))
                for _ in range(4)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["count"], 100)


if __name__ == "__main__":
    unittest.main()
