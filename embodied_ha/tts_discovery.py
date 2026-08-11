"""Home Assistant TTS engine metadata discovery over its WebSocket API."""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect


class TtsDiscoveryError(RuntimeError):
    """A safe, user-facing TTS discovery failure."""


def websocket_url(ha_url: str) -> str:
    """Convert a Home Assistant REST base URL to its WebSocket endpoint."""

    override = os.environ.get("EHA_HA_WS_URL", "").strip()
    if override:
        return override

    parsed = urlsplit(ha_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TtsDiscoveryError("Home Assistant URL is invalid")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/").removesuffix("/api")
    path = f"{path}/websocket" if path else "/api/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _receive_json(ws) -> dict:
    try:
        raw = ws.recv(timeout=5)
    except Exception as exc:
        raise TtsDiscoveryError("Home Assistant TTS API timed out") from exc
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    try:
        message = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise TtsDiscoveryError("Home Assistant TTS API returned invalid JSON") from exc
    if not isinstance(message, dict):
        raise TtsDiscoveryError("Home Assistant TTS API returned an invalid message")
    return message


def _request(ws, request_id: int, message_type: str, **fields) -> object:
    request = {"id": request_id, "type": message_type, **fields}
    try:
        ws.send(json.dumps(request, ensure_ascii=False))
    except Exception as exc:
        raise TtsDiscoveryError("Home Assistant TTS API connection failed") from exc
    response = _receive_json(ws)
    if response.get("id") != request_id or response.get("type") != "result":
        raise TtsDiscoveryError("Home Assistant TTS API returned an unexpected response")
    if response.get("success") is not True:
        raise TtsDiscoveryError("Home Assistant TTS API rejected the request")
    return response.get("result")


def discover_tts_options(
    ha_url: str,
    access_token: str,
    provider: str,
    language: str = "",
) -> dict:
    """Return supported languages and, when requested, voices for one TTS entity."""

    if not access_token:
        raise TtsDiscoveryError("Home Assistant authentication is unavailable")

    try:
        ws_context = connect(
            websocket_url(ha_url),
            open_timeout=5,
            close_timeout=1,
        )
        with ws_context as ws:
            required = _receive_json(ws)
            if required.get("type") != "auth_required":
                raise TtsDiscoveryError("Home Assistant TTS API authentication failed")
            ws.send(json.dumps({"type": "auth", "access_token": access_token}))
            authenticated = _receive_json(ws)
            if authenticated.get("type") != "auth_ok":
                raise TtsDiscoveryError("Home Assistant TTS API authentication failed")

            listed = _request(ws, 1, "tts/engine/list")
            providers = listed.get("providers") if isinstance(listed, dict) else None
            if not isinstance(providers, list):
                raise TtsDiscoveryError("Home Assistant TTS API returned invalid providers")
            selected = next(
                (
                    item
                    for item in providers
                    if isinstance(item, dict) and item.get("engine_id") == provider
                ),
                None,
            )
            if selected is None:
                raise TtsDiscoveryError("The selected TTS entity is unavailable")
            languages = selected.get("supported_languages")
            if not isinstance(languages, list) or not all(
                isinstance(value, str) for value in languages
            ):
                raise TtsDiscoveryError("Home Assistant TTS API returned invalid languages")

            result = {"languages": languages, "voices": []}
            if not language:
                return result
            if language not in languages:
                raise TtsDiscoveryError("The selected TTS language is unavailable")

            voice_result = _request(
                ws,
                2,
                "tts/engine/voices",
                engine_id=provider,
                language=language,
            )
            voices = voice_result.get("voices") if isinstance(voice_result, dict) else None
            if voices is None:
                return result
            if not isinstance(voices, list):
                raise TtsDiscoveryError("Home Assistant TTS API returned invalid voices")
            normalized = []
            for voice in voices:
                if not isinstance(voice, dict):
                    raise TtsDiscoveryError("Home Assistant TTS API returned invalid voices")
                voice_id = voice.get("voice_id")
                name = voice.get("name")
                if not isinstance(voice_id, str) or not isinstance(name, str):
                    raise TtsDiscoveryError("Home Assistant TTS API returned invalid voices")
                normalized.append({"voice_id": voice_id, "name": name})
            result["voices"] = normalized
            return result
    except TtsDiscoveryError:
        raise
    except Exception as exc:
        raise TtsDiscoveryError("Home Assistant TTS API connection failed") from exc
