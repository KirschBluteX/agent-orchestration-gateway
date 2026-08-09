#!/usr/bin/env python3
"""Compact cco.v9 plan, wave, routing, and lifecycle control plane."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping
import unicodedata

from protocol_hash import (
    ProtocolHashError,
    canonical_bytes,
    parse_canonical_json_object,
    parse_safe_integer,
    reject_constant,
    reject_float,
    repository_scopes_overlap,
    require_repository_path,
)
from host_paths import HostPathError, host_path
from operation_deadline import (
    OperationDeadlineExceeded,
    checkpoint,
    deadline_after,
    remaining_seconds,
)
from routing_catalog import (
    RoutingCatalogError,
    load_native_catalog,
    load_route_policy,
    resolve_route_plan,
)
from state_lock import StateLockBusy, acquire
from workspace_guard import (
    WorkspaceGuardError,
    WorkspaceGuardUnavailable,
    capture as capture_workspace,
    discover_workspace,
    normalize_scope_groups,
    verify_state as verify_workspace,
)


PROTOCOL = "cco.v9"
PLAN_PROTOCOL = "cco.plan.v1"
WAVE_PROTOCOL = "cco.wave.v2"
LEGACY_WAVE_PROTOCOL = "cco.wave.v1"
BATCH_PROTOCOL = "cco.wave-batch.v2"
LIFECYCLE_PROTOCOL = "cco.lifecycle.v1"
TASK_HEADER = "CCO_TASK cco.v9"
CONTINUE_HEADER = "CCO_CONTINUE cco.v9"
RESULT_HEADER = "CCO_RESULT cco.v9"
READ_ROLE = "cost_orchestrator_read_leaf"
WRITE_ROLE = "cost_orchestrator_write_leaf"
ROLES = frozenset({"explorer", "worker", "reviewer"})
ASSURANCES = frozenset({"mechanical", "bounded", "guarded"})
LOGICAL_STATES = frozenset(
    {
        "waiting",
        "ready",
        "starting",
        "running",
        "paused",
        "retired",
        "fenced",
    }
)
DISPATCH_STATES = frozenset(
    {"starting", "running", "paused", "retired", "fenced", "rejected"}
)
ACTIVE_STATES = frozenset({"running", "paused"})
NODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
ACCEPTANCE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
TASK_PATH_RE = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FAILURE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
MAX_INPUT_BYTES = 1024 * 1024
MAX_AGGREGATE_MEMBERS = 4
MAX_TOMBSTONES = 256
MAX_TRANSIENT_RETRIES = 3
NATIVE_CLAIM_TTL_MILLISECONDS = 120_000
PREFLIGHT_VERIFICATION_SECONDS = 14.0
PREFLIGHT_ROLLBACK_RESERVE_SECONDS = 4.0
NATIVE_FAILURE_KINDS = frozenset(
    {
        "network",
        "other",
        "owner_unavailable",
        "rate_limit",
        "route_rejected",
        "service",
        "timeout",
    }
)
EFFORT_LABELS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
STATE_FILE_RE = re.compile(r"^(?P<workspace>[0-9a-f]{64})--(?P<session>[0-9a-f]{64})\.json$")
RECOVERY_FILE_RE = re.compile(r"^\.cco-recovery-[A-Za-z0-9_-]+\.json$")
STATE_ROOT_SENTINEL = ".cco-state-root-v1"
STATE_ROOT_SENTINEL_BYTES = b"cco.state-root.v1\n"
STATE_ROOT_CAPACITY_LOCK = "state-root-capacity"
MAX_STATE_FILE_BYTES = 32 * 1024 * 1024
MAX_STATE_FILES = 4_096
MAX_RECOVERY_FILES = 32
STATE_READ_CHUNK_BYTES = 1024 * 1024


class ControlPlaneError(RuntimeError):
    """A cco.v9 contract or lifecycle transition is invalid."""


class ControlPlaneUnavailable(ControlPlaneError):
    """CCO state infrastructure is temporarily unavailable; do not fence work."""


def _text(value: object, label: str, *, limit: int = 8_192) -> str:
    if not isinstance(value, str):
        raise ControlPlaneError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized.encode("utf-8")) > limit:
        raise ControlPlaneError(f"{label} is empty or too large")
    return normalized


def _digest(domain: bytes, value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(domain + canonical_bytes(dict(value))).hexdigest()


def _state_root() -> Path:
    configured = os.environ.get("CCO_STATE_DIR")
    root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "v9"
    )
    return Path(os.path.abspath(root))


def _workspace_key(value: object) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ControlPlaneError("lifecycle workspace root is invalid")
    try:
        ordinary = host_path(os.fspath(value))
    except (HostPathError, OSError, TypeError) as error:
        raise ControlPlaneError("lifecycle workspace root is invalid") from error
    return os.path.normcase(os.path.realpath(os.path.abspath(ordinary)))


def _workspace_lock_identity(value: object) -> str:
    return f"workspace-{_workspace_digest(value)}"


def _workspace_digest(value: object) -> str:
    return hashlib.sha256(_workspace_key(value).encode("utf-8")).hexdigest()


def _session_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lifecycle_state_path(root: Path, workspace: object, session_id: str) -> Path:
    return root / f"{_workspace_digest(workspace)}--{_session_digest(session_id)}.json"


def _lifecycle_lineage_id(
    workspace: object,
    session_id: str,
    plan_id: str,
) -> str:
    return _digest(
        b"cco.lifecycle-lineage.v1\0",
        {
            "plan_id": plan_id,
            "session_id": session_id,
            "workspace_root": _workspace_key(workspace),
        },
    )


def _preflight_verification_budget() -> float:
    remaining = remaining_seconds(reserve=PREFLIGHT_ROLLBACK_RESERVE_SECONDS)
    return (
        PREFLIGHT_VERIFICATION_SECONDS
        if remaining is None
        else min(PREFLIGHT_VERIFICATION_SECONDS, remaining)
    )


def _bounded_lock_timeout(limit: float) -> float:
    remaining = remaining_seconds()
    return limit if remaining is None else min(limit, remaining)


def _state_content_id(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Workspace adapters legitimately contain platform inode/device integers above
    # JavaScript's safe range.  Artifact identities bind their state_id, while the
    # persisted snapshot itself uses deterministic ordinary JSON without re-hashing
    # those host-native integers through the wire-protocol canonicalizer.
    serialized = (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".cco-v9-",
            delete=False,
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            staged = Path(handle.name)
        os.replace(staged, path)
        staged = None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        if _load_object(path, "immutable CCO artifact") != dict(value):
            raise ControlPlaneError("immutable CCO artifact identity collision")
        return
    _atomic_write(path, value)


def _read_bounded_bytes(
    path: Path,
    label: str,
    *,
    limit: int = MAX_STATE_FILE_BYTES,
) -> bytes:
    checkpoint()
    raw = bytearray()
    try:
        with path.open("rb") as handle:
            while True:
                checkpoint()
                chunk = handle.read(min(STATE_READ_CHUNK_BYTES, limit - len(raw) + 1))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > limit:
                    raise ControlPlaneError(f"{label} is too large")
    except OSError as error:
        raise ControlPlaneUnavailable(f"{label} is unavailable") from error
    return bytes(raw)


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    checkpoint()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ControlPlaneError,
        ProtocolHashError,
    ) as error:
        raise ControlPlaneError(f"{label} is not valid JSON") from error
    checkpoint()
    if not isinstance(value, dict):
        raise ControlPlaneError(f"{label} is malformed")
    return value


def _load_object(path: Path, label: str) -> dict[str, Any]:
    return _decode_object(_read_bounded_bytes(path, label), label)


def _state_json_paths(root: Path) -> list[Path]:
    """Return a deterministic, memory-bounded snapshot of lifecycle files."""

    paths: list[Path] = []
    ordinary_count = 0
    recovery_count = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                checkpoint()
                if not entry.name.endswith(".json"):
                    continue
                if RECOVERY_FILE_RE.fullmatch(entry.name) is not None:
                    if recovery_count >= MAX_RECOVERY_FILES:
                        raise ControlPlaneUnavailable(
                            "lifecycle state directory exceeds the "
                            f"{MAX_RECOVERY_FILES} recovery file limit"
                        )
                    recovery_count += 1
                elif ordinary_count >= MAX_STATE_FILES:
                    raise ControlPlaneUnavailable(
                        "lifecycle state directory exceeds the "
                        f"{MAX_STATE_FILES} file limit"
                    )
                else:
                    ordinary_count += 1
                paths.append(Path(entry.path))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ControlPlaneUnavailable(
            "lifecycle state directory is unavailable"
        ) from error
    checkpoint()
    paths.sort(key=lambda item: item.name)
    return paths


def _state_capacity_used(paths: list[Path]) -> int:
    return sum(RECOVERY_FILE_RE.fullmatch(path.name) is None for path in paths)


def _session_state_paths(root: Path, session_id: str) -> list[Path]:
    """Find only one task's state even when the shared root is over capacity."""

    suffix = f"--{_session_digest(session_id)}.json"
    legacy_name = f"{session_id}.json"
    matches: list[Path] = []
    recovery_paths: list[Path] = []
    recovery_count = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                checkpoint()
                if entry.name == legacy_name or entry.name.endswith(suffix):
                    matches.append(Path(entry.path))
                elif RECOVERY_FILE_RE.fullmatch(entry.name) is not None:
                    if recovery_count >= MAX_RECOVERY_FILES:
                        raise ControlPlaneUnavailable(
                            "lifecycle state directory exceeds the "
                            f"{MAX_RECOVERY_FILES} recovery file limit"
                        )
                    recovery_count += 1
                    recovery_paths.append(Path(entry.path))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ControlPlaneUnavailable(
            "lifecycle state directory is unavailable"
        ) from error
    for path in recovery_paths:
        try:
            candidate = _load_object(path, "cco.v9 recovery lifecycle state")
        except FileNotFoundError:
            continue
        except ControlPlaneUnavailable:
            raise
        except ControlPlaneError:
            continue
        if candidate.get("session_id") == session_id:
            matches.append(path)
    matches.sort(key=lambda item: item.name)
    return matches


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlPlaneError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _external_scopes(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ControlPlaneError("node scopes must be a non-empty list")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"kind", "path"}:
            raise ControlPlaneError(f"node scope {index} must contain kind and path")
        kind = item["kind"]
        if kind in {"file", "exact"}:
            internal = "exact"
        elif kind in {"tree", "prefix"}:
            internal = "prefix"
        else:
            raise ControlPlaneError(f"node scope {index}.kind must be file or tree")
        normalized.append({"kind": internal, "path": item["path"]})
    return normalized


def _single_node_brief(value: object) -> dict[str, Any]:
    """Expand the common one-child contract without exposing the full DAG schema."""

    if not isinstance(value, Mapping):
        raise ControlPlaneError("single-child contract must be an object")
    allowed = {
        "acceptance",
        "context_turns",
        "decision",
        "goal",
        "objective",
        "pin",
        "risks",
        "role",
        "scopes",
        "verification",
    }
    if set(value) - allowed:
        raise ControlPlaneError("single-child contract contains unsupported fields")
    required = {"acceptance", "objective", "role", "scopes"}
    if not required <= set(value):
        raise ControlPlaneError("single-child contract is incomplete")
    objective = _text(value["objective"], "single-child objective")
    criteria = value["acceptance"]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 32:
        raise ControlPlaneError("single-child acceptance must contain 1 to 32 criteria")
    acceptance = {
        f"A{index:02d}": _text(item, f"single-child acceptance {index}", limit=4_096)
        for index, item in enumerate(criteria, start=1)
    }
    node = {
        key: deepcopy(value[key])
        for key in (
            "context_turns",
            "decision",
            "pin",
            "risks",
            "verification",
        )
        if key in value
    }
    node.update(
        {
            "acceptance": list(acceptance),
            "id": "task",
            "objective": objective,
            "role": value["role"],
            "scopes": deepcopy(value["scopes"]),
        }
    )
    return {
        "acceptance": acceptance,
        "goal": _text(value.get("goal", objective), "single-child goal"),
        "nodes": [node],
    }


def _compact_graph_brief(value: object) -> dict[str, Any]:
    """Expand per-node acceptance text into one ordinary cco.v9 DAG brief."""

    if not isinstance(value, Mapping) or set(value) != {"goal", "nodes"}:
        raise ControlPlaneError("compact graph must contain only goal and nodes")
    nodes_value = value["nodes"]
    if not isinstance(nodes_value, list) or not nodes_value:
        raise ControlPlaneError("compact graph nodes must be a non-empty list")
    allowed = {
        "acceptance",
        "context_turns",
        "decision",
        "depends_on",
        "id",
        "objective",
        "pin",
        "review_of",
        "risks",
        "role",
        "scopes",
        "verification",
    }
    required = {"acceptance", "id", "objective", "role", "scopes"}
    acceptance: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    counter = 0
    for index, raw in enumerate(nodes_value):
        if not isinstance(raw, Mapping) or set(raw) - allowed or not required <= set(raw):
            raise ControlPlaneError(f"compact graph node {index} is invalid")
        criteria = raw["acceptance"]
        if not isinstance(criteria, list) or not criteria:
            raise ControlPlaneError(f"compact graph node {index} has no acceptance criteria")
        ids: list[str] = []
        for criterion in criteria:
            counter += 1
            if counter > 999:
                raise ControlPlaneError("compact graph exceeds 999 acceptance criteria")
            acceptance_id = f"A{counter:03d}"
            acceptance[acceptance_id] = _text(
                criterion,
                f"compact graph acceptance {counter}",
                limit=4_096,
            )
            ids.append(acceptance_id)
        node = deepcopy(dict(raw))
        node["acceptance"] = ids
        nodes.append(node)
    return {
        "acceptance": acceptance,
        "goal": _text(value["goal"], "compact graph goal"),
        "nodes": nodes,
    }


def _prepare_brief(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping) and "nodes" in value:
        if set(value) == {"acceptance", "goal", "nodes"}:
            return deepcopy(dict(value))
        return _compact_graph_brief(value)
    return _single_node_brief(value)


def _normalize_pin(value: object) -> dict[str, str | None]:
    if value is None:
        return {"fixed_effort": None, "fixed_model": None, "source": "automatic"}
    if not isinstance(value, Mapping) or set(value) - {"model", "effort"}:
        raise ControlPlaneError("route pin may contain only model and effort")
    model = value.get("model")
    effort = value.get("effort")
    if model is not None:
        model = _text(model, "route pin model", limit=128)
    if effort is not None:
        effort = _text(effort, "route pin effort", limit=32)
        if effort not in EFFORT_LABELS:
            raise ControlPlaneError("route pin effort is unsupported")
    if model is None and effort is None:
        raise ControlPlaneError("route pin must select a model or effort")
    return {"fixed_effort": effort, "fixed_model": model, "source": "user"}


def _derive_assurance(node: Mapping[str, Any]) -> str:
    if node["role"] == "reviewer" or node["risks"] or node["verification"] != "deterministic":
        return "guarded"
    return "mechanical" if node["decision"] == "mechanical" else "bounded"


