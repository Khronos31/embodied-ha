"""Retire preferences owned by the removed always-on audio pipeline.

The migration is intentionally separate from the source-schema migration.  It
removes only settings that could reactivate the legacy background listener
after a rollback.  Active listening still uses the top-level STT provider and
language, so those settings are preserved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

TOP_LEVEL_KEYS = ("wake_words", "wake_ack")
MIC_KEYS = (
    "stt_enabled",
    "stt_retention_hours",
    "wake_word_enabled",
    "background_hearing_enabled",
)


def retire_always_on_audio_preferences(
    preferences: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and a stable list of removed setting paths."""

    migrated = dict(preferences)
    removed: list[str] = []

    for key in TOP_LEVEL_KEYS:
        if key in migrated:
            migrated.pop(key)
            removed.append(key)

    raw_mics = migrated.get("mics")
    if isinstance(raw_mics, list):
        migrated_mics: list[Any] = []
        for index, raw_item in enumerate(raw_mics):
            if not isinstance(raw_item, dict):
                migrated_mics.append(raw_item)
                continue
            item = dict(raw_item)
            for key in MIC_KEYS:
                if key in item:
                    item.pop(key)
                    removed.append(f"mics[{index}].{key}")
            migrated_mics.append(item)
        migrated["mics"] = migrated_mics

    return migrated, removed


def _load_preferences(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError("preferences file must contain a JSON object")
    return payload


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _backup_path(path: Path) -> Path:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.{stamp}.always-on-audio.bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}.{stamp}.{suffix}.always-on-audio.bak"
        )
        suffix += 1
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("preferences", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    try:
        original = _load_preferences(args.preferences)
        migrated, removed = retire_always_on_audio_preferences(original)
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        print(f"always-on audio retirement failed: {exc}", file=sys.stderr)
        return 1

    if not removed:
        print("always-on audio settings already retired; nothing to do")
        return 0

    if not args.apply:
        print("would remove: " + ", ".join(removed))
        return 0

    backup = _backup_path(args.preferences)
    try:
        shutil.copy2(args.preferences, backup)
        _write_atomic_json(args.preferences, migrated)
    except OSError as exc:
        print(f"always-on audio retirement failed: {exc}", file=sys.stderr)
        return 1

    print("removed retired always-on audio settings: " + ", ".join(removed))
    print(f"backup: {backup}")
    print(
        "spoken wake activation now requires an external provider such as "
        "RTSP Assist Gateway; active listening remains available"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
