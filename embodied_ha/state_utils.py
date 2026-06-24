"""Shared low-level helpers for the *_state.py modules.

Whitespace cleanup, numeric coercion/clamping, timezone-aware timestamps and
atomic JSON IO were duplicated verbatim across body_state / sociality_state /
memory_state / desire_state / anomaly_state. They live here once so the state
modules can import them under their existing private names.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any


def clean(value: Any) -> str:
    """Stringify and collapse all runs of whitespace; ``None`` becomes ``""``."""
    return " ".join(str(value or "").split()).strip()


def clamp(
    value: Any,
    low: float = 0.0,
    high: float = 1.0,
    default: float | None = None,
) -> float:
    """Coerce ``value`` to float and clamp to ``[low, high]``.

    On coercion failure fall back to ``default`` when provided, otherwise to
    ``low`` (the historical behavior of every caller except memory_state, which
    passed an explicit default).
    """
    try:
        number = float(value)
    except Exception:
        number = low if default is None else default
    return max(low, min(high, number))


def coerce_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to float, returning ``default`` on failure."""
    try:
        return float(value)
    except Exception:
        return default


def now() -> _dt.datetime:
    """Timezone-aware local ``now()``."""
    return _dt.datetime.now().astimezone()


def parse_ts(value: Any) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp; assume local tz when naive. ``None`` on failure."""
    text = clean(value)
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now().tzinfo)
    return parsed


def write_json(path: str, data: Any) -> None:
    """Atomically write ``data`` as pretty UTF-8 JSON (tmp file + ``os.replace``)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON from ``path``; return ``default`` on any failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default
