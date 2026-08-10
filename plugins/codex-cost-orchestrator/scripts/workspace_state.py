#!/usr/bin/env python3
"""Capture and verify a Git workspace delta without mutating Git state."""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator

from operation_deadline import (
    OperationDeadlineExceeded,
    checkpoint,
    remaining_seconds,
)
from protocol_hash import (
    ProtocolHashError,
    parse_repository_scope_text,
    require_repository_scope,
)


SCHEMA = "cco.workspace-state.v4"
WORKSPACE_MODES = frozenset({"light", "strict"})
IGNORED_POLICY_GLOBAL_V1 = "global-v1"
IGNORED_POLICY_SCOPED_READER_V1 = "scoped-reader-v1"
IGNORED_POLICIES = frozenset(
    {IGNORED_POLICY_GLOBAL_V1, IGNORED_POLICY_SCOPED_READER_V1}
)
DEFAULT_IGNORED_MAX_FILES = 10_000
DEFAULT_IGNORED_MAX_BYTES = 256 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_GIT_RECORDS = 200_000
MAX_GIT_CONTROL_ENTRIES = 100_000
GIT_ADMIN_PATHS = (
    "AUTO_MERGE",
    "BISECT_EXPECTED_REV",
    "BISECT_LOG",
    "BISECT_NAMES",
    "BISECT_START",
    "BISECT_TERMS",
    "CHERRY_PICK_HEAD",
    "FETCH_HEAD",
    "HEAD.lock",
    "config.lock",
    "index.lock",
    "MERGE_HEAD",
    "MERGE_MODE",
    "MERGE_MSG",
    "ORIG_HEAD",
    "REBASE_HEAD",
    "REVERT_HEAD",
    "logs",
    "objects/info",
    "packed-refs.lock",
    "rebase-apply",
    "rebase-merge",
    "sequencer",
    "shallow",
    "shallow.lock",
    "refs",
    "worktrees",
)
SNAPSHOT_FIELDS = frozenset(
    {
        "entries",
        "git_admin_sha256",
        "git_config_sha256",
        "git_control_identities",
        "git_info_sha256",
        "head",
        "hooks_sha256",
        "ignored_limits",
        "ignored_mode",
        "index_sha256",
        "refs_sha256",
        "repo_identity",
        "repo_root",
        "schema",
        "state_id",
        "symbolic_head",
        "ignored_policy",
        "ignored_scope_digest",
    }
)


class StateError(Exception):
    pass


class StateUnavailable(StateError):
    """Git or filesystem inspection was temporarily unavailable."""


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    command = ["git", *args]
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryFile(mode="w+b") as output:
            process = subprocess.Popen(
                command,
                cwd=repo,
                env=environment,
                stdout=output,
                stderr=subprocess.DEVNULL,
            )
            try:
                while True:
                    remaining = remaining_seconds()
                    wait_slice = 0.01 if remaining is None else min(0.01, remaining)
                    try:
                        process.wait(timeout=wait_slice)
                    except subprocess.TimeoutExpired:
                        if os.fstat(output.fileno()).st_size > MAX_GIT_OUTPUT_BYTES:
                            process.kill()
                            process.wait()
                            raise StateUnavailable(
                                "Git repository inspection exceeds the "
                                f"{MAX_GIT_OUTPUT_BYTES} output byte limit"
                            )
                        continue
                    break
            except OperationDeadlineExceeded as error:
                process.kill()
                process.wait()
                raise OperationDeadlineExceeded(
                    "Git workspace inspection exceeded the CCO Hook deadline"
                ) from error
            if os.fstat(output.fileno()).st_size > MAX_GIT_OUTPUT_BYTES:
                raise StateUnavailable(
                    "Git repository inspection exceeds the "
                    f"{MAX_GIT_OUTPUT_BYTES} output byte limit"
                )
            output.seek(0)
            raw = output.read(MAX_GIT_OUTPUT_BYTES + 1)
    except (OperationDeadlineExceeded, StateUnavailable):
        raise
    except OSError as error:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise StateUnavailable("Git repository inspection is unavailable") from error
    if len(raw) > MAX_GIT_OUTPUT_BYTES:
        raise StateUnavailable(
            "Git repository inspection exceeds the "
            f"{MAX_GIT_OUTPUT_BYTES} output byte limit"
        )
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=raw,
        stderr=b"",
    )


def git(repo: Path, *args: str) -> bytes:
    result = _run_git(repo, *args)
    if result.returncode:
        raise StateUnavailable("Git repository inspection failed")
    return result.stdout


def _git_records(raw: bytes, separator: bytes, label: str) -> Iterator[bytes]:
    """Yield bounded Git records without materializing a split list."""

    start = 0
    count = 0
    while start <= len(raw):
        checkpoint()
        end = raw.find(separator, start)
        if end < 0:
            record = raw[start:]
            start = len(raw) + 1
        else:
            record = raw[start:end]
            start = end + len(separator)
        if not record:
            continue
        count += 1
        if count > MAX_GIT_RECORDS:
            raise StateUnavailable(
                f"{label} exceeds the {MAX_GIT_RECORDS} record limit"
            )
        yield record


def _assert_git_record_budget(raw: bytes, separator: bytes, label: str) -> None:
    for _record in _git_records(raw, separator, label):
        pass


def decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def repository_root(repo: Path) -> Path:
    candidate = repo.expanduser().resolve()
    output = git(candidate, "rev-parse", "--show-toplevel")
    if not output:
        raise StateUnavailable("--repo must identify a Git work tree")
    return Path(os.fsdecode(output.rstrip(b"\r\n"))).resolve()


