import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import anomaly_state as ast  # type: ignore  # noqa: E402


class AnomalyStateTests(unittest.TestCase):
    def _ts(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc)

    def test_missing_state_returns_normalized_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "anomaly_state.json"
            state = ast.load_state_or_default(str(path))

        self.assertEqual(state["version"], ast.STATE_VERSION)
        self.assertEqual(sorted(state["anomalies"].keys()), list(ast.ANOMALY_TYPES))
        self.assertTrue(all(record["resolved"] for record in state["anomalies"].values()))
        self.assertEqual(ast.compute_explore_urgency(state), 0)
        self.assertEqual(ast.format_context_block(state), "（特になし）")

    def test_sensor_spike_detects_resolves_and_round_trips(self):
        base = {"living_room_temp": "20.0", "hall_motion": "clear"}
        changed = {"living_room_temp": "35.0", "hall_motion": "occupied"}

        state = ast.detect_anomalies(base, [], None, now=self._ts(12), trigger_reason="watch")
        state = ast.detect_anomalies(changed, [], state, now=self._ts(12, 5), trigger_reason="watch")

        spike = state["anomalies"]["sensor_spike"]
        self.assertFalse(spike["resolved"])
        self.assertEqual(spike["active_since"], self._ts(12, 5).isoformat(timespec="seconds"))
        self.assertGreater(spike["severity"], 0)
        self.assertGreater(ast.compute_explore_urgency(state, now=self._ts(12, 5)), 0)
        self.assertIn("センサー急変", ast.format_context_block(state))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "anomaly_state.json"
            ast.save_state(str(path), state)
            loaded = ast.load_state(str(path))

        self.assertEqual(loaded["anomalies"]["sensor_spike"]["fingerprint"], spike["fingerprint"])
        self.assertEqual(loaded["anomalies"]["sensor_spike"]["summary"], spike["summary"])

        resolved = ast.detect_anomalies(changed, [], loaded, now=self._ts(12, 10), trigger_reason="watch")
        self.assertTrue(resolved["anomalies"]["sensor_spike"]["resolved"])
        self.assertEqual(ast.compute_explore_urgency(resolved, now=self._ts(12, 10)), 0)
        self.assertEqual(ast.format_context_block(resolved), "（特になし）")

    def test_sensor_spike_ignores_noisy_small_numeric_changes(self):
        base = {"battery_level": "80", "pc_idle_minutes": "300", "living_room_temp": "22.0"}
        changed = {"battery_level": "79", "pc_idle_minutes": "360", "living_room_temp": "23.0"}

        state = ast.detect_anomalies(base, [], None, now=self._ts(10), trigger_reason="watch")
        state = ast.detect_anomalies(changed, [], state, now=self._ts(10, 5), trigger_reason="watch")

        self.assertTrue(state["anomalies"]["sensor_spike"]["resolved"])
        self.assertEqual(ast.compute_explore_urgency(state, now=self._ts(10, 5)), 0)

    def test_stale_urgency_decays_and_new_fingerprint_resets(self):
        base = {"living_room_temp": "20.0"}
        active_since = self._ts(12)
        state = ast.normalize_state(
            {
                "last_sensor_snapshot": base,
                "anomalies": {
                    "sensor_spike": {
                        "type": "sensor_spike",
                        "severity": 1.0,
                        "detected_at": active_since.isoformat(timespec="seconds"),
                        "active_since": active_since.isoformat(timespec="seconds"),
                        "last_seen_at": active_since.isoformat(timespec="seconds"),
                        "resolved": False,
                        "trigger_explore": True,
                        "fingerprint": "old",
                    }
                },
            }
        )

        fresh = ast.compute_explore_urgency(state, now=self._ts(12, 30))
        stale = ast.compute_explore_urgency(state, now=self._ts(20))
        self.assertGreater(fresh, 0)
        self.assertEqual(stale, 0)

        reset = ast.detect_anomalies({"living_room_temp": "30.0"}, [], state, now=self._ts(20), trigger_reason="watch")
        spike = reset["anomalies"]["sensor_spike"]
        self.assertFalse(spike["resolved"])
        self.assertNotEqual(spike["fingerprint"], "old")
        self.assertEqual(spike["active_since"], self._ts(20).isoformat(timespec="seconds"))
        self.assertGreater(ast.compute_explore_urgency(reset, now=self._ts(20)), 0)

    def test_unresolved_loop_age_gate_detects_old_loop_and_resolves(self):
        new_loop = [
            {
                "id": "loop-new",
                "source": "explore",
                "text": "洗濯機の終了後確認",
                "created": "2026-01-01T12:00:00+00:00",
            }
        ]
        old_loop = [{**new_loop[0], "id": "loop-old", "created": "2026-01-01T00:00:00+00:00"}]

        state = ast.detect_anomalies({}, new_loop, None, now=self._ts(13), trigger_reason="watch")
        self.assertTrue(state["anomalies"]["unresolved_loop"]["resolved"])
        self.assertEqual(ast.compute_explore_urgency(state, now=self._ts(13)), 0)

        state = ast.detect_anomalies({}, old_loop, state, now=self._ts(13), trigger_reason="watch")
        record = state["anomalies"]["unresolved_loop"]
        self.assertFalse(record["resolved"])
        self.assertIn("未解決ループ", ast.format_context_block(state))
        self.assertGreater(ast.compute_explore_urgency(state, now=self._ts(13)), 0)

        resolved = ast.detect_anomalies({}, [], state, now=self._ts(13, 10), trigger_reason="watch")
        self.assertTrue(resolved["anomalies"]["unresolved_loop"]["resolved"])
        self.assertEqual(ast.compute_explore_urgency(resolved, now=self._ts(13, 10)), 0)
        self.assertEqual(ast.format_context_block(resolved), "（特になし）")

    def test_world_model_mismatch_removed_and_old_state_dropped(self):
        self.assertNotIn("world_model_mismatch", ast.ANOMALY_TYPES)
        self.assertFalse(hasattr(ast, "_world_model_mismatch_detection"))

        old_state = {
            "anomalies": {
                "sensor_spike": {"type": "sensor_spike", "resolved": True},
                "unresolved_loop": {"type": "unresolved_loop", "resolved": True},
                "world_model_mismatch": {
                    "type": "world_model_mismatch",
                    "resolved": False,
                    "severity": 1.0,
                    "detected_at": "2026-01-01T00:00:00+00:00",
                },
            }
        }

        normalized = ast.normalize_state(old_state)
        self.assertEqual(sorted(normalized["anomalies"].keys()), list(ast.ANOMALY_TYPES))
        self.assertNotIn("world_model_mismatch", normalized["anomalies"])
        self.assertEqual(ast.compute_explore_urgency(normalized, now=self._ts(12)), 0)

    def test_supported_anomalies_can_coexist_without_losing_evidence(self):
        baseline = "living_room_temp: 20.0\nhall_motion: clear"
        changed = "living_room_temp: 35.0\nhall_motion: occupied"
        loops = [
            {
                "id": "loop-3",
                "source": "watch",
                "text": "エアコンのフィルター掃除を近いうちに確認",
                "created": "2026-01-01T00:00:00+00:00",
            }
        ]

        state = ast.detect_anomalies(baseline, loops, None, now=self._ts(15), trigger_reason="watch")
        state = ast.detect_anomalies(changed, loops, state, now=self._ts(15, 5), trigger_reason="watch")

        self.assertFalse(state["anomalies"]["sensor_spike"]["resolved"])
        self.assertFalse(state["anomalies"]["unresolved_loop"]["resolved"])
        self.assertGreater(state["anomalies"]["sensor_spike"]["count"], 0)
        self.assertGreater(state["anomalies"]["unresolved_loop"]["count"], 0)
        self.assertGreater(ast.compute_explore_urgency(state, now=self._ts(15, 5)), 0)
        self.assertIn("センサー急変", ast.format_context_block(state))
        self.assertIn("未解決ループ", ast.format_context_block(state))
        self.assertNotIn("世界モデルのズレ", ast.format_context_block(state))


if __name__ == "__main__":
    unittest.main()
