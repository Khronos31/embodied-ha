import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import desire_state  # type: ignore  # noqa: E402


class DesireStateTests(unittest.TestCase):
    def _catalog(self):
        return {
            "check_weather": {
                "growth_rate": 0.033,
                "prompt": "外の天気を確認したい。",
            },
            "curious_about_residents": {
                "growth_rate": 0.08,
                "prompt": "一緒に住む人の様子が気になる。",
            },
            "want_to_reflect": {
                "growth_rate": 0.025,
                "prompt": "少し振り返りたい。",
            },
            "return_to_body": {
                "growth_rate": 0.0,
                "prompt": "なんとなく落ち着く場所に戻りたい。",
                "tags": ["embodiment", "return_to_body"],
            },
            "want_to_stretch": {
                "growth_rate": 0.015,
                "prompt": "同じ場所に留まり続けて、なんだか身体がこわばってきた。ストレッチがてら少し家の中を歩きたい。",
                "tags": ["embodiment", "physical_roam", "stretch"],
            },
            "want_to_roam_remotely": {
                "growth_rate": 0.03,
                "prompt": "今の場所にいたまま、ちょっと自由に飛び回りたい。別の窓から家の様子を覗いてみたい。",
                "tags": ["exploration", "remote_wander", "remote_avatar"],
            },
        }

    def test_load_state_returns_catalog_defaults_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "desire_state.json"
            catalog = desire_state.load_catalog(str(ROOT / "embodied_ha" / "desires.json"))
            state = desire_state.load_state(str(path), catalog=catalog)
            self.assertEqual(state["version"], 1)
            self.assertIn("check_weather", state["desires"])
            self.assertEqual(state["desires"]["check_weather"]["state"], "dormant")
            self.assertEqual(state["desires"]["check_weather"]["priority"], 1.0)

    def test_save_state_round_trips_through_atomic_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "desire_state.json"
            catalog = self._catalog()
            state = desire_state.load_state(str(path), catalog=catalog)
            state = desire_state.stimulate(
                state,
                "check_weather",
                catalog=catalog,
                now=datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc),
            )
            desire_state.save_state(str(path), state, catalog=catalog)
            self.assertTrue(path.exists())
            self.assertFalse(Path(str(path) + ".tmp").exists())
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["desires"]["check_weather"]["state"], "active")
            self.assertGreaterEqual(loaded["desires"]["check_weather"]["charge"], 0.6)

    def test_stimulate_satisfy_and_decay_transition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "desire_state.json"
            catalog = self._catalog()
            base = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
            state = desire_state.load_state(str(path), catalog=catalog)
            state = desire_state.stimulate(state, "check_weather", catalog=catalog, now=base)
            self.assertEqual(state["desires"]["check_weather"]["state"], "active")
            self.assertTrue(state["desires"]["check_weather"]["last_triggered_at"])

            state = desire_state.satisfy(state, "check_weather", catalog=catalog, now=base + timedelta(minutes=1))
            self.assertEqual(state["desires"]["check_weather"]["state"], "satisfied")
            self.assertGreater(state["desires"]["check_weather"]["satisfaction"], 0.5)

            state = desire_state.decay_tick(
                state,
                catalog=catalog,
                now=base + timedelta(hours=8),
            )
            self.assertEqual(state["desires"]["check_weather"]["state"], "dormant")
            self.assertLess(state["desires"]["check_weather"]["satisfaction"], 0.4)

    def test_curiosity_activates_exploration_desire(self):
        catalog = self._catalog()
        base = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)

        low = desire_state.normalize_state(None, catalog)
        low["desires"]["check_weather"]["charge"] = 0.55
        low["desires"]["check_weather"]["state"] = "dormant"

        high = copy.deepcopy(low)

        low = desire_state.decay_tick(
            low,
            catalog=catalog,
            body_state={"curiosity": 0.20, "energy": 0.7, "stress": 0.2},
            now=base,
        )
        high = desire_state.decay_tick(
            high,
            catalog=catalog,
            body_state={"curiosity": 0.95, "energy": 0.7, "stress": 0.2},
            now=base,
        )

        self.assertEqual(low["desires"]["check_weather"]["state"], "dormant")
        self.assertEqual(high["desires"]["check_weather"]["state"], "active")

    def test_legacy_numeric_state_loads_and_pressure_reflects_activity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "desire_state.json"
            catalog = self._catalog()
            path.write_text(
                json.dumps({"check_weather": 0.7, "want_to_reflect": 0.2}, ensure_ascii=False),
                encoding="utf-8",
            )
            state = desire_state.load_state(str(path), catalog=catalog)
            self.assertEqual(state["desires"]["check_weather"]["state"], "active")
            self.assertAlmostEqual(state["desires"]["check_weather"]["charge"], 0.7, places=3)
            self.assertEqual(state["desires"]["want_to_reflect"]["state"], "dormant")

            dormant_pressure = desire_state.compute_pressure(
                desire_state.normalize_state(None, catalog),
                catalog=catalog,
                body_state={"curiosity": 0.2},
            )
            active_pressure = desire_state.compute_pressure(
                state,
                catalog=catalog,
                body_state={"curiosity": 0.95},
            )
            self.assertGreater(active_pressure, dormant_pressure)

    def test_seed_catalog_round_trips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "desires.json"
            dst = Path(tmpdir) / "seeded.json"
            src.write_text(
                json.dumps(
                    {
                        "check_weather": {
                            "growth_rate": 0.033,
                            "prompt": "外の天気を確認したい。",
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            seeded = desire_state.seed_catalog(str(src), str(dst))
            self.assertIn("check_weather", seeded)
            self.assertTrue(dst.exists())
            loaded = json.loads(dst.read_text(encoding="utf-8"))
            self.assertEqual(loaded["check_weather"]["prompt"], "外の天気を確認したい。")

    def test_return_to_body_pressure_activates_embodiment_desire(self):
        catalog = self._catalog()
        state = desire_state.normalize_state(None, catalog)
        state = desire_state.decay_tick(
            state,
            catalog=catalog,
            body_state={"return_to_body_pressure": 0.9, "remote_mode": "remote_avatar", "stress": 0.2, "energy": 0.7},
            now=datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(state["desires"]["return_to_body"]["state"], "active")
        self.assertGreater(desire_state.compute_pressure(state, catalog=catalog, body_state={"return_to_body_pressure": 0.9}), 0.0)

    def test_physical_stretch_prefers_direct_presence(self):
        catalog = self._catalog()
        base = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)
        direct = desire_state.normalize_state(None, catalog)
        remote = desire_state.normalize_state(None, catalog)

        direct = desire_state.decay_tick(
            direct,
            catalog=catalog,
            body_state={"curiosity": 0.3, "energy": 0.8, "stress": 0.2, "remote_mode": ""},
            now=base,
        )
        remote = desire_state.decay_tick(
            remote,
            catalog=catalog,
            body_state={"curiosity": 0.3, "energy": 0.8, "stress": 0.2, "remote_mode": "remote_avatar"},
            now=base,
        )

        self.assertGreater(
            direct["desires"]["want_to_stretch"]["charge"],
            remote["desires"]["want_to_stretch"]["charge"],
        )

    def test_remote_roam_prefers_high_curiosity_and_direct_mode(self):
        catalog = self._catalog()
        base = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)
        low = desire_state.normalize_state(None, catalog)
        high = desire_state.normalize_state(None, catalog)

        low = desire_state.decay_tick(
            low,
            catalog=catalog,
            body_state={"curiosity": 0.2, "energy": 0.7, "stress": 0.2, "remote_mode": ""},
            now=base,
        )
        high = desire_state.decay_tick(
            high,
            catalog=catalog,
            body_state={"curiosity": 0.95, "energy": 0.7, "stress": 0.2, "remote_mode": ""},
            now=base,
        )

        self.assertGreater(
            high["desires"]["want_to_roam_remotely"]["charge"],
            low["desires"]["want_to_roam_remotely"]["charge"],
        )


if __name__ == "__main__":
    unittest.main()
