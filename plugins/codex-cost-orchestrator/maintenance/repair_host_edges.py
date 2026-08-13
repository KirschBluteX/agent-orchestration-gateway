#!/usr/bin/env python3
"""Offline audit and repair for stale Codex Desktop spawn edges.

This is deliberately outside the Hook path.  Codex Desktop owns its task-card
registry; this tool only closes an exact persisted ``open`` edge after the
child's own trusted rollout proves that its host lifecycle reached
``task_complete``.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Iterable, Iterator, Mapping


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from host_paths import HostPathError, host_path, is_within  # noqa: E402
from rollout_io import (  # noqa: E402
    RolloutError,
    is_rollout_path,
    iter_records,
)
from state_lock import StateLockBusy, acquire as acquire_state_lock  # noqa: E402


PROTOCOL = "cco.host-edge-repair.v2"
THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
AGENT_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]{0,127})+$")
CCO_ROLES = frozenset(
    {
        "cost_orchestrator_read_leaf",
        "cost_orchestrator_write_leaf",
    }
)
MAX_SESSION_META_BYTES = 64 * 1024
ROLLBACK_PROTOCOL = "cco.host-edge-rollback.v2"
ROLLBACK_RETENTION = 3
JOURNAL_PREFIX = "cco-host-edge-"
JOURNAL_SUFFIX = ".rollback.json"
JOURNAL_LOCK_IDENTITY = "host-edge-rollback-journals"

# Codex has used more than one spelling while reporting a native turn's start
# and interruption.  Starts after completion and any interruption make the
# current edge unsafe to close.  Other active-turn events are allowed only
# before the eventual task_complete event, so error completion stays supported.
START_EVENTS = frozenset(
    {
        "agent_start",
        "agent_started",
        "task_start",
        "task_started",
        "turn_start",
        "turn_started",
    }
)
INTERRUPTION_EVENTS = frozenset(
    {
        "agent_aborted",
        "agent_interrupted",
        "interrupted",
        "task_aborted",
        "task_interrupted",
        "turn_aborted",
        "turn_interrupted",
    }
)


class HostEdgeRepairError(RuntimeError):
    """Raised when host state cannot be audited or repaired safely."""


def _note_secondary_failure(
    primary: BaseException, *, operation: str, secondary: BaseException
) -> None:
    """Retain a cleanup failure without replacing the operation that failed first."""

    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(f"{operation} also failed: {secondary}")


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _resolved_plain_file(path: Path, *, root: Path, label: str) -> Path:
    try:
        absolute = host_path(path)
        trusted_root = host_path(root)
    except HostPathError as error:
        raise HostEdgeRepairError(f"{label} uses an unsupported host path") from error
    if any(_is_reparse(candidate) for candidate in (absolute, *absolute.parents)):
        raise HostEdgeRepairError(f"{label} cannot use a reparse ancestor")
    try:
        resolved = absolute.resolve(strict=True)
        resolved_root = trusted_root.resolve(strict=True)
    except OSError as error:
        raise HostEdgeRepairError(f"{label} is unavailable") from error
    if not is_within(resolved_root, resolved) or not resolved.is_file():
        raise HostEdgeRepairError(f"{label} is outside its trusted root")
    return resolved


def _resolved_plain_directory(path: Path, *, label: str) -> Path:
    try:
        absolute = host_path(path)
    except HostPathError as error:
        raise HostEdgeRepairError(f"{label} uses an unsupported host path") from error
    if any(_is_reparse(candidate) for candidate in (absolute, *absolute.parents)):
        raise HostEdgeRepairError(f"{label} cannot use a reparse ancestor")
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise HostEdgeRepairError(f"{label} is unavailable") from error
    if not resolved.is_dir():
        raise HostEdgeRepairError(f"{label} is not a directory")
    return resolved


def _trusted_codex_home(codex_home: Path) -> Path:
    return _resolved_plain_directory(codex_home, label="Codex home")


def _sessions_root(codex_home: Path) -> Path:
    sessions = _resolved_plain_directory(
        codex_home / "sessions", label="Codex sessions root"
    )
    if not is_within(codex_home, sessions):
        raise HostEdgeRepairError("Codex sessions root is outside Codex home")
    return sessions


def _discover_state_db(codex_home: Path) -> Path:
    candidates: list[tuple[int, Path]] = []
    for path in codex_home.glob("state_*.sqlite"):
        match = re.fullmatch(r"state_([0-9]+)\.sqlite", path.name)
        if match is not None:
            candidates.append((int(match.group(1)), path))
    if not candidates:
        raise HostEdgeRepairError("Codex state database was not found")
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return _resolved_plain_file(candidates[0][1], root=codex_home, label="state database")


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "threads": {"id", "rollout_path", "agent_path", "agent_role"},
        "thread_spawn_edges": {
            "parent_thread_id",
            "child_thread_id",
            "status",
        },
    }
    for table, columns in required.items():
        present = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not columns <= present:
            raise HostEdgeRepairError(f"Codex state database has no supported {table} schema")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HostEdgeRepairError("agent rollout proof is invalid") from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_host_lifecycle(records: Iterable[Mapping[str, Any]]) -> None:
    """Accept only a terminal host lifecycle with harmless trailing usage.

    ``task_complete`` is authoritative whether it represents normal completion,
    an ordinary error, or a usage-limit error.  Codex can append token_count
    records after that event; those records describe accounting, not a new turn.
    Every other record after completion is rejected fail-closed.
    """

    completed = False
    for record in records:
        record_type = record.get("type")
        if isinstance(record_type, str) and record_type in INTERRUPTION_EVENTS:
            raise HostEdgeRepairError("agent rollout is interrupted or aborted")
        if completed:
            if record_type == "token_count":
                continue
            if isinstance(record_type, str) and record_type in START_EVENTS:
                raise HostEdgeRepairError("agent rollout starts after task_complete")
            payload = record.get("payload")
            if record_type == "event_msg" and isinstance(payload, Mapping):
                event_type = payload.get("type")
                if not isinstance(event_type, str) or not event_type:
                    raise HostEdgeRepairError("agent rollout has a malformed tail")
                if event_type == "token_count":
                    continue
                if event_type in START_EVENTS:
                    raise HostEdgeRepairError("agent rollout starts after task_complete")
                if event_type in INTERRUPTION_EVENTS:
                    raise HostEdgeRepairError(
                        "agent rollout is interrupted after task_complete"
                    )
            raise HostEdgeRepairError("agent rollout has an unknown tail after task_complete")

        if record_type != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise HostEdgeRepairError("agent rollout has a malformed host lifecycle")
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise HostEdgeRepairError("agent rollout has a malformed host lifecycle")
        if event_type in INTERRUPTION_EVENTS:
            raise HostEdgeRepairError("agent rollout is interrupted or aborted")
        if event_type == "task_complete":
            completed = True

    if not completed:
        raise HostEdgeRepairError("agent rollout has no authoritative task_complete tail")


def _rollout_proof(path: Path) -> tuple[Mapping[str, Any], str]:
    records = iter_records(path)
    try:
        first = next(records)
    except StopIteration as error:
        raise HostEdgeRepairError("agent rollout is empty") from error
    except RolloutError as error:
        raise HostEdgeRepairError("agent rollout cannot be read") from error
    try:
        first_size = len(
            json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError) as error:
        raise HostEdgeRepairError("agent session metadata is invalid") from error
    if first_size > MAX_SESSION_META_BYTES:
        raise HostEdgeRepairError("agent session metadata exceeds the size limit")
    digest = hashlib.sha256()

    def observed() -> Iterable[Mapping[str, Any]]:
        for record in chain((first,), records):
            try:
                encoded = json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise HostEdgeRepairError("agent rollout proof is invalid") from error
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            yield record

    try:
        _validate_host_lifecycle(observed())
    except RolloutError as error:
        raise HostEdgeRepairError("agent rollout cannot be read") from error
    return first, "sha256:" + digest.hexdigest()


def _session_source(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    source = payload.get("source")
    if not isinstance(source, Mapping):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, Mapping):
        return None
    spawn = subagent.get("thread_spawn")
    return spawn if isinstance(spawn, Mapping) else None


def _source_matches_edge(
    source: Mapping[str, Any],
    *,
    child_thread_id: str,
    parent_thread_id: str,
    agent_path: str,
    agent_role: str,
) -> bool:
    if (
        source.get("agent_path") != agent_path
        or source.get("agent_role") != agent_role
    ):
        return False
    optional_bindings = {
        "child_thread_id": child_thread_id,
        "id": child_thread_id,
        "parent_thread_id": parent_thread_id,
    }
    return all(
        key not in source or source.get(key) == expected
        for key, expected in optional_bindings.items()
    )


def _validate_terminal_edge(
    row: sqlite3.Row,
    *,
    sessions_root: Path,
    all_native: bool = False,
    state_root: Path | None = None,
) -> dict[str, Any]:
    del state_root
    parent_value = row["parent_thread_id"]
    child_value = row["child_thread_id"]
    parent = parent_value if isinstance(parent_value, str) else ""
    child = child_value if isinstance(child_value, str) else ""
    agent_path = row["agent_path"]
    agent_role = row["agent_role"]
    result: dict[str, Any] = {
        "agent_path": agent_path if isinstance(agent_path, str) else None,
        "agent_role": agent_role if isinstance(agent_role, str) else None,
        "child_thread_id": child or str(child_value),
        "evidence": None,
        "parent_thread_id": parent or str(parent_value),
        "prior_status": row["status"] if isinstance(row["status"], str) else None,
        "proof_digest": None,
        "reason": None,
        "rollout_path": None,
        "verdict": "skipped",
    }
    try:
        if THREAD_ID.fullmatch(parent) is None or THREAD_ID.fullmatch(child) is None:
            raise HostEdgeRepairError("thread identity is not canonical")
        if not isinstance(agent_role, str) or not agent_role:
            raise HostEdgeRepairError("agent role is missing")
        if not all_native and agent_role not in CCO_ROLES:
            raise HostEdgeRepairError("agent role is not CCO-owned")
        if not isinstance(agent_path, str) or AGENT_PATH.fullmatch(agent_path) is None:
            raise HostEdgeRepairError("agent path is not canonical")
        rollout_value = row["rollout_path"]
        if not isinstance(rollout_value, str):
            raise HostEdgeRepairError("agent rollout path is missing")
        rollout = _resolved_plain_file(
            Path(rollout_value),
            root=sessions_root,
            label="agent rollout",
        )
        if not is_rollout_path(rollout):
            raise HostEdgeRepairError("agent rollout has an unsupported suffix")
        first, proof_digest = _rollout_proof(rollout)
        payload = first.get("payload")
        if first.get("type") != "session_meta" or not isinstance(payload, Mapping):
            raise HostEdgeRepairError("agent rollout does not begin with session metadata")
        source = _session_source(payload)
        if (
            payload.get("id") != child
            or payload.get("parent_thread_id") != parent
            or payload.get("agent_path") != agent_path
            or payload.get("agent_role") != agent_role
            or source is None
            or not _source_matches_edge(
                source,
                child_thread_id=child,
                parent_thread_id=parent,
                agent_path=agent_path,
                agent_role=agent_role,
            )
        ):
            raise HostEdgeRepairError("agent session metadata does not match the spawn edge")
    except HostEdgeRepairError as error:
        result["reason"] = str(error)
        return result
    result.update(
        {
            "evidence": "session_meta+host_lifecycle/task_complete",
            "proof_digest": proof_digest,
            "rollout_path": str(rollout),
            "verdict": "repairable",
        }
    )
    return result


def _validated_child_ids(child_thread_ids: list[str] | None) -> list[str] | None:
    if child_thread_ids is None:
        return None
    if any(
        not isinstance(child, str) or THREAD_ID.fullmatch(child) is None
        for child in child_thread_ids
    ):
        raise HostEdgeRepairError("child thread identities are invalid or duplicated")
    children = sorted(child_thread_ids)
    if len(set(children)) != len(children):
        raise HostEdgeRepairError("child thread identities are invalid or duplicated")
    return children


def _edge_rows(
    connection: sqlite3.Connection,
    *,
    parent_thread_id: str | None,
    child_thread_ids: list[str] | None,
    all_native: bool = False,
) -> list[sqlite3.Row]:
    if parent_thread_id is not None and (
        not isinstance(parent_thread_id, str)
        or THREAD_ID.fullmatch(parent_thread_id) is None
    ):
        raise HostEdgeRepairError("parent thread identity is invalid")
    children = _validated_child_ids(child_thread_ids)
    query = """
        SELECT
            edge.parent_thread_id,
            edge.child_thread_id,
            edge.status,
            threads.rollout_path,
            threads.agent_path,
            threads.agent_role
        FROM thread_spawn_edges AS edge
        JOIN threads ON threads.id = edge.child_thread_id
        WHERE edge.status = 'open'
    """
    parameters: list[object] = []
    if not all_native:
        query += " AND threads.agent_role IN (?, ?)"
        parameters.extend(sorted(CCO_ROLES))
    if parent_thread_id is not None:
        query += " AND edge.parent_thread_id = ?"
        parameters.append(parent_thread_id)
    if children:
        query += " AND edge.child_thread_id IN (" + ",".join("?" for _ in children) + ")"
        parameters.extend(children)
    query += " ORDER BY edge.child_thread_id"
    return connection.execute(query, parameters).fetchall()


def audit_edges(
    *,
    codex_home: Path,
    parent_thread_id: str | None,
    child_thread_ids: list[str] | None = None,
    all_native: bool = False,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Read only the current persisted edge state and trusted child rollouts.

    ``state_root`` remains an ignored compatibility argument.  Host liveness no
    longer depends on CCO lifecycle state, so a missing or stale state root must
    not suppress a valid host audit.
    """

    del state_root
    home = _trusted_codex_home(codex_home)
    database = _discover_state_db(home)
    sessions_root = _sessions_root(home)
    uri = database.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        _require_schema(connection)
        rows = _edge_rows(
            connection,
            parent_thread_id=parent_thread_id,
            child_thread_ids=child_thread_ids,
            all_native=all_native,
        )
    edges = [
        _validate_terminal_edge(
            row,
            sessions_root=sessions_root,
            all_native=all_native,
        )
        for row in rows
    ]
    return {
        "all_native": all_native,
        "edges": edges,
        "examined": len(edges),
        "journal": None,
        "mode": "check",
        "protocol": PROTOCOL,
        "repairable": sum(edge["verdict"] == "repairable" for edge in edges),
        "repaired": 0,
        "state_db": str(database),
    }


