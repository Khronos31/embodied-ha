#!/usr/bin/env python3
"""音声 listen 用 MCP サーバー。"""

from __future__ import annotations

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
import urllib.error
import urllib.request
from pathlib import Path

from mcp_lib import serve, text
from sensory_origin import classify_sensory_origin
from state_utils import clean, now, parse_ts

DEFAULT_SOURCES = [
    {"source": "rtsp://localhost:8554/capture_tv", "label": "TV・レコーダー"},
    {"source": "rtsp://localhost:8556/mic_only",   "label": "PC"},
    {"source": "rtsp://localhost:8558/mic_only",   "label": "Google TV"},
    {"source": "alsa://default",                    "label": "スタディマイク"},
]
DEFAULT_SOURCE = "rtsp://localhost:8554/capture_tv"
MAX_DURATION = 30
TMP_DIR = Path("/tmp/embodied-ha/audio")
DEFAULT_ACTIVE_LISTEN_LOG_FILE = "/data/embodied-ha/log/active_listen_log.jsonl"
DEFAULT_AUDITORY_EVENTS_FILE = "/data/embodied-ha/log/auditory_events.jsonl"
_ACTIVE_LISTEN_LOCK = threading.Lock()


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


def default_active_listen_request_dir() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "runtime", "active_listen_requests")
    return "/config/embodied-ha/runtime/active_listen_requests"


def active_listen_request_dir() -> str:
    return clean(os.environ.get("EHA_ACTIVE_LISTEN_REQUEST_DIR")) or default_active_listen_request_dir()


def _write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def request_daemon_capture_to_wav(source: str, duration: int, output_path: str) -> None:
    request_id = uuid.uuid4().hex
    request_dir = active_listen_request_dir()
    os.makedirs(request_dir, exist_ok=True)
    response_path = os.path.join(request_dir, f"{request_id}.response.json")
    request_path = os.path.join(request_dir, f"{request_id}.json")
    payload = {
        'request_id': request_id,
        'source': normalize_source_uri(source),
        'duration': max(1, int(duration)),
        'output_path': output_path,
        'response_path': response_path,
        'created_at': time.time(),
    }
    _write_json_atomic(request_path, payload)
    deadline = time.monotonic() + duration + 12
    try:
        while time.monotonic() < deadline:
            if os.path.exists(response_path):
                with open(response_path, encoding='utf-8') as f:
                    response = json.load(f)
                if response.get('ok') is not True:
                    raise TimeoutError(clean(response.get('error')) or 'daemon capture failed')
                return
            time.sleep(0.1)
        raise TimeoutError(f'timed out waiting for daemon capture for {source}')
    finally:
        for candidate in (request_path, response_path):
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass


def _prefs_path() -> str:
    return os.environ.get("EHA_PREFS_FILE", "")


def normalize_source_uri(value: str) -> str:
    source = clean(value)
    if source in {"", "alsa", "default"}:
        return "alsa://default"
    if source.startswith("alsa://"):
        device = source[len("alsa://"):].lstrip("/")
        return f"alsa://{device or 'default'}"
    return source


def load_preferences() -> dict:
    path = _prefs_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_audio_source_configs() -> list[dict]:
    sources = load_preferences().get("audio_sources")
    if not isinstance(sources, list):
        sources = DEFAULT_SOURCES

    normalized = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        source = normalize_source_uri(item.get("source"))
        if not source:
            continue
        config = dict(item)
        config["source"] = source
        config["label"] = clean(item.get("label")) or source
        normalized.append(config)
    return normalized or list(DEFAULT_SOURCES)


def load_audio_sources() -> list[dict]:
    return [
        {"source": item["source"], "label": item["label"]}
        for item in load_audio_source_configs()
    ]


def default_listen_source() -> str:
    sources = load_audio_sources()
    return sources[0]["source"] if sources else DEFAULT_SOURCE


def load_stt_provider() -> str | None:
    provider = clean(load_preferences().get("stt_provider"))
    return provider or None


def load_stt_language() -> str:
    lang = clean(load_preferences().get("stt_language"))
    return lang or "ja-JP"


