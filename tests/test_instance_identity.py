import importlib
import importlib.util
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
RUN_SH = EHA_DIR / "run.sh"
SERVER_PATH = EHA_DIR / "web" / "server.py"
sys.path.insert(0, str(EHA_DIR))

import instance_identity  # noqa: E402


EXPECTED_DEFAULT_IDENTIFIERS = {
    "embodied_ha/body/current_place/state",
    "embodied_ha/body/physical_room/state",
    "embodied_ha/chat/set",
    "embodied_ha/chat/state",
    "embodied_ha/emotion/state",
    "embodied_ha/last_speak/state",
    "embodied_ha/loop/trigger",
    "embodied_ha/observation/state",
    "embodied_ha_body_current_place",
    "embodied_ha_body_physical_room",
    "embodied_ha_chat",
    "embodied_ha_chat_input",
    "embodied_ha_emotion",
    "embodied_ha_harness_setup_required",
    "embodied_ha_last_speak",
    "embodied_ha_loop",
    "embodied_ha_observation",
    "embodied_ha_observe",
}


def reload_identity(prefix: str | None, data_dir: str | None = None):
    values = {}
    if prefix is not None:
        values["EHA_MQTT_PREFIX"] = prefix
    if data_dir is not None:
        values["EHA_DATA_DIR"] = data_dir
    with mock.patch.dict(os.environ, values, clear=False):
        return importlib.reload(instance_identity)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_daemon(name: str):
    source = (EHA_DIR / "daemon.py").read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType(name)
    module.__file__ = str(EHA_DIR / "daemon.py")
    with mock.patch.dict(os.environ, {"HA_URL": "http://supervisor/core/api"}, clear=False):
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def load_server(name: str):
    env = {
        "HA_URL": "http://supervisor/core/api",
        "SUPERVISOR_TOKEN": "test-token",
        "EHA_LOG_DIR": "/tmp",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return load_module(name, SERVER_PATH)


def render_discovery_raw(prefix: str) -> str:
    source = RUN_SH.read_text(encoding="utf-8")
    start = source.index("    # 内省ログ（loop/observe ループが書き込む）")
    end = source.index('    echo "[run] MQTT discovery 完了', start)
    block = source[start:end]
    script = "\n".join([
        "set -euo pipefail",
        f"export EHA_MQTT_PREFIX={prefix}",
        "_pub() { printf '%s\\t%s\\n' \"$2\" \"$4\"; }",
        block,
    ])
    return subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout


def render_discovery(prefix: str) -> list[tuple[str, dict]]:
    return [
        (topic, json.loads(payload))
        for topic, payload in (line.split("\t", 1) for line in render_discovery_raw(prefix).splitlines())
    ]


# 既定(EHA_MQTT_PREFIX未設定=本番あかね)で run.sh が生成する MQTT discovery の生バイト列の
# golden(SHA-256)。トピック/unique_id だけでなく name/icon/entity_category/max/payload_press・
# JSON整形・エスケープまで含む完全一致ゲート。この値が変わる=本番あかねのエンティティ定義が
# 変わる可能性=デプロイで表示破壊。リファクタで変えてはならない(sol review 2026-07-20指摘の
# 集合比較の弱さを補強)。
DISCOVERY_GOLDEN_SHA256 = "d59214f61204af3168cdc18dbd9704188280745cb6b7a646c7e8c7d6b716456f"


class InstanceIdentityTests(unittest.TestCase):
    def tearDown(self):
        importlib.reload(instance_identity)

    def test_default_identity_and_generated_identifiers_are_byte_exact(self):
        identity = reload_identity("", "")
        self.assertEqual(identity.MQTT_PREFIX, "embodied_ha")

        discovery = render_discovery(identity.MQTT_PREFIX)
        discovery_identifiers = set()
        for config_topic, payload in discovery:
            discovery_identifiers.add(config_topic.rsplit("/", 2)[-2])
            discovery_identifiers.add(payload["unique_id"])
            discovery_identifiers.update(value for key, value in payload.items() if key.endswith("_topic"))

        daemon = load_daemon("daemon_instance_identity_default")
        daemon_topics = []
        daemon.mqtt_pub = lambda topic, message: daemon_topics.append(topic)
        daemon._chat_lock = mock.Mock(acquire=mock.Mock(return_value=False))
        daemon.run_chat("test")
        self.assertEqual(daemon_topics, ["embodied_ha/chat/state"])

        postprocess = load_module("chat_postprocess_instance_identity_default", EHA_DIR / "chat_postprocess.py")
        postprocess_topics = []
        postprocess.publish_private_to_mqtt(
            {"private": "test"}, "mqtt", run=lambda command, **kwargs: postprocess_topics.append(command[command.index("-t") + 1])
        )

        body = load_module("body_mcp_instance_identity_default", EHA_DIR / "body-mcp.py")
        body_topics = []
        with mock.patch.dict(os.environ, {"MQTT_HOST": "mqtt"}, clear=False), \
             mock.patch.object(body.subprocess, "run", side_effect=lambda command, **kwargs: body_topics.append(command[command.index("-t") + 1])):
            body.publish_body_presence({"current_room": "study", "current_entity": ""})

        server = load_server("server_instance_identity_default")
        server.MQTT_HOST = ""
        server.HA_URL = "http://supervisor/core/api"
        server.HA_TOKEN = "test-token"
        with mock.patch.object(server.subprocess, "run") as run:
            server.send_chat("test")
        fallback_entity = json.loads(run.call_args.args[0][run.call_args.args[0].index("-d") + 1])["entity_id"]

        actual = discovery_identifiers | set(daemon_topics) | set(postprocess_topics) | set(body_topics) | {
            daemon._SETUP_WAIT_NOTIFICATION_ID,
            fallback_entity.removeprefix("input_text."),
        }
        self.assertEqual(actual, EXPECTED_DEFAULT_IDENTIFIERS)

    def test_prefix_updates_python_and_discovery_identifiers_together(self):
        identity = reload_identity("eha_test")
        self.assertEqual(identity.MQTT_PREFIX, "eha_test")

        discovery = render_discovery(identity.MQTT_PREFIX)
        self.assertEqual(discovery[0][0], "homeassistant/sensor/eha_test_observation/config")
        self.assertEqual(discovery[0][1]["unique_id"], "eha_test_observation")
        self.assertEqual(discovery[0][1]["state_topic"], "eha_test/observation/state")
        discovery_identifiers = set()
        for config_topic, payload in discovery:
            discovery_identifiers.add(config_topic.rsplit("/", 2)[-2])
            discovery_identifiers.add(payload["unique_id"])
            discovery_identifiers.update(value for key, value in payload.items() if key.endswith("_topic"))

        daemon = load_daemon("daemon_instance_identity_custom")
        daemon_topics = []
        daemon.mqtt_pub = lambda topic, message: daemon_topics.append(topic)
        daemon._chat_lock = mock.Mock(acquire=mock.Mock(return_value=False))
        daemon.run_chat("test")

        postprocess = load_module("chat_postprocess_instance_identity_custom", EHA_DIR / "chat_postprocess.py")
        command = []
        postprocess.publish_private_to_mqtt(
            {"private": "test"}, "mqtt", run=lambda args, **kwargs: command.extend(args)
        )
        self.assertIn("eha_test/observation/state", command)

        body = load_module("body_mcp_instance_identity_custom", EHA_DIR / "body-mcp.py")
        published = []
        with mock.patch.dict(os.environ, {"MQTT_HOST": "mqtt"}, clear=False), \
             mock.patch.object(body.subprocess, "run", side_effect=lambda args, **kwargs: published.append(args)):
            body.publish_body_presence({"current_room": "study", "current_entity": "avatar"})
        self.assertEqual({args[args.index("-t") + 1] for args in published}, {
            "eha_test/body/physical_room/state", "eha_test/body/current_place/state",
        })

        server = load_server("server_instance_identity_custom")
        server.MQTT_HOST = "mqtt"
        with mock.patch.object(server.subprocess, "run") as run:
            server.send_chat("test")
        self.assertIn("eha_test/chat/set", run.call_args.args[0])
        server.MQTT_HOST = ""
        with mock.patch.object(server.subprocess, "run") as run:
            server.send_chat("test")
        fallback_entity = json.loads(run.call_args.args[0][run.call_args.args[0].index("-d") + 1])["entity_id"]

        actual = discovery_identifiers | set(daemon_topics) | {
            daemon._SETUP_WAIT_NOTIFICATION_ID,
            fallback_entity.removeprefix("input_text."),
            command[command.index("-t") + 1],
        } | {args[args.index("-t") + 1] for args in published}
        expected = {item.replace("embodied_ha", "eha_test", 1) for item in EXPECTED_DEFAULT_IDENTIFIERS}
        self.assertEqual(actual, expected)

    def test_discovery_output_is_byte_exact_golden(self):
        # 既定での run.sh discovery 生出力が golden と1バイトも違わないこと(整形/name/icon含む)。
        import hashlib
        raw = render_discovery_raw("embodied_ha")
        self.assertEqual(hashlib.sha256(raw.encode("utf-8")).hexdigest(), DISCOVERY_GOLDEN_SHA256)

    def test_mcp_config_propagates_mqtt_prefix_to_subprocesses(self):
        # sol指摘(高1): MCPサーバー(body-mcp等)はconfig生成時のenvだけを受け取るため、
        # EHA_MQTT_PREFIX が mcp-config の受け渡しリストに無いと、フォーク時に body-mcp だけ
        # 既定プレフィックスへ戻り discovery/daemon と不整合になる。
        with mock.patch.dict(os.environ, {"EHA_MQTT_PREFIX": "eha_test"}, clear=False):
            mcp_config = load_module("mcp_config_prefix_propagation", EHA_DIR / "mcp-config.py")
        self.assertIn("EHA_MQTT_PREFIX", mcp_config._ENV_KEYS)
        self.assertEqual(mcp_config.COMMON_ENV.get("EHA_MQTT_PREFIX"), "eha_test")

    def test_lounge_import_module_name_is_not_an_instance_identifier(self):
        source = SERVER_PATH.read_text(encoding="utf-8")
        self.assertIn('spec_from_file_location("embodied_ha_lounge_mcp", path)', source)

    def test_chat_shell_uses_the_exported_prefix(self):
        source = (EHA_DIR / "chat.sh").read_text(encoding="utf-8")
        self.assertIn('EHA_MQTT_PREFIX="$EHA_MQTT_PREFIX"', source)
        self.assertIn('f"{prefix}/observation/state"', source)
