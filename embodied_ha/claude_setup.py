"""Helpers for the bundled Claude Code CLI authentication state."""
from __future__ import annotations

import os

CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_CONFIG_DIR = "/data/.claude"
_CREDENTIALS_FILENAMES = (".credentials.json", "credentials.json")


def config_dir() -> str:
    """Return the Claude Code configuration directory.

    意図的な逸脱(2026-07-18、ゆの承認): 旧server.py実装はimport時に値を固定して
    いたが、本モジュールは呼び出しごとにenvを解決する。本番ではrun.shが起動前に
    一度設定するだけで実行中に変わる経路が無く実挙動差はゼロ。codex_setup/
    antigravity_setupの毎回解決方式と揃え、テスト容易性とimport順非依存を優先した。
    """
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
    """Remove persisted Claude Code credentials without affecting API-key auth.

    非原子的な複数ファイル削除のため、部分成功を握り潰さない: 消せたものは
    removed_filesへ、失敗はerrorsへ載せて返す(FileNotFoundErrorは並行削除等の
    冪等成功として扱う)。
    """
    removed_files = []
    errors = []
    for path in credentials_paths():
        try:
            os.remove(path)
            removed_files.append(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    result = {"removed_files": removed_files}
    if errors:
        result["errors"] = errors
    return result


def state() -> dict:
    """Return the common setup status shape for Claude Code."""
    return {
        "installed": is_installed(),
        "authenticated": is_authenticated(),
        "config_dir": config_dir(),
        "credentials_paths": list(credentials_paths()),
    }
