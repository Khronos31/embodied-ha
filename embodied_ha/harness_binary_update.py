"""Operator-driven, atomic updates of the harness CLIs.

Design contract 0.7 puts harness binaries outside the add-on image: the operator
installs them from the vendor at runtime, and the add-on's job is to make that
"explicitly update, and roll back when it breaks" rather than to manage versions
on the operator's behalf. This module is the update half of that contract, for
all three harnesses.

Two shapes, one transaction
---------------------------
The three CLIs are not installed alike, so the *unit* being swapped differs:

- **agy** is a single binary file placed by a vendor shell script.
- **claude / codex** are install *roots* (a directory with ``bin/<name>``)
  placed by ``claude_setup.install()`` / ``codex_setup.install()``.

The transaction around them is the same: record the current build, keep it
aside, swap atomically, verify, and put the old one back if verification fails.
Only the "how do I obtain and place the new build" step is per-harness, and for
claude/codex that step is *their existing installer*, which already downloads,
verifies a SHA-256, stages into a temp directory and ``os.replace``s the root.
Re-implementing that here would duplicate verified code for no gain, so this
module wraps it rather than replacing it.

What was missing for claude/codex was never the swap — it was that the previous
build is deleted on success, so there is nothing to roll back to, and that
nobody wrote down which version got installed.

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
import http.client
import json
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

import agy_update_freeze
import antigravity_setup
import harness_pin

# claude/codex helpers are optional in the same defensive sense as the web
# server treats them: a missing module must degrade to "this harness cannot be
# updated here", never to an import error that takes the whole module down.
try:
    import claude_setup
except Exception:  # pragma: no cover - exercised only on a broken install
    claude_setup = None
try:
    import codex_setup
except Exception:  # pragma: no cover - exercised only on a broken install
    codex_setup = None

HARNESS = "agy"
HARNESSES = ("claude", "codex", "agy")

# How many superseded builds to keep per harness. One is what rollback needs;
# more would accumulate in /data, which is included in Home Assistant backups —
# the same way an unbounded cache turned one instance's /data into 14.7 GB.
# Superseded builds beyond this are removed from disk and from the pin record.
RETAINED_GENERATIONS = 1

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
# Per-harness accessors
#
# Everything below this block is written against these four questions, so the
# transaction itself never branches on the harness name.
# --------------------------------------------------------------------------


def _setup_module(harness: str):
    module = {"claude": claude_setup, "codex": codex_setup}.get(harness)
    if module is None:
        raise UpdateError(f"{harness} setup helpers are unavailable")
    return module


def _binary_path(harness: str) -> str:
    """The executable to run for ``--version``."""
    if harness == "agy":
        return antigravity_setup.binary_path()
    return _setup_module(harness).binary_path()


def _unit_path(harness: str) -> str:
    """The filesystem object that gets swapped and retained.

    For agy that is the binary itself; for claude/codex it is the whole install
    root, because that is what their installer replaces atomically.
    """
    if harness == "agy":
        return antigravity_setup.binary_path()
    return _setup_module(harness)._resolved_install_root()


def _is_installed(harness: str) -> bool:
    if harness == "agy":
        return antigravity_setup.is_installed()
    module = {"claude": claude_setup, "codex": codex_setup}.get(harness)
    return bool(module and module.is_installed())


def _subprocess_env(harness: str) -> dict[str, str]:
    if harness == "agy":
        return antigravity_setup.subprocess_env()
    module = _setup_module(harness)
    env = getattr(module, "subprocess_env", None)
    return env() if callable(env) else dict(os.environ)


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------


def installed_version(harness: str = HARNESS, timeout: float = 15) -> str | None:
    """Return the running build's version, or ``None`` when it cannot be read.

    ``None`` is not an error here: an operator may have a binary the CLI cannot
    describe, and refusing to show the update screen for that would strand them.
    All three CLIs print a version containing a plain ``x.y.z`` (measured:
    ``1.1.12``, ``2.1.228 (Claude Code)``, ``codex-cli 0.145.0``).
    """
    if not _is_installed(harness):
        return None
    try:
        proc = subprocess.run(
            [_binary_path(harness), "--version"],
            capture_output=True,
            text=True,
            env=_subprocess_env(harness),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UpdateError):
        return None
    if proc.returncode != 0:
        return None
    match = _VERSION_RE.search(f"{proc.stdout}\n{proc.stderr}")
    return match.group(1) if match else None


def latest_version(harness: str = HARNESS, timeout: int = 60) -> str:
    """Resolve the version the vendor is currently publishing.

    Each CLI already has a resolver; this only picks the right one. Note the
    asymmetry: agy's manifest is latest-only, while claude/codex resolve a
    channel name. Either way "update" means "move to what is published now",
    matching how Home Assistant updates add-ons.
    """
    if harness == "agy":
        return fetch_manifest(timeout=timeout)["version"]
    module = _setup_module(harness)
    if harness == "claude":
        return module.resolve_version(timeout=timeout)
    return module.resolve_release(timeout=timeout)["version"]


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


def _nameservers() -> list[str]:
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.append(parts[1])
    except OSError:
        pass
    return servers


def _resolve_bypassing_hosts(hostname: str, timeout: float = 5) -> str:
    """Resolve ``hostname`` by asking DNS directly, ignoring ``/etc/hosts``.

    The update freeze works by pointing this very hostname at 127.0.0.1 in
    ``/etc/hosts``, which the libc resolver consults before DNS. That stops agy's
    background updater — and equally stops *us* from reading the release
    manifest, which is why checking used to require lifting the freeze and
    opening a window where the background updater could slip through.

    Asking the nameserver directly skips the hosts file, so the freeze can stay
    up while we look. This matters more than it sounds: the only frozen host is
    the manifest endpoint (the archive itself lives on storage.googleapis.com),
    so with this the whole update path never lowers the guard. It is also what
    makes a future periodic update check safe to add — otherwise every scheduled
    check would open the window again.

    A hand-written query is used rather than a DNS library to avoid adding a
    dependency for one A record. Anything unexpected raises, and the caller
    falls back to the ordinary path.
    """
    servers = _nameservers()
    if not servers:
        raise UpdateError("no nameserver to resolve the release host")
    query_id = 0x4548  # "EH" — fixed: the socket is connected and single-use.
    query = struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    for label in hostname.split("."):
        encoded = label.encode("idna" if not label.isascii() else "ascii")
        if not 0 < len(encoded) < 64:
            raise UpdateError(f"bad label in release host: {label!r}")
        query += bytes([len(encoded)]) + encoded
    query += b"\x00" + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN

    last_error: Exception | None = None
    for server in servers:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(query, (server, 53))
                payload = sock.recv(4096)
            return _first_a_record(payload, query_id)
        except Exception as exc:  # noqa: BLE001 - try the next nameserver
            last_error = exc
    raise UpdateError(f"could not resolve the release host: {last_error}")


def _first_a_record(payload: bytes, query_id: int) -> str:
    """Pull the first A record out of a DNS response, following CNAMEs."""
    if len(payload) < 12:
        raise UpdateError("truncated DNS response")
    response_id, flags, _qd, answer_count = struct.unpack(">HHHH", payload[:8])
    if response_id != query_id:
        raise UpdateError("DNS response id did not match the query")
    if flags & 0x0200:
        raise UpdateError("DNS response was truncated")
    if flags & 0x000F:
        raise UpdateError(f"DNS server returned rcode {flags & 0x000F}")
    offset = 12
    while payload[offset]:  # skip the echoed question name
        offset += payload[offset] + 1
    offset += 5  # terminator + QTYPE + QCLASS
    for _ in range(answer_count):
        if payload[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while payload[offset]:
                offset += payload[offset] + 1
            offset += 1
        record_type, _cls, _ttl, length = struct.unpack(">HHIH", payload[offset : offset + 10])
        offset += 10
        if record_type == 1 and length == 4:
            return socket.inet_ntoa(payload[offset : offset + 4])
        offset += length
    raise UpdateError("DNS response carried no A record")


def _read_via_pinned_ip(url: str, timeout: float, max_bytes: int) -> bytes:
    """GET ``url`` from a directly-resolved IP, still validating TLS by name.

    Connecting to the address while keeping SNI and certificate verification on
    the hostname means bypassing ``/etc/hosts`` costs nothing in transport
    security: a wrong answer from DNS fails the handshake rather than serving us
    someone else's manifest.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise UpdateError(f"refusing to pin a non-https URL: {url}")
    address = _resolve_bypassing_hosts(parsed.hostname)
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(
        parsed.hostname, parsed.port or 443, timeout=timeout, context=context
    )
    # Keep `host` (SNI, Host header, cert check) and change only where we dial.
    connection._dns_pinned_address = address  # noqa: SLF001 - read by _connect_pinned

    def _connect_pinned() -> None:
        sock = socket.create_connection((address, connection.port), timeout)
        connection.sock = context.wrap_socket(sock, server_hostname=parsed.hostname)

    connection.connect = _connect_pinned  # type: ignore[method-assign]
    try:
        connection.request("GET", parsed.path or "/", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise UpdateError(f"release manifest returned HTTP {response.status}")
        return response.read(max_bytes + 1)
    finally:
        connection.close()


def _read_manifest_bytes(timeout: int) -> bytes:
    """Read the manifest, preferring the path that leaves the freeze untouched.

    The fallback exists because the pinned path has more ways to fail than a
    plain fetch (no nameserver, a DNS answer we cannot parse, a middlebox). When
    it does, we pay the old cost — lower the freeze for one request — rather than
    refusing to check at all.
    """
    url = f"{MANIFEST_BASE_URL}/{release_platform()}.json"
    try:
        return _read_via_pinned_ip(url, timeout, MAX_MANIFEST_BYTES)
    except Exception as exc:  # noqa: BLE001 - any failure falls back
        print(
            f"[agy-update] direct-IP manifest fetch failed ({exc}); "
            "falling back to a fetch that briefly lowers the update freeze",
            file=sys.stderr,
        )
    agy_update_freeze.remove_hosts_redirect()
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise UpdateError(f"could not fetch the Antigravity release manifest: {exc}") from exc
    finally:
        _restore_freeze()


def fetch_manifest(timeout: int = 60) -> dict[str, str]:
    """Fetch the vendor manifest describing the current release.

    The manifest is *latest-only*: it names one build, with no way to ask for a
    specific version. "Update" therefore means "move to whatever the vendor is
    publishing now" — the same semantics as an HA add-on update. Going back is
    served by retained binaries, not by asking the vendor for an old build.
    """
    payload = _read_manifest_bytes(timeout)
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


def check_for_update(harness: str = HARNESS, timeout: int = 60) -> dict[str, Any]:
    """Report the installed build against the vendor's current release.

    For agy, reaching the vendor requires lifting the update freeze, which is why
    this is an explicit call and not something the daemon does on a timer: the
    freeze exists so model behaviour cannot drift without the operator asking.
    claude/codex have no such redirect (claude is pinned by ``DISABLE_UPDATES=1``
    in its own environment, codex only checks on start-up), so for them this is
    an ordinary release lookup.
    """
    current = installed_version(harness)
    pin = harness_pin.read_pin(harness)
    # 凍結は解かない。manifest は hosts を迂回して実IPから取るため（_read_manifest_bytes）、
    # 確認のたびに窓を開ける必要がなくなった。迂回に失敗した場合だけ、その1回の取得の
    # 中で従来どおり一時解除される。
    available = latest_version(harness, timeout=timeout)
    return {
        "harness": harness,
        "installed_version": current,
        "pinned_version": pin.get("version") if pin else None,
        "available_version": available,
        "update_available": bool(current) and current != available,
        "retained": harness_pin.retained_builds(harness),
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


def _retain_current_root(harness: str, version: str, progress: Progress) -> str:
    """Copy a claude/codex install root aside under a version-qualified name.

    Their installer replaces the root and then deletes its own backup, so the
    copy has to be taken here, before the installer runs. Staged next to the
    root so the later rollback is a same-filesystem rename.
    """
    root = _unit_path(harness)
    retained = f"{root}-{version}"
    _emit(progress, f"旧バージョンを保管: {retained}")
    parent = os.path.dirname(root) or "."
    staging = tempfile.mkdtemp(prefix=f".{harness}-retain-", dir=parent)
    try:
        copied = os.path.join(staging, "root")
        shutil.copytree(root, copied, symlinks=True)
        if os.path.lexists(retained):
            shutil.rmtree(retained, ignore_errors=True)
        os.replace(copied, retained)
    except OSError as exc:
        raise UpdateError(f"could not retain the current {harness} install: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return retained


def _unit_digest(harness: str, unit_path: str) -> str:
    """Digest identifying a retained unit.

    Directories are identified by the digest of their CLI binary rather than of
    the whole tree: it is the file whose swap matters, and hashing a tree would
    make the check depend on incidental files the installer may add.
    """
    if harness == "agy":
        return _sha512_of(unit_path)
    return _sha512_of(os.path.join(unit_path, "bin", harness))


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


def update(
    harness: str = HARNESS, progress: Progress = None, timeout: int = 300
) -> dict[str, Any]:
    """Move ``harness`` to the vendor's current release, atomically.

    Returns a summary on success. On failure the previous build is put back and
    ``UpdateError`` is raised; the only state that survives a failure is the
    retained copy, which is harmless and reusable.
    """
    if harness not in HARNESSES:
        raise UpdateError(f"unknown harness: {harness!r}")
    result = _update_agy(progress, timeout) if harness == "agy" else _update_root(
        harness, progress, timeout
    )
    if result.get("status") == "updated":
        _prune_retained(harness, progress)
    return result


def _update_root(harness: str, progress: Progress, timeout: int) -> dict[str, Any]:
    """Update claude/codex by driving their own installer.

    Their ``install()`` already resolves the channel, downloads, verifies a
    SHA-256, stages into a temp directory and replaces the install root with
    ``os.replace`` — the atomicity this module exists to guarantee is already
    there. What it does not do is keep the superseded root, so the retention
    happens here, before the installer is allowed to run.
    """
    module = _setup_module(harness)
    if not _is_installed(harness):
        raise UpdateError(f"{harness} is not installed; use install rather than update")
    current = installed_version(harness)
    if not current:
        raise UpdateError(f"could not determine the installed {harness} version")

    available = latest_version(harness, timeout=60)
    if available == current:
        _emit(progress, f"すでに最新です（{current}）")
        return {"status": "unchanged", "version": current, "available_version": available}
    _emit(progress, f"{current} → {available} へ更新します")

    retained_path = _retain_current_root(harness, current, progress)
    harness_pin.add_retained(
        harness, current, retained_path, binary_sha512=_unit_digest(harness, retained_path)
    )
    _write_journal(
        {
            "harness": harness,
            "from_version": current,
            "to_version": available,
            "retained_path": retained_path,
            "phase": "prepared",
        }
    )

    try:
        result = module.install(available, timeout=timeout, progress=lambda t: _emit(progress, t))
        installed = result.get("version") if isinstance(result, dict) else None
        # The installer's own report is not taken on trust: what matters is what
        # the binary on disk now says about itself.
        actual = installed_version(harness)
        if not _is_installed(harness):
            raise UpdateError(f"no executable {harness} binary after the update")
        if actual != available:
            raise UpdateError(f"installed {harness} reports {actual!r}, expected {available!r}")
        _emit(progress, "検証に成功しました")
        harness_pin.record_install(
            harness,
            installed or actual,
            binary_sha512=_unit_digest(harness, _unit_path(harness)),
            source="update",
        )
        _clear_journal()
        return {
            "status": "updated",
            "previous_version": current,
            "version": actual,
            "retained_path": retained_path,
        }
    except BaseException as original:
        _emit(progress, "更新に失敗したため元のバージョンへ戻します")
        try:
            _restore_from(retained_path, harness)
        except Exception as rollback_error:
            _write_journal(
                {
                    "harness": harness,
                    "phase": "rollback_failed",
                    "retained_path": retained_path,
                    "error": str(rollback_error),
                }
            )
            raise UpdateError(
                f"{harness} update failed and rollback also failed: {rollback_error}"
            ) from original
        _clear_journal()
        raise


def _update_agy(progress: Progress = None, timeout: int = 300) -> dict[str, Any]:
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
        # 凍結は張ったまま進める。manifest は hosts を迂回して取り、アーカイブ本体は
        # storage.googleapis.com（リダイレクト対象外）から取るため、更新中に
        # bg-updater が本物のホストへ抜ける窓は開かない。
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


def _restore_from(retained_path: str, harness: str = HARNESS) -> None:
    """Put a retained unit back at the live path, atomically.

    Staged next to the target first so the final step is a same-filesystem
    rename: the live path is never left without a usable build, which is the
    same invariant the forward direction keeps.
    """
    if harness == "agy":
        if not os.path.isfile(retained_path):
            raise UpdateError(f"retained Antigravity binary is missing: {retained_path}")
        incoming = os.path.join(antigravity_setup.bin_dir(), ".agy-restore")
        shutil.copy2(retained_path, incoming)
        os.chmod(incoming, 0o755)
        os.replace(incoming, antigravity_setup.binary_path())
        return

    if not os.path.isdir(retained_path):
        raise UpdateError(f"retained {harness} install is missing: {retained_path}")
    root = _unit_path(harness)
    parent = os.path.dirname(root) or "."
    staging = tempfile.mkdtemp(prefix=f".{harness}-restore-", dir=parent)
    try:
        incoming = os.path.join(staging, "root")
        shutil.copytree(retained_path, incoming, symlinks=True)
        superseded = os.path.join(staging, "superseded")
        if os.path.lexists(root):
            os.replace(root, superseded)
        os.replace(incoming, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def rollback(
    harness: str = HARNESS, version: str | None = None, progress: Progress = None
) -> dict[str, Any]:
    """Put a retained build back in place.

    With no ``version`` the most recently retained build is used, which is the
    one the last update replaced.
    """
    if harness not in HARNESSES:
        raise UpdateError(f"unknown harness: {harness!r}")
    retained = harness_pin.retained_builds(harness)
    if not retained:
        raise UpdateError(f"no retained {harness} build to roll back to")
    if version:
        matches = [item for item in retained if item.get("version") == version]
        if not matches:
            raise UpdateError(f"no retained {harness} build for version {version!r}")
        target = matches[-1]
    else:
        target = retained[-1]

    path = target.get("path")
    if not path:
        raise UpdateError(f"retained {harness} build has no recorded path")
    # 存在確認を先に行う。順序を逆にすると、消えた保管版に対して素の
    # FileNotFoundError が利用者へそのまま出る（Playwright 検証で実際に出た）。
    if not os.path.exists(path):
        raise UpdateError(f"retained {harness} build is missing: {path}")
    expected = target.get("binary_sha512")
    if expected and _unit_digest(harness, path) != expected:
        raise UpdateError(f"retained {harness} build does not match its recorded SHA-512")

    previous = installed_version(harness)
    _emit(progress, f"{previous} → {target.get('version')} へ戻します")
    _restore_from(path, harness)
    actual = installed_version(harness)
    if actual != target.get("version"):
        raise UpdateError(
            f"rolled back {harness} reports {actual!r}, expected {target.get('version')!r}"
        )
    harness_pin.record_install(
        harness,
        str(target.get("version")),
        url=target.get("url"),
        binary_sha512=expected,
        source="rollback",
    )
    _clear_journal()
    _emit(progress, "ロールバックしました")
    return {"status": "rolled_back", "previous_version": previous, "version": target.get("version")}


def _prune_retained(harness: str, progress: Progress = None) -> list[str]:
    """Keep only the newest ``RETAINED_GENERATIONS`` superseded builds.

    Retained builds live under ``/data``, which Home Assistant includes in its
    backups. Left unbounded, a few updates of a ~200 MB CLI would quietly inflate
    every backup — the same failure shape as an unbounded conversation cache, and
    one this feature would otherwise introduce while claiming to be housekeeping.

    Removal is best effort and never fails the update that triggered it: losing
    the ability to roll back two versions is not worth failing a good update.
    """
    retained = harness_pin.retained_builds(harness)
    excess = retained[: max(0, len(retained) - RETAINED_GENERATIONS)]
    removed: list[str] = []
    for item in excess:
        path = item.get("path")
        try:
            if path and os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif path and os.path.lexists(path):
                os.unlink(path)
            harness_pin.drop_retained(harness, str(item.get("version")))
            removed.append(str(item.get("version")))
        except Exception as exc:  # noqa: BLE001 - housekeeping never fails the update
            print(
                f"[{harness}-update] could not remove retained {item.get('version')}: {exc}",
                file=sys.stderr,
            )
    if removed:
        _emit(progress, f"古い保管版を削除しました: {', '.join(removed)}")
    return removed


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
    # The journal names its own harness, so start-up does not have to guess
    # which one was mid-update.
    harness = entry.get("harness") if isinstance(entry, dict) else None
    if harness not in HARNESSES:
        harness = HARNESS
    actual = installed_version(harness)
    if actual is None:
        return {
            "status": "unresolved",
            "reason": "version_unreadable",
            "harness": harness,
            "journal": entry,
        }
    pin = harness_pin.read_pin(harness)
    if not pin or pin.get("version") != actual:
        harness_pin.record_install(harness, actual, source="reconcile")
    _clear_journal()
    return {"status": "reconciled", "harness": harness, "version": actual, "journal": entry}


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
