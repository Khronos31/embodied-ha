#!/usr/bin/env python3
"""Counterfactual logging for actions the agent chose not to take."""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from state_utils import clamp as _clamp
from state_utils import clean as _clean
from state_utils import now as _now
from state_utils import parse_ts as _parse_ts

_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_DIR, "log"))
_FILENAME = "counterfactuals.jsonl"


def counterfactuals_path(log_dir: str | None = None) -> str:
    return os.path.join(log_dir or _DEFAULT_LOG_DIR, _FILENAME)


def _normalize_evidence(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values is not None else []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _append_jsonl(path: str, row: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def record_counterfactual(
    loop: str,
    intent: str,
    summary: str,
    rejected_because: str,
    evidence: list[str],
    confidence: float,
    boundary_reason: str | None = None,
    log_dir: str | None = None,
) -> dict[str, Any]:
    """Append one counterfactual row and return the normalized record."""

    row = {
        "timestamp": _now().isoformat(timespec="seconds"),
        "loop": _clean(loop) or "unknown",
        "intent": _clean(intent) or "propose",
        "summary": _clean(summary) or "何かをしようとして、やめた",
        "rejected_because": _clean(rejected_because) or "unknown",
        "evidence": _normalize_evidence(evidence),
        "confidence": round(_clamp(confidence, 0.0, 1.0, 0.5), 3),
        "boundary_reason": _clean(boundary_reason),
    }
    _append_jsonl(counterfactuals_path(log_dir), row)
    return row


def recent_counterfactuals(log_dir: str | None = None, *, hours: int = 24, limit: int | None = None) -> list[dict[str, Any]]:
    """Return recent rows newest first, best-effort on malformed lines."""

    path = counterfactuals_path(log_dir)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    cutoff = _now() - dt.timedelta(hours=max(1, int(hours or 24)))
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        parsed = _parse_ts(row.get("timestamp"))
        if parsed is None or parsed < cutoff:
            continue
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows


def best_recent_counterfactual(log_dir: str | None = None, *, hours: int = 24) -> dict[str, Any] | None:
    rows = recent_counterfactuals(log_dir, hours=hours)
    if not rows:
        return None
    return max(rows, key=lambda row: (_clamp(row.get("confidence"), 0.0, 1.0, 0.5), _clean(row.get("timestamp"))))


def counterfactual_sentence(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    summary = _clean(row.get("summary"))
    reason = _clean(row.get("boundary_reason")) or _clean(row.get("rejected_because"))
    if not summary:
        return ""
    if reason:
        return f"{summary}けど、{reason}からやめた"
    return f"{summary}けど、やめた"
