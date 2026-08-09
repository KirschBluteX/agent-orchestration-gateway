#!/usr/bin/env python3
"""Compact cco.v9 plan, wave, routing, and lifecycle control plane."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
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
WAVE_PROTOCOL = "cco.wave.v1"
BATCH_PROTOCOL = "cco.wave-batch.v1"
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
    {"network", "other", "rate_limit", "route_rejected", "service", "timeout"}
)
EFFORT_LABELS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
STATE_FILE_RE = re.compile(r"^(?P<workspace>[0-9a-f]{64})--(?P<session>[0-9a-f]{64})\.json$")
STATE_ROOT_SENTINEL = ".cco-state-root-v1"
STATE_ROOT_SENTINEL_BYTES = b"cco.state-root.v1\n"
MAX_STATE_FILE_BYTES = 32 * 1024 * 1024
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


def _preflight_verification_budget() -> float:
    remaining = remaining_seconds(reserve=PREFLIGHT_ROLLBACK_RESERVE_SECONDS)
    return (
        PREFLIGHT_VERIFICATION_SECONDS
        if remaining is None
        else min(PREFLIGHT_VERIFICATION_SECONDS, remaining)
    )


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


def _same_file_version(left: os.stat_result, right: os.stat_result) -> bool:
    return os.path.samestat(left, right) and (
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


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
        try:
            json_files = list(self.root.glob("*.json"))
        except OSError as error:
            raise ControlPlaneUnavailable("lifecycle state directory is unavailable") from error
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

        if self._state_path is not None:
            return self._state_path
        suffix = f"--{_session_digest(self.session_id)}.json"
        try:
            snapshot = sorted(self.root.glob("*.json"), key=lambda item: item.name)
        except OSError as error:
            raise ControlPlaneUnavailable("lifecycle state directory is unavailable") from error
        legacy = self.root / f"{self.session_id}.json"
        matches = [path for path in snapshot if path.name.endswith(suffix)]
        if len(matches) > 1:
            raise ControlPlaneError("current task has multiple lifecycle state files")
        self._state_path = (
            matches[0]
            if matches
            else next((path for path in snapshot if path.name == legacy.name), legacy)
        )
        return self._state_path

    @staticmethod
    def _validate_lifecycle_state(
        state: Mapping[str, Any],
        *,
        expected_session: str | None = None,
    ) -> dict[str, Any]:
        legacy = any(
            isinstance(dispatch, Mapping) and dispatch.get("state") == "interrupting"
            for dispatch in (state.get("dispatches") or {}).values()
        ) if isinstance(state.get("dispatches"), Mapping) else False
        logical_value = state.get("logical")
        legacy = legacy or (
            isinstance(logical_value, Mapping)
            and any(
                isinstance(item, Mapping) and item.get("state") == "interrupting"
                for item in logical_value.values()
            )
        )
        normalized = deepcopy(dict(state)) if legacy else dict(state)
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
        dispatches = normalized.get("dispatches")
        if not isinstance(dispatches, Mapping):
            raise ControlPlaneError("lifecycle dispatch collection is invalid")
        for dispatch_id, dispatch in dispatches.items():
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

    def _discard_stale_spawn_wave(self, dispatch_id: str, tool_use_id: str) -> bool:
        """Discard a baseline that never reached a native child and can be recaptured."""

        with self._coordinated():
            state = self._read_state()
            dispatch = self._find_dispatch(state, dispatch_id)
            if (
                dispatch.get("state") != "starting"
                or dispatch.get("tool_kind") != "spawn"
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
                item.get("owner") is None
                and (
                    item.get("state") == "rejected"
                    or (
                        item.get("state") == "starting"
                        and (
                            item.get("tool_use_id") is None
                            or item.get("dispatch_id") == dispatch_id
                        )
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

    def _quarantine_legacy_state(self, path: Path) -> None:
        """Isolate invalid legacy state only inside a marked CCO-owned root."""

        if not self._state_root_is_marked():
            return
        try:
            before = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ControlPlaneUnavailable("legacy lifecycle state is unavailable") from error
        raw = _read_bounded_bytes(path, "legacy lifecycle state")
        try:
            after_read = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ControlPlaneUnavailable("legacy lifecycle state is unavailable") from error
        if not _same_file_version(before, after_read):
            return
        try:
            self._validate_lifecycle_state(
                _decode_object(raw, "legacy cco.v9 lifecycle state")
            )
        except ControlPlaneError:
            pass
        else:
            return
        quarantine = self.root / "quarantine"
        quarantine.mkdir(parents=True, exist_ok=True)
        name_identity = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]
        content_identity = hashlib.sha256(raw).hexdigest()
        destination = quarantine / f"legacy-{name_identity}-{content_identity}.json"
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            if _read_bounded_bytes(destination, "quarantined lifecycle state") != raw:
                raise ControlPlaneError("legacy quarantine identity collision")
        except FileNotFoundError:
            return
        else:
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        try:
            before_unlink = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ControlPlaneUnavailable("legacy lifecycle state is unavailable") from error
        if not _same_file_version(before, before_unlink):
            return
        current = _read_bounded_bytes(path, "legacy lifecycle state")
        try:
            after_reread = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ControlPlaneUnavailable("legacy lifecycle state is unavailable") from error
        if (
            current != raw
            or not _same_file_version(before_unlink, after_reread)
            or not _same_file_version(before, after_reread)
        ):
            return
        try:
            self._validate_lifecycle_state(
                _decode_object(current, "legacy cco.v9 lifecycle state")
            )
        except ControlPlaneError:
            pass
        else:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _workspace_state_candidates(
        self,
        workspace_root: object,
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Load only indexed same-workspace state plus quarantinable legacy files."""

        workspace_digest = _workspace_digest(workspace_root)
        try:
            snapshot = sorted(self.root.glob("*.json"), key=lambda item: item.name)
        except OSError as error:
            raise ControlPlaneUnavailable(
                "lifecycle state directory is unavailable"
            ) from error
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
                self._quarantine_legacy_state(path)
                continue
            if state_workspace_digest == workspace_digest:
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
        source = self.state_path
        raw_state = _load_object(source, "cco.v9 lifecycle state")
        state = self._validate_lifecycle_state(
            raw_state,
            expected_session=self.session_id,
        )
        canonical = _lifecycle_state_path(
            self.root,
            state["workspace_root"],
            self.session_id,
        )
        if STATE_FILE_RE.fullmatch(source.name) is not None and source != canonical:
            raise ControlPlaneError("indexed lifecycle filename does not match its state")
        legacy = self.root / f"{self.session_id}.json"
        if source == canonical and legacy.exists():
            legacy_raw = _load_object(legacy, "legacy cco.v9 lifecycle state")
            legacy_state = self._validate_lifecycle_state(
                legacy_raw,
                expected_session=self.session_id,
            )
            canonical_revision = state.get("revision")
            legacy_revision = legacy_state.get("revision")
            comparable = deepcopy(legacy_state)
            comparable["revision"] = canonical_revision
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
            self._state_path = canonical
            self._write_state(state)
            try:
                source.unlink()
            except FileNotFoundError:
                pass
        elif state != raw_state:
            self._write_state(state)
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self._mark_state_root_if_safe()
        self._state_path = _lifecycle_state_path(
            self.root,
            state["workspace_root"],
            self.session_id,
        )
        state["revision"] = int(state.get("revision", 0)) + 1
        _atomic_write(self.state_path, state)
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
        if wave.get("protocol") != WAVE_PROTOCOL or wave.get("wave_id") != wave_id:
            raise ControlPlaneError("wave artifact identity is invalid")
        identity = {
            key: wave.get(key)
            for key in ("baseline_id", "plan_id", "protocol", "sequence", "units")
        }
        if (
            wave.get("plan_id") != state.get("plan_id")
            or not isinstance(wave.get("baseline"), Mapping)
            or wave["baseline"].get("state_id") != wave.get("baseline_id")
            or _digest(b"cco.wave.v1\0", identity) != wave_id
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
        with acquire(self.root, self.session_id, timeout=self.lock_timeout):
            if self.state_path.exists():
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
                    "the current task already has CCO lifecycle proof; run explicit cleanup first"
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
            self._write_state(state)
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

    def _dispatch_record(
        self,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        wave_id: str,
        unit: Mapping[str, Any],
        *,
        route_cursor: int,
    ) -> dict[str, Any]:
        route = unit["route"]["candidates"][route_cursor]
        generation = max(state["logical"][item]["generation"] for item in unit["members"])
        identity = {
            "cursor": 0,
            "generation": generation,
            "members": unit["members"],
            "route": route,
            "route_cursor": route_cursor,
            "wave_id": wave_id,
        }
        dispatch_id = _digest(b"cco.dispatch.v1\0", identity)
        task_name = _task_name(unit, route, generation, dispatch_id)
        message = _render_task(
            plan,
            unit,
            dispatch_id,
            cursor=0,
            dependency_evidence=self._dependency_evidence(state, plan, unit["members"]),
        )
        native = {
            "agent_type": WRITE_ROLE if unit["role"] == "worker" else READ_ROLE,
            "fork_turns": "none" if unit["context_turns"] == 0 else str(unit["context_turns"]),
            "message": message,
            "model": route["model"],
            "reasoning_effort": route["effort"],
            "task_name": task_name,
        }
        return {
            "assurance": unit["assurance"],
            "claim_expires_at": (
                _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
            ),
            "cursor": 0,
            "dispatch_id": dispatch_id,
            "generation": generation,
            "members": list(unit["members"]),
            "native": native,
            "owner": None,
            "pending_cursor": None,
            "transient_retries": 0,
            "role": unit["role"],
            "route_candidates": deepcopy(unit["route"]["candidates"]),
            "route_cursor": route_cursor,
            "scopes": deepcopy(unit["scopes"]),
            "state": "starting",
            "task_name": task_name,
            "tool_kind": "spawn",
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
        return {
            "dispatches": [deepcopy(item["native"]) for item in dispatches],
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
            wave_id = _digest(b"cco.wave.v1\0", wave_identity)
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
            ("generation", "generation"),
            ("members", "members"),
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

    def _verify_native_admission(
        self,
        dispatch_id: str,
        tool_use_id: str,
        claim: Callable[
            [dict[str, Any]],
            tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]],
        ],
        *,
        recapture_stale_spawn: bool,
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
            if recapture_stale_spawn:
                recaptured = self._discard_stale_spawn_wave(dispatch_id, tool_use_id)
                action = (
                    "call next again"
                    if recaptured
                    else "inspect and retry the fenced node"
                )
                raise ControlPlaneError(
                    f"{error}; the stale native admission was settled—{action}"
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
            recapture_stale_spawn=True,
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
            recapture_stale_spawn=False,
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
        return self._dispatch_record(
            state,
            plan,
            rejected["wave_id"],
            unit,
            route_cursor=next_cursor,
        )

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
        with acquire(self.root, self.session_id, timeout=self.lock_timeout):
            state = self._read_state()
            return any(item.get("owner") == owner for item in state["dispatches"].values())

    def preflight_interrupt(self, payload: Mapping[str, Any]) -> None:
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
        with self._coordinated():
            state = self._read_state()
            matches = [
                item
                for item in state["dispatches"].values()
                if item.get("owner") == owner and item["state"] in {"running", "paused"}
            ]
            if len(matches) != 1:
                raise ControlPlaneError("interrupt target has no unique active dispatch")

    def postflight_interrupt(self, payload: Mapping[str, Any]) -> None:
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
        with self._coordinated():
            state = self._read_state()
            matches = [
                item
                for item in state["dispatches"].values()
                if item.get("owner") == owner
            ]
            if not matches:
                return
            if len(matches) != 1:
                raise ControlPlaneError("interrupt result has no unique managed dispatch")
            dispatch = matches[0]
            if dispatch["state"] in {"retired", "fenced", "rejected"}:
                return
            if (
                isinstance(previous_status, str)
                and previous_status in {"interrupted", "pending_init", "running"}
                and dispatch["state"] in {"running", "paused"}
            ):
                self._fence_members(state, dispatch, "interrupted")
                self._settle_wave(state)
                self._write_state(state)

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
        if dispatch["state"] == "starting" and dispatch["tool_kind"] == "spawn":
            dispatch["claim_expires_at"] = (
                _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
            )
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

        with acquire(self.root, self.session_id, timeout=self.lock_timeout):
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
