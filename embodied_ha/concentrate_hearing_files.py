"""Lifecycle management for temporary concentrate_hearing WebM files."""

from __future__ import annotations

import time
from pathlib import Path

CONCENTRATE_HEARING_DIR = Path("/tmp/embodied-ha/audio/concentrate_hearing")
CONCENTRATE_HEARING_FILE_TTL_SECONDS = 15 * 60
CONCENTRATE_HEARING_CLEANUP_INTERVAL_SECONDS = 30
_FILE_PATTERN = "eha-concentrate-hearing-*.webm"


def prune_stale_files(
    current_epoch: float | None = None,
    *,
    directory: Path | None = None,
    ttl_seconds: int = CONCENTRATE_HEARING_FILE_TTL_SECONDS,
) -> int:
    """Remove expired tool files and return the number removed."""
    target_dir = directory or CONCENTRATE_HEARING_DIR
    cutoff = (time.time() if current_epoch is None else current_epoch) - ttl_seconds
    try:
        candidates = tuple(target_dir.glob(_FILE_PATTERN))
    except OSError:
        return 0

    removed = 0
    for path in candidates:
        try:
            if path.lstat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def cleanup_forever(
    *,
    sleep_fn=time.sleep,
    interval_seconds: int = CONCENTRATE_HEARING_CLEANUP_INTERVAL_SECONDS,
) -> None:
    """Prune immediately and periodically without depending on another tool call."""
    while True:
        prune_stale_files()
        sleep_fn(interval_seconds)
