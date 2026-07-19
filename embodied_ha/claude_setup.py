"""Helpers for optional Claude Code CLI installation and authentication state."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
from urllib.request import urlopen

CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
NEW_DEFAULT_CONFIG_DIR = "/data/claude-home"
DEFAULT_CONFIG_DIR = NEW_DEFAULT_CONFIG_DIR
_CREDENTIALS_FILENAMES = (".credentials.json", "credentials.json")
RELEASES_URL = "https://downloads.claude.ai/claude-code-releases"
INSTALL_ROOT_ENV = "EHA_CLAUDE_INSTALL_ROOT"
MAX_DOWNLOAD_BYTES = 384 * 1024 * 1024
DEFAULT_RELEASE_CHANNEL = "stable"
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)+")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_BINARY_NAME_RE = re.compile(r"claude(?:\.exe)?")


def config_dir() -> str:
    """Return the Claude Code configuration directory.

    意図的な逸脱(2026-07-18、ゆの承認): 旧server.py実装はimport時に値を固定して
    いたが、本モジュールは呼び出しごとにenvを解決する。本番ではrun.shが起動前に
    一度設定するだけで実行中に変わる経路が無く実挙動差はゼロ。codex_setup/
    antigravity_setupの毎回解決方式と揃え、テスト容易性とimport順非依存を優先した。
    """
    return os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR)


def _has_config_substance(path: str) -> bool:
    """Return whether a Claude config directory has auth or persisted sessions."""
    if any(os.path.exists(os.path.join(path, name)) for name in _CREDENTIALS_FILENAMES):
        return True
    return os.path.isdir(os.path.join(path, "projects"))


def resolve_config_dir(option: str, data_dir: str) -> str:
    """Resolve option, grandfathered legacy config, then the new default.

    Existing instances keep the former ``<data_dir>/.claude`` location only
    when it contains Claude authentication or persisted project state.
    """
    option = option.strip()
    if option:
        return option
    legacy_dir = os.path.join(data_dir, ".claude")
    if _has_config_substance(legacy_dir):
        return legacy_dir
    return NEW_DEFAULT_CONFIG_DIR


def credentials_paths() -> tuple[str, ...]:
    """Return every credential file location accepted by Claude Code."""
    return tuple(os.path.join(config_dir(), name) for name in _CREDENTIALS_FILENAMES)


def install_root() -> str:
    """Return the directory containing the independently installed CLI."""
    return os.environ.get(INSTALL_ROOT_ENV, "/data/claude-cli")


def _resolved_install_root() -> str:
    """Return a safe, canonical install root for filesystem mutations."""
    root = install_root()
    if not root or not root.strip() or not os.path.isabs(root):
        raise RuntimeError("Claude install root must be a non-empty absolute path")
    resolved = os.path.realpath(root)
    if resolved == os.path.sep:
        raise RuntimeError("Refusing to install or uninstall Claude from filesystem root")
    return resolved


def bin_dir() -> str:
    return os.path.join(install_root(), "bin")


def binary_path() -> str:
    return os.path.join(bin_dir(), "claude")


def resolve_claude_bin() -> str | None:
    """Resolve the runnable Claude CLI: managed DIY binary, then PATH fallback."""
    path = binary_path()
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return shutil.which("claude")


def is_installed() -> bool:
    """Return whether the independently installed CLI is executable."""
    path = binary_path()
    return os.path.isfile(path) and os.access(path, os.X_OK)


def is_authenticated() -> bool:
    """Return whether API-key or persisted Claude Code authentication exists.

    サブスク認証はOAuthトークン本体(.credentials.json)の有無で判定する。
    .claude.jsonのuserIDは「ログイン記録」であって認証実体ではない——
    userIDがあってもトークンが無ければclaudeは "Not logged in" になる。
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    return any(os.path.exists(path) for path in credentials_paths())


def runtime_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a minimal, non-secret environment for Claude CLI subprocesses."""
    env = {
        CONFIG_DIR_ENV: config_dir(),
        "DISABLE_UPDATES": "1",
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if extra:
        env.update(extra)
    # A caller must never be able to re-enable self-updates for this managed CLI.
    env["DISABLE_UPDATES"] = "1"
    return env


def platform_target(machine: str | None = None) -> str:
    """Map the container CPU architecture to Claude's glibc release target."""
    machine = (machine or platform.machine()).lower()
    if machine in ("x86_64", "amd64"):
        return "linux-x64"
    if machine in ("aarch64", "arm64"):
        return "linux-arm64"
    raise RuntimeError(f"Unsupported architecture: {machine}")


def _read_url(url: str, timeout: int = 60) -> bytes:
    """Read a release resource while enforcing the maximum binary size."""
    with urlopen(url, timeout=timeout) as res:
        content_length = res.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("Claude download exceeds the size limit")
        data = res.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("Claude download exceeds the size limit")
    return data


def _release_url(version: str) -> str | None:
    if version in ("stable", "latest"):
        return f"{RELEASES_URL}/{version}"
    if not _VERSION_RE.fullmatch(version):
        raise RuntimeError("Claude version must be stable, latest, or a numeric release version")
    return None


