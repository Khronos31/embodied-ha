#!/usr/bin/env python3
"""speak.py <room> <message> — preferences.json の speakers に従ってTTS/通知を送る。
環境変数: EHA_PREFS_FILE, HA_URL, SUPERVISOR_TOKEN
"""
import sys
import json
import os
import socket
import subprocess
import urllib.request
import urllib.error
from urllib.parse import urlparse


def get_ha_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def curl_post(url, payload, ha_token):
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "5", "-X", "POST",
         "-H", f"Authorization: Bearer {ha_token}",
         "-H", "Content-Type: application/json",
         "-d", payload, url],
        capture_output=True
    )
    return r.returncode == 0


def _find_speaker(speakers, room: str) -> dict:
    """speakers がリスト形式でも旧辞書形式でも room に対応する設定を返す。"""
    if isinstance(speakers, list):
        for item in speakers:
            if isinstance(item, dict) and item.get("room") == room:
                return item
        return {}
    if isinstance(speakers, dict):
        return speakers.get(room, {})
    return {}


def _rewrite_tts_url(tts_url: str, ha_url: str) -> str:
    """外部向け TTS URL を supervisor プロキシ経由 URL に書き換える。
    ha_url = "http://supervisor/core/api" のとき "/api" を取り除いた
    "http://supervisor/core" をベースに tts_url のパスを接続する。
    """
    parsed = urlparse(tts_url)
    base = ha_url.rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]  # "http://supervisor/core"
    qs = f"?{parsed.query}" if parsed.query else ""
    return f"{base}{parsed.path}{qs}"


def _fetch_pcm_for_message(message: str, ha_url: str, ha_token: str,
                            tts_provider: str, tts_language: str) -> bytes:
    """HA TTS から raw mono s16le 16kHz PCM バイト列を取得する。"""
    payload = json.dumps({
        "platform": tts_provider,
        "message": message,
        "language": tts_language,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        f"{ha_url}/tts_get_url",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"tts_get_url HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"tts_get_url failed: {exc}") from exc

    tts_url = (result.get("url") or "").strip()
    if not tts_url:
        raise RuntimeError("tts_get_url returned no url")

    audio_url = _rewrite_tts_url(tts_url, ha_url)
    fetch_req = urllib.request.Request(
        audio_url,
        headers={"Authorization": f"Bearer {ha_token}"},
    )
    try:
        with urllib.request.urlopen(fetch_req, timeout=15) as resp:
            audio_bytes = resp.read()
    except Exception as exc:
        raise RuntimeError(f"tts audio fetch failed ({audio_url}): {exc}") from exc

    if not audio_bytes:
        raise RuntimeError("tts audio fetch returned empty content")

    proc = subprocess.Popen(
        [
            "ffmpeg", "-loglevel", "error",
            "-i", "pipe:0",
            "-ar", "16000", "-ac", "1", "-f", "s16le",
            "pipe:1",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        pcm_bytes, ffmpeg_err = proc.communicate(input=audio_bytes, timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("ffmpeg conversion timed out")

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {ffmpeg_err.decode('utf-8', errors='replace').strip()}"
        )
    if not pcm_bytes:
        raise RuntimeError("ffmpeg produced empty PCM output")
    return pcm_bytes


def speak(room, message):
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    ha_url = os.environ["HA_URL"]
    ha_token = get_ha_token()

    prefs = {}
    try:
        prefs = json.load(open(prefs_file, encoding="utf-8"))
    except Exception:
        pass

    config = _find_speaker(prefs.get("speakers", []), room)

    if not config:
        print(f"[speak] '{room}' は preferences.json に未登録。TTS をスキップ。", file=sys.stderr)
        return False

    if config.get("type") == "tts":
        payload = json.dumps({
            "entity_id": config["tts_entity"],
            "message": message,
            "media_player_entity_id": config["media_player"]
        }, ensure_ascii=False)
        ok = curl_post(f"{ha_url}/services/tts/speak", payload, ha_token)
        print(f"[speak] TTS:{room} {'OK' if ok else 'NG'}")
        return ok

    elif config.get("type") == "notify":
        # Alexa/mobile_app などは notify エンティティ（notify.xxx）になっており、
        # 旧形式 services/notify/<name> は 400 になる（2026-06-23 実機で確認）。
        entity = config.get("entity", "")
        # 旧設定（notify. を外したサービス名）でも動くよう notify. を補完する
        if entity and not entity.startswith("notify."):
            entity = "notify." + entity
        data = {"entity_id": entity, "message": message}
        if config.get("title"):
            data["title"] = config["title"]
        payload = json.dumps(data, ensure_ascii=False)
        ok = curl_post(f"{ha_url}/services/notify/send_message", payload, ha_token)
        print(f"[speak] notify:{room} {'OK' if ok else 'NG'}")
        return ok

    elif config.get("type") == "tcp":
        # VoiceS3R 等の TCP スピーカーに raw mono s16le 16kHz PCM を push する。
        # デバイス側がサーバー（port 3334 listen）で、TCP 切断が終了合図。
        host = (config.get("host") or "").strip()
        try:
            port = int(config.get("port") or 3334)
        except Exception:
            port = 3334
        if not host or port <= 0:
            print(f"[speak] tcp speaker '{room}': host/port が未設定", file=sys.stderr)
            return False

        tts_provider = (
            config.get("tts_provider")
            or prefs.get("tts_provider")
            or ""
        ).strip()
        tts_language = (
            config.get("tts_language")
            or prefs.get("stt_language")
            or "ja-JP"
        ).strip()
        if not tts_provider:
            print(f"[speak] tcp speaker '{room}': tts_provider が未設定", file=sys.stderr)
            return False

        try:
            pcm_bytes = _fetch_pcm_for_message(
                message, ha_url, ha_token, tts_provider, tts_language
            )
        except Exception as exc:
            print(f"[speak] tcp:{room} TTS 取得失敗: {exc}", file=sys.stderr)
            return False

        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.sendall(pcm_bytes)
            print(f"[speak] tcp:{room} OK sent={len(pcm_bytes)}B ({host}:{port})")
            return True
        except Exception as exc:
            print(f"[speak] tcp:{room} 送信失敗: {exc}", file=sys.stderr)
            return False

    else:
        print(f"[speak] 不明な speaker type: {config.get('type')}", file=sys.stderr)
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("使い方: speak.py <room> <message>", file=sys.stderr)
        sys.exit(1)
    ok = speak(sys.argv[1], sys.argv[2])
    sys.exit(0 if ok else 1)