def build_listen_spec() -> dict:
    sources = load_audio_sources()
    source_lines = "\n".join(
        f'  - "{s["source"]}"（{s["label"]}）' for s in sources
    )
    default = sources[0]["source"] if sources else "alsa://default"
    return {
        "name": "listen",
        "description": (
            "短時間だけ音を聴く。\n"
            f"利用可能なソース:\n{source_lines}\n"
            f"source を省略すると最初のソース（{default}）を使う。"
            "transcribe は必要なときだけ true にする。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "上記ソースのいずれか。省略時はデフォルト",
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


TOOL_READ_AUDIO_LOG = {
    "name": "read_audio_log",
    "description": "最近の常時STT生ログを読む。VAD/STTの成功・失敗・スキップ診断を含む。",
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
    "description": "最近の常時STTで聞こえた発話ログを読む。会話コンテキストに入る聴覚イベント。",
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
    "description": "最近、自分から listen で聞きに行った音声ログを読む。常時STTログとは別。",
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
    return {clean(item.get("source")): item for item in load_audio_sources()}


def _source_config_map() -> dict[str, dict]:
    return {clean(item.get("source")): item for item in load_audio_source_configs()}


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


def active_listen_retention_hours(source: str) -> int:
    item = _source_config_map().get(clean(source))
    if item:
        try:
            retention = int(item.get("stt_retention_hours", 0))
        except Exception:
            retention = 0
        if retention > 0:
            return retention

    try:
        fallback = int(clean(os.environ.get("EHA_ACTIVE_LISTEN_RETENTION_HOURS")) or 24)
    except Exception:
        fallback = 24
    return max(1, fallback)


def normalize_duration(value) -> int:
    try:
        seconds = int(value) if clean(value) else 5
    except Exception:
        seconds = 5
    return max(1, min(MAX_DURATION, seconds))


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def build_record_command(source: str, duration: int) -> list[str]:
    source = normalize_source_uri(source)
    if source.startswith("tcp://"):
        raise ValueError("tcp sources require direct socket capture")
    if source.startswith("alsa://"):
        device = source[len("alsa://"):].lstrip("/") or "default"
        return [
            "ffmpeg",
            "-f", "alsa",
            "-i", device,
            "-ar", "16000",
            "-ac", "1",
            "-t", str(duration),
            "-y",
        ]
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
    token = clean(os.environ.get("SUPERVISOR_TOKEN"))
    if not token:
        return None
    lang = load_stt_language()
    with open(path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(
        f"http://supervisor/core/api/stt/{provider}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "audio/wav",
            "X-Speech-Content": (
                f"format=wav; codec=pcm; sample_rate=16000; bit_rate=16; channel=1; language={lang}"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    text_value = clean(payload.get("text")) if isinstance(payload, dict) else ""
    return text_value or None


def transcribe_via_local(path: str) -> str | None:
    if not _truthy(os.environ.get("EHA_FEATURE_AUDIO_STT_LOCAL", "0")):
        return None
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        return None
    try:
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(path, language="ja")
        joined = " ".join(clean(seg.text) for seg in segments if clean(seg.text))
    except Exception:
        return None
    return joined or None


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


def listen(args: dict):
    source = normalize_source_uri(args.get("source") or default_listen_source())

    duration = normalize_duration(args.get("duration"))
    transcribe_arg = args.get("transcribe", False)
    transcribe = transcribe_arg if isinstance(transcribe_arg, bool) else _truthy(transcribe_arg)
    timestamp = now().isoformat(timespec="seconds")
    actor = clean(os.environ.get("EHA_ACTOR")) or "unknown"
    source_label = label_for_source(source)
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
        }

    # 未登録でも rtsp:// / alsa:// / tcp:// なら直接使用する
    if source not in _source_map() and not (source.startswith("rtsp://") or source.startswith("alsa://") or source.startswith("tcp://")):
        payload = {**base_payload(), "error": f"unknown source: {source}"}
        record_active_listen(payload, source)
        return [text(json.dumps({"error": payload["error"]}, ensure_ascii=False))], True

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        payload = {**base_payload(), "error": "ffmpeg not found"}
        record_active_listen(payload, source)
        return [text(json.dumps({"error": "ffmpeg not found"}, ensure_ascii=False))]

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=TMP_DIR, suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        if source.startswith("tcp://"):
            try:
                request_daemon_capture_to_wav(source, duration, tmp_path)
            except Exception as exc:
                message = clean(str(exc)) or "tcp recording failed"
                payload = {**base_payload(), "error": message}
                record_active_listen(payload, source)
                return [text(json.dumps({"error": message, "source": source}, ensure_ascii=False))], True
        else:
            command = build_record_command(source, duration) + [tmp_path]
            command[0] = ffmpeg
            record = subprocess.run(command, capture_output=True, text=True, timeout=duration + 15)
            if record.returncode != 0:
                message = clean(record.stderr) or clean(record.stdout) or "recording failed"
                payload = {**base_payload(), "error": message}
                record_active_listen(payload, source)
                return [text(json.dumps({"error": message, "source": source}, ensure_ascii=False))], True

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
        if transcribe:
            payload["stt_provider"] = load_stt_provider()
            payload["stt_language"] = load_stt_language()
            payload["transcript"] = transcribe_audio(tmp_path)
        record_active_listen(payload, source)
        payload["audio_context"] = build_audio_context(payload)
        return [text(json.dumps(payload, ensure_ascii=False))]
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    serve("audio-mcp", "1.0", {
        "listen": {"spec": TOOL_LISTEN, "handler": listen},
        "read_audio_log": {"spec": TOOL_READ_AUDIO_LOG, "handler": read_audio_log},
        "read_heard_audio_log": {"spec": TOOL_READ_HEARD_AUDIO_LOG, "handler": read_heard_audio_log},
        "read_active_listen_log": {"spec": TOOL_READ_ACTIVE_LISTEN_LOG, "handler": read_active_listen_log},
        "read_non_speech_audio_events": {"spec": TOOL_READ_NON_SPEECH_AUDIO_EVENTS, "handler": read_non_speech_audio_events},
        "read_audio_event_tags": {"spec": TOOL_READ_AUDIO_EVENT_TAGS, "handler": read_audio_event_tags},
    })
