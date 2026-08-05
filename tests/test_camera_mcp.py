import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_camera_mcp_module():
    import sys

    path = ROOT / "embodied_ha" / "camera-mcp.py"
    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("camera_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CameraMcpTests(unittest.TestCase):
    def test_camera_context_uses_preferences_metadata(self):
        camera_mcp = load_camera_mcp_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(
                json.dumps(
                    {
                        "cameras": [
                            {
                                "source": "camera.living",
                                "label": "リビング",
                                "room": "living",
                                "preset": "sofa",
                                "direction": "left",
                                "ptz": {
                                    "left": "button.example_pan_left",
                                    "right": "button.example_pan_right",
                                    "up": "button.example_tilt_up",
                                    "down": "button.example_tilt_down",
                                },
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old = os.environ.get("EHA_PREFS_FILE")
            os.environ["EHA_PREFS_FILE"] = str(prefs)
            try:
                context = camera_mcp.camera_context("camera.living")
            finally:
                if old is None:
                    os.environ.pop("EHA_PREFS_FILE", None)
                else:
                    os.environ["EHA_PREFS_FILE"] = old
        self.assertEqual(context["source"], "camera.living")
        self.assertEqual(context["room"], "living")
        self.assertEqual(context["preset"], "sofa")
        self.assertEqual(context["direction"], "left")
        self.assertTrue(context["timestamp"])

    def test_handle_ptz_uses_camera_specific_button_mapping(self):
        camera_mcp = load_camera_mcp_module()
        camera = {
            "ptz": {
                "left": "button.example_pan_left",
                "right": "button.example_pan_right",
                "up": "button.example_tilt_up",
                "down": "button.example_tilt_down",
            }
        }
        sent = []
        with mock.patch.object(camera_mcp, "press_button", return_value=True) as press_mock, \
             mock.patch.object(camera_mcp, "send", side_effect=sent.append):
            camera_mcp._handle_ptz(camera, "camera.living", "http://supervisor/core/api", "left", 99)
        press_mock.assert_called_once_with("button.example_pan_left", "http://supervisor/core/api")
        self.assertEqual(sent[-1]["result"]["content"][0]["text"], "カメラをleftに向けました")
        self.assertFalse(sent[-1]["result"]["isError"])



    def test_handle_capture_uses_shared_fetch_frame_helper(self):
        camera_mcp = load_camera_mcp_module()
        camera = {"source": "capture_tv"}
        sent = []
        with mock.patch.object(camera_mcp, "fetch_frame", return_value=b"jpeg-bytes") as fetch_mock,              mock.patch.object(camera_mcp, "send", side_effect=sent.append),              mock.patch.object(camera_mcp, "camera_context", return_value={"source": "capture_tv", "timestamp": "2026-06-26T10:00:00+09:00"}),              mock.patch.object(camera_mcp, "get_ha_token", return_value=""):
            camera_mcp._handle_capture(camera, "camera.living", "http://supervisor/core/api", "http://homeassistant.local:1984", 7)
        fetch_mock.assert_called_once_with("capture_tv", ha_url="http://supervisor/core/api", go2rtc_url="http://homeassistant.local:1984", token="")
        payload = sent[-1]["result"]["content"]
        self.assertEqual(payload[0]["type"], "text")
        self.assertEqual(payload[1]["type"], "image")

    def test_handle_watch_media_resolves_single_video_media_without_invasion(self):
        camera_mcp = load_camera_mcp_module()
        sent = []
        prefs = {"video_media": [{"id": "capture_tv", "source": "capture_tv", "room": "living", "label": "テレビ"}]}
        with mock.patch.object(camera_mcp, "_load_prefs", return_value=prefs),              mock.patch.object(camera_mcp, "fetch_frame", return_value=b"jpeg-bytes") as fetch_mock,              mock.patch.object(camera_mcp, "send", side_effect=sent.append),              mock.patch.object(camera_mcp, "get_ha_token", return_value=""):
            camera_mcp._handle_watch_media(None, "http://supervisor/core/api", "http://homeassistant.local:1984", 11)
        fetch_mock.assert_called_once_with("capture_tv", ha_url="http://supervisor/core/api", go2rtc_url="http://homeassistant.local:1984", token="")
        self.assertEqual(sent[-1]["id"], 11)
        text_payload = sent[-1]["result"]["content"][0]["text"]
        self.assertIn('"media_context"', text_payload)
        self.assertIn('"label": "テレビ"', text_payload)
        self.assertIn('record_episode(kind="media_watch"', sent[-1]["result"]["content"][-1]["text"])
        self.assertFalse(sent[-1]["result"].get("isError", False))

    def test_handle_watch_media_errors_for_unknown_source(self):
        camera_mcp = load_camera_mcp_module()
        sent = []
        with mock.patch.object(camera_mcp, "_load_prefs", return_value={}),              mock.patch.object(camera_mcp, "send", side_effect=sent.append):
            camera_mcp._handle_watch_media("missing", "http://supervisor/core/api", "http://homeassistant.local:1984", 12)
        self.assertTrue(sent[-1]["result"]["isError"])
        self.assertIn("未登録です", sent[-1]["result"]["content"][0]["text"])

    def test_main_replies_method_not_found_for_unknown_request_with_id(self):
        camera_mcp = load_camera_mcp_module()
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"protocolVersion": "2026-07-28"}}) + "\n"
        )
        sent = []
        with mock.patch.object(camera_mcp.sys, "argv", ["camera-mcp.py"]), \
             mock.patch.object(camera_mcp.sys, "stdin", stdin), \
             mock.patch.object(camera_mcp, "send", side_effect=sent.append):
            camera_mcp.main()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["id"], 1)
        self.assertEqual(sent[0]["error"]["code"], -32601)
        self.assertIn("server/discover", sent[0]["error"]["message"])

    def test_main_stays_silent_for_unknown_notification_without_id(self):
        camera_mcp = load_camera_mcp_module()
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/unknown"}) + "\n"
        )
        sent = []
        with mock.patch.object(camera_mcp.sys, "argv", ["camera-mcp.py"]), \
             mock.patch.object(camera_mcp.sys, "stdin", stdin), \
             mock.patch.object(camera_mcp, "send", side_effect=sent.append):
            camera_mcp.main()
        self.assertEqual(sent, [])

    def test_main_survives_agy_handshake_sequence(self):
        """Regression test for the agy 1.1.3 hang: server/discover before initialize."""
        camera_mcp = load_camera_mcp_module()
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"protocolVersion": "2026-07-28"}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        ]
        stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
        sent = []
        with mock.patch.object(camera_mcp.sys, "argv", ["camera-mcp.py"]), \
             mock.patch.object(camera_mcp.sys, "stdin", stdin), \
             mock.patch.object(camera_mcp, "send", side_effect=sent.append):
            camera_mcp.main()

        self.assertEqual(len(sent), 3)

        discover_reply = sent[0]
        self.assertEqual(discover_reply["id"], 1)
        self.assertEqual(discover_reply["error"]["code"], -32601)

        initialize_reply = sent[1]
        self.assertEqual(initialize_reply["id"], 2)
        self.assertIn("protocolVersion", initialize_reply["result"])
        self.assertIn("serverInfo", initialize_reply["result"])

        tools_list_reply = sent[2]
        self.assertEqual(tools_list_reply["id"], 3)
        tool_names = {tool["name"] for tool in tools_list_reply["result"]["tools"]}
        self.assertEqual(tool_names, {"use_device_camera", "watch_media"})

    def test_history_tool_is_visible_only_when_both_env_and_preference_enable_it(self):
        camera_mcp = load_camera_mcp_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            prefs.write_text(
                json.dumps({"camera_history_enabled": True}), encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_CAMERA_HISTORY_ENABLED": "1",
                },
                clear=False,
            ):
                tool_names = {tool["name"] for tool in camera_mcp._available_tools()}
                self.assertIn("review_camera_history", tool_names)
                history_tool = next(
                    tool
                    for tool in camera_mcp._available_tools()
                    if tool["name"] == "review_camera_history"
                )
                properties = history_tool["inputSchema"]["properties"]
                self.assertNotIn("source", properties)
                self.assertNotIn("path", properties)

                prefs.write_text(
                    json.dumps({"camera_history_enabled": False}), encoding="utf-8"
                )
                self.assertNotIn(
                    "review_camera_history",
                    {tool["name"] for tool in camera_mcp._available_tools()},
                )

    def test_history_tool_returns_only_current_camera_frames(self):
        camera_mcp = load_camera_mcp_module()
        jpeg = b"\xff\xd8" + (b"history" * 40) + b"\xff\xd9"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "history"
            prefs = Path(tmpdir) / "preferences.json"
            body = Path(tmpdir) / "body_location.json"
            prefs.write_text(
                json.dumps(
                    {
                        "camera_history_enabled": True,
                        "camera_history_minutes": 10,
                        "cameras": [
                            {"source": "camera.living", "label": "リビング"},
                            {"source": "study_capture", "label": "スタディ"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            body.write_text(
                json.dumps({"current_entity": "camera.living"}), encoding="utf-8"
            )
            camera_mcp.camera_history.store_frame(
                root, "camera.living", jpeg, captured_at=time.time() - 30
            )
            camera_mcp.camera_history.store_frame(
                root, "study_capture", jpeg, captured_at=time.time() - 30
            )
            sent = []
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(body),
                    "EHA_CAMERA_HISTORY_ENABLED": "1",
                    "EHA_CAMERA_HISTORY_DIR": str(root),
                },
                clear=False,
            ), mock.patch.object(camera_mcp, "send", side_effect=sent.append):
                camera_mcp._handle_review_camera_history(
                    {"start_seconds_ago": 30, "max_frames": 1}, 77
                )

            result = sent[-1]["result"]
            self.assertFalse(result.get("isError", False))
            image_blocks = [
                block for block in result["content"] if block.get("type") == "image"
            ]
            self.assertEqual(len(image_blocks), 1)
            context = json.loads(result["content"][0]["text"])[
                "camera_history_context"
            ]
            self.assertEqual(context["source"], "camera.living")
            self.assertNotIn("study_capture", result["content"][0]["text"])

    def test_history_tool_rejects_physical_body_and_disabled_direct_call(self):
        camera_mcp = load_camera_mcp_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            body = Path(tmpdir) / "body_location.json"
            prefs.write_text(
                json.dumps({"camera_history_enabled": True}), encoding="utf-8"
            )
            body.write_text("{}", encoding="utf-8")
            sent = []
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(body),
                    "EHA_CAMERA_HISTORY_ENABLED": "1",
                },
                clear=False,
            ), mock.patch.object(camera_mcp, "send", side_effect=sent.append):
                camera_mcp._handle_review_camera_history({}, 78)
            self.assertTrue(sent[-1]["result"]["isError"])
            self.assertIn("物理体", sent[-1]["result"]["content"][0]["text"])

            sent.clear()
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(body),
                    "EHA_CAMERA_HISTORY_ENABLED": "0",
                },
                clear=False,
            ), mock.patch.object(camera_mcp, "send", side_effect=sent.append):
                camera_mcp._handle_review_camera_history({}, 79)
            self.assertTrue(sent[-1]["result"]["isError"])
            self.assertIn("無効", sent[-1]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
