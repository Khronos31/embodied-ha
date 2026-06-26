#!/usr/bin/env python3
"""Classify where a sensory input is experienced from."""
from __future__ import annotations

import heapq
import json
import os
import subprocess
import time
from typing import Any

from state_utils import clean, read_json

DEFAULT_DATA_DIR = "/config/embodied-ha"
DEFAULT_ROOM_GRAPH_FILE = "/config/embodied-ha/floorplan_room_graph_draft.json"
DEFAULT_BODY_LOCATION_FILE = "/config/embodied-ha/body_location.json"

SPECIAL_SOURCE_HINTS = {
    "camera.home_pc_screenshot": "study",
    "home_pc": "study",
    "home-pc": "study",
    "capture_pc": "study",
    "capture_pc2": "study",
}


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


def data_dir() -> str:
    return clean(os.environ.get("EHA_DATA_DIR")) or DEFAULT_DATA_DIR


def room_graph_path() -> str:
    return clean(os.environ.get("EHA_ROOM_GRAPH_FILE")) or os.path.join(data_dir(), "floorplan_room_graph_draft.json") or DEFAULT_ROOM_GRAPH_FILE


def body_location_path() -> str:
    return clean(os.environ.get("EHA_BODY_LOCATION_FILE")) or os.path.join(data_dir(), "body_location.json") or DEFAULT_BODY_LOCATION_FILE


def load_room_graph() -> dict[str, Any]:
    value = read_json(room_graph_path(), {})
    return value if isinstance(value, dict) else {}


def rooms(graph: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    value = graph.get("rooms")
    if not isinstance(value, dict):
        return {}
    return {clean(k): v for k, v in value.items() if clean(k) and isinstance(v, dict)}


def room_label(room_id: str | None, graph: dict[str, Any] | None = None) -> str:
    if not room_id:
        return ""
    graph = graph if isinstance(graph, dict) else load_room_graph()
    item = rooms(graph).get(room_id, {})
    return clean(item.get("display_name")) or room_id


def alias_map(graph: dict[str, Any] | None = None) -> dict[str, str]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    result: dict[str, str] = {}
    pending: dict[str, list[str]] = {}

    for room_id, item in rooms(graph).items():
        candidates = [room_id, item.get("display_name")]
        tags = item.get("tags")
        if isinstance(tags, list):
            candidates.extend(tags)
        for candidate in candidates:
            key = clean(candidate).lower()
            if key:
                pending.setdefault(key, []).append(room_id)

    raw_aliases = graph.get("aliases_pending")
    if isinstance(raw_aliases, dict):
        for room_id, values in raw_aliases.items():
            canonical = clean(room_id)
            if canonical not in rooms(graph) or not isinstance(values, list):
                continue
            for value in values:
                key = clean(value).lower()
                if key:
                    pending.setdefault(key, []).append(canonical)

    for key, room_ids in pending.items():
        unique = sorted(set(room_ids))
        if len(unique) == 1:
            result[key] = unique[0]
    return result


def resolve_room(value: Any, graph: dict[str, Any] | None = None) -> str | None:
    key = clean(value)
    if not key:
        return None
    graph = graph if isinstance(graph, dict) else load_room_graph()
    if key in rooms(graph):
        return key
    return alias_map(graph).get(key.lower())


def infer_room_from_text(*values: Any, graph: dict[str, Any] | None = None) -> str | None:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    haystack = " ".join(clean(value).lower() for value in values if clean(value))
    if not haystack:
        return None

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


def initial_room(graph: dict[str, Any] | None = None) -> str:
    room_map = rooms(graph)
    if "study" in room_map:
        return "study"
    if "living_room" in room_map:
        return "living_room"
    return next(iter(room_map), "unknown")


def current_body_room(graph: dict[str, Any] | None = None) -> str:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    state = read_json(body_location_path(), {})
    if not isinstance(state, dict):
        state = {}
    return resolve_room(state.get("current_room"), graph) or initial_room(graph)


def adjacency(graph: dict[str, Any] | None = None) -> dict[str, list[tuple[str, float]]]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    adj: dict[str, list[tuple[str, float]]] = {room_id: [] for room_id in rooms(graph)}
    edges = graph.get("edges")
    if not isinstance(edges, list):
        return adj
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src = resolve_room(edge.get("from"), graph)
        dst = resolve_room(edge.get("to"), graph)
        if not src or not dst:
            continue
        try:
            cost = max(0.1, float(edge.get("cost", 1)))
        except Exception:
            cost = 1.0
        adj.setdefault(src, []).append((dst, cost))
        adj.setdefault(dst, []).append((src, cost))
    return adj


def shortest_path(from_room: str | None, to_room: str | None, graph: dict[str, Any] | None = None) -> tuple[float | None, list[str]]:
    if not from_room or not to_room:
        return None, []
    graph = graph if isinstance(graph, dict) else load_room_graph()
    if from_room == to_room:
        return 0.0, [from_room]
    adj = adjacency(graph)
    if from_room not in adj or to_room not in adj:
        return None, []
    queue: list[tuple[float, str, list[str]]] = [(0.0, from_room, [from_room])]
    best: dict[str, float] = {}
    while queue:
        cost, room_id, path = heapq.heappop(queue)
        if room_id in best and best[room_id] <= cost:
            continue
        best[room_id] = cost
        if room_id == to_room:
            return cost, path
        for nxt, edge_cost in adj.get(room_id, []):
            next_cost = cost + edge_cost
            if nxt in best and best[nxt] <= next_cost:
                continue
            heapq.heappush(queue, (next_cost, nxt, path + [nxt]))
    return None, []


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
        origin = "direct" if source_room == body_room else "remote"
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
