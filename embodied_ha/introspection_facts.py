#!/usr/bin/env python3
"""Ground observed loop-session facts against private introspection."""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from typing import Any

SPEAK_TOOLS = {"mcp__audio__speak", "mcp__audio__use_device_speaker"}
ACTION_TOOLS = {"mcp__hacontrol__ha_call_service"}
CAMERA_TOOL = "mcp__camera__use_device_camera"

# 完了形のみマッチ。願望（〜たい）・過去願望（〜たかった）・仮定（〜たら）・並列（〜たり）は除外
_SPEECH_CLAIM_RE = re.compile(
    r"(?:伝え(?:た(?!い|かった|ら|り)|ました)|報告(?:した(?!い|かった|ら|り)|しました)|話し(?:た(?!い|かった|ら|り)|ました)|知らせ(?:た(?!い|かった|ら|り)|ました)|言っ(?:た(?!ら|り)|ておいた)|言いました)"
)
_VISUAL_CLAIM_RE = re.compile(r"(?:見え|見た|見て|映っ|映り|視界|カメラに|カメラで|画像|映像)")


def _content_blocks(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = event.get("content")
    message = event.get("message")
    if content is None and isinstance(message, Mapping):
        content = message.get("content")
    if isinstance(content, Mapping):
        return [content]
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, Mapping)]


def _tool_name_for_result(block: Mapping[str, Any], tool_names_by_id: Mapping[str, str]) -> str:
    name = str(block.get("name") or "").strip()
    if name:
        return name
    tool_id = str(block.get("tool_use_id") or block.get("id") or "").strip()
    if tool_id:
        return tool_names_by_id.get(tool_id, "")
    return ""


def extract_facts_from_stream_lines(lines: Iterable[str]) -> dict[str, Any]:
    """Extract observed tool facts from Claude stream-json lines."""
    tools_used: collections.Counter[str] = collections.Counter()
    error_tools: list[str] = []
    tool_names_by_id: dict[str, str] = {}
    tool_calls = 0
    tool_errors = 0
    speak_ok = 0
    action_ok = 0

    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        blocks = _content_blocks(event)
        if event_type == "assistant":
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "").strip()
                if not name:
                    continue
                tool_calls += 1
                tools_used[name] += 1
                tool_id = str(block.get("id") or "").strip()
                if tool_id:
                    tool_names_by_id[tool_id] = name
        elif event_type == "user":
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                name = _tool_name_for_result(block, tool_names_by_id)
                is_error = bool(block.get("is_error"))
                if is_error:
                    tool_errors += 1
                    error_tools.append(name or "unknown")
                elif name in SPEAK_TOOLS:
                    speak_ok += 1
                elif name in ACTION_TOOLS:
                    action_ok += 1

    return {
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tools_used": dict(sorted(tools_used.items())),
        "error_tools": error_tools,
        "speak_ok": speak_ok,
        "action_ok": action_ok,
    }


def extract_facts_from_stream_text(text: str) -> dict[str, Any]:
    return extract_facts_from_stream_lines(text.splitlines())


def write_facts_file(path: str, facts: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dict(facts), f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def load_facts_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _has_proposal(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        text = value.strip()
        return bool(text) and text.lower() not in {"null", "none"}
    return True


def has_completed_speech_claim(text: str) -> bool:
    return bool(_SPEECH_CLAIM_RE.search(str(text or "")))


def should_flag_ungrounded_speech_claim(
    *,
    private: str,
    topic: str = "",
    facts: Mapping[str, Any] | None,
    proposal: Any = None,
) -> bool:
    if not isinstance(facts, Mapping):
        return False
    try:
        speak_ok = int(facts.get("speak_ok", 0))
    except Exception:
        speak_ok = 0
    if speak_ok > 0 or _has_proposal(proposal):
        return False
    return has_completed_speech_claim(f"{private}\n{topic}")


def has_visual_claim(text: str) -> bool:
    return bool(_VISUAL_CLAIM_RE.search(str(text or "")))


def _tool_count(facts: Mapping[str, Any], name: str) -> int:
    tools_used = facts.get("tools_used")
    if not isinstance(tools_used, Mapping):
        return 0
    try:
        return int(tools_used.get(name, 0))
    except Exception:
        return 0


def should_flag_ungrounded_visual_claim(
    *,
    private: str,
    topic: str = "",
    speak: str = "",
    facts: Mapping[str, Any] | None,
    current_entity: str = "",
) -> bool:
    if str(current_entity or "").strip().startswith("camera."):
        return False
    if not isinstance(facts, Mapping):
        return False
    if _tool_count(facts, CAMERA_TOOL) > 0:
        return False
    return has_visual_claim(f"{private}\n{topic}\n{speak}")


def format_facts_summary(facts: Mapping[str, Any] | None) -> str:
    if not isinstance(facts, Mapping):
        return ""
    calls = _int(facts.get("tool_calls"))
    errors = _int(facts.get("tool_errors"))
    speak_ok = _int(facts.get("speak_ok"))
    action_ok = _int(facts.get("action_ok"))
    parts = [f"ツール{calls}回/エラー{errors}"]
    if speak_ok:
        parts.append(f"発話{speak_ok}")
    if action_ok:
        parts.append(f"操作{action_ok}")
    return "・".join(parts)


def _int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _display_tool_name(name: str) -> str:
    text = str(name or "").strip()
    if text.startswith("mcp__"):
        parts = text.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return text or "unknown"


def _parse_ts(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def recent_facts_from_logs(
    paths: Iterable[str],
    *,
    now: dt.datetime | None = None,
    hours: int = 24,
    limit: int = 10,
) -> list[dict[str, Any]]:
    now = now or dt.datetime.now().astimezone()
    cutoff = now - dt.timedelta(hours=hours)
    items: list[tuple[dt.datetime, dict[str, Any]]] = []
    for path in paths:
        try:
            f = open(path, encoding="utf-8")
        except FileNotFoundError:
            continue
        except Exception:
            continue
        with f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("facts"), dict):
                    continue
                ts = _parse_ts(row.get("timestamp"))
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=now.tzinfo)
                if ts < cutoff:
                    continue
                items.append((ts, row))
    items.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in items[: max(0, limit)]]


def format_recent_facts_block(rows: Iterable[Mapping[str, Any]], *, hours: int = 24) -> str:
    facts_items = [row.get("facts") for row in rows if isinstance(row.get("facts"), Mapping)]
    if not facts_items:
        return ""
    calls = sum(_int(f.get("tool_calls")) for f in facts_items)
    errors = sum(_int(f.get("tool_errors")) for f in facts_items)
    speak_ok = sum(_int(f.get("speak_ok")) for f in facts_items)
    action_ok = sum(_int(f.get("action_ok")) for f in facts_items)
    error_counts: collections.Counter[str] = collections.Counter()
    for facts in facts_items:
        for name in facts.get("error_tools") or []:
            error_counts[_display_tool_name(str(name))] += 1
    error_detail = ""
    if error_counts:
        detail = "・".join(f"{name} {count}" for name, count in error_counts.most_common(4))
        error_detail = f"（{detail}）"
    elif errors:
        error_detail = "（詳細不明）"
    return (
        f"【直近の実測】直近{hours}時間の{len(facts_items)}セッションで"
        f"ツール{calls}回中エラー{errors}回{error_detail}。発話{speak_ok}回・家電操作{action_ok}回。"
    )