def _ensure_owner_only(path: Path, *, mode: int, label: str) -> None:
    try:
        os.chmod(path, mode)
    except OSError as error:
        raise HostEdgeRepairError(f"{label} permissions could not be secured") from error
    _verify_owner_only(path, label=label)


def _verify_owner_only(path: Path, *, label: str) -> None:
    try:
        current_mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise HostEdgeRepairError(f"{label} permissions could not be secured") from error
    # Windows maps POSIX modes onto its owner/read-only model.  chmod is still
    # the available owner-only request there; POSIX modes can be verified exactly.
    if os.name != "nt" and current_mode & 0o077:
        raise HostEdgeRepairError(f"{label} permissions are not owner-only")


def _sync_directory(path: Path, *, label: str) -> None:
    """Persist a journal namespace change on POSIX hosts."""

    if os.name == "nt":
        # Windows lacks a directory fsync equivalent.  Each Windows caller
        # reaches this boundary through MoveFileExW(MOVEFILE_WRITE_THROUGH)
        # instead; see _replace_journal and _remove_journal_durably.
        return

    descriptor: int | None = None
    sync_error: OSError | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError as error:
        sync_error = error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                if sync_error is None:
                    sync_error = error
    if sync_error is not None:
        raise HostEdgeRepairError(f"{label} cannot be synchronized") from sync_error


