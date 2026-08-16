#!/usr/bin/env python3
"""One exact-state interface over Git and bounded directory workspaces."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from operation_deadline import checkpoint
from directory_state import (
    DirectoryStateError,
    DirectoryStateUnavailable,
    capture_directory_state,
    directory_root,
    normalize_directory_scope,
    validate_directory_snapshot,
    verify_directory_pre_spawn,
    verify_directory_state,
)
from protocol_hash import (
    ProtocolHashError,
    repository_scopes_overlap,
    require_repository_scope,
)
from workspace_state import (
    IGNORED_POLICY_GLOBAL_V1,
    IGNORED_POLICY_SCOPED_READER_V1,
    StateError,
    StateUnavailable,
    ScopeReparseBudget,
    ignored_scope_digest,
    normalize_allow,
    repository_control_roots,
    repository_gitlinks,
    repository_index_records,
    repository_path_spelling_map,
    repository_root,
    state_payload,
    snapshot_ignored_policy,
    validate_snapshot,
    verify,
)


PROTOCOL = "aog.workspace-guard.v1"


class WorkspaceGuardError(RuntimeError):
    """The requested workspace cannot be represented or verified safely."""


class WorkspaceGuardUnavailable(WorkspaceGuardError):
    """Workspace inspection infrastructure is temporarily unavailable."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _root_key(path: Path) -> str:
    """Compare recorded roots without resolving them a second time."""

    return os.path.normcase(os.path.normpath(os.fspath(_absolute(path))))


def _has_git_marker(path: Path) -> bool:
    for ancestor in (path, *path.parents):
        checkpoint()
        try:
            (ancestor / ".git").lstat()
            return True
        except FileNotFoundError:
            continue
        except OSError as error:
            raise WorkspaceGuardUnavailable(
                "Git workspace marker is unavailable"
            ) from error
    return False


def discover_workspace(path: Path) -> tuple[str, Path]:
    """Return the authoritative backend and root without creating a repository."""

    checkpoint()
    requested = _absolute(path)
    try:
        return "git", repository_root(requested)
    except (OSError, StateUnavailable):
        if _has_git_marker(requested):
            raise WorkspaceGuardUnavailable(
                "Git workspace is present but its authoritative root is unavailable"
            )
        try:
            return "directory", directory_root(requested)
        except (OSError, DirectoryStateUnavailable) as error:
            raise WorkspaceGuardUnavailable(str(error)) from error
        except DirectoryStateError as error:
            raise WorkspaceGuardError(str(error)) from error
    except StateError as error:
        raise WorkspaceGuardError(str(error)) from error


def normalize_scope_groups(
    root: Path,
    groups: object,
    *,
    backend: str | None = None,
) -> list[list[dict[str, str]]]:
    """Normalize every plan node through one shared workspace discovery pass."""

    if backend is None:
        backend, workspace = discover_workspace(root)
    else:
        if backend not in {"git", "directory"}:
            raise WorkspaceGuardError("workspace backend is unsupported")
        workspace = _absolute(root)
    return _normalize_scope_groups(backend, workspace, groups)


def _normalize_scope_groups(
    backend: str,
    workspace: Path,
    groups: object,
    *,
    control_roots: tuple[Path, ...] | None = None,
    index_records: dict[str, list[dict[str, str]]] | None = None,
) -> list[list[dict[str, str]]]:
    if not isinstance(groups, list) or not groups:
        raise WorkspaceGuardError("workspace scope groups must be a non-empty list")
    requested_groups = [_syntax_scopes(group) for group in groups]
    try:
        if backend == "directory":
            normalized_groups = [
                [normalize_directory_scope(workspace, item) for item in group]
                for group in requested_groups
            ]
        else:
            active_control_roots = (
                repository_control_roots(workspace)
                if control_roots is None
                else control_roots
            )
            active_index_records = (
                repository_index_records(workspace)
                if index_records is None
                else index_records
            )
            gitlinks = repository_gitlinks(workspace, active_index_records)
            tracked_spellings = repository_path_spelling_map(active_index_records)
            directory_spellings: dict[str, frozenset[str]] = {}
            reparse_budget = ScopeReparseBudget()
            normalized_groups = [
                [
                    normalize_allow(
                        workspace,
                        f"{item['kind']}:{item['path']}",
                        protected_roots=active_control_roots,
                        gitlinks=gitlinks,
                        tracked_spellings=tracked_spellings,
                        directory_spellings=directory_spellings,
                        reparse_budget=reparse_budget,
                    )
                    for item in group
                ]
                for group in requested_groups
            ]
    except (DirectoryStateUnavailable, OSError, StateUnavailable) as error:
        raise WorkspaceGuardUnavailable(str(error)) from error
    except (DirectoryStateError, StateError) as error:
        raise WorkspaceGuardError(str(error)) from error
    result: list[list[dict[str, str]]] = []
    for normalized in normalized_groups:
        by_identity = {(item["kind"], item["path"]): item for item in normalized}
        if len(by_identity) != len(normalized):
            raise WorkspaceGuardError("workspace scopes contain duplicates or aliases")
        result.append([by_identity[key] for key in sorted(by_identity)])
    return result


