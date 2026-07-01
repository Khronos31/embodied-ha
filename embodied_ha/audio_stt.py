from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from state_utils import clean, load_prefs


def load_preferences() -> dict:
    prefs_file = clean(os.environ.get("EHA_PREFS_FILE"))
    if prefs_file:
        try:
            return load_prefs(prefs_file)
        except Exception:
            return {}
    return {}


def load_stt_provider() -> str | None:
    provider = clean(load_preferences().get("stt_provider"))
    return provider or None


def load_stt_language() -> str:
    lang = clean(load_preferences().get("stt_language"))
    return lang or "ja-JP"


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return clean(value).lower() in {"1", "true", "yes", "on"}


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