def _replace_journal(source: Path, target: Path) -> None:
    """Durably rename one same-directory journal or journal directory."""

    if os.name != "nt":
        os.replace(source, target)
        return
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    replace_existing = 0x00000001
    write_through = 0x00000008
    if not move_file(str(source), str(target), replace_existing | write_through):
        code = ctypes.get_last_error()
        raise OSError(code, "MoveFileExW rollback journal replacement failed", str(target))


def _remove_journal_durably(journal: Path) -> None:
    """Make an uncommitted journal unreachable across a host crash.

    POSIX persists an unlink through its containing directory.  Windows has no
    portable directory fsync, so a write-through rename first makes the record
    non-journal data; a later crash can leave only a harmless private tombstone.
    """

    if os.name != "nt":
        journal.unlink(missing_ok=True)
        _sync_directory(
            journal.parent,
            label="host-edge rollback journal directory",
        )
        return
    if not journal.exists():
        return
    tombstone = journal.parent / (
        f".{journal.name}.{secrets.token_hex(8)}.discarded"
    )
    _replace_journal(journal, tombstone)
    try:
        tombstone.unlink(missing_ok=True)
    except OSError:
        # The durable rename has already made recovery ignore this aborted
        # journal.  Surface the cleanup error to the caller, which preserves
        # any primary repair failure and records this secondary failure.
        raise


