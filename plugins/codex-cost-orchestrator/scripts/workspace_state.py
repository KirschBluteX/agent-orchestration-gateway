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
from typing import Any

from protocol_hash import (
    ProtocolHashError,
    parse_repository_scope_text,
    require_repository_scope,
)


SCHEMA = "cco.workspace-state.v3"
WORKSPACE_MODES = frozenset({"light", "strict"})
DEFAULT_IGNORED_MAX_FILES = 10_000
DEFAULT_IGNORED_MAX_BYTES = 256 * 1024 * 1024
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
    }
)


class StateError(Exception):
    pass


def git(repo: Path, *args: str, allow_failure: bool = False) -> bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode and not allow_failure:
        raise StateError("Git repository inspection failed")
    return result.stdout


def decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def repository_root(repo: Path) -> Path:
    candidate = repo.expanduser().resolve()
    output = git(candidate, "rev-parse", "--show-toplevel")
    if not output:
        raise StateError("--repo must identify a Git work tree")
    return Path(os.fsdecode(output.rstrip(b"\r\n"))).resolve()


def repository_control_path(root: Path, option: str) -> Path:
    output = git(root, "rev-parse", option).rstrip(b"\r\n")
    if not output:
        raise StateError("Git control path is unavailable")
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
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git_config_digest(root: Path) -> str:
    return sha256_bytes(
        git(root, "config", "--null", "--show-origin", "--list", "--includes")
    )


def refs_digest(root: Path) -> str:
    return sha256_bytes(
        git(
            root,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(symref)",
        )
    )


def reparse_target(path: str | os.PathLike[str]) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def reparse_resolved_record(path: Path) -> dict[str, Any]:
    try:
        if path.is_dir():
            return {
                "kind": "directory",
                "sha256": directory_digest(path, follow_reparse_content=False),
            }
        if path.is_file():
            return {"kind": "file", "sha256": sha256_file(path)}
        if not path.exists():
            return {"kind": "missing"}
        return {"kind": "special"}
    except OSError as error:
        raise StateError("Git control reparse target inspection failed") from error


def directory_digest(path: Path, *, follow_reparse_content: bool = True) -> str:
    if not path.exists():
        return sha256_bytes(b'{"kind":"missing"}')
    records: list[dict[str, Any]] = []
    stack: list[tuple[Path, str]] = [(path, "")]
    while stack:
        current, prefix = stack.pop()
        try:
            children = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as error:
            raise StateError("Git control directory inspection failed") from error
        for child in children:
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
                            reparse_resolved_record(Path(child.path))
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


def control_entry_record(path: Path) -> dict[str, Any]:
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
            "resolved": reparse_resolved_record(path),
            "target_sha256": (
                sha256_bytes(os.fsencode(target)) if target is not None else None
            ),
        }
    if path.is_dir():
        return {"kind": "directory", "mode": mode, "sha256": directory_digest(path)}
    if path.is_file():
        return {"kind": "file", "mode": mode, "sha256": sha256_file(path)}
    return {"kind": "special", "mode": mode}


def control_entry_digest(path: Path) -> str:
    canonical = json.dumps(
        control_entry_record(path),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256_bytes(canonical)


def git_admin_digest(root: Path) -> str:
    paths = repository_git_paths(root, GIT_ADMIN_PATHS)
    records = [
        {"name": name, **control_entry_record(paths[name])}
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
    fields = raw.split(b"\0")
    entries: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
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
            if len(parts) != 10 or index >= len(fields):
                raise StateError("unexpected Git rename record")
            path = decode_path(parts[9])
            original = decode_path(fields[index])
            index += 1
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
    raw = git(
        root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    )
    paths = sorted(decode_path(value) for value in raw.split(b"\0") if value)
    if scopes is not None:
        paths = [path for path in paths if is_allowed(path, scopes)]
    if len(paths) > max_files:
        raise StateError(
            f"ignored scan exceeds the {max_files} file limit"
        )
    total_bytes = 0
    entries: dict[str, dict[str, Any]] = {}
    for path in paths:
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
    for record in raw.split(b"\0"):
        if not record:
            continue
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
    selected_records = (
        index_records
        if scopes is None
        else {path: records for path, records in index_records.items() if is_allowed(path, scopes)}
    )
    for path, records in sorted(selected_records.items()):
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
                        ignored_mode=ignored_mode,
                        ignored_max_files=ignored_max_files,
                        ignored_max_bytes=ignored_max_bytes,
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
    scopes: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    normalized_scopes = (
        [
            require_repository_scope(scope, f"workspace scope[{index}]")
            for index, scope in enumerate(scopes)
        ]
        if scopes is not None
        else None
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
            scopes=normalized_scopes,
        )
        if ignored_mode == "strict" or normalized_scopes is not None
        else {}
    )
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(set(status) | set(tracked) | set(ignored)):
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
    output = git(root, "rev-parse", "--verify", "HEAD", allow_failure=True).strip()
    return output.decode("ascii") if output else None


def symbolic_head(root: Path) -> str | None:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise StateError("Git symbolic HEAD inspection failed")
    output = result.stdout.rstrip(b"\r\n")
    if not output:
        raise StateError("Git symbolic HEAD inspection returned an empty reference")
    return output.decode("utf-8", errors="strict")


def state_payload(
    root: Path,
    *,
    control_roots: tuple[Path, ...] | None = None,
    index_records: dict[str, list[dict[str, str]]] | None = None,
    ignored_mode: str = "light",
    ignored_max_files: int = DEFAULT_IGNORED_MAX_FILES,
    ignored_max_bytes: int = DEFAULT_IGNORED_MAX_BYTES,
    scopes: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if ignored_mode not in WORKSPACE_MODES:
        raise StateError("workspace mode must be light or strict")
    if ignored_max_files < 0 or ignored_max_bytes < 0:
        raise StateError("ignored scan limits must be non-negative")
    control_roots = (
        repository_control_roots(root) if control_roots is None else control_roots
    )
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
        "git_admin_sha256": git_admin_digest(root),
        "git_config_sha256": git_config_digest(root),
        "refs_sha256": refs_digest(root),
        "hooks_sha256": control_entry_digest(repository_git_path(root, "hooks")),
        "git_info_sha256": control_entry_digest(repository_git_path(root, "info")),
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
            scopes=scopes,
        ),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    payload["state_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return payload


def validate_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise StateError("baseline uses an unsupported schema")
    if set(value) != SNAPSHOT_FIELDS:
        raise StateError("baseline does not contain the exact v3 required fields")
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
        for field in sorted(set(SNAPSHOT_FIELDS) - {"entries", "state_id"}):
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
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    limits = baseline["ignored_limits"]
    current = state_payload(
        root,
        control_roots=control_roots,
        index_records=index_records,
        ignored_mode=baseline["ignored_mode"],
        ignored_max_files=limits["max_files"],
        ignored_max_bytes=limits["max_bytes"],
        scopes=allowed if scope_entries else None,
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
