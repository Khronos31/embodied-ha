"""Secure export builders for the two Step4 export paths (§14.7, 増分3-redo).

Path 1 — ``build_memory_bundle``: the user-visible "メモリファイルのエクスポート".
Exports Claude Code's built-in auto-memory (MEMORY.md + memory files) *whole*.
No content scanning here — memory is human-readable and silently dropping a
memory file would corrupt the backup worse than any token it might contain
(ゆの決定 2026-07-21: 黙ったトリム禁止). Safety comes from (i) the scope being
structurally outside the credential store, (ii) the user-responsibility notice.

Path 2 — ``build_data_dump``: the developer-tab "/data 生データダンプ".
Dumps the three harness homes (resolved CLAUDE_CONFIG_DIR / CODEX_HOME /
agy ``.gemini``) with credentials removed.  Exclusion is layered (§14.7):
  1st net  — known credential/secret-carrier names and directories (cheap),
  2nd net  — **value-based content scan**: every candidate file is scanned for
             the *live* immediate-takeover secrets (SUPERVISOR_TOKEN,
             ANTHROPIC_API_KEY / claude_api_key, harness OAuth tokens); a file
             containing any of them is dropped whole (no sanitizing — sol H1)
             and recorded in the manifest.  This is the guarantee that the
             "即時乗っ取り可能な機密は含めない" principle (判断事項10) holds
             regardless of where a config file chose to embed a token.

Shared machinery (from export v1, kept): openat-style dir-fd walk with
``O_NOFOLLOW`` at every component, regular-file + ``st_nlink == 1`` checks,
before/after name→inode re-verification, unlinked-temp streaming, admission
caps.  Strengthened per sol M1/M3: full-size read verification, mtime/ctime
stability check, *loud* skips (every skipped file is recorded with a reason —
nothing is dropped silently), a compressed-output cap and a free-disk
admission check.

**Consistency contract**: per-file integrity only (hash matches archived
bytes); cross-file consistency is best-effort.  The dump is a point-in-time
diagnostic snapshot, not a complete backup (manifest carries layout mode).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import tarfile
import tempfile

MANIFEST_VERSION = 2

# Admission caps (sol H6/M3). Abort the whole export when exceeded — never a partial bundle.
MAX_MEMBERS = 20000                      # source data files (manifest reserved separately)
MAX_FILE_BYTES = 64 * 1024 * 1024        # 64 MiB per file
MAX_TOTAL_BYTES = 512 * 1024 * 1024      # 512 MiB of source payload across the bundle
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024    # compressed .tar.gz output cap (sol M3)
MAX_DIR_ENTRIES = 100000                 # scanned entries per directory (DoS guard)
MAX_SCAN_ENTRIES = 500000                # total entries examined across the whole walk
MAX_WALK_DEPTH = 8                       # recursion bound for the home walk
MAX_SKIP_RECORDS = 2000                  # manifest skip-log bound (counter keeps counting)
MIN_FREE_BYTES = 256 * 1024 * 1024       # absolute temp-fs reserve (sol M3)
MIN_FREE_RATIO = 0.05                    # relative temp-fs reserve
MIN_SECRET_LEN = 8                       # ignore collected values shorter than this

# --- Path-2 first net: names/dirs that must never enter the dump ------------------
# Credential stores and known secret-carrier configs (mcp_config.json embeds
# SUPERVISOR_TOKEN / ANTHROPIC_API_KEY — sol H3). Matched case-sensitively on the
# basename; *_PATTERNS use fnmatch.
_EXCLUDE_FILE_NAMES = frozenset({
    ".credentials.json", "credentials.json",   # Claude OAuth store
    ".claude.json",                            # Claude login record (userID)
    "auth.json",                               # Codex auth store
    "antigravity-oauth-token",                 # agy OAuth token
    "eha-auth-ok",                             # agy auth marker (grants is_authenticated)
    "mcp_config.json",                         # embeds SUPERVISOR_TOKEN / API keys
    "options.json",                            # claude_api_key (not under homes, but be safe)
})
_EXCLUDE_FILE_PATTERNS = (
    "*.config.toml",                           # Codex temp launch profiles (sol H3)
    ".claude.json*",                           # .claude.json backups/rotations
    "*token*", "*credential*", "*secret*",     # second name-net (loud-skip, sol H2)
)
_EXCLUDE_DIR_NAMES = frozenset({
    ".agents",                                 # agy per-site MCP configs (sol H3)
    "antigravity-cli",                         # agy token dir
})
# Directories excluded only when directly under a specific home root:
_EXCLUDE_DIR_AT_ROOT = {
    "gemini": frozenset({"config"}),           # .gemini/config (mcp config w/ tokens)
}

_HOME_ROOTS = ("claude", "codex", "gemini")    # manifest namespace names for path 2


class ExportError(Exception):
    """Raised for a bad request, resolution failure, or when admission caps are hit."""


# --- resolved locations -----------------------------------------------------------

def _claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or "/data/claude-home"


def _codex_home() -> str:
    return os.environ.get("EHA_CODEX_HOME") or "/data/codex-home"


def _gemini_dir() -> str:
    home = os.environ.get("EHA_ANTIGRAVITY_HOME") or "/data/"
    return os.path.join(home, ".gemini")


def _layout_mode() -> str:
    data_dir = os.environ.get("EHA_DATA_DIR", "/config/embodied-ha")
    return "data-fallback" if data_dir.startswith("/data") else "config-primary"


def resolve_memory_dir() -> str:
    """Resolve the auto-memory directory for path 1, fail-closed (sol H5).

    Precedence: an explicit ``autoMemoryDirectory`` pinned in
    ``CLAUDE_CONFIG_DIR/settings.json`` wins; otherwise we *discover* (never
    compute) the project memory dir: exactly one ``projects/*/memory`` must
    exist.  Zero → no memory yet; multiple → ambiguous.  Both raise instead of
    guessing or returning an empty bundle (空 tar は作らない).
    """
    config_dir = _claude_config_dir()
    settings_path = os.path.join(config_dir, "settings.json")
    try:
        with open(settings_path, encoding="utf-8") as f:
            pinned = json.load(f).get("autoMemoryDirectory")
    except (OSError, ValueError):
        pinned = None
    if isinstance(pinned, str) and pinned.strip():
        pinned = pinned.strip()
        if not os.path.isabs(pinned):
            raise ExportError("autoMemoryDirectory must be an absolute path")
        if not os.path.isdir(pinned):
            raise ExportError("autoMemoryDirectory does not exist; nothing to export")
        return pinned

    projects = os.path.join(config_dir, "projects")
    candidates = []
    try:
        names = sorted(os.listdir(projects))
    except OSError:
        names = []
    for name in names:
        mem = os.path.join(projects, name, "memory")
        try:
            st = os.lstat(mem)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            candidates.append(mem)
    if not candidates:
        raise ExportError("no auto-memory directory found; nothing to export yet")
    if len(candidates) > 1:
        raise ExportError(
            "multiple auto-memory directories found; refusing to guess "
            "(pin autoMemoryDirectory in settings.json)"
        )
    return candidates[0]


# --- low-level secure open/read (from export v1, strengthened per sol M1) ---------

def _open_root(path: str):
    """Open a trusted root dir. The root itself is EHA-resolved (admin-level), so a
    symlinked root is acceptable; everything BELOW it is opened with O_NOFOLLOW."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None


def _regular_singlelink(st) -> bool:
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


class _Skips:
    """Loud-skip recorder (sol M1: no silent skips). Bounded record list + full count."""

    def __init__(self):
        self.records: list[dict] = []
        self.count = 0

    def add(self, path: str, reason: str):
        self.count += 1
        if len(self.records) < MAX_SKIP_RECORDS:
            self.records.append({"path": path, "reason": reason})


def _secure_read(parent_fd: int, name: str, path_for_error: str, skips: _Skips):
    """Read ``name`` (single component) under ``parent_fd`` without following symlinks.
    Returns bytes only when the name stably maps to ONE single-linked regular inode
    whose (size, mtime_ns, ctime_ns) did not change across the read and whose full
    st_size was actually read (sol M1). None = skipped (always recorded by caller
    or here via ``skips``)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as e:
        skips.add(path_for_error, f"open-failed:{e.__class__.__name__}")
        return None
    try:
        st_fd = os.fstat(fd)
        if not _regular_singlelink(st_fd):
            skips.add(path_for_error, "not-regular-or-hardlinked")
            return None
        key = (st_fd.st_dev, st_fd.st_ino)
        try:
            st_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            skips.add(path_for_error, "race:name-vanished")
            return None
        if (st_before.st_dev, st_before.st_ino) != key or not _regular_singlelink(st_before):
            skips.add(path_for_error, "race:name-rebound")
            return None
        if st_fd.st_size > MAX_FILE_BYTES:
            raise ExportError(f"file exceeds per-file limit: {path_for_error}")
        chunks = []
        remaining = st_fd.st_size
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != st_fd.st_size:
            skips.add(path_for_error, "race:short-read")
            return None
        # Re-verify after reading: same inode, still single-linked, not mutated in place.
        try:
            st_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            skips.add(path_for_error, "race:name-vanished-after")
            return None
        st_fd_after = os.fstat(fd)
        if (st_after.st_dev, st_after.st_ino) != key or not _regular_singlelink(st_after):
            skips.add(path_for_error, "race:name-rebound-after")
            return None
        if st_fd_after.st_nlink != 1:
            skips.add(path_for_error, "race:hardlink-appeared")
            return None
        if (st_fd_after.st_size, st_fd_after.st_mtime_ns, st_fd_after.st_ctime_ns) != (
            st_fd.st_size, st_fd.st_mtime_ns, st_fd.st_ctime_ns
        ):
            skips.add(path_for_error, "race:mutated-during-read")
            return None
        return data
    finally:
        os.close(fd)


# --- path 2: live-secret collection + content scan (§14.7 ①) ----------------------

def _json_string_leaves(obj, out: set):
    if isinstance(obj, str):
        if len(obj) >= MIN_SECRET_LEN:
            out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _json_string_leaves(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_string_leaves(v, out)


def _read_secret_file(path: str) -> bytes | None:
    """Read a credential file securely for the scan set. Returns bytes, or None if the
    file is absent (fine). Raises ExportError if it EXISTS but cannot be read cleanly —
    fail-closed, so a mid-refresh/partial/FIFO credential store aborts the export
    rather than letting it proceed with a stale scan set (sol Med: fail-open collection)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ExportError(f"credential store unreadable ({e.__class__.__name__}); refusing to export")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ExportError("credential store is not a regular file; refusing to export")
        if st.st_size > MAX_FILE_BYTES:
            raise ExportError("credential store exceeds size limit; refusing to export")
        data = b""
        while len(data) < st.st_size:
            chunk = os.read(fd, min(65536, st.st_size - len(data)))
            if not chunk:
                break
            data += chunk
        return data
    finally:
        os.close(fd)


def _collect_live_secrets() -> list[bytes]:
    """Collect the current immediate-takeover secret *values* used to content-scan
    path-2 candidates (§14.7 ①). Inventory (sol impl-review High3): SUPERVISOR_TOKEN,
    MQTT_PASS, ANTHROPIC_API_KEY, options.json string leaves, the three harness
    credential stores' string leaves + raw, and the GitHub App PEM. Present-but-
    unreadable stores abort (fail-closed). Values only live in-process; never logged.

    Residual (accepted, §14.7 / 判断事項10 ②): this guarantees exclusion of the KNOWN
    live values in their literal byte form. Deliberately re-encoded secrets (base64/
    gzip/UTF-16) or future unknown credentials in *content* fall under ユーザー責任 +
    共有厳禁, NOT ①. Opaque/compressed files are excluded from path 2 entirely so an
    included file is always plaintext-scanned (see _is_opaque)."""
    values: set = set()
    for env_key in ("SUPERVISOR_TOKEN", "MQTT_PASS", "ANTHROPIC_API_KEY"):
        v = os.environ.get(env_key) or ""
        if len(v) >= MIN_SECRET_LEN:
            values.add(v)
    options_path = os.environ.get("EHA_OPTIONS_JSON", "/data/options.json")
    data = _read_secret_file(options_path)
    if data is not None:
        try:
            _json_string_leaves(json.loads(data.decode("utf-8")), values)
        except (ValueError, UnicodeDecodeError):
            raise ExportError("options.json unparseable; refusing to export")
    # JSON credential stores (Claude / Codex): parse leaves AND keep raw as a fallback.
    for path in (os.path.join(_claude_config_dir(), ".credentials.json"),
                 os.path.join(_claude_config_dir(), "credentials.json"),
                 os.path.join(_codex_home(), "auth.json")):
        data = _read_secret_file(path)
        if data is None:
            continue
        text = data.decode("utf-8", errors="ignore")
        if len(text.strip()) >= MIN_SECRET_LEN:
            values.add(text.strip())
        try:
            _json_string_leaves(json.loads(text), values)
        except ValueError:
            pass
    # agy OAuth token (opaque) + GitHub App PEM (private key): raw content.
    for path in (os.path.join(_gemini_dir(), "antigravity-cli", "antigravity-oauth-token"),
                 os.environ.get("EHA_GITHUB_APP_PEM") or "/config/embodied-ha/github_app.pem"):
        data = _read_secret_file(path)
        if data is None:
            continue
        stripped = data.decode("utf-8", errors="ignore").strip()
        if len(stripped) >= MIN_SECRET_LEN:
            values.add(stripped)
    return [v.encode("utf-8") for v in values if len(v) >= MIN_SECRET_LEN]


# Magic bytes for common compressed/archive containers the plaintext scan can't see into.
_OPAQUE_MAGIC = (b"\x1f\x8b", b"PK\x03\x04", b"BZh", b"\xfd7zXZ", b"\x28\xb5\x2f\xfd",
                 b"\x04\x22\x4d\x18", b"7z\xbc\xaf\x27\x1c", b"Rar!")


def _is_opaque(data: bytes) -> bool:
    """A file the value-scan can't meaningfully read: binary (NUL in head) or a known
    compressed/archive container. Such files are excluded from path 2 so every INCLUDED
    file was plaintext-scanned — closing the 'secret hidden in a .gz' bypass (sol High3)."""
    head = data[:8192]
    if b"\x00" in head:
        return True
    return any(data.startswith(m) for m in _OPAQUE_MAGIC)


def _contains_secret(data: bytes, secrets: list[bytes]) -> bool:
    return any(s in data for s in secrets)


# --- path-2 root sanity (sol impl-review High2) -----------------------------------

_FORBIDDEN_ROOTS = frozenset({"/", "/data", "/config", "/root", "/etc", "/home"})


def _validate_home_roots(roots: dict[str, str]) -> dict[str, str]:
    """Reject home roots that would turn the 3-home dump into a broad /config or /data
    dump (e.g. a misconfigured CLAUDE_CONFIG_DIR). Returns {name: reason} for the roots
    that must be skipped; the caller records them loudly and does not walk them."""
    bad: dict[str, str] = {}
    real = {}
    for name, path in roots.items():
        rp = os.path.realpath(path)
        real[name] = rp
        if rp in _FORBIDDEN_ROOTS or os.path.dirname(rp) == rp:
            bad[name] = "root-too-broad"
    # Mutual containment: one root inside another collapses the boundary.
    for a, ra in real.items():
        if a in bad:
            continue
        for b, rb in real.items():
            if a == b:
                continue
            if ra == rb or (ra + "/").startswith(rb + "/"):
                bad[a] = "root-overlaps-another"
                break
    return bad


def _name_excluded(name: str) -> bool:
    if name in _EXCLUDE_FILE_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in _EXCLUDE_FILE_PATTERNS)


# --- secure recursive walk for path 2 ---------------------------------------------

def _walk_home(root_name: str, root_fd: int, secrets: list[bytes], skips: _Skips, scanned: list):
    """Yield (virtual_path, data) for every exportable regular file under the home.
    Recurses via dir fds only (never paths), prunes excluded dirs, applies the name
    net, then excludes opaque files, then the content scan. Symlinks and non-regular
    entries are loud-skipped. ``scanned`` is a 1-elem counter bounding total entries
    examined across all homes (sol Med: whole-walk scan cap)."""

    def recurse(dir_fd: int, rel: str, depth: int):
        if depth > MAX_WALK_DEPTH:
            skips.add(f"{root_name}/{rel}" if rel else root_name, "depth-limit")
            return
        try:
            names = os.listdir(dir_fd)
        except OSError as e:
            skips.add(f"{root_name}/{rel}" if rel else root_name, f"listdir-failed:{e.__class__.__name__}")
            return
        if len(names) > MAX_DIR_ENTRIES:
            raise ExportError(f"directory has too many entries: {root_name}/{rel}")
        scanned[0] += len(names)
        if scanned[0] > MAX_SCAN_ENTRIES:
            raise ExportError("export scanned too many entries")
        for name in sorted(names):
            vpath = f"{root_name}/{rel}/{name}" if rel else f"{root_name}/{name}"
            try:
                st = os.lstat(name, dir_fd=dir_fd)
            except OSError:
                skips.add(vpath, "race:name-vanished")
                continue
            if stat.S_ISLNK(st.st_mode):
                skips.add(vpath, "symlink")
                continue
            if stat.S_ISDIR(st.st_mode):
                if name in _EXCLUDE_DIR_NAMES:
                    skips.add(vpath, "excluded-dir")
                    continue
                if rel == "" and name in _EXCLUDE_DIR_AT_ROOT.get(root_name, ()):
                    skips.add(vpath, "excluded-dir")
                    continue
                try:
                    sub_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=dir_fd)
                except OSError as e:
                    skips.add(vpath, f"opendir-failed:{e.__class__.__name__}")
                    continue
                try:
                    yield from recurse(sub_fd, f"{rel}/{name}" if rel else name, depth + 1)
                finally:
                    os.close(sub_fd)
                continue
            if not stat.S_ISREG(st.st_mode):
                skips.add(vpath, "not-regular")
                continue
            if _name_excluded(name):
                skips.add(vpath, "excluded-name")
                continue
            data = _secure_read(dir_fd, name, vpath, skips)
            if data is None:
                continue
            if _is_opaque(data):
                skips.add(vpath, "opaque-not-scannable")
                continue
            if _contains_secret(data, secrets):
                skips.add(vpath, "contains-live-secret")
                continue
            yield vpath, data

    yield from recurse(root_fd, "", 0)


# --- path 1: memory bundle --------------------------------------------------------

def _iter_memory(memory_dir: str, skips: _Skips):
    """Yield (virtual_path, data) for markdown files under the resolved memory dir
    (bounded depth). NON-markdown entries are loud-skipped, never silently dropped,
    and NEVER cause a memory file to be omitted (黙ったトリム禁止). No content scan."""
    root_fd = _open_root(memory_dir)
    if root_fd is None:
        raise ExportError("memory directory is not readable")
    try:
        def recurse(dir_fd: int, rel: str, depth: int):
            if depth > 3:
                skips.add(f"memory/{rel}", "depth-limit")
                return
            try:
                names = os.listdir(dir_fd)
            except OSError as e:
                skips.add(f"memory/{rel}" if rel else "memory", f"listdir-failed:{e.__class__.__name__}")
                return
            if len(names) > MAX_DIR_ENTRIES:
                raise ExportError("memory directory has too many entries")
            for name in sorted(names):
                vpath = f"memory/{rel}/{name}" if rel else f"memory/{name}"
                try:
                    st = os.lstat(name, dir_fd=dir_fd)
                except OSError:
                    skips.add(vpath, "race:name-vanished")
                    continue
                if stat.S_ISLNK(st.st_mode):
                    skips.add(vpath, "symlink")
                    continue
                if stat.S_ISDIR(st.st_mode):
                    try:
                        sub_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         dir_fd=dir_fd)
                    except OSError as e:
                        skips.add(vpath, f"opendir-failed:{e.__class__.__name__}")
                        continue
                    try:
                        yield from recurse(sub_fd, f"{rel}/{name}" if rel else name, depth + 1)
                    finally:
                        os.close(sub_fd)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    skips.add(vpath, "not-regular")
                    continue
                if not name.endswith(".md"):
                    skips.add(vpath, "not-markdown")
                    continue
                data = _secure_read(dir_fd, name, vpath, skips)
                if data is not None:
                    yield vpath, data

        yield from recurse(root_fd, "", 0)
    finally:
        os.close(root_fd)


# --- bundle assembly --------------------------------------------------------------

class _BytesReader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


class _CappedWriter:
    """Wraps the destination fileobj and aborts when compressed output exceeds the
    archive cap (sol M3) — before any HTTP header could have promised success."""

    def __init__(self, fileobj):
        self._f = fileobj
        self.written = 0

    def write(self, data):
        self.written += len(data)
        if self.written > MAX_ARCHIVE_BYTES:
            raise ExportError("archive exceeds compressed size limit")
        return self._f.write(data)

    def flush(self):
        return self._f.flush()

    def close(self):  # the temp fileobj is owned by the caller, never by tarfile
        return None

    def __getattr__(self, name):  # delegate tell/seek/seekable/etc. to the real fileobj
        return getattr(self._f, name)


def _check_temp_space():
    """Free-space admission on the temp filesystem (sol M3): absolute + relative reserve."""
    try:
        sv = os.statvfs(tempfile.gettempdir())
    except OSError:
        return
    free = sv.f_bavail * sv.f_frsize
    total = sv.f_blocks * sv.f_frsize
    # Account for the archive we may write: keep the reserve free AFTER a max-size output
    # (sol Med: admission ignored MAX_ARCHIVE_BYTES).
    usable = free - MAX_ARCHIVE_BYTES
    if usable < MIN_FREE_BYTES or (total > 0 and usable / total < MIN_FREE_RATIO):
        raise ExportError("not enough free temp space for export")


def _assemble(kind: str, entries_iter, dest_fileobj, skips: _Skips, extra_manifest: dict) -> dict:
    entries: list[dict] = []
    total_bytes = 0
    capped = _CappedWriter(dest_fileobj)
    with tarfile.open(fileobj=capped, mode="w:gz") as tar:
        for virtual_path, data in entries_iter:
            if len(entries) >= MAX_MEMBERS:
                raise ExportError("bundle exceeds member limit")
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ExportError("bundle exceeds total size limit")
            entries.append({
                "path": virtual_path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            info = tarfile.TarInfo(name=f"data/{virtual_path}")
            info.size = len(data)
            info.mode = 0o600
            info.type = tarfile.REGTYPE
            tar.addfile(info, _BytesReader(data))

        manifest = {
            "version": MANIFEST_VERSION,
            "kind": kind,
            "layout": _layout_mode(),
            "entries": entries,
            "source_bytes": total_bytes,
            "member_count": len(entries),
            "skipped": skips.records,
            "skipped_count": skips.count,
            **extra_manifest,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        info.mode = 0o600
        info.type = tarfile.REGTYPE
        tar.addfile(info, _BytesReader(manifest_bytes))
    manifest["archive_bytes"] = capped.written
    return manifest


def build_memory_bundle(dest_fileobj) -> dict:
    """Path 1: write the whole auto-memory bundle to ``dest_fileobj``; return manifest.
    Fail-closed resolution (sol H5); no content scan (黙ったトリム禁止); an EMPTY
    memory dir raises rather than producing a deceptive empty backup."""
    memory_dir = resolve_memory_dir()
    skips = _Skips()
    produced = False

    def _iter():
        nonlocal produced
        for item in _iter_memory(memory_dir, skips):
            produced = True
            yield item

    manifest = _assemble("memory", _iter(), dest_fileobj, skips,
                         {"memory_dir": memory_dir})
    if not produced:
        raise ExportError("memory directory contained no exportable markdown files")
    return manifest


def build_data_dump(dest_fileobj) -> dict:
    """Path 2: write the 3-home diagnostic dump to ``dest_fileobj``; return manifest.
    Homes: resolved CLAUDE_CONFIG_DIR / CODEX_HOME / agy ``.gemini``.  Credentials
    are removed by the layered nets; every candidate file is content-scanned for
    live immediate-takeover secrets before inclusion (§14.7 ①)."""
    secrets = _collect_live_secrets()
    skips = _Skips()
    scanned = [0]
    roots = {
        "claude": _claude_config_dir(),
        "codex": _codex_home(),
        "gemini": _gemini_dir(),
    }
    bad_roots = _validate_home_roots(roots)     # sol High2: reject broad/overlapping roots
    excluded_roots = [
        {"path": "/data/options.json", "reason": "credential-store"},
        {"path": "/data/selected_harness", "reason": "instance-selection (§13.4)"},
        {"path": "claude-cli/codex-cli/python-packages/voicevox_core/word2vec",
         "reason": "reproducible-artifacts (sol H6)"},
    ]

    def _iter():
        for root_name in _HOME_ROOTS:
            if root_name in bad_roots:
                skips.add(root_name, bad_roots[root_name])
                continue
            path = roots[root_name]
            root_fd = _open_root(path)
            if root_fd is None:
                skips.add(root_name, "home-missing")
                continue
            try:
                yield from _walk_home(root_name, root_fd, secrets, skips, scanned)
            finally:
                os.close(root_fd)

    return _assemble("data-dump", _iter(), dest_fileobj, skips,
                     {"homes": roots, "excluded_roots": excluded_roots,
                      "note": "point-in-time diagnostic dump, not a complete backup"})


def build_to_tempfile(kind: str):
    """Build the requested bundle into an already-unlinked temp file and return
    (fileobj, manifest). The caller streams from ``fileobj`` (seek(0) first) and
    closes it — there is no on-disk name to race against (sol H3)."""
    if kind == "memory":
        builder = build_memory_bundle
    elif kind == "data-dump":
        builder = build_data_dump
    else:
        raise ExportError(f"unknown export kind: {kind!r}")
    _check_temp_space()
    tmp = tempfile.TemporaryFile(prefix="eha-export-", suffix=".tar.gz")
    try:
        manifest = builder(tmp)
        tmp.flush()
        return tmp, manifest
    except Exception:
        tmp.close()
        raise
