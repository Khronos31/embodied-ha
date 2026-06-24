#!/usr/bin/env python3
"""Homeostasis state helpers for embodied-ha.

This module keeps the homeostasis vector small and testable:
curiosity / energy / stress / confidence / social_openness.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Mapping

from state_utils import clamp as _clamp
from state_utils import clean as _clean
from state_utils import now as _now
from state_utils import parse_ts as _parse_ts

STATE_KEYS = (
    "curiosity",
    "energy",
    "stress",
    "confidence",
    "social_openness",
)

DEFAULT_STATE: dict[str, Any] = {
    "curiosity": 0.52,
    "energy": 0.68,
    "stress": 0.24,
    "confidence": 0.56,
    "social_openness": 0.50,
    "updated_at": "",
    "last_loop": "",
    "last_event": "",
    "last_result": "",
}


def normalize_state(raw: Any) -> dict[str, Any]:
    state = dict(DEFAULT_STATE)
    if not isinstance(raw, dict):
        return state

    for key in STATE_KEYS:
        state[key] = round(_clamp(raw.get(key), state[key]), 3)

    for key in ("updated_at", "last_loop", "last_event", "last_result"):
        state[key] = _clean(raw.get(key))

    return state


def serialize_state(state: Mapping[str, Any]) -> str:
    return json.dumps(normalize_state(dict(state)), ensure_ascii=False, separators=(",", ":"))


def load_state(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return normalize_state(None)
    return normalize_state(raw)


def save_state(path: str, state: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalize_state(dict(state)), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _elapsed_hours(state: Mapping[str, Any], now: _dt.datetime) -> float:
    parsed = _parse_ts(state.get("updated_at"))
    if parsed is None:
        return 0.0
    try:
        return max(0.0, (now - parsed).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def advance_tick(
    state: Mapping[str, Any],
    *,
    loop_name: str,
    trigger_reason: str = "",
    active_desires: list[str] | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Update the body state for a scheduler tick.

    This is a small drift model:
    - curiosity slowly rises when time passes or desires are active
    - energy and stress relax toward a baseline
    - unexpected triggers slightly raise curiosity and stress
    """

    current = normalize_state(dict(state))
    current_now = now or _now()
    elapsed_hours = _elapsed_hours(current, current_now)
    desire_count = len(active_desires or [])
    reason = _clean(trigger_reason)

    curiosity = current["curiosity"]
    energy = current["energy"]
    stress = current["stress"]
    confidence = current["confidence"]
    social_openness = current["social_openness"]

    curiosity += 0.01 + min(0.06, elapsed_hours * 0.012) + min(0.03, desire_count * 0.004)
    energy += (0.66 - energy) * min(0.18, 0.04 + elapsed_hours * 0.02)
    stress += (0.22 - stress) * min(0.16, 0.03 + elapsed_hours * 0.02)
    confidence += (0.58 - confidence) * min(0.10, 0.02 + elapsed_hours * 0.01)
    social_openness += (0.50 - social_openness) * min(0.08, 0.02 + elapsed_hours * 0.01)

    if reason and reason not in ("定期実行", "手動実行"):
        curiosity += 0.015
        stress += 0.010

    if loop_name == "explore":
        curiosity += 0.015
    elif loop_name == "watch":
        stress += 0.004
    elif loop_name == "chat":
        social_openness += 0.012
        confidence += 0.006

    current["curiosity"] = round(_clamp(curiosity), 3)
    current["energy"] = round(_clamp(energy), 3)
    current["stress"] = round(_clamp(stress), 3)
    current["confidence"] = round(_clamp(confidence), 3)
    current["social_openness"] = round(_clamp(social_openness), 3)
    current["updated_at"] = current_now.isoformat(timespec="seconds")
    current["last_loop"] = loop_name
    current["last_event"] = reason or "tick"
    current["last_result"] = "tick"
    return current


