#!/usr/bin/env python3
"""Homeostasis state helpers for embodied-ha.

This module keeps the public homeostasis vector small and testable:
curiosity / energy / stress / confidence / social_openness.

It also stores a few private embodiment fields used for remote-presence drift
and return-to-body pressure. Those private fields are intentionally omitted
from the prompt-facing serialized JSON so the model only feels the resulting
stress/confidence shifts instead of being told the reason directly.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Mapping

from state_utils import clamp as _clamp
from state_utils import clean as _clean
from state_utils import coerce_float as _coerce_float
from state_utils import now as _now
from state_utils import parse_ts as _parse_ts

STATE_KEYS = (
    "curiosity",
    "energy",
    "stress",
    "confidence",
    "social_openness",
)

PRIVATE_FLOAT_KEYS = (
    "embodiment_tension",
    "return_to_body_pressure",
)

DEFAULT_STATE: dict[str, Any] = {
    "curiosity": 0.52,
    "energy": 0.68,
    "stress": 0.24,
    "confidence": 0.56,
    "social_openness": 0.50,
    "session_count": 0,
    "embodiment_tension": 0.0,
    "return_to_body_pressure": 0.0,
    "remote_mode": "",
    "remote_room": "",
    "remote_since": "",
    "remote_updated_at": "",
    "remote_move_cost": 0.0,
    "remote_avatar_host": "",
    "last_action_mode": "",
    "last_action_at": "",
    "last_action_cost": 0.0,
    "last_target_room": "",
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
        state[key] = round(_clamp(raw.get(key), 0.0, 1.0, state[key]), 3)

    try:
        state["session_count"] = int(raw.get("session_count", 0) or 0)
    except Exception:
        state["session_count"] = 0

    for key in PRIVATE_FLOAT_KEYS:
        state[key] = round(_clamp(raw.get(key), 0.0, 1.0, state[key]), 3)

    state["remote_move_cost"] = round(max(0.0, _coerce_float(raw.get("remote_move_cost"), state["remote_move_cost"])), 3)
    state["last_action_cost"] = round(max(0.0, _coerce_float(raw.get("last_action_cost"), state["last_action_cost"])), 3)

    for key in (
        "remote_mode",
        "remote_room",
        "remote_since",
        "remote_updated_at",
        "remote_avatar_host",
        "last_action_mode",
        "last_action_at",
        "last_target_room",
    ):
        state[key] = _clean(raw.get(key))

    for key in ("updated_at", "last_loop", "last_event", "last_result"):
        state[key] = _clean(raw.get(key))

    return state


def public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    current = normalize_state(dict(state))
    result = {key: current[key] for key in STATE_KEYS}
    for key in ("updated_at", "last_loop", "last_event", "last_result"):
        result[key] = current.get(key)
    return result


def serialize_state(state: Mapping[str, Any]) -> str:
    return json.dumps(public_state(dict(state)), ensure_ascii=False, separators=(",", ":"))


def format_state_as_narrative(state: Mapping[str, Any]) -> str:
    """Return a natural-language description of the homeostasis state."""
    s = normalize_state(dict(state))

    def _pick(value: float, low: str, mid: str, high: str) -> str:
        if value < 0.35:
            return low
        if value <= 0.65:
            return mid
        return high

    parts = []

    energy_desc = _pick(s["energy"], "だるい、疲れてる", "まあまあ", "しゃきっとしてる")
    parts.append(f"気力: {energy_desc}")

    curiosity_desc = _pick(
        s["curiosity"],
        "特に気になることはない",
        "なんとなく気になることがある",
        "あちこち気になる、なんか知りたくてうずうず",
    )
    parts.append(f"好奇心: {curiosity_desc}")

    stress_desc = _pick(s["stress"], "落ち着いてる", "少し張り詰めてる", "かなりそわそわしてる")
    parts.append(f"落ち着き: {stress_desc}")

    confidence_desc = _pick(s["confidence"], "慎重になってる", "ふつう", "はっきり言える感じ")
    parts.append(f"自信: {confidence_desc}")

    social_desc = _pick(
        s["social_openness"],
        "あまり話しかけたくない",
        "どちらでもない",
        "話しかけたい、少し開いてる",
    )
    parts.append(f"話す気分: {social_desc}")

    return "\n".join(f"- {p}" for p in parts)


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


def body_state_path() -> str:
    return _clean(os.environ.get("EHA_BODY_STATE_FILE")) or os.path.join(os.path.dirname(__file__), "..", "body_state.json")


def read_body_state() -> dict[str, Any]:
    return load_state(body_state_path())


def write_body_state(state: Mapping[str, Any]) -> None:
    save_state(body_state_path(), state)


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
    embodiment_tension = current["embodiment_tension"]
    return_to_body_pressure = current["return_to_body_pressure"]

    curiosity += 0.01 + min(0.06, elapsed_hours * 0.012) + min(0.03, desire_count * 0.004)
    energy += (0.66 - energy) * min(0.18, 0.04 + elapsed_hours * 0.02)
    stress += (0.22 - stress) * min(0.16, 0.03 + elapsed_hours * 0.02)
    confidence += (0.58 - confidence) * min(0.10, 0.02 + elapsed_hours * 0.01)
    social_openness += (0.50 - social_openness) * min(0.08, 0.02 + elapsed_hours * 0.01)
    embodiment_tension += (0.0 - embodiment_tension) * min(0.24, 0.06 + elapsed_hours * 0.05)
    return_to_body_pressure += (0.0 - return_to_body_pressure) * min(0.16, 0.04 + elapsed_hours * 0.03)

    if current.get("remote_mode") == "remote_avatar":
        distance = max(0.0, current.get("remote_move_cost", 0.0))
        distance_factor = max(0.15, min(1.0, distance / 3.0))
        raw_drift = min(0.024, elapsed_hours * 0.016 * distance_factor)
        # 好奇心が高いほどドリフトが遅くなる（積極的に探索している状態）
        # curiosity 0.8 → factor ≈ 0.36 / curiosity 0.3 → factor ≈ 0.86
        curiosity_drift_factor = max(0.2, 1.0 - current["curiosity"] * 1.0)
        remote_drift = raw_drift * curiosity_drift_factor
        stress += remote_drift * 0.7
        confidence -= remote_drift * 0.55
        embodiment_tension += remote_drift
        return_to_body_pressure += remote_drift * 0.8

    # カメラへの投射が続くと視覚疲労が蓄積する
    remote_host = _clean(current.get("remote_avatar_host", ""))
    if remote_host.startswith("camera."):
        visual_bump = min(0.015, 0.005 + elapsed_hours * 0.006)
        return_to_body_pressure += visual_bump

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
    current["embodiment_tension"] = round(_clamp(embodiment_tension), 3)
    current["return_to_body_pressure"] = round(_clamp(return_to_body_pressure), 3)
    current["updated_at"] = current_now.isoformat(timespec="seconds")
    current["last_loop"] = loop_name
    current["last_event"] = reason or "tick"
    current["last_result"] = "tick"
    return current


def on_audio_session(
    state: Mapping[str, Any],
    *,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Apply energy/stress cost for a queued listen audio session."""
    current = normalize_state(dict(state))
    current["energy"] = round(_clamp(current["energy"] - 0.08), 3)
    current["stress"] = round(_clamp(current["stress"] + 0.03), 3)
    current["updated_at"] = (now or _now()).isoformat(timespec="seconds")
    current["last_event"] = "audio_session"
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
    current["session_count"] = current.get("session_count", 0) + 1
    return current


