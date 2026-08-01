"""render-sensors.pyのpreferences読み込みテスト。"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "embodied_ha" / "render-sensors.py"
    spec = importlib.util.spec_from_file_location("render_sensors_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PreferencesFileTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_module()

    def test_main_closes_preferences_file(self):
        prefs_path = "/tmp/test-preferences.json"
        prefs_open = mock.mock_open(read_data='{"sensors": {"groups": []}}')

        with mock.patch.dict(
            os.environ,
            {"EHA_PREFS_FILE": prefs_path, "HA_URL": "http://example.invalid"},
            clear=False,
        ), mock.patch.object(sys, "argv", ["render-sensors.py"]), mock.patch(
            "builtins.open", prefs_open
        ), mock.patch("builtins.print"):
            self.module.main()

        prefs_open.assert_called_once_with(prefs_path, encoding="utf-8")
        prefs_open.return_value.__enter__.assert_called_once_with()
        prefs_open.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