def _make_journal_directory(
    path: Path,
    *,
    label: str,
    owner_only: bool = True,
) -> bool:
    """Create one directory and publish its first use crash-durably."""

    if any(_is_reparse(candidate) for candidate in (path, *path.parents)):
        raise HostEdgeRepairError(f"{label} cannot use a reparse ancestor")
    if path.exists():
        if not path.is_dir():
            raise HostEdgeRepairError(f"{label} failed")
        if owner_only:
            _ensure_owner_only(path, mode=0o700, label=label)
        # A prior interrupted POSIX setup may have created this directory
        # before its caller could synchronize it; complete that first-use
        # publication before placing a journal inside it.
        _sync_directory(path, label=label)
        _sync_directory(path.parent, label=f"{label} parent directory")
        return False

    temporary: Path | None = None
    published = False
    try:
        temporary = Path(
            tempfile.mkdtemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
        )
        if owner_only:
            _ensure_owner_only(temporary, mode=0o700, label=label)
        _sync_directory(temporary, label=label)
        try:
            _replace_journal(temporary, path)
            published = True
        except FileExistsError:
            # A concurrent offline invocation may have published this exact
            # first-use directory after our initial existence check.  Its
            # durable directory is safe to reuse after the same checks.
            if not path.is_dir():
                raise
            if owner_only:
                _ensure_owner_only(path, mode=0o700, label=label)
        _sync_directory(path, label=label)
        _sync_directory(path.parent, label=f"{label} parent directory")
        return published
    except OSError as error:
        raise HostEdgeRepairError(f"{label} failed") from error
    finally:
        if temporary is not None and not published:
            try:
                temporary.rmdir()
            except OSError:
                pass


