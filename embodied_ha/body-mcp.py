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
import subprocess
import threading
from typing import Any

from embodied_action import action_fields_for_move, apply_action_to_body_state
from mcp_lib import serve, text
from state_utils import clean, load_prefs, now, read_json, write_json

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


def prefs_path() -> str:
    return clean(os.environ.get("EHA_PREFS_FILE")) or os.path.join(_data_dir(), "preferences.json")


def load_projection_targets() -> list[dict[str, Any]]:
    prefs = read_json(prefs_path(), {})
    if not isinstance(prefs, dict):
        return []
    targets = prefs.get("projection_targets", [])
    return targets if isinstance(targets, list) else []


def resolve_external_room(entity: str) -> str | None:
    """external://xxx → room_id を projection_targets から引く"""
    for target in load_projection_targets():
        if isinstance(target, dict) and target.get("id") == entity:
            return clean(target.get("room")) or None
    return None


def load_preferences() -> dict[str, Any]:
    return load_prefs(prefs_path())


def _tcp_host(entity: str) -> str:
    entity = clean(entity)
    if entity.startswith("tcp://"):
        return entity[6:].split(":")[0]
    return ""


def normalize_cyberspace_entity(entity: str, prefs: dict[str, Any]) -> tuple[str, str | None]:
    entity = clean(entity)
    if not entity:
        return "", None

    for item in prefs.get("mics", []):
        if not isinstance(item, dict):
            continue
        if clean(item.get("source")) == entity:
            normalized = clean(item.get("entity")) or entity
            return normalized, clean(item.get("room")) or None

    host = _tcp_host(entity)
    if host:
        speakers = prefs.get("speakers", [])
        if isinstance(speakers, dict):
            speakers = [{**(v if isinstance(v, dict) else {}), "room": k} for k, v in speakers.items()]
        elif not isinstance(speakers, list):
            speakers = []
        for item in speakers:
            if clean(item.get("host")) == host:
                normalized = clean(item.get("entity")) or entity
                return normalized, clean(item.get("room")) or None

    for item in prefs.get("cameras", []):
        if not isinstance(item, dict):
            continue
        matched = any(
            clean(item.get(k)) == entity
            for k in ("entity", "source", "ha_entity")
            if clean(item.get(k))
        )
        if matched:
            canonical = clean(item.get("entity")) or entity
            return canonical, clean(item.get("room")) or None

    return entity, None


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
    projected = resolve_room(state.get("projected_room"), graph)
    return {
        "current_room": current,
        "display_name": _room_label(current, graph),
        "updated_at": clean(state.get("updated_at")) or None,
        "previous_room": resolve_room(state.get("previous_room"), graph),
        "last_move_cost": state.get("last_move_cost"),
        "last_move_path": state.get("last_move_path") if isinstance(state.get("last_move_path"), list) else [],
        "projected_room": projected,
        "projected_display_name": _room_label(projected, graph) if projected else None,
        "projection_updated_at": clean(state.get("projection_updated_at")) or None,
        "current_entity": clean(state.get("current_entity")) or "",
        "source": clean(state.get("source")) or "alsa://default",
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




def publish_body_presence(state: dict[str, Any]) -> None:
    mqtt_host = clean(os.environ.get("MQTT_HOST"))
    if not mqtt_host:
        return
    mqtt_port = clean(os.environ.get("MQTT_PORT")) or "1883"
    mqtt_user = clean(os.environ.get("MQTT_USER"))
    mqtt_pass = clean(os.environ.get("MQTT_PASS"))
    physical_room = clean(state.get("current_room"))
    current_entity = clean(state.get("current_entity"))
    current_place = current_entity or "身体の中"
    base = ["mosquitto_pub", "-h", mqtt_host, "-p", mqtt_port]
    if mqtt_user:
        base.extend(["-u", mqtt_user])
    if mqtt_pass:
        base.extend(["-P", mqtt_pass])
    for topic, payload in (
        ("embodied_ha/body/physical_room/state", physical_room),
        ("embodied_ha/body/current_place/state", current_place),
    ):
        try:
            subprocess.run(base + ["-r", "-t", topic, "-m", payload], capture_output=True, text=True, timeout=5)
        except Exception:
            pass


def get_location(args: dict[str, Any]):
    graph = load_room_graph()
    state = load_location_state(graph)
    current = state["current_room"]
    projected = state.get("projected_room")
    presence_mode = "remote_avatar" if projected else "direct"
    payload = {
        "current_room": current,
        "display_name": _room_label(current, graph),
        "physical_room": current,
        "physical_display_name": _room_label(current, graph),
        "presence_mode": presence_mode,
        "active_room": projected or current,
        "active_display_name": _room_label(projected or current, graph),
        "projected_room": projected,
        "projected_display_name": state.get("projected_display_name"),
        "current_entity": state.get("current_entity"),
        "updated_at": state.get("updated_at"),
        "previous_room": state.get("previous_room"),
        "last_move_cost": state.get("last_move_cost"),
        "last_move_path": state.get("last_move_path"),
        "sensory_origin_hint": "remote_avatar" if projected else "direct",
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
    if state.get("projected_room"):
        return _json_text({
            "error": "cannot move physical body while in cyber mode",
            "projected_room": state.get("projected_room"),
            "current_entity": state.get("current_entity"),
            "hint": "return_to_body を先に呼んでください",
        }), True
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
        "projected_room": None,
        "projected_display_name": None,
        "projection_updated_at": None,
        "current_entity": "",
        "source": clean(args.get("source")) or "body-mcp",
    }
    if reason:
        new_state["reason"] = reason
    save_location_state(new_state)
    publish_body_presence(new_state)
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
    event.update(action_fields_for_move(target, cost))
    append_move_log(event)
    try:
        apply_action_to_body_state(
            action_mode=event.get("action_mode"),
            action_cost=event.get("action_cost"),
            target_room=target,
            move_cost=cost,
        )
    except Exception:
        pass
    return _json_text({"state": new_state, "event": event})


def enter_cyberspace(args: dict[str, Any]):
    graph = load_room_graph()
    state = load_location_state(graph)
    if state.get("projected_room"):
        return _json_text({
            "error": "already in cyberspace",
            "projected_room": state.get("projected_room"),
            "current_entity": state.get("current_entity"),
        }), True
    raw_entity = clean(args.get("entity"))
    if not raw_entity:
        return _json_text({"error": "missing entity"}), True
    prefs = load_preferences()
    entity, entry_room = normalize_cyberspace_entity(raw_entity, prefs)
    target_room: str | None = None
    if raw_entity.startswith("external://"):
        target_room = resolve_external_room(raw_entity)
        if not target_room:
            return _json_text({"error": "unknown external projection target", "entity": raw_entity}), True
    else:
        target_room = entry_room
        if not target_room:
            from sensory_origin import area_for_entity, resolve_area_room

            area = area_for_entity(raw_entity)
            target_room = resolve_area_room(area, graph) if area else None
        if not target_room:
            return _json_text({
                "error": "エンティティの部屋を解決できません。preferences.json に room を設定するか、HA でエリアを設定してください",
                "entity": raw_entity,
                "normalized_entity": entity,
            }), True
    body_room = state["current_room"]
    if target_room != body_room:
        return _json_text({
            "error": "physical room mismatch",
            "physical_room": body_room,
            "requested_room": target_room,
        }), True
    cost, path = shortest_path(body_room, target_room, graph)
    timestamp = now().isoformat(timespec="seconds")
    reason = clean(args.get("reason")) or None
    new_state = {
        **state,
        "current_room": body_room,
        "display_name": _room_label(body_room, graph),
        "projected_room": target_room,
        "projected_display_name": _room_label(target_room, graph),
        "projection_updated_at": timestamp,
        "current_entity": entity,
        "updated_at": timestamp,
        "source": clean(args.get("source")) or "body-mcp",
    }
    if reason:
        new_state["reason"] = reason
    save_location_state(new_state)
    publish_body_presence(new_state)
    event = {
        "timestamp": timestamp,
        "kind": "body_project",
        "body_room": body_room,
        "body_display_name": _room_label(body_room, graph),
        "to": target_room,
        "to_display_name": _room_label(target_room, graph),
        "target_host": entity,
        "cost": cost,
        "path": path,
        "path_display": _format_path(path, graph),
        "reason": reason,
        "projection_mode": "enter_remote",
        "sensory_origin_after_project": "remote_avatar",
        "action_mode": "remote_avatar",
        "action_cost": 0.35,
        "target_room": target_room,
    }
    append_move_log(event)
    try:
        apply_action_to_body_state(
            action_mode=event.get("action_mode"),
            action_cost=event.get("action_cost"),
            target_room=target_room,
            target_host=entity,
            move_cost=cost,
        )
    except Exception:
        pass
    return _json_text({"state": new_state, "event": event})


def move_cyber(args: dict[str, Any]):
    graph = load_room_graph()
    state = load_location_state(graph)
    if not state.get("projected_room"):
        return _json_text({
            "error": "not in cyberspace",
            "projected_room": state.get("projected_room"),
        }), True
    raw_entity = clean(args.get("entity"))
    if not raw_entity:
        return _json_text({"error": "missing entity"}), True
    prefs = load_preferences()
    entity, entry_room = normalize_cyberspace_entity(raw_entity, prefs)
    target_room: str | None = None
    if raw_entity.startswith("external://"):
        target_room = resolve_external_room(raw_entity)
        if not target_room:
            return _json_text({"error": "unknown external projection target", "entity": raw_entity}), True
    else:
        target_room = entry_room
        if not target_room:
            from sensory_origin import area_for_entity, resolve_area_room

            area = area_for_entity(raw_entity)
            target_room = resolve_area_room(area, graph) if area else None
    current_projected_room = state["projected_room"]
    final_projected_room = target_room or current_projected_room
    cost = 0.0
    path = [current_projected_room]
    if target_room and target_room != current_projected_room:
        resolved_cost, resolved_path = shortest_path(current_projected_room, target_room, graph)
        if resolved_cost is not None:
            cost = resolved_cost
            path = resolved_path
    timestamp = now().isoformat(timespec="seconds")
    reason = clean(args.get("reason")) or None
    new_state = {
        **state,
        "current_room": state["current_room"],
        "display_name": _room_label(state["current_room"], graph),
        "projected_room": final_projected_room,
        "projected_display_name": _room_label(final_projected_room, graph) if final_projected_room else None,
        "projection_updated_at": timestamp,
        "current_entity": entity,
        "updated_at": timestamp,
        "source": clean(args.get("source")) or "body-mcp",
    }
    if reason:
        new_state["reason"] = reason
    save_location_state(new_state)
    publish_body_presence(new_state)
    event = {
        "timestamp": timestamp,
        "kind": "body_project",
        "body_room": state["current_room"],
        "body_display_name": _room_label(state["current_room"], graph),
        "to": final_projected_room,
        "to_display_name": _room_label(final_projected_room, graph) if final_projected_room else None,
        "target_host": entity,
        "cost": cost,
        "path": path,
        "path_display": _format_path(path, graph) if path else [],
        "reason": reason,
        "projection_mode": "remote_move",
        "sensory_origin_after_project": "remote_avatar",
        "action_mode": "remote_avatar",
        "action_cost": 0.0,
        "target_room": final_projected_room,
    }
    append_move_log(event)
    try:
        apply_action_to_body_state(
            action_mode=event.get("action_mode"),
            action_cost=event.get("action_cost"),
            target_room=final_projected_room,
            target_host=entity,
            move_cost=cost,
        )
    except Exception:
        pass
    return _json_text({"state": new_state, "event": event})


def return_to_body(args: dict[str, Any]):
    graph = load_room_graph()
    state = load_location_state(graph)
    projected_room = state.get("projected_room")
    body_room = state["current_room"]
    if projected_room and projected_room != body_room:
        return _json_text({
            "error": "cannot return to body from a different room",
            "physical_room": body_room,
            "projected_room": projected_room,
            "current_entity": state.get("current_entity"),
            "hint": f"先に move_cyber で {body_room} にあるエンティティへ移動してから return_to_body を呼んでください",
        }), True
    current = state["current_room"]
    timestamp = now().isoformat(timespec="seconds")
    reason = clean(args.get("reason")) or None
    host = clean(args.get("host")) or clean(state.get("source")) or "alsa://default"
    new_state = {
        **state,
        "current_room": current,
        "display_name": _room_label(current, graph),
        "projected_room": None,
        "projected_display_name": None,
        "projection_updated_at": timestamp,
        "current_entity": "",
        "updated_at": timestamp,
        "source": clean(args.get("source")) or "body-mcp",
    }
    if reason:
        new_state["reason"] = reason
    save_location_state(new_state)
    publish_body_presence(new_state)
    event = {
        "timestamp": timestamp,
        "kind": "body_return",
        "to": current,
        "to_display_name": _room_label(current, graph),
        "target_host": host,
        "reason": reason,
        "sensory_origin_after_return": "direct",
        "action_mode": "direct_in_room",
        "action_cost": 0.05,
        "target_room": current,
    }
    append_move_log(event)
    try:
        apply_action_to_body_state(
            action_mode="direct_in_room",
            action_cost=event.get("action_cost"),
            target_room=current,
            target_host=host,
            move_cost=0.0,
        )
    except Exception:
        pass
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
    "description": "部屋グラフ上で物理体そのものを移動し、移動コストと経路を記録する。物理体モード中（電脳体に投射していない状態）のみ使用できる。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "room": {"type": "string", "description": "移動先の room id または別名"},
            "reason": {"type": "string", "description": "移動する理由。任意"},
        },
        "required": ["room"],
    },
}


