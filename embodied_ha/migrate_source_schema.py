#!/usr/bin/env python3
"""Migrate legacy source schema to the 4-category perception schema.

Legacy keys:
  - cameras
  - audio_sources

New keys:
  - cameras
  - mics
  - video_media
  - audio_media

The classifier is intentionally conservative: if an entry is not clearly media,
it stays in the sensing buckets (cameras/mics).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from state_utils import clean  # type: ignore


NEW_BUCKETS = ("cameras", "mics", "video_media", "audio_media")
LEGACY_BUCKETS = ("cameras", "audio_sources")


# 強いメディア信号: エンティティ型/センサー語より優先してメディアに分類する。
# スクショ/画面/キャプチャ/レコーダーは、たとえHAカメラエンティティ型(camera.*)でも
# “目”ではなく“画面/コンテンツ”。音声の rtsp:// はキャプチャ箱/AVフィード(実マイクは
# alsa/tcp/i2s)なのでメディア扱い。弱いメディア信号(_tv/tv/video等)はセンサー語と競合したら
# 保守的にセンサー側へ残す。
CAMERA_STRONG_MEDIA = (
    "screenshot",
    "screen_capture",
    "pc_screen",
    "screen",
    "capture",
    "recorder",
    "recording",
)
CAMERA_WEAK_MEDIA = (
    "_tv",
    "tv_",
    "tv",
    "video",
    "movie",
)

CAMERA_SENSOR_TOKENS = (
    "camera.",
    "webcam",
    "live_view",
    "liveview",
    "ipcam",
    "doorbell",
)

AUDIO_STRONG_MEDIA = (
    "screenshot",
    "screen_capture",
    "pc_screen",
    "capture",
    "recorder",
    "recording",
    "rtsp://",
    "rtsp:",
)
AUDIO_WEAK_MEDIA = (
    "_tv",
    "tv_",
    "tv",
    "video",
    "movie",
    "music",
)

AUDIO_SENSOR_TOKENS = (
    "mic_only",
    "microphone",
    "micro",
    "alsa",
    "tcp://",
    "tcp:",
    "voice",
    "voices3r",
    "line_in",
    "input",
)


@dataclass(frozen=True)
class Classification:
    category: str
    reason: str
    ambiguous: bool = False
    matched: tuple[str, ...] = ()


def _haystack(entry: dict[str, Any]) -> str:
    parts = []
    for key in ("entity", "id", "source", "room", "label", "note", "ha_entity"):
        parts.append(clean(entry.get(key)).lower())
    return " ".join(part for part in parts if part)


def _match_tokens(haystack: str, tokens: tuple[str, ...]) -> list[str]:
    hits = []
    for token in tokens:
        if token in haystack and token not in hits:
            hits.append(token)
    return hits


def classify_source_detail(kind: str, entry: dict[str, Any]) -> Classification:
    """Return the conservative 4-category classification for a source entry."""

    haystack = _haystack(entry)
    if kind not in {"camera", "audio"}:
        raise ValueError(f"unsupported kind: {kind}")

    strong_media = CAMERA_STRONG_MEDIA if kind == "camera" else AUDIO_STRONG_MEDIA
    weak_media = CAMERA_WEAK_MEDIA if kind == "camera" else AUDIO_WEAK_MEDIA
    sensor_tokens = CAMERA_SENSOR_TOKENS if kind == "camera" else AUDIO_SENSOR_TOKENS
    media_cat = "video_media" if kind == "camera" else "audio_media"
    sense_cat = "cameras" if kind == "camera" else "mics"

    strong_hits = _match_tokens(haystack, strong_media)
    weak_hits = _match_tokens(haystack, weak_media)
    sensor_hits = _match_tokens(haystack, sensor_tokens)

    # 強いメディア信号(screenshot/capture/rtsp等)はセンサー語を上書きしてメディアへ。
    if strong_hits:
        return Classification(
            category=media_cat,
            reason=f"strong media: {strong_hits[0]}",
            matched=tuple(strong_hits),
        )

    # 弱いメディア信号 + センサー語 = 曖昧 → 保守的にセンサー側へ残す。
    if weak_hits and sensor_hits:
        return Classification(
            category=sense_cat,
            reason=f"ambiguous: media={','.join(weak_hits)} sensor={','.join(sensor_hits)}",
            ambiguous=True,
            matched=tuple(weak_hits + sensor_hits),
        )

    if weak_hits:
        return Classification(
            category=media_cat,
            reason=f"media keyword: {weak_hits[0]}",
            matched=tuple(weak_hits),
        )

    if sensor_hits:
        return Classification(
            category=sense_cat,
            reason=f"sense keyword: {sensor_hits[0]}",
            matched=tuple(sensor_hits),
        )

    return Classification(
        category=sense_cat,
        reason="defaulted to sensing bucket",
        ambiguous=False,
        matched=(),
    )


def classify_source(kind: str, entry: dict[str, Any]) -> str:
    return classify_source_detail(kind, entry).category


def _coerce_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            out.append(dict(item))
    return out


def _normalize_media_item(entry: dict[str, Any], *, source_key: str) -> dict[str, Any]:
    item = dict(entry)
    if "id" not in item and item.get("entity") is not None:
        item["id"] = item.pop("entity")
    item.pop("entity", None)
    if source_key == "video_media":
        item.pop("video_media", None)
    return item


def build_source_draft(prefs: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Transform legacy source lists into the new four-bucket schema."""

    draft: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in NEW_BUCKETS}
    warnings: list[str] = []

    for idx, entry in enumerate(_coerce_list(prefs.get("cameras"))):
        detail = classify_source_detail("camera", entry)
        item = dict(entry)
        if detail.category in ("video_media", "audio_media"):
            item = _normalize_media_item(item, source_key=detail.category)
        draft[detail.category].append(item)
        suffix = " [ambiguous]" if detail.ambiguous else ""
        warnings.append(
            f"cameras[{idx}] -> {detail.category}{suffix}: {detail.reason}"
        )

    for idx, entry in enumerate(_coerce_list(prefs.get("audio_sources"))):
        detail = classify_source_detail("audio", entry)
        item = dict(entry)
        if detail.category in ("video_media", "audio_media"):
            item = _normalize_media_item(item, source_key=detail.category)
        draft[detail.category].append(item)
        suffix = " [ambiguous]" if detail.ambiguous else ""
        warnings.append(
            f"audio_sources[{idx}] -> {detail.category}{suffix}: {detail.reason}"
        )

    return draft, warnings


