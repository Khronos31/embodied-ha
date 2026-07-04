#!/usr/bin/env python3
"""Realtime audio STT daemon for Embodied HA."""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse
from typing import Any
import wave
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from auditory_context import append_auditory_event
from sensory_origin import area_for_entity, classify_sensory_origin, infer_room_from_text, resolve_room
from state_utils import clean, now, parse_ts, read_json

try:
    from pysilero_vad import SileroVoiceActivityDetector
except Exception:  # pragma: no cover - exercised through fallback path
    SileroVoiceActivityDetector = None


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK_SAMPLES = 512
CHUNK_BYTES = CHUNK_SAMPLES * SAMPLE_WIDTH
PREBUFFER_SECONDS = 0.3
SILENCE_SECONDS = 0.8
MIN_SEGMENT_SECONDS = 0.5
MAX_SEGMENT_SECONDS = 30.0
VAD_THRESHOLD = 0.5
FALLBACK_DB_THRESHOLD = -47.0
FALLBACK_SEGMENT_MIN_SPEECH_RATIO = 0.12
FALLBACK_SEGMENT_MIN_PEAK_DB = -42.0
FALLBACK_SEGMENT_HARD_PEAK_DB = -36.0
NON_SPEECH_IMPORTANCE_THRESHOLD = 0.55
NON_SPEECH_EMPTY_TRANSCRIPTION_THRESHOLD = 0.65
TMP_DIR = Path("/tmp/embodied-ha/audio-daemon")
DEFAULT_AUDIO_LOG_FILE = "/data/embodied-ha/log/audio_log.jsonl"
DEFAULT_BACKGROUND_AUDIO_LOG_FILE = "/data/embodied-ha/log/background_audio_log.jsonl"
DEFAULT_NON_SPEECH_AUDIO_EVENTS_FILE = "/data/embodied-ha/log/non_speech_audio_events.jsonl"
BACKGROUND_LOG_MIN_INTERVAL_SECONDS = 300
NON_SPEECH_AUDIO_RETENTION_HOURS = 24
NON_SPEECH_MAX_CLIP_SECONDS = 8.0
BACKGROUND_LOG_RETENTION_HOURS = 24
_LOG_LOCK = threading.Lock()
_BACKGROUND_LOG_LOCK = threading.Lock()
_NON_SPEECH_LOCK = threading.Lock()
_HA_STATES_CACHE: dict[str, object] = {"expires_at": 0.0, "states": []}
_non_speech_cache: dict[tuple, float] = {}
TRANSCRIPT_DEDUP_WINDOW_SECONDS = 5.0
_TRANSCRIPT_DEDUP_CACHE: dict[str, tuple[str, float]] = {}
_TRANSCRIPT_DEDUP_LOCK = threading.Lock()

MOTION_CLASSES = {"motion", "occupancy", "presence"}
MOTION_ACTIVE_STATES = {"on", "open", "occupied", "detected", "home"}
HA_STATES_CACHE_TTL_SECONDS = 15.0
RECENT_MOTION_WINDOW_MINUTES = 20
RECENT_VISUAL_WINDOW_MINUTES = 60
RELATED_HA_STATE_LIMIT = 5
LOCATION_PRIOR_ROOM_LIMIT = 4



@dataclass(frozen=True)
class AudioSourceConfig:
    source: str
    label: str
    retention_hours: int
    wake_word_enabled: bool
    background_only: bool = False
    room: str = ""
    note: str = ""
    transport: str = "alsa"
    host: str = ""
    port: int = 0
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    audio_format: str = "s16le"


@dataclass(frozen=True)
class RuntimeSettings:
    config: AudioSourceConfig
    provider: str | None
    language: str
    wake_words: list[str]
    stt_enabled: bool


def log(message: str) -> None:
    print(f"[audio-daemon] {message}", file=sys.stderr, flush=True)


def prefs_path() -> str:
    return os.environ.get("EHA_PREFS_FILE", "")


def default_audio_log_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "audio_log.jsonl")
    return "/config/embodied-ha/log/audio_log.jsonl"


def audio_log_path() -> str:
    return clean(os.environ.get("EHA_AUDIO_LOG_FILE")) or default_audio_log_path() or DEFAULT_AUDIO_LOG_FILE


def default_background_audio_log_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "background_audio_log.jsonl")
    return "/config/embodied-ha/log/background_audio_log.jsonl"


def background_audio_log_path() -> str:
    return (
        clean(os.environ.get("EHA_BACKGROUND_AUDIO_LOG_FILE"))
        or default_background_audio_log_path()
        or DEFAULT_BACKGROUND_AUDIO_LOG_FILE
    )


def background_audio_retention_hours() -> int:
    try:
        return max(1, int(clean(os.environ.get("EHA_BACKGROUND_AUDIO_RETENTION_HOURS")) or BACKGROUND_LOG_RETENTION_HOURS))
    except Exception:
        return BACKGROUND_LOG_RETENTION_HOURS


def default_non_speech_audio_events_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "non_speech_audio_events.jsonl")
    return "/config/embodied-ha/log/non_speech_audio_events.jsonl"


def default_body_location_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "body_location.json")
    return "/config/embodied-ha/body_location.json"


def body_location_path() -> str:
    return clean(os.environ.get("EHA_BODY_LOCATION_FILE")) or default_body_location_path()


def non_speech_audio_events_path() -> str:
    return (
        clean(os.environ.get("EHA_NON_SPEECH_AUDIO_EVENTS_FILE"))
        or default_non_speech_audio_events_path()
        or DEFAULT_NON_SPEECH_AUDIO_EVENTS_FILE
    )


def default_audio_wav_dir() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "wav")
    return "/config/embodied-ha/wav"


def audio_wav_dir() -> str:
    return clean(os.environ.get("EHA_AUDIO_WAV_DIR")) or default_audio_wav_dir()


def non_speech_audio_retention_hours() -> int:
    try:
        return max(1, int(clean(os.environ.get("EHA_NON_SPEECH_AUDIO_RETENTION_HOURS")) or NON_SPEECH_AUDIO_RETENTION_HOURS))
    except Exception:
        return NON_SPEECH_AUDIO_RETENTION_HOURS


def canonical_source(value: str) -> str:
    source = clean(value)
    return "alsa://default" if source in {"", "alsa", "default"} else source


def log_dir_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    fallback = os.path.join(data_dir, "log") if data_dir else "/config/embodied-ha/log"
    return clean(os.environ.get("EHA_LOG_DIR")) or fallback


def scene_state_path() -> str:
    return os.path.join(log_dir_path(), "scene_state.json")


def ha_api_base() -> str:
    return clean(os.environ.get("HA_URL")) or "http://supervisor/core/api"


def ha_token() -> str:
    return clean(os.environ.get("SUPERVISOR_TOKEN")) or clean(os.environ.get("HASSIO_TOKEN"))


def ha_api_json(path: str) -> Any:
    token = ha_token()
    if not token:
        return None
    target = path if path.startswith("http://") or path.startswith("https://") else f"{ha_api_base().rstrip('/')}" + path
    request = urllib.request.Request(target, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset, errors="replace"))
    except Exception:
        return None