def resolve_version(version: str = DEFAULT_RELEASE_CHANNEL, timeout: int = 60) -> str:
    """Resolve a release channel to a concrete Claude Code version."""
    release_url = _release_url(version)
    if release_url is None:
        return version
    try:
        resolved = _read_url(release_url, timeout).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not resolve Claude release {version}: {exc}") from exc
    if not _VERSION_RE.fullmatch(resolved):
        raise RuntimeError("Claude release channel did not return a valid version")
    return resolved


def manifest_url(version: str) -> str:
    return f"{RELEASES_URL}/{version}/manifest.json"


def resolve_manifest(version: str = DEFAULT_RELEASE_CHANNEL, timeout: int = 60) -> tuple[str, dict]:
    """Fetch and validate the release manifest for a concrete Claude version."""
    resolved_version = resolve_version(version, timeout)
    try:
        manifest = json.loads(_read_url(manifest_url(resolved_version), timeout).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not fetch Claude release manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != resolved_version:
        raise RuntimeError("Claude release manifest version did not match requested version")
    if not isinstance(manifest.get("platforms"), dict):
        raise RuntimeError("Claude release manifest has no platforms")
    return resolved_version, manifest


def _platform_asset(manifest: dict, target: str) -> dict:
    asset = manifest["platforms"].get(target)
    if not isinstance(asset, dict):
        raise RuntimeError(f"Claude release manifest has no platform {target}")
    binary = asset.get("binary")
    checksum = asset.get("checksum")
    size = asset.get("size")
    if not isinstance(binary, str) or not _BINARY_NAME_RE.fullmatch(binary):
        raise RuntimeError("Claude release manifest has an unsafe binary name")
    if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
        raise RuntimeError("Claude release manifest has an invalid SHA-256 checksum")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise RuntimeError("Claude release manifest has an invalid binary size")
    if size > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("Claude download exceeds the size limit")
    return {"binary": binary, "checksum": checksum.lower(), "size": size}


def binary_url(version: str, target: str, binary: str) -> str:
    return f"{RELEASES_URL}/{version}/{target}/{binary}"


def verify_sha256(data: bytes, expected: str) -> None:
    if hashlib.sha256(data).hexdigest() != expected.lower():
        raise RuntimeError("Downloaded Claude binary checksum did not match expected digest")


def _replace_install_root(staged_root: str, root: str) -> None:
    """Atomically replace an install root, restoring the prior root on error."""
    parent = os.path.dirname(root) or "."
    backup = tempfile.mkdtemp(prefix=".claude-backup-", dir=parent)
    os.rmdir(backup)
    moved_old = False
    installed = False
    restored = False
    try:
        if os.path.lexists(root):
            os.replace(root, backup)
            moved_old = True
        os.replace(staged_root, root)
        installed = True
    except Exception as exc:
        if moved_old and not os.path.lexists(root):
            try:
                os.replace(backup, root)
                restored = True
            except Exception as restore_exc:
                raise RuntimeError(
                    "Claude installation failed and the previous installation could not be restored; "
                    f"backup remains at {backup}: {restore_exc}"
                ) from exc
        raise
    finally:
        if (installed or restored) and os.path.lexists(backup):
            if os.path.isdir(backup) and not os.path.islink(backup):
                shutil.rmtree(backup)
            else:
                os.remove(backup)


def install(version: str = DEFAULT_RELEASE_CHANNEL, timeout: int = 60, progress=None) -> dict:
    """Download, verify, and atomically install a Claude Code release."""
    root = _resolved_install_root()
    report = progress or (lambda _message: None)
    report("Resolving Claude release")
    resolved_version, manifest = resolve_manifest(version, timeout)
    target = platform_target()
    asset = _platform_asset(manifest, target)
    report(f"Downloading Claude {resolved_version} for {target}")
    binary = _read_url(binary_url(resolved_version, target, asset["binary"]), timeout)
    if len(binary) != asset["size"]:
        raise RuntimeError("Claude binary size did not match release manifest")
    verify_sha256(binary, asset["checksum"])
    if target.startswith("linux-") and not binary.startswith(b"\x7fELF"):
        raise RuntimeError("Downloaded Claude binary is not an ELF executable")
    report("Verified SHA-256 checksum")

    parent = os.path.dirname(root) or "."
    os.makedirs(parent, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".claude-install-", dir=parent) as temp_dir:
        staged_root = os.path.join(temp_dir, "claude-cli")
        staged_bin_dir = os.path.join(staged_root, "bin")
        os.makedirs(staged_bin_dir)
        staged_binary = os.path.join(staged_bin_dir, "claude")
        with open(staged_binary, "wb") as handle:
            handle.write(binary)
        os.chmod(staged_binary, os.stat(staged_binary).st_mode | 0o111)
        _replace_install_root(staged_root, root)
    report("Claude installation complete")
    return {
        "version": resolved_version,
        "platform": target,
        "checksum_verified": True,
        "binary_path": binary_path(),
    }


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
        "install_root": install_root(),
        "bin_dir": bin_dir(),
        "binary_path": binary_path(),
        "checksum_source": "Claude release manifest checksum",
        "config_dir": config_dir(),
        "credentials_paths": list(credentials_paths()),
    }


def uninstall() -> dict:
    """Remove only the independently installed Claude CLI, not credentials."""
    removed_files = []
    root = _resolved_install_root()
    if os.path.lexists(root):
        if os.path.isdir(root) and not os.path.islink(root):
            shutil.rmtree(root)
        else:
            os.remove(root)
        removed_files.append(root)
    return {"removed_files": removed_files}