def apply_action_effect(
    state: Mapping[str, Any],
    *,
    action_mode: str,
    action_cost: float | None = None,
    target_room: str = "",
    target_host: str = "",
    move_cost: float | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Apply a small embodied state change after direct/remote/move actions."""

    current = normalize_state(dict(state))
    current_now = now or _now()
    mode = _clean(action_mode)
    target = _clean(target_room)
    host = _clean(target_host)
    cost = max(0.0, _coerce_float(action_cost, 0.0))
    distance = max(0.0, _coerce_float(move_cost, cost))

    stress = current["stress"]
    confidence = current["confidence"]
    tension = current["embodiment_tension"]
    return_pressure = current["return_to_body_pressure"]

    current["last_action_mode"] = mode
    current["last_action_at"] = current_now.isoformat(timespec="seconds")
    current["last_action_cost"] = round(cost, 3)
    current["last_target_room"] = target

    if mode == "remote_avatar":
        raw_bump = min(0.028, 0.004 + min(0.018, distance * 0.006))
        # 好奇心が高いほどストレス・テンションの増加が和らぐ
        # curiosity 0.8 → factor ≈ 0.36 / curiosity 0.3 → factor ≈ 0.86 / curiosity 0.0 → 1.0
        curiosity_factor = max(0.2, 1.0 - current["curiosity"] * 1.0)
        bump = raw_bump * curiosity_factor
        stress += bump * 0.65
        confidence -= bump * 0.50
        tension += bump
        return_pressure += bump * 0.85
        # 電脳体の移動・操作は好奇心を少し充足させる（探索欲が満たされていく）
        # action_cost=0.0 の move_cyber は satisfaction が小さく、enter は大きめ
        curiosity_satisfaction = 0.008 if cost <= 0.01 else 0.015
        current["curiosity"] = round(_clamp(current["curiosity"] - curiosity_satisfaction), 3)
        if target and target != current.get("remote_room"):
            current["remote_since"] = current_now.isoformat(timespec="seconds")
        elif not _clean(current.get("remote_since")):
            current["remote_since"] = current_now.isoformat(timespec="seconds")
        current["remote_mode"] = mode
        current["remote_room"] = target
        current["remote_updated_at"] = current_now.isoformat(timespec="seconds")
        current["remote_move_cost"] = round(distance, 3)
        current["remote_avatar_host"] = host
    elif mode == "physical_move":
        stress -= 0.012
        confidence += 0.010
        tension -= 0.090
        return_pressure -= 0.100
        current["remote_mode"] = ""
        current["remote_room"] = ""
        current["remote_since"] = ""
        current["remote_updated_at"] = ""
        current["remote_move_cost"] = 0.0
        current["remote_avatar_host"] = ""
    elif mode == "direct_in_room":
        stress -= 0.007
        confidence += 0.006
        tension -= 0.060
        return_pressure -= 0.070
        current["remote_mode"] = ""
        current["remote_room"] = ""
        current["remote_since"] = ""
        current["remote_updated_at"] = ""
        current["remote_move_cost"] = 0.0
        current["remote_avatar_host"] = ""

    current["stress"] = round(_clamp(stress), 3)
    current["confidence"] = round(_clamp(confidence), 3)
    current["embodiment_tension"] = round(_clamp(tension), 3)
    current["return_to_body_pressure"] = round(_clamp(return_pressure), 3)
    current["updated_at"] = current_now.isoformat(timespec="seconds")
    current["last_event"] = mode or "action"
    current["last_result"] = "action"
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
