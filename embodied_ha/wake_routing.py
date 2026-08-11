"""Validation and replay protection for versioned wake-command chat triggers."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_PAYLOAD_BYTES = 4096
MAX_COMMAND_LENGTH = 500
MAX_LABEL_LENGTH = 128
MAX_EVENT_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
REPLAY_TTL_SECONDS = 3600
REPLAY_MAX_ENTRIES = 256
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SUPPORTED_BACKENDS = frozenset({"ha_stt", "microwakeword"})


class TriggerError(ValueError):
    """A safe-to-log validation failure with no household transcript content."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ReplayLedgerError(RuntimeError):
    """Persistent replay state could not be read or committed safely."""


@dataclass(frozen=True)
class ChatTrigger:
    message: str
    source: str
    versioned: bool = False
    request_id: str = ""
    source_id: str = ""
    room: str = ""
    wake_word_id: str = ""
    backend: str = ""
    timestamp: str = ""


def _clean_label(value: Any, field: str, *, maximum: int = MAX_LABEL_LENGTH) -> str:
    if not isinstance(value, str):
        raise TriggerError(f"invalid_{field}")
    clean = value.strip()
    if not clean or len(clean) > maximum or any(ord(character) < 32 for character in clean):
        raise TriggerError(f"invalid_{field}")
    return clean


def _parse_timestamp(value: Any, now: dt.datetime) -> str:
    raw = _clean_label(value, "timestamp")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TriggerError("invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise TriggerError("invalid_timestamp")
    parsed = parsed.astimezone(dt.UTC)
    age = (now - parsed).total_seconds()
    if age > MAX_EVENT_AGE_SECONDS:
        raise TriggerError("stale_timestamp")
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise TriggerError("future_timestamp")
    return raw


def _looks_versioned(payload: dict[str, Any]) -> bool:
    return "version" in payload or payload.get("event") == "wake_command_detected"


def parse_chat_trigger(raw_payload: str, *, now: dt.datetime | None = None) -> ChatTrigger | None:
    """Parse legacy chat payloads or the strict version-1 wake envelope."""
    raw = str(raw_payload or "").strip()
    if not raw:
        return None
    if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise TriggerError("payload_too_large")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ChatTrigger(message=raw, source="chat")

    if not isinstance(parsed, dict):
        # Preserve the old behavior for JSON scalars: they remain literal chat text.
        return ChatTrigger(message=raw, source="chat")
    if not _looks_versioned(parsed):
        message = str(parsed.get("message", "")).strip()
        if not message:
            return None
        source = str(parsed.get("source", "chat")).strip() or "chat"
        return ChatTrigger(message=message, source=source)

    required = {
        "version",
        "event",
        "message",
        "source",
        "request_id",
        "source_id",
        "room",
        "wake_word_id",
        "backend",
        "timestamp",
    }
    if set(parsed) != required:
        raise TriggerError("invalid_fields")
    if isinstance(parsed.get("version"), bool) or parsed.get("version") != 1:
        raise TriggerError("unsupported_version")
    if parsed.get("event") != "wake_command_detected":
        raise TriggerError("unsupported_event")
    if parsed.get("source") != "rtsp_assist_gateway":
        raise TriggerError("unsupported_source")
    if parsed.get("backend") not in SUPPORTED_BACKENDS:
        raise TriggerError("unsupported_backend")

    message = _clean_label(parsed.get("message"), "message", maximum=MAX_COMMAND_LENGTH)
    request_id = _clean_label(parsed.get("request_id"), "request_id", maximum=36)
    try:
        canonical_request_id = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise TriggerError("invalid_request_id") from exc
    if request_id.lower() != canonical_request_id:
        raise TriggerError("invalid_request_id")

    source_id = _clean_label(parsed.get("source_id"), "source_id")
    wake_word_id = _clean_label(parsed.get("wake_word_id"), "wake_word_id")
    backend = _clean_label(parsed.get("backend"), "backend")
    if not ID_RE.fullmatch(source_id):
        raise TriggerError("invalid_source_id")
    if not ID_RE.fullmatch(wake_word_id):
        raise TriggerError("invalid_wake_word_id")
    room = _clean_label(parsed.get("room"), "room")
    event_time = _parse_timestamp(parsed.get("timestamp"), now or dt.datetime.now(dt.UTC))

    return ChatTrigger(
        message=message,
        source="voice",
        versioned=True,
        request_id=canonical_request_id,
        source_id=source_id,
        room=room,
        wake_word_id=wake_word_id,
        backend=backend,
        timestamp=event_time,
    )


class RequestReplayLedger:
    """Bounded persistent at-most-once request-ID claim ledger."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        ttl_seconds: int = REPLAY_TTL_SECONDS,
        max_entries: int = REPLAY_MAX_ENTRIES,
        lock: threading.Lock | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.lock = lock or threading.Lock()

    def _read_entries(self) -> dict[str, float]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("entries"), dict):
                raise ValueError
            return {
                str(key): float(value)
                for key, value in payload["entries"].items()
                if isinstance(key, str) and isinstance(value, (int, float))
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReplayLedgerError("replay_ledger_unreadable") from exc

    def _write_entries(self, entries: dict[str, float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "entries": entries},
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ReplayLedgerError("replay_ledger_unwritable") from exc

    def claim(self, request_id: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        with self.lock:
            entries = self._read_entries()
            entries = {
                key: seen_at
                for key, seen_at in entries.items()
                if current - seen_at <= self.ttl_seconds and seen_at <= current + MAX_FUTURE_SKEW_SECONDS
            }
            if request_id in entries:
                return False
            entries[request_id] = current
            if len(entries) > self.max_entries:
                entries = dict(
                    sorted(entries.items(), key=lambda item: item[1], reverse=True)[: self.max_entries]
                )
            self._write_entries(entries)
            return True


def update_location_belief(data_dir: str, trigger: ChatTrigger) -> None:
    """Atomically record the room inferred by the household routing boundary."""
    path = Path(data_dir) / "location_belief.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(
        {
            "room": trigger.room,
            "source": trigger.source_id,
            "method": "wake_word_gateway",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
