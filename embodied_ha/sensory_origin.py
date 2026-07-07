#!/usr/bin/env python3
"""Classify where a sensory input is experienced from."""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from room_graph import (
    alias_map,
    data_dir,
    initial_room,
    load_room_graph,
    resolve_room,
    room_graph_path as room_graph_path,  # noqa: F401
    rooms,
    shortest_path,
)
from state_utils import clean, load_prefs, read_json

DEFAULT_DATA_DIR = "/config/embodied-ha"
DEFAULT_BODY_LOCATION_FILE = "/config/embodied-ha/body_location.json"
DEFAULT_CALIB_FILE = "/config/embodied-ha/calibration/audio_calibration.json"

_CALIB_INVALID_THRESHOLD = -200.0

SPECIAL_SOURCE_HINTS = {}


AREA_CACHE_TTL_SEC = 300.0
_AREA_CACHE: dict[str, tuple[float, str | None]] = {}


def _ha_token() -> str:
    return clean(os.environ.get("SUPERVISOR_TOKEN")) or clean(os.environ.get("HASSIO_TOKEN"))


def _ha_api_base() -> str:
    base = clean(os.environ.get("EHA_HA_API_URL")) or clean(os.environ.get("HA_URL"))
    if base:
        return base if base.endswith("/api") else f"{base.rstrip('/')}/api"
    return "http://supervisor/core/api"


def _looks_like_entity_id(value: Any) -> bool:
    text = clean(value)
    if not text or "://" in text or " " in text:
        return False
    head, sep, tail = text.partition(".")
    return bool(sep and head and tail)


