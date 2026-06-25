#!/usr/bin/env python3
"""Shared sociality state helpers for embodied-ha.

This module stores per-person boundary state in ``person_models.json`` and
provides the core interrupt decision used by both the MCP server and the
runtime boundary gate.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from state_utils import clean as _clean
from state_utils import coerce_float as _coerce_float
from state_utils import now as _now
from state_utils import parse_ts as _parse_ts
from state_utils import write_json as _write_json

_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_DIR, "log"))

PERSON_MODELS_FILE = "person_models.json"
SHARED_FOCUS_FILE = "shared_focus.json"


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on", "home", "present", "occupied"}


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _path(log_dir: str | None, name: str) -> str:
    return os.path.join(log_dir or _DEFAULT_LOG_DIR, name)


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if isinstance(data, type(default)) else default


def default_quiet_window() -> dict[str, Any]:
    return {
        "active": False,
        "start": "",
        "end": "",
        "note": "",
    }


def default_consent() -> dict[str, Any]:
    return {
        "speak": True,
        "action": True,
        "updated_at": "",
        "note": "",
    }


def default_turn_taking() -> dict[str, Any]:
    return {
        "state": "open",
        "awaiting_reply": False,
        "last_speaker": "",
        "last_kind": "",
        "last_text": "",
        "last_turn_at": "",
        "cooldown_seconds": 90,
    }


def default_shared_focus() -> dict[str, Any]:
    return {
        "topic": "",
        "context": "",
        "object_id": "",
        "scene_source": "",
        "last_seen_at": "",
        "updated_at": "",
    }


def default_person_model(person: str = "", shared_focus: Mapping[str, Any] | None = None) -> dict[str, Any]:
    model = {
        "person": _clean(person),
        "boundary": {
            "quiet_window": default_quiet_window(),
            "consent": default_consent(),
            "turn_taking": default_turn_taking(),
        },
        "shared_focus": normalize_shared_focus(shared_focus),
        "updated_at": "",
    }
    return model


def normalize_quiet_window(raw: Any) -> dict[str, Any]:
    window = default_quiet_window()
    if not isinstance(raw, dict):
        return window
    window["active"] = _truthy(raw.get("active"))
    window["start"] = _clean(raw.get("start"))
    window["end"] = _clean(raw.get("end"))
    window["note"] = _clean(raw.get("note"))
    return window


def normalize_consent(raw: Any) -> dict[str, Any]:
    consent = default_consent()
    if not isinstance(raw, dict):
        return consent
    consent["speak"] = _truthy(raw.get("speak", consent["speak"]))
    consent["action"] = _truthy(raw.get("action", consent["action"]))
    consent["updated_at"] = _clean(raw.get("updated_at"))
    consent["note"] = _clean(raw.get("note"))
    return consent


def normalize_turn_taking(raw: Any) -> dict[str, Any]:
    turn = default_turn_taking()
    if not isinstance(raw, dict):
        return turn
    turn["state"] = _clean(raw.get("state")) or turn["state"]
    turn["awaiting_reply"] = _truthy(raw.get("awaiting_reply"))
    turn["last_speaker"] = _clean(raw.get("last_speaker"))
    turn["last_kind"] = _clean(raw.get("last_kind"))
    turn["last_text"] = _clean(raw.get("last_text"))
    turn["last_turn_at"] = _clean(raw.get("last_turn_at"))
    turn["cooldown_seconds"] = max(0, _coerce_int(raw.get("cooldown_seconds"), turn["cooldown_seconds"]))
    return turn


def normalize_shared_focus(raw: Any) -> dict[str, Any]:
    focus = default_shared_focus()
    if not isinstance(raw, dict):
        return focus
    focus["topic"] = _clean(raw.get("topic"))
    focus["context"] = _clean(raw.get("context"))
    focus["object_id"] = _clean(raw.get("object_id"))
    focus["scene_source"] = _clean(raw.get("scene_source"))
    focus["last_seen_at"] = _clean(raw.get("last_seen_at"))
    focus["updated_at"] = _clean(raw.get("updated_at"))
    return focus


def normalize_person_model(person: str, raw: Any, shared_focus: Mapping[str, Any] | None = None) -> dict[str, Any]:
    model = default_person_model(person, shared_focus=shared_focus)
    if not isinstance(raw, dict):
        return model

    source = raw.get("boundary") if isinstance(raw.get("boundary"), dict) else raw
    model["person"] = _clean(raw.get("person")) or model["person"]
    model["boundary"]["quiet_window"] = normalize_quiet_window(source.get("quiet_window"))
    model["boundary"]["consent"] = normalize_consent(source.get("consent"))
    model["boundary"]["turn_taking"] = normalize_turn_taking(source.get("turn_taking"))

    focus_source = raw.get("shared_focus")
    if isinstance(focus_source, dict):
        model["shared_focus"] = normalize_shared_focus(focus_source)
    elif shared_focus is not None:
        model["shared_focus"] = normalize_shared_focus(shared_focus)

    model["updated_at"] = _clean(raw.get("updated_at"))
    return model


def load_shared_focus(log_dir: str | None = None) -> dict[str, Any]:
    return normalize_shared_focus(_load_json(_path(log_dir, SHARED_FOCUS_FILE), default_shared_focus()))


def save_shared_focus(log_dir: str | None, focus: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_shared_focus(dict(focus))
    _write_json(_path(log_dir, SHARED_FOCUS_FILE), normalized)
    return normalized


def load_person_models(log_dir: str | None = None) -> dict[str, Any]:
    data = _load_json(_path(log_dir, PERSON_MODELS_FILE), {})
    return data if isinstance(data, dict) else {}


def save_person_models(log_dir: str | None, models: Mapping[str, Any]) -> None:
    _write_json(_path(log_dir, PERSON_MODELS_FILE), dict(models))


def get_person_model(log_dir: str | None, person: str) -> dict[str, Any]:
    person_name = _clean(person)
    models = load_person_models(log_dir)
    shared_focus = load_shared_focus(log_dir)
    return normalize_person_model(person_name, models.get(person_name), shared_focus=shared_focus)


def _merge_mapping(target: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key] = _merge_mapping(dict(target[key]), value)
        else:
            target[key] = value
    return target


def _apply_shared_focus_patch(log_dir: str | None, patch: Any) -> dict[str, Any] | None:
    if isinstance(patch, str):
        topic = _clean(patch)
        if not topic:
            return None
        focus = {"topic": topic, "context": "", "updated_at": _now().isoformat(timespec="seconds")}
        return save_shared_focus(log_dir, focus)
    if isinstance(patch, dict):
        focus = load_shared_focus(log_dir)
        focus = _merge_mapping(focus, patch)
        focus["updated_at"] = _clean(focus.get("updated_at")) or _now().isoformat(timespec="seconds")
        return save_shared_focus(log_dir, focus)
    return None


def save_person_model(log_dir: str | None, person: str, model: Mapping[str, Any]) -> dict[str, Any]:
    person_name = _clean(person)
    normalized = normalize_person_model(person_name, dict(model), shared_focus=load_shared_focus(log_dir))
    if not person_name:
        return normalized
    models = load_person_models(log_dir)
    models[person_name] = normalized
    save_person_models(log_dir, models)
    return normalized


def merge_person_model(log_dir: str | None, person: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    person_name = _clean(person)
    current = get_person_model(log_dir, person_name)
    merged = normalize_person_model(person_name, current, shared_focus=current.get("shared_focus"))

    boundary_patch = patch.get("boundary")
    if isinstance(boundary_patch, dict):
        for section in ("quiet_window", "consent", "turn_taking"):
            if section in boundary_patch:
                merged["boundary"][section] = _merge_mapping(
                    dict(merged["boundary"][section]),
                    boundary_patch.get(section) or {},
                )

    for section in ("quiet_window", "consent", "turn_taking"):
        if section in patch and isinstance(patch.get(section), dict):
            merged["boundary"][section] = _merge_mapping(
                dict(merged["boundary"][section]),
                patch.get(section) or {},
            )

    if "shared_focus" in patch and patch.get("shared_focus") is not None:
        focus_patch = patch.get("shared_focus")
        if isinstance(focus_patch, dict):
            merged["shared_focus"] = _merge_mapping(dict(merged["shared_focus"]), focus_patch)
            merged["shared_focus"]["updated_at"] = (
                _clean(merged["shared_focus"].get("updated_at"))
                or _now().isoformat(timespec="seconds")
            )
            if _truthy(patch.get("sync_shared_focus", True)):
                save_shared_focus(log_dir, merged["shared_focus"])
        else:
            synced = _apply_shared_focus_patch(log_dir, focus_patch)
            if synced is not None:
                merged["shared_focus"] = synced

    if "updated_at" in patch:
        merged["updated_at"] = _clean(patch.get("updated_at")) or _now().isoformat(timespec="seconds")
    else:
        merged["updated_at"] = _now().isoformat(timespec="seconds")

    return save_person_model(log_dir, person_name, merged)


def record_boundary(log_dir: str | None, person: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    if not _clean(person):
        return default_person_model("", load_shared_focus(log_dir))
    return merge_person_model(log_dir, person, patch)


def record_consent(
    log_dir: str | None,
    person: str,
    kind: str,
    granted: Any,
    note: str = "",
) -> dict[str, Any]:
    person_name = _clean(person)
    if not person_name:
        return default_person_model("", load_shared_focus(log_dir))

    current = get_person_model(log_dir, person_name)
    consent = dict(current["boundary"]["consent"])
    granted_bool = _truthy(granted)
    kind_norm = _compact(kind)

    targets = []
    if kind_norm in {"", "all", "boundary", "general"}:
        targets = ["speak", "action"]
    elif kind_norm in {"speak", "talk", "chat", "interrupt"}:
        targets = ["speak"]
    elif kind_norm in {"action", "operate", "control"}:
        targets = ["action"]
    elif kind_norm in {"sharedfocus", "shared_focus"}:
        targets = ["shared_focus"]
    else:
        targets = ["speak", "action"]

    for target in targets:
        if target in consent:
            consent[target] = granted_bool

    consent["updated_at"] = _now().isoformat(timespec="seconds")
    if note:
        consent["note"] = _clean(note)

    current["boundary"]["consent"] = consent
    current["updated_at"] = consent["updated_at"]
    return save_person_model(log_dir, person_name, current)


def _focus_matches(metadata_blob: str, focus: Mapping[str, Any]) -> bool:
    topic = _compact(focus.get("topic"))
    context = _compact(focus.get("context"))
    if topic and topic in metadata_blob:
        return True
    if context and context in metadata_blob:
        return True
    return False


def _quiet_window_active(hour: int, window: Mapping[str, Any]) -> bool:
    if not _truthy(window.get("active")):
        return False

    start_raw = _clean(window.get("start"))
    end_raw = _clean(window.get("end"))
    if not start_raw and not end_raw:
        return True

    def _hour(value: str) -> int | None:
        if not value:
            return None
        try:
            return max(0, min(23, int(str(value).split(":", 1)[0])))
        except Exception:
            return None

    start = _hour(start_raw)
    end = _hour(end_raw)
    if start is None and end is None:
        return True
    if start is None:
        start = end
    if end is None:
        end = start
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _body_state_bias(body_state: Mapping[str, Any] | None) -> dict[str, float]:
    state = body_state if isinstance(body_state, Mapping) else {}
    return {
        "energy": _coerce_float(state.get("energy"), 0.6),
        "stress": _coerce_float(state.get("stress"), 0.25),
        "confidence": _coerce_float(state.get("confidence"), 0.55),
        "social_openness": _coerce_float(state.get("social_openness"), 0.5),
    }


def _text_blob(metadata: Mapping[str, Any]) -> str:
    parts = []
    for key in ("text", "message", "utterance", "reason", "trigger", "note", "summary"):
        value = metadata.get(key)
        if value is not None:
            parts.append(_clean(value))
    for key in ("room", "source", "speaker", "kind", "intent"):
        value = metadata.get(key)
        if value is not None:
            parts.append(_clean(value))
    return _compact(" ".join(part for part in parts if part))


def _direct_override(metadata: Mapping[str, Any], blob: str) -> bool:
    explicit = any(
        _truthy(metadata.get(key))
        for key in (
            "direct",
            "direct_call",
            "explicit_call",
            "addressed",
            "called",
            "reply_expected",
        )
    )
    if explicit:
        return True
    direct_keywords = (
        "呼んだ",
        "呼ばれ",
        "話しかけ",
        "返事",
        "お願い",
        "助けて",
        "ちょっと",
        "聞いて",
        "見て",
        "help",
        "hey",
    )
    return any(_compact(keyword) in blob for keyword in direct_keywords)


def _urgent_override(metadata: Mapping[str, Any], blob: str) -> bool:
    if any(
        _truthy(metadata.get(key))
        for key in ("urgent", "emergency", "danger", "hazard", "alert", "alarm", "safety")
    ):
        return True
    urgent_keywords = (
        "fire",
        "smoke",
        "gas",
        "leak",
        "flood",
        "water",
        "intruder",
        "fall",
        "injury",
        "倒れ",
        "火事",
        "煙",
        "ガス",
        "漏れ",
        "洪水",
        "侵入",
        "転倒",
        "救急",
    )
    return any(_compact(keyword) in blob for keyword in urgent_keywords)


def _turn_taking_blocks(turn: Mapping[str, Any], *, hour: int, focus_match: bool) -> bool:
    if focus_match:
        return False

    state = _compact(turn.get("state"))
    awaiting = _truthy(turn.get("awaiting_reply"))
    cooldown_seconds = max(0, _coerce_int(turn.get("cooldown_seconds"), 0))
    last_turn = _parse_ts(turn.get("last_turn_at"))
    elapsed = None
    if last_turn is not None:
        try:
            elapsed = max(0, int((_now() - last_turn).total_seconds()))
        except Exception:
            elapsed = None

    if state in {"closed", "blocked", "held", "waiting"}:
        return True
    if awaiting:
        return True
    if cooldown_seconds > 0 and elapsed is not None and elapsed < cooldown_seconds:
        return True
    return False


def _build_result(
    *,
    allowed: bool,
    reason: str,
    fallback: Any = None,
    model: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    direct: bool = False,
    urgent: bool = False,
    focus_match: bool = False,
    quiet_window_active: bool = False,
) -> dict[str, Any]:
    result = {
        "allowed": bool(allowed),
        "reason": reason,
        "fallback": fallback,
    }
    if model is not None:
        result["person"] = _clean(model.get("person"))
        result["quiet_window"] = model.get("boundary", {}).get("quiet_window", {})
        result["consent"] = model.get("boundary", {}).get("consent", {})
        result["turn_taking"] = model.get("boundary", {}).get("turn_taking", {})
        result["shared_focus"] = model.get("shared_focus", {})
    if metadata is not None:
        result["metadata"] = dict(metadata)
    result["direct_override"] = bool(direct)
    result["urgent_override"] = bool(urgent)
    result["focus_match"] = bool(focus_match)
    result["quiet_window_active"] = bool(quiet_window_active)
    return result


def evaluate_interrupt(
    model: Mapping[str, Any],
    *,
    mode: str,
    intent: str,
    hour: int,
    metadata: Mapping[str, Any] | None = None,
    body_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the sociality boundary decision for a person model."""

    metadata = metadata if isinstance(metadata, Mapping) else {}
    intent = _compact(intent)
    mode = _compact(mode)
    try:
        hour = int(hour)
    except Exception:
        hour = 12

    if intent not in {"speak", "action"}:
        return _build_result(
            allowed=False,
            reason=f"未知のintent: {intent or '（空）'}",
            model=model,
            metadata=metadata,
        )

    blob = _text_blob(metadata)
    focus = normalize_shared_focus(model.get("shared_focus"))
    direct = _direct_override(metadata, blob)
    urgent = _urgent_override(metadata, blob)
    quiet_window_active = _quiet_window_active(hour, model.get("boundary", {}).get("quiet_window", {}))
    focus_match = _focus_matches(blob, focus)
    body = _body_state_bias(body_state)

    if direct:
        return _build_result(
            allowed=True,
            reason="direct_override",
            model=model,
            metadata=metadata,
            direct=True,
            urgent=urgent,
            focus_match=focus_match,
            quiet_window_active=quiet_window_active,
        )

    if mode == "chat" and intent == "speak":
        return _build_result(
            allowed=True,
            reason="chat direct response",
            model=model,
            metadata=metadata,
            direct=False,
            urgent=urgent,
            focus_match=focus_match,
            quiet_window_active=quiet_window_active,
        )

    if urgent:
        return _build_result(
            allowed=True,
            reason="urgent_override",
            model=model,
            metadata=metadata,
            direct=direct,
            urgent=True,
            focus_match=focus_match,
            quiet_window_active=quiet_window_active,
        )

    consent = model.get("boundary", {}).get("consent", {})
    consent_key = "speak" if intent == "speak" else "action"
    if quiet_window_active:
        return _build_result(
            allowed=False,
            reason="quiet_window",
            fallback="wait",
            model=model,
            metadata=metadata,
            focus_match=focus_match,
            quiet_window_active=True,
        )

    if not _truthy(consent.get(consent_key, True)):
        return _build_result(
            allowed=False,
            reason=f"consent:{consent_key}",
            fallback="reconfirm",
            model=model,
            metadata=metadata,
            focus_match=focus_match,
        )

    if _turn_taking_blocks(model.get("boundary", {}).get("turn_taking", {}), hour=hour, focus_match=focus_match):
        return _build_result(
            allowed=False,
            reason="turn_taking",
            fallback="wait",
            model=model,
            metadata=metadata,
            focus_match=focus_match,
        )

    if intent == "speak":
        stress = body["stress"]
        social_openness = body["social_openness"]
        energy = body["energy"]
        if not focus_match and (stress >= 0.78 or (stress >= 0.65 and social_openness <= 0.42) or (energy <= 0.20 and stress >= 0.5)):
            return _build_result(
                allowed=False,
                reason="body_state",
                fallback="wait",
                model=model,
                metadata=metadata,
                focus_match=focus_match,
            )

    return _build_result(
        allowed=True,
        reason="許可",
        model=model,
        metadata=metadata,
        focus_match=focus_match,
    )


