"""Short-lived, opt-in camera frame history for Embodied HA.

The history is deliberately stored outside persistent ``/config`` data.  It is
a sensory buffer, not an archive: add-on restarts may erase it, and disabling
the feature removes it.  Callers cannot choose a source or file path across the
MCP boundary; they can only inspect the camera currently occupied by the body.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_capture import fetch_frame

DEFAULT_HISTORY_ROOT = "/tmp/embodied-ha-camera-history"
DEFAULT_RETENTION_MINUTES = 10
MIN_RETENTION_MINUTES = 1
MAX_RETENTION_MINUTES = 60
CAPTURE_INTERVAL_SECONDS = 10.0
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FRAMES_PER_CAMERA = math.ceil(
    MAX_RETENTION_MINUTES * 60 / CAPTURE_INTERVAL_SECONDS
) + 2
MAX_RETURN_FRAMES = 3
MAX_STORED_FRAME_BYTES = 16 * 1024 * 1024
HISTORY_FETCH_TIMEOUT_SECONDS = 4
HISTORY_MAX_FETCH_ATTEMPTS = 2
HISTORY_RETRY_DELAY_SECONDS = 0.25
MAX_CAPTURE_WORKERS = 4
HISTORY_RETRY_MAX_SOURCES = MAX_CAPTURE_WORKERS
FAILURE_LOG_EVERY = 30
_CAMERA_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
_FRAME_NAME_RE = re.compile(r"^(\d{1,20})\.jpg$")


@dataclass(frozen=True)
class FrameRecord:
    path: Path
    captured_at: float
    size: int


@dataclass(frozen=True)
class CaptureResult:
    status: str
    attempts: int
    captured_at: float | None = None


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def history_settings(prefs: object) -> tuple[bool, int]:
    """Return the opt-in flag and a bounded retention duration."""

    prefs = prefs if isinstance(prefs, dict) else {}
    enabled = prefs.get("camera_history_enabled") is True
    try:
        minutes = int(prefs.get("camera_history_minutes", DEFAULT_RETENTION_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_RETENTION_MINUTES
    minutes = max(MIN_RETENTION_MINUTES, min(MAX_RETENTION_MINUTES, minutes))
    return enabled, minutes


def camera_sources(prefs: object) -> list[str]:
    """Resolve configured HA camera entities and go2rtc sources in order."""

    if not isinstance(prefs, dict) or not isinstance(prefs.get("cameras"), list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in prefs["cameras"]:
        if not isinstance(item, dict):
            continue
        source = (
            _clean(item.get("ha_entity"))
            or _clean(item.get("source"))
            or _clean(item.get("entity"))
        )
        if not source or source in seen:
            continue
        seen.add(source)
        result.append(source)
    return result


def source_key(source: str) -> str:
    """Return a path-safe stable identifier without leaking camera names."""

    return hashlib.sha256(_clean(source).encode("utf-8")).hexdigest()[:16]


def camera_directory(history_root: str | os.PathLike[str], source: str) -> Path:
    return Path(history_root) / source_key(source)


def _looks_like_jpeg(frame: object) -> bool:
    return (
        isinstance(frame, (bytes, bytearray))
        and 4 <= len(frame) <= MAX_STORED_FRAME_BYTES
        and bytes(frame[:2]) == b"\xff\xd8"
        and bytes(frame[-2:]) == b"\xff\xd9"
    )


def store_frame(
    history_root: str | os.PathLike[str],
    source: str,
    frame: bytes,
    *,
    captured_at: float | None = None,
) -> FrameRecord | None:
    """Atomically store one JPEG under a generated camera directory."""

    source = _clean(source)
    if not source or not _looks_like_jpeg(frame):
        return None
    captured_at = time.time() if captured_at is None else float(captured_at)
    timestamp_ms = max(0, round(captured_at * 1000))
    root = Path(history_root)
    if root.is_symlink():
        return None
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
    except OSError:
        return None
    camera_dir = camera_directory(root, source)
    if camera_dir.is_symlink():
        return None
    try:
        camera_dir.mkdir(exist_ok=True, mode=0o700)
    except OSError:
        return None
    if camera_dir.is_symlink() or not camera_dir.is_dir():
        return None
    try:
        os.chmod(camera_dir, 0o700)
    except OSError:
        pass

    target = camera_dir / f"{timestamp_ms}.jpg"
    temp_path = camera_dir / f".{timestamp_ms}.{uuid.uuid4().hex}.tmp"
    fd = None
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(bytes(frame))
            fh.flush()
        os.replace(temp_path, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except OSError:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        return None
    return FrameRecord(target, timestamp_ms / 1000.0, len(frame))


def _record_from_path(path: Path) -> FrameRecord | None:
    if path.is_symlink() or not path.is_file():
        return None
    match = _FRAME_NAME_RE.fullmatch(path.name)
    if not match:
        return None
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        return None
    return FrameRecord(path, int(match.group(1)) / 1000.0, info.st_size)


def list_frames(history_root: str | os.PathLike[str], source: str) -> list[FrameRecord]:
    camera_dir = camera_directory(history_root, source)
    if camera_dir.is_symlink() or not camera_dir.is_dir():
        return []
    records = []
    try:
        paths = list(camera_dir.iterdir())
    except OSError:
        return []
    for path in paths:
        record = _record_from_path(path)
        if record is not None:
            records.append(record)
    return sorted(records, key=lambda item: (item.captured_at, item.path.name))


def _all_frame_records(history_root: Path) -> list[FrameRecord]:
    if history_root.is_symlink() or not history_root.is_dir():
        return []
    records = []
    try:
        directories = list(history_root.iterdir())
    except OSError:
        return []
    for directory in directories:
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not _CAMERA_KEY_RE.fullmatch(directory.name)
        ):
            continue
        try:
            paths = list(directory.iterdir())
        except OSError:
            continue
        for path in paths:
            record = _record_from_path(path)
            if record is not None:
                records.append(record)
    return records


def _unlink_record(record: FrameRecord) -> bool:
    try:
        record.path.unlink()
        return True
    except OSError:
        return False


def _remove_empty_camera_dirs(history_root: Path) -> None:
    if not history_root.is_dir() or history_root.is_symlink():
        return
    try:
        directories = list(history_root.iterdir())
    except OSError:
        return
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        history_root.rmdir()
    except OSError:
        pass


def prune_history(
    history_root: str | os.PathLike[str],
    *,
    retention_minutes: int,
    now: float | None = None,
    max_frames_per_camera: int = MAX_FRAMES_PER_CAMERA,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> int:
    """Enforce time, per-camera count, and global byte bounds."""

    root = Path(history_root)
    now = time.time() if now is None else float(now)
    retention = max(
        MIN_RETENTION_MINUTES,
        min(MAX_RETENTION_MINUTES, int(retention_minutes)),
    )
    cutoff = now - retention * 60
    removed = 0

    records = _all_frame_records(root)
    for record in records:
        # Future timestamps can be left by a wall-clock correction.  Removing
        # them keeps relative-time queries truthful and space bounded.
        if record.captured_at < cutoff or record.captured_at > now + 1:
            removed += int(_unlink_record(record))

    records = _all_frame_records(root)
    by_camera: dict[Path, list[FrameRecord]] = {}
    for record in records:
        by_camera.setdefault(record.path.parent, []).append(record)
    keep_per_camera = max(1, int(max_frames_per_camera))
    for camera_records in by_camera.values():
        camera_records.sort(key=lambda item: (item.captured_at, item.path.name))
        for record in camera_records[:-keep_per_camera]:
            removed += int(_unlink_record(record))

    records = sorted(
        _all_frame_records(root), key=lambda item: (item.captured_at, item.path.name)
    )
    total_bytes = sum(record.size for record in records)
    byte_limit = max(0, int(max_total_bytes))
    for record in records:
        if total_bytes <= byte_limit:
            break
        if _unlink_record(record):
            total_bytes -= record.size
            removed += 1

    _remove_empty_camera_dirs(root)
    return removed


def clear_history(history_root: str | os.PathLike[str]) -> int:
    """Remove only the configured history root, never a broad system path."""

    root = Path(history_root)
    if not root.exists() and not root.is_symlink():
        return 0
    resolved = root.resolve(strict=False)
    forbidden = {Path("/"), Path("/tmp"), Path("/config"), Path("/data")}
    if resolved in forbidden or len(resolved.parts) < 3:
        raise ValueError(f"unsafe camera history root: {root}")
    count = len(_all_frame_records(root))
    if root.is_symlink():
        root.unlink()
    else:
        shutil.rmtree(root)
    return count


def select_frames(
    history_root: str | os.PathLike[str],
    source: str,
    *,
    start_seconds_ago: int = 0,
    end_seconds_ago: int | None = None,
    max_frames: int = 1,
    retention_minutes: int = DEFAULT_RETENTION_MINUTES,
    now: float | None = None,
) -> list[FrameRecord]:
    """Select nearest frames across an older-to-newer relative time range."""

    now = time.time() if now is None else float(now)
    start = max(0, int(start_seconds_ago))
    end = start if end_seconds_ago is None else max(0, int(end_seconds_ago))
    if start < end:
        raise ValueError("start_seconds_ago must be greater than or equal to end_seconds_ago")
    retention = max(
        MIN_RETENTION_MINUTES,
        min(MAX_RETENTION_MINUTES, int(retention_minutes)),
    )
    if start > retention * 60:
        raise ValueError("requested camera history is older than the configured retention")

    lower_bound = now - retention * 60
    records = [
        record
        for record in list_frames(history_root, source)
        if lower_bound <= record.captured_at <= now + 1
    ]
    if not records:
        return []

    count = max(1, min(MAX_RETURN_FRAMES, int(max_frames)))
    older_target = now - start
    newer_target = now - end
    if count == 1 or older_target == newer_target:
        targets = [older_target]
    else:
        step = (newer_target - older_target) / (count - 1)
        targets = [older_target + step * index for index in range(count)]

    selected: list[FrameRecord] = []
    seen: set[Path] = set()
    for target in targets:
        nearest = min(records, key=lambda item: abs(item.captured_at - target))
        if nearest.path not in seen:
            seen.add(nearest.path)
            selected.append(nearest)
    return selected


def read_frame(record: FrameRecord) -> bytes:
    """Read a selected regular file without following a replaced symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(record.path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STORED_FRAME_BYTES:
            raise OSError("camera history frame is not a safe regular file")
        chunks = []
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if not _looks_like_jpeg(data):
        raise OSError("camera history frame is not a valid JPEG")
    return data


def load_preferences(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


class CameraHistoryWorker:
    """Poll preferences and maintain the opt-in short-term frame buffer."""

    def __init__(
        self,
        *,
        prefs_file: str,
        history_root: str = DEFAULT_HISTORY_ROOT,
        ha_url: str,
        go2rtc_url: str,
        token: str,
        fetch: Callable[..., bytes | None] = fetch_frame,
        capture_interval: float = CAPTURE_INTERVAL_SECONDS,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        fetch_timeout: int = HISTORY_FETCH_TIMEOUT_SECONDS,
        retry_delay: float = HISTORY_RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.prefs_file = prefs_file
        self.history_root = history_root
        self.ha_url = ha_url
        self.go2rtc_url = go2rtc_url
        self.token = token
        self.fetch = fetch
        self.capture_interval = max(1.0, float(capture_interval))
        self.max_total_bytes = max(0, int(max_total_bytes))
        self.fetch_timeout = max(1, int(fetch_timeout))
        self.retry_delay = max(0.0, float(retry_delay))
        self.sleep = sleep
        self._last_enabled: bool | None = None
        self._last_failures: tuple[str, ...] = ()
        self._tracked_sources: tuple[str, ...] | None = None
        self._failure_states: dict[str, tuple[str, int]] = {}
        self._pending_reset_reason: str | None = "worker_start"

    def _reset_failure_tracking(self, reason: str) -> None:
        if self._failure_states or self._tracked_sources is not None:
            self._pending_reset_reason = reason
        self._failure_states = {}
        self._tracked_sources = None
        self._last_failures = ()

    def _capture_source(
        self,
        source: str,
        captured_at: float | None,
        *,
        allow_retry: bool,
    ) -> CaptureResult:
        attempts_allowed = HISTORY_MAX_FETCH_ATTEMPTS if allow_retry else 1
        frame: bytes | None = None
        attempt = 0
        for attempt in range(1, attempts_allowed + 1):
            try:
                frame = self.fetch(
                    source,
                    ha_url=self.ha_url,
                    go2rtc_url=self.go2rtc_url,
                    token=self.token,
                    timeout_seconds=self.fetch_timeout,
                )
            except Exception:
                return CaptureResult("fetch_exception", attempt)
            if frame:
                break
            if attempt < attempts_allowed and self.retry_delay:
                self.sleep(self.retry_delay)
        if not frame:
            return CaptureResult("fetch_unavailable", attempts_allowed)
        if not _looks_like_jpeg(frame):
            return CaptureResult("invalid_frame", attempt)

        # captured_at is retrieval completion time in production. Tests may pass
        # a deterministic cycle time explicitly.
        completed_at = time.time() if captured_at is None else captured_at
        record = store_frame(
            self.history_root,
            source,
            frame,
            captured_at=completed_at,
        )
        if record is None:
            return CaptureResult("store_failed", attempt)
        return CaptureResult("captured", attempt, record.captured_at)

    def _update_failure_tracking(
        self,
        sources: list[str],
        capture_results: dict[str, CaptureResult],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        source_tuple = tuple(sources)
        if self._tracked_sources is not None and self._tracked_sources != source_tuple:
            self._reset_failure_tracking("source_set_changed")
        self._tracked_sources = source_tuple

        failures: list[dict[str, Any]] = []
        recoveries: list[dict[str, Any]] = []
        for index, source in enumerate(sources, start=1):
            capture = capture_results[source]
            previous = self._failure_states.get(source)
            if capture.status == "captured":
                if previous:
                    recoveries.append(
                        {
                            "source_index": index,
                            "reason": previous[0],
                            "previous_consecutive": previous[1],
                        }
                    )
                    self._failure_states.pop(source, None)
                continue

            consecutive = previous[1] + 1 if previous and previous[0] == capture.status else 1
            self._failure_states[source] = (capture.status, consecutive)
            failures.append(
                {
                    "source_index": index,
                    "reason": capture.status,
                    "consecutive": consecutive,
                    "attempts": capture.attempts,
                }
            )
        return failures, recoveries

    @staticmethod
    def _should_log_failure(consecutive: int) -> bool:
        return consecutive <= 6 or consecutive % FAILURE_LOG_EVERY == 0

    def _log_cycle_events(self, result: dict[str, Any]) -> None:
        reset_reason = result.get("tracking_reset")
        if reset_reason:
            print(
                f"[camera-history] failure tracking reset reason={reset_reason}",
                flush=True,
            )
        for event in result.get("failure_events", []):
            if not self._should_log_failure(int(event["consecutive"])):
                continue
            print(
                "[camera-history] capture failed "
                f"source_index={event['source_index']} reason={event['reason']} "
                f"consecutive={event['consecutive']} attempts={event['attempts']}",
                flush=True,
            )
        for event in result.get("recovery_events", []):
            print(
                "[camera-history] capture recovered "
                f"source_index={event['source_index']} reason={event['reason']} "
                f"previous_consecutive={event['previous_consecutive']}",
                flush=True,
            )

    def _result_with_tracking_reset(self, result: dict[str, Any]) -> dict[str, Any]:
        result["tracking_reset"] = self._pending_reset_reason
        self._pending_reset_reason = None
        return result

    def run_cycle(self, *, now: float | None = None) -> dict[str, Any]:
        deterministic_time = now is not None
        now = time.time() if now is None else float(now)
        prefs = load_preferences(self.prefs_file)
        if prefs is None:
            try:
                removed = clear_history(self.history_root)
            except (OSError, ValueError):
                removed = 0
            self._last_enabled = False
            self._reset_failure_tracking("preferences_unavailable")
            return self._result_with_tracking_reset(
                {
                    "status": "preferences_unavailable",
                    "enabled": False,
                    "captured": 0,
                    "failed": 0,
                    "removed": removed,
                }
            )

        enabled, retention_minutes = history_settings(prefs)
        self._last_enabled = enabled
        if not enabled:
            try:
                removed = clear_history(self.history_root)
            except (OSError, ValueError):
                removed = 0
            self._reset_failure_tracking("disabled")
            return self._result_with_tracking_reset(
                {
                    "status": "disabled",
                    "enabled": False,
                    "captured": 0,
                    "failed": 0,
                    "removed": removed,
                }
            )

        sources = camera_sources(prefs)
        captured = 0
        failures: list[str] = []
        capture_results: dict[str, CaptureResult] = {}
        if sources:
            worker_count = min(MAX_CAPTURE_WORKERS, len(sources))
            allow_retry = len(sources) <= HISTORY_RETRY_MAX_SOURCES
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(
                        self._capture_source,
                        source,
                        now if deterministic_time else None,
                        allow_retry=allow_retry,
                    ): source
                    for source in sources
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        capture = future.result()
                    except Exception:
                        capture = CaptureResult("worker_exception", 0)
                    capture_results[source] = capture
                    if capture.status == "captured":
                        captured += 1
                    else:
                        failures.append(source)

        failure_events, recovery_events = self._update_failure_tracking(
            sources,
            capture_results,
        )
        prune_now = now if deterministic_time else time.time()
        removed = prune_history(
            self.history_root,
            retention_minutes=retention_minutes,
            now=prune_now,
            max_total_bytes=self.max_total_bytes,
        )
        self._last_failures = tuple(sorted(failures))
        return self._result_with_tracking_reset(
            {
                "status": "enabled",
                "enabled": True,
                "retention_minutes": retention_minutes,
                "sources": len(sources),
                "captured": captured,
                "failed": len(failures),
                "removed": removed,
                "failure_events": failure_events,
                "recovery_events": recovery_events,
            }
        )

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        last_status = None
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                result = self.run_cycle()
            except Exception as exc:
                print(f"[camera-history] cycle failed: {exc}", flush=True)
                result = {"status": "cycle_failed"}
            status = result.get("status")
            if status != last_status:
                print(f"[camera-history] status={status}", flush=True)
                last_status = status
            self._log_cycle_events(result)
            elapsed = time.monotonic() - started
            stop_event.wait(max(1.0, self.capture_interval - elapsed))


def run_from_environment() -> None:
    history_root = os.environ.get("EHA_CAMERA_HISTORY_DIR", DEFAULT_HISTORY_ROOT)
    try:
        clear_history(history_root)
    except (OSError, ValueError) as exc:
        print(f"[camera-history] startup cleanup failed: {exc}", flush=True)
    worker = CameraHistoryWorker(
        prefs_file=os.environ.get("EHA_PREFS_FILE", "preferences.json"),
        history_root=history_root,
        ha_url=os.environ.get("HA_URL", "http://supervisor/core/api"),
        go2rtc_url=os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984"),
        token=os.environ.get("SUPERVISOR_TOKEN", ""),
    )
    worker.run_forever()


if __name__ == "__main__":
    run_from_environment()
