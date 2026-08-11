from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EHA_DIR))

import wake_routing  # noqa: E402


def load_daemon(name: str):
    source = (EHA_DIR / "daemon.py").read_text(encoding="utf-8").split("# --- 多重起動ガード", 1)[0]
    module = types.ModuleType(name)
    module.__file__ = str(EHA_DIR / "daemon.py")
    with mock.patch.dict(os.environ, {"HA_URL": "http://supervisor/core/api"}, clear=False):
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def valid_envelope(
    *,
    request_id: str | None = None,
    timestamp: str | None = None,
    message="電気を消して",
    backend="ha_stt",
):
    return {
        "version": 1,
        "event": "wake_command_detected",
        "message": message,
        "source": "rtsp_assist_gateway",
        "request_id": request_id or str(uuid.uuid4()),
        "source_id": "study",
        "room": "study",
        "wake_word_id": "sample_agent",
        "backend": backend,
        "timestamp": timestamp or dt.datetime.now(dt.UTC).isoformat(),
    }


def test_legacy_plain_text_and_json_contracts_remain_supported() -> None:
    plain = wake_routing.parse_chat_trigger("こんにちは")
    structured = wake_routing.parse_chat_trigger(
        json.dumps({"message": "こんにちは", "source": "web"}, ensure_ascii=False)
    )
    assert plain is not None and (plain.message, plain.source, plain.versioned) == (
        "こんにちは",
        "chat",
        False,
    )
    assert structured is not None and (structured.message, structured.source, structured.versioned) == (
        "こんにちは",
        "web",
        False,
    )


def test_valid_versioned_envelope_is_voice_bound_to_room() -> None:
    trigger = wake_routing.parse_chat_trigger(json.dumps(valid_envelope(), ensure_ascii=False))
    assert trigger is not None
    assert trigger.versioned is True
    assert trigger.source == "voice"
    assert trigger.room == "study"
    assert trigger.message == "電気を消して"


@pytest.mark.parametrize("backend", ["ha_stt", "microwakeword"])
def test_supported_gateway_backends_share_the_same_voice_contract(backend: str) -> None:
    trigger = wake_routing.parse_chat_trigger(
        json.dumps(valid_envelope(backend=backend), ensure_ascii=False)
    )
    assert trigger is not None
    assert trigger.backend == backend
    assert trigger.source == "voice"
    assert trigger.room == "study"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda payload: payload.update(version=2), "unsupported_version"),
        (lambda payload: payload.update(event="other"), "unsupported_event"),
        (lambda payload: payload.update(source="other"), "unsupported_source"),
        (lambda payload: payload.update(backend="other"), "unsupported_backend"),
        (lambda payload: payload.update(request_id="not-a-uuid"), "invalid_request_id"),
        (lambda payload: payload.update(message="x" * 501), "invalid_message"),
        (lambda payload: payload.update(room="study\nforged"), "invalid_room"),
        (lambda payload: payload.update(extra=True), "invalid_fields"),
    ],
)
def test_malformed_versioned_envelopes_fail_closed(mutate, reason: str) -> None:
    payload = valid_envelope()
    mutate(payload)
    with pytest.raises(wake_routing.TriggerError, match=reason):
        wake_routing.parse_chat_trigger(json.dumps(payload, ensure_ascii=False))


def test_stale_and_future_envelopes_fail_closed() -> None:
    now = dt.datetime.now(dt.UTC)
    stale = valid_envelope(timestamp=(now - dt.timedelta(seconds=301)).isoformat())
    future = valid_envelope(timestamp=(now + dt.timedelta(seconds=31)).isoformat())
    with pytest.raises(wake_routing.TriggerError, match="stale_timestamp"):
        wake_routing.parse_chat_trigger(json.dumps(stale), now=now)
    with pytest.raises(wake_routing.TriggerError, match="future_timestamp"):
        wake_routing.parse_chat_trigger(json.dumps(future), now=now)


def test_replay_ledger_survives_new_instance_and_is_bounded() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "wake_request_ids.json"
        first = wake_routing.RequestReplayLedger(path, ttl_seconds=60, max_entries=2)
        request_id = str(uuid.uuid4())
        assert first.claim(request_id, now=100) is True
        restarted = wake_routing.RequestReplayLedger(path, ttl_seconds=60, max_entries=2)
        assert restarted.claim(request_id, now=101) is False
        assert restarted.claim(str(uuid.uuid4()), now=102) is True
        assert restarted.claim(str(uuid.uuid4()), now=103) is True
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert len(payload["entries"]) == 2


