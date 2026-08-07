#!/usr/bin/env python3
"""Compact cco.v9 plan, wave, routing, and lifecycle control plane."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping
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
from routing_catalog import (
    RoutingCatalogError,
    load_native_catalog,
    load_route_policy,
    resolve_route_plan,
)
from state_lock import StateLockBusy, acquire
from workspace_guard import (
    WorkspaceGuardError,
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
    {"waiting", "ready", "starting", "running", "paused", "retired", "fenced"}
)
DISPATCH_STATES = frozenset(
    {"starting", "running", "paused", "retired", "fenced", "rejected"}
)
NODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
ACCEPTANCE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
TASK_PATH_RE = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FAILURE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
MAX_INPUT_BYTES = 1024 * 1024
MAX_AGGREGATE_MEMBERS = 4
MAX_TOMBSTONES = 256
EFFORT_LABELS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})


class ControlPlaneError(RuntimeError):
    """A cco.v9 contract or lifecycle transition is invalid."""


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


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ControlPlaneError(f"{label} is unavailable") from error
    if len(raw) > 32 * 1024 * 1024:
        raise ControlPlaneError(f"{label} is too large")
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
    if not isinstance(value, dict):
        raise ControlPlaneError(f"{label} is malformed")
    return value


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


def _task_name(unit: Mapping[str, Any], route: Mapping[str, str], generation: int) -> str:
    role = unit["role"]
    prefix = {"explorer": "explorer", "worker": "worker", "reviewer": "reviewer"}[role]
    base = re.sub(r"[^a-z0-9_]+", "_", str(unit["id"]).casefold()).strip("_")[:32]
    return f"{prefix}_{base}_{_model_label(route['model'])}_{route['effort']}_g{generation:02d}"


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
                "id": member_id,
                "objective": node["objective"],
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
    evidence = {
        _text(key, "result evidence ID", limit=32): _text(value, "result evidence", limit=8_192)
        for key, value in evidence_value.items()
    }
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


def _native_rejected(value: object) -> bool:
    if isinstance(value, str):
        lowered = value.casefold()
        return any(token in lowered for token in ("unknown model", "unknown agent", "not supported", "unsupported", "rejected"))
    if isinstance(value, Mapping):
        if value.get("isError") is True or value.get("is_error") is True:
            return True
        return any(_native_rejected(item) for item in value.values())
    if isinstance(value, list):
        return any(_native_rejected(item) for item in value)
    return False


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


class ControlPlane:
    """One deep interface for cco.v9 plan, wave, and lifecycle behavior."""

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        if SESSION_RE.fullmatch(session_id) is None:
            raise ControlPlaneError("session identity is invalid")
        self.session_id = session_id
        self.root = Path(os.path.abspath((root or _state_root()).expanduser()))
        self.state_path = self.root / f"{session_id}.json"

    def _artifact_path(self, kind: str, identity: str) -> Path:
        if kind not in {"plan", "wave"} or SHA256_RE.fullmatch(identity) is None:
            raise ControlPlaneError("artifact identity is invalid")
        return self.root / "artifacts" / f"{self.session_id}-{kind}-{identity[7:]}.json"

    def _read_state(self) -> dict[str, Any]:
        state = _load_object(self.state_path, "cco.v9 lifecycle state")
        if state.get("protocol") != LIFECYCLE_PROTOCOL:
            raise ControlPlaneError("lifecycle state is not cco.v9; start a new Codex task")
        if state.get("session_id") != self.session_id:
            raise ControlPlaneError("lifecycle state session does not match")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
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

    def create_plan(self, repo: Path, brief: object) -> dict[str, Any]:
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
        with acquire(self.root, self.session_id):
            if self.state_path.exists():
                current = self._read_state()
                active = any(
                    item["state"] in {"starting", "running", "paused"}
                    for item in current.get("dispatches", {}).values()
                )
                if active:
                    raise ControlPlaneError("the current task already has active CCO work")
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
    def _refresh_ready(state: dict[str, Any], plan: Mapping[str, Any]) -> None:
        nodes = _node_map(plan)
        changed = True
        while changed:
            changed = False
            for node_id, logical in state["logical"].items():
                if logical["state"] != "waiting":
                    continue
                dependencies = [state["logical"][item]["state"] for item in nodes[node_id]["depends_on"]]
                if all(item == "retired" for item in dependencies):
                    logical["state"] = "ready"
                    changed = True

    @staticmethod
    def _overall_state(state: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
        logical = state["logical"]
        states = [item["state"] for item in logical.values()]
        if any(item in {"starting", "running", "paused"} for item in states):
            return "active"
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
        task_name = _task_name(unit, route, generation)
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
            "cursor": 0,
            "dispatch_id": dispatch_id,
            "generation": generation,
            "members": list(unit["members"]),
            "native": native,
            "owner": None,
            "pending_cursor": None,
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
        with acquire(self.root, self.session_id):
            state = self._read_state()
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
                    return self._public_batch(state, sorted(pending, key=lambda item: item["task_name"]))
                active = [
                    item
                    for item in state["dispatches"].values()
                    if item["wave_id"] == state["active_wave_id"]
                    and item["state"] in {"starting", "running", "paused"}
                ]
                if active:
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
            wave_scopes = {
                (scope["kind"], scope["path"]): dict(scope)
                for unit in selected
                for scope in unit["scopes"]
            }
            baseline = capture_workspace(
                Path(plan["workspace_root"]),
                scopes=[wave_scopes[key] for key in sorted(wave_scopes)],
                writable=any(unit["role"] == "worker" for unit in selected),
            )
            state["wave_sequence"] += 1
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
            wave_identity = {
                "baseline_id": baseline["state_id"],
                "plan_id": plan["plan_id"],
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
                    route_cursor=0,
                )
                state["dispatches"][dispatch["dispatch_id"]] = dispatch
                for member in unit["members"]:
                    state["logical"][member]["dispatch_id"] = dispatch["dispatch_id"]
                    state["logical"][member]["state"] = "starting"
                created.append(dispatch)
            state["active_wave_id"] = wave_id
            self._write_state(state)
            result = self._public_batch(state, created)
            if route_errors:
                result["blocked"] = [
                    {"node": node, "reason": route_errors[node]}
                    for node in sorted(route_errors)
                ]
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
            if dispatch["tool_use_id"] not in {None, tool_use_id}:
                raise ControlPlaneError("dispatch already has an in-flight spawn")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if dispatch["role"] == "worker" and sibling_writer_scopes:
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            return dispatch, wave, sibling_writer_scopes

        with acquire(self.root, self.session_id):
            state = self._read_state()
            dispatch, wave, allowed = claim(state)
            workspace_root = Path(dispatch["workspace_root"])
            baseline = deepcopy(wave["baseline"])
            owner_scopes = deepcopy(dispatch["scopes"])
        try:
            verify_workspace(
                workspace_root,
                baseline,
                allowed_scopes=allowed,
                owner_scopes=owner_scopes,
                pre_spawn=True,
            )
        except WorkspaceGuardError as error:
            raise ControlPlaneError(str(error)) from error
        with acquire(self.root, self.session_id):
            state = self._read_state()
            dispatch, _wave, _allowed = claim(state)
            dispatch["tool_use_id"] = tool_use_id
            self._write_state(state)

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
            if dispatch["state"] != "starting" or dispatch["tool_kind"] != "continuation":
                raise ControlPlaneError("continuation is not ready")
            if (
                tool_input.get("target") != dispatch["owner"]
                or tool_input.get("message") != dispatch["native"].get("message")
            ):
                raise ControlPlaneError("continuation does not match its prepared input")
            if body.get("cursor") != dispatch["pending_cursor"]:
                raise ControlPlaneError("continuation cursor is stale")
            if dispatch["tool_use_id"] not in {None, tool_use_id}:
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

        with acquire(self.root, self.session_id):
            state = self._read_state()
            dispatch, wave, allowed = claim(state)
            workspace_root = Path(dispatch["workspace_root"])
            baseline = deepcopy(wave["baseline"])
            owner_scopes = deepcopy(dispatch["scopes"])
        try:
            verify_workspace(
                workspace_root,
                baseline,
                allowed_scopes=allowed,
                owner_scopes=owner_scopes,
                pre_spawn=True,
            )
        except WorkspaceGuardError as error:
            raise ControlPlaneError(str(error)) from error
        with acquire(self.root, self.session_id):
            state = self._read_state()
            dispatch, _wave, _allowed = claim(state)
            dispatch["tool_use_id"] = tool_use_id
            self._write_state(state)

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

    def postflight_tool(self, payload: Mapping[str, Any]) -> None:
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            raise ControlPlaneError("native tool result has no call identity")
        with acquire(self.root, self.session_id):
            state = self._read_state()
            matches = [item for item in state["dispatches"].values() if item.get("tool_use_id") == tool_use_id]
            if len(matches) != 1:
                raise ControlPlaneError("native tool result has no unique dispatch")
            dispatch = matches[0]
            response = payload.get("tool_response")
            if _native_rejected(response):
                dispatch["tool_use_id"] = None
                if dispatch["tool_kind"] == "continuation":
                    dispatch["pending_cursor"] = None
                    dispatch["state"] = "paused"
                    for member in dispatch["members"]:
                        state["logical"][member]["state"] = "paused"
                    self._write_state(state)
                    return
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
                self._write_state(state)
                return
            if dispatch["tool_kind"] == "spawn":
                owners = _task_paths(response)
                if len(owners) != 1:
                    self._fence_members(state, dispatch, "native_owner_unresolved")
                    self._settle_wave(state)
                    self._write_state(state)
                    return
                dispatch["owner"] = owners.pop()
            elif dispatch["pending_cursor"] is not None:
                dispatch["cursor"] = dispatch["pending_cursor"]
                dispatch["pending_cursor"] = None
            dispatch["state"] = "running"
            dispatch["tool_use_id"] = None
            for member in dispatch["members"]:
                state["logical"][member]["state"] = "running"
            self._write_state(state)

    def prepare_continuation(self, dispatch_id: str, evidence_delta: object) -> dict[str, Any]:
        if not isinstance(evidence_delta, Mapping) or not evidence_delta:
            raise ControlPlaneError("continuation requires a non-empty evidence object")
        with acquire(self.root, self.session_id):
            state = self._read_state()
            dispatch = self._find_dispatch(state, dispatch_id)
            if dispatch["state"] != "paused" or not isinstance(dispatch.get("owner"), str):
                raise ControlPlaneError("dispatch is not continuable")
            cursor = dispatch["cursor"] + 1
            try:
                message = _render_continue(dispatch, evidence_delta, cursor)
            except ProtocolHashError as error:
                raise ControlPlaneError(str(error)) from error
            dispatch["native"] = {"message": message, "target": dispatch["owner"]}
            dispatch["pending_cursor"] = cursor
            dispatch["state"] = "starting"
            dispatch["tool_kind"] = "continuation"
            dispatch["tool_use_id"] = None
            for member in dispatch["members"]:
                state["logical"][member]["state"] = "starting"
            self._write_state(state)
            return {
                "dispatch_id": dispatch_id,
                "message": message,
                "protocol": "cco.continuation.v1",
                "target": dispatch["owner"],
            }

    def owner_is_managed(self, owner: str) -> bool:
        if not self.state_path.exists():
            return False
        with acquire(self.root, self.session_id):
            state = self._read_state()
            return any(item.get("owner") == owner for item in state["dispatches"].values())

    def interrupt_owner(self, owner: str) -> None:
        with acquire(self.root, self.session_id):
            state = self._read_state()
            matches = [
                item
                for item in state["dispatches"].values()
                if item.get("owner") == owner and item["state"] in {"running", "paused", "starting"}
            ]
            if len(matches) != 1:
                raise ControlPlaneError("interrupt target has no unique active dispatch")
            self._fence_members(state, matches[0], "interrupted")
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
            if dispatch.get("owner") != owner or dispatch["state"] != "running":
                raise ControlPlaneError("result owner is stale or fenced")
            if result["cursor"] != dispatch["cursor"]:
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

        with acquire(self.root, self.session_id):
            state = self._read_state()
            dispatch, _plan, _nodes, wave, allowed = claim(state)
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
        except WorkspaceGuardError as error:
            raise ControlPlaneError(str(error)) from error
        actual = verification["owner_changed_paths"]
        if actual != result["changed_paths"]:
            raise ControlPlaneError(
                "declared changed paths do not match the verified owner delta"
            )
        if role != "worker" and actual:
            raise ControlPlaneError("read-only child changed its declared scope")

        with acquire(self.root, self.session_id):
            state = self._read_state()
            dispatch, plan, nodes, _wave, _allowed = claim(state)
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

    def fence_invalid_result(self, owner: str, reason: str = "invalid_result") -> None:
        with acquire(self.root, self.session_id):
            state = self._read_state()
            matches = [
                item
                for item in state["dispatches"].values()
                if item.get("owner") == owner and item["state"] in {"running", "paused", "starting"}
            ]
            if len(matches) == 1:
                self._fence_members(state, matches[0], reason)
                self._settle_wave(state)
                self._write_state(state)

    def restart(self) -> int:
        if not self.state_path.exists():
            return 0
        with acquire(self.root, self.session_id):
            state = self._read_state()
            count = 0
            for dispatch in state["dispatches"].values():
                if dispatch["state"] in {"starting", "running", "paused"}:
                    self._fence_members(state, dispatch, "host_restart")
                    count += 1
            self._settle_wave(state)
            state["epoch"] += 1
            self._write_state(state)
            return count

    def abandon(self, node_id: str) -> None:
        with acquire(self.root, self.session_id):
            state = self._read_state()
            logical = state["logical"].get(node_id)
            if not isinstance(logical, dict) or logical["state"] != "paused":
                raise ControlPlaneError("only a paused node can be abandoned")
            dispatch = self._find_dispatch(state, logical["dispatch_id"])
            self._fence_members(state, dispatch, "abandoned")
            self._settle_wave(state)
            self._write_state(state)

    def retry(self, node_id: str) -> None:
        with acquire(self.root, self.session_id):
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
            dependencies = [state["logical"][item]["state"] for item in nodes[node_id]["depends_on"]]
            logical["state"] = "ready" if all(item == "retired" for item in dependencies) else "waiting"
            self._write_state(state)

    def status(self) -> dict[str, Any]:
        with acquire(self.root, self.session_id):
            state = self._read_state()
            plan = self._read_plan(state)
            counts = {name: 0 for name in sorted(LOGICAL_STATES)}
            for item in state["logical"].values():
                counts[item["state"]] += 1
            return {
                "counts": counts,
                "epoch": state["epoch"],
                "plan_id": state["plan_id"],
                "protocol": "cco.status.v1",
                "state": self._overall_state(state, plan),
            }

    def cleanup(self) -> int:
        """Remove only this task's inactive v9 state and immutable artifacts."""

        removed = 0
        with acquire(self.root, self.session_id):
            if self.state_path.exists():
                state = self._read_state()
                if any(
                    item["state"] in {"starting", "running", "paused"}
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

        with acquire(self.root, self.session_id):
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
        with acquire(self.root, self.session_id):
            state = self._read_state()
            if any(
                item["state"] in {"starting", "running"}
                for item in state["dispatches"].values()
            ):
                return "CCO child work is still active; wait for its native terminal event."
            return None


def _session_arg(value: str | None) -> str:
    session = value or os.environ.get("CODEX_THREAD_ID")
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
    root.add_argument("--session")
    sub = root.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--repo", type=Path, default=Path.cwd())
    next_parser = sub.add_parser("next")
    next_parser.add_argument("--capacity", type=int, required=True)
    next_parser.add_argument("--catalog", type=Path)
    continuation = sub.add_parser("continue")
    continuation.add_argument("--dispatch", required=True)
    abandon = sub.add_parser("abandon")
    abandon.add_argument("--node", required=True)
    retry = sub.add_parser("retry")
    retry.add_argument("--node", required=True)
    sub.add_parser("status")
    sub.add_parser("restart")
    sub.add_parser("cleanup")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        control = ControlPlane(_session_arg(args.session))
        if args.command == "plan":
            result = control.create_plan(args.repo, _stdin_json())
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
