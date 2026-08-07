#!/usr/bin/env python3
"""Launch one MCP server with credentials loaded outside model-readable config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"mcp-env-launcher.py: {message}")


def _load_server_env(path: Path, server_name: str) -> dict[str, str]:
    try:
        mode = path.stat().st_mode & 0o777
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"cannot read credential file: {type(exc).__name__}")
    if mode & 0o077:
        _fail("credential file must not be group/world accessible")
    servers = value.get("servers") if isinstance(value, dict) else None
    server_env = servers.get(server_name) if isinstance(servers, dict) else None
    if not isinstance(server_env, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in server_env.items()
    ):
        _fail(f"credential entry is missing or invalid for server {server_name}")
    return server_env


def main() -> None:
    if len(sys.argv) < 4:
        _fail("usage: mcp-env-launcher.py CREDENTIAL_FILE SERVER COMMAND [ARG ...]")
    credential_path = Path(sys.argv[1])
    server_name = sys.argv[2]
    command = sys.argv[3]
    command_args = sys.argv[4:]
    child_env = dict(os.environ)
    child_env.update(_load_server_env(credential_path, server_name))
    os.execvpe(command, [command, *command_args], child_env)


if __name__ == "__main__":
    main()
