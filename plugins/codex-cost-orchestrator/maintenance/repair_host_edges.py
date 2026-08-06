#!/usr/bin/env python3
"""Audit stale Codex host spawn edges without changing CCO runtime state."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Any, Mapping


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


class HostEdgeRepairError(RuntimeError):
    """Raised when host state cannot be audited safely."""


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
    absolute = Path(os.path.abspath(path.expanduser()))
    if any(_is_reparse(candidate) for candidate in (absolute, *absolute.parents)):
        raise HostEdgeRepairError(f"{label} cannot use a reparse ancestor")
    try:
        resolved = absolute.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise HostEdgeRepairError(f"{label} is unavailable") from error
    try:
        inside = (
            os.path.commonpath(
                (os.path.normcase(str(resolved_root)), os.path.normcase(str(resolved)))
            )
            == os.path.normcase(str(resolved_root))
        )
    except ValueError as error:
        raise HostEdgeRepairError(f"{label} is outside its trusted root") from error
    if not inside or not resolved.is_file():
        raise HostEdgeRepairError(f"{label} is outside its trusted root")
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


def _first_record(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as stream:
            line = stream.readline(MAX_SESSION_META_BYTES + 1)
    except OSError as error:
        raise HostEdgeRepairError("agent rollout cannot be read") from error
    if not line or len(line) > MAX_SESSION_META_BYTES:
        raise HostEdgeRepairError("agent session metadata is missing or oversized")
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostEdgeRepairError("agent session metadata is invalid") from error
    if not isinstance(record, Mapping):
        raise HostEdgeRepairError("agent session metadata is invalid")
    return record


def _last_record(path: Path) -> Mapping[str, Any]:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            start = max(0, size - MAX_TERMINAL_TAIL_BYTES)
            stream.seek(start)
            data = stream.read(MAX_TERMINAL_TAIL_BYTES)
    except OSError as error:
        raise HostEdgeRepairError("agent rollout tail cannot be read") from error
    if start:
        separator = data.find(b"\n")
        data = b"" if separator < 0 else data[separator + 1 :]
    lines = [line for line in data.splitlines() if line.strip()]
    if not lines:
        raise HostEdgeRepairError("agent rollout has no terminal record")
    try:
        record = json.loads(lines[-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostEdgeRepairError("agent rollout terminal record is invalid") from error
    if not isinstance(record, Mapping):
        raise HostEdgeRepairError("agent rollout terminal record is invalid")
    return record


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
        first = _first_record(rollout)
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
        terminal = _last_record(rollout)
        terminal_payload = terminal.get("payload")
        if (
            terminal.get("type") != "event_msg"
            or not isinstance(terminal_payload, Mapping)
            or terminal_payload.get("type") != "task_complete"
        ):
            raise HostEdgeRepairError("agent rollout has no authoritative task_complete tail")
    except HostEdgeRepairError as error:
        result["reason"] = str(error)
        return result
    result.update(
        {
            "evidence": "event_msg/task_complete",
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
    parent_thread_id: str | None,
    child_thread_ids: list[str] | None = None,
) -> dict[str, Any]:
    home = Path(os.path.abspath(codex_home.expanduser())).resolve(strict=True)
    if _is_reparse(home):
        raise HostEdgeRepairError("Codex home cannot be a reparse point")
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
        _validate_terminal_edge(row, sessions_root=sessions_root)
        for row in rows
    ]
    return {
        "backup": None,
        "edges": edges,
        "examined": len(edges),
        "mode": "check",
        "protocol": PROTOCOL,
        "repairable": sum(edge["verdict"] == "repairable" for edge in edges),
        "repaired": 0,
        "state_db": str(database),
    }


def _backup_database(database: Path, *, codex_home: Path) -> Path:
    backup_root = codex_home / "backups" / "cco-host-edge-repair"
    backup_root.mkdir(parents=True, exist_ok=True)
    if any(_is_reparse(candidate) for candidate in (backup_root, *backup_root.parents)):
        raise HostEdgeRepairError("host-edge backup directory cannot use a reparse ancestor")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_root / f"{database.stem}-{timestamp}.sqlite"
    source_uri = database.as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
                destination.commit()
    except sqlite3.Error as error:
        backup.unlink(missing_ok=True)
        raise HostEdgeRepairError("Codex state database backup failed") from error
    return backup.resolve(strict=True)


def repair_edges(
    *,
    codex_home: Path,
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

    home = Path(os.path.abspath(codex_home.expanduser())).resolve(strict=True)
    database = Path(result["state_db"])
    backup = _backup_database(database, codex_home=home)
    repaired = 0
    with closing(sqlite3.connect(database, timeout=5.0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_schema(connection)
            sessions_root = (home / "sessions").resolve(strict=True)
            fresh_edges = [
                _validate_terminal_edge(row, sessions_root=sessions_root)
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
                    "requested child edges changed after backup; no repair was committed"
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
    result["backup"] = str(backup)
    result["repaired"] = repaired
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit proof-backed stale CCO spawn edges in Codex Desktop state."
    )
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
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
        help="back up host state, then close proof-backed edges for one parent",
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
            )
        else:
            result = audit_edges(
                codex_home=args.codex_home,
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