def _ha_template(template: str) -> str | None:
    token = _ha_token()
    if not token:
        return None
    body = json.dumps({"template": template}, ensure_ascii=False)
    result = subprocess.run(
        [
            "curl", "-sf", "--max-time", "5",
            "-X", "POST",
            "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/json",
            "-d", body,
            f"{_ha_api_base().rstrip('/')}/template",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = clean(result.stdout)
    return value or None


def prefs_path() -> str:
    return clean(os.environ.get("EHA_PREFS_FILE"))


def _load_prefs() -> dict[str, Any]:
    return load_prefs(prefs_path())


def _source_room_hints(graph: dict[str, Any]) -> dict[str, str]:
    prefs = _load_prefs()
    raw_hints = prefs.get("source_room_hints")
    if not isinstance(raw_hints, dict):
        return {}

    hints: dict[str, str] = {}
    for token, room_value in raw_hints.items():
        key = clean(token).lower()
        room_id = resolve_room(room_value, graph)
        if key and room_id:
            hints[key] = room_id
    return hints


def area_for_entity(entity_id: Any) -> str | None:
    eid = clean(entity_id)
    if not _looks_like_entity_id(eid):
        return None
    now = time.time()
    cached = _AREA_CACHE.get(eid)
    if cached and cached[0] > now:
        return cached[1]
    template = "{{ area_name(%s) or '' }}" % json.dumps(eid, ensure_ascii=False)
    area = _ha_template(template)
    _AREA_CACHE[eid] = (now + AREA_CACHE_TTL_SEC, area)
    return area


def resolve_area_room(area: Any, graph: dict[str, Any] | None = None) -> str | None:
    return resolve_room(area, graph)



def body_location_path() -> str:
    return clean(os.environ.get("EHA_BODY_LOCATION_FILE")) or os.path.join(data_dir(), "body_location.json") or DEFAULT_BODY_LOCATION_FILE



def room_label(room_id: str | None, graph: dict[str, Any] | None = None) -> str:
    if not room_id:
        return ""
    graph = graph if isinstance(graph, dict) else load_room_graph()
    item = rooms(graph).get(room_id, {})
    return clean(item.get("display_name")) or room_id



def infer_room_from_text(*values: Any, graph: dict[str, Any] | None = None) -> str | None:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    haystack = " ".join(clean(value).lower() for value in values if clean(value))
    if not haystack:
        return None

    for token, room_id in _source_room_hints(graph).items():
        if token in haystack:
            return room_id

    for token, room_id in SPECIAL_SOURCE_HINTS.items():
        if token in haystack and resolve_room(room_id, graph):
            return resolve_room(room_id, graph)

    aliases = alias_map(graph)
    for token, room_id in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if len(token) < 2:
            continue
        if token in haystack:
            return room_id
    return None



def current_body_room(graph: dict[str, Any] | None = None) -> str:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    state = read_json(body_location_path(), {})
    if not isinstance(state, dict):
        state = {}
    return resolve_room(state.get("current_room"), graph) or initial_room(graph)


def current_projected_room(graph: dict[str, Any] | None = None) -> str | None:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    state = read_json(body_location_path(), {})
    if not isinstance(state, dict):
        return None
    return resolve_room(state.get("projected_room"), graph) or None



def _calib_path() -> str:
    data = clean(os.environ.get("EHA_DATA_DIR")) or DEFAULT_DATA_DIR
    calib_dir = clean(os.environ.get("EHA_CALIB_DIR")) or os.path.join(data, "calibration")
    return clean(os.environ.get("EHA_CALIB_FILE")) or os.path.join(calib_dir, "audio_calibration.json")


def _attenuation_db(body_room: str, source_room: str) -> float | None:
    """校正データから body_room 基準での source_room の相対減衰 (dB) を返す。"""
    if body_room == source_room:
        return 0.0
    calib = read_json(_calib_path(), {})
    if not isinstance(calib, dict):
        return None
    body_sources = calib.get(body_room, {}).get("sources", {})
    ref_node, ref_db = None, None
    for node, v in body_sources.items():
        db = v.get("tone_db")
        if isinstance(db, (int, float)) and db > _CALIB_INVALID_THRESHOLD:
            if ref_db is None or db > ref_db:
                ref_node, ref_db = node, db
    if ref_node is None or ref_db is None:
        return None
    src_v = calib.get(source_room, {}).get("sources", {}).get(ref_node, {})
    src_db = src_v.get("tone_db")
    if not isinstance(src_db, (int, float)) or src_db <= _CALIB_INVALID_THRESHOLD:
        return None
    return round(src_db - ref_db, 1)


def classify_sensory_origin(
    *,
    source: Any = "",
    label: Any = "",
    room: Any = "",
    area: Any = "",
    entity_id: Any = "",
    note: Any = "",
    modality: str = "",
    graph: dict[str, Any] | None = None,
    current_room: Any = "",
) -> dict[str, Any]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    body_room = resolve_room(current_room, graph) or current_body_room(graph)
    effective_entity_id = clean(entity_id) or (clean(source) if _looks_like_entity_id(source) else "")
    resolved_area = clean(area) or area_for_entity(effective_entity_id)
    source_room = (
        resolve_room(room, graph)
        or resolve_area_room(resolved_area, graph)
        or infer_room_from_text(source, label, note, graph=graph)
    )

    if source_room:
        projected_room = current_projected_room(graph)
        if source_room == body_room:
            origin = "direct"
        elif projected_room and source_room == projected_room:
            origin = "cyber_direct"
        else:
            origin = "remote"
        move_cost, move_path = shortest_path(body_room, source_room, graph)
        attenuation = _attenuation_db(body_room, source_room)
    else:
        origin = "home_assistant"
        move_cost, move_path = None, []
        attenuation = None

    return {
        "modality": clean(modality) or None,
        "body_room": body_room,
        "body_room_label": room_label(body_room, graph),
        "source_room": source_room,
        "source_room_label": room_label(source_room, graph),
        "source_area": resolved_area,
        "source_entity_id": effective_entity_id or None,
        "sensory_origin": origin,
        "access_mode": origin,
        "move_cost": move_cost,
        "move_path": move_path,
        "attenuation_db": attenuation,
    }
