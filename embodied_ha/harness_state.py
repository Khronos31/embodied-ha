"""Persistent selection of the CLI harness used by this add-on instance."""
from __future__ import annotations

import os
import tempfile

VALID_HARNESSES = ("claude", "codex", "agy")
_FLAG_FILE_ENV = "EHA_HARNESS_FLAG_FILE"
_DEFAULT_FLAG_FILE = "/data/selected_harness"


def flag_path() -> str:
    """Return the selected-harness flag path, resolving the environment each call."""
    return os.environ.get(_FLAG_FILE_ENV, _DEFAULT_FLAG_FILE)


def get_selected_harness() -> str | None:
    """Read and validate the selected harness without caching its value."""
    state, harness = read_selection()
    return harness if state == "valid" else None


def read_selection() -> tuple[str, str | None]:
    """Read the selection, distinguishing an absent flag from a bad one.

    ``missing`` is reserved for instances created before this flag existed.
    A present but empty, invalid, or unreadable file is ``invalid`` so callers
    can fail closed rather than treating corruption as a legacy installation.
    """
    try:
        with open(flag_path(), encoding="utf-8") as f:
            harness = f.read().strip()
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeError):
        return "invalid", None
    if harness in VALID_HARNESSES:
        return "valid", harness
    return "invalid", None


def set_selected_harness(harness: str) -> None:
    """Atomically persist a validated harness selection."""
    if harness not in VALID_HARNESSES:
        raise ValueError(f"Invalid harness: {harness!r}")
    path = flag_path()
    parent = os.path.dirname(path) or os.curdir
    os.makedirs(parent, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".selected_harness-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{harness}\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise
