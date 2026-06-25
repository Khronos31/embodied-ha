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
FALLBACK_DB_THRESHOLD = -50.0
TMP_DIR = Path("/tmp/embodied-ha/audio-daemon")
DEFAULT_AUDIO_LOG_FILE = "/data/embodied-ha/audio_log.jsonl"
_LOG_LOCK = threading.Lock()


@dataclass(frozen=True)
class AudioSourceConfig:
    source: str
    label: str
    retention_hours: int
    wake_word_enabled: bool


def log(message: str) -> None:
    print(f"[audio-daemon] {message}", file=sys.stderr, flush=True)


def prefs_path() -> str:
    return os.environ.get("EHA_PREFS_FILE", "")


def audio_log_path() -> str:
    return os.environ.get("EHA_AUDIO_LOG_FILE", DEFAULT_AUDIO_LOG_FILE)


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
        enabled.append(
            AudioSourceConfig(
                source=source,
                label=label,
                retention_hours=max(1, retention),
                wake_word_enabled=bool(item.get("wake_word_enabled")),
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
        if config.wake_word_enabled and should_trigger_wake_word(text_value, wake_words):
            post_wake_message(text_value)
    except Exception as exc:
        entry["error"] = str(exc)
        append_audio_log(entry, config.retention_hours, config.label)
        log(f"segment processing failed for {config.label}: {exc}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def new_vad():
    if SileroVoiceActivityDetector is None:
        log("pysilero_vad unavailable; using -50dB fallback VAD")
        return None
    try:
        return SileroVoiceActivityDetector()
    except Exception as exc:
        log(f"failed to initialize pysilero_vad; using fallback VAD: {exc}")
        return None


def audio_worker(
    config: AudioSourceConfig,
    provider: str | None,
    language: str,
    token: str,
    wake_words: list[str],
) -> None:
    prebuffer_chunks = max(1, math.ceil(PREBUFFER_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))
    max_silence_chunks = max(1, math.ceil(SILENCE_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))
    max_segment_chunks = max(1, math.ceil(MAX_SEGMENT_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES))

    while True:
        proc: subprocess.Popen | None = None
        detector = new_vad()
        prebuffer: deque[bytes] = deque(maxlen=prebuffer_chunks)
        segment_chunks: list[bytes] = []
        silence_chunks = 0
        active = False
        try:
            cmd = build_ffmpeg_command(config.source)
            log(f"starting ffmpeg for {config.label}: {' '.join(cmd)}")
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

                if active:
                    segment_chunks.append(chunk)
                    silence_chunks = 0 if is_speech else silence_chunks + 1
                    if len(segment_chunks) >= max_segment_chunks or silence_chunks >= max_silence_chunks:
                        process_segment(
                            config,
                            b"".join(segment_chunks),
                            provider,
                            language,
                            token,
                            wake_words,
                        )
                        segment_chunks = []
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
                    silence_chunks = 0
                    active = True
                    prebuffer.clear()
                else:
                    prebuffer.append(chunk)

            if active and segment_chunks:
                process_segment(
                    config,
                    b"".join(segment_chunks),
                    provider,
                    language,
                    token,
                    wake_words,
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
        log("no STT-enabled audio sources; exiting")
        return 0

    provider = load_stt_provider(preferences)
    language = load_stt_language(preferences)
    wake_words = load_wake_words(preferences)
    token = clean(os.environ.get("SUPERVISOR_TOKEN"))

    threads = []
    for config in sources:
        thread = threading.Thread(
            target=audio_worker,
            args=(config, provider, language, token, wake_words),
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
