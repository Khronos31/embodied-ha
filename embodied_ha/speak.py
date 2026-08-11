#!/usr/bin/env python3
"""speak.py <room> <message> — preferences.json の speakers に従ってTTS/通知を送る。
環境変数: EHA_PREFS_FILE, HA_URL, SUPERVISOR_TOKEN
"""
import sys
import errno
import hashlib
import json
import mimetypes
import os
import shutil
import stat
import subprocess
import tempfile
import wave
from pathlib import Path
from urllib.parse import urlencode

PCM_SAMPLE_RATE = 16000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH_BYTES = 2
MAX_PLAYBACK_SECONDS = 600
MAX_PCM_BYTES = (
    PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH_BYTES * MAX_PLAYBACK_SECONDS
)
MAX_AUDIO_INPUT_BYTES = 128 * 1024 * 1024
MEDIA_SOURCE_DIR = "/media/embodied-ha"
MEDIA_SOURCE_URI_PREFIX = "media-source://media_source/local/embodied-ha"


def get_ha_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def curl_post(url, payload, ha_token):
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "5", "-X", "POST",
         "-H", "@-",
         "-H", "Content-Type: application/json",
         "-d", payload, url],
        input=f"Authorization: Bearer {ha_token}\n".encode(),
        capture_output=True
    )
    return r.returncode == 0


def _normalize_speakers(speakers):
    if isinstance(speakers, list):
        return [item for item in speakers if isinstance(item, dict)]
    if isinstance(speakers, dict):
        return [{**(cfg if isinstance(cfg, dict) else {}), "room": room}
                for room, cfg in speakers.items()]
    return []


def _find_speaker(speakers, room: str) -> dict:
    """speakers がリスト形式でも旧辞書形式でも room に対応する設定を返す。"""
    for item in _normalize_speakers(speakers):
        if item.get("room") == room:
            return item
    return {}


def _tts_media_source_uri(
    tts_entity: str,
    message: str,
    language: str = "",
    voice: str = "",
) -> str:
    """Build a provider-neutral HA TTS Media Source URI.

    Embodied HA may select only a language and a language-specific voice.
    Every other provider-specific setting belongs to the selected Home
    Assistant TTS entity.
    """
    if not tts_entity.startswith("tts."):
        raise ValueError("tts_entity must be a tts.* entity")
    params = {"message": message, "cache": "false"}
    language = str(language or "").strip()
    voice = str(voice or "").strip()
    if language:
        params["language"] = language
        if voice:
            params["voice"] = voice
    query = urlencode(params)
    return f"media-source://tts/{tts_entity}?{query}"


def _tts_selection(prefs: dict, tts_entity: str) -> tuple[str, str]:
    """Return the language and voice stored for the active TTS entity only."""

    selections = prefs.get("tts_selections")
    if not isinstance(selections, dict):
        return "", ""
    selection = selections.get(tts_entity)
    if not isinstance(selection, dict):
        return "", ""
    language = selection.get("language")
    voice = selection.get("voice")
    language = language.strip() if isinstance(language, str) else ""
    voice = voice.strip() if language and isinstance(voice, str) else ""
    return language, voice


