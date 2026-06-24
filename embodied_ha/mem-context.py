#!/usr/bin/env python3
"""structured daybook + consolidated episodes + memory.md を LLM 送信用に整形する。

- daybook があれば先頭に置く
- その後に統合済み episode を続ける
- その後に memory.md のコア記憶 + 最近の気づきを続ける
- memory.md が無くても daybook と episode だけは出せる

使い方: mem-context.py <memory.md path> [N=40]
"""
from __future__ import annotations

import os
import sys
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import memory_state as ms  # noqa: E402


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _legacy_memory_text(content: str, n: int) -> str:
    if not content:
        return ""

    MARKER = "## 最近の気づき"
    if MARKER in content:
        core, recent = content.split(MARKER, 1)
    else:
        core, recent = content, ""
    core = core.rstrip()
    if core.endswith("---"):
        core = core[:-3].rstrip()

    recent_entries = [ln for ln in recent.splitlines() if ln.strip().startswith("-")]
    kept = recent_entries[-n:] if len(recent_entries) > n else recent_entries

    out = core.rstrip()
    if kept:
        omitted = len(recent_entries) - len(kept)
        note = f"（古い{omitted}件は省略。コア記憶に要約済み）\n" if omitted > 0 else ""
        out += "\n\n---\n\n## 最近の気づき\n\n" + note + "\n".join(kept)
    return out.strip()


def _daybook_section(log_dir: str, limit: int = 3) -> str:
    daybooks = ms.list_daybooks(log_dir, limit=limit) if log_dir else []
    if not daybooks:
        return ""

    lines = ["## 日次サマリー"]
    for daybook in daybooks:
        date = _clean(daybook.get("date")) or "(日付不明)"
        summary = _clean(daybook.get("summary")) or "要約なし"
        themes = [_clean(item) for item in (daybook.get("themes") or []) if _clean(item)]
        header = f"- {date} | {summary}"
        if themes:
            header += f" | themes: {' / '.join(themes[:4])}"
        lines.append(header)

        highlights = daybook.get("highlights") or []
        for item in highlights[:3]:
            if isinstance(item, dict):
                text = _clean(item.get("summary") or item.get("text"))
            else:
                text = _clean(item)
            if text:
                lines.append(f"  - {text}")

        open_questions = [_clean(item) for item in (daybook.get("open_questions") or []) if _clean(item)]
        if open_questions:
            lines.append(f"  - open: {' / '.join(open_questions[:3])}")
    return "\n".join(lines)


def _episode_section(log_dir: str, limit: int = 12) -> str:
    canonical = ms.list_episodes(log_dir, status="canonical", limit=limit, reverse=True) if log_dir else []
    conflict_limit = max(4, min(8, limit // 2 if limit else 4))
    conflict = ms.list_episodes(log_dir, status="conflict", limit=conflict_limit, reverse=True) if log_dir else []
    if not canonical and not conflict:
        return ""

    lines = ["## 統合済みエピソード"]
    for episode in canonical:
        lines.append(ms.episode_brief(episode))

    if conflict:
        lines.append("## conflict episodes")
        for episode in conflict:
            lines.append(ms.episode_brief(episode))
    return "\n".join(lines)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    log_dir = os.path.dirname(path) if path else os.environ.get("EHA_LOG_DIR", "")

    daybooks = _daybook_section(log_dir, limit=3) if log_dir else ""
    episodes = _episode_section(log_dir, limit=min(12, max(6, n // 2))) if log_dir else ""
    legacy = _legacy_memory_text(_read_text(path), n) if path else ""

    sections = [section for section in (daybooks, episodes, legacy) if section and section.strip()]
    if not sections:
        print("なし")
        return
    print("\n\n---\n\n".join(sections))


if __name__ == "__main__":
    main()
