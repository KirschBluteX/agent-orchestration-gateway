#!/usr/bin/env python3
"""Audit stale Codex host spawn edges without changing CCO runtime state."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Mapping


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from host_paths import HostPathError, host_path, is_within  # noqa: E402
from packet_compiler import (  # noqa: E402
    CapsuleError,
    RESULT_HEADER,
    parse_result_message,
    validate_result_for_dispatch,
)
from rollout_io import (  # noqa: E402
    RolloutError,
    first_record,
    is_rollout_path,
    iter_tail_records,
)
from task_ledger import LedgerBusy, LedgerConflict, TaskLedger  # noqa: E402


PROTOCOL = "cco.host-edge-repair.v1"
THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
AGENT_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]{0,127})+$")
CCO_ROLES = frozenset(
    {
        "cost_orchestrator_read_leaf",
        "cost_orchestrator_write_leaf",
    }
)
MAX_SESSION_META_BYTES = 64 * 1024
MAX_TERMINAL_TAIL_BYTES = 1024 * 1024
ROLLBACK_PROTOCOL = "cco.host-edge-rollback.v1"
ROLLBACK_RETENTION = 3


class HostEdgeRepairError(RuntimeError):
    """Raised when host state cannot be audited safely."""


def _default_ledger_root() -> Path:
    configured = os.environ.get("CCO_LEDGER_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    )


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


def _text_values(value: object) -> list[str]:
    if isinstance(value, Mapping):
        return [text for child in value.values() for text in _text_values(child)]
    if isinstance(value, list):
        return [text for child in value for text in _text_values(child)]
    return [value] if isinstance(value, str) else []


def _assistant_result_messages(record: Mapping[str, Any]) -> list[str]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return []
    response_message = (
        record.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "assistant"
    )
    event_message = (
        record.get("type") == "event_msg"
        and payload.get("type") == "agent_message"
    )
    if not response_message and not event_message:
        return []
    return [
        text.strip()
        for text in _text_values(payload)
        if text.strip().startswith(RESULT_HEADER + "\n")
    ]


def _rollout_proof(path: Path) -> tuple[Mapping[str, Any], dict[str, Any]]:
    try:
        first = first_record(path)
    except RolloutError as error:
        raise HostEdgeRepairError("agent rollout cannot be read") from error
    try:
        first_size = len(
            json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError) as error:
        raise HostEdgeRepairError("agent session metadata is invalid") from error
    if first_size > MAX_SESSION_META_BYTES:
        raise HostEdgeRepairError("agent session metadata exceeds the size limit")
    last: Mapping[str, Any] | None = None
    result_messages: list[str] = []
    try:
        for record in iter_tail_records(path, max_bytes=MAX_TERMINAL_TAIL_BYTES):
            last = record
            result_messages.extend(_assistant_result_messages(record))
    except RolloutError as error:
        raise HostEdgeRepairError("agent rollout cannot be read") from error
    if first is None or last is None:
        raise HostEdgeRepairError("agent rollout is empty")
    terminal_payload = last.get("payload")
    if (
        last.get("type") != "event_msg"
        or not isinstance(terminal_payload, Mapping)
        or terminal_payload.get("type") != "task_complete"
    ):
        raise HostEdgeRepairError("agent rollout has no authoritative task_complete tail")
    if not result_messages:
        raise HostEdgeRepairError("agent rollout has no assistant CCO_RESULT")
    try:
        parsed = parse_result_message(result_messages[-1])
    except CapsuleError as error:
        raise HostEdgeRepairError("agent rollout CCO_RESULT is invalid") from error
    return first, parsed


def _validate_ledger_result(
    *,
    agent_path: str,
    agent_role: str,
    parent_thread_id: str,
    ledger_root: Path,
    result: Mapping[str, Any],
) -> None:
    if not (ledger_root / f"{parent_thread_id}.json").is_file():
        raise HostEdgeRepairError("CCO task ledger is unavailable")
    try:
        rows = TaskLedger(ledger_root, parent_thread_id).read_rows()
    except (LedgerBusy, LedgerConflict, OSError, ValueError) as error:
        raise HostEdgeRepairError("CCO task ledger is unavailable") from error
    matches = [
        row
        for row in rows
        if row.get("owner") == agent_path
        and row.get("input_sha256") == result.get("dispatch_sha256")
    ]
    if len(matches) != 1:
        raise HostEdgeRepairError("CCO task ledger has no exact terminal owner")
    row = matches[0]
    expected_role = (
        "cost_orchestrator_write_leaf"
        if row.get("role") == "worker"
        else "cost_orchestrator_read_leaf"
    )
    if agent_role != expected_role:
        raise HostEdgeRepairError("CCO task ledger role does not match the host edge")
    try:
        normalized = validate_result_for_dispatch(
            result,
            role=str(row.get("role")),
            acceptance_ids=row.get("acceptance_ids"),
        )
    except (CapsuleError, ValueError) as error:
        raise HostEdgeRepairError("CCO_RESULT does not match its ledger contract") from error
    if (
        normalized["status"] != "complete"
        or normalized["disposition"] not in {"retire", "accept"}
        or normalized["payload"]["blockers"]
        or normalized["payload"]["deviations"]
        or row.get("state") != "retired"
        or row.get("review_seed")
        != {
            "disposition": normalized["disposition"],
            "payload": normalized["payload"],
            "status": normalized["status"],
        }
    ):
        raise HostEdgeRepairError("CCO child is not proof-backed terminal work")


def _session_source(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    source = payload.get("source")
    if not isinstance(source, Mapping):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, Mapping):
        return None
    spawn = subagent.get("thread_spawn")
    return spawn if isinstance(spawn, Mapping) else None


def _validate_terminal_edge(
    row: sqlite3.Row,
    *,
    ledger_root: Path,
    sessions_root: Path,
) -> dict[str, Any]:
    parent = str(row["parent_thread_id"])
    child = str(row["child_thread_id"])
    agent_path = row["agent_path"]
    agent_role = row["agent_role"]
    result = {
        "agent_path": agent_path if isinstance(agent_path, str) else None,
        "agent_role": agent_role if isinstance(agent_role, str) else None,
        "child_thread_id": child,
        "evidence": None,
        "parent_thread_id": parent,
        "reason": None,
        "verdict": "skipped",
    }
    try:
        if THREAD_ID.fullmatch(parent) is None or THREAD_ID.fullmatch(child) is None:
            raise HostEdgeRepairError("thread identity is not canonical")
        if agent_role not in CCO_ROLES:
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
        first, cco_result = _rollout_proof(rollout)
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
            or source.get("agent_path") != agent_path
            or source.get("agent_role") != agent_role
        ):
            raise HostEdgeRepairError("agent session metadata does not match the spawn edge")
        _validate_ledger_result(
            agent_path=agent_path,
            agent_role=agent_role,
            parent_thread_id=parent,
            ledger_root=ledger_root,
            result=cco_result,
        )
    except HostEdgeRepairError as error:
        result["reason"] = str(error)
        return result
    result.update(
        {
            "evidence": "CCO_RESULT+TaskLedger+event_msg/task_complete",
            "verdict": "repairable",
        }
    )
    return result


def _edge_rows(
    connection: sqlite3.Connection,
    *,
    parent_thread_id: str | None,
    child_thread_ids: list[str] | None,
) -> list[sqlite3.Row]:
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
          AND threads.agent_role IN (?, ?)
    """
    parameters: list[object] = sorted(CCO_ROLES)
    if parent_thread_id is not None:
        if THREAD_ID.fullmatch(parent_thread_id) is None:
            raise HostEdgeRepairError("parent thread identity is invalid")
        query += " AND edge.parent_thread_id = ?"
        parameters.append(parent_thread_id)
    if child_thread_ids:
        children = sorted(set(child_thread_ids))
        if len(children) != len(child_thread_ids) or any(
            THREAD_ID.fullmatch(child) is None for child in children
        ):
            raise HostEdgeRepairError("child thread identities are invalid or duplicated")
        query += " AND edge.child_thread_id IN (" + ",".join("?" for _ in children) + ")"
        parameters.extend(children)
    query += " ORDER BY edge.child_thread_id"
    return connection.execute(query, parameters).fetchall()


