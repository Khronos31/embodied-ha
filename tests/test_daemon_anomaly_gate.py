import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
os.environ.setdefault("HA_URL", "http://example.invalid")

import body_state  # type: ignore  # noqa: E402


def _load_daemon_without_boot():
    path = ROOT / "embodied_ha" / "daemon.py"
    source = path.read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType("daemon_under_test")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


daemon = _load_daemon_without_boot()


class DaemonAnomalyGateTests(unittest.TestCase):
    def _schedule(self):
        return {
            "day_probability": 10,
            "late_probability": 10,
            "night_probability": 10,
            "min_probability": 0,
            "anomaly_night_urgency_threshold": 30,
        }

    def _body(self):
        return body_state.normalize_state(None)

    def test_daytime_anomaly_urgency_contributes(self):
        base = daemon.run_chance(self._schedule(), self._body(), "loop", anomaly_urgency=0, hour=12)
        chance = daemon.run_chance(self._schedule(), self._body(), "loop", anomaly_urgency=20, hour=12)
        self.assertEqual(chance - base, 20)

    def test_night_low_anomaly_urgency_is_suppressed(self):
        base = daemon.run_chance(self._schedule(), self._body(), "loop", anomaly_urgency=0, hour=1)
        chance = daemon.run_chance(self._schedule(), self._body(), "loop", anomaly_urgency=20, hour=1)
        self.assertEqual(chance, base)

    def test_night_high_anomaly_urgency_contributes(self):
        base = daemon.run_chance(self._schedule(), self._body(), "loop", anomaly_urgency=0, hour=1)
        chance = daemon.run_chance(self._schedule(), self._body(), "loop", anomaly_urgency=30, hour=1)
        self.assertEqual(chance - base, 30)


if __name__ == "__main__":
    unittest.main()
