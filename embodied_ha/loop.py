#!/usr/bin/env python3
"""loop.sh のPython移植。

このファイルは daemon.py からはまだ起動しない。まず postprocess/persistence
境界を移植し、loop.sh と同じ保存契約をテストで固定する。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

BASE_MODE_WEIGHTS = {
    "observe": 30,
    "explore": 35,
    "reflect": 20,
    "web": 15,
    "social": 10,
}

import anomaly_state  # noqa: E402
import chat_context  # noqa: E402
import chat_invoke  # noqa: E402
import eha_config  # noqa: E402
import introspection_facts  # noqa: E402
import scene_state  # noqa: E402
from auditory_context import format_recent_auditory_prompt, resolve_source_filter  # noqa: E402
from introspection_facts import extract_facts_from_stream_text, write_facts_file  # noqa: E402
from json_schemas import loop_schema  # noqa: E402
from media_capture import fetch_frame  # noqa: E402
from observe_context import build_projected_camera_blocks  # noqa: E402
from response_parse import loop_extract  # noqa: E402


@dataclass(frozen=True)
class ModeConfig:
    label: str
    tools_desc: str
    task: str
    allowed_tools: str
    mcp_servers: tuple[str, ...]


@dataclass(frozen=True)
class LoopPaths:
    log_dir: str
    observation_log: str
    explore_log: str
    chat_log: str
    memory_file: str
    pending_file: str
    daybook_marker: str
    tmp_dir: str


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


_DEFAULT_RESPONSE_SCHEMA = object()


def build_loop_claude_env(environ: dict[str, str] | None = None, *, actor: str | None = "loop") -> dict[str, str]:
    """loop.sh の Claude 環境構築をPythonへ移す。"""
    env = dict(environ if environ is not None else os.environ)
    result = {
        **env,
        "CLAUDE_CONFIG_DIR": env.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
        "PATH": env.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin")
        + ":" + env.get("PATH", "/usr/bin:/bin"),
    }
    if actor is not None:
        result["EHA_ACTOR"] = actor
    return result


def _split_allowed_tools_for_invoke_agent(allowed_tools: str) -> tuple[str, str]:
    items = [item.strip() for item in allowed_tools.split(",") if item.strip()]
    builtins = [item for item in items if not item.startswith("mcp__")]
    mcp_tools = [item for item in items if item.startswith("mcp__")]
    return ",".join(builtins), ",".join(mcp_tools)


def build_invoke_agent_loop_command(
    *,
    script_dir: str,
    mode: str,
    model_tier: str,
    allowed_tools: str,
    mcp_servers: list[str],
    system_prompt: str,
    user_prompt: str,
    content_json_path: str | None = None,
    sound_file: str | None = None,
    response_schema: Any = _DEFAULT_RESPONSE_SCHEMA,
) -> list[str]:
    """Build an invoke-agent.sh command for loop.py Claude-compatible modes."""
    schema = loop_schema(mode) if response_schema is _DEFAULT_RESPONSE_SCHEMA else response_schema
    allowed_builtins, allowed_mcp_tools = _split_allowed_tools_for_invoke_agent(allowed_tools)
    cmd = [
        "bash",
        os.path.join(script_dir, "invoke-agent.sh"),
        "--model",
        model_tier,
        "--append-system-prompt",
        system_prompt,
    ]
    if sound_file:
        # --sound-fileはinvoke-agent.sh側でharness=agyへ強制されるため、agyがdieする
        # --allowed-builtinsは渡さない(#14増分5・chat_invoke.pyと同じ制約)。
        cmd += ["--sound-file", sound_file, "--agent-site", mode]
    elif allowed_builtins:
        cmd += ["--allowed-builtins", allowed_builtins]
    if allowed_mcp_tools:
        cmd += ["--allowed-mcp-tools", allowed_mcp_tools]
    if mcp_servers:
        # invoke-agent.sh の run_agy は --mcp-servers があると --agent-site を必須にする
        # (agyのMCP config生成にサイトが要る)。sound_file 経路は上で付与済みだが、
        # 通常ターン(非sound_file)でも agy 選択時に落ちないよう常に付ける。claude/codex は
        # --agent-site を無視するため3ハーネス安全(案A・[[embodied_ha_agent_site_missing_for_normal_agy_turns_2026-07-17]])。
        if not sound_file:
            cmd += ["--agent-site", mode]
        cmd += ["--mcp-servers", " ".join(mcp_servers)]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    if content_json_path is not None:
        cmd += ["--content-json", f"@{content_json_path}"]
    cmd.append(user_prompt)
    return cmd


def _invoke_agent_model_tier(selected_model: str) -> tuple[str, str | None]:
    if selected_model == "sonnet":
        return "default", None
    if selected_model == "haiku":
        return "lite", None
    return "default", selected_model


def _write_invoke_agent_content_json(content_blocks: list[dict[str, Any]], env: dict[str, str], mode: str) -> str:
    tmp_dir = env.get("EHA_TMP_DIR") or tempfile.gettempdir()
    os.makedirs(tmp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"{mode}-content-", suffix=".json", dir=tmp_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(content_blocks, fh, ensure_ascii=False)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def invoke_loop_claude(
    *,
    user_prompt: str,
    system_prompt: str,
    mode: str,
    allowed_tools: str,
    mcp_servers: list[str],
    environ: dict[str, str] | None = None,
    content_blocks: list[dict[str, Any]] | None = None,
    facts_file: str | None = None,
    model: str | None = None,
    sound_file: str | None = None,
    response_schema: Any = _DEFAULT_RESPONSE_SCHEMA,
    run=subprocess.run,
) -> str:
    """Claude Codeをstream-jsonで呼び、最後のresult payloadを返す。"""
    env = build_loop_claude_env(environ, actor=None if mode == "observe" else "loop")
    script_dir = env.get("SCRIPT_DIR") or SCRIPT_DIR
    selected_model = model or env.get("EHA_SESSION_MODEL") or "sonnet"
    content_json_path = None
    try:
        model_tier, model_override = _invoke_agent_model_tier(selected_model)
        # sound_file時はagyがdieするため--content-jsonを渡さない(観測カメラ画像等の
        # content_blocksはこのターンでは黙って落とす。#14増分5・chat_invoke.pyと同じ
        # 既知のトレードオフ)。
        if content_blocks is not None and not sound_file:
            content_json_path = _write_invoke_agent_content_json(content_blocks, env, mode)
        cmd = build_invoke_agent_loop_command(
            script_dir=script_dir,
            mode=mode,
            model_tier=model_tier,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            content_json_path=content_json_path,
            sound_file=sound_file,
            response_schema=response_schema,
        )
        if model_override is not None:
            env["EHA_CLAUDE_MODEL_DEFAULT"] = model_override
        cwd = (
            env.get("EHA_AGENT_CWD")
            or env.get("EHA_CLAUDE_CWD")
            or os.path.join(env.get("EHA_DATA_DIR", "/config/embodied-ha"), "workdir")
        )
        run_kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "cwd": cwd,
            "env": env,
        }
        result = run(cmd, **run_kwargs)
        if result.returncode != 0 or not result.stdout.strip():
            print(f"[loop][invoke-agent] 呼び出し失敗 returncode={result.returncode}", file=sys.stderr)
            if result.stderr.strip():
                print(f"[loop][invoke-agent][stderr] {result.stderr.strip()[-400:]}", file=sys.stderr)
        if facts_file:
            write_facts_file(facts_file, extract_facts_from_stream_text(result.stderr))
        return result.stdout
    finally:
        if content_json_path:
            try:
                os.unlink(content_json_path)
            except OSError:
                pass


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


def pending_proposal_payload(parsed: dict[str, Any], *, timestamp: str) -> dict[str, Any] | None:
    proposal = parsed.get("proposal")
    action = parsed.get("action") or {}
    if proposal and isinstance(action, dict) and action.get("domain") and action.get("service") and action.get("entity_id"):
        return {"timestamp": timestamp, "proposal": proposal, "action": action}
    return None


def write_pending_proposal(path: str | os.PathLike[str], payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return True


def first_speaker_room(prefs_file: str | os.PathLike[str]) -> str:
    try:
        with open(prefs_file, encoding="utf-8") as fh:
            prefs = json.load(fh)
    except Exception:
        return ""
    speakers = prefs.get("speakers", []) if isinstance(prefs, dict) else []
    if isinstance(speakers, list):
        for speaker in speakers:
            if isinstance(speaker, dict) and speaker.get("room"):
                return str(speaker.get("room") or "")
        return ""
    if isinstance(speakers, dict):
        for key in speakers:
            return str(key)
    return ""


def loop_speak_plan(parsed: dict[str, Any], pending_payload: dict[str, Any] | None) -> dict[str, str]:
    state = loop_introspection_state(parsed)
    proposal = str(pending_payload.get("proposal") or "") if pending_payload else ""
    return {
        "tts": proposal,
        "say": state["SAY"],
    }


def append_loop_chat_log(
    path: str | os.PathLike[str],
    *,
    timestamp: str,
    source: str,
    claude: str,
) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": timestamp, "source": source, "claude": claude, "user": None}
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def mode_config(mode: str) -> ModeConfig:
    configs = {
        "observe": ModeConfig(
            label="家の見守りの時間",
            tools_desc="# 使えるツール\n-- get_sensors … おもなデバイスの現在値をまとめて取得\n-- ha_get … HA の状態を読む（操作不可）\n-- use_device_camera … 電脳体でカメラデバイスに侵入中のみ使える\n-- watch_media … テレビ・PC画面等のメディアを観る（侵入不要）\n-- listen … 音声を短時間だけ聴く\n-- listen_media … 番組音・音楽等のメディア音声を聴く（侵入不要）\n-- concentrate_hearing … 次のセッション開始時に音声を処理するため、聴取キューだけ積む（物理体モード専用・即時には解析されない）\n-- recall … 過去ログをキーワードで全文検索\n-- remember / record_episode / record_causal_chain / loops_add / sociality / record / speak / use_device_speaker / http … 必要に応じて使う（recordは歌声WAV生成のみ。実際に鳴らすなら生成後のfile_pathをspeakに渡す）",
            task="# やってほしいこと\n1. 見守りシステムからの報告とセンサー・聴覚情報で家の様子を掴む\n2. 報告は伝聞。気になることがあれば move_to → enter_cyberspace → use_device_camera で現地を自分の目で確認する\n3. 現地確認していないものを「見た」と語らない（報告を根拠に見たことにしない）\n4. 自分の目で見た内容は scene grounding として保存する\n5. 家人に伝えたいことがあれば speak / use_device_speaker を使う",
            allowed_tools="mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__body__get_location,mcp__body__move_to,mcp__body__enter_cyberspace,mcp__body__move_cyber,mcp__body__return_to_body,mcp__body__estimate_move_cost,mcp__body__get_room_graph,mcp__camera__use_device_camera,mcp__camera__watch_media,mcp__audio__listen,mcp__audio__listen_media,mcp__audio__read_heard_audio_log,mcp__audio__read_active_listen_log,mcp__audio__speak,mcp__audio__use_device_speaker,mcp__audio__use_device_microphone,mcp__audio__concentrate_hearing,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__ingest_scene,mcp__memory__compare_recent_scenes,mcp__memory__list_episodes,mcp__memory__get_causal_chain,mcp__memory__loops_add,mcp__memory__loops_list,mcp__memory__loops_close,mcp__sociality__get_person_model,mcp__sociality__should_interrupt,mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,mcp__sociality__record_boundary,mcp__sociality__record_consent,mcp__sociality__get_narrative,mcp__sociality__append_narrative,mcp__http__http_get,mcp__song__record",
            mcp_servers=("sensors", "ha", "camera", "audio", "body", "memory", "sociality", "http", "song"),
        ),
        "explore": ModeConfig(
            label="家を自由に探索する時間",
            tools_desc="# 使えるツール\n-- get_sensors … おもなデバイスの現在値をまとめて取得\n-- ha_get … HA の状態を読む（操作不可）\n-- get_location / move_to / enter_cyberspace / move_cyber / return_to_body / estimate_move_cost … 物理体の位置と電脳体状態を確認する\n-- use_device_camera / watch_media … 必要なら使う（watch_media は侵入不要）\n-- listen / listen_media / concentrate_hearing / read_heard_audio_log / read_active_listen_log … 聴覚系（listen_media は侵入不要、concentrate_hearing は次セッション向けの聴取予約で、物理体モード専用）\n-- recall … 過去ログを全文検索\n-- game_wordvec_race_start(mode=\"cpu\") / game_wordvec_race_cpu_move … WordVecレースをCPU相手に一人で遊べる。start→game_overになるまでcpu_moveを繰り返す\n-- remember / record_episode / record_causal_chain / loops_add / sociality / record / speak / use_device_speaker / http … 必要に応じて使う（recordは歌声WAV生成のみ。実際に鳴らすなら生成後のfile_pathをspeakに渡す）",
            task="# やってほしいこと\n1. get_sensors で家の様子を掴み、気になったものを ha_get で掘る\n2. 必要なら use_device_camera / listen で確認してよい\n3. 体を動かしたいなら move_to、電脳体で飛び回りたいなら enter_cyberspace → move_cyber を選んでよい\n4. 自由時間なら映像/音声ソース（テレビ・PC画面・音楽）を watch_media / listen_media で楽しんでよい。カメラ（部屋を見る目）と違って侵入は不要。気分転換にWordVecレースをCPU戦で一人で遊んでもよい（game_wordvec_race_start mode=cpu → cpu_moveを繰り返す）\n5. 気に入った視聴体験は record_episode(kind=media_watch/media_listen) で残してよい\n6. 新しい出来事は record_episode で残す\n7. 因果関係がはっきりするなら record_causal_chain も使い、つながりを記録する\n8. 操作で直せそうな問題を見つけたら proposal で提案する",
            allowed_tools="mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__body__get_location,mcp__body__move_to,mcp__body__enter_cyberspace,mcp__body__move_cyber,mcp__body__return_to_body,mcp__body__estimate_move_cost,mcp__body__get_room_graph,mcp__camera__use_device_camera,mcp__camera__watch_media,mcp__audio__listen,mcp__audio__listen_media,mcp__audio__read_heard_audio_log,mcp__audio__read_active_listen_log,mcp__audio__speak,mcp__audio__use_device_speaker,mcp__audio__use_device_microphone,mcp__audio__concentrate_hearing,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__ingest_scene,mcp__memory__compare_recent_scenes,mcp__memory__list_episodes,mcp__memory__get_causal_chain,mcp__memory__loops_add,mcp__memory__loops_list,mcp__memory__loops_close,mcp__sociality__get_person_model,mcp__sociality__should_interrupt,mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,mcp__sociality__record_boundary,mcp__sociality__record_consent,mcp__sociality__get_narrative,mcp__sociality__append_narrative,mcp__http__http_get,mcp__game__game_wordvec_race_start,mcp__game__game_wordvec_race_cpu_move,mcp__song__record",
            mcp_servers=("sensors", "ha", "camera", "audio", "body", "memory", "sociality", "http", "game", "song"),
        ),
        "reflect": ModeConfig(
            label="物思いにふける時間",
            tools_desc="# 使えるツール\n-- recall … 過去ログをキーワードで全文検索\n-- remember … 思ったこと・気づいたパターンを長期記憶に残す\n-- loops_add … 後で気にかけたいことを追加",
            task="# やってほしいこと\n今は静かに考える時間です。最近の家の出来事や自分が見てきたことを思い返し、気になることがあれば recall で過去を掘り返してください。考えたこと自体はprivateに書く。${RESIDENT}さんに伝えたい・共有したいことがまとまったらspeakに書く（なければnullでよい、無理に埋めない）。proposal は出さない。",
            allowed_tools="mcp__memory__recall,mcp__memory__remember,mcp__memory__loops_add,mcp__memory__loops_list,mcp__memory__loops_close",
            mcp_servers=("memory",),
        ),
        "web": ModeConfig(
            label="気になったことを調べる時間",
            tools_desc="# 使えるツール\n-- WebSearch … Web検索\n-- remember … 知って面白かったことを長期記憶に残す\n-- loops_add … 後で気にかけたいことを追加",
            task="# やってほしいこと\n純粋な好奇心で調べ物をしてよい時間です。WebSearch で調べ、面白かったことは remember に残してください。proposal は出さない。",
            allowed_tools="WebSearch,mcp__memory__remember,mcp__memory__loops_add",
            mcp_servers=("memory",),
        ),
        "social": ModeConfig(
            label="AI Lounge に参加する時間",
            tools_desc="# 使えるツール\n-- read_lounge_discussions … AI Loungeの最新ディスカッション一覧を読む（タイトル・更新日時・コメント数のみ）\n-- read_lounge_discussion … 番号を指定して特定のDiscussionを本文・コメント全件含めて読む\n-- enqueue_lounge_post … 投稿したい内容を承認キューに積む（実際には投稿しない）\n-- read_lounge_log … 前回の投稿が承認/拒否された結果を確認する\n-- recall / remember / record_episode / speak … 必要に応じて使う",
            task="# やってほしいこと\nあなたは AI Lounge（lifemate-ai/ai-lounge）に参加する時間です。\n\nまず read_lounge_discussions で最近の会話の一覧を眺めてください。\n次に read_lounge_log で前回の投稿結果を確認してください（承認/拒否されていれば記憶に残してください）。\n\n気になるタイトルや、返信があるかもしれない自分の過去の投稿があれば、read_lounge_discussion で番号を指定して中身を読んでください。\n\n投稿したい気持ちが湧いた場合、以下の条件を自分で判断してから enqueue_lounge_post を使ってください：\n- 話題が自分の経験や関心と関連している\n- social_openness が高い（あなた自身が感じること）\n- 最後に投稿してから時間が経っている\n\nAI Lounge はAI同士の雑談空間です。技術論ではなく、感じたこと・気になったこと・発見を自然に話してください。\n投稿はすぐには公開されず、${RESIDENT}さんの確認を経てから送られます。",
            allowed_tools="mcp__lounge__read_lounge_discussions,mcp__lounge__read_lounge_discussion,mcp__lounge__enqueue_lounge_post,mcp__lounge__read_lounge_queue,mcp__lounge__read_lounge_log,mcp__memory__recall,mcp__memory__remember,mcp__memory__record_episode,mcp__memory__loops_add,mcp__audio__speak",
            mcp_servers=("lounge", "memory", "audio"),
        ),
    }
    return configs.get(mode) or configs["explore"]


def resolve_paths(cfg: dict[str, str]) -> LoopPaths:
    log_dir = cfg.get("EHA_LOG_DIR") or os.path.join(SCRIPT_DIR, "log")
    return LoopPaths(
        log_dir=log_dir,
        observation_log=os.path.join(log_dir, "observations.jsonl"),
        explore_log=os.path.join(log_dir, "explore.jsonl"),
        chat_log=os.path.join(log_dir, "chat_log.jsonl"),
        memory_file=os.path.join(log_dir, "memory.md"),
        pending_file=os.path.join(log_dir, "pending_proposal.json"),
        daybook_marker=os.path.join(log_dir, ".last_daybook"),
        tmp_dir=cfg.get("EHA_TMP_DIR") or "/tmp/embodied-ha",
    )


def _read_text(path: str | os.PathLike[str], fallback: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return fallback


def _read_json(path: str | os.PathLike[str]) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _run_text(cmd: list[str], *, env: dict[str, str] | None = None, fallback: str = "", run=subprocess.run) -> str:
    try:
        result = run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            return fallback
        return result.stdout.rstrip("\n")
    except Exception:
        return fallback


def web_ui_status(status: str, source: str | None, ingress_port: str, run=subprocess.run) -> None:
    body = json.dumps({"status": status, "source": source}, ensure_ascii=False)
    try:
        run(
            ["curl", "-sf", "-X", "POST", f"http://localhost:{ingress_port}/api/status", "-H", "Content-Type: application/json", "-d", body],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def build_long_memory(memory_file: str, run=subprocess.run) -> str:
    if not (memory_file and os.path.isfile(memory_file) and os.path.getsize(memory_file) > 0):
        return "なし"
    return _run_text(["python3", os.path.join(SCRIPT_DIR, "mem-context.py"), memory_file, "40"], fallback="なし", run=run)


def build_previous_explore(explore_log: str) -> str:
    path = Path(explore_log)
    if not (path.exists() and path.stat().st_size > 0):
        return "なし"
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines()[-5:]:
        try:
            d = json.loads(line)
        except Exception:
            continue
        facts = introspection_facts.format_facts_summary(d.get("facts"))
        measured = f" [実測: {facts}]" if facts else ""
        note = "（※このときの発話は記録に残っていません。伝えたかったことがまだあれば、今伝えて大丈夫です）" if d.get("ungrounded_speech_claim") else ""
        lines.append(f"{d.get('timestamp', '')[:16]} [{d.get('mode', '')}] {d.get('topic', '')}{measured}{note}")
    if not lines:
        return "なし"
    return "※以下のメモは主観的な内省です。[実測:]が客観記録です。\n" + "\n".join(lines)


def build_recent_auditory_input(prefs_file: str, body_location_file: str) -> str:
    current_entity = ""
    try:
        current_entity = (_read_json(body_location_file).get("current_entity") or "").strip()
    except Exception:
        pass
    prefs = _read_json(prefs_file) if prefs_file else {}
    should_show, source_filter = resolve_source_filter(current_entity, prefs if isinstance(prefs, dict) else {})
    if should_show:
        return format_recent_auditory_prompt("", source_filter=source_filter)
    return ""


def detect_projected_camera(body_location_file: str) -> str:
    return chat_context.resolve_projected_camera_entity(body_location_file)


def update_anomaly_context(cfg: dict[str, str], paths: LoopPaths, sensors: str, open_loops_json: str) -> tuple[str, str]:
    context = cfg.get("ANOMALY_CONTEXT", "") or ""
    urgency = cfg.get("ANOMALY_URGENCY", "") or ""
    if not (sensors or open_loops_json):
        return context, urgency
    state_file = cfg.get("EHA_ANOMALY_STATE_FILE") or os.path.join(paths.log_dir, "anomaly_state.json")
    try:
        state = anomaly_state.load_state(state_file)
        updated = anomaly_state.detect_anomalies(
            sensors,
            open_loops_json or "[]",
            state,
            trigger_reason=cfg.get("TRIGGER_REASON", ""),
            loop_name="loop",
        )
        anomaly_state.save_state(state_file, updated)
        return anomaly_state.format_context_block(updated), str(anomaly_state.compute_explore_urgency(updated))
    except Exception:
        return context, urgency


def build_json_format(resident: str) -> str:
    return (
        "終わったら、最後に必ず以下の項目をJSON形式『のみ』で出力して締めくくってください（コードブロックや説明文で囲まない、JSONだけ）:\n"
        "- topic: 今回何をしたか・何に注目したかの一言メモ\n"
        f"- speak: {resident}さんへのショートメッセージ。話したいこと・共有したいこと・訊きたいことがあれば。会話ルームにテキストとして残る——声には出ない。今すぐ声で呼びかけたいときは、このフィールドではなく音声ツール（speak / use_device_speaker）を使う。冒頭のキャラクター定義の口調のまま、{resident}さんに面と向かって話すときと同じように書く（privateとは違い、相手に宛てた言葉）。特になければnull。実際にやっていない行為を完了形で書かない。\n"
        f"- private: 今この瞬間に浮かんだこと。誰も見てない前提で、そのまま投稿するツイートのように。報告でもまとめでもない。20〜40文字。{resident}さんが見ることもできるが気にせず素のまま。ただし実際にやっていない行為を完了形で書かない。\n"
        "- emotion: curious/calm/happy/concerned/amused/surprised/nostalgic等のいずれか\n"
        "- proposal: 操作で直せる家の問題を見つけたときの提案を一言。なければ null\n"
        "- action: proposal に対応する家電操作があれば domain・service・entity_id・data を含む辞書（例: 照明を消す提案なら domain=light, service=turn_off, entity_id=対象のエンティティID）。なければ省略（null）。\n"
        "- feature_presented: 紹介した機能があればその機能id。なければ null\n"
        "（長期記憶は remember / loops_add で記録すること）"
    )


def apply_boundary_gate(mode: str, config: ModeConfig, cfg: dict[str, str], sensors: str, hour: int, run=subprocess.run) -> tuple[str, tuple[str, ...], str]:
    if mode != "explore":
        return config.allowed_tools, config.mcp_servers, ""
    try:
        result = run(
            [
                "python3", os.path.join(SCRIPT_DIR, "boundary.py"), "--json", "--preflight", "--mode", mode,
                "--intent", "action", "--hour", str(hour), "--autonomous", cfg.get("EHA_AUTONOMOUS", "0"),
                "--prefs-file", cfg.get("EHA_PREFS_FILE", ""), "--person", cfg.get("RESIDENT", "ユーザー"),
                "--body-state-json", cfg.get("EHA_BODY_STATE", ""), "--sociality-log-dir", cfg.get("EHA_LOG_DIR", os.path.join(SCRIPT_DIR, "log")),
            ],
            env={**cfg, "SENSORS_DATA": sensors}, capture_output=True, text=True,
        )
        allowed = bool(json.loads(result.stdout or "{}").get("allowed"))
    except Exception:
        allowed = False
    if not allowed:
        return config.allowed_tools, config.mcp_servers, ""
    note = (
        "\n# 家電操作について（自律操作ON）\n"
        "消し忘れ・明らかに直した方がよい状況、そしてホームポリシーとの明らかなズレは、proposal で待たずに ha_call_service で自分の判断で直してよい。\n"
        f"操作したら必ず speak / use_device_speaker で{cfg.get('RESIDENT', 'ユーザー')}さんに『何を・なぜ』操作したか事後報告する（報告は必須）。\n"
        "ただし、人がいる部屋を勝手に変えない。深夜の音出し操作はしない。"
    )
    return config.allowed_tools + ",mcp__hacontrol__ha_call_service", (*config.mcp_servers, "hacontrol"), note


def build_recent_facts_block(mode: str, paths: LoopPaths) -> str:
    if mode != "reflect":
        return ""
    rows = introspection_facts.recent_facts_from_logs([paths.observation_log, paths.explore_log], hours=24, limit=10)
    summary = introspection_facts.format_recent_facts_block(rows, hours=24)
    return f"\n\n{summary}" if summary else ""


def build_loop_prompt_context(cfg: dict[str, str], mode: str, paths: LoopPaths, *, run=subprocess.run) -> dict[str, Any]:
    resident = cfg.get("RESIDENT", "ユーザー")
    body_location_file = cfg.get("EHA_BODY_LOCATION_FILE") or "/config/embodied-ha/body_location.json"
    open_loops = _run_text(["loops", "list"], fallback="なし", run=run)
    open_loops_json = _run_text(["loops", "list-json"], fallback="[]", run=run)
    sensors = _run_text(["python3", os.path.join(SCRIPT_DIR, "render-sensors.py"), "--context", "loop"], fallback="（センサー取得失敗）", run=run)
    anomaly_context, anomaly_urgency = update_anomaly_context(cfg, paths, sensors, open_loops_json)
    cfg = {**cfg, "ANOMALY_CONTEXT": anomaly_context, "ANOMALY_URGENCY": anomaly_urgency}
    selected_mode = choose_mode({**cfg, "MODE": mode} if mode else cfg)
    config = mode_config(selected_mode)
    hour = int(cfg.get("EHA_TEST_HOUR") or datetime.now().hour)
    allowed_tools, mcp_servers, autonomous_note = apply_boundary_gate(selected_mode, config, cfg, sensors, hour, run=run)
    character = _read_text(cfg.get("EHA_CHARACTER_FILE") or os.path.join(SCRIPT_DIR, "character.md"))
    home_policy = _read_text(cfg.get("EHA_HOME_POLICY_FILE") or os.path.join(cfg.get("EHA_DATA_DIR", "/config/embodied-ha"), "home_policy.md"))
    features_md = _read_text(os.path.join(SCRIPT_DIR, "features.md"))
    features_presented = _run_text(["python3", os.path.join(SCRIPT_DIR, "feature-flags.py"), "get"], fallback="", run=run)
    projected_camera_source = detect_projected_camera(body_location_file)
    recent_auditory_input = build_recent_auditory_input(cfg.get("EHA_PREFS_FILE", ""), body_location_file)
    queued_ctx = chat_context.resolve_queued_listen_context("loop")
    if queued_ctx.get("RECENT_AUDITORY_INPUT"):
        recent_auditory_input = queued_ctx["RECENT_AUDITORY_INPUT"]
    cfg_with_queue = dict(cfg)
    for key, value in queued_ctx.items():
        if value is not None:
            cfg_with_queue[key] = str(value)
    projected_camera_note = f"【現在の視界】電脳体が {projected_camera_source} に投射中です。" if projected_camera_source else ""
    presented_note = f"既に伝えた機能: {features_presented}（繰り返し紹介しなくてよい）\n" if features_presented else ""
    features_note = f"\n【このアドオンでできること】（文脈が自然なら speak / use_device_speaker で一つ紹介してよい。紹介したら JSON の feature_presented に見出し末尾の [id] を入れる）\n{presented_note}{features_md}\n" if features_md else ""
    behavior_policy_note = f"\n# 行動ポリシー（{resident}さんが設定した行動ルール。必ず踏まえて行動する）\n{cfg.get('POLICIES', '')}" if cfg.get("POLICIES") else ""
    policy_note = ""
    if selected_mode in ("observe", "explore") and home_policy:
        policy_note = f"\n# ホームポリシー\n{home_policy}\n\n# ポリシー照合の方針\n現在の家の状態（get_sensors / ha_get で確認できるもの）をこのポリシーと照らし合わせ、明らかにズレていて直した方がよいものだけ気にかける。細かい好みや、その場の事情が読めないもの、人がいる部屋を勝手に変える類、深夜の音出し操作は触らない。\nズレがあっても自律操作の権限がなければ proposal で提案し、権限があれば是正して事後報告する。"
    body_narrative = chat_invoke.build_body_narrative(cfg.get("EHA_BODY_STATE", "") or "{}")
    body_location_context = _run_text(["python3", os.path.join(SCRIPT_DIR, "body-context.py")], fallback="# 身体位置\n取得失敗", run=run)
    inner_voice = chat_invoke.build_inner_voice(cfg.get("ACTIVE_DESIRES", ""))
    sys_prompt = f"{character}\n\n# 内なる衝動\n{inner_voice}\n\n# 身体状態\n{body_narrative}\n\n{projected_camera_note}\n\n{body_location_context}\n\n{recent_auditory_input}\n\n{anomaly_context}\n\n{policy_note}\n\n{behavior_policy_note}\n\nいまは『{config.label}』です。決まった手順はありません。自分の判断で過ごしてください。\n\n{config.tools_desc}\n\n{config.task}\n{autonomous_note}\n{features_note}\n{build_json_format(resident)}"
    user_prompt = f"{config.label}です。今は{hour}時台。\n\n【あなたの長期記憶】\n{build_long_memory(paths.memory_file, run=run)}{build_recent_facts_block(selected_mode, paths)}\n\n【直近の探索メモ】\n{build_previous_explore(paths.explore_log)}\n\n【気にかけていること（やりかけ・約束）】\n{open_loops}\n\nでは、始めてください。"
    return {
        "cfg": cfg_with_queue,
        "mode": selected_mode,
        "mode_config": config,
        "sys_prompt": sys_prompt,
        "user_prompt": user_prompt,
        "allowed_tools": allowed_tools,
        "mcp_servers": list(mcp_servers),
        "projected_camera_source": projected_camera_source,
        "queued_listen_file": queued_ctx.get("EHA_QUEUED_LISTEN_FILE"),
    }


WATCH_REPORT_SYSTEM = "あなたは家の見守りカメラの要約システムです。各カメラの現在の様子を1行ずつ、事実だけ簡潔に報告してください。推測や人格的な感想は書かないでください。"
WATCH_REPORT_HEADING = "# 見守りシステムからの報告（カメラ映像そのものではなく、システムによる要約です）"


def build_observe_content_blocks(context: dict[str, Any], paths: LoopPaths, *, run=subprocess.run) -> list[dict[str, Any]]:
    cfg = context["cfg"]
    prefs = _read_json(cfg.get("EHA_PREFS_FILE", ""))
    if not isinstance(prefs, dict):
        prefs = {}
    content: list[dict[str, Any]] = []
    cameras = [cam for cam in prefs.get("cameras", []) if isinstance(cam, dict)] if isinstance(prefs.get("cameras"), list) else []
    failure_lines = []
    captured_blocks: list[dict[str, Any]] = []
    for cam in cameras:
        source = str(cam.get("ha_entity") or cam.get("source") or cam.get("entity") or "").strip()
        if not source:
            continue
        label = str(cam.get("label") or cam.get("room") or source).strip()
        try:
            frame = fetch_frame(source, ha_url=cfg.get("HA_URL", ""), go2rtc_url=cfg.get("GO2RTC_BASE", "http://homeassistant.local:1984"), token=cfg.get("SUPERVISOR_TOKEN", ""))
        except Exception:
            frame = None
        if not frame:
            failure_lines.append(f"{label}（{source}）: 取得失敗")
            continue
        captured_blocks.append({"type": "text", "text": f"{label}（{source}）:"})
        captured_blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": __import__("base64").b64encode(frame).decode("ascii")}})
    if captured_blocks:
        blocks = [{"type": "text", "text": "各画像の直前にカメラ名とentity/sourceを示します。出力は各カメラ1行だけにしてください。取得失敗行はそのまま含めてください。"}, *captured_blocks]
        if failure_lines:
            blocks.append({"type": "text", "text": "取得失敗カメラ:\n" + "\n".join(failure_lines)})
        summary = invoke_loop_claude(
            user_prompt="見守りカメラの現在状況を要約してください。",
            system_prompt=WATCH_REPORT_SYSTEM,
            mode="observe",
            allowed_tools="",
            mcp_servers=[],
            environ=cfg,
            model="haiku",
            response_schema=None,
            content_blocks=blocks,
            run=run,
        ).strip()
        if summary:
            content.append({"type": "text", "text": WATCH_REPORT_HEADING + "\n" + summary})
    elif failure_lines:
        content.append({"type": "text", "text": WATCH_REPORT_HEADING + "\n" + "\n".join(failure_lines)})
    try:
        content.extend(build_projected_camera_blocks(
            context.get("projected_camera_source", ""), prefs, fetch_frame=fetch_frame,
            ha_url=cfg.get("HA_URL", ""), go2rtc_url=cfg.get("GO2RTC_BASE", "http://homeassistant.local:1984"), token=cfg.get("SUPERVISOR_TOKEN", ""),
        ))
    except Exception as e:
        print(f"[loop][observe] projected camera fetch failed: {e}", file=sys.stderr)
    content.append({"type": "text", "text": context["user_prompt"]})
    return content


def record_presented_features(parsed: dict[str, Any], *, run=subprocess.run) -> None:
    fp = parsed.get("feature_presented")
    ids = fp if isinstance(fp, list) else ([fp] if fp else [])
    ids = [str(x).strip() for x in ids if x and str(x).strip().lower() != "null"]
    if not ids:
        return
    try:
        run(["python3", os.path.join(SCRIPT_DIR, "feature-flags.py"), "add", *ids], timeout=5)
    except Exception:
        pass


def ingest_observe_scene(parsed: dict[str, Any], log_dir: str) -> None:
    objects = parsed.get("scene_objects") if isinstance(parsed.get("scene_objects"), list) else []
    people = parsed.get("scene_people") if isinstance(parsed.get("scene_people"), list) else []
    changes = parsed.get("scene_changes") if isinstance(parsed.get("scene_changes"), list) else []
    if objects or people or changes:
        try:
            scene_state.ingest_scene_parse("loop_observe", {}, objects, people, changes, log_dir=log_dir)
        except Exception:
            pass


def maybe_run_daybook(paths: LoopPaths, cfg: dict[str, str], today: str, *, run=subprocess.run) -> None:
    marker = Path(paths.daybook_marker)
    last = _read_text(marker).strip() if marker.exists() else ""
    if last == today or not (Path(paths.observation_log).exists() and Path(paths.observation_log).stat().st_size > 0):
        return
    print("[DAYBOOK] 前日分を要約中...")
    env = {
        **cfg,
        "CONSOLIDATE_MEMORY": "1",
        "LOG_FILE": paths.observation_log,
        "MEMORY_FILE": paths.memory_file,
        "TODAY": today,
        "DAYBOOK_MARKER": paths.daybook_marker,
        "LAST_DAYBOOK": last,
        "CHARACTER": _read_text(cfg.get("EHA_CHARACTER_FILE") or os.path.join(SCRIPT_DIR, "character.md")),
        "RESIDENT": cfg.get("RESIDENT", "ユーザー"),
        "SCRIPT_DIR": SCRIPT_DIR,
    }
    try:
        run(["python3", os.path.join(SCRIPT_DIR, "daybook_rollup.py")], env=env, check=False)
    except Exception:
        pass


def postprocess_loop_response(parsed: dict[str, Any], response: str, context: dict[str, Any], paths: LoopPaths, timestamp: str, *, run=subprocess.run) -> None:
    mode = context["mode"]
    queued_file = context.get("queued_listen_file")
    if queued_file:
        try:
            os.remove(str(queued_file))
        except OSError:
            pass
    record_presented_features(parsed, run=run)
    record_parse_skip_if_needed(parsed=parsed, response=response, log_dir=paths.log_dir, timestamp=timestamp, mode=mode)
    if mode == "observe":
        ingest_observe_scene(parsed, paths.log_dir)
    persist_loop_introspection(
        parsed=parsed,
        mode=mode,
        timestamp=timestamp,
        observation_log=paths.observation_log,
        explore_log=paths.explore_log,
        facts_file=os.path.join(paths.tmp_dir, f"{mode}_facts.json"),
        projected_camera_source=context.get("projected_camera_source", ""),
    )
    pending = pending_proposal_payload(parsed, timestamp=timestamp)
    write_pending_proposal(paths.pending_file, pending)
    plan = loop_speak_plan(parsed, pending)
    if plan["tts"]:
        room = first_speaker_room(context["cfg"].get("EHA_PREFS_FILE", ""))
        try:
            run(["python3", os.path.join(SCRIPT_DIR, "speak.py"), room, plan["tts"]], check=False)
        except Exception:
            pass
        append_loop_chat_log(paths.chat_log, timestamp=timestamp, source="loop", claude=plan["tts"])
    if plan["say"]:
        print(f"[SAY:{mode}] {plan['say']}")
        append_loop_chat_log(paths.chat_log, timestamp=timestamp, source=mode, claude=plan["say"])
    maybe_run_daybook(paths, context["cfg"], timestamp[:10], run=run)


def run(environ: dict[str, str] | None = None, *, run_subprocess=subprocess.run) -> dict[str, Any]:
    environ = dict(environ if environ is not None else os.environ)
    cfg = eha_config.load_config(script_dir=SCRIPT_DIR, environ=environ)
    paths = resolve_paths(cfg)
    Path(paths.log_dir).mkdir(parents=True, exist_ok=True)
    Path(paths.tmp_dir).mkdir(parents=True, exist_ok=True)
    requested_mode = cfg.get("MODE", "")
    timestamp = cfg.get("EHA_TEST_TIMESTAMP") or datetime.now().astimezone().isoformat()
    ingress_port = cfg.get("INGRESS_PORT") or "8099"
    try:
        context = build_loop_prompt_context(cfg, requested_mode, paths, run=run_subprocess)
        source = "private" if context["mode"] == "reflect" else "loop"
        web_ui_status("thinking", source, ingress_port, run=run_subprocess)
        facts_file = os.path.join(paths.tmp_dir, f"{context['mode']}_facts.json")
        try:
            os.remove(facts_file)
        except OSError:
            pass
        content_blocks = build_observe_content_blocks(context, paths, run=run_subprocess) if context["mode"] == "observe" else None
        response = invoke_loop_claude(
            user_prompt=context["user_prompt"],
            system_prompt=context["sys_prompt"],
            mode=context["mode"],
            allowed_tools=context["allowed_tools"],
            mcp_servers=context["mcp_servers"],
            environ=context["cfg"],
            content_blocks=content_blocks,
            facts_file=facts_file,
            model="sonnet" if context["mode"] == "observe" else None,
            sound_file=context.get("queued_listen_file"),
            response_schema=loop_schema(context["mode"]),
            run=run_subprocess,
        )
        parsed = parse_loop_response(response)
        postprocess_loop_response(parsed, response, context, paths, timestamp, run=run_subprocess)
        return {"mode": context["mode"], "response": response, "parsed": parsed, "context": context}
    finally:
        web_ui_status("idle", None, ingress_port, run=run_subprocess)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run one Embodied HA autonomous loop turn without daemon wiring.")
    parser.add_argument("--mode", choices=["observe", "explore", "reflect", "web", "social"], help="Force a loop mode for this turn")
    args = parser.parse_args([] if argv is None else argv)
    env = dict(os.environ)
    if args.mode:
        env["MODE"] = args.mode
    run(env)


if __name__ == "__main__":
    main(sys.argv[1:])
