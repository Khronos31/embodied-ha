"""Helpers for observe-mode visual context."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from typing import Any


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _camera_items(prefs: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(prefs, Mapping):
        return []
    cameras = prefs.get("cameras")
    if not isinstance(cameras, list):
        return []
    return [item for item in cameras if isinstance(item, dict)]


def match_camera_device(current_entity: str, prefs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a configured camera matching ``current_entity``.

    Mirrors camera-mcp.py's legacy matching: first exact ``source``, then
    ``entity``/``ha_entity``.
    """

    source = _clean(current_entity)
    if not source:
        return {}
    items = _camera_items(prefs)
    for item in items:
        if _clean(item.get("source")) == source:
            return item
    for item in items:
        if _clean(item.get("entity")) == source or _clean(item.get("ha_entity")) == source:
            return item
    return {}


def projected_camera_source(current_entity: str, prefs: Mapping[str, Any] | None) -> dict[str, str] | None:
    current_entity = _clean(current_entity)
    if not current_entity.startswith("camera."):
        return None

    matched = match_camera_device(current_entity, prefs)
    label = _clean(matched.get("label")) if matched else ""
    capture_source = (
        _clean(matched.get("ha_entity"))
        or _clean(matched.get("source"))
        or _clean(matched.get("entity"))
        or current_entity
    )
    return {
        "entity_id": current_entity,
        "source": capture_source,
        "label": label,
    }


def projected_camera_caption(current_entity: str, prefs: Mapping[str, Any] | None, *, frame_available: bool = True) -> str:
    resolved = projected_camera_source(current_entity, prefs)
    if not resolved:
        return ""
    entity_id = resolved["entity_id"]
    label = resolved["label"]
    subject = f"{label}（{entity_id}）" if label else entity_id
    if frame_available:
        return f"（現在の視界: {subject} に投射中。以下はその映像）"
    return f"（現在の視界: {subject} に投射中。ただし映像取得に失敗しました）"


def build_projected_camera_blocks(
    current_entity: str,
    prefs: Mapping[str, Any] | None,
    *,
    fetch_frame: Callable[..., bytes | None],
    ha_url: str,
    go2rtc_url: str,
    token: str,
) -> list[dict[str, Any]]:
    """Build Claude content blocks for the currently projected camera.

    Non-camera locations produce no blocks. Camera projection injects a source
    caption and, when capture succeeds, the current frame.
    """

    resolved = projected_camera_source(current_entity, prefs)
    if not resolved:
        return []

    frame = fetch_frame(resolved["source"], ha_url=ha_url, go2rtc_url=go2rtc_url, token=token)
    if not frame:
        return [{"type": "text", "text": projected_camera_caption(current_entity, prefs, frame_available=False)}]

    return [
        {"type": "text", "text": projected_camera_caption(current_entity, prefs, frame_available=True)},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(frame).decode("ascii"),
            },
        },
    ]
