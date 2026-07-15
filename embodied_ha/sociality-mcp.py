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
  get_person_model       ... return the current boundary model for one person
  record_boundary        ... update quiet_window / consent / turn-taking / focus
  record_consent         ... record granted/denied consent for speak/action
  should_interrupt       ... evaluate whether we should speak/intervene now
  get_turn_taking_state   ... return the current turn-taking state
  ingest_interaction     ... ingest a recent human/agent interaction

All persistent files live under EHA_LOG_DIR. Missing files return empty/default values.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

from mcp_lib import log, serve, text
import sociality_state as ss

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


def _args_preview(args: dict[str, Any], *, max_value_len: int = 80) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for key in sorted(args):
        value = args.get(key)
        if isinstance(value, (dict, list)):
            value_text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            value_text = str(value)
        if len(value_text) > max_value_len:
            value_text = value_text[:max_value_len] + "..."
        preview[key] = value_text
    return preview


def _log_invalid_args(tool_name: str, args: dict[str, Any], reason: str) -> None:
    keys = sorted(str(key) for key in args)
    log(f"[sociality-mcp] {tool_name} invalid args: reason={reason} keys={keys} preview={_args_preview(args)}")


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


def _json_load(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


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
        "object_id": "",
        "scene_source": "",
        "last_seen_at": "",
        "updated_at": "",
    }
    data = _load_json(_shared_focus_path(), default)
    if not isinstance(data, dict):
        return default
    focus = dict(default)
    focus["topic"] = _clean(data.get("topic"))
    focus["context"] = _clean(data.get("context"))
    focus["object_id"] = _clean(data.get("object_id"))
    focus["scene_source"] = _clean(data.get("scene_source"))
    focus["last_seen_at"] = _clean(data.get("last_seen_at"))
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
        _log_invalid_args("update_relationship", args, "missing_person")
        return [text("person が空です")], True
    if not note:
        _log_invalid_args("update_relationship", args, "missing_note")
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
    narrative_text = _clean(args.get("text"))
    if not narrative_text:
        return [text("text が空です")], True

    path = _narrative_path()
    content = _read_text(path).rstrip()
    line = f"- {_now_ts()} | {narrative_text}"
    content = f"{content}\n{line}\n" if content else f"{line}\n"
    _write_text(path, content)
    log(f"[sociality-mcp] narrative append: {narrative_text[:60]}")
    return [text("self_narrative に追記しました")]


def get_social_state(args: dict[str, Any]):
    return _json_text(_state_with_elapsed(_load_social_state()))


def update_social_state(args: dict[str, Any]):
    event = _clean(args.get("event"))
    if not event:
        _log_invalid_args("update_social_state", args, "missing_event")
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


def get_person_model(args: dict[str, Any]):
    person = _clean(args.get("person"))
    return _json_text(ss.get_person_model(LOG_DIR, person))


def record_boundary(args: dict[str, Any]):
    person = _clean(args.get("person"))
    patch: dict[str, Any] = {}
    for key in ("quiet_window", "consent", "turn_taking", "shared_focus", "boundary", "updated_at"):
        if key in args and args.get(key) is not None:
            patch[key] = _json_load(args.get(key))
    return _json_text(ss.record_boundary(LOG_DIR, person, patch))


def record_consent(args: dict[str, Any]):
    person = _clean(args.get("person"))
    kind = _clean(args.get("kind")) or "all"
    granted = args.get("granted", True)
    note = _clean(args.get("note"))
    return _json_text(ss.record_consent(LOG_DIR, person, kind, granted, note=note))


def should_interrupt(args: dict[str, Any]):
    person = _clean(args.get("person"))
    mode = _clean(args.get("mode")) or "loop"
    intent = _clean(args.get("intent")) or "speak"
    hour = args.get("hour", 12)
    metadata = _json_load(args.get("metadata") or args.get("metadata_json") or {})
    body_state = _json_load(args.get("body_state") or args.get("body_state_json") or {})
    model = ss.get_person_model(LOG_DIR, person)
    decision = ss.evaluate_interrupt(
        model,
        mode=mode,
        intent=intent,
        hour=hour,
        metadata=metadata if isinstance(metadata, dict) else {},
        body_state=body_state if isinstance(body_state, dict) else {},
    )
    return _json_text(decision)