def _journal_root(codex_home: Path) -> Path:
    root = codex_home / "backups" / "cco-host-edge-repair"
    _make_journal_directory(
        root.parent,
        label="host-edge rollback journal parent directory",
        owner_only=False,
    )
    _make_journal_directory(root, label="host-edge rollback journal directory")
    if any(_is_reparse(candidate) for candidate in (root, *root.parents)):
        raise HostEdgeRepairError("host-edge rollback journal directory cannot use a reparse ancestor")
    _ensure_owner_only(root, mode=0o700, label="host-edge rollback journal directory")
    try:
        return root.resolve(strict=True)
    except OSError as error:
        raise HostEdgeRepairError("host-edge rollback journal directory failed") from error


@contextmanager
def _journal_directory_lock(root: Path) -> Iterator[None]:
    """Serialize journal publication with post-commit retention."""

    try:
        with acquire_state_lock(root, JOURNAL_LOCK_IDENTITY):
            yield
    except StateLockBusy as error:
        raise HostEdgeRepairError(
            "host-edge rollback journal directory is busy"
        ) from error


def _journal_edge(edge: Mapping[str, Any]) -> dict[str, str]:
    fields = (
        "parent_thread_id",
        "child_thread_id",
        "prior_status",
        "rollout_path",
        "proof_digest",
    )
    values = {field: edge.get(field) for field in fields}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise HostEdgeRepairError("host-edge rollback journal has incomplete edge proof")
    return {field: str(values[field]) for field in fields}


def _write_rollback_journal(
    database: Path,
    *,
    codex_home: Path,
    edges: list[Mapping[str, Any]],
    parent_thread_id: str | None = None,
    all_native: bool = False,
    journal_root: Path | None = None,
) -> Path:
    """Persist one journal while the caller holds its directory lock."""

    # A caller holding the directory lock passes its already-resolved root so
    # publication and retention cannot silently resolve different namespaces.
    root = journal_root if journal_root is not None else _journal_root(codex_home)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    operation_id = "sha256:" + hashlib.sha256(
        f"{timestamp}:{secrets.token_hex(16)}".encode("utf-8")
    ).hexdigest()
    journal_edges = [
        _journal_edge(edge)
        for edge in sorted(edges, key=lambda item: str(item["child_thread_id"]))
    ]
    if parent_thread_id is None:
        parent_ids = {edge["parent_thread_id"] for edge in journal_edges}
        if len(parent_ids) != 1:
            raise HostEdgeRepairError("host-edge rollback journal needs one exact parent")
        parent_thread_id = parent_ids.pop()
    unsigned: dict[str, Any] = {
        "edges": journal_edges,
        "operation": {
            "all_native": all_native,
            "created_at": timestamp,
            "id": operation_id,
            "kind": "close_open_edges",
            "parent_thread_id": parent_thread_id,
            "state_db": database.name,
        },
        "protocol": ROLLBACK_PROTOCOL,
    }
    document = {**unsigned, "sha256": _canonical_digest(unsigned)}
    journal = root / f"{JOURNAL_PREFIX}{timestamp}-{secrets.token_hex(8)}{JOURNAL_SUFFIX}"
    descriptor = -1
    temporary: Path | None = None
    published = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=root,
            prefix=f".{JOURNAL_PREFIX}",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        _ensure_owner_only(temporary, mode=0o600, label="host-edge rollback journal")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        _replace_journal(temporary, journal)
        published = True
        # Permissions were fixed while this was the private temporary file.
        # Verify only after its write-through publication so no metadata change
        # can outrun the journal namespace durability boundary.
        _verify_owner_only(journal, label="host-edge rollback journal")
        _sync_directory(root, label="host-edge rollback journal directory")
    except (HostEdgeRepairError, OSError) as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as cleanup_error:
                _note_secondary_failure(
                    error,
                    operation="host-edge rollback journal descriptor cleanup",
                    secondary=cleanup_error,
                )
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError as cleanup_error:
            _note_secondary_failure(
                error,
                operation="host-edge rollback journal temporary cleanup",
                secondary=cleanup_error,
            )
        if published:
            try:
                _remove_journal_durably(journal)
            except OSError as cleanup_error:
                _note_secondary_failure(
                    error,
                    operation="host-edge rollback journal cleanup",
                    secondary=cleanup_error,
                )
            except HostEdgeRepairError as cleanup_error:
                _note_secondary_failure(
                    error,
                    operation="host-edge rollback journal deletion synchronization",
                    secondary=cleanup_error,
                )
        if isinstance(error, HostEdgeRepairError):
            raise
        raise HostEdgeRepairError("host-edge rollback journal failed") from error
    return journal.resolve(strict=True)