def get_current_ha_states(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    now_ts = time.monotonic()
    if not force_refresh:
        try:
            expires_at = float(_HA_STATES_CACHE.get("expires_at") or 0.0)
        except Exception:
            expires_at = 0.0
        if expires_at > now_ts:
            states = _HA_STATES_CACHE.get("states")
            if isinstance(states, list):
                return [row for row in states if isinstance(row, dict)]
    data = ha_api_json("/states")
    states = data if isinstance(data, list) else []
    _HA_STATES_CACHE["states"] = [row for row in states if isinstance(row, dict)]
    _HA_STATES_CACHE["expires_at"] = now_ts + HA_STATES_CACHE_TTL_SECONDS
    return [row for row in _HA_STATES_CACHE["states"] if isinstance(row, dict)]


def state_by_entity_id(states: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in states:
        if not isinstance(row, dict):
            continue
        eid = clean(row.get("entity_id"))
        if eid:
            out[eid] = row
    return out


def _looks_like_motion(entity_id: str, title: str = "", attributes: dict[str, Any] | None = None) -> bool:
    entity_id = clean(entity_id)
    if not entity_id.startswith("binary_sensor."):
        return False
    if entity_id.endswith("_motion"):
        return True
    attrs = attributes if isinstance(attributes, dict) else {}
    if clean(attrs.get("device_class")).lower() in MOTION_CLASSES:
        return True
    title_text = clean(title).lower()
    return "人感" in clean(title) or "motion" in title_text or "occupancy" in title_text or "presence" in title_text


def _entity_area_room(entity_id: str) -> str | None:
    return resolve_room(area_for_entity(entity_id))


def _infer_item_room(item: dict[str, Any], group_title: str = "") -> str | None:
    if not isinstance(item, dict):
        return None
    for candidate in (item.get("room"), item.get("area"), item.get("label"), group_title, item.get("note")):
        room_id = resolve_room(candidate) or infer_room_from_text(candidate)
        if room_id:
            return room_id
    entity_id = clean(item.get("entity")) or clean(item.get("entity_id"))
    if entity_id:
        room_id = _entity_area_room(entity_id) or infer_room_from_text(entity_id)
        if room_id:
            return room_id
    return None


def _minutes_ago(timestamp: str | None, *, reference_ts: str | None = None) -> float | None:
    parsed = parse_ts(timestamp) if timestamp else None
    reference = parse_ts(reference_ts) if reference_ts else now()
    if parsed is None or reference is None:
        return None
    return round(max(0.0, (reference - parsed).total_seconds() / 60.0), 1)


def _format_state_value(row: dict[str, Any]) -> str:
    state = clean(row.get("state"))
    if state in {"unknown", "unavailable", ""}:
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        for key in ("friendly_name", "temperature", "current_temperature"):
            value = clean(attrs.get(key))
            if value:
                return value
    return state


def _state_is_active_motion(value: Any) -> bool:
    state = clean(value).lower()
    return bool(state) and state in MOTION_ACTIVE_STATES


def load_recent_scenes(limit: int = 20) -> list[dict[str, Any]]:
    data = read_json(scene_state_path(), {})
    scenes = data.get("scenes") if isinstance(data, dict) else []
    if not isinstance(scenes, list):
        return []
    return [scene for scene in scenes[-max(1, limit):] if isinstance(scene, dict)]


def build_recent_visual_context(source_room: str | None, timestamp: str) -> dict[str, Any] | None:
    if not source_room:
        return None
    candidates: list[tuple[Any, dict[str, Any]]] = []
    for scene in reversed(load_recent_scenes()):
        camera_pose = scene.get("camera_pose") if isinstance(scene.get("camera_pose"), dict) else {}
        room_id = resolve_room(camera_pose.get("room")) or infer_room_from_text(scene.get("source"), camera_pose.get("room"))
        if room_id != source_room:
            continue
        scene_minutes_ago = _minutes_ago(clean(scene.get("timestamp")), reference_ts=timestamp)
        if scene_minutes_ago is not None and scene_minutes_ago > RECENT_VISUAL_WINDOW_MINUTES:
            continue
        objects = [clean(item.get("label")) for item in scene.get("objects", []) if isinstance(item, dict) and clean(item.get("label"))]
        people = [clean(item.get("label")) for item in scene.get("people", []) if isinstance(item, dict) and clean(item.get("label"))]
        payload = {
            "scene_id": clean(scene.get("id")) or None,
            "source": clean(scene.get("source")) or None,
            "room": room_id,
            "timestamp": clean(scene.get("timestamp")) or None,
            "minutes_ago": scene_minutes_ago,
            "changes": [clean(item) for item in (scene.get("changes") or []) if clean(item)][:3],
            "objects": objects[:5],
            "people": people[:3],
        }
        candidates.append((parse_ts(scene.get("timestamp")) or now(), payload))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def motion_entities_for_room(source_room: str | None, preferences: dict[str, Any], states: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not source_room:
        return []
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    sensors = preferences.get("sensors") if isinstance(preferences, dict) else {}
    groups = sensors.get("groups") if isinstance(sensors, dict) else []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            title = clean(group.get("title"))
            for item in group.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                entity_id = clean(item.get("entity"))
                if not entity_id or entity_id in seen or not _looks_like_motion(entity_id, title):
                    continue
                room_id = _infer_item_room(item, title)
                if room_id != source_room:
                    continue
                seen.add(entity_id)
                results.append({
                    "entity_id": entity_id,
                    "label": clean(item.get("label")) or entity_id,
                    "room": room_id,
                })
    if results:
        return results
    for row in states:
        entity_id = clean(row.get("entity_id"))
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        if not _looks_like_motion(entity_id, clean(attrs.get("friendly_name")), attrs):
            continue
        room_id = _entity_area_room(entity_id) or infer_room_from_text(attrs.get("friendly_name"), entity_id)
        if room_id != source_room or entity_id in seen:
            continue
        seen.add(entity_id)
        results.append({
            "entity_id": entity_id,
            "label": clean(attrs.get("friendly_name")) or entity_id,
            "room": room_id,
        })
    return results


def build_recent_motion_context(source_room: str | None, timestamp: str, preferences: dict[str, Any], states: list[dict[str, Any]]) -> dict[str, Any] | None:
    motion_entities = motion_entities_for_room(source_room, preferences, states)
    if not motion_entities:
        return None
    start = (parse_ts(timestamp) or now()) - timedelta(minutes=RECENT_MOTION_WINDOW_MINUTES)
    csv = ",".join(entity["entity_id"] for entity in motion_entities)
    path = f"/history/period/{quote(start.astimezone().isoformat())}?filter_entity_id={csv}&minimal_response"
    data = ha_api_json(path)
    if not isinstance(data, list):
        return None
    label_map = {item["entity_id"]: item for item in motion_entities}
    events: list[dict[str, Any]] = []
    for series in data:
        if not isinstance(series, list) or not series:
            continue
        entity_id = clean(series[0].get("entity_id"))
        meta = label_map.get(entity_id)
        if not meta:
            continue
        for row in series:
            if not isinstance(row, dict) or not _state_is_active_motion(row.get("state")):
                continue
            event_ts = clean(row.get("last_changed") or row.get("last_updated"))
            if not event_ts:
                continue
            events.append({
                "entity_id": entity_id,
                "label": meta["label"],
                "room": meta["room"],
                "state": clean(row.get("state")),
                "timestamp": event_ts,
                "minutes_ago": _minutes_ago(event_ts, reference_ts=timestamp),
            })
    if not events:
        return None
    events.sort(key=lambda item: parse_ts(item.get("timestamp")) or now(), reverse=True)
    return {
        "window_minutes": RECENT_MOTION_WINDOW_MINUTES,
        "events": events[:3],
    }


def room_related_sensor_items(source_room: str | None, preferences: dict[str, Any]) -> list[dict[str, str]]:
    if not source_room:
        return []
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    sensors = preferences.get("sensors") if isinstance(preferences, dict) else {}
    groups = sensors.get("groups") if isinstance(sensors, dict) else []
    if not isinstance(groups, list):
        return results
    for group in groups:
        if not isinstance(group, dict):
            continue
        title = clean(group.get("title"))
        for item in group.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            entity_id = clean(item.get("entity")) or clean(item.get("entity_id"))
            if not entity_id or entity_id in seen:
                continue
            room_id = _infer_item_room(item, title)
            if room_id != source_room:
                continue
            seen.add(entity_id)
            results.append({
                "entity_id": entity_id,
                "label": clean(item.get("label")) or clean(title) or entity_id,
                "group": title,
            })
    return results


def build_related_ha_state_context(source_room: str | None, timestamp: str, preferences: dict[str, Any], states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = state_by_entity_id(states)
    results: list[dict[str, Any]] = []
    for item in room_related_sensor_items(source_room, preferences)[:RELATED_HA_STATE_LIMIT]:
        row = by_id.get(item["entity_id"])
        if not row:
            continue
        results.append({
            "entity_id": item["entity_id"],
            "label": item["label"],
            "group": item.get("group") or None,
            "state": _format_state_value(row),
            "changed_minutes_ago": _minutes_ago(clean(row.get("last_changed") or row.get("last_updated")), reference_ts=timestamp),
        })
    presence_entity = clean((preferences.get("presence") or {}).get("entity")) if isinstance(preferences, dict) else ""
    if presence_entity and all(row.get("entity_id") != presence_entity for row in results):
        row = by_id.get(presence_entity)
        if row:
            results.append({
                "entity_id": presence_entity,
                "label": "在宅",
                "group": "presence",
                "state": _format_state_value(row),
                "changed_minutes_ago": _minutes_ago(clean(row.get("last_changed") or row.get("last_updated")), reference_ts=timestamp),
            })
    return results[:RELATED_HA_STATE_LIMIT]


def build_location_prior_context(source_room: str | None, body_room: str | None, recent_motion: dict[str, Any] | None, recent_visual_context: dict[str, Any] | None) -> dict[str, Any] | None:
    scores: dict[str, float] = {}
    basis: list[str] = []

    def boost(room_id: str | None, amount: float, reason: str) -> None:
        room_key = clean(room_id)
        if not room_key:
            return
        scores[room_key] = scores.get(room_key, 0.0) + amount
        basis.append(f"{reason}:{room_key}")

    boost(source_room, 0.35, "source_room")
    boost(body_room, 0.30, "body_room")
    if isinstance(recent_motion, dict):
        for index, event in enumerate(recent_motion.get("events") or []):
            if not isinstance(event, dict):
                continue
            room_id = clean(event.get("room")) or source_room
            minutes_ago = event.get("minutes_ago")
            try:
                minutes_value = float(minutes_ago)
            except Exception:
                minutes_value = RECENT_MOTION_WINDOW_MINUTES
            amount = 0.30 if minutes_value <= 5 else 0.18 if minutes_value <= 15 else 0.10
            amount *= max(0.4, 1.0 - (0.15 * index))
            boost(room_id, amount, "recent_motion")
    if isinstance(recent_visual_context, dict):
        boost(recent_visual_context.get("room"), 0.15, "recent_visual")

    if not scores:
        return None
    total = sum(scores.values()) or 1.0
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:LOCATION_PRIOR_ROOM_LIMIT]
    return {
        "best_room": ordered[0][0],
        "candidate_rooms": [
            {"room": room_id, "score": round(score / total, 3)}
            for room_id, score in ordered
        ],
        "basis": basis[:8],
    }


def build_non_speech_situational_context(config: AudioSourceConfig, timestamp: str, sensory: dict[str, Any]) -> dict[str, Any]:
    source_room = clean(sensory.get("source_room")) or None
    body_room = clean(sensory.get("body_room")) or None
    preferences = load_preferences()
    states = get_current_ha_states()
    recent_motion = build_recent_motion_context(source_room, timestamp, preferences, states)
    recent_visual_context = build_recent_visual_context(source_room, timestamp)
    related_ha_state = build_related_ha_state_context(source_room, timestamp, preferences, states)
    return {
        "body_room": body_room,
        "source_room": source_room,
        "sensory_origin": sensory.get("sensory_origin"),
        "move_cost": sensory.get("move_cost"),
        "time_of_day": _time_of_day(timestamp),
        "recent_motion": recent_motion,
        "related_ha_state": related_ha_state,
        "recent_visual_context": recent_visual_context,
        "location_prior": build_location_prior_context(source_room, body_room, recent_motion, recent_visual_context),
    }


def load_preferences() -> dict:
    path = prefs_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log(f"failed to load preferences: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def background_hearing_enabled(item: dict) -> bool:
    if "background_hearing_enabled" in item:
        return item.get("background_hearing_enabled") is True
    return True


def _audio_source_entry_by_source(preferences: dict | None, source: str) -> dict | None:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    raw_sources = prefs.get("mics")
    if not isinstance(raw_sources, list):
        return None
    target = clean(source)
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        if _source_identity(item, infer_transport(item)) == target:
            return item
    return None


def _location_belief_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR")) or "/config/embodied-ha"
    return os.path.join(data_dir, "location_belief.json")


def _update_user_location_belief(room: str, source: str) -> None:
    """ウェイクワード検知時にユーザーの推定位置を更新する。エージェント自身の body_location は変更しない。"""
    path = _location_belief_path()
    state = read_json(path, {})
    if not isinstance(state, dict):
        state = {}
    state["room"] = room
    state["source"] = source
    state["method"] = "wake_word"
    _write_json_atomic(path, state)


def update_current_room_from_audio_source(config: AudioSourceConfig, preferences: dict | None = None) -> None:
    entry = _audio_source_entry_by_source(preferences, config.source)
    if not isinstance(entry, dict):
        return
    room = clean(entry.get("room"))
    if not room:
        return
    try:
        _update_user_location_belief(room, config.source)
    except Exception:
        return
    print(f"[audio] wake word: user location_belief → {room}")


def default_active_listen_request_dir() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "runtime", "active_listen_requests")
    return "/config/embodied-ha/runtime/active_listen_requests"


def active_listen_request_dir() -> str:
    return clean(os.environ.get("EHA_ACTIVE_LISTEN_REQUEST_DIR")) or default_active_listen_request_dir()


def _write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _load_json_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _pending_active_listen_requests(config: AudioSourceConfig) -> list[dict]:
    directory = active_listen_request_dir()
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    now_ts = time.time()
    requests: list[dict] = []
    for name in names:
        if not name.endswith('.json') or name.endswith('.response.json'):
            continue
        req_path = os.path.join(directory, name)
        payload = _load_json_file(req_path)
        if not payload:
            continue
        if clean(payload.get('source')) != config.source:
            continue
        request_id = clean(payload.get('request_id'))
        output_path = clean(payload.get('output_path'))
        response_path = clean(payload.get('response_path'))
        try:
            duration = int(payload.get('duration') or 0)
        except Exception:
            duration = 0
        try:
            created_at = float(payload.get('created_at') or 0.0)
        except Exception:
            created_at = 0.0
        if not request_id or not output_path or not response_path or duration <= 0:
            continue
        if created_at and now_ts - created_at > max(15.0, duration + 15.0):
            _write_json_atomic(response_path, {
                'ok': False,
                'request_id': request_id,
                'source': config.source,
                'error': 'request expired',
            })
            try:
                os.unlink(req_path)
            except FileNotFoundError:
                pass
            continue
        requests.append({
            'request_id': request_id,
            'source': config.source,
            'duration': duration,
            'output_path': output_path,
            'response_path': response_path,
            'request_path': req_path,
            'expected_bytes': duration * SAMPLE_RATE * SAMPLE_WIDTH,
        })
    return requests


def _finalize_active_listen_capture(request: dict, audio_bytes: bytes, error: str | None = None) -> None:
    response = {
        'ok': error is None,
        'request_id': request.get('request_id'),
        'source': request.get('source'),
        'output_path': request.get('output_path'),
    }
    if error is None:
        try:
            write_wav(request['output_path'], audio_bytes)
            response['captured_bytes'] = len(audio_bytes)
            response['duration_sec'] = round(len(audio_bytes) / float(SAMPLE_RATE * SAMPLE_WIDTH), 2)
        except Exception as exc:
            error = str(exc) or 'failed to write capture'
    if error is not None:
        response['ok'] = False
        response['error'] = error
    _write_json_atomic(request['response_path'], response)
    try:
        os.unlink(request['request_path'])
    except FileNotFoundError:
        pass


def _service_active_listen_requests(
    config: AudioSourceConfig,
    chunk: bytes,
    active_requests: dict[str, dict],
    last_scan_at: float,
) -> float:
    now_ts = time.monotonic()
    if now_ts - last_scan_at >= 0.25:
        for request in _pending_active_listen_requests(config):
            request_id = request['request_id']
            if request_id not in active_requests:
                active_requests[request_id] = {**request, 'buffer': bytearray()}
        last_scan_at = now_ts

    completed: list[str] = []
    for request_id, state in list(active_requests.items()):
        state['buffer'].extend(chunk)
        if len(state['buffer']) >= state['expected_bytes']:
            _finalize_active_listen_capture(state, bytes(state['buffer'][:state['expected_bytes']]))
            completed.append(request_id)
    for request_id in completed:
        active_requests.pop(request_id, None)
    return last_scan_at


def normalize_source_uri(value: str) -> str:
    source = clean(value)
    if source in {"", "alsa", "default"}:
        return "alsa://default"
    if source.startswith("alsa://"):
        device = source[len("alsa://"):].lstrip("/")
        return f"alsa://{device or 'default'}"
    return source


def infer_transport(item: dict) -> str:
    source = normalize_source_uri(item.get("source"))
    if source.startswith("rtsp://"):
        return "rtsp"
    if source.startswith("tcp://"):
        return "tcp_pull"
    if source.startswith("alsa://"):
        return "alsa"
    if clean(item.get("host")) or clean(item.get("port")):
        return "tcp_pull"
    return "unknown"


def _source_identity(item: dict, transport: str) -> str:
    source = normalize_source_uri(item.get("source"))
    if transport == "tcp_pull" and source and not source.startswith("tcp://") and clean(item.get("host")):
        host = clean(item.get("host"))
        port = clean(item.get("port"))
        if host and port:
            return f"tcp://{host}:{port}"
    return source


def parse_tcp_port(value) -> int | None:
    try:
        port = int(value)
    except Exception:
        return None
    if port <= 0 or port > 65535:
        return None
    return port


def parse_source_uri(source: str) -> tuple[str, str, str, int] | None:
    normalized = normalize_source_uri(source)
    if normalized.startswith("alsa://"):
        device = normalized[len("alsa://"):].lstrip("/") or "default"
        return "alsa", normalized, device, 0
    if normalized.startswith("rtsp://"):
        return "rtsp", normalized, "", 0
    if normalized.startswith("tcp://"):
        parsed = urlparse(normalized)
        host = clean(parsed.hostname)
        port = parse_tcp_port(parsed.port)
        if not host or port is None:
            return None
        if clean(parsed.path) not in {"", "/"}:
            return None
        return "tcp_pull", normalized, host, port
    return None


def build_audio_source_config(item: dict) -> AudioSourceConfig | None:
    if not isinstance(item, dict):
        return None
    transport = infer_transport(item)
    source = _source_identity(item, transport)
    if not source:
        log(f"invalid audio source config: missing source for transport={transport}")
        return None
    parsed = parse_source_uri(source)
    if parsed is None:
        log(f"invalid audio source config: unsupported source URI {source}")
        return None
    transport, source, host_or_device, port = parsed
    label = clean(item.get("label")) or source
    room = clean(item.get("room"))
    if not room:
        log(f"invalid audio source config for {label}: room is required")
        return None
    try:
        retention = int(item.get("stt_retention_hours", 60))
    except Exception:
        retention = 60
    background_only = retention <= 0
    if background_only and not background_hearing_enabled(item):
        return None

    host = ""
    sample_rate = SAMPLE_RATE
    channels = CHANNELS
    audio_format = "s16le"

    if transport == "tcp_pull":
        host = host_or_device
        try:
            sample_rate = int(item.get("sample_rate", SAMPLE_RATE))
        except Exception:
            sample_rate = -1
        try:
            channels = int(item.get("channels", CHANNELS))
        except Exception:
            channels = -1
        audio_format = clean(item.get("format")).lower() or "s16le"
        if sample_rate != SAMPLE_RATE or channels != CHANNELS or audio_format != "s16le":
            log(
                f"invalid audio source config for {label}: tcp_pull only supports "
                f"sample_rate={SAMPLE_RATE}, channels={CHANNELS}, format=s16le "
                f"(got sample_rate={sample_rate}, channels={channels}, format={audio_format or 'unset'})"
            )
            return None
    elif transport == "alsa":
        host = host_or_device

    return AudioSourceConfig(
        source=source,
        label=label,
        retention_hours=max(1, retention) if not background_only else background_audio_retention_hours(),
        wake_word_enabled=bool(item.get("wake_word_enabled")),
        background_only=background_only,
        room=room,
        note=clean(item.get("note")),
        transport=transport,
        host=host,
        port=port,
        sample_rate=sample_rate,
        channels=channels,
        audio_format=audio_format,
    )


def load_enabled_mics(preferences: dict | None = None) -> list[AudioSourceConfig]:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    raw_sources = prefs.get("mics")
    if not isinstance(raw_sources, list):
        return []

    enabled: list[AudioSourceConfig] = []
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        if item.get("stt_enabled") is not True:
            continue
        config = build_audio_source_config(item)
        if config is None:
            continue
        enabled.append(config)
    return enabled


def load_stt_provider(preferences: dict | None = None) -> str | None:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    provider = clean(prefs.get("stt_provider"))
    return provider or None


def load_stt_language(preferences: dict | None = None) -> str:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    language = clean(prefs.get("stt_language"))
    return language or "ja-JP"


def load_wake_words(preferences: dict | None = None) -> list[str]:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    words = prefs.get("wake_words")
    if not isinstance(words, list):
        return []
    normalized = [clean(word).lower() for word in words if clean(word)]
    return normalized


def load_runtime_settings(
    base_config: AudioSourceConfig,
    preferences: dict | None = None,
) -> RuntimeSettings:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    provider = load_stt_provider(prefs)
    language = load_stt_language(prefs)
    wake_words = load_wake_words(prefs)
    effective_config = base_config
    stt_enabled = True

    raw_sources = prefs.get("mics")
    if isinstance(raw_sources, list):
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            source = _source_identity(item, infer_transport(item))
            if source != base_config.source:
                continue
            label = clean(item.get("label")) or source
            if label != base_config.label:
                continue
            try:
                retention = int(item.get("stt_retention_hours", base_config.retention_hours))
            except Exception:
                retention = base_config.retention_hours
            background_only = retention <= 0 and background_hearing_enabled(item)
            effective_config = AudioSourceConfig(
                source=base_config.source,
                label=base_config.label,
                retention_hours=max(1, retention) if not background_only else background_audio_retention_hours(),
                wake_word_enabled=bool(item.get("wake_word_enabled")),
                background_only=background_only,
                room=clean(item.get("room")) or base_config.room,
                note=clean(item.get("note")) or base_config.note,
                transport=base_config.transport,
                host=base_config.host,
                port=base_config.port,
                sample_rate=base_config.sample_rate,
                channels=base_config.channels,
                audio_format=base_config.audio_format,
            )
            stt_enabled = item.get("stt_enabled") is True and retention > 0
            break

    return RuntimeSettings(
        config=effective_config,
        provider=provider,
        language=language,
        wake_words=wake_words,
        stt_enabled=stt_enabled,
    )


def build_ffmpeg_command(config: AudioSourceConfig) -> list[str]:
    if config.transport == "alsa":
        return [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "alsa",
            "-i",
            config.host or "default",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-f",
            "s16le",
            "-",
        ]
    if config.transport != "rtsp":
        raise ValueError(f"ffmpeg transport unsupported for {config.transport}")
    return [
        "ffmpeg",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        config.source,
        "-vn",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-f",
        "s16le",
        "-",
    ]


def read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        piece = stream.read(remaining)
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def chunk_db(chunk: bytes) -> float:
    if not chunk:
        return float("-inf")
    samples = memoryview(chunk).cast("h")
    if not samples:
        return float("-inf")
    square_sum = 0.0
    for sample in samples:
        square_sum += float(sample) * float(sample)
    rms = math.sqrt(square_sum / len(samples))
    if rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(rms / 32767.0)


def fallback_voice_probability(chunk: bytes) -> float:
    return 1.0 if chunk_db(chunk) > FALLBACK_DB_THRESHOLD else 0.0


def detect_voice(chunk: bytes, detector) -> float:
    if detector is None:
        return fallback_voice_probability(chunk)
    try:
        return float(detector(chunk))
    except Exception as exc:
        log(f"vad process failed, falling back to energy threshold: {exc}")
        return fallback_voice_probability(chunk)


def summarize_chunk_levels(levels: list[float]) -> tuple[float | None, float | None]:
    finite_levels = [level for level in levels if math.isfinite(level)]
    if not finite_levels:
        return None, None
    peak_db = max(finite_levels)
    mean_db = sum(finite_levels) / len(finite_levels)
    return round(peak_db, 1), round(mean_db, 1)


def should_transcribe_segment(vad_mode: str, diagnostics: dict | None) -> tuple[bool, str | None]:
    if vad_mode != "fallback":
        return True, None
    if not isinstance(diagnostics, dict):
        return True, None

    speech_ratio = diagnostics.get("speech_ratio")
    peak_db = diagnostics.get("peak_db")
    try:
        speech_ratio_value = float(speech_ratio)
    except Exception:
        speech_ratio_value = None
    try:
        peak_db_value = float(peak_db)
    except Exception:
        peak_db_value = None

    if peak_db_value is not None and peak_db_value >= FALLBACK_SEGMENT_HARD_PEAK_DB:
        return True, None
    if speech_ratio_value is None or peak_db_value is None:
        return False, "fallback_gate_missing_metrics"
    if speech_ratio_value < FALLBACK_SEGMENT_MIN_SPEECH_RATIO:
        return False, "fallback_gate_low_speech_ratio"
    if peak_db_value < FALLBACK_SEGMENT_MIN_PEAK_DB:
        return False, "fallback_gate_low_peak_db"
    return True, None


def write_wav(path: str, audio_bytes: bytes) -> None:
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(audio_bytes)


def _iter_samples(audio_bytes: bytes):
    if not audio_bytes:
        return
    usable = len(audio_bytes) - (len(audio_bytes) % SAMPLE_WIDTH)
    if usable <= 0:
        return
    samples = memoryview(audio_bytes[:usable]).cast("h")
    for sample in samples:
        yield int(sample)


def _analysis_samples(audio_bytes: bytes, max_samples: int = SAMPLE_RATE) -> list[int]:
    samples = list(_iter_samples(audio_bytes) or [])
    if len(samples) <= max_samples:
        return samples
    step = max(1, len(samples) // max_samples)
    return samples[::step][:max_samples]


def _goertzel_power(samples: list[int], freq_hz: float) -> float:
    if not samples or freq_hz <= 0:
        return 0.0
    normalized = freq_hz / SAMPLE_RATE
    coeff = 2.0 * math.cos(2.0 * math.pi * normalized)
    q0 = q1 = q2 = 0.0
    for sample in samples:
        q0 = coeff * q1 - q2 + (sample / 32768.0)
        q2 = q1
        q1 = q0
    return max(0.0, q1 * q1 + q2 * q2 - coeff * q1 * q2) / max(1, len(samples))


def _band_energy(samples: list[int]) -> dict[str, float]:
    bands = {
        "low": [125.0, 250.0, 500.0],
        "mid": [1000.0, 2000.0],
        "high": [4000.0, 6000.0],
    }
    raw = {name: sum(_goertzel_power(samples, freq) for freq in freqs) for name, freqs in bands.items()}
    total = sum(raw.values())
    if total <= 0:
        return {"low_energy": 0.0, "mid_energy": 0.0, "high_energy": 0.0}
    return {
        "low_energy": round(raw["low"] / total, 3),
        "mid_energy": round(raw["mid"] / total, 3),
        "high_energy": round(raw["high"] / total, 3),
    }


def zero_crossing_rate_hz(samples: list[int]) -> float | None:
    if len(samples) < 2:
        return None
    crossings = 0
    previous = samples[0]
    for sample in samples[1:]:
        if (previous < 0 <= sample) or (previous >= 0 > sample):
            crossings += 1
        previous = sample
    duration = len(samples) / float(SAMPLE_RATE)
    if duration <= 0:
        return None
    return round(crossings / duration, 1)


def build_acoustic_features(audio_bytes: bytes, diagnostics: dict | None = None) -> dict:
    duration_sec = len(audio_bytes) / float(SAMPLE_RATE * SAMPLE_WIDTH)
    samples = _analysis_samples(audio_bytes)
    band = _band_energy(samples)
    zcr = zero_crossing_rate_hz(samples)
    centroid_hint = round(zcr / 2.0, 1) if zcr is not None else None
    if centroid_hint is not None:
        if centroid_hint < 700.0:
            dominant_band = "low"
            band = {"low_energy": 1.0, "mid_energy": 0.0, "high_energy": 0.0}
        elif centroid_hint < 3000.0:
            dominant_band = "mid"
            band = {"low_energy": 0.0, "mid_energy": 1.0, "high_energy": 0.0}
        else:
            dominant_band = "high"
            band = {"low_energy": 0.0, "mid_energy": 0.0, "high_energy": 1.0}
    else:
        dominant_band = max(
            (("low", band["low_energy"]), ("mid", band["mid_energy"]), ("high", band["high_energy"])),
            key=lambda item: item[1],
        )[0]
    peak_db = (diagnostics or {}).get("peak_db")
    mean_db = (diagnostics or {}).get("mean_db")
    speech_ratio = (diagnostics or {}).get("speech_ratio")
    transient = False
    if peak_db is not None and mean_db is not None:
        try:
            transient = float(peak_db) - float(mean_db) >= 12.0
        except Exception:
            transient = False
    periodic = False
    if zcr is not None:
        # Stable tones and buzzes tend to produce a steady crossing rate. This is
        # only a cheap hint; external taggers and human labels decide semantics.
        periodic = 60.0 <= zcr <= 6000.0 and duration_sec >= 0.3 and not transient
    return {
        "duration_sec": round(duration_sec, 2),
        "peak_db": peak_db,
        "mean_db": mean_db,
        "speech_ratio": speech_ratio,
        **band,
        "dominant_band": dominant_band,
        "zero_crossing_rate_hz": zcr,
        "spectral_centroid_hz": centroid_hint,
        "transient": transient,
        "periodic": periodic,
    }


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def non_speech_importance_score(reason: str, features: dict) -> float:
    try:
        peak_db = float(features.get("peak_db"))
    except Exception:
        peak_db = -120.0
    try:
        duration_sec = float(features.get("duration_sec") or 0.0)
    except Exception:
        duration_sec = 0.0
    try:
        speech_ratio = float(features.get("speech_ratio") or 0.0)
    except Exception:
        speech_ratio = 0.0
    high_energy = float(features.get("high_energy") or 0.0)
    mid_energy = float(features.get("mid_energy") or 0.0)
    transient = features.get("transient") is True
    periodic = features.get("periodic") is True

    peak_score = clamp01((peak_db + 42.0) / 10.0)
    duration_score = clamp01((duration_sec - MIN_SEGMENT_SECONDS) / 2.5)
    speech_score = clamp01((speech_ratio - 0.08) / 0.32)
    band_score = 1.0 if (high_energy >= 0.55 or mid_energy >= 0.55) else 0.0
    transient_score = 1.0 if transient else 0.0
    periodic_penalty = 0.1 if periodic and not transient else 0.0

    score = (
        0.45 * peak_score
        + 0.20 * duration_score
        + 0.20 * speech_score
        + 0.10 * band_score
        + 0.10 * transient_score
        - periodic_penalty
    )
    if reason == "empty_transcription":
        score += 0.05 * speech_score
    return round(clamp01(score), 3)


def should_record_non_speech_event(reason: str, features: dict) -> bool:
    try:
        peak_db = float(features.get("peak_db"))
    except Exception:
        peak_db = None
    try:
        mean_db = float(features.get("mean_db"))
    except Exception:
        mean_db = None
    try:
        duration_sec = float(features.get("duration_sec") or 0)
    except Exception:
        duration_sec = 0.0

    if duration_sec < MIN_SEGMENT_SECONDS or peak_db is None:
        return False
    if mean_db is not None and mean_db < FALLBACK_DB_THRESHOLD:
        return False

    score = non_speech_importance_score(reason, features)
    threshold = NON_SPEECH_EMPTY_TRANSCRIPTION_THRESHOLD if reason == "empty_transcription" else NON_SPEECH_IMPORTANCE_THRESHOLD
    return score >= threshold


def non_speech_event_id(timestamp: str, config: AudioSourceConfig, audio_bytes: bytes) -> str:
    digest = hashlib.sha1(audio_bytes[:65536] + config.source.encode("utf-8", errors="ignore")).hexdigest()[:10]
    safe_ts = "".join(ch for ch in timestamp if ch.isdigit())[:14]
    return f"audio_{safe_ts}_{digest}"


def _non_speech_fingerprint(features: dict) -> tuple:
    low = features.get("low_energy", 0)
    mid = features.get("mid_energy", 0)
    high = features.get("high_energy", 0)
    dominant = "low" if low >= mid and low >= high else ("mid" if mid >= high else "high")
    peak_bucket = round(features.get("peak_db", 0) / 5) * 5
    source = features.get("source", "")
    return (source, dominant, peak_bucket)


def _should_suppress_non_speech(features: dict, now_ts: float) -> bool:
    if features.get("transient"):
        return False
    fp = _non_speech_fingerprint(features)
    last = _non_speech_cache.get(fp)
    if last is None:
        _non_speech_cache[fp] = now_ts
        return False
    window = 300.0 if features.get("periodic") else 60.0
    if now_ts - last < window:
        cached_peak = fp[2]
        current_peak = round(features.get("peak_db", 0) / 5) * 5
        if current_peak >= cached_peak + 10:
            _non_speech_cache[fp] = now_ts
            return False
        return True
    _non_speech_cache[fp] = now_ts
    return False


def _claim_transcript_primary(text_value: str, source: str) -> bool:
    """Returns True if this source is the first to produce this transcript within the dedup window.
    Same-source re-utterances always return True to allow genuine repetitions through."""
    key = " ".join(text_value.strip().lower().split())
    if not key:
        return True
    now_ts = time.monotonic()
    with _TRANSCRIPT_DEDUP_LOCK:
        stale = [k for k, (_, ts) in _TRANSCRIPT_DEDUP_CACHE.items() if now_ts - ts > TRANSCRIPT_DEDUP_WINDOW_SECONDS * 2]
        for k in stale:
            del _TRANSCRIPT_DEDUP_CACHE[k]
        cached = _TRANSCRIPT_DEDUP_CACHE.get(key)
        if cached is not None:
            cached_source, cached_ts = cached
            if now_ts - cached_ts <= TRANSCRIPT_DEDUP_WINDOW_SECONDS and cached_source != source:
                return False
        _TRANSCRIPT_DEDUP_CACHE[key] = (source, now_ts)
        return True


def save_non_speech_audio_clip(event_id: str, audio_bytes: bytes) -> str:
    max_bytes = int(NON_SPEECH_MAX_CLIP_SECONDS * SAMPLE_RATE * SAMPLE_WIDTH)
    clipped = audio_bytes[:max_bytes]
    directory = audio_wav_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{event_id}.wav")
    write_wav(path, clipped)
    return path


def _time_of_day(timestamp: str) -> str:
    parsed = parse_ts(timestamp) or now()
    hour = parsed.hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "daytime"
    if 17 <= hour < 22:
        return "evening"
    return "late_night"


def append_non_speech_audio_event(entry: dict, retention_hours: int, source_label: str) -> None:
    path = non_speech_audio_events_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cutoff = now() - timedelta(hours=max(1, retention_hours))
    source = clean(source_label) or clean(entry.get("source"))
    old_wavs: list[str] = []

    with _NON_SPEECH_LOCK:
        entries: list[dict] = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            parsed = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        ts = parse_ts(parsed.get("timestamp"))
                        if source and clean(parsed.get("source")) == source and ts and ts < cutoff:
                            wav_ref = clean(parsed.get("wav_ref"))
                            if wav_ref:
                                old_wavs.append(wav_ref)
                            continue
                        entries.append(parsed)
            except Exception as exc:
                log(f"failed to read non-speech audio events for retention pruning: {exc}")
        entries.append(entry)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)

    wav_root = os.path.abspath(audio_wav_dir())
    for wav_ref in old_wavs:
        try:
            abs_ref = os.path.abspath(wav_ref)
            if abs_ref.startswith(wav_root + os.sep):
                os.unlink(abs_ref)
        except FileNotFoundError:
            pass
        except Exception:
            pass


def record_non_speech_audio_event(
    config: AudioSourceConfig,
    audio_bytes: bytes,
    timestamp: str,
    reason: str,
    diagnostics: dict | None = None,
    error: str | None = None,
) -> dict | None:
    features = build_acoustic_features(audio_bytes, diagnostics)
    features["importance_score"] = non_speech_importance_score(reason, features)
    if _should_suppress_non_speech(features, time.monotonic()):
        return None
    if not should_record_non_speech_event(reason, features):
        return None
    sensory = classify_sensory_origin(
        source=config.source,
        label=config.label,
        room=config.room,
        note=config.note,
        modality="auditory",
    )
    event_id = non_speech_event_id(timestamp, config, audio_bytes)
    try:
        wav_ref = save_non_speech_audio_clip(event_id, audio_bytes)
    except Exception as exc:
        log(f"failed to save non-speech audio clip for {config.label}: {exc}")
        wav_ref = None
    entry = {
        "event_id": event_id,
        "timestamp": timestamp,
        "kind": "non_speech_audio_event",
        "modality": "auditory",
        "origin": config.source,
        "source": config.label,
        "duration_sec": features.get("duration_sec"),
        "importance_score": features.get("importance_score"),
        "has_sound": True,
        "reason": reason,
        "stt_error": error,
        "transcript": None,
        "wav_ref": wav_ref,
        "acoustic_features": features,
        "situational_context": build_non_speech_situational_context(config, timestamp, sensory),
        **sensory,
    }
    if isinstance(diagnostics, dict):
        for key in ("vad_mode", "speech_ratio", "peak_db", "mean_db", "skip_reason"):
            if key in diagnostics:
                entry[key] = diagnostics[key]
    retention_hours = min(max(1, config.retention_hours), non_speech_audio_retention_hours())
    append_non_speech_audio_event(entry, retention_hours, config.label)
    log(
        "non-speech audio event recorded for "
        f"{config.label}: reason={reason}, event_id={event_id}, "
        f"peak_db={features.get('peak_db')}, dominant_band={features.get('dominant_band')}"
    )
    return entry


def transcribe_wav(path: str, provider: str, language: str, token: str) -> str:
    with open(path, "rb") as f:
        body = f.read()
    request = urllib.request.Request(
        f"http://supervisor/core/api/stt/{provider}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "audio/wav",
            "X-Speech-Content": (
                "format=wav; codec=pcm; sample_rate=16000; bit_rate=16; "
                f"channel=1; language={language}"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc
    except TimeoutError as exc:
        raise RuntimeError("request timeout") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid STT response JSON") from exc
    text = clean(payload.get("text")) if isinstance(payload, dict) else ""
    if not text:
        raise RuntimeError("empty transcription")
    return text


def append_audio_log(entry: dict, retention_hours: int, source_label: str) -> None:
    path = audio_log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cutoff = now() - timedelta(hours=max(1, retention_hours))

    with _LOG_LOCK:
        entries: list[dict] = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            parsed = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        ts = parse_ts(parsed.get("timestamp"))
                        if ts and clean(parsed.get("source")) == source_label and ts < cutoff:
                            continue
                        entries.append(parsed)
            except Exception as exc:
                log(f"failed to read audio log for retention pruning: {exc}")
        entries.append(entry)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)


def append_background_audio_log(entry: dict, retention_hours: int | None = None, source_label: str | None = None) -> None:
    path = background_audio_log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    retention = retention_hours if retention_hours is not None else background_audio_retention_hours()
    cutoff = now() - timedelta(hours=max(1, retention))
    source = clean(source_label) or clean(entry.get("source"))

    with _BACKGROUND_LOG_LOCK:
        entries: list[dict] = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            parsed = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        ts = parse_ts(parsed.get("timestamp"))
                        if source and clean(parsed.get("source")) == source and ts and ts < cutoff:
                            continue
                        entries.append(parsed)
            except Exception as exc:
                log(f"failed to read background audio log for retention pruning: {exc}")
        entries.append(entry)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)


def build_background_audio_event(
    config: AudioSourceConfig,
    duration_sec: float,
    vad_mode: str,
    diagnostics: dict | None = None,
) -> dict:
    sensory = classify_sensory_origin(
        source=config.source,
        label=config.label,
        room=config.room,
        note=config.note,
        modality="auditory",
    )
    entry = {
        "timestamp": now().isoformat(timespec="seconds"),
        "kind": "background_audio",
        "modality": "auditory",
        "awareness": "background",
        "origin": config.source,
        "source": config.label,
        "duration_sec": round(duration_sec, 2),
        "has_sound": True,
        "stt_requested": False,
        "transcript": None,
        "vad_mode": vad_mode,
        **sensory,
    }
    if isinstance(diagnostics, dict):
        for key in ("speech_ratio", "peak_db", "mean_db"):
            if key in diagnostics:
                entry[key] = diagnostics[key]
    return entry


def maybe_record_background_audio(
    config: AudioSourceConfig,
    audio_bytes: bytes,
    vad_mode: str,
    diagnostics: dict | None,
    last_logged_at: float,
) -> float:
    current = time.monotonic()
    if current - last_logged_at < BACKGROUND_LOG_MIN_INTERVAL_SECONDS:
        return last_logged_at
    duration_sec = len(audio_bytes) / float(SAMPLE_RATE * SAMPLE_WIDTH)
    if duration_sec < MIN_SEGMENT_SECONDS:
        return last_logged_at
    append_background_audio_log(
        build_background_audio_event(config, duration_sec, vad_mode, diagnostics),
        config.retention_hours,
        config.label,
    )
    log(
        "background audio noted for "
        f"{config.label}: duration={duration_sec:.2f}s, vad={vad_mode}, "
        f"speech_ratio={(diagnostics or {}).get('speech_ratio')}, peak_db={(diagnostics or {}).get('peak_db')}"
    )
    return current


def build_auditory_event(
    config: AudioSourceConfig,
    transcript: str,
    duration_sec: float,
    provider: str | None,
    language: str,
    timestamp: str,
    diagnostics: dict | None = None,
) -> dict:
    sensory = classify_sensory_origin(
        source=config.source,
        label=config.label,
        room=config.room,
        note=config.note,
        modality="auditory",
    )
    event = {
        "timestamp": timestamp,
        "modality": "auditory",
        "origin": config.source,
        "source": config.label,
        "speaker_hint": "user" if config.wake_word_enabled else "unknown",
        "transcript": transcript,
        "duration_sec": round(duration_sec, 2),
        "stt_provider": provider,
        "stt_language": language,
        "confidence": None,
        "raw_audio_ref": None,
        **sensory,
    }
    if isinstance(diagnostics, dict):
        for key in ("vad_mode", "speech_ratio", "peak_db", "mean_db"):
            if key in diagnostics:
                event[key] = diagnostics[key]
    return event


def should_trigger_wake_word(text_value: str, wake_words: list[str]) -> bool:
    lowered = clean(text_value).lower()
    return bool(lowered) and any(lowered.startswith(word) for word in wake_words)


def post_wake_message(text_value: str) -> None:
    ingress_port = clean(os.environ.get("INGRESS_PORT")) or "8099"
    body = json.dumps({"message": text_value, "source": "voice"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://localhost:{ingress_port}/api/send",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return
    except Exception as exc:
        log(f"wake-word forward failed: {exc}")


def process_segment(
    config: AudioSourceConfig,
    audio_bytes: bytes,
    provider: str | None,
    language: str,
    token: str,
    wake_words: list[str],
    diagnostics: dict | None = None,
) -> None:
    duration_sec = len(audio_bytes) / float(SAMPLE_RATE * SAMPLE_WIDTH)
    if duration_sec < MIN_SEGMENT_SECONDS:
        return

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = now().isoformat(timespec="seconds")
    entry: dict = {
        "timestamp": timestamp,
        "source": config.label,
        "duration_sec": round(duration_sec, 2),
    }
    if isinstance(diagnostics, dict):
        entry.update(diagnostics)
    entry.update(classify_sensory_origin(
        source=config.source,
        label=config.label,
        room=config.room,
        note=config.note,
        modality="auditory",
    ))
    vad_mode = clean(entry.get("vad_mode")) or "unknown"
    should_transcribe, skip_reason = should_transcribe_segment(vad_mode, entry)
    if not should_transcribe:
        entry["skipped"] = True
        entry["skip_reason"] = skip_reason
        append_audio_log(entry, config.retention_hours, config.label)
        record_non_speech_audio_event(
            config,
            audio_bytes,
            timestamp,
            clean(skip_reason) or "stt_skipped",
            diagnostics={**entry, "skip_reason": skip_reason},
        )
        log(
            "segment skipped for "
            f"{config.label}: {skip_reason} "
            f"(duration={entry.get('duration_sec')}s, vad={vad_mode}, "
            f"speech_ratio={entry.get('speech_ratio')}, peak_db={entry.get('peak_db')}, "
            f"mean_db={entry.get('mean_db')})"
        )
        return
    tmp_path: str | None = None
    try:
        if not provider:
            raise RuntimeError("stt_provider is not configured")
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is not set")
        with tempfile.NamedTemporaryFile(dir=TMP_DIR, suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        write_wav(tmp_path, audio_bytes)
        text_value = transcribe_wav(tmp_path, provider, language, token)
        entry["text"] = text_value
        is_primary = _claim_transcript_primary(text_value, config.source)
        if not is_primary:
            entry["deduplicated"] = True
        append_audio_log(entry, config.retention_hours, config.label)
        if not is_primary:
            log(f"duplicate transcript suppressed for {config.label}: '{text_value[:40]}'")
            return
        append_auditory_event(
            build_auditory_event(
                config,
                text_value,
                duration_sec,
                provider,
                language,
                timestamp,
                diagnostics=diagnostics,
            ),
            config.retention_hours,
            config.label,
        )
        if config.wake_word_enabled and should_trigger_wake_word(text_value, wake_words):
            update_current_room_from_audio_source(config)
            post_wake_message(text_value)
    except Exception as exc:
        error_text = str(exc)
        entry["error"] = error_text
        append_audio_log(entry, config.retention_hours, config.label)
        if "empty transcription" in error_text.lower():
            record_non_speech_audio_event(
                config,
                audio_bytes,
                timestamp,
                "empty_transcription",
                diagnostics=entry,
                error=error_text,
            )
        log(
            "segment processing failed for "
            f"{config.label}: {exc} "
            f"(duration={entry.get('duration_sec')}s, vad={entry.get('vad_mode','unknown')}, "
            f"speech_ratio={entry.get('speech_ratio')}, peak_db={entry.get('peak_db')}, "
            f"mean_db={entry.get('mean_db')})"
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def new_vad():
    if SileroVoiceActivityDetector is None:
        log(
            "pysilero_vad unavailable; using "
            f"{FALLBACK_DB_THRESHOLD:.0f}dB fallback VAD "
            f"(speech_ratio>={FALLBACK_SEGMENT_MIN_SPEECH_RATIO}, peak>={FALLBACK_SEGMENT_MIN_PEAK_DB}dB)"
        )
        return None, "fallback"
    try:
        return SileroVoiceActivityDetector(), "silero"
    except Exception as exc:
        log(
            "failed to initialize pysilero_vad; using fallback VAD: "
            f"{exc} (threshold={FALLBACK_DB_THRESHOLD:.0f}dB, "
            f"speech_ratio>={FALLBACK_SEGMENT_MIN_SPEECH_RATIO}, peak>={FALLBACK_SEGMENT_MIN_PEAK_DB}dB)"
        )
        return None, "fallback"


def _runtime_signature(settings: RuntimeSettings) -> tuple[str | None, str, tuple[str, ...], bool, bool]:
    return (
        settings.provider,
        settings.language,
        tuple(settings.wake_words),
        settings.config.wake_word_enabled,
        settings.config.background_only,
    )


def log_runtime_settings(config: AudioSourceConfig, settings: RuntimeSettings) -> None:
    log(
        "runtime settings updated for "
        f"{config.label}: provider={settings.provider or 'unset'}, "
        f"language={settings.language}, "
        f"wake_words={len(settings.wake_words)}, "
        f"wake_word_enabled={'yes' if settings.config.wake_word_enabled else 'no'}, mode={'background' if settings.config.background_only else 'stt'}"
    )


def read_exact_socket(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        piece = conn.recv(remaining)
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def reset_vad(detector) -> None:
    if detector is None:
        return
    try:
        detector.reset()
    except Exception:
        pass


def run_audio_stream_session(
    config: AudioSourceConfig,
    token: str,
    read_chunk,
    detector,
    vad_mode: str,
) -> dict[str, int]:
    prebuffer_chunks = max(1, math.ceil(PREBUFFER_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))
    max_silence_chunks = max(1, math.ceil(SILENCE_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))
    max_segment_chunks = max(1, math.ceil(MAX_SEGMENT_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))

    last_settings_signature: tuple[str | None, str, tuple[str, ...], bool, bool] | None = None
    last_background_log_at = 0.0
    active_listen_requests: dict[str, dict] = {}
    last_request_scan_at = 0.0
    prebuffer: deque[bytes] = deque(maxlen=prebuffer_chunks)
    segment_chunks: list[bytes] = []
    segment_levels: list[float] = []
    segment_speech_chunks = 0
    silence_chunks = 0
    active = False
    stats = {"chunks": 0, "bytes": 0}

    while True:
        chunk = read_chunk()
        if len(chunk) < CHUNK_BYTES:
            break
        stats["chunks"] += 1
        stats["bytes"] += len(chunk)
        last_request_scan_at = _service_active_listen_requests(
            config,
            chunk,
            active_listen_requests,
            last_request_scan_at,
        )

        voice_prob = detect_voice(chunk, detector)
        is_speech = voice_prob > VAD_THRESHOLD
        level_db = chunk_db(chunk)

        if active:
            segment_chunks.append(chunk)
            segment_levels.append(level_db)
            if is_speech:
                segment_speech_chunks += 1
            silence_chunks = 0 if is_speech else silence_chunks + 1
            if len(segment_chunks) >= max_segment_chunks or silence_chunks >= max_silence_chunks:
                peak_db, mean_db = summarize_chunk_levels(segment_levels)
                speech_ratio = round(segment_speech_chunks / max(1, len(segment_chunks)), 3)
                settings = load_runtime_settings(config)
                signature = _runtime_signature(settings)
                if signature != last_settings_signature:
                    last_settings_signature = signature
                    log_runtime_settings(config, settings)
                diagnostics = {
                    "vad_mode": vad_mode,
                    "speech_ratio": speech_ratio,
                    "peak_db": peak_db,
                    "mean_db": mean_db,
                }
                if not settings.stt_enabled:
                    if settings.config.background_only:
                        last_background_log_at = maybe_record_background_audio(
                            settings.config,
                            b"".join(segment_chunks),
                            vad_mode,
                            diagnostics,
                            last_background_log_at,
                        )
                    segment_chunks = []
                    segment_levels = []
                    segment_speech_chunks = 0
                    silence_chunks = 0
                    active = False
                    prebuffer.clear()
                    reset_vad(detector)
                    continue
                process_segment(
                    settings.config,
                    b"".join(segment_chunks),
                    settings.provider,
                    settings.language,
                    token,
                    settings.wake_words,
                    diagnostics=diagnostics,
                )
                segment_chunks = []
                segment_levels = []
                segment_speech_chunks = 0
                silence_chunks = 0
                active = False
                prebuffer.clear()
                reset_vad(detector)
        elif is_speech:
            segment_chunks = list(prebuffer)
            segment_chunks.append(chunk)
            segment_levels = [chunk_db(buffered) for buffered in prebuffer]
            segment_levels.append(level_db)
            segment_speech_chunks = 1
            silence_chunks = 0
            active = True
            prebuffer.clear()
        else:
            prebuffer.append(chunk)

    if active and segment_chunks:
        peak_db, mean_db = summarize_chunk_levels(segment_levels)
        speech_ratio = round(segment_speech_chunks / max(1, len(segment_chunks)), 3)
        settings = load_runtime_settings(config)
        signature = _runtime_signature(settings)
        if signature != last_settings_signature:
            last_settings_signature = signature
            log_runtime_settings(config, settings)
        diagnostics = {
            "vad_mode": vad_mode,
            "speech_ratio": speech_ratio,
            "peak_db": peak_db,
            "mean_db": mean_db,
        }
        if settings.stt_enabled:
            process_segment(
                settings.config,
                b"".join(segment_chunks),
                settings.provider,
                settings.language,
                token,
                settings.wake_words,
                diagnostics=diagnostics,
            )
        elif settings.config.background_only:
            maybe_record_background_audio(
                settings.config,
                b"".join(segment_chunks),
                vad_mode,
                diagnostics,
                last_background_log_at,
            )

    for request in list(active_listen_requests.values()):
        _finalize_active_listen_capture(request, bytes(request.get('buffer') or b''), error='stream ended before capture completed')
    return stats


def audio_worker(
    config: AudioSourceConfig,
    token: str,
) -> None:
    while True:
        proc: subprocess.Popen | None = None
        detector, vad_mode = new_vad()
        try:
            cmd = build_ffmpeg_command(config)
            log(f"starting ffmpeg for {config.label} ({vad_mode}): {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            if proc.stdout is None:
                raise RuntimeError("ffmpeg stdout is unavailable")
            stats = run_audio_stream_session(
                config,
                token,
                lambda: read_exact(proc.stdout, CHUNK_BYTES),
                detector,
                vad_mode,
            )
            rc = proc.wait(timeout=2)
            log(
                f"ffmpeg exited for {config.label} with code {rc}; "
                f"chunks={stats['chunks']} bytes={stats['bytes']}; retrying in 10s"
            )
        except Exception as exc:
            log(f"worker error for {config.label}: {exc}")
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        time.sleep(10)


def tcp_pull_worker(
    config: AudioSourceConfig,
    token: str,
) -> None:
    while True:
        conn: socket.socket | None = None
        detector, vad_mode = new_vad()
        try:
            log(
                f"tcp pull connecting for {config.label}: source={config.source} host={config.host} port={config.port} "
                f"sample_rate={config.sample_rate} channels={config.channels} format={config.audio_format}"
            )
            conn = socket.create_connection((config.host, config.port), timeout=10)
            conn.settimeout(10)
            stats = run_audio_stream_session(
                config,
                token,
                lambda: read_exact_socket(conn, CHUNK_BYTES),
                detector,
                vad_mode,
            )
            log(
                f"tcp pull disconnected for {config.label}: source={config.source} host={config.host} port={config.port} "
                f"chunks={stats['chunks']} bytes={stats['bytes']}; retrying in 3s"
            )
            time.sleep(3)
        except Exception as exc:
            log(f"tcp pull error for {config.label}: {exc}; retrying in 10s")
            time.sleep(10)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def main() -> int:
    preferences = load_preferences()
    sources = load_enabled_mics(preferences)
    if not sources:
        log("no STT/background audio sources; exiting")
        return 0

    token = clean(os.environ.get("SUPERVISOR_TOKEN"))

    threads = []
    for config in sources:
        worker = tcp_pull_worker if config.transport == "tcp_pull" else audio_worker
        thread = threading.Thread(
            target=worker,
            args=(config, token),
            daemon=False,
            name=f"audio:{config.label}",
        )
        thread.start()
        threads.append(thread)
        log(f"audio source thread started: {config.label}")

    for thread in threads:
        thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
