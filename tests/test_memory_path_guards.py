import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import memory_state


class MemoryPathGuardTests(unittest.TestCase):
    def test_valid_components_stay_in_their_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                Path(memory_state.episode_path(tmp, "ep_20260731_example")).parent.name,
                "episodes",
            )
            self.assertEqual(
                Path(memory_state.daybook_path(tmp, "2026-07-31")).parent.name,
                "daybooks",
            )
            self.assertEqual(
                Path(memory_state.causal_chain_path(tmp, "causal_abc")).parent.name,
                "causal_chains",
            )

    def test_path_traversal_and_absolute_paths_are_rejected(self):
        builders = (
            lambda tmp, value: memory_state.episode_path(tmp, value),
            lambda tmp, value: memory_state.daybook_path(tmp, value),
            lambda tmp, value: memory_state.causal_chain_path(tmp, value),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for builder in builders:
                for value in ("../outside", "..\\outside", "/tmp/outside", ".."):
                    with self.subTest(builder=builder, value=value), self.assertRaises(ValueError):
                        builder(tmp, value)

    def test_daybook_date_must_be_real_iso_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("2026-02-30", "31-07-2026", "today"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    memory_state.daybook_path(tmp, value)


if __name__ == "__main__":
    unittest.main()