def audit_edges(
    *,
    codex_home: Path,
    ledger_root: Path,
    parent_thread_id: str | None,
    child_thread_ids: list[str] | None = None,
) -> dict[str, Any]:
    try:
        home = host_path(codex_home).resolve(strict=True)
    except (HostPathError, OSError) as error:
        raise HostEdgeRepairError("Codex home is unavailable") from error
    if _is_reparse(home):
        raise HostEdgeRepairError("Codex home cannot be a reparse point")
    ledger = _resolved_plain_directory(ledger_root, label="CCO ledger root")
    database = _discover_state_db(home)
    sessions_root = (home / "sessions").resolve(strict=True)
    uri = database.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        _require_schema(connection)
        rows = _edge_rows(
            connection,
            parent_thread_id=parent_thread_id,
            child_thread_ids=child_thread_ids,
        )
    edges = [
        _validate_terminal_edge(row, ledger_root=ledger, sessions_root=sessions_root)
        for row in rows
    ]
    return {
        "backup": None,
        "edges": edges,
        "examined": len(edges),
        "mode": "check",
        "ledger_root": str(ledger),
        "protocol": PROTOCOL,
        "repairable": sum(edge["verdict"] == "repairable" for edge in edges),
        "repaired": 0,
        "state_db": str(database),
    }


