"""daemon 側からの身体位置 publish のテスト。

body-mcp 側にも同じ publish があるが、そちらは MQTT の接続情報を MCP サーバーの
env に必要とする。env 宣言を置き換えとして扱うかはエージェントCLIごとに違い、
宣言を守るCLIでは publish が黙って no-op になり、身体位置のエンティティが
unknown のまま残る。接続情報を持つ daemon から流せばCLIの違いに関係なく届く。
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
os.environ.setdefault("HA_URL", "http://example.invalid")


def _load_daemon_without_boot():
    path = ROOT / "embodied_ha" / "daemon.py"
    source = path.read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType("daemon_body_location_publish_test")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


class DaemonBodyLocationPublishTests(unittest.TestCase):
    def setUp(self):
        self.daemon = _load_daemon_without_boot()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "body_location.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _publish(self, state):
        if state is not None:
            self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(os.environ, {"EHA_BODY_LOCATION_FILE": str(self.path)}), \
                mock.patch.object(self.daemon, "mqtt_pub") as pub:
            self.daemon.publish_body_location()
        return [call.args for call in pub.call_args_list]

    def test_publishes_physical_room_and_current_place(self):
        calls = self._publish({"current_room": "living_room", "current_entity": "camera.living"})
        prefix = self.daemon.MQTT_PREFIX
        self.assertEqual(calls, [
            (f"{prefix}/body/physical_room/state", "living_room"),
            (f"{prefix}/body/current_place/state", "camera.living"),
        ])

    def test_no_projection_reports_being_in_the_body(self):
        calls = self._publish({"current_room": "study", "current_entity": ""})
        self.assertEqual(calls[1][1], "身体の中")

    def test_missing_file_publishes_nothing(self):
        self.assertEqual(self._publish(None), [])

    def test_unreadable_content_publishes_nothing(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        with mock.patch.dict(os.environ, {"EHA_BODY_LOCATION_FILE": str(self.path)}), \
                mock.patch.object(self.daemon, "mqtt_pub") as pub:
            self.daemon.publish_body_location()
        self.assertEqual(pub.call_args_list, [])

    def test_loop_completion_publishes_the_location(self):
        with mock.patch.object(self.daemon, "publish_body_location") as pub, \
                mock.patch.object(self.daemon.body_state, "update_state", return_value={}), \
                mock.patch.object(self.daemon, "_log_body_state"):
            self.daemon.finish_body_state("loop", True, 1.0)
        pub.assert_called_once()


if __name__ == "__main__":
    unittest.main()