def _convert_audio_file_to_pcm(audio_path: str) -> bytes:
    """WAV等のヘッダ付き音声ファイルを raw mono s16le 16kHz PCM に変換する。"""
    proc = subprocess.Popen(
        [
            "ffmpeg", "-loglevel", "error", "-xerror", "-err_detect", "explode",
            "-i", audio_path,
            "-t", str(MAX_PLAYBACK_SECONDS + 1),
            "-ar", str(PCM_SAMPLE_RATE), "-ac", str(PCM_CHANNELS), "-f", "s16le",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        pcm_bytes, ffmpeg_err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("ffmpeg conversion timed out")

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {ffmpeg_err.decode('utf-8', errors='replace').strip()}"
        )
    if not pcm_bytes:
        raise RuntimeError("ffmpeg produced empty PCM output")
    if len(pcm_bytes) > MAX_PCM_BYTES:
        raise OSError(errno.EFBIG, os.strerror(errno.EFBIG), audio_path)
    return pcm_bytes


def _pcm_bytes_from_file(audio_path: str) -> bytes:
    """明示的なraw PCMはそのまま、通常音声はffmpegで再生用PCMへ正規化する。"""
    try:
        file_stat = os.stat(audio_path)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), audio_path)
        if file_stat.st_size <= 0:
            raise OSError(errno.ENODATA, os.strerror(errno.ENODATA), audio_path)
        if file_stat.st_size > MAX_AUDIO_INPUT_BYTES:
            raise OSError(errno.EFBIG, os.strerror(errno.EFBIG), audio_path)
    except Exception as exc:
        raise RuntimeError(f"audio file read failed ({audio_path}): {exc}") from exc

    if os.path.splitext(audio_path)[1].lower() != ".pcm":
        return _convert_audio_file_to_pcm(audio_path)

    # raw PCMには形式を自己記述するヘッダがない。.pcmは呼び出し側が
    # mono s16le/16kHzを保証する明示契約とし、最低限の長さと上限だけ検証する。
    if file_stat.st_size > MAX_PCM_BYTES:
        raise OSError(errno.EFBIG, os.strerror(errno.EFBIG), audio_path)
    if file_stat.st_size % PCM_SAMPLE_WIDTH_BYTES:
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), audio_path)
    try:
        with open(audio_path, "rb") as f:
            raw_bytes = f.read(MAX_PCM_BYTES + 1)
    except Exception as exc:
        raise RuntimeError(f"audio file read failed ({audio_path}): {exc}") from exc
    if not raw_bytes:
        raise OSError(errno.ENODATA, os.strerror(errno.ENODATA), audio_path)
    if len(raw_bytes) != file_stat.st_size:
        raise OSError(errno.EIO, os.strerror(errno.EIO), audio_path)
    if len(raw_bytes) > MAX_PCM_BYTES:
        raise OSError(errno.EFBIG, os.strerror(errno.EFBIG), audio_path)
    if len(raw_bytes) % PCM_SAMPLE_WIDTH_BYTES:
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), audio_path)
    return raw_bytes


def _speaker_media_player(config: dict) -> str:
    legacy_type = str(config.get("type") or "").strip()
    if legacy_type not in {"", "tts"}:
        raise ValueError(f"legacy speaker type is unsupported: {legacy_type}")
    entity_id = str(config.get("entity") or config.get("media_player") or "").strip()
    if not entity_id.startswith("media_player."):
        raise ValueError("media_player entity is required")
    return entity_id