def apply_feedback(
    state: Mapping[str, Any],
    *,
    loop_name: str,
    success: bool,
    duration_seconds: float | None = None,
    spoke: bool = False,
    action_taken: bool = False,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Update the body state after a loop completes."""

    current = normalize_state(dict(state))
    current_now = now or _now()
    duration = max(0.0, float(duration_seconds or 0.0))

    energy_cost = 0.018 + min(0.080, duration / 1800.0 * 0.045)
    current["energy"] = round(_clamp(current["energy"] - energy_cost), 3)

    if loop_name == "watch":
        current["curiosity"] = round(
            _clamp(current["curiosity"] - (0.040 if success else -0.010)),
            3,
        )
        current["stress"] = round(
            _clamp(current["stress"] + (-0.012 if success else 0.080)),
            3,
        )
        current["confidence"] = round(
            _clamp(current["confidence"] + (0.020 if success else -0.050)),
            3,
        )
    elif loop_name == "explore":
        current["curiosity"] = round(
            _clamp(current["curiosity"] - (0.060 if success else -0.015)),
            3,
        )
        current["stress"] = round(
            _clamp(current["stress"] + (-0.010 if success else 0.080)),
            3,
        )
        current["confidence"] = round(
            _clamp(current["confidence"] + (0.030 if success else -0.050)),
            3,
        )
    elif loop_name == "chat":
        current["social_openness"] = round(
            _clamp(current["social_openness"] + (0.040 if success else -0.020)),
            3,
        )
        current["confidence"] = round(
            _clamp(current["confidence"] + (0.020 if success else -0.030)),
            3,
        )
        current["curiosity"] = round(_clamp(current["curiosity"] + 0.010), 3)

    if spoke:
        current["social_openness"] = round(_clamp(current["social_openness"] + 0.010), 3)
    if action_taken:
        current["confidence"] = round(
            _clamp(current["confidence"] + (0.015 if success else -0.015)),
            3,
        )
        current["stress"] = round(
            _clamp(current["stress"] + (0.010 if not success else 0.0)),
            3,
        )

    current["stress"] = round(
        _clamp(current["stress"] + (-0.006 if success else 0.020)),
        3,
    )
    current["updated_at"] = current_now.isoformat(timespec="seconds")
    current["last_loop"] = loop_name
    current["last_event"] = "success" if success else "failure"
    current["last_result"] = "success" if success else "failure"
    return current


def compute_run_chance(base_chance: int, state: Mapping[str, Any], loop_name: str) -> int:
    """Bias the scheduler by the current body state."""

    current = normalize_state(dict(state))
    chance = int(base_chance)

    curiosity = current["curiosity"]
    energy = current["energy"]
    stress = current["stress"]
    confidence = current["confidence"]
    social_openness = current["social_openness"]

    if loop_name == "watch":
        chance += round((curiosity - 0.5) * 26)
        chance += round((confidence - 0.5) * 8)
        chance += round((energy - 0.5) * 10)
        chance -= round(max(0.0, stress - 0.35) * 24)
    elif loop_name == "explore":
        chance += round((curiosity - 0.5) * 34)
        chance += round((energy - 0.55) * 16)
        chance += round((confidence - 0.5) * 6)
        chance -= round(max(0.0, stress - 0.30) * 30)
    else:
        chance += round((social_openness - 0.5) * 12)

    if energy < 0.30:
        chance -= 15
    if stress > 0.70:
        chance -= 12
    if curiosity > 0.75:
        chance += 8

    return max(5, min(100, chance))


def format_log_line(label: str, state: Mapping[str, Any], **fields: Any) -> str:
    """Return a compact, human-readable log line."""

    current = normalize_state(dict(state))
    parts = [
        f"curiosity={current['curiosity']:.3f}",
        f"energy={current['energy']:.3f}",
        f"stress={current['stress']:.3f}",
        f"confidence={current['confidence']:.3f}",
        f"social_openness={current['social_openness']:.3f}",
    ]
    for key, value in fields.items():
        if value is None:
            continue
        text = _clean(value)
        if text:
            parts.append(f"{key}={text}")
    return "[body_state] " + " ".join([_clean(label)] + parts)