TOOL_ENTER_CYBERSPACE = {
    "name": "enter_cyberspace",
    "description": (
        "物理体は今の部屋に残したまま、同室にあるデバイスやカメラへ電脳体として侵入する。\n"
        "【使う場面】カメラの映像を取得する / 特定マイクで集中して聴く / スマホや外部デバイスに意識を飛ばす。\n"
        "【使わない場面】スピーカーから声を出したいだけなら enter_cyberspace は不要。\n"
        "  - 現在の部屋で話す → speak\n"
        "  - 別の部屋で話す → move_to でその部屋に移動してから speak\n"
        "侵入できるのは物理体と同じ部屋にあるエンティティのみ。別室のエンティティを指定するとエラー。\n"
        "すでに電脳体モード中の場合は使えない（move_cyber を使うこと）。\n"
        "entity には preferences に登録済みのデバイス entity id（例: voice_s3r_hallway, camera_hallway）"
        "または HA エンティティ ID（camera.xxx など）または external://xxx を渡す。\n"
        "HA エンティティの部屋は HA のエリア設定から自動解決する（エリア未設定はエラー）。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "侵入先のデバイス entity id または HA エンティティ ID または external://xxx"},
            "reason": {"type": "string", "description": "侵入する理由。任意"},
        },
        "required": ["entity"],
    },
}

