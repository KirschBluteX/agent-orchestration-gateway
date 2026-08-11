#!/usr/bin/env python3
"""Bounded, content-free snapshots for non-Git directory workspaces."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Mapping
import unicodedata

from operation_deadline import checkpoint
from protocol_hash import (
    ProtocolHashError,
    canonical_bytes,
    repository_scopes_overlap,
    require_repository_path,
    require_repository_scope,
)


SCHEMA = "cco.directory-state.v2"
CAPTURE_MODES = frozenset({"full", "scope"})
DEFAULT_MAX_ENTRIES = 20_000
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
_DOMAIN = b"cco.directory-state.v2\0"
_SNAPSHOT_FIELDS = frozenset(
    {
        "backend",
        "capture_mode",
        "directory_count",
        "entries",
        "file_count",
        "limits",
        "root_identity",
        "root_path",
        "schema",
        "scopes",
        "state_id",
        "total_bytes",
        "workspace_mode",
    }
)


class DirectoryStateError(ValueError):
    """A non-Git workspace cannot be represented or verified safely."""


class DirectoryStateUnavailable(DirectoryStateError):
    """Directory metadata or content could not be inspected temporarily."""


class DirectoryBudgetError(DirectoryStateError):
    """A safe directory capture would exceed its configured metadata budget."""


def _is_reparse_metadata(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _identity(metadata: os.stat_result) -> dict[str, str]:
    return {"device": str(metadata.st_dev), "inode": str(metadata.st_ino)}


def _root_metadata(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DirectoryStateUnavailable("directory workspace root is unavailable") from error
    if _is_reparse_metadata(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise DirectoryStateError("directory workspace root must be a real directory")
    return metadata


def directory_root(path: Path) -> Path:
    """Resolve one exact non-reparse directory root without creating it."""

    candidate = Path(os.path.abspath(Path(path).expanduser()))
    _root_metadata(candidate)
    for ancestor in (candidate, *candidate.parents):
        try:
            metadata = ancestor.lstat()
        except OSError as error:
            raise DirectoryStateUnavailable(
                "directory workspace ancestry is unavailable"
            ) from error
        if _is_reparse_metadata(metadata):
            raise DirectoryStateError("directory workspace cannot use a reparse ancestor")
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise DirectoryStateUnavailable(
            "directory workspace root cannot be resolved"
        ) from error


def _case_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _canonical_child_path(relative: str) -> str:
    if unicodedata.normalize("NFC", relative) != relative:
        raise DirectoryStateError("directory entry names must use NFC normalization")
    try:
        return require_repository_path(relative.replace(os.sep, "/"), "directory entry")
    except ProtocolHashError as error:
        raise DirectoryStateError(str(error)) from error


def _child_names(path: Path, *, max_names: int) -> list[str]:
    if (
        isinstance(max_names, bool)
        or not isinstance(max_names, int)
        or max_names < 0
    ):
        raise DirectoryStateError("directory entry budget is invalid")
    checkpoint()
    names: list[str] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                checkpoint()
                if len(names) >= max_names:
                    raise DirectoryBudgetError(
                        "directory snapshot exceeds the configured entry budget"
                    )
                names.append(entry.name)
    except OSError as error:
        raise DirectoryStateUnavailable(
            "directory workspace enumeration failed"
        ) from error
    keys: dict[str, str] = {}
    for name in names:
        checkpoint()
        if unicodedata.normalize("NFC", name) != name:
            raise DirectoryStateError("directory entry names must use NFC normalization")
        key = name.casefold()
        if key in keys and keys[key] != name:
            raise DirectoryStateError("directory contains a case-insensitive path alias")
        keys[key] = name
    ordered = sorted(names, key=lambda value: (value.casefold(), value))
    checkpoint()
    return ordered


def normalize_directory_scope(
    root: Path,
    value: object,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict[str, str]:
    """Bind a protocol scope to the observed spelling under a directory root."""

    try:
        scope = require_repository_scope(value, "directory scope")
    except ProtocolHashError as error:
        raise DirectoryStateError(str(error)) from error
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries < 1
    ):
        raise DirectoryStateError("directory entry budget is invalid")
    workspace = directory_root(root)
    current = workspace
    spelling: list[str] = []
    segments = scope["path"].split("/")
    for index, segment in enumerate(segments):
        checkpoint()
        names = _child_names(current, max_names=max_entries)
        matches = [name for name in names if name.casefold() == segment.casefold()]
        if len(matches) > 1:
            raise DirectoryStateError("directory scope has an ambiguous path spelling")
        actual = matches[0] if matches else segment
        spelling.append(actual)
        candidate = current / actual
        if not candidate.exists() and not candidate.is_symlink():
            spelling.extend(segments[index + 1 :])
            break
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise DirectoryStateUnavailable(
                "directory scope cannot be inspected"
            ) from error
        if _is_reparse_metadata(metadata):
            raise DirectoryStateError("directory scope cannot traverse a reparse point")
        if stat.S_ISDIR(metadata.st_mode):
            if scope["kind"] == "exact" and index == len(segments) - 1:
                raise DirectoryStateError(
                    "directory exact scope cannot identify an ordinary directory"
                )
            current = candidate
        elif index < len(segments) - 1:
            raise DirectoryStateError("directory scope traverses a non-directory")
    return {"kind": scope["kind"], "path": "/".join(spelling)}


def _metadata_token(metadata: os.stat_result, kind: str) -> tuple[Any, ...]:
    return (
        kind,
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
    )


def _inspect_entry(path: Path, relative: str) -> tuple[dict[str, Any], tuple[Any, ...]]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DirectoryStateUnavailable("directory entry inspection failed") from error
    if _is_reparse_metadata(metadata):
        raise DirectoryStateError(f"directory entry is a reparse point: {relative}")
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISREG(metadata.st_mode):
        return (
            {"kind": "file", "mode": mode, "path": relative, "size": int(metadata.st_size)},
            _metadata_token(metadata, "file"),
        )
    if stat.S_ISDIR(metadata.st_mode):
        return (
            {"kind": "directory", "mode": mode, "path": relative},
            _metadata_token(metadata, "directory"),
        )
    raise DirectoryStateError(f"directory entry has an unsupported type: {relative}")


def _append_with_budget(
    found: list[tuple[dict[str, Any], tuple[Any, ...], Path | None]],
    item: tuple[dict[str, Any], tuple[Any, ...], Path | None],
    *,
    max_entries: int,
    max_bytes: int,
    totals: dict[str, int],
) -> None:
    totals["entries"] += 1
    if item[0]["kind"] == "file":
        totals["bytes"] += int(item[0]["size"])
    if totals["entries"] > max_entries or totals["bytes"] > max_bytes:
        raise DirectoryBudgetError(
            "directory snapshot exceeds the configured entry or byte budget"
        )
    found.append(item)


def _walk_tree(
    root: Path,
    start: Path,
    relative: str,
    *,
    found: list[tuple[dict[str, Any], tuple[Any, ...], Path | None]],
    max_entries: int,
    max_bytes: int,
    totals: dict[str, int],
) -> None:
    record, token = _inspect_entry(start, relative)
    _append_with_budget(
        found,
        (record, token, start if record["kind"] == "file" else None),
        max_entries=max_entries,
        max_bytes=max_bytes,
        totals=totals,
    )
    if record["kind"] != "directory":
        return
    stack: list[tuple[Path, str]] = [(start, relative)]
    while stack:
        checkpoint()
        current, prefix = stack.pop()
        directories: list[tuple[Path, str]] = []
        for name in _child_names(
            current,
            max_names=max_entries - totals["entries"],
        ):
            checkpoint()
            child = current / name
            child_relative = _canonical_child_path(f"{prefix}/{name}" if prefix else name)
            child_record, child_token = _inspect_entry(child, child_relative)
            _append_with_budget(
                found,
                (
                    child_record,
                    child_token,
                    child if child_record["kind"] == "file" else None,
                ),
                max_entries=max_entries,
                max_bytes=max_bytes,
                totals=totals,
            )
            if child_record["kind"] == "directory":
                directories.append((child, child_relative))
        stack.extend(reversed(directories))
def _enumerate(
    root: Path,
    scopes: list[dict[str, str]],
    capture_mode: str,
    *,
    max_entries: int,
    max_bytes: int,
) -> list[tuple[dict[str, Any], tuple[Any, ...], Path | None]]:
    found: list[tuple[dict[str, Any], tuple[Any, ...], Path | None]] = []
    totals = {"bytes": 0, "entries": 0}
    if capture_mode == "full":
        for name in _child_names(root, max_names=max_entries):
            checkpoint()
            child = root / name
            relative = _canonical_child_path(name)
            _walk_tree(
                root,
                child,
                relative,
                found=found,
                max_entries=max_entries,
                max_bytes=max_bytes,
                totals=totals,
            )
    else:
        for scope in scopes:
            checkpoint()
            target = root / Path(scope["path"])
            if not target.exists() and not target.is_symlink():
                _append_with_budget(
                    found,
                    (
                        {"kind": "missing", "path": scope["path"]},
                        ("missing",),
                        None,
                    ),
                    max_entries=max_entries,
                    max_bytes=max_bytes,
                    totals=totals,
                )
                continue
            record, token = _inspect_entry(target, scope["path"])
            if scope["kind"] == "prefix" and record["kind"] == "directory":
                _walk_tree(
                    root,
                    target,
                    scope["path"],
                    found=found,
                    max_entries=max_entries,
                    max_bytes=max_bytes,
                    totals=totals,
                )
            else:
                _append_with_budget(
                    found,
                    (record, token, target if record["kind"] == "file" else None),
                    max_entries=max_entries,
                    max_bytes=max_bytes,
                    totals=totals,
                )

    unique: dict[str, tuple[dict[str, Any], tuple[Any, ...], Path | None]] = {}
    for item in found:
        key = _case_key(item[0]["path"])
        previous = unique.get(key)
        if previous is not None and previous[0]["path"] != item[0]["path"]:
            raise DirectoryStateError("directory snapshot contains a path alias")
        unique[key] = item
    return [unique[key] for key in sorted(unique)]


def _limits(max_entries: int, max_bytes: int) -> dict[str, int]:
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries < 1
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 1
    ):
        raise DirectoryStateError("directory snapshot limits are invalid")
    return {"max_bytes": max_bytes, "max_entries": max_entries}


def _counts(items: list[tuple[dict[str, Any], tuple[Any, ...], Path | None]]) -> tuple[int, int, int]:
    file_count = sum(item[0]["kind"] == "file" for item in items)
    directory_count = sum(item[0]["kind"] == "directory" for item in items)
    total_bytes = sum(
        int(item[0]["size"]) for item in items if item[0]["kind"] == "file"
    )
    return file_count, directory_count, total_bytes


def _digest_file(path: Path, expected: tuple[Any, ...]) -> str:
    digest = hashlib.sha256()
    try:
        before = path.stat(follow_symlinks=False)
        if _metadata_token(before, "file") != expected:
            raise DirectoryStateError("directory file changed during snapshot capture")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checkpoint()
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except OSError as error:
        raise DirectoryStateUnavailable("directory file hashing failed") from error
    if _metadata_token(after, "file") != expected:
        raise DirectoryStateError("directory file changed during snapshot capture")
    return digest.hexdigest()


def _state_id(value: Mapping[str, Any]) -> str:
    payload = {key: deepcopy(item) for key, item in value.items() if key != "state_id"}
    return "sha256:" + hashlib.sha256(_DOMAIN + canonical_bytes(payload)).hexdigest()


def capture_directory_state(
    root: Path,
    *,
    scopes: object,
    capture_mode: str,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    workspace_mode: str = "light",
) -> dict[str, Any]:
    """Preflight a bounded directory, then hash content without retaining it."""

    workspace = directory_root(root)
    if capture_mode not in CAPTURE_MODES or workspace_mode not in {"light", "strict"}:
        raise DirectoryStateError("directory snapshot mode is invalid")
    if not isinstance(scopes, list) or not scopes:
        raise DirectoryStateError("directory snapshot scopes must be a non-empty list")
    limits = _limits(max_entries, max_bytes)
    normalized = [
        normalize_directory_scope(
            workspace,
            scope,
            max_entries=limits["max_entries"],
        )
        for scope in scopes
    ]
    normalized.sort(key=lambda item: (item["kind"], item["path"]))
    if len({(item["kind"], _case_key(item["path"])) for item in normalized}) != len(normalized):
        raise DirectoryStateError("directory snapshot scopes are duplicated")
    root_before = _root_metadata(workspace)
    first = _enumerate(
        workspace,
        normalized,
        capture_mode,
        max_entries=max_entries,
        max_bytes=max_bytes,
    )
    file_count, directory_count, total_bytes = _counts(first)
    entries: list[dict[str, Any]] = []
    for record, token, path in first:
        checkpoint()
        item = dict(record)
        if path is not None:
            item["sha256"] = _digest_file(path, token)
        entries.append(item)
    second = _enumerate(
        workspace,
        normalized,
        capture_mode,
        max_entries=max_entries,
        max_bytes=max_bytes,
    )
    if [(item[0], item[1]) for item in first] != [(item[0], item[1]) for item in second]:
        raise DirectoryStateError("directory workspace changed during snapshot capture")
    root_after = _root_metadata(workspace)
    if _identity(root_before) != _identity(root_after):
        raise DirectoryStateError("directory workspace root changed during snapshot capture")
    snapshot: dict[str, Any] = {
        "backend": "directory",
        "capture_mode": capture_mode,
        "directory_count": directory_count,
        "entries": entries,
        "file_count": file_count,
        "limits": limits,
        "root_identity": _identity(root_after),
        "root_path": str(workspace),
        "schema": SCHEMA,
        "scopes": normalized,
        "state_id": "",
        "total_bytes": total_bytes,
        "workspace_mode": workspace_mode,
    }
    snapshot["state_id"] = _state_id(snapshot)
    return validate_directory_snapshot(snapshot)


def validate_directory_snapshot(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SNAPSHOT_FIELDS:
        raise DirectoryStateError("directory snapshot is malformed")
    if (
        value.get("schema") != SCHEMA
        or value.get("backend") != "directory"
        or value.get("capture_mode") not in CAPTURE_MODES
        or value.get("workspace_mode") not in {"light", "strict"}
    ):
        raise DirectoryStateError("directory snapshot identity is invalid")
    root_path = value.get("root_path")
    root_identity = value.get("root_identity")
    if (
        not isinstance(root_path, str)
        or not Path(root_path).is_absolute()
        or not isinstance(root_identity, Mapping)
        or set(root_identity) != {"device", "inode"}
        or any(
            not isinstance(item, str) or not item.isascii() or not item.isdecimal()
            for item in root_identity.values()
        )
    ):
        raise DirectoryStateError("directory snapshot root is invalid")
    limits = value.get("limits")
    if not isinstance(limits, Mapping) or set(limits) != {"max_bytes", "max_entries"}:
        raise DirectoryStateError("directory snapshot limits are malformed")
    normalized_limits = _limits(limits["max_entries"], limits["max_bytes"])
    scopes_value = value.get("scopes")
    try:
        scopes = [require_repository_scope(item, "directory snapshot scope") for item in scopes_value]
    except (ProtocolHashError, TypeError) as error:
        raise DirectoryStateError("directory snapshot scopes are malformed") from error
    if (
        not scopes
        or len({(item["kind"], _case_key(item["path"])) for item in scopes}) != len(scopes)
        or scopes != sorted(scopes, key=lambda item: (item["kind"], item["path"]))
    ):
        raise DirectoryStateError("directory snapshot scopes are not canonical")
    entries_value = value.get("entries")
    if not isinstance(entries_value, list):
        raise DirectoryStateError("directory snapshot entries are malformed")
    entries: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw in entries_value:
        if not isinstance(raw, Mapping) or raw.get("kind") not in {"directory", "file", "missing"}:
            raise DirectoryStateError("directory snapshot entry is malformed")
        kind = raw["kind"]
        expected = {"kind", "path"}
        if kind == "directory":
            expected.add("mode")
        elif kind == "file":
            expected.update({"mode", "sha256", "size"})
        if set(raw) != expected:
            raise DirectoryStateError("directory snapshot entry fields are malformed")
        try:
            path = require_repository_path(raw["path"], "directory snapshot entry")
        except ProtocolHashError as error:
            raise DirectoryStateError(str(error)) from error
        key = _case_key(path)
        if key in keys:
            raise DirectoryStateError("directory snapshot entries contain an alias")
        keys.add(key)
        item = {"kind": kind, "path": path}
        if kind in {"directory", "file"}:
            mode = raw["mode"]
            if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0:
                raise DirectoryStateError("directory snapshot mode is invalid")
            item["mode"] = mode
        if kind == "file":
            size, digest = raw["size"], raw["sha256"]
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise DirectoryStateError("directory snapshot file identity is invalid")
            item.update({"sha256": digest, "size": size})
        entries.append(item)
    if entries != sorted(entries, key=lambda item: _case_key(item["path"])):
        raise DirectoryStateError("directory snapshot entries are not canonical")
    file_count = sum(item["kind"] == "file" for item in entries)
    directory_count = sum(item["kind"] == "directory" for item in entries)
    total_bytes = sum(item.get("size", 0) for item in entries)
    if (
        value.get("file_count") != file_count
        or value.get("directory_count") != directory_count
        or value.get("total_bytes") != total_bytes
        or len(entries) > normalized_limits["max_entries"]
        or total_bytes > normalized_limits["max_bytes"]
    ):
        raise DirectoryStateError("directory snapshot counts are inconsistent")
    normalized = {
        "backend": "directory",
        "capture_mode": value["capture_mode"],
        "directory_count": directory_count,
        "entries": entries,
        "file_count": file_count,
        "limits": normalized_limits,
        "root_identity": {"device": root_identity["device"], "inode": root_identity["inode"]},
        "root_path": root_path,
        "schema": SCHEMA,
        "scopes": scopes,
        "state_id": value.get("state_id"),
        "total_bytes": total_bytes,
        "workspace_mode": value["workspace_mode"],
    }
    if normalized["state_id"] != _state_id(normalized):
        raise DirectoryStateError("directory snapshot digest is inconsistent")
    return normalized


def _entry_map(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {_case_key(item["path"]): item for item in snapshot["entries"]}


def verify_directory_state(
    root: Path,
    snapshot: object,
    *,
    allowed_scopes: object,
) -> dict[str, Any]:
    baseline = validate_directory_snapshot(snapshot)
    workspace = directory_root(root)
    if str(workspace).casefold() != str(baseline["root_path"]).casefold():
        raise DirectoryStateError("directory snapshot root does not match workspace")
    if not isinstance(allowed_scopes, list):
        raise DirectoryStateError("directory verification scopes must be a list")
    allowed = [
        normalize_directory_scope(
            workspace,
            scope,
            max_entries=baseline["limits"]["max_entries"],
        )
        for scope in allowed_scopes
    ]
    current = capture_directory_state(
        workspace,
        scopes=baseline["scopes"],
        capture_mode=baseline["capture_mode"],
        max_entries=baseline["limits"]["max_entries"],
        max_bytes=baseline["limits"]["max_bytes"],
        workspace_mode=baseline["workspace_mode"],
    )
    before, after = _entry_map(baseline), _entry_map(current)
    changed_paths = sorted(
        {
            (after.get(key) or before[key])["path"]
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        },
        key=_case_key,
    )
    violations: list[str] = []
    if current["root_identity"] != baseline["root_identity"]:
        violations.append("root_replaced")
    for path in changed_paths:
        exact = {"kind": "exact", "path": path}
        if not any(repository_scopes_overlap(scope, exact) for scope in allowed):
            violations.append(f"outside_scope:{path}")
    return {
        "baseline_state": baseline["state_id"],
        "changed_paths": changed_paths,
        "current_state": current["state_id"],
        "schema": "cco.directory-state-verification.v1",
        "violations": sorted(set(violations)),
        "verdict": "pass" if not violations else "fail",
    }


def verify_directory_pre_spawn(
    root: Path,
    snapshot: object,
    *,
    active_scopes: object,
    graph_scopes: object,
) -> dict[str, Any]:
    """Verify full worker capture while hashing only graph-visible content."""

    baseline = validate_directory_snapshot(snapshot)
    workspace = directory_root(root)
    if not isinstance(active_scopes, list) or not isinstance(graph_scopes, list):
        raise DirectoryStateError("directory pre-spawn scopes must be lists")
    active = [
        normalize_directory_scope(
            workspace,
            scope,
            max_entries=baseline["limits"]["max_entries"],
        )
        for scope in active_scopes
    ]
    graph = [
        normalize_directory_scope(
            workspace,
            scope,
            max_entries=baseline["limits"]["max_entries"],
        )
        for scope in graph_scopes
    ]
    current = capture_directory_state(
        workspace,
        scopes=baseline["scopes"],
        capture_mode=baseline["capture_mode"],
        max_entries=baseline["limits"]["max_entries"],
        max_bytes=baseline["limits"]["max_bytes"],
        workspace_mode=baseline["workspace_mode"],
    )
    before, after = _entry_map(baseline), _entry_map(current)
    all_changed = sorted(
        {
            (after.get(key) or before[key])["path"]
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        },
        key=_case_key,
    )
    changed_paths = [
        path
        for path in all_changed
        if any(
            repository_scopes_overlap(scope, {"kind": "exact", "path": path})
            for scope in graph
        )
    ]
    violations: list[str] = []
    if current["root_identity"] != baseline["root_identity"]:
        violations.append("root_replaced")
    for path in all_changed:
        exact = {"kind": "exact", "path": path}
        if any(repository_scopes_overlap(scope, exact) for scope in active):
            continue
        if any(repository_scopes_overlap(scope, exact) for scope in graph):
            violations.append(f"outside_scope:{path}")
        else:
            violations.append(f"outside_graph:{path}")
    return {
        "baseline_state": baseline["state_id"],
        "changed_paths": changed_paths,
        "current_state": current["state_id"],
        "schema": "cco.directory-pre-spawn-verification.v1",
        "violations": sorted(set(violations)),
        "verdict": "pass" if not violations else "fail",
    }
