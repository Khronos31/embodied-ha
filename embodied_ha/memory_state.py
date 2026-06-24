#!/usr/bin/env python3
"""Structured memory helpers for embodied-ha.

This module manages episodic memories and daybooks under EHA_LOG_DIR/memory.
It keeps the code style aligned with body_state.py / sociality_state.py:
small pure helpers, normalization, and atomic writes.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from typing import Any, Iterable, Mapping

_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_DIR, "log"))

_MEMORY_DIR = "memory"
_EPISODES_DIR = "episodes"
_DAYBOOKS_DIR = "daybooks"
_CAUSAL_CHAINS_DIR = "causal_chains"
_CONSOLIDATIONS_DIR = "consolidations"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _clamp(value: Any, low: float = 0.0, high: float = 1.0, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        number = default
    return max(low, min(high, number))


def _now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _parse_ts(value: Any) -> _dt.datetime | None:
    text_value = _clean(value)
    if not text_value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text_value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_now().tzinfo)
    return parsed


def _slug(value: Any, fallback: str = "item") -> str:
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", _clean(value))
    return text or fallback


def _unique_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _normalize_text_list(values: Any) -> list[str]:
    return _unique_list(values)


def _normalize_mapping_list(values: Any, *, fallback_key: str = "summary") -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    out: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, dict):
            row = {}
            for key, value in item.items():
                if isinstance(value, list):
                    row[key] = _unique_list(value)
                elif key == "importance":
                    row[key] = round(_clamp(value, 0.0, 1.0, 0.5), 3)
                else:
                    row[key] = _clean(value)
            if row.get(fallback_key) or row.get("episode_id") or row.get("episode_index") is not None:
                out.append(row)
        else:
            text = _clean(item)
            if text:
                out.append({fallback_key: text})
    return out


_CAUSAL_RELATION_ALIASES = {
    "caused": {"caused", "cause", "causes", "causing", "triggered", "resulted", "ledto", "led", "produced", "generated", "made"},
    "enabled": {"enabled", "enable", "enables", "facilitated", "helped", "allowed", "supported", "unlocked"},
    "prevented": {"prevented", "prevent", "prevents", "blocked", "avoided", "stopped", "suppressed", "hindered"},
    "correlated": {"correlated", "correlation", "related", "associated", "linked", "cooccurred", "cooccur", "same"},
}


def _normalize_causal_relation(value: Any) -> str:
    text = re.sub(r"[\s_-]+", "", _clean(value).lower())
    if not text:
        return "correlated"
    for canonical, aliases in _CAUSAL_RELATION_ALIASES.items():
        if text == canonical or text in aliases:
            return canonical
    if text.startswith(("cause", "trigger", "result", "lead", "make")):
        return "caused"
    if text.startswith(("enable", "facil", "help", "allow", "support")):
        return "enabled"
    if text.startswith(("prevent", "block", "avoid", "stop", "hinder")):
        return "prevented"
    if text.startswith(("correl", "relat", "associ", "link", "cooccur")):
        return "correlated"
    return "correlated"


def _path(log_dir: str | None, *parts: str) -> str:
    base = log_dir or _DEFAULT_LOG_DIR
    return os.path.join(base, _MEMORY_DIR, *parts)


def memory_root(log_dir: str | None = None) -> str:
    return _path(log_dir)


def episodes_dir(log_dir: str | None = None) -> str:
    return _path(log_dir, _EPISODES_DIR)


def daybooks_dir(log_dir: str | None = None) -> str:
    return _path(log_dir, _DAYBOOKS_DIR)


def causal_chains_dir(log_dir: str | None = None) -> str:
    return _path(log_dir, _CAUSAL_CHAINS_DIR)


def consolidations_dir(log_dir: str | None = None) -> str:
    return _path(log_dir, _CONSOLIDATIONS_DIR)


def episode_path(log_dir: str | None, episode_id: str) -> str:
    return _path(log_dir, _EPISODES_DIR, f"{_clean(episode_id)}.json")


def daybook_path(log_dir: str | None, date: str) -> str:
    return _path(log_dir, _DAYBOOKS_DIR, f"{_clean(date)}.json")


def causal_chain_path(log_dir: str | None, chain_id: str) -> str:
    return _path(log_dir, _CAUSAL_CHAINS_DIR, f"{_clean(chain_id)}.json")


def consolidation_report_path(log_dir: str | None, scope: str) -> str:
    return _path(log_dir, _CONSOLIDATIONS_DIR, f"{_slug(scope, 'all')}.json")


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _timestamp_to_day(timestamp: str) -> str:
    parsed = _parse_ts(timestamp)
    if parsed is None:
        return ""
    return parsed.date().isoformat()


def default_episode(episode_id: str = "", day: str = "") -> dict[str, Any]:
    return {
        "id": _clean(episode_id),
        "timestamp": "",
        "day": _clean(day),
        "kind": "observation",
        "source": "",
        "summary": "",
        "detail": "",
        "tags": [],
        "entities": [],
        "actors": [],
        "importance": 0.5,
        "evidence": [],
        "status": "canonical",
        "fingerprint": "",
        "topic_fingerprint": "",
        "consolidated_at": "",
        "merged_into": "",
        "conflict_group": "",
        "conflict_reason": "",
        "links": {"causes": [], "effects": []},
    }


def default_daybook(date: str = "") -> dict[str, Any]:
    return {
        "date": _clean(date),
        "generated_at": "",
        "source": "watch",
        "episode_ids": [],
        "summary": "",
        "themes": [],
        "highlights": [],
        "open_questions": [],
        "importance_cutoff": 0.65,
        "raw_entry_count": 0,
        "episode_count": 0,
    }


def _make_episode_id(data: Mapping[str, Any]) -> str:
    timestamp = _clean(data.get("timestamp")) or _now().isoformat(timespec="seconds")
    day = _clean(data.get("day")) or _timestamp_to_day(timestamp) or _now().date().isoformat()
    source = _slug(data.get("source"), "source")
    kind = _slug(data.get("kind"), "episode")
    summary = _clean(data.get("summary"))
    detail = _clean(data.get("detail"))
    basis = "|".join([timestamp, day, source, kind, summary, detail, ",".join(_unique_list(data.get("tags")))])
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
    return f"ep_{day.replace('-', '')}_{source}_{kind}_{digest}"


def _normalize_evidence(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    out: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, dict):
            cleaned = {
                key: _clean(value)
                for key, value in item.items()
                if _clean(value)
            }
            if cleaned:
                out.append(cleaned)
        else:
            text = _clean(item)
            if text:
                out.append({"text": text})
    return out


def _normalize_links(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "causes": _unique_list(raw.get("causes")),
        "effects": _unique_list(raw.get("effects")),
    }


def normalize_episode(raw: Any, *, fallback_id: str = "", fallback_day: str = "") -> dict[str, Any]:
    episode = default_episode(fallback_id, fallback_day)
    if not isinstance(raw, dict):
        return episode

    source = raw.get("episode") if isinstance(raw.get("episode"), dict) else raw

    episode["id"] = _clean(source.get("id") or source.get("episode_id") or fallback_id)
    episode["timestamp"] = _clean(source.get("timestamp")) or _now().isoformat(timespec="seconds")
    episode["day"] = _clean(source.get("day")) or _timestamp_to_day(episode["timestamp"]) or fallback_day
    episode["kind"] = _clean(source.get("kind")) or episode["kind"]
    episode["source"] = _clean(source.get("source"))
    episode["summary"] = _clean(source.get("summary"))
    episode["detail"] = _clean(source.get("detail"))
    episode["tags"] = _normalize_text_list(source.get("tags"))
    episode["entities"] = _normalize_text_list(source.get("entities"))
    episode["actors"] = _normalize_text_list(source.get("actors"))
    episode["importance"] = round(_clamp(source.get("importance"), 0.0, 1.0, 0.5), 3)
    episode["evidence"] = _normalize_evidence(source.get("evidence"))
    episode["status"] = _clean(source.get("status")) or episode["status"]
    episode["fingerprint"] = _clean(source.get("fingerprint")) or _make_episode_fingerprint(episode)
    episode["topic_fingerprint"] = _clean(source.get("topic_fingerprint")) or _make_episode_topic_fingerprint(episode)
    episode["consolidated_at"] = _clean(source.get("consolidated_at"))
    episode["merged_into"] = _clean(source.get("merged_into"))
    episode["conflict_group"] = _clean(source.get("conflict_group"))
    episode["conflict_reason"] = _clean(source.get("conflict_reason"))
    episode["links"] = _normalize_links(source.get("links"))

    if not episode["id"]:
        episode["id"] = _make_episode_id(episode)
    if not episode["day"]:
        episode["day"] = _timestamp_to_day(episode["timestamp"]) or fallback_day
    if not episode["summary"]:
        episode["summary"] = episode["detail"] or episode["kind"] or "episode"
    return episode


def normalize_daybook(raw: Any, *, fallback_date: str = "") -> dict[str, Any]:
    daybook = default_daybook(fallback_date)
    if not isinstance(raw, dict):
        return daybook

    source = raw.get("daybook") if isinstance(raw.get("daybook"), dict) else raw

    daybook["date"] = _clean(source.get("date")) or fallback_date or daybook["date"]
    daybook["generated_at"] = _clean(source.get("generated_at"))
    daybook["source"] = _clean(source.get("source")) or daybook["source"]
    daybook["episode_ids"] = _normalize_text_list(source.get("episode_ids"))
    daybook["summary"] = _clean(source.get("summary"))
    daybook["themes"] = _normalize_text_list(source.get("themes"))
    daybook["highlights"] = _normalize_mapping_list(source.get("highlights"))
    daybook["open_questions"] = _normalize_text_list(source.get("open_questions"))
    daybook["importance_cutoff"] = round(
        _clamp(source.get("importance_cutoff"), 0.0, 1.0, 0.65),
        3,
    )
    try:
        daybook["raw_entry_count"] = max(0, int(source.get("raw_entry_count", source.get("entry_count", 0))))
    except Exception:
        daybook["raw_entry_count"] = 0
    try:
        daybook["episode_count"] = max(0, int(source.get("episode_count", len(daybook["episode_ids"]))))
    except Exception:
        daybook["episode_count"] = len(daybook["episode_ids"])
    if not daybook["summary"] and daybook["highlights"]:
        first = daybook["highlights"][0]
        daybook["summary"] = _clean(first.get("summary"))
    return daybook


def save_episode(log_dir: str | None, episode: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_episode(dict(episode))
    if not normalized["id"]:
        normalized["id"] = _make_episode_id(normalized)
    _write_json(episode_path(log_dir, normalized["id"]), normalized)
    return normalized


def load_episode(log_dir: str | None, episode_id: str) -> dict[str, Any]:
    episode_id = _clean(episode_id)
    if not episode_id:
        return default_episode("")
    path = episode_path(log_dir, episode_id)
    if not os.path.exists(path):
        return default_episode(episode_id)
    data = _load_json(path, default_episode(episode_id))
    return normalize_episode(data, fallback_id=episode_id)


def list_episodes(
    log_dir: str | None,
    *,
    day: str | None = None,
    source: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    reverse: bool = True,
) -> list[dict[str, Any]]:
    dir_path = episodes_dir(log_dir)
    if not os.path.isdir(dir_path):
        return []

    day = _clean(day) if day is not None else ""
    source = _clean(source) if source is not None else ""
    kind = _clean(kind) if kind is not None else ""
    status = _clean(status) if status is not None else ""

    items: list[dict[str, Any]] = []
    for name in os.listdir(dir_path):
        if not name.endswith(".json"):
            continue
        path = os.path.join(dir_path, name)
        episode = normalize_episode(_load_json(path, {}), fallback_id=name[:-5])
        if day and episode["day"] != day:
            continue
        if source and episode["source"] != source:
            continue
        if kind and episode["kind"] != kind:
            continue
        if status and episode["status"] != status:
            continue
        items.append(episode)

    items.sort(key=lambda item: (item.get("timestamp", ""), item.get("id", "")))
    if reverse:
        items.reverse()
    items.sort(key=lambda item: _episode_status_rank(item.get("status")))
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def load_daybook(log_dir: str | None, date: str) -> dict[str, Any]:
    date = _clean(date)
    if not date:
        return default_daybook("")
    data = _load_json(daybook_path(log_dir, date), default_daybook(date))
    return normalize_daybook(data, fallback_date=date)


def daybook_exists(log_dir: str | None, date: str) -> bool:
    return os.path.exists(daybook_path(log_dir, date))


def save_daybook(log_dir: str | None, daybook: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_daybook(dict(daybook))
    if not normalized["date"]:
        normalized["date"] = _clean(daybook.get("date")) or _now().date().isoformat()
    _write_json(daybook_path(log_dir, normalized["date"]), normalized)
    return normalized


def build_daybook(
    log_dir: str | None,
    date: str,
    *,
    episodes: Iterable[Mapping[str, Any]] | None = None,
    episode_ids: Iterable[str] | None = None,
    summary: str = "",
    themes: Iterable[str] | None = None,
    highlights: Iterable[Mapping[str, Any]] | None = None,
    open_questions: Iterable[str] | None = None,
    importance_cutoff: float = 0.65,
    source: str = "watch",
    raw_entry_count: int | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create or reuse a daybook for one date.

    The function is idempotent by default: if a daybook already exists for the
    requested date, the stored record is returned as-is.
    """

    date = _clean(date)
    if not date:
        date = _now().date().isoformat()

    existing_path = daybook_path(log_dir, date)
    if os.path.exists(existing_path) and not overwrite:
        return load_daybook(log_dir, date)

    normalized_episodes = []
    if episodes is not None:
        normalized_episodes = [normalize_episode(ep) for ep in episodes]
    episode_id_list = _normalize_text_list(episode_ids or [ep["id"] for ep in normalized_episodes])
    summary = _clean(summary)
    if not summary and normalized_episodes:
        summary = _clean(normalized_episodes[0].get("summary"))
    if not summary and highlights:
        h_list = _normalize_mapping_list(list(highlights))
        if h_list:
            summary = _clean(h_list[0].get("summary"))

    daybook = default_daybook(date)
    daybook.update({
        "generated_at": _now().isoformat(timespec="seconds"),
        "source": _clean(source) or "watch",
        "episode_ids": episode_id_list,
        "summary": summary,
        "themes": _normalize_text_list(list(themes or [])),
        "highlights": _normalize_mapping_list(list(highlights or [])),
        "open_questions": _normalize_text_list(list(open_questions or [])),
        "importance_cutoff": round(_clamp(importance_cutoff, 0.0, 1.0, 0.65), 3),
        "raw_entry_count": max(0, int(raw_entry_count or len(episode_id_list))),
        "episode_count": len(episode_id_list),
    })
    return save_daybook(log_dir, daybook)


