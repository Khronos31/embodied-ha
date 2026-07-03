#!/usr/bin/env python3
"""Stateful desire helpers for embodied-ha.

`desires.json` remains the editable desire catalog.
`desire_state.json` stores the runtime state for each desire.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import uuid
from typing import Any, Mapping

from state_utils import clamp as _clamp
from state_utils import clean as _clean
from state_utils import coerce_float as _coerce_float
from state_utils import now as _now
from state_utils import parse_ts as _parse_ts
from state_utils import read_json as _read_json

STATE_VERSION = 1
ACTIVATION_THRESHOLD = 0.6
SATISFIED_DORMANT_THRESHOLD = 0.35
ACTIVE_DORMANT_THRESHOLD = 0.42
RECENT_ACTION_WINDOW_SECONDS = 20 * 60

VALID_STATES = {"active", "satisfied", "dormant", "suppressed"}
RUNTIME_RECORD_KEYS = {
    "state",
    "priority",
    "satisfaction",
    "charge",
    "last_triggered_at",
    "last_satisfied_at",
    "last_decay_at",
    "suppressed_until",
    "updated_at",
}
CATALOG_KEYS = {"prompt", "growth_rate", "priority", "tags"}
EXPLORATION_HINTS = (
    "check_",
    "check ",
    "curious",
    "explore",
    "observe",
    "weather",
    "temp",
    "temperature",
    "humidity",
    "power",
    "resident",
    "camera",
    "sensor",
    "memory",
)

DEFAULT_STATE: dict[str, Any] = {
    "version": STATE_VERSION,
    "updated_at": "",
    "last_tick": "",
    "last_loop": "",
    "last_reason": "",
    "last_result": "",
    "desires": {},
}

DEFAULT_RECORD: dict[str, Any] = {
    "state": "dormant",
    "priority": 1.0,
    "satisfaction": 0.0,
    "charge": 0.0,
    "last_triggered_at": "",
    "last_satisfied_at": "",
    "last_decay_at": "",
    "suppressed_until": "",
    "updated_at": "",
}


def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _default_catalog_path() -> str:
    return os.path.join(_script_dir(), "desires.json")


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("，", ",").split(",")]
        return [item for item in items if item]
    if isinstance(value, list):
        items = [_clean(item) for item in value]
        return [item for item in items if item]
    return []


def _is_exploration_desire(name: str, cfg: Mapping[str, Any] | None = None) -> bool:
    text = f"{name} {_clean((cfg or {}).get('prompt'))} {' '.join(_normalize_tags((cfg or {}).get('tags')))}".lower()
    return any(hint in text for hint in EXPLORATION_HINTS)


def _has_tag(cfg: Mapping[str, Any] | None, tag: str) -> bool:
    needle = _clean(tag).lower()
    if not needle:
        return False
    return needle in {item.lower() for item in _normalize_tags((cfg or {}).get("tags"))}


def _curiosity(body_state: Mapping[str, Any] | None) -> float | None:
    if not isinstance(body_state, Mapping):
        return None
    try:
        return float(body_state.get("curiosity", 0.0))
    except Exception:
        return None




def _recent_action_matches(
    body_state: Mapping[str, Any] | None,
    *,
    mode: str,
    now: _dt.datetime | None = None,
    within_seconds: int = RECENT_ACTION_WINDOW_SECONDS,
) -> bool:
    if not isinstance(body_state, Mapping):
        return False
    if _clean(body_state.get("last_action_mode")) != _clean(mode):
        return False
    ts = _parse_ts(body_state.get("last_action_at"))
    if ts is None:
        return False
    current_now = now or _now()
    try:
        delta = (current_now - ts).total_seconds()
    except Exception:
        return False
    return 0.0 <= delta <= max(0, within_seconds)

def _body_scalar(body_state: Mapping[str, Any] | None, key: str, default: float = 0.5) -> float:
    if not isinstance(body_state, Mapping):
        return default
    try:
        return float(body_state.get(key, default))
    except Exception:
        return default


def _normalize_state_name(value: Any) -> str:
    text = _clean(value).lower()
    if text in VALID_STATES:
        return text
    if text in {"satisfied", "fulfilled", "satiated"}:
        return "satisfied"
    if text in {"sleeping", "idle", "resting"}:
        return "dormant"
    if text in {"blocked", "hold", "paused"}:
        return "suppressed"
    return "dormant"


def normalize_desires(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize a desire catalog while preserving user-defined extras."""

    if isinstance(raw, dict) and isinstance(raw.get("desires"), dict):
        raw = raw["desires"]

    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for raw_name, raw_cfg in raw.items():
        name = _clean(raw_name)
        if not name:
            continue

        cfg = raw_cfg if isinstance(raw_cfg, Mapping) else {"prompt": raw_cfg}
        prompt = _clean(cfg.get("prompt"))
        if not prompt:
            continue

        growth_rate = round(max(0.0, _coerce_float(cfg.get("growth_rate"), 0.0)), 3)
        priority = round(max(0.0, _coerce_float(cfg.get("priority"), 1.0)), 3)
        record: dict[str, Any] = {
            "prompt": prompt,
            "growth_rate": growth_rate,
            "priority": priority,
        }
        tags = _normalize_tags(cfg.get("tags"))
        if tags:
            record["tags"] = tags

        for key, value in cfg.items():
            if key in record or key in RUNTIME_RECORD_KEYS:
                continue
            if key in CATALOG_KEYS:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                record[key] = value
            elif isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
                record[key] = value

        normalized[name] = record

    return normalized