def _prune_rollback_journals(journal: Path) -> None:
    """Keep the just-committed journal even when wall-clock ordering is wrong."""

    current = os.path.normcase(os.path.abspath(str(journal)))
    candidates: list[tuple[int, str, Path, tuple[int, int], str]] = []
    for path in journal.parent.iterdir():
        if not path.name.startswith(JOURNAL_PREFIX) or not path.name.endswith(JOURNAL_SUFFIX):
            continue
        try:
            before = path.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse(path) or not stat.S_ISREG(before.st_mode):
            continue
        identity = (before.st_dev, before.st_ino)
        canonical = os.path.normcase(os.path.abspath(str(path)))
        candidates.append((before.st_mtime_ns, path.name, path, identity, canonical))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    current_present = any(item[4] == current for item in candidates)
    retained_others = 0
    for _mtime, _name, stale, identity, canonical in candidates:
        if canonical == current:
            continue
        if retained_others < ROLLBACK_RETENTION - int(current_present):
            retained_others += 1
            continue
        try:
            observed = stale.lstat()
        except FileNotFoundError:
            continue
        if (
            _is_reparse(stale)
            or not stat.S_ISREG(observed.st_mode)
            or (observed.st_dev, observed.st_ino) != identity
        ):
            continue
        _remove_journal_durably(stale)


def _post_commit_warnings(
    journal: Path,
    *,
    directory_locked: bool = False,
) -> list[str]:
    try:
        if directory_locked:
            _prune_rollback_journals(journal)
        else:
            with _journal_directory_lock(journal.parent):
                _prune_rollback_journals(journal)
    except (HostEdgeRepairError, OSError):
        return ["repair committed, but old rollback journals could not be pruned"]
    return []


def _require_offline_repair(offline_confirmed: bool) -> None:
    if os.environ.get("CODEX_THREAD_ID", "").strip():
        raise HostEdgeRepairError("repair cannot run from an active Codex task")
    if not offline_confirmed:
        raise HostEdgeRepairError("repair requires explicit --offline-confirm")


def _discard_uncommitted_journal(journal: Path | None) -> None:
    if journal is None:
        return
    try:
        _remove_journal_durably(journal)
    except (HostEdgeRepairError, OSError) as error:
        raise HostEdgeRepairError(
            "repair rolled back, but its uncommitted rollback journal could not be removed"
        ) from error


def _commit_transaction(connection: sqlite3.Connection) -> None:
    """One testable commit boundary for proof finalization and compensation."""

    connection.commit()


def _rollback_transaction(connection: sqlite3.Connection) -> None:
    """One testable rollback boundary that never replaces a primary failure."""

    connection.rollback()


def _rollback_uncommitted_repair(
    connection: sqlite3.Connection,
    journal: Path | None,
    *,
    primary: BaseException,
) -> None:
    """Attempt both rollback steps while preserving the failure that started them."""

    try:
        _rollback_transaction(connection)
    except BaseException as rollback_error:
        _note_secondary_failure(
            primary,
            operation="host-edge database rollback",
            secondary=rollback_error,
        )
    try:
        _discard_uncommitted_journal(journal)
    except BaseException as cleanup_error:
        _note_secondary_failure(
            primary,
            operation="host-edge rollback journal cleanup",
            secondary=cleanup_error,
        )


def _rollback_committed_repair(
    connection: sqlite3.Connection,
    *,
    parent_thread_id: str,
    candidates: Mapping[str, Mapping[str, Any]],
    primary: BaseException,
) -> bool:
    """Compensate a commit whose immediately-following proof check changed.

    The journal remains until this transaction commits.  Returning ``False``
    leaves that durable undo record in place and adds all recovery failures to
    the original finalization failure.
    """

    try:
        connection.execute("BEGIN IMMEDIATE")
        restored = 0
        for child in sorted(candidates):
            edge = candidates[child]
            raw_rollout_path = edge.get("_rollout_db_path")
            if not isinstance(raw_rollout_path, str):
                raise HostEdgeRepairError(
                    "committed repair cannot restore an unbound rollout path"
                )
            changed = connection.execute(
                """
                UPDATE thread_spawn_edges
                SET status = 'open'
                WHERE parent_thread_id = ?
                  AND child_thread_id = ?
                  AND status = 'closed'
                  AND EXISTS (
                      SELECT 1
                      FROM threads
                      WHERE threads.id = thread_spawn_edges.child_thread_id
                        AND threads.agent_path = ?
                        AND threads.agent_role = ?
                        AND threads.rollout_path = ?
                  )
                """,
                (
                    parent_thread_id,
                    child,
                    edge["agent_path"],
                    edge["agent_role"],
                    raw_rollout_path,
                ),
            ).rowcount
            restored += max(0, changed)
        if restored != len(candidates):
            raise HostEdgeRepairError(
                "committed repair could not be rolled back safely"
            )
        _commit_transaction(connection)
    except BaseException as rollback_error:
        _note_secondary_failure(
            primary,
            operation="host-edge committed repair rollback",
            secondary=rollback_error,
        )
        try:
            _rollback_transaction(connection)
        except BaseException as cleanup_error:
            _note_secondary_failure(
                primary,
                operation="host-edge committed repair rollback cleanup",
                secondary=cleanup_error,
            )
        return False
    return True


