#!/usr/bin/env python3
"""Fail-closed conversion of prepared graphs into short native-spawn references.

The transaction is intentionally only a crash-safe gate around Codex's native
Agent tool.  It never starts, schedules, polls, or waits for an agent itself.
Full v7 spawn inputs live briefly in immutable files outside the repository;
the durable state file contains only hashes, native argument metadata, and the
small lifecycle cursor needed by hooks.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Iterator, Mapping

from packet_compiler import parse_message
from protocol_hash import ProtocolHashError, canonical_bytes, require_repository_scope
from workspace_state import StateError, repository_root

from prepared_graph import (
    graph_scopes,
    load_artifact,
    verify_artifact_workspace,
    verify_pre_spawn_workspace,
)


BATCH_PROTOCOL = "cco.dispatch-batch.v2"
STATE_PROTOCOL = "cco.dispatch-transaction-state.v1"
BUNDLE_PROTOCOL = "cco.dispatch-transaction-bundle.v1"
REF_HEADER = "CCO_DISPATCH_REF cco.dispatch-batch.v2"
ABORT_HEADER = "CCO_ABORT_DISPATCH cco.dispatch-batch.v2"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
NODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
TASK_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
SPAWN_FIELDS = frozenset(
    {"agent_type", "fork_turns", "message", "model", "reasoning_effort", "task_name"}
)
NODE_STATES = frozenset({"prepared", "dispatching", "active", "rejected", "fenced", "terminal"})
TRANSACTION_STATES = NODE_STATES
_LOCK_WAIT_SECONDS = 0.25
_STALE_LOCK_SECONDS = 60.0
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_MAX_TRANSACTIONS = 32


class DispatchTransactionError(RuntimeError):
    """A short spawn reference cannot safely become a native spawn."""


class DispatchTransactionBusy(DispatchTransactionError):
    """The tiny transaction cursor could not be locked promptly."""


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise DispatchTransactionError(f"{label} is invalid")
    return value


def _digest(domain: bytes, value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(domain + canonical_bytes(dict(value))).hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _has_reparse_ancestor(path: Path) -> bool:
    absolute = Path(os.path.abspath(path.expanduser()))
    return any(_is_reparse(candidate) for candidate in (absolute, *absolute.parents))


def _external_root(ledger_root: Path, repo: Path) -> Path:
    resolved = _resolved_ledger_root(ledger_root)
    try:
        repository = repository_root(Path(repo)).resolve()
    except (OSError, StateError) as error:
        raise DispatchTransactionError("transaction directory cannot be resolved") from error
    if resolved == repository or repository in resolved.parents or resolved in repository.parents:
        raise DispatchTransactionError("transaction directory must be outside repository")
    return resolved


def _resolved_ledger_root(ledger_root: Path) -> Path:
    root = Path(os.path.abspath(Path(ledger_root).expanduser()))
    if _has_reparse_ancestor(root):
        raise DispatchTransactionError("transaction directory cannot use a reparse ancestor")
    try:
        resolved = root.resolve()
    except OSError as error:
        raise DispatchTransactionError("transaction directory cannot be resolved") from error
    return resolved


def ledger_root_for_payload(payload: Mapping[str, Any]) -> Path:
    configured = os.environ.get("CCO_LEDGER_DIR")
    root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    return _resolved_ledger_root(root)


def _session(value: object) -> str:
    if not isinstance(value, str) or SESSION_ID.fullmatch(value) is None:
        raise DispatchTransactionError("transaction session identity is invalid")
    return value


def _native_response_rejected(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in {"error", "iserror", "rejected", "failed"} and (
                child is True or (isinstance(child, str) and child)
            ):
                return True
            if normalized == "status" and isinstance(child, str) and child.casefold() in {"error", "failed", "rejected"}:
                return True
            if _native_response_rejected(child):
                return True
    elif isinstance(value, list):
        return any(_native_response_rejected(child) for child in value)
    return False


def _task_paths(value: Any, *, key: str = "") -> set[str]:
    if isinstance(value, Mapping):
        found: set[str] = set()
        for child_key, child in value.items():
            found.update(_task_paths(child, key=str(child_key)))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for child in value:
            found.update(_task_paths(child, key=key))
        return found
    if not isinstance(value, str):
        return set()
    if TASK_PATH.fullmatch(value):
        return {value}
    if key in {"task_name", "canonical_task_path", "task_path", "agent_name"} and NODE.fullmatch(value):
        return {f"/root/{value}"}
    try:
        return _task_paths(json.loads(value), key=key)
    except (TypeError, ValueError):
        return set()


def _state_path(root: Path, session_id: str) -> Path:
    return root / f"{session_id}.dispatch-transactions.json"


def _lock_path(root: Path, session_id: str) -> Path:
    return root / f".{session_id}.dispatch-transactions.lock"


def bundle_path(ledger_root: Path, session_id: str, transaction_id: str, spawn_ref: str) -> Path:
    """Return the one exact external immutable bundle location for a reference."""

    session = _session(session_id)
    transaction = _sha(transaction_id, "transaction identity")
    reference = _sha(spawn_ref, "spawn reference")
    root = Path(os.path.abspath(Path(ledger_root).expanduser())).resolve()
    return root.parent / "dispatch-bundles" / f"{session}-{transaction[7:]}" / f"{reference[7:]}.json"


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(path.parent):
        raise DispatchTransactionError("transaction output directory is a reparse point")
    payload = canonical_bytes(dict(value))
    descriptor, temporary_name = tempfile.mkstemp(prefix=".cco-transaction-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(root: Path, session_id: str) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(root, session_id)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _STALE_LOCK_SECONDS:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise DispatchTransactionBusy("transaction lock acquisition timed out")
            time.sleep(0.01)
    os.close(descriptor)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _empty_document(session_id: str) -> dict[str, Any]:
    return {"protocol": STATE_PROTOCOL, "session_id": session_id, "transactions": {}}


def _read_document(root: Path, session_id: str) -> dict[str, Any]:
    path = _state_path(root, session_id)
    if not path.exists():
        return _empty_document(session_id)
    if path.is_symlink() or not path.is_file():
        raise DispatchTransactionError("transaction state is unavailable")
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            raise DispatchTransactionError("transaction state exceeds the size limit")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        if isinstance(error, DispatchTransactionError):
            raise
        raise DispatchTransactionError("transaction state is unreadable") from error
    if (
        not isinstance(document, Mapping)
        or set(document) != {"protocol", "session_id", "transactions"}
        or document.get("protocol") != STATE_PROTOCOL
        or document.get("session_id") != session_id
        or not isinstance(document.get("transactions"), Mapping)
        or len(document["transactions"]) > _MAX_TRANSACTIONS
    ):
        raise DispatchTransactionError("transaction state is corrupted")
    copied = {
        "protocol": STATE_PROTOCOL,
        "session_id": session_id,
        "transactions": {str(key): deepcopy(value) for key, value in document["transactions"].items()},
    }
    for transaction_id, transaction in copied["transactions"].items():
        _validate_transaction(transaction_id, transaction)
    return copied


def _write_document(root: Path, session_id: str, document: Mapping[str, Any]) -> None:
    if set(document) != {"protocol", "session_id", "transactions"}:
        raise DispatchTransactionError("transaction state cannot be committed")
    _write_atomic(_state_path(root, session_id), document)


def _candidate(value: object, *, node: str) -> dict[str, Any]:
    required = {
        "agent_type", "fork_turns", "input_sha256", "model", "rank", "reasoning_effort",
        "ref", "state", "task_name",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise DispatchTransactionError("transaction candidate is corrupted")
    if value["state"] not in {"prepared", "dispatching", "active", "rejected", "fenced", "terminal"}:
        raise DispatchTransactionError("transaction candidate state is corrupted")
    if (
        _sha(value["ref"], "transaction spawn reference") != value["ref"]
        or _sha(value["input_sha256"], "transaction input identity") != value["input_sha256"]
        or isinstance(value["rank"], bool)
        or not isinstance(value["rank"], int)
        or value["rank"] < 1
        or not all(isinstance(value[name], str) and value[name] for name in ("agent_type", "fork_turns", "model", "reasoning_effort", "task_name"))
    ):
        raise DispatchTransactionError("transaction candidate identity is corrupted")
    return dict(value)


def _scopes(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise DispatchTransactionError(f"{label} is corrupted")
    try:
        scopes = [require_repository_scope(item, f"{label}[{index}]") for index, item in enumerate(value)]
    except ProtocolHashError as error:
        raise DispatchTransactionError(f"{label} is corrupted") from error
    if scopes != sorted(scopes, key=lambda item: (item["kind"], item["path"])) or len({(item["kind"], item["path"]) for item in scopes}) != len(scopes):
        raise DispatchTransactionError(f"{label} is not canonical")
    return scopes


def _node_state(value: object, *, expected_node: str) -> dict[str, Any]:
    required = {"call_id", "candidates", "dispatch_ref", "eligible_ref", "owner", "scopes", "state"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise DispatchTransactionError("transaction node state is corrupted")
    if value["state"] not in NODE_STATES:
        raise DispatchTransactionError("transaction node lifecycle is corrupted")
    if value["call_id"] is not None and (not isinstance(value["call_id"], str) or not value["call_id"]):
        raise DispatchTransactionError("transaction node call identity is corrupted")
    if value["owner"] is not None and (
        not isinstance(value["owner"], str) or TASK_PATH.fullmatch(value["owner"]) is None
    ):
        raise DispatchTransactionError("transaction node owner is corrupted")
    candidates = [_candidate(candidate, node=expected_node) for candidate in value["candidates"]] if isinstance(value["candidates"], list) else None
    if not candidates or [candidate["rank"] for candidate in candidates] != list(range(1, len(candidates) + 1)):
        raise DispatchTransactionError("transaction node candidates are corrupted")
    refs = [candidate["ref"] for candidate in candidates]
    if len(set(refs)) != len(refs):
        raise DispatchTransactionError("transaction node references are not unique")
    eligible = value["eligible_ref"]
    if eligible is not None and eligible not in refs:
        raise DispatchTransactionError("transaction eligible reference is corrupted")
    dispatch_ref = value["dispatch_ref"]
    if dispatch_ref is not None and dispatch_ref not in refs:
        raise DispatchTransactionError("transaction dispatch reference is corrupted")
    prepared = [candidate for candidate in candidates if candidate["state"] == "prepared"]
    dispatching = [candidate for candidate in candidates if candidate["state"] == "dispatching"]
    active = [candidate for candidate in candidates if candidate["state"] == "active"]
    state = value["state"]
    if state in {"prepared", "rejected"}:
        if value["call_id"] is not None or dispatch_ref is not None or value["owner"] is not None:
            raise DispatchTransactionError("transaction pending node cursor is corrupted")
        expected_eligible = prepared[0]["ref"] if prepared else None
        if eligible != expected_eligible:
            raise DispatchTransactionError("transaction pending node skipped a fallback")
    elif state == "dispatching":
        if (
            value["call_id"] is None
            or dispatch_ref is None
            or eligible != dispatch_ref
            or len(dispatching) != 1
            or dispatching[0]["ref"] != dispatch_ref
            or value["owner"] is not None
        ):
            raise DispatchTransactionError("transaction dispatch cursor is corrupted")
    elif state == "active":
        if (
            value["owner"] is None
            or value["call_id"] is not None
            or dispatch_ref is not None
            or eligible is not None
            or len(active) != 1
            or prepared
            or dispatching
        ):
            raise DispatchTransactionError("transaction active node state is corrupted")
    elif state == "fenced":
        if value["owner"] is not None or eligible is not None or active or prepared or dispatching:
            raise DispatchTransactionError("transaction fenced node state is corrupted")
    elif state == "terminal":
        if value["owner"] is not None or value["call_id"] is not None or dispatch_ref is not None or eligible is not None or active or prepared or dispatching:
            raise DispatchTransactionError("transaction terminal node state is corrupted")
    return {
        "call_id": value["call_id"],
        "candidates": candidates,
        "dispatch_ref": dispatch_ref,
        "eligible_ref": eligible,
        "owner": value["owner"],
        "scopes": _scopes(value["scopes"], f"transaction node {expected_node} scopes"),
        "state": value["state"],
    }


def _validate_transaction(transaction_id: str, value: object) -> dict[str, Any]:
    required = {
        "baseline", "baseline_path", "commit", "created_at", "graph_sha256", "identity_sha256",
        "nodes", "recovery_count", "repo", "state", "transaction_id", "workspace_mode",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise DispatchTransactionError("transaction record is corrupted")
    if _sha(transaction_id, "transaction record key") != transaction_id or value.get("transaction_id") != transaction_id:
        raise DispatchTransactionError("transaction identity is corrupted")
    if value.get("commit") != "committed" or value.get("state") not in TRANSACTION_STATES:
        raise DispatchTransactionError("transaction commit marker is corrupted")
    for name in ("baseline", "graph_sha256", "identity_sha256"):
        _sha(value.get(name), f"transaction {name}")
    if not isinstance(value.get("baseline_path"), str) or not Path(value["baseline_path"]).is_absolute():
        raise DispatchTransactionError("transaction baseline path is corrupted")
    if not isinstance(value.get("repo"), str) or not Path(value["repo"]).is_absolute():
        raise DispatchTransactionError("transaction repository is corrupted")
    if value.get("workspace_mode") not in {"light", "strict", "fresh", "delta"}:
        raise DispatchTransactionError("transaction workspace mode is corrupted")
    if isinstance(value.get("recovery_count"), bool) or not isinstance(value.get("recovery_count"), int) or value["recovery_count"] < 0 or value["recovery_count"] > 1:
        raise DispatchTransactionError("transaction recovery state is corrupted")
    if not isinstance(value.get("created_at"), (int, float)):
        raise DispatchTransactionError("transaction timestamp is corrupted")
    if not isinstance(value.get("nodes"), Mapping) or not value["nodes"]:
        raise DispatchTransactionError("transaction nodes are corrupted")
    normalized_nodes = {str(node): _node_state(node_value, expected_node=str(node)) for node, node_value in value["nodes"].items()}
    if list(normalized_nodes) != sorted(normalized_nodes) or any(NODE.fullmatch(node) is None for node in normalized_nodes):
        raise DispatchTransactionError("transaction nodes are not canonical")
    lifecycle_probe = {"nodes": normalized_nodes, "state": value["state"]}
    _refresh_transaction_state(lifecycle_probe)
    derived_state = lifecycle_probe["state"]
    terminal_complete = not _transaction_pending({"nodes": normalized_nodes}) and not any(
        node["state"] == "active" for node in normalized_nodes.values()
    )
    if value["state"] != derived_state and not (
        (value["state"] == "fenced" and derived_state in {"prepared", "dispatching", "active", "rejected", "fenced"})
        or (value["state"] == "terminal" and terminal_complete)
    ):
        raise DispatchTransactionError("transaction lifecycle is corrupted")
    all_refs = [candidate["ref"] for node in normalized_nodes.values() for candidate in node["candidates"]]
    if len(set(all_refs)) != len(all_refs):
        raise DispatchTransactionError("transaction references overlap")
    expected_identity = _digest(
        b"cco.dispatch-transaction-state.v1\0",
        {
            "baseline": value["baseline"],
            "graph_sha256": value["graph_sha256"],
            "refs": all_refs,
            "transaction_id": transaction_id,
        },
    )
    if value["identity_sha256"] != expected_identity:
        raise DispatchTransactionError("transaction state identity is corrupted")
    return {
        "baseline": value["baseline"],
        "baseline_path": value["baseline_path"],
        "commit": "committed",
        "created_at": value["created_at"],
        "graph_sha256": value["graph_sha256"],
        "identity_sha256": value["identity_sha256"],
        "nodes": normalized_nodes,
        "recovery_count": value["recovery_count"],
        "repo": value["repo"],
        "state": value["state"],
        "transaction_id": transaction_id,
        "workspace_mode": value["workspace_mode"],
    }


def _full_input_digest(dispatch: Mapping[str, Any]) -> str:
    return _digest(b"cco.dispatch-native-input.v2\0", dispatch)


def _transaction_identity(*, session_id: str, repo: Path, batch: Mapping[str, Any], candidates: list[dict[str, Any]]) -> str:
    return _digest(
        b"cco.dispatch-transaction.v2\0",
        {
            "baseline": batch["baseline"],
            "candidates": [
                {"input_sha256": item["input_sha256"], "node": item["node"], "rank": item["rank"]}
                for item in candidates
            ],
            "graph_sha256": batch["graph_sha256"],
            "repo": str(repo),
            "session_id": session_id,
        },
    )


def _spawn_reference(transaction_id: str, *, node: str, input_sha256: str) -> str:
    return _digest(
        b"cco.dispatch-spawn-ref.v2\0",
        {"input_sha256": input_sha256, "node": node, "transaction_id": transaction_id},
    )


def render_spawn_reference(transaction_id: str, spawn_ref: str) -> str:
    return f"{REF_HEADER}\nTRANSACTION_ID: {_sha(transaction_id, 'transaction identity')}\nSPAWN_REF: {_sha(spawn_ref, 'spawn reference')}"


def parse_spawn_reference(message: object) -> tuple[str, str]:
    if not isinstance(message, str) or len(message.encode("utf-8")) > 1024:
        raise DispatchTransactionError("spawn reference is invalid")
    lines = message.split("\n")
    if len(lines) != 3 or lines[0] != REF_HEADER or not lines[1].startswith("TRANSACTION_ID: ") or not lines[2].startswith("SPAWN_REF: "):
        raise DispatchTransactionError("spawn reference is not a compact v2 envelope")
    return (
        _sha(lines[1][len("TRANSACTION_ID: "):], "transaction identity"),
        _sha(lines[2][len("SPAWN_REF: "):], "spawn reference"),
    )


def render_abort_command(transaction_id: str) -> str:
    return f"{ABORT_HEADER}\nTRANSACTION_ID: {_sha(transaction_id, 'transaction identity')}"


def parse_abort_command(message: object) -> str:
    if not isinstance(message, str) or len(message.encode("utf-8")) > 512:
        raise DispatchTransactionError("transaction abort command is invalid")
    lines = message.split("\n")
    if len(lines) != 2 or lines[0] != ABORT_HEADER or not lines[1].startswith("TRANSACTION_ID: "):
        raise DispatchTransactionError("transaction abort command is not exact")
    return _sha(lines[1][len("TRANSACTION_ID: "):], "transaction identity")


def _reference_input(candidate: Mapping[str, Any], transaction_id: str) -> dict[str, str]:
    return {
        "agent_type": str(candidate["agent_type"]),
        "fork_turns": str(candidate["fork_turns"]),
        "message": render_spawn_reference(transaction_id, str(candidate["ref"])),
        "model": str(candidate["model"]),
        "reasoning_effort": str(candidate["reasoning_effort"]),
        "task_name": str(candidate["task_name"]),
    }


def _canonical_dispatch(value: object, *, batch: Mapping[str, Any], fallback: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != SPAWN_FIELDS:
        raise DispatchTransactionError("prepared native dispatch shape is invalid")
    dispatch = {name: value[name] for name in SPAWN_FIELDS}
    try:
        capsule = parse_message(dispatch["message"])
    except Exception as error:
        raise DispatchTransactionError("prepared native dispatch is not a canonical v7 input") from error
    if capsule["baseline"] != batch["baseline"] or capsule["graph_sha256"] != batch["graph_sha256"]:
        raise DispatchTransactionError("prepared native dispatch does not match its graph identity")
    expected_agent_type = (
        "cost_orchestrator_write_leaf"
        if capsule["role"] == "worker"
        else "cost_orchestrator_read_leaf"
    )
    expected = {
        "agent_type": expected_agent_type,
        "fork_turns": capsule["execution"]["fork_turns"],
        "model": capsule["route"]["selected"]["model"],
        "reasoning_effort": capsule["route"]["selected"]["effort"],
        "task_name": capsule["execution"]["task_name"],
    }
    if any(dispatch[name] != expected[name] for name in expected):
        raise DispatchTransactionError("prepared native dispatch fields do not match its v7 capsule")
    route = capsule["route"]
    return {
        "agent_type": dispatch["agent_type"],
        "capsule": capsule,
        "dispatch": dispatch,
        "fallback": fallback,
        "fork_turns": dispatch["fork_turns"],
        "input_sha256": _full_input_digest(dispatch),
        "model": dispatch["model"],
        "node": capsule["node"],
        "rank": route["rank"],
        "reasoning_effort": dispatch["reasoning_effort"],
        "task_name": dispatch["task_name"],
    }


def _normalize_batch(value: object) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    required = {
        "baseline", "baseline_path", "blocked_dependency_nodes", "deferred_nodes", "dispatches",
        "fallback_dispatches", "graph_sha256", "primary_nodes", "protocol", "route_errors",
    }
    dag_optional = {"completed_nodes", "member_mapping"}
    if (
        not isinstance(value, Mapping)
        or not required <= set(value)
        or set(value) - (required | dag_optional)
        or value.get("protocol") not in {"cco.dispatch-batch.v1", BATCH_PROTOCOL}
    ):
        raise DispatchTransactionError("prepared dispatch batch is malformed")
    if not value["dispatches"]:
        if value["baseline"] is not None or value["baseline_path"] is not None or value["graph_sha256"] is not None:
            raise DispatchTransactionError("empty prepared batch carries a graph artifact")
        return {key: deepcopy(value[key]) for key in set(value)}, []
    if (
        _sha(value["baseline"], "prepared batch baseline") != value["baseline"]
        or _sha(value["graph_sha256"], "prepared batch graph identity") != value["graph_sha256"]
        or not isinstance(value["baseline_path"], str)
        or not Path(value["baseline_path"]).is_absolute()
        or not isinstance(value["dispatches"], list)
        or not isinstance(value["fallback_dispatches"], Mapping)
    ):
        raise DispatchTransactionError("prepared dispatch batch identity is invalid")
    batch = {key: deepcopy(value[key]) for key in set(value)}
    first = [_canonical_dispatch(item, batch=batch, fallback=False) for item in value["dispatches"]]
    by_node = {item["node"]: item for item in first}
    if len(by_node) != len(first):
        raise DispatchTransactionError("prepared batch has duplicate initial nodes")
    if set(value["fallback_dispatches"]) != set(by_node):
        raise DispatchTransactionError("prepared batch fallback nodes do not match initial nodes")
    candidates: list[dict[str, Any]] = []
    for node in sorted(by_node):
        chain = [by_node[node]]
        fallback_chain = value["fallback_dispatches"][node]
        if not isinstance(fallback_chain, list):
            raise DispatchTransactionError("prepared fallback chain is malformed")
        chain.extend(_canonical_dispatch(item, batch=batch, fallback=True) for item in fallback_chain)
        if any(item["node"] != node for item in chain) or [item["rank"] for item in chain] != list(range(1, len(chain) + 1)):
            raise DispatchTransactionError("prepared fallback chain is not canonical")
        candidates.extend(chain)
    return batch, candidates


def _bundle_document(*, transaction_id: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dispatch": candidate["dispatch"],
        "input_sha256": candidate["input_sha256"],
        "node": candidate["node"],
        "protocol": BUNDLE_PROTOCOL,
        "spawn_ref": candidate["ref"],
        "transaction_id": transaction_id,
    }


def _read_bundle(
    root: Path,
    session_id: str,
    transaction_id: str,
    candidate: Mapping[str, Any],
    *,
    expected_node: str | None = None,
) -> dict[str, Any]:
    path = bundle_path(root, session_id, transaction_id, str(candidate["ref"]))
    if path.is_symlink() or not path.is_file():
        raise DispatchTransactionError("transaction dispatch bundle is unavailable")
    try:
        if path.stat().st_size > _MAX_BUNDLE_BYTES:
            raise DispatchTransactionError("transaction dispatch bundle exceeds the size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        if isinstance(error, DispatchTransactionError):
            raise
        raise DispatchTransactionError("transaction dispatch bundle is unreadable") from error
    required = {"dispatch", "input_sha256", "node", "protocol", "spawn_ref", "transaction_id"}
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or value.get("protocol") != BUNDLE_PROTOCOL
        or value.get("transaction_id") != transaction_id
        or value.get("spawn_ref") != candidate["ref"]
        or (expected_node is not None and value.get("node") != expected_node)
        or value.get("input_sha256") != candidate["input_sha256"]
        or not isinstance(value.get("dispatch"), Mapping)
        or _full_input_digest(value["dispatch"]) != candidate["input_sha256"]
    ):
        raise DispatchTransactionError("transaction dispatch bundle is inconsistent")
    return {name: value["dispatch"][name] for name in SPAWN_FIELDS}


def _remove_bundle(root: Path, session_id: str, transaction_id: str, candidate: Mapping[str, Any]) -> None:
    path = bundle_path(root, session_id, transaction_id, str(candidate["ref"]))
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            raise DispatchTransactionError("transaction bundle path is not a file")
        path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _remove_transaction_bundles(root: Path, session_id: str, transaction: Mapping[str, Any]) -> None:
    transaction_id = str(transaction["transaction_id"])
    for node in transaction["nodes"].values():
        for candidate in node["candidates"]:
            _remove_bundle(root, session_id, transaction_id, candidate)


def _candidate_for_ref(transaction: Mapping[str, Any], spawn_ref: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    matches: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for node_name, node in transaction["nodes"].items():
        for candidate in node["candidates"]:
            if candidate["ref"] == spawn_ref:
                matches.append((node_name, node, candidate))
    if len(matches) != 1:
        raise DispatchTransactionError("spawn reference is absent or ambiguous")
    return matches[0]


def _transaction_pending(transaction: Mapping[str, Any]) -> bool:
    return any(
        node["state"] in {"prepared", "dispatching"}
        or (node["state"] == "rejected" and node["eligible_ref"] is not None)
        for node in transaction["nodes"].values()
    )


def _refresh_transaction_state(transaction: dict[str, Any]) -> None:
    nodes = list(transaction["nodes"].values())
    if any(node["state"] == "active" for node in nodes):
        transaction["state"] = "active"
    elif any(node["state"] == "dispatching" for node in nodes):
        transaction["state"] = "dispatching"
    elif any(node["state"] == "prepared" for node in nodes):
        transaction["state"] = "prepared"
    elif any(node["state"] == "rejected" and node["eligible_ref"] is not None for node in nodes):
        transaction["state"] = "rejected"
    elif any(node["state"] == "fenced" for node in nodes):
        transaction["state"] = "fenced"
    elif any(node["state"] == "rejected" for node in nodes):
        transaction["state"] = "rejected"
    else:
        transaction["state"] = "terminal"


def _mark_terminal_if_done(transaction: dict[str, Any]) -> None:
    _refresh_transaction_state(transaction)
    if not _transaction_pending(transaction) and not any(
        node["state"] == "active" for node in transaction["nodes"].values()
    ):
        transaction["state"] = "terminal"


def _cleanup_settled_bundles(root: Path, session_id: str, transaction: Mapping[str, Any]) -> None:
    """Discard full inputs as soon as their ref cannot be dispatched again."""

    if transaction["state"] == "terminal":
        _remove_transaction_bundles(root, session_id, transaction)
        return
    transaction_id = str(transaction["transaction_id"])
    for node in transaction["nodes"].values():
        for candidate in node["candidates"]:
            if candidate["state"] in {"active", "rejected", "fenced", "terminal"}:
                _remove_bundle(root, session_id, transaction_id, candidate)


def _workspace_verdict(
    transaction: Mapping[str, Any],
    repo: Path,
    *,
    pending_node: str | None = None,
) -> None:
    """Verify exact preparation or the active-sibling lease for one pending spawn."""

    path = Path(str(transaction["baseline_path"]))
    try:
        if pending_node is not None:
            if pending_node not in transaction["nodes"]:
                raise DispatchTransactionError("pending transaction node is absent")
            active_sibling_scopes = [
                scope
                for node_name, node in transaction["nodes"].items()
                if node_name != pending_node and node["state"] == "active"
                for scope in node["scopes"]
            ]
            result = verify_pre_spawn_workspace(
                path,
                repo=repo,
                baseline=str(transaction["baseline"]),
                graph_sha256_value=str(transaction["graph_sha256"]),
                graph_scopes_value=graph_scopes(load_artifact(path)["manifest"]),
                workspace_mode=str(transaction["workspace_mode"]),
                active_sibling_scopes=active_sibling_scopes,
                pending_candidate_scopes=transaction["nodes"][pending_node]["scopes"],
            )
        else:
            artifact = load_artifact(path)
            result = verify_artifact_workspace(
                path,
                repo=repo,
                baseline=str(transaction["baseline"]),
                graph_sha256_value=str(transaction["graph_sha256"]),
                graph_scopes_value=graph_scopes(artifact["manifest"]),
                workspace_mode=str(transaction["workspace_mode"]),
            )
    except Exception as error:
        raise DispatchTransactionError("pre-spawn workspace verification failed") from error
    if not isinstance(result, Mapping) or result.get("verdict") != "pass":
        violations = result.get("violations") if isinstance(result, Mapping) else None
        detail = ",".join(violations) if isinstance(violations, list) and all(isinstance(item, str) for item in violations) else "invalid-verdict"
        raise DispatchTransactionError(f"pre-spawn workspace verification rejected: {detail}")


def _public_batch(batch: Mapping[str, Any], transaction: Mapping[str, Any]) -> dict[str, Any]:
    initial: list[dict[str, str]] = []
    fallback: dict[str, list[dict[str, str]]] = {}
    for node_name, node in transaction["nodes"].items():
        references = [_reference_input(candidate, str(transaction["transaction_id"])) for candidate in node["candidates"]]
        initial.append(references[0])
        fallback[node_name] = references[1:]
    initial.sort(key=lambda item: item["task_name"])
    public = {
        "baseline": batch["baseline"],
        "baseline_path": batch["baseline_path"],
        "blocked_dependency_nodes": deepcopy(batch["blocked_dependency_nodes"]),
        "deferred_nodes": deepcopy(batch["deferred_nodes"]),
        "dispatches": initial,
        "fallback_dispatches": fallback,
        "graph_sha256": batch["graph_sha256"],
        "primary_nodes": deepcopy(batch["primary_nodes"]),
        "protocol": BATCH_PROTOCOL,
        "route_errors": deepcopy(batch["route_errors"]),
        "transaction_id": transaction["transaction_id"],
    }
    for name in ("completed_nodes", "member_mapping"):
        if name in batch:
            public[name] = deepcopy(batch[name])
    return public


def prepare_dispatch_batch(
    prepared_batch: Mapping[str, Any],
    *,
    ledger_root: Path,
    repo: Path,
    session_id: str,
) -> dict[str, Any]:
    """Persist a prepared v1 batch and return v2 native-tool-safe short refs.

    Bundle files are fully flushed before the state document gets its committed
    marker.  A crash can therefore leave only unreachable bundles, never a
    committed reference without its canonical full spawn input.
    """

    session = _session(session_id)
    root = _external_root(Path(ledger_root), Path(repo))
    resolved_repo = repository_root(Path(repo)).resolve()
    batch, raw_candidates = _normalize_batch(prepared_batch)
    if not raw_candidates:
        empty = {
            "baseline": None,
            "baseline_path": None,
            "blocked_dependency_nodes": deepcopy(batch["blocked_dependency_nodes"]),
            "deferred_nodes": deepcopy(batch["deferred_nodes"]),
            "dispatches": [],
            "fallback_dispatches": {},
            "graph_sha256": None,
            "primary_nodes": deepcopy(batch["primary_nodes"]),
            "protocol": BATCH_PROTOCOL,
            "route_errors": deepcopy(batch["route_errors"]),
            "transaction_id": None,
        }
        for name in ("completed_nodes", "member_mapping"):
            if name in batch:
                empty[name] = deepcopy(batch[name])
        return empty
    transaction_id = _transaction_identity(
        session_id=session,
        repo=resolved_repo,
        batch=batch,
        candidates=raw_candidates,
    )
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates:
        candidate = {
            "agent_type": raw["agent_type"],
            "dispatch": raw["dispatch"],
            "fork_turns": raw["fork_turns"],
            "input_sha256": raw["input_sha256"],
            "model": raw["model"],
            "node": raw["node"],
            "rank": raw["rank"],
            "reasoning_effort": raw["reasoning_effort"],
            "scopes": raw["capsule"]["scopes"],
            "task_name": raw["task_name"],
        }
        candidate["ref"] = _spawn_reference(transaction_id, node=str(candidate["node"]), input_sha256=str(candidate["input_sha256"]))
        candidates.append(candidate)
    identity_sha256 = _digest(
        b"cco.dispatch-transaction-state.v1\0",
        {
            "baseline": batch["baseline"],
            "graph_sha256": batch["graph_sha256"],
            "refs": [candidate["ref"] for candidate in candidates],
            "transaction_id": transaction_id,
        },
    )
    by_node: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_node.setdefault(str(candidate["node"]), []).append(candidate)
    nodes: dict[str, dict[str, Any]] = {}
    for node_name in sorted(by_node):
        chain = sorted(by_node[node_name], key=lambda item: int(item["rank"]))
        scopes = chain[0]["scopes"]
        if any(candidate["scopes"] != scopes for candidate in chain):
            raise DispatchTransactionError("prepared fallback chain changes node scopes")
        nodes[node_name] = {
            "call_id": None,
            "candidates": [
                {
                    "agent_type": candidate["agent_type"],
                    "fork_turns": candidate["fork_turns"],
                    "input_sha256": candidate["input_sha256"],
                    "model": candidate["model"],
                    "rank": candidate["rank"],
                    "reasoning_effort": candidate["reasoning_effort"],
                    "ref": candidate["ref"],
                    "state": "prepared",
                    "task_name": candidate["task_name"],
                }
                for candidate in chain
            ],
            "dispatch_ref": None,
            "eligible_ref": chain[0]["ref"],
            "owner": None,
            "scopes": scopes,
            "state": "prepared",
        }
    transaction: dict[str, Any] = {
        "baseline": batch["baseline"],
        "baseline_path": str(Path(str(batch["baseline_path"])).resolve()),
        "commit": "committed",
        "created_at": int(time.time()),
        "graph_sha256": batch["graph_sha256"],
        "identity_sha256": identity_sha256,
        "nodes": nodes,
        "recovery_count": 0,
        "repo": str(resolved_repo),
        "state": "prepared",
        "transaction_id": transaction_id,
        "workspace_mode": load_artifact(Path(str(batch["baseline_path"]))) ["manifest"]["workspace_mode"],
    }
    # Verify before publishing any reference.  An artifact, Git control, or scope
    # failure is graph-fatal and never gets a committed transaction marker.
    _workspace_verdict(transaction, resolved_repo)
    with _lock(root, session):
        document = _read_document(root, session)
        existing = document["transactions"].get(transaction_id)
        if existing is not None:
            normalized = _validate_transaction(transaction_id, existing)
            if normalized["identity_sha256"] != identity_sha256:
                raise DispatchTransactionError("transaction identity collides with different state")
            if normalized["state"] in {"fenced", "terminal"}:
                raise DispatchTransactionError("prepared transaction is terminal or fenced; capture a new graph")
            return _public_batch(batch, normalized)
        if any(
            _transaction_pending(item)
            for item in document["transactions"].values()
        ):
            raise DispatchTransactionError("another managed dispatch transaction is pending")
        # Write every immutable full input first.  The following state write is the
        # commit marker observed by hooks.
        for candidate in candidates:
            _write_atomic(
                bundle_path(root, session, transaction_id, str(candidate["ref"])),
                _bundle_document(transaction_id=transaction_id, candidate=candidate),
            )
        document["transactions"][transaction_id] = transaction
        _write_document(root, session, document)
    return _public_batch(batch, transaction)


# Kept as intentionally narrow aliases for graph_compiler's future direct import.
prepare_dispatch_transaction = prepare_dispatch_batch
convert_prepared_batch = prepare_dispatch_batch


def read_transaction_state(ledger_root: Path, session_id: str, transaction_id: str) -> dict[str, Any]:
    root = Path(os.path.abspath(Path(ledger_root).expanduser())).resolve()
    session = _session(session_id)
    transaction = _sha(transaction_id, "transaction identity")
    with _lock(root, session):
        document = _read_document(root, session)
        value = document["transactions"].get(transaction)
        if value is None:
            raise DispatchTransactionError("transaction state is absent")
        return deepcopy(_validate_transaction(transaction, value))


def _pending_records(root: Path, session_id: str, *, repo: Path | None = None) -> list[dict[str, Any]]:
    with _lock(root, session_id):
        document = _read_document(root, session_id)
        records = [_validate_transaction(transaction_id, value) for transaction_id, value in document["transactions"].items()]
    if repo is not None:
        expected = str(repository_root(repo).resolve())
        records = [record for record in records if record["repo"] == expected]
    return [record for record in records if _transaction_pending(record)]


def _records_for_payload(payload: Mapping[str, Any]) -> tuple[Path, str, Path | None, list[dict[str, Any]]]:
    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    if not _state_path(root, session).exists():
        return root, session, None, []
    with _lock(root, session):
        document = _read_document(root, session)
        records = [
            _validate_transaction(transaction_id, value)
            for transaction_id, value in document["transactions"].items()
        ]
    # A desktop task may be rooted above the repository (or outside Git entirely).
    # The session-bound transaction already contains the exact, prepare-time
    # repository identity, so lifecycle discovery must not derive authority from
    # the host cwd.  Gating every pending transaction in the session also prevents
    # a cwd change from bypassing an unfinished dispatch batch.
    return root, session, None, records


def pending_transactions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    _root, _session_id, _repo, records = _records_for_payload(payload)
    return [record for record in records if _transaction_pending(record)]


def has_pending_transaction(payload: Mapping[str, Any]) -> bool:
    return bool(pending_transactions(payload))


def _fence_transaction(
    root: Path,
    session_id: str,
    transaction: dict[str, Any],
    *,
    graph_fatal: bool = False,
) -> None:
    for node in transaction["nodes"].values():
        if node["state"] in {"prepared", "dispatching", "rejected"}:
            was_dispatching = node["state"] == "dispatching"
            node["state"] = "fenced"
            node["eligible_ref"] = None
            for candidate in node["candidates"]:
                if candidate["state"] in {"prepared", "dispatching"}:
                    candidate["state"] = "fenced"
            # Retain one tiny call/ref tombstone for a late PostToolUse.  Clearing
            # it would let the host's updated full v7 input fall through to the
            # legacy activation branch after an exact abort or Stop fence.
            if not was_dispatching:
                node["call_id"] = None
                node["dispatch_ref"] = None
    _refresh_transaction_state(transaction)
    if graph_fatal:
        transaction["state"] = "fenced"
    elif not any(node["state"] == "active" for node in transaction["nodes"].values()):
        transaction["state"] = "fenced"
        _mark_terminal_if_done(transaction)


def abort_pending_transaction(payload: Mapping[str, Any], transaction_id: str) -> None:
    """Fence only undispatched work.  Existing native owners are left intact."""

    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    requested = _sha(transaction_id, "transaction identity")
    with _lock(root, session):
        document = _read_document(root, session)
        raw = document["transactions"].get(requested)
        if raw is None:
            raise DispatchTransactionError("transaction abort target is absent")
        transaction = _validate_transaction(requested, raw)
        _fence_transaction(root, session, transaction)
        document["transactions"][requested] = transaction
        _write_document(root, session, document)
        _cleanup_settled_bundles(root, session, transaction)


def _fail_graph(root: Path, session_id: str, document: dict[str, Any], transaction: dict[str, Any]) -> None:
    _fence_transaction(root, session_id, transaction, graph_fatal=True)
    document["transactions"][transaction["transaction_id"]] = transaction
    _write_document(root, session_id, document)
    _cleanup_settled_bundles(root, session_id, transaction)


def claim_spawn_reference(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one exact short ref, verify its workspace, and mark it dispatching."""

    if payload.get("tool_name") not in {"spawn_agent", "Agent"}:
        raise DispatchTransactionError("pending transaction requires an exact native spawn reference")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        raise DispatchTransactionError("pending transaction tool input is missing")
    transaction_id, spawn_ref = parse_spawn_reference(tool_input.get("message"))
    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    call_id = payload.get("tool_use_id")
    if not isinstance(call_id, str) or not call_id:
        raise DispatchTransactionError("spawn reference has no exact tool call identity")
    with _lock(root, session):
        document = _read_document(root, session)
        raw = document["transactions"].get(transaction_id)
        if raw is None:
            raise DispatchTransactionError("spawn reference transaction is absent")
        transaction = _validate_transaction(transaction_id, raw)
        try:
            repo = repository_root(Path(transaction["repo"])).resolve()
        except (OSError, StateError) as error:
            _fail_graph(root, session, document, transaction)
            raise DispatchTransactionError(
                "spawn reference repository is unavailable"
            ) from error
        if transaction["repo"] != str(repo):
            _fail_graph(root, session, document, transaction)
            raise DispatchTransactionError("spawn reference repository does not match its transaction")
        node_name, node, candidate = _candidate_for_ref(transaction, spawn_ref)
        expected = _reference_input(candidate, transaction_id)
        if dict(tool_input) != expected:
            _fail_graph(root, session, document, transaction)
            raise DispatchTransactionError("spawn reference native fields are not exact")
        if node["state"] == "dispatching" and node["call_id"] == call_id and node["eligible_ref"] == spawn_ref:
            return _read_bundle(root, session, transaction_id, candidate, expected_node=node_name)
        if node["state"] not in {"prepared", "rejected"} or node["eligible_ref"] != spawn_ref or candidate["state"] != "prepared":
            raise DispatchTransactionError("spawn reference is not the current pending candidate")
        try:
            _workspace_verdict(transaction, repo, pending_node=node_name)
            dispatch = _read_bundle(root, session, transaction_id, candidate, expected_node=node_name)
            capsule = parse_message(dispatch["message"])
            if capsule["node"] != node_name or capsule["capsule_sha256"] not in dispatch["message"]:
                raise DispatchTransactionError("transaction bundle capsule is inconsistent")
        except Exception as error:
            _fail_graph(root, session, document, transaction)
            if isinstance(error, DispatchTransactionError):
                raise
            raise DispatchTransactionError("spawn reference verification failed") from error
        node["state"] = "dispatching"
        node["call_id"] = call_id
        node["dispatch_ref"] = spawn_ref
        candidate["state"] = "dispatching"
        _refresh_transaction_state(transaction)
        document["transactions"][transaction_id] = transaction
        _write_document(root, session, document)
        return dispatch


