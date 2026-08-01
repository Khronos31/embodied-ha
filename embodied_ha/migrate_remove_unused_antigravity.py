"""Remove or quarantine legacy Antigravity fallback assets."""

from __future__ import annotations

import json
import os
import sys
import tempfile

import agy_update_freeze
import antigravity_setup
import harness_state

_LEGACY_GLOBAL_MCP_SERVERS = {
    "audio": "/app/audio-mcp.py",
    "memory": "/app/memory-mcp.py",
    "ha": "/app/ha-mcp.py",
    "sensors": "/app/sensors-mcp.py",
    "body": "/app/body-mcp.py",
}
_LEGACY_REQUIRED_ENV_KEYS = {"HA_URL", "SUPERVISOR_TOKEN", "EHA_DATA_DIR", "PATH"}


def _legacy_global_mcp_config_path() -> str:
    return os.path.join(antigravity_setup.home_dir(), ".gemini", "config", "mcp_config.json")


def _is_eha_legacy_global_mcp_config(data: object) -> bool:
    """Return whether data exactly matches the old EHA-owned global config shape."""
    if not isinstance(data, dict) or set(data) != {"mcpServers"}:
        return False
    servers = data["mcpServers"]
    if not isinstance(servers, dict) or set(servers) != set(_LEGACY_GLOBAL_MCP_SERVERS):
        return False

    for name, script_path in _LEGACY_GLOBAL_MCP_SERVERS.items():
        server = servers[name]
        if not isinstance(server, dict) or set(server) != {"command", "args", "env"}:
            return False
        if server["command"] != "python3" or server["args"] != [script_path]:
            return False
        env = server["env"]
        if not isinstance(env, dict) or not _LEGACY_REQUIRED_ENV_KEYS.issubset(env):
            return False
    return True


def quarantine_legacy_global_mcp_config() -> str | None:
    """Atomically move an EHA-owned legacy global MCP config to a unique backup."""
    config_path = _legacy_global_mcp_config_path()
    try:
        with open(config_path, encoding="utf-8") as config_file:
            data = json.load(config_file)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeError):
        return None

    if not _is_eha_legacy_global_mcp_config(data):
        return None

    config_dir = os.path.dirname(config_path)
    backup_fd, backup_path = tempfile.mkstemp(
        prefix="mcp_config.json.eha-f141-legacy-",
        suffix=".bak",
        dir=config_dir,
    )
    os.close(backup_fd)
    try:
        os.replace(config_path, backup_path)
    except OSError:
        try:
            os.unlink(backup_path)
        except FileNotFoundError:
            pass
        raise
    return backup_path


def migrate() -> dict:
    """Quarantine legacy config, then remove agy only from valid non-agy instances.

    Missing, invalid, and unreadable selection state must not be interpreted as
    Claude: an older Antigravity instance may not have a valid flag yet.
    """
    legacy_mcp_backup = quarantine_legacy_global_mcp_config()
    state, selected = harness_state.read_selection()
    if state != "valid":
        result = {"status": "skipped", "reason": f"selection_{state}"}
        if legacy_mcp_backup:
            result["legacy_mcp_backup"] = legacy_mcp_backup
        return result
    if selected == "agy":
        result = {"status": "skipped", "reason": "antigravity_selected"}
        if legacy_mcp_backup:
            result["legacy_mcp_backup"] = legacy_mcp_backup
        return result

    failed_steps = []
    try:
        result = antigravity_setup.uninstall()
    except OSError as exc:
        result = {}
        failed_steps.append(f"uninstall:{type(exc).__name__}")
    try:
        redirect_removed = agy_update_freeze.remove_hosts_redirect()
    except OSError as exc:
        redirect_removed = False
        failed_steps.append(f"freeze_redirect:{type(exc).__name__}")
    removed_files = result.get("removed_files", []) if isinstance(result, dict) else []
    result = {
        "status": "partial" if failed_steps else "removed",
        "selected": selected,
        "removed_file_count": len(removed_files),
        "redirect_removed": bool(redirect_removed),
        "failed_steps": failed_steps,
    }
    if legacy_mcp_backup:
        result["legacy_mcp_backup"] = legacy_mcp_backup
    return result


def main() -> int:
    try:
        result = migrate()
    except Exception as exc:  # noqa: BLE001 - startup migration must not abort the add-on
        print(f"[f141-agy-cleanup] cleanup failed: {exc}", file=sys.stderr)
        return 1
    if legacy_mcp_backup := result.get("legacy_mcp_backup"):
        print(
            "[f141-agy-cleanup] quarantined legacy global MCP config: "
            f"backup={legacy_mcp_backup}"
        )
    if result["status"] == "skipped":
        print(f"[f141-agy-cleanup] skipped: {result['reason']}")
        return 0
    print(
        "[f141-agy-cleanup] removed unused Antigravity credentials/binary: "
        f"selected={result['selected']} files={result['removed_file_count']} "
        f"freeze_redirect={result['redirect_removed']}"
    )
    if result["status"] == "partial":
        print(
            "[f141-agy-cleanup] incomplete steps; next startup will retry: "
            + ",".join(result["failed_steps"]),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