def _verify_commit_proofs(
    edges: Iterable[Mapping[str, Any]], *, sessions_root: Path
) -> None:
    """Bind each journalled rollout proof immediately before the DB commit."""

    for edge in edges:
        rollout_value = edge.get("rollout_path")
        expected_digest = edge.get("proof_digest")
        try:
            if not isinstance(rollout_value, str) or not isinstance(expected_digest, str):
                raise HostEdgeRepairError("journalled rollout proof is incomplete")
            rollout = _resolved_plain_file(
                Path(rollout_value),
                root=sessions_root,
                label="agent rollout",
            )
            if str(rollout) != rollout_value:
                raise HostEdgeRepairError("journalled rollout path changed")
            _first, observed_digest = _rollout_proof(rollout)
            if observed_digest != expected_digest:
                raise HostEdgeRepairError("journalled rollout proof changed")
        except HostEdgeRepairError as error:
            raise HostEdgeRepairError(
                "requested child rollout proof changed before repair commit; "
                "no repair was committed"
            ) from error


def _verify_finalized_proofs(
    edges: Iterable[Mapping[str, Any]], *, sessions_root: Path
) -> None:
    """Detect a proof mutation that raced the final database commit.

    This second digest has a distinct purpose from the pre-commit check: a
    change observed here is compensated in a new durable transaction before
    the repair reports failure.  The documented offline boundary makes a
    mutation after this finalization check out of scope; it must have no live
    host writer while this utility runs.
    """

    _verify_commit_proofs(edges, sessions_root=sessions_root)


