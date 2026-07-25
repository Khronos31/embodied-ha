"""Runtime PATH construction shared by agent subprocess callers."""

import os
from collections.abc import Mapping


DEFAULT_TOOLS_PATH = "/usr/local/bin"
DEFAULT_SYSTEM_PATH = "/usr/bin:/bin"


def build_tools_path(environ: Mapping[str, str] | None = None) -> str:
    """Prepend EHA_TOOLS_PATH while preserving order and removing duplicates."""
    env = environ if environ is not None else os.environ
    parts = (
        env.get("EHA_TOOLS_PATH", DEFAULT_TOOLS_PATH).split(":")
        + env.get("PATH", DEFAULT_SYSTEM_PATH).split(":")
    )
    return ":".join(dict.fromkeys(parts))
