#!/usr/bin/env python3
"""VOICEVOX Song synthesis helpers."""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

FRAME_RATE = 93.75
SILENCE_FRAMES = 15
DEFAULT_SINGER_NAME = "春日部つむぎ"
DEFAULT_STYLE_NAME = "ノーマル"
SINGING_TEACHER_STYLE_ID = 6000
VOICEVOX_DIR = os.environ.get("EHA_VOICEVOX_CORE_DIR", "/data/voicevox_core")
VOICEVOX_PKG_DIR = os.path.join(VOICEVOX_DIR, "python-packages")

_DURATION_BEATS = {
    "whole": 4.0,
    "half": 2.0,
    "quarter": 1.0,
    "eighth": 0.5,
    "sixteenth": 0.25,
}
_PITCH_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")
_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SYNTH = None


def plugin_disabled_payload() -> dict[str, str]:
    return {
        "error": "plugin_disabled",
        "message": "VOICEVOX Songが無効です。Web UIのその他の機能タブからインストールしてください。",
    }


def add_voicevox_python_path() -> None:
    if os.path.isdir(VOICEVOX_PKG_DIR) and VOICEVOX_PKG_DIR not in sys.path:
        sys.path.insert(0, VOICEVOX_PKG_DIR)


def is_installed(data_dir: str = VOICEVOX_DIR) -> bool:
    base = Path(data_dir)
    return (
        (base / "python-packages" / "voicevox_core").exists()
        and (base / "dict" / "open_jtalk_dic_utf_8-1.11").exists()
        and (base / "onnxruntime" / "lib").exists()
        and any((base / "models" / "vvms").glob("*.vvm"))
    )


def _load_bindings():
    add_voicevox_python_path()
    from voicevox_core import Note, Score
    from voicevox_core.blocking import Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile
    return Note, Score, Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile


def create_synthesizer(data_dir: str = VOICEVOX_DIR):
    Note, Score, Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile = _load_bindings()
    base = Path(data_dir)
    ort = Onnxruntime.load_once(
        filename=str(base / "onnxruntime" / "lib" / Onnxruntime.LIB_VERSIONED_FILENAME)
    )
    synth = Synthesizer(
        ort,
        OpenJtalk(str(base / "dict" / "open_jtalk_dic_utf_8-1.11")),
        acceleration_mode="CPU",
        cpu_num_threads=max(os.cpu_count() or 1, 2),
    )
    for vvm_path in sorted((base / "models" / "vvms").glob("*.vvm")):
        try:
            with VoiceModelFile.open(vvm_path) as model:
                synth.load_voice_model(model)
        except Exception:
            pass
    return synth


def get_synthesizer(data_dir: str = VOICEVOX_DIR):
    global _SYNTH
    if _SYNTH is None:
        _SYNTH = create_synthesizer(data_dir)
    return _SYNTH


def parse_pitch(pitch: str) -> int | None:
    value = str(pitch or "").strip()
    if value.lower() == "rest":
        return None
    match = _PITCH_RE.match(value)
    if not match:
        raise ValueError(f"invalid pitch: {pitch}")
    name, accidental, octave_text = match.groups()
    semitone = _SEMITONES[name.upper()]
    if accidental == "#":
        semitone += 1
    elif accidental == "b":
        semitone -= 1
    midi = (int(octave_text) + 1) * 12 + semitone
    if midi < 0 or midi > 127:
        raise ValueError(f"pitch out of MIDI range: {pitch}")
    return midi


def duration_to_frames(duration: str, bpm: int | float) -> int:
    duration_key = str(duration or "").strip().lower()
    if duration_key not in _DURATION_BEATS:
        raise ValueError(f"invalid duration: {duration}")
    try:
        bpm_value = float(bpm)
    except Exception as exc:
        raise ValueError("bpm must be numeric") from exc
    if bpm_value <= 0:
        raise ValueError("bpm must be positive")
    seconds = (60.0 / bpm_value) * _DURATION_BEATS[duration_key]
    return max(1, round(seconds * FRAME_RATE))


def build_score_entries(notes: list[dict[str, Any]], bpm: int | float, *, pad_frames: int = SILENCE_FRAMES) -> list[dict[str, Any]]:
    if not isinstance(notes, list) or not notes:
        raise ValueError("notes must be a non-empty array")
    entries: list[dict[str, Any]] = [{"key": None, "frame_length": int(pad_frames), "lyric": ""}]
    for index, item in enumerate(notes):
        if not isinstance(item, dict):
            raise ValueError(f"notes[{index}] must be an object")
        key = parse_pitch(str(item.get("pitch") or ""))
        frame_length = duration_to_frames(str(item.get("duration") or ""), bpm)
        if key is None:
            lyric = ""
        else:
            lyric = str(item.get("lyric") or "").strip()
            if not lyric:
                raise ValueError(f"notes[{index}].lyric is required for pitched notes")
        entries.append({"key": key, "frame_length": frame_length, "lyric": lyric})
    entries.append({"key": None, "frame_length": int(pad_frames), "lyric": ""})
    return entries


def create_score_from_entries(entries: list[dict[str, Any]]):
    Note, Score, *_ = _load_bindings()
    return Score([
        Note(int(entry["frame_length"]), str(entry.get("lyric") or ""), key=entry.get("key"))
        for entry in entries
    ])


