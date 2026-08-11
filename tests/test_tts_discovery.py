"""Home Assistant TTS WebSocket discovery tests."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tts_discovery_module", ROOT / "embodied_ha" / "tts_discovery.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConnection:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def recv(self, timeout=None):
        if not self.messages:
            raise TimeoutError
        value = self.messages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)

    def send(self, payload):
        self.sent.append(json.loads(payload))


def success_messages(voices=None):
    messages = [
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {
            "id": 1,
            "type": "result",
            "success": True,
            "result": {
                "providers": [
                    {
                        "engine_id": "tts.example",
                        "supported_languages": ["ja-JP", "en-US"],
                    }
                ]
            },
        },
    ]
    if voices is not ...:
        messages.append(
            {
                "id": 2,
                "type": "result",
                "success": True,
                "result": {"voices": voices},
            }
        )
    return messages


class TtsDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_module()

    def test_derives_supervisor_and_regular_websocket_urls(self):
        self.assertEqual(
            self.module.websocket_url("http://supervisor/core/api"),
            "ws://supervisor/core/websocket",
        )
        self.assertEqual(
            self.module.websocket_url("https://ha.example/api"),
            "wss://ha.example/api/websocket",
        )
        self.assertEqual(
            self.module.websocket_url("http://ha.local:8123"),
            "ws://ha.local:8123/api/websocket",
        )

    def test_lists_languages_and_language_specific_voices(self):
        connection = FakeConnection(
            success_messages([{"voice_id": "zephyr", "name": "Zephyr"}])
        )
        with mock.patch.object(self.module, "connect", return_value=connection):
            result = self.module.discover_tts_options(
                "http://supervisor/core/api", "secret", "tts.example", "ja-JP"
            )
        self.assertEqual(result["languages"], ["ja-JP", "en-US"])
        self.assertEqual(result["voices"], [{"voice_id": "zephyr", "name": "Zephyr"}])
        self.assertEqual(connection.sent[0], {"type": "auth", "access_token": "secret"})
        self.assertEqual(connection.sent[2]["type"], "tts/engine/voices")

    def test_voice_provider_may_return_null(self):
        connection = FakeConnection(success_messages(None))
        with mock.patch.object(self.module, "connect", return_value=connection):
            result = self.module.discover_tts_options(
                "http://supervisor/core/api", "secret", "tts.example", "ja-JP"
            )
        self.assertEqual(result["voices"], [])

    def test_language_only_skips_voice_request(self):
        connection = FakeConnection(success_messages(...))
        with mock.patch.object(self.module, "connect", return_value=connection):
            result = self.module.discover_tts_options(
                "http://supervisor/core/api", "secret", "tts.example"
            )
        self.assertEqual(result, {"languages": ["ja-JP", "en-US"], "voices": []})
        self.assertEqual([item.get("type") for item in connection.sent], ["auth", "tts/engine/list"])

    def test_authentication_failure_is_safe(self):
        connection = FakeConnection([{"type": "auth_required"}, {"type": "auth_invalid"}])
        with mock.patch.object(
            self.module, "connect", return_value=connection
        ), self.assertRaisesRegex(
            self.module.TtsDiscoveryError, "authentication failed"
        ):
            self.module.discover_tts_options(
                "http://supervisor/core/api", "do-not-leak", "tts.example"
            )

    def test_timeout_invalid_json_and_mismatched_id_are_rejected(self):
        cases = [
            ([{"type": "auth_required"}, TimeoutError()], "timed out"),
            ([{"type": "auth_required"}, "not-json"], "invalid JSON"),
            (
                [
                    {"type": "auth_required"},
                    {"type": "auth_ok"},
                    {"id": 99, "type": "result", "success": True, "result": {}},
                ],
                "unexpected response",
            ),
        ]
        for messages, expected in cases:
            with self.subTest(expected=expected):
                connection = FakeConnection(messages)
                with mock.patch.object(
                    self.module, "connect", return_value=connection
                ), self.assertRaisesRegex(self.module.TtsDiscoveryError, expected):
                    self.module.discover_tts_options(
                        "http://supervisor/core/api", "secret", "tts.example"
                    )

    def test_each_invocation_owns_a_connection(self):
        connections = [
            FakeConnection(success_messages(...)),
            FakeConnection(success_messages(...)),
        ]
        with mock.patch.object(self.module, "connect", side_effect=connections) as connect_mock:
            for _ in range(2):
                self.module.discover_tts_options(
                    "http://supervisor/core/api", "secret", "tts.example"
                )
        self.assertEqual(connect_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
