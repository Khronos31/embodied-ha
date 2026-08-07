#!/usr/bin/env python3
"""Build a secret-free MCP tool manifest for Antigravity prompts.

Antigravity's headless tool adapter does not reliably expose JSON Schema to the
model before the first call.  Reading ``.agents/mcp_config.json`` is not an
acceptable fallback because server environment blocks can contain credentials.
This helper asks each configured first-party server for ``tools/list`` and
writes only tool names, descriptions, and input schemas.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"mcp-schema-manifest.py: {message}")


def _load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"cannot read MCP config: {type(exc).__name__}")
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers"), dict):
        _fail("MCP config must contain an mcpServers object")
    return value


def _list_tools(server_name: str, server: dict[str, Any]) -> list[dict[str, Any]]:
    command = server.get("command")
    args = server.get("args", [])
    env = server.get("env", {})
    if not isinstance(command, str) or not command:
        _fail(f"server {server_name} has no command")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        _fail(f"server {server_name} args must be strings")
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        _fail(f"server {server_name} env must contain strings")

    request = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        ensure_ascii=False,
    ) + "\n"
    try:
        proc = subprocess.run(
            [command, *args],
            input=request,
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail(f"server {server_name} tools/list failed: {type(exc).__name__}")
    if proc.returncode != 0:
        _fail(f"server {server_name} tools/list exited with {proc.returncode}")

    response = None
    for line in proc.stdout.splitlines():
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("id") == 1:
            response = candidate
            break
    tools = (response or {}).get("result", {}).get("tools")
    if not isinstance(tools, list):
        _fail(f"server {server_name} returned no tools/list result")
    return [tool for tool in tools if isinstance(tool, dict)]


def _build_manifest(config: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for server_name, raw_server in config["mcpServers"].items():
        if not isinstance(server_name, str) or not isinstance(raw_server, dict):
            _fail("MCP server entries must be objects")
        include = raw_server.get("includeTools")
        if include is not None and (
            not isinstance(include, list)
            or not all(isinstance(item, str) for item in include)
        ):
            _fail(f"server {server_name} includeTools must be strings")
        included = set(include) if include is not None else None
        found = set()
        for tool in _list_tools(server_name, raw_server):
            name = tool.get("name")
            if not isinstance(name, str) or (included is not None and name not in included):
                continue
            description = tool.get("description", "")
            schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            if not isinstance(description, str) or not isinstance(schema, dict):
                _fail(f"server {server_name} tool {name} has an invalid schema")
            entries.append(
                {
                    "server": server_name,
                    "name": name,
                    "description": description,
                    "inputSchema": schema,
                }
            )
            found.add(name)
        if included is not None and found != included:
            missing = ", ".join(sorted(included - found))
            _fail(f"server {server_name} tools/list omitted allowed tools: {missing}")
    return {"version": 1, "tools": entries}


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main() -> None:
    if len(sys.argv) != 3:
        _fail("usage: mcp-schema-manifest.py MCP_CONFIG OUTPUT")
    config_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    _write_atomic(output_path, _build_manifest(_load_config(config_path)))


if __name__ == "__main__":
    main()