def _frame_decode_styles(synth) -> list[dict[str, Any]]:
    singers: list[dict[str, Any]] = []
    for meta in synth.metas():
        for style in meta.styles:
            if getattr(style, "type", "") == "frame_decode":
                singers.append({
                    "name": meta.name,
                    "style_name": style.name,
                    "style_id": style.id,
                    "credit": f"VOICEVOX:{meta.name}",
                })
    return singers


def list_singers(data_dir: str = VOICEVOX_DIR) -> list[dict[str, Any]]:
    if not is_installed(data_dir):
        return []
    return _frame_decode_styles(get_synthesizer(data_dir))


def resolve_singer_style_id(synth, character_name: str, style_name: str = DEFAULT_STYLE_NAME) -> int:
    for meta in synth.metas():
        if meta.name == character_name:
            fallback = None
            for style in meta.styles:
                if getattr(style, "type", "") == "frame_decode":
                    if style.name == style_name:
                        return int(style.id)
                    if fallback is None:
                        fallback = int(style.id)
            if fallback is not None:
                return fallback
    raise ValueError(f"{character_name} に歌唱スタイルが見つかりません")


def _style_by_id(synth, style_id: int) -> tuple[str, str, int] | None:
    for meta in synth.metas():
        for style in meta.styles:
            if int(style.id) == int(style_id) and getattr(style, "type", "") == "frame_decode":
                return meta.name, style.name, int(style.id)
    return None


def _normalize_for_match(value: str) -> str:
    return re.sub(r"[\s_.\-:]+", "", value).lower()


def _character_name_from_tts_entity(tts_entity: str, synth) -> str:
    normalized_entity = _normalize_for_match(tts_entity)
    aliases = {
        DEFAULT_SINGER_NAME: {"kasukabetsumugi", "tsumugi", "kasukabe"},
    }
    for meta in synth.metas():
        names = {_normalize_for_match(meta.name), *aliases.get(meta.name, set())}
        if any(name and name in normalized_entity for name in names):
            return meta.name
    return ""


def resolve_singer_from_preferences(synth, prefs: dict[str, Any]) -> dict[str, Any]:
    configured = prefs.get("sing_speaker") if isinstance(prefs, dict) else None
    if isinstance(configured, dict):
        style_id = configured.get("style_id")
        if style_id is not None:
            try:
                found = _style_by_id(synth, int(style_id))
            except Exception:
                found = None
            if found:
                name, style_name, resolved_id = found
                return {"name": name, "style_name": style_name, "style_id": resolved_id, "credit": f"VOICEVOX:{name}"}
        name = str(configured.get("name") or "").strip()
        if name:
            resolved_id = resolve_singer_style_id(synth, name, str(configured.get("style_name") or DEFAULT_STYLE_NAME))
            return {"name": name, "style_name": str(configured.get("style_name") or DEFAULT_STYLE_NAME), "style_id": resolved_id, "credit": f"VOICEVOX:{name}"}

    tts_name = _character_name_from_tts_entity(str(prefs.get("tts_entity") or "") if isinstance(prefs, dict) else "", synth)
    if tts_name:
        try:
            resolved_id = resolve_singer_style_id(synth, tts_name, DEFAULT_STYLE_NAME)
            return {"name": tts_name, "style_name": DEFAULT_STYLE_NAME, "style_id": resolved_id, "credit": f"VOICEVOX:{tts_name}"}
        except Exception:
            pass

    resolved_id = resolve_singer_style_id(synth, DEFAULT_SINGER_NAME, DEFAULT_STYLE_NAME)
    return {"name": DEFAULT_SINGER_NAME, "style_name": DEFAULT_STYLE_NAME, "style_id": resolved_id, "credit": f"VOICEVOX:{DEFAULT_SINGER_NAME}"}


def load_preferences() -> dict[str, Any]:
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    if not prefs_file:
        return {}
    try:
        with open(prefs_file, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def default_wav_dir() -> str:
    configured = os.environ.get("EHA_AUDIO_WAV_DIR", "").strip()
    if configured:
        return configured
    data_dir = os.environ.get("EHA_DATA_DIR", "").strip()
    if data_dir:
        return os.path.join(data_dir, "wav")
    return "/config/embodied-ha/wav"


def write_wav_atomic(wav_bytes: bytes, output_dir: str | None = None) -> str:
    target_dir = output_dir or default_wav_dir()
    os.makedirs(target_dir, exist_ok=True)
    final_path = os.path.join(target_dir, f"song-{uuid.uuid4().hex}.wav")
    tmp_path = f"{final_path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(wav_bytes)
    os.replace(tmp_path, final_path)
    return final_path


def synthesize_song(args: dict[str, Any], *, data_dir: str = VOICEVOX_DIR, output_dir: str | None = None) -> dict[str, Any]:
    if not is_installed(data_dir):
        raise RuntimeError("VOICEVOX Song is not installed")
    bpm = args.get("bpm", 100)
    entries = build_score_entries(args.get("notes"), bpm)
    synth = get_synthesizer(data_dir)
    prefs = load_preferences()
    singer = resolve_singer_from_preferences(synth, prefs)
    score = create_score_from_entries(entries)
    query = synth.create_sing_frame_audio_query(score, SINGING_TEACHER_STYLE_ID)
    wav_bytes = synth.frame_synthesis(query, singer["style_id"])
    wav_path = write_wav_atomic(wav_bytes, output_dir=output_dir)
    return {"wav_path": wav_path, "score": entries, "credit": singer["credit"]}
