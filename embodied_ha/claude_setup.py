"""Helpers for the bundled Claude Code CLI authentication state."""
from __future__ import annotations

import os

CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_CONFIG_DIR = "/data/.claude"
_CREDENTIALS_FILENAMES = (".credentials.json", "credentials.json")


def config_dir() -> str:
    """Return the Claude Code configuration directory."""
    return os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR)


def credentials_paths() -> tuple[str, ...]:
    """Return every credential file location accepted by Claude Code."""
    return tuple(os.path.join(config_dir(), name) for name in _CREDENTIALS_FILENAMES)


def is_installed() -> bool:
    """Report the bundled CLI as installed.

    Claude Code is baked into the Docker image today. A future on-demand
    installation step will replace this constant result with a real check.
    """
    return True


def is_authenticated() -> bool:
    """Return whether API-key or persisted Claude Code authentication exists.

    サブスク認証はOAuthトークン本体(.credentials.json)の有無で判定する。
    .claude.jsonのuserIDは「ログイン記録」であって認証実体ではない——
    userIDがあってもトークンが無ければclaudeは "Not logged in" になる。
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    return any(os.path.exists(path) for path in credentials_paths())


def clear_auth() -> dict:
    """Remove persisted Claude Code credentials without affecting API-key auth."""
    removed_files = []
    for path in credentials_paths():
        if os.path.exists(path):
            os.remove(path)
            removed_files.append(path)
    return {"removed_files": removed_files}


def state() -> dict:
    """Return the common setup status shape for Claude Code."""
    return {
        "installed": is_installed(),
        "authenticated": is_authenticated(),
        "config_dir": config_dir(),
        "credentials_paths": list(credentials_paths()),
    }
