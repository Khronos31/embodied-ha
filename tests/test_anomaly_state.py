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
        changed = {"living_room_temp": "28.5", "hall_motion": "occupied"}

        state = ast.detect_anomalies(base, [], None, now=self._ts(12), trigger_reason="watch")
        state = ast.detect_anomalies(changed, [], state, now=self._ts(12, 5), trigger_reason="watch")

        spike = state["anomalies"]["sensor_spike"]
        self.assertFalse(spike["resolved"])
        self.assertGreater(spike["severity"], 0)
        self.assertGreater(ast.compute_explore_urgency(state), 0)
        self.assertIn("センサー急変", ast.format_context_block(state))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "anomaly_state.json"
            ast.save_state(str(path), state)
            loaded = ast.load_state(str(path))

        self.assertEqual(loaded["anomalies"]["sensor_spike"]["fingerprint"], spike["fingerprint"])
        self.assertEqual(loaded["anomalies"]["sensor_spike"]["summary"], spike["summary"])

        resolved = ast.detect_anomalies(changed, [], loaded, now=self._ts(12, 10), trigger_reason="watch")
        self.assertTrue(resolved["anomalies"]["sensor_spike"]["resolved"])
        self.assertEqual(ast.compute_explore_urgency(resolved), 0)
        self.assertEqual(ast.format_context_block(resolved), "（特になし）")

    def test_unresolved_loop_detects_and_resolves(self):
        loops = [
            {
                "id": "loop-1",
                "source": "explore",
                "text": "洗濯機の終了後確認",
                "created": "2026-01-01T09:00:00+00:00",
            }
        ]

        state = ast.detect_anomalies({}, loops, None, now=self._ts(13), trigger_reason="watch")
        record = state["anomalies"]["unresolved_loop"]
        self.assertFalse(record["resolved"])
        self.assertIn("未解決ループ", ast.format_context_block(state))
        self.assertGreater(ast.compute_explore_urgency(state), 0)

        resolved = ast.detect_anomalies({}, [], state, now=self._ts(13, 10), trigger_reason="watch")
        self.assertTrue(resolved["anomalies"]["unresolved_loop"]["resolved"])
        self.assertEqual(ast.compute_explore_urgency(resolved), 0)
        self.assertEqual(ast.format_context_block(resolved), "（特になし）")

    def test_world_model_mismatch_detects_and_resolves(self):
        loops = [
            {
                "id": "loop-2",
                "source": "explore",
                "text": "玄関ドアは open のままのはず",
                "created": "2026-01-01T10:00:00+00:00",
            }
        ]

        state = ast.detect_anomalies("玄関ドアは closed です", loops, None, now=self._ts(14), trigger_reason="watch")
        mismatch = state["anomalies"]["world_model_mismatch"]
        self.assertFalse(mismatch["resolved"])
        self.assertIn("世界モデルのズレ", ast.format_context_block(state))
        self.assertGreater(ast.compute_explore_urgency(state), 0)

        resolved = ast.detect_anomalies("玄関ドアは closed です", [], state, now=self._ts(14, 10), trigger_reason="watch")
        self.assertTrue(resolved["anomalies"]["world_model_mismatch"]["resolved"])
        self.assertEqual(ast.compute_explore_urgency(resolved), 0)
        self.assertEqual(ast.format_context_block(resolved), "（特になし）")

    def test_all_anomalies_can_coexist_without_losing_evidence(self):
        baseline = "living_room_temp: 20.0\nhall_motion: clear\n玄関ドア: open"
        changed = "living_room_temp: 29.0\nhall_motion: occupied\n玄関ドア: closed"
        loops = [
            {
                "id": "loop-3",
                "source": "watch",
                "text": "玄関ドアは open のままのはず",
                "created": "2026-01-01T11:00:00+00:00",
            }
        ]

        state = ast.detect_anomalies(baseline, loops, None, now=self._ts(15), trigger_reason="watch")
        state = ast.detect_anomalies(changed, loops, state, now=self._ts(15, 5), trigger_reason="watch")

        self.assertFalse(state["anomalies"]["sensor_spike"]["resolved"])
        self.assertFalse(state["anomalies"]["unresolved_loop"]["resolved"])
        self.assertFalse(state["anomalies"]["world_model_mismatch"]["resolved"])
        self.assertGreater(state["anomalies"]["sensor_spike"]["count"], 0)
        self.assertGreater(state["anomalies"]["unresolved_loop"]["count"], 0)
        self.assertGreater(state["anomalies"]["world_model_mismatch"]["count"], 0)
        self.assertGreater(ast.compute_explore_urgency(state), 0)
        self.assertIn("センサー急変", ast.format_context_block(state))
        self.assertIn("未解決ループ", ast.format_context_block(state))
        self.assertIn("世界モデルのズレ", ast.format_context_block(state))


if __name__ == "__main__":
    unittest.main()