TOOL_MOVE_CYBER = {
    "name": "move_cyber",
    "description": (
        "電脳体モード中に、別のエンティティへ移動する。\n"
        "enter_cyberspace で電脳空間に入った後にのみ使える。\n"
        "物理体の場所に関係なく、どのエンティティへでも自由に移動できる。\n"
        "entity には preferences に登録済みのデバイス entity id"
        "または HA エンティティ ID または external://xxx を渡す。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "移動先のデバイス entity id または HA エンティティ ID または external://xxx"},
            "reason": {"type": "string", "description": "移動する理由。任意"},
        },
        "required": ["entity"],
    },
}

TOOL_RETURN_TO_BODY = {
    "name": "return_to_body",
    "description": "電脳体モードを解除して物理体に帰還する。物理体と同じ部屋のエンティティに投射中のときのみ使用できる。別の部屋にいる場合は、先に move_cyber で物理体のいる部屋のエンティティへ移動すること。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "戻った先のデバイス識別子。例: alsa://default"},
            "reason": {"type": "string", "description": "戻る理由。任意"},
        },
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
        "enter_cyberspace": {"spec": TOOL_ENTER_CYBERSPACE, "handler": enter_cyberspace},
        "move_cyber": {"spec": TOOL_MOVE_CYBER, "handler": move_cyber},
        "return_to_body": {"spec": TOOL_RETURN_TO_BODY, "handler": return_to_body},
        "estimate_move_cost": {"spec": TOOL_ESTIMATE_MOVE_COST, "handler": estimate_move_cost},
        "get_room_graph": {"spec": TOOL_GET_ROOM_GRAPH, "handler": get_room_graph},
    })
