#!/usr/bin/env python3
"""Single deterministic entry point for preparing a cco.v7 dispatch graph."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import heapq
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Mapping

from decision_policy import (
    DecisionPolicyError,
    derive_node_decision,
    normalize_risk_assessment,
    select_ready_nodes,
)
from dispatch_transaction import (
    DispatchTransactionError,
    prepare_dispatch_batch as prepare_transaction_batch,
)
from packet_compiler import CapsuleError, compile_dispatch
from prepared_graph import (
    artifact_path,
    graph_scopes,
    graph_sha256,
    load_artifact,
    verify_artifact_workspace,
    write_artifact,
)
from protocol_hash import ProtocolHashError, canonical_bytes, require_repository_scope
from routing_catalog import (
    RoutingCatalogError,
    advance_route_plan,
    load_json_bytes,
    load_native_catalog,
    load_route_policy,
    resolve_route_plan,
)
from workspace_state import (
    DEFAULT_IGNORED_MAX_BYTES,
    DEFAULT_IGNORED_MAX_FILES,
    StateError,
    WORKSPACE_MODES,
    normalize_allow,
    repository_control_roots,
    repository_gitlinks,
    repository_index_records,
    repository_path_spelling_map,
    repository_root,
    state_payload,
)


PROTOCOL = "cco.prepared-graph.v3"
GRAPH_PROTOCOL = "cco.graph.v4"
DISPATCH_BATCH_PROTOCOL = "cco.dispatch-batch.v2"
CLI_INPUT_MAX_BYTES = 4 * 1024 * 1024
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
NODE_FIELDS = frozenset(
    {
        "acceptance_facts",
        "closure",
        "contract",
        "generation",
        "node",
        "placement",
        "role",
        "scopes",
        "selection",
    }
)
OPTIONAL_NODE_FIELDS = frozenset(
    {"current_state", "epoch", "evidence", "fork_turns", "route"}
)
DEFAULTABLE_NODE_FIELDS = frozenset(
    {"acceptance_facts", "closure", "fork_turns", "generation", "placement", "role", "route"}
)
NODE_ID = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
PREPARED_FIELDS = frozenset(
    {
        "baseline",
        "baseline_path",
        "blocked_dependency_nodes",
        "completed_nodes",
        "deferred_nodes",
        "dispatches",
        "fallback_dispatches",
        "graph_sha256",
        "manifest",
        "member_mapping",
        "primary_nodes",
        "protocol",
        "route_errors",
        "route_plan",
    }
)


class GraphCompilerError(ValueError):
    """A graph cannot be safely derived or bound to the current workspace."""


def _scopes(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise GraphCompilerError(f"{label} must be a list")
    try:
        scopes = [require_repository_scope(item, f"{label}[{i}]") for i, item in enumerate(value)]
    except ProtocolHashError as error:
        raise GraphCompilerError(str(error)) from error
    if scopes != sorted(scopes, key=lambda item: (item["kind"], item["path"])) or len({(item["kind"], item["path"]) for item in scopes}) != len(scopes):
        raise GraphCompilerError(f"{label} must be sorted and duplicate-free")
    return scopes


def _route_constraints(value: object) -> dict[str, Any]:
    if value is None:
        return {"fixed_effort": None, "fixed_model": None, "source": "automatic"}
    if not isinstance(value, Mapping) or set(value) != {"fixed_effort", "fixed_model", "source"}:
        raise GraphCompilerError("node route constraints are malformed")
    if value["source"] not in {"automatic", "user"}:
        raise GraphCompilerError("node route constraint source is invalid")
    return dict(value)


def _route_for(plan: Mapping[str, Any], node: str) -> dict[str, Any]:
    matches = [route for route in plan["routes"] if route["node"] == node]
    if len(matches) != 1:
        raise GraphCompilerError(f"route plan lacks one exact route for {node}")
    return matches[0]


def _route_bindings(plans: list[Mapping[str, Any]], *, node: str) -> list[dict[str, Any]]:
    return [
        {
            "constraints": dict(route["constraints"]),
            "decision_sha256": route["decision_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "rank": route["dispatch"]["rank"],
            "selected": dict(route["selected"]),
        }
        for plan in plans
        for route in [_route_for(plan, node)]
    ]


def _aggregation_bytes(value: dict[str, Any], label: str) -> bytes:
    try:
        return canonical_bytes(value)
    except ProtocolHashError as error:
        raise GraphCompilerError(f"microtask aggregation {label} is invalid: {error}") from error


def _aggregation_key(item: Mapping[str, Any]) -> tuple[object, ...]:
    """Return the complete compatibility frontier for one primary microtask."""

    decision = item["decision"]
    optional = item["optional"]
    shared_optional = {
        name: optional[name]
        for name in ("current_state", "epoch", "evidence")
        if name in optional
    }
    return (
        decision["role"],
        decision["assurance"],
        decision["acceptance"]["mode"],
        _aggregation_bytes(item["route_constraints"], "route constraints"),
        item["generation"],
        optional.get("fork_turns", "none"),
        item["selection"]["responsibility"],
        tuple(item["selection"]["depends_on"]),
        _aggregation_bytes(shared_optional, "optional state"),
    )


def _aggregate_node_name(members: list[Mapping[str, Any]]) -> str:
    material = {"members": [item["node"] for item in members]}
    return "aggregate_" + hashlib.sha256(canonical_bytes(material)).hexdigest()[:48]


def _union_scopes(members: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    by_scope = {
        (scope["kind"], scope["path"]): dict(scope)
        for item in members
        for scope in item["scopes"]
    }
    return [by_scope[key] for key in sorted(by_scope)]


def _aggregate_microtasks(
    members: list[Mapping[str, Any]],
    *,
    downstream: Mapping[str, set[str]],
    completed_nodes: set[str],
) -> dict[str, Any]:
    """Build one physical child node without discarding its member contracts."""

    ordered_members = sorted(members, key=lambda item: item["node"])
    first = ordered_members[0]
    decision = first["decision"]
    node = _aggregate_node_name(ordered_members)
    member_nodes = [item["node"] for item in ordered_members]
    member_set = set(member_nodes)
    revisions = [item["contract"].get("contract_rev") for item in ordered_members]
    contract_rev = (
        max(revision for revision in revisions if isinstance(revision, int) and not isinstance(revision, bool))
        if all(isinstance(revision, int) and not isinstance(revision, bool) for revision in revisions)
        else 1
    )
    acceptance_ids = sorted(
        {
            acceptance_id
            for item in ordered_members
            for acceptance_id in item["decision"]["acceptance_ids"]
        }
    )
    acceptance_reasons = sorted(
        {
            reason
            for item in ordered_members
            for reason in item["decision"]["acceptance"]["reasons"]
        }
    )
    optional = {
        name: first["optional"][name]
        for name in ("current_state", "epoch", "evidence", "fork_turns")
        if name in first["optional"]
    }
    aggregate_downstream = set().union(
        *(downstream[item["node"]] for item in ordered_members)
    )
    return {
        "acceptance_facts": None,
        "contract": {
            "contract_rev": contract_rev,
            "members": [dict(item["contract"]) for item in ordered_members],
            "node": node,
            "objective": "aggregate microtasks",
        },
        "decision": {
            "acceptance": {
                "mode": decision["acceptance"]["mode"],
                "reasons": acceptance_reasons,
            },
            "acceptance_ids": acceptance_ids,
            "assurance": decision["assurance"],
            "placement": {"reason": "aggregated_microtasks", "target": "child"},
            "role": decision["role"],
        },
        "generation": first["generation"],
        "member_nodes": member_nodes,
        "node": node,
        "optional": optional,
        "route_constraints": dict(first["route_constraints"]),
        "scopes": _union_scopes(ordered_members),
        "selection": {
            "depends_on": list(first["selection"]["depends_on"]),
            "dependencies_ready": first["selection"]["dependencies_ready"],
            "downstream_count": len(aggregate_downstream - member_set - completed_nodes),
            "responsibility": first["selection"]["responsibility"],
        },
    }


def _dispatch_items(
    normalized: list[dict[str, Any]],
    *,
    completed_nodes: list[str],
    downstream: Mapping[str, set[str]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Apply whole-graph microtask aggregation before child placement is final."""

    completed = set(completed_nodes)
    candidates = [
        item
        for item in normalized
        if item["node"] not in completed
        and item["decision"]["placement"] == {"reason": "microtask", "target": "primary"}
    ]
    groups: dict[tuple[object, ...], list[dict[str, Any]]] = {}
    for item in candidates:
        groups.setdefault(_aggregation_key(item), []).append(item)

    aggregates: list[dict[str, Any]] = []
    member_mapping: dict[str, str] = {}
    existing_nodes = {item["node"] for item in normalized}
    aggregated_members: set[str] = set()
    for members in sorted(groups.values(), key=lambda group: min(item["node"] for item in group)):
        if len(members) < 2:
            continue
        aggregate = _aggregate_microtasks(
            members,
            downstream=downstream,
            completed_nodes=completed,
        )
        if aggregate["node"] in existing_nodes:
            raise GraphCompilerError("aggregate node identity collides with a declared node")
        existing_nodes.add(aggregate["node"])
        aggregates.append(aggregate)
        aggregated_members.update(aggregate["member_nodes"])
        member_mapping.update(
            {member: aggregate["node"] for member in aggregate["member_nodes"]}
        )

    physical: list[dict[str, Any]] = []
    for item in normalized:
        if item["node"] in completed or item["node"] in aggregated_members:
            continue
        if item["decision"]["placement"]["target"] != "child":
            continue
        physical_item = dict(item)
        physical_item["member_nodes"] = [item["node"]]
        physical.append(physical_item)
    physical.extend(aggregates)
    return sorted(physical, key=lambda item: item["node"]), {
        member: member_mapping[member] for member in sorted(member_mapping)
    }


