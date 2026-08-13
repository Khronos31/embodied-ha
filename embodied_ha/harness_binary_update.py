"""Operator-driven, atomic updates of the Antigravity CLI binary.

Design contract 0.7 puts harness binaries outside the add-on image: the operator
installs them from the vendor at runtime, and the add-on's job is to make that
"explicitly update, and roll back when it breaks" rather than to manage versions
on the operator's behalf. This module is the update half of that contract.

Why not reuse the vendor installer for updates
----------------------------------------------
The official installer refuses to touch an existing ``agy`` and exits
successfully, so ``migrate_antigravity_structured_output.py`` deletes the binary
first. For a one-shot start-up migration that window is acceptable. Behind a Web
UI button on a *running* daemon it is not: a loop tick landing in the window
finds no binary and loses a turn.

So updates download the release archive directly and swap it in with
``os.replace``. The path always holds a complete, executable binary — before the
call the old one, after it the new one, never neither. A turn already running
keeps its open inode and finishes on the old build (verified: writing to a
running ELF fails with ``ETXTBSY``, renaming over it does not disturb it).

The vendor installer stays in charge of *first* install, where there is no
binary to protect and its PATH/postinstall steps matter.

Retention and interruption are separate facts
---------------------------------------------
The migration inferred "an update was interrupted" from the presence of a backup
file, which forced it to delete the backup on success — leaving nothing to roll
back to. Here the previous build is kept under a version-qualified name and
recorded in ``harness_pin``, while interruption is recorded in an explicit
journal. A retained binary therefore means "you can go back", never "something
crashed".
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
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import agy_update_freeze
import antigravity_setup
import harness_pin

HARNESS = "agy"

MANIFEST_BASE_URL = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests"
)
# The vendor tarball carries exactly one member, the binary, named `antigravity`.
ARCHIVE_MEMBER = "antigravity"
# Observed releases are ~100 MiB; the cap only stops an unbounded read.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 65_536

# Mirrors the migration's parser so both agree on what "1.1.9" means in the
# CLI's free-form --version output.
_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")

_JOURNAL_ENV = "EHA_HARNESS_UPDATE_JOURNAL"
_DEFAULT_JOURNAL = "/data/harness_update_journal.json"

Progress = Callable[[str], None] | None


class UpdateError(RuntimeError):
    """A step of the update transaction failed; state is described by the caller."""


def journal_path() -> str:
    """Return the in-progress journal path, resolving the environment each call."""
    return os.environ.get(_JOURNAL_ENV, _DEFAULT_JOURNAL)


def _emit(progress: Progress, text: str) -> None:
    if progress is not None:
        progress(text)


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------


def installed_version(timeout: float = 15) -> str | None:
    """Return the running binary's version, or ``None`` when it cannot be read.

    ``None`` is not an error here: an operator may have a binary the CLI cannot
    describe, and refusing to show the update screen for that would strand them.
    """
    binary = antigravity_setup.binary_path()
    if not antigravity_setup.is_installed():
        return None
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            env=antigravity_setup.subprocess_env(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = _VERSION_RE.search(f"{proc.stdout}\n{proc.stderr}")
    return match.group(1) if match else None


def release_platform() -> str:
    """Return the vendor's platform key for this machine."""
    if platform.system().lower() != "linux":
        raise UpdateError(f"unsupported Antigravity update OS: {platform.system()}")
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux_amd64"
    if machine in {"aarch64", "arm64"}:
        return "linux_arm64"
    raise UpdateError(f"unsupported Antigravity update architecture: {machine}")


def fetch_manifest(timeout: int = 60) -> dict[str, str]:
    """Fetch the vendor manifest describing the current release.

    The manifest is *latest-only*: it names one build, with no way to ask for a
    specific version. "Update" therefore means "move to whatever the vendor is
    publishing now" — the same semantics as an HA add-on update. Going back is
    served by retained binaries, not by asking the vendor for an old build.
    """
    url = f"{MANIFEST_BASE_URL}/{release_platform()}.json"
    try:
        with urlopen(url, timeout=timeout) as response:
            payload = response.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise UpdateError(f"could not fetch the Antigravity release manifest: {exc}") from exc
    if len(payload) > MAX_MANIFEST_BYTES:
        raise UpdateError("Antigravity release manifest exceeds 64 KiB")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Antigravity release manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise UpdateError("Antigravity release manifest is not an object")
    version = manifest.get("version")
    archive_url = manifest.get("url")
    sha512 = manifest.get("sha512")
    if not (isinstance(version, str) and version.strip()):
        raise UpdateError("Antigravity release manifest has no usable version")
    if not (isinstance(archive_url, str) and archive_url.startswith("https://")):
        raise UpdateError("Antigravity release manifest has no https download URL")
    if not (isinstance(sha512, str) and len(sha512) == 128):
        raise UpdateError("Antigravity release manifest has no SHA-512 digest")
    return {"version": version.strip(), "url": archive_url, "sha512": sha512.lower()}


