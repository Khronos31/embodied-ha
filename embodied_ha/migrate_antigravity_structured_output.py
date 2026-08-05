#!/usr/bin/env python3
"""Upgrade selected Antigravity instances to native structured output support.

EHA freezes agy background updates so model/runtime behavior cannot drift without an
add-on release. F-157 requires the native ``--output-format`` and ``--json-schema``
flags introduced after the production 1.1.6 binary was frozen. This migration makes
that one required tool transition explicitly, then restores the freeze.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import agy_update_freeze
import antigravity_setup
import harness_state


INSTALL_TIMEOUT_SECONDS = 300
BACKUP_SUFFIX = ".eha-f157-pre-update.bak"
INSTALLER_SHA256 = "ee1ea43ce4e9e56356c4ab6dad907ef357ae4bdfcaadb682735909fb57c9c640"
MANIFEST_BASE_URL = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests"
)
PINNED_RELEASES = {
    "linux_amd64": {
        "version": "1.1.9",
        "url": "https://storage.googleapis.com/antigravity-public/antigravity-cli/1.1.9-6572839516635136/linux-x64/cli_linux_x64.tar.gz",
        "archive_sha512": "3bebfd6fdaa43fff77d33e12927f3db2b1449b008e4398dbb986ea5ee73c55fce512de22d9a711855464ec4fcfc37ea85113e47248a610e53c0e6d5e5297ed95",
        "manifest_sha256": "2159a259437e8d916bdf3d6a8ed07df8e7eb943ad26b9bf4ebf44bca88e3fabe",
        "binary_sha512": "6191bd53b1686eb59fa9396e8d0658dcbc7d9052f94a7d154c01a4e2abef0b62a961034dc290fa0faeaa23121e78d74816e7a1995785b229d8e849f35297a753",
    },
    "linux_arm64": {
        "version": "1.1.9",
        "url": "https://storage.googleapis.com/antigravity-public/antigravity-cli/1.1.9-6572839516635136/linux-arm/cli_linux_arm64.tar.gz",
        "archive_sha512": "9d28ab7e767d7625a88ec74d72148747ce7ac32089c1a78f418edbed5a35a1743c05e86495184b640110a06c5d28e8933e352ea15e0836dd2d64c8e1cf68f199",
        "manifest_sha256": "7ede4e5fad1057196c69cd2c9e23b41f486b917bde6e95e8f1b62a9805ff60ee",
        "binary_sha512": "9e5240be122ef93681ecb371163e7f0304cce08b75891004971c5221c11ab5f80c067456abb62a6e5515142b71fbdc1d4d33008e1315be5483bb78cfbc1fa0f2",
    },
}
_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")


def _run_cli(*args: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [antigravity_setup.binary_path(), *args],
        capture_output=True,
        text=True,
        env=antigravity_setup.subprocess_env(),
        timeout=timeout,
        check=False,
    )


def _cli_version() -> str:
    try:
        proc = _run_cli("--version")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    match = _VERSION_RE.search(f"{proc.stdout}\n{proc.stderr}")
    return match.group(1) if match else "unknown"


def _supports_structured_output() -> bool:
    try:
        proc = _run_cli("--help")
    except (OSError, subprocess.SubprocessError):
        return False
    output = f"{proc.stdout}\n{proc.stderr}"
    return (
        proc.returncode == 0
        and "--output-format" in output
        and "--json-schema" in output
    )


def _binary_backup_path() -> Path:
    return Path(antigravity_setup.binary_path() + BACKUP_SUFFIX)


def _copy_binary_backup() -> Path:
    binary = Path(antigravity_setup.binary_path())
    backup = _binary_backup_path()
    if backup.exists():
        return backup
    backup.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{backup.name}.", dir=backup.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(binary, tmp)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, backup)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return backup


def _restore_binary_backup() -> bool:
    backup = _binary_backup_path()
    if not backup.exists():
        return False
    binary = Path(antigravity_setup.binary_path())
    binary.parent.mkdir(parents=True, exist_ok=True)
    os.replace(backup, binary)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return True


def _recover_interrupted_update() -> bool:
    """Restore a saved binary when an earlier installer died mid-overwrite."""
    backup = _binary_backup_path()
    if not backup.exists():
        return False
    if _supports_structured_output():
        # The new CLI passed its capability check before a previous process stopped.
        # Keeping this backup would make a later intentional uninstall look like an
        # interrupted update and resurrect the old CLI on the next startup.
        backup.unlink()
        return False
    return _restore_binary_backup()


def _auth_snapshot() -> dict[Path, tuple[bytes, int]]:
    snapshot: dict[Path, tuple[bytes, int]] = {}
    for raw_path in (antigravity_setup.oauth_token_path(), antigravity_setup.auth_marker_path()):
        path = Path(raw_path)
        try:
            snapshot[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        except FileNotFoundError:
            continue
    return snapshot


def _auth_is_preserved(snapshot: dict[Path, tuple[bytes, int]]) -> bool:
    for path, (content, mode) in snapshot.items():
        try:
            if path.read_bytes() != content or stat.S_IMODE(path.stat().st_mode) != mode:
                return False
        except FileNotFoundError:
            return False
    return True


def _restore_auth_snapshot(snapshot: dict[Path, tuple[bytes, int]]) -> None:
    for path, (content, mode) in snapshot.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def _rollback_update(auth_snapshot: dict[Path, tuple[bytes, int]]) -> None:
    """Attempt both binary and auth rollback, even if either operation fails."""
    errors: list[BaseException] = []
    try:
        if not _restore_binary_backup():
            errors.append(RuntimeError("rollback binary backup is missing"))
    except BaseException as exc:
        errors.append(exc)
    try:
        _restore_auth_snapshot(auth_snapshot)
    except BaseException as exc:
        errors.append(exc)
    if errors:
        detail = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
        raise RuntimeError(f"rollback incomplete: {detail}") from errors[0]


def _installer_error_output(proc: subprocess.CompletedProcess[str]) -> str:
    text = " ".join((proc.stdout or "").split())
    return text[-800:]


def _release_platform() -> str:
    if platform.system().lower() != "linux":
        raise RuntimeError(f"unsupported Antigravity update OS: {platform.system()}")
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux_amd64"
    if machine in {"aarch64", "arm64"}:
        return "linux_arm64"
    raise RuntimeError(f"unsupported Antigravity update architecture: {machine}")


def _fetch_manifest(platform_key: str, timeout: int = 60) -> bytes:
    url = f"{MANIFEST_BASE_URL}/{platform_key}.json"
    with urlopen(url, timeout=timeout) as response:
        payload = response.read(65_537)
    if len(payload) > 65_536:
        raise RuntimeError("Antigravity release manifest exceeds 64 KiB")
    return payload


def _pinned_install_contract() -> tuple[str, dict[str, str]]:
    script = antigravity_setup.fetch_install_script(timeout=60)
    installer_digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if installer_digest != INSTALLER_SHA256:
        raise RuntimeError(
            "official installer does not match the EHA 2.1.5 pinned SHA-256"
        )

    platform_key = _release_platform()
    pinned = PINNED_RELEASES[platform_key]
    manifest_payload = _fetch_manifest(platform_key)
    if hashlib.sha256(manifest_payload).hexdigest() != pinned["manifest_sha256"]:
        raise RuntimeError("Antigravity 1.1.9 manifest does not match the pinned SHA-256")
    try:
        manifest = json.loads(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Antigravity 1.1.9 manifest is not valid JSON") from exc
    expected_manifest = {
        "version": pinned["version"],
        "url": pinned["url"],
        "sha512": pinned["archive_sha512"],
    }
    if manifest != expected_manifest:
        raise RuntimeError("Antigravity release manifest fields do not match the pinned release")
    return script, pinned


def _binary_sha512() -> str:
    digest = hashlib.sha512()
    with open(antigravity_setup.binary_path(), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_official_installer() -> None:
    script, pinned = _pinned_install_contract()
    proc = subprocess.run(
        ["bash", "-s", "--", "--dir", antigravity_setup.bin_dir()],
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=antigravity_setup.subprocess_env(),
        timeout=INSTALL_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        detail = _installer_error_output(proc)
        raise RuntimeError(
            f"official installer failed with exit code {proc.returncode}"
            + (f": {detail}" if detail else "")
        )
    if not antigravity_setup.is_installed():
        raise RuntimeError("official installer did not leave an executable agy binary")
    if not _supports_structured_output():
        raise RuntimeError("installed agy does not support native structured output")
    if _cli_version() != pinned["version"]:
        raise RuntimeError("installed agy version does not match the pinned release")
    if _binary_sha512() != pinned["binary_sha512"]:
        raise RuntimeError("installed agy binary does not match the pinned SHA-512")


def migrate() -> dict[str, Any]:
    selection_state, selected = harness_state.read_selection()
    if selection_state != "valid":
        return {"status": "skipped", "reason": f"selection_{selection_state}"}
    if selected != "agy":
        return {"status": "skipped", "reason": "antigravity_not_selected"}

    # Freeze before even probing --help, then open only the bounded installer window.
    agy_update_freeze.add_hosts_redirect()
    try:
        recovered = _recover_interrupted_update()
        if not antigravity_setup.is_installed():
            return {"status": "skipped", "reason": "antigravity_not_installed"}
        old_version = _cli_version()
        if _supports_structured_output():
            return {
                "status": "skipped",
                "reason": "structured_output_supported",
                "version": old_version,
                "recovered_interrupted_update": recovered,
            }

        auth_before = _auth_snapshot()
        backup = _copy_binary_backup()
        try:
            # The official installer deliberately exits successfully without updating when
            # TARGET_DIR/agy already exists. The durable backup is therefore established
            # first, then only the binary (never its home/auth data) is removed.
            Path(antigravity_setup.binary_path()).unlink()
            agy_update_freeze.remove_hosts_redirect()
            _run_official_installer()
            if not antigravity_setup.is_installed():
                raise RuntimeError("official installer did not leave an executable agy binary")
            if not _supports_structured_output():
                raise RuntimeError("installed agy does not support native structured output")
            if not _auth_is_preserved(auth_before):
                raise RuntimeError("Antigravity authentication changed during CLI update")
            # Rollback is needed only inside the migration transaction. A persistent
            # old binary would conflict with a later intentional uninstall.
            backup.unlink()
        except BaseException as original:
            try:
                _rollback_update(auth_before)
            except Exception as rollback_error:
                raise RuntimeError(
                    f"Antigravity CLI update failed and {rollback_error}"
                ) from original
            raise

        return {
            "status": "updated",
            "old_version": old_version,
            "new_version": _cli_version(),
            "auth_preserved": True,
            "recovered_interrupted_update": recovered,
        }
    finally:
        if antigravity_setup.is_installed():
            agy_update_freeze.add_hosts_redirect()
        else:
            agy_update_freeze.remove_hosts_redirect()


def main() -> int:
    try:
        result = migrate()
    except Exception as exc:
        print(f"[f157-agy-upgrade] failed: {type(exc).__name__}: {exc}")
        return 1
    if result["status"] == "updated":
        print(
            "[f157-agy-upgrade] updated: "
            f"{result['old_version']} -> {result['new_version']} "
            "auth_preserved=yes rollback_backup=removed"
        )
    else:
        suffix = f" version={result['version']}" if result.get("version") else ""
        print(f"[f157-agy-upgrade] skipped: {result['reason']}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