def _write_rollback_journal(
    database: Path,
    *,
    codex_home: Path,
    edges: list[Mapping[str, Any]],
) -> Path:
    """Persist only the rows needed to undo this repair while the DB is locked."""

    backup_root = codex_home / "backups" / "cco-host-edge-repair"
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(backup_root, 0o700)
    except OSError:
        pass
    if any(_is_reparse(candidate) for candidate in (backup_root, *backup_root.parents)):
        raise HostEdgeRepairError("host-edge backup directory cannot use a reparse ancestor")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_root / f"{database.stem}-{timestamp}.rollback.json"
    unsigned = {
        "created_at": timestamp,
        "edges": [
            {
                "child_thread_id": str(edge["child_thread_id"]),
                "parent_thread_id": str(edge["parent_thread_id"]),
                "prior_status": "open",
            }
            for edge in sorted(edges, key=lambda item: str(item["child_thread_id"]))
        ],
        "protocol": ROLLBACK_PROTOCOL,
        "state_db": database.name,
    }
    encoded = json.dumps(
        unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    document = {
        **unsigned,
        "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        dir=backup_root,
        prefix=".cco-host-edge-",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as stream:
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
        os.replace(temporary, backup)
        try:
            os.chmod(backup, 0o600)
        except OSError:
            pass
    except OSError as error:
        raise HostEdgeRepairError("host-edge rollback journal failed") from error
    finally:
        temporary.unlink(missing_ok=True)
    return backup.resolve(strict=True)


def _prune_rollback_journals(backup: Path) -> None:
    pattern = re.compile(
        rf"^{re.escape(backup.name.split('-', 1)[0])}-.*\.rollback\.json$"
    )
    candidates = sorted(
        (
            path
            for path in backup.parent.iterdir()
            if path.is_file() and not path.is_symlink() and pattern.fullmatch(path.name)
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for stale in candidates[ROLLBACK_RETENTION:]:
        stale.unlink(missing_ok=True)


def repair_edges(
    *,
    codex_home: Path,
    ledger_root: Path,
    parent_thread_id: str | None,
    child_thread_ids: list[str] | None,
) -> dict[str, Any]:
    if parent_thread_id is None:
        raise HostEdgeRepairError("--repair requires --parent-thread-id")
    if not child_thread_ids:
        raise HostEdgeRepairError("--repair requires at least one --child-thread-id")
    requested = set(child_thread_ids)
    result = audit_edges(
        codex_home=codex_home,
        ledger_root=ledger_root,
        parent_thread_id=parent_thread_id,
        child_thread_ids=child_thread_ids,
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

    home = host_path(codex_home).resolve(strict=True)
    ledger = Path(str(result["ledger_root"]))
    database = Path(result["state_db"])
    backup: Path | None = None
    repaired = 0
    with closing(sqlite3.connect(database, timeout=5.0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_schema(connection)
            sessions_root = (home / "sessions").resolve(strict=True)
            fresh_edges = [
                _validate_terminal_edge(
                    row,
                    ledger_root=ledger,
                    sessions_root=sessions_root,
                )
                for row in _edge_rows(
                    connection,
                    parent_thread_id=parent_thread_id,
                    child_thread_ids=child_thread_ids,
                )
            ]
            candidates = {
                edge["child_thread_id"]: edge
                for edge in fresh_edges
                if edge["verdict"] == "repairable"
            }
            if set(candidates) != requested:
                raise HostEdgeRepairError(
                    "requested child edges changed before repair; no repair was committed"
                )
            backup = _write_rollback_journal(
                database,
                codex_home=home,
                edges=list(candidates.values()),
            )
            for child, edge in candidates.items():
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
                      )
                    """,
                    (
                        parent_thread_id,
                        child,
                        edge["agent_path"],
                        edge["agent_role"],
                    ),
                ).rowcount
                repaired += max(0, changed)
            if repaired != len(requested):
                raise HostEdgeRepairError(
                    "requested child edges changed during repair; no repair was committed"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    if backup is None:
        raise HostEdgeRepairError("host-edge rollback journal was not created")
    _prune_rollback_journals(backup)
    result["backup"] = str(backup)
    result["repaired"] = repaired
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit proof-backed stale CCO spawn edges in Codex Desktop state."
    )
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument(
        "--ledger-root",
        type=Path,
        default=_default_ledger_root(),
        help="CCO task-ledger directory used to prove terminal ownership",
    )
    parser.add_argument("--parent-thread-id")
    parser.add_argument(
        "--child-thread-id",
        action="append",
        dest="child_thread_ids",
        help="exact child to inspect or repair; repeat for several children",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="run the default read-only audit")
    mode.add_argument(
        "--repair",
        action="store_true",
        help="write a minimal rollback journal, then close proof-backed edges for one parent",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.repair:
            result = repair_edges(
                codex_home=args.codex_home,
                ledger_root=args.ledger_root,
                parent_thread_id=args.parent_thread_id,
                child_thread_ids=args.child_thread_ids,
            )
        else:
            result = audit_edges(
                codex_home=args.codex_home,
                ledger_root=args.ledger_root,
                parent_thread_id=args.parent_thread_id,
                child_thread_ids=args.child_thread_ids,
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
        if result["backup"] is not None:
            print(f"BACKUP: {result['backup']}")
        for edge in result["edges"]:
            detail = edge["evidence"] or edge["reason"]
            print(f"{edge['verdict'].upper()}: {edge['child_thread_id']} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
