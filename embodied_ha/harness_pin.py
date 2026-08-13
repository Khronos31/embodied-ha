"""Persistent record of which harness CLI build this instance actually runs.

The add-on deliberately does not ship harness binaries (design contract 0.7):
the operator downloads them from the vendor at runtime. That makes "which build
is installed" a fact only this instance knows, and until now nobody wrote it
down — ``antigravity_setup.install()`` delegates to the vendor script and
returns no version, yet the CLI is frozen against background updates right
afterwards, so the instance was pinned to a build no record named (F-80).

This module is that record. It is persistence-only and dependency-free, in the
same spirit as ``harness_state.py``: no network, no subprocess, no policy. The
update transaction in ``harness_binary_update.py`` decides *what* to install and
calls in here to say what happened.

Retained builds are listed here rather than inferred from the filesystem so that
"an old binary exists" never again doubles as a signal that something crashed
mid-update (the ``.eha-f157-pre-update.bak`` convention conflated the two).
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import tempfile

SCHEMA_VERSION = 1
VALID_HARNESSES = ("claude", "codex", "agy")

_PIN_FILE_ENV = "EHA_HARNESS_PIN_FILE"
_DEFAULT_PIN_FILE = "/data/harness_pin.json"

# A pin record grows an entry per retained build; 64 KiB is far above any real
# history and keeps a corrupted file from being read into memory unbounded.
_MAX_PIN_BYTES = 65_536


def pin_path() -> str:
    """Return the pin record path, resolving the environment each call."""
    return os.environ.get(_PIN_FILE_ENV, _DEFAULT_PIN_FILE)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _empty_record() -> dict:
    return {"schema_version": SCHEMA_VERSION, "harnesses": {}}


def read_record() -> tuple[str, dict]:
    """Read the whole pin record, distinguishing absent from corrupt.

    Returns ``(state, record)`` where ``state`` is one of:

    - ``"missing"`` — no file yet (an instance that predates this record, or one
      whose harness was installed before the pin was written).
    - ``"valid"`` — parsed and shaped as expected.
    - ``"invalid"`` — present but unreadable, oversized, not JSON, or the wrong
      shape. Callers must fail closed rather than treat corruption as "missing",
      because "missing" legitimately means "install whatever you like" while
      corruption means "we no longer know what is installed".

    ``record`` is an empty record for the non-valid states so callers can read
    fields without branching first.
    """
    path = pin_path()
    try:
        with open(path, "rb") as handle:
            payload = handle.read(_MAX_PIN_BYTES + 1)
    except FileNotFoundError:
        return "missing", _empty_record()
    except OSError:
        return "invalid", _empty_record()
    if len(payload) > _MAX_PIN_BYTES:
        return "invalid", _empty_record()
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", _empty_record()
    if not isinstance(record, dict):
        return "invalid", _empty_record()
    if record.get("schema_version") != SCHEMA_VERSION:
        return "invalid", _empty_record()
    harnesses = record.get("harnesses")
    if not isinstance(harnesses, dict):
        return "invalid", _empty_record()
    for name, entry in harnesses.items():
        if name not in VALID_HARNESSES or not isinstance(entry, dict):
            return "invalid", _empty_record()
    return "valid", record


def read_pin(harness: str) -> dict | None:
    """Return the recorded build for ``harness``, or ``None`` when unknown.

    ``None`` covers both "no record" and "record exists but not for this
    harness"; neither lets a caller claim to know the installed build.
    """
    if harness not in VALID_HARNESSES:
        raise ValueError(f"Invalid harness: {harness!r}")
    state, record = read_record()
    if state != "valid":
        return None
    entry = record["harnesses"].get(harness)
    return dict(entry) if isinstance(entry, dict) else None


def retained_builds(harness: str) -> list[dict]:
    """Return the retained older builds for ``harness``, oldest entry first."""
    pin = read_pin(harness)
    if not pin:
        return []
    retained = pin.get("retained")
    return [dict(item) for item in retained if isinstance(item, dict)] if isinstance(retained, list) else []


def _atomic_write(path: str, record: dict) -> None:
    parent = os.path.dirname(path) or os.curdir
    os.makedirs(parent, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".harness_pin-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def _locked_update(mutate) -> dict:
    """Serialise read→mutate→write across processes and return the new entry.

    The Web UI worker thread and the start-up migration can both reach this, and
    a lost update here would leave the record describing a build that is not on
    disk. A corrupt record is rebuilt rather than merged into: merging would
    silently keep whatever fragments survived, which is exactly the "nobody
    knows what is installed" state this module exists to end.
    """
    path = pin_path()
    parent = os.path.dirname(path) or os.curdir
    os.makedirs(parent, exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            state, record = read_record()
            if state == "invalid":
                record = _empty_record()
            entry = mutate(record)
            _atomic_write(path, record)
            return entry
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def record_install(
    harness: str,
    version: str,
    *,
    url: str | None = None,
    binary_sha512: str | None = None,
    source: str = "install",
) -> dict:
    """Record ``version`` as the build now installed for ``harness``.

    ``source`` distinguishes a first install from an update or a rollback; it is
    descriptive only, so a future source value cannot invalidate an old record.
    Retained builds are preserved untouched — installing does not discard the
    ability to go back.
    """
    if harness not in VALID_HARNESSES:
        raise ValueError(f"Invalid harness: {harness!r}")
    if not version or not version.strip():
        raise ValueError("version must be a non-empty string")

    def mutate(record: dict) -> dict:
        previous = record["harnesses"].get(harness)
        retained = previous.get("retained") if isinstance(previous, dict) else None
        entry = {
            "version": version.strip(),
            "url": url,
            "binary_sha512": binary_sha512,
            "installed_at": _utc_now(),
            "source": source,
            "retained": retained if isinstance(retained, list) else [],
        }
        record["harnesses"][harness] = entry
        return entry

    return _locked_update(mutate)


def add_retained(
    harness: str,
    version: str,
    path: str,
    *,
    binary_sha512: str | None = None,
    url: str | None = None,
) -> dict:
    """Record that ``version`` is kept at ``path`` and can be rolled back to.

    Re-retaining a version already listed replaces its entry rather than
    appending a duplicate, so repeated update/rollback cycles cannot grow the
    record without bound.
    """
    if harness not in VALID_HARNESSES:
        raise ValueError(f"Invalid harness: {harness!r}")
    if not version or not version.strip():
        raise ValueError("version must be a non-empty string")
    if not path:
        raise ValueError("path must be a non-empty string")

    def mutate(record: dict) -> dict:
        entry = record["harnesses"].setdefault(harness, {"retained": []})
        retained = entry.get("retained")
        if not isinstance(retained, list):
            retained = []
        retained = [item for item in retained if not _is_version(item, version)]
        retained.append(
            {
                "version": version.strip(),
                "path": path,
                "binary_sha512": binary_sha512,
                "url": url,
                "retained_at": _utc_now(),
            }
        )
        entry["retained"] = retained
        return entry

    return _locked_update(mutate)


def drop_retained(harness: str, version: str) -> dict:
    """Forget the retained ``version`` for ``harness`` (file removal is the caller's)."""
    if harness not in VALID_HARNESSES:
        raise ValueError(f"Invalid harness: {harness!r}")

    def mutate(record: dict) -> dict:
        entry = record["harnesses"].get(harness)
        if not isinstance(entry, dict):
            return {}
        retained = entry.get("retained")
        if isinstance(retained, list):
            entry["retained"] = [item for item in retained if not _is_version(item, version)]
        return entry

    return _locked_update(mutate)


def _is_version(item: object, version: str) -> bool:
    return isinstance(item, dict) and item.get("version") == version.strip()
