"""Helpers for optional Codex CLI installation and auth state."""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import tarfile
import tempfile
import re
from pathlib import PurePosixPath
from urllib.request import urlopen

RELEASES_URL = "https://api.github.com/repos/openai/codex/releases"
INSTALL_ROOT_ENV = "EHA_CODEX_INSTALL_ROOT"
HOME_ENV = "EHA_CODEX_HOME"
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
# The current Codex package has roughly 11 members and expands to about 50 MiB.
# Keep ample room for normal growth while bounding decompression resource use.
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 1024 * 1024 * 1024

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|[0-9])")
_DEVICE_AUTH_URL_RE = re.compile(
    r"https://auth\.openai\.com/codex/device"
    r"(?:\?[A-Za-z0-9._~:/?#[\]@!$&'()*+,;=%-]*)?(?=$|\s)"
)
_DEVICE_AUTH_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{5}\b")


def install_root() -> str:
    return os.environ.get(INSTALL_ROOT_ENV, "/data/codex-cli")


def home_dir() -> str:
    return os.environ.get(HOME_ENV, "/data/codex-home")


def bin_dir() -> str:
    return os.path.join(install_root(), "bin")


def binary_path() -> str:
    return os.path.join(bin_dir(), "codex")


def auth_path() -> str:
    return os.path.join(home_dir(), "auth.json")


def is_installed() -> bool:
    path = binary_path()
    return os.path.isfile(path) and os.access(path, os.X_OK)


def is_authenticated() -> bool:
    return os.path.exists(auth_path())


def subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the minimal non-secret environment for Codex subprocesses.

    The installer currently uses no subprocess, but device login and version
    checks need this allow-list rather than inheriting add-on credentials.
    """
    env = {
        "HOME": home_dir(),
        "CODEX_HOME": home_dir(),
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if extra:
        env.update(extra)
    return env


def device_auth_values(line: str) -> list[str]:
    """Extract only device-auth URL and code values safe to send to the browser.

    Do not forward the CLI line itself: future versions could add diagnostics or
    terminal control sequences alongside the device-auth details.
    """
    clean = _ANSI_ESCAPE_RE.sub("", line).replace("\r", "")
    return [match.group(0) for match in _DEVICE_AUTH_URL_RE.finditer(clean)] + [
        match.group(0) for match in _DEVICE_AUTH_CODE_RE.finditer(clean)
    ]


def platform_target(machine: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64-unknown-linux-musl"
    if machine in ("aarch64", "arm64"):
        return "aarch64-unknown-linux-musl"
    raise RuntimeError(f"Unsupported architecture: {machine}")


def package_asset_name(target: str) -> str:
    return f"codex-package-{target}.tar.gz"


def checksum_asset_name() -> str:
    return "codex-package_SHA256SUMS"


def _read_url(url: str, timeout: int = 60) -> bytes:
    with urlopen(url, timeout=timeout) as res:
        content_length = res.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("Codex download exceeds the size limit")
        data = res.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("Codex download exceeds the size limit")
    return data


def _release_url(version: str) -> str:
    if version == "latest":
        return f"{RELEASES_URL}/latest"
    return f"{RELEASES_URL}/tags/rust-v{version}"


def resolve_release(version: str = "latest", timeout: int = 60) -> dict:
    """Fetch and validate the release metadata for latest or a Rust version."""
    try:
        release = json.loads(_read_url(_release_url(version), timeout).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not fetch Codex release metadata: {exc}") from exc
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not tag.startswith("rust-v") or not tag[6:]:
        raise RuntimeError("Codex release metadata has no rust-v tag")
    if version != "latest" and tag != f"rust-v{version}":
        raise RuntimeError("Codex release metadata tag did not match requested version")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("Codex release metadata has no assets")
    return {"version": tag[6:], "assets": assets}


def _asset(release: dict, name: str) -> dict:
    for asset in release["assets"]:
        if isinstance(asset, dict) and asset.get("name") == name:
            if isinstance(asset.get("browser_download_url"), str):
                return asset
    raise RuntimeError(f"Could not find release asset {name}")


def expected_sha256(checksums: bytes, asset_name: str) -> str:
    """Read an archive checksum from the official release checksum manifest."""
    try:
        text = checksums.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Codex checksum manifest was not UTF-8") from exc
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == asset_name:
            digest = parts[0].lower()
            if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                return digest
    raise RuntimeError(f"Could not find SHA-256 digest for {asset_name}")


def verify_sha256(data: bytes, expected: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected.lower():
        raise RuntimeError("Downloaded Codex archive checksum did not match expected digest")


def _safe_extract(archive: bytes, destination: str) -> None:
    """Extract a tar archive without links or paths outside destination."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise RuntimeError("Codex archive exceeds the member count limit")
        total_size = 0
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise RuntimeError(f"Unsafe path in Codex archive: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise RuntimeError(f"Unsupported entry in Codex archive: {member.name}")
            if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                raise RuntimeError(f"Codex archive member exceeds the size limit: {member.name}")
            total_size += member.size
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                raise RuntimeError("Codex archive exceeds the total extracted size limit")
        tar.extractall(destination, filter="data")


def _release_directory(extract_dir: str) -> str:
    # 実配布のcodex-packageはアーカイブ直下がリリースルート(bin/codex等)。
    # 2026-07-18のrust-v0.144.5実物で確認。バージョンディレクトリ入れ子の
    # 配布に変わった場合に備え、単一サブディレクトリのフォールバックも残す。
    if os.path.isfile(os.path.join(extract_dir, "bin", "codex")):
        return extract_dir
    candidates = []
    for entry in os.scandir(extract_dir):
        if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "bin", "codex")):
            candidates.append(entry.path)
    if len(candidates) != 1:
        raise RuntimeError("Codex archive did not contain one release directory")
    return candidates[0]


