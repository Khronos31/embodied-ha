#!/usr/bin/env python3
"""音声 listen 用 MCP サーバー。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
import urllib.error
import urllib.request
from pathlib import Path

from mcp_lib import serve, text
from state_utils import clean, now, parse_ts

DEFAULT_SOURCES = [
    {"source": "rtsp://localhost:8554/capture_tv", "label": "TV・レコーダー"},
    {"source": "rtsp://localhost:8556/mic_only",   "label": "PC"},
    {"source": "rtsp://localhost:8558/mic_only",   "label": "Google TV"},
    {"source": "default",                          "label": "スタディマイク"},
]
DEFAULT_SOURCE = "rtsp://localhost:8554/capture_tv"
MAX_DURATION = 30
TMP_DIR = Path("/tmp/embodied-ha/audio")


def default_audio_log_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "audio_log.jsonl")
    return "/config/embodied-ha/audio_log.jsonl"


AUDIO_LOG_FILE = clean(os.environ.get("EHA_AUDIO_LOG_FILE")) or default_audio_log_path()


def _prefs_path() -> str:
    return os.environ.get("EHA_PREFS_FILE", "")


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


def load_audio_sources() -> list[dict]:
    sources = load_preferences().get("audio_sources")
    if not isinstance(sources, list):
        return list(DEFAULT_SOURCES)
    normalized = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        source = clean(item.get("source"))
        if source == "alsa":
            source = "default"
        if not source:
            continue
        normalized.append(
            {
                "source": source,
                "label": clean(item.get("label")) or source,
            }
        )
    return normalized or list(DEFAULT_SOURCES)


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
    default = sources[0]["source"] if sources else "default"
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
    "description": "最近の音声認識ログを読む。STTデーモンが記録した発話テキストの一覧。",
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
    if source in {"default", "alsa"}:
        return [
            "ffmpeg",
            "-f", "alsa",
            "-i", "default",
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
        with open(AUDIO_LOG_FILE, encoding="utf-8") as f:
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


def listen(args: dict):
    source = clean(args.get("source")) or DEFAULT_SOURCE
    if source == "alsa":
        source = "default"
    # 未登録でも rtsp:// または default なら直接使用する
    if source not in _source_map() and not (source.startswith("rtsp://") or source == "default"):
        return [text(json.dumps({"error": f"unknown source: {source}"}, ensure_ascii=False))], True

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return [text(json.dumps({"error": "ffmpeg not found"}, ensure_ascii=False))]

    duration = normalize_duration(args.get("duration"))
    transcribe_arg = args.get("transcribe", False)
    transcribe = transcribe_arg if isinstance(transcribe_arg, bool) else _truthy(transcribe_arg)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=TMP_DIR, suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        command = build_record_command(source, duration) + [tmp_path]
        command[0] = ffmpeg
        record = subprocess.run(command, capture_output=True, text=True, timeout=duration + 15)
        if record.returncode != 0:
            message = clean(record.stderr) or clean(record.stdout) or "recording failed"
            return [text(json.dumps({"error": message, "source": source}, ensure_ascii=False))], True

        peak_db, mean_db = analyze_volume(tmp_path)
        payload = {
            "source": source,
            "duration": duration,
            "timestamp": now().isoformat(timespec="seconds"),
            "has_sound": has_sound_from_peak(peak_db),
            "peak_db": peak_db,
            "mean_db": mean_db,
            "transcript": None,
        }
        if transcribe:
            payload["transcript"] = transcribe_audio(tmp_path)
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
    })
