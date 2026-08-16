#!/usr/bin/env python3
"""Bounded cooperative-writer isolates and reversible apply helpers.

The control plane owns lifecycle state. This module owns only the short-lived
AOG namespace and the content-addressed operations performed inside it. Every
tree operation goes through the same lstat/open/fstat walker: it does not
follow links, bounds work before retaining metadata, and rechecks a file after
it has been read.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping

from git_environment import clean_git_environment
from operation_deadline import checkpoint, remaining_seconds
from protocol_hash import ProtocolHashError, canonical_bytes, require_repository_path
from workspace_guard import (
    WorkspaceGuardError,
    WorkspaceGuardUnavailable,
    verify_state as verify_workspace,
)


PROTOCOL = "aog.writer-isolation.v1"
COOPERATIVE = "cooperative"
WORKTREE = "git_worktree"
COPY = "bounded_copy"
MAX_GROUP_SIZE = 4
# Keep slot validation coupled to the exported native writer capacity.  The
# control plane imports ``MAX_GROUP_SIZE`` as its cooperative-wave limit, while
# persisted layouts use this alias to make the same bound explicit at every
# filesystem admission point.
MAX_ISOLATE_ROOTS = MAX_GROUP_SIZE
MAX_FILES = 2_048
MAX_BYTES = 64 * 1024 * 1024
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_ENTRIES = 512
MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
# The journal retains both sides of every apply mutation.  Cap its total file
# descriptions as well as its existing byte and entry reservations.
MAX_JOURNAL_FILES = MAX_FILES
# A group can use every native writer slot, but no member can turn that into an
# unbounded multiplication of the copy budget.  These are deliberately one
# aggregate reservation for the group, rather than per-isolate reservations.
MAX_GROUP_FILES = MAX_FILES
MAX_GROUP_BYTES = MAX_BYTES
_CHUNK_BYTES = 1024 * 1024
_MARKER = ".aog-writer-isolation-owned-v1"
_MARKER_BYTES = b"aog.writer-isolation-owned.v1\n"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_LAYOUT_DIGEST_RE = re.compile(r"^[a-z2-7]{52}$")
_ROOT_LAYOUT_DIGEST_RE = re.compile(r"^[a-z2-7]{16}$")
_SLOT_RE = re.compile(r"^n(?P<index>[0-9]{2})$")


class WriterIsolationError(RuntimeError):
    """The requested cooperative isolation cannot be represented safely."""


class WriterIsolationUnavailable(WriterIsolationError):
    """A temporary filesystem or Git failure prevented safe isolation."""


class WriterIsolationUnsupported(WriterIsolationError):
    """The workspace shape cannot use the experimental cooperative mode."""


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _identity(value: Mapping[str, Any]) -> str:
    return _sha256(canonical_bytes(dict(value)))


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _absolute(path: Path | str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_key(path: Path | str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(_absolute(path))))


def _lstat(path: Path, label: str, *, missing: bool = False) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        if missing:
            return None
        raise WriterIsolationError(f"{label} does not exist") from None
    except OSError as error:
        raise WriterIsolationUnavailable(f"{label} is unavailable") from error


def _lstat_directory(path: Path, label: str) -> os.stat_result:
    _assert_real_ancestors(path, label, include_leaf=True)
    metadata = _lstat(path, label)
    assert metadata is not None
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise WriterIsolationUnsupported(f"{label} must be a real directory")
    return metadata


def _node_key(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.S_IFMT(metadata.st_mode),
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", 0)),
    )


def _same_node(before: os.stat_result, after: os.stat_result) -> bool:
    return _node_key(before) == _node_key(after)


def _require_child(root: Path, relative: str, label: str) -> Path:
    try:
        normalized = require_repository_path(relative, label)
    except ProtocolHashError as error:
        raise WriterIsolationError(str(error)) from error
    return root.joinpath(*normalized.split("/"))


def _layout_digest(value: str) -> str:
    """Encode a full SHA-256 identity as a portable path component.

    Git worktree administrative paths on Windows include the destination path.
    Base32 preserves all 256 bits while avoiding the two extra characters per
    byte of hexadecimal digests, so the owned layout stays inside Git's historical
    path ceiling without weakening the namespace identity.
    """

    return base64.b32encode(hashlib.sha256(value.encode("utf-8")).digest()).decode(
        "ascii"
    ).lower().rstrip("=")


def _require_layout_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _LAYOUT_DIGEST_RE.fullmatch(value) is None:
        raise WriterIsolationError(f"{label} is invalid")
    return value


def _canonical_layout_digest(root: Path | str) -> str:
    """Return the fixed, portable canonical-root partition name.

    The full session and batch identities remain in the next two components.
    This 80-bit canonical partition prevents different workspaces sharing a
    state root from ever adopting each other's isolate paths, while keeping
    clean Git worktrees below Windows' administrative path ceiling.
    """

    canonical = os.path.normcase(os.path.realpath(os.fspath(_absolute(root))))
    return _layout_digest(canonical)[:16]


def _require_canonical_layout_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _ROOT_LAYOUT_DIGEST_RE.fullmatch(value) is None:
        raise WriterIsolationError(f"{label} is invalid")
    return value


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or _DIGEST_RE.fullmatch(value[7:]) is None
    ):
        raise WriterIsolationError(f"{label} is invalid")
    return value


def _assert_real_ancestors(path: Path, label: str, *, include_leaf: bool) -> None:
    current = _absolute(path) if include_leaf else _absolute(path).parent
    while True:
        metadata = _lstat(current, label, missing=True)
        if metadata is not None and _is_reparse(metadata):
            raise WriterIsolationError(f"{label} has a reparse ancestor")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _resolved_overlap(left: Path, right: Path) -> bool:
    left_value = os.path.normcase(os.path.realpath(os.fspath(_absolute(left))))
    right_value = os.path.normcase(os.path.realpath(os.fspath(_absolute(right))))
    try:
        common = os.path.commonpath((left_value, right_value))
    except ValueError:
        return False
    return common == left_value or common == right_value


def _assert_state_root_separate(state_root: Path, canonical_root: Path) -> None:
    root = _absolute(state_root)
    canonical = _absolute(canonical_root)
    _assert_real_ancestors(root, "AOG state root", include_leaf=True)
    _assert_real_ancestors(canonical, "canonical workspace root", include_leaf=True)
    _lstat_directory(canonical, "canonical workspace root")
    if _resolved_overlap(root, canonical):
        raise WriterIsolationError(
            "AOG state root cannot be inside, above, or aliased to the canonical workspace"
        )


@contextmanager
def _open_regular(path: Path, label: str) -> Iterator[int]:
    _assert_real_ancestors(path, label, include_leaf=True)
    before = _lstat(path, label)
    assert before is not None
    if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise WriterIsolationError(f"{label} is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WriterIsolationUnavailable(f"{label} cannot be opened") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_node(before, opened):
            raise WriterIsolationError(f"{label} changed while being opened")
        yield descriptor
        closed = os.fstat(descriptor)
        if not _same_node(opened, closed):
            raise WriterIsolationError(f"{label} changed while being read")
    except OSError as error:
        raise WriterIsolationUnavailable(f"{label} cannot be inspected") from error
    finally:
        os.close(descriptor)


def _read_descriptor(descriptor: int, limit: int, *, retain: bool = False) -> tuple[bytes, str, int]:
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    total = 0
    while True:
        checkpoint()
        try:
            chunk = os.read(descriptor, _CHUNK_BYTES)
        except OSError as error:
            raise WriterIsolationUnavailable("cooperative file cannot be read") from error
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise WriterIsolationError("cooperative content exceeds its byte capacity")
        digest.update(chunk)
        if retain:
            chunks.append(chunk)
    return b"".join(chunks), digest.hexdigest(), total


def _read_regular(path: Path, *, limit: int) -> dict[str, Any]:
    with _open_regular(path, "cooperative regular file") as descriptor:
        _content, digest, total = _read_descriptor(descriptor, limit)
        metadata = os.fstat(descriptor)
    return {"bytes": total, "mode": stat.S_IMODE(metadata.st_mode), "sha256": digest}


def _read_marker(namespace: Path) -> bytes:
    with _open_regular(namespace / _MARKER, "AOG isolate namespace marker") as descriptor:
        content, _digest, _size = _read_descriptor(
            descriptor, len(_MARKER_BYTES) + 1, retain=True
        )
    return content


def _write_marker(namespace: Path) -> None:
    marker = namespace / _MARKER
    metadata = _lstat(marker, "AOG isolate namespace marker", missing=True)
    if metadata is None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(marker, flags, 0o600)
        except OSError as error:
            raise WriterIsolationUnavailable(
                "AOG isolate namespace marker cannot be created"
            ) from error
        try:
            if os.write(descriptor, _MARKER_BYTES) != len(_MARKER_BYTES):
                raise WriterIsolationUnavailable("AOG isolate namespace marker cannot be written")
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise WriterIsolationError("AOG isolate namespace marker is unsafe")
        finally:
            os.close(descriptor)
    elif _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise WriterIsolationError("AOG isolate namespace marker is unsafe")
    _require_marker(namespace)


def _require_marker(namespace: Path) -> None:
    metadata = _lstat(namespace / _MARKER, "AOG isolate namespace marker", missing=True)
    if metadata is None:
        raise WriterIsolationError("AOG isolate namespace is unmarked")
    if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise WriterIsolationError("AOG isolate namespace marker is unsafe")
    if _read_marker(namespace) != _MARKER_BYTES:
        raise WriterIsolationError("AOG isolate namespace marker is invalid")


def _namespace_base(state_root: Path, name: str, *, canonical_root: Path) -> Path:
    if name not in {"isolates", "isolate-journals"}:
        raise WriterIsolationError("AOG isolate namespace name is invalid")
    _assert_state_root_separate(state_root, canonical_root)
    owner = _absolute(state_root)
    if _lstat(owner, "AOG state root", missing=True) is None:
        try:
            owner.mkdir(parents=True)
        except OSError as error:
            raise WriterIsolationUnavailable("AOG state root cannot be created") from error
    _lstat_directory(owner, "AOG state root")
    namespace = owner / name
    created = _lstat(namespace, "AOG isolate namespace", missing=True) is None
    if created:
        try:
            namespace.mkdir()
        except OSError as error:
            raise WriterIsolationUnavailable("AOG isolate namespace cannot be created") from error
    _lstat_directory(namespace, "AOG isolate namespace")
    if created:
        _write_marker(namespace)
    else:
        # A pre-existing directory is never adopted.  This keeps cleanup and
        # preparation within a namespace that AOG demonstrably owns.
        _require_marker(namespace)
    return namespace


def _existing_namespace(
    state_root: Path,
    name: str,
    *,
    canonical_root: Path | None,
    terminal_recovery: bool,
) -> Path | None:
    if canonical_root is not None:
        _assert_state_root_separate(state_root, canonical_root)
    owner = _absolute(state_root)
    _assert_real_ancestors(owner, "AOG state root", include_leaf=True)
    if _lstat(owner, "AOG state root", missing=True) is None:
        if terminal_recovery:
            return None
        raise WriterIsolationError("AOG state root is missing")
    _lstat_directory(owner, "AOG state root")
    namespace = owner / name
    if _lstat(namespace, "AOG isolate namespace", missing=True) is None:
        if terminal_recovery:
            return None
        raise WriterIsolationError("AOG isolate namespace is missing")
    _lstat_directory(namespace, "AOG isolate namespace")
    _require_marker(namespace)
    return namespace


def _scan_directory(
    directory: Path,
    label: str,
    *,
    require_stable: bool = True,
) -> Iterable[tuple[Path, str, os.stat_result]]:
    """Read one bounded unsorted directory epoch without following links."""

    _assert_real_ancestors(directory, label, include_leaf=True)
    before = _lstat_directory(directory, label)
    children: list[tuple[Path, str, os.stat_result]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                checkpoint()
                child = directory / entry.name
                metadata = _lstat(child, "cooperative tree entry")
                assert metadata is not None
                children.append((child, entry.name, metadata))
                if len(children) > MAX_FILES:
                    raise WriterIsolationError(
                        "cooperative tree exceeds its file capacity"
                    )
    except OSError as error:
        raise WriterIsolationUnavailable("cooperative tree cannot be enumerated") from error
    after = _lstat_directory(directory, label)
    if require_stable and not _same_node(before, after):
        raise WriterIsolationError("cooperative source directory changed while being scanned")
    # Do not expose a child to copy or cleanup until its parent has passed the
    # post-enumeration identity check. The list is bounded and intentionally
    # left in filesystem order; deterministic identities sort metadata later.
    yield from children


def _describe(
    path: Path,
    *,
    limit: int = MAX_JOURNAL_BYTES,
    exclude_git: bool = False,
) -> dict[str, Any]:
    """Describe a missing/file/tree node with bounded no-follow reads."""

    target = _absolute(path)
    _assert_real_ancestors(target, "cooperative content", include_leaf=True)
    metadata = _lstat(target, "cooperative content", missing=True)
    if metadata is None:
        return {
            "bytes": 0,
            "content_id": _identity({"kind": "missing"}),
            "files": 0,
            "kind": "missing",
        }
    if _is_reparse(metadata):
        raise WriterIsolationError("cooperative content is a reparse point")
    if stat.S_ISREG(metadata.st_mode):
        record = {"files": 1, "kind": "file", **_read_regular(target, limit=limit)}
        return {**record, "content_id": _identity(record)}
    if not stat.S_ISDIR(metadata.st_mode):
        raise WriterIsolationError("cooperative content is unsupported")

    entries: list[dict[str, Any]] = []
    files = 0
    total = 0
    stack: list[tuple[Path, str]] = [(target, "")]
    while stack:
        directory, relative = stack.pop()
        directory_stat = _lstat_directory(directory, "cooperative tree directory")
        entries.append(
            {
                "kind": "directory",
                "mode": stat.S_IMODE(directory_stat.st_mode),
                "path": relative,
            }
        )
        for child, name, child_stat in _scan_directory(directory, "cooperative tree directory"):
            if exclude_git and name.casefold() == ".git":
                continue
            child_relative = f"{relative}/{name}" if relative else name
            if _is_reparse(child_stat):
                raise WriterIsolationError("cooperative tree contains a reparse point")
            files += 1
            if files > MAX_FILES:
                raise WriterIsolationError("cooperative tree exceeds its file capacity")
            if stat.S_ISDIR(child_stat.st_mode):
                stack.append((child, child_relative))
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise WriterIsolationError("cooperative tree contains an unsupported entry")
            file = _read_regular(child, limit=max(0, limit - total))
            total += int(file["bytes"])
            if total > limit:
                raise WriterIsolationError("cooperative tree exceeds its byte capacity")
            entries.append({"kind": "file", "path": child_relative, **file})
    entries.sort(key=lambda item: (str(item["path"]).casefold(), str(item["path"])))
    record = {"bytes": total, "entries": entries, "files": files, "kind": "directory"}
    return {**record, "content_id": _identity(record)}


def _walk_visible(root: Path, *, exclude_git: bool) -> tuple[int, int]:
    description = _describe(root, limit=MAX_BYTES, exclude_git=exclude_git)
    if description["kind"] != "directory":
        raise WriterIsolationUnsupported("workspace root must be a real directory")
    return int(description["files"]), int(description["bytes"])


def _make_directory(
    path: Path,
    label: str,
    *,
    mode: int | None = None,
    require_new: bool = False,
) -> os.stat_result:
    """Create a real directory without adopting a raced copy target.

    Copy destinations receive their final directory mode at creation time.  In
    particular, do not ``chmod(path)`` afterwards: a pathname can have been
    replaced between creation and that call.  The default preserves the normal
    ``mkdir`` behaviour for AOG namespace parents.
    """

    existing = _lstat(path, label, missing=True)
    if existing is None:
        try:
            path.mkdir(mode=0o777 if mode is None else mode)
        except FileExistsError:
            if require_new:
                raise WriterIsolationError(f"{label} already exists") from None
        except OSError as error:
            raise WriterIsolationUnavailable(f"{label} cannot be created") from error
    elif require_new:
        raise WriterIsolationError(f"{label} already exists")
    return _lstat_directory(path, label)


def _ensure_parent(path: Path) -> None:
    parent = path.parent
    missing: list[Path] = []
    while _lstat(parent, "cooperative target parent", missing=True) is None:
        missing.append(parent)
        parent = parent.parent
    _lstat_directory(parent, "cooperative target parent")
    for candidate in reversed(missing):
        _make_directory(candidate, "cooperative target parent")


def _same_open_object(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare the object bound to an open descriptor, not its path spelling."""

    return (
        stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
    )


