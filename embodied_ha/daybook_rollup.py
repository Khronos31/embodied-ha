#!/usr/bin/env python3
"""loop.sh から呼ばれる structured daybook 生成ヘルパー。

環境変数で入力を受け取り、前日の観察ログを episode/daybook に圧縮して保存する。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import uuid
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import memory_state as ms  # noqa: E402
import counterfactual_state as cs  # noqa: E402
from state_utils import file_lock  # noqa: E402


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _short(value: Any, limit: int = 64) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _ensure_memory_seed(path: str) -> str:
    content = _read_text(path)
    if content.strip():
        return content
    seed = "## コア記憶\n\n（まだ蓄積されていません）\n\n---\n\n## 最近の気づき\n\n"
    _write_text(path, seed)
    return seed


def _append_memory_brief(path: str, brief: str) -> bool:
    with file_lock(path):
        content = _ensure_memory_seed(path)
        if brief in content:
            return False
        if not content.endswith("\n"):
            content += "\n"
        content += f"{brief}\n"
        _write_text(path, content)
        return True


def _write_marker(path: str, value: str) -> None:
    _write_text(path, value)


def _parse_json_payload(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?\s*|```", "", text).strip()
    if not cleaned:
        return {}
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _listify(values: Any) -> list[str]:
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


def _build_raw_episode(day: str, entry: dict[str, Any], index: int) -> dict[str, Any] | None:
    timestamp = _clean(entry.get("timestamp"))
    private = _clean(entry.get("private"))
    speak = _clean(entry.get("speak"))
    emotion = _clean(entry.get("emotion"))
    if not private and not speak:
        return None

    summary = private or speak or f"{day} の出来事"
    detail_parts = []
    if private:
        detail_parts.append(private)
    if speak:
        detail_parts.append(f"発話: {speak}")
    if emotion:
        detail_parts.append(f"emotion: {emotion}")

    importance = 0.48
    if speak:
        importance += 0.12
    if emotion and emotion.lower() not in {"", "none", "normal"}:
        importance += 0.08
    if len(summary) > 80:
        importance += 0.05

    return {
        "timestamp": timestamp,
        "day": day,
        "kind": "observation",
        "source": "loop",
        "summary": _short(summary, 96),
        "detail": " / ".join(detail_parts),
        "tags": [tag for tag in [emotion, "speak" if speak else ""] if tag],
        "entities": [],
        "actors": [],
        "importance": max(0.0, min(1.0, round(importance, 3))),
        "evidence": [
            {
                "timestamp": timestamp,
                "emotion": emotion,
                "private": private,
                "speak": speak,
                "index": index,
            }
        ],
        "status": "canonical",
        "links": {"causes": [], "effects": []},
    }


def _fallback_draft(day: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    snippets: list[str] = []
    has_speak = False
    has_emotion = False
    episodes: list[dict[str, Any]] = []

    for index, entry in enumerate(entries):
        built = _build_raw_episode(day, entry, index)
        if built:
            episodes.append(built)
        text = _clean(entry.get("private")) or _clean(entry.get("speak"))
        if text:
            snippets.append(text)
        if _clean(entry.get("speak")):
            has_speak = True
        if _clean(entry.get("emotion")):
            has_emotion = True

    summary = f"{day} の観察を {len(entries)} 件記録"
    if snippets:
        summary = " / ".join(_short(text, 48) for text in snippets[:2])

    themes = ["観察"]
    if has_speak:
        themes.insert(0, "会話")
    if has_emotion:
        themes.append("感情")

    highlights: list[dict[str, Any]] = []
    if snippets:
        highlights.append({"summary": _short(snippets[0], 72), "importance": 0.5})

    return {
        "summary": summary,
        "themes": themes,
        "highlights": highlights,
        "open_questions": [],
        "episodes": episodes,
    }


def _summarize_with_claude(day: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    character = os.environ.get("CHARACTER", "").strip()
    resident = os.environ.get("RESIDENT", "ユーザー")

    lines: list[str] = []
    for item in entries:
        ts = _clean(item.get("timestamp"))
        emo = _clean(item.get("emotion"))
        obs = _clean(item.get("private"))
        spk = _clean(item.get("speak"))
        line = f"- {ts[11:16] if len(ts) >= 16 else ts} [{emo}] {obs}"
        if spk:
            line += f" → 発話: {spk}"
        lines.append(line)

    prompt = (character + "\n\n") if character else ""
    prompt += f"{day} の観察ログをもとに structured な日次メモを作ってください。\n\n"
    prompt += f"対象の一日は {resident} さんの暮らしを観察した記録です。\n"
    prompt += "出力は JSON のみ。前置き・後書き・コードフェンスは禁止。\n\n"
    prompt += "JSON の形は次の通りです:\n"
    prompt += "{\n"
    prompt += '  "summary": "1〜3文の要約",\n'
    prompt += '  "themes": ["主題", "主題"],\n'
    prompt += '  "highlights": [{"summary": "...", "detail": "...", "tags": ["..."], "importance": 0.0}],\n'
    prompt += '  "open_questions": ["..."],\n'
    prompt += '  "episodes": [{"timestamp": "...", "kind": "observation", "source": "loop", "summary": "...", "detail": "...", "tags": ["..."], "entities": ["..."], "actors": ["..."], "importance": 0.0, "evidence": [{"timestamp": "...", "private": "..."}], "status": "canonical", "links": {"causes": [], "effects": []}}]\n'
    prompt += "}\n\n"
    prompt += "制約:\n"
    prompt += "- episodes は出来事単位にまとめる\n"
    prompt += "- highlights は最大5件\n"
    prompt += "- summary は日全体の見取り図にする\n"
    prompt += "- 可能なら episodes は 1〜8 件程度に圧縮する\n\n"
    prompt += "観察ログ:\n" + "\n".join(lines)

    claude = os.environ.get("CLAUDE_BIN", "/config/.tools/npm-global/bin/claude")
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
        "PATH": os.environ.get("EHA_TOOLS_PATH", "/config/.tools/npm-global/bin:/config/.tools/node/bin")
        + ":"
        + os.environ.get("PATH", "/usr/bin:/bin"),
    }
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
    proc = subprocess.run(
        [claude, "-p", "--model", "sonnet", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
        input=msg,
        capture_output=True,
        text=True,
        cwd=os.environ.get("EHA_CLAUDE_CWD") or os.environ.get("SCRIPT_DIR", SCRIPT_DIR),
        env=env,
    )
    raw = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if data.get("type") == "result":
            raw = data.get("result", "").strip()
            break
    return _parse_json_payload(raw)


def _normalize_draft(day: str, entries: list[dict[str, Any]], draft: dict[str, Any]) -> dict[str, Any]:
    fallback = _fallback_draft(day, entries)
    summary = _clean(draft.get("summary")) or fallback["summary"]
    themes = _listify(draft.get("themes")) or fallback["themes"]
    open_questions = _listify(draft.get("open_questions")) or fallback["open_questions"]

    highlights_raw = draft.get("highlights") if isinstance(draft.get("highlights"), list) else []
    highlights: list[dict[str, Any]] = []
    for item in highlights_raw:
        if isinstance(item, dict):
            highlights.append(item)
        else:
            text = _clean(item)
            if text:
                highlights.append({"summary": text})
    if not highlights:
        highlights = fallback["highlights"]

    episodes_raw = draft.get("episodes") if isinstance(draft.get("episodes"), list) else []
    episodes: list[dict[str, Any]] = []
    for item in episodes_raw:
        if isinstance(item, dict):
            episodes.append(item)
    if not episodes:
        episodes = fallback["episodes"]

    return {
        "summary": summary,
        "themes": themes,
        "highlights": highlights,
        "open_questions": open_questions,
        "episodes": episodes,
    }


def _save_episodes(log_dir: str, day: str, draft: dict[str, Any], entries: list[dict[str, Any]]) -> list[str]:
    saved_ids: list[str] = []
    for index, episode in enumerate(draft.get("episodes") or []):
        if not isinstance(episode, dict):
            continue
        payload = dict(episode)
        payload.setdefault("day", day)
        payload.setdefault("source", "loop")
        payload.setdefault("kind", "observation")
        payload.setdefault("timestamp", payload.get("timestamp") or f"{day}T00:00:00+09:00")
        saved = ms.save_episode(log_dir, payload)
        saved_ids.append(saved["id"])

    if saved_ids:
        return saved_ids

    for index, entry in enumerate(entries):
        built = _build_raw_episode(day, entry, index)
        if not built:
            continue
        saved = ms.save_episode(log_dir, built)
        saved_ids.append(saved["id"])
    return saved_ids


def _write_daybook(log_dir: str, memory_file: str, day: str, draft: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    episode_ids = _save_episodes(log_dir, day, draft, entries)
    counterfactual_line = cs.counterfactual_sentence(cs.best_recent_counterfactual(log_dir, hours=24))
    summary = draft["summary"]
    highlights = list(draft["highlights"])
    if counterfactual_line and counterfactual_line not in summary:
        summary = f"{summary} / {counterfactual_line}" if summary else counterfactual_line
        highlights.append({"summary": counterfactual_line, "importance": 0.55, "tags": ["counterfactual"]})
    daybook = ms.build_daybook(
        log_dir,
        day,
        episode_ids=episode_ids,
        summary=summary,
        themes=draft["themes"],
        highlights=highlights,
        open_questions=draft["open_questions"],
        raw_entry_count=len(entries),
        source="loop",
    )
    brief = ms.daybook_brief(daybook)
    if _append_memory_brief(memory_file, brief):
        print(f"[DAYBOOK] {day} 記録完了: {daybook.get('summary', '')[:40]}...")
    else:
        print(f"[DAYBOOK] {day} 記録完了（既存ブリーフ再利用）: {daybook.get('summary', '')[:40]}...")


def _maybe_consolidate(log_dir: str, scope: str, day: str | None = None) -> None:
    if not _truthy(os.environ.get("CONSOLIDATE_MEMORY")):
        return
    try:
        report = ms.consolidate_memory(log_dir, scope=scope, day=day or scope)
    except Exception as e:
        print(f"[DAYBOOK] consolidation error: {e}")
        return
    print(
        f"[DAYBOOK] consolidation done: {scope} merged={len(report.get('superseded_episode_ids', []))} conflicts={len(report.get('conflict_groups', []))}"
    )


def main() -> None:
    log_file = os.environ["LOG_FILE"]
    memory_file = os.environ["MEMORY_FILE"]
    today = os.environ["TODAY"]
    daybook_marker = os.environ["DAYBOOK_MARKER"]
    last_daybook = os.environ.get("LAST_DAYBOOK", "").strip()
    log_dir = os.path.dirname(memory_file)

    today_d = dt.date.fromisoformat(today)
    yesterday_d = today_d - dt.timedelta(days=1)

    if last_daybook:
        try:
            start_d = dt.date.fromisoformat(last_daybook) + dt.timedelta(days=1)
        except Exception:
            start_d = yesterday_d
    else:
        start_d = yesterday_d
    if start_d > yesterday_d:
        _write_marker(daybook_marker, today)
        raise SystemExit(0)

    max_days = 7
    span = (yesterday_d - start_d).days + 1
    if span > max_days:
        print(f"[DAYBOOK] {span - max_days}日分が古すぎるためスキップ")
        start_d = yesterday_d - dt.timedelta(days=max_days - 1)

    target_dates: list[str] = []
    dd = start_d
    while dd <= yesterday_d:
        target_dates.append(dd.isoformat())
        dd += dt.timedelta(days=1)

    entries_by_day = {d: [] for d in target_dates}
    seen_entries: set[tuple[str, str]] = set()

    def add_entry(row: dict[str, Any]) -> None:
        ts = _clean(row.get("timestamp"))
        day = ts[:10]
        if day not in entries_by_day:
            return
        private = _clean(row.get("private"))
        key = (ts, private)
        if key in seen_entries:
            return
        seen_entries.add(key)
        entries_by_day[day].append(
            {
                "timestamp": ts,
                "emotion": _clean(row.get("emotion")),
                "private": private,
                "speak": _clean(row.get("speak")),
            }
        )

    def read_observation_log(path: str, *, optional: bool = False) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(row, dict):
                        add_entry(row)
        except FileNotFoundError:
            if not optional:
                raise

    read_observation_log(log_file)
    recovered_log_file = os.path.join(os.path.dirname(log_file) or log_dir, "observations_recovered.jsonl")
    if os.path.abspath(recovered_log_file) != os.path.abspath(log_file):
        read_observation_log(recovered_log_file, optional=True)

    target_day = next((d for d in target_dates if entries_by_day.get(d)), None)
    new_marker = None
    if target_day is None:
        new_marker = today
    else:
        if ms.daybook_exists(log_dir, target_day):
            daybook = ms.load_daybook(log_dir, target_day)
            brief = ms.daybook_brief(daybook)
            if _append_memory_brief(memory_file, brief):
                print(f"[DAYBOOK] 既存の structured daybook を反映: {target_day}")
            else:
                print(f"[DAYBOOK] 既存の structured daybook を再利用: {target_day}")
            _maybe_consolidate(log_dir, target_day, target_day)
            new_marker = today if target_day == yesterday_d.isoformat() else target_day
        else:
            draft = _summarize_with_claude(target_day, entries_by_day[target_day])
            if not draft:
                draft = _fallback_draft(target_day, entries_by_day[target_day])
            normalized = _normalize_draft(target_day, entries_by_day[target_day], draft)
            _write_daybook(log_dir, memory_file, target_day, normalized, entries_by_day[target_day])
            _maybe_consolidate(log_dir, target_day, target_day)
            new_marker = today if target_day == yesterday_d.isoformat() else target_day

    if new_marker:
        _write_marker(daybook_marker, new_marker)


if __name__ == "__main__":
    main()
