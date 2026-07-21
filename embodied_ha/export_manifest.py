"""Secure export-bundle builder for akane's portable identity + memory (Step4増分3).

Exports a category-selected allowlist bundle. The allowlist is an *explicit
enumeration* (fixed files + fixed-depth/fixed-suffix dir listings) — never a
recursive walk — so authentication stores are structurally unreachable, not merely
denylisted (sol design red-team H3).

Every path is resolved by walking down from a trusted directory FD one component at
a time with ``O_NOFOLLOW | O_DIRECTORY`` (an openat-style walk), so a symlink at ANY
component — including an intermediate directory — cannot redirect the read outside
the root, and there is no realpath→open TOCTOU (sol export-review H1). Opened files
must be regular AND have ``st_nlink == 1``, rejecting hardlinks to root-external
secrets (H2). ``O_NONBLOCK`` avoids hanging on a FIFO named in the allowlist (Med).
Size/count caps bound resources before anything is written (H6). The bundle is built
into an already-unlinked temp FD and streamed from that same FD, so its filename can
never be swapped for a symlink to an auth file (H3).

**Consistency contract**: the manifest hash matches the archived bytes exactly, but
that is the ONLY guarantee — there is no logical/temporal snapshot. A file appended
to concurrently (JSONL) may be captured mid-write with a partial trailing line, and
cross-file consistency is best-effort.

Import (apply side) is a *separate* increment (3b) with its own journal/rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import tempfile

MANIFEST_VERSION = 1

# Admission caps (sol H6). Abort the whole export if exceeded — never a partial bundle.
MAX_MEMBERS = 20000                      # source data files (manifest reserved separately)
MAX_FILE_BYTES = 64 * 1024 * 1024        # 64 MiB per file
MAX_TOTAL_BYTES = 512 * 1024 * 1024      # 512 MiB of source payload across the bundle
MAX_DIR_ENTRIES = 100000                 # scanned entries per dir source (DoS guard)

_EHA = "eha"      # EHA_DATA_DIR (akane's data)
_ADDON = "addon"  # /data (addon-private)


def _roots() -> dict[str, str]:
    return {
        _EHA: os.environ.get("EHA_DATA_DIR", "/config/embodied-ha"),
        _ADDON: os.environ.get("EHA_ADDON_DATA_DIR", "/data"),
    }


def _f(relpath: str, root: str = _EHA):
    return ("file", root, relpath)


def _d(reldir: str, suffix: str, root: str = _EHA):
    return ("dir", root, reldir, suffix)


# Category -> sources. AUTH paths appear in NO category, so they cannot be requested.
CATEGORIES: dict[str, list] = {
    "identity": [
        _f("character.md"), _f("home_policy.md"), _f("personal.inc"), _f("desires.json"),
    ],
    "memory": [
        _f("log/memory.md"),
        _d("log/memory/episodes", ".json"),
        _d("log/memory/daybooks", ".json"),
        _d("log/memory/causal_chains", ".json"),
        _d("log/memory/consolidations", ".json"),
        _f("log/working_memory.json"),
        _f("log/scene_state.json"),
        _f("log/person_models.json"),
        _f("log/relationships.json"),
        _f("log/self_narrative.md"),
        _f("log/social_state.json"),
        _f("log/shared_focus.json"),
        _f("log/counterfactuals.jsonl"),
        _f("log/open_loops.jsonl"),
    ],
    "preferences": [_f("preferences.json")],
    "floorplan": [_f("floorplan_room_graph_draft.json")],
    "agent_prefs": [_f("agent_prefs.json", root=_ADDON)],
    "transcripts": [
        _f("log/chat_log.jsonl"), _f("log/observations.jsonl"), _f("log/explore.jsonl"),
        _f("log/loop_parse_errors.jsonl"), _f("log/observations_recovered.jsonl"),
    ],
    "audio": [
        _d("wav", ".webm"), _d("wav", ".wav"),
        _f("log/audio_log.jsonl"), _f("log/background_audio_log.jsonl"),
        _f("log/active_listen_log.jsonl"), _f("log/non_speech_audio_events.jsonl"),
        _f("log/audio_event_tags.jsonl"),
    ],
}
CORE_CATEGORIES = ("identity", "memory")
VALID_CATEGORIES = tuple(CATEGORIES.keys())


class ExportError(Exception):
    """Raised for a bad request or when admission caps are exceeded."""


def normalize_categories(categories) -> list[str]:
    """Validate the requested category set; default to core when unspecified."""
    if categories is None:
        return list(CORE_CATEGORIES)
    if not isinstance(categories, (list, tuple)):
        raise ExportError("categories must be a list")
    result = []
    for name in categories:
        if name not in CATEGORIES:
            raise ExportError(f"unknown category: {name!r}")
        if name not in result:
            result.append(name)
    return result or list(CORE_CATEGORIES)


def _open_root(path: str):
    """Open the trusted root dir. The root itself is admin-configured, so a symlinked
    root is acceptable; everything BELOW it is opened with O_NOFOLLOW."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None


