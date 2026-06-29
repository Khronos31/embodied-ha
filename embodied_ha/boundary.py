#!/usr/bin/env python3
"""Boundary checks for embodied-ha.

This module keeps the final enforcement logic in one place and can be used both
as a pure function and as a small CLI helper for shell scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

import sociality_state as ss
import counterfactual_state as cs


QUIET_HOURS = range(1, 7)


def _compact(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {
        "1",
        "true",
        "yes",
        "y",
        "on",
        "home",
        "present",
        "occupied",
        "detected",
    }


def _coerce_presence(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, bool] = {}
    for key, item in value.items():
        out[str(key)] = _truthy(item)
    return out


def _coerce_policies(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _coerce_metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_body_state(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _presence_any_home(presence: dict[str, bool]) -> bool:
    return any(bool(v) for v in presence.values())


def _quiet_hours_reason(intent: str) -> str:
    if intent == "speak":
        return "深夜帯（1-6時）のため発話抑制"
    return "深夜帯（1-6時）のため自律操作抑制"


def check(
    mode: str,
    intent: str,
    hour: int,
    is_autonomous: bool,
    presence: dict[str, bool],
    policies: list[str],
    metadata: dict[str, Any],
    person: str = "",
    body_state: dict[str, Any] | None = None,
    sociality_log_dir: str | None = None,
) -> dict[str, Any]:
    """Return a normalized boundary decision."""

    mode = _compact(mode)
    intent = _compact(intent)
    person = _compact(person)
    try:
        hour = int(hour)
    except Exception:
        hour = 12

    is_autonomous = _truthy(is_autonomous)
    presence = _coerce_presence(presence)
    policies = _coerce_policies(policies)
    metadata = _coerce_metadata(metadata)
    body_state = _coerce_body_state(body_state)

    if intent not in {"speak", "action"}:
        return {"allowed": False, "reason": f"未知のintent: {intent or '（空）'}", "fallback": None}

    if intent == "action" and mode != "loop":
        return {"allowed": False, "reason": f"{mode or 'unknown'}モードでは家電操作しない", "fallback": None}

    if mode == "chat" and intent == "speak":
        model = ss.get_person_model(sociality_log_dir, person or metadata.get("person", ""))
        return {
            "allowed": True,
            "reason": "chat direct response",
            "fallback": None,
            "person": model.get("person", ""),
            "quiet_window": model.get("boundary", {}).get("quiet_window", {}),
            "consent": model.get("boundary", {}).get("consent", {}),
            "turn_taking": model.get("boundary", {}).get("turn_taking", {}),
            "shared_focus": model.get("shared_focus", {}),
        }

    model = ss.get_person_model(sociality_log_dir, person or metadata.get("person", "") or os.environ.get("RESIDENT", ""))
    social = ss.evaluate_interrupt(
        model,
        mode=mode,
        intent=intent,
        hour=hour,
        metadata=metadata,
        body_state=body_state,
    )
    if social.get("direct_override") or social.get("urgent_override"):
        return social

    if hour in QUIET_HOURS:
        return {
            "allowed": False,
            "reason": _quiet_hours_reason(intent),
            "fallback": None,
        }

    if not social.get("allowed", True):
        return social

    if intent == "action":
        if not is_autonomous:
            return {
                "allowed": False,
                "reason": "自律操作OFFのため家電操作しない",
                "fallback": None,
            }
        if not _presence_any_home(presence):
            return {
                "allowed": False,
                "reason": "不在のため家電操作を抑制",
                "fallback": None,
            }
        return {"allowed": True, "reason": "許可", "fallback": None}

    return social


def _load_json_file(path: str) -> Any:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_policies(*paths: str) -> list[str]:
    for path in paths:
        data = _load_json_file(path)
        if isinstance(data, dict):
            return _coerce_policies(data.get("policies", []))
        if isinstance(data, list):
            return _coerce_policies(data)
    return []


def _presence_from_sensors_text(text: str, resident_label: str) -> dict[str, bool]:
    if not text:
        return {}

    labels = [resident_label]
    if resident_label:
        labels.append(f"{resident_label}さん")

    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("---"):
            continue
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        if any(left.strip() == label for label in labels if label):
            return {resident_label or "resident": _truthy(right.strip())}
    return {}


def _load_presence(args: argparse.Namespace, prefs: dict[str, Any]) -> dict[str, bool]:
    if args.presence_json:
        try:
            parsed = json.loads(args.presence_json)
            presence = _coerce_presence(parsed)
            if presence:
                return presence
        except Exception:
            pass

    for path in (args.presence_file, args.prefs_file):
        if not path:
            continue
        data = _load_json_file(path)
        if isinstance(data, dict):
            direct = data.get("presence")
            if isinstance(direct, dict) and direct and all(
                isinstance(v, (bool, int, float, str)) for v in direct.values()
            ):
                presence = _coerce_presence(direct)
                if presence:
                    return presence

    sensors_text = args.sensors_text or os.environ.get("SENSORS_DATA", "")
    resident_label = os.environ.get("RESIDENT", "resident")
    presence = _presence_from_sensors_text(sensors_text, resident_label)
    if presence:
        return presence

    if isinstance(prefs.get("presence"), dict):
        # Preferences only stores the entity id, so there is no live state here.
        # Keep the shape stable by returning an empty mapping when no state is available.
        return {}

    return {}


def _load_prefs(path: str) -> dict[str, Any]:
    data = _load_json_file(path)
    return data if isinstance(data, dict) else {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="loop")
    parser.add_argument("--intent", default="speak")
    parser.add_argument("--hour", default="12")
    parser.add_argument("--autonomous", default="0")
    parser.add_argument("--prefs-file", default=os.environ.get("EHA_PREFS_FILE", ""))
    parser.add_argument("--presence-file", default="")
    parser.add_argument("--policies-file", default="")
    parser.add_argument("--presence-json", default="")
    parser.add_argument("--policies-json", default="")
    parser.add_argument("--metadata-json", default="")
    parser.add_argument("--sensors-text", default="")
    parser.add_argument("--person", default=os.environ.get("RESIDENT", ""))
    parser.add_argument("--body-state-json", default=os.environ.get("EHA_BODY_STATE", ""))
    parser.add_argument("--sociality-log-dir", default=os.environ.get("EHA_LOG_DIR", ""))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    prefs = _load_prefs(args.prefs_file)

    policies = []
    if args.policies_json:
        try:
            policies = _coerce_policies(json.loads(args.policies_json))
        except Exception:
            policies = []
    if not policies:
        policies = _load_policies(args.policies_file, args.prefs_file)

    metadata = {}
    if args.metadata_json:
        try:
            parsed = json.loads(args.metadata_json)
            metadata = _coerce_metadata(parsed)
        except Exception:
            metadata = {}

    body_state = _coerce_body_state(args.body_state_json)
    presence = _load_presence(args, prefs)
    result = check(
        mode=args.mode,
        intent=args.intent,
        hour=args.hour,
        is_autonomous=args.autonomous,
        presence=presence,
        policies=policies,
        metadata=metadata,
        person=args.person,
        body_state=body_state,
        sociality_log_dir=args.sociality_log_dir,
    )
    if not result.get("allowed"):
        intent = _compact(args.intent)
        reason = str(result.get("reason") or "boundary_denied")
        evidence = [f"hour={args.hour}"]
        if args.metadata_json:
            evidence.append(f"metadata={args.metadata_json}")
        if args.body_state_json:
            evidence.append("body_state=present")
        try:
            hour = int(args.hour)
        except Exception:
            hour = 12
        try:
            cs.record_counterfactual(
                args.mode,
                "act" if intent == "action" else intent,
                "発話しようとした" if intent == "speak" else "家電操作しようとした",
                "quiet_window" if hour in QUIET_HOURS else "boundary_denied",
                evidence,
                0.7,
                boundary_reason=reason,
                log_dir=args.sociality_log_dir or None,
            )
        except Exception:
            pass
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("allowed=" + str(result["allowed"]))
        print("reason=" + str(result["reason"]))
    return 0 if result["allowed"] else 1


if __name__ == "__main__":
    sys.exit(main())
