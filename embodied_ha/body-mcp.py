#!/usr/bin/env python3
"""Body/location MCP server for Embodied HA.

This server gives Akane a lightweight embodied position model. It does not
forbid remote HA access; it records whether perception/action is direct or
remote by making the current body location explicit.
"""
from __future__ import annotations

import heapq
import json
import os
import threading
from typing import Any

from mcp_lib import serve, text
from state_utils import clean, now, read_json, write_json

DEFAULT_ROOM_GRAPH_FILE = "/config/embodied-ha/floorplan_room_graph_draft.json"
DEFAULT_BODY_LOCATION_FILE = "/config/embodied-ha/body_location.json"
DEFAULT_BODY_LOCATION_LOG_FILE = "/config/embodied-ha/log/body_location_log.jsonl"
_STATE_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()


def _data_dir() -> str:
    return clean(os.environ.get("EHA_DATA_DIR")) or "/config/embodied-ha"


def room_graph_path() -> str:
    return clean(os.environ.get("EHA_ROOM_GRAPH_FILE")) or os.path.join(_data_dir(), "floorplan_room_graph_draft.json") or DEFAULT_ROOM_GRAPH_FILE


def body_location_path() -> str:
    return clean(os.environ.get("EHA_BODY_LOCATION_FILE")) or os.path.join(_data_dir(), "body_location.json") or DEFAULT_BODY_LOCATION_FILE


def body_location_log_path() -> str:
    return clean(os.environ.get("EHA_BODY_LOCATION_LOG_FILE")) or os.path.join(_data_dir(), "log", "body_location_log.jsonl") or DEFAULT_BODY_LOCATION_LOG_FILE


def _json_text(data: Any) -> list[dict[str, str]]:
    return [text(json.dumps(data, ensure_ascii=False, indent=2))]


def load_room_graph() -> dict[str, Any]:
    data = read_json(room_graph_path(), {})
    return data if isinstance(data, dict) else {}