def list_daybooks(log_dir: str | None, *, limit: int | None = None, reverse: bool = True) -> list[dict[str, Any]]:
    dir_path = daybooks_dir(log_dir)
    if not os.path.isdir(dir_path):
        return []

    items: list[dict[str, Any]] = []
    for name in os.listdir(dir_path):
        if not name.endswith(".json"):
            continue
        path = os.path.join(dir_path, name)
        daybook = normalize_daybook(_load_json(path, {}), fallback_date=name[:-5])
        items.append(daybook)

    items.sort(key=lambda item: (item.get("date", ""), item.get("generated_at", "")))
    if reverse:
        items.reverse()
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def episode_brief(episode: Mapping[str, Any]) -> str:
    stamp = _clean(episode.get("timestamp"))
    stamp = stamp[:16] if stamp else _clean(episode.get("day"))
    kind = _clean(episode.get("kind")) or "observation"
    summary = _clean(episode.get("summary")) or "episode"
    tags = _normalize_text_list(episode.get("tags"))
    status = _clean(episode.get("status"))
    status_note = f"/{status}" if status and status != "canonical" else ""
    tag_text = f" | tags: {' / '.join(tags[:4])}" if tags else ""
    return f"- {stamp} | 【エピソード:{kind}{status_note}】{summary}{tag_text}"


