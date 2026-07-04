#!/usr/bin/env python3
"""chat_log.jsonl から今日の直近会話より前の文脈を圧縮して返す。"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\r", " ").replace("\n", " ").strip()


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def _load_today_entries(log_path: str) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    entries: list[dict[str, Any]] = []

    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            timestamp = row.get("timestamp")
            if not isinstance(timestamp, str) or len(timestamp) < 10:
                continue
            if timestamp[:10] != today:
                continue
            try:
                datetime.fromisoformat(timestamp)
            except Exception:
                continue
            entries.append(row)

    return entries


def format_earlier_today_chat(
    log_path: str,
    resident: str,
    tail_n: int = 10,
    max_chars: int = 80,
    character_name: str = "エージェント",
) -> str:
    try:
        if not log_path:
            return ""

        entries = _load_today_entries(log_path)
        tail_n = max(0, tail_n)
        if len(entries) <= tail_n:
            return ""

        target = entries[: len(entries) - tail_n]
        lines = ["（今日の会話・それ以前）"]
        for row in target:
            timestamp = row.get("timestamp", "")
            hhmm = timestamp[11:16] if isinstance(timestamp, str) and len(timestamp) >= 16 else "--:--"

            user = _truncate(_clean_text(row.get("user")), max_chars)
            claude = _truncate(_clean_text(row.get("claude")), max_chars)

            if user:
                lines.append(f'{hhmm} {resident}さん: 「{user}」')
            if claude:
                lines.append(f"{hhmm} {character_name}: {claude}")

        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception:
        return ""


if __name__ == "__main__":
    log_dir = os.environ.get("EHA_LOG_DIR") or os.environ.get("LOG_DIR", "")
    log_path = os.path.join(log_dir, "chat_log.jsonl") if log_dir else ""
    resident = os.environ.get("RESIDENT", "ユーザー")
    character_name = os.environ.get("EHA_CHARACTER_NAME") or "エージェント"
    print(format_earlier_today_chat(log_path, resident, character_name=character_name))
