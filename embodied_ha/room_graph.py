#!/usr/bin/env python3
"""Shared room graph helpers for embodied location and sensory routing."""
from __future__ import annotations

import heapq
import os
from typing import Any

from state_utils import clean, read_json

DEFAULT_DATA_DIR = "/config/embodied-ha"
DEFAULT_ROOM_GRAPH_FILE = "/config/embodied-ha/floorplan_room_graph_draft.json"


def data_dir() -> str:
    return clean(os.environ.get("EHA_DATA_DIR")) or DEFAULT_DATA_DIR


def room_graph_path() -> str:
    return clean(os.environ.get("EHA_ROOM_GRAPH_FILE")) or os.path.join(data_dir(), "floorplan_room_graph_draft.json") or DEFAULT_ROOM_GRAPH_FILE


def load_room_graph() -> dict[str, Any]:
    value = read_json(room_graph_path(), {})
    return value if isinstance(value, dict) else {}


def rooms(graph: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    value = graph.get("rooms")
    if not isinstance(value, dict):
        return {}
    return {clean(k): v for k, v in value.items() if clean(k) and isinstance(v, dict)}


def alias_map(graph: dict[str, Any] | None = None) -> dict[str, str]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    result: dict[str, str] = {}
    pending: dict[str, list[str]] = {}
    room_map = rooms(graph)

    for room_id, item in room_map.items():
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
            if canonical not in room_map or not isinstance(values, list):
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


def initial_room(graph: dict[str, Any] | None = None) -> str:
    room_map = rooms(graph)
    if "study" in room_map:
        return "study"
    if "living_room" in room_map:
        return "living_room"
    return next(iter(room_map), "unknown")


def _adjacency(graph: dict[str, Any] | None = None) -> dict[str, list[tuple[str, float]]]:
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
    adj = _adjacency(graph)
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
