#!/usr/bin/env python3
"""Translate Claude-style content blocks for non-Claude harnesses."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shutil
import time
from pathlib import Path


MAX_IMAGES = 8
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024
MAX_INPUT_JSON_BYTES = 48 * 1024 * 1024
STALE_SECONDS = 60 * 60

_MEDIA = {
    "image/jpeg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/png": (".png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/webp": (
        ".webp",
        lambda data: len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12] == b"WEBP",
    ),
}


def _decode_image(block: dict, index: int) -> tuple[bytes, str]:
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        raise ValueError(f"image block {index}: source.type must be base64")
    media_type = source.get("media_type")
    if media_type not in _MEDIA:
        raise ValueError(f"image block {index}: unsupported media_type")
    encoded = source.get("data")
    if not isinstance(encoded, str):
        raise ValueError(f"image block {index}: data must be a base64 string")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"image block {index}: invalid base64") from exc
    if not decoded or len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError(f"image block {index}: decoded size is out of range")
    suffix, matches_magic = _MEDIA[media_type]
    if not matches_magic(decoded):
        raise ValueError(f"image block {index}: content does not match media_type")
    return decoded, suffix


def expand_content_blocks(content: object, output_dir: Path) -> list[Path]:
    """Validate blocks and write ordered prompts/images into output_dir."""
    if not isinstance(content, list):
        raise ValueError("--content-json must be a JSON array")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("content output directory must be empty")
    output_dir.chmod(0o700)
    codex_parts: list[str] = []
    agy_parts: list[str] = []
    image_paths: list[Path] = []
    text_bytes = 0
    total_image_bytes = 0

    try:
        for block_index, block in enumerate(content, start=1):
            if not isinstance(block, dict):
                raise ValueError(f"content block {block_index}: must be an object")
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ValueError(f"text block {block_index}: text must be a string")
                text_bytes += len(text.encode("utf-8"))
                if text_bytes > MAX_TEXT_BYTES:
                    raise ValueError("content text exceeds 32 KiB")
                codex_parts.append(text)
                agy_parts.append(text)
                continue
            if block_type != "image":
                raise ValueError(f"content block {block_index}: unsupported type")
            if len(image_paths) >= MAX_IMAGES:
                raise ValueError("content contains more than 8 images")
            decoded, suffix = _decode_image(block, block_index)
            total_image_bytes += len(decoded)
            if total_image_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise ValueError("decoded image total exceeds 32 MiB")
            image_number = len(image_paths) + 1
            image_path = output_dir / f"image-{image_number:03d}{suffix}"
            image_path.write_bytes(decoded)
            image_paths.append(image_path)
            codex_parts.append(
                f"【画像{image_number}】直前の説明に対応する添付画像{image_number}です。"
            )
            agy_parts.append(
                f"【画像{image_number}】直前の説明に対応する画像です。\n"
                f"view_fileで直接読み込んでください。commandやスクリプトで解析しないでください。\n"
                f"@{image_path}"
            )

        (output_dir / "codex-prompt.txt").write_text(
            "\n\n".join(codex_parts), encoding="utf-8"
        )
        (output_dir / "agy-prompt.txt").write_text(
            "\n\n".join(agy_parts), encoding="utf-8"
        )
        return image_paths
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def cleanup_stale(root: Path, *, now: float | None = None) -> int:
    """Remove expired eha-content-* directories without following symlinks."""
    now = time.time() if now is None else now
    removed = 0
    if not root.is_dir():
        return removed
    for path in root.glob("eha-content-*"):
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            if now - path.stat().st_mtime < STALE_SECONDS:
                continue
            shutil.rmtree(path)
            removed += 1
        except OSError:
            continue
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?")
    parser.add_argument("output_dir", nargs="?")
    parser.add_argument("--cleanup-stale")
    args = parser.parse_args()

    if args.cleanup_stale:
        cleanup_stale(Path(args.cleanup_stale))
        return 0
    if not args.input or not args.output_dir:
        parser.error("input and output_dir are required")

    if args.input == "-":
        encoded = os.read(0, MAX_INPUT_JSON_BYTES + 1)
    else:
        input_path = Path(args.input)
        if input_path.stat().st_size > MAX_INPUT_JSON_BYTES:
            raise ValueError("content JSON exceeds 48 MiB")
        encoded = input_path.read_bytes()
    if len(encoded) > MAX_INPUT_JSON_BYTES:
        raise ValueError("content JSON exceeds 48 MiB")
    raw = encoded.decode("utf-8")
    content = json.loads(raw)
    expand_content_blocks(content, Path(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