def update_turn_taking(
    log_dir: str | None,
    person: str,
    *,
    speaker: str = "",
    kind: str = "",
    text: str = "",
    shared_focus: Any = None,
) -> dict[str, Any]:
    person_name = _clean(person)
    if not person_name:
        return default_person_model("", load_shared_focus(log_dir))

    current = get_person_model(log_dir, person_name)
    turn = dict(current["boundary"]["turn_taking"])
    speaker_norm = _compact(speaker)
    kind_norm = _compact(kind)
    text_clean = _clean(text)
    now = _now().isoformat(timespec="seconds")

    turn["last_turn_at"] = now
    turn["last_kind"] = kind_norm or speaker_norm
    turn["last_speaker"] = _clean(speaker) or _clean(kind)
    turn["last_text"] = text_clean[:180]

    is_agent = speaker_norm in {"agent", "assistant", "self", "claude", "model", "ai"}
    is_resident = speaker_norm in {"resident", "user", "human", "person", "guest"}

    if kind_norm in {"question", "request", "proposal"} and is_agent:
        turn["state"] = "awaiting_reply"
        turn["awaiting_reply"] = True
    elif kind_norm in {"answer", "reply", "response"} and is_agent:
        turn["state"] = "open"
        turn["awaiting_reply"] = False
    elif is_resident or kind_norm in {"resident", "user", "human"}:
        turn["state"] = "awaiting_reply"
        turn["awaiting_reply"] = True
    elif is_agent:
        turn["state"] = "holding"
        turn["awaiting_reply"] = False
    else:
        turn["state"] = turn.get("state") or "open"

    current["boundary"]["turn_taking"] = turn
    current["updated_at"] = now
    if shared_focus is not None:
        if isinstance(shared_focus, dict):
            current["shared_focus"] = normalize_shared_focus(shared_focus)
            if not current["shared_focus"]["updated_at"]:
                current["shared_focus"]["updated_at"] = now
            save_shared_focus(log_dir, current["shared_focus"])
        else:
            focused = _apply_shared_focus_patch(log_dir, shared_focus)
            if focused is not None:
                current["shared_focus"] = focused
    return save_person_model(log_dir, person_name, current)


def get_turn_taking_state(log_dir: str | None, person: str) -> dict[str, Any]:
    current = get_person_model(log_dir, person)
    turn = dict(current["boundary"]["turn_taking"])
    parsed = _parse_ts(turn.get("last_turn_at"))
    elapsed = None
    if parsed is not None:
        try:
            elapsed = max(0, int((_now() - parsed).total_seconds()))
        except Exception:
            elapsed = None
    turn["elapsed_seconds"] = elapsed
    return {
        "person": _clean(person),
        "turn_taking": turn,
        "quiet_window": current["boundary"]["quiet_window"],
        "consent": current["boundary"]["consent"],
        "shared_focus": current["shared_focus"],
        "updated_at": current.get("updated_at", ""),
    }