def load_catalog(path: str) -> dict[str, dict[str, Any]]:
    raw = _read_json(path)
    if raw is None and path != _default_catalog_path():
        raw = _read_json(_default_catalog_path())
    return normalize_desires(raw)


def save_catalog(path: str, catalog: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalize_desires(dict(catalog)), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


load_desires = load_catalog
save_desires = save_catalog


def _normalize_record(
    name: str,
    raw: Any,
    catalog_cfg: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = dict(DEFAULT_RECORD)
    cfg = catalog_cfg if isinstance(catalog_cfg, Mapping) else {}
    record["priority"] = round(max(0.0, _coerce_float(cfg.get("priority"), 1.0)), 3)

    if isinstance(raw, Mapping):
        record["state"] = _normalize_state_name(raw.get("state"))
        record["priority"] = round(max(0.0, _coerce_float(raw.get("priority"), record["priority"])), 3)
        record["satisfaction"] = round(_clamp(raw.get("satisfaction"), record["satisfaction"]), 3)
        record["charge"] = round(_clamp(raw.get("charge", raw.get("intensity")), record["charge"]), 3)
        for key in ("last_triggered_at", "last_satisfied_at", "last_decay_at", "suppressed_until", "updated_at"):
            record[key] = _clean(raw.get(key))
    elif isinstance(raw, (int, float)):
        record["charge"] = round(_clamp(raw), 3)
        record["state"] = "active" if record["charge"] >= ACTIVATION_THRESHOLD else "dormant"
    elif isinstance(raw, str):
        maybe_state = _normalize_state_name(raw)
        record["state"] = maybe_state
        if maybe_state == "active":
            record["charge"] = ACTIVATION_THRESHOLD
    elif raw is None:
        pass

    if record["state"] == "active" and record["charge"] < ACTIVATION_THRESHOLD:
        record["charge"] = ACTIVATION_THRESHOLD
    if record["state"] == "satisfied" and record["satisfaction"] <= 0.0:
        record["satisfaction"] = 0.7
    if record["state"] == "suppressed" and not record["suppressed_until"]:
        record["suppressed_until"] = _now().isoformat(timespec="seconds")

    return record


def normalize_state(raw: Any, catalog: Mapping[str, Any] | None = None) -> dict[str, Any]:
    state = dict(DEFAULT_STATE)
    catalog = normalize_desires(catalog or {})

    if isinstance(raw, Mapping) and isinstance(raw.get("desires"), Mapping):
        state["version"] = int(_coerce_float(raw.get("version"), STATE_VERSION))
        state["updated_at"] = _clean(raw.get("updated_at"))
        state["last_tick"] = _clean(raw.get("last_tick"))
        state["last_loop"] = _clean(raw.get("last_loop"))
        state["last_reason"] = _clean(raw.get("last_reason"))
        state["last_result"] = _clean(raw.get("last_result"))
        raw_desires = raw.get("desires", {})
    elif isinstance(raw, Mapping):
        raw_desires = raw
    else:
        raw_desires = {}

    if isinstance(raw_desires, Mapping):
        names = list(catalog.keys())
        for key in raw_desires.keys():
            name = _clean(key)
            if name and name not in names:
                names.append(name)
        state["desires"] = {
            name: _normalize_record(name, raw_desires.get(name), catalog.get(name))
            for name in names
        }
    else:
        state["desires"] = {
            name: _normalize_record(name, None, cfg) for name, cfg in catalog.items()
        }

    return state


def serialize_state(state: Mapping[str, Any], catalog: Mapping[str, Any] | None = None) -> str:
    return json.dumps(normalize_state(dict(state), catalog), ensure_ascii=False, separators=(",", ":"))


def load_state(path: str, catalog: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return normalize_state(_read_json(path), catalog)


def save_state(path: str, state: Mapping[str, Any], catalog: Mapping[str, Any] | None = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalize_state(dict(state), catalog), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _state_and_record(
    store: Mapping[str, Any],
    name: str,
    catalog: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = normalize_state(store, catalog)
    record = current["desires"].get(name)
    if record is None:
        record = _normalize_record(name, None, (catalog or {}).get(name))
        current["desires"][name] = record
    return current, record


def stimulate(
    store: Mapping[str, Any],
    name: str,
    *,
    strength: float = 1.0,
    body_state: Mapping[str, Any] | None = None,
    catalog: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    current, record = _state_and_record(store, name, catalog)
    cfg = (normalize_desires(catalog or {})).get(name, {})
    ts = (now or _now()).isoformat(timespec="seconds")
    growth_rate = max(0.0, _coerce_float(cfg.get("growth_rate"), 0.0))
    strength = max(0.0, _coerce_float(strength, 1.0))
    current["updated_at"] = ts
    current["last_tick"] = ts
    current["last_loop"] = current.get("last_loop", "")
    current["last_reason"] = f"stimulate:{name}"
    current["last_result"] = "stimulated"

    if record["state"] == "suppressed" and not force:
        record["charge"] = round(_clamp(record["charge"] + max(0.02, growth_rate * strength * 0.2)), 3)
        record["updated_at"] = ts
        current["desires"][name] = record
        return current

    boost = max(ACTIVATION_THRESHOLD, growth_rate * 12.0 * max(0.5, strength))
    if _is_exploration_desire(name, cfg):
        curiosity = _curiosity(body_state)
        if curiosity is not None:
            boost += max(0.0, curiosity - 0.5) * 0.15

    record["charge"] = round(_clamp(max(record["charge"], ACTIVATION_THRESHOLD) + boost), 3)
    record["state"] = "active"
    record["satisfaction"] = round(min(record["satisfaction"], 0.1), 3)
    record["last_triggered_at"] = ts
    record["updated_at"] = ts
    current["desires"][name] = record
    return current


def satisfy(
    store: Mapping[str, Any],
    name: str,
    *,
    amount: float = 1.0,
    catalog: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    current, record = _state_and_record(store, name, catalog)
    ts = (now or _now()).isoformat(timespec="seconds")
    amount = max(0.0, _coerce_float(amount, 1.0))

    record["state"] = "satisfied"
    record["satisfaction"] = round(_clamp(max(record["satisfaction"], 0.55) + amount * 0.25), 3)
    record["charge"] = round(_clamp(min(record["charge"], 0.40)), 3)
    record["last_satisfied_at"] = ts
    record["updated_at"] = ts
    current["updated_at"] = ts
    current["last_tick"] = ts
    current["last_reason"] = f"satisfy:{name}"
    current["last_result"] = "satisfied"
    current["desires"][name] = record
    return current


def decay_tick(
    store: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
    body_state: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
    loop_name: str = "",
    trigger_reason: str = "",
) -> dict[str, Any]:
    current = normalize_state(store, catalog)
    catalog = normalize_desires(catalog or {})
    current_now = now or _now()
    ts = current_now.isoformat(timespec="seconds")

    previous = _parse_ts(current.get("updated_at"))
    elapsed_hours = 0.0
    if previous is not None:
        try:
            elapsed_hours = max(0.0, (current_now - previous).total_seconds() / 3600.0)
        except Exception:
            elapsed_hours = 0.0
    tick_factor = max(1.0, elapsed_hours / 0.333) if elapsed_hours > 0 else 1.0

    curiosity = _curiosity(body_state)
    stress = _body_scalar(body_state, "stress", 0.25)
    energy = _body_scalar(body_state, "energy", 0.65)
    return_to_body_pressure = _body_scalar(body_state, "return_to_body_pressure", 0.0)
    remote_mode = _clean((body_state or {}).get("remote_mode")) if isinstance(body_state, Mapping) else ""

    for name, record in current["desires"].items():
        cfg = catalog.get(name, {})
        growth_rate = max(0.0, _coerce_float(cfg.get("growth_rate"), 0.0))
        is_exploration = _is_exploration_desire(name, cfg)
        is_return = _has_tag(cfg, "return_to_body")
        is_stretch = _has_tag(cfg, "stretch")
        is_remote_roam = _has_tag(cfg, "remote_wander")
        is_camera_view = _has_tag(cfg, "camera")

        suppressed_until = _parse_ts(record.get("suppressed_until"))
        if record["state"] == "suppressed" and suppressed_until is not None and current_now < suppressed_until:
            record["charge"] = round(_clamp(record["charge"] - min(0.08, 0.02 * tick_factor)), 3)
            record["updated_at"] = ts
            record["last_decay_at"] = ts
            continue
        if record["state"] == "suppressed" and (suppressed_until is None or current_now >= suppressed_until):
            record["state"] = "dormant"
            record["suppressed_until"] = ""

        growth = growth_rate * tick_factor
        growth -= min(0.06, 0.01 * tick_factor)
        if curiosity is not None and is_exploration:
            if curiosity >= 0.55:
                growth += (curiosity - 0.55) * 0.30
        elif curiosity is not None and curiosity >= 0.8:
            growth += (curiosity - 0.8) * 0.05

        if is_return:
            growth += return_to_body_pressure * 0.75
            if remote_mode == "remote_avatar":
                growth += 0.05 * tick_factor
            elif return_to_body_pressure <= 0.08 and record["state"] in {"active", "dormant"}:
                record["state"] = "satisfied"
                record["satisfaction"] = round(max(record["satisfaction"], 0.72), 3)
                record["charge"] = round(min(record["charge"], 0.24), 3)

        if is_stretch:
            if _recent_action_matches(body_state, mode="physical_move", now=current_now):
                record["state"] = "satisfied"
                record["satisfaction"] = round(max(record["satisfaction"], 0.68), 3)
                record["charge"] = round(min(record["charge"], 0.22), 3)
            elif remote_mode == "remote_avatar":
                growth -= 0.08
            else:
                if energy >= 0.45:
                    growth += min(0.05, 0.012 * tick_factor + (energy - 0.45) * 0.08)
                if stress <= 0.55:
                    growth += max(0.0, 0.55 - stress) * 0.05
                if curiosity is not None and curiosity <= 0.45:
                    growth += max(0.0, 0.45 - curiosity) * 0.04

        if is_camera_view:
            remote_host = _clean((body_state or {}).get("remote_avatar_host", ""))
            if remote_host.startswith("camera."):
                growth += 0.06 * tick_factor
            elif record["state"] in {"active", "dormant"} and record["charge"] > 0.1:
                record["state"] = "satisfied"
                record["satisfaction"] = round(max(record["satisfaction"], 0.80), 3)
                record["charge"] = round(min(record["charge"], 0.30), 3)

        if is_remote_roam:
            if remote_mode == "remote_avatar" or _recent_action_matches(body_state, mode="remote_avatar", now=current_now):
                record["state"] = "satisfied"
                record["satisfaction"] = round(max(record["satisfaction"], 0.66), 3)
                record["charge"] = round(min(record["charge"], 0.20), 3)
            else:
                if curiosity is not None and curiosity >= 0.58:
                    growth += max(0.0, curiosity - 0.58) * 0.24
                if return_to_body_pressure <= 0.28:
                    growth += max(0.0, 0.28 - return_to_body_pressure) * 0.08
                if energy <= 0.30:
                    growth -= 0.02
                if stress >= 0.6:
                    growth -= min(0.05, (stress - 0.6) * 0.14)

        if stress >= 0.7:
            growth -= min(0.06, (stress - 0.7) * 0.18)
        if energy <= 0.35:
            growth -= min(0.04, (0.35 - energy) * 0.12)

        if record["state"] == "satisfied":
            satisfaction = max(0.0, record["satisfaction"] - (0.10 * tick_factor))
            record["satisfaction"] = round(satisfaction, 3)
            record["charge"] = round(_clamp(record["charge"] + growth - min(0.05, 0.02 * tick_factor)), 3)
            if satisfaction <= SATISFIED_DORMANT_THRESHOLD:
                record["state"] = "dormant"
                record["charge"] = round(min(record["charge"], 0.40), 3)
        else:
            charge = record["charge"] + growth
            if record["state"] == "active":
                charge -= min(0.03, 0.008 * tick_factor)
            else:
                charge -= min(0.02, 0.006 * tick_factor)
            record["charge"] = round(_clamp(charge), 3)

            if record["state"] == "active" and record["charge"] < ACTIVE_DORMANT_THRESHOLD:
                record["state"] = "dormant"

            if record["state"] == "dormant":
                activation_floor = ACTIVATION_THRESHOLD - (0.08 if is_exploration and curiosity is not None and curiosity >= 0.8 else 0.0)
                if record["charge"] >= activation_floor:
                    record["state"] = "active"
                    record["last_triggered_at"] = ts
                    record["satisfaction"] = round(min(record["satisfaction"], 0.1), 3)

        record["last_decay_at"] = ts
        record["updated_at"] = ts
        current["desires"][name] = record

    current["version"] = STATE_VERSION
    current["updated_at"] = ts
    current["last_tick"] = ts
    current["last_loop"] = _clean(loop_name)
    current["last_reason"] = _clean(trigger_reason)
    current["last_result"] = "tick"
    return current


def active_desire_items(
    store: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    current = normalize_state(store, catalog)
    catalog = normalize_desires(catalog or {})
    items: list[tuple[str, str, dict[str, Any]]] = []
    for name, record in current["desires"].items():
        if record.get("state") != "active":
            continue
        prompt = _clean(catalog.get(name, {}).get("prompt")) or name
        items.append((name, prompt, record))
    items.sort(key=lambda item: (-_coerce_float(item[2].get("priority"), 0.0), -_coerce_float(item[2].get("charge"), 0.0), item[0]))
    return items


def active_desire_names(
    store: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
) -> list[str]:
    return [name for name, _, _ in active_desire_items(store, catalog)]


def active_desire_prompts(
    store: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
) -> list[str]:
    return [prompt for _, prompt, _ in active_desire_items(store, catalog)]


def consume_active_desires(
    store: Mapping[str, Any],
    names: list[str],
    *,
    catalog: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    current = normalize_state(store, catalog)
    ts = now or _now()
    for name in names:
        current = satisfy(current, name, amount=1.0, catalog=catalog, now=ts)
    current["updated_at"] = ts.isoformat(timespec="seconds")
    current["last_tick"] = current["updated_at"]
    current["last_result"] = "consumed"
    return current


def compute_pressure(
    store: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
    body_state: Mapping[str, Any] | None = None,
) -> float:
    current = normalize_state(store, catalog)
    catalog = normalize_desires(catalog or {})
    if not current["desires"]:
        return 0.0

    curiosity = _curiosity(body_state)
    return_to_body_pressure = _body_scalar(body_state, "return_to_body_pressure", 0.0)
    remote_mode = _clean((body_state or {}).get("remote_mode")) if isinstance(body_state, Mapping) else ""
    total = 0.0
    count = 0
    for name, record in current["desires"].items():
        cfg = catalog.get(name, {})
        score = 0.0
        charge = _clamp(record.get("charge"))
        score += charge * 0.65
        score += min(0.20, _coerce_float(record.get("priority"), 1.0) * 0.08)
        if record.get("state") == "active":
            score += 0.25
        elif record.get("state") == "satisfied":
            score += _clamp(record.get("satisfaction")) * 0.10
        elif record.get("state") == "suppressed":
            score *= 0.25

        if curiosity is not None and _is_exploration_desire(name, cfg):
            score += max(0.0, curiosity - 0.55) * 0.22
        if _has_tag(cfg, "return_to_body"):
            score += return_to_body_pressure * 0.45
        if _has_tag(cfg, "stretch") and remote_mode != "remote_avatar":
            score += max(0.0, 0.58 - return_to_body_pressure) * 0.10
        if _has_tag(cfg, "remote_wander") and remote_mode != "remote_avatar":
            score += max(0.0, (curiosity or 0.0) - 0.58) * 0.10

        total += score
        count += 1

    if count == 0:
        return 0.0
    return round(_clamp(total / count), 3)


def summarize_state(
    store: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
    body_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = normalize_state(store, catalog)
    counts = {"active": 0, "satisfied": 0, "dormant": 0, "suppressed": 0}
    for record in current["desires"].values():
        state = record.get("state", "dormant")
        if state not in counts:
            state = "dormant"
        counts[state] += 1
    return {
        "pressure": compute_pressure(current, catalog=catalog, body_state=body_state),
        "active": counts["active"],
        "satisfied": counts["satisfied"],
        "dormant": counts["dormant"],
        "suppressed": counts["suppressed"],
        "total": sum(counts.values()),
    }


def format_log_line(
    label: str,
    store: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
    body_state: Mapping[str, Any] | None = None,
    **fields: Any,
) -> str:
    current = normalize_state(store, catalog)
    summary = summarize_state(current, catalog=catalog, body_state=body_state)
    parts = [
        f"active={summary['active']}",
        f"satisfied={summary['satisfied']}",
        f"dormant={summary['dormant']}",
        f"suppressed={summary['suppressed']}",
        f"pressure={summary['pressure']:.3f}",
    ]
    for key, value in fields.items():
        if value is None:
            continue
        text = _clean(value)
        if text:
            parts.append(f"{key}={text}")
    return "[desire_state] " + " ".join([_clean(label)] + parts)


def load_desires_or_default(path: str) -> dict[str, dict[str, Any]]:
    catalog = load_catalog(path)
    return catalog


def save_desires_normalized(path: str, desires: Mapping[str, Any]) -> None:
    save_catalog(path, desires)


def seed_catalog(source_path: str, destination_path: str) -> dict[str, dict[str, Any]]:
    catalog = load_catalog(source_path)
    save_catalog(destination_path, catalog)
    return catalog
