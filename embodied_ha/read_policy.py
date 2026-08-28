"""Shared sensitive-path policy for every agent file-reading route."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import PurePosixPath

from state_utils import file_lock

_DENIED_COMPONENTS = frozenset({
    ".storage",
    ".ssh",
    ".claude",
    ".gemini",
    ".codex",
    "claude-home",
    "codex-home",
})

# 退避されたツール結果が置かれる枝。アドオン自身の永続領域(/data)配下の、
# エージェントCLIが生成する作業ディレクトリに限って、上の拒否から外す。
# ⚠️ 構成要素の集合で判定してはいけない。開発機の作業ディレクトリ
# (/config/.tools/... 配下)にも同じ形の枝があり、そこには非公開の資料から
# 組み立てたプロンプト全文が入る。アドオンは config を map しているので届く。
_SPILL_PATTERN = re.compile(
    r"^/data/\.gemini/antigravity-cli/brain/[^/]+/\.system_generated/(?:steps|messages)/"
)

CLAUDE_DENY_RULES = (
    "Read(**/secrets.yaml)",
    "Read(**/.storage/**)",
    "Read(**/.ssh/**)",
    "Read(**/.gemini/**)",
    "Read(/data/options.json)",
    "Read(**/claude-home/**)",
    "Read(**/codex-home/**)",
    "Read(**/.claude/**)",
    "Read(**/.codex/**)",
    "Read(**/*.pem)",
    "Read(**/eha-mcp-*.config.toml)",
)


def read_deny_reason(path: str) -> str:
    normalized = os.path.normpath(path)
    pure = PurePosixPath(normalized)
    name = pure.name.casefold()
    components = {part.casefold() for part in pure.parts}
    if normalized == "/data/options.json":
        return "アドオン設定ファイルは読めません"
    if name == "secrets.yaml":
        return "機密設定ファイルは読めません"
    if name.endswith(".pem"):
        return "秘密鍵ファイルは読めません"
    if name.startswith("eha-mcp-") and name.endswith(".config.toml"):
        return "一時的なエージェント設定は読めません"
    # Antigravity は大きい MCP ツール結果を .system_generated 配下へ退避する。そこが
    # 読めないと「出力が大きいほど届かない」状態になり、ツールを呼べても結果を受け取れない。
    # `steps/` に置かれるのは、小さければそのまま提示されていたツール結果。
    # `messages/` はエージェント間のメッセージ(sender/recipient/content)。
    # ⚠️ 同じ枝の `logs/` は開けない。そこには注入したプロンプト全文とモデルの
    # 思考過程が入り、「提示されていたもの」ではないため。範囲を広げるときは
    # それが自己の思考を一次資料として読み返せることを意味する点を踏まえること。
    if _SPILL_PATTERN.match(normalized):
        return ""
    if components & _DENIED_COMPONENTS:
        return "認証・機密設定ディレクトリ内のファイルは読めません"
    return ""


def _write_settings(path: str, settings: dict, mode: int) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
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


def merge_claude_settings(path: str) -> None:
    """Add deny rules without replacing unrelated user settings."""
    with file_lock(path):
        existed = os.path.exists(path)
        settings = {}
        if existed:
            try:
                with open(path, encoding="utf-8") as f:
                    settings = json.load(f)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("既存のClaude設定を読めないため安全設定を適用できません") from exc
            if not isinstance(settings, dict):
                raise TypeError("既存のClaude設定がJSONオブジェクトではありません")

        excludes = settings.setdefault("claudeMdExcludes", [])
        if not isinstance(excludes, list):
            raise TypeError("claudeMdExcludes が配列ではありません")
        for item in ("/config/CLAUDE.md", "/config/CLAUDE.local.md"):
            if item not in excludes:
                excludes.append(item)

        permissions = settings.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            raise TypeError("permissions がオブジェクトではありません")
        deny = permissions.setdefault("deny", [])
        if not isinstance(deny, list):
            raise TypeError("permissions.deny が配列ではありません")
        for rule in CLAUDE_DENY_RULES:
            if rule not in deny:
                deny.append(rule)

        mode = os.stat(path).st_mode & 0o777 if existed else 0o600
        _write_settings(path, settings, mode)


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "merge-claude-settings":
        print("usage: read_policy.py merge-claude-settings PATH", file=sys.stderr)
        return 2
    try:
        merge_claude_settings(sys.argv[2])
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as exc:
        print(f"[read-policy] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
