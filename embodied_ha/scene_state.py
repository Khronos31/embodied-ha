#!/usr/bin/env python3
"""Lightweight scene grounding state for camera observations."""

from __future__ import annotations

import hashlib
import os
from typing import Any, Mapping

from state_utils import clean as _clean
from state_utils import now as _now
from state_utils import read_json as _read_json
from state_utils import write_json as _write_json

_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_DIR, "log"))
_SCENE_STATE_FILE = "scene_state.json"
_MAX_SCENES = 20


def scene_state_path(log_dir: str | None = None) -> str:
    return os.path.join(log_dir or _DEFAULT_LOG_DIR, _SCENE_STATE_FILE)


def _normalize_mapping(value: Any, allowed: set[str] | None = None) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    items = value.items() if allowed is None else ((key, value.get(key)) for key in allowed)
    return {str(key): _clean(item) for key, item in items if _clean(item)}


def _normalize_item_list(values: Any, *, fallback_prefix: str) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for key in ("id", "label", "location", "confidence"):
            if key == "confidence":
                try:
                    row[key] = max(0.0, min(1.0, float(item.get(key, 0.5))))
                except Exception:
                    row[key] = 0.5
                continue
            text = _clean(item.get(key))
            if text:
                row[key] = text
        if not row.get("id"):
            basis = f"{fallback_prefix}|{index}|{row.get('label','')}|{row.get('location','')}"
            row["id"] = f"{fallback_prefix}_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:8]}"
        if row.get("label") or row.get("location"):
            out.append(row)
    return out


def _load_state(log_dir: str | None = None) -> dict[str, Any]:
    data = _read_json(scene_state_path(log_dir), {})
    if not isinstance(data, dict):
        data = {}
    scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
    return {"scenes": [scene for scene in scenes if isinstance(scene, dict)][-_MAX_SCENES:]}


def _save_state(log_dir: str | None, state: Mapping[str, Any]) -> None:
    scenes = state.get("scenes") if isinstance(state.get("scenes"), list) else []
    _write_json(scene_state_path(log_dir), {"scenes": scenes[-_MAX_SCENES:]})


def ingest_scene_parse(
    source: str,
    camera_pose: dict,
    objects: list[dict],
    people: list[dict],
    changes: list[str],
    log_dir: str | None = None,
) -> str:
    timestamp = _now().isoformat(timespec="seconds")
    source = _clean(source)
    digest_basis = f"{timestamp}|{source}|{len(objects)}|{len(people)}"
    scene_id = f"scene_{hashlib.sha1(digest_basis.encode('utf-8')).hexdigest()[:12]}"
    scene = {
        "id": scene_id,
        "timestamp": timestamp,
        "source": source,
        "camera_pose": _normalize_mapping(camera_pose, {"preset", "direction", "room"}),
        "objects": _normalize_item_list(objects, fallback_prefix="obj"),
        "people": _normalize_item_list(people, fallback_prefix="person"),
        "changes": [_clean(item) for item in changes if _clean(item)] if isinstance(changes, list) else [],
    }
    state = _load_state(log_dir)
    scenes = state.get("scenes", [])
    scenes.append(scene)
    state["scenes"] = scenes[-_MAX_SCENES:]
    _save_state(log_dir, state)
    return scene_id


def _latest_scenes(log_dir: str | None = None, source: str | None = None, limit: int = 2) -> list[dict[str, Any]]:
    source = _clean(source) if source is not None else ""
    scenes = list(reversed(_load_state(log_dir).get("scenes", [])))
    if source:
        scenes = [scene for scene in scenes if _clean(scene.get("source")) == source]
    return scenes[: max(1, limit)]


def resolve_reference(phrase: str, shared_focus: Mapping[str, Any] | None = None, log_dir: str | None = None) -> dict[str, Any] | None:
    phrase_norm = _clean(phrase).lower()
    if not phrase_norm:
        return None
    scenes = _latest_scenes(log_dir, limit=3)
    if not scenes:
        return None
    focus_object_id = _clean((shared_focus or {}).get("object_id"))
    candidates: list[dict[str, Any]] = []
    for scene in scenes:
        for kind in ("objects", "people"):
            for item in scene.get(kind, []) or []:
                label = _clean(item.get("label"))
                location = _clean(item.get("location"))
                score = 0.35
                if focus_object_id and _clean(item.get("id")) == focus_object_id:
                    score += 0.45
                if label and label.lower() in phrase_norm:
                    score += 0.35
                if location and location.lower() in phrase_norm:
                    score += 0.25
                if phrase_norm in {"それ", "あれ", "これ", "さっきの", "右のやつ", "左のやつ"}:
                    score += 0.1
                candidates.append({
                    "scene_id": scene.get("id", ""),
                    "scene_source": scene.get("source", ""),
                    "last_seen_at": scene.get("timestamp", ""),
                    "kind": kind[:-1],
                    "candidate": item,
                    "score": round(min(1.0, score), 3),
                })
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item["score"], item["last_seen_at"]), reverse=True)
    return candidates[0]


def compare_recent_scenes(source: str | None = None, log_dir: str | None = None) -> dict[str, Any]:
    scenes = _latest_scenes(log_dir, source=source, limit=2)
    if len(scenes) < 2:
        return {"source": _clean(source), "status": "insufficient_scenes", "changes": []}
    current, previous = scenes[0], scenes[1]
    prev_labels = {_clean(item.get("label")) for item in previous.get("objects", []) if _clean(item.get("label"))}
    cur_labels = {_clean(item.get("label")) for item in current.get("objects", []) if _clean(item.get("label"))}
    changes = list(current.get("changes") or [])
    added = sorted(cur_labels - prev_labels)
    removed = sorted(prev_labels - cur_labels)
    if added:
        changes.append("増えたもの: " + ", ".join(added[:5]))
    if removed:
        changes.append("見えなくなったもの: " + ", ".join(removed[:5]))
    return {
        "source": _clean(source) or current.get("source", ""),
        "status": "ok",
        "current_scene_id": current.get("id", ""),
        "previous_scene_id": previous.get("id", ""),
        "changes": changes,
    }