def repository_control_path(root: Path, option: str) -> Path:
    output = git(root, "rev-parse", option).rstrip(b"\r\n")
    if not output:
        raise StateUnavailable("Git control path is unavailable")
    path = Path(os.fsdecode(output))
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def repository_git_path(root: Path, name: str) -> Path:
    return repository_git_paths(root, (name,))[name]


def repository_git_paths(root: Path, names: tuple[str, ...]) -> dict[str, Path]:
    arguments: list[str] = ["rev-parse"]
    for name in names:
        arguments.extend(("--git-path", name))
    outputs = git(root, *arguments).splitlines()
    if len(outputs) != len(names) or any(not output for output in outputs):
        raise StateError("Git path is unavailable")
    paths: dict[str, Path] = {}
    for name, output in zip(names, outputs, strict=True):
        path = Path(os.fsdecode(output))
        if not path.is_absolute():
            path = root / path
        paths[name] = Path(os.path.abspath(path))
    return paths


def repository_control_roots(root: Path) -> tuple[Path, ...]:
    roots = {
        repository_control_path(root, "--absolute-git-dir"),
        repository_control_path(root, "--git-common-dir"),
    }
    return tuple(sorted(roots, key=str))


def _nearest_existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _is_inside_by_identity(path: Path, protected: Path) -> bool:
    candidate = _nearest_existing_ancestor(path)
    for ancestor in (candidate, *candidate.parents):
        try:
            if os.path.samefile(ancestor, protected):
                return True
        except OSError:
            continue
    return False


def _assert_output_outside_repository(root: Path, requested: Path) -> Path:
    spelling = str(requested)
    if os.name == "nt" and spelling.replace("/", "\\").startswith("\\\\"):
        raise StateError("baseline output must be outside the repository")
    resolved = requested.resolve(strict=False)
    for protected in (root, *repository_control_roots(root)):
        try:
            resolved.relative_to(protected)
        except ValueError:
            pass
        else:
            raise StateError("baseline output must be outside the repository")
        if _is_inside_by_identity(resolved.parent, protected):
            raise StateError("baseline output must be outside the repository")
    return resolved


def filesystem_identity(path: Path) -> dict[str, int]:
    metadata = path.stat(follow_symlinks=False)
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalized_snapshot_scopes(
    scopes: object,
    *,
    require_nonempty: bool = False,
) -> list[dict[str, str]] | None:
    """Return the canonical scope sequence used by a scoped snapshot."""

    if scopes is None:
        if require_nonempty:
            raise StateError("scoped ignored policy requires reader scopes")
        return None
    if not isinstance(scopes, list):
        raise StateError("workspace scopes must be a list")
    try:
        normalized = [
            require_repository_scope(scope, f"workspace scope[{index}]")
            for index, scope in enumerate(scopes)
        ]
    except ProtocolHashError as error:
        raise StateError(str(error)) from error
    identities = {(scope["kind"], scope["path"]): scope for scope in normalized}
    if len(identities) != len(normalized):
        raise StateError("workspace scopes contain duplicates")
    canonical = [identities[key] for key in sorted(identities)]
    if require_nonempty and not canonical:
        raise StateError("scoped ignored policy requires reader scopes")
    return canonical


def ignored_scope_digest(scopes: object) -> str:
    """Digest canonical reader scopes before a scoped ignored scan."""

    canonical = normalized_snapshot_scopes(scopes, require_nonempty=True)
    assert canonical is not None
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + sha256_bytes(encoded)


def snapshot_ignored_policy(snapshot: dict[str, Any]) -> str:
    """Return the only policy compatible with one validated snapshot."""

    return snapshot["ignored_policy"]


def git_config_digest(root: Path) -> str:
    raw = git(root, "config", "--null", "--show-origin", "--list", "--includes")
    _assert_git_record_budget(raw, b"\0", "Git config")
    return sha256_bytes(raw)


def refs_digest(root: Path) -> str:
    raw = git(
        root,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(symref)",
    )
    _assert_git_record_budget(raw, b"\n", "Git refs")
    return sha256_bytes(raw)


def reparse_target(path: str | os.PathLike[str]) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


class _ControlEntryBudget:
    """One shared entry budget for a complete Git-control inspection."""

    def __init__(self) -> None:
        self.remaining = MAX_GIT_CONTROL_ENTRIES

    def consume(self) -> None:
        if self.remaining <= 0:
            raise StateUnavailable(
                "Git control directory exceeds the "
                f"{MAX_GIT_CONTROL_ENTRIES} control entry limit"
            )
        self.remaining -= 1


def reparse_resolved_record(
    path: Path,
    *,
    _budget: _ControlEntryBudget | None = None,
) -> dict[str, Any]:
    budget = _budget or _ControlEntryBudget()
    try:
        if path.is_dir():
            return {
                "kind": "directory",
                "sha256": directory_digest(
                    path,
                    follow_reparse_content=False,
                    _budget=budget,
                ),
            }
        if path.is_file():
            return {"kind": "file", "sha256": sha256_file(path)}
        if not path.exists():
            return {"kind": "missing"}
        return {"kind": "special"}
    except OSError as error:
        raise StateUnavailable("Git control reparse target inspection failed") from error


