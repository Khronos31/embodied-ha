# DEPRECATED: audio_speak は audio-mcp.py に統合されました。mcp-config.py はこのファイルを起動しません。
#!/usr/bin/env python3
"""TTS / 通知 MCP サーバー（embodied-ha 用）。

ツール:
  audio_speak … body_location.json を参照して現在位置に応じたスピーカーへ自動ルーティングして発話する。
    - 物理体モード: current_room のスピーカーへ（同室に複数ある場合は TCP を優先）
    - 電脳体モード（TCP スピーカーに侵入中）: そのスピーカーへ直接
    - 電脳体モード（非スピーカーデバイスに侵入中）: 発話失敗

env: EHA_PREFS_FILE, EHA_BODY_LOCATION_FILE, HA_URL, SUPERVISOR_TOKEN
"""
import datetime
import json
import os
import subprocess

from mcp_lib import serve, text, log

SPEAK_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speak.py")


def _extract_tcp_host(entity: str) -> str:
    """tcp://192.168.1.153:3333 → '192.168.1.153'"""
    if entity.startswith("tcp://"):
        return entity[6:].split(":")[0]
    return ""


def _find_tcp_speaker_by_host(speakers, host: str) -> dict:
    if isinstance(speakers, list):
        for s in speakers:
            if isinstance(s, dict) and s.get("type") == "tcp" and s.get("host") == host:
                return s
    return {}


def _find_speakers_by_room(speakers, room: str) -> list:
    if isinstance(speakers, list):
        return [s for s in speakers if isinstance(s, dict) and s.get("room") == room]
    if isinstance(speakers, dict):
        cfg = speakers.get(room)
        return [cfg] if cfg else []
    return []


def audio_speak(args):
    message = (args.get("message") or "").strip()
    if not message:
        return [text("message が必要です")], True

    # body_location.json を読んで現在位置を確認
    body_loc_path = (
        os.environ.get("EHA_BODY_LOCATION_FILE")
        or "/config/embodied-ha/body_location.json"
    )
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")

    loc = {}
    try:
        with open(body_loc_path, encoding="utf-8") as f:
            loc = json.load(f)
    except Exception:
        pass

    prefs = {}
    try:
        with open(prefs_file, encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception:
        pass

    speakers = prefs.get("speakers", [])
    current_entity = (loc.get("current_entity") or "").strip()
    current_room = (loc.get("current_room") or "").strip()
    projected_room = (loc.get("projected_room") or "").strip()

    speak_room = ""
    speak_host = ""

    if projected_room and current_entity:
        # 電脳体モード: current_entity のホストと一致する TCP スピーカーを探す
        entity_host = _extract_tcp_host(current_entity)
        if not entity_host:
            return [text(
                f"電脳体モードですが current_entity の形式が不明です: {current_entity}"
            )], True
        tcp_speaker = _find_tcp_speaker_by_host(speakers, entity_host)
        if not tcp_speaker:
            label = tcp_speaker.get("label") if tcp_speaker else current_entity
            return [text(
                f"侵入中のデバイス（{current_entity}）はスピーカーとして登録されていません。"
                "発話するには物理体に戻るか、スピーカーノードに侵入し直してください。"
            )], True
        speak_room = tcp_speaker.get("room", "")
        speak_host = entity_host

    else:
        # 物理体モード: current_room のスピーカーを探す
        if not current_room:
            return [text(
                "現在位置が不明です（body_location.json の current_room が空）"
            )], True

        room_speakers = _find_speakers_by_room(speakers, current_room)
        if not room_speakers:
            return [text(
                f"現在の部屋（{current_room}）にスピーカーが登録されていません。"
                "preferences.json の speakers に部屋を追加してください。"
            )], True

        # 同室複数スピーカーは TCP 優先（VoiceS3R ノード）、なければ先頭
        tcp_in_room = [s for s in room_speakers if s.get("type") == "tcp"]
        chosen = tcp_in_room[0] if tcp_in_room else room_speakers[0]
        speak_room = chosen.get("room", current_room)
        speak_host = chosen.get("host", "") if chosen.get("type") == "tcp" else ""

    # speak.py に委譲
    cmd = ["python3", SPEAK_PY, speak_room, message]
    if speak_host:
        cmd += ["--host", speak_host]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    detail = (r.stdout or "").strip() or (r.stderr or "").strip()
    if r.returncode != 0:
        return [text(f"発話できませんでした: {detail}")], True

    mode_desc = f"電脳体→{speak_host}" if speak_host and projected_room else f"物理体@{current_room}"
    log(f"[tts-mcp] audio_speak [{mode_desc}] room={speak_room}: {message[:40]}")
    label = chosen.get("label", speak_room) if not projected_room else speak_host

    # chat_log.jsonl に書いて last_speak 追跡が機能するようにする
    log_dir = os.environ.get("EHA_LOG_DIR", "")
    if log_dir:
        try:
            ts = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
            chat_log = os.path.join(log_dir, "chat_log.jsonl")
            entry = json.dumps({"timestamp": ts, "source": "speak", "claude": message, "user": None},
                               ensure_ascii=False)
            with open(chat_log, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass

    return [text(f"発話しました（{label}）")]


serve("tts-mcp", "1.0", {
    "audio_speak": {
        "spec": {
            "name": "audio_speak",
            "description": (
                "現在の身体の場所に応じて適切なスピーカーから声を出す。\n"
                "room 指定は不要——body_location.json を参照して自動ルーティングする:\n"
                "- 物理体モード: 現在の部屋のスピーカーへ（VoiceS3R TCP スピーカー優先）\n"
                "- 電脳体で VoiceS3R スピーカーに侵入中: そのノードから直接発話\n"
                "- 電脳体で非スピーカーデバイスに侵入中: 発話失敗（物理体に戻る必要あり）\n"
                "観察・探索ループで家人に声をかけたいときに使う。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "話す内容"},
                },
                "required": ["message"],
            },
        },
        "handler": audio_speak,
    },
})
