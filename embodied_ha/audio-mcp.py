#!/usr/bin/env python3
"""音声 listen 用 MCP サーバー。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from mcp_lib import serve, text
from state_utils import clean, now

DEFAULT_SOURCES = [
    {"source": "rtsp://localhost:8554/capture_tv", "label": "TV・レコーダー"},
    {"source": "rtsp://localhost:8556/mic_only",   "label": "PC"},
    {"source": "rtsp://localhost:8558/mic_only",   "label": "Google TV"},
    {"source": "alsa",                             "label": "スタディマイク"},
]
DEFAULT_SOURCE = "rtsp://localhost:8554/capture_tv"
MAX_DURATION = 30
TMP_DIR = Path("/tmp/embodied-ha/audio")
def build_listen_spec() -> dict:
    sources = load_audio_sources()
    source_lines = "\n".join(
        f'  - "{s["source"]}"（{s["label"]}）' for s in sources
    )
    default = sources[0]["source"] if sources else "alsa"
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
    if source == "alsa":
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
    with open(path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(
        f"http://supervisor/core/api/stt/{provider}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "audio/wav",
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


def listen(args: dict):
    source = clean(args.get("source")) or DEFAULT_SOURCE
    # 未登録でも rtsp:// または alsa なら直接使用する
    if source not in _source_map() and not (source.startswith("rtsp://") or source == "alsa"):
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


TOOL_DIAGNOSE = {
    "name": "diagnose",
    "description": "オーディオ環境の診断情報を返す。音量ゼロ・デバイス不明時のデバッグ用。",
    "inputSchema": {"type": "object", "properties": {}},
}


def diagnose(args: dict):
    result: dict = {}

    # H2: PulseAudio関連環境変数（HAOS が何を注入しているか全部見る）
    pulse_env = {k: v for k, v in os.environ.items() if "PULSE" in k or "AUDIO" in k or "SOUND" in k}
    result["env_pulse"] = pulse_env

    # preferences.json が読めているか確認
    result["prefs_file"] = _prefs_path() or "(EHA_PREFS_FILE未設定)"
    prefs = load_preferences()
    result["audio_sources_loaded"] = prefs.get("audio_sources", "(未設定→DEFAULTを使用)")
    result["stt_provider_loaded"] = prefs.get("stt_provider", "(未設定)")

    # ソケットを実際に探す（HAOSがどのパスに置くか確認）
    socket_candidates = [
        "/run/audio",
        "/run/audio/native",
        "/run/pulse/native",
        "/var/run/pulse/native",
        "/run/user/0/pulse/native",
        "/run/user/1000/pulse/native",
        "/tmp/pulse",
        "/root/.pulse/native",
        "/var/run/pulse",
        "/run/pulse",
    ]
    # /run/audio 配下も全列挙
    try:
        result["run_audio_ls"] = sorted(os.listdir("/run/audio"))
    except Exception as e:
        result["run_audio_ls"] = str(e)
    result["socket_search"] = {}
    for path in socket_candidates:
        import stat as stat_mod
        try:
            st = os.stat(path)
            is_sock = stat_mod.S_ISSOCK(st.st_mode)
            is_dir = stat_mod.S_ISDIR(st.st_mode)
            result["socket_search"][path] = "socket" if is_sock else ("dir" if is_dir else "file")
        except FileNotFoundError:
            result["socket_search"][path] = "not found"

    # /run 直下を列挙（HAOS が何を注入しているか）
    try:
        result["run_ls"] = sorted(os.listdir("/run"))
    except Exception as e:
        result["run_ls"] = str(e)

    # /dev/snd があるか（raw ALSA）
    try:
        result["dev_snd_ls"] = sorted(os.listdir("/dev/snd"))
    except Exception:
        result["dev_snd_ls"] = "not found"

    # H4: find_ffmpeg() がどのバイナリを返すか
    ffmpeg = find_ffmpeg()
    result["ffmpeg_path"] = ffmpeg or "not found"
    if ffmpeg:
        r = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=10)
        first = (r.stdout or r.stderr or "").splitlines()
        result["ffmpeg_version"] = first[0] if first else ""
        r2 = subprocess.run([ffmpeg, "-f", "pulse", "-i", "dummy", "-t", "0", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=5)
        result["ffmpeg_pulse_support"] = "pulse" in (r2.stderr or "").lower() and \
            "unknown input format" not in (r2.stderr or "").lower()

    # H1: pactl でPulseAudioソース一覧
    pactl = shutil.which("pactl")
    result["pactl_path"] = pactl or "not found"
    if pactl:
        r = subprocess.run([pactl, "info"], capture_output=True, text=True, timeout=5)
        result["pactl_info"] = (r.stdout or r.stderr or "").strip()
        r2 = subprocess.run([pactl, "list", "sources", "short"],
                            capture_output=True, text=True, timeout=5)
        result["pactl_sources"] = (r2.stdout or r2.stderr or "").strip()

    # H3: ALSA cards
    r = subprocess.run(["cat", "/proc/asound/cards"], capture_output=True, text=True, timeout=5)
    result["alsa_cards"] = (r.stdout or "not available").strip()

    # H1追加: ffmpegでpulse default録音テスト（PULSE_SERVER設定済みのとき）
    if ffmpeg and result.get("ffmpeg_pulse_support") and os.environ.get("PULSE_SERVER"):
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=TMP_DIR, suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            subprocess.run(
                [ffmpeg, "-f", "pulse", "-i", "default", "-ar", "16000", "-ac", "1", "-t", "2", "-y", tmp_path],
                capture_output=True, text=True, timeout=15,
            )
            peak, mean = analyze_volume(tmp_path)
            result["pulse_direct_test"] = {"peak_db": peak, "mean_db": mean, "has_sound": has_sound_from_peak(peak)}
        except Exception as e:
            result["pulse_direct_test"] = {"error": str(e)}
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return [text(json.dumps(result, indent=2, ensure_ascii=False))]


if __name__ == "__main__":
    serve("audio-mcp", "1.0", {
        "listen":   {"spec": TOOL_LISTEN,   "handler": listen},
        "diagnose": {"spec": TOOL_DIAGNOSE, "handler": diagnose},
    })
