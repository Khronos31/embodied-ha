"""Process-safe transactions for ``preferences.json`` updates."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from typing import Any

from state_utils import file_lock


class PreferencesReadError(RuntimeError):
    pass


def load_for_update(path: str) -> dict[str, Any]:
    """Load an existing object; only a missing file is treated as empty."""
    if not path:
        raise PreferencesReadError("preferences.json のパスが空です")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreferencesReadError(
            "既存の preferences.json を読めないため更新を中止しました"
        ) from exc
    if not isinstance(data, dict):
        raise PreferencesReadError(
            "既存の preferences.json がJSONオブジェクトではないため更新を中止しました"
        )
    return data


def _write_atomic(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    existed = os.path.exists(path)
    mode = os.stat(path).st_mode & 0o777 if existed else 0o600
    fd, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def update(
    path: str,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any]:
    """Run one complete read-modify-write transaction under an OS lock.

    Returning ``None`` from ``mutator`` skips the write.
    """
    if not path:
        raise PreferencesReadError("preferences.json のパスが空です")
    with file_lock(path):
        current = load_for_update(path)
        updated = mutator(current)
        if updated is None:
            return current
        if not isinstance(updated, dict):
            raise TypeError("preferences updater must return a dict or None")
        _write_atomic(path, updated)
        return updated
