#!/usr/bin/env python3
"""sociality MCP server for embodied-ha.

Tools:
  get_relationship       ... return a person's relationship profile/history
  update_relationship    ... append a relationship note for a person
  get_narrative          ... return the current self-narrative thread
  append_narrative       ... append one sentence to the self-narrative
  get_social_state       ... return current social mode / recent interaction state
  update_social_state     ... record a social-state event
  get_shared_focus       ... return the current shared-attention topic/context
  set_shared_focus       ... update the shared-attention focus

All persistent files live under EHA_LOG_DIR. Missing files return empty/default values.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

from mcp_lib import log, serve, text

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_DIR, "log"))

_RELATIONSHIPS_FILE = "relationships.json"
_NARRATIVE_FILE = "self_narrative.md"
_SOCIAL_STATE_FILE = "social_state.json"
_SHARED_FOCUS_FILE = "shared_focus.json"


def _path(name: str) -> str:
    return os.path.join(LOG_DIR, name)


def _now_ts() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _clean(text_value: Any) -> str:
    return " ".join(str(text_value or "").split()).strip()


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if isinstance(data, type(default)) else default


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _write_json(path: str, data: Any) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _write_text(path: str, content: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _parse_ts(value: Any) -> _dt.datetime | None:
    text_value = _clean(value)
    if not text_value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text_value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
    return parsed


def _json_text(data: Any) -> list[dict[str, str]]:
    return [text(json.dumps(data, ensure_ascii=False, indent=2))]


def _default_relationship(person: str) -> dict[str, Any]:
    return {
        "person": person,
        "notes": [],
        "last_seen": "",
        "interaction_count": 0,
    }


def _normalize_relationship(person: str, raw: Any) -> dict[str, Any]:
    profile = _default_relationship(person)
    if not isinstance(raw, dict):
        return profile

    notes = raw.get("notes", [])
    if isinstance(notes, list):
        profile["notes"] = [
            cleaned
            for note in notes
            if (cleaned := _clean(note))
        ]

    last_seen = _clean(raw.get("last_seen"))
    if last_seen:
        profile["last_seen"] = last_seen

    try:
        count = int(raw.get("interaction_count", len(profile["notes"])))
    except Exception:
        count = len(profile["notes"])
    profile["interaction_count"] = max(0, count)
    return profile


def _relationships_path() -> str:
    return _path(_RELATIONSHIPS_FILE)


def _narrative_path() -> str:
    return _path(_NARRATIVE_FILE)


def _social_state_path() -> str:
    return _path(_SOCIAL_STATE_FILE)


def _shared_focus_path() -> str:
    return _path(_SHARED_FOCUS_FILE)


def _load_relationships() -> dict[str, Any]:
    data = _load_json(_relationships_path(), {})
    return data if isinstance(data, dict) else {}


def _load_social_state() -> dict[str, Any]:
    default = {
        "mode": "idle",
        "last_event": "",
        "last_event_ts": "",
        "last_interaction_ts": "",
    }
    data = _load_json(_social_state_path(), default)
    if not isinstance(data, dict):
        return default
    state = dict(default)
    state["mode"] = _clean(data.get("mode")) or default["mode"]
    state["last_event"] = _clean(data.get("last_event"))
    state["last_event_ts"] = _clean(data.get("last_event_ts"))
    state["last_interaction_ts"] = _clean(data.get("last_interaction_ts"))
    return state


def _load_shared_focus() -> dict[str, Any]:
    default = {
        "topic": "",
        "context": "",
        "updated_at": "",
    }
    data = _load_json(_shared_focus_path(), default)
    if not isinstance(data, dict):
        return default
    focus = dict(default)
    focus["topic"] = _clean(data.get("topic"))
    focus["context"] = _clean(data.get("context"))
    focus["updated_at"] = _clean(data.get("updated_at"))
    return focus


def _state_with_elapsed(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    parsed = _parse_ts(state.get("last_interaction_ts"))
    if parsed is None:
        out["elapsed_since_last_interaction_seconds"] = None
        return out
    now = _dt.datetime.now().astimezone()
    try:
        elapsed = int((now - parsed).total_seconds())
    except Exception:
        elapsed = None
    out["elapsed_since_last_interaction_seconds"] = elapsed
    return out


def get_relationship(args: dict[str, Any]):
    person = _clean(args.get("person"))
    if not person:
        return [text("person が空です")], True
    data = _load_relationships()
    profile = _normalize_relationship(person, data.get(person))
    return _json_text(profile)


def update_relationship(args: dict[str, Any]):
    person = _clean(args.get("person"))
    note = _clean(args.get("note"))
    if not person:
        return [text("person が空です")], True
    if not note:
        return [text("note が空です")], True

    data = _load_relationships()
    profile = _normalize_relationship(person, data.get(person))
    profile["notes"].append(note)
    profile["last_seen"] = _now_ts()
    profile["interaction_count"] = int(profile.get("interaction_count", 0)) + 1
    data[person] = {
        "notes": profile["notes"],
        "last_seen": profile["last_seen"],
        "interaction_count": profile["interaction_count"],
    }
    _write_json(_relationships_path(), data)
    log(f"[sociality-mcp] relationship update: {person} (+1)")
    return _json_text(profile)


def get_narrative(args: dict[str, Any]):
    return [text(_read_text(_narrative_path()))]


def append_narrative(args: dict[str, Any]):
    entry = _clean(args.get("entry"))
    if not entry:
        return [text("entry が空です")], True

    path = _narrative_path()
    content = _read_text(path).rstrip()
    line = f"- {_now_ts()} | {entry}"
    content = f"{content}\n{line}\n" if content else f"{line}\n"
    _write_text(path, content)
    log(f"[sociality-mcp] narrative append: {entry[:60]}")
    return [text("self_narrative に追記しました")]


def get_social_state(args: dict[str, Any]):
    return _json_text(_state_with_elapsed(_load_social_state()))


def update_social_state(args: dict[str, Any]):
    event = _clean(args.get("event"))
    if not event:
        return [text("event が空です")], True

    state = _load_social_state()
    now = _now_ts()
    state["last_event"] = event
    state["last_event_ts"] = now
    state["last_interaction_ts"] = now
    if not _clean(state.get("mode")):
        state["mode"] = "idle"
    _write_json(_social_state_path(), state)
    log(f"[sociality-mcp] social state event: {event[:60]}")
    return _json_text(_state_with_elapsed(state))


def get_shared_focus(args: dict[str, Any]):
    return _json_text(_load_shared_focus())


def set_shared_focus(args: dict[str, Any]):
    topic = _clean(args.get("topic"))
    context = _clean(args.get("context"))
    if not topic:
        return [text("topic が空です")], True
    if not context:
        return [text("context が空です")], True

    focus = {
        "topic": topic,
        "context": context,
        "updated_at": _now_ts(),
    }
    _write_json(_shared_focus_path(), focus)
    log(f"[sociality-mcp] shared focus: {topic[:60]}")
    return _json_text(focus)


def main() -> None:
    serve("sociality-mcp", "1.0", {
        "get_relationship": {
            "spec": {
                "name": "get_relationship",
                "description": (
                    "特定の人との関係プロファイルと交流履歴を返す。"
                    "未登録なら空のプロフィールを返す。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "person": {
                            "type": "string",
                            "description": "関係を見たい人物名",
                        },
                    },
                    "required": ["person"],
                },
            },
            "handler": get_relationship,
        },
        "update_relationship": {
            "spec": {
                "name": "update_relationship",
                "description": (
                    "特定の人への関係性メモを追記する。"
                    "会話や気づきを短い一文で残す。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "person": {
                            "type": "string",
                            "description": "関係を更新する人物名",
                        },
                        "note": {
                            "type": "string",
                            "description": "追記するメモ",
                        },
                    },
                    "required": ["person", "note"],
                },
            },
            "handler": update_relationship,
        },
        "get_narrative": {
            "spec": {
                "name": "get_narrative",
                "description": "自分の現在の物語スレッド（self_narrative.md）を返す。",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "handler": get_narrative,
        },
        "append_narrative": {
            "spec": {
                "name": "append_narrative",
                "description": (
                    "自分の物語に一文を追記する。"
                    "一時的な気づきや会話の流れを短く積む。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "entry": {
                            "type": "string",
                            "description": "追記する一文",
                        },
                    },
                    "required": ["entry"],
                },
            },
            "handler": append_narrative,
        },
        "get_social_state": {
            "spec": {
                "name": "get_social_state",
                "description": (
                    "社会状態（モード、最近のイベント、最終交流からの経過時間）を返す。"
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            "handler": get_social_state,
        },
        "update_social_state": {
            "spec": {
                "name": "update_social_state",
                "description": (
                    "社会状態の更新イベントを記録する。"
                    "会話の開始・終了、関係の切り替えなどを残す。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event": {
                            "type": "string",
                            "description": "記録するイベント名や短い説明",
                        },
                    },
                    "required": ["event"],
                },
            },
            "handler": update_social_state,
        },
        "get_shared_focus": {
            "spec": {
                "name": "get_shared_focus",
                "description": "現在の共同注意フォーカス（topic/context）を返す。",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "handler": get_shared_focus,
        },
        "set_shared_focus": {
            "spec": {
                "name": "set_shared_focus",
                "description": (
                    "共同注意フォーカスを更新する。"
                    "今何に注目しているかを topic/context で記録する。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "注目対象の短い名前",
                        },
                        "context": {
                            "type": "string",
                            "description": "補足コンテキスト",
                        },
                    },
                    "required": ["topic", "context"],
                },
            },
            "handler": set_shared_focus,
        },
    })


if __name__ == "__main__":
    main()
