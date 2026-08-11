#!/usr/bin/env python3
"""音声 listen 用 MCP サーバー。"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import audio_stt
from audio_source_resolve import DEFAULT_SOURCE, resolve_audio_source
import concentrate_hearing_files
from embodied_action import action_fields_for_sensory, apply_action_to_body_state
from media_registry import resolve_media_item
from mcp_lib import log, serve, text
from spatial_context import classify_sensory_origin
from state_utils import clean, get_device_capabilities, now, parse_ts

MAX_DURATION = 30
TMP_DIR = Path("/tmp/embodied-ha/audio")
CONCENTRATE_HEARING_COOLDOWN_SECONDS = 60
CONCENTRATE_HEARING_FILE_TTL_SECONDS = (
    concentrate_hearing_files.CONCENTRATE_HEARING_FILE_TTL_SECONDS
)
CONCENTRATE_HEARING_DIR = concentrate_hearing_files.CONCENTRATE_HEARING_DIR
DEFAULT_ACTIVE_LISTEN_LOG_FILE = "/data/embodied-ha/log/active_listen_log.jsonl"
DEFAULT_AUDITORY_EVENTS_FILE = "/data/embodied-ha/log/auditory_events.jsonl"
_ACTIVE_LISTEN_LOCK = threading.Lock()
_CONCENTRATE_HEARING_FILES: set[str] = set()


def _cleanup_created_concentrate_hearing_files() -> None:
    for raw_path in tuple(_CONCENTRATE_HEARING_FILES):
        try:
            Path(raw_path).unlink()
        except FileNotFoundError:
            pass
        finally:
            _CONCENTRATE_HEARING_FILES.discard(raw_path)


atexit.register(_cleanup_created_concentrate_hearing_files)


def default_audio_log_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "audio_log.jsonl")
    return "/config/embodied-ha/log/audio_log.jsonl"


def default_active_listen_log_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "active_listen_log.jsonl")
    return "/config/embodied-ha/log/active_listen_log.jsonl"


def default_auditory_events_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "auditory_events.jsonl")
    return "/config/embodied-ha/log/auditory_events.jsonl"


def default_non_speech_audio_events_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "non_speech_audio_events.jsonl")
    return "/config/embodied-ha/log/non_speech_audio_events.jsonl"


def default_audio_event_tags_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "audio_event_tags.jsonl")
    return "/config/embodied-ha/log/audio_event_tags.jsonl"


AUDIO_LOG_FILE = clean(os.environ.get("EHA_AUDIO_LOG_FILE")) or default_audio_log_path()
ACTIVE_LISTEN_LOG_FILE = (
    clean(os.environ.get("EHA_ACTIVE_LISTEN_LOG_FILE"))
    or default_active_listen_log_path()
    or DEFAULT_ACTIVE_LISTEN_LOG_FILE
)
AUDITORY_EVENTS_FILE = (
    clean(os.environ.get("EHA_AUDITORY_EVENTS_FILE"))
    or default_auditory_events_path()
    or DEFAULT_AUDITORY_EVENTS_FILE
)
NON_SPEECH_AUDIO_EVENTS_FILE = clean(os.environ.get("EHA_NON_SPEECH_AUDIO_EVENTS_FILE")) or default_non_speech_audio_events_path()
AUDIO_EVENT_TAGS_FILE = clean(os.environ.get("EHA_AUDIO_EVENT_TAGS_FILE")) or default_audio_event_tags_path()


def active_listen_log_name() -> str:
    return os.path.basename(ACTIVE_LISTEN_LOG_FILE) or "active_listen_log.jsonl"


def concentrate_hearing_state_path() -> Path:
    data_dir = clean(os.environ.get("EHA_DATA_DIR")) or "/config/embodied-ha"
    configured = clean(os.environ.get("EHA_CONCENTRATE_HEARING_STATE_FILE"))
    return Path(configured) if configured else Path(data_dir) / "runtime" / "concentrate_hearing_state.json"


def concentrate_hearing_cooldown_seconds() -> int:
    try:
        configured = clean(os.environ.get("EHA_CONCENTRATE_HEARING_COOLDOWN_SECONDS"))
        return max(5, int(configured or CONCENTRATE_HEARING_COOLDOWN_SECONDS))
    except (OSError, json.JSONDecodeError):
        return CONCENTRATE_HEARING_COOLDOWN_SECONDS


def _is_antigravity_harness() -> bool:
    return clean(os.environ.get("EHA_AGENT_HARNESS")).lower() in {"agy", "antigravity", "gemini"}


def _prune_stale_concentrate_hearing_files(current_time: float | None = None) -> None:
    concentrate_hearing_files.prune_stale_files(
        current_time,
        directory=CONCENTRATE_HEARING_DIR,
        ttl_seconds=CONCENTRATE_HEARING_FILE_TTL_SECONDS,
    )
    _CONCENTRATE_HEARING_FILES.intersection_update(
        {path for path in _CONCENTRATE_HEARING_FILES if Path(path).exists()}
    )


def _read_concentrate_hearing_success_epoch(path: Path) -> float | None:
    try:
        with path.open(encoding="utf-8") as f:
            payload = json.load(f)
        value = float(payload.get("last_success_epoch"))
        return value if value > 0 else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_concentrate_hearing_success_epoch(path: Path, epoch: float) -> None:
    _write_json_atomic(str(path), {"last_success_epoch": epoch})


def _concentrate_hearing_lock_path() -> Path:
    state_path = concentrate_hearing_state_path()
    return state_path.with_name(f"{state_path.name}.lock")


def _write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _prefs_path() -> str:
    return os.environ.get("EHA_PREFS_FILE", "")


def normalize_source_uri(value: str) -> str:
    return clean(value)


def load_preferences() -> dict:
    path = _prefs_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_mic_configs() -> list[dict]:
    sources = load_preferences().get("mics")
    if not isinstance(sources, list):
        sources = []

    normalized = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        source = normalize_source_uri(item.get("source"))
        if not source.startswith("rtsp://"):
            continue
        config = dict(item)
        config["source"] = source
        config["label"] = clean(item.get("label")) or source
        normalized.append(config)
    return normalized


def load_mics() -> list[dict]:
    return [
        {"source": item["source"], "label": item["label"]}
        for item in load_mic_configs()
    ]


def _body_location_path() -> str:
    return (
        clean(os.environ.get("EHA_BODY_LOCATION_FILE"))
        or "/config/embodied-ha/body_location.json"
    )


def _load_body_location() -> dict:
    try:
        with open(_body_location_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def auto_listen_source(body_loc: dict, source_configs: list[dict]) -> str:
    return resolve_audio_source(body_loc, source_configs, default_source=DEFAULT_SOURCE)


def default_listen_source() -> str:
    return normalize_source_uri(resolve_audio_source(_load_body_location(), load_mic_configs(), default_source=DEFAULT_SOURCE))


def load_stt_provider() -> str | None:
    return audio_stt.load_stt_provider()


def load_stt_language() -> str:
    return audio_stt.load_stt_language()


def build_listen_spec() -> dict:
    sources = load_mics()
    source_lines = "\n".join(
        f'  - "{s["source"]}"（{s["label"]}）' for s in sources
    )
    return {
        "name": "listen",
        "description": (
            "短時間だけ音を聴く。\n"
            "source を省略すると body_location.json を参照して自動選択する:\n"
            "- 電脳体: 投射先の部屋に登録されたRTSPマイク\n"
            "- 物理体: 現在の部屋に登録されたRTSPマイク\n"
            f"source を明示する場合の利用可能なソース:\n{source_lines}\n"
            "transcribe は必要なときだけ true にする。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "録音ソース URI。省略すると body_location から自動解決する。",
                },
                "duration": {
                    "type": "integer",
                    "description": "録音秒数。デフォルト 5、最大 30",
                },
                "transcribe": {
                    "type": "boolean",
                    "description": "STT を行うか。デフォルト false",
                },
            },
        },
    }

TOOL_LISTEN = build_listen_spec()

TOOL_LISTEN_MEDIA = {
    "name": "listen_media",
    "description": (
        "番組音・音楽等のメディア音声を聴く。マイク(部屋を聞く耳)とは別で、侵入不要。\n"
        "audio_media に登録された音声ソースを id / source / label で解決して録音・STT する。\n"
        "current_entity を問わず使える。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "audio_media の id / source / label。",
            },
            "duration": {
                "type": "integer",
                "description": "録音秒数。デフォルト 5、最大 30",
            },
            "transcribe": {
                "type": "boolean",
                "description": "STT を行うか。デフォルト false",
            },
        },
    },
}

TOOL_READ_AUDIO_LOG = {

    "name": "read_audio_log",
    "description": "旧常時STTの生ログを読む。本体からは新規生成せず、既存のVAD/STT診断履歴を参照するためのツール。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返す件数。デフォルト20",
            },
            "since_minutes": {
                "type": "integer",
                "description": "指定した分以内のログだけ返す",
            },
        },
    },
}


TOOL_READ_HEARD_AUDIO_LOG = {
    "name": "read_heard_audio_log",
    "description": "旧常時STTで聞こえた発話履歴を読む。本体からは新規生成しない。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返す件数。デフォルト20",
            },
            "since_minutes": {
                "type": "integer",
                "description": "指定した分以内のログだけ返す",
            },
        },
    },
}


TOOL_READ_ACTIVE_LISTEN_LOG = {
    "name": "read_active_listen_log",
    "description": "最近、自分から listen で聞きに行った音声ログを読む。旧常時STT履歴とは別。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返す件数。デフォルト20",
            },
            "since_minutes": {
                "type": "integer",
                "description": "指定した分以内のログだけ返す",
            },
        },
    },
}


TOOL_READ_NON_SPEECH_AUDIO_EVENTS = {
    "name": "read_non_speech_audio_events",
    "description": "最近の非音声聴覚イベントを読む。STT対象外・STT失敗だが特徴的な音のDSP特徴量とwav_refを含む。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返す件数。デフォルト20",
            },
            "since_minutes": {
                "type": "integer",
                "description": "指定した分以内のログだけ返す",
            },
        },
    },
}


TOOL_READ_AUDIO_EVENT_TAGS = {
    "name": "read_audio_event_tags",
    "description": "非音声聴覚イベントに付いた manual/gemini 等のタグ履歴を読む。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返す件数。デフォルト20",
            },
            "since_minutes": {
                "type": "integer",
                "description": "指定した分以内のログだけ返す",
            },
        },
    },
}


def _source_map() -> dict[str, dict]:
    return {clean(item.get("source")): item for item in load_mics()}


def _source_config_map() -> dict[str, dict]:
    return {clean(item.get("source")): item for item in load_mic_configs()}


def _audio_media_source_uri(item: dict) -> str:
    source = clean(item.get("source"))
    if not source:
        return ""
    if "://" in source:
        return source
    return f"rtsp://localhost:8554/{source}"


def _resolve_audio_media_item(source: str | None = None) -> tuple[dict, str]:
    prefs = load_preferences()
    item, _, _ = resolve_media_item(prefs, source, buckets=("audio_media",), allow_single=True)
    if not item:
        return {}, ""
    return item, _audio_media_source_uri(item)


def label_for_source(source: str) -> str:
    item = _source_config_map().get(clean(source))
    return clean(item.get("label")) if item else clean(source)


def sensory_for_source(source: str, label: str, modality: str = "auditory") -> dict:
    item = _source_config_map().get(clean(source)) or {}
    return classify_sensory_origin(
        source=source,
        label=label,
        room=item.get("room") if isinstance(item, dict) else "",
        area=item.get("area") if isinstance(item, dict) else "",
        entity_id=item.get("entity_id") if isinstance(item, dict) else "",
        note=item.get("note") if isinstance(item, dict) else "",
        modality=modality,
    )


def active_listen_retention_hours(_source: str) -> int:
    try:
        fallback = int(clean(os.environ.get("EHA_ACTIVE_LISTEN_RETENTION_HOURS")) or 24)
    except (TypeError, ValueError):
        fallback = 24
    return max(1, fallback)


def normalize_duration(value) -> int:
    try:
        seconds = int(value) if clean(value) else 5
    except Exception:
        seconds = 5
    return max(1, min(MAX_DURATION, seconds))


def validate_concentrate_hearing_duration(value) -> int:
    if value is None:
        return 5
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DURATION:
        raise ValueError(f"duration は1〜{MAX_DURATION}の整数で指定してください。")
    return value


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def build_record_command(source: str, duration: int) -> list[str]:
    source = normalize_source_uri(source)
    if not source.startswith("rtsp://"):
        raise ValueError("only rtsp microphone sources are supported")
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", source,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-t", str(duration),
        "-y",
    ]


def parse_volumedetect(stderr: str) -> tuple[float | None, float | None]:
    peak_match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr or "")
    mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr or "")
    peak = float(peak_match.group(1)) if peak_match else None
    mean = float(mean_match.group(1)) if mean_match else None
    return peak, mean


def analyze_volume(path: str) -> tuple[float | None, float | None]:
    result = subprocess.run(
        ["ffmpeg", "-i", path, "-af", "volumedetect", "-f", "null", "/dev/null"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    return parse_volumedetect((result.stderr or "") + (result.stdout or ""))


def has_sound_from_peak(peak_db: float | None) -> bool:
    return peak_db is not None and peak_db > -50.0


def transcribe_via_ha(path: str, provider: str) -> str | None:
    return audio_stt.transcribe_via_ha(path, provider)


def transcribe_via_local(path: str) -> str | None:
    return audio_stt.transcribe_via_local(path)


def transcribe_audio(path: str) -> str | None:
    provider = load_stt_provider()
    if provider:
        transcript = transcribe_via_ha(path, provider)
        if transcript:
            return transcript
    return transcribe_via_local(path)



def read_audio_log(args: dict):
    return read_jsonl_log(AUDIO_LOG_FILE, args)


def read_heard_audio_log(args: dict):
    return read_jsonl_log(AUDITORY_EVENTS_FILE, args)


def read_jsonl_log(path: str, args: dict):
    try:
        limit = int(args.get("limit", 20) or 20)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 200))

    since_minutes = args.get("since_minutes")
    cutoff = None
    if since_minutes is not None:
        try:
            minutes = int(since_minutes)
            if minutes > 0:
                cutoff = now() - timedelta(minutes=minutes)
        except Exception:
            cutoff = None

    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                if cutoff is not None:
                    ts = parse_ts(entry.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                entries.append(entry)
    except FileNotFoundError:
        entries = []
    return [text(json.dumps(entries[-limit:], ensure_ascii=False))]


def append_active_listen_log(entry: dict, retention_hours: int, source: str) -> None:
    path = ACTIVE_LISTEN_LOG_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cutoff = now() - timedelta(hours=max(1, retention_hours))
    source_key = clean(source)

    with _ACTIVE_LISTEN_LOCK:
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
                        if source_key and clean(parsed.get("source")) == source_key and ts and ts < cutoff:
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


def record_active_listen(entry: dict, source: str) -> None:
    try:
        append_active_listen_log(entry, active_listen_retention_hours(source), source)
    except Exception:
        pass


def build_audio_context(entry: dict) -> dict:
    return {
        "type": "active_listen",
        "timestamp": entry.get("timestamp"),
        "actor": entry.get("actor"),
        "source": entry.get("source"),
        "source_label": entry.get("source_label"),
        "duration_sec": entry.get("duration_sec"),
        "has_sound": entry.get("has_sound"),
        "peak_db": entry.get("peak_db"),
        "mean_db": entry.get("mean_db"),
        "transcribe_requested": entry.get("transcribe_requested"),
        "transcript": entry.get("transcript"),
        "stt_provider": entry.get("stt_provider"),
        "stt_language": entry.get("stt_language"),
        "body_room": entry.get("body_room"),
        "body_room_label": entry.get("body_room_label"),
        "source_room": entry.get("source_room"),
        "source_room_label": entry.get("source_room_label"),
        "sensory_origin": entry.get("sensory_origin"),
        "access_mode": entry.get("access_mode"),
        "move_cost": entry.get("move_cost"),
        "move_path": entry.get("move_path"),
        "action_mode": entry.get("action_mode"),
        "action_cost": entry.get("action_cost"),
        "target_host": entry.get("target_host"),
        "log_ref": {
            "file": active_listen_log_name(),
            "timestamp": entry.get("timestamp"),
        },
    }


def read_active_listen_log(args: dict):
    return read_jsonl_log(ACTIVE_LISTEN_LOG_FILE, args)


def read_non_speech_audio_events(args: dict):
    return read_jsonl_log(NON_SPEECH_AUDIO_EVENTS_FILE, args)


def read_audio_event_tags(args: dict):
    return read_jsonl_log(AUDIO_EVENT_TAGS_FILE, args)


# ─── audio_speak ──────────────────────────────────────────────────────────────

_SPEAK_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speak.py")


def _find_ha_speaker_by_entity(speakers, entity_id: str) -> dict:
    if isinstance(speakers, list):
        for s in speakers:
            if isinstance(s, dict):
                if s.get("entity") == entity_id or s.get("media_player") == entity_id:
                    return s
    return {}


def _find_speakers_by_room(speakers, room: str) -> list:
    if isinstance(speakers, list):
        return [s for s in speakers if isinstance(s, dict) and s.get("room") == room]
    if isinstance(speakers, dict):
        cfg = speakers.get(room)
        return [cfg] if cfg else []
    return []


TOOL_SPEAK = {
    "name": "speak",
    "description": (
        "物理体として声を出す。物理体モード専用。\n"
        "現在の部屋に応じたスピーカーへ自動ルーティングする（デバイス ID の指定不要・enter_cyberspace 不要）。\n"
        "【別の部屋で話したいとき】move_to でその部屋に移動してから speak を呼ぶ。"
        "スピーカーの entity を指定したり enter_cyberspace したりする必要はない。\n"
        "電脳体モードの場合はエラーを返す。電脳体でスピーカーから発話したい場合は use_device_speaker を使う。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "話す内容。file_pathとは排他"},
            "file_path": {"type": "string", "description": "再生する最長10分の音声ファイルパス。HA Media Sourceへ永続保存して再生する。messageとは排他"},
        },
        "required": [],
    },
}

TOOL_USE_DEVICE_SPEAKER = {
    "name": "use_device_speaker",
    "description": (
        "現在侵入中のスピーカーデバイスから声を出す。電脳体でスピーカーデバイスに侵入中のみ使用可能。\n"
        "物理体モード、またはスピーカー以外のデバイスに侵入中の場合はエラーを返す。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "話す内容。file_pathとは排他"},
            "file_path": {"type": "string", "description": "再生する最長10分の音声ファイルパス。HA Media Sourceへ永続保存して再生する。messageとは排他"},
        },
        "required": [],
    },
}

TOOL_USE_DEVICE_MICROPHONE = {
    "name": "use_device_microphone",
    "description": (
        "現在侵入中のマイクデバイスで音声を取得する。電脳体でマイクデバイスに侵入中のみ使用可能。\n"
        "物理体モード、またはマイク以外のデバイスに侵入中の場合はエラーを返す。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "duration": {"type": "integer", "description": "録音秒数（デフォルト5）"},
            "transcribe": {"type": "boolean", "description": "STT文字起こしを行うか（デフォルト false）"},
        },
        "required": [],
    },
}

TOOL_CONCENTRATE_HEARING = {
    "name": "concentrate_hearing",
    "description": (
        "耳を澄ませ、その場で録音した音声ファイルのパスを返す。物理体モード専用。"
        "録音時間はデフォルト5秒、最大30秒。音声の内容や文字起こしは返さない。\n"
        "返り値の file_path を同じターンで必ず view_file に直接渡し、音として確認すること。"
        "command、shell、terminal、Python、外部スクリプト、外部STTで解析してはいけない。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "duration": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "default": 5,
                "description": "録音秒数。デフォルト 5、最大 30",
            },
        },
        "required": [],
    },
}


def _record_concentrate_hearing_webm(source: str, duration: int) -> str:
    source = normalize_source_uri(source)
    if source not in _source_map() and not source.startswith("rtsp://"):
        raise RuntimeError(f"unknown source: {source}")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")

    CONCENTRATE_HEARING_DIR.mkdir(parents=True, exist_ok=True)
    _prune_stale_concentrate_hearing_files()
    wav_path = ""
    webm_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=CONCENTRATE_HEARING_DIR,
            prefix="eha-concentrate-hearing-",
            suffix=".wav",
            delete=False,
        ) as wav_file:
            wav_path = wav_file.name
        with tempfile.NamedTemporaryFile(
            dir=CONCENTRATE_HEARING_DIR,
            prefix="eha-concentrate-hearing-",
            suffix=".webm",
            delete=False,
        ) as webm_file:
            webm_path = webm_file.name

        command = build_record_command(source, duration) + [wav_path]
        command[0] = ffmpeg
        recorded = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=duration + 15,
            check=False,
        )
        if recorded.returncode != 0:
            raise RuntimeError(clean(recorded.stderr) or clean(recorded.stdout) or "recording failed")

        converted = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", wav_path, "-vn", "-c:a", "libopus", webm_path],
            capture_output=True,
            text=True,
            timeout=duration + 15,
            check=False,
        )
        if converted.returncode != 0:
            raise RuntimeError(clean(converted.stderr) or clean(converted.stdout) or "WebM conversion failed")
        if not os.path.isfile(webm_path) or os.path.getsize(webm_path) <= 0:
            raise RuntimeError("WebM conversion produced an empty file")
        os.chmod(webm_path, 0o600)
        _CONCENTRATE_HEARING_FILES.add(webm_path)
        return webm_path
    except Exception:
        if webm_path:
            try:
                os.unlink(webm_path)
            except FileNotFoundError:
                pass
        raise
    finally:
        if wav_path:
            try:
                os.unlink(wav_path)
            except FileNotFoundError:
                pass


def _current_body_state() -> tuple[dict, str, str, str]:
    loc = _load_body_location()
    current_entity = clean(loc.get("current_entity"))
    current_room = clean(loc.get("current_room"))
    projected_room = clean(loc.get("projected_room"))
    return loc, current_entity, current_room, projected_room


def _broken_cyber_state_error(current_entity: str, projected_room: str) -> tuple[list, bool] | None:
    if current_entity and not projected_room:
        return [text("状態が壊れています（current_entity あり projected_room なし）。body_repair を呼んでください。")], True
    return None


def _resolve_room_speaker(speakers: list, room: str) -> dict:
    room_speakers = _find_speakers_by_room(speakers, room)
    return room_speakers[0] if room_speakers else {}


def _run_speak(speak_room: str, message: str = "", file_path: str = "") -> tuple[list, bool]:
    cmd = ["python3", _SPEAK_PY, speak_room]
    if file_path:
        cmd += ["--file-path", file_path]
    else:
        cmd.append(message)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    detail = (r.stdout or "").strip() or (r.stderr or "").strip()
    if r.returncode != 0:
        action = "再生" if file_path else "発話"
        return [text(f"{action}できませんでした: {detail}")], True
    return [], False


def _speak_payload(args: dict) -> tuple[str, str, tuple[list, bool] | None]:
    message = clean(args.get("message"))
    file_path = clean(args.get("file_path"))
    if bool(message) == bool(file_path):
        return "", "", ([text("message と file_path のどちらか一方だけが必要です")], True)
    return message, file_path, None


def _speak_with_entry(entry: dict, message: str = "", *, file_path: str = "", mode_desc: str = "") -> tuple[list, bool]:
    speak_room = clean(entry.get("room")) or ""
    label = clean(entry.get("label")) or speak_room
    result, is_error = _run_speak(speak_room, message, file_path=file_path)
    if is_error:
        return result, True
    if file_path:
        log(f"[audio-mcp] play file [{mode_desc}] room={speak_room}: {file_path}")
        return [text(f"再生しました（{label}）")], False
    log(f"[audio-mcp] speak [{mode_desc}] room={speak_room}: {message[:40]}")
    log_dir = os.environ.get("EHA_LOG_DIR", "")
    if log_dir:
        try:
            ts = now().isoformat(timespec="seconds")
            log_entry = json.dumps(
                {"timestamp": ts, "source": "speak", "agent": message, "user": None},
                ensure_ascii=False,
            )
            with open(os.path.join(log_dir, "chat_log.jsonl"), "a", encoding="utf-8") as f:
                f.write(log_entry + "\n")
        except Exception:
            pass
    return [text(f"発話しました（{label}）")], False


def _audio_listen_from_source(
    source: str,
    duration: int,
    transcribe: bool,
    *,
    source_label_override: str | None = None,
    extra_payload: dict | None = None,
    media_kind_hint: str | None = None,
):
    source = normalize_source_uri(source)
    timestamp = now().isoformat(timespec="seconds")
    actor = clean(os.environ.get("EHA_ACTOR")) or "unknown"
    source_label = source_label_override or label_for_source(source)
    sensory = sensory_for_source(source, source_label)

    def base_payload() -> dict:
        return {
            "timestamp": timestamp,
            "kind": "active_listen",
            "type": "active_listen",
            "actor": actor,
            "source": source,
            "source_label": source_label,
            "duration": duration,
            "duration_sec": duration,
            "transcribe_requested": transcribe,
            **sensory,
            **action_fields_for_sensory(sensory, host=source),
        }

    if source not in _source_map() and not source.startswith("rtsp://"):
        payload = {**base_payload(), "error": f"unknown source: {source}"}
        record_active_listen(payload, source)
        return [text(json.dumps({"error": payload["error"]}, ensure_ascii=False))], True

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        payload = {**base_payload(), "error": "ffmpeg not found"}
        record_active_listen(payload, source)
        return [text(json.dumps({"error": "ffmpeg not found"}, ensure_ascii=False))], True

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=TMP_DIR, suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        command = build_record_command(source, duration) + [tmp_path]
        command[0] = ffmpeg
        record = subprocess.run(command, capture_output=True, text=True, timeout=duration + 15)
        if record.returncode != 0:
            err_msg = clean(record.stderr) or clean(record.stdout) or "recording failed"
            payload = {**base_payload(), "error": err_msg}
            record_active_listen(payload, source)
            return [text(json.dumps({"error": err_msg, "source": source}, ensure_ascii=False))], True

        peak_db, mean_db = analyze_volume(tmp_path)
        payload = {
            **base_payload(),
            "has_sound": has_sound_from_peak(peak_db),
            "peak_db": peak_db,
            "mean_db": mean_db,
            "transcript": None,
            "stt_provider": None,
            "stt_language": None,
        }
        if extra_payload:
            payload.update(extra_payload)
        if transcribe:
            payload["stt_provider"] = load_stt_provider()
            payload["stt_language"] = load_stt_language()
            payload["transcript"] = transcribe_audio(tmp_path)
        try:
            apply_action_to_body_state(
                action_mode=payload.get("action_mode"),
                action_cost=payload.get("action_cost"),
                target_room=payload.get("source_room"),
                target_host=payload.get("target_host"),
                move_cost=payload.get("move_cost"),
            )
        except Exception:
            pass
        record_active_listen(payload, source)
        payload["audio_context"] = build_audio_context(payload)
        content = [text(json.dumps(payload, ensure_ascii=False))]
        if media_kind_hint:
            content.append(text(media_kind_hint))
        return content
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


# ─────────────────────────────────────────────────────────────────────────────

def speak(args: dict):
    message, file_path, payload_error = _speak_payload(args if isinstance(args, dict) else {})
    if payload_error:
        return payload_error
    _, current_entity, current_room, _ = _current_body_state()
    if current_entity:
        return [text("電脳体モードでは speak は使えません。use_device_speaker を使ってください。")], True
    if not current_room:
        return [text("現在位置が不明です（body_location.json の current_room が空）")], True
    speakers = load_preferences().get("speakers", [])
    chosen = _resolve_room_speaker(speakers, current_room)
    if not chosen:
        return [text(
            f"現在の部屋（{current_room}）にスピーカーが登録されていません。"
            "preferences.json の speakers に部屋を追加してください。"
        )], True
    return _speak_with_entry(chosen, message, file_path=file_path, mode_desc=f"物理体@{current_room}")


def use_device_speaker(args: dict):
    message, file_path, payload_error = _speak_payload(args if isinstance(args, dict) else {})
    if payload_error:
        return payload_error
    _, current_entity, current_room, projected_room = _current_body_state()
    broken_state = _broken_cyber_state_error(current_entity, projected_room)
    if broken_state:
        return broken_state
    if not current_entity:
        return [text("物理体モードでは use_device_speaker は使えません。speak を使ってください。")], True
    prefs = load_preferences()
    caps = get_device_capabilities(current_entity, prefs)
    speaker = caps.get("speaker")
    if not caps.get("is_speaker") or not isinstance(speaker, dict):
        return [text(f"現在侵入中のデバイス（{current_entity}）はスピーカーデバイスではありません。")], True
    return _speak_with_entry(speaker, message, file_path=file_path, mode_desc=f"電脳体@{current_entity}")


def listen(args: dict):
    _, current_entity, _, projected_room = _current_body_state()
    broken_state = _broken_cyber_state_error(current_entity, projected_room)
    if broken_state:
        return broken_state
    if current_entity:
        return [text("電脳体モードでは listen は使えません。use_device_microphone を使ってください。")], True
    _body_loc = _load_body_location()
    _src_cfgs = load_mic_configs()
    requested_source = clean(args.get("source"))
    source = normalize_source_uri(requested_source) if requested_source else normalize_source_uri(resolve_audio_source(_body_loc, _src_cfgs, default_source=DEFAULT_SOURCE))
    if not source:
        return [text(
            "利用できるマイクが登録されていません。"
            "preferences.json の mics に音声ソースを追加してください。"
        )], True
    duration = normalize_duration(args.get("duration"))
    transcribe_arg = args.get("transcribe", False)
    transcribe = transcribe_arg if isinstance(transcribe_arg, bool) else _truthy(transcribe_arg)
    return _audio_listen_from_source(source, duration, transcribe)


def listen_media(args: dict):
    _, current_entity, _, projected_room = _current_body_state()
    broken_state = _broken_cyber_state_error(current_entity, projected_room)
    if broken_state:
        return broken_state
    source_arg = clean(args.get("source"))
    duration = normalize_duration(args.get("duration"))
    transcribe_arg = args.get("transcribe", False)
    transcribe = transcribe_arg if isinstance(transcribe_arg, bool) else _truthy(transcribe_arg)
    item, record_source = _resolve_audio_media_item(source_arg or None)
    if not item:
        if source_arg:
            return [text(f"その音声ソースは未登録です（audio_media に追加してください）: {source_arg}")], True
        return [text("listen_media に使える audio_media が見つかりません")], True
    media_context = {
        "media_id": clean(item.get("id")),
        "media_source": record_source,
        "label": clean(item.get("label")),
        "room": clean(item.get("room")),
        "timestamp": now().isoformat(timespec="seconds"),
    }
    return _audio_listen_from_source(
        record_source,
        duration,
        transcribe,
        source_label_override=clean(item.get("label")),
        extra_payload={"media_context": media_context},
        media_kind_hint='聴いた内容を残すなら record_episode(kind="media_listen", ...) を使ってよい。',
    )


def use_device_microphone(args: dict):
    _, current_entity, _, projected_room = _current_body_state()
    broken_state = _broken_cyber_state_error(current_entity, projected_room)
    if broken_state:
        return broken_state
    if not current_entity:
        return [text("物理体モードでは use_device_microphone は使えません。listen を使ってください。")], True
    if current_entity.startswith(("tcp://", "alsa://")):
        return [text(
            "現在の身体状態は廃止済みのマイク入力を参照しています。"
            "return_to_body で身体へ戻り、preferences.json のRTSPマイクへ入り直してください。"
        )], True
    prefs = load_preferences()
    caps = get_device_capabilities(current_entity, prefs)
    source = clean(caps.get("mic_source"))
    if not caps.get("is_mic") or not source:
        return [text(f"現在侵入中のデバイス（{current_entity}）はマイクデバイスではありません。")], True
    duration = normalize_duration(args.get("duration"))
    transcribe_arg = args.get("transcribe", False)
    transcribe = transcribe_arg if isinstance(transcribe_arg, bool) else _truthy(transcribe_arg)
    return _audio_listen_from_source(source, duration, transcribe)


def concentrate_hearing(args: dict):
    if not _is_antigravity_harness():
        return [text("現在のハーネスでは concentrate_hearing は使えません。")], True
    _, current_entity, _, projected_room = _current_body_state()
    broken_state = _broken_cyber_state_error(current_entity, projected_room)
    if broken_state:
        return broken_state
    if current_entity:
        return [text("電脳体モードでは concentrate_hearing は使えません。")], True
    source = default_listen_source()
    if not source:
        return [text(
            "利用できるマイクが登録されていません。"
            "preferences.json の mics に音声ソースを追加してください。"
        )], True
    try:
        duration = validate_concentrate_hearing_duration(args.get("duration"))
    except ValueError as exc:
        return [text(str(exc))], True

    state_path = concentrate_hearing_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _concentrate_hearing_lock_path()
    with lock_path.open("a+b") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        current_epoch = time.time()
        last_success = _read_concentrate_hearing_success_epoch(state_path)
        cooldown = concentrate_hearing_cooldown_seconds()
        if last_success is not None and current_epoch - last_success < cooldown:
            remaining = max(1, int(cooldown - (current_epoch - last_success) + 0.999))
            return [text(f"concentrate_hearing はクールダウン中です。あと {remaining} 秒待ってください。")], True
        try:
            file_path = _record_concentrate_hearing_webm(source, duration)
        except Exception as exc:
            detail = clean(str(exc)) or "unknown error"
            return [text(f"concentrate_hearing の録音に失敗しました: {detail}")], True
        _write_concentrate_hearing_success_epoch(state_path, time.time())

    try:
        import body_state as _body_state

        _body_state.update_body_state(lambda state: _body_state.on_audio_session(state))
    except Exception as exc:
        log(f"[audio-mcp] concentrate_hearing body-state update failed: {exc}")
    record_active_listen({
        "timestamp": now().isoformat(timespec="seconds"),
        "kind": "active_listen",
        "type": "active_listen",
        "actor": clean(os.environ.get("EHA_ACTOR")) or "unknown",
        "source": source,
        "source_label": label_for_source(source),
        "duration": duration,
        "duration_sec": duration,
        "transcribe_requested": False,
        "transcript": None,
        "deep_listen": True,
    }, source)
    payload = {
        "file_path": file_path,
        "media_type": "audio/webm",
        "audio_codec": "opus",
        "duration_sec": duration,
        "available_for_current_turn": True,
        "instruction": (
            "file_path を同じターンで view_file に直接渡して音として確認してください。"
            "command、shell、terminal、Python、外部スクリプト、外部STTは使わないでください。"
        ),
    }
    return [text(json.dumps(payload, ensure_ascii=False))]


def _audio_tools() -> dict:
    tools = {
        "listen": {"spec": TOOL_LISTEN, "handler": listen},
        "listen_media": {"spec": TOOL_LISTEN_MEDIA, "handler": listen_media},
        "read_audio_log": {"spec": TOOL_READ_AUDIO_LOG, "handler": read_audio_log},
        "read_heard_audio_log": {"spec": TOOL_READ_HEARD_AUDIO_LOG, "handler": read_heard_audio_log},
        "read_active_listen_log": {"spec": TOOL_READ_ACTIVE_LISTEN_LOG, "handler": read_active_listen_log},
        "read_non_speech_audio_events": {"spec": TOOL_READ_NON_SPEECH_AUDIO_EVENTS, "handler": read_non_speech_audio_events},
        "read_audio_event_tags": {"spec": TOOL_READ_AUDIO_EVENT_TAGS, "handler": read_audio_event_tags},
        "speak": {"spec": TOOL_SPEAK, "handler": speak},
        "use_device_speaker": {"spec": TOOL_USE_DEVICE_SPEAKER, "handler": use_device_speaker},
        "use_device_microphone": {"spec": TOOL_USE_DEVICE_MICROPHONE, "handler": use_device_microphone},
    }
    if _is_antigravity_harness():
        tools["concentrate_hearing"] = {"spec": TOOL_CONCENTRATE_HEARING, "handler": concentrate_hearing}
    return tools


if __name__ == "__main__":
    serve("audio-mcp", "1.0", _audio_tools())
