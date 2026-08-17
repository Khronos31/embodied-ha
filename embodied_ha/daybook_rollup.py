#!/usr/bin/env python3
"""loop.py から呼ばれる structured daybook 生成ヘルパー。

環境変数で入力を受け取り、前日の観察ログを episode/daybook に圧縮して保存する。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import memory_state as ms  # noqa: E402
import counterfactual_state as cs  # noqa: E402
from introspection_facts import format_facts_summary  # noqa: E402
from json_schemas import daybook_schema  # noqa: E402
from path_env import build_tools_path  # noqa: E402
from state_utils import file_lock  # noqa: E402


DAYBOOK_AGENT_TIMEOUT_SECONDS = 300
DAYBOOK_AGENT_KILL_GRACE_SECONDS = 2
_STAGE_FORMAT_VERSION = 1
_STAGE_CONTEXT_ENV_KEYS = (
    "EHA_AGENT_HARNESS",
    "EHA_CLAUDE_MODEL_DEFAULT",
    "EHA_CLAUDE_EFFORT_DEFAULT",
    "EHA_CODEX_MODEL_DEFAULT",
    "EHA_CODEX_REASONING_EFFORT_DEFAULT",
    "EHA_AGY_MODEL_DEFAULT",
)


class DaybookAgentError(RuntimeError):
    """選択ハーネスが検証済みdaybook draftを返せなかった。"""


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


def _draft_stage_path(log_dir: str, day: str) -> str:
    return os.path.join(log_dir, "memory", "daybook_staging", f"{day}.json")


def _stage_context_sha256(day: str, entries: list[dict[str, Any]]) -> str:
    payload = {
        "day": day,
        "entries": entries,
        "character": os.environ.get("CHARACTER", "").strip(),
        "resident": os.environ.get("RESIDENT", "ユーザー"),
        "generator": {key: os.environ.get(key, "") for key in _STAGE_CONTEXT_ENV_KEYS},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_staged_draft(
    log_dir: str,
    day: str,
    context_sha256: str,
) -> dict[str, Any] | None:
    path = _draft_stage_path(log_dir, day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            stage = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise DaybookAgentError(f"staged daybook draft is unreadable: {e}") from e
    if not isinstance(stage, dict):
        raise DaybookAgentError("staged daybook draft root is not an object")
    if stage.get("version") != _STAGE_FORMAT_VERSION:
        raise DaybookAgentError("staged daybook draft has an unsupported format")
    write_started = stage.get("write_started")
    if not isinstance(write_started, bool):
        raise DaybookAgentError("staged daybook draft has no write phase")
    staged_context = stage.get("context_sha256")
    if not isinstance(staged_context, str) or staged_context != context_sha256:
        if not write_started:
            # No persistent write was allowed to begin, so this draft can be
            # discarded and regenerated from the current snapshot without making
            # orphaned episode IDs.
            _clear_staged_draft(log_dir, day)
            return None
        # A write may be partial. Neither regenerating (duplicate episode IDs) nor
        # reusing changed input (stale marker) is safe, so fail closed for inspection.
        raise DaybookAgentError("staged daybook context changed; marker not advanced")
    draft = stage.get("draft")
    if not isinstance(draft, dict):
        raise DaybookAgentError("staged daybook draft is not an object")
    _validate_agent_draft(draft)
    return draft


def _stage_draft(
    log_dir: str,
    day: str,
    context_sha256: str,
    draft: dict[str, Any],
) -> None:
    _validate_agent_draft(draft)
    stage = {
        "version": _STAGE_FORMAT_VERSION,
        "context_sha256": context_sha256,
        "write_started": False,
        "draft": draft,
    }
    _write_text(
        _draft_stage_path(log_dir, day),
        json.dumps(stage, ensure_ascii=False, indent=2) + "\n",
    )


def _mark_staged_write_started(log_dir: str, day: str) -> None:
    path = _draft_stage_path(log_dir, day)
    try:
        with open(path, encoding="utf-8") as f:
            stage = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise DaybookAgentError(f"staged daybook draft is unreadable: {e}") from e
    if not isinstance(stage, dict) or stage.get("version") != _STAGE_FORMAT_VERSION:
        raise DaybookAgentError("staged daybook draft has an unsupported format")
    stage["write_started"] = True
    _write_text(path, json.dumps(stage, ensure_ascii=False, indent=2) + "\n")


def _clear_staged_draft(log_dir: str, day: str) -> None:
    try:
        os.unlink(_draft_stage_path(log_dir, day))
    except FileNotFoundError:
        pass


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


def _schema_error(value: Any, schema: dict[str, Any], path: str = "$") -> str:
    """daybook_schemaが使うJSON Schema部分集合を依存追加なしで検証する。"""
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]

    def matches_type(name: Any) -> bool:
        if name == "null":
            return value is None
        if name == "object":
            return isinstance(value, dict)
        if name == "array":
            return isinstance(value, list)
        if name == "string":
            return isinstance(value, str)
        if name == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if name == "boolean":
            return isinstance(value, bool)
        return False

    if expected is not None and not any(matches_type(name) for name in expected_types):
        return f"{path}: expected {expected!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: value is not in enum"

    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                return f"{path}: missing required property {key!r}"
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                return f"{path}: additional property {extras[0]!r} is not allowed"
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                error = _schema_error(child, child_schema, f"{path}.{key}")
                if error:
                    return error

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            error = _schema_error(item, schema["items"], f"{path}[{index}]")
            if error:
                return error
    return ""


def _validate_agent_draft(draft: dict[str, Any]) -> None:
    error = _schema_error(draft, daybook_schema())
    if error:
        raise DaybookAgentError(f"daybook response schema mismatch: {error}")


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
    facts = entry.get("facts") if isinstance(entry.get("facts"), dict) else None
    facts_summary = format_facts_summary(facts)
    ungrounded = bool(entry.get("ungrounded_speech_claim"))
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
    if facts_summary:
        detail_parts.append(f"実測: {facts_summary}")
    if ungrounded:
        detail_parts.append("※発話記録なし")

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
        "tags": [tag for tag in [emotion, "speak" if speak else "", "発話記録なし" if ungrounded else ""] if tag],
        "entities": [],
        "actors": [],
        "importance": max(0.0, min(1.0, round(importance, 3))),
        "evidence": [
            {
                "timestamp": timestamp,
                "emotion": emotion,
                "private": private,
                "speak": speak,
                "facts": facts,
                "ungrounded_speech_claim": ungrounded,
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


def _run_agent_process(
    cmd: list[str],
    *,
    input: str,
    capture_output: bool,
    text: bool,
    cwd: str,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess:
    """Run one harness in its own process group and bound the marker lock time."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=DAYBOOK_AGENT_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        raise DaybookAgentError(f"invoke-agent timed out after {timeout:g}s") from e
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _summarize_with_agent(
    day: str,
    entries: list[dict[str, Any]],
    *,
    run=None,
) -> dict[str, Any]:
    character = os.environ.get("CHARACTER", "").strip()
    resident = os.environ.get("RESIDENT", "ユーザー")

    lines: list[str] = []
    for item in entries:
        ts = _clean(item.get("timestamp"))
        emo = _clean(item.get("emotion"))
        obs = _clean(item.get("private"))
        spk = _clean(item.get("speak"))
        line = f"- {ts[11:16] if len(ts) >= 16 else ts} [{emo}] {obs}"
        facts_summary = format_facts_summary(item.get("facts"))
        if facts_summary:
            line += f" [実測: {facts_summary}]"
        if item.get("ungrounded_speech_claim"):
            line += " ※発話記録なし"
        if spk:
            line += f" → 発話: {spk}"
        lines.append(line)

    prompt = (character + "\n\n") if character else ""
    prompt += f"{day} の観察ログをもとに structured な日次メモを作ってください。\n\n"
    prompt += f"対象の一日は {resident} さんの暮らしを観察した記録です。\n"
    prompt += "出力は JSON のみ。前置き・後書き・コードフェンスは禁止。\n\n"
    prompt += "JSON に含める項目は次の通りです（キー名はこの通りに、値は指示に従って埋めてください）:\n"
    prompt += "- summary: 1〜3文の要約（文字列）\n"
    prompt += "- themes: 主題の配列（文字列のリスト）\n"
    prompt += "- highlights: 重要な出来事の配列。各要素は summary（一言要約）・detail（詳細）・tags（文字列のリスト）・importance（0.0〜1.0の重要度）を持つ。最大5件\n"
    prompt += "- open_questions: 未解決の疑問点の配列（文字列のリスト）\n"
    prompt += (
        "- episodes: 出来事単位の配列。各要素は timestamp・kind（例: observation）・source（例: loop）・"
        "summary・detail・tags（文字列のリスト）・entities（文字列のリスト）・actors（文字列のリスト）・"
        "importance（0.0〜1.0）・evidence（timestamp と private を持つオブジェクトの配列）・"
        "status（例: canonical）・links（causes と effects をキーに持つオブジェクト、それぞれ配列）を持つ\n\n"
    )
    prompt += "制約:\n"
    prompt += "- episodes は出来事単位にまとめる\n"
    prompt += "- highlights は最大5件\n"
    prompt += "- summary は日全体の見取り図にする\n"
    prompt += "- 可能なら episodes は 1〜8 件程度に圧縮する\n\n"
    prompt += "観察ログ:\n" + "\n".join(lines)

    env = {
        **os.environ,
        "PATH": build_tools_path(),
    }
    runner = run or _run_agent_process
    try:
        proc = runner(
            [
                "bash",
                os.path.join(SCRIPT_DIR, "invoke-agent.sh"),
                "--model",
                "default",
                "--no-tools",
                "--agent-site",
                "daybook",
                "--json-schema",
                json.dumps(daybook_schema(), ensure_ascii=False),
            ],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=(
                os.environ.get("EHA_AGENT_CWD")
                or os.environ.get("EHA_CLAUDE_CWD")
                or os.path.join(os.environ.get("EHA_DATA_DIR", "/config/embodied-ha"), "workdir")
            ),
            env=env,
            timeout=DAYBOOK_AGENT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise DaybookAgentError(
            f"invoke-agent timed out after {DAYBOOK_AGENT_TIMEOUT_SECONDS}s"
        ) from e
    if proc.returncode != 0:
        stderr = " ".join((proc.stderr or "").split())[:800]
        raise DaybookAgentError(
            f"invoke-agent failed: returncode={proc.returncode}"
            + (f" stderr={stderr}" if stderr else "")
        )
    draft = _parse_json_payload(proc.stdout)
    if not draft:
        raise DaybookAgentError("invoke-agent returned no JSON object")
    _validate_agent_draft(draft)
    return draft


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


def _daybook_is_hollow(daybook: dict[str, Any]) -> bool:
    """中身が空のdaybookスタブか（要約・エピソード・ハイライト等が全て空）。

    エージェントがMCPの build_daybook を当日日付・内容なしで呼ぶと空スタブができる
    （2026-08-14に実測。過去にも複数回発生し、その日の
    実エントリが「既存daybookあり」扱いで要約されずに失われた）。空スタブは
    「日誌なし」として扱い、夜間rollupが実エントリから正規の日誌で上書きする。
    """
    return not any((
        _clean(daybook.get("summary")),
        daybook.get("episode_ids"),
        daybook.get("highlights"),
        daybook.get("themes"),
        daybook.get("open_questions"),
    ))


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
        # 対象日に空スタブが残っている場合だけ置き換える。無条件のoverwriteにしないのは、
        # 要約生成の間（数分）にチャット側などの別経路が同じ日の正規の日誌を書きうるため
        # （_chat_lockと_loop_lockは別ロック）。判定はbuild_daybook側がファイルロックの
        # 内側で行うので、ここで見た内容が古くなっていても壊さない。
        overwrite_if=_daybook_is_hollow,
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


def _run_locked() -> None:
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
        # マーカーは「日誌を作った最後の日」。ここで today を書くと、まだ終わっていない
        # 今日の日誌ができたことになり、翌日は start_d = today+1 > yesterday でまた即スキップ——
        # 一度ずれると永久に空振りする（2026-07-05 以降、実際にそうなっていた）。
        # yesterday なら「昨日まで済んでいる」を正しく表し、この分岐は冪等になる。
        _write_marker(daybook_marker, yesterday_d.isoformat())
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
        entry = {
            "timestamp": ts,
            "emotion": _clean(row.get("emotion")),
            "private": private,
            "speak": _clean(row.get("speak")),
        }
        if isinstance(row.get("facts"), dict):
            entry["facts"] = row.get("facts")
        if row.get("ungrounded_speech_claim"):
            entry["ungrounded_speech_claim"] = True
        entries_by_day[day].append(entry)

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
        # 要約するものが無かった日。ここでも today ではなく yesterday。
        # today にすると翌日以降ずっと即スキップになる。
        new_marker = yesterday_d.isoformat()
    else:
        existing_daybook = (
            ms.load_daybook(log_dir, target_day) if ms.daybook_exists(log_dir, target_day) else None
        )
        if existing_daybook is not None and not _daybook_is_hollow(existing_daybook):
            daybook = existing_daybook
            brief = ms.daybook_brief(daybook)
            if _append_memory_brief(memory_file, brief):
                print(f"[DAYBOOK] 既存の structured daybook を反映: {target_day}")
            else:
                print(f"[DAYBOOK] 既存の structured daybook を再利用: {target_day}")
            _maybe_consolidate(log_dir, target_day, target_day)
            _clear_staged_draft(log_dir, target_day)
            # マーカーは「日誌を作った最後の日」。ここで today を書くと、その日自身が
            # 二度と要約されない（翌日は start_d = today+1 > yesterday で即スキップ）。
            new_marker = target_day
        else:
            context_sha256 = _stage_context_sha256(target_day, entries_by_day[target_day])
            draft = _load_staged_draft(log_dir, target_day, context_sha256)
            if draft is None:
                draft = _summarize_with_agent(target_day, entries_by_day[target_day])
                # ここから先は複数ファイルへの永続化。途中停止しても同じ生成結果で
                # 再開し、異なるepisode IDが重複しないよう入力fingerprintとdraftを固定する。
                _stage_draft(log_dir, target_day, context_sha256, draft)
            _mark_staged_write_started(log_dir, target_day)
            normalized = _normalize_draft(target_day, entries_by_day[target_day], draft)
            _write_daybook(log_dir, memory_file, target_day, normalized, entries_by_day[target_day])
            _maybe_consolidate(log_dir, target_day, target_day)
            _clear_staged_draft(log_dir, target_day)
            # マーカーは「日誌を作った最後の日」。ここで today を書くと、その日自身が
            # 二度と要約されない（翌日は start_d = today+1 > yesterday で即スキップ）。
            new_marker = target_day

    if new_marker:
        _write_marker(daybook_marker, new_marker)


def main() -> None:
    # loop側にも排他はあるが、手動起動や将来の別callerを含めてdaybook全体を直列化する。
    # markerと同じロックを使うことで、同じ日を並行生成して複数draftを作らない。
    with file_lock(os.environ["DAYBOOK_MARKER"]):
        _run_locked()


if __name__ == "__main__":
    main()