def _replace_install_root(staged_root: str, root: str) -> None:
    parent = os.path.dirname(root) or "."
    backup = tempfile.mkdtemp(prefix=".codex-backup-", dir=parent)
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
                    "Codex installation failed and the previous installation could not be restored; "
                    f"backup remains at {backup}: {restore_exc}"
                ) from exc
        raise
    finally:
        if (installed or restored) and os.path.exists(backup):
            if os.path.isdir(backup) and not os.path.islink(backup):
                shutil.rmtree(backup)
            else:
                os.remove(backup)


def install(version: str = "latest", timeout: int = 60, progress=None) -> dict:
    """Download, verify, and atomically install a Codex release from GitHub."""
    report = progress or (lambda _message: None)
    report("Resolving Codex release")
    release = resolve_release(version, timeout)
    target = platform_target()
    archive_name = package_asset_name(target)
    archive_asset = _asset(release, archive_name)
    checksum_asset = _asset(release, checksum_asset_name())
    report(f"Downloading Codex {release['version']} for {target}")
    checksums = _read_url(checksum_asset["browser_download_url"], timeout)
    expected = expected_sha256(checksums, archive_name)
    archive = _read_url(archive_asset["browser_download_url"], timeout)
    verify_sha256(archive, expected)
    report("Verified SHA-256 checksum")

    root = os.path.abspath(install_root())
    parent = os.path.dirname(root) or "."
    os.makedirs(parent, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".codex-install-", dir=parent) as temp_dir:
        extract_dir = os.path.join(temp_dir, "extract")
        os.mkdir(extract_dir)
        _safe_extract(archive, extract_dir)
        staged_root = _release_directory(extract_dir)
        binary = os.path.join(staged_root, "bin", "codex")
        if not os.path.isfile(binary):
            raise RuntimeError("Codex archive did not contain bin/codex")
        os.chmod(binary, os.stat(binary).st_mode | 0o111)
        _replace_install_root(staged_root, root)
    report("Codex installation complete")
    return {"version": release["version"], "target": target, "checksum_verified": True, "binary_path": binary_path()}


def state() -> dict:
    return {
        "installed": is_installed(),
        "authenticated": is_authenticated(),
        "install_root": install_root(),
        "home_dir": home_dir(),
        "bin_dir": bin_dir(),
        "binary_path": binary_path(),
        "auth_path": auth_path(),
        "checksum_source": "codex-package_SHA256SUMS",
    }


def clear_auth() -> dict:
    removed_files = []
    path = auth_path()
    if os.path.exists(path):
        os.remove(path)
        removed_files.append(path)
    return {"removed_files": removed_files}


def uninstall() -> dict:
    removed_files = []
    root = os.path.abspath(install_root())
    if root == os.path.sep:
        raise RuntimeError("Refusing to uninstall Codex from filesystem root")
    if os.path.exists(root):
        shutil.rmtree(root)
        removed_files.append(root)
    removed_files.extend(clear_auth()["removed_files"])
    return {"removed_files": removed_files}
