#!/usr/bin/env python3
"""Upgrade selected Antigravity instances to native structured output support.

EHA freezes agy background updates so model/runtime behavior cannot drift without an
add-on release. F-157 requires the native ``--output-format`` and ``--json-schema``
flags introduced after the production 1.1.6 binary was frozen. This migration makes
that one required tool transition explicitly, then restores the freeze.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import agy_update_freeze
import antigravity_setup
import harness_state


INSTALL_TIMEOUT_SECONDS = 300
BACKUP_SUFFIX = ".eha-f157-pre-update.bak"
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


def _run_official_installer() -> None:
    script = antigravity_setup.fetch_install_script(timeout=60)
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