def directory_digest(
    path: Path,
    *,
    follow_reparse_content: bool = True,
    _budget: _ControlEntryBudget | None = None,
) -> str:
    if not path.exists():
        return sha256_bytes(b'{"kind":"missing"}')
    budget = _budget or _ControlEntryBudget()
    records: list[dict[str, Any]] = []
    stack: list[tuple[Path, str]] = [(path, "")]
    while stack:
        checkpoint()
        current, prefix = stack.pop()
        children: list[os.DirEntry[str]] = []
        try:
            with os.scandir(current) as entries:
                for child in entries:
                    checkpoint()
                    budget.consume()
                    children.append(child)
        except OSError as error:
            raise StateUnavailable("Git control directory inspection failed") from error
        children.sort(key=lambda entry: entry.name)
        for child in children:
            checkpoint()
            relative = f"{prefix}/{child.name}" if prefix else child.name
            metadata = child.stat(follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            attributes = getattr(metadata, "st_file_attributes", 0)
            is_reparse = bool(
                attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            if child.is_symlink() or is_reparse:
                target = reparse_target(child.path)
                records.append(
                    {
                        "kind": "reparse",
                        "mode": mode,
                        "path": relative,
                        "resolved": (
                            reparse_resolved_record(Path(child.path), _budget=budget)
                            if follow_reparse_content
                            else None
                        ),
                        "target_sha256": (
                            sha256_bytes(os.fsencode(target)) if target is not None else None
                        ),
                    }
                )
            elif child.is_dir(follow_symlinks=False):
                records.append({"kind": "directory", "mode": mode, "path": relative})
                stack.append((Path(child.path), relative))
            elif child.is_file(follow_symlinks=False):
                records.append(
                    {
                        "kind": "file",
                        "mode": mode,
                        "path": relative,
                        "sha256": sha256_file(Path(child.path)),
                    }
                )
            else:
                records.append({"kind": "special", "mode": mode, "path": relative})
    canonical = json.dumps(
        sorted(records, key=lambda item: item["path"]),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256_bytes(canonical)


def control_entry_record(
    path: Path,
    *,
    _budget: _ControlEntryBudget | None = None,
) -> dict[str, Any]:
    budget = _budget or _ControlEntryBudget()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    mode = stat.S_IMODE(metadata.st_mode)
    attributes = getattr(metadata, "st_file_attributes", 0)
    is_reparse = bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
    if path.is_symlink() or is_reparse:
        target = reparse_target(path)
        return {
            "kind": "reparse",
            "mode": mode,
            "resolved": reparse_resolved_record(path, _budget=budget),
            "target_sha256": (
                sha256_bytes(os.fsencode(target)) if target is not None else None
            ),
        }
    if path.is_dir():
        return {
            "kind": "directory",
            "mode": mode,
            "sha256": directory_digest(path, _budget=budget),
        }
    if path.is_file():
        return {"kind": "file", "mode": mode, "sha256": sha256_file(path)}
    return {"kind": "special", "mode": mode}


def control_entry_digest(
    path: Path,
    *,
    _budget: _ControlEntryBudget | None = None,
) -> str:
    canonical = json.dumps(
        control_entry_record(path, _budget=_budget),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256_bytes(canonical)


def git_admin_digest(
    root: Path,
    *,
    _budget: _ControlEntryBudget | None = None,
) -> str:
    paths = repository_git_paths(root, GIT_ADMIN_PATHS)
    budget = _budget or _ControlEntryBudget()
    records = [
        {"name": name, **control_entry_record(paths[name], _budget=budget)}
        for name in GIT_ADMIN_PATHS
    ]
    canonical = json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256_bytes(canonical)


def fingerprint(root: Path, relative: str) -> dict[str, Any]:
    path = root / Path(relative)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        return {
            "kind": "symlink",
            "mode": mode,
            "sha256": hashlib.sha256(os.fsencode(target)).hexdigest(),
        }
    if path.is_file():
        return {
            "kind": "file",
            "mode": mode,
            "sha256": sha256_file(path),
        }
    if path.is_dir():
        return {"kind": "directory", "mode": mode}
    return {"kind": "special", "mode": mode}


def status_entries(root: Path) -> dict[str, dict[str, Any]]:
    raw = git(
        root,
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    entries: dict[str, dict[str, Any]] = {}
    records = iter(_git_records(raw, b"\0", "Git status"))
    for record in records:
        marker = record[:1]
        if marker == b"1":
            parts = record.split(b" ", 8)
            if len(parts) != 9:
                raise StateError("unexpected Git status record")
            path = decode_path(parts[8])
            entries[path] = {
                "status": decode_path(b" ".join(parts[:8])),
                "fingerprint": fingerprint(root, path),
            }
        elif marker == b"2":
            parts = record.split(b" ", 9)
            if len(parts) != 10:
                raise StateError("unexpected Git rename record")
            path = decode_path(parts[9])
            try:
                original = decode_path(next(records))
            except StopIteration as error:
                raise StateError("unexpected Git rename record") from error
            status = decode_path(b" ".join(parts[:9]))
            entries[path] = {
                "status": status,
                "rename_role": "destination",
                "other_path": original,
                "fingerprint": fingerprint(root, path),
            }
            entries[original] = {
                "status": status,
                "rename_role": "source",
                "other_path": path,
                "fingerprint": fingerprint(root, original),
            }
        elif marker == b"u":
            parts = record.split(b" ", 10)
            if len(parts) != 11:
                raise StateError("unexpected Git unmerged record")
            path = decode_path(parts[10])
            entries[path] = {
                "status": decode_path(b" ".join(parts[:10])),
                "fingerprint": fingerprint(root, path),
            }
        elif marker in (b"?", b"!"):
            if len(record) < 3:
                raise StateError("unexpected Git path record")
            path = decode_path(record[2:])
            entries[path] = {
                "status": marker.decode("ascii"),
                "fingerprint": fingerprint(root, path),
            }
        else:
            raise StateError("unsupported Git status record")
    return dict(sorted(entries.items()))


def ignored_entries(
    root: Path,
    *,
    max_files: int,
    max_bytes: int,
    scopes: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fingerprint ignored paths globally or inside declared graph scopes.

    The scan is deliberately bounded. Silently sampling would turn a fail-closed
    boundary into an unverifiable heuristic. Scope filtering happens before file and
    byte limits so normal prepared graphs pay only for their typed authority.
    """

    if max_files < 0 or max_bytes < 0:
        raise StateError("ignored scan limits must be non-negative")
    if scopes == []:
        return {}
    pathspecs: list[str] | None = None
    if scopes is not None:
        pathspecs = []
        for scope in scopes:
            candidate = root / Path(scope["path"])
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise StateUnavailable(
                    "ignored scope inspection is unavailable"
                ) from error
            if scope["kind"] == "exact" and stat.S_ISDIR(metadata.st_mode):
                continue
            pathspecs.append(f":(top,literal){scope['path']}")
        if not pathspecs:
            return {}
    arguments = [
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    ]
    if pathspecs is not None:
        arguments.append("--")
        arguments.extend(pathspecs)
    raw = git(
        root,
        *arguments,
    )
    paths: list[str] = []
    for value in _git_records(raw, b"\0", "Git ignored paths"):
        path = decode_path(value)
        if scopes is not None and not is_allowed(path, scopes):
            continue
        if len(paths) >= max_files:
            raise StateError(f"ignored scan exceeds the {max_files} file limit")
        paths.append(path)
    paths.sort()
    total_bytes = 0
    entries: dict[str, dict[str, Any]] = {}
    for path in paths:
        checkpoint()
        candidate = root / Path(path)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            total_bytes += metadata.st_size
            if total_bytes > max_bytes:
                raise StateError(
                    f"ignored scan exceeds the {max_bytes} byte limit"
                )
        entries[path] = {
            "fingerprint": fingerprint(root, path),
            "status": "!",
        }
    return entries


def repository_index_records(root: Path) -> dict[str, list[dict[str, str]]]:
    raw = git(root, "ls-files", "--stage", "--cached", "-z")
    index_records: dict[str, list[dict[str, str]]] = {}
    for record in _git_records(raw, b"\0", "Git index"):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split(" ")
        except (UnicodeError, ValueError) as error:
            raise StateError("unexpected Git index record") from error
        path = decode_path(raw_path)
        index_records.setdefault(path, []).append(
            {"mode": mode, "object_id": object_id, "stage": stage}
        )
    return index_records


def repository_status_hidden_paths(root: Path) -> frozenset[str]:
    """Return tracked paths whose worktree content Git status may suppress.

    ``git ls-files -v`` uses the normal ``H`` tag for ordinary cached paths,
    ``S`` for skip-worktree, and a lowercase tag when assume-unchanged is set.
    Fingerprinting the non-``H`` set closes the typed-scope blind spot without
    hashing every tracked file in a large repository.
    """

    raw = git(root, "ls-files", "-v", "-z")
    hidden: set[str] = set()
    for record in _git_records(raw, b"\0", "Git visibility"):
        try:
            marker, raw_path = record.split(b" ", 1)
        except ValueError as error:
            raise StateError("unexpected Git visibility record") from error
        if len(marker) != 1:
            raise StateError("unexpected Git visibility marker")
        if marker != b"H":
            hidden.add(decode_path(raw_path))
    return frozenset(hidden)


def repository_gitlinks(
    root: Path,
    index_records: dict[str, list[dict[str, str]]] | None = None,
) -> frozenset[str]:
    records_by_path = (
        repository_index_records(root) if index_records is None else index_records
    )
    return frozenset(
        path
        for path, records in records_by_path.items()
        if any(record["mode"] == "160000" for record in records)
    )


def repository_path_spelling_map(
    index_records: dict[str, list[dict[str, str]]],
) -> dict[str, str]:
    """Map native case-insensitive aliases to exact tracked Git prefixes."""
    spellings: dict[str, str] = {}
    for path in index_records:
        segments = path.split("/")
        for length in range(1, len(segments) + 1):
            prefix = "/".join(segments[:length])
            key = repository_path_spelling_key(prefix)
            previous = spellings.setdefault(key, prefix)
            if previous != prefix:
                raise StateError("Git index contains ambiguous path spellings")
    return spellings


def repository_path_spelling_key(value: str) -> str:
    """Fold case without aliasing a POSIX backslash to a Git separator."""
    if os.name == "nt":
        return ntpath.normcase(value).replace("\\", "/")
    return value.lower()


def tracked_entries(
    root: Path,
    index_records: dict[str, list[dict[str, str]]] | None = None,
    *,
    ignored_mode: str = "light",
    ignored_max_files: int = DEFAULT_IGNORED_MAX_FILES,
    ignored_max_bytes: int = DEFAULT_IGNORED_MAX_BYTES,
    scopes: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    index_records = (
        repository_index_records(root) if index_records is None else index_records
    )
    entries: dict[str, dict[str, Any]] = {}
    status_hidden = repository_status_hidden_paths(root) if scopes is not None else frozenset()
    selected_records = (
        index_records
        if scopes is None
        else {
            path: records
            for path, records in index_records.items()
            if is_allowed(path, scopes) or path in status_hidden
        }
    )
    for path, records in sorted(selected_records.items()):
        checkpoint()
        selected_by_scope = scopes is not None and is_allowed(path, scopes)
        ordered_records = sorted(
            records,
            key=lambda item: (item["stage"], item["mode"], item["object_id"]),
        )
        worktree_fingerprint = fingerprint(root, path)
        if any(record["mode"] == "160000" for record in ordered_records):
            nested = root / Path(path)
            git_marker = nested / ".git"
            if nested.is_dir() and git_marker.exists():
                nested_root = repository_root(nested)
                if nested_root == root or nested_root != nested.resolve():
                    raise StateError("Git submodule root is inconsistent")
                worktree_fingerprint = {
                    "kind": "submodule",
                    "git_marker": fingerprint(nested, ".git"),
                    "state": state_payload(
                        nested_root,
                        # An outer exact gitlink scope represents the complete
                        # initialized submodule: inner paths cannot be leased
                        # separately.  Capture all bounded ignored content so a
                        # scoped reader or writer cannot mutate an invisible file.
                        ignored_mode=("strict" if selected_by_scope else ignored_mode),
                        ignored_max_files=ignored_max_files,
                        ignored_max_bytes=ignored_max_bytes,
                        ignored_policy=IGNORED_POLICY_GLOBAL_V1,
                    ),
                }
            else:
                worktree_fingerprint = {
                    "kind": "uninitialized_submodule",
                    "fingerprint": worktree_fingerprint,
                }
        entries[path] = {
            "fingerprint": worktree_fingerprint,
            "index": ordered_records,
        }
    return entries


def workspace_entries(
    root: Path,
    index_records: dict[str, list[dict[str, str]]] | None = None,
    *,
    ignored_mode: str = "light",
    ignored_max_files: int = DEFAULT_IGNORED_MAX_FILES,
    ignored_max_bytes: int = DEFAULT_IGNORED_MAX_BYTES,
    ignored_policy: str = IGNORED_POLICY_GLOBAL_V1,
    scopes: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    if type(ignored_policy) is not str or ignored_policy not in IGNORED_POLICIES:
        raise StateError("ignored scan policy is invalid")
    normalized_scopes = normalized_snapshot_scopes(
        scopes,
        require_nonempty=(ignored_policy == IGNORED_POLICY_SCOPED_READER_V1),
    )
    status = status_entries(root)
    tracked = tracked_entries(
        root,
        index_records,
        ignored_mode=ignored_mode,
        ignored_max_files=ignored_max_files,
        ignored_max_bytes=ignored_max_bytes,
        scopes=normalized_scopes,
    )
    ignored = (
        ignored_entries(
            root,
            max_files=ignored_max_files,
            max_bytes=ignored_max_bytes,
            scopes=(
                normalized_scopes
                if ignored_policy == IGNORED_POLICY_SCOPED_READER_V1
                else None
            ),
        )
        if ignored_mode == "strict" or normalized_scopes is not None
        else {}
    )
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(set(status) | set(tracked) | set(ignored)):
        checkpoint()
        entry: dict[str, Any] = {}
        if path in status:
            entry["status"] = status[path]
        if path in tracked:
            entry["tracked"] = tracked[path]
        if path in ignored:
            entry["ignored"] = ignored[path]
        entries[path] = entry
    return entries


def index_digest(root: Path) -> str | None:
    raw_path = git(root, "rev-parse", "--git-path", "index").rstrip(b"\r\n")
    if not raw_path:
        raise StateError("Git index path is unavailable")
    index_path = Path(os.fsdecode(raw_path))
    if not index_path.is_absolute():
        index_path = root / index_path
    try:
        return sha256_file(index_path)
    except FileNotFoundError:
        return None


def head_oid(root: Path) -> str | None:
    result = _run_git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    if result.returncode == 0:
        output = result.stdout.strip()
        try:
            oid = output.decode("ascii")
        except UnicodeDecodeError as error:
            raise StateUnavailable("Git HEAD inspection returned a non-ASCII OID") from error
        if len(oid) not in {40, 64} or any(
            character not in "0123456789abcdefABCDEF" for character in oid
        ):
            raise StateUnavailable("Git HEAD inspection returned an invalid OID")
        return oid.lower()
    if result.returncode != 1:
        raise StateUnavailable("Git HEAD inspection failed")

    symbolic = _run_git(root, "symbolic-ref", "-q", "HEAD")
    if symbolic.returncode != 0:
        raise StateUnavailable("Git HEAD is neither a valid object nor an unborn branch")
    reference = symbolic.stdout.rstrip(b"\r\n")
    if not reference:
        raise StateUnavailable("Git symbolic HEAD inspection returned an empty reference")
    try:
        reference_text = reference.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise StateUnavailable("Git symbolic HEAD inspection returned invalid UTF-8") from error
    target = _run_git(
        root,
        "show-ref",
        "--verify",
        "--quiet",
        reference_text,
    )
    if target.returncode == 1:
        return None
    raise StateUnavailable("Git HEAD reference inspection failed")


def symbolic_head(root: Path) -> str | None:
    result = _run_git(root, "symbolic-ref", "-q", "HEAD")
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise StateUnavailable("Git symbolic HEAD inspection failed")
    output = result.stdout.rstrip(b"\r\n")
    if not output:
        raise StateUnavailable("Git symbolic HEAD inspection returned an empty reference")
    return output.decode("utf-8", errors="strict")


def state_payload(
    root: Path,
    *,
    control_roots: tuple[Path, ...] | None = None,
    index_records: dict[str, list[dict[str, str]]] | None = None,
    ignored_mode: str = "light",
    ignored_max_files: int = DEFAULT_IGNORED_MAX_FILES,
    ignored_max_bytes: int = DEFAULT_IGNORED_MAX_BYTES,
    ignored_policy: str = IGNORED_POLICY_GLOBAL_V1,
    scopes: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if ignored_mode not in WORKSPACE_MODES:
        raise StateError("workspace mode must be light or strict")
    if ignored_max_files < 0 or ignored_max_bytes < 0:
        raise StateError("ignored scan limits must be non-negative")
    if type(ignored_policy) is not str or ignored_policy not in IGNORED_POLICIES:
        raise StateError("ignored scan policy is invalid")
    normalized_scopes = normalized_snapshot_scopes(
        scopes,
        require_nonempty=(ignored_policy == IGNORED_POLICY_SCOPED_READER_V1),
    )
    control_roots = (
        repository_control_roots(root) if control_roots is None else control_roots
    )
    git_control_budget = _ControlEntryBudget()
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "repo_root": str(root),
        "repo_identity": filesystem_identity(root),
        "git_control_identities": [
            {"identity": filesystem_identity(path), "path": str(path)}
            for path in control_roots
        ],
        "head": head_oid(root),
        "symbolic_head": symbolic_head(root),
        "git_admin_sha256": git_admin_digest(root, _budget=git_control_budget),
        "git_config_sha256": git_config_digest(root),
        "refs_sha256": refs_digest(root),
        "hooks_sha256": control_entry_digest(
            repository_git_path(root, "hooks"),
            _budget=git_control_budget,
        ),
        "git_info_sha256": control_entry_digest(
            repository_git_path(root, "info"),
            _budget=git_control_budget,
        ),
        "ignored_mode": ignored_mode,
        "ignored_limits": {
            "max_bytes": ignored_max_bytes,
            "max_files": ignored_max_files,
        },
        "index_sha256": index_digest(root),
        "entries": workspace_entries(
            root,
            index_records,
            ignored_mode=ignored_mode,
            ignored_max_files=ignored_max_files,
            ignored_max_bytes=ignored_max_bytes,
            ignored_policy=ignored_policy,
            scopes=normalized_scopes,
        ),
    }
    payload["ignored_policy"] = ignored_policy
    payload["ignored_scope_digest"] = (
        ignored_scope_digest(normalized_scopes)
        if ignored_policy == IGNORED_POLICY_SCOPED_READER_V1
        else None
    )
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    payload["state_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return payload


def validate_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateError("baseline uses an unsupported schema")
    schema = value.get("schema")
    if schema != SCHEMA:
        raise StateError("baseline uses an unsupported schema")
    fields = SNAPSHOT_FIELDS
    if set(value) != fields:
        raise StateError(
            "baseline does not contain the exact "
            f"{schema.rsplit('.', 1)[-1]} required fields"
        )
    state_id = value.get("state_id")
    unsigned = {key: item for key, item in value.items() if key != "state_id"}
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    expected = "sha256:" + hashlib.sha256(canonical).hexdigest()
    if not isinstance(state_id, str) or state_id != expected:
        raise StateError("baseline state identifier does not match its content")
    if not isinstance(value.get("entries"), dict):
        raise StateError("baseline entries are invalid")
    if value.get("ignored_mode") not in WORKSPACE_MODES:
        raise StateError("baseline workspace mode is invalid")
    ignored_policy = value.get("ignored_policy")
    ignored_scope = value.get("ignored_scope_digest")
    if type(ignored_policy) is not str or ignored_policy not in IGNORED_POLICIES:
        raise StateError("baseline ignored scan policy is invalid")
    if ignored_policy == IGNORED_POLICY_GLOBAL_V1:
        if ignored_scope is not None:
            raise StateError("global ignored policy must not bind reader scopes")
    elif (
        not isinstance(ignored_scope, str)
        or len(ignored_scope) != 71
        or not ignored_scope.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in ignored_scope[7:])
    ):
        raise StateError("scoped ignored policy has an invalid scope digest")
    limits = value.get("ignored_limits")
    if (
        not isinstance(limits, dict)
        or set(limits) != {"max_bytes", "max_files"}
        or any(type(limits[name]) is not int or limits[name] < 0 for name in limits)
    ):
        raise StateError("baseline ignored scan limits are invalid")
    return value


def validate_repository_lease_path(
    root: Path,
    value: str,
    *,
    scope_kind: str = "exact",
    protected_roots: tuple[Path, ...] | None = None,
    gitlinks: frozenset[str] | None = None,
    tracked_spellings: dict[str, str] | None = None,
    directory_spellings: dict[str, frozenset[str]] | None = None,
) -> None:
    index_records: dict[str, list[dict[str, str]]] | None = None
    if gitlinks is None or tracked_spellings is None:
        index_records = repository_index_records(root)
    active_gitlinks = (
        repository_gitlinks(root, index_records) if gitlinks is None else gitlinks
    )
    active_spellings = (
        repository_path_spelling_map(index_records or {})
        if tracked_spellings is None
        else tracked_spellings
    )
    active_directory_spellings = (
        {} if directory_spellings is None else directory_spellings
    )
    value_segments = value.split("/")
    for gitlink in active_gitlinks:
        gitlink_segments = gitlink.split("/")
        if len(value_segments) < len(gitlink_segments):
            if (
                scope_kind == "prefix"
                and gitlink.startswith(value.rstrip("/") + "/")
            ):
                raise StateError(f"invalid lease path: {value}")
            continue
        prefix = "/".join(value_segments[: len(gitlink_segments)])
        if prefix == gitlink:
            if len(value_segments) > len(gitlink_segments):
                raise StateError(f"invalid lease path: {value}")
            if scope_kind == "prefix":
                raise StateError(f"invalid lease path: {value}")
            continue
        try:
            if os.path.samefile(root / Path(prefix), root / Path(gitlink)):
                raise StateError(f"invalid lease path: {value}")
        except FileNotFoundError:
            pass
    current = root
    current_spelling: list[str] = []
    control_roots = (
        repository_control_roots(root) if protected_roots is None else protected_roots
    )
    for segment in value.split("/"):
        current_spelling.append(segment)
        prefix = "/".join(current_spelling)
        spelling_key = repository_path_spelling_key(prefix)
        tracked = active_spellings.get(spelling_key)
        if tracked is not None and tracked != prefix:
            raise StateError(f"invalid lease path: {value}")
        directory_key = os.fspath(current)
        names = active_directory_spellings.get(directory_key)
        if names is None:
            try:
                with os.scandir(current) as entries:
                    names = frozenset(entry.name for entry in entries)
            except (FileNotFoundError, NotADirectoryError):
                break
            active_directory_spellings[directory_key] = names
        if segment not in names:
            try:
                (current / segment).lstat()
            except FileNotFoundError:
                break
            else:
                raise StateError(f"invalid lease path: {value}")
        current /= segment
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or (
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise StateError(f"invalid lease path: {value}")
        for control_root in control_roots:
            try:
                if os.path.samefile(current, control_root):
                    raise StateError(f"invalid lease path: {value}")
            except FileNotFoundError:
                continue
    if scope_kind == "prefix":
        _reject_reparse_descendants(root / Path(value), value)


def _reject_reparse_descendants(path: Path, value: str) -> None:
    if not path.exists():
        return
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            children = os.scandir(current)
        except (FileNotFoundError, NotADirectoryError):
            continue
        with children:
            for child in children:
                metadata = child.stat(follow_symlinks=False)
                attributes = getattr(metadata, "st_file_attributes", 0)
                if child.is_symlink() or (
                    attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                ):
                    raise StateError(f"invalid lease path: {value}")
                if child.is_dir(follow_symlinks=False):
                    stack.append(Path(child.path))


def normalize_allow(
    root: Path,
    value: str,
    *,
    protected_roots: tuple[Path, ...] | None = None,
    gitlinks: frozenset[str] | None = None,
    tracked_spellings: dict[str, str] | None = None,
    directory_spellings: dict[str, frozenset[str]] | None = None,
) -> dict[str, str]:
    try:
        scope = parse_repository_scope_text(value, "lease scope")
    except ProtocolHashError as error:
        raise StateError(f"invalid lease path: {value} ({error})") from error
    active_gitlinks = repository_gitlinks(root) if gitlinks is None else gitlinks
    validate_repository_lease_path(
        root,
        scope["path"],
        scope_kind=scope["kind"],
        protected_roots=protected_roots,
        gitlinks=active_gitlinks,
        tracked_spellings=tracked_spellings,
        directory_spellings=directory_spellings,
    )
    return scope


def is_allowed(path: str, allowed: list[dict[str, str]]) -> bool:
    return any(
        path == scope["path"]
        or (
            scope["kind"] == "prefix"
            and path.startswith(scope["path"] + "/")
        )
        for scope in allowed
    )


def submodule_control_violations(
    baseline_entries: dict[str, Any], current_entries: dict[str, Any]
) -> list[str]:
    violations: list[str] = []
    for path in sorted(set(baseline_entries) & set(current_entries)):
        baseline_fingerprint = (
            baseline_entries[path].get("tracked", {}).get("fingerprint", {})
        )
        current_fingerprint = (
            current_entries[path].get("tracked", {}).get("fingerprint", {})
        )
        baseline_kind = baseline_fingerprint.get("kind")
        current_kind = current_fingerprint.get("kind")
        submodule_kinds = {"submodule", "uninitialized_submodule"}
        if baseline_kind != current_kind and (
            baseline_kind in submodule_kinds or current_kind in submodule_kinds
        ):
            violations.append(f"submodule_control_changed:{path}:kind")
            continue
        if baseline_kind != "submodule" or current_kind != "submodule":
            continue
        if baseline_fingerprint.get("git_marker") != current_fingerprint.get(
            "git_marker"
        ):
            violations.append(f"submodule_control_changed:{path}:git_marker")
        baseline_state = baseline_fingerprint.get("state", {})
        current_state = current_fingerprint.get("state", {})
        baseline_schema = baseline_state.get("schema")
        current_schema = current_state.get("schema")
        if baseline_schema != current_schema:
            violations.append(f"submodule_control_changed:{path}:schema")
            continue
        if baseline_schema != SCHEMA:
            raise StateError("submodule snapshot uses an unsupported schema")
        fields = SNAPSHOT_FIELDS
        for field in sorted(fields - {"entries", "state_id"}):
            if baseline_state.get(field) != current_state.get(field):
                violations.append(f"submodule_control_changed:{path}:{field}")
    return violations


def verify(
    root: Path,
    baseline: dict[str, Any],
    allowed: list[dict[str, str]],
    *,
    control_roots: tuple[Path, ...] | None = None,
    index_records: dict[str, list[dict[str, str]]] | None = None,
    scope_entries: bool = False,
    entry_scopes: list[dict[str, str]] | None = None,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Verify one baseline while separating readable entries from write leases.

    ``allowed`` remains the set of paths permitted to differ.  A prepared graph
    can, however, have a wider captured scope than the leases currently active
    at a spawn boundary.  ``entry_scopes`` preserves that capture boundary
    without accidentally treating an unleased graph path as absent.
    """

    baseline = validate_snapshot(baseline)
    limits = baseline["ignored_limits"]
    captured_scopes = (
        entry_scopes
        if entry_scopes is not None
        else allowed if scope_entries else None
    )
    ignored_policy = snapshot_ignored_policy(baseline)
    if ignored_policy == IGNORED_POLICY_SCOPED_READER_V1:
        if captured_scopes is None:
            raise StateError("scoped ignored baseline has no captured reader scopes")
        if baseline["ignored_scope_digest"] != ignored_scope_digest(captured_scopes):
            raise StateError("scoped ignored baseline reader scopes do not match")
    current = state_payload(
        root,
        control_roots=control_roots,
        index_records=index_records,
        ignored_mode=baseline["ignored_mode"],
        ignored_max_files=limits["max_files"],
        ignored_max_bytes=limits["max_bytes"],
        ignored_policy=ignored_policy,
        scopes=captured_scopes,
    )
    baseline_entries = baseline["entries"]
    current_entries = current["entries"]
    changed = sorted(
        path
        for path in set(baseline_entries) | set(current_entries)
        if baseline_entries.get(path) != current_entries.get(path)
    )
    violations: list[str] = []
    if baseline.get("repo_root") != current["repo_root"]:
        violations.append("repository_changed")
    if baseline.get("repo_identity") != current["repo_identity"]:
        violations.append("repository_identity_changed")
    if baseline.get("git_control_identities") != current["git_control_identities"]:
        violations.append("git_control_identity_changed")
    if baseline.get("head") != current["head"]:
        violations.append("head_changed")
    if baseline.get("symbolic_head") != current["symbolic_head"]:
        violations.append("symbolic_head_changed")
    if baseline.get("git_admin_sha256") != current["git_admin_sha256"]:
        violations.append("git_admin_changed")
    if baseline.get("git_config_sha256") != current["git_config_sha256"]:
        violations.append("git_config_changed")
    if baseline.get("refs_sha256") != current["refs_sha256"]:
        violations.append("refs_changed")
    if baseline.get("hooks_sha256") != current["hooks_sha256"]:
        violations.append("hooks_changed")
    if baseline.get("git_info_sha256") != current["git_info_sha256"]:
        violations.append("git_info_changed")
    if baseline.get("index_sha256") != current["index_sha256"]:
        violations.append("index_changed")
    violations.extend(submodule_control_violations(baseline_entries, current_entries))
    violations.extend(
        f"outside_lease:{path}" for path in changed if not is_allowed(path, allowed)
    )
    result = {
        "schema": "cco.workspace-verification.v3",
        "baseline_state": baseline["state_id"],
        "current_state": current["state_id"],
        "allowed_scopes": allowed,
        "changed_paths": changed,
        "violations": violations,
        "verdict": "pass" if not violations else "violation",
    }
    return (0 if not violations else 1), result, current


def write_snapshot(root: Path, output: Path, serialized: str) -> None:
    requested = Path(os.path.abspath(output.expanduser()))
    if requested.is_symlink():
        raise StateError("baseline output must not be a symlink")
    resolved = _assert_output_outside_repository(root, requested)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved = _assert_output_outside_repository(root, resolved)
    if resolved.exists() and not resolved.is_file():
        raise StateError("baseline output must be a regular file")
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=resolved.parent,
            prefix=".cco-state-",
            delete=False,
        ) as staged:
            staged.write(serialized)
            staged.write("\n")
            staged.flush()
            os.fsync(staged.fileno())
            staged_path = Path(staged.name)
        _assert_output_outside_repository(root, resolved)
        os.replace(staged_path, resolved)
        staged_path = None
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Capture and verify baseline-relative Codex worker deltas."
    )
    subparsers = root.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--repo", type=Path, default=Path.cwd())
    capture.add_argument("--mode", choices=sorted(WORKSPACE_MODES), default="light")
    capture.add_argument(
        "--ignored-max-files", type=int, default=DEFAULT_IGNORED_MAX_FILES
    )
    capture.add_argument(
        "--ignored-max-bytes", type=int, default=DEFAULT_IGNORED_MAX_BYTES
    )
    capture.add_argument(
        "--output",
        type=Path,
        help="Write UTF-8 JSON atomically outside the repository instead of stdout.",
    )
    check = subparsers.add_parser("verify")
    check.add_argument("--repo", type=Path, default=Path.cwd())
    check.add_argument("--baseline", type=Path, required=True)
    check.add_argument(
        "--allow",
        action="append",
        default=[],
        help="Allow one explicit exact:<path> or prefix:<directory> scope; omit for a read-only check.",
    )
    check.add_argument(
        "--next-baseline",
        type=Path,
        help="Atomically write the verified current snapshot for the next serial lease.",
    )
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        root = repository_root(args.repo)
        if args.command == "capture":
            result = state_payload(
                root,
                ignored_mode=args.mode,
                ignored_max_files=args.ignored_max_files,
                ignored_max_bytes=args.ignored_max_bytes,
            )
            code = 0
        else:
            baseline = validate_snapshot(
                json.loads(args.baseline.read_text(encoding="utf-8"))
            )
            control_roots = repository_control_roots(root)
            index_records = repository_index_records(root)
            gitlinks = repository_gitlinks(root, index_records)
            tracked_spellings = repository_path_spelling_map(index_records)
            directory_spellings: dict[str, frozenset[str]] = {}
            scopes = [
                normalize_allow(
                    root,
                    item,
                    protected_roots=control_roots,
                    gitlinks=gitlinks,
                    tracked_spellings=tracked_spellings,
                    directory_spellings=directory_spellings,
                )
                for item in args.allow
            ]
            identities = {(scope["path"], scope["kind"]): scope for scope in scopes}
            allowed = [
                identities[key]
                for key in sorted(
                    identities,
                    key=lambda item: (
                        item[0].encode("utf-8"), item[1].encode("utf-8")
                    ),
                )
            ]
            code, result, current = verify(
                root,
                baseline,
                allowed,
                control_roots=control_roots,
                index_records=index_records,
            )
            if code == 0 and args.next_baseline is not None:
                write_snapshot(
                    root,
                    args.next_baseline,
                    json.dumps(
                        current,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ),
                )
    except (OSError, json.JSONDecodeError, StateError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    serialized = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if args.command == "capture" and args.output is not None:
        try:
            write_snapshot(root, args.output, serialized)
        except (OSError, StateError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
    else:
        print(serialized)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