def _restore_freeze() -> None:
    """Re-freeze whenever a binary is installed, regardless of the prior state.

    Restoring only what we observed lifted would leave the freeze down when the
    hosts file was edited by hand or a previous run died mid-window: the probe
    would report "not active", so nothing would be restored. "Installed implies
    frozen" is the invariant the install endpoint already keeps (server.py's
    post-install finally block), so this mirrors it rather than inventing a
    second rule. Failure is logged, not raised — a stuck freeze must not turn a
    successful update into a failed one.

    ⚠️ The window this closes is not the only one: while the redirect is down,
    another agy turn's background updater can reach the real host and update the
    CLI behind our back. That hole is inherited from the install path (accepted
    in Phase 1, Fable/sol review 2026-07-20) and is now reachable on every
    update, not only on the rare install.
    """
    try:
        if antigravity_setup.is_installed():
            agy_update_freeze.add_hosts_redirect()
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not mask the result
        print(f"[agy-update] could not restore the update freeze: {exc}", file=sys.stderr)


def check_for_update(timeout: int = 60) -> dict[str, Any]:
    """Report the installed build against the vendor's current release.

    Reaching the vendor requires lifting the update freeze, which is why this is
    an explicit call and not something the daemon does on a timer: the freeze
    exists so model behaviour cannot drift without the operator asking.
    """
    current = installed_version()
    pin = harness_pin.read_pin(HARNESS)
    agy_update_freeze.remove_hosts_redirect()
    try:
        manifest = fetch_manifest(timeout=timeout)
    finally:
        _restore_freeze()
    return {
        "installed_version": current,
        "pinned_version": pin.get("version") if pin else None,
        "available_version": manifest["version"],
        "update_available": bool(current) and current != manifest["version"],
        "retained": harness_pin.retained_builds(HARNESS),
    }


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------


def _write_journal(entry: dict[str, Any]) -> None:
    path = journal_path()
    parent = os.path.dirname(path) or os.curdir
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".harness_update-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entry, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_journal() -> dict[str, Any] | None:
    """Return the in-progress update record, or ``None`` when no update is open."""
    try:
        with open(journal_path(), encoding="utf-8") as handle:
            entry = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # An unreadable journal still means "an update was open"; report it as
        # such rather than as "no update", so reconcile() inspects the binary.
        return {"phase": "unreadable"}
    return entry if isinstance(entry, dict) else {"phase": "unreadable"}


def _clear_journal() -> None:
    try:
        os.unlink(journal_path())
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# Update transaction
# --------------------------------------------------------------------------