def _is_new_schema_present(prefs: dict[str, Any]) -> bool:
    return any(bucket in prefs for bucket in ("mics", "video_media", "audio_media"))


def _summary(draft: dict[str, list[dict[str, Any]]]) -> str:
    counts = ", ".join(f"{bucket}={len(draft[bucket])}" for bucket in NEW_BUCKETS)
    return counts


def _load_prefs(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("preferences file must contain a JSON object")
    return data


def _write_atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _backup_path(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    return path.with_name(f"{path.name}.{stamp}.bak")


def migrate_preferences(prefs: dict[str, Any]) -> dict[str, Any]:
    """Return a migrated preferences object without mutating the input."""

    migrated = dict(prefs)
    draft, _warnings = build_source_draft(prefs)
    for bucket in NEW_BUCKETS:
        migrated[bucket] = draft[bucket]
    migrated.pop("audio_sources", None)
    return migrated


def _report_dry_run(prefs_path: Path, prefs: dict[str, Any]) -> int:
    if _is_new_schema_present(prefs):
        print(f"{prefs_path}: already migrated; new source buckets are present")
        return 0

    draft, warnings = build_source_draft(prefs)
    print(f"{prefs_path}: dry-run preview")
    if warnings:
        for line in warnings:
            print(f"  [warn] {line}")
    for bucket in NEW_BUCKETS:
        print(f"{bucket} ({len(draft[bucket])})")
        for item in draft[bucket]:
            ident = item.get("id", item.get("entity", ""))
            source = clean(item.get("source"))
            label = clean(item.get("label"))
            room = clean(item.get("room"))
            extra = []
            if source:
                extra.append(f"source={source}")
            if room:
                extra.append(f"room={room}")
            if label:
                extra.append(f"label={label}")
            if item.get("ptz") is not None:
                extra.append("ptz")
            if item.get("video_media"):
                extra.append(f"video_media={clean(item.get('video_media'))}")
            print(f"  - {ident} | {'; '.join(extra) if extra else 'no metadata'}")
    print(f"summary: {_summary(draft)}")
    return 0


def _report_apply(prefs_path: Path, prefs: dict[str, Any]) -> int:
    if _is_new_schema_present(prefs):
        print(f"{prefs_path}: already migrated; nothing to do")
        return 0

    draft, warnings = build_source_draft(prefs)
    for line in warnings:
        print(f"[warn] {line}")

    backup = _backup_path(prefs_path)
    shutil.copy2(prefs_path, backup)
    migrated = migrate_preferences(prefs)
    _write_atomic_json(prefs_path, migrated)
    print(f"backup: {backup}")
    print(f"written: {prefs_path}")
    print(f"summary: {_summary(draft)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy source schema to 4 buckets")
    parser.add_argument("prefs_file", nargs="?", default=os.environ.get("EHA_PREFS_FILE", "preferences.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the migration preview only")
    mode.add_argument("--apply", action="store_true", help="write the migrated file atomically")
    args = parser.parse_args(argv)

    prefs_path = Path(args.prefs_file)
    try:
        prefs = _load_prefs(prefs_path)
    except Exception as exc:
        print(f"{prefs_path}: failed to read preferences.json: {exc}", file=sys.stderr)
        return 1

    if args.apply:
        return _report_apply(prefs_path, prefs)
    return _report_dry_run(prefs_path, prefs)


if __name__ == "__main__":
    raise SystemExit(main())
