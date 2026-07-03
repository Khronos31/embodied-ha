#!/usr/bin/env python3
"""Stateful anomaly helpers for embodied-ha.

This module keeps a small persistent anomaly state so loop.sh can record
sensor spikes / unresolved loops / world-model mismatches and the daemon can
turn them into explore urgency.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import uuid
from typing import Any, Mapping

from state_utils import clamp as _clamp
from state_utils import clean as _clean
from state_utils import coerce_float as _coerce_float
from state_utils import now as _now
from state_utils import parse_ts as _parse_ts

STATE_VERSION = 1
ANOMALY_TYPES = ("sensor_spike", "unresolved_loop", "world_model_mismatch")

DEFAULT_RECORD: dict[str, Any] = {
    "type": "",
    "severity": 0.0,
    "detected_at": "",
    "last_seen_at": "",
    "resolved": True,
    "resolved_at": "",
    "trigger_explore": False,
    "fingerprint": "",
    "count": 0,
    "summary": "",
    "details": [],
}

DEFAULT_STATE: dict[str, Any] = {
    "version": STATE_VERSION,
    "updated_at": "",
    "last_detected_at": "",
    "last_resolved_at": "",
    "last_trigger_reason": "",
    "last_loop": "",
    "last_sensor_text": "",
    "last_open_loops_text": "",
    "last_sensor_snapshot": {},
    "last_open_loops": [],
    "anomalies": {},
}

_POSITIVE_CUES = (
    " on",
    "on ",
    " open",
    "open ",
    "open",
    "開いて",
    "ついて",
    "点灯",
    "起動",
    "稼働",
    "正常",
    "在宅",
    "明るい",
    "warm",
    "hot",
    "detected",
    "occupied",
    "home",
    "true",
    "yes",
)
_NEGATIVE_CUES = (
    " off",
    "off ",
    " closed",
    "closed ",
    "closed",
    "閉じ",
    "消えて",
    "消灯",
    "停止",
    "止ま",
    "不在",
    "暗い",
    "cold",
    "away",
    "idle",
    "unoccupied",
    "false",
    "no",
)


def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _default_state_path() -> str:
    return os.path.join(os.environ.get("EHA_LOG_DIR", os.path.join(_script_dir(), "log")), "anomaly_state.json")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = _clean(value).lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _numbers_from_text(text: str) -> list[float]:
    numbers: list[float] = []
    for match in re.finditer(r"-?\d+(?:\.\d+)?", text):
        try:
            numbers.append(float(match.group(0)))
        except Exception:
            continue
    return numbers


def _signal_from_text(text: str) -> int | None:
    lowered = text.lower()
    if any(cue in lowered for cue in _POSITIVE_CUES):
        return 1
    if any(cue in lowered for cue in _NEGATIVE_CUES):
        return 0
    return None


def _snapshot_entry(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and ("raw" in value or "text" in value or "numbers" in value or "signal" in value):
        raw = _clean(value.get("raw", value.get("text", "")))
        numbers = value.get("numbers")
        if not isinstance(numbers, list):
            numbers = _numbers_from_text(raw)
        else:
            coerced: list[float] = []
            for item in numbers:
                try:
                    coerced.append(float(item))
                except Exception:
                    continue
            numbers = coerced
        signal = value.get("signal")
        if signal not in (0, 1, None):
            signal = _signal_from_text(raw)
        return {
            "raw": raw,
            "numbers": numbers,
            "signal": signal,
        }

    raw = _clean(value)
    return {
        "raw": raw,
        "numbers": _numbers_from_text(raw),
        "signal": _signal_from_text(raw),
    }


def _snapshot_from_sensor_logs(sensor_logs: Any) -> dict[str, dict[str, Any]]:
    if sensor_logs is None:
        return {}
    if isinstance(sensor_logs, Mapping):
        snapshot: dict[str, dict[str, Any]] = {}
        for key, value in sensor_logs.items():
            label = _clean(key)
            if not label:
                continue
            snapshot[label] = _snapshot_entry(value)
        return snapshot

    if isinstance(sensor_logs, list):
        snapshot = {}
        for item in sensor_logs:
            if isinstance(item, Mapping):
                label = _clean(item.get("label") or item.get("name") or item.get("key") or item.get("entity") or item.get("title"))
                if not label:
                    continue
                snapshot[label] = _snapshot_entry(item.get("value", item.get("raw", item.get("text", item))))
        return snapshot

    text = _clean(sensor_logs)
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, Mapping):
            return _snapshot_from_sensor_logs(parsed)
        if isinstance(parsed, list):
            return _snapshot_from_sensor_logs(parsed)

    snapshot = {}
    for raw_line in str(sensor_logs).splitlines():
        line = _clean(raw_line)
        if not line:
            continue
        if line.startswith("---"):
            continue
        if ":" in line:
            left, right = line.split(":", 1)
        elif "=" in line:
            left, right = line.split("=", 1)
        else:
            continue
        label = _clean(left).strip("【】[]")
        value = _clean(right)
        if not label or not value:
            continue
        snapshot[label] = _snapshot_entry(value)
    return snapshot


def _loops_from_input(open_loops: Any) -> list[dict[str, Any]]:
    if open_loops is None:
        return []
    if isinstance(open_loops, list):
        loops: list[dict[str, Any]] = []
        for item in open_loops:
            if isinstance(item, Mapping):
                loops.append(
                    {
                        "id": _clean(item.get("id")),
                        "source": _clean(item.get("source")),
                        "text": _clean(item.get("text")),
                        "created": _clean(item.get("created")),
                    }
                )
            else:
                text = _clean(item)
                if text:
                    loops.append({"id": "", "source": "", "text": text, "created": ""})
        return loops

    text = _clean(open_loops)
    if not text or text in {"なし", "[]"}:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return _loops_from_input(parsed)
        if isinstance(parsed, Mapping):
            return _loops_from_input([parsed])
    loops = []
    for line in text.splitlines():
        cleaned = _clean(line)
        if cleaned:
            loops.append({"id": "", "source": "", "text": cleaned, "created": ""})
    return loops


def _stable_text(snapshot: Mapping[str, Any] | None) -> str:
    if not snapshot:
        return ""
    try:
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    except Exception:
        return _clean(snapshot)


def _normalize_details(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    text = _clean(value)
    return [text] if text else []


def _normalize_anomaly_record(type_name: str, raw: Any) -> dict[str, Any]:
    record = dict(DEFAULT_RECORD)
    record["type"] = type_name

    if isinstance(raw, Mapping):
        record["type"] = _clean(raw.get("type")) or type_name
        record["severity"] = round(_clamp(raw.get("severity"), 0.0, 1.0), 3)
        record["detected_at"] = _clean(raw.get("detected_at"))
        record["last_seen_at"] = _clean(raw.get("last_seen_at")) or record["detected_at"]
        record["resolved"] = _as_bool(raw.get("resolved"), True)
        record["resolved_at"] = _clean(raw.get("resolved_at"))
        record["trigger_explore"] = _as_bool(raw.get("trigger_explore"), False)
        record["fingerprint"] = _clean(raw.get("fingerprint"))
        record["count"] = max(0, int(_coerce_float(raw.get("count"), 0.0)))
        record["summary"] = _clean(raw.get("summary"))
        record["details"] = _normalize_details(raw.get("details"))
    return record


def normalize_state(raw: Any) -> dict[str, Any]:
    state = dict(DEFAULT_STATE)
    anomalies: dict[str, dict[str, Any]] = {}

    if isinstance(raw, Mapping):
        state["version"] = int(_coerce_float(raw.get("version"), STATE_VERSION))
        for key in (
            "updated_at",
            "last_detected_at",
            "last_resolved_at",
            "last_trigger_reason",
            "last_loop",
            "last_sensor_text",
            "last_open_loops_text",
        ):
            state[key] = _clean(raw.get(key))

        sensor_snapshot = raw.get("last_sensor_snapshot", {})
        if isinstance(sensor_snapshot, Mapping):
            state["last_sensor_snapshot"] = {
                _clean(key): _snapshot_entry(value)
                for key, value in sensor_snapshot.items()
                if _clean(key)
            }

        loops = raw.get("last_open_loops", [])
        if isinstance(loops, list):
            state["last_open_loops"] = _loops_from_input(loops)

        raw_anomalies = raw.get("anomalies", {})
        if isinstance(raw_anomalies, Mapping):
            keys = list(ANOMALY_TYPES)
            for key in raw_anomalies.keys():
                clean_key = _clean(key)
                if clean_key and clean_key not in keys:
                    keys.append(clean_key)
            for key in keys:
                anomalies[key] = _normalize_anomaly_record(key, raw_anomalies.get(key))
        elif isinstance(raw_anomalies, list):
            for item in raw_anomalies:
                if isinstance(item, Mapping):
                    key = _clean(item.get("type"))
                    if key:
                        anomalies[key] = _normalize_anomaly_record(key, item)

    if not anomalies:
        for key in ANOMALY_TYPES:
            anomalies[key] = _normalize_anomaly_record(key, None)

    state["anomalies"] = anomalies
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
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalize_state(dict(state)), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _sensor_spike_detection(
    current_snapshot: Mapping[str, dict[str, Any]],
    previous_snapshot: Mapping[str, dict[str, Any]],
) -> tuple[bool, float, str, list[str], str]:
    if not previous_snapshot or not current_snapshot:
        return False, 0.0, "", [], ""

    changed: list[str] = []
    severity_points = 0.0
    mismatch_parts: list[str] = []

    all_keys = sorted(set(current_snapshot) | set(previous_snapshot))
    for key in all_keys:
        prev = previous_snapshot.get(key)
        curr = current_snapshot.get(key)
        if prev is None or curr is None:
            changed.append(key)
            severity_points += 0.2
            continue

        prev_signal = prev.get("signal")
        curr_signal = curr.get("signal")
        if prev_signal is not None and curr_signal is not None and prev_signal != curr_signal:
            changed.append(key)
            severity_points += 0.9
            mismatch_parts.append(f"{key}: {prev.get('raw', '')} -> {curr.get('raw', '')}")
            continue

        prev_numbers = prev.get("numbers") or []
        curr_numbers = curr.get("numbers") or []
        if prev_numbers and curr_numbers:
            paired = list(zip(prev_numbers, curr_numbers))
            deltas = [abs(a - b) for a, b in paired]
            if len(prev_numbers) != len(curr_numbers):
                deltas.append(abs(len(prev_numbers) - len(curr_numbers)) * 0.5)
            max_delta = max(deltas) if deltas else 0.0
            baseline = max(1.0, max([abs(n) for n in prev_numbers + curr_numbers] or [1.0]))
            relative = max_delta / baseline
            if max_delta >= 1.5 or relative >= 0.20:
                changed.append(key)
                severity_points += min(1.0, 0.35 + max_delta * 0.08 + relative * 0.15)
                mismatch_parts.append(f"{key}: {prev.get('raw', '')} -> {curr.get('raw', '')}")
                continue

        if prev.get("raw") != curr.get("raw"):
            changed.append(key)
            severity_points += 0.12

    if not changed:
        return False, 0.0, "", [], ""

    severity = min(1.0, 0.28 + severity_points / max(1.0, len(changed)))
    trigger = severity >= 0.32 or len(changed) >= 2
    summary = f"センサー急変 {len(changed)}件"
    fingerprint = _hash_text("sensor_spike:" + "|".join(changed) + "|" + _stable_text(current_snapshot))
    details = mismatch_parts[:4] if mismatch_parts else [", ".join(changed[:4])]
    return trigger, round(severity, 3), summary, details, fingerprint


def _unresolved_loop_detection(
    loops: list[dict[str, Any]],
) -> tuple[bool, float, str, list[str], str]:
    if not loops:
        return False, 0.0, "", [], ""

    entries: list[str] = []
    max_age_hours = 0.0
    now = _now()
    for loop in loops:
        text = _clean(loop.get("text"))
        loop_id = _clean(loop.get("id"))
        source = _clean(loop.get("source"))
        if text:
            entries.append(f"{loop_id or source or 'loop'}: {text}")
        created = _parse_ts(loop.get("created"))
        if created is not None:
            try:
                age_hours = max(0.0, (now - created).total_seconds() / 3600.0)
                max_age_hours = max(max_age_hours, age_hours)
            except Exception:
                pass

    severity = min(1.0, 0.24 + len(loops) * 0.14 + min(0.3, max_age_hours * 0.04))
    trigger = severity >= 0.20
    summary = f"未解決ループ {len(loops)}件"
    fingerprint = _hash_text("unresolved_loop:" + "|".join(sorted(item.get("id") or item.get("text", "") for item in loops)))
    details = entries[:4]
    return trigger, round(severity, 3), summary, details, fingerprint


def _world_model_mismatch_detection(
    sensor_text: str,
    loops: list[dict[str, Any]],
) -> tuple[bool, float, str, list[str], str]:
    if not loops:
        return False, 0.0, "", [], ""

    sensor_lower = sensor_text.lower()
    contradictions: list[str] = []
    for loop in loops:
        text = _clean(loop.get("text"))
        if not text:
            continue
        loop_lower = text.lower()
        positive_loop = any(cue in loop_lower for cue in ("open", "on", "開いて", "点灯", "ついて", "起動", "在宅", "稼働", "正常"))
        negative_loop = any(cue in loop_lower for cue in ("closed", "off", "閉じ", "消灯", "停止", "不在", "暗い"))
        positive_sensor = any(cue in sensor_lower for cue in ("open", "on", "開いて", "点灯", "ついて", "起動", "在宅", "稼働", "正常"))
        negative_sensor = any(cue in sensor_lower for cue in ("closed", "off", "閉じ", "消灯", "停止", "不在", "暗い"))

        if positive_loop and negative_sensor:
            contradictions.append(f"{text} / sensor: {sensor_text[:80]}")
        elif negative_loop and positive_sensor:
            contradictions.append(f"{text} / sensor: {sensor_text[:80]}")

    if not contradictions:
        return False, 0.0, "", [], ""

    severity = min(1.0, 0.38 + len(contradictions) * 0.16)
    trigger = severity >= 0.30
    summary = f"世界モデルのズレ {len(contradictions)}件"
    fingerprint = _hash_text("world_model_mismatch:" + "|".join(contradictions))
    return trigger, round(severity, 3), summary, contradictions[:4], fingerprint


def _update_record(
    record: dict[str, Any],
    *,
    active: bool,
    severity: float,
    summary: str,
    details: list[str],
    fingerprint: str,
    trigger_explore: bool,
    now: _dt.datetime,
) -> dict[str, Any]:
    ts = now.isoformat(timespec="seconds")
    if active:
        fresh_detection = record.get("resolved", True) or _clean(record.get("fingerprint")) != fingerprint
        if fresh_detection:
            record["count"] = max(0, int(_coerce_float(record.get("count"), 0.0))) + 1
            record["detected_at"] = ts
        record["last_seen_at"] = ts
        record["resolved"] = False
        record["resolved_at"] = ""
        record["severity"] = round(max(_clamp(record.get("severity")), _clamp(severity)), 3)
        record["trigger_explore"] = bool(trigger_explore)
        record["fingerprint"] = fingerprint
        record["summary"] = _clean(summary) or _clean(record.get("summary"))
        record["details"] = details[:4]
    else:
        if not _as_bool(record.get("resolved"), True):
            record["resolved_at"] = ts
        record["resolved"] = True
        record["last_seen_at"] = ts
        record["trigger_explore"] = False
        if summary:
            record["summary"] = _clean(summary)
        if details:
            record["details"] = details[:4]
        if not record.get("detected_at"):
            record["detected_at"] = ts
    return record


def detect_anomalies(
    sensor_logs: Any,
    open_loops: Any,
    state: Mapping[str, Any] | None = None,
    *,
    now: _dt.datetime | None = None,
    trigger_reason: str = "",
    loop_name: str = "loop",
) -> dict[str, Any]:
    """Detect anomalies from sensor logs and open loop data."""

    current = normalize_state(state)
    current_now = now or _now()
    sensor_snapshot = _snapshot_from_sensor_logs(sensor_logs)
    loop_snapshot = _loops_from_input(open_loops)
    sensor_text = _clean(sensor_logs)
    loops_text = "\n".join(_clean(loop.get("text")) for loop in loop_snapshot if _clean(loop.get("text")))

    spike_active, spike_severity, spike_summary, spike_details, spike_fp = _sensor_spike_detection(
        sensor_snapshot,
        current.get("last_sensor_snapshot", {}),
    )
    loop_active, loop_severity, loop_summary, loop_details, loop_fp = _unresolved_loop_detection(loop_snapshot)
    mismatch_active, mismatch_severity, mismatch_summary, mismatch_details, mismatch_fp = _world_model_mismatch_detection(
        sensor_text or _stable_text(sensor_snapshot),
        loop_snapshot,
    )

    updated_anomalies = dict(current["anomalies"])
    updated_anomalies["sensor_spike"] = _update_record(
        updated_anomalies["sensor_spike"],
        active=spike_active,
        severity=spike_severity,
        summary=spike_summary,
        details=spike_details,
        fingerprint=spike_fp,
        trigger_explore=spike_active and spike_severity >= 0.32,
        now=current_now,
    )
    updated_anomalies["unresolved_loop"] = _update_record(
        updated_anomalies["unresolved_loop"],
        active=loop_active,
        severity=loop_severity,
        summary=loop_summary,
        details=loop_details,
        fingerprint=loop_fp,
        trigger_explore=loop_active,
        now=current_now,
    )
    updated_anomalies["world_model_mismatch"] = _update_record(
        updated_anomalies["world_model_mismatch"],
        active=mismatch_active,
        severity=mismatch_severity,
        summary=mismatch_summary,
        details=mismatch_details,
        fingerprint=mismatch_fp,
        trigger_explore=mismatch_active,
        now=current_now,
    )

    current["version"] = STATE_VERSION
    current["updated_at"] = current_now.isoformat(timespec="seconds")
    current["last_loop"] = _clean(loop_name)
    current["last_trigger_reason"] = _clean(trigger_reason)
    current["last_sensor_text"] = sensor_text
    current["last_open_loops_text"] = loops_text
    current["last_sensor_snapshot"] = sensor_snapshot
    current["last_open_loops"] = loop_snapshot
    current["anomalies"] = updated_anomalies

    active = active_anomalies(current)
    if active:
        current["last_detected_at"] = active[0]["last_seen_at"] or active[0]["detected_at"]
    else:
        current["last_resolved_at"] = current_now.isoformat(timespec="seconds")
    return current


def active_anomalies(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    current = normalize_state(state)
    active = [record for record in current["anomalies"].values() if not _as_bool(record.get("resolved"), True)]
    active.sort(key=lambda item: (-_coerce_float(item.get("severity"), 0.0), item.get("type", "")))
    return active


def summarize_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    current = normalize_state(state)
    active = active_anomalies(current)
    return {
        "active": len(active),
        "sensor_spike": int(not _as_bool(current["anomalies"]["sensor_spike"].get("resolved"), True)),
        "unresolved_loop": int(not _as_bool(current["anomalies"]["unresolved_loop"].get("resolved"), True)),
        "world_model_mismatch": int(not _as_bool(current["anomalies"]["world_model_mismatch"].get("resolved"), True)),
        "urgency": compute_explore_urgency(current),
    }


def compute_explore_urgency(state: Mapping[str, Any] | None) -> int:
    current = normalize_state(state)
    urgency = 0.0
    weights = {
        "sensor_spike": 18.0,
        "unresolved_loop": 14.0,
        "world_model_mismatch": 22.0,
    }
    for record in current["anomalies"].values():
        if _as_bool(record.get("resolved"), True):
            continue
        type_name = _clean(record.get("type"))
        weight = weights.get(type_name, 10.0)
        severity = _clamp(record.get("severity"))
        urgency += severity * weight
        if _as_bool(record.get("trigger_explore"), False):
            urgency += severity * 6.0
    return int(max(0.0, min(45.0, round(urgency))))


def format_context_block(state: Mapping[str, Any] | None, limit: int = 3) -> str:
    active = active_anomalies(state)
    if not active:
        return "（特になし）"

    type_labels = {
        "sensor_spike": "センサー急変",
        "unresolved_loop": "未解決ループ",
        "world_model_mismatch": "世界モデルのズレ",
    }
    lines = []
    for record in active[: max(1, limit)]:
        type_name = _clean(record.get("type"))
        label = type_labels.get(type_name, type_name or "anomaly")
        severity = _clamp(record.get("severity"))
        summary = _clean(record.get("summary"))
        details = _normalize_details(record.get("details"))
        line = f"- {label}: severity={severity:.2f}"
        if summary:
            line += f" | {summary}"
        if details:
            line += f" | {details[0]}"
        lines.append(line)
    return "\n".join(lines)


def format_log_line(label: str, state: Mapping[str, Any] | None, **fields: Any) -> str:
    current = normalize_state(state)
    summary = summarize_state(current)
    parts = [
        f"active={summary['active']}",
        f"urgency={summary['urgency']}",
        f"sensor_spike={summary['sensor_spike']}",
        f"unresolved_loop={summary['unresolved_loop']}",
        f"world_model_mismatch={summary['world_model_mismatch']}",
    ]
    for key, value in fields.items():
        if value is None:
            continue
        text = _clean(value)
        if text:
            parts.append(f"{key}={text}")
    return "[anomaly_state] " + " ".join([_clean(label)] + parts)


def load_state_or_default(path: str | None = None) -> dict[str, Any]:
    return load_state(path or _default_state_path())