def _sha512_of(path: str) -> str:
    digest = hashlib.sha512()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: str, timeout: int, progress: Progress) -> None:
    _emit(progress, f"ダウンロード中: {url}")
    written = 0
    try:
        with urlopen(url, timeout=timeout) as response, open(destination, "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ARCHIVE_BYTES:
                    raise UpdateError("Antigravity release archive exceeds the size cap")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise UpdateError(f"could not download the Antigravity release: {exc}") from exc
    _emit(progress, f"ダウンロード完了: {written} bytes")


def _extract_binary(archive: str, destination: str) -> None:
    """Write the single binary member of ``archive`` to ``destination``.

    The member is read by name and written by hand rather than extracted, so no
    path from the archive is ever used to build a filesystem path.
    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                member = tar.getmember(ARCHIVE_MEMBER)
            except KeyError as exc:
                raise UpdateError(
                    f"Antigravity release archive has no `{ARCHIVE_MEMBER}` member"
                ) from exc
            if not member.isfile():
                raise UpdateError(f"`{ARCHIVE_MEMBER}` in the release archive is not a file")
            source = tar.extractfile(member)
            if source is None:
                raise UpdateError(f"`{ARCHIVE_MEMBER}` in the release archive is unreadable")
            with source, open(destination, "wb") as handle:
                shutil.copyfileobj(source, handle, length=1024 * 1024)
                handle.flush()
                os.fsync(handle.fileno())
    except tarfile.TarError as exc:
        raise UpdateError(f"could not read the Antigravity release archive: {exc}") from exc
    os.chmod(destination, 0o755)


def _auth_snapshot() -> dict[str, tuple[bytes, int]]:
    snapshot: dict[str, tuple[bytes, int]] = {}
    for raw_path in (antigravity_setup.oauth_token_path(), antigravity_setup.auth_marker_path()):
        path = Path(raw_path)
        try:
            snapshot[raw_path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UpdateError(f"could not read Antigravity auth state: {exc}") from exc
    return snapshot


def _auth_is_preserved(snapshot: dict[str, tuple[bytes, int]]) -> bool:
    for raw_path, (content, mode) in snapshot.items():
        path = Path(raw_path)
        try:
            if path.read_bytes() != content or stat.S_IMODE(path.stat().st_mode) != mode:
                return False
        except OSError:
            return False
    return True


def _retain_current_binary(version: str, progress: Progress) -> str:
    """Copy the running binary aside under a version-qualified name.

    A copy, not a rename: the live path must keep a working binary for the whole
    preparation phase. The copy is what a later rollback replaces back into
    place, so it is written durably before the swap is attempted.
    """
    binary = antigravity_setup.binary_path()
    retained = os.path.join(antigravity_setup.bin_dir(), f"agy-{version}")
    _emit(progress, f"旧バイナリを保管: {retained}")
    fd, temporary = tempfile.mkstemp(prefix=".agy-retain-", dir=antigravity_setup.bin_dir())
    os.close(fd)
    try:
        shutil.copy2(binary, temporary)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, retained)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise UpdateError(f"could not retain the current Antigravity binary: {exc}") from exc
    return retained


def _verify_installed(expected_version: str, expected_sha512: str) -> None:
    """Run the same acceptance chain the F-157 migration established.

    ``--version`` alone would accept a binary that runs but lost the structured
    output flags, which is the capability the whole update exists to move.
    """
    if not antigravity_setup.is_installed():
        raise UpdateError("no executable Antigravity binary after the swap")
    if _sha512_of(antigravity_setup.binary_path()) != expected_sha512:
        raise UpdateError("installed Antigravity binary does not match the release SHA-512")
    actual = installed_version()
    if actual != expected_version:
        raise UpdateError(
            f"installed Antigravity reports {actual!r}, expected {expected_version!r}"
        )
    if not supports_structured_output():
        raise UpdateError("installed Antigravity does not support native structured output")


def supports_structured_output(timeout: float = 15) -> bool:
    """Whether the installed CLI exposes the native structured-output flags."""
    try:
        proc = subprocess.run(
            [antigravity_setup.binary_path(), "--help"],
            capture_output=True,
            text=True,
            env=antigravity_setup.subprocess_env(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = f"{proc.stdout}\n{proc.stderr}"
    return proc.returncode == 0 and "--output-format" in output and "--json-schema" in output


def update(progress: Progress = None, timeout: int = 300) -> dict[str, Any]:
    """Move the Antigravity CLI to the vendor's current release, atomically.

    Returns a summary on success. On failure the previous binary is put back and
    ``UpdateError`` is raised; the only state that survives a failure is the
    retained copy, which is harmless and reusable.
    """
    if not antigravity_setup.is_installed():
        raise UpdateError("Antigravity is not installed; use install rather than update")

    current = installed_version()
    if not current:
        raise UpdateError("could not determine the installed Antigravity version")

    staging = tempfile.mkdtemp(prefix="eha-agy-update-")
    retained_path: str | None = None
    swapped = False
    auth_before = _auth_snapshot()

    try:
        _emit(progress, "更新の凍結を一時解除します")
        agy_update_freeze.remove_hosts_redirect()

        manifest = fetch_manifest(timeout=60)
        if manifest["version"] == current:
            _emit(progress, f"すでに最新です（{current}）")
            return {
                "status": "unchanged",
                "version": current,
                "available_version": manifest["version"],
            }
        _emit(progress, f"{current} → {manifest['version']} へ更新します")

        archive = os.path.join(staging, "agy.tar.gz")
        _download(manifest["url"], archive, timeout, progress)
        actual = _sha512_of(archive)
        if actual != manifest["sha512"]:
            raise UpdateError("downloaded Antigravity archive does not match the manifest SHA-512")
        _emit(progress, "SHA-512 検証に成功しました")

        candidate = os.path.join(staging, "agy")
        _extract_binary(archive, candidate)
        candidate_sha512 = _sha512_of(candidate)

        retained_path = _retain_current_binary(current, progress)
        harness_pin.add_retained(
            HARNESS,
            current,
            retained_path,
            binary_sha512=_sha512_of(retained_path),
        )
        _write_journal(
            {
                "harness": HARNESS,
                "from_version": current,
                "to_version": manifest["version"],
                "retained_path": retained_path,
                "phase": "prepared",
            }
        )

        # The swap itself. `os.replace` within the same filesystem is atomic, so
        # a crash here leaves either the old or the new binary — never a gap.
        # Staging lives in TMPDIR, which may be a different device, so the
        # candidate is moved next to the target first.
        staged_next_to_target = os.path.join(antigravity_setup.bin_dir(), ".agy-incoming")
        shutil.copy2(candidate, staged_next_to_target)
        os.chmod(staged_next_to_target, 0o755)
        os.replace(staged_next_to_target, antigravity_setup.binary_path())
        swapped = True
        _emit(progress, "バイナリを差し替えました")

        _verify_installed(manifest["version"], candidate_sha512)
        if not _auth_is_preserved(auth_before):
            raise UpdateError("Antigravity authentication changed during the update")
        _emit(progress, "検証に成功しました")

        harness_pin.record_install(
            HARNESS,
            manifest["version"],
            url=manifest["url"],
            binary_sha512=candidate_sha512,
            source="update",
        )
        _clear_journal()
        return {
            "status": "updated",
            "previous_version": current,
            "version": manifest["version"],
            "retained_path": retained_path,
        }
    except BaseException as original:
        if swapped and retained_path:
            _emit(progress, "検証に失敗したため元のバイナリへ戻します")
            try:
                _restore_from(retained_path)
            except Exception as rollback_error:
                _write_journal(
                    {
                        "harness": HARNESS,
                        "phase": "rollback_failed",
                        "retained_path": retained_path,
                        "error": str(rollback_error),
                    }
                )
                raise UpdateError(
                    f"Antigravity update failed and rollback also failed: {rollback_error}"
                ) from original
        _clear_journal()
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _restore_freeze()


def _restore_from(retained_path: str) -> None:
    if not os.path.isfile(retained_path):
        raise UpdateError(f"retained Antigravity binary is missing: {retained_path}")
    incoming = os.path.join(antigravity_setup.bin_dir(), ".agy-restore")
    shutil.copy2(retained_path, incoming)
    os.chmod(incoming, 0o755)
    os.replace(incoming, antigravity_setup.binary_path())


def rollback(version: str | None = None, progress: Progress = None) -> dict[str, Any]:
    """Put a retained build back in place.

    With no ``version`` the most recently retained build is used, which is the
    one the last update replaced.
    """
    retained = harness_pin.retained_builds(HARNESS)
    if not retained:
        raise UpdateError("no retained Antigravity build to roll back to")
    if version:
        matches = [item for item in retained if item.get("version") == version]
        if not matches:
            raise UpdateError(f"no retained Antigravity build for version {version!r}")
        target = matches[-1]
    else:
        target = retained[-1]

    path = target.get("path")
    if not path:
        raise UpdateError("retained Antigravity build has no recorded path")
    # 存在確認を先に行う。順序を逆にすると、消えた保管版に対して素の
    # FileNotFoundError が利用者へそのまま出る（Playwright 検証で実際に出た）。
    if not os.path.isfile(path):
        raise UpdateError(f"retained Antigravity binary is missing: {path}")
    expected = target.get("binary_sha512")
    if expected and _sha512_of(path) != expected:
        raise UpdateError("retained Antigravity binary does not match its recorded SHA-512")

    previous = installed_version()
    _emit(progress, f"{previous} → {target.get('version')} へ戻します")
    _restore_from(path)
    actual = installed_version()
    if actual != target.get("version"):
        raise UpdateError(
            f"rolled back binary reports {actual!r}, expected {target.get('version')!r}"
        )
    harness_pin.record_install(
        HARNESS,
        str(target.get("version")),
        url=target.get("url"),
        binary_sha512=expected,
        source="rollback",
    )
    _clear_journal()
    _emit(progress, "ロールバックしました")
    return {"status": "rolled_back", "previous_version": previous, "version": target.get("version")}


def reconcile() -> dict[str, Any]:
    """Resolve a journal left behind by an interrupted update (start-up path).

    Because the swap is atomic, an interruption cannot leave a missing binary —
    only a disagreement between what is on disk and what the pin record claims.
    This makes the record agree with the disk, and never re-runs an update the
    operator did not ask for again.
    """
    entry = read_journal()
    if entry is None:
        return {"status": "clean"}
    actual = installed_version()
    if actual is None:
        return {"status": "unresolved", "reason": "version_unreadable", "journal": entry}
    pin = harness_pin.read_pin(HARNESS)
    if not pin or pin.get("version") != actual:
        harness_pin.record_install(HARNESS, actual, source="reconcile")
    _clear_journal()
    return {"status": "reconciled", "version": actual, "journal": entry}


def main(argv: list[str]) -> int:
    """`reconcile` sub-command for run.sh; never fails start-up.

    Start-up only ever reconciles. Updating is an operator action, so nothing
    here reaches the network or changes which build is installed.
    """
    if len(argv) != 2 or argv[1] != "reconcile":
        print("usage: harness_binary_update.py reconcile", file=sys.stderr)
        return 2
    try:
        result = reconcile()
    except Exception as exc:  # noqa: BLE001 - start-up must not fail on bookkeeping
        print(f"[agy-update] reconcile failed: {exc}", file=sys.stderr)
        return 0
    if result["status"] != "clean":
        print(f"[agy-update] {result['status']}: {json.dumps(result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
