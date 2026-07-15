#!/usr/bin/env python3
"""loop.sh のPython移植。

このファイルは daemon.py からはまだ起動しない。まず postprocess/persistence
境界を移植し、loop.sh と同じ保存契約をテストで固定する。
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_MODE_WEIGHTS = {
    "observe": 30,
    "explore": 35,
    "reflect": 20,
    "web": 15,
    "social": 10,
}

import introspection_facts  # noqa: E402
from response_parse import loop_extract  # noqa: E402


def parse_loop_response(text: str) -> dict[str, Any]:
    """loop.sh の抽出 heredoc と同じく、失敗時は raw を private fallback に残す。"""
    return loop_extract(text)


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _num(mapping: dict[str, Any], key: str, default: float = 0.5) -> float:
    try:
        return float(mapping.get(key, default))
    except Exception:
        return default


def compute_mode_weights(
    body_state: dict[str, Any],
    *,
    anomaly_urgency: float = 0.0,
    github_app_exists: bool = False,
) -> dict[str, int]:
    """loop.sh のモード抽選重みをPythonへ移植する。

    `ANOMALY_URGENCY` は body_state ではなく独立した入力として扱う。
    """
    curiosity = _num(body_state, "curiosity")
    social_openness = _num(body_state, "social_openness")
    energy = _num(body_state, "energy")
    stress = _num(body_state, "stress")

    weights = dict(BASE_MODE_WEIGHTS)
    weights["observe"] += int((curiosity - 0.5) * 24 + (energy - 0.5) * 10 - stress * 10)
    weights["explore"] += int((curiosity - 0.5) * 34 + (energy - 0.5) * 15 - stress * 12)
    weights["reflect"] += int(stress * 22 + max(0.0, 0.5 - energy) * 26)
    weights["web"] += int(max(0.0, curiosity - 0.45) * 10)
    weights["social"] += int((social_openness - 0.5) * 20)
    if anomaly_urgency > 0:
        weights["observe"] += int(anomaly_urgency * 0.8)
        weights["explore"] += int(anomaly_urgency * 1.2)
    for key in list(weights):
        weights[key] = max(5, weights[key])
    if not github_app_exists:
        weights["social"] = 0
    return weights


def choose_mode(environ: dict[str, str] | None = None, *, choices=random.choices) -> str:
    """MODE env があれば尊重し、無ければ身体状態から自律ループのモードを抽選する。"""
    env = dict(environ if environ is not None else os.environ)
    if env.get("MODE"):
        return str(env["MODE"])
    body_state = _json_dict(env.get("EHA_BODY_STATE"))
    anomaly_urgency = _num({"ANOMALY_URGENCY": env.get("ANOMALY_URGENCY")}, "ANOMALY_URGENCY", 0.0)
    github_app_path = env.get("EHA_GITHUB_APP_PEM") or "/config/embodied-ha/github_app.pem"
    weights = compute_mode_weights(
        body_state,
        anomaly_urgency=anomaly_urgency,
        github_app_exists=os.path.exists(github_app_path),
    )
    modes = list(weights.keys())
    return choices(modes, weights=[weights[key] for key in modes], k=1)[0]


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
