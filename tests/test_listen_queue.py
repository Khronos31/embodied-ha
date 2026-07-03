import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_listen_queue_module():
    path = ROOT / "embodied_ha" / "listen_queue.py"
    import sys

    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("listen_queue_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ListenQueueTests(unittest.TestCase):
    def setUp(self):
        self.listen_queue = load_listen_queue_module()

    def test_check_listen_queue_cooldown_blocks_when_too_soon(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            body_state_path = Path(tmpdir) / "body_state.json"
            log_path = Path(tmpdir) / "next_listen_log.jsonl"
            body_state_path.write_text(json.dumps({"session_count": 10}, ensure_ascii=False), encoding="utf-8")
            log_path.write_text(json.dumps({"action": "queue", "session_count": 8}, ensure_ascii=False) + "\n", encoding="utf-8")
            old_body_state = os.environ.get("EHA_BODY_STATE_FILE")
            old_log = os.environ.get("EHA_NEXT_LISTEN_LOG_FILE")
            old_cooldown = os.environ.get("EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS")
            try:
                os.environ["EHA_BODY_STATE_FILE"] = str(body_state_path)
                os.environ["EHA_NEXT_LISTEN_LOG_FILE"] = str(log_path)
                os.environ["EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS"] = "3"
                ok, reason = self.listen_queue.check_listen_queue_cooldown()
            finally:
                if old_body_state is None:
                    os.environ.pop("EHA_BODY_STATE_FILE", None)
                else:
                    os.environ["EHA_BODY_STATE_FILE"] = old_body_state
                if old_log is None:
                    os.environ.pop("EHA_NEXT_LISTEN_LOG_FILE", None)
                else:
                    os.environ["EHA_NEXT_LISTEN_LOG_FILE"] = old_log
                if old_cooldown is None:
                    os.environ.pop("EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS", None)
                else:
                    os.environ["EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS"] = old_cooldown
        self.assertFalse(ok)
        self.assertIn("クールダウン中", reason)

    def test_check_listen_queue_cooldown_allows_after_gap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            body_state_path = Path(tmpdir) / "body_state.json"
            log_path = Path(tmpdir) / "next_listen_log.jsonl"
            body_state_path.write_text(json.dumps({"session_count": 12}, ensure_ascii=False), encoding="utf-8")
            log_path.write_text(json.dumps({"action": "queue", "session_count": 8}, ensure_ascii=False) + "\n", encoding="utf-8")
            old_body_state = os.environ.get("EHA_BODY_STATE_FILE")
            old_log = os.environ.get("EHA_NEXT_LISTEN_LOG_FILE")
            old_cooldown = os.environ.get("EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS")
            try:
                os.environ["EHA_BODY_STATE_FILE"] = str(body_state_path)
                os.environ["EHA_NEXT_LISTEN_LOG_FILE"] = str(log_path)
                os.environ["EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS"] = "3"
                ok, reason = self.listen_queue.check_listen_queue_cooldown()
            finally:
                if old_body_state is None:
                    os.environ.pop("EHA_BODY_STATE_FILE", None)
                else:
                    os.environ["EHA_BODY_STATE_FILE"] = old_body_state
                if old_log is None:
                    os.environ.pop("EHA_NEXT_LISTEN_LOG_FILE", None)
                else:
                    os.environ["EHA_NEXT_LISTEN_LOG_FILE"] = old_log
                if old_cooldown is None:
                    os.environ.pop("EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS", None)
                else:
                    os.environ["EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS"] = old_cooldown
        self.assertTrue(ok)
        self.assertEqual(reason, "")


    def test_prepare_queued_listen_session_resolves_current_entity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            body_location_path = Path(tmpdir) / "body_location.json"
            prefs_path = Path(tmpdir) / "preferences.json"
            request_path = Path(tmpdir) / "next_listen_request.json"
            body_location_path.write_text(json.dumps({"current_entity": "camera.kitchen"}, ensure_ascii=False), encoding="utf-8")
            prefs_path.write_text(json.dumps({"audio_sources": [{"entity": "camera.kitchen", "source": "rtsp://example.local/kitchen", "label": "Kitchen"}]}, ensure_ascii=False), encoding="utf-8")
            request_path.write_text(json.dumps({"request_id": "req-1", "duration": 4, "transcribe": True, "mode": "watch"}, ensure_ascii=False), encoding="utf-8")
            old_env = {k: os.environ.get(k) for k in ["EHA_BODY_LOCATION_FILE", "EHA_PREFS_FILE", "EHA_NEXT_LISTEN_REQUEST_FILE"]}
            logged = []
            try:
                os.environ["EHA_BODY_LOCATION_FILE"] = str(body_location_path)
                os.environ["EHA_PREFS_FILE"] = str(prefs_path)
                os.environ["EHA_NEXT_LISTEN_REQUEST_FILE"] = str(request_path)
                with mock.patch.object(self.listen_queue, "record_request_to_wav") as record_mock, \
                     mock.patch.object(self.listen_queue, "_transcribe_recorded_audio", return_value=("人の話し声がした", "wyoming", "ja-JP")), \
                     mock.patch.object(self.listen_queue, "append_active_listen_result", side_effect=lambda entry: logged.append(entry)):
                    ctx = self.listen_queue.prepare_queued_listen_session("watch")
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["EHA_QUEUED_LISTEN_SOURCE"], "rtsp://example.local/kitchen")
        self.assertEqual(ctx["RECENT_AUDITORY_INPUT"], "# 予約していた聴取結果\n人の話し声がした")
        record_mock.assert_called_once()
        queued_request = record_mock.call_args.args[0]
        self.assertIsInstance(queued_request, dict)
        self.assertEqual(queued_request["source"], "rtsp://example.local/kitchen")
        self.assertEqual(queued_request["duration"], 4)
        self.assertEqual(len(logged), 1)
        self.assertTrue(logged[0]["prepared_for_session"])
        self.assertEqual(logged[0]["source"], "rtsp://example.local/kitchen")
        self.assertEqual(logged[0]["transcript"], "人の話し声がした")

    def test_prepare_queued_listen_session_uses_current_room_for_physical_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            body_location_path = Path(tmpdir) / "body_location.json"
            prefs_path = Path(tmpdir) / "preferences.json"
            request_path = Path(tmpdir) / "next_listen_request.json"
            body_location_path.write_text(json.dumps({"current_entity": "", "current_room": "study"}, ensure_ascii=False), encoding="utf-8")
            prefs_path.write_text(json.dumps({"audio_sources": [
                {"entity": "camera.living", "source": "rtsp://example.local/living", "label": "Living", "room": "living"},
                {"entity": "camera.study", "source": "rtsp://example.local/study", "label": "Study", "room": "study"},
            ]}, ensure_ascii=False), encoding="utf-8")
            request_path.write_text(json.dumps({"request_id": "req-2", "duration": 4, "mode": "watch"}, ensure_ascii=False), encoding="utf-8")
            old_env = {k: os.environ.get(k) for k in ["EHA_BODY_LOCATION_FILE", "EHA_PREFS_FILE", "EHA_NEXT_LISTEN_REQUEST_FILE"]}
            logged = []
            try:
                os.environ["EHA_BODY_LOCATION_FILE"] = str(body_location_path)
                os.environ["EHA_PREFS_FILE"] = str(prefs_path)
                os.environ["EHA_NEXT_LISTEN_REQUEST_FILE"] = str(request_path)
                with mock.patch.object(self.listen_queue, "record_request_to_wav") as record_mock,                      mock.patch.object(self.listen_queue, "_transcribe_recorded_audio", return_value=("fallback transcript", "wyoming", "ja-JP")),                      mock.patch.object(self.listen_queue, "append_active_listen_result", side_effect=lambda entry: logged.append(entry)),                      mock.patch.dict("sys.modules", {"body_state": mock.Mock(update_body_state=lambda updater: updater({}), on_audio_session=lambda state: state)}, clear=False):
                    ctx = self.listen_queue.prepare_queued_listen_session("watch")
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        self.assertIsNotNone(ctx)
        self.assertNotIn("EHA_QUEUED_LISTEN_ERROR", ctx)
        self.assertEqual(ctx["EHA_QUEUED_LISTEN_SOURCE"], "rtsp://example.local/study")
        record_mock.assert_called_once()
        self.assertEqual(len(logged), 1)
        self.assertTrue(logged[0]["prepared_for_session"])
        self.assertEqual(logged[0]["source"], "rtsp://example.local/study")
        self.assertEqual(logged[0]["source_label"], "Study")
        self.assertEqual(logged[0]["transcript"], "fallback transcript")

    def test_prepare_queued_listen_session_omits_prompt_block_when_transcript_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            body_location_path = Path(tmpdir) / "body_location.json"
            prefs_path = Path(tmpdir) / "preferences.json"
            request_path = Path(tmpdir) / "next_listen_request.json"
            body_location_path.write_text(json.dumps({"current_entity": "camera.kitchen"}, ensure_ascii=False), encoding="utf-8")
            prefs_path.write_text(json.dumps({"audio_sources": [{"entity": "camera.kitchen", "source": "rtsp://example.local/kitchen"}]}, ensure_ascii=False), encoding="utf-8")
            request_path.write_text(json.dumps({"request_id": "req-3", "duration": 4, "mode": "watch"}, ensure_ascii=False), encoding="utf-8")
            old_env = {k: os.environ.get(k) for k in ["EHA_BODY_LOCATION_FILE", "EHA_PREFS_FILE", "EHA_NEXT_LISTEN_REQUEST_FILE"]}
            try:
                os.environ["EHA_BODY_LOCATION_FILE"] = str(body_location_path)
                os.environ["EHA_PREFS_FILE"] = str(prefs_path)
                os.environ["EHA_NEXT_LISTEN_REQUEST_FILE"] = str(request_path)
                with mock.patch.object(self.listen_queue, "record_request_to_wav"), \
                     mock.patch.object(self.listen_queue, "_transcribe_recorded_audio", return_value=("", "", "")), \
                     mock.patch.object(self.listen_queue, "append_active_listen_result"):
                    ctx = self.listen_queue.prepare_queued_listen_session("watch")
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["RECENT_AUDITORY_INPUT"], "")
