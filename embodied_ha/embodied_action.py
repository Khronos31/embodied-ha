#!/usr/bin/env python3
"""Helpers for embodied action/perception cost classification.

The goal is to keep the world model simple:
- same-room interaction is cheap and grounding
- remote access is convenient but slightly destabilizing
- physical movement reuses the existing move_cost and grounds the body again
"""

from __future__ import annotations

import os
from typing import Any

import body_state
from sensory_origin import classify_sensory_origin
from state_utils import clean, coerce_float


def _data_dir() -> str:
    return clean(os.environ.get("EHA_DATA_DIR")) or "/config/embodied-ha"


def body_state_path() -> str:
    return clean(os.environ.get("EHA_BODY_STATE_FILE")) or os.path.join(_data_dir(), "body_state.json")


def action_mode_for_rooms(body_room: Any, target_room: Any) -> str:
    if clean(body_room) and clean(body_room) == clean(target_room):
        return "direct_in_room"
    return "remote_avatar"


def action_cost_for_mode(action_mode: str, move_cost: Any = None) -> float:
    mode = clean(action_mode)
    distance = max(0.0, coerce_float(move_cost, 0.0))
    if mode == "physical_move":
        return round(distance, 3)
    if mode == "direct_in_room":
        return 0.05
    return round(0.35 + min(0.20, distance * 0.05), 3)


def action_fields_for_sensory(sensory: dict[str, Any], host: Any = "") -> dict[str, Any]:
    body_room = clean(sensory.get("body_room"))
    source_room = clean(sensory.get("source_room"))
    move_cost = sensory.get("move_cost")
    mode = action_mode_for_rooms(body_room, source_room)
    return {
        "action_mode": mode,
        "action_cost": action_cost_for_mode(mode, move_cost),
        "target_room": source_room or None,
        "target_host": clean(host) or None,
    }


def action_fields_for_control(entity_id: Any, domain: Any = "", service: Any = "") -> dict[str, Any]:
    sensory = classify_sensory_origin(
        source=entity_id,
        label=f"{clean(domain)}.{clean(service)}",
        entity_id=entity_id,
        modality="action",
    )
    return {
        **action_fields_for_sensory(sensory),
        "body_room": sensory.get("body_room"),
        "source_room": sensory.get("source_room"),
        "source_area": sensory.get("source_area"),
        "move_cost": sensory.get("move_cost"),
        "target_host": clean(entity_id) or None,
    }


def action_fields_for_move(target_room: Any, move_cost: Any) -> dict[str, Any]:
    return {
        "action_mode": "physical_move",
        "action_cost": action_cost_for_mode("physical_move", move_cost),
        "target_room": clean(target_room) or None,
        "target_host": None,
    }


def apply_action_to_body_state(
    *,
    action_mode: Any,
    action_cost: Any = None,
    target_room: Any = "",
    target_host: Any = "",
    move_cost: Any = None,
) -> dict[str, Any]:
    path = body_state_path()
    current = body_state.load_state(path)
    updated = body_state.apply_action_effect(
        current,
        action_mode=clean(action_mode),
        action_cost=coerce_float(action_cost, 0.0),
        target_room=clean(target_room),
        target_host=clean(target_host),
        move_cost=coerce_float(move_cost, 0.0),
    )
    body_state.save_state(path, updated)
    return updated