def _write_wav_atomic(path: Path, pcm_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w+b") as raw_file:
            with wave.open(raw_file, "wb") as wav_file:
                wav_file.setnchannels(PCM_CHANNELS)
                wav_file.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
                wav_file.setframerate(PCM_SAMPLE_RATE)
                wav_file.writeframes(pcm_bytes)
            raw_file.flush()
            os.fsync(raw_file.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with source.open("rb") as source_file, os.fdopen(fd, "wb") as target_file:
            shutil.copyfileobj(source_file, target_file)
            target_file.flush()
            os.fsync(target_file.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, destination)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _stage_media_source(audio_path: str) -> tuple[Path, str, str]:
    """Validate an audio file and persist it in HA's local Media Source."""
    source = Path(audio_path)
    pcm_bytes = _pcm_bytes_from_file(str(source))
    source_suffix = source.suffix.lower()
    guessed_type = mimetypes.guess_type(source.name)[0] or ""
    if guessed_type in {"audio/x-wav", "audio/vnd.wave"}:
        guessed_type = "audio/wav"

    # Raw PCM has no self-describing header. Formats HA cannot identify as audio
    # are normalized to WAV as well, preserving the existing ffmpeg acceptance
    # contract while ensuring media_player receives a playable object.
    convert_to_wav = source_suffix == ".pcm" or not guessed_type.startswith("audio/")
    digest_source = pcm_bytes if convert_to_wav else source.read_bytes()
    digest = hashlib.sha256(digest_source).hexdigest()
    suffix = ".wav" if convert_to_wav else source_suffix
    media_type = "audio/wav" if convert_to_wav else guessed_type
    media_dir = Path(os.environ.get("EHA_MEDIA_SOURCE_DIR") or MEDIA_SOURCE_DIR)
    destination = media_dir / f"{digest}{suffix}"

    if not destination.exists():
        if convert_to_wav:
            _write_wav_atomic(destination, pcm_bytes)
        else:
            _copy_atomic(source, destination)
    uri = f"{MEDIA_SOURCE_URI_PREFIX}/{destination.name}"
    return destination, uri, media_type


def _load_preferences() -> dict:
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    try:
        with open(prefs_file, encoding="utf-8") as f:
            loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def play_audio_file(room, audio_path):
    prefs = _load_preferences()
    ha_url = os.environ["HA_URL"]
    ha_token = get_ha_token()

    config = _find_speaker(prefs.get("speakers", []), room)

    if not config:
        print(f"[speak] '{room}' は preferences.json に未登録。音声再生をスキップ。", file=sys.stderr)
        return False

    try:
        media_player = _speaker_media_player(config)
        persisted_path, media_uri, media_type = _stage_media_source(audio_path)
    except Exception as exc:
        print(f"[speak] audio file staging failed ({audio_path}): {exc}", file=sys.stderr)
        return False

    payload = json.dumps({
        "entity_id": media_player,
        "media_content_id": media_uri,
        "media_content_type": media_type,
    }, ensure_ascii=False)
    ok = curl_post(f"{ha_url}/services/media_player/play_media", payload, ha_token)
    print(
        f"[speak] Media Source:{room} {'OK' if ok else 'NG'} "
        f"path={persisted_path}"
    )
    return ok


def speak(room, message):
    ha_url = os.environ["HA_URL"]
    ha_token = get_ha_token()
    prefs = _load_preferences()
    config = _find_speaker(prefs.get("speakers", []), room)

    if not config:
        print(f"[speak] '{room}' は preferences.json に未登録。TTS をスキップ。", file=sys.stderr)
        return False

    try:
        media_player = _speaker_media_player(config)
    except ValueError as exc:
        print(f"[speak] speaker '{room}' の設定が無効です: {exc}", file=sys.stderr)
        return False
    tts_entity = str(prefs.get("tts_entity") or "").strip()
    language, voice = _tts_selection(prefs, tts_entity)
    try:
        media_uri = _tts_media_source_uri(tts_entity, message, language, voice)
    except ValueError as exc:
        print(f"[speak] tts speaker '{room}': {exc}", file=sys.stderr)
        return False
    payload = json.dumps({
        "entity_id": media_player,
        "media": {
            "media_content_id": media_uri,
            # HA resolves a Media Source URI and replaces this generic class
            # with the TTS stream's actual MIME type before device playback.
            "media_content_type": "music",
        },
    }, ensure_ascii=False)
    ok = curl_post(f"{ha_url}/services/media_player/play_media", payload, ha_token)
    if not ok and (language or voice):
        print(
            f"[speak] TTS options failed for {tts_entity}; retrying entity defaults",
            file=sys.stderr,
        )
        payload_data = json.loads(payload)
        payload_data["media"]["media_content_id"] = _tts_media_source_uri(
            tts_entity,
            message,
        )
        ok = curl_post(
            f"{ha_url}/services/media_player/play_media",
            json.dumps(payload_data, ensure_ascii=False),
            ha_token,
        )
    print(f"[speak] TTS:{room} {'OK' if ok else 'NG'}")
    return ok


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("room")
    parser.add_argument("message", nargs="?", default="")
    parser.add_argument(
        "--file-path",
        default="",
        help="最長10分の音声ファイルをHA Media Source経由で再生する",
    )
    a = parser.parse_args()
    if a.file_path:
        ok = play_audio_file(a.room, a.file_path)
    else:
        if not a.message:
            parser.error("message or --file-path is required")
        ok = speak(a.room, a.message)
    sys.exit(0 if ok else 1)
