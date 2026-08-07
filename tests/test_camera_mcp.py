import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_camera_mcp_module():
    path = ROOT / "embodied_ha" / "camera-mcp.py"
    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("camera_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def unpack_mcp_result(result):
    if isinstance(result, tuple):
        return result
    return result, False


def run_main(camera_mcp, requests):
    stdin = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
    stdout = io.StringIO()
    with mock.patch.object(sys, "argv", ["camera-mcp.py"]), \
         mock.patch.object(sys, "stdin", stdin), \
         redirect_stdout(stdout):
        camera_mcp.main()
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


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

    def test_same_room_camera_context_without_projection_remains_direct(self):
        camera_mcp = load_camera_mcp_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prefs = root / "preferences.json"
            location = root / "body_location.json"
            graph = root / "floorplan_room_graph_draft.json"
            prefs.write_text(
                json.dumps(
                    {
                        "cameras": [
                            {
                                "source": "camera.study",
                                "label": "スタディカメラ",
                                "room": "study",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            location.write_text(
                json.dumps({"current_room": "study"}), encoding="utf-8"
            )
            graph.write_text(
                json.dumps(
                    {
                        "rooms": {"study": {"display_name": "スタディ"}},
                        "edges": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(location),
                    "EHA_ROOM_GRAPH_FILE": str(graph),
                },
                clear=False,
            ):
                context = camera_mcp.camera_context("camera.study")

        self.assertEqual(context["sensory_origin"], "direct")
        self.assertEqual(context["access_mode"], "direct")
        self.assertEqual(context["action_mode"], "direct_in_room")

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
        with mock.patch.object(camera_mcp, "press_button", return_value=True) as press_mock:
            content, is_error = unpack_mcp_result(
                camera_mcp._handle_ptz(
                    camera, "camera.living", "http://supervisor/core/api", "left"
                )
            )
        press_mock.assert_called_once_with("button.example_pan_left", "http://supervisor/core/api")
        self.assertEqual(content[0]["text"], "カメラをleftに向けました")
        self.assertFalse(is_error)

    def test_handle_capture_uses_shared_fetch_frame_helper(self):
        camera_mcp = load_camera_mcp_module()
        camera = {"source": "capture_tv"}
        with mock.patch.object(camera_mcp, "fetch_frame", return_value=b"jpeg-bytes") as fetch_mock, \
             mock.patch.object(camera_mcp, "camera_context", return_value={"source": "capture_tv", "timestamp": "2026-06-26T10:00:00+09:00"}), \
             mock.patch.object(camera_mcp, "_camera_projection_still_active", return_value=True), \
             mock.patch.object(camera_mcp, "get_ha_token", return_value=""):
            content, is_error = unpack_mcp_result(
                camera_mcp._handle_capture(
                    camera,
                    "camera.living",
                    "http://supervisor/core/api",
                    "http://homeassistant.local:1984",
                )
            )
        fetch_mock.assert_called_once_with("capture_tv", ha_url="http://supervisor/core/api", go2rtc_url="http://homeassistant.local:1984", token="")
        self.assertFalse(is_error)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image")

    def test_projected_camera_capture_preserves_projection_state(self):
        camera_mcp = load_camera_mcp_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prefs = root / "preferences.json"
            location = root / "body_location.json"
            state = root / "body_state.json"
            graph = root / "floorplan_room_graph_draft.json"
            prefs.write_text(
                json.dumps(
                    {
                        "cameras": [
                            {
                                "source": "camera.study",
                                "label": "スタディカメラ",
                                "room": "study",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            location.write_text(
                json.dumps(
                    {
                        "current_room": "study",
                        "projected_room": "study",
                        "current_entity": "camera.study",
                    }
                ),
                encoding="utf-8",
            )
            before = {
                "remote_mode": "remote_avatar",
                "remote_room": "study",
                "remote_since": "2026-08-07T10:00:00+09:00",
                "remote_avatar_host": "camera.study",
                "embodiment_tension": 0.4,
                "return_to_body_pressure": 0.5,
            }
            state.write_text(json.dumps(before), encoding="utf-8")
            graph.write_text(
                json.dumps(
                    {
                        "rooms": {"study": {"display_name": "スタディ"}},
                        "edges": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(location),
                    "EHA_BODY_STATE_FILE": str(state),
                    "EHA_ROOM_GRAPH_FILE": str(graph),
                },
                clear=False,
            ), mock.patch.object(
                camera_mcp, "fetch_frame", return_value=b"jpeg-bytes"
            ), mock.patch.object(camera_mcp, "get_ha_token", return_value=""):
                content, is_error = unpack_mcp_result(
                    camera_mcp._handle_use_device_camera(
                        {"action": "capture"},
                        "http://supervisor/core/api",
                        "http://homeassistant.local:1984",
                    )
                )

            self.assertFalse(is_error)
            context = json.loads(content[0]["text"])["camera_context"]
            self.assertEqual(context["sensory_origin"], "cyber_direct")
            self.assertEqual(context["access_mode"], "cyber_direct")
            self.assertEqual(context["action_mode"], "cyber_in_room")
            saved = json.loads(state.read_text(encoding="utf-8"))
            for key, value in before.items():
                self.assertEqual(saved[key], value)

    def test_capture_rejects_projection_change_after_frame_fetch(self):
        camera_mcp = load_camera_mcp_module()
        camera = {"source": "camera.study"}
        with mock.patch.object(
            camera_mcp, "fetch_frame", return_value=b"jpeg-bytes"
        ), mock.patch.object(
            camera_mcp, "_camera_projection_still_active", return_value=False
        ), mock.patch.object(camera_mcp, "apply_action_to_body_state") as apply_mock:
            content, is_error = unpack_mcp_result(
                camera_mcp._handle_capture(
                    camera,
                    "camera.study",
                    "http://supervisor/core/api",
                    "http://homeassistant.local:1984",
                )
            )

        self.assertTrue(is_error)
        self.assertIn("侵入状態が変わりました", content[0]["text"])
        self.assertFalse(any(block.get("type") == "image" for block in content))
        apply_mock.assert_not_called()

    def test_capture_rejects_current_entity_without_projected_room(self):
        camera_mcp = load_camera_mcp_module()
        camera = {"source": "camera.study"}
        with mock.patch.object(
            camera_mcp,
            "_load_current_camera",
            return_value=(
                {"current_entity": "camera.study", "projected_room": ""},
                "camera.study",
                camera,
            ),
        ), mock.patch.object(camera_mcp, "fetch_frame") as fetch_mock:
            content, is_error = unpack_mcp_result(
                camera_mcp._handle_use_device_camera(
                    {"action": "capture"},
                    "http://supervisor/core/api",
                    "http://homeassistant.local:1984",
                )
            )

        self.assertTrue(is_error)
        self.assertIn("物理体モード", content[0]["text"])
        fetch_mock.assert_not_called()

    def test_handle_watch_media_resolves_single_video_media_without_invasion(self):
        camera_mcp = load_camera_mcp_module()
        prefs = {"video_media": [{"id": "capture_tv", "source": "capture_tv", "room": "living", "label": "テレビ"}]}
        with mock.patch.object(camera_mcp, "_load_prefs", return_value=prefs), \
             mock.patch.object(camera_mcp, "fetch_frame", return_value=b"jpeg-bytes") as fetch_mock, \
             mock.patch.object(camera_mcp, "get_ha_token", return_value=""):
            content, is_error = unpack_mcp_result(
                camera_mcp._handle_watch_media(
                    None,
                    "http://supervisor/core/api",
                    "http://homeassistant.local:1984",
                )
            )
        fetch_mock.assert_called_once_with("capture_tv", ha_url="http://supervisor/core/api", go2rtc_url="http://homeassistant.local:1984", token="")
        self.assertFalse(is_error)
        text_payload = content[0]["text"]
        self.assertIn('"media_context"', text_payload)
        self.assertIn('"label": "テレビ"', text_payload)
        self.assertIn('record_episode(kind="media_watch"', content[-1]["text"])

    def test_handle_watch_media_errors_for_unknown_source(self):
        camera_mcp = load_camera_mcp_module()
        with mock.patch.object(camera_mcp, "_load_prefs", return_value={}):
            content, is_error = unpack_mcp_result(
                camera_mcp._handle_watch_media(
                    "missing",
                    "http://supervisor/core/api",
                    "http://homeassistant.local:1984",
                )
            )
        self.assertTrue(is_error)
        self.assertIn("未登録です", content[0]["text"])

    def test_main_replies_method_not_found_for_unknown_request_with_id(self):
        camera_mcp = load_camera_mcp_module()
        sent = run_main(camera_mcp, [
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"protocolVersion": "2026-07-28"}},
        ])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["id"], 1)
        self.assertEqual(sent[0]["error"]["code"], -32601)
        self.assertIn("server/discover", sent[0]["error"]["message"])

    def test_main_stays_silent_for_unknown_notification_without_id(self):
        camera_mcp = load_camera_mcp_module()
        sent = run_main(camera_mcp, [
            {"jsonrpc": "2.0", "method": "notifications/unknown"},
        ])
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
        sent = run_main(camera_mcp, requests)

        self.assertEqual(len(sent), 3)

        discover_reply = sent[0]
        self.assertEqual(discover_reply["id"], 1)
        self.assertEqual(discover_reply["error"]["code"], -32601)

        initialize_reply = sent[1]
        self.assertEqual(initialize_reply["id"], 2)
        self.assertIn("protocolVersion", initialize_reply["result"])
        self.assertIn("serverInfo", initialize_reply["result"])
        self.assertEqual(
            initialize_reply["result"]["serverInfo"],
            {"name": "camera-mcp", "version": "3.1"},
        )

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
                registry = camera_mcp._tool_registry(
                    "http://supervisor/core/api", "http://homeassistant.local:1984"
                )
                self.assertIn("review_camera_history", registry)
                history_tool = registry["review_camera_history"]["spec"]
                properties = history_tool["inputSchema"]["properties"]
                self.assertNotIn("source", properties)
                self.assertNotIn("path", properties)

                listed = run_main(camera_mcp, [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                ])
                self.assertIn(
                    "review_camera_history",
                    {tool["name"] for tool in listed[0]["result"]["tools"]},
                )

                prefs.write_text(
                    json.dumps({"camera_history_enabled": False}), encoding="utf-8"
                )
                # A running MCP process keeps its startup registry; the next
                # invocation creates a new process and reloads preferences.
                self.assertIn("review_camera_history", registry)
                self.assertNotIn(
                    "review_camera_history",
                    camera_mcp._tool_registry(
                        "http://supervisor/core/api", "http://homeassistant.local:1984"
                    ),
                )

    def test_history_tool_returns_only_current_camera_frames(self):
        camera_mcp = load_camera_mcp_module()
        jpeg = b"\xff\xd8" + (b"history" * 40) + b"\xff\xd9"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "history"
            prefs = Path(tmpdir) / "preferences.json"
            body = Path(tmpdir) / "body_location.json"
            body_state = Path(tmpdir) / "body_state.json"
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
                json.dumps(
                    {
                        "projected_room": "living_room",
                        "current_entity": "camera.living",
                    }
                ),
                encoding="utf-8",
            )
            before = {
                "remote_mode": "remote_avatar",
                "remote_room": "living_room",
                "remote_since": "2026-08-07T10:00:00+09:00",
                "remote_avatar_host": "camera.living",
                "embodiment_tension": 0.4,
                "return_to_body_pressure": 0.5,
            }
            body_state.write_text(json.dumps(before), encoding="utf-8")
            camera_mcp.camera_history.store_frame(
                root, "camera.living", jpeg, captured_at=time.time() - 30
            )
            camera_mcp.camera_history.store_frame(
                root, "study_capture", jpeg, captured_at=time.time() - 30
            )
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(body),
                    "EHA_BODY_STATE_FILE": str(body_state),
                    "EHA_CAMERA_HISTORY_ENABLED": "1",
                    "EHA_CAMERA_HISTORY_DIR": str(root),
                },
                clear=False,
            ):
                content, is_error = unpack_mcp_result(
                    camera_mcp._handle_review_camera_history(
                        {"start_seconds_ago": 30, "max_frames": 1}
                    )
                )

            self.assertFalse(is_error)
            image_blocks = [
                block for block in content if block.get("type") == "image"
            ]
            self.assertEqual(len(image_blocks), 1)
            context = json.loads(content[0]["text"])[
                "camera_history_context"
            ]
            self.assertEqual(context["source"], "camera.living")
            self.assertNotIn("study_capture", content[0]["text"])
            saved = json.loads(body_state.read_text(encoding="utf-8"))
            self.assertEqual(saved["last_action_mode"], "cyber_in_room")
            for key, value in before.items():
                self.assertEqual(saved[key], value)

    def test_history_rejects_projection_change_before_returning_frames(self):
        camera_mcp = load_camera_mcp_module()
        jpeg = b"\xff\xd8" + (b"history" * 40) + b"\xff\xd9"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "history"
            prefs = Path(tmpdir) / "preferences.json"
            location = Path(tmpdir) / "body_location.json"
            prefs.write_text(
                json.dumps(
                    {
                        "camera_history_enabled": True,
                        "camera_history_minutes": 10,
                        "cameras": [{"source": "camera.living", "label": "リビング"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            location.write_text(
                json.dumps(
                    {
                        "projected_room": "living_room",
                        "current_entity": "camera.living",
                    }
                ),
                encoding="utf-8",
            )
            camera_mcp.camera_history.store_frame(
                root, "camera.living", jpeg, captured_at=time.time() - 30
            )
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(location),
                    "EHA_CAMERA_HISTORY_ENABLED": "1",
                    "EHA_CAMERA_HISTORY_DIR": str(root),
                },
                clear=False,
            ), mock.patch.object(
                camera_mcp, "_camera_projection_still_active", return_value=False
            ), mock.patch.object(camera_mcp, "apply_action_to_body_state") as apply_mock:
                content, is_error = unpack_mcp_result(
                    camera_mcp._handle_review_camera_history(
                        {"start_seconds_ago": 30, "max_frames": 1}
                    )
                )

        self.assertTrue(is_error)
        self.assertIn("侵入状態が変わりました", content[0]["text"])
        self.assertFalse(any(block.get("type") == "image" for block in content))
        apply_mock.assert_not_called()

    def test_history_tool_rejects_physical_body_and_disabled_direct_call(self):
        camera_mcp = load_camera_mcp_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            prefs = Path(tmpdir) / "preferences.json"
            body = Path(tmpdir) / "body_location.json"
            prefs.write_text(
                json.dumps({"camera_history_enabled": True}), encoding="utf-8"
            )
            body.write_text("{}", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(body),
                    "EHA_CAMERA_HISTORY_ENABLED": "1",
                },
                clear=False,
            ):
                content, is_error = unpack_mcp_result(
                    camera_mcp._handle_review_camera_history({})
                )
            self.assertTrue(is_error)
            self.assertIn("物理体", content[0]["text"])

            with mock.patch.dict(
                os.environ,
                {
                    "EHA_PREFS_FILE": str(prefs),
                    "EHA_BODY_LOCATION_FILE": str(body),
                    "EHA_CAMERA_HISTORY_ENABLED": "0",
                },
                clear=False,
            ):
                content, is_error = unpack_mcp_result(
                    camera_mcp._handle_review_camera_history({})
                )
            self.assertTrue(is_error)
            self.assertIn("無効", content[0]["text"])

    def test_missing_tool_params_returns_error_and_server_continues(self):
        camera_mcp = load_camera_mcp_module()
        sent = run_main(camera_mcp, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
        ])

        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["id"], 1)
        self.assertTrue(sent[0]["result"]["isError"])
        self.assertEqual(sent[0]["result"]["content"][0]["text"], "未知のツール: ")
        self.assertEqual(sent[1]["id"], 2)
        self.assertEqual(sent[1]["result"]["serverInfo"]["version"], "3.1")

    def test_handler_exception_returns_error_and_server_continues(self):
        camera_mcp = load_camera_mcp_module()
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "use_device_camera", "arguments": {"action": "capture"}},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        with mock.patch.object(
            camera_mcp,
            "_load_current_camera",
            return_value=(
                {
                    "projected_room": "living_room",
                    "current_entity": "camera.living",
                },
                "camera.living",
                {"source": "camera.living"},
            ),
        ), mock.patch.object(
            camera_mcp, "fetch_frame", side_effect=RuntimeError("synthetic capture failure")
        ):
            sent = run_main(camera_mcp, requests)

        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["id"], 1)
        self.assertTrue(sent[0]["result"]["isError"])
        self.assertEqual(
            sent[0]["result"]["content"][0]["text"],
            "ツール実行エラー（use_device_camera）",
        )
        self.assertEqual(sent[1]["id"], 2)
        self.assertIn("tools", sent[1]["result"])


if __name__ == "__main__":
    unittest.main()