def _normalize_brief(
    value: object,
    workspace_root: Path,
    workspace_backend: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlPlaneError("plan brief must be an object")
    if set(value) - {"goal", "acceptance", "nodes"}:
        raise ControlPlaneError("plan brief contains unsupported fields")
    if set(value) != {"goal", "acceptance", "nodes"}:
        raise ControlPlaneError("plan brief is incomplete")
    goal = _text(value["goal"], "plan goal")
    acceptance_value = value["acceptance"]
    if not isinstance(acceptance_value, Mapping) or not acceptance_value:
        raise ControlPlaneError("plan acceptance must be a non-empty object")
    acceptance: dict[str, str] = {}
    for raw_id, raw_criterion in acceptance_value.items():
        acceptance_id = _text(raw_id, "acceptance ID", limit=32)
        if ACCEPTANCE_RE.fullmatch(acceptance_id) is None:
            raise ControlPlaneError(f"invalid acceptance ID: {acceptance_id}")
        if acceptance_id in acceptance:
            raise ControlPlaneError(
                f"acceptance IDs collide after normalization: {acceptance_id}"
            )
        acceptance[acceptance_id] = _text(
            raw_criterion,
            f"acceptance {acceptance_id}",
            limit=4_096,
        )
    nodes_value = value["nodes"]
    if not isinstance(nodes_value, list) or not nodes_value:
        raise ControlPlaneError("plan nodes must be a non-empty list")
    nodes: list[dict[str, Any]] = []
    scope_groups: list[list[dict[str, str]]] = []
    assigned_acceptance: set[str] = set()
    allowed = {
        "id",
        "role",
        "objective",
        "acceptance",
        "scopes",
        "depends_on",
        "decision",
        "verification",
        "risks",
        "pin",
        "context_turns",
        "review_of",
    }
    for index, raw in enumerate(nodes_value):
        if not isinstance(raw, Mapping) or set(raw) - allowed:
            raise ControlPlaneError(f"plan node {index} contains unsupported fields")
        required = {"id", "role", "objective", "acceptance", "scopes"}
        if not required <= set(raw):
            raise ControlPlaneError(f"plan node {index} is incomplete")
        node_id = _text(raw["id"], f"plan node {index}.id", limit=48)
        if NODE_RE.fullmatch(node_id) is None:
            raise ControlPlaneError(f"invalid node ID: {node_id}")
        role = raw["role"]
        if role not in ROLES:
            raise ControlPlaneError(f"plan node {node_id} has an invalid role")
        objective = _text(raw["objective"], f"plan node {node_id}.objective")
        ids_value = raw["acceptance"]
        if not isinstance(ids_value, list) or not ids_value:
            raise ControlPlaneError(f"plan node {node_id} has no acceptance IDs")
        ids = sorted({_text(item, f"plan node {node_id} acceptance", limit=32) for item in ids_value})
        if len(ids) != len(ids_value) or any(item not in acceptance for item in ids):
            raise ControlPlaneError(f"plan node {node_id} acceptance IDs are invalid")
        assigned_acceptance.update(ids)
        depends_value = raw.get("depends_on", [])
        if not isinstance(depends_value, list):
            raise ControlPlaneError(f"plan node {node_id}.depends_on must be a list")
        depends = sorted({_text(item, f"plan node {node_id} dependency", limit=48) for item in depends_value})
        if len(depends) != len(depends_value) or node_id in depends:
            raise ControlPlaneError(f"plan node {node_id} dependencies are invalid")
        decision = raw.get("decision", "bounded")
        if decision not in {"mechanical", "bounded"}:
            raise ControlPlaneError(f"plan node {node_id}.decision is invalid")
        verification = raw.get("verification", "deterministic")
        if verification not in {"deterministic", "semantic", "manual"}:
            raise ControlPlaneError(f"plan node {node_id}.verification is invalid")
        risks_value = raw.get("risks", [])
        if not isinstance(risks_value, list):
            raise ControlPlaneError(f"plan node {node_id}.risks must be a list")
        risks = sorted({_text(item, f"plan node {node_id} risk", limit=64) for item in risks_value})
        if len(risks) != len(risks_value):
            raise ControlPlaneError(f"plan node {node_id}.risks contains duplicates")
        context_turns = raw.get("context_turns", 0)
        if isinstance(context_turns, bool) or not isinstance(context_turns, int) or not 0 <= context_turns <= 32:
            raise ControlPlaneError(f"plan node {node_id}.context_turns is invalid")
        review_of = raw.get("review_of")
        if review_of is not None:
            review_of = _text(review_of, f"plan node {node_id}.review_of", limit=48)
            if role != "reviewer":
                raise ControlPlaneError("review_of is valid only for reviewers")
        scopes = _external_scopes(raw["scopes"])
        node = {
            "acceptance": ids,
            "assurance": "",
            "context_turns": context_turns,
            "decision": decision,
            "depends_on": depends,
            "id": node_id,
            "objective": objective,
            "pin": _normalize_pin(raw.get("pin")),
            "review_of": review_of,
            "risks": risks,
            "role": role,
            "scopes": scopes,
            "verification": verification,
        }
        node["assurance"] = _derive_assurance(node)
        nodes.append(node)
        scope_groups.append(scopes)
    normalized_scope_groups = normalize_scope_groups(
        workspace_root,
        scope_groups,
        backend=workspace_backend,
    )
    for node, scopes in zip(nodes, normalized_scope_groups, strict=True):
        node["scopes"] = scopes
    nodes.sort(key=lambda item: item["id"])
    node_ids = {item["id"] for item in nodes}
    if len(node_ids) != len(nodes):
        raise ControlPlaneError("plan node IDs must be unique")
    unknown = sorted(
        {dependency for item in nodes for dependency in item["depends_on"] if dependency not in node_ids}
    )
    if unknown:
        raise ControlPlaneError("plan contains unknown dependencies: " + ", ".join(unknown))
    for item in nodes:
        if item["review_of"] is not None and item["review_of"] not in node_ids:
            raise ControlPlaneError(f"reviewer {item['id']} names an unknown source")
        if item["review_of"] is not None and item["review_of"] not in item["depends_on"]:
            raise ControlPlaneError(
                f"reviewer {item['id']} must depend on its review_of source"
            )
    indegree = {item["id"]: len(item["depends_on"]) for item in nodes}
    downstream: dict[str, set[str]] = {item["id"]: set() for item in nodes}
    for item in nodes:
        for dependency in item["depends_on"]:
            downstream[dependency].add(item["id"])
    queue = sorted(node for node, count in indegree.items() if count == 0)
    visited: list[str] = []
    while queue:
        current = queue.pop(0)
        visited.append(current)
        for child in sorted(downstream[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
                queue.sort()
    if len(visited) != len(nodes):
        raise ControlPlaneError("plan dependency graph contains a cycle")
    if assigned_acceptance != set(acceptance):
        missing = sorted(set(acceptance) - assigned_acceptance)
        raise ControlPlaneError("plan acceptance is unowned: " + ", ".join(missing))
    return {
        "acceptance": {key: acceptance[key] for key in sorted(acceptance)},
        "goal": goal,
        "nodes": nodes,
    }


def _node_map(plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in plan["nodes"]}


def _descendant_counts(plan: Mapping[str, Any]) -> dict[str, int]:
    direct: dict[str, set[str]] = {item["id"]: set() for item in plan["nodes"]}
    for item in plan["nodes"]:
        for dependency in item["depends_on"]:
            direct[dependency].add(item["id"])
    result: dict[str, int] = {}
    for node in direct:
        seen: set[str] = set()
        stack = list(direct[node])
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(direct[current])
        result[node] = len(seen)
    return result


def _scopes_overlap(left: list[dict[str, str]], right: list[dict[str, str]]) -> bool:
    return any(repository_scopes_overlap(a, b) for a in left for b in right)


def _scopes_within(
    child_scopes: list[dict[str, str]],
    parent_scopes: list[dict[str, str]],
) -> bool:
    """Return whether every child scope is contained by a parent scope."""

    def contains(parent: Mapping[str, str], child: Mapping[str, str]) -> bool:
        parent_path = ntpath.normcase(parent["path"]).replace("\\", "/")
        child_path = ntpath.normcase(child["path"]).replace("\\", "/")
        if parent["kind"] == "exact":
            return child["kind"] == "exact" and child_path == parent_path
        return child_path == parent_path or child_path.startswith(parent_path + "/")

    return all(
        any(contains(parent, child) for parent in parent_scopes)
        for child in child_scopes
    )


def _units_conflict(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["role"] == right["role"] == "worker":
        return True
    if left["role"] != "worker" and right["role"] != "worker":
        return False
    return _scopes_overlap(left["scopes"], right["scopes"])


def _select_units(units: list[dict[str, Any]], capacity: int) -> list[dict[str, Any]]:
    if capacity < 1:
        return []
    readers = [item for item in units if item["role"] != "worker"]
    writers = [item for item in units if item["role"] == "worker"]

    def ranked(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda item: (-item["downstream_count"], item["id"]),
        )

    candidates = [ranked(readers)[:capacity]]
    for writer in writers:
        compatible = [
            reader for reader in readers if not _units_conflict(writer, reader)
        ]
        candidates.append([writer, *ranked(compatible)[: max(0, capacity - 1)]])

    def better(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
        left_score = sum(item["downstream_count"] for item in left)
        right_score = sum(item["downstream_count"] for item in right)
        if (len(left), left_score) != (len(right), right_score):
            return (len(left), left_score) > (len(right), right_score)
        return tuple(sorted(item["id"] for item in left)) < tuple(
            sorted(item["id"] for item in right)
        )

    best: list[dict[str, Any]] = []
    for candidate in candidates:
        if better(candidate, best):
            best = candidate
    return sorted(best, key=lambda item: item["id"])


def _route_key(route: Mapping[str, Any], node: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        node["role"],
        node["assurance"],
        tuple((item["model"], item["effort"]) for item in route["candidates"]),
        node["context_turns"],
    )


def _physical_units(
    ready: list[dict[str, Any]],
    routes: Mapping[str, Mapping[str, Any]],
    *,
    capacity: int,
    downstream: Mapping[str, int],
) -> list[dict[str, Any]]:
    groups: dict[tuple[object, ...], list[dict[str, Any]]] = {}
    singles: list[dict[str, Any]] = []
    should_aggregate = len(ready) > capacity
    for node in ready:
        if should_aggregate and node["assurance"] == "mechanical":
            groups.setdefault(_route_key(routes[node["id"]], node), []).append(node)
        else:
            singles.append(node)
    packed: list[list[dict[str, Any]]] = [[node] for node in singles]
    for members in groups.values():
        buckets: list[list[dict[str, Any]]] = []
        for node in sorted(members, key=lambda item: item["id"]):
            target = next(
                (
                    bucket
                    for bucket in buckets
                    if len(bucket) < MAX_AGGREGATE_MEMBERS
                    and (
                        node["role"] != "worker"
                        or all(
                            not _scopes_overlap(node["scopes"], item["scopes"])
                            for item in bucket
                        )
                    )
                ),
                None,
            )
            if target is None:
                target = []
                buckets.append(target)
            target.append(node)
        packed.extend(buckets)
    units: list[dict[str, Any]] = []
    for members in packed:
        member_ids = [item["id"] for item in members]
        first = members[0]
        scopes = {
            (scope["kind"], scope["path"]): dict(scope)
            for item in members
            for scope in item["scopes"]
        }
        unit_id = (
            member_ids[0]
            if len(member_ids) == 1
            else "aggregate_" + hashlib.sha256(",".join(member_ids).encode()).hexdigest()[:16]
        )
        units.append(
            {
                "assurance": first["assurance"],
                "context_turns": first["context_turns"],
                "downstream_count": sum(downstream[item] for item in member_ids),
                "id": unit_id,
                "members": member_ids,
                "role": first["role"],
                "route": deepcopy(routes[first["id"]]),
                "scopes": [scopes[key] for key in sorted(scopes)],
            }
        )
    return units


def _model_label(model: str) -> str:
    lowered = model.casefold()
    for label in ("luna", "terra", "sol"):
        if label in lowered:
            return label
    return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")[-16:] or "model"


def _task_name(
    unit: Mapping[str, Any],
    route: Mapping[str, str],
    generation: int,
    dispatch_id: str,
) -> str:
    role = unit["role"]
    prefix = {"explorer": "explorer", "worker": "worker", "reviewer": "reviewer"}[role]
    base = re.sub(r"[^a-z0-9_]+", "_", str(unit["id"]).casefold()).strip("_")[:32]
    suffix = dispatch_id.removeprefix("sha256:")[:10]
    return (
        f"{prefix}_{base}_{suffix}_{_model_label(route['model'])}_"
        f"{route['effort']}_g{generation:02d}"
    )


def _render_task(
    plan: Mapping[str, Any],
    unit: Mapping[str, Any],
    dispatch_id: str,
    *,
    cursor: int,
    dependency_evidence: Mapping[str, Any],
) -> str:
    nodes = _node_map(plan)
    members = []
    acceptance: dict[str, str] = {}
    for member_id in unit["members"]:
        node = nodes[member_id]
        members.append(
            {
                "acceptance": node["acceptance"],
                "depends_on": node["depends_on"],
                "id": member_id,
                "objective": node["objective"],
                "review_of": node["review_of"],
                "scopes": node["scopes"],
            }
        )
        for acceptance_id in node["acceptance"]:
            acceptance[acceptance_id] = plan["acceptance"][acceptance_id]
    body = {
        "acceptance": {key: acceptance[key] for key in sorted(acceptance)},
        "assurance": unit["assurance"],
        "cursor": cursor,
        "dependency_evidence": dict(dependency_evidence),
        "dispatch_id": dispatch_id,
        "members": members,
        "protocol": PROTOCOL,
        "result_fields": [
            "blockers",
            "changed_paths",
            "cursor",
            "deviations",
            "dispatch_id",
            "evidence",
            "failure_signature",
            "outcome",
            "status",
            "summary",
        ],
        "result_mode": "cumulative_from_wave_baseline",
        "role": unit["role"],
        "scopes": unit["scopes"],
        "workspace_root": plan["workspace_root"],
    }
    return TASK_HEADER + "\n" + canonical_bytes(body).decode("utf-8")


def parse_task_message(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(TASK_HEADER + "\n"):
        raise ControlPlaneError("spawn does not contain a cco.v9 task")
    try:
        body = parse_canonical_json_object(value.split("\n", 1)[1], "cco.v9 task")
    except ProtocolHashError as error:
        raise ControlPlaneError(str(error)) from error
    if body.get("protocol") != PROTOCOL or SHA256_RE.fullmatch(str(body.get("dispatch_id"))) is None:
        raise ControlPlaneError("cco.v9 task identity is invalid")
    return body


def _render_continue(dispatch: Mapping[str, Any], evidence_delta: object, cursor: int) -> str:
    body = {
        "cursor": cursor,
        "dispatch_id": dispatch["dispatch_id"],
        "evidence_delta": evidence_delta,
        "protocol": PROTOCOL,
        "result_mode": "cumulative_from_wave_baseline",
        "workspace_root": dispatch["workspace_root"],
    }
    return CONTINUE_HEADER + "\n" + canonical_bytes(body).decode("utf-8")


def parse_continue_message(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(CONTINUE_HEADER + "\n"):
        raise ControlPlaneError("continuation does not contain a cco.v9 contract")
    try:
        body = parse_canonical_json_object(value.split("\n", 1)[1], "cco.v9 continuation")
    except ProtocolHashError as error:
        raise ControlPlaneError(str(error)) from error
    if body.get("protocol") != PROTOCOL or SHA256_RE.fullmatch(str(body.get("dispatch_id"))) is None:
        raise ControlPlaneError("cco.v9 continuation identity is invalid")
    return body


def parse_result(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(RESULT_HEADER + "\n"):
        raise ControlPlaneError("child did not return a cco.v9 result")
    raw = value.split("\n", 1)[1].strip()
    try:
        result = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, ControlPlaneError) as error:
        raise ControlPlaneError("cco.v9 result is not JSON") from error
    required = {
        "blockers",
        "changed_paths",
        "cursor",
        "deviations",
        "dispatch_id",
        "evidence",
        "failure_signature",
        "outcome",
        "status",
        "summary",
    }
    if not isinstance(result, Mapping) or set(result) != required:
        raise ControlPlaneError("cco.v9 result fields are malformed")
    if SHA256_RE.fullmatch(str(result["dispatch_id"])) is None:
        raise ControlPlaneError("cco.v9 result dispatch identity is invalid")
    cursor = result["cursor"]
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ControlPlaneError("cco.v9 result cursor is invalid")
    status = result["status"]
    outcome = result["outcome"]
    if status not in {"complete", "partial", "blocked"} or outcome not in {
        "retire",
        "pause",
        "accept",
    }:
        raise ControlPlaneError("cco.v9 result status or outcome is invalid")
    blockers = result["blockers"]
    deviations = result["deviations"]
    if not isinstance(blockers, list) or not isinstance(deviations, list):
        raise ControlPlaneError("cco.v9 result blockers or deviations are invalid")
    normalized_blockers = sorted({_text(item, "result blocker", limit=2_048) for item in blockers})
    normalized_deviations = sorted({_text(item, "result deviation", limit=2_048) for item in deviations})
    if len(normalized_blockers) != len(blockers) or len(normalized_deviations) != len(deviations):
        raise ControlPlaneError("cco.v9 result lists contain duplicates")
    if status == "blocked" and not normalized_blockers:
        raise ControlPlaneError("blocked result must name a blocker")
    if status != "complete" and outcome != "pause":
        raise ControlPlaneError("incomplete result must pause for an explicit decision")
    if status == "complete" and (normalized_blockers or normalized_deviations):
        raise ControlPlaneError("complete result cannot contain blockers or deviations")
    paths_value = result["changed_paths"]
    if not isinstance(paths_value, list):
        raise ControlPlaneError("cco.v9 changed_paths must be a list")
    try:
        changed_paths = sorted(
            {require_repository_path(item, "result changed path") for item in paths_value}
        )
    except ProtocolHashError as error:
        raise ControlPlaneError(str(error)) from error
    if len(changed_paths) != len(paths_value):
        raise ControlPlaneError("cco.v9 changed_paths contains duplicates")
    evidence_value = result["evidence"]
    if not isinstance(evidence_value, Mapping):
        raise ControlPlaneError("cco.v9 evidence must be an object")
    evidence: dict[str, str] = {}
    for raw_id, raw_evidence in evidence_value.items():
        evidence_id = _text(raw_id, "result evidence ID", limit=32)
        if evidence_id in evidence:
            raise ControlPlaneError(
                f"result evidence IDs collide after normalization: {evidence_id}"
            )
        evidence[evidence_id] = _text(
            raw_evidence,
            "result evidence",
            limit=8_192,
        )
    failure = result["failure_signature"]
    if failure is not None:
        failure = _text(failure, "result failure signature", limit=256)
        if FAILURE_RE.fullmatch(failure) is None:
            raise ControlPlaneError("result failure signature is not canonical")
    if (status != "complete" or normalized_deviations or normalized_blockers) and failure is None:
        raise ControlPlaneError("non-success result requires a failure signature")
    if status == "complete" and not normalized_deviations and not normalized_blockers and failure is not None:
        raise ControlPlaneError("successful result cannot carry a failure signature")
    return {
        "blockers": normalized_blockers,
        "changed_paths": changed_paths,
        "cursor": cursor,
        "deviations": normalized_deviations,
        "dispatch_id": result["dispatch_id"],
        "evidence": {key: evidence[key] for key in sorted(evidence)},
        "failure_signature": failure,
        "outcome": outcome,
        "status": status,
        "summary": _text(result["summary"], "result summary", limit=4_096),
    }


def _native_response_failed(value: object) -> bool:
    """Recognize only an explicit failure marker; never infer a failure kind."""

    if not isinstance(value, Mapping):
        return False
    error = value.get("error")
    return (
        value.get("isError") is True
        or value.get("is_error") is True
        or value.get("success") is False
        or value.get("ok") is False
        or str(value.get("status", "")).casefold() in {"error", "failed", "failure"}
        or (error is not None and error is not False and error != "")
    )


def _tool_action(
    action: str,
    tool_name: str | None,
    tool_input: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "action": action,
        "tool_input": deepcopy(dict(tool_input)) if tool_input is not None else None,
        "tool_name": tool_name,
    }


def _task_paths(value: object, *, key: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        if TASK_PATH_RE.fullmatch(value) is not None and (
            key in {"task_name", "task_path", "agent_path", "target"} or value.startswith("/root/")
        ):
            found.add(value)
    elif isinstance(value, Mapping):
        for child_key, child in value.items():
            found.update(_task_paths(child, key=str(child_key)))
    elif isinstance(value, list):
        for child in value:
            found.update(_task_paths(child, key=key))
    return found


def _owner_matches_task(owner: object, task_name: object) -> bool:
    return (
        isinstance(owner, str)
        and isinstance(task_name, str)
        and TASK_PATH_RE.fullmatch(owner) is not None
        and owner.endswith("/" + task_name)
    )


def _sibling_writer_scopes(
    state: Mapping[str, Any],
    dispatch: Mapping[str, Any],
) -> list[dict[str, str]]:
    return [
        scope
        for item in state["dispatches"].values()
        if item["wave_id"] == dispatch["wave_id"]
        and item["role"] == "worker"
        and item["dispatch_id"] != dispatch["dispatch_id"]
        and item["state"] in {"starting", "running", "paused", "retired"}
        for scope in item["scopes"]
    ]


def _now_milliseconds() -> int:
    return int(time.time() * 1000)


def _native_claim_active(dispatch: Mapping[str, Any], *, now: int | None = None) -> bool:
    deadline = dispatch.get("claim_expires_at")
    return (
        dispatch.get("state") == "starting"
        and (
            isinstance(dispatch.get("tool_use_id"), str)
            or (
                isinstance(deadline, int)
                and not isinstance(deadline, bool)
                and deadline > (_now_milliseconds() if now is None else now)
            )
        )
    )


def _native_settlement_overdue(
    dispatch: Mapping[str, Any], *, now: int | None = None
) -> bool:
    deadline = dispatch.get("claim_expires_at")
    return (
        dispatch.get("state") == "starting"
        and isinstance(dispatch.get("tool_use_id"), str)
        and isinstance(deadline, int)
        and not isinstance(deadline, bool)
        and deadline <= (_now_milliseconds() if now is None else now)
    )


def _writer_lease_active(dispatch: Mapping[str, Any], *, now: int | None = None) -> bool:
    if dispatch.get("role") != "worker":
        return False
    if dispatch.get("state") in {"running", "paused"}:
        return True
    if dispatch.get("state") == "starting" and dispatch.get("tool_kind") == "continuation":
        return True
    return _native_claim_active(dispatch, now=now)


def _reader_active(dispatch: Mapping[str, Any], *, now: int | None = None) -> bool:
    return dispatch.get("role") != "worker" and (
        dispatch.get("state") == "running" or _native_claim_active(dispatch, now=now)
    )


def _scopes_overlap(
    left: list[Mapping[str, str]],
    right: list[Mapping[str, str]],
) -> bool:
    return any(
        repository_scopes_overlap(left_scope, right_scope)
        for left_scope in left
        for right_scope in right
    )


class ControlPlane:
    """One deep interface for cco.v9 plan, wave, and lifecycle behavior."""

    def __init__(
        self,
        session_id: str,
        *,
        root: Path | None = None,
        lock_timeout: float = 10.0,
    ) -> None:
        if SESSION_RE.fullmatch(session_id) is None:
            raise ControlPlaneError("session identity is invalid")
        if lock_timeout <= 0:
            raise ControlPlaneError("lock timeout must be positive")
        self.session_id = session_id
        self._uses_default_root = root is None and not os.environ.get("CCO_STATE_DIR")
        self.root = Path(os.path.abspath((root or _state_root()).expanduser()))
        self._state_path: Path | None = None
        self.lock_timeout = float(lock_timeout)

    @property
    def _state_root_sentinel(self) -> Path:
        return self.root / STATE_ROOT_SENTINEL

    def _state_root_is_marked(self) -> bool:
        marker = self._state_root_sentinel
        if not marker.exists():
            return False
        if _read_bounded_bytes(marker, "CCO state-root sentinel", limit=128) != (
            STATE_ROOT_SENTINEL_BYTES
        ):
            raise ControlPlaneError("CCO state-root sentinel is invalid")
        return True

    def _mark_state_root_if_safe(self) -> None:
        """Mark only a dedicated or empty state root as CCO-owned."""

        self.root.mkdir(parents=True, exist_ok=True)
        if self._state_root_is_marked():
            return
        json_files = _state_json_paths(self.root)
        if json_files and not self._uses_default_root:
            for path in json_files:
                if STATE_FILE_RE.fullmatch(path.name) is not None:
                    continue
                try:
                    self._validate_lifecycle_state(
                        _load_object(path, "legacy cco.v9 lifecycle state")
                    )
                except ControlPlaneUnavailable:
                    raise
                except ControlPlaneError:
                    return
        try:
            descriptor = os.open(
                self._state_root_sentinel,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            if not self._state_root_is_marked():
                raise ControlPlaneError("CCO state-root sentinel is invalid")
            return
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(STATE_ROOT_SENTINEL_BYTES)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self._state_root_sentinel.unlink(missing_ok=True)
            raise

    @property
    def state_path(self) -> Path:
        """Resolve this task's workspace-partitioned state without parsing other tasks."""

        if self._state_path is not None and self._state_path.exists():
            return self._state_path
        self._state_path = None
        legacy = self.root / f"{self.session_id}.json"
        with acquire(
            self.root,
            STATE_ROOT_CAPACITY_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            matches = _session_state_paths(self.root, self.session_id)
        indexed = [path for path in matches if STATE_FILE_RE.fullmatch(path.name)]
        recovery = [path for path in matches if RECOVERY_FILE_RE.fullmatch(path.name)]
        if len(indexed) > 1:
            raise ControlPlaneError("current task has multiple lifecycle state files")
        if len(recovery) > 1:
            raise ControlPlaneError("current task has multiple lifecycle recovery files")
        self._state_path = (
            recovery[0]
            if recovery
            else indexed[0]
            if indexed
            else next((path for path in matches if path.name == legacy.name), legacy)
        )
        return self._state_path

    @staticmethod
    def _validate_lifecycle_state(
        state: Mapping[str, Any],
        *,
        expected_session: str | None = None,
    ) -> dict[str, Any]:
        raw_dispatches = state.get("dispatches")
        legacy = any(
            isinstance(dispatch, Mapping) and dispatch.get("state") == "interrupting"
            for dispatch in (raw_dispatches or {}).values()
        ) if isinstance(raw_dispatches, Mapping) else False
        legacy_layout = "lineage_id" not in state and isinstance(
            raw_dispatches, Mapping
        ) and any(
            isinstance(dispatch, Mapping)
            and any(
                field not in dispatch
                for field in ("context_turns", "fallback_from_owner", "reused_from")
            )
            for dispatch in raw_dispatches.values()
        )
        logical_value = state.get("logical")
        legacy = legacy or (
            isinstance(logical_value, Mapping)
            and any(
                isinstance(item, Mapping) and item.get("state") == "interrupting"
                for item in logical_value.values()
            )
        )
        normalized = deepcopy(dict(state)) if legacy or legacy_layout else dict(state)
        if legacy:
            logical = normalized.get("logical")
            dispatches = normalized.get("dispatches")
            migrated_members: set[str] = set()
            if isinstance(dispatches, Mapping):
                for dispatch in dispatches.values():
                    if not isinstance(dispatch, dict) or dispatch.get("state") != "interrupting":
                        continue
                    previous = dispatch.pop("interrupt_previous", None)
                    previous_state = (
                        previous.get("state")
                        if isinstance(previous, Mapping)
                        and previous.get("state") in {"running", "paused"}
                        else "running"
                    )
                    previous_tool = (
                        previous.get("tool_kind")
                        if isinstance(previous, Mapping)
                        and previous.get("tool_kind") in {"spawn", "continuation"}
                        else (
                            "continuation"
                            if dispatch.get("pending_cursor") is not None
                            else "spawn"
                        )
                    )
                    dispatch["state"] = previous_state
                    dispatch["tool_kind"] = previous_tool
                    dispatch["tool_use_id"] = None
                    dispatch["claim_expires_at"] = None
                    for member in dispatch.get("members", []):
                        if isinstance(member, str):
                            migrated_members.add(member)
                        item = (
                            logical.get(member)
                            if isinstance(logical, Mapping) and isinstance(member, str)
                            else None
                        )
                        if isinstance(item, dict):
                            item["state"] = previous_state
            if isinstance(logical, Mapping):
                for member, item in logical.items():
                    if (
                        isinstance(item, dict)
                        and item.get("state") == "interrupting"
                        and member not in migrated_members
                    ):
                        item["state"] = "fenced"
                        item["result"] = {
                            "failure_signature": "legacy_interrupting_orphaned",
                            "summary": "legacy interrupting member had no owning dispatch",
                        }

        session = normalized.get("session_id")
        if normalized.get("protocol") != LIFECYCLE_PROTOCOL:
            raise ControlPlaneError("lifecycle state is not cco.v9; start a new Codex task")
        if not isinstance(session, str) or SESSION_RE.fullmatch(session) is None:
            raise ControlPlaneError("lifecycle state session is invalid")
        if expected_session is not None and session != expected_session:
            raise ControlPlaneError("lifecycle state session does not match")
        if not isinstance(normalized.get("workspace_root"), str):
            raise ControlPlaneError("lifecycle workspace root is invalid")
        plan_id = normalized.get("plan_id")
        revision = normalized.get("revision")
        if not isinstance(plan_id, str) or SHA256_RE.fullmatch(plan_id) is None:
            raise ControlPlaneError("lifecycle plan identity is invalid")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ControlPlaneError("lifecycle revision is invalid")
        parent_state = normalized.get("parent_state_sha256")
        if parent_state is not None and (
            not isinstance(parent_state, str)
            or SHA256_RE.fullmatch(parent_state) is None
        ):
            raise ControlPlaneError("lifecycle parent state identity is invalid")
        normalized.setdefault("parent_state_sha256", None)
        lineage_id = _lifecycle_lineage_id(
            normalized["workspace_root"],
            session,
            plan_id,
        )
        if normalized.get("lineage_id", lineage_id) != lineage_id:
            raise ControlPlaneError("lifecycle lineage identity is invalid")
        normalized["lineage_id"] = lineage_id
        dispatches = normalized.get("dispatches")
        if not isinstance(dispatches, Mapping):
            raise ControlPlaneError("lifecycle dispatch collection is invalid")
        for dispatch_id, dispatch in dispatches.items():
            if isinstance(dispatch, dict) and legacy_layout:
                native = dispatch.get("native")
                fork_turns = native.get("fork_turns") if isinstance(native, Mapping) else None
                if "context_turns" not in dispatch:
                    if fork_turns == "none":
                        dispatch["context_turns"] = 0
                    elif (
                        isinstance(fork_turns, str)
                        and fork_turns.isdigit()
                        and 1 <= int(fork_turns) <= 32
                    ):
                        dispatch["context_turns"] = int(fork_turns)
                    elif (
                        dispatch.get("tool_kind") == "continuation"
                        and isinstance(native, Mapping)
                        and isinstance(native.get("message"), str)
                        and isinstance(native.get("target"), str)
                    ):
                        # Old cco.wave.v1 replaced spawn input with the follow-up
                        # input, so context inheritance must be recovered from the
                        # immutable wave rather than guessed from this record.
                        dispatch["context_turns"] = 0
                        dispatch["legacy_context_unknown"] = True
                    else:
                        raise ControlPlaneError(
                            "legacy lifecycle context inheritance is invalid"
                        )
                dispatch.setdefault("fallback_from_owner", None)
                dispatch.setdefault("reused_from", None)
            if (
                not isinstance(dispatch_id, str)
                or SHA256_RE.fullmatch(dispatch_id) is None
                or not isinstance(dispatch, Mapping)
                or dispatch.get("dispatch_id") != dispatch_id
                or dispatch.get("state") not in DISPATCH_STATES
                or dispatch.get("role") not in ROLES
            ):
                raise ControlPlaneError("lifecycle dispatch record is invalid")
        logical = normalized.get("logical")
        if not isinstance(logical, Mapping) or any(
            not isinstance(item, Mapping) or item.get("state") not in LOGICAL_STATES
            for item in logical.values()
        ):
            raise ControlPlaneError("lifecycle logical state is invalid")
        return normalized

    def _workspace_hint(self) -> str:
        state = self._validate_lifecycle_state(
            _load_object(self.state_path, "cco.v9 lifecycle state"),
            expected_session=self.session_id,
        )
        return str(state["workspace_root"])

    @contextmanager
    def _coordinated(self, workspace_root: object | None = None) -> Iterator[None]:
        """Serialize lease-affecting state changes for one canonical workspace."""

        workspace = self._workspace_hint() if workspace_root is None else workspace_root
        deadline = time.monotonic() + self.lock_timeout

        def remaining() -> float:
            local = max(0.0, deadline - time.monotonic())
            operation = remaining_seconds()
            return local if operation is None else min(local, operation)

        with acquire(
            self.root,
            _workspace_lock_identity(workspace),
            timeout=remaining(),
        ):
            with acquire(self.root, self.session_id, timeout=remaining()):
                yield

    @staticmethod
    def _reconcile_expired_claims(state: dict[str, Any], *, now: int | None = None) -> bool:
        changed = False
        current = _now_milliseconds() if now is None else now
        for dispatch in state["dispatches"].values():
            if dispatch.get("state") != "starting" or _native_claim_active(
                dispatch, now=current
            ):
                continue
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = None
            if dispatch.get("tool_kind") == "continuation":
                dispatch["state"] = "paused"
                for member in dispatch["members"]:
                    state["logical"][member]["state"] = "paused"
            changed = True
        return changed

    @staticmethod
    def _begin_native_claim(
        state: dict[str, Any],
        dispatch: dict[str, Any],
        tool_use_id: str,
    ) -> None:
        dispatch["state"] = "starting"
        dispatch["tool_use_id"] = tool_use_id
        dispatch["claim_expires_at"] = (
            _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
        )
        for member in dispatch["members"]:
            state["logical"][member]["state"] = "starting"

    def _rollback_native_claim(self, dispatch_id: str, tool_use_id: str) -> None:
        with self._coordinated():
            state = self._read_state()
            dispatch = self._find_dispatch(state, dispatch_id)
            if (
                dispatch.get("state") != "starting"
                or dispatch.get("tool_use_id") != tool_use_id
            ):
                return
            dispatch["tool_use_id"] = None
            if dispatch.get("tool_kind") == "continuation":
                dispatch["state"] = "paused"
                dispatch["claim_expires_at"] = None
                for member in dispatch["members"]:
                    state["logical"][member]["state"] = "paused"
            else:
                dispatch["claim_expires_at"] = (
                    _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
                )
            self._write_state(state)

    def _discard_stale_unstarted_wave(self, dispatch_id: str, tool_use_id: str) -> bool:
        """Discard a baseline that never reached a native child and can be recaptured."""

        with self._coordinated():
            state = self._read_state()
            dispatch = self._find_dispatch(state, dispatch_id)
            if (
                dispatch.get("state") != "starting"
                or dispatch.get("tool_kind") not in {"spawn", "reuse"}
                or dispatch.get("tool_use_id") != tool_use_id
            ):
                return False
            wave_id = dispatch["wave_id"]
            wave_records = [
                item
                for item in state["dispatches"].values()
                if item.get("wave_id") == wave_id
            ]
            rebuildable = bool(wave_records) and all(
                (item.get("state") == "rejected" and item.get("owner") is None)
                or (
                    item.get("state") == "starting"
                    and item.get("tool_kind") in {"spawn", "reuse"}
                    and (
                        item.get("tool_use_id") is None
                        or item.get("dispatch_id") == dispatch_id
                    )
                    and (
                        item.get("owner") is None
                        if item.get("tool_kind") == "spawn"
                        else isinstance(item.get("owner"), str)
                        and isinstance(item.get("reused_from"), str)
                    )
                )
                for item in wave_records
            )
            if not rebuildable:
                self._fence_members(state, dispatch, "workspace_baseline_stale")
                self._settle_wave(state)
                self._write_state(state)
                return False
            plan = self._read_plan(state)
            for item in wave_records:
                if item.get("state") == "rejected":
                    continue
                self._append_tombstone(state, item, "workspace_baseline_recaptured")
                for member in item["members"]:
                    logical = state["logical"][member]
                    logical["dispatch_id"] = None
                    logical["result"] = None
                    logical["state"] = "waiting"
                del state["dispatches"][item["dispatch_id"]]
            state["active_wave_id"] = None
            self._refresh_ready(state, plan)
            self._write_state(state)
            return True

    def _quarantine_legacy_state(self, path: Path) -> list[dict[str, Any]]:
        """Atomically isolate invalid legacy state inside a marked CCO root."""

        if not self._state_root_is_marked():
            return []
        reservation: Path | None = None
        with acquire(
            self.root,
            STATE_ROOT_CAPACITY_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            recovery_count = sum(
                RECOVERY_FILE_RE.fullmatch(candidate.name) is not None
                for candidate in _state_json_paths(self.root)
            )
            source_is_recovery = RECOVERY_FILE_RE.fullmatch(path.name) is not None
            if recovery_count - int(source_is_recovery) >= MAX_RECOVERY_FILES:
                raise ControlPlaneUnavailable(
                    "lifecycle state directory exceeds the "
                    f"{MAX_RECOVERY_FILES} recovery file limit"
                )
            try:
                descriptor, reservation_name = tempfile.mkstemp(
                    dir=self.root,
                    prefix=".cco-recovery-",
                    suffix=".reserve",
                )
                os.close(descriptor)
                reservation = Path(reservation_name)
            except OSError as error:
                raise ControlPlaneUnavailable("legacy quarantine is unavailable") from error
            staging = reservation.with_suffix(".json")
            try:
                os.replace(path, staging)
            except FileNotFoundError:
                return []
            except OSError as error:
                raise ControlPlaneUnavailable("legacy lifecycle state is unavailable") from error
            finally:
                if reservation is not None:
                    try:
                        reservation.unlink(missing_ok=True)
                    except OSError as error:
                        raise ControlPlaneUnavailable(
                            "legacy recovery reservation cleanup failed"
                        ) from error

        # Recovery staging stays in the state root and ends in .json.  A crash or I/O
        # failure therefore remains visible to the next workspace lease scan.
        raw = _read_bounded_bytes(staging, "staged legacy lifecycle state")
        try:
            staged_state = self._validate_lifecycle_state(
                _decode_object(raw, "staged legacy cco.v9 lifecycle state")
            )
        except ControlPlaneError:
            staged_state = None

        if staged_state is not None:
            _replayed_path, replayed = self._replay_recovery_state(
                staging,
                staged_state,
            )
            recovered = [replayed]
            if path.exists():
                try:
                    replacement = self._validate_lifecycle_state(
                        _load_object(path, "replacement legacy cco.v9 lifecycle state")
                    )
                except ControlPlaneUnavailable:
                    raise
                except ControlPlaneError:
                    pass
                else:
                    if replacement not in recovered:
                        recovered.append(replacement)
            return recovered

        name_identity = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]
        content_identity = hashlib.sha256(raw).hexdigest()
        quarantine = self.root / "quarantine"
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
            destination = quarantine / f"legacy-{name_identity}-{content_identity}.json"
            os.link(staging, destination)
        except FileExistsError:
            if _read_bounded_bytes(destination, "quarantined lifecycle state") != raw:
                raise ControlPlaneError("legacy quarantine identity collision")
        except OSError as error:
            raise ControlPlaneUnavailable("legacy quarantine is unavailable") from error
        try:
            staging.unlink(missing_ok=True)
        except OSError as error:
            raise ControlPlaneUnavailable("legacy quarantine finalization failed") from error
        recovered: list[dict[str, Any]] = []
        if path.exists():
            try:
                replacement = self._validate_lifecycle_state(
                    _load_object(path, "replacement legacy cco.v9 lifecycle state")
                )
            except ControlPlaneUnavailable:
                raise
            except ControlPlaneError:
                pass
            else:
                if replacement not in recovered:
                    recovered.append(replacement)
        return recovered

    def _replay_recovery_state(
        self,
        path: Path,
        state: dict[str, Any],
    ) -> tuple[Path, dict[str, Any]]:
        """Publish one recovery under its own workspace lock and direct-parent CAS."""

        canonical = _lifecycle_state_path(
            self.root,
            state["workspace_root"],
            state["session_id"],
        )
        with acquire(
            self.root,
            _workspace_lock_identity(state["workspace_root"]),
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            with acquire(
                self.root,
                STATE_ROOT_CAPACITY_LOCK,
                timeout=_bounded_lock_timeout(self.lock_timeout),
            ):
                recovery_raw = _read_bounded_bytes(
                    path,
                    "recoverable cco.v9 lifecycle state",
                )
                recovery = self._validate_lifecycle_state(
                    _decode_object(
                        recovery_raw,
                        "recoverable cco.v9 lifecycle state",
                    ),
                    expected_session=state["session_id"],
                )
                if (
                    recovery["lineage_id"] != state["lineage_id"]
                    or recovery["revision"] != state["revision"]
                ):
                    raise ControlPlaneError("lifecycle recovery changed during replay")
                if canonical.exists():
                    current_raw = _read_bounded_bytes(
                        canonical,
                        "recovered cco.v9 lifecycle state",
                    )
                    current = self._validate_lifecycle_state(
                        _decode_object(
                            current_raw,
                            "recovered cco.v9 lifecycle state",
                        ),
                        expected_session=state["session_id"],
                    )
                    same_state = current == recovery
                    current_is_child = (
                        current["lineage_id"] == recovery["lineage_id"]
                        and current["revision"] == recovery["revision"] + 1
                        and current.get("parent_state_sha256")
                        == _state_content_id(recovery_raw)
                    )
                    recovery_is_child = (
                        current["lineage_id"] == recovery["lineage_id"]
                        and recovery["revision"] == current["revision"] + 1
                        and recovery.get("parent_state_sha256")
                        == _state_content_id(current_raw)
                    )
                    if same_state or current_is_child:
                        if _read_bounded_bytes(
                            path,
                            "recoverable cco.v9 lifecycle state",
                        ) != recovery_raw:
                            raise ControlPlaneUnavailable(
                                "lifecycle recovery changed during finalization"
                            )
                        try:
                            path.unlink(missing_ok=True)
                        except OSError as error:
                            raise ControlPlaneUnavailable(
                                "lifecycle recovery finalization failed"
                            ) from error
                        return canonical, current
                    if recovery_is_child:
                        if (
                            _read_bounded_bytes(
                                canonical,
                                "recovered cco.v9 lifecycle state",
                            )
                            != current_raw
                            or _read_bounded_bytes(
                                path,
                                "recoverable cco.v9 lifecycle state",
                            )
                            != recovery_raw
                        ):
                            raise ControlPlaneUnavailable(
                                "lifecycle recovery changed during publication"
                            )
                        try:
                            os.replace(path, canonical)
                        except OSError as error:
                            raise ControlPlaneUnavailable(
                                "lifecycle recovery publication failed"
                            ) from error
                        return canonical, recovery
                    raise ControlPlaneError("conflicting lifecycle recovery")
                if _state_capacity_used(_state_json_paths(self.root)) >= MAX_STATE_FILES:
                    return path, recovery
                try:
                    os.link(path, canonical)
                except FileExistsError:
                    return path, recovery
                except OSError as error:
                    raise ControlPlaneUnavailable(
                        "valid legacy lifecycle state could not be restored"
                    ) from error
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    try:
                        if os.path.samefile(path, canonical):
                            canonical.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise ControlPlaneUnavailable(
                        "lifecycle recovery finalization failed"
                    ) from error
                return canonical, recovery

    def _workspace_state_candidates(
        self,
        workspace_root: object,
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Load only indexed same-workspace state plus quarantinable legacy files."""

        workspace_digest = _workspace_digest(workspace_root)
        snapshot = _state_json_paths(self.root)
        indexed: list[Path] = []
        legacy: list[Path] = []
        for path in snapshot:
            match = STATE_FILE_RE.fullmatch(path.name)
            if match is None:
                legacy.append(path)
                continue
            if match.group("workspace") == workspace_digest:
                indexed.append(path)
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in indexed:
            checkpoint()
            match = STATE_FILE_RE.fullmatch(path.name)
            if match is None:
                raise ControlPlaneError("indexed lifecycle filename is invalid")
            raw_state = _load_object(path, "cco.v9 lifecycle state")
            state = self._validate_lifecycle_state(raw_state)
            if (
                match.group("workspace") != _workspace_digest(state["workspace_root"])
                or match.group("session") != _session_digest(state["session_id"])
            ):
                raise ControlPlaneError("indexed lifecycle filename does not match its state")
            candidates.append((path, state))
        for path in legacy:
            checkpoint()
            try:
                raw_state = _load_object(path, "legacy cco.v9 lifecycle state")
                state = self._validate_lifecycle_state(raw_state)
                state_workspace_digest = _workspace_digest(state["workspace_root"])
            except ControlPlaneUnavailable:
                raise
            except ControlPlaneError:
                recovered = self._quarantine_legacy_state(path)
                for state in recovered:
                    if _workspace_digest(state["workspace_root"]) == workspace_digest:
                        candidates.append((path, state))
                continue
            if state_workspace_digest == workspace_digest:
                if (
                    RECOVERY_FILE_RE.fullmatch(path.name) is not None
                    and self._state_root_is_marked()
                ):
                    path, state = self._replay_recovery_state(path, state)
                candidates.append((path, state))
        return candidates

    def _assert_cross_task_compatible(
        self,
        workspace_root: object,
        *,
        role: str,
        scopes: list[Mapping[str, str]],
        current_dispatch: str | None = None,
    ) -> None:
        target = _workspace_key(workspace_root)
        now = _now_milliseconds()
        for _path, state in self._workspace_state_candidates(workspace_root):
            checkpoint()
            if _workspace_key(state["workspace_root"]) != target:
                continue
            for dispatch in state["dispatches"].values():
                if state["session_id"] == self.session_id and dispatch.get(
                    "dispatch_id"
                ) == current_dispatch:
                    continue
                if role == "worker" and _writer_lease_active(dispatch, now=now):
                    raise ControlPlaneError(
                        "workspace writer lease is already held by "
                        f"{state['session_id']}:{dispatch['dispatch_id']}"
                    )
                if (
                    role == "worker"
                    and _reader_active(dispatch, now=now)
                    and _scopes_overlap(scopes, dispatch["scopes"])
                ):
                    raise ControlPlaneError(
                        "workspace has an overlapping reader held by "
                        f"{state['session_id']}:{dispatch['dispatch_id']}"
                    )
                if (
                    role != "worker"
                    and _writer_lease_active(dispatch, now=now)
                    and _scopes_overlap(scopes, dispatch["scopes"])
                ):
                    raise ControlPlaneError(
                        "workspace writer overlaps this reader: "
                        f"{state['session_id']}:{dispatch['dispatch_id']}"
                    )

    def _artifact_path(self, kind: str, identity: str) -> Path:
        if kind not in {"plan", "wave"} or SHA256_RE.fullmatch(identity) is None:
            raise ControlPlaneError("artifact identity is invalid")
        return self.root / "artifacts" / f"{self.session_id}-{kind}-{identity[7:]}.json"

    def _read_state(self) -> dict[str, Any]:
        # A failed recovery finalization can leave both the cached canonical path
        # and a later recovery path. Refresh only at the authoritative read point
        # so an existing ControlPlane cannot advance past an unseen recovery.
        if (
            self._state_path is not None
            and self._state_path.exists()
            and STATE_FILE_RE.fullmatch(self._state_path.name) is not None
        ):
            with acquire(
                self.root,
                STATE_ROOT_CAPACITY_LOCK,
                timeout=_bounded_lock_timeout(self.lock_timeout),
            ):
                matches = _session_state_paths(self.root, self.session_id)
            recoveries = [
                path for path in matches if RECOVERY_FILE_RE.fullmatch(path.name)
            ]
            if len(recoveries) > 1:
                raise ControlPlaneError(
                    "current task has multiple lifecycle recovery files"
                )
            if recoveries:
                self._state_path = recoveries[0]
        source = self.state_path
        raw_state = _load_object(source, "cco.v9 lifecycle state")
        state = self._validate_lifecycle_state(
            raw_state,
            expected_session=self.session_id,
        )
        state = self._restore_legacy_context_from_wave(state)
        canonical = _lifecycle_state_path(
            self.root,
            state["workspace_root"],
            self.session_id,
        )
        if (
            RECOVERY_FILE_RE.fullmatch(source.name) is not None
            and self._state_root_is_marked()
        ):
            source, state = self._replay_recovery_state(source, state)
            self._state_path = source
            raw_state = deepcopy(state)
            state = self._restore_legacy_context_from_wave(state)
            if source != canonical:
                return state
        if STATE_FILE_RE.fullmatch(source.name) is not None and source != canonical:
            raise ControlPlaneError("indexed lifecycle filename does not match its state")
        legacy = self.root / f"{self.session_id}.json"
        if source == canonical and legacy.exists():
            legacy_raw = _load_object(legacy, "legacy cco.v9 lifecycle state")
            legacy_state = self._validate_lifecycle_state(
                legacy_raw,
                expected_session=self.session_id,
            )
            legacy_state = self._restore_legacy_context_from_wave(legacy_state)
            canonical_revision = state.get("revision")
            legacy_revision = legacy_state.get("revision")
            comparable = deepcopy(legacy_state)
            comparable["revision"] = canonical_revision
            # Older indexed migrations predate parent hashes. Equality of every
            # other field plus the exact one-revision step remains their proof.
            comparable["parent_state_sha256"] = state.get("parent_state_sha256")
            if (
                isinstance(canonical_revision, bool)
                or not isinstance(canonical_revision, int)
                or isinstance(legacy_revision, bool)
                or not isinstance(legacy_revision, int)
                or canonical_revision != legacy_revision + 1
                or comparable != state
            ):
                raise ControlPlaneError("current task has conflicting lifecycle state files")
            try:
                legacy.unlink()
            except FileNotFoundError:
                pass
        if source != canonical:
            if canonical.exists():
                raise ControlPlaneError("current task has conflicting lifecycle state files")
            try:
                os.replace(source, canonical)
            except FileNotFoundError as error:
                raise ControlPlaneUnavailable("lifecycle state migration was interrupted") from error
            except OSError as error:
                raise ControlPlaneUnavailable("lifecycle state migration failed") from error
            self._state_path = canonical
            self._write_state(state)
        elif state != raw_state:
            self._write_state(state)
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self._mark_state_root_if_safe()
        canonical = _lifecycle_state_path(
            self.root,
            state["workspace_root"],
            self.session_id,
        )
        if (
            self._state_path is not None
            and RECOVERY_FILE_RE.fullmatch(self._state_path.name) is not None
            and not canonical.exists()
        ):
            target = self._state_path
        else:
            target = canonical
        self._state_path = target
        if target.exists():
            current_raw = _read_bounded_bytes(target, "current cco.v9 lifecycle state")
            current = self._validate_lifecycle_state(
                _decode_object(current_raw, "current cco.v9 lifecycle state"),
                expected_session=self.session_id,
            )
            if (
                current["lineage_id"] != state.get("lineage_id")
                or current["revision"] != state.get("revision")
            ):
                raise ControlPlaneError("lifecycle state changed before persistence")
            state["parent_state_sha256"] = _state_content_id(current_raw)
        else:
            state["parent_state_sha256"] = None
        state["revision"] = int(state.get("revision", 0)) + 1
        _atomic_write(target, state)
        artifacts = self.root / "artifacts"
        if not artifacts.is_dir():
            return
        active_wave = state.get("active_wave_id")
        keep_wave = (
            self._artifact_path("wave", active_wave)
            if isinstance(active_wave, str)
            else None
        )
        for path in artifacts.glob(f"{self.session_id}-wave-*.json"):
            if keep_wave is None or path != keep_wave:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        keep_plan = Path(state["plan_path"])
        for path in artifacts.glob(f"{self.session_id}-plan-*.json"):
            if path != keep_plan:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _read_plan(self, state: Mapping[str, Any]) -> dict[str, Any]:
        plan = _load_object(Path(state["plan_path"]), "cco.v9 plan artifact")
        if plan.get("protocol") != PLAN_PROTOCOL or plan.get("plan_id") != state.get("plan_id"):
            raise ControlPlaneError("plan artifact identity is invalid")
        unsigned = {key: value for key, value in plan.items() if key != "plan_id"}
        if _digest(b"cco.plan.v1\0", unsigned) != plan["plan_id"]:
            raise ControlPlaneError("plan artifact digest is invalid")
        return plan

    def _read_wave(self, state: Mapping[str, Any]) -> dict[str, Any]:
        wave_id = state.get("active_wave_id")
        if not isinstance(wave_id, str):
            raise ControlPlaneError("there is no active wave")
        wave = _load_object(self._artifact_path("wave", wave_id), "cco.v9 wave artifact")
        protocol = wave.get("protocol")
        if protocol not in {LEGACY_WAVE_PROTOCOL, WAVE_PROTOCOL} or wave.get(
            "wave_id"
        ) != wave_id:
            raise ControlPlaneError("wave artifact identity is invalid")
        identity = {
            key: wave.get(key)
            for key in ("baseline_id", "plan_id", "protocol", "sequence", "units")
        }
        if (
            wave.get("plan_id") != state.get("plan_id")
            or not isinstance(wave.get("baseline"), Mapping)
            or wave["baseline"].get("state_id") != wave.get("baseline_id")
            or _digest(
                b"cco.wave.v1\0"
                if protocol == LEGACY_WAVE_PROTOCOL
                else b"cco.wave.v2\0",
                identity,
            )
            != wave_id
        ):
            raise ControlPlaneError("wave artifact digest is invalid")
        units = wave.get("units")
        if not isinstance(units, list) or not units:
            raise ControlPlaneError("wave artifact has no physical units")
        expected_scopes = {
            (scope["kind"], scope["path"]): scope
            for unit in units
            if isinstance(unit, Mapping) and isinstance(unit.get("scopes"), list)
            for scope in unit["scopes"]
            if isinstance(scope, Mapping)
            and set(scope) == {"kind", "path"}
        }
        if wave["baseline"].get("scopes") != [
            expected_scopes[key] for key in sorted(expected_scopes)
        ]:
            raise ControlPlaneError("wave baseline scopes do not match its physical units")
        return wave

    def _restore_legacy_context_from_wave(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        unknown = [
            dispatch
            for dispatch in state["dispatches"].values()
            if dispatch.get("legacy_context_unknown") is True
        ]
        if not unknown or not isinstance(state.get("active_wave_id"), str):
            return state
        wave = self._read_wave(state)
        units = {
            unit.get("id"): unit
            for unit in wave["units"]
            if isinstance(unit, Mapping) and isinstance(unit.get("id"), str)
        }
        for dispatch in unknown:
            if dispatch.get("wave_id") != wave["wave_id"]:
                continue
            unit = units.get(dispatch.get("unit_id"))
            context_turns = unit.get("context_turns") if isinstance(unit, Mapping) else None
            if (
                isinstance(context_turns, bool)
                or not isinstance(context_turns, int)
                or not 0 <= context_turns <= 32
            ):
                continue
            dispatch["context_turns"] = context_turns
            dispatch.pop("legacy_context_unknown", None)
        return state

    def create_plan(
        self,
        repo: Path,
        brief: object,
        *,
        resume_identical: bool = False,
    ) -> dict[str, Any]:
        backend, workspace = discover_workspace(repo)
        normalized = _normalize_brief(brief, workspace, backend)
        unsigned = {
            **normalized,
            "protocol": PLAN_PROTOCOL,
            "workspace_backend": backend,
            "workspace_root": str(workspace),
        }
        plan_id = _digest(b"cco.plan.v1\0", unsigned)
        plan = {**unsigned, "plan_id": plan_id}
        plan_path = self._artifact_path("plan", plan_id)
        if self.state_path.exists():
            with self._coordinated():
                state = self._read_state()
                if (
                    resume_identical
                    and state.get("plan_id") == plan_id
                    and state.get("active_wave_id") is None
                    and state.get("wave_sequence") == 0
                    and not state.get("dispatches")
                    and all(
                        item.get("state") in {"waiting", "ready"}
                        for item in state["logical"].values()
                    )
                    and self._read_plan(state) == plan
                ):
                    return {
                        "plan_id": plan_id,
                        "protocol": PLAN_PROTOCOL,
                        "ready": sorted(
                            node
                            for node, item in state["logical"].items()
                            if item["state"] == "ready"
                        ),
                        "workspace_root": str(workspace),
                    }
                raise ControlPlaneError(
                    "the current task already has CCO lifecycle proof; "
                    "run explicit cleanup first"
                )
        with acquire(self.root, self.session_id, timeout=self.lock_timeout):
            if self.state_path.exists():
                raise ControlPlaneError(
                    "the current task already has CCO lifecycle proof; run explicit cleanup first"
                )
            with acquire(
                self.root,
                STATE_ROOT_CAPACITY_LOCK,
                timeout=_bounded_lock_timeout(self.lock_timeout),
            ):
                if _state_capacity_used(_state_json_paths(self.root)) >= MAX_STATE_FILES:
                    raise ControlPlaneUnavailable(
                        "lifecycle state directory exceeds the "
                        f"{MAX_STATE_FILES} file limit"
                    )
                _write_immutable(plan_path, plan)
                logical = {
                    item["id"]: {
                        "assurance": item["assurance"],
                        "dispatch_id": None,
                        "generation": 1,
                        "result": None,
                        "state": "waiting",
                    }
                    for item in plan["nodes"]
                }
                state = {
                    "active_wave_id": None,
                    "dispatches": {},
                    "epoch": 1,
                    "lineage_id": _lifecycle_lineage_id(
                        workspace,
                        self.session_id,
                        plan_id,
                    ),
                    "logical": logical,
                    "plan_id": plan_id,
                    "plan_path": str(plan_path),
                    "protocol": LIFECYCLE_PROTOCOL,
                    "revision": 0,
                    "session_id": self.session_id,
                    "tombstones": [],
                    "wave_sequence": 0,
                    "workspace_root": str(workspace),
                }
                self._refresh_ready(state, plan)
                try:
                    self._write_state(state)
                except Exception:
                    if not self.state_path.exists():
                        plan_path.unlink(missing_ok=True)
                    raise
        return {
            "plan_id": plan_id,
            "protocol": PLAN_PROTOCOL,
            "ready": sorted(node for node, item in logical.items() if item["state"] == "ready"),
            "workspace_root": str(workspace),
        }

    @staticmethod
    def _logical_satisfied(
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        node_id: str,
    ) -> bool:
        logical = state["logical"][node_id]
        if logical["state"] != "retired":
            return False
        node = _node_map(plan)[node_id]
        return (
            node["role"] != "reviewer"
            or (logical.get("result") or {}).get("outcome") == "accept"
        )

    @classmethod
    def _refresh_ready(cls, state: dict[str, Any], plan: Mapping[str, Any]) -> None:
        nodes = _node_map(plan)
        changed = True
        while changed:
            changed = False
            for node_id, logical in state["logical"].items():
                if logical["state"] != "waiting":
                    continue
                if all(
                    cls._logical_satisfied(state, plan, dependency)
                    for dependency in nodes[node_id]["depends_on"]
                ):
                    logical["state"] = "ready"
                    changed = True

    @staticmethod
    def _overall_state(state: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
        logical = state["logical"]
        states = [item["state"] for item in logical.values()]
        if any(
            _native_claim_active(dispatch)
            or dispatch.get("state") in ACTIVE_STATES
            for dispatch in state["dispatches"].values()
        ):
            return "active"
        if any(item in {"ready", "starting"} for item in states):
            return "ready"
        if any(item in {"fenced", "waiting"} for item in states):
            return "blocked"
        if all(item == "retired" for item in states):
            nodes = _node_map(plan)
            rejected_review = any(
                nodes[node_id]["role"] == "reviewer"
                and (item.get("result") or {}).get("outcome") != "accept"
                for node_id, item in logical.items()
            )
            return "blocked" if rejected_review else "complete"
        return "ready"

    def _routes(
        self,
        plan: Mapping[str, Any],
        nodes: list[dict[str, Any]],
        native_catalog: object,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        requests = [
            {
                "assurance": node["assurance"],
                "constraints": node["pin"],
                "node": node["id"],
                "role": node["role"],
            }
            for node in nodes
        ]
        policy = load_route_policy(Path(plan["workspace_root"]))["policy"]
        errors: dict[str, str] = {}
        try:
            route_plan = resolve_route_plan(requests, native_catalog, policy=policy)
            return {item["node"]: item for item in route_plan["routes"]}, errors
        except RoutingCatalogError:
            routes: dict[str, dict[str, Any]] = {}
            for request in requests:
                try:
                    resolved = resolve_route_plan([request], native_catalog, policy=policy)
                    routes[request["node"]] = resolved["routes"][0]
                except RoutingCatalogError as error:
                    errors[request["node"]] = str(error)
            return routes, errors

    def _dependency_evidence(
        self,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        members: list[str],
    ) -> dict[str, Any]:
        nodes = _node_map(plan)
        dependencies = sorted({item for member in members for item in nodes[member]["depends_on"]})
        return {
            item: state["logical"][item]["result"]
            for item in dependencies
            if state["logical"][item]["result"] is not None
        }

    @staticmethod
    def _selected_dispatch_route(dispatch: Mapping[str, Any]) -> Mapping[str, Any] | None:
        candidates = dispatch.get("route_candidates")
        cursor = dispatch.get("route_cursor")
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not isinstance(candidates, list)
            or not 0 <= cursor < len(candidates)
            or not isinstance(candidates[cursor], Mapping)
        ):
            return None
        return candidates[cursor]

    @classmethod
    def _source_matches_reuse(
        cls,
        source: Mapping[str, Any],
        *,
        dependency_dispatches: set[str],
        role: str,
        assurance: str,
        route: Mapping[str, Any],
        scopes: list[dict[str, str]],
    ) -> bool:
        result = source.get("result")
        selected = cls._selected_dispatch_route(source)
        owner = source.get("owner")
        return (
            source.get("dispatch_id") in dependency_dispatches
            and source.get("state") == "retired"
            and source.get("role") == role
            and role in {"explorer", "worker"}
            and source.get("assurance") == assurance
            and isinstance(source.get("members"), list)
            and len(source["members"]) == 1
            and isinstance(owner, str)
            and TASK_PATH_RE.fullmatch(owner) is not None
            and source.get("legacy_context_unknown") is not True
            and source.get("transient_retries") == 0
            and isinstance(result, Mapping)
            and result.get("status") == "complete"
            and result.get("outcome") == "retire"
            and result.get("blockers") == []
            and result.get("deviations") == []
            and result.get("failure_signature") is None
            and isinstance(selected, Mapping)
            and selected.get("model") == route.get("model")
            and selected.get("effort") == route.get("effort")
            and isinstance(source.get("scopes"), list)
            and _scopes_within(scopes, source["scopes"])
        )

    @classmethod
    def _reuse_candidate(
        cls,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        unit: Mapping[str, Any],
        *,
        route_cursor: int,
        reserved_owners: set[str],
    ) -> Mapping[str, Any] | None:
        if (
            unit.get("role") == "reviewer"
            or unit.get("context_turns") != 0
            or not isinstance(unit.get("members"), list)
            or len(unit["members"]) != 1
        ):
            return None
        member = unit["members"][0]
        node = _node_map(plan)[member]
        dependency_dispatches = {
            state["logical"][dependency].get("dispatch_id")
            for dependency in node["depends_on"]
            if isinstance(state["logical"][dependency].get("dispatch_id"), str)
        }
        route = unit["route"]["candidates"][route_cursor]
        candidates: list[Mapping[str, Any]] = []
        for dispatch_id in sorted(dependency_dispatches):
            source = state["dispatches"].get(dispatch_id)
            if not isinstance(source, Mapping) or not cls._source_matches_reuse(
                source,
                dependency_dispatches=dependency_dispatches,
                role=unit["role"],
                assurance=unit["assurance"],
                route=route,
                scopes=unit["scopes"],
            ):
                continue
            owner = source["owner"]
            if owner in reserved_owners or any(
                other.get("dispatch_id") != source.get("dispatch_id")
                and other.get("owner") == owner
                and other.get("state") in {"starting", "running", "paused"}
                for other in state["dispatches"].values()
            ):
                continue
            candidates.append(source)
        return candidates[0] if len(candidates) == 1 else None

    def _dispatch_record(
        self,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        wave_id: str,
        unit: Mapping[str, Any],
        *,
        route_cursor: int,
        reused_from: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        route = unit["route"]["candidates"][route_cursor]
        generation = max(state["logical"][item]["generation"] for item in unit["members"])
        source_id = reused_from.get("dispatch_id") if reused_from is not None else None
        identity = {
            "cursor": 0,
            "generation": generation,
            "members": unit["members"],
            "reused_from": source_id,
            "route": route,
            "route_cursor": route_cursor,
            "wave_id": wave_id,
        }
        dispatch_id = _digest(b"cco.dispatch.v2\0", identity)
        task_name = _task_name(unit, route, generation, dispatch_id)
        message = _render_task(
            plan,
            unit,
            dispatch_id,
            cursor=0,
            dependency_evidence=self._dependency_evidence(state, plan, unit["members"]),
        )
        if reused_from is None:
            native = {
                "agent_type": WRITE_ROLE if unit["role"] == "worker" else READ_ROLE,
                "fork_turns": "none"
                if unit["context_turns"] == 0
                else str(unit["context_turns"]),
                "message": message,
                "model": route["model"],
                "reasoning_effort": route["effort"],
                "task_name": task_name,
            }
            owner = None
            tool_kind = "spawn"
        else:
            native = {"message": message, "target": reused_from["owner"]}
            owner = reused_from["owner"]
            tool_kind = "reuse"
        return {
            "assurance": unit["assurance"],
            "claim_expires_at": (
                _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
            ),
            "cursor": 0,
            "context_turns": unit["context_turns"],
            "dispatch_id": dispatch_id,
            "fallback_from_owner": None,
            "generation": generation,
            "members": list(unit["members"]),
            "native": native,
            "owner": owner,
            "pending_cursor": None,
            "transient_retries": 0,
            "role": unit["role"],
            "route_candidates": deepcopy(unit["route"]["candidates"]),
            "route_cursor": route_cursor,
            "reused_from": source_id,
            "scopes": deepcopy(unit["scopes"]),
            "state": "starting",
            "task_name": task_name,
            "tool_kind": tool_kind,
            "tool_use_id": None,
            "unit_id": unit["id"],
            "wave_id": wave_id,
            "workspace_root": plan["workspace_root"],
        }

    @staticmethod
    def _available_route_cursor(
        state: Mapping[str, Any],
        unit: Mapping[str, Any],
    ) -> int | None:
        generation = max(
            state["logical"][member]["generation"] for member in unit["members"]
        )
        rejected: set[tuple[str, str]] = set()
        for dispatch in state["dispatches"].values():
            if (
                dispatch.get("state") != "rejected"
                or dispatch.get("generation") != generation
                or dispatch.get("members") != unit.get("members")
            ):
                continue
            cursor = dispatch.get("route_cursor")
            candidates = dispatch.get("route_candidates")
            if (
                isinstance(cursor, bool)
                or not isinstance(cursor, int)
                or not isinstance(candidates, list)
                or not 0 <= cursor < len(candidates)
                or not isinstance(candidates[cursor], Mapping)
            ):
                raise ControlPlaneError("rejected route history is invalid")
            route = candidates[cursor]
            rejected.add((str(route.get("model")), str(route.get("effort"))))
        for cursor, route in enumerate(unit["route"]["candidates"]):
            if (str(route.get("model")), str(route.get("effort"))) not in rejected:
                return cursor
        return None

    @staticmethod
    def _public_batch(state: Mapping[str, Any], dispatches: list[Mapping[str, Any]]) -> dict[str, Any]:
        actions = []
        for dispatch in dispatches:
            if dispatch.get("tool_kind") == "spawn":
                actions.append(
                    _tool_action("spawn_new_owner", "spawn_agent", dispatch["native"])
                )
            elif dispatch.get("tool_kind") == "reuse":
                actions.append(
                    _tool_action("reuse_owner", "followup_task", dispatch["native"])
                )
            else:
                raise ControlPlaneError("wave contains an unsupported native action")
        return {
            "dispatches": actions,
            "plan_id": state["plan_id"],
            "protocol": BATCH_PROTOCOL,
            "state": "dispatch" if dispatches else "waiting",
            "wave_id": state.get("active_wave_id"),
        }

    def next_wave(
        self,
        *,
        capacity: int,
        native_catalog: object | None = None,
    ) -> dict[str, Any]:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ControlPlaneError("native capacity must be a positive integer")
        catalog = load_native_catalog() if native_catalog is None else native_catalog
        with self._coordinated():
            state = self._read_state()
            reconciled = self._reconcile_expired_claims(state)
            plan = self._read_plan(state)
            if state["active_wave_id"] is not None:
                pending = [
                    item
                    for item in state["dispatches"].values()
                    if item["wave_id"] == state["active_wave_id"]
                    and item["state"] == "starting"
                    and item["tool_use_id"] is None
                ]
                if pending:
                    for dispatch in pending:
                        self._assert_cross_task_compatible(
                            state["workspace_root"],
                            role=dispatch["role"],
                            scopes=dispatch["scopes"],
                            current_dispatch=dispatch["dispatch_id"],
                        )
                    deadline = _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
                    for dispatch in pending:
                        dispatch["claim_expires_at"] = deadline
                    self._write_state(state)
                    return self._public_batch(state, sorted(pending, key=lambda item: item["task_name"]))
                active = [
                    item
                    for item in state["dispatches"].values()
                    if item["wave_id"] == state["active_wave_id"]
                    and (
                        item["state"] in ACTIVE_STATES
                        or _native_claim_active(item)
                    )
                ]
                if active:
                    if reconciled:
                        self._write_state(state)
                    return self._public_batch(state, [])
                state["active_wave_id"] = None
                self._refresh_ready(state, plan)
            nodes = _node_map(plan)
            ready = []
            for node_id, logical in state["logical"].items():
                if logical["state"] != "ready":
                    continue
                node = deepcopy(nodes[node_id])
                node["assurance"] = logical["assurance"]
                ready.append(node)
            if not ready:
                self._write_state(state)
                return {
                    "dispatches": [],
                    "plan_id": state["plan_id"],
                    "protocol": BATCH_PROTOCOL,
                    "state": self._overall_state(state, plan),
                    "wave_id": None,
                }
            routes, route_errors = self._routes(plan, ready, catalog)
            for node_id, error in route_errors.items():
                state["logical"][node_id]["state"] = "fenced"
                state["logical"][node_id]["result"] = {
                    "failure_signature": "route_unavailable",
                    "summary": error,
                }
            routable = [item for item in ready if item["id"] in routes]
            if not routable:
                self._write_state(state)
                return {
                    **self._public_batch(state, []),
                    "blocked": [
                        {"node": node, "reason": route_errors[node]}
                        for node in sorted(route_errors)
                    ],
                    "state": "blocked",
                }
            downstream = _descendant_counts(plan)
            units = _physical_units(
                routable,
                routes,
                capacity=capacity,
                downstream=downstream,
            )
            selected = _select_units(units, capacity)
            route_cursors: dict[str, int] = {}
            available: list[dict[str, Any]] = []
            for unit in selected:
                cursor = self._available_route_cursor(state, unit)
                if cursor is None:
                    for member in unit["members"]:
                        state["logical"][member]["state"] = "fenced"
                        state["logical"][member]["result"] = {
                            "failure_signature": "route_exhausted",
                            "summary": "all prepared native routes were rejected",
                        }
                        route_errors[member] = "all prepared native routes were rejected"
                    continue
                route_cursors[unit["id"]] = cursor
                available.append(unit)
            selected = available
            if not selected:
                self._write_state(state)
                return {
                    **self._public_batch(state, []),
                    "blocked": [
                        {"node": node, "reason": route_errors[node]}
                        for node in sorted(route_errors)
                    ],
                    "state": "blocked",
                }
            reuse_sources: dict[str, str | None] = {}
            reserved_owners: set[str] = set()
            for unit in selected:
                source = self._reuse_candidate(
                    state,
                    plan,
                    unit,
                    route_cursor=route_cursors[unit["id"]],
                    reserved_owners=reserved_owners,
                )
                source_id = source.get("dispatch_id") if source is not None else None
                reuse_sources[unit["id"]] = source_id
                if source is not None:
                    reserved_owners.add(source["owner"])
            wave_scopes = {
                (scope["kind"], scope["path"]): dict(scope)
                for unit in selected
                for scope in unit["scopes"]
            }
            artifact_units = []
            for unit in selected:
                artifact_units.append(
                    {
                        "assurance": unit["assurance"],
                        "context_turns": unit["context_turns"],
                        "generation": max(
                            state["logical"][member]["generation"]
                            for member in unit["members"]
                        ),
                        "id": unit["id"],
                        "members": list(unit["members"]),
                        "reused_from": reuse_sources[unit["id"]],
                        "role": unit["role"],
                        "route_candidates": deepcopy(unit["route"]["candidates"]),
                        "scopes": deepcopy(unit["scopes"]),
                    }
                )
            self._write_state(state)
            expected_revision = state["revision"]
            workspace_root = plan["workspace_root"]
            plan_id = plan["plan_id"]
            blocked = [
                {"node": node, "reason": route_errors[node]}
                for node in sorted(route_errors)
            ]

        baseline = capture_workspace(
            Path(workspace_root),
            scopes=[wave_scopes[key] for key in sorted(wave_scopes)],
            writable=any(unit["role"] == "worker" for unit in selected),
        )

        with self._coordinated(workspace_root):
            state = self._read_state()
            if self._reconcile_expired_claims(state):
                self._write_state(state)
                raise ControlPlaneError(
                    "lifecycle changed while preparing the wave; retry next"
                )
            if (
                state["revision"] != expected_revision
                or state["active_wave_id"] is not None
                or any(
                    state["logical"][member]["state"] != "ready"
                    for unit in selected
                    for member in unit["members"]
                )
            ):
                raise ControlPlaneError(
                    "lifecycle changed while preparing the wave; retry next"
                )
            plan = self._read_plan(state)
            if plan["plan_id"] != plan_id or plan["workspace_root"] != workspace_root:
                raise ControlPlaneError("plan changed while preparing the wave")
            for unit in selected:
                self._assert_cross_task_compatible(
                    workspace_root,
                    role=unit["role"],
                    scopes=unit["scopes"],
                )
            state["wave_sequence"] += 1
            wave_identity = {
                "baseline_id": baseline["state_id"],
                "plan_id": plan_id,
                "protocol": WAVE_PROTOCOL,
                "sequence": state["wave_sequence"],
                "units": artifact_units,
            }
            wave_id = _digest(b"cco.wave.v2\0", wave_identity)
            wave = {**wave_identity, "baseline": baseline, "wave_id": wave_id}
            _write_immutable(self._artifact_path("wave", wave_id), wave)
            created: list[dict[str, Any]] = []
            for unit in selected:
                dispatch = self._dispatch_record(
                    state,
                    plan,
                    wave_id,
                    unit,
                    route_cursor=route_cursors[unit["id"]],
                    reused_from=(
                        state["dispatches"][reuse_sources[unit["id"]]]
                        if reuse_sources[unit["id"]] is not None
                        else None
                    ),
                )
                state["dispatches"][dispatch["dispatch_id"]] = dispatch
                for member in unit["members"]:
                    state["logical"][member]["dispatch_id"] = dispatch["dispatch_id"]
                    state["logical"][member]["state"] = "starting"
                created.append(dispatch)
            state["active_wave_id"] = wave_id
            self._write_state(state)
            result = self._public_batch(state, created)
            if blocked:
                result["blocked"] = blocked
            return result

    def _find_dispatch(self, state: Mapping[str, Any], dispatch_id: str) -> dict[str, Any]:
        dispatch = state.get("dispatches", {}).get(dispatch_id)
        if not isinstance(dispatch, dict):
            raise ControlPlaneError("dispatch is unknown or expired")
        return dispatch

    @staticmethod
    def _validate_dispatch_wave(dispatch: Mapping[str, Any], wave: Mapping[str, Any]) -> None:
        matches = [
            unit
            for unit in wave["units"]
            if isinstance(unit, Mapping) and unit.get("id") == dispatch.get("unit_id")
        ]
        if len(matches) != 1:
            raise ControlPlaneError("dispatch has no unique immutable wave unit")
        unit = matches[0]
        for field, dispatch_field in (
            ("assurance", "assurance"),
            ("context_turns", "context_turns"),
            ("generation", "generation"),
            ("members", "members"),
            ("reused_from", "reused_from"),
            ("role", "role"),
            ("route_candidates", "route_candidates"),
            ("scopes", "scopes"),
        ):
            if unit.get(field) != dispatch.get(dispatch_field):
                raise ControlPlaneError(f"dispatch {dispatch_field} does not match its wave")
        cursor = dispatch.get("route_cursor")
        candidates = unit.get("route_candidates")
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not isinstance(candidates, list)
            or not 0 <= cursor < len(candidates)
        ):
            raise ControlPlaneError("dispatch route cursor is invalid")
        selected = candidates[cursor]
        native = dispatch.get("native")
        if dispatch.get("tool_kind") == "spawn" and (
            not isinstance(native, Mapping)
            or native.get("model") != selected.get("model")
            or native.get("reasoning_effort") != selected.get("effort")
        ):
            raise ControlPlaneError("dispatch native route does not match its wave")
        if dispatch.get("tool_kind") == "reuse" and (
            not isinstance(native, Mapping)
            or native.get("target") != dispatch.get("owner")
            or native.get("message") is None
            or not isinstance(dispatch.get("reused_from"), str)
        ):
            raise ControlPlaneError("dispatch reuse input does not match its wave")

    def _verify_native_admission(
        self,
        dispatch_id: str,
        tool_use_id: str,
        claim: Callable[
            [dict[str, Any]],
            tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]],
        ],
        *,
        recapture_stale_native: bool,
    ) -> None:
        """Run the shared two-phase admission around lock-free workspace verification."""

        with self._coordinated():
            state = self._read_state()
            if self._reconcile_expired_claims(state):
                self._write_state(state)
            dispatch, wave, allowed = claim(state)
            workspace_root = Path(dispatch["workspace_root"])
            self._assert_cross_task_compatible(
                workspace_root,
                role=dispatch["role"],
                scopes=dispatch["scopes"],
                current_dispatch=dispatch["dispatch_id"],
            )
            baseline = deepcopy(wave["baseline"])
            owner_scopes = deepcopy(dispatch["scopes"])
            checkpoint()
            self._begin_native_claim(state, dispatch, tool_use_id)
            self._write_state(state)
            claim_revision = state["revision"]
        try:
            with deadline_after(_preflight_verification_budget()):
                verify_workspace(
                    workspace_root,
                    baseline,
                    allowed_scopes=allowed,
                    owner_scopes=owner_scopes,
                    pre_spawn=True,
                )
        except OperationDeadlineExceeded as error:
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise ControlPlaneUnavailable(str(error)) from error
        except WorkspaceGuardUnavailable as error:
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise ControlPlaneUnavailable(str(error)) from error
        except WorkspaceGuardError as error:
            if recapture_stale_native:
                recaptured = self._discard_stale_unstarted_wave(dispatch_id, tool_use_id)
                action = (
                    "call next again"
                    if recaptured
                    else "inspect and retry the fenced node"
                )
                raise ControlPlaneError(
                    f"{error}; the stale native admission was settled; {action}"
                ) from error
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise ControlPlaneError(str(error)) from error
        try:
            with self._coordinated():
                state = self._read_state()
                if state["revision"] != claim_revision:
                    raise ControlPlaneError(
                        "lifecycle changed while verifying the native admission"
                    )
                dispatch, _wave, _allowed = claim(state)
                if dispatch.get("tool_use_id") != tool_use_id:
                    raise ControlPlaneError("native admission claim is stale")
                self._assert_cross_task_compatible(
                    workspace_root,
                    role=dispatch["role"],
                    scopes=dispatch["scopes"],
                    current_dispatch=dispatch["dispatch_id"],
                )
                dispatch["claim_expires_at"] = (
                    _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
                )
                checkpoint()
                self._write_state(state)
        except Exception:
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise

    def preflight_spawn(self, payload: Mapping[str, Any]) -> None:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("spawn input is missing")
        task = parse_task_message(tool_input.get("message"))
        dispatch_id = task["dispatch_id"]
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise ControlPlaneError("spawn has no native tool-use identity")

        def claim(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
            dispatch = self._find_dispatch(state, dispatch_id)
            if dispatch["state"] != "starting" or dispatch["tool_kind"] != "spawn":
                raise ControlPlaneError("dispatch is not ready to spawn")
            expected = dispatch["native"]
            for key in (
                "agent_type",
                "fork_turns",
                "message",
                "model",
                "reasoning_effort",
                "task_name",
            ):
                if tool_input.get(key) != expected[key]:
                    raise ControlPlaneError(
                        f"spawn {key} does not match the prepared wave"
                    )
            if (
                _native_claim_active(dispatch)
                and dispatch["tool_use_id"] is not None
                and dispatch["tool_use_id"] != tool_use_id
            ):
                raise ControlPlaneError("dispatch already has an in-flight spawn")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if dispatch["role"] == "worker" and sibling_writer_scopes:
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            return dispatch, wave, sibling_writer_scopes

        self._verify_native_admission(
            dispatch_id,
            tool_use_id,
            claim,
            recapture_stale_native=True,
        )

    def preflight_reuse(self, payload: Mapping[str, Any]) -> None:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("reuse input is missing")
        task = parse_task_message(tool_input.get("message"))
        dispatch_id = task["dispatch_id"]
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise ControlPlaneError("reuse has no native tool-use identity")

        def claim(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
            dispatch = self._find_dispatch(state, dispatch_id)
            if dispatch["state"] != "starting" or dispatch["tool_kind"] != "reuse":
                raise ControlPlaneError("dispatch is not ready to reuse an owner")
            if (
                tool_input.get("target") != dispatch.get("owner")
                or tool_input.get("message") != dispatch["native"].get("message")
            ):
                raise ControlPlaneError("reuse does not match its prepared input")
            if (
                _native_claim_active(dispatch)
                and dispatch["tool_use_id"] is not None
                and dispatch["tool_use_id"] != tool_use_id
            ):
                raise ControlPlaneError("dispatch already has an in-flight reuse")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            plan = self._read_plan(state)
            members = dispatch.get("members")
            if not isinstance(members, list) or len(members) != 1:
                raise ControlPlaneError("reuse requires one logical member")
            node = _node_map(plan)[members[0]]
            dependency_dispatches = {
                state["logical"][dependency].get("dispatch_id")
                for dependency in node["depends_on"]
                if isinstance(state["logical"][dependency].get("dispatch_id"), str)
            }
            source = state["dispatches"].get(dispatch.get("reused_from"))
            route = self._selected_dispatch_route(dispatch)
            if (
                not isinstance(source, Mapping)
                or not isinstance(route, Mapping)
                or not self._source_matches_reuse(
                    source,
                    dependency_dispatches=dependency_dispatches,
                    role=dispatch["role"],
                    assurance=dispatch["assurance"],
                    route=route,
                    scopes=dispatch["scopes"],
                )
                or source.get("owner") != dispatch.get("owner")
                or any(
                    other.get("dispatch_id")
                    not in {dispatch.get("dispatch_id"), source.get("dispatch_id")}
                    and other.get("owner") == dispatch.get("owner")
                    and other.get("state") in {"starting", "running", "paused"}
                    for other in state["dispatches"].values()
                )
            ):
                raise ControlPlaneError("prepared owner reuse is no longer valid")
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if dispatch["role"] == "worker" and sibling_writer_scopes:
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            return dispatch, wave, sibling_writer_scopes

        self._verify_native_admission(
            dispatch_id,
            tool_use_id,
            claim,
            recapture_stale_native=True,
        )

    def preflight_continuation(self, payload: Mapping[str, Any]) -> None:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("continuation input is missing")
        body = parse_continue_message(tool_input.get("message"))
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise ControlPlaneError("continuation has no tool-use identity")

        def claim(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
            dispatch = self._find_dispatch(state, body["dispatch_id"])
            if (
                dispatch["state"] not in {"paused", "starting"}
                or dispatch["tool_kind"] != "continuation"
            ):
                raise ControlPlaneError("continuation is not ready")
            if (
                tool_input.get("target") != dispatch["owner"]
                or tool_input.get("message") != dispatch["native"].get("message")
            ):
                raise ControlPlaneError("continuation does not match its prepared input")
            if body.get("cursor") != dispatch["pending_cursor"]:
                raise ControlPlaneError("continuation cursor is stale")
            if (
                _native_claim_active(dispatch)
                and dispatch["tool_use_id"] is not None
                and dispatch["tool_use_id"] != tool_use_id
            ):
                raise ControlPlaneError("continuation is already in flight")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if dispatch["role"] == "worker" and sibling_writer_scopes:
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            allowed = (
                dispatch["scopes"]
                if dispatch["role"] == "worker"
                else sibling_writer_scopes
            )
            return dispatch, wave, allowed

        self._verify_native_admission(
            body["dispatch_id"],
            tool_use_id,
            claim,
            recapture_stale_native=False,
        )

    def _append_tombstone(self, state: dict[str, Any], dispatch: Mapping[str, Any], reason: str) -> None:
        state["tombstones"].append(
            {
                "cursor": dispatch["cursor"],
                "dispatch_id": dispatch["dispatch_id"],
                "owner": dispatch.get("owner"),
                "reason": reason,
            }
        )
        state["tombstones"] = state["tombstones"][-MAX_TOMBSTONES:]

    def _settle_wave(self, state: dict[str, Any]) -> None:
        wave_id = state.get("active_wave_id")
        if not isinstance(wave_id, str):
            return
        records = [
            item for item in state["dispatches"].values() if item["wave_id"] == wave_id
        ]
        if records and all(
            item["state"] in {"retired", "fenced", "rejected"} for item in records
        ):
            state["active_wave_id"] = None

    def _fence_members(self, state: dict[str, Any], dispatch: Mapping[str, Any], reason: str) -> None:
        dispatch["state"] = "fenced"
        for member in dispatch["members"]:
            state["logical"][member]["state"] = "fenced"
            state["logical"][member]["result"] = {
                "failure_signature": reason,
                "summary": reason.replace("_", " "),
            }
        self._append_tombstone(state, dispatch, reason)

    def _fallback_dispatch(
        self,
        state: dict[str, Any],
        plan: Mapping[str, Any],
        rejected: dict[str, Any],
    ) -> dict[str, Any] | None:
        next_cursor = rejected["route_cursor"] + 1
        if next_cursor >= len(rejected["route_candidates"]):
            return None
        unit = {
            "assurance": rejected["assurance"],
            "context_turns": 0
            if rejected["native"]["fork_turns"] == "none"
            else int(rejected["native"]["fork_turns"]),
            "id": (
                rejected["members"][0]
                if len(rejected["members"]) == 1
                else "aggregate_"
                + hashlib.sha256(",".join(rejected["members"]).encode()).hexdigest()[:16]
            ),
            "members": rejected["members"],
            "role": rejected["role"],
            "route": {"candidates": rejected["route_candidates"]},
            "scopes": rejected["scopes"],
        }
        source_id = rejected.get("reused_from")
        source = state["dispatches"].get(source_id) if isinstance(source_id, str) else None
        if source_id is not None and not isinstance(source, Mapping):
            raise ControlPlaneError("rejected reuse route has no source dispatch")
        fallback = self._dispatch_record(
            state,
            plan,
            rejected["wave_id"],
            unit,
            route_cursor=next_cursor,
            reused_from=source,
        )
        if source is not None:
            route = fallback["route_candidates"][next_cursor]
            message = fallback["native"]["message"]
            fallback["native"] = {
                "agent_type": WRITE_ROLE
                if fallback["role"] == "worker"
                else READ_ROLE,
                "fork_turns": "none"
                if fallback["context_turns"] == 0
                else str(fallback["context_turns"]),
                "message": message,
                "model": route["model"],
                "reasoning_effort": route["effort"],
                "task_name": fallback["task_name"],
            }
            fallback["fallback_from_owner"] = rejected.get("fallback_from_owner")
            fallback["owner"] = None
            fallback["tool_kind"] = "spawn"
        return fallback

    def _reject_route_locked(
        self,
        state: dict[str, Any],
        dispatch: dict[str, Any],
    ) -> dict[str, Any] | None:
        dispatch["tool_use_id"] = None
        dispatch["claim_expires_at"] = None
        if dispatch["tool_kind"] == "continuation":
            dispatch["state"] = "paused"
            for member in dispatch["members"]:
                state["logical"][member]["state"] = "paused"
            return None
        dispatch["state"] = "rejected"
        self._append_tombstone(state, dispatch, "native_route_rejected")
        plan = self._read_plan(state)
        fallback = self._fallback_dispatch(state, plan, dispatch)
        if fallback is None:
            self._fence_members(state, dispatch, "route_exhausted")
        else:
            state["dispatches"][fallback["dispatch_id"]] = fallback
            for member in fallback["members"]:
                state["logical"][member]["dispatch_id"] = fallback["dispatch_id"]
                state["logical"][member]["state"] = "starting"
        self._settle_wave(state)
        return fallback

    def postflight_tool(self, payload: Mapping[str, Any]) -> None:
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            raise ControlPlaneError("native tool result has no call identity")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("native tool result has no input identity")
        message = tool_input.get("message")
        if isinstance(message, str) and message.startswith(TASK_HEADER + "\n"):
            dispatch_id = parse_task_message(message)["dispatch_id"]
        elif isinstance(message, str) and message.startswith(CONTINUE_HEADER + "\n"):
            dispatch_id = parse_continue_message(message)["dispatch_id"]
        else:
            raise ControlPlaneError("native tool result is not CCO-owned")
        with self._coordinated():
            state = self._read_state()
            dispatch = self._find_dispatch(state, dispatch_id)
            if dispatch["state"] == "retired":
                return
            if dispatch["state"] in {"fenced", "rejected"}:
                return
            expected = dispatch.get("native")
            if not isinstance(expected, Mapping) or any(
                tool_input.get(key) != expected.get(key) for key in expected
            ):
                raise ControlPlaneError("native tool result input is stale")
            if isinstance(dispatch.get("tool_use_id"), str) and dispatch[
                "tool_use_id"
            ] != tool_use_id:
                raise ControlPlaneError("native tool result call identity is stale")
            response = payload.get("tool_response")
            if _native_response_failed(response):
                raise ControlPlaneError(
                    "failure-side PostToolUse is not a settlement event; use native-failure"
                )
            self._assert_cross_task_compatible(
                dispatch["workspace_root"],
                role=dispatch["role"],
                scopes=dispatch["scopes"],
                current_dispatch=dispatch["dispatch_id"],
            )
            if dispatch["state"] == "starting" and dispatch["tool_kind"] == "spawn":
                owners = _task_paths(response)
                if len(owners) > 1:
                    self._fence_members(state, dispatch, "native_owner_ambiguous")
                    self._settle_wave(state)
                    self._write_state(state)
                    return
                if owners:
                    owner = owners.pop()
                    if not _owner_matches_task(owner, dispatch["task_name"]):
                        self._fence_members(state, dispatch, "native_owner_mismatch")
                        self._settle_wave(state)
                        self._write_state(state)
                        return
                    dispatch["owner"] = owner
            elif dispatch["pending_cursor"] is not None:
                dispatch["cursor"] = dispatch["pending_cursor"]
                dispatch["pending_cursor"] = None
            dispatch["state"] = "running"
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = None
            for member in dispatch["members"]:
                state["logical"][member]["state"] = "running"
            self._write_state(state)
            return

    def prepare_continuation(self, dispatch_id: str, evidence_delta: object) -> dict[str, Any]:
        if not isinstance(evidence_delta, Mapping) or not evidence_delta:
            raise ControlPlaneError("continuation requires a non-empty evidence object")
        with self._coordinated():
            state = self._read_state()
            dispatch = self._find_dispatch(state, dispatch_id)
            if dispatch["state"] != "paused" or not isinstance(dispatch.get("owner"), str):
                raise ControlPlaneError("dispatch is not continuable")
            if dispatch.get("pending_cursor") is not None:
                raise ControlPlaneError("dispatch already has a prepared continuation")
            self._assert_cross_task_compatible(
                dispatch["workspace_root"],
                role=dispatch["role"],
                scopes=dispatch["scopes"],
                current_dispatch=dispatch["dispatch_id"],
            )
            cursor = dispatch["cursor"] + 1
            try:
                message = _render_continue(dispatch, evidence_delta, cursor)
            except ProtocolHashError as error:
                raise ControlPlaneError(str(error)) from error
            dispatch["native"] = {"message": message, "target": dispatch["owner"]}
            dispatch["pending_cursor"] = cursor
            dispatch["tool_kind"] = "continuation"
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = None
            self._write_state(state)
            return _tool_action(
                "continue_same_owner",
                "followup_task",
                dispatch["native"],
            )

    def owner_is_managed(self, owner: str) -> bool:
        if not self.state_path.exists():
            return False
        with self._coordinated():
            state = self._read_state()
            return any(item.get("owner") == owner for item in state["dispatches"].values())

    def preflight_interrupt(self, payload: Mapping[str, Any]) -> bool:
        tool_input = payload.get("tool_input")
        tool_use_id = payload.get("tool_use_id")
        if (
            not isinstance(tool_input, Mapping)
            or not isinstance(tool_input.get("target"), str)
            or not isinstance(tool_use_id, str)
            or not tool_use_id
        ):
            raise ControlPlaneError("interrupt input is incomplete")
        owner = tool_input["target"]
        if not self.state_path.exists():
            return False
        with self._coordinated():
            state = self._read_state()
            matches = [
                item
                for item in state["dispatches"].values()
                if item.get("owner") == owner and item["state"] in {"running", "paused"}
            ]
            managed = any(
                item.get("owner") == owner for item in state["dispatches"].values()
            )
            if not matches and not managed:
                return False
            if len(matches) != 1:
                raise ControlPlaneError("interrupt target has no unique active dispatch")
            matches[0]["interrupt_tool_use_id"] = tool_use_id
            self._write_state(state)
            return True

    def postflight_interrupt(self, payload: Mapping[str, Any]) -> bool:
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            raise ControlPlaneError("interrupt result has no call identity")
        tool_input = payload.get("tool_input")
        owner = tool_input.get("target") if isinstance(tool_input, Mapping) else None
        if not isinstance(owner, str):
            raise ControlPlaneError("interrupt result has no target identity")
        response = payload.get("tool_response")
        previous_status = (
            response.get("previous_status") if isinstance(response, Mapping) else None
        )
        if previous_status is None:
            raise ControlPlaneError("interrupt result has no previous native status")
        if not self.state_path.exists():
            return False
        with self._coordinated():
            state = self._read_state()
            matches = [
                item
                for item in state["dispatches"].values()
                if item.get("owner") == owner
                and item.get("interrupt_tool_use_id") == tool_use_id
            ]
            if not matches:
                return any(
                    item.get("owner") == owner
                    for item in state["dispatches"].values()
                )
            if len(matches) != 1:
                raise ControlPlaneError("interrupt result has no unique prepared dispatch")
            dispatch = matches[0]
            dispatch.pop("interrupt_tool_use_id", None)
            if (
                isinstance(previous_status, str)
                and previous_status in {"interrupted", "pending_init", "running"}
                and dispatch["state"] in {"running", "paused"}
            ):
                self._fence_members(state, dispatch, "interrupted")
                self._settle_wave(state)
            self._write_state(state)
            return True

    def record_result(self, owner: str, raw_result: object) -> dict[str, Any]:
        result = parse_result(raw_result)

        def claim(
            state: dict[str, Any],
        ) -> tuple[
            dict[str, Any],
            dict[str, Any],
            dict[str, dict[str, Any]],
            dict[str, Any],
            list[dict[str, str]],
        ]:
            dispatch = self._find_dispatch(state, result["dispatch_id"])
            if dispatch["state"] not in {"running", "starting"}:
                raise ControlPlaneError("result owner is stale or fenced")
            if dispatch["state"] == "starting" and dispatch.get("tool_kind") not in {
                "spawn",
                "continuation",
                "reuse",
            }:
                raise ControlPlaneError("result owner is stale or fenced")
            if dispatch.get("owner") is None:
                if not _owner_matches_task(owner, dispatch.get("task_name")):
                    raise ControlPlaneError("result owner does not match the prepared task")
                dispatch["owner"] = owner
            elif dispatch.get("owner") != owner:
                raise ControlPlaneError("result owner is stale or fenced")
            expected_cursor = (
                dispatch.get("pending_cursor")
                if dispatch["state"] == "starting"
                and dispatch.get("tool_kind") == "continuation"
                else dispatch["cursor"]
            )
            if result["cursor"] != expected_cursor:
                raise ControlPlaneError("result cursor is stale")
            if result["outcome"] == "accept" and dispatch["role"] != "reviewer":
                raise ControlPlaneError("only a reviewer may claim acceptance")
            if result["outcome"] == "pause" and result["status"] == "complete":
                raise ControlPlaneError("a complete result cannot pause")
            plan = self._read_plan(state)
            nodes = _node_map(plan)
            acceptance_ids = sorted(
                {
                    acceptance
                    for member in dispatch["members"]
                    for acceptance in nodes[member]["acceptance"]
                }
            )
            if not set(result["evidence"]) <= set(acceptance_ids):
                raise ControlPlaneError(
                    "result evidence contains an unknown acceptance ID"
                )
            if (
                result["status"] == "complete"
                and sorted(result["evidence"]) != acceptance_ids
            ):
                raise ControlPlaneError(
                    "complete result does not cover every acceptance ID"
                )
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if dispatch["role"] == "worker" and sibling_writer_scopes:
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            allowed = (
                dispatch["scopes"]
                if dispatch["role"] == "worker"
                else sibling_writer_scopes
            )
            return dispatch, plan, nodes, wave, allowed

        with self._coordinated():
            state = self._read_state()
            owner_was_pending = self._find_dispatch(
                state, result["dispatch_id"]
            ).get("owner") is None
            dispatch, _plan, _nodes, wave, allowed = claim(state)
            if owner_was_pending:
                self._write_state(state)
            workspace_root = Path(dispatch["workspace_root"])
            baseline = deepcopy(wave["baseline"])
            owner_scopes = deepcopy(dispatch["scopes"])
            role = dispatch["role"]
        try:
            verification = verify_workspace(
                workspace_root,
                baseline,
                allowed_scopes=allowed,
                owner_scopes=owner_scopes,
            )
        except (OperationDeadlineExceeded, WorkspaceGuardUnavailable) as error:
            raise ControlPlaneUnavailable(str(error)) from error
        except WorkspaceGuardError as error:
            raise ControlPlaneError(str(error)) from error
        actual = verification["owner_changed_paths"]
        if actual != result["changed_paths"]:
            raise ControlPlaneError(
                "declared changed paths do not match the verified owner delta"
            )
        if role != "worker" and actual:
            raise ControlPlaneError("read-only child changed its declared scope")

        with self._coordinated():
            state = self._read_state()
            dispatch, plan, nodes, _wave, _allowed = claim(state)
            if dispatch.get("pending_cursor") is not None:
                dispatch["cursor"] = dispatch["pending_cursor"]
                dispatch["pending_cursor"] = None
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = None
            dispatch["result"] = result
            if result["outcome"] == "pause":
                dispatch["state"] = "paused"
                for member in dispatch["members"]:
                    state["logical"][member]["state"] = "paused"
                    state["logical"][member]["result"] = {
                        "evidence": {
                            key: result["evidence"][key]
                            for key in nodes[member]["acceptance"]
                            if key in result["evidence"]
                        },
                        "failure_signature": result["failure_signature"],
                        "summary": result["summary"],
                    }
            else:
                dispatch["state"] = "retired"
                self._append_tombstone(state, dispatch, "retired")
                for member in dispatch["members"]:
                    state["logical"][member]["state"] = "retired"
                    state["logical"][member]["result"] = {
                        "changed_paths": [
                            path
                            for path in actual
                            if any(
                                repository_scopes_overlap(
                                    {"kind": "exact", "path": path},
                                    scope,
                                )
                                for scope in nodes[member]["scopes"]
                            )
                        ],
                        "evidence": {key: result["evidence"][key] for key in nodes[member]["acceptance"]},
                        "outcome": result["outcome"],
                        "summary": result["summary"],
                    }
                self._refresh_ready(state, plan)
                self._settle_wave(state)
            self._write_state(state)
            return {
                "dispatch_id": dispatch["dispatch_id"],
                "members": dispatch["members"],
                "state": dispatch["state"],
                "verification": verification,
            }

    def _settle_native_failure_locked(
        self,
        state: dict[str, Any],
        dispatch: dict[str, Any],
        kind: str,
    ) -> dict[str, Any]:
        if kind == "owner_unavailable":
            if (
                dispatch.get("state") != "starting"
                or dispatch.get("tool_kind") != "reuse"
                or not isinstance(dispatch.get("owner"), str)
                or dispatch.get("fallback_from_owner") is not None
            ):
                raise ControlPlaneError(
                    "only one prepared owner reuse can fall back to a fresh spawn"
                )
            route = self._selected_dispatch_route(dispatch)
            native = dispatch.get("native")
            if not isinstance(route, Mapping) or not isinstance(native, Mapping):
                raise ControlPlaneError("owner reuse fallback contract is invalid")
            previous_owner = dispatch["owner"]
            dispatch["native"] = {
                "agent_type": WRITE_ROLE
                if dispatch["role"] == "worker"
                else READ_ROLE,
                "fork_turns": "none"
                if dispatch["context_turns"] == 0
                else str(dispatch["context_turns"]),
                "message": native["message"],
                "model": route["model"],
                "reasoning_effort": route["effort"],
                "task_name": dispatch["task_name"],
            }
            dispatch["fallback_from_owner"] = previous_owner
            dispatch["owner"] = None
            dispatch["tool_kind"] = "spawn"
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = (
                _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
            )
            return _tool_action(
                "spawn_new_owner",
                "spawn_agent",
                dispatch["native"],
            )
        if kind == "route_rejected":
            fallback = self._reject_route_locked(state, dispatch)
            if fallback is None:
                return _tool_action("fenced", None, None)
            return _tool_action("fallback_route", "spawn_agent", fallback["native"])
        if kind == "other":
            self._fence_members(state, dispatch, "native_call_failed")
            self._settle_wave(state)
            return _tool_action("fenced", None, None)
        attempts = dispatch.get("transient_retries", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ControlPlaneError("transient retry counter is invalid")
        if attempts >= MAX_TRANSIENT_RETRIES:
            self._fence_members(state, dispatch, "transient_retry_exhausted")
            self._settle_wave(state)
            return _tool_action("fenced", None, None)
        attempts += 1
        dispatch["transient_retries"] = attempts
        dispatch["last_transient_failure"] = f"native_{kind}"
        dispatch["tool_use_id"] = None
        dispatch["claim_expires_at"] = None
        if dispatch["state"] == "starting" and dispatch["tool_kind"] in {
            "spawn",
            "reuse",
        }:
            dispatch["claim_expires_at"] = (
                _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
            )
            if dispatch["tool_kind"] == "reuse":
                return _tool_action("reuse_owner", "followup_task", dispatch["native"])
            return _tool_action("retry_same_call", "spawn_agent", dispatch["native"])
        if not isinstance(dispatch.get("owner"), str):
            raise ControlPlaneError("native failure dispatch has no continuation owner")
        if dispatch.get("pending_cursor") is None:
            cursor = dispatch["cursor"] + 1
            dispatch["native"] = {
                "message": _render_continue(
                    dispatch,
                    {"native_failure": kind, "retry": attempts},
                    cursor,
                ),
                "target": dispatch["owner"],
            }
            dispatch["pending_cursor"] = cursor
        dispatch["state"] = "paused"
        dispatch["tool_kind"] = "continuation"
        for member in dispatch["members"]:
            state["logical"][member]["state"] = "paused"
        return _tool_action(
            "continue_same_owner",
            "followup_task",
            dispatch["native"],
        )

    def settle_native_failure(self, dispatch_id: str, kind: str) -> dict[str, Any]:
        """Settle one Primary-observed typed native failure without parsing prose."""

        if kind not in NATIVE_FAILURE_KINDS:
            raise ControlPlaneError("native failure kind is invalid")
        with self._coordinated():
            state = self._read_state()
            dispatch = self._find_dispatch(state, dispatch_id)
            if dispatch["state"] == "paused":
                raise ControlPlaneError("native failure has no unsettled native call")
            if dispatch["state"] not in {"starting", "running"}:
                raise ControlPlaneError("native failure dispatch is not active")
            if dispatch["state"] == "starting" and not isinstance(
                dispatch.get("tool_use_id"), str
            ):
                raise ControlPlaneError("native failure has no unsettled native call")
            if kind == "route_rejected" and (
                dispatch["state"] != "starting" or dispatch["tool_kind"] != "spawn"
            ):
                raise ControlPlaneError("only a prepared spawn route can be rejected")
            if kind == "owner_unavailable" and (
                dispatch["state"] != "starting" or dispatch["tool_kind"] != "reuse"
            ):
                raise ControlPlaneError("only a prepared owner reuse can be unavailable")
            result = self._settle_native_failure_locked(state, dispatch, kind)
            self._write_state(state)
            return result

    def fence_invalid_result(self, owner: str, reason: str = "invalid_result") -> None:
        with self._coordinated():
            state = self._read_state()
            matches = [
                item
                for item in state["dispatches"].values()
                if item["state"] in {"starting", "running", "paused"}
                and (
                    item.get("owner") == owner
                    or (
                        item.get("owner") is None
                        and _owner_matches_task(owner, item.get("task_name"))
                    )
                )
            ]
            if len(matches) == 1:
                if matches[0].get("owner") is None:
                    matches[0]["owner"] = owner
                self._fence_members(state, matches[0], reason)
                self._settle_wave(state)
                self._write_state(state)

    def restart(self) -> int:
        if not self.state_path.exists():
            return 0
        with self._coordinated():
            state = self._read_state()
            count = 0
            for dispatch in state["dispatches"].values():
                if dispatch["state"] in ACTIVE_STATES or _native_claim_active(dispatch):
                    self._fence_members(state, dispatch, "host_restart")
                    count += 1
            self._settle_wave(state)
            state["epoch"] += 1
            self._write_state(state)
            return count

    def abandon(self, node_id: str) -> None:
        with self._coordinated():
            state = self._read_state()
            logical = state["logical"].get(node_id)
            if not isinstance(logical, dict) or logical["state"] != "paused":
                raise ControlPlaneError("only a paused node can be abandoned")
            dispatch = self._find_dispatch(state, logical["dispatch_id"])
            self._fence_members(state, dispatch, "abandoned")
            self._settle_wave(state)
            self._write_state(state)

    def retry(self, node_id: str) -> None:
        with self._coordinated():
            state = self._read_state()
            plan = self._read_plan(state)
            logical = state["logical"].get(node_id)
            if not isinstance(logical, dict) or logical["state"] != "fenced":
                raise ControlPlaneError("only a fenced node can start a newer generation")
            logical["generation"] += 1
            logical["assurance"] = "guarded"
            logical["dispatch_id"] = None
            logical["result"] = None
            nodes = _node_map(plan)
            logical["state"] = (
                "ready"
                if all(
                    self._logical_satisfied(state, plan, dependency)
                    for dependency in nodes[node_id]["depends_on"]
                )
                else "waiting"
            )
            self._write_state(state)

    def status(self) -> dict[str, Any]:
        with self._coordinated():
            state = self._read_state()
            if self._reconcile_expired_claims(state):
                self._write_state(state)
            plan = self._read_plan(state)
            counts = {name: 0 for name in sorted(LOGICAL_STATES)}
            for item in state["logical"].values():
                counts[item["state"]] += 1
            attention = []
            for node_id, logical in sorted(state["logical"].items()):
                if logical["state"] not in {"paused", "fenced"}:
                    continue
                dispatch_id = logical.get("dispatch_id")
                dispatch = state["dispatches"].get(dispatch_id, {})
                attention.append(
                    {
                        "dispatch_id": dispatch_id,
                        "nodes": [node_id],
                        "owner": dispatch.get("owner"),
                        "state": logical["state"],
                    }
                )
            for dispatch in sorted(
                state["dispatches"].values(),
                key=lambda item: item["dispatch_id"],
            ):
                if _native_settlement_overdue(dispatch):
                    attention.append(
                        {
                            "dispatch_id": dispatch["dispatch_id"],
                            "nodes": list(dispatch["members"]),
                            "owner": dispatch.get("owner"),
                            "reason": "native_settlement_required",
                            "state": "starting",
                            "task_name": dispatch["task_name"],
                        }
                    )
                if dispatch["state"] == "running" and dispatch.get("owner") is None:
                    attention.append(
                        {
                            "dispatch_id": dispatch["dispatch_id"],
                            "nodes": list(dispatch["members"]),
                            "owner": None,
                            "reason": "awaiting_native_owner",
                            "state": "running",
                            "task_name": dispatch["task_name"],
                        }
                    )
            return {
                "attention": attention,
                "counts": counts,
                "epoch": state["epoch"],
                "plan_id": state["plan_id"],
                "protocol": "cco.status.v1",
                "state": self._overall_state(state, plan),
            }

    def cleanup(self) -> int:
        """Remove only this task's inactive v9 state and immutable artifacts."""

        removed = 0
        coordination = (
            self._coordinated()
            if self.state_path.exists()
            else acquire(self.root, self.session_id, timeout=self.lock_timeout)
        )
        with coordination:
            if self.state_path.exists():
                state = self._read_state()
                if self._reconcile_expired_claims(state):
                    self._write_state(state)
                if any(
                    item["state"] in ACTIVE_STATES or _native_claim_active(item)
                    for item in state["dispatches"].values()
                ):
                    raise ControlPlaneError(
                        "active or paused child work must settle or be abandoned before cleanup"
                    )
                self.state_path.unlink()
                removed += 1
            artifacts = self.root / "artifacts"
            if artifacts.is_dir():
                for kind in ("plan", "wave"):
                    for path in artifacts.glob(f"{self.session_id}-{kind}-*.json"):
                        try:
                            path.unlink()
                            removed += 1
                        except FileNotFoundError:
                            pass
        return removed

    def terminal_proof(self, owner: str, result: Mapping[str, Any]) -> dict[str, Any]:
        """Return a proof-backed retired dispatch for explicit host maintenance."""

        with self._coordinated():
            state = self._read_state()
            dispatch = self._find_dispatch(state, str(result.get("dispatch_id")))
            if (
                dispatch.get("owner") != owner
                or dispatch.get("state") != "retired"
                or dispatch.get("result") != dict(result)
                or result.get("status") != "complete"
                or result.get("outcome") not in {"retire", "accept"}
                or result.get("blockers")
                or result.get("deviations")
            ):
                raise ControlPlaneError("CCO child is not proof-backed terminal work")
            return {
                "dispatch_id": dispatch["dispatch_id"],
                "role": dispatch["role"],
                "state": dispatch["state"],
            }

    def stop_reason(self) -> str | None:
        if not self.state_path.exists():
            return None
        with self._coordinated():
            state = self._read_state()
            if self._reconcile_expired_claims(state):
                self._write_state(state)
            if any(
                item["state"] in ACTIVE_STATES or _native_claim_active(item)
                for item in state["dispatches"].values()
            ):
                return "CCO child work is still active; wait for its native terminal event."
            return None


def _session_arg() -> str:
    session = os.environ.get("CODEX_THREAD_ID")
    if not session:
        raise ControlPlaneError("CODEX_THREAD_ID is unavailable")
    return session


def _stdin_json() -> Any:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ControlPlaneError("input exceeds 1 MiB")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
            parse_int=parse_safe_integer,
        )
    except (ControlPlaneError, ProtocolHashError) as error:
        raise ControlPlaneError(str(error)) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlPlaneError("input is not valid UTF-8 JSON") from error


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Compile and operate one compact cco.v9 plan.")
    sub = root.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--repo", type=Path, default=Path.cwd())
    prepare.add_argument("--capacity", type=int, default=1)
    prepare.add_argument("--catalog", type=Path)
    next_parser = sub.add_parser("next")
    next_parser.add_argument("--capacity", type=int, required=True)
    next_parser.add_argument("--catalog", type=Path)
    continuation = sub.add_parser("continue")
    continuation.add_argument("--dispatch", required=True)
    abandon = sub.add_parser("abandon")
    abandon.add_argument("--node", required=True)
    retry = sub.add_parser("retry")
    retry.add_argument("--node", required=True)
    native_failure = sub.add_parser("native-failure")
    native_failure.add_argument("--dispatch", required=True)
    native_failure.add_argument("--kind", choices=sorted(NATIVE_FAILURE_KINDS), required=True)
    sub.add_parser("status")
    sub.add_parser("restart")
    sub.add_parser("cleanup")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        control = ControlPlane(_session_arg())
        if args.command == "prepare":
            if args.capacity < 1:
                raise ControlPlaneError("native capacity must be a positive integer")
            brief = _prepare_brief(_stdin_json())
            catalog = (
                _load_object(args.catalog, "native catalogue")
                if args.catalog
                else load_native_catalog()
            )
            control.create_plan(args.repo, brief, resume_identical=True)
            result = control.next_wave(
                capacity=args.capacity,
                native_catalog=catalog,
            )
        elif args.command == "next":
            catalog = _load_object(args.catalog, "native catalogue") if args.catalog else None
            result = control.next_wave(capacity=args.capacity, native_catalog=catalog)
        elif args.command == "continue":
            result = control.prepare_continuation(args.dispatch, _stdin_json())
        elif args.command == "abandon":
            control.abandon(args.node)
            result = control.status()
        elif args.command == "retry":
            control.retry(args.node)
            result = control.status()
        elif args.command == "native-failure":
            result = control.settle_native_failure(args.dispatch, args.kind)
        elif args.command == "restart":
            result = {"interrupted": control.restart(), "protocol": "cco.restart.v1"}
        elif args.command == "cleanup":
            result = {"protocol": "cco.cleanup.v1", "removed": control.cleanup()}
        else:
            result = control.status()
    except (
        ControlPlaneError,
        OSError,
        RoutingCatalogError,
        StateLockBusy,
        WorkspaceGuardError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(canonical_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