def get_turn_taking_state(args: dict[str, Any]):
    person = _clean(args.get("person"))
    return _json_text(ss.get_turn_taking_state(LOG_DIR, person))


def ingest_interaction(args: dict[str, Any]):
    person = _clean(args.get("person"))
    speaker = _clean(args.get("speaker"))
    kind = _clean(args.get("kind"))
    text_value = _clean(args.get("text") or args.get("utterance"))
    shared_focus = _json_load(args.get("shared_focus") or args.get("shared_focus_json") or None)
    return _json_text(
        ss.update_turn_taking(
            LOG_DIR,
            person,
            speaker=speaker,
            kind=kind,
            text=text_value,
            shared_focus=shared_focus,
        )
    )


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
        "object_id": _clean(args.get("object_id")),
        "scene_source": _clean(args.get("scene_source")),
        "last_seen_at": _clean(args.get("last_seen_at")),
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
                        "text": {
                            "type": "string",
                            "description": "追記する一文",
                        },
                    },
                    "required": ["text"],
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
                        "object_id": {"type": "string", "description": "scene 内の注目 object id"},
                        "scene_source": {"type": "string", "description": "scene の camera/source"},
                        "last_seen_at": {"type": "string", "description": "最後に見た時刻"},
                    },
                    "required": ["topic", "context"],
                },
            },
            "handler": set_shared_focus,
        },
        "get_person_model": {
            "spec": {
                "name": "get_person_model",
                "description": (
                    "特定の人物について、quiet_window / consent / turn-taking / shared_focus を含む"
                    " boundary モデルを返す。未登録でも default モデルを返す。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "person": {
                            "type": "string",
                            "description": "人物名（空でも可）",
                        },
                    },
                },
            },
            "handler": get_person_model,
        },
        "record_boundary": {
            "spec": {
                "name": "record_boundary",
                "description": (
                    "quiet_window / consent / turn-taking / shared_focus の設定や観測を記録する。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "person": {"type": "string"},
                        "quiet_window": {"type": "object"},
                        "consent": {"type": "object"},
                        "turn_taking": {"type": "object"},
                        "shared_focus": {"type": ["object", "string"]},
                        "boundary": {"type": "object"},
                    },
                },
            },
            "handler": record_boundary,
        },
        "record_consent": {
            "spec": {
                "name": "record_consent",
                "description": (
                    "人物ごとの consent を更新する。kind は speak / action など。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "person": {"type": "string"},
                        "kind": {"type": "string"},
                        "granted": {"type": "boolean"},
                        "note": {"type": "string"},
                    },
                    "required": ["person", "kind", "granted"],
                },
            },
            "handler": record_consent,
        },
        "should_interrupt": {
            "spec": {
                "name": "should_interrupt",
                "description": (
                    "quiet_window / consent / turn-taking / shared_focus / body_state を見て、"
                    "今 speak / intervene すべきか判定する。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "person": {"type": "string"},
                        "mode": {"type": "string"},
                        "intent": {"type": "string"},
                        "hour": {"type": "integer"},
                        "metadata": {"type": "object"},
                        "metadata_json": {"type": "string"},
                        "body_state": {"type": "object"},
                        "body_state_json": {"type": "string"},
                    },
                },
            },
            "handler": should_interrupt,
        },
        "get_turn_taking_state": {
            "spec": {
                "name": "get_turn_taking_state",
                "description": (
                    "人物ごとの turn-taking 状態と、その時点の boundary モデルを返す。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "person": {"type": "string"},
                    },
                },
            },
            "handler": get_turn_taking_state,
        },
        "ingest_interaction": {
            "spec": {
                "name": "ingest_interaction",
                "description": (
                    "最近の会話・呼びかけ・応答を turn-taking に取り込む。"
                    "必要なら shared_focus も同時に更新する。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "person": {"type": "string"},
                        "speaker": {"type": "string"},
                        "kind": {"type": "string"},
                        "text": {"type": "string"},
                        "utterance": {"type": "string"},
                        "shared_focus": {"type": ["object", "string"]},
                        "shared_focus_json": {"type": "string"},
                    },
                    "required": ["person"],
                },
            },
            "handler": ingest_interaction,
        },
    })


if __name__ == "__main__":
    main()
