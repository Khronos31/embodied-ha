#!/usr/bin/env python3
"""Render opt-in ambient speech history under Embodied HA sensory rules.

The Extensions add-on owns RTSP/VAD/STT and bounded transcript storage.  This
module remains in Embodied HA because body position, prompt meaning, and the
decision to surface an observation are agent responsibilities.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from auditory_context import format_recent_auditory_prompt
from state_utils import clean, get_device_capabilities, load_prefs

DEFAULT_DATA_DIR = Path("/config/embodied-ha-extensions/apps/ambient_speech_context")
Scope = tuple[Literal["all", "room", "none"], str | None]


def _read_json_dict(path: str | Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def resolve_auditory_scope(prefs_file: str, body_location_file: str) -> Scope:
    """Resolve the automatic auditory scope for the current embodiment.

    A valid physical-body state (no ``current_entity``) keeps the established
    all-source background-hearing contract.  Cyber projection is narrower: a
    configured microphone exposes only its canonical room; every other device
    exposes no ambient transcript.  Unknown body state fails closed.
    """
    location = _read_json_dict(body_location_file)
    if location is None:
        return "none", None
    current_entity = clean(location.get("current_entity"))
    if not current_entity:
        return "all", None

    prefs = load_prefs(prefs_file)
    capabilities = get_device_capabilities(current_entity, prefs)
    room = clean(capabilities.get("mic_room"))
    if not capabilities.get("is_mic") or not room:
        return "none", None
    return "room", room


def _configured_max_lines(data_dir: Path) -> int:
    status = _read_json_dict(data_dir / "status.json") or {}
    value = status.get("max_lines", 3)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        return 3
    return value


def render_context(
    *,
    kind: str,
    source: str,
    prefs_file: str,
    body_location_file: str,
    user_msg: str = "",
    data_dir: Path = DEFAULT_DATA_DIR,
) -> str:
    """Return a prompt block, or an empty string when it must not be injected."""
    normalized_kind = clean(kind).lower()
    normalized_source = clean(source).lower()
    usage_path = data_dir / "usage.md"

    if normalized_kind == "chat" and normalized_source != "voice":
        return (
            f"【周辺会話履歴】必要なら {usage_path} を能動的に参照できます。"
            "これは現在地で聞こえた証拠ではありません。"
        )
    if normalized_kind != "loop" and not (
        normalized_kind == "chat" and normalized_source == "voice"
    ):
        return ""

    scope, room = resolve_auditory_scope(prefs_file, body_location_file)
    if scope == "none":
        return ""
    return format_recent_auditory_prompt(
        user_msg,
        limit=_configured_max_lines(data_dir),
        source_room_filter=room if scope == "room" else None,
        events_file=str(data_dir / "auditory_events.jsonl"),
        untrusted_observation=True,
    )


def main() -> int:
    text = render_context(
        kind=os.environ.get("EHA_EXTRA_CONTEXT_KIND", ""),
        source=os.environ.get("EHA_EXTRA_CONTEXT_SOURCE", ""),
        prefs_file=os.environ.get("EHA_PREFS_FILE", ""),
        body_location_file=os.environ.get("EHA_BODY_LOCATION_FILE", ""),
        user_msg=os.environ.get("CHAT_MESSAGE", ""),
    )
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