def _baseline_path(session_id: str, identity: str) -> Path:
    configured = os.environ.get("CCO_LEDGER_DIR")
    root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    absolute = Path(os.path.abspath(root))
    for candidate in (absolute, *absolute.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise GraphCompilerError("ledger directory cannot use a reparse ancestor")
    return artifact_path(absolute.resolve(), session_id, identity)


def _normalize_node(value: object, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - (NODE_FIELDS | OPTIONAL_NODE_FIELDS) or not NODE_FIELDS <= set(value):
        raise GraphCompilerError(f"graph node {index} has unsupported or missing fields")
    node = value["node"]
    if not isinstance(node, str) or NODE_ID.fullmatch(node) is None:
        raise GraphCompilerError(f"graph node {index}.node is invalid")
    contract = value["contract"]
    if not isinstance(contract, Mapping) or contract.get("node") != node:
        raise GraphCompilerError(f"graph node {index}.contract identity is inconsistent")
    try:
        decision = derive_node_decision(
            {
                "acceptance_facts": value["acceptance_facts"],
                "closure": value["closure"],
                "placement": value["placement"],
                "role": value["role"],
            }
        )
    except DecisionPolicyError as error:
        raise GraphCompilerError(f"node {node} decision facts are invalid: {error}") from error
    scopes = _scopes(value["scopes"], f"graph node {index}.scopes")
    selection = value["selection"]
    selection_required = {"depends_on", "responsibility"}
    if (
        not isinstance(selection, Mapping)
        or set(selection) != selection_required
        or not selection_required <= set(selection)
        or not isinstance(selection["responsibility"], str)
        or not selection["responsibility"]
    ):
        raise GraphCompilerError(f"graph node {index}.selection is malformed")
    depends_on = selection["depends_on"]
    if not isinstance(depends_on, list):
        raise GraphCompilerError(f"graph node {index}.selection.depends_on must be a list")
    if any(not isinstance(dependency, str) or NODE_ID.fullmatch(dependency) is None for dependency in depends_on):
        raise GraphCompilerError(f"graph node {index}.selection.depends_on contains an invalid identifier")
    if depends_on != sorted(depends_on) or len(depends_on) != len(set(depends_on)):
        raise GraphCompilerError(f"graph node {index}.selection.depends_on must be sorted and duplicate-free")
    if node in depends_on:
        raise GraphCompilerError(f"graph node {node} has a self dependency")
    generation = value["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise GraphCompilerError(f"graph node {index}.generation is invalid")
    route_constraints = _route_constraints(value.get("route"))
    return {
        "acceptance_facts": {
            "acceptance_ids": list(value["acceptance_facts"]["acceptance_ids"]),
            "deterministic_graph_coverage": list(value["acceptance_facts"]["deterministic_graph_coverage"]),
            "events": list(value["acceptance_facts"]["events"]),
            "required_verification_strengths": list(value["acceptance_facts"]["required_verification_strengths"]),
            "risk_assessment": normalize_risk_assessment(value["acceptance_facts"]["risk_assessment"]),
        },
        "contract": dict(contract),
        "decision": decision,
        "generation": generation,
        "node": node,
        "optional": {name: value[name] for name in OPTIONAL_NODE_FIELDS if name in value},
        "route_constraints": route_constraints,
        "scopes": scopes,
        "selection": {
            "depends_on": list(depends_on),
            "dependencies_ready": False,
            "downstream_count": 0,
            "responsibility": selection["responsibility"],
        },
    }


def _completed_node_ids(value: object) -> list[str]:
    """Return the canonical completed dependency frontier for one preparation."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise GraphCompilerError("completed_nodes must be a list")
    if any(not isinstance(node, str) or NODE_ID.fullmatch(node) is None for node in value):
        raise GraphCompilerError("completed_nodes contains an invalid identifier")
    if value != sorted(value) or len(value) != len(set(value)):
        raise GraphCompilerError("completed_nodes must be sorted and duplicate-free")
    return list(value)


def _derive_dependency_facts(
    normalized: list[dict[str, Any]], *, completed_nodes: list[str]
) -> dict[str, set[str]]:
    """Validate the declared DAG and derive readiness plus transitive priority."""

    by_node = {item["node"]: item for item in normalized}
    node_names = set(by_node)
    completed = set(completed_nodes)
    for item in normalized:
        for dependency in item["selection"]["depends_on"]:
            if dependency not in node_names and dependency not in completed:
                raise GraphCompilerError(
                    f"graph node {item['node']} has an unknown dependency {dependency}"
                )

    dependents = {node: set() for node in node_names}
    unresolved_dependencies = {node: 0 for node in node_names}
    for item in normalized:
        for dependency in item["selection"]["depends_on"]:
            if dependency in dependents:
                dependents[dependency].add(item["node"])
                unresolved_dependencies[item["node"]] += 1

    ready = [node for node in node_names if unresolved_dependencies[node] == 0]
    heapq.heapify(ready)
    visited: set[str] = set()
    while ready:
        node = heapq.heappop(ready)
        visited.add(node)
        for dependent in sorted(dependents[node]):
            unresolved_dependencies[dependent] -= 1
            if unresolved_dependencies[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(visited) != len(node_names):
        raise GraphCompilerError(
            f"graph dependency cycle includes {min(node_names - visited)}"
        )

    downstream: dict[str, set[str]] = {}
    for node in sorted(node_names):
        pending = list(sorted(dependents[node], reverse=True))
        seen: set[str] = set()
        while pending:
            descendant = pending.pop()
            if descendant in seen:
                continue
            seen.add(descendant)
            pending.extend(sorted(dependents[descendant], reverse=True))
        downstream[node] = seen

    for item in normalized:
        selection = item["selection"]
        selection["dependencies_ready"] = all(
            dependency in completed for dependency in selection["depends_on"]
        )
        selection["downstream_count"] = len(downstream[item["node"]] - completed)
    return downstream


def _route_request(item: Mapping[str, Any]) -> dict[str, Any]:
    decision = item["decision"]
    return {
        "assurance": decision["assurance"],
        "constraints": item["route_constraints"],
        "node": item["node"],
        "role": decision["role"],
    }


def _node_partitions(
    normalized: list[Mapping[str, Any]],
    routable_items: list[Mapping[str, Any]],
    selected_nodes: list[str],
    *,
    completed_nodes: list[str],
) -> dict[str, list[str]]:
    completed = set(completed_nodes)
    routable_nodes = {
        member
        for item in routable_items
        for member in item["member_nodes"]
    }
    selected = set(selected_nodes)
    return {
        "blocked_dependency_nodes": sorted(
            member
            for item in routable_items
            for member in item["member_nodes"]
            if not item["selection"]["dependencies_ready"]
        ),
        "deferred_nodes": sorted(
            member
            for item in routable_items
            for member in item["member_nodes"]
            if item["selection"]["dependencies_ready"] and item["node"] not in selected
        ),
        "primary_nodes": sorted(
            item["node"]
            for item in normalized
            if item["node"] not in completed and item["node"] not in routable_nodes
        ),
    }


def _no_dispatch_result(
    normalized: list[Mapping[str, Any]],
    route_errors: Mapping[str, str],
    *,
    routable_items: list[Mapping[str, Any]],
    completed_nodes: list[str],
    member_mapping: Mapping[str, str],
) -> dict[str, Any]:
    partitions = _node_partitions(
        normalized,
        routable_items,
        [],
        completed_nodes=completed_nodes,
    )
    return {
        "baseline": None,
        "baseline_path": None,
        "blocked_dependency_nodes": partitions["blocked_dependency_nodes"],
        "completed_nodes": list(completed_nodes),
        "deferred_nodes": partitions["deferred_nodes"],
        "dispatches": [],
        "fallback_dispatches": {},
        "graph_sha256": None,
        "manifest": None,
        "member_mapping": dict(member_mapping),
        "primary_nodes": partitions["primary_nodes"],
        "protocol": PROTOCOL,
        "route_errors": {node: route_errors[node] for node in sorted(route_errors)},
        "route_plan": None,
    }


def prepare_dispatch_graph(
    nodes: object,
    *,
    completed_nodes: object = None,
    native_capacity: int,
    repo: Path,
    native_catalog: object | None = None,
    policy: object = None,
    codex_home: Path | None = None,
    workspace_mode: str = "light",
    ignored_max_files: int = DEFAULT_IGNORED_MAX_FILES,
    ignored_max_bytes: int = DEFAULT_IGNORED_MAX_BYTES,
) -> dict[str, Any]:
    """Derive, route, baseline, and compile one complete graph in one call."""

    if not isinstance(nodes, list) or not nodes:
        raise GraphCompilerError("graph nodes must be a non-empty list")
    if workspace_mode not in WORKSPACE_MODES:
        raise GraphCompilerError("workspace mode must be light or strict")
    session_id = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise GraphCompilerError("CODEX_THREAD_ID is required for task-local baselines")
    normalized = [_normalize_node(value, index) for index, value in enumerate(nodes)]
    if len({item["node"] for item in normalized}) != len(normalized):
        raise GraphCompilerError("graph node identities must be unique")
    completed = _completed_node_ids(completed_nodes)
    downstream = _derive_dependency_facts(normalized, completed_nodes=completed)
    root = repository_root(Path(repo))
    control_roots = repository_control_roots(root)
    index_records = repository_index_records(root)
    gitlinks = repository_gitlinks(root, index_records)
    tracked_spellings = repository_path_spelling_map(index_records)
    directory_spellings: dict[str, frozenset[str]] = {}
    try:
        for item in normalized:
            item["scopes"] = [
                normalize_allow(
                    root,
                    f"{scope['kind']}:{scope['path']}",
                    protected_roots=control_roots,
                    gitlinks=gitlinks,
                    tracked_spellings=tracked_spellings,
                    directory_spellings=directory_spellings,
                )
                for scope in item["scopes"]
            ]
    except StateError as error:
        raise GraphCompilerError(f"graph scope is unsafe: {error}") from error
    child_items, member_mapping = _dispatch_items(
        normalized,
        completed_nodes=completed,
        downstream=downstream,
    )
    if not child_items:
        return _no_dispatch_result(
            normalized,
            {},
            routable_items=[],
            completed_nodes=completed,
            member_mapping=member_mapping,
        )
    physical_items = child_items
    route_errors: dict[str, str] = {}
    catalog_error: RoutingCatalogError | None = None
    if native_catalog is None:
        try:
            native_catalog = load_native_catalog()
        except RoutingCatalogError as error:
            catalog_error = error
    loaded_policy = policy
    policy_error: RoutingCatalogError | None = None
    if loaded_policy is None and catalog_error is None:
        try:
            loaded = load_route_policy(root, codex_home=codex_home)
            loaded_policy = loaded["policy"]
        except RoutingCatalogError as error:
            policy_error = error

    routable_items: list[dict[str, Any]] = []
    if catalog_error is not None or policy_error is not None:
        error = catalog_error or policy_error
        for item in child_items:
            route_errors[item["node"]] = str(error)
    else:
        for item in child_items:
            try:
                resolve_route_plan(
                    [_route_request(item)],
                    native_catalog,
                    policy=loaded_policy,
                )
            except RoutingCatalogError as error:
                route_errors[item["node"]] = str(error)
            else:
                routable_items.append(item)
    for item in child_items:
        if item["node"] in route_errors:
            item["decision"]["placement"] = {
                "reason": "route_unavailable",
                "target": "primary",
            }
    logical_route_errors = {
        member: route_errors[item["node"]]
        for item in child_items
        if item["node"] in route_errors
        for member in item["member_nodes"]
    }
    child_items = routable_items
    try:
        route_plan = (
            resolve_route_plan(
                [_route_request(item) for item in child_items],
                native_catalog,
                policy=loaded_policy,
            )
            if child_items
            else None
        )
    except RoutingCatalogError as error:  # defensive: per-node probes already passed
        raise GraphCompilerError(f"combined route plan is inconsistent: {error}") from error
    if not child_items:
        return _no_dispatch_result(
            normalized,
            logical_route_errors,
            routable_items=[],
            completed_nodes=completed,
            member_mapping=member_mapping,
        )

    route_variants: dict[str, list[dict[str, Any]]] = {}
    for item in child_items:
        if route_plan is None:
            raise GraphCompilerError("child route plan is missing")
        variants = [route_plan]
        current = route_plan
        route = _route_for(current, item["node"])
        while route["dispatch"]["rank"] < len(route["candidates"]):
            selected = route["selected"]
            try:
                current = advance_route_plan(
                    current,
                    node=item["node"],
                    rejected_model=selected["model"],
                    rejected_effort=selected["effort"],
                    rejection_ticket=f"native:prethread-rejected-r{route['dispatch']['rank']:02d}",
                )
            except RoutingCatalogError as error:
                raise GraphCompilerError(f"route fallback is invalid: {error}") from error
            variants.append(current)
            route = _route_for(current, item["node"])
        route_variants[item["node"]] = variants

    selector_nodes = [
        {
            "access": "write" if item["decision"]["role"] == "worker" else "read",
            "dependencies_ready": item["selection"]["dependencies_ready"],
            "node": item["node"],
            "responsibility": item["selection"]["responsibility"],
            "scope": item["scopes"],
            "downstream_count": item["selection"]["downstream_count"],
        }
        for item in child_items
    ]
    try:
        selected_nodes = select_ready_nodes(selector_nodes, native_capacity=native_capacity)
    except DecisionPolicyError as error:
        raise GraphCompilerError(f"graph capacity selection failed: {error}") from error
    if not selected_nodes:
        return _no_dispatch_result(
            normalized,
            logical_route_errors,
            routable_items=child_items,
            completed_nodes=completed,
            member_mapping=member_mapping,
        )

    snapshot = state_payload(
        root,
        control_roots=control_roots,
        index_records=index_records,
        ignored_mode=workspace_mode,
        ignored_max_files=ignored_max_files,
        ignored_max_bytes=ignored_max_bytes,
        scopes=[scope for item in normalized for scope in item["scopes"]],
    )
    manifest_by_node = {item["node"]: item for item in normalized}
    manifest_by_node.update(
        {
            item["node"]: item
            for item in physical_items
            if len(item["member_nodes"]) > 1
        }
    )
    manifest_nodes: list[dict[str, Any]] = []
    for item in sorted(manifest_by_node.values(), key=lambda entry: entry["node"]):
        bindings = _route_bindings(route_variants[item["node"]], node=item["node"]) if item["node"] in route_variants else []
        manifest_node: dict[str, Any] = {
            "contract": item["contract"],
            "decision": item["decision"],
            "node": item["node"],
            "route_bindings": bindings,
            "scopes": item["scopes"],
            "selection": {
                "depends_on": list(item["selection"]["depends_on"]),
                "dependencies_ready": item["selection"]["dependencies_ready"],
                "downstream_count": item["selection"]["downstream_count"],
                "responsibility": item["selection"]["responsibility"],
            },
        }
        if len(item.get("member_nodes", [])) > 1:
            manifest_node["member_nodes"] = list(item["member_nodes"])
        manifest_nodes.append(manifest_node)
    manifest = {
        "baseline": snapshot["state_id"],
        "completed_nodes": completed,
        "member_mapping": member_mapping,
        "nodes": manifest_nodes,
        "protocol": GRAPH_PROTOCOL,
        "route_plan_sha256": route_plan["plan_sha256"] if route_plan else None,
        "workspace_mode": workspace_mode,
    }
    identity = graph_sha256(manifest)
    baseline_path = _baseline_path(session_id, identity)
    by_node = {item["node"]: item for item in physical_items}
    dispatches: list[dict[str, Any]] = []
    fallback_dispatches: dict[str, list[dict[str, Any]]] = {}
    for node in selected_nodes:
        item = by_node[node]
        decision = item["decision"]
        route = _route_for(route_variants[node][0], node)
        optional = item["optional"]
        spec: dict[str, Any] = {
            "acceptance": decision["acceptance"],
            "acceptance_ids": decision["acceptance_ids"],
            "assurance": decision["assurance"],
            "baseline": snapshot["state_id"],
            "contract": item["contract"],
            "fork_turns": optional.get("fork_turns", "none"),
            "generation": item["generation"],
            "graph_sha256": identity,
            "mode": "fresh" if decision["role"] == "reviewer" else workspace_mode,
            "node": node,
            "role": decision["role"],
            "route": {
                "constraints": route["constraints"],
                "decision_sha256": route["decision_sha256"],
                "plan_sha256": route_variants[node][0]["plan_sha256"],
                "rank": route["dispatch"]["rank"],
                "selected": route["selected"],
            },
            "scopes": item["scopes"],
        }
        for name in ("current_state", "epoch", "evidence"):
            if name in optional:
                spec[name] = optional[name]
        compiled: list[dict[str, Any]] = []
        for variant in route_variants[node]:
            variant_route = _route_for(variant, node)
            variant_spec = dict(spec)
            variant_spec["route"] = {
                "constraints": variant_route["constraints"],
                "decision_sha256": variant_route["decision_sha256"],
                "plan_sha256": variant["plan_sha256"],
                "rank": variant_route["dispatch"]["rank"],
                "selected": variant_route["selected"],
            }
            try:
                compiled.append(compile_dispatch(variant_spec))
            except CapsuleError as error:
                raise GraphCompilerError(f"graph node {node} could not compile: {error}") from error
        dispatches.append(compiled[0])
        fallback_dispatches[node] = compiled[1:]
    write_artifact(root, baseline_path, manifest=manifest, snapshot=snapshot)
    partitions = _node_partitions(
        normalized,
        child_items,
        selected_nodes,
        completed_nodes=completed,
    )
    return {
        "baseline": snapshot["state_id"],
        "baseline_path": str(baseline_path),
        "blocked_dependency_nodes": partitions["blocked_dependency_nodes"],
        "completed_nodes": completed,
        "deferred_nodes": partitions["deferred_nodes"],
        "dispatches": dispatches,
        "fallback_dispatches": fallback_dispatches,
        "graph_sha256": identity,
        "manifest": manifest,
        "member_mapping": member_mapping,
        "primary_nodes": partitions["primary_nodes"],
        "protocol": PROTOCOL,
        "route_errors": {
            node: logical_route_errors[node] for node in sorted(logical_route_errors)
        },
        "route_plan": route_plan,
    }


def verify_prepared_graph(prepared: object, *, repo: Path) -> dict[str, Any]:
    if not isinstance(prepared, Mapping) or set(prepared) != PREPARED_FIELDS or prepared.get("protocol") != PROTOCOL:
        raise GraphCompilerError("prepared graph is malformed")
    partition_names = ("primary_nodes", "deferred_nodes", "blocked_dependency_nodes")
    partitions = [prepared[name] for name in partition_names]
    if any(
        not isinstance(nodes, list)
        or any(not isinstance(node, str) or NODE_ID.fullmatch(node) is None for node in nodes)
        or nodes != sorted(nodes)
        or len(nodes) != len(set(nodes))
        for nodes in partitions
    ):
        raise GraphCompilerError("prepared graph node partitions are malformed")
    if len(set().union(*(set(nodes) for nodes in partitions))) != sum(len(nodes) for nodes in partitions):
        raise GraphCompilerError("prepared graph node partitions overlap")
    completed_nodes = _completed_node_ids(prepared["completed_nodes"])
    if any(set(nodes) & set(completed_nodes) for nodes in partitions):
        raise GraphCompilerError("prepared graph completed nodes overlap active partitions")
    member_mapping = prepared["member_mapping"]
    if (
        not isinstance(member_mapping, Mapping)
        or any(
            not isinstance(member, str)
            or NODE_ID.fullmatch(member) is None
            or not isinstance(aggregate, str)
            or NODE_ID.fullmatch(aggregate) is None
            for member, aggregate in member_mapping.items()
        )
        or list(member_mapping) != sorted(member_mapping)
    ):
        raise GraphCompilerError("prepared graph member mapping is malformed")
    if not prepared["dispatches"]:
        if (
            prepared["baseline"] is not None
            or prepared["baseline_path"] is not None
            or prepared["graph_sha256"] is not None
            or prepared["manifest"] is not None
            or prepared["fallback_dispatches"] != {}
            or prepared["route_plan"] is not None
        ):
            raise GraphCompilerError("empty dispatch graph has an artifact")
        return {}
    manifest = prepared.get("manifest")
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("protocol") != GRAPH_PROTOCOL
        or graph_sha256(manifest) != prepared.get("graph_sha256")
    ):
        raise GraphCompilerError("prepared graph identity does not match manifest")
    if (
        manifest.get("completed_nodes") != completed_nodes
        or manifest.get("member_mapping") != dict(member_mapping)
    ):
        raise GraphCompilerError("prepared graph DAG metadata is inconsistent")
    baseline_path = Path(str(prepared["baseline_path"]))
    try:
        artifact = load_artifact(baseline_path)
    except ValueError as error:
        raise GraphCompilerError(f"prepared graph baseline is invalid: {error}") from error
    if artifact["snapshot"]["state_id"] != prepared["baseline"] or artifact["manifest"] != manifest or artifact["graph_sha256"] != prepared["graph_sha256"]:
        raise GraphCompilerError("prepared graph baseline identity is inconsistent")
    try:
        return verify_artifact_workspace(
            baseline_path,
            repo=repo,
            baseline=str(prepared["baseline"]),
            graph_sha256_value=str(prepared["graph_sha256"]),
            graph_scopes_value=graph_scopes(manifest),
            workspace_mode=str(manifest["workspace_mode"]),
        )
    except ValueError as error:
        raise GraphCompilerError(f"prepared graph verification failed: {error}") from error


def compact_dispatch_batch(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the data Primary needs to invoke native Agent tools."""

    if (
        not isinstance(prepared, Mapping)
        or set(prepared) != PREPARED_FIELDS
        or prepared.get("protocol") != PROTOCOL
        or not isinstance(prepared["dispatches"], list)
    ):
        raise GraphCompilerError("prepared graph is malformed")
    if prepared["dispatches"]:
        manifest = prepared["manifest"]
        try:
            current_manifest = (
                isinstance(manifest, Mapping)
                and manifest.get("protocol") == GRAPH_PROTOCOL
                and graph_sha256(manifest) == prepared["graph_sha256"]
            )
        except ValueError:
            current_manifest = False
        if not current_manifest:
            raise GraphCompilerError("prepared graph is malformed")
    elif any(
        prepared[name] is not None
        for name in ("baseline", "baseline_path", "graph_sha256", "manifest", "route_plan")
    ) or prepared["fallback_dispatches"] != {}:
        raise GraphCompilerError("prepared graph is malformed")
    return {
        "baseline": prepared["baseline"],
        "baseline_path": prepared["baseline_path"],
        "blocked_dependency_nodes": prepared["blocked_dependency_nodes"],
        "completed_nodes": prepared["completed_nodes"],
        "deferred_nodes": prepared["deferred_nodes"],
        "dispatches": prepared["dispatches"],
        "fallback_dispatches": prepared["fallback_dispatches"],
        "graph_sha256": prepared["graph_sha256"],
        "member_mapping": prepared["member_mapping"],
        "primary_nodes": prepared["primary_nodes"],
        "protocol": DISPATCH_BATCH_PROTOCOL,
        "route_errors": prepared["route_errors"],
    }


def _prepare_document(document: object, args: argparse.Namespace) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise GraphCompilerError("prepare input must be a JSON object")
    optional = {
        "completed_nodes",
        "codex_home",
        "defaults",
        "ignored_max_bytes",
        "ignored_max_files",
        "native_catalog",
        "policy",
    }
    if set(document) - ({"nodes"} | optional) or "nodes" not in document:
        raise GraphCompilerError("prepare input fields are incomplete or unsupported")
    codex_home = document.get("codex_home")
    if codex_home is not None and not isinstance(codex_home, str):
        raise GraphCompilerError("codex_home must be a path string")
    defaults = document.get("defaults", {})
    if (
        not isinstance(defaults, Mapping)
        or set(defaults) - DEFAULTABLE_NODE_FIELDS
    ):
        raise GraphCompilerError("graph defaults contain unsupported fields")
    source_nodes = document["nodes"]
    if not isinstance(source_nodes, list):
        raise GraphCompilerError("graph nodes must be a list")
    nodes: list[object] = []
    for index, value in enumerate(source_nodes):
        if not isinstance(value, Mapping):
            raise GraphCompilerError(f"graph node {index} must be an object")
        merged = {name: deepcopy(item) for name, item in defaults.items()}
        merged.update({name: deepcopy(item) for name, item in value.items()})
        nodes.append(merged)
    prepared = prepare_dispatch_graph(
        nodes,
        completed_nodes=document.get("completed_nodes"),
        native_capacity=args.native_capacity,
        repo=args.repo,
        native_catalog=document.get("native_catalog"),
        policy=document.get("policy"),
        codex_home=Path(codex_home) if codex_home is not None else None,
        workspace_mode=args.workspace_mode,
        ignored_max_files=document.get("ignored_max_files", DEFAULT_IGNORED_MAX_FILES),
        ignored_max_bytes=document.get("ignored_max_bytes", DEFAULT_IGNORED_MAX_BYTES),
    )
    if args.full:
        return prepared
    batch = compact_dispatch_batch(prepared)
    configured = os.environ.get("CCO_LEDGER_DIR")
    ledger_root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    )
    session_id = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise GraphCompilerError("CODEX_THREAD_ID is required for dispatch transactions")
    try:
        return prepare_transaction_batch(
            batch,
            ledger_root=ledger_root,
            repo=args.repo,
            session_id=session_id,
        )
    except DispatchTransactionError as error:
        raise GraphCompilerError(f"dispatch transaction preparation failed: {error}") from error


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Prepare one network-free cco.v7 dispatch graph from JSON stdin."
    )
    root.add_argument("--repo", type=Path, required=True)
    root.add_argument("--native-capacity", type=int, required=True)
    root.add_argument("--workspace-mode", choices=tuple(sorted(WORKSPACE_MODES)), default="light")
    root.add_argument("--full", action="store_true", help="Emit diagnostic manifest and route plan")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        raw = sys.stdin.buffer.read(CLI_INPUT_MAX_BYTES + 1)
        if len(raw) > CLI_INPUT_MAX_BYTES:
            raise GraphCompilerError("prepare input exceeds the size limit")
        document = load_json_bytes(raw, "graph prepare input")
        print(canonical_bytes(_prepare_document(document, args)).decode("utf-8"))
        return 0
    except (GraphCompilerError, RoutingCatalogError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