def rooms(graph: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    raw = graph.get("rooms")
    if isinstance(raw, dict):
        return {clean(k): v for k, v in raw.items() if clean(k) and isinstance(v, dict)}
    return {}


def aliases(graph: dict[str, Any] | None = None) -> dict[str, str]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    result: dict[str, str] = {}
    for room_id, item in rooms(graph).items():
        result[room_id.lower()] = room_id
        display = clean(item.get("display_name"))
        if display:
            result[display.lower()] = room_id
    raw_aliases = graph.get("aliases_pending")
    if isinstance(raw_aliases, dict):
        for room_id, values in raw_aliases.items():
            canonical = clean(room_id)
            if canonical not in rooms(graph):
                continue
            if isinstance(values, list):
                for value in values:
                    alias = clean(value)
                    if alias:
                        result[alias.lower()] = canonical
    return result


def resolve_room(value: Any, graph: dict[str, Any] | None = None) -> str | None:
    key = clean(value)
    if not key:
        return None
    graph = graph if isinstance(graph, dict) else load_room_graph()
    room_ids = rooms(graph)
    if key in room_ids:
        return key
    return aliases(graph).get(key.lower())


def initial_room(graph: dict[str, Any] | None = None) -> str:
    room_ids = rooms(graph)
    if "study" in room_ids:
        return "study"
    if "living_room" in room_ids:
        return "living_room"
    return next(iter(room_ids), "unknown")


def _room_label(room_id: str, graph: dict[str, Any]) -> str:
    item = rooms(graph).get(room_id, {})
    return clean(item.get("display_name")) or room_id


def load_location_state(graph: dict[str, Any] | None = None) -> dict[str, Any]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    state = read_json(body_location_path(), {})
    if not isinstance(state, dict):
        state = {}
    current = resolve_room(state.get("current_room"), graph) or initial_room(graph)
    return {
        "current_room": current,
        "display_name": _room_label(current, graph),
        "updated_at": clean(state.get("updated_at")) or None,
        "previous_room": resolve_room(state.get("previous_room"), graph),
        "last_move_cost": state.get("last_move_cost"),
        "last_move_path": state.get("last_move_path") if isinstance(state.get("last_move_path"), list) else [],
        "source": clean(state.get("source")) or "default",
    }


def save_location_state(state: dict[str, Any]) -> None:
    with _STATE_LOCK:
        write_json(body_location_path(), state)


def adjacency(graph: dict[str, Any] | None = None) -> dict[str, list[tuple[str, float]]]:
    graph = graph if isinstance(graph, dict) else load_room_graph()
    room_ids = rooms(graph)
    adj: dict[str, list[tuple[str, float]]] = {room_id: [] for room_id in room_ids}
    raw_edges = graph.get("edges")
    if isinstance(raw_edges, list):
        for edge in raw_edges:
            if not isinstance(edge, dict):
                continue
            src = resolve_room(edge.get("from"), graph)
            dst = resolve_room(edge.get("to"), graph)
            if not src or not dst:
                continue
            try:
                cost = float(edge.get("cost", 1))
            except Exception:
                cost = 1.0
            cost = max(0.1, cost)
            adj.setdefault(src, []).append((dst, cost))
            adj.setdefault(dst, []).append((src, cost))
    return adj


def shortest_path(from_room: str, to_room: str, graph: dict[str, Any] | None = None) -> tuple[float | None, list[str]]:
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
            if nxt in best and best[nxt] <= cost + edge_cost:
                continue
            heapq.heappush(queue, (cost + edge_cost, nxt, path + [nxt]))
    return None, []


def _format_path(path: list[str], graph: dict[str, Any]) -> list[dict[str, str]]:
    return [{"room": room_id, "display_name": _room_label(room_id, graph)} for room_id in path]


def append_move_log(entry: dict[str, Any]) -> None:
    path = body_location_log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _LOG_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_location(args: dict[str, Any]):
    graph = load_room_graph()
    state = load_location_state(graph)
    current = state["current_room"]
    payload = {
        "current_room": current,
        "display_name": _room_label(current, graph),
        "updated_at": state.get("updated_at"),
        "previous_room": state.get("previous_room"),
        "last_move_cost": state.get("last_move_cost"),
        "last_move_path": state.get("last_move_path"),
        "sensory_origin_hint": "direct",
        "available_rooms": [
            {"room": room_id, "display_name": _room_label(room_id, graph)}
            for room_id in rooms(graph)
        ],
        "state_file": body_location_path(),
        "room_graph_file": room_graph_path(),
    }
    return _json_text(payload)


def estimate_move_cost(args: dict[str, Any]):
    graph = load_room_graph()
    state = load_location_state(graph)
    from_room = resolve_room(args.get("from") or args.get("from_room") or state["current_room"], graph)
    to_room = resolve_room(args.get("to") or args.get("room") or args.get("to_room"), graph)
    if not from_room or not to_room:
        return _json_text({
            "error": "unknown room",
            "from": args.get("from") or args.get("from_room"),
            "to": args.get("to") or args.get("room") or args.get("to_room"),
            "available_rooms": list(rooms(graph).keys()),
        }), True
    cost, path = shortest_path(from_room, to_room, graph)
    if cost is None:
        return _json_text({"error": "no path", "from": from_room, "to": to_room}), True
    return _json_text({
        "from": from_room,
        "to": to_room,
        "cost": cost,
        "path": path,
        "path_display": _format_path(path, graph),
    })


def move_to(args: dict[str, Any]):
    graph = load_room_graph()
    target = resolve_room(args.get("room") or args.get("to") or args.get("to_room"), graph)
    if not target:
        return _json_text({
            "error": "unknown target room",
            "room": args.get("room") or args.get("to") or args.get("to_room"),
            "available_rooms": list(rooms(graph).keys()),
        }), True
    state = load_location_state(graph)
    current = state["current_room"]
    cost, path = shortest_path(current, target, graph)
    if cost is None:
        return _json_text({"error": "no path", "from": current, "to": target}), True
    timestamp = now().isoformat(timespec="seconds")
    reason = clean(args.get("reason")) or None
    new_state = {
        "current_room": target,
        "display_name": _room_label(target, graph),
        "previous_room": current,
        "previous_display_name": _room_label(current, graph),
        "updated_at": timestamp,
        "last_move_cost": cost,
        "last_move_path": path,
        "source": clean(args.get("source")) or "body-mcp",
    }
    if reason:
        new_state["reason"] = reason
    save_location_state(new_state)
    event = {
        "timestamp": timestamp,
        "kind": "body_move",
        "from": current,
        "from_display_name": _room_label(current, graph),
        "to": target,
        "to_display_name": _room_label(target, graph),
        "cost": cost,
        "path": path,
        "path_display": _format_path(path, graph),
        "reason": reason,
        "sensory_origin_after_move": "direct",
    }
    append_move_log(event)
    return _json_text({"state": new_state, "event": event})


def get_room_graph(args: dict[str, Any]):
    graph = load_room_graph()
    include_cost_matrix = args.get("include_cost_matrix") is True
    payload = {
        "rooms": rooms(graph),
        "edges": graph.get("edges") if isinstance(graph.get("edges"), list) else [],
        "aliases_pending": graph.get("aliases_pending") if isinstance(graph.get("aliases_pending"), dict) else {},
        "assumptions": graph.get("assumptions") if isinstance(graph.get("assumptions"), list) else [],
        "questions_for_user": graph.get("questions_for_user") if isinstance(graph.get("questions_for_user"), list) else [],
        "room_graph_file": room_graph_path(),
    }
    if include_cost_matrix:
        payload["cost_matrix"] = graph.get("cost_matrix") if isinstance(graph.get("cost_matrix"), dict) else {}
    return _json_text(payload)


TOOL_GET_LOCATION = {
    "name": "get_location",
    "description": "あかねの現在位置と、部屋グラフ上の利用可能な部屋を返す。",
    "inputSchema": {"type": "object", "properties": {}},
}

TOOL_MOVE_TO = {
    "name": "move_to",
    "description": "部屋グラフ上であかねの現在位置を移動し、移動コストと経路を記録する。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "room": {"type": "string", "description": "移動先の room id または別名"},
            "reason": {"type": "string", "description": "移動する理由。任意"},
        },
        "required": ["room"],
    },
}

TOOL_ESTIMATE_MOVE_COST = {
    "name": "estimate_move_cost",
    "description": "現在位置または指定部屋から、目的の部屋までの移動コストと経路を見積もる。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "from": {"type": "string", "description": "出発 room id または別名。省略時は現在位置"},
            "to": {"type": "string", "description": "目的 room id または別名"},
            "room": {"type": "string", "description": "to の別名"},
        },
    },
}

TOOL_GET_ROOM_GRAPH = {
    "name": "get_room_graph",
    "description": "部屋一覧、接続、仮別名、前提を返す。必要なときだけ使う。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "include_cost_matrix": {"type": "boolean", "description": "移動コスト行列も含めるか"},
        },
    },
}


if __name__ == "__main__":
    serve("body-mcp", "1.0", {
        "get_location": {"spec": TOOL_GET_LOCATION, "handler": get_location},
        "move_to": {"spec": TOOL_MOVE_TO, "handler": move_to},
        "estimate_move_cost": {"spec": TOOL_ESTIMATE_MOVE_COST, "handler": estimate_move_cost},
        "get_room_graph": {"spec": TOOL_GET_ROOM_GRAPH, "handler": get_room_graph},
    })