def _open_beneath(root_fd: int, relpath: str, want_dir: bool):
    """openat-style walk from root_fd, following NO symlink at any component. Returns an
    fd (dir fd if want_dir) or None. Rejects `..`/absolute escapes."""
    parts = [p for p in relpath.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    cur = os.dup(root_fd)
    try:
        for i, comp in enumerate(parts):
            last = i == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            if last and not want_dir:
                flags |= os.O_NONBLOCK          # never block on a FIFO named here
            else:
                flags |= os.O_DIRECTORY          # each intermediate must be a real dir
            try:
                nxt = os.open(comp, flags, dir_fd=cur)
            except OSError:
                return None                      # ELOOP (symlink), ENOENT, ENOTDIR, ...
            os.close(cur)
            cur = nxt
        result, cur = cur, None
        return result
    finally:
        if cur is not None:
            os.close(cur)


def _regular_singlelink(st) -> bool:
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _secure_read(parent_fd: int, name: str, path_for_error: str):
    """Read ``name`` (a single component) relative to ``parent_fd`` without following a
    symlink, returning its bytes only if the name stably maps to ONE single-linked
    regular inode across the whole read. Re-checking the name→inode binding before and
    after the read closes the hardlink/unlink TOCTOU where an attacker drops st_nlink
    from 2→1 during the open→fstat window (sol export-refix High). None = not exportable."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError:
        return None
    try:
        st_fd = os.fstat(fd)
        if not _regular_singlelink(st_fd):
            return None                          # FIFO/device/dir/socket, or a hardlink
        key = (st_fd.st_dev, st_fd.st_ino)
        try:
            st_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return None
        if (st_before.st_dev, st_before.st_ino) != key or not _regular_singlelink(st_before):
            return None                          # name doesn't point to our single-linked inode
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
        # Re-verify after reading: same inode, still single-linked (no unlink-race snuck in).
        try:
            st_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return None
        if (st_after.st_dev, st_after.st_ino) != key or not _regular_singlelink(st_after):
            return None
        if os.fstat(fd).st_nlink != 1:
            return None
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_file(root_fd: int, relpath: str, path_for_error: str):
    parts = [p for p in relpath.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    *dirparts, base = parts
    if not dirparts:
        return _secure_read(root_fd, base, path_for_error)
    parent = _open_beneath(root_fd, "/".join(dirparts), want_dir=True)
    if parent is None:
        return None
    try:
        return _secure_read(parent, base, path_for_error)
    finally:
        os.close(parent)


def _iter_dir(root_fd: int, reldir: str, suffix: str):
    """Yield (name, data) for suffix-matching regular files directly under reldir,
    opened relative to the (symlink-free) directory fd."""
    dir_fd = _open_beneath(root_fd, reldir, want_dir=True)
    if dir_fd is None:
        return
    try:
        names = os.listdir(dir_fd)
        if len(names) > MAX_DIR_ENTRIES:          # count ALL entries, not just matches (sol Med)
            raise ExportError(f"directory has too many entries: {reldir}")
        for name in sorted(n for n in names if n.endswith(suffix)):
            data = _secure_read(dir_fd, name, f"{reldir}/{name}")
            if data is not None:
                yield name, data
    finally:
        os.close(dir_fd)


def _iter_entries(categories: list[str]):
    """Yield (category, virtual_path, data) for every exportable file."""
    roots = _roots()
    root_fds = {name: _open_root(path) for name, path in roots.items()}
    try:
        for category in categories:
            for source in CATEGORIES[category]:
                kind, root_name = source[0], source[1]
                root_fd = root_fds.get(root_name)
                if root_fd is None:
                    continue
                if kind == "file":
                    relpath = source[2]
                    data = _read_file(root_fd, relpath, relpath)
                    if data is not None:
                        yield category, f"{root_name}/{relpath}", data
                else:
                    reldir, suffix = source[2], source[3]
                    for name, data in _iter_dir(root_fd, reldir, suffix):
                        yield category, f"{root_name}/{reldir}/{name}", data
    finally:
        for fd in root_fds.values():
            if fd is not None:
                os.close(fd)


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


def build_bundle(categories, dest_fileobj) -> dict:
    """Write a .tar.gz bundle of the selected categories to ``dest_fileobj`` and return
    the manifest dict. Applies admission caps; on any breach raises ExportError before
    completing (caller discards the partial output)."""
    categories = normalize_categories(categories)
    entries: list[dict] = []
    total_bytes = 0
    with tarfile.open(fileobj=dest_fileobj, mode="w:gz") as tar:
        for category, virtual_path, data in _iter_entries(categories):
            if len(entries) >= MAX_MEMBERS:
                raise ExportError("bundle exceeds member limit")
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ExportError("bundle exceeds total size limit")
            entries.append({
                "path": virtual_path,
                "category": category,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            info = tarfile.TarInfo(name=f"data/{virtual_path}")
            info.size = len(data)
            info.mode = 0o600
            info.type = tarfile.REGTYPE
            tar.addfile(info, _BytesReader(data))

        manifest = {"version": MANIFEST_VERSION, "categories": categories, "entries": entries}
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        info.mode = 0o600
        info.type = tarfile.REGTYPE
        tar.addfile(info, _BytesReader(manifest_bytes))
    return manifest


def build_bundle_to_tempfile(categories):
    """Build the bundle into an already-unlinked temp file and return (fileobj, manifest).
    The caller streams from ``fileobj`` (seek(0) first) and closes it — there is no
    on-disk name to race against (sol H3)."""
    tmp = tempfile.TemporaryFile(prefix="eha-export-", suffix=".tar.gz")
    try:
        manifest = build_bundle(categories, tmp)
        tmp.flush()
        return tmp, manifest
    except Exception:
        tmp.close()
        raise
