"""Queued listen intent helpers.

The model can ask for a future listening session instead of calling the
immediate `listen` tool. We persist that intent as a small JSON file so the
next chat/watch/explore session can inject the captured audio into the normal
prompt flow and, only for that session, swap the backend binary/model.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from state_utils import clean, now

DEFAULT_AUDIO_SESSION_BIN = "agy"


def _data_dir() -> str:
    return clean(os.environ.get("EHA_DATA_DIR")) or "/config/embodied-ha"


def queue_request_path() -> str:
    return clean(os.environ.get("EHA_NEXT_LISTEN_REQUEST_FILE")) or os.path.join(_data_dir(), "runtime", "next_listen_request.json")


def next_listen_log_path() -> str:
    return clean(os.environ.get("EHA_NEXT_LISTEN_LOG_FILE")) or os.path.join(_data_dir(), "log", "next_listen_log.jsonl")


def queue_log_path() -> str:
    return next_listen_log_path()


def active_listen_log_path() -> str:
    return clean(os.environ.get("EHA_ACTIVE_LISTEN_LOG_FILE")) or os.path.join(_data_dir(), "log", "active_listen_log.jsonl")


def next_listen_ttl_seconds() -> int:
    try:
        return max(60, int(clean(os.environ.get("EHA_NEXT_LISTEN_TTL_SECONDS")) or 3600))
    except Exception:
        return 3600


def normalize_source_uri(value: str) -> str:
    source = clean(value)
    if source in {"", "alsa", "default"}:
        return "alsa://default"
    if source.startswith("alsa://"):
        device = source[len("alsa://"):].lstrip("/")
        return f"alsa://{device or 'default'}"
    return source


def default_audio_session_bin() -> str:
    return clean(os.environ.get("EHA_SESSION_BIN")) or clean(os.environ.get("EHA_AUDIO_SESSION_BIN")) or clean(os.environ.get("EHA_ANTIGRAVITY_BIN")) or DEFAULT_AUDIO_SESSION_BIN


def audio_session_model() -> str | None:
    model = clean(os.environ.get("EHA_SESSION_MODEL")) or clean(os.environ.get("EHA_AUDIO_SESSION_MODEL"))
    return model or None


def _cooldown_sessions() -> int:
    """セッション数ベースのクールダウン（デフォルト3）。"""
    return max(1, int(os.environ.get("EHA_LISTEN_QUEUE_COOLDOWN_SESSIONS", "3") or "3"))


def _current_session_count() -> int:
    """body_state.json から現在の session_count を返す。読めなければ 0。"""
    try:
        import body_state as _bs

        return int(_bs.read_body_state().get("session_count", 0))
    except Exception:
        return 0


def _last_queue_session_count() -> int | None:
    """next_listen_log.jsonl の最後の action=queue エントリから session_count を返す。なければ None。"""
    path = next_listen_log_path()
    if not os.path.exists(path):
        return None
    last = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("action") == "queue":
                        last = d
                except Exception:
                    pass
    except Exception:
        return None
    if last is None:
        return None
    sc = last.get("session_count")
    return int(sc) if sc is not None else None


def check_listen_queue_cooldown() -> tuple[bool, str]:
    """
    キューを入れてよいか確認する。
    Returns (ok, reason): ok=True なら許可、False なら reason にメッセージ。
    """
    cooldown = _cooldown_sessions()
    last_sc = _last_queue_session_count()
    if last_sc is None:
        return True, ""
    current_sc = _current_session_count()
    elapsed = current_sc - last_sc
    if elapsed < cooldown:
        remaining = cooldown - elapsed
        return False, (
            f"クールダウン中：前回の音声セッションからまだ {elapsed} セッションしか経っていません。"
            f"あと {remaining} セッション後に使えます。"
        )
    return True, ""


def _write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _append_jsonl(path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def queue_next_listen_request(payload: dict) -> str:
    request = dict(payload)
    request.setdefault("request_id", uuid.uuid4().hex)
    request.setdefault("created_at", now().isoformat(timespec="seconds"))
    request.setdefault("expires_after_sec", next_listen_ttl_seconds())
    request["duration"] = max(1, int(request.get("duration") or 5))
    request["transcribe"] = bool(request.get("transcribe", False))
    request["mode"] = clean(request.get("mode")) or "unknown"
    request["session_count"] = _current_session_count()
    request.setdefault("file_path", "")
    request.setdefault("reason", "")
    request.setdefault("note", "")
    _write_json_atomic(queue_request_path(), request)
    _append_jsonl(queue_log_path(), {"action": "queue", **request})
    return queue_request_path()


def load_next_listen_request() -> dict | None:
    path = queue_request_path()
    try:
        with open(path, encoding="utf-8") as f:
            request = json.load(f)
    except Exception:
        return None
    if not isinstance(request, dict):
        return None
    try:
        request["duration"] = max(1, int(request.get("duration") or 5))
    except Exception:
        request["duration"] = 5
    try:
        request["expires_after_sec"] = max(60, int(request.get("expires_after_sec") or next_listen_ttl_seconds()))
    except Exception:
        request["expires_after_sec"] = next_listen_ttl_seconds()
    return request


def consume_next_listen_request() -> dict | None:
    request = load_next_listen_request()
    if not request:
        return None
    created_at = clean(request.get("created_at"))
    if created_at:
        try:
            created_ts = time.mktime(time.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            created_ts = None
        if created_ts is not None and (time.time() - created_ts) > int(request.get("expires_after_sec") or next_listen_ttl_seconds()):
            try:
                os.unlink(queue_request_path())
            except FileNotFoundError:
                pass
            return None
    try:
        os.unlink(queue_request_path())
    except FileNotFoundError:
        pass
    return request


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


def _tcp_request_dir() -> str:
    data_dir = _data_dir()
    return clean(os.environ.get("EHA_ACTIVE_LISTEN_REQUEST_DIR")) or os.path.join(data_dir, "runtime", "active_listen_requests")


def _write_request_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def request_daemon_capture_to_wav(source: str, duration: int, output_path: str) -> None:
    request_id = uuid.uuid4().hex
    request_dir = _tcp_request_dir()
    os.makedirs(request_dir, exist_ok=True)
    response_path = os.path.join(request_dir, f"{request_id}.response.json")
    request_path = os.path.join(request_dir, f"{request_id}.json")
    payload = {
        "request_id": request_id,
        "source": normalize_source_uri(source),
        "duration": max(1, int(duration)),
        "output_path": output_path,
        "response_path": response_path,
        "created_at": time.time(),
    }
    _write_request_json_atomic(request_path, payload)
    deadline = time.monotonic() + duration + 12
    try:
        while time.monotonic() < deadline:
            if os.path.exists(response_path):
                with open(response_path, encoding="utf-8") as f:
                    response = json.load(f)
                if response.get("ok") is not True:
                    raise RuntimeError(clean(response.get("error")) or "daemon capture failed")
                return
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for daemon capture for {source}")
    finally:
        for candidate in (request_path, response_path):
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass


def record_request_to_wav(request: dict, output_path: str) -> None:
    source = normalize_source_uri(request.get("source") or "alsa://default")
    duration = max(1, int(request.get("duration") or 5))
    if source.startswith("tcp://"):
        request_daemon_capture_to_wav(source, duration, output_path)
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    command = build_record_command(source, duration) + [output_path]
    command[0] = ffmpeg
    record = subprocess.run(command, capture_output=True, text=True, timeout=duration + 15)
    if record.returncode != 0:
        message = clean(record.stderr) or clean(record.stdout) or "recording failed"
        raise RuntimeError(message)


def append_active_listen_result(entry: dict) -> None:
    payload = dict(entry)
    payload.setdefault("timestamp", now().isoformat(timespec="seconds"))
    payload.setdefault("kind", "active_listen")
    payload.setdefault("type", "active_listen")
    _append_jsonl(active_listen_log_path(), payload)


def prepare_queued_listen_session(mode: str, *, cwd: str | None = None) -> dict | None:
    request = consume_next_listen_request()
    if not request:
        return None

    bl_path = os.environ.get("EHA_BODY_LOCATION_FILE") or os.path.join(_data_dir(), "body_location.json")
    try:
        with open(bl_path, encoding="utf-8") as f:
            bl = json.load(f)
    except Exception:
        bl = {}
    current_entity = (bl.get("current_entity") or "").strip()

    prefs_path = os.environ.get("EHA_PREFS_FILE") or os.path.join(_data_dir(), "preferences.json")
    try:
        with open(prefs_path, encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception:
        prefs = {}

    audio_sources = prefs.get("audio_sources") or []
    matched_source = None
    matched_label = None
    for entry in audio_sources:
        if not isinstance(entry, dict):
            continue
        entry_entity = (entry.get("entity") or "").strip()
        entry_source = (entry.get("source") or "").strip()
        if entry_entity and entry_entity == current_entity:
            matched_source = entry_source
            matched_label = clean(entry.get("label"))
            break
        if not entry_entity and entry_source and entry_source == current_entity:
            matched_source = entry_source
            matched_label = clean(entry.get("label"))
            break

    if not matched_source:
        entry = {
            'timestamp': now().isoformat(timespec='seconds'),
            'actor': mode,
            'mode': mode,
            'queued_listen': True,
            'prepared_for_session': False,
            'error': f"current_entity '{current_entity}' は audio_sources に登録されていません。VoiceS3R ノードに enter_cyberspace してから queue_next_listen を呼んでください。",
            'request_id': request.get('request_id'),
        }
        append_active_listen_result(entry)
        return {
            'EHA_QUEUED_LISTEN_ERROR': entry['error'],
            'EHA_QUEUED_LISTEN_REQUEST_ID': request.get('request_id') or '',
        }

    source = normalize_source_uri(matched_source)
    audio_dir = os.path.join('/tmp/embodied-ha', 'queued_listen')
    os.makedirs(audio_dir, exist_ok=True)
    wav_path = os.path.join(audio_dir, f"{request.get('request_id') or uuid.uuid4().hex}.wav")
    started_at = now().isoformat(timespec='seconds')
    entry = {
        'timestamp': started_at,
        'actor': mode,
        'mode': mode,
        'source': source,
        'source_label': matched_label or clean(matched_source) or '不明',
        'duration_sec': max(1, int(request.get('duration') or 5)),
        'transcribe_requested': bool(request.get('transcribe', False)),
        'queued_listen': True,
        'request_id': request.get('request_id'),
        'note': clean(request.get('note')),
        'reason': clean(request.get('reason')),
        'file_path': wav_path,
        'prepared_for_session': True,
        'session_bin': default_audio_session_bin(),
        'session_model': audio_session_model() or 'Gemini 3.5 Flash (High)',
    }
    try:
        record_request_to_wav(source, wav_path)
        append_active_listen_result(entry)
        try:
            import body_state as _bs

            _state = _bs.read_body_state()
            _state = _bs.on_audio_session(_state)
            _bs.write_body_state(_state)
        except Exception:
            pass
        return {
            'RECENT_AUDITORY_INPUT': wav_path,
            'EHA_SESSION_BIN': default_audio_session_bin(),
            'EHA_SESSION_MODEL': audio_session_model() or 'Gemini 3.5 Flash (High)',
            'EHA_QUEUED_LISTEN_REQUEST_ID': request.get('request_id') or '',
            'EHA_QUEUED_LISTEN_FILE': wav_path,
            'EHA_QUEUED_LISTEN_SOURCE': entry['source'],
            'EHA_QUEUED_LISTEN_DURATION_SEC': str(entry['duration_sec']),
        }
    except Exception as exc:
        entry['error'] = clean(str(exc)) or 'queued listen session failed'
        append_active_listen_result(entry)
        return {
            'EHA_QUEUED_LISTEN_ERROR': entry['error'],
            'EHA_QUEUED_LISTEN_REQUEST_ID': request.get('request_id') or '',
        }
