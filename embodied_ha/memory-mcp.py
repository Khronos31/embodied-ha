#!/usr/bin/env python3
"""記憶 MCP サーバー（embodied-ha 用）。

ツール:
  recall          … 過去ログ（観察・探索・会話・記憶）を全文検索（読み取り専用）
  remember        … 長期記憶 memory.md に一文追記
  loops_list      … 開いたループ（やりかけ・約束）一覧
  loops_add       … 新しいループを追加
  loops_close     … ループをクローズ
  record_episode  … 構造化された episode を保存
  get_episode     … episode を取得
  list_episodes   … episode 一覧を取得
  build_daybook   … 日次 daybook を生成・保存
  get_daybook     … daybook を取得
  record_causal_chain … 因果関係を保存
  get_causal_chain    … 因果関係を取得
  consolidate_memory  … 重複 episode を統合し report を保存

recall / loops は既存の recall.sh / loops.sh をサブプロセスで呼ぶ。
env: EHA_LOG_DIR, EHA_DATA_DIR, EHA_TOOLS_PATH
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import uuid
from typing import Any, Mapping

from mcp_lib import log, serve, text
import memory_state as ms
import counterfactual_state as cs
import scene_state as scenes
import sociality_state as ss
from state_utils import file_lock

_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_log_dir() -> str:
    data_dir = " ".join(str(os.environ.get("EHA_DATA_DIR") or "").split()).strip()
    if data_dir:
        return os.path.join(data_dir, "log")
    return os.path.join(_DIR, "log")


LOG_DIR = os.environ.get("EHA_LOG_DIR") or _default_log_dir()
RECALL = os.path.join(_DIR, "recall.sh")
LOOPS = os.path.join(_DIR, "loops.sh")
MEMORY_FILE = "memory.md"
_EPISODE_ID_RE = re.compile(r"\bep_[0-9A-Za-z_.-]+")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    text_value = str(value).strip().lower()
    return text_value in {"1", "true", "yes", "y", "on"}


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["EHA_LOG_DIR"] = LOG_DIR
    return env


def _json_text(data: Any) -> list[dict[str, str]]:
    return [text(json.dumps(data, ensure_ascii=False, indent=2))]


def _memory_path() -> str:
    return os.path.join(LOG_DIR, MEMORY_FILE)


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


def _ensure_memory_seed() -> str:
    content = _read_text(_memory_path())
    if content.strip():
        return content
    return "## コア記憶\n\n（まだ蓄積されていません）\n\n---\n\n## 最近の気づき\n\n"


def _append_memory_line(line: str) -> bool:
    path = _memory_path()
    with file_lock(path):
        content = _ensure_memory_seed()
        if line in content:
            return False
        if not content.endswith("\n"):
            content += "\n"
        content += f"{line}\n"
        _write_text(path, content)
        return True


def _merge_payload(args: Mapping[str, Any], key: str) -> dict[str, Any]:
    payload = {k: v for k, v in dict(args).items() if k != key}
    nested = args.get(key)
    if isinstance(nested, dict):
        payload.update(nested)
    return payload


def recall(args: dict[str, Any]):
    kw = args.get("keywords") or []
    if isinstance(kw, str):
        kw = kw.split()
    kw = [_clean(k) for k in kw if _clean(k)]
    if not kw:
        return [text("keywords が空です（例: [\"エアコン\", \"冷房\"]）")], True
    r = subprocess.run(["bash", RECALL, *kw], capture_output=True, text=True, timeout=20, env=_child_env())
    out = (r.stdout or "").strip()
    for episode_id in dict.fromkeys(_EPISODE_ID_RE.findall(out)):
        ms.update_working_memory(LOG_DIR, episode_id, "recall")
    return [text(out if out else "（ヒットなし）")]


def remember(args: dict[str, Any]):
    note = _clean(args.get("note"))
    if not note:
        return [text("note が空です")], True
    ts = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        changed = _append_memory_line(f"- {ts} | {note}")
    except Exception as e:
        return [text(f"記憶の追記に失敗: {e}")], True
    if changed:
        log(f"[memory-mcp] remember: {note[:40]}")
    else:
        log(f"[memory-mcp] remember: duplicate skipped: {note[:40]}")
    return [text("記憶に残しました")]


def loops_list(args: dict[str, Any]):
    r = subprocess.run(["bash", LOOPS, "list"], capture_output=True, text=True, timeout=10, env=_child_env())
    out = (r.stdout or "").strip()
    return [text(out if out else "（開いているループはありません）")]


def loops_add(args: dict[str, Any]):
    note = _clean(args.get("text"))
    source = _clean(args.get("source")) or "loop"
    if not note:
        return [text("text が空です")], True
    r = subprocess.run(["bash", LOOPS, "add", source, note], capture_output=True, text=True, timeout=10, env=_child_env())
    new_id = (r.stdout or "").strip()
    if r.returncode != 0:
        return [text(f"ループ追加に失敗: {r.stderr.strip()}")], True
    return [text(f"ループを追加しました（id={new_id}）")]


def loops_close(args: dict[str, Any]):
    loop_id = _clean(args.get("id"))
    reason = _clean(args.get("reason"))
    if not loop_id:
        return [text("id が必要です")], True
    cmd = ["bash", LOOPS, "close", loop_id]
    if reason:
        cmd.append(reason)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=_child_env())
    if r.returncode != 0:
        return [text(f"クローズに失敗: {r.stderr.strip()}")], True
    return [text(f"ループ {loop_id} をクローズしました")]


def record_episode(args: dict[str, Any]):
    payload = _merge_payload(args, "episode")
    episode = ms.save_episode(LOG_DIR, payload)
    ms.update_working_memory(LOG_DIR, episode.get("id", ""), "record_episode")
    log(f"[memory-mcp] episode: {episode.get('id', '')} {episode.get('summary', '')[:40]}")
    return _json_text(episode)


def record_counterfactual(args: dict[str, Any]):
    evidence = args.get("evidence")
    if isinstance(evidence, str):
        evidence = [evidence]
    elif not isinstance(evidence, list):
        evidence = []
    row = cs.record_counterfactual(
        _clean(args.get("loop")),
        _clean(args.get("intent")),
        _clean(args.get("summary")),
        _clean(args.get("rejected_because")),
        evidence,
        args.get("confidence", 0.5),
        boundary_reason=_clean(args.get("boundary_reason")),
        log_dir=LOG_DIR,
    )
    log(f"[memory-mcp] counterfactual: {row.get('loop', '')} {row.get('summary', '')[:40]}")
    return _json_text(row)


def get_episode(args: dict[str, Any]):
    episode_id = _clean(args.get("episode_id") or args.get("id"))
    if not episode_id and isinstance(args.get("episode"), dict):
        episode_id = _clean(args["episode"].get("id"))
    episode = ms.load_episode(LOG_DIR, episode_id)
    if episode.get("id"):
        ms.update_working_memory(LOG_DIR, episode["id"], "get_episode")
    return _json_text(episode)


def get_working_memory(args: dict[str, Any]):
    return _json_text(ms.get_working_memory(LOG_DIR))


def ingest_scene(args: dict[str, Any]):
    payload = _merge_payload(args, "scene")
    scene_id = scenes.ingest_scene_parse(
        _clean(payload.get("source")),
        payload.get("camera_pose") if isinstance(payload.get("camera_pose"), dict) else {},
        payload.get("objects") if isinstance(payload.get("objects"), list) else [],
        payload.get("people") if isinstance(payload.get("people"), list) else [],
        payload.get("changes") if isinstance(payload.get("changes"), list) else [],
        log_dir=LOG_DIR,
    )
    log(f"[memory-mcp] scene: {scene_id} {payload.get('source', '')}")
    return _json_text({"scene_id": scene_id})


def resolve_reference(args: dict[str, Any]):
    phrase = _clean(args.get("phrase"))
    shared_focus = ss.load_shared_focus(LOG_DIR)
    resolved = scenes.resolve_reference(phrase, shared_focus=shared_focus, log_dir=LOG_DIR)
    return _json_text(resolved or {})


def compare_recent_scenes(args: dict[str, Any]):
    return _json_text(scenes.compare_recent_scenes(_clean(args.get("source")) or None, log_dir=LOG_DIR))


def list_episodes(args: dict[str, Any]):
    day = _clean(args.get("day"))
    source = _clean(args.get("source"))
    kind = _clean(args.get("kind"))
    status = _clean(args.get("status"))
    limit = args.get("limit")
    try:
        limit_value = int(limit) if limit is not None and _clean(limit) else None
    except Exception:
        limit_value = None
    reverse = args.get("reverse")
    reverse_value = True if reverse is None else _truthy(reverse)
    episodes = ms.list_episodes(LOG_DIR, day=day or None, source=source or None, kind=kind or None, status=status or None, limit=limit_value, reverse=reverse_value)
    return _json_text(episodes)


def build_daybook(args: dict[str, Any]):
    payload = _merge_payload(args, "daybook")
    date = _clean(payload.get("date") or payload.get("day"))
    episodes = payload.get("episodes")
    if isinstance(episodes, dict):
        episodes = [episodes]
    elif not isinstance(episodes, list):
        episodes = None
    episode_ids = payload.get("episode_ids")
    themes = payload.get("themes")
    highlights = payload.get("highlights")
    open_questions = payload.get("open_questions")
    try:
        importance_cutoff = float(payload.get("importance_cutoff", 0.65))
    except Exception:
        importance_cutoff = 0.65
    try:
        raw_entry_count = payload.get("raw_entry_count")
        raw_entry_count = int(raw_entry_count) if raw_entry_count is not None and _clean(raw_entry_count) else None
    except Exception:
        raw_entry_count = None
    daybook = ms.build_daybook(
        LOG_DIR,
        date,
        episodes=episodes,
        episode_ids=episode_ids,
        summary=_clean(payload.get("summary")),
        themes=themes,
        highlights=highlights,
        open_questions=open_questions,
        importance_cutoff=importance_cutoff,
        source=_clean(payload.get("source")) or "loop",
        raw_entry_count=raw_entry_count,
        overwrite=_truthy(payload.get("overwrite")),
    )
    log(f"[memory-mcp] daybook: {daybook.get('date', '')} {daybook.get('summary', '')[:40]}")
    return _json_text(daybook)


def get_daybook(args: dict[str, Any]):
    date = _clean(args.get("date") or args.get("day"))
    return _json_text(ms.load_daybook(LOG_DIR, date))


def consolidate_memory(args: dict[str, Any]):
    payload = _merge_payload(args, "consolidation")
    scope = _clean(payload.get("scope") or payload.get("day"))
    try:
        report = ms.consolidate_memory(
            LOG_DIR,
            scope=scope,
            day=_clean(payload.get("day")),
            overwrite=_truthy(payload.get("overwrite")),
        )
    except Exception as e:
        return [text(f"記憶の統合に失敗: {e}")], True
    log(
        f"[memory-mcp] consolidate: {report.get('scope', '')} merged={len(report.get('superseded_episode_ids', []))} conflicts={len(report.get('conflict_groups', []))}"
    )
    return _json_text(report)


def _save_linked_episode(value: Any) -> str:
    if isinstance(value, dict):
        return ms.save_episode(LOG_DIR, value)["id"]
    return ""


def record_causal_chain(args: dict[str, Any]):
    payload = _merge_payload(args, "causal_chain")
    cause_episode_id = _clean(payload.get("cause_episode_id"))
    effect_episode_id = _clean(payload.get("effect_episode_id"))

    cause_episode = payload.get("cause_episode")
    effect_episode = payload.get("effect_episode")
    if not cause_episode_id:
        cause_episode_id = _save_linked_episode(cause_episode)
    if not effect_episode_id:
        effect_episode_id = _save_linked_episode(effect_episode)

    if not cause_episode_id or not effect_episode_id:
        return [text("cause_episode_id / effect_episode_id が必要です")], True

    chain_payload = {
        k: v
        for k, v in payload.items()
        if k not in {"cause_episode", "effect_episode", "cause_episode_id", "effect_episode_id"}
    }
    chain_payload["cause_episode_id"] = cause_episode_id
    chain_payload["effect_episode_id"] = effect_episode_id
    try:
        chain = ms.save_causal_chain(LOG_DIR, chain_payload, overwrite=_truthy(payload.get("overwrite")))
    except Exception as e:
        return [text(f"因果メモの保存に失敗: {e}")], True
    log(
        f"[memory-mcp] causal: {chain.get('cause_episode_id', '')} -> {chain.get('effect_episode_id', '')} {chain.get('relation', '')}"
    )
    return _json_text(chain)


def get_causal_chain(args: dict[str, Any]):
    chain_id = _clean(args.get("chain_id") or args.get("id"))
    cause_episode_id = _clean(args.get("cause_episode_id"))
    effect_episode_id = _clean(args.get("effect_episode_id"))
    if not cause_episode_id and isinstance(args.get("cause_episode"), dict):
        cause_episode_id = _clean(args["cause_episode"].get("id"))
    if not effect_episode_id and isinstance(args.get("effect_episode"), dict):
        effect_episode_id = _clean(args["effect_episode"].get("id"))
    return _json_text(
        ms.get_causal_chain(
            LOG_DIR,
            chain_id=chain_id,
            cause_episode_id=cause_episode_id,
            effect_episode_id=effect_episode_id,
        )
    )


def main() -> None:
    serve("memory-mcp", "1.0", {
        "recall": {
            "spec": {
                "name": "recall",
                "description": (
                    "過去ログ（観察・探索・会話・長期記憶）をキーワードで全文検索する。\n"
                    "長期記憶や直近の会話に無い昔のことを思い出したいときに使う。\n"
                    "複数キーワードは OR 検索。類義語も一緒に渡すと取りこぼしが減る。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "keywords": {"type": "array", "items": {"type": "string"},
                                      "description": "検索キーワード（OR検索）"},
                    },
                    "required": ["keywords"],
                },
            },
            "handler": recall,
        },
        "remember": {
            "spec": {
                "name": "remember",
                "description": (
                    "長期記憶（memory.md）に一文を追記する。\n"
                    "家の構造・家人の好み・繰り返し気づいたパターンなど、"
                    "後々まで覚えておきたいことを残す。一時的な観察は残さない。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "note": {"type": "string", "description": "記憶に残す一文"},
                    },
                    "required": ["note"],
                },
            },
            "handler": remember,
        },
        "loops_list": {
            "spec": {
                "name": "loops_list",
                "description": "開いたループ（やりかけ・家人との約束）の一覧を見る。",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "handler": loops_list,
        },
        "loops_add": {
            "spec": {
                "name": "loops_add",
                "description": (
                    "新しいループ（やりかけ・約束・後で気にかけたいこと）を追加する。\n"
                    "例: 「金曜にフィルター掃除をする約束をした」"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "ループの内容"},
                        "source": {"type": "string", "description": "loop/chat のいずれか（既定 loop）"},
                    },
                    "required": ["text"],
                },
            },
            "handler": loops_add,
        },
        "loops_close": {
            "spec": {
                "name": "loops_close",
                "description": "完了した・不要になったループを id 指定でクローズする。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "ループのid（loops_listで確認）"},
                        "reason": {"type": "string", "description": "クローズ理由（任意）"},
                    },
                    "required": ["id"],
                },
            },
            "handler": loops_close,
        },
        "record_episode": {
            "spec": {
                "name": "record_episode",
                "description": (
                    "出来事単位の episode を構造化して保存する。\n"
                    "episode オブジェクトを丸ごと渡しても、トップレベルに summary / tags / importance を置いてもよい。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "episode": {"type": "object", "description": "episode オブジェクト（任意）"},
                        "id": {"type": "string"},
                        "episode_id": {"type": "string"},
                        "timestamp": {"type": "string"},
                        "day": {"type": "string"},
                        "kind": {"type": "string"},
                        "source": {"type": "string"},
                        "summary": {"type": "string"},
                        "detail": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "entities": {"type": "array", "items": {"type": "string"}},
                        "actors": {"type": "array", "items": {"type": "string"}},
                        "importance": {"type": "number"},
                        "evidence": {"type": "array", "items": {"type": "object"}, "description": "根拠。camera_context / audio_context オブジェクトを含められる"},
                        "status": {"type": "string"},
                        "links": {"type": "object"},
                    },
                },
            },
            "handler": record_episode,
        },
        "record_counterfactual": {
            "spec": {
                "name": "record_counterfactual",
                "description": (
                    "声をかける・操作する・提案するつもりだったが、境界や確信不足でやめたことを記録する。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "loop": {"type": "string", "description": "loop/chat"},
                        "intent": {"type": "string", "description": "speak/act/propose"},
                        "summary": {"type": "string", "description": "しようとしたことの短い説明"},
                        "rejected_because": {"type": "string", "description": "quiet_window/low_confidence/turn_taking など"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number"},
                        "boundary_reason": {"type": "string"},
                    },
                    "required": ["loop", "intent", "summary", "rejected_because", "evidence", "confidence"],
                },
            },
            "handler": record_counterfactual,
        },
        "get_episode": {
            "spec": {
                "name": "get_episode",
                "description": "保存済み episode を id で取得する。未登録や空 id なら default を返す。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "episode_id": {"type": "string"},
                        "id": {"type": "string"},
                    },
                },
            },
            "handler": get_episode,
        },
        "get_working_memory": {
            "spec": {
                "name": "get_working_memory",
                "description": "直近で活性化した episode を activation の高い順に最大5件返す。",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "handler": get_working_memory,
        },
        "ingest_scene": {
            "spec": {
                "name": "ingest_scene",
                "description": "カメラ観察から抽出した objects/people/changes を scene として保存する。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "scene": {"type": "object"},
                        "source": {"type": "string"},
                        "camera_pose": {"type": "object"},
                        "objects": {"type": "array", "items": {"type": "object"}},
                        "people": {"type": "array", "items": {"type": "object"}},
                        "changes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["source"],
                },
            },
            "handler": ingest_scene,
        },
        "resolve_reference": {
            "spec": {
                "name": "resolve_reference",
                "description": "「それ」「あれ」「右のやつ」などを直近 scene と shared_focus から候補解決する。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"phrase": {"type": "string"}},
                    "required": ["phrase"],
                },
            },
            "handler": resolve_reference,
        },
        "compare_recent_scenes": {
            "spec": {
                "name": "compare_recent_scenes",
                "description": "同じ camera/source の直近2 scene を比較し、見えた差分を返す。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"source": {"type": "string"}},
                },
            },
            "handler": compare_recent_scenes,
        },
        "list_episodes": {
            "spec": {
                "name": "list_episodes",
                "description": "episode を一覧化する。day/source/kind/limit で絞り込める。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "string"},
                        "source": {"type": "string"},
                        "kind": {"type": "string"},
                        "status": {"type": "string"},
                        "limit": {"type": "integer"},
                        "reverse": {"type": "boolean"},
                    },
                },
            },
            "handler": list_episodes,
        },
        "build_daybook": {
            "spec": {
                "name": "build_daybook",
                "description": (
                    "指定日の daybook を生成・保存する。既存の daybook があれば、それをそのまま返す。\n"
                    "episodes か episode_ids を渡して structured な日次圧縮を保存できる。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "daybook": {"type": "object", "description": "daybook オブジェクト（任意）"},
                        "date": {"type": "string"},
                        "day": {"type": "string"},
                        "episode_ids": {"type": "array", "items": {"type": "string"}},
                        "episodes": {"type": "array", "items": {"type": "object"}},
                        "summary": {"type": "string"},
                        "themes": {"type": "array", "items": {"type": "string"}},
                        "highlights": {"type": "array", "items": {"type": "object"}},
                        "open_questions": {"type": "array", "items": {"type": "string"}},
                        "importance_cutoff": {"type": "number"},
                        "source": {"type": "string"},
                        "raw_entry_count": {"type": "integer"},
                        "overwrite": {"type": "boolean"},
                    },
                },
            },
            "handler": build_daybook,
        },
        "get_daybook": {
            "spec": {
                "name": "get_daybook",
                "description": "保存済み daybook を取得する。未登録や空 date なら default を返す。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string"},
                        "day": {"type": "string"},
                    },
                },
            },
            "handler": get_daybook,
        },
        "record_causal_chain": {
            "spec": {
                "name": "record_causal_chain",
                "description": (
                    "出来事どうしの因果関係を保存する。\n"
                    "cause_episode_id / effect_episode_id を指定し、必要なら cause_episode / effect_episode も丸ごと渡せる。\n"
                    "relation は caused / enabled / prevented / correlated のいずれかに正規化される。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "causal_chain": {"type": "object", "description": "causal_chain オブジェクト（任意）"},
                        "id": {"type": "string"},
                        "chain_id": {"type": "string"},
                        "cause_episode_id": {"type": "string"},
                        "effect_episode_id": {"type": "string"},
                        "cause_episode": {"type": "object"},
                        "effect_episode": {"type": "object"},
                        "relation": {"type": "string"},
                        "summary": {"type": "string"},
                        "mechanism": {"type": "string"},
                        "confidence": {"type": "number"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "support_episode_ids": {"type": "array", "items": {"type": "string"}},
                        "status": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                    },
                },
            },
            "handler": record_causal_chain,
        },
        "get_causal_chain": {
            "spec": {
                "name": "get_causal_chain",
                "description": "保存済み causal_chain を取得する。未登録や空 pair なら default を返す。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "chain_id": {"type": "string"},
                        "id": {"type": "string"},
                        "cause_episode_id": {"type": "string"},
                        "effect_episode_id": {"type": "string"},
                        "cause_episode": {"type": "object"},
                        "effect_episode": {"type": "object"},
                    },
                },
            },
            "handler": get_causal_chain,
        },
        "consolidate_memory": {
            "spec": {
                "name": "consolidate_memory",
                "description": (
                    "episode の重複を fingerprint で統合し、矛盾は conflict として残した consolidation report を保存する。\n"
                    "scope か day を渡すと report ファイル名の目安になる（省略時は all）。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string"},
                        "day": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                    },
                },
            },
            "handler": consolidate_memory,
        },
    })


if __name__ == "__main__":
    main()
