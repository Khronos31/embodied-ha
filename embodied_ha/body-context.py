#!/usr/bin/env python3
"""Format the current embodied location as a small prompt context block."""
from __future__ import annotations

import heapq
import os
from typing import Any

from state_utils import clean, read_json

DEFAULT_DATA_DIR = "/config/embodied-ha"
DEFAULT_ROOM_GRAPH_FILE = "/config/embodied-ha/floorplan_room_graph_draft.json"
DEFAULT_BODY_LOCATION_FILE = "/config/embodied-ha/body_location.json"
DEFAULT_BODY_STATE_FILE = "/config/embodied-ha/body_state.json"


def data_dir() -> str:
    return clean(os.environ.get("EHA_DATA_DIR")) or DEFAULT_DATA_DIR


def room_graph_path() -> str:
    return clean(os.environ.get("EHA_ROOM_GRAPH_FILE")) or os.path.join(data_dir(), "floorplan_room_graph_draft.json") or DEFAULT_ROOM_GRAPH_FILE


def body_location_path() -> str:
    return clean(os.environ.get("EHA_BODY_LOCATION_FILE")) or os.path.join(data_dir(), "body_location.json") or DEFAULT_BODY_LOCATION_FILE


def body_state_path() -> str:
    return clean(os.environ.get("EHA_BODY_STATE_FILE")) or os.path.join(data_dir(), "body_state.json") or DEFAULT_BODY_STATE_FILE


def load_graph() -> dict[str, Any]:
    value = read_json(room_graph_path(), {})
    return value if isinstance(value, dict) else {}


def load_body_state() -> dict[str, Any]:
    value = read_json(body_state_path(), {})
    return value if isinstance(value, dict) else {}


def rooms(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = graph.get("rooms")
    if not isinstance(value, dict):
        return {}
    return {clean(k): v for k, v in value.items() if clean(k) and isinstance(v, dict)}


def room_label(room_id: str, graph: dict[str, Any]) -> str:
    item = rooms(graph).get(room_id, {})
    return clean(item.get("display_name")) or room_id


def resolve_room(value: Any, graph: dict[str, Any]) -> str | None:
    key = clean(value)
    if not key:
        return None
    room_map = rooms(graph)
    if key in room_map:
        return key
    lowered = key.lower()
    for room_id, item in room_map.items():
        if clean(item.get("display_name")).lower() == lowered:
            return room_id
    aliases = graph.get("aliases_pending")
    if isinstance(aliases, dict):
        for room_id, values in aliases.items():
            canonical = clean(room_id)
            if canonical not in room_map or not isinstance(values, list):
                continue
            if any(clean(alias).lower() == lowered for alias in values):
                return canonical
    return None


def initial_room(graph: dict[str, Any]) -> str:
    room_map = rooms(graph)
    if "study" in room_map:
        return "study"
    if "living_room" in room_map:
        return "living_room"
    return next(iter(room_map), "unknown")


def load_location(graph: dict[str, Any]) -> dict[str, Any]:
    value = read_json(body_location_path(), {})
    state = value if isinstance(value, dict) else {}
    current = resolve_room(state.get("current_room"), graph) or initial_room(graph)
    previous = resolve_room(state.get("previous_room"), graph)
    projected = resolve_room(state.get("projected_room"), graph)
    return {
        "current_room": current,
        "previous_room": previous,
        "projected_room": projected,
        "current_entity": clean(state.get("current_entity")) or "",
        "projection_updated_at": clean(state.get("projection_updated_at")) or None,
        "updated_at": clean(state.get("updated_at")) or None,
        "last_move_cost": state.get("last_move_cost"),
        "last_move_path": state.get("last_move_path") if isinstance(state.get("last_move_path"), list) else [],
    }


def adjacency(graph: dict[str, Any]) -> dict[str, list[tuple[str, float]]]:
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


def shortest_costs(start: str, graph: dict[str, Any]) -> dict[str, float]:
    adj = adjacency(graph)
    if start not in adj:
        return {}
    queue: list[tuple[float, str]] = [(0.0, start)]
    best: dict[str, float] = {}
    while queue:
        cost, room_id = heapq.heappop(queue)
        if room_id in best and best[room_id] <= cost:
            continue
        best[room_id] = cost
        for nxt, edge_cost in adj.get(room_id, []):
            next_cost = cost + edge_cost
            if nxt in best and best[nxt] <= next_cost:
                continue
            heapq.heappush(queue, (next_cost, nxt))
    return best


def format_body_context(limit: int = 5) -> str:
    graph = load_graph()
    room_map = rooms(graph)
    if not room_map:
        return "# 身体位置\n部屋グラフが未設定です。必要なら get_room_graph で確認してください。"

    state = load_location(graph)
    body_state = load_body_state()
    current = state["current_room"]
    projected = state.get("projected_room")
    current_entity = clean(state.get("current_entity"))
    remote_avatar_host = clean(body_state.get("remote_avatar_host"))
    body_room_name = room_label(current, graph)
    projected_room_name = room_label(projected, graph) if projected else body_room_name
    lines = ["# 今いる場所"]

    if projected and current_entity:
        display_entity = current_entity or remote_avatar_host
        if current_entity.startswith("camera."):
            lines.append(f"{projected_room_name} の `{display_entity}` から見ている（電脳体）。身体は {body_room_name} にある。")
        else:
            lines.append(f"`{display_entity}` の中にいる（電脳体）。身体は {body_room_name} にある。")
        lines.append("別のデバイスへ移動するなら move_cyber。戻るなら return_to_body。")
    else:
        lines.append(f"{body_room_name}にいる。")
        costs = shortest_costs(current, graph)
        nearby = [
            (room_id, cost)
            for room_id, cost in sorted(costs.items(), key=lambda item: (item[1], item[0]))
            if room_id != current
        ][: max(0, limit)]
        if nearby:
            cost_text = " / ".join(f"{room_label(room_id, graph)}({cost:g})" for room_id, cost in nearby)
            lines.append(f"近くへ移動: {cost_text}")
        lines.append("電脳空間に入るなら enter_cyberspace、身体ごと移動なら move_to。")

    if state.get("previous_room"):
        lines.append(f"直前の物理移動: {room_label(state['previous_room'], graph)} (`{state['previous_room']}`) から来た")
    if state.get("last_move_cost") is not None:
        lines.append(f"直前の物理移動コスト: {state['last_move_cost']}")

    return "\n".join(lines)

def main() -> None:
    print(format_body_context())


if __name__ == "__main__":
    main()