def repair_edges(
    *,
    codex_home: Path,
    parent_thread_id: str | None,
    child_thread_ids: list[str] | None,
    all_native: bool = False,
    offline_confirmed: bool = False,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Close exactly named proof-backed edges in one immediate transaction."""

    del state_root
    _require_offline_repair(offline_confirmed)
    if parent_thread_id is None:
        raise HostEdgeRepairError("--repair requires --parent-thread-id")
    if not child_thread_ids:
        raise HostEdgeRepairError("--repair requires at least one --child-thread-id")
    requested_children = _validated_child_ids(child_thread_ids)
    assert requested_children is not None
    requested = set(requested_children)
    result = audit_edges(
        codex_home=codex_home,
        parent_thread_id=parent_thread_id,
        child_thread_ids=requested_children,
        all_native=all_native,
    )
    result["mode"] = "repair"
    reported = {edge["child_thread_id"] for edge in result["edges"]}
    if reported != requested:
        missing = ",".join(sorted(requested - reported))
        raise HostEdgeRepairError(f"requested child edges are absent or not open: {missing}")
    unproven = [
        edge["child_thread_id"]
        for edge in result["edges"]
        if edge["verdict"] != "repairable"
    ]
    if unproven:
        raise HostEdgeRepairError(
            "requested child edges are not proof-backed: " + ",".join(sorted(unproven))
        )

    home = _trusted_codex_home(codex_home)
    database = _resolved_plain_file(
        Path(str(result["state_db"])), root=home, label="state database"
    )
    sessions_root = _sessions_root(home)
    journal: Path | None = None
    repaired = 0
    warnings: list[str] = []
    candidates: dict[str, dict[str, Any]] = {}
    state_committed = False
    uncommitted_cleanup_attempted = False
    with closing(sqlite3.connect(database, timeout=5.0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        # The journal has already reached durable storage before this
        # connection commits.  Force the matching SQLite durability boundary
        # for both the repair and any proof-race compensation below.
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_schema(connection)
            fresh_rows = _edge_rows(
                connection,
                parent_thread_id=parent_thread_id,
                child_thread_ids=requested_children,
                all_native=all_native,
            )
            fresh_edges = [
                _validate_terminal_edge(
                    row,
                    sessions_root=sessions_root,
                    all_native=all_native,
                )
                for row in fresh_rows
            ]
            candidates = {
                edge["child_thread_id"]: edge
                for edge in fresh_edges
                if edge["verdict"] == "repairable"
            }
            # Keep the exact DB spelling separately from the canonical path
            # recorded in the journal.  Hosts may expose equivalent short/long
            # or case-normalized path spellings, while SQLite must bind the
            # original persisted identity exactly.
            for row, edge in zip(fresh_rows, fresh_edges):
                if edge["verdict"] == "repairable":
                    raw_rollout_path = row["rollout_path"]
                    if not isinstance(raw_rollout_path, str):
                        raise HostEdgeRepairError(
                            "requested child rollout path is not persisted"
                        )
                    edge["_rollout_db_path"] = raw_rollout_path
            if set(candidates) != requested:
                raise HostEdgeRepairError(
                    "requested child edges changed before repair; no repair was committed"
                )
            journal_root = _journal_root(home)
            # SQLite serializes the state update; this lock also keeps a later
            # committed repair from pruning this repair's published-but-
            # uncommitted journal.
            with _journal_directory_lock(journal_root):
                journal = _write_rollback_journal(
                    database,
                    codex_home=home,
                    edges=list(candidates.values()),
                    parent_thread_id=parent_thread_id,
                    all_native=all_native,
                    journal_root=journal_root,
                )
                try:
                    for child in sorted(candidates):
                        edge = candidates[child]
                        changed = connection.execute(
                            """
                            UPDATE thread_spawn_edges
                            SET status = 'closed'
                            WHERE parent_thread_id = ?
                              AND child_thread_id = ?
                              AND status = 'open'
                              AND EXISTS (
                                  SELECT 1
                                  FROM threads
                                  WHERE threads.id = thread_spawn_edges.child_thread_id
                                    AND threads.agent_path = ?
                                    AND threads.agent_role = ?
                                    AND threads.rollout_path = ?
                              )
                            """,
                            (
                                parent_thread_id,
                                child,
                                edge["agent_path"],
                                edge["agent_role"],
                                edge["_rollout_db_path"],
                            ),
                        ).rowcount
                        repaired += max(0, changed)
                    if repaired != len(requested):
                        raise HostEdgeRepairError(
                            "requested child edges changed during repair; no repair was committed"
                        )
                    # This check binds the exact proof to the pending state write.
                    # A distinct post-commit check below detects the only remaining
                    # pre-commit/commit race and compensates it durably.
                    _verify_commit_proofs(candidates.values(), sessions_root=sessions_root)
                    _commit_transaction(connection)
                    state_committed = True
                    try:
                        _verify_finalized_proofs(
                            candidates.values(), sessions_root=sessions_root
                        )
                    except BaseException as error:
                        if _rollback_committed_repair(
                            connection,
                            parent_thread_id=parent_thread_id,
                            candidates=candidates,
                            primary=error,
                        ):
                            try:
                                _discard_uncommitted_journal(journal)
                            except BaseException as cleanup_error:
                                _note_secondary_failure(
                                    error,
                                    operation="host-edge compensated rollback journal cleanup",
                                    secondary=cleanup_error,
                                )
                        raise

                    if journal is not None:
                        warnings = _post_commit_warnings(journal, directory_locked=True)
                except BaseException as error:
                    if not state_committed:
                        uncommitted_cleanup_attempted = True
                        _rollback_uncommitted_repair(connection, journal, primary=error)
                    raise
        except BaseException as error:
            if not state_committed and not uncommitted_cleanup_attempted:
                _rollback_uncommitted_repair(connection, journal, primary=error)
            raise
    if journal is None:
        raise HostEdgeRepairError("host-edge rollback journal was not created")
    result["journal"] = str(journal)
    result["repaired"] = repaired
    result["warnings"] = warnings
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit persisted Codex Desktop spawn edges outside a live Codex task."
    )
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    # Kept only so existing offline runbooks do not fail at argument parsing.  The
    # value is intentionally unused: liveness derives exclusively from host state.
    parser.add_argument("--state-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--parent-thread-id")
    parser.add_argument(
        "--child-thread-id",
        action="append",
        dest="child_thread_ids",
        help="exact child to inspect or repair; repeat for several children",
    )
    parser.add_argument(
        "--all-native",
        action="store_true",
        help="include non-CCO native roles; repair still requires an exact parent and children",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="run the default read-only audit")
    mode.add_argument(
        "--repair",
        action="store_true",
        help="write a rollback journal, then close exact proof-backed edges",
    )
    parser.add_argument(
        "--offline-confirm",
        "--confirm-offline",
        dest="offline_confirmed",
        action="store_true",
        help="confirm that this repair is being run offline, outside an active Codex task",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.repair:
            result = repair_edges(
                codex_home=args.codex_home,
                parent_thread_id=args.parent_thread_id,
                child_thread_ids=args.child_thread_ids,
                all_native=args.all_native,
                offline_confirmed=args.offline_confirmed,
                state_root=args.state_root,
            )
        else:
            result = audit_edges(
                codex_home=args.codex_home,
                parent_thread_id=args.parent_thread_id,
                child_thread_ids=args.child_thread_ids,
                all_native=args.all_native,
                state_root=args.state_root,
            )
    except (HostEdgeRepairError, OSError, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        print(
            f"{result['mode'].upper()}: examined={result['examined']} "
            f"repairable={result['repairable']} repaired={result['repaired']} "
            f"state_db={result['state_db']}"
        )
        if result["journal"] is not None:
            print(f"JOURNAL: {result['journal']}")
        for warning in result.get("warnings", []):
            print(f"WARNING: {warning}", file=sys.stderr)
        for edge in result["edges"]:
            detail = edge["evidence"] or edge["reason"]
            print(f"{edge['verdict'].upper()}: {edge['child_thread_id']} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