def release_spawn_claim(payload: Mapping[str, Any]) -> None:
    """Undo a local reservation failure before Codex sees the native tool call."""

    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    call_id = payload.get("tool_use_id")
    if not isinstance(call_id, str):
        return
    with _lock(root, session):
        document = _read_document(root, session)
        changed = False
        for transaction_id, raw in document["transactions"].items():
            transaction = _validate_transaction(transaction_id, raw)
            for node in transaction["nodes"].values():
                if node["state"] == "dispatching" and node["call_id"] == call_id:
                    node["state"] = "prepared"
                    node["call_id"] = None
                    node["dispatch_ref"] = None
                    for candidate in node["candidates"]:
                        if candidate["state"] == "dispatching":
                            candidate["state"] = "prepared"
                    _refresh_transaction_state(transaction)
                    document["transactions"][transaction_id] = transaction
                    changed = True
        if changed:
            _write_document(root, session, document)


def spawn_claim_for_call(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the small state record for a PreToolUse reference already claimed."""

    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    call_id = payload.get("tool_use_id")
    if not isinstance(call_id, str):
        return None
    with _lock(root, session):
        document = _read_document(root, session)
        matches: list[dict[str, Any]] = []
        for transaction_id, raw in document["transactions"].items():
            transaction = _validate_transaction(transaction_id, raw)
            for node_name, node in transaction["nodes"].items():
                if node["call_id"] != call_id:
                    continue
                candidate = next(
                    (item for item in node["candidates"] if item["ref"] == node["dispatch_ref"]),
                    None,
                )
                if candidate is None:
                    raise DispatchTransactionError("spawn call reference is absent")
                matches.append(
                    {
                        "node": node_name,
                        "owner": "/root/" + candidate["task_name"],
                        "repo": transaction["repo"],
                        "state": node["state"],
                        "transaction_id": transaction_id,
                    }
                )
        if len(matches) > 1:
            raise DispatchTransactionError("spawn call identity is ambiguous")
        return matches[0] if matches else None


def settle_spawn_success(payload: Mapping[str, Any], owner: str) -> None:
    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    call_id = payload.get("tool_use_id")
    if not isinstance(call_id, str) or TASK_PATH.fullmatch(owner) is None:
        raise DispatchTransactionError("spawn activation identity is invalid")
    with _lock(root, session):
        document = _read_document(root, session)
        matches: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for transaction_id, raw in document["transactions"].items():
            transaction = _validate_transaction(transaction_id, raw)
            for node in transaction["nodes"].values():
                if node["call_id"] == call_id:
                    candidate = next((item for item in node["candidates"] if item["state"] == "dispatching"), None)
                    if candidate is not None:
                        matches.append((transaction_id, transaction, node, candidate))
        if len(matches) != 1:
            raise DispatchTransactionError("spawn activation call is absent or ambiguous")
        transaction_id, transaction, node, candidate = matches[0]
        expected = "/root/" + candidate["task_name"]
        if owner != expected:
            _fail_graph(root, session, document, transaction)
            raise DispatchTransactionError("spawn activation owner is not exact")
        node["state"] = "active"
        node["owner"] = owner
        node["call_id"] = None
        node["dispatch_ref"] = None
        node["eligible_ref"] = None
        candidate["state"] = "active"
        for fallback in node["candidates"]:
            if fallback["state"] == "prepared":
                fallback["state"] = "terminal"
        _refresh_transaction_state(transaction)
        document["transactions"][transaction_id] = transaction
        _write_document(root, session, document)
        _cleanup_settled_bundles(root, session, transaction)


def settle_spawn_rejection(payload: Mapping[str, Any]) -> None:
    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    call_id = payload.get("tool_use_id")
    if not isinstance(call_id, str):
        raise DispatchTransactionError("spawn rejection has no call identity")
    with _lock(root, session):
        document = _read_document(root, session)
        matches: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for transaction_id, raw in document["transactions"].items():
            transaction = _validate_transaction(transaction_id, raw)
            for node in transaction["nodes"].values():
                if node["call_id"] == call_id:
                    candidate = next((item for item in node["candidates"] if item["state"] == "dispatching"), None)
                    if candidate is not None:
                        matches.append((transaction_id, transaction, node, candidate))
        if len(matches) != 1:
            raise DispatchTransactionError("spawn rejection call is absent or ambiguous")
        transaction_id, transaction, node, candidate = matches[0]
        candidate["state"] = "rejected"
        node["call_id"] = None
        node["dispatch_ref"] = None
        next_candidate = next((item for item in node["candidates"] if item["state"] == "prepared"), None)
        node["eligible_ref"] = next_candidate["ref"] if next_candidate is not None else None
        node["state"] = "rejected"
        _refresh_transaction_state(transaction)
        _mark_terminal_if_done(transaction)
        document["transactions"][transaction_id] = transaction
        _write_document(root, session, document)
        _cleanup_settled_bundles(root, session, transaction)


def fence_spawn_call(payload: Mapping[str, Any]) -> None:
    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    call_id = payload.get("tool_use_id")
    if not isinstance(call_id, str):
        return
    with _lock(root, session):
        document = _read_document(root, session)
        changed = False
        for transaction_id, raw in document["transactions"].items():
            transaction = _validate_transaction(transaction_id, raw)
            if any(node["call_id"] == call_id for node in transaction["nodes"].values()):
                _fence_transaction(root, session, transaction, graph_fatal=True)
                document["transactions"][transaction_id] = transaction
                changed = True
        if changed:
            _write_document(root, session, document)
            for transaction in document["transactions"].values():
                _cleanup_settled_bundles(root, session, _validate_transaction(str(transaction["transaction_id"]), transaction))


def retire_owner(payload: Mapping[str, Any], owner: str, *, fenced: bool = False) -> None:
    """Record native owner terminality after SubagentStop or native interrupt."""

    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    if TASK_PATH.fullmatch(owner) is None:
        raise DispatchTransactionError("transaction owner is not canonical")
    with _lock(root, session):
        document = _read_document(root, session)
        changed = False
        for transaction_id, raw in document["transactions"].items():
            transaction = _validate_transaction(transaction_id, raw)
            for node in transaction["nodes"].values():
                if node["owner"] != owner:
                    continue
                node["state"] = "fenced" if fenced else "terminal"
                node["owner"] = None
                node["call_id"] = None
                node["dispatch_ref"] = None
                for candidate in node["candidates"]:
                    if candidate["state"] == "active":
                        candidate["state"] = "fenced" if fenced else "terminal"
                _mark_terminal_if_done(transaction)
                document["transactions"][transaction_id] = transaction
                changed = True
        if changed:
            _write_document(root, session, document)
            for transaction in document["transactions"].values():
                _cleanup_settled_bundles(root, session, _validate_transaction(str(transaction["transaction_id"]), transaction))


def _compact_context(transaction: Mapping[str, Any]) -> str:
    active = sorted(node["owner"] for node in transaction["nodes"].values() if isinstance(node["owner"], str))
    pending = sorted(
        node_name
        for node_name, node in transaction["nodes"].items()
        if node["state"] in {"prepared", "dispatching"} or (node["state"] == "rejected" and node["eligible_ref"] is not None)
    )
    return (
        f"CCO dispatch transaction {transaction['transaction_id'][7:19]} state={transaction['state']} "
        f"active={','.join(active) or '-'} pending={','.join(pending) or '-'} "
        f"recovery={transaction['recovery_count']}."
    )


def stop_outcome(payload: Mapping[str, Any]) -> dict[str, str]:
    """Request one event-first recovery or fence pending work without looping."""

    session = _session(payload.get("session_id"))
    root = ledger_root_for_payload(payload)
    with _lock(root, session):
        document = _read_document(root, session)
        records = [
            _validate_transaction(transaction_id, value)
            for transaction_id, value in document["transactions"].items()
        ]
        in_flight = [
            record
            for record in records
            if any(node["state"] in {"active", "dispatching"} for node in record["nodes"].values())
        ]
        if in_flight:
            detail = "; ".join(_compact_context(record) for record in in_flight)
            return {
                "decision": "block",
                "reason": (
                    "CCO_EVENT_FIRST_WAIT 1800000 ms requested for active native owner(s) or in-flight spawn(s); "
                    "wait for a native event and do not emit a progress-only response. " + detail
                ),
            }
        pending = [record for record in records if _transaction_pending(record)]
        if not pending:
            return {}
        first = pending[0]
        if first["recovery_count"] == 0:
            first["recovery_count"] = 1
            document["transactions"][first["transaction_id"]] = first
            _write_document(root, session, document)
            return {
                "decision": "block",
                "reason": (
                    "CCO dispatch recovery continuation is available exactly once for undispatched work; "
                    "use its exact pending reference, then wait for a native event. " + _compact_context(first)
                ),
            }
        for record in pending:
            _fence_transaction(root, session, record)
            document["transactions"][record["transaction_id"]] = record
        _write_document(root, session, document)
        for record in pending:
            _cleanup_settled_bundles(root, session, record)
        return {
            "systemMessage": "CCO dispatch recovery was already used; remaining undispatched nodes were fenced.",
        }


def user_prompt_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    _root, _session_id, _repo, records = _records_for_payload(payload)
    records = [
        record
        for record in records
        if _transaction_pending(record) or any(node["state"] == "active" for node in record["nodes"].values())
    ]
    if not records:
        return {}
    return {
        "hookSpecificOutput": {
            "additionalContext": " ".join(_compact_context(record) for record in records)[:1024],
            "hookEventName": "UserPromptSubmit",
        }
    }


def exact_abort_for_payload(payload: Mapping[str, Any]) -> str | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping) or set(tool_input) - {"message", "target"}:
        return None
    message = tool_input.get("message")
    if not isinstance(message, str) or not message.startswith(ABORT_HEADER):
        return None
    return parse_abort_command(message)


def is_native_rejection(value: Any) -> bool:
    """Expose the bounded response classifier to the lifecycle adapter."""

    return _native_response_rejected(value)


def response_task_paths(value: Any) -> set[str]:
    """Expose exact canonical native-owner extraction to the lifecycle adapter."""

    return _task_paths(value)