def test_corrupt_replay_ledger_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "wake_request_ids.json"
        path.write_text("not-json", encoding="utf-8")
        with pytest.raises(wake_routing.ReplayLedgerError, match="unreadable"):
            wake_routing.RequestReplayLedger(path).claim(str(uuid.uuid4()))


def test_daemon_runs_versioned_trigger_once_and_updates_location_belief() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        payload = json.dumps(valid_envelope(), ensure_ascii=False)
        first = load_daemon("daemon_wake_first")
        first.run_chat = mock.Mock(return_value=True)
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": temporary}, clear=False):
            first.on_chat_trigger(payload)
        first.run_chat.assert_called_once_with(
            "電気を消して",
            source="voice",
            voice_room="study",
            wait_for_lock=True,
        )
        belief = json.loads((Path(temporary) / "location_belief.json").read_text(encoding="utf-8"))
        assert belief == {
            "room": "study",
            "source": "study",
            "method": "wake_word_gateway",
        }

        restarted = load_daemon("daemon_wake_restarted")
        restarted.run_chat = mock.Mock(return_value=True)
        with mock.patch.dict(os.environ, {"EHA_DATA_DIR": temporary}, clear=False):
            restarted.on_chat_trigger(payload)
        restarted.run_chat.assert_not_called()


def test_daemon_rejects_invalid_envelope_without_logging_command(capsys) -> None:
    secret = "絶対にログへ出さない秘密"
    payload = valid_envelope(message=secret)
    payload["version"] = 2
    daemon = load_daemon("daemon_wake_invalid")
    daemon.run_chat = mock.Mock(return_value=True)
    daemon.on_chat_trigger(json.dumps(payload, ensure_ascii=False))
    daemon.run_chat.assert_not_called()
    assert secret not in capsys.readouterr().out


def test_run_chat_waits_for_busy_lock_then_runs_once() -> None:
    daemon = load_daemon("daemon_wake_busy")
    daemon._chat_lock = threading.Lock()
    daemon._chat_lock.acquire()
    daemon.CHAT_LOCK_WAIT_SECONDS = 1

    def release_lock():
        time.sleep(0.05)
        daemon._chat_lock.release()

    release_thread = threading.Thread(target=release_lock)
    release_thread.start()
    body_snapshot = {"schema_version": 1}
    with mock.patch.object(daemon, "mqtt_pub"), \
         mock.patch.object(daemon, "_load_body_state", return_value=body_snapshot), \
         mock.patch.object(daemon, "tick_desires", return_value=([], 0)), \
         mock.patch.object(daemon, "tick_body_state", return_value=body_snapshot), \
         mock.patch.object(daemon, "_body_state_json", return_value="{}"), \
         mock.patch.object(daemon, "finish_body_state"), \
         mock.patch.object(daemon.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
        assert daemon.run_chat(
            "テスト",
            source="voice",
            voice_room="study",
            wait_for_lock=True,
        ) is True
    release_thread.join(timeout=1)
    assert run.call_count == 1
    assert run.call_args.kwargs["env"]["EHA_VOICE_USER_ROOM"] == "study"


def test_run_chat_reports_busy_timeout_separately_from_execution_failure() -> None:
    daemon = load_daemon("daemon_wake_busy_timeout")
    daemon._chat_lock = threading.Lock()
    daemon._chat_lock.acquire()
    daemon.CHAT_LOCK_WAIT_SECONDS = 0

    with mock.patch.object(daemon, "mqtt_pub"):
        assert daemon.run_chat(
            "テスト",
            source="voice",
            voice_room="study",
            wait_for_lock=True,
        ) is None
    daemon._chat_lock.release()


def test_non_threaded_mqtt_dispatch_calls_handler_inline() -> None:
    daemon = load_daemon("daemon_mqtt_sequential")
    calls = []
    current_thread = threading.get_ident()
    daemon._dispatch_mqtt_line(
        lambda line: calls.append((line, threading.get_ident())),
        "payload",
        threaded=False,
    )
    assert calls == [("payload", current_thread)]


def test_runtime_chat_listener_is_sequential_qos_one() -> None:
    source = (EHA_DIR / "daemon.py").read_text(encoding="utf-8")
    assert 'kwargs={"threaded": False, "qos": 1}' in source