def _syntax_scopes(value: object, *, allow_empty: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise WorkspaceGuardError("workspace scopes must be a non-empty list")
    try:
        normalized = [
            require_repository_scope(item, f"workspace scope {index}")
            for index, item in enumerate(value)
        ]
    except ProtocolHashError as error:
        raise WorkspaceGuardError(str(error)) from error
    by_identity = {(item["kind"], item["path"]): item for item in normalized}
    if len(by_identity) != len(normalized):
        raise WorkspaceGuardError("workspace scopes contain duplicates")
    return [by_identity[key] for key in sorted(by_identity)]


def capture(
    root: Path,
    *,
    scopes: object,
    writable: bool,
    workspace_mode: str = "light",
) -> dict[str, Any]:
    """Capture one immutable wave baseline."""

    if type(writable) is not bool:
        raise WorkspaceGuardError("wave baseline writable flag is invalid")
    backend, workspace = discover_workspace(root)
    control_roots = None
    index_records = None
    if backend == "git":
        try:
            control_roots = repository_control_roots(workspace)
            index_records = repository_index_records(workspace)
        except (OSError, StateUnavailable) as error:
            raise WorkspaceGuardUnavailable(str(error)) from error
        except StateError as error:
            raise WorkspaceGuardError(str(error)) from error
    normalized = _normalize_scope_groups(
        backend,
        workspace,
        [scopes],
        control_roots=control_roots,
        index_records=index_records,
    )[0]
    try:
        if backend == "git":
            ignored_policy = (
                IGNORED_POLICY_GLOBAL_V1
                if writable
                else IGNORED_POLICY_SCOPED_READER_V1
            )
            snapshot = state_payload(
                workspace,
                control_roots=control_roots,
                index_records=index_records,
                ignored_mode=workspace_mode,
                ignored_policy=ignored_policy,
                scopes=normalized,
            )
        else:
            snapshot = capture_directory_state(
                workspace,
                scopes=normalized,
                capture_mode="full" if writable else "scope",
                workspace_mode=workspace_mode,
            )
    except (DirectoryStateUnavailable, OSError, StateUnavailable) as error:
        raise WorkspaceGuardUnavailable(str(error)) from error
    except (DirectoryStateError, StateError) as error:
        raise WorkspaceGuardError(str(error)) from error
    return {
        "backend": backend,
        "protocol": PROTOCOL,
        "root": str(workspace),
        "scopes": normalized,
        "snapshot": snapshot,
        "state_id": snapshot["state_id"],
        "writable": writable,
    }


def validate_baseline(value: object) -> dict[str, Any]:
    required = {
        "backend",
        "protocol",
        "root",
        "scopes",
        "snapshot",
        "state_id",
        "writable",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise WorkspaceGuardError("wave baseline is malformed")
    if value.get("protocol") != PROTOCOL or value.get("backend") not in {
        "git",
        "directory",
    }:
        raise WorkspaceGuardError("wave baseline protocol is unsupported")
    if type(value.get("writable")) is not bool:
        raise WorkspaceGuardError("wave baseline writable flag is invalid")
    root_value = value.get("root")
    if not isinstance(root_value, str):
        raise WorkspaceGuardError("wave baseline root is not absolute")
    root = Path(root_value)
    if not root.is_absolute():
        raise WorkspaceGuardError("wave baseline root is not absolute")
    scopes = _syntax_scopes(value.get("scopes"))
    snapshot_value = value.get("snapshot")
    if not isinstance(snapshot_value, Mapping):
        raise WorkspaceGuardError("wave baseline snapshot is malformed")
    try:
        snapshot = (
            validate_snapshot(dict(snapshot_value))
            if value["backend"] == "git"
            else validate_directory_snapshot(snapshot_value)
        )
    except (DirectoryStateError, StateError) as error:
        raise WorkspaceGuardError(str(error)) from error
    if snapshot.get("state_id") != value.get("state_id"):
        raise WorkspaceGuardError("wave baseline identity is inconsistent")
    snapshot_root = (
        snapshot.get("repo_root")
        if value["backend"] == "git"
        else snapshot.get("root_path")
    )
    if not isinstance(snapshot_root, str) or _root_key(root) != _root_key(
        Path(snapshot_root)
    ):
        raise WorkspaceGuardError(
            "wave baseline snapshot root does not match its workspace root"
        )
    if value["backend"] == "git":
        ignored_policy = snapshot_ignored_policy(snapshot)
        if ignored_policy == IGNORED_POLICY_SCOPED_READER_V1 and value["writable"]:
            raise WorkspaceGuardError(
                "writable wave cannot use the scoped reader ignored policy"
            )
        if ignored_policy == IGNORED_POLICY_SCOPED_READER_V1 and snapshot.get(
            "ignored_scope_digest"
        ) != ignored_scope_digest(scopes):
            raise WorkspaceGuardError(
                "scoped reader ignored policy does not match wave scopes"
            )
    return {
        "backend": value["backend"],
        "protocol": PROTOCOL,
        "root": str(root),
        "scopes": scopes,
        "snapshot": dict(snapshot),
        "state_id": value["state_id"],
        "writable": value["writable"],
    }


def _overlaps(path: str, scopes: list[dict[str, str]]) -> bool:
    exact = {"kind": "exact", "path": path}
    return any(repository_scopes_overlap(exact, scope) for scope in scopes)


def verify_state(
    root: Path,
    baseline: object,
    *,
    allowed_scopes: object,
    owner_scopes: object,
    pre_spawn: bool = False,
) -> dict[str, Any]:
    """Verify a wave and return only the current owner's attributable delta."""

    checkpoint()
    bound = validate_baseline(baseline)
    backend, workspace = discover_workspace(root)
    if backend != bound["backend"] or _root_key(workspace) != _root_key(
        Path(bound["root"])
    ):
        raise WorkspaceGuardError("workspace no longer matches the wave baseline")
    # These scopes were canonicalized once when the immutable plan and wave were
    # built.  Result-time verification checks their syntax and wave binding without
    # repeating Git index/path-alias discovery three times per Hook call.
    allowed = _syntax_scopes(allowed_scopes, allow_empty=True)
    owner = _syntax_scopes(owner_scopes)
    try:
        if backend == "git":
            code, result, _current = verify(
                workspace,
                dict(bound["snapshot"]),
                allowed,
                entry_scopes=bound["scopes"],
            )
            if code != 0:
                raise WorkspaceGuardError(
                    "workspace verification failed: " + ", ".join(result["violations"])
                )
        elif pre_spawn:
            result = verify_directory_pre_spawn(
                workspace,
                bound["snapshot"],
                active_scopes=allowed,
                graph_scopes=bound["scopes"],
            )
            if result["verdict"] != "pass":
                raise WorkspaceGuardError(
                    "workspace verification failed: " + ", ".join(result["violations"])
                )
        else:
            result = verify_directory_state(
                workspace,
                bound["snapshot"],
                allowed_scopes=allowed,
            )
            if result["verdict"] != "pass":
                raise WorkspaceGuardError(
                    "workspace verification failed: " + ", ".join(result["violations"])
                )
    except (DirectoryStateUnavailable, OSError, StateError) as error:
        raise WorkspaceGuardUnavailable(str(error)) from error
    except DirectoryStateError as error:
        raise WorkspaceGuardError(str(error)) from error
    changed = list(result["changed_paths"])
    return {
        "baseline_id": bound["state_id"],
        "changed_paths": changed,
        "current_state": result["current_state"],
        "owner_changed_paths": [path for path in changed if _overlaps(path, owner)],
        "protocol": "aog.workspace-verification.v1",
        "verdict": "pass",
    }
