"""Shared audio source resolution helpers."""

from __future__ import annotations

from state_utils import clean

DEFAULT_SOURCE = ""


def _room_sources(source_configs: list[dict], room: str) -> list[dict]:
    target_room = clean(room)
    if not target_room:
        return []
    return [
        cfg
        for cfg in source_configs
        if isinstance(cfg, dict) and clean(cfg.get("room")) == target_room
    ]


def _best_room_source(source_configs: list[dict], room: str) -> str:
    room_cfgs = _room_sources(source_configs, room)
    best = room_cfgs[0] if room_cfgs else None
    return clean(best.get("source")) if best else ""


def resolve_audio_source(body_loc: dict, source_configs: list[dict], default_source: str = DEFAULT_SOURCE) -> str:
    """Resolve the listen source from body location and configured audio sources.

    Resolution order:
    - cyber body projected into a room: that room's configured RTSP source
    - physical body: current room's configured RTSP source
    - otherwise: first configured source, then ``default_source``
    """

    body_loc = body_loc if isinstance(body_loc, dict) else {}
    source_configs = [cfg for cfg in source_configs if isinstance(cfg, dict)]

    current_entity = clean(body_loc.get("current_entity"))
    projected_room = clean(body_loc.get("projected_room"))
    current_room = clean(body_loc.get("current_room"))

    if current_entity and projected_room:
        room_source = _best_room_source(source_configs, projected_room)
        if room_source:
            return room_source

    if current_room:
        room_source = _best_room_source(source_configs, current_room)
        if room_source:
            return room_source

    if source_configs:
        fallback = clean(source_configs[0].get("source"))
        if fallback:
            return fallback

    return clean(default_source)
