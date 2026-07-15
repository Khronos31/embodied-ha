#!/usr/bin/env python3
"""loop.sh のPython移植。

このファイルは daemon.py からはまだ起動しない。まず postprocess/persistence
境界を移植し、loop.sh と同じ保存契約をテストで固定する。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

import introspection_facts  # noqa: E402
from response_parse import loop_extract  # noqa: E402


def parse_loop_response(text: str) -> dict[str, Any]:
    """loop.sh の抽出 heredoc と同じく、失敗時は raw を private fallback に残す。"""
    return loop_extract(text)


def loop_introspection_state(parsed: dict[str, Any]) -> dict[str, str]:
    """loop.sh の PARSE_OK / INTROSPECTION_EMPTY / SAY 算出と同じ契約。"""
    private = parsed.get("private", "") or ""
    emotion = parsed.get("emotion", "") or ""
    say_v = parsed.get("speak")
    say = str(say_v).strip() if say_v not in (None, "", "null") else ""
    return {
        "PARSE_OK": "1" if parsed.get("_parse_ok") else "0",
        "INTROSPECTION_EMPTY": "1" if not str(private).strip() and not str(emotion).strip() else "0",
        "SAY": say,
    }


def append_loop_parse_error(
    *,
    log_dir: str | os.PathLike[str],
    timestamp: str,
    mode: str,
    reason: str,
    raw: str,
) -> None:
    """loop_parse_errors.jsonl へ raw 診断を追記する。"""
    path = Path(log_dir) / "loop_parse_errors.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": timestamp,
        "mode": mode,
        "reason": reason,
        "raw": raw[:2000],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_parse_skip_if_needed(
    *,
    parsed: dict[str, Any],
    response: str,
    log_dir: str | os.PathLike[str],
    timestamp: str,
    mode: str,
) -> bool:
    """parse失敗または空内省なら診断ログへ記録し、通常保存をskipすべきなら True。"""
    state = loop_introspection_state(parsed)
    if state["PARSE_OK"] == "1" and state["INTROSPECTION_EMPTY"] != "1":
        return False
    reason = "json_parse_failed" if state["PARSE_OK"] != "1" else "empty_introspection"
    append_loop_parse_error(
        log_dir=log_dir,
        timestamp=timestamp,
        mode=mode,
        reason=reason,
        raw=response,
    )
    return True


def should_persist_introspection(parsed: dict[str, Any]) -> bool:
    """通常 memory/daybook 経路へ保存してよい内省だけを通す。

    抽出フォールバックは raw を private に残すが、通常ログには混ぜない。
    parse失敗時の raw は loop_parse_errors.jsonl にだけ保存する。
    """
    state = loop_introspection_state(parsed)
    return state["PARSE_OK"] == "1" and state["INTROSPECTION_EMPTY"] != "1"


def persist_loop_introspection(
    *,
    parsed: dict[str, Any],
    mode: str,
    timestamp: str,
    observation_log: str | os.PathLike[str],
    explore_log: str | os.PathLike[str],
    facts_file: str | os.PathLike[str] | None = None,
    projected_camera_source: str = "",
) -> bool:
    """loop.sh の observations/explore 保存分岐を移植する。

    戻り値は通常ログへ保存したかどうか。
    """
    if not should_persist_introspection(parsed):
        return False

    facts = introspection_facts.load_facts_file(str(facts_file or ""))
    private = parsed.get("private", "") or ""
    topic = parsed.get("topic", "") or ""

    if mode == "observe":
        row: dict[str, Any] = {
            "timestamp": timestamp,
            "emotion": parsed.get("emotion", "") or "",
            "private": private,
        }
        path = Path(observation_log)
    else:
        row = {
            "timestamp": timestamp,
            "mode": mode,
            "emotion": parsed.get("emotion", "") or "",
            "private": private,
            "topic": topic,
        }
        path = Path(explore_log)

    if facts is not None:
        row["facts"] = facts
    if introspection_facts.should_flag_ungrounded_speech_claim(
        private=private,
        topic=topic,
        facts=facts,
        proposal=parsed.get("proposal"),
    ):
        row["ungrounded_speech_claim"] = True
    if introspection_facts.should_flag_ungrounded_visual_claim(
        private=private,
        topic=topic,
        speak=parsed.get("speak", "") or "",
        facts=facts,
        current_entity=projected_camera_source,
    ):
        row["ungrounded_visual_claim"] = True

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True


def main() -> None:
    raise SystemExit("loop.py migration is not wired yet; daemon.py still runs loop.sh")


if __name__ == "__main__":
    main()
