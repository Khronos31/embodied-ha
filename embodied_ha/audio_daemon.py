#!/usr/bin/env python3
"""Realtime audio STT daemon for Embodied HA."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from auditory_context import append_auditory_event
from sensory_origin import classify_sensory_origin
from state_utils import clean, now, parse_ts

try:
    from pysilero_vad import SileroVoiceActivityDetector
except Exception:  # pragma: no cover - exercised through fallback path
    SileroVoiceActivityDetector = None


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK_SAMPLES = 512
CHUNK_BYTES = CHUNK_SAMPLES * SAMPLE_WIDTH
PREBUFFER_SECONDS = 0.3
SILENCE_SECONDS = 0.8
MIN_SEGMENT_SECONDS = 0.5
MAX_SEGMENT_SECONDS = 30.0
VAD_THRESHOLD = 0.5
FALLBACK_DB_THRESHOLD = -47.0
FALLBACK_SEGMENT_MIN_SPEECH_RATIO = 0.12
FALLBACK_SEGMENT_MIN_PEAK_DB = -42.0
FALLBACK_SEGMENT_HARD_PEAK_DB = -36.0
TMP_DIR = Path("/tmp/embodied-ha/audio-daemon")
DEFAULT_AUDIO_LOG_FILE = "/data/embodied-ha/log/audio_log.jsonl"
DEFAULT_BACKGROUND_AUDIO_LOG_FILE = "/data/embodied-ha/log/background_audio_log.jsonl"
BACKGROUND_LOG_MIN_INTERVAL_SECONDS = 300
BACKGROUND_LOG_RETENTION_HOURS = 24
_LOG_LOCK = threading.Lock()
_BACKGROUND_LOG_LOCK = threading.Lock()


@dataclass(frozen=True)
class AudioSourceConfig:
    source: str
    label: str
    retention_hours: int
    wake_word_enabled: bool
    background_only: bool = False
    room: str = ""
    note: str = ""


@dataclass(frozen=True)
class RuntimeSettings:
    config: AudioSourceConfig
    provider: str | None
    language: str
    wake_words: list[str]
    stt_enabled: bool


def log(message: str) -> None:
    print(f"[audio-daemon] {message}", file=sys.stderr, flush=True)


def prefs_path() -> str:
    return os.environ.get("EHA_PREFS_FILE", "")


def default_audio_log_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "audio_log.jsonl")
    return "/config/embodied-ha/log/audio_log.jsonl"


def audio_log_path() -> str:
    return clean(os.environ.get("EHA_AUDIO_LOG_FILE")) or default_audio_log_path() or DEFAULT_AUDIO_LOG_FILE


def default_background_audio_log_path() -> str:
    data_dir = clean(os.environ.get("EHA_DATA_DIR"))
    if data_dir:
        return os.path.join(data_dir, "log", "background_audio_log.jsonl")
    return "/config/embodied-ha/log/background_audio_log.jsonl"


def background_audio_log_path() -> str:
    return (
        clean(os.environ.get("EHA_BACKGROUND_AUDIO_LOG_FILE"))
        or default_background_audio_log_path()
        or DEFAULT_BACKGROUND_AUDIO_LOG_FILE
    )


def background_audio_retention_hours() -> int:
    try:
        return max(1, int(clean(os.environ.get("EHA_BACKGROUND_AUDIO_RETENTION_HOURS")) or BACKGROUND_LOG_RETENTION_HOURS))
    except Exception:
        return BACKGROUND_LOG_RETENTION_HOURS


def canonical_source(value: str) -> str:
    source = clean(value)
    return "default" if source in {"", "alsa", "default"} else source


def load_preferences() -> dict:
    path = prefs_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log(f"failed to load preferences: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def background_hearing_enabled(item: dict) -> bool:
    if "background_hearing_enabled" in item:
        return item.get("background_hearing_enabled") is True
    return True


def load_enabled_audio_sources(preferences: dict | None = None) -> list[AudioSourceConfig]:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    raw_sources = prefs.get("audio_sources")
    if not isinstance(raw_sources, list):
        return []

    enabled: list[AudioSourceConfig] = []
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        if item.get("stt_enabled") is not True:
            continue
        source = canonical_source(item.get("source"))
        if not source:
            continue
        label = clean(item.get("label")) or source
        try:
            retention = int(item.get("stt_retention_hours", 60))
        except Exception:
            retention = 60
        background_only = retention <= 0
        if background_only and not background_hearing_enabled(item):
            continue
        enabled.append(
            AudioSourceConfig(
                source=source,
                label=label,
                retention_hours=max(1, retention) if not background_only else background_audio_retention_hours(),
                wake_word_enabled=bool(item.get("wake_word_enabled")),
                background_only=background_only,
                room=clean(item.get("room")),
                note=clean(item.get("note")),
            )
        )
    return enabled


def load_stt_provider(preferences: dict | None = None) -> str | None:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    provider = clean(prefs.get("stt_provider"))
    return provider or None


def load_stt_language(preferences: dict | None = None) -> str:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    language = clean(prefs.get("stt_language"))
    return language or "ja-JP"


def load_wake_words(preferences: dict | None = None) -> list[str]:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    words = prefs.get("wake_words")
    if not isinstance(words, list):
        return []
    normalized = [clean(word).lower() for word in words if clean(word)]
    return normalized


def load_runtime_settings(
    base_config: AudioSourceConfig,
    preferences: dict | None = None,
) -> RuntimeSettings:
    prefs = preferences if isinstance(preferences, dict) else load_preferences()
    provider = load_stt_provider(prefs)
    language = load_stt_language(prefs)
    wake_words = load_wake_words(prefs)
    effective_config = base_config
    stt_enabled = True

    raw_sources = prefs.get("audio_sources")
    if isinstance(raw_sources, list):
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            source = canonical_source(item.get("source"))
            if source != base_config.source:
                continue
            label = clean(item.get("label")) or source
            if label != base_config.label:
                continue
            try:
                retention = int(item.get("stt_retention_hours", base_config.retention_hours))
            except Exception:
                retention = base_config.retention_hours
            background_only = retention <= 0 and background_hearing_enabled(item)
            effective_config = AudioSourceConfig(
                source=base_config.source,
                label=base_config.label,
                retention_hours=max(1, retention) if not background_only else background_audio_retention_hours(),
                wake_word_enabled=bool(item.get("wake_word_enabled")),
                background_only=background_only,
                room=clean(item.get("room")) or base_config.room,
                note=clean(item.get("note")) or base_config.note,
            )
            stt_enabled = item.get("stt_enabled") is True and retention > 0
            break

    return RuntimeSettings(
        config=effective_config,
        provider=provider,
        language=language,
        wake_words=wake_words,
        stt_enabled=stt_enabled,
    )


def build_ffmpeg_command(source: str) -> list[str]:
    if source == "default":
        return [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "alsa",
            "-i",
            "default",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-f",
            "s16le",
            "-",
        ]
    return [
        "ffmpeg",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        source,
        "-vn",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-f",
        "s16le",
        "-",
    ]


def read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        piece = stream.read(remaining)
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def chunk_db(chunk: bytes) -> float:
    if not chunk:
        return float("-inf")
    samples = memoryview(chunk).cast("h")
    if not samples:
        return float("-inf")
    square_sum = 0.0
    for sample in samples:
        square_sum += float(sample) * float(sample)
    rms = math.sqrt(square_sum / len(samples))
    if rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(rms / 32767.0)


def fallback_voice_probability(chunk: bytes) -> float:
    return 1.0 if chunk_db(chunk) > FALLBACK_DB_THRESHOLD else 0.0


def detect_voice(chunk: bytes, detector) -> float:
    if detector is None:
        return fallback_voice_probability(chunk)
    try:
        return float(detector(chunk))
    except Exception as exc:
        log(f"vad process failed, falling back to energy threshold: {exc}")
        return fallback_voice_probability(chunk)


def summarize_chunk_levels(levels: list[float]) -> tuple[float | None, float | None]:
    finite_levels = [level for level in levels if math.isfinite(level)]
    if not finite_levels:
        return None, None
    peak_db = max(finite_levels)
    mean_db = sum(finite_levels) / len(finite_levels)
    return round(peak_db, 1), round(mean_db, 1)


def should_transcribe_segment(vad_mode: str, diagnostics: dict | None) -> tuple[bool, str | None]:
    if vad_mode != "fallback":
        return True, None
    if not isinstance(diagnostics, dict):
        return True, None

    speech_ratio = diagnostics.get("speech_ratio")
    peak_db = diagnostics.get("peak_db")
    try:
        speech_ratio_value = float(speech_ratio)
    except Exception:
        speech_ratio_value = None
    try:
        peak_db_value = float(peak_db)
    except Exception:
        peak_db_value = None

    if peak_db_value is not None and peak_db_value >= FALLBACK_SEGMENT_HARD_PEAK_DB:
        return True, None
    if speech_ratio_value is None or peak_db_value is None:
        return False, "fallback_gate_missing_metrics"
    if speech_ratio_value < FALLBACK_SEGMENT_MIN_SPEECH_RATIO:
        return False, "fallback_gate_low_speech_ratio"
    if peak_db_value < FALLBACK_SEGMENT_MIN_PEAK_DB:
        return False, "fallback_gate_low_peak_db"
    return True, None


def write_wav(path: str, audio_bytes: bytes) -> None:
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(audio_bytes)


def transcribe_wav(path: str, provider: str, language: str, token: str) -> str:
    with open(path, "rb") as f:
        body = f.read()
    request = urllib.request.Request(
        f"http://supervisor/core/api/stt/{provider}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "audio/wav",
            "X-Speech-Content": (
                "format=wav; codec=pcm; sample_rate=16000; bit_rate=16; "
                f"channel=1; language={language}"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc
    except TimeoutError as exc:
        raise RuntimeError("request timeout") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid STT response JSON") from exc
    text = clean(payload.get("text")) if isinstance(payload, dict) else ""
    if not text:
        raise RuntimeError("empty transcription")
    return text


def append_audio_log(entry: dict, retention_hours: int, source_label: str) -> None:
    path = audio_log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cutoff = now() - timedelta(hours=max(1, retention_hours))

    with _LOG_LOCK:
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
                        if ts and clean(parsed.get("source")) == source_label and ts < cutoff:
                            continue
                        entries.append(parsed)
            except Exception as exc:
                log(f"failed to read audio log for retention pruning: {exc}")
        entries.append(entry)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)


def append_background_audio_log(entry: dict, retention_hours: int | None = None, source_label: str | None = None) -> None:
    path = background_audio_log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    retention = retention_hours if retention_hours is not None else background_audio_retention_hours()
    cutoff = now() - timedelta(hours=max(1, retention))
    source = clean(source_label) or clean(entry.get("source"))

    with _BACKGROUND_LOG_LOCK:
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
                        if source and clean(parsed.get("source")) == source and ts and ts < cutoff:
                            continue
                        entries.append(parsed)
            except Exception as exc:
                log(f"failed to read background audio log for retention pruning: {exc}")
        entries.append(entry)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)


def build_background_audio_event(
    config: AudioSourceConfig,
    duration_sec: float,
    vad_mode: str,
    diagnostics: dict | None = None,
) -> dict:
    sensory = classify_sensory_origin(
        source=config.source,
        label=config.label,
        room=config.room,
        note=config.note,
        modality="auditory",
    )
    entry = {
        "timestamp": now().isoformat(timespec="seconds"),
        "kind": "background_audio",
        "modality": "auditory",
        "awareness": "background",
        "origin": config.source,
        "source": config.label,
        "duration_sec": round(duration_sec, 2),
        "has_sound": True,
        "stt_requested": False,
        "transcript": None,
        "vad_mode": vad_mode,
        **sensory,
    }
    if isinstance(diagnostics, dict):
        for key in ("speech_ratio", "peak_db", "mean_db"):
            if key in diagnostics:
                entry[key] = diagnostics[key]
    return entry


def maybe_record_background_audio(
    config: AudioSourceConfig,
    audio_bytes: bytes,
    vad_mode: str,
    diagnostics: dict | None,
    last_logged_at: float,
) -> float:
    current = time.monotonic()
    if current - last_logged_at < BACKGROUND_LOG_MIN_INTERVAL_SECONDS:
        return last_logged_at
    duration_sec = len(audio_bytes) / float(SAMPLE_RATE * SAMPLE_WIDTH)
    if duration_sec < MIN_SEGMENT_SECONDS:
        return last_logged_at
    append_background_audio_log(
        build_background_audio_event(config, duration_sec, vad_mode, diagnostics),
        config.retention_hours,
        config.label,
    )
    log(
        "background audio noted for "
        f"{config.label}: duration={duration_sec:.2f}s, vad={vad_mode}, "
        f"speech_ratio={(diagnostics or {}).get('speech_ratio')}, peak_db={(diagnostics or {}).get('peak_db')}"
    )
    return current


def build_auditory_event(
    config: AudioSourceConfig,
    transcript: str,
    duration_sec: float,
    provider: str | None,
    language: str,
    timestamp: str,
    diagnostics: dict | None = None,
) -> dict:
    sensory = classify_sensory_origin(
        source=config.source,
        label=config.label,
        room=config.room,
        note=config.note,
        modality="auditory",
    )
    event = {
        "timestamp": timestamp,
        "modality": "auditory",
        "origin": config.source,
        "source": config.label,
        "speaker_hint": "user" if config.wake_word_enabled else "unknown",
        "transcript": transcript,
        "duration_sec": round(duration_sec, 2),
        "stt_provider": provider,
        "stt_language": language,
        "confidence": None,
        "raw_audio_ref": None,
        **sensory,
    }
    if isinstance(diagnostics, dict):
        for key in ("vad_mode", "speech_ratio", "peak_db", "mean_db"):
            if key in diagnostics:
                event[key] = diagnostics[key]
    return event


def should_trigger_wake_word(text_value: str, wake_words: list[str]) -> bool:
    lowered = clean(text_value).lower()
    return bool(lowered) and any(word in lowered for word in wake_words)


def post_wake_message(text_value: str) -> None:
    ingress_port = clean(os.environ.get("INGRESS_PORT")) or "8099"
    body = json.dumps({"message": text_value, "source": "voice"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://localhost:{ingress_port}/api/send",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return
    except Exception as exc:
        log(f"wake-word forward failed: {exc}")


def process_segment(
    config: AudioSourceConfig,
    audio_bytes: bytes,
    provider: str | None,
    language: str,
    token: str,
    wake_words: list[str],
    diagnostics: dict | None = None,
) -> None:
    duration_sec = len(audio_bytes) / float(SAMPLE_RATE * SAMPLE_WIDTH)
    if duration_sec < MIN_SEGMENT_SECONDS:
        return

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = now().isoformat(timespec="seconds")
    entry: dict = {
        "timestamp": timestamp,
        "source": config.label,
        "duration_sec": round(duration_sec, 2),
    }
    if isinstance(diagnostics, dict):
        entry.update(diagnostics)
    vad_mode = clean(entry.get("vad_mode")) or "unknown"
    should_transcribe, skip_reason = should_transcribe_segment(vad_mode, entry)
    if not should_transcribe:
        entry["skipped"] = True
        entry["skip_reason"] = skip_reason
        append_audio_log(entry, config.retention_hours, config.label)
        log(
            "segment skipped for "
            f"{config.label}: {skip_reason} "
            f"(duration={entry.get('duration_sec')}s, vad={vad_mode}, "
            f"speech_ratio={entry.get('speech_ratio')}, peak_db={entry.get('peak_db')}, "
            f"mean_db={entry.get('mean_db')})"
        )
        return
    tmp_path: str | None = None
    try:
        if not provider:
            raise RuntimeError("stt_provider is not configured")
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is not set")
        with tempfile.NamedTemporaryFile(dir=TMP_DIR, suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        write_wav(tmp_path, audio_bytes)
        text_value = transcribe_wav(tmp_path, provider, language, token)
        entry["text"] = text_value
        append_audio_log(entry, config.retention_hours, config.label)
        append_auditory_event(
            build_auditory_event(
                config,
                text_value,
                duration_sec,
                provider,
                language,
                timestamp,
                diagnostics=diagnostics,
            ),
            config.retention_hours,
            config.label,
        )
        if config.wake_word_enabled and should_trigger_wake_word(text_value, wake_words):
            post_wake_message(text_value)
    except Exception as exc:
        entry["error"] = str(exc)
        append_audio_log(entry, config.retention_hours, config.label)
        log(
            "segment processing failed for "
            f"{config.label}: {exc} "
            f"(duration={entry.get('duration_sec')}s, vad={entry.get('vad_mode','unknown')}, "
            f"speech_ratio={entry.get('speech_ratio')}, peak_db={entry.get('peak_db')}, "
            f"mean_db={entry.get('mean_db')})"
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def new_vad():
    if SileroVoiceActivityDetector is None:
        log(
            "pysilero_vad unavailable; using "
            f"{FALLBACK_DB_THRESHOLD:.0f}dB fallback VAD "
            f"(speech_ratio>={FALLBACK_SEGMENT_MIN_SPEECH_RATIO}, peak>={FALLBACK_SEGMENT_MIN_PEAK_DB}dB)"
        )
        return None, "fallback"
    try:
        return SileroVoiceActivityDetector(), "silero"
    except Exception as exc:
        log(
            "failed to initialize pysilero_vad; using fallback VAD: "
            f"{exc} (threshold={FALLBACK_DB_THRESHOLD:.0f}dB, "
            f"speech_ratio>={FALLBACK_SEGMENT_MIN_SPEECH_RATIO}, peak>={FALLBACK_SEGMENT_MIN_PEAK_DB}dB)"
        )
        return None, "fallback"


def audio_worker(
    config: AudioSourceConfig,
    token: str,
) -> None:
    prebuffer_chunks = max(1, math.ceil(PREBUFFER_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))
    max_silence_chunks = max(1, math.ceil(SILENCE_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))
    max_segment_chunks = max(1, math.ceil(MAX_SEGMENT_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))

    while True:
        proc: subprocess.Popen | None = None
        detector, vad_mode = new_vad()
        last_settings_signature: tuple[str | None, str, tuple[str, ...], bool, bool] | None = None
        last_background_log_at = 0.0
        prebuffer: deque[bytes] = deque(maxlen=prebuffer_chunks)
        segment_chunks: list[bytes] = []
        segment_levels: list[float] = []
        segment_speech_chunks = 0
        silence_chunks = 0
        active = False
        try:
            cmd = build_ffmpeg_command(config.source)
            log(f"starting ffmpeg for {config.label} ({vad_mode}): {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            if proc.stdout is None:
                raise RuntimeError("ffmpeg stdout is unavailable")

            while True:
                chunk = read_exact(proc.stdout, CHUNK_BYTES)
                if len(chunk) < CHUNK_BYTES:
                    break

                voice_prob = detect_voice(chunk, detector)
                is_speech = voice_prob > VAD_THRESHOLD
                level_db = chunk_db(chunk)

                if active:
                    segment_chunks.append(chunk)
                    segment_levels.append(level_db)
                    if is_speech:
                        segment_speech_chunks += 1
                    silence_chunks = 0 if is_speech else silence_chunks + 1
                    if len(segment_chunks) >= max_segment_chunks or silence_chunks >= max_silence_chunks:
                        peak_db, mean_db = summarize_chunk_levels(segment_levels)
                        speech_ratio = round(segment_speech_chunks / max(1, len(segment_chunks)), 3)
                        settings = load_runtime_settings(config)
                        signature = (
                            settings.provider,
                            settings.language,
                            tuple(settings.wake_words),
                            settings.config.wake_word_enabled,
                            settings.config.background_only,
                        )
                        if signature != last_settings_signature:
                            last_settings_signature = signature
                            log(
                                "runtime settings updated for "
                                f"{config.label}: provider={settings.provider or 'unset'}, "
                                f"language={settings.language}, "
                                f"wake_words={len(settings.wake_words)}, "
                                f"wake_word_enabled={'yes' if settings.config.wake_word_enabled else 'no'}, mode={'background' if settings.config.background_only else 'stt'}"
                            )
                        diagnostics = {
                            "vad_mode": vad_mode,
                            "speech_ratio": speech_ratio,
                            "peak_db": peak_db,
                            "mean_db": mean_db,
                        }
                        if not settings.stt_enabled:
                            if settings.config.background_only:
                                last_background_log_at = maybe_record_background_audio(
                                    settings.config,
                                    b"".join(segment_chunks),
                                    vad_mode,
                                    diagnostics,
                                    last_background_log_at,
                                )
                            segment_chunks = []
                            segment_levels = []
                            segment_speech_chunks = 0
                            silence_chunks = 0
                            active = False
                            prebuffer.clear()
                            if detector is not None:
                                try:
                                    detector.reset()
                                except Exception:
                                    pass
                            continue
                        process_segment(
                            settings.config,
                            b"".join(segment_chunks),
                            settings.provider,
                            settings.language,
                            token,
                            settings.wake_words,
                            diagnostics=diagnostics,
                        )
                        segment_chunks = []
                        segment_levels = []
                        segment_speech_chunks = 0
                        silence_chunks = 0
                        active = False
                        prebuffer.clear()
                        if detector is not None:
                            try:
                                detector.reset()
                            except Exception:
                                pass
                elif is_speech:
                    segment_chunks = list(prebuffer)
                    segment_chunks.append(chunk)
                    segment_levels = [chunk_db(buffered) for buffered in prebuffer]
                    segment_levels.append(level_db)
                    segment_speech_chunks = 1
                    silence_chunks = 0
                    active = True
                    prebuffer.clear()
                else:
                    prebuffer.append(chunk)

            if active and segment_chunks:
                peak_db, mean_db = summarize_chunk_levels(segment_levels)
                speech_ratio = round(segment_speech_chunks / max(1, len(segment_chunks)), 3)
                settings = load_runtime_settings(config)
                signature = (
                    settings.provider,
                    settings.language,
                    tuple(settings.wake_words),
                    settings.config.wake_word_enabled,
                    settings.config.background_only,
                )
                if signature != last_settings_signature:
                    last_settings_signature = signature
                    log(
                        "runtime settings updated for "
                        f"{config.label}: provider={settings.provider or 'unset'}, "
                        f"language={settings.language}, "
                        f"wake_words={len(settings.wake_words)}, "
                        f"wake_word_enabled={'yes' if settings.config.wake_word_enabled else 'no'}, mode={'background' if settings.config.background_only else 'stt'}"
                    )
                diagnostics = {
                    "vad_mode": vad_mode,
                    "speech_ratio": speech_ratio,
                    "peak_db": peak_db,
                    "mean_db": mean_db,
                }
                if settings.stt_enabled:
                    process_segment(
                        settings.config,
                        b"".join(segment_chunks),
                        settings.provider,
                        settings.language,
                        token,
                        settings.wake_words,
                        diagnostics=diagnostics,
                    )
                elif settings.config.background_only:
                    last_background_log_at = maybe_record_background_audio(
                        settings.config,
                        b"".join(segment_chunks),
                        vad_mode,
                        diagnostics,
                        last_background_log_at,
                    )

            rc = proc.wait(timeout=2)
            log(f"ffmpeg exited for {config.label} with code {rc}; retrying in 10s")
        except Exception as exc:
            log(f"worker error for {config.label}: {exc}")
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        time.sleep(10)


def main() -> int:
    preferences = load_preferences()
    sources = load_enabled_audio_sources(preferences)
    if not sources:
        log("no STT/background audio sources; exiting")
        return 0

    token = clean(os.environ.get("SUPERVISOR_TOKEN"))

    threads = []
    for config in sources:
        thread = threading.Thread(
            target=audio_worker,
            args=(config, token),
            daemon=False,
            name=f"audio:{config.label}",
        )
        thread.start()
        threads.append(thread)
        log(f"audio source thread started: {config.label}")

    for thread in threads:
        thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