def daybook_brief(daybook: Mapping[str, Any]) -> str:
    date = _clean(daybook.get("date"))
    summary = _clean(daybook.get("summary")) or "要約なし"
    themes = _normalize_text_list(daybook.get("themes"))
    if themes:
        theme_text = " / ".join(themes[:4])
        return f"- {date} | 【日記】{summary} | themes: {theme_text}"
    return f"- {date} | 【日記】{summary}"



def _make_causal_chain_id(cause_episode_id: str, effect_episode_id: str) -> str:
    basis = f"{_clean(cause_episode_id)}|{_clean(effect_episode_id)}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return f"cc_{digest}"


def default_causal_chain(
    chain_id: str = "",
    cause_episode_id: str = "",
    effect_episode_id: str = "",
) -> dict[str, Any]:
    return {
        "id": _clean(chain_id),
        "cause_episode_id": _clean(cause_episode_id),
        "effect_episode_id": _clean(effect_episode_id),
        "relation": "correlated",
        "summary": "",
        "mechanism": "",
        "confidence": 0.5,
        "tags": [],
        "support_episode_ids": [],
        "status": "canonical",
        "created_at": "",
        "day": "",
    }


def normalize_causal_chain(
    raw: Any,
    *,
    fallback_id: str = "",
    fallback_cause_episode_id: str = "",
    fallback_effect_episode_id: str = "",
) -> dict[str, Any]:
    chain = default_causal_chain(fallback_id, fallback_cause_episode_id, fallback_effect_episode_id)
    if not isinstance(raw, dict):
        return chain

    source = raw.get("causal_chain") if isinstance(raw.get("causal_chain"), dict) else raw

    chain["id"] = _clean(source.get("id") or source.get("causal_chain_id") or fallback_id)
    chain["cause_episode_id"] = _clean(source.get("cause_episode_id")) or _clean(fallback_cause_episode_id)
    chain["effect_episode_id"] = _clean(source.get("effect_episode_id")) or _clean(fallback_effect_episode_id)
    chain["relation"] = _normalize_causal_relation(source.get("relation"))
    chain["summary"] = _clean(source.get("summary"))
    chain["mechanism"] = _clean(source.get("mechanism"))
    chain["confidence"] = round(_clamp(source.get("confidence"), 0.0, 1.0, 0.5), 3)
    chain["tags"] = _normalize_text_list(source.get("tags"))
    support = _normalize_text_list(source.get("support_episode_ids"))
    for episode_id in (chain["cause_episode_id"], chain["effect_episode_id"]):
        if episode_id and episode_id not in support:
            support.append(episode_id)
    chain["support_episode_ids"] = support
    chain["status"] = _clean(source.get("status")) or chain["status"]
    chain["created_at"] = _clean(source.get("created_at"))
    if not chain["created_at"]:
        chain["created_at"] = _now().isoformat(timespec="seconds")
    chain["day"] = _clean(source.get("day")) or _timestamp_to_day(chain["created_at"])

    if chain["cause_episode_id"] and chain["effect_episode_id"]:
        chain["id"] = _make_causal_chain_id(chain["cause_episode_id"], chain["effect_episode_id"])
    return chain


