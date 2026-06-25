#!/usr/bin/env python3
"""Helpers for persistent auditory perception events."""

from __future__ import annotations

import json
import os
import threading
from datetime import timedelta

from state_utils import clean, now, parse_ts

DEFAULT_AUDITORY_EVENTS_FILE = "/data/embodied-ha/log/auditory_events.jsonl"
_AUDITORY_EVENTS_LOCK = threading.Lock()


def default_auditory_events_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "auditory_events.jsonl")
    return "/config/embodied-ha/log/auditory_events.jsonl"


def auditory_events_path() -> str:
    return (
        clean(os.environ.get("EHA_AUDITORY_EVENTS_FILE"))
        or default_auditory_events_path()
        or DEFAULT_AUDITORY_EVENTS_FILE
    )


def append_auditory_event(
    entry: dict,
    retention_hours: int | None = None,
    source_label: str | None = None,
) -> None:
    path = auditory_events_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    with _AUDITORY_EVENTS_LOCK:
        if retention_hours is None:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return

        try:
            retention = int(retention_hours)
        except Exception:
            retention = 1
        if retention <= 0:
            return

        source = clean(source_label) or clean(entry.get("source"))
        cutoff = now() - timedelta(hours=retention)
        entries: list[dict] = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            parsed = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        ts = parse_ts(parsed.get("timestamp"))
                        if source and clean(parsed.get("source")) == source and ts and ts < cutoff:
                            continue
                        entries.append(parsed)
            except Exception:
                entries = []
        entries.append(entry)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)


def load_recent_auditory_events(user_msg: str, limit: int = 3) -> list[dict]:
    path = auditory_events_path()
    if not os.path.exists(path):
        return []

    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
    except Exception:
        return []

    if not entries:
        return []

    normalized_user_msg = clean(user_msg)
    selected: list[dict] = []
    if normalized_user_msg:
        for entry in reversed(entries):
            if clean(entry.get("transcript")) == normalized_user_msg:
                selected.append(entry)
                if len(selected) >= max(1, limit):
                    break

    if not selected:
        for entry in reversed(entries):
            selected.append(entry)
            if len(selected) >= max(1, limit):
                break

    selected.reverse()
    return selected


def _format_metric(value: object, digits: int = 1) -> str | None:
    try:
        number = float(value)
    except Exception:
        return None
    formatted = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return formatted or "0"


def format_recent_auditory_prompt(user_msg: str, limit: int = 3) -> str:
    events = load_recent_auditory_events(user_msg, limit=limit)
    if not events:
        return ""

    lines = [
        "# 直近の聴覚入力",
        "これはテキストチャットではなく、部屋の音声入力からSTTされた発話です。",
    ]
    for event in events:
        timestamp = clean(event.get("timestamp")) or "不明"
        source = clean(event.get("source")) or clean(event.get("origin")) or "不明"
        origin = clean(event.get("origin"))
        if origin and origin != source:
            source = f"{source} ({origin})"
        speaker_hint = clean(event.get("speaker_hint")) or "unknown"
        transcript = clean(event.get("transcript")) or "（なし）"
        duration = _format_metric(event.get("duration_sec"))
        peak_db = _format_metric(event.get("peak_db"))
        speech_ratio = _format_metric(event.get("speech_ratio"), digits=2)
        feature_parts: list[str] = []
        if duration is not None:
            feature_parts.append(f"duration={duration}s")
        if peak_db is not None:
            feature_parts.append(f"peak={peak_db}dB")
        if speech_ratio is not None:
            feature_parts.append(f"speech_ratio={speech_ratio}")
        feature_text = ", ".join(feature_parts) if feature_parts else "なし"
        lines.extend(
            [
                f"- 時刻: {timestamp}",
                f"- 音源: {source}",
                f"- 話者推定: {speaker_hint}",
                f'- 内容: 「{transcript}」',
                f"- 音声特徴: {feature_text}",
            ]
        )
    return "\n".join(lines)
