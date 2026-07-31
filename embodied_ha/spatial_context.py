"""Resolve where an observed or heard event sits in the configured room graph."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from room_graph import (
    alias_map,
    data_dir,
    initial_room,
    load_room_graph,
    resolve_room,
    rooms,
    shortest_path,
)
from state_utils import clean, load_prefs, read_json

DEFAULT_DATA_DIR = "/config/embodied-ha"
DEFAULT_BODY_LOCATION_FILE = "/config/embodied-ha/body_location.json"
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
    request = urllib.request.Request(
        f"{_ha_api_base().rstrip('/')}/template",
        data=json.dumps({"template": template}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            value = clean(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
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
    template = f"{{{{ area_name({json.dumps(eid, ensure_ascii=False)}) or '' }}}}"
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
    else:
        origin = "home_assistant"
        move_cost, move_path = None, []

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
    }