def save_causal_chain(log_dir: str | None, causal_chain: Mapping[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
    normalized = normalize_causal_chain(dict(causal_chain))
    if not normalized["cause_episode_id"] or not normalized["effect_episode_id"]:
        raise ValueError("cause_episode_id と effect_episode_id が必要です")
    chain_id = normalized["id"] or _make_causal_chain_id(normalized["cause_episode_id"], normalized["effect_episode_id"])
    normalized["id"] = chain_id
    path = causal_chain_path(log_dir, chain_id)
    if os.path.exists(path) and not overwrite:
        return load_causal_chain(log_dir, chain_id)
    _write_json(path, normalized)
    return normalized


def load_causal_chain(
    log_dir: str | None,
    chain_id: str = "",
    *,
    cause_episode_id: str = "",
    effect_episode_id: str = "",
) -> dict[str, Any]:
    chain_id = _clean(chain_id)
    cause_episode_id = _clean(cause_episode_id)
    effect_episode_id = _clean(effect_episode_id)
    if not chain_id and cause_episode_id and effect_episode_id:
        chain_id = _make_causal_chain_id(cause_episode_id, effect_episode_id)
    if not chain_id:
        return default_causal_chain("", cause_episode_id, effect_episode_id)
    path = causal_chain_path(log_dir, chain_id)
    if not os.path.exists(path):
        return default_causal_chain(chain_id, cause_episode_id, effect_episode_id)
    data = _load_json(path, default_causal_chain(chain_id, cause_episode_id, effect_episode_id))
    return normalize_causal_chain(
        data,
        fallback_id=chain_id,
        fallback_cause_episode_id=cause_episode_id,
        fallback_effect_episode_id=effect_episode_id,
    )


def get_causal_chain(
    log_dir: str | None,
    *,
    chain_id: str = "",
    cause_episode_id: str = "",
    effect_episode_id: str = "",
) -> dict[str, Any]:
    return load_causal_chain(
        log_dir,
        chain_id,
        cause_episode_id=cause_episode_id,
        effect_episode_id=effect_episode_id,
    )


def list_causal_chains(
    log_dir: str | None,
    *,
    cause_episode_id: str | None = None,
    effect_episode_id: str | None = None,
    relation: str | None = None,
    limit: int | None = None,
    reverse: bool = True,
) -> list[dict[str, Any]]:
    dir_path = causal_chains_dir(log_dir)
    if not os.path.isdir(dir_path):
        return []

    cause_episode_id = _clean(cause_episode_id) if cause_episode_id is not None else ""
    effect_episode_id = _clean(effect_episode_id) if effect_episode_id is not None else ""
    relation = _normalize_causal_relation(relation) if relation is not None else ""

    items: list[dict[str, Any]] = []
    for name in os.listdir(dir_path):
        if not name.endswith(".json"):
            continue
        path = os.path.join(dir_path, name)
        chain = normalize_causal_chain(_load_json(path, {}), fallback_id=name[:-5])
        if cause_episode_id and chain["cause_episode_id"] != cause_episode_id:
            continue
        if effect_episode_id and chain["effect_episode_id"] != effect_episode_id:
            continue
        if relation and chain["relation"] != relation:
            continue
        items.append(chain)

    items.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
    if reverse:
        items.reverse()
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def causal_chain_brief(chain: Mapping[str, Any]) -> str:
    created = _clean(chain.get("created_at"))
    stamp = created[:16] if created else _clean(chain.get("day"))
    cause = _clean(chain.get("cause_episode_id")) or "?"
    effect = _clean(chain.get("effect_episode_id")) or "?"
    relation = _clean(chain.get("relation")) or "correlated"
    summary = _clean(chain.get("summary")) or "因果メモ"
    mechanism = _clean(chain.get("mechanism"))
    line = f"- {stamp} | 【因果】{cause} -> {effect} [{relation}] {summary}"
    if mechanism:
        line += f" | mechanism: {mechanism}"
    return line



def _episode_status_rank(status: Any) -> int:
    return {"canonical": 0, "conflict": 1, "superseded": 2}.get(_clean(status), 3)


def _compact_text(value: Any) -> str:
    text = _clean(value).lower()
    if not text:
        return ""
    for pattern in [
        r"ついていた",
        r"ついてる",
        r"点いていた",
        r"点いてる",
        r"消えていた",
        r"消えてる",
        r"開いていた",
        r"開いてる",
        r"閉まっていた",
        r"閉まってる",
        r"入っていた",
        r"入ってる",
        r"切れていた",
        r"切れてる",
        r"止まっていた",
        r"止まってる",
        r"上がっていた",
        r"上がってる",
        r"下がっていた",
        r"下がってる",
        r"増えていた",
        r"増えてる",
        r"減っていた",
        r"減ってる",
        r"\bon\b",
        r"\boff\b",
        r"\btrue\b",
        r"\bfalse\b",
        r"\benabled\b",
        r"\bdisabled\b",
        r"ない",
        r"なかった",
        r"ではない",
        r"じゃない",
        r"不在",
        r"失敗",
        r"成功",
        r"未",
    ]:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^0-9A-Za-z_\u3040-\u30ff\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


def _episode_fingerprint_basis(episode: Mapping[str, Any]) -> str:
    return "|".join([
        _clean(episode.get("kind")),
        _clean(episode.get("source")),
        _clean(episode.get("summary")),
        _clean(episode.get("detail")),
    ])


def _episode_topic_fingerprint_basis(episode: Mapping[str, Any]) -> str:
    parts = [
        _clean(episode.get("kind")),
        _clean(episode.get("source")),
        _compact_text(f"{episode.get('summary', '')} {episode.get('detail', '')}"),
        " ".join(_normalize_text_list(episode.get("tags"))),
        " ".join(_normalize_text_list(episode.get("entities"))),
        " ".join(_normalize_text_list(episode.get("actors"))),
    ]
    return "|".join(part for part in parts if part)


def _make_episode_fingerprint(episode: Mapping[str, Any]) -> str:
    digest = hashlib.sha1(_episode_fingerprint_basis(episode).encode("utf-8")).hexdigest()[:14]
    return f"ef_{digest}"


def _make_episode_topic_fingerprint(episode: Mapping[str, Any]) -> str:
    digest = hashlib.sha1(_episode_topic_fingerprint_basis(episode).encode("utf-8")).hexdigest()[:14]
    return f"tf_{digest}"


def _best_episode_candidate(episodes: list[Mapping[str, Any]]) -> dict[str, Any]:
    return dict(min(episodes, key=lambda item: (
        _episode_status_rank(item.get("status")),
        -_clamp(item.get("importance"), 0.0, 1.0, 0.5),
        -len(item.get("evidence") or []),
        _clean(item.get("timestamp")) or _clean(item.get("day")),
        _clean(item.get("id")),
    )))


def _merge_unique_dicts(values: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for key, value in item.items():
            if isinstance(value, list):
                row[key] = _unique_list(value)
            else:
                text = _clean(value)
                if text:
                    row[key] = text
        if not row:
            continue
        sig = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(row)
    return out


def _merge_episode_group(episodes: list[dict[str, Any]], canonical: dict[str, Any], now: str, fingerprint: str) -> dict[str, Any]:
    canonical = dict(canonical)
    canonical["status"] = "canonical"
    canonical["fingerprint"] = fingerprint
    canonical["topic_fingerprint"] = _make_episode_topic_fingerprint(canonical)
    canonical["consolidated_at"] = now
    canonical["merged_into"] = ""
    canonical["conflict_group"] = _clean(canonical.get("conflict_group"))
    canonical["conflict_reason"] = _clean(canonical.get("conflict_reason"))

    combined_evidence: list[dict[str, Any]] = []
    combined_tags: list[str] = []
    combined_entities: list[str] = []
    combined_actors: list[str] = []
    combined_causes: list[str] = []
    combined_effects: list[str] = []
    max_importance = _clamp(canonical.get("importance"), 0.0, 1.0, 0.5)
    for item in episodes:
        combined_evidence.extend(_merge_unique_dicts(item.get("evidence") or []))
        combined_tags.extend(_normalize_text_list(item.get("tags")))
        combined_entities.extend(_normalize_text_list(item.get("entities")))
        combined_actors.extend(_normalize_text_list(item.get("actors")))
        links = item.get("links") if isinstance(item.get("links"), dict) else {}
        combined_causes.extend(_normalize_text_list(links.get("causes")))
        combined_effects.extend(_normalize_text_list(links.get("effects")))
        max_importance = max(max_importance, _clamp(item.get("importance"), 0.0, 1.0, 0.5))

    if combined_evidence:
        canonical["evidence"] = combined_evidence
    canonical["tags"] = _normalize_text_list(combined_tags)
    canonical["entities"] = _normalize_text_list(combined_entities)
    canonical["actors"] = _normalize_text_list(combined_actors)
    canonical["links"] = {"causes": _normalize_text_list(combined_causes), "effects": _normalize_text_list(combined_effects)}
    canonical["importance"] = round(max_importance, 3)
    return canonical


def _report_core(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(report).items()
        if key != "updated_at"
    }


def _episode_report_relpath(log_dir: str | None, episode_id: str) -> str:
    return os.path.relpath(episode_path(log_dir, episode_id), log_dir or _DEFAULT_LOG_DIR)


def _topic_label(episode: Mapping[str, Any]) -> str:
    text = _compact_text(f"{episode.get('summary', '')} {episode.get('detail', '')}")
    if text:
        return text[:80]
    return _clean(episode.get("summary")) or _clean(episode.get("kind")) or "topic"


def consolidate_memory(
    log_dir: str | None,
    *,
    scope: str = "",
    day: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    scope_value = _clean(scope) or _clean(day) or "all"
    report_path = consolidation_report_path(log_dir, scope_value)
    existing_report = _load_json(report_path, {})
    now = _now().isoformat(timespec="seconds")

    day_filter = _clean(day)
    if not day_filter and re.fullmatch(r"\d{4}-\d{2}-\d{2}", scope_value):
        day_filter = scope_value
    episodes = list_episodes(log_dir, day=day_filter or None, reverse=False)
    if not episodes:
        report = {
            "run_id": f"cons_{_slug(scope_value, 'all')}",
            "scope": scope_value,
            "merged_episode_ids": [],
            "superseded_episode_ids": [],
            "canonical_episode_ids": [],
            "conflict_groups": [],
            "updated_files": [],
            "episode_count": 0,
            "updated_at": now,
        }
        if not overwrite and existing_report and _report_core(existing_report) == _report_core(report):
            return existing_report if isinstance(existing_report, dict) else report
        _write_json(report_path, report)
        return report

    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    by_topic: dict[str, list[dict[str, Any]]] = {}
    changed_ids: set[str] = set()

    for episode in episodes:
        fingerprint = _make_episode_fingerprint(episode)
        topic_fingerprint = _make_episode_topic_fingerprint(episode)
        if episode.get("fingerprint") != fingerprint:
            episode["fingerprint"] = fingerprint
            changed_ids.add(_clean(episode.get("id")))
        if episode.get("topic_fingerprint") != topic_fingerprint:
            episode["topic_fingerprint"] = topic_fingerprint
            changed_ids.add(_clean(episode.get("id")))
        by_fingerprint.setdefault(fingerprint, []).append(episode)
        by_topic.setdefault(topic_fingerprint, []).append(episode)

    merged_episode_ids: list[str] = []
    superseded_episode_ids: list[str] = []
    canonical_episode_ids: list[str] = []

    for fingerprint in sorted(by_fingerprint):
        group = by_fingerprint[fingerprint]
        if len(group) <= 1:
            continue
        canonical = _best_episode_candidate(group)
        canonical_id = _clean(canonical.get("id"))
        merged = _merge_episode_group(group, canonical, now, fingerprint)
        for episode in group:
            episode_id = _clean(episode.get("id"))
            if episode_id == canonical_id:
                if episode != merged:
                    episode.update(merged)
                    changed_ids.add(episode_id)
                continue
            episode["status"] = "superseded"
            episode["merged_into"] = canonical_id
            episode["fingerprint"] = fingerprint
            episode["consolidated_at"] = now
            episode["conflict_reason"] = _clean(episode.get("conflict_reason"))
            changed_ids.add(episode_id)
            superseded_episode_ids.append(episode_id)
        canonical_episode_ids.append(canonical_id)
        merged_episode_ids.extend(sorted({_clean(item.get("id")) for item in group if _clean(item.get("id"))}))

    conflict_groups: list[dict[str, Any]] = []
    for topic_fingerprint in sorted(by_topic):
        group = by_topic[topic_fingerprint]
        surviving = [episode for episode in group if _clean(episode.get("status")) != "superseded"]
        unique_fingerprints = sorted({ _clean(episode.get("fingerprint")) for episode in surviving if _clean(episode.get("fingerprint")) })
        if len(unique_fingerprints) <= 1:
            continue
        representative = _best_episode_candidate(surviving)
        representative_id = _clean(representative.get("id"))
        label = _topic_label(representative)
        conflict_ids = sorted({_clean(item.get("id")) for item in surviving if _clean(item.get("id"))})
        conflict_groups.append({
            "topic": label,
            "topic_fingerprint": topic_fingerprint,
            "episodes": conflict_ids,
            "resolution": "both_kept",
        })
        for episode in surviving:
            episode_id = _clean(episode.get("id"))
            if episode_id == representative_id:
                if episode.get("status") != "canonical" or episode.get("conflict_group") != topic_fingerprint or episode.get("conflict_reason") != "same_topic_variants":
                    episode["status"] = "canonical"
                    episode["conflict_group"] = topic_fingerprint
                    episode["conflict_reason"] = "same_topic_variants"
                    episode["consolidated_at"] = now
                    changed_ids.add(episode_id)
                continue
            if episode.get("status") != "conflict" or episode.get("conflict_group") != topic_fingerprint or episode.get("conflict_reason") != "same_topic_variants":
                episode["status"] = "conflict"
                episode["conflict_group"] = topic_fingerprint
                episode["conflict_reason"] = "same_topic_variants"
                episode["consolidated_at"] = now
                changed_ids.add(episode_id)

    updated_files: list[str] = []
    for episode in episodes:
        episode_id = _clean(episode.get("id"))
        if not episode_id or episode_id not in changed_ids:
            continue
        _write_json(episode_path(log_dir, episode_id), episode)
        updated_files.append(_episode_report_relpath(log_dir, episode_id))

    updated_files = sorted(dict.fromkeys(updated_files))
    merged_episode_ids = sorted(dict.fromkeys(merged_episode_ids))
    superseded_episode_ids = sorted(dict.fromkeys(superseded_episode_ids))
    canonical_episode_ids = sorted(dict.fromkeys(canonical_episode_ids))
    conflict_groups = sorted(conflict_groups, key=lambda item: (item.get("topic_fingerprint", ""), ",".join(item.get("episodes", []))))

    report = {
        "run_id": f"cons_{_slug(scope_value, 'all')}",
        "scope": scope_value,
        "merged_episode_ids": merged_episode_ids,
        "superseded_episode_ids": superseded_episode_ids,
        "canonical_episode_ids": canonical_episode_ids,
        "conflict_groups": conflict_groups,
        "updated_files": updated_files,
        "episode_count": len(episodes),
        "updated_at": now,
    }
    report["updated_files"].append(os.path.relpath(report_path, log_dir or _DEFAULT_LOG_DIR))
    report["updated_files"] = sorted(dict.fromkeys(report["updated_files"]))

    if not overwrite and existing_report and _report_core(existing_report) == _report_core(report):
        return existing_report if isinstance(existing_report, dict) else report

    _write_json(report_path, report)
    return report
