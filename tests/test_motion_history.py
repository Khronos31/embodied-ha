import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "embodied_ha" / "motion-history.py"


def load_module():
    spec = importlib.util.spec_from_file_location("motion_history_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MotionHistoryHttpTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_ha_get_uses_urllib_authorization_header(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            [{"entity_id": "binary_sensor.hall_motion"}],
        ).encode()
        with mock.patch.object(self.module.urllib.request, "urlopen", return_value=response) as urlopen:
            result = self.module._ha_get("http://ha/api/states", "secret-token")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)
        self.assertEqual(result[0]["entity_id"], "binary_sensor.hall_motion")

    def test_ha_get_returns_none_for_transport_and_json_errors(self):
        with mock.patch.object(
            self.module.urllib.request,
            "urlopen",
            side_effect=self.module.urllib.error.URLError("offline"),
        ):
            self.assertIsNone(self.module._ha_get("http://ha/api/states", "token"))

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"not json"
        with mock.patch.object(self.module.urllib.request, "urlopen", return_value=response):
            self.assertIsNone(self.module._ha_get("http://ha/api/states", "token"))


if __name__ == "__main__":
    unittest.main()
