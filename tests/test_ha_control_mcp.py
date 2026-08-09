import datetime
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "embodied_ha" / "ha-control-mcp.py"


def load_ha_control_module():
    module_name = "ha_control_mcp_test"
    sys.modules.pop(module_name, None)
    os.environ.setdefault("HA_URL", "http://127.0.0.1:8123")
    os.environ.setdefault("SUPERVISOR_TOKEN", "test-token")
    pkg = str(ROOT / "embodied_ha")
    if pkg not in sys.path:
        sys.path.insert(0, pkg)  # ha-control-mcp が import する embodied_action 等を解決
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HaControlToolDescriptionTests(unittest.TestCase):
    def test_post_action_reporting_uses_reachable_tools_and_recovery_path(self):
        tool_spec = load_ha_control_module().TOOLS["ha_call_service"]["spec"]
        description = tool_spec["description"]

        self.assertNotIn("reply", description)
        self.assertIn("speak / use_device_speaker", description)
        self.assertIn("move_cyber", description)
        self.assertIn("return_to_body", description)


class _FakeDatetime(datetime.datetime):
    fixed_hour = 12

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 4, cls.fixed_hour, 0, 0, tzinfo=tz)


class HaControlQuietHoursTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["EHA_LOG_DIR"] = self.tmpdir.name
        self.mcp = load_ha_control_module()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _call(self, payload):
        result = self.mcp.ha_call_service(payload)
        if isinstance(result, tuple):
            content, is_error = result
        else:
            content, is_error = result, False
        self.assertIsInstance(content, list)
        self.assertGreater(len(content), 0)
        self.assertEqual(content[0]["type"], "text")
        return content[0]["text"], is_error

    def _patch_time(self, hour):
        fake = type("FakeDatetime", (_FakeDatetime,), {"fixed_hour": hour})
        return mock.patch.object(self.mcp.datetime, "datetime", fake)

    def test_night_blocks_light_turn_on_and_records_denial(self):
        with self._patch_time(2), mock.patch.object(self.mcp, "action_fields_for_control", return_value={}), mock.patch.object(self.mcp.subprocess, "run") as run_mock:
            text, is_error = self._call({"domain": "light", "service": "turn_on", "entity_id": "light.living"})
        self.assertTrue(is_error)
        self.assertIn("深夜帯（1-6時）は、消す系操作とエアコン/扇風機以外の家電操作を控えています。", text)
        run_mock.assert_not_called()
        log_path = Path(self.tmpdir.name) / "actions.jsonl"
        self.assertTrue(log_path.exists())
        record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertFalse(record["ok"])
        self.assertEqual(record["reason"], "深夜帯（1-6時）は、消す系操作とエアコン/扇風機以外の家電操作を控えています。（起こしてしまう操作を避けるため）")
        self.assertEqual(record["domain"], "light")
        self.assertEqual(record["service"], "turn_on")

    def test_night_allows_climate_turn_on(self):
        with self._patch_time(2), mock.patch.object(self.mcp, "action_fields_for_control", return_value={}), mock.patch.object(self.mcp.subprocess, "run", return_value=mock.Mock(returncode=0)) as run_mock, mock.patch.object(self.mcp, "apply_action_to_body_state"):
            text, is_error = self._call({"domain": "climate", "service": "turn_on", "entity_id": "climate.bedroom"})
        self.assertFalse(is_error)
        self.assertIn("実行しました: climate.turn_on climate.bedroom", text)
        run_mock.assert_called_once()

    def test_night_allows_light_turn_off(self):
        with self._patch_time(2), mock.patch.object(self.mcp, "action_fields_for_control", return_value={}), mock.patch.object(self.mcp.subprocess, "run", return_value=mock.Mock(returncode=0)) as run_mock, mock.patch.object(self.mcp, "apply_action_to_body_state"):
            text, is_error = self._call({"domain": "light", "service": "turn_off", "entity_id": "light.living"})
        self.assertFalse(is_error)
        self.assertIn("実行しました: light.turn_off light.living", text)
        run_mock.assert_called_once()

    def test_daytime_allows_light_turn_on(self):
        with self._patch_time(12), mock.patch.object(self.mcp, "action_fields_for_control", return_value={}), mock.patch.object(self.mcp.subprocess, "run", return_value=mock.Mock(returncode=0)) as run_mock, mock.patch.object(self.mcp, "apply_action_to_body_state"):
            text, is_error = self._call({"domain": "light", "service": "turn_on", "entity_id": "light.living"})
        self.assertFalse(is_error)
        self.assertIn("実行しました: light.turn_on light.living", text)
        run_mock.assert_called_once()


class HaControlServiceValidationTests(unittest.TestCase):
    """`service` が無検査で URL へ連結されないこと（F-28① の書き込み版）。

    `domain` は ALLOWED_DOMAINS で守られているが `service` は素通しだったため、
    curl の `..` 正規化で allowlist の外へ **POST** できた。
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["EHA_LOG_DIR"] = self.tmpdir.name
        self.mcp = load_ha_control_module()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _call(self, payload):
        result = self.mcp.ha_call_service(payload)
        content, is_error = result if isinstance(result, tuple) else (result, False)
        return content[0]["text"], is_error

    @mock.patch("subprocess.run")
    def test_traversal_service_is_rejected_before_curl(self, mock_run):
        text, is_error = self._call({
            "domain": "light",
            "service": "../../addons/self/options",
            "entity_id": "light.living",
        })
        self.assertTrue(is_error)
        self.assertIn("拒否", text)
        mock_run.assert_not_called()  # curl まで到達させない

    @mock.patch("subprocess.run")
    def test_slash_in_service_is_rejected(self, mock_run):
        _, is_error = self._call({
            "domain": "script",
            "service": "a/b",
        })
        self.assertTrue(is_error)
        mock_run.assert_not_called()

    @mock.patch("subprocess.run")
    def test_script_direct_call_still_works(self, mock_run):
        # by-design で許可しているスクリプト名の直呼び（entity_id 省略可）を壊さない
        mock_run.return_value = mock.Mock(returncode=0, stdout="")
        fake_daytime = type("FakeDatetime", (_FakeDatetime,), {"fixed_hour": 12})
        with mock.patch.object(self.mcp.datetime, "datetime", fake_daytime):
            _, is_error = self._call({
                "domain": "script",
                "service": "viewing_reservation_set",
                "data": {"reservation_channel": "フジテレビ"},
            })
        self.assertFalse(is_error)
        called_url = mock_run.call_args[0][0][-1]
        self.assertTrue(called_url.endswith("/services/script/viewing_reservation_set"))

    @mock.patch("subprocess.run")
    def test_explicit_entity_id_cannot_be_overridden_by_data(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(self.mcp, "action_fields_for_control", return_value={}), \
             mock.patch.object(self.mcp, "apply_action_to_body_state"):
            _, is_error = self._call({
                "domain": "light",
                "service": "turn_off",
                "entity_id": "light.living",
                "data": {"entity_id": "light.bedroom", "transition": 1},
            })

        self.assertFalse(is_error)
        command = mock_run.call_args.args[0]
        payload = json.loads(command[command.index("-d") + 1])
        self.assertEqual(payload["entity_id"], "light.living")
        record = json.loads(
            (Path(self.tmpdir.name) / "actions.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()[-1]
        )
        self.assertEqual(record["entity_id"], payload["entity_id"])


if __name__ == "__main__":
    unittest.main()