def _descriptor_has_content(
    descriptor: int,
    *,
    size: int,
    digest: str,
) -> bool:
    """Boundedly compare one open regular file with the bytes AOG wrote.

    This intentionally does not call ``checkpoint``.  It is also used while
    unwinding a cancellation, where a second deadline/interrupt must never
    obscure the original exception.
    """

    if size < 0:
        return False
    os.lseek(descriptor, 0, os.SEEK_SET)
    observed = hashlib.sha256()
    total = 0
    while total <= size:
        chunk = os.read(descriptor, min(_CHUNK_BYTES, size - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > size:
            return False
        observed.update(chunk)
    return total == size and observed.hexdigest() == digest


def _copy_target_matches(
    target: Path,
    expected_object: os.stat_result,
    *,
    size: int,
    digest: str,
    mode: int,
    descriptor: int | None = None,
    suppress_errors: bool = False,
) -> bool:
    """Prove a failed copy still names the object and bytes AOG created."""

    try:
        current = os.lstat(target)
        if (
            _is_reparse(current)
            or not stat.S_ISREG(current.st_mode)
            or not _same_node(current, expected_object)
            or stat.S_IMODE(current.st_mode) != mode
            or int(current.st_size) != size
        ):
            return False
        if descriptor is not None:
            opened = os.fstat(descriptor)
            if not _same_node(opened, expected_object):
                return False
            return _descriptor_has_content(descriptor, size=size, digest=digest)
        with _open_regular(target, "cooperative copy target") as candidate:
            opened = os.fstat(candidate)
            if not _same_node(opened, expected_object):
                return False
            return _descriptor_has_content(candidate, size=size, digest=digest)
    except BaseException:
        # Cleanup is best-effort and must preserve the original materialisation
        # exception.  A failed proof leaves the pathname untouched.  Normal
        # materialisation still propagates cancellation/deadline exceptions.
        if suppress_errors:
            return False
        raise


def _discard_owned_copy(
    target: Path,
    expected_object: os.stat_result,
    *,
    size: int,
    digest: str,
    mode: int,
    descriptor: int | None = None,
) -> bool:
    """Remove only a still-owned failed target; never adopt a replacement."""

    if not _copy_target_matches(
        target,
        expected_object,
        size=size,
        digest=digest,
        mode=mode,
        descriptor=descriptor,
        suppress_errors=True,
    ):
        return False
    try:
        os.unlink(target)
    except BaseException:
        return False
    return True


def _copy_file(
    source: Path,
    target: Path,
    *,
    limit: int = MAX_BYTES,
    ownership: _OwnedCopyTree | None = None,
) -> int:
    source_path = _absolute(source)
    target_path = _absolute(target)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise WriterIsolationError("cooperative copy byte capacity is invalid")
    _assert_real_ancestors(source_path, "cooperative copy source", include_leaf=True)
    _assert_real_ancestors(target_path, "cooperative copy target", include_leaf=True)
    _ensure_parent(target_path)
    if _lstat(target_path, "cooperative copy target", missing=True) is not None:
        raise WriterIsolationError("cooperative copy target already exists")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        output = os.open(target_path, flags, 0o600)
    except OSError as error:
        raise WriterIsolationUnavailable("cooperative file target cannot be opened") from error
    opened_identity: os.stat_result | None = None
    owned_identity: os.stat_result | None = None
    copied = 0
    output_digest = hashlib.sha256()
    output_mode = 0o600
    try:
        opened_identity = os.fstat(output)
        owned_identity = opened_identity
        output_mode = stat.S_IMODE(opened_identity.st_mode)
        with _open_regular(source_path, "cooperative copy source") as input_descriptor:
            source_before = os.fstat(input_descriptor)
            source_digest = hashlib.sha256()
            while True:
                checkpoint()
                chunk = os.read(input_descriptor, _CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > limit:
                    raise WriterIsolationError(
                        "cooperative file copy exceeds its byte capacity"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(output, view)
                    if written <= 0:
                        raise WriterIsolationUnavailable("cooperative file copy failed")
                    output_digest.update(view[:written])
                    view = view[written:]
                    # This snapshot is the last state AOG itself observed
                    # after writing.  Cleanup compares against it instead of
                    # re-fstat'ing after an external race has already won.
                    owned_identity = os.fstat(output)
                source_digest.update(chunk)
            source_stat = os.fstat(input_descriptor)
            if (
                not _same_node(source_before, source_stat)
                or stat.S_IMODE(source_before.st_mode)
                != stat.S_IMODE(source_stat.st_mode)
            ):
                raise WriterIsolationError("cooperative copy source changed during materialization")
            if not stat.S_ISREG(os.fstat(output).st_mode):
                raise WriterIsolationError("cooperative copy target changed type")
            try:
                os.fchmod(output, stat.S_IMODE(source_stat.st_mode))
            except OSError as error:
                raise WriterIsolationUnavailable(
                    "cooperative file target permissions cannot be applied"
                ) from error
            output_stat = os.fstat(output)
            output_mode = stat.S_IMODE(output_stat.st_mode)
            if output_mode != stat.S_IMODE(source_stat.st_mode):
                raise WriterIsolationError("cooperative file target permissions changed")
            if not _same_open_object(opened_identity, output_stat):
                raise WriterIsolationError("cooperative copy target changed while being written")
            owned_identity = output_stat
            if source_digest.hexdigest() != output_digest.hexdigest() or not _descriptor_has_content(
                output,
                size=copied,
                digest=output_digest.hexdigest(),
            ):
                raise WriterIsolationError("cooperative file copy identity changed")
            if not _copy_target_matches(
                target_path,
                output_stat,
                size=copied,
                digest=output_digest.hexdigest(),
                mode=output_mode,
                descriptor=output,
            ):
                raise WriterIsolationError("cooperative file copy target changed")
            claim_owner = ownership if ownership is not None else _ACTIVE_COPY_OWNERSHIP.get()
            if claim_owner is not None:
                claim_owner.add_file(
                    target_path,
                    output_stat,
                    {
                        "bytes": copied,
                        "mode": output_mode,
                        "sha256": output_digest.hexdigest(),
                    },
                )
        try:
            os.close(output)
        finally:
            output = -1
        return copied
    except BaseException as error:
        # Keep the descriptor open for the first unlink attempt.  That binds
        # the pathname to the object AOG opened and prevents inode reuse while
        # checking the byte identity.  Windows may require a close before
        # unlinking, so re-prove both identities after that close as well.
        expected_object = owned_identity
        if output >= 0:
            try:
                if expected_object is not None:
                    _discard_owned_copy(
                        target_path,
                        expected_object,
                        size=copied,
                        digest=output_digest.hexdigest(),
                        mode=output_mode,
                        descriptor=output,
                    )
            except BaseException:
                pass
            try:
                os.close(output)
            except BaseException:
                pass
            output = -1
        if expected_object is not None:
            _discard_owned_copy(
                target_path,
                expected_object,
                size=copied,
                digest=output_digest.hexdigest(),
                mode=output_mode,
            )
        if isinstance(error, OSError):
            raise WriterIsolationUnavailable("cooperative file copy failed") from error
        raise


def _remove_tree(
    path: Path,
    *,
    terminal_recovery: bool = False,
    allow_missing: bool = False,
) -> None:
    """Delete an ordinary tree by streamed lstat traversal without links."""

    target = _absolute(path)
    _assert_real_ancestors(target, "AOG isolate tree", include_leaf=True)
    metadata = _lstat(target, "AOG isolate tree", missing=True)
    if metadata is None:
        if terminal_recovery or allow_missing:
            return
        raise WriterIsolationError("AOG isolate tree is missing")
    if _is_reparse(metadata):
        raise WriterIsolationError("AOG isolate tree became a reparse point")
    if stat.S_ISREG(metadata.st_mode):
        try:
            os.unlink(target)
        except OSError as error:
            raise WriterIsolationUnavailable("AOG isolate file cannot be removed") from error
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise WriterIsolationError("AOG isolate tree has an unsupported entry")
    stack: list[tuple[Path, bool]] = [(target, False)]
    while stack:
        current, visited = stack.pop()
        _assert_real_ancestors(
            current, "AOG isolate cleanup directory", include_leaf=True
        )
        current_stat = _lstat(current, "AOG isolate cleanup directory", missing=True)
        if current_stat is None:
            if terminal_recovery:
                continue
            raise WriterIsolationError("AOG isolate cleanup target disappeared")
        if _is_reparse(current_stat) or not stat.S_ISDIR(current_stat.st_mode):
            raise WriterIsolationError("AOG isolate cleanup found an unsafe entry")
        if visited:
            try:
                os.rmdir(current)
            except OSError as error:
                raise WriterIsolationUnavailable("AOG isolate directory cannot be removed") from error
            continue
        stack.append((current, True))
        for child, _name, child_stat in _scan_directory(
            current, "AOG isolate cleanup directory", require_stable=False
        ):
            if _is_reparse(child_stat):
                raise WriterIsolationError("AOG isolate cleanup found an unsafe entry")
            if stat.S_ISDIR(child_stat.st_mode):
                stack.append((child, False))
            elif stat.S_ISREG(child_stat.st_mode):
                _assert_real_ancestors(
                    child, "AOG isolate cleanup entry", include_leaf=True
                )
                try:
                    os.unlink(child)
                except OSError as error:
                    raise WriterIsolationUnavailable(
                        "AOG isolate cleanup file cannot be removed"
                    ) from error
            else:
                raise WriterIsolationError("AOG isolate cleanup found an unsafe entry")


def _remove_partial_tree(path: Path) -> None:
    """Retire only an empty failed root when no ownership manifest exists."""

    try:
        metadata = _lstat(path, "AOG isolate partial tree", missing=True)
        if (
            metadata is not None
            and not _is_reparse(metadata)
            and stat.S_ISDIR(metadata.st_mode)
        ):
            os.rmdir(path)
    except BaseException:
        # Without exact file claims, recursive deletion could adopt a raced
        # replacement. The durable preparation reservation keeps it fenced.
        pass


class _GroupCopyBudget:
    """Shared file/byte capacity for every bounded-copy isolate in one group."""

    def __init__(self) -> None:
        self.files = 0
        self.bytes = 0

    def consume_file(self) -> None:
        if self.files >= MAX_GROUP_FILES:
            raise WriterIsolationError(
                "cooperative writer group exceeds its aggregate file capacity"
            )
        self.files += 1

    def remaining_bytes(self) -> int:
        return max(0, MAX_GROUP_BYTES - self.bytes)

    def consume_bytes(self, value: int) -> None:
        if value < 0 or value > self.remaining_bytes():
            raise WriterIsolationError(
                "cooperative writer group exceeds its aggregate byte capacity"
            )
        self.bytes += value


class _OwnedCopyTree:
    """The exact fresh nodes one failed bounded copy may reclaim.

    A preparation path is AOG-owned, but that does not make a concurrent
    replacement AOG-owned.  Claims pair each file's content with its object
    identity and retain each created directory identity.  Cleanup only unlinks
    a still matching file and only removes an empty still matching directory.
    """

    def __init__(self) -> None:
        self.directories: list[tuple[Path, os.stat_result]] = []
        self.files: list[tuple[Path, os.stat_result, dict[str, Any]]] = []

    def add_directory(self, path: Path, metadata: os.stat_result) -> None:
        self.directories.append((path, metadata))

    def add_file(
        self,
        path: Path,
        metadata: os.stat_result,
        content: Mapping[str, Any],
    ) -> None:
        self.files.append((path, metadata, dict(content)))

    def cleanup(self) -> None:
        for path, metadata, content in reversed(self.files):
            try:
                _discard_owned_copy(
                    path,
                    metadata,
                    size=int(content["bytes"]),
                    digest=str(content["sha256"]),
                    mode=int(content["mode"]),
                )
            except BaseException:
                pass
        for path, metadata in reversed(self.directories):
            try:
                current = _lstat(path, "AOG isolate cleanup directory", missing=True)
                if (
                    current is not None
                    and not _is_reparse(current)
                    and stat.S_ISDIR(current.st_mode)
                    and _same_open_object(current, metadata)
                ):
                    os.rmdir(path)
            except BaseException:
                # An external child/replacement leaves the directory fenced for
                # terminal recovery instead of deleting someone else's tree.
                pass


_ACTIVE_COPY_OWNERSHIP: ContextVar[_OwnedCopyTree | None] = ContextVar(
    "aog_active_copy_ownership",
    default=None,
)


@contextmanager
def _copy_ownership(ownership: _OwnedCopyTree) -> Iterator[None]:
    """Carry claims through `_copy_node` without widening its public shape."""

    token = _ACTIVE_COPY_OWNERSHIP.set(ownership)
    try:
        yield
    finally:
        _ACTIVE_COPY_OWNERSHIP.reset(token)


def _copy_tree(
    source: Path,
    target: Path,
    *,
    exclude_git: bool = False,
    limit: int = MAX_BYTES,
    group_budget: _GroupCopyBudget | None = None,
    ownership: _OwnedCopyTree | None = None,
) -> tuple[int, int]:
    source_path = _absolute(source)
    target_path = _absolute(target)
    _assert_real_ancestors(source_path, "cooperative copy source", include_leaf=True)
    _assert_real_ancestors(target_path, "cooperative copy target", include_leaf=True)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise WriterIsolationError("cooperative copy byte capacity is invalid")
    source_identity = _lstat_directory(source_path, "cooperative copy source")
    before = _describe(source_path, limit=limit, exclude_git=exclude_git)
    if before["kind"] != "directory":
        raise WriterIsolationUnsupported("cooperative copy source must be a directory")
    if _lstat(target_path, "cooperative copy target", missing=True) is not None:
        raise WriterIsolationError("cooperative copy target already exists")
    _ensure_parent(target_path)
    owned = ownership if ownership is not None else _OwnedCopyTree()
    try:
        target_identity = _make_directory(
            target_path,
            "cooperative copy target",
            mode=stat.S_IMODE(source_identity.st_mode),
            require_new=True,
        )
        owned.add_directory(target_path, target_identity)
        stack: list[tuple[Path, Path, os.stat_result]] = [
            (source_path, target_path, target_identity)
        ]
        copied_files = 0
        copied_bytes = 0
        while stack:
            source_dir, target_dir, expected_target_dir = stack.pop()
            _lstat_directory(source_dir, "cooperative copy source")
            current_target_dir = _lstat_directory(
                target_dir, "cooperative copy target"
            )
            if not _same_open_object(expected_target_dir, current_target_dir):
                raise WriterIsolationError(
                    "cooperative copy target directory changed during materialization"
                )
            for child, name, child_stat in _scan_directory(
                source_dir, "cooperative copy source"
            ):
                if exclude_git and name.casefold() == ".git":
                    continue
                target_child = target_dir / name
                if _is_reparse(child_stat):
                    raise WriterIsolationUnsupported(
                        "cooperative copy cannot include a reparse point"
                    )
                copied_files += 1
                if copied_files > MAX_FILES:
                    raise WriterIsolationUnsupported(
                        "cooperative isolation exceeds its fixed file budget"
                    )
                if group_budget is not None:
                    group_budget.consume_file()
                if stat.S_ISDIR(child_stat.st_mode):
                    target_child_identity = _make_directory(
                        target_child,
                        "cooperative copy target",
                        mode=stat.S_IMODE(child_stat.st_mode),
                        require_new=True,
                    )
                    owned.add_directory(target_child, target_child_identity)
                    stack.append((child, target_child, target_child_identity))
                    continue
                if not stat.S_ISREG(child_stat.st_mode):
                    raise WriterIsolationUnsupported(
                        "cooperative copy has an unsupported entry"
                    )
                copied_file = _copy_file(
                    child,
                    target_child,
                    limit=min(
                        limit - copied_bytes,
                        group_budget.remaining_bytes()
                        if group_budget is not None
                        else limit - copied_bytes,
                    ),
                    ownership=owned,
                )
                copied_bytes += copied_file
                if copied_bytes > limit:
                    raise WriterIsolationUnsupported(
                        "cooperative isolation exceeds its fixed byte budget"
                    )
                if group_budget is not None:
                    group_budget.consume_bytes(copied_file)
        after = _describe(source_path, limit=limit, exclude_git=exclude_git)
        copied = _describe(target_path, limit=limit, exclude_git=exclude_git)
        if (
            not _same_node(
                source_identity,
                _lstat_directory(source_path, "cooperative copy source"),
            )
            or after["content_id"] != before["content_id"]
            or copied["content_id"] != before["content_id"]
        ):
            raise WriterIsolationError("cooperative copy source changed during materialization")
        return int(before["files"]), int(before["bytes"])
    except BaseException:
        owned.cleanup()
        raise


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    remaining = remaining_seconds()
    timeout = 20.0 if remaining is None else min(20.0, remaining)
    try:
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout,
            tempfile.TemporaryFile(mode="w+b") as stderr,
        ):
            completed = subprocess.run(
                ["git", "-c", "core.longpaths=true", "-C", str(root), *args],
                check=False,
                env=clean_git_environment(),
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
            )
            stdout_size = os.fstat(stdout.fileno()).st_size
            stderr_size = os.fstat(stderr.fileno()).st_size
            if stdout_size + stderr_size > MAX_GIT_OUTPUT_BYTES:
                raise WriterIsolationUnavailable(
                    "Git cooperative isolation exceeds its output capacity"
                )
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(
                completed.args,
                completed.returncode,
                stdout=stdout.read(MAX_GIT_OUTPUT_BYTES + 1),
                stderr=stderr.read(MAX_GIT_OUTPUT_BYTES + 1),
            )
    except subprocess.TimeoutExpired as error:
        checkpoint()
        raise WriterIsolationUnavailable("Git cooperative isolation timed out") from error
    except WriterIsolationUnavailable:
        raise
    except OSError as error:
        raise WriterIsolationUnavailable("Git cooperative isolation is unavailable") from error


def _clean_git_workspace(
    root: Path,
    scopes: Iterable[Mapping[str, str]],
) -> bool:
    # A detached worktree cannot materialize ignored content. Check only the
    # declared writer union, and select the bounded-copy path when any tracked,
    # untracked, or ignored content there differs from HEAD. `normal` and
    # `matching` collapse untracked/ignored directories instead of recursively
    # enumerating every child; any record is enough for this decision.
    pathspecs = [
        f":(top,literal){scope['path']}"
        for scope in sorted(scopes, key=lambda item: (item["path"], item["kind"]))
    ]
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
        "--ignored=matching",
        "--",
        *pathspecs,
    )
    if status.returncode:
        raise WriterIsolationUnavailable("Git workspace cleanliness is unavailable")
    head = _git(root, "rev-parse", "--verify", "HEAD")
    return not status.stdout and head.returncode == 0


def _same_location(left: Path, right: Path, label: str) -> None:
    try:
        if not os.path.samefile(left, right):
            raise WriterIsolationError(f"{label} does not match its AOG namespace")
    except FileNotFoundError as error:
        raise WriterIsolationError(f"{label} is missing") from error
    except OSError as error:
        raise WriterIsolationUnavailable(f"{label} cannot be compared") from error


def _record_layout(
    record: Mapping[str, Any],
    state_root: Path,
    *,
    terminal_recovery: bool,
) -> tuple[Path, Path]:
    recovery = record.get("recovery")
    if not isinstance(recovery, Mapping) or set(recovery) != {"batch_id", "canonical_root"}:
        raise WriterIsolationError("cooperative isolate recovery identity is invalid")
    batch_id = _require_sha256(recovery.get("batch_id"), "cooperative batch identity")
    canonical_text = recovery.get("canonical_root")
    if not isinstance(canonical_text, str) or not Path(canonical_text).is_absolute():
        raise WriterIsolationError("cooperative isolate canonical recovery root is invalid")
    canonical = _absolute(canonical_text)
    namespace = _existing_namespace(
        state_root,
        "isolates",
        canonical_root=canonical,
        terminal_recovery=terminal_recovery,
    )
    isolate_text = record.get("isolate_root")
    if not isinstance(isolate_text, str) or not Path(isolate_text).is_absolute():
        raise WriterIsolationError("cooperative isolate root is invalid")
    isolate = _absolute(isolate_text)
    slot_match = _SLOT_RE.fullmatch(isolate.name)
    if slot_match is None or int(slot_match.group("index")) >= MAX_ISOLATE_ROOTS:
        raise WriterIsolationError("cooperative isolate slot is invalid")
    batch_dir = isolate.parent
    session_dir = batch_dir.parent
    root_dir = session_dir.parent
    if _require_layout_digest(
        batch_dir.name, "cooperative isolate batch layout"
    ) != _layout_digest(batch_id):
        raise WriterIsolationError("cooperative isolate batch does not match recovery identity")
    _require_layout_digest(session_dir.name, "cooperative isolate session layout")
    if _require_canonical_layout_digest(
        root_dir.name, "cooperative isolate canonical layout"
    ) != _canonical_layout_digest(canonical):
        raise WriterIsolationError(
            "cooperative isolate canonical layout does not match recovery identity"
        )
    if namespace is None:
        if _lstat(isolate, "cooperative isolate root", missing=True) is not None:
            raise WriterIsolationError(
                "cooperative isolate namespace is missing for a materialized root"
            )
    else:
        _same_location(root_dir.parent, namespace, "cooperative isolate namespace")
    return isolate, canonical


def _owned_isolate_source(
    state_root: Path,
    canonical_root: Path,
    source_root: Path,
) -> bool:
    """Accept the one intentional state-root/source relationship.

    Apply sources normally live in AOG's isolate namespace, which is below the
    state root.  No arbitrary source below (or above) that root is safe: only
    the marker-validated ``digest/session/batch/nXX`` shape is admitted.
    """

    source = _absolute(source_root)
    _assert_real_ancestors(source, "cooperative source root", include_leaf=True)
    if (
        _lstat(
            _absolute(state_root) / "isolates",
            "AOG isolate namespace",
            missing=True,
        )
        is None
    ):
        return False
    namespace = _existing_namespace(
        state_root,
        "isolates",
        canonical_root=canonical_root,
        terminal_recovery=False,
    )
    assert namespace is not None
    slot = _SLOT_RE.fullmatch(source.name)
    if slot is None or int(slot.group("index")) >= MAX_ISOLATE_ROOTS:
        return False
    batch = source.parent
    session = batch.parent
    root_partition = session.parent
    if (
        _LAYOUT_DIGEST_RE.fullmatch(batch.name) is None
        or _LAYOUT_DIGEST_RE.fullmatch(session.name) is None
        or _ROOT_LAYOUT_DIGEST_RE.fullmatch(root_partition.name) is None
        or root_partition.name != _canonical_layout_digest(canonical_root)
    ):
        return False
    try:
        _same_location(
            root_partition.parent,
            namespace,
            "cooperative isolate namespace",
        )
    except WriterIsolationError:
        return False
    return True


def _assert_apply_source_safe(
    state_root: Path,
    canonical_root: Path,
    source_root: Path,
) -> None:
    source = _absolute(source_root)
    _assert_real_ancestors(source, "cooperative source root", include_leaf=True)
    if not _resolved_overlap(_absolute(state_root), source):
        return
    if _owned_isolate_source(state_root, canonical_root, source):
        return
    raise WriterIsolationError(
        "AOG state root cannot be inside, above, or aliased to an unowned source"
    )


def _worktree_registered(canonical: Path, isolate: Path) -> bool:
    listing = _git(canonical, "worktree", "list", "--porcelain")
    if listing.returncode:
        raise WriterIsolationUnavailable("Git worktree registry is unavailable")
    wanted = _path_key(isolate)
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree ") and _path_key(Path(line[9:])) == wanted:
            return True
    return False


def _linked_worktree_canonical(isolate: Path) -> Path:
    """Recover the primary worktree before removing an orphan linked root."""

    listing = _git(isolate, "worktree", "list", "--porcelain")
    if listing.returncode:
        raise WriterIsolationUnavailable("Git worktree registry is unavailable")
    primary: Path | None = None
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            primary = _absolute(Path(line[9:]))
            break
    if primary is None or _path_key(primary) == _path_key(isolate):
        raise WriterIsolationError("Git orphan worktree has no primary workspace")
    _assert_real_ancestors(primary, "canonical workspace root", include_leaf=True)
    _lstat_directory(primary, "canonical workspace root")
    return primary


def _remove_worktree(canonical: Path, isolate: Path, *, terminal_recovery: bool) -> None:
    """Remove/prune the registry before raw deletion, including add failures."""

    root = _absolute(canonical)
    if _path_key(root) == _path_key(isolate):
        root = _linked_worktree_canonical(isolate)
    _lstat_directory(root, "canonical workspace root")
    removed = _git(root, "worktree", "remove", "--force", str(isolate))
    pruned = _git(root, "worktree", "prune")
    if pruned.returncode:
        raise WriterIsolationUnavailable("Git worktree prune failed during cooperative cleanup")
    if _worktree_registered(root, isolate):
        detail = removed.stderr.decode("utf-8", "replace").strip()
        raise WriterIsolationError(
            "Git worktree remains registered during cooperative cleanup"
            + (": " + detail if detail else "")
        )
    _remove_tree(isolate, terminal_recovery=terminal_recovery)


def _cleanup_preparing_worktree(canonical: Path, isolate: Path) -> None:
    """Reclaim only an unmodified worktree that never reached a child."""

    try:
        status = _git(
            isolate,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        )
        if status.returncode or status.stdout:
            return
        removed = _git(canonical, "worktree", "remove", str(isolate))
        if removed.returncode:
            return
        _git(canonical, "worktree", "prune")
    except BaseException:
        # A failed proof or a cancellation leaves the reservation for terminal
        # recovery; it must not turn into a forced deletion of a replacement.
        return


def _remove_isolate(record: Mapping[str, Any], state_root: Path) -> None:
    isolate, canonical = _record_layout(record, state_root, terminal_recovery=True)
    if record.get("mode") == WORKTREE:
        _remove_worktree(canonical, isolate, terminal_recovery=True)
    elif record.get("mode") == COPY:
        _remove_tree(isolate, terminal_recovery=True)
    else:
        raise WriterIsolationError("cooperative isolate mode is invalid")


def cleanup_isolates(state_root: Path, records: Iterable[Mapping[str, Any]]) -> int:
    removed = 0
    for record in records:
        _remove_isolate(record, state_root)
        removed += 1
    return removed


def preparing_isolate_roots(
    state_root: Path,
    *,
    canonical_root: Path,
    session_id: str,
    batch_id: str,
    count: int,
) -> list[str]:
    """Derive, but never discover, roots protected by a preparation lease."""

    if not isinstance(session_id, str) or not session_id:
        raise WriterIsolationError("cooperative preparation session is invalid")
    if not 1 <= count <= MAX_ISOLATE_ROOTS:
        raise WriterIsolationError("cooperative preparation member count is invalid")
    canonical = _absolute(canonical_root)
    _assert_state_root_separate(state_root, canonical)
    batch = _require_sha256(batch_id, "cooperative preparation batch")
    base = (
        _absolute(state_root)
        / "isolates"
        / _canonical_layout_digest(canonical)
        / _layout_digest(session_id)
        / _layout_digest(batch)
    )
    return [str(base / f"n{index:02d}") for index in range(count)]


def cleanup_preparing_isolates(
    state_root: Path,
    *,
    canonical_root: Path,
    session_id: str,
    batch_id: str,
    backend: str,
    count: int,
) -> int:
    """Recover a reservation that died before records could be published."""

    if backend not in {"git", "directory"}:
        raise WriterIsolationError("cooperative preparation backend is invalid")
    canonical = _absolute(canonical_root)
    roots = preparing_isolate_roots(
        state_root,
        canonical_root=canonical,
        session_id=session_id,
        batch_id=batch_id,
        count=count,
    )
    namespace = _existing_namespace(
        state_root,
        "isolates",
        canonical_root=canonical,
        terminal_recovery=True,
    )
    if namespace is None:
        return 0
    base = Path(roots[0]).parent
    # The namespace is marker-validated and every component is deterministic;
    # never accept a caller-supplied containment path during recovery.
    _same_location(base.parent.parent.parent, namespace, "cooperative preparation namespace")
    removed = 0
    for isolate_text in roots:
        isolate = Path(isolate_text)
        if backend == "git":
            _remove_worktree(canonical, isolate, terminal_recovery=True)
        else:
            _remove_tree(isolate, terminal_recovery=True)
        removed += 1
    for directory in (base, base.parent, base.parent.parent):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    return removed


def _active_path_keys(active_roots: Iterable[str]) -> set[str]:
    keys: set[str] = set()
    for root in active_roots:
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise WriterIsolationError("active cooperative root is invalid")
        keys.add(_path_key(root))
    return keys


def cleanup_unused_isolate_batches(
    state_root: Path,
    active_roots: Iterable[str],
    *,
    canonical_root: Path | None = None,
) -> int:
    base = _existing_namespace(
        state_root,
        "isolates",
        canonical_root=canonical_root,
        terminal_recovery=True,
    )
    if base is None:
        return 0
    active = _active_path_keys(active_roots)
    removed = 0
    for root_partition, root_name, root_stat in _scan_directory(
        base, "AOG isolate namespace", require_stable=False
    ):
        if root_name == _MARKER:
            continue
        if _is_reparse(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise WriterIsolationError("AOG isolate namespace is unsafe")
        _require_canonical_layout_digest(
            root_name, "AOG isolate canonical namespace"
        )
        if (
            canonical_root is not None
            and root_name != _canonical_layout_digest(canonical_root)
        ):
            raise WriterIsolationError(
                "AOG isolate canonical namespace does not match cleanup workspace"
            )
        for session, session_name, session_stat in _scan_directory(
            root_partition,
            "AOG isolate canonical namespace",
            require_stable=False,
        ):
            if _is_reparse(session_stat) or not stat.S_ISDIR(session_stat.st_mode):
                raise WriterIsolationError("AOG isolate session namespace is unsafe")
            _require_layout_digest(session_name, "AOG isolate session namespace")
            for batch, batch_name, batch_stat in _scan_directory(
                session, "AOG isolate session namespace", require_stable=False
            ):
                if _is_reparse(batch_stat) or not stat.S_ISDIR(batch_stat.st_mode):
                    raise WriterIsolationError("AOG isolate batch namespace is unsafe")
                _require_layout_digest(batch_name, "AOG isolate batch namespace")
                for isolate, slot, isolate_stat in _scan_directory(
                    batch, "AOG isolate batch namespace", require_stable=False
                ):
                    match = _SLOT_RE.fullmatch(slot)
                    if (
                        match is None
                        or int(match.group("index")) >= MAX_ISOLATE_ROOTS
                        or _is_reparse(isolate_stat)
                        or not stat.S_ISDIR(isolate_stat.st_mode)
                    ):
                        raise WriterIsolationError("AOG isolate slot namespace is unsafe")
                    if _path_key(isolate) in active:
                        continue
                    git_marker = _lstat(
                        isolate / ".git", "AOG orphan worktree marker", missing=True
                    )
                    if git_marker is not None:
                        if _is_reparse(git_marker) or not stat.S_ISREG(git_marker.st_mode):
                            raise WriterIsolationError("AOG orphan worktree marker is unsafe")
                        # A clean managed worktree has a root .git file; use either
                        # the canonical root known by the caller or the linked
                        # worktree itself to remove/prune the registry before raw
                        # deletion. A copy isolate never carries this marker.
                        cleanup_root = (
                            _absolute(canonical_root)
                            if canonical_root is not None
                            else _linked_worktree_canonical(isolate)
                        )
                        if root_name != _canonical_layout_digest(cleanup_root):
                            raise WriterIsolationError(
                                "AOG orphan worktree canonical layout is invalid"
                            )
                        _remove_worktree(
                            cleanup_root,
                            isolate,
                            terminal_recovery=True,
                        )
                    else:
                        _remove_tree(isolate, terminal_recovery=True)
                    removed += 1
                try:
                    os.rmdir(batch)
                except OSError:
                    pass
            try:
                os.rmdir(session)
            except OSError:
                pass
        try:
            os.rmdir(root_partition)
        except OSError:
            pass
    return removed


def cleanup_unused_journal_batches(
    state_root: Path,
    active_roots: Iterable[str],
    *,
    canonical_root: Path | None = None,
) -> int:
    base = _existing_namespace(
        state_root,
        "isolate-journals",
        canonical_root=canonical_root,
        terminal_recovery=True,
    )
    if base is None:
        return 0
    active = _active_path_keys(active_roots)
    removed = 0
    for batch, name, batch_stat in _scan_directory(
        base, "AOG isolate journal namespace", require_stable=False
    ):
        if name == _MARKER:
            continue
        if _is_reparse(batch_stat) or not stat.S_ISDIR(batch_stat.st_mode):
            raise WriterIsolationError("AOG isolate journal namespace is unsafe")
        _require_layout_digest(name, "AOG isolate journal batch")
        if _path_key(batch) in active:
            continue
        _remove_tree(batch, terminal_recovery=True)
        removed += 1
    return removed


def _new_record(
    canonical: Path,
    batch_id: str,
    isolate: Path,
    mode: str,
    scopes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "isolate_root": str(isolate),
        "mode": mode,
        "recovery": {"batch_id": batch_id, "canonical_root": str(canonical)},
        "scopes": [dict(scope) for scope in scopes],
    }


def _normalize_isolation_scopes(value: object, label: str) -> list[dict[str, str]]:
    """Reject empty or ambiguous typed scopes before isolate materialization."""

    if not isinstance(value, list) or not value:
        raise WriterIsolationError(f"{label} scopes are invalid")
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, str]] = []
    for raw_scope in value:
        if not isinstance(raw_scope, Mapping) or set(raw_scope) != {"kind", "path"}:
            raise WriterIsolationError(f"{label} scope is invalid")
        kind = raw_scope.get("kind")
        if kind not in {"exact", "prefix"}:
            raise WriterIsolationError(f"{label} scope is invalid")
        try:
            path = require_repository_path(raw_scope.get("path"), f"{label} scope")
        except ProtocolHashError as error:
            raise WriterIsolationError(str(error)) from error
        identity = (str(kind), path)
        if identity in seen:
            raise WriterIsolationError(f"{label} scope is ambiguous")
        seen.add(identity)
        normalized.append({"kind": str(kind), "path": path})
    normalized.sort(key=lambda item: (item["kind"], item["path"]))
    return normalized


def prepare_isolates(
    state_root: Path,
    canonical_root: Path,
    *,
    backend: str,
    session_id: str,
    batch_id: str,
    members: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Create one bounded cooperative writer group before child admission."""

    if backend not in {"git", "directory"}:
        raise WriterIsolationUnsupported("cooperative workspace backend is unsupported")
    if not isinstance(session_id, str) or not session_id:
        raise WriterIsolationError("cooperative session identity is invalid")
    if not 1 <= len(members) <= MAX_GROUP_SIZE or len(members) > MAX_ISOLATE_ROOTS:
        raise WriterIsolationError("cooperative writer group size is invalid")
    prepared_members: list[dict[str, Any]] = []
    for member in members:
        if not isinstance(member, Mapping):
            raise WriterIsolationError("cooperative member identity is invalid")
        member_id = member.get("id")
        if not isinstance(member_id, str) or not member_id:
            raise WriterIsolationError("cooperative member identity is invalid")
        prepared_members.append(
            {
                "id": member_id,
                "scopes": _normalize_isolation_scopes(
                    member.get("scopes"), "cooperative member"
                ),
            }
        )
    canonical = _absolute(canonical_root)
    _assert_state_root_separate(state_root, canonical)
    _lstat_directory(canonical, "canonical workspace root")
    writer_scopes = [
        scope
        for member in prepared_members
        for scope in member["scopes"]
    ]
    mode = (
        WORKTREE
        if backend == "git" and _clean_git_workspace(canonical, writer_scopes)
        else COPY
    )
    copy_budget: _GroupCopyBudget | None = None
    if mode == COPY:
        source_files, source_bytes = _walk_visible(canonical, exclude_git=True)
        if (
            source_files * len(prepared_members) > MAX_GROUP_FILES
            or source_bytes * len(prepared_members) > MAX_GROUP_BYTES
        ):
            raise WriterIsolationError(
                "cooperative writer group exceeds its aggregate copy capacity"
            )
        copy_budget = _GroupCopyBudget()
    namespace = _namespace_base(state_root, "isolates", canonical_root=canonical)
    session = _layout_digest(session_id)
    batch = _layout_digest(
        _require_sha256(batch_id, "cooperative batch identity")
    )
    root_partition = namespace / _canonical_layout_digest(canonical)
    _make_directory(root_partition, "AOG isolate canonical namespace")
    session_root = root_partition / session
    _make_directory(session_root, "AOG isolate session namespace")
    base = session_root / batch
    if _lstat(base, "AOG isolate batch namespace", missing=True) is not None:
        raise WriterIsolationError("cooperative isolate batch already exists")
    _make_directory(base, "AOG isolate batch namespace")
    records: list[dict[str, Any]] = []
    prepared: list[tuple[dict[str, Any], _OwnedCopyTree | None]] = []
    current_isolate: Path | None = None
    current_ownership: _OwnedCopyTree | None = None
    try:
        for index, member in enumerate(prepared_members):
            member_id = member.get("id")
            scopes = member.get("scopes")
            if not isinstance(member_id, str) or not isinstance(scopes, list):
                raise WriterIsolationError("cooperative member identity is invalid")
            isolate = base / f"n{index:02d}"
            current_isolate = isolate
            record = _new_record(canonical, batch_id, isolate, mode, scopes)
            if mode == WORKTREE:
                result = _git(canonical, "worktree", "add", "--detach", str(isolate), "HEAD")
                if result.returncode:
                    _cleanup_preparing_worktree(canonical, isolate)
                    raise WriterIsolationUnsupported(
                        "clean Git workspace cannot create a detached managed worktree"
                    )
            else:
                current_ownership = _OwnedCopyTree()
                _copy_tree(
                    canonical,
                    isolate,
                    exclude_git=True,
                    group_budget=copy_budget,
                    ownership=current_ownership,
                )
            records.append(record)
            prepared.append((record, current_ownership))
            current_isolate = None
            current_ownership = None
    except BaseException:
        for record, ownership in reversed(prepared):
            try:
                if ownership is not None:
                    ownership.cleanup()
                else:
                    _cleanup_preparing_worktree(canonical, Path(record["isolate_root"]))
            except BaseException:
                pass
        if current_isolate is not None:
            try:
                if mode == WORKTREE:
                    _cleanup_preparing_worktree(canonical, current_isolate)
                elif current_ownership is not None:
                    current_ownership.cleanup()
            except BaseException:
                pass
        # All materialized children have either been reclaimed through a
        # matching ownership claim or intentionally left fenced.  Only remove
        # deterministic namespace directories when they are now empty.
        for directory in (base, session_root, root_partition):
            try:
                os.rmdir(directory)
            except BaseException:
                pass
        raise
    return records


def validate_record(
    record: object,
    state_root: Path,
    *,
    terminal_recovery: bool = False,
) -> dict[str, Any]:
    required = {"isolate_root", "mode", "recovery", "scopes"}
    if not isinstance(record, Mapping) or set(record) != required:
        raise WriterIsolationError("cooperative isolate record is malformed")
    if record.get("mode") not in {WORKTREE, COPY}:
        raise WriterIsolationError("cooperative isolate mode is invalid")
    normalized_scopes = _normalize_isolation_scopes(
        record.get("scopes"), "cooperative isolate"
    )
    isolate, _canonical = _record_layout(record, state_root, terminal_recovery=terminal_recovery)
    metadata = _lstat(isolate, "cooperative isolate root", missing=True)
    if metadata is None:
        if not terminal_recovery:
            raise WriterIsolationError("cooperative isolate root is missing")
    elif _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise WriterIsolationError("cooperative isolate root is unsafe")
    return {
        "isolate_root": str(isolate),
        "mode": str(record["mode"]),
        "recovery": dict(record["recovery"]),
        "scopes": normalized_scopes,
    }


def verify_canonical(
    canonical_root: Path,
    baseline: object,
    *,
    scope: object,
) -> dict[str, Any]:
    try:
        return verify_workspace(
            canonical_root,
            baseline,
            allowed_scopes=[],
            owner_scopes=scope,
        )
    except WorkspaceGuardUnavailable as error:
        raise WriterIsolationUnavailable(str(error)) from error
    except WorkspaceGuardError as error:
        raise WriterIsolationError("canonical workspace drift: " + str(error)) from error


def verify_isolate(
    record: object,
    state_root: Path,
    baseline: object | None = None,
) -> dict[str, Any]:
    bound = validate_record(record, state_root)
    if baseline is None:
        raise WriterIsolationError("cooperative isolate snapshot is unavailable")
    active_baseline = baseline
    try:
        return verify_workspace(
            Path(bound["isolate_root"]),
            active_baseline,
            allowed_scopes=bound["scopes"],
            owner_scopes=bound["scopes"],
        )
    except WorkspaceGuardUnavailable as error:
        raise WriterIsolationUnavailable(str(error)) from error
    except WorkspaceGuardError as error:
        raise WriterIsolationError("isolate verification failed: " + str(error)) from error


def scoped_content_identity(root: Path, scopes: object) -> str:
    workspace = _absolute(root)
    _lstat_directory(workspace, "cooperative comparison root")
    normalized_scopes = _normalize_isolation_scopes(
        scopes, "cooperative comparison"
    )
    entries: list[dict[str, str]] = []
    for scope in normalized_scopes:
        relative = scope["path"]
        description = _describe(
            _require_child(workspace, relative, "cooperative comparison path"),
            limit=MAX_BYTES,
        )
        entries.append(
            {
                "content_id": str(description["content_id"]),
                "kind": scope["kind"],
                "path": relative,
            }
        )
    entries.sort(key=lambda item: (item["path"], item["kind"]))
    return _identity({"entries": entries, "protocol": PROTOCOL})


def _copy_node(source: Path, target: Path, *, limit: int = MAX_JOURNAL_BYTES) -> None:
    description = _describe(source, limit=limit)
    if description["kind"] == "missing":
        return
    if description["kind"] == "file":
        _copy_file(
            source,
            target,
            limit=limit,
            ownership=_ACTIVE_COPY_OWNERSHIP.get(),
        )
        return
    _copy_tree(
        source,
        target,
        limit=limit,
        ownership=_ACTIVE_COPY_OWNERSHIP.get(),
    )


def _remove_node(path: Path) -> None:
    _assert_real_ancestors(path, "cooperative apply target", include_leaf=True)
    metadata = _lstat(path, "cooperative apply target", missing=True)
    if metadata is None:
        return
    if _is_reparse(metadata):
        raise WriterIsolationError("cooperative apply target became a reparse point")
    if stat.S_ISREG(metadata.st_mode):
        try:
            os.unlink(path)
        except OSError as error:
            raise WriterIsolationUnavailable("cooperative apply target cannot be removed") from error
    elif stat.S_ISDIR(metadata.st_mode):
        _remove_tree(path)
    else:
        raise WriterIsolationError("cooperative apply target is unsupported")


def _cleanup_staged_backups(
    backup_root: Path,
    root_identity: os.stat_result,
    claims: Iterable[tuple[Path, Mapping[str, Any], os.stat_result]],
) -> None:
    """Best-effort staging cleanup that cannot adopt a raced backup path."""

    for backup, expected, identity in reversed(tuple(claims)):
        try:
            kind = expected.get("kind")
            if kind == "file":
                _discard_owned_copy(
                    backup,
                    identity,
                    size=int(expected["bytes"]),
                    digest=str(expected["sha256"]),
                    mode=int(expected["mode"]),
                )
            elif kind == "directory":
                current = _lstat(backup, "cooperative apply backup", missing=True)
                if (
                    current is not None
                    and not _is_reparse(current)
                    and stat.S_ISDIR(current.st_mode)
                    and _same_open_object(current, identity)
                    and _content_matches(backup, expected)
                ):
                    _remove_tree(backup, allow_missing=True)
        except BaseException:
            # Preserve the preparation/staging failure. A nonmatching backup
            # stays inside the marker-validated journal namespace for recovery.
            pass
    try:
        current_root = _lstat(
            backup_root, "cooperative apply journal", missing=True
        )
        if (
            current_root is not None
            and not _is_reparse(current_root)
            and stat.S_ISDIR(current_root.st_mode)
            and _same_open_object(current_root, root_identity)
        ):
            os.rmdir(backup_root)
    except BaseException:
        pass


def stage_apply_journal(
    state_root: Path,
    *,
    wave_id: str,
    canonical_root: Path,
    changes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not 1 <= len(changes) <= MAX_JOURNAL_ENTRIES:
        raise WriterIsolationError("cooperative apply journal entry capacity is invalid")
    canonical = _absolute(canonical_root)
    _assert_state_root_separate(state_root, canonical)
    _lstat_directory(canonical, "canonical workspace root")
    wave = _layout_digest(_require_sha256(wave_id, "cooperative wave identity"))
    backup_root = _namespace_base(
        state_root, "isolate-journals", canonical_root=canonical
    ) / wave
    if _lstat(backup_root, "cooperative apply journal", missing=True) is not None:
        raise WriterIsolationError("cooperative apply journal already exists")
    backup_root_identity = _make_directory(
        backup_root,
        "cooperative apply journal",
        require_new=True,
    )
    paths: list[str] = []
    for raw_path in sorted(changes):
        try:
            path = require_repository_path(raw_path, "cooperative changed path")
        except ProtocolHashError as error:
            raise WriterIsolationError(str(error)) from error
        if not any(path == parent or path.startswith(parent + "/") for parent in paths):
            paths.append(path)
    entries: list[dict[str, Any]] = []
    backup_claims: list[tuple[Path, Mapping[str, Any], os.stat_result]] = []
    backup_ownership = _OwnedCopyTree()
    total_bytes = 0
    total_files = 0
    try:
        with _copy_ownership(backup_ownership):
            for index, relative in enumerate(paths):
                change = changes[relative]
                source_root_value = change.get("source_root")
                if not isinstance(source_root_value, str) or not Path(source_root_value).is_absolute():
                    raise WriterIsolationError("cooperative source root is invalid")
                source_root = _absolute(source_root_value)
                _lstat_directory(source_root, "cooperative source root")
                _assert_apply_source_safe(state_root, canonical, source_root)
                source = _require_child(source_root, relative, "cooperative source path")
                target = _require_child(canonical, relative, "cooperative canonical path")
                before = _describe(target, limit=MAX_JOURNAL_BYTES - total_bytes)
                after = _describe(source, limit=MAX_JOURNAL_BYTES - total_bytes)
                total_bytes += int(before["bytes"]) + int(after["bytes"])
                total_files += int(before["files"]) + int(after["files"])
                if total_bytes > MAX_JOURNAL_BYTES:
                    raise WriterIsolationError("cooperative apply journal exceeds its byte capacity")
                if total_files > MAX_JOURNAL_FILES:
                    raise WriterIsolationError("cooperative apply journal exceeds its file capacity")
                backup = backup_root / f"b{index:04d}"
                if before["kind"] != "missing":
                    _copy_node(target, backup)
                    backup_identity = _lstat(backup, "cooperative apply backup")
                    assert backup_identity is not None
                    backup_claims.append((backup, before, backup_identity))
                    if _describe(backup, limit=MAX_JOURNAL_BYTES)["content_id"] != before[
                        "content_id"
                    ]:
                        raise WriterIsolationError("cooperative apply backup identity changed")
                    before = {**before, "backup_root": str(backup)}
                else:
                    before = {**before, "backup_root": None}
                entries.append(
                    {
                        "after": after,
                        "before": before,
                        "path": relative,
                        "phase": "staged",
                        "source_root": str(source_root),
                    }
                )
    except BaseException:
        backup_ownership.cleanup()
        _cleanup_staged_backups(backup_root, backup_root_identity, backup_claims)
        raise
    journal = {
        "backup_root": str(backup_root),
        "canonical_root": str(canonical),
        "entries": entries,
        "phase": "staged",
        "protocol": PROTOCOL,
        "wave_id": wave_id,
    }
    if len(canonical_bytes(journal)) > MAX_JOURNAL_BYTES:
        backup_ownership.cleanup()
        _cleanup_staged_backups(backup_root, backup_root_identity, backup_claims)
        raise WriterIsolationError("cooperative lifecycle journal exceeds its byte capacity")
    return journal


def _validate_content(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("kind") not in {
        "missing",
        "file",
        "directory",
    }:
        raise WriterIsolationError(f"{label} is invalid")
    _require_sha256(value.get("content_id"), label)
    if (
        isinstance(value.get("bytes"), bool)
        or not isinstance(value.get("bytes"), int)
        or value["bytes"] < 0
        or isinstance(value.get("files"), bool)
        or not isinstance(value.get("files"), int)
        or value["files"] < 0
    ):
        raise WriterIsolationError(f"{label} is invalid")
    return dict(value)


def validate_journal(journal: object, state_root: Path) -> dict[str, Any]:
    required = {"backup_root", "canonical_root", "entries", "phase", "protocol", "wave_id"}
    if not isinstance(journal, Mapping) or set(journal) != required:
        raise WriterIsolationError("cooperative apply journal is malformed")
    if journal.get("protocol") != PROTOCOL or journal.get("phase") not in {
        "staged",
        "applying",
        "applied",
        "rolling_back",
        "rolled_back",
        "recovery_required",
    }:
        raise WriterIsolationError("cooperative apply journal phase is invalid")
    canonical_text = journal.get("canonical_root")
    if not isinstance(canonical_text, str) or not Path(canonical_text).is_absolute():
        raise WriterIsolationError("cooperative journal canonical root is invalid")
    canonical = _absolute(canonical_text)
    base = _existing_namespace(
        state_root,
        "isolate-journals",
        canonical_root=canonical,
        terminal_recovery=journal.get("phase") in {"rolled_back", "applied"},
    )
    backup_text = journal.get("backup_root")
    if not isinstance(backup_text, str) or not Path(backup_text).is_absolute():
        raise WriterIsolationError("cooperative apply backup is invalid")
    backup = _absolute(backup_text)
    wave_id = _require_sha256(journal.get("wave_id"), "cooperative journal wave identity")
    if _require_layout_digest(
        backup.name, "cooperative journal backup layout"
    ) != _layout_digest(wave_id):
        raise WriterIsolationError("cooperative journal backup does not match its wave")
    if base is None:
        if _lstat(backup, "cooperative apply backup", missing=True) is not None:
            raise WriterIsolationError(
                "cooperative journal namespace is missing for a materialized backup"
            )
    else:
        _same_location(backup.parent, base, "cooperative journal namespace")
    if not isinstance(journal.get("entries"), list) or not 1 <= len(journal["entries"]) <= MAX_JOURNAL_ENTRIES:
        raise WriterIsolationError("cooperative apply journal entries are invalid")
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    total = 0
    files = 0
    for entry in journal["entries"]:
        if not isinstance(entry, Mapping) or set(entry) != {
            "after",
            "before",
            "path",
            "phase",
            "source_root",
        }:
            raise WriterIsolationError("cooperative apply journal entry is malformed")
        if entry.get("phase") not in {"staged", "applying", "applied"}:
            raise WriterIsolationError("cooperative apply journal entry phase is invalid")
        try:
            relative = require_repository_path(entry.get("path"), "cooperative journal path")
        except ProtocolHashError as error:
            raise WriterIsolationError(str(error)) from error
        if relative in seen:
            raise WriterIsolationError("cooperative apply journal paths are duplicated")
        seen.add(relative)
        source_root_text = entry.get("source_root")
        if not isinstance(source_root_text, str) or not Path(source_root_text).is_absolute():
            raise WriterIsolationError("cooperative journal source root is invalid")
        before = _validate_content(entry.get("before"), "cooperative journal before identity")
        after = _validate_content(entry.get("after"), "cooperative journal after identity")
        total += int(before["bytes"]) + int(after["bytes"])
        files += int(before["files"]) + int(after["files"])
        backup_root = before.get("backup_root")
        if before["kind"] == "missing":
            if backup_root is not None:
                raise WriterIsolationError("cooperative missing backup is invalid")
        elif not isinstance(backup_root, str) or not Path(backup_root).is_absolute():
            raise WriterIsolationError("cooperative backup path is invalid")
        else:
            candidate = _absolute(backup_root)
            if not candidate.name.startswith("b") or candidate.parent != backup:
                raise WriterIsolationError("cooperative backup layout is invalid")
        entries.append(
            {
                "after": after,
                "before": before,
                "path": relative,
                "phase": str(entry["phase"]),
                "source_root": str(_absolute(source_root_text)),
            }
        )
    if total > MAX_JOURNAL_BYTES:
        raise WriterIsolationError("cooperative apply journal exceeds its byte capacity")
    if files > MAX_JOURNAL_FILES:
        raise WriterIsolationError("cooperative apply journal exceeds its file capacity")
    return {
        "backup_root": str(backup),
        "canonical_root": str(canonical),
        "entries": entries,
        "phase": str(journal["phase"]),
        "protocol": PROTOCOL,
        "wave_id": wave_id,
    }


def _content_matches(path: Path, expected: Mapping[str, Any]) -> bool:
    return _describe(path, limit=MAX_JOURNAL_BYTES).get("content_id") == expected.get(
        "content_id"
    )


def _persist_entry_phase(journal: object, path: str, phase: str) -> None:
    """Mirror bounded working-journal progress into the caller's durable object."""

    if not isinstance(journal, dict):
        return
    entries = journal.get("entries")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path") == path:
            entry["phase"] = phase
            return


def _verify_ready_isolates(
    value: Iterable[Mapping[str, Any]] | None,
    state_root: Path,
) -> None:
    """Recheck persisted ready snapshots immediately before each mutation."""

    if value is None:
        return
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "baseline",
            "ready_state",
            "record",
        }:
            raise WriterIsolationError("cooperative ready isolate proof is invalid")
        record = item.get("record")
        baseline = item.get("baseline")
        ready_state = item.get("ready_state")
        if not isinstance(record, Mapping) or not isinstance(baseline, Mapping):
            raise WriterIsolationError("cooperative ready isolate proof is invalid")
        if not isinstance(ready_state, str) or _require_sha256(
            ready_state, "cooperative ready isolate state"
        ) != ready_state:
            raise WriterIsolationError("cooperative ready isolate proof is invalid")
        bound = validate_record(record, state_root)
        root = bound["isolate_root"]
        if root in seen:
            raise WriterIsolationError("cooperative ready isolate proof is ambiguous")
        seen.add(root)
        verification = verify_isolate(record, state_root, baseline)
        if verification.get("current_state") != ready_state:
            raise WriterIsolationError(
                "cooperative isolate changed after its ready snapshot"
            )


def apply_journal(
    journal: object,
    state_root: Path,
    *,
    canonical_identity: str | None = None,
    canonical_scopes: object | None = None,
    progress: Callable[[], None] | None = None,
    ready_isolates: Iterable[Mapping[str, Any]] | None = None,
) -> None:
    """Apply pre-staged content with per-entry canonical/source CAS checks."""

    bound = validate_journal(journal, state_root)
    if ready_isolates is None:
        ready_proofs: tuple[Mapping[str, Any], ...] | None = None
    else:
        try:
            ready_proofs = tuple(ready_isolates)
        except TypeError as error:
            raise WriterIsolationError("cooperative ready isolate proof is invalid") from error
        if not 1 <= len(ready_proofs) <= MAX_GROUP_SIZE:
            raise WriterIsolationError("cooperative ready isolate proof is invalid")
    canonical = Path(bound["canonical_root"])
    if (canonical_identity is None) != (canonical_scopes is None):
        raise WriterIsolationError("cooperative canonical CAS proof is incomplete")
    expected_canonical_identity = canonical_identity
    if expected_canonical_identity is not None:
        _require_sha256(
            expected_canonical_identity,
            "cooperative canonical CAS identity",
        )
        # Syntax and bounded traversal are checked once here, then before and
        # after every canonical mutation below.
        if not isinstance(canonical_scopes, list) or not canonical_scopes:
            raise WriterIsolationError("cooperative canonical CAS scopes are invalid")
    for entry in bound["entries"]:
        target = _require_child(canonical, entry["path"], "cooperative canonical path")
        source = _require_child(
            Path(entry["source_root"]), entry["path"], "cooperative source path"
        )
        if entry["phase"] == "applied":
            continue
        entry["phase"] = "applying"
        _persist_entry_phase(journal, entry["path"], "applying")
        if progress is not None:
            progress()
        if (
            expected_canonical_identity is not None
            and scoped_content_identity(canonical, canonical_scopes)
            != expected_canonical_identity
        ):
            raise WriterIsolationError("canonical workspace changed before cooperative apply")
        if not _content_matches(target, entry["before"]):
            raise WriterIsolationError("canonical content changed before cooperative apply")
        if not _content_matches(source, entry["after"]):
            raise WriterIsolationError("isolate content changed before cooperative apply")
        _verify_ready_isolates(ready_proofs, state_root)
        if (
            expected_canonical_identity is not None
            and scoped_content_identity(canonical, canonical_scopes)
            != expected_canonical_identity
        ):
            raise WriterIsolationError("canonical workspace changed before cooperative apply")
        # The full-isolate check can take longer than a file description.  Bind
        # both operands again at the actual mutation boundary so a canonical or
        # isolate race during that verification cannot be overwritten.
        if not _content_matches(target, entry["before"]):
            raise WriterIsolationError("canonical content changed before cooperative apply")
        if not _content_matches(source, entry["after"]):
            raise WriterIsolationError("isolate content changed before cooperative apply")
        _remove_node(target)
        _copy_node(source, target)
        if not _content_matches(target, entry["after"]):
            raise WriterIsolationError("cooperative canonical content changed during apply")
        if expected_canonical_identity is not None:
            expected_canonical_identity = scoped_content_identity(
                canonical, canonical_scopes
            )
        entry["phase"] = "applied"
        _persist_entry_phase(journal, entry["path"], "applied")
        if progress is not None:
            progress()


def rollback_journal(journal: object, state_root: Path) -> None:
    """Restore only targets still carrying AOG's applied identity."""

    bound = validate_journal(journal, state_root)
    canonical = Path(bound["canonical_root"])
    for entry in reversed(bound["entries"]):
        if entry["phase"] == "staged":
            continue
        target = _require_child(canonical, entry["path"], "cooperative canonical path")
        before = entry["before"]
        after = entry["after"]
        current = _describe(target, limit=MAX_JOURNAL_BYTES)
        if current.get("content_id") == before.get("content_id"):
            entry["phase"] = "staged"
            _persist_entry_phase(journal, entry["path"], "staged")
            continue
        # A crash/failure immediately after AOG removes a formerly present
        # target leaves it missing. Restoring the known backup cannot overwrite
        # external content; every other unexpected identity is fenced.
        missing_during_apply = (
            entry["phase"] == "applying"
            and current.get("kind") == "missing"
            and before.get("kind") != "missing"
        )
        if not missing_during_apply and current.get("content_id") != after.get("content_id"):
            raise WriterIsolationError(
                "cooperative apply target changed; refusing to overwrite external edits"
            )
        _remove_node(target)
        if before["kind"] != "missing":
            backup_text = before.get("backup_root")
            assert isinstance(backup_text, str)
            backup = Path(backup_text)
            if not _content_matches(backup, before):
                raise WriterIsolationError("cooperative backup changed before rollback")
            _copy_node(backup, target)
            if not _content_matches(target, before):
                raise WriterIsolationError("cooperative canonical content changed during rollback")
        entry["phase"] = "staged"
        _persist_entry_phase(journal, entry["path"], "staged")


def cleanup_journal(journal: object, state_root: Path) -> int:
    bound = validate_journal(journal, state_root)
    _remove_tree(Path(bound["backup_root"]), terminal_recovery=True)
    return 1
