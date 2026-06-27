import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


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
