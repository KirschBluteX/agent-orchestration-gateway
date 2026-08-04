#!/usr/bin/env python3
"""Prepare one closed CCO graph from facts, routing, and a real workspace state."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from decision_policy import (
    DecisionPolicyError,
    derive_acceptance,
    derive_judgment,
    normalize_closure,
    normalize_dispatch_decision,
    normalize_placement_benefits,
    normalize_risk_assessment,
    classify_purpose,
    select_placement,
    select_ready_nodes,
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
from protocol_hash import ProtocolHashError, require_repository_scope
from routing_catalog import (
    RoutingCatalogError,
    advance_route_plan,
    validate_route_plan,
)
from workspace_state import (
    DEFAULT_IGNORED_MAX_BYTES,
    DEFAULT_IGNORED_MAX_FILES,
    WORKSPACE_MODES,
    repository_root,
    state_payload,
)


PROTOCOL = "cco.prepared-graph.v1"
GRAPH_PROTOCOL = "cco.graph.v1"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
NODE_FIELDS = frozenset(
    {
        "acceptance_facts",
        "closure",
        "contract",
        "effects",
        "generation",
        "node",
        "placement",
        "scopes",
        "selection",
    }
)
OPTIONAL_NODE_FIELDS = frozenset(
    {"current_state", "epoch", "evidence", "fork_turns"}
)


class GraphCompilerError(ValueError):
    """A graph cannot be safely derived or bound to the current workspace."""


def _digest(value: Mapping[str, Any]) -> str:
    return graph_sha256(value)


def _scopes(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise GraphCompilerError(f"{label} must be a list")
    try:
        scopes = [
            require_repository_scope(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    except ProtocolHashError as error:
        raise GraphCompilerError(str(error)) from error
    ordered = sorted(scopes, key=lambda item: (item["kind"], item["path"]))
    if scopes != ordered or len({(x["kind"], x["path"]) for x in scopes}) != len(scopes):
        raise GraphCompilerError(f"{label} must be sorted and duplicate-free")
    return scopes


def _route_for(plan: Mapping[str, Any], purpose: str, judgment: str) -> dict[str, Any]:
    matches = [
        route
        for route in plan["routes"]
        if route["purpose"] == purpose and route["judgment"] == judgment
    ]
    if len(matches) != 1:
        raise GraphCompilerError("route plan lacks one exact derived route key")
    return matches[0]


def _decision(item: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    effects = item["effects"]
    if not isinstance(effects, Mapping) or set(effects) != {
        "acceptance_verdict",
        "diagnostic_process",
        "repository_mutation",
    }:
        raise GraphCompilerError("node effects are malformed")
    try:
        purpose = classify_purpose(
            repository_mutation=effects["repository_mutation"],
            diagnostic_process=effects["diagnostic_process"],
            acceptance_verdict=effects["acceptance_verdict"],
        )
        closure = normalize_closure(item["closure"])
        judgment = derive_judgment(closure)
        if judgment == "unresolved":
            raise GraphCompilerError("unresolved judgment stays in Primary")
        route = _route_for(plan, purpose, judgment)
        placement_facts = item["placement"]
        if not isinstance(placement_facts, Mapping) or set(placement_facts) != {
            "benefits",
            "primary_model",
        }:
            raise GraphCompilerError("node placement facts are malformed")
        benefits = normalize_placement_benefits(placement_facts["benefits"])
        placement = select_placement(
            purpose=purpose,
            primary_model=placement_facts["primary_model"],
            selected_model=route["selected"]["model"],
            benefits=benefits,
        )
        if route["placement"] != placement:
            raise GraphCompilerError("route placement does not match derived placement")
        acceptance_facts = item["acceptance_facts"]
        if not isinstance(acceptance_facts, Mapping):
            raise GraphCompilerError("node acceptance facts are malformed")
        acceptance = derive_acceptance(**acceptance_facts)
        decision = {
            "acceptance_facts": {
                "acceptance_ids": list(acceptance_facts["acceptance_ids"]),
                "deterministic_graph_coverage": list(
                    acceptance_facts["deterministic_graph_coverage"]
                ),
                "events": list(acceptance_facts["events"]),
                "required_verification_strengths": list(
                    acceptance_facts["required_verification_strengths"]
                ),
                "risk_assessment": normalize_risk_assessment(
                    acceptance_facts["risk_assessment"]
                ),
            },
            "closure": closure,
            "derived": {
                "acceptance": acceptance,
                "judgment": judgment,
                "placement": placement,
                "purpose": purpose,
            },
            "effects": {name: effects[name] for name in sorted(effects)},
            "placement": {
                "benefits": benefits,
                "primary_model": placement_facts["primary_model"],
            },
        }
        return normalize_dispatch_decision(
            decision, selected_model=route["selected"]["model"]
        )
    except (DecisionPolicyError, KeyError, TypeError) as error:
        if isinstance(error, GraphCompilerError):
            raise
        raise GraphCompilerError(f"node decision facts are invalid: {error}") from error


def _route_variants(
    plan: dict[str, Any],
    *,
    decision: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Precompute every fallback that preserves the closed child placement."""

    derived = decision["derived"]
    purpose = str(derived["purpose"])
    judgment = str(derived["judgment"])
    variants = [plan]
    current = plan
    while True:
        route = _route_for(current, purpose, judgment)
        rank = int(route["dispatch"]["rank"])
        if rank >= len(route["candidates"]):
            break
        selected = route["selected"]
        try:
            advanced = advance_route_plan(
                current,
                purpose=purpose,
                judgment=judgment,
                rejected_model=selected["model"],
                rejected_effort=selected["effort"],
                rejection_ticket=f"native:prethread-rejected-r{rank:02d}",
            )
            next_route = _route_for(advanced, purpose, judgment)
            normalize_dispatch_decision(
                decision,
                selected_model=next_route["selected"]["model"],
            )
        except (DecisionPolicyError, RoutingCatalogError) as error:
            if isinstance(error, DecisionPolicyError):
                break
            raise GraphCompilerError(f"route fallback is invalid: {error}") from error
        variants.append(advanced)
        current = advanced
    return variants


def _route_bindings(
    variants: list[dict[str, Any]], *, purpose: str, judgment: str
) -> list[dict[str, Any]]:
    return [
        {
            "plan_sha256": variant["plan_sha256"],
            "rank": route["dispatch"]["rank"],
            "selected": dict(route["selected"]),
        }
        for variant in variants
        for route in [_route_for(variant, purpose, judgment)]
    ]


def _baseline_path(session_id: str, graph_sha256: str) -> Path:
    configured = os.environ.get("CCO_LEDGER_DIR")
    configured_root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    )
    ledger_root = Path(os.path.abspath(configured_root)).resolve()
    return artifact_path(ledger_root, session_id, graph_sha256)


def prepare_dispatch_graph(
    nodes: object,
    *,
    route_plan: object,
    native_capacity: int,
    repo: Path,
    workspace_mode: str = "light",
    ignored_max_files: int = DEFAULT_IGNORED_MAX_FILES,
    ignored_max_bytes: int = DEFAULT_IGNORED_MAX_BYTES,
) -> dict[str, Any]:
    """Derive, place, baseline, and compile one graph through the safe entry."""

    if not isinstance(nodes, list) or not nodes:
        raise GraphCompilerError("graph nodes must be a non-empty list")
    if workspace_mode not in WORKSPACE_MODES:
        raise GraphCompilerError("workspace mode must be light or strict")
    session_id = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise GraphCompilerError("CODEX_THREAD_ID is required for task-local baselines")
    try:
        plan = validate_route_plan(route_plan)
    except RoutingCatalogError as error:
        raise GraphCompilerError(f"route plan is invalid: {error}") from error

    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(nodes):
        if not isinstance(value, Mapping) or set(value) - OPTIONAL_NODE_FIELDS != NODE_FIELDS:
            raise GraphCompilerError(f"graph node {index} has unsupported or missing fields")
        node = value["node"]
        contract = value["contract"]
        if (
            not isinstance(node, str)
            or not isinstance(contract, Mapping)
            or contract.get("node") != node
        ):
            raise GraphCompilerError(f"graph node {index} identity is inconsistent")
        decision = _decision(value, plan)
        route_variants = (
            _route_variants(plan, decision=decision)
            if decision["derived"]["placement"]["target"] == "child"
            else [plan]
        )
        scopes = _scopes(value["scopes"], f"graph node {index}.scopes")
        selection = value["selection"]
        if not isinstance(selection, Mapping) or set(selection) != {
            "dependencies_ready",
            "responsibility",
        }:
            raise GraphCompilerError(f"graph node {index} selection is malformed")
        normalized.append(
            {
                **{name: value[name] for name in OPTIONAL_NODE_FIELDS if name in value},
                "contract": dict(contract),
                "decision": decision,
                "generation": value["generation"],
                "node": node,
                "route_plans": route_variants,
                "scopes": scopes,
                "selection": dict(selection),
            }
        )
    if len({item["node"] for item in normalized}) != len(normalized):
        raise GraphCompilerError("graph node identities must be unique")

    root = repository_root(Path(repo))
    snapshot = state_payload(
        root,
        ignored_mode=workspace_mode,
        ignored_max_files=ignored_max_files,
        ignored_max_bytes=ignored_max_bytes,
    )
    manifest = {
        "baseline": snapshot["state_id"],
        "nodes": [
            {
                "contract": item["contract"],
                "decision": item["decision"],
                "node": item["node"],
                "route_bindings": _route_bindings(
                    item["route_plans"],
                    purpose=str(item["decision"]["derived"]["purpose"]),
                    judgment=str(item["decision"]["derived"]["judgment"]),
                ),
                "scopes": item["scopes"],
            }
            for item in sorted(normalized, key=lambda entry: entry["node"])
        ],
        "protocol": GRAPH_PROTOCOL,
        "route_plan_sha256": plan["plan_sha256"],
        "workspace_mode": workspace_mode,
    }
    graph_sha256 = _digest(manifest)
    baseline_path = _baseline_path(session_id, graph_sha256)

    selector_nodes = [
        {
            "access": (
                "write"
                if item["decision"]["derived"]["purpose"] == "implementation"
                else "read"
            ),
            "dependencies_ready": item["selection"]["dependencies_ready"],
            "node": item["node"],
            "responsibility": item["selection"]["responsibility"],
            "scope": item["scopes"],
        }
        for item in normalized
        if item["decision"]["derived"]["placement"]["target"] == "child"
    ]
    try:
        selected = select_ready_nodes(selector_nodes, native_capacity=native_capacity)
    except DecisionPolicyError as error:
        raise GraphCompilerError(f"graph capacity selection failed: {error}") from error
    by_node = {item["node"]: item for item in normalized}
    dispatches: list[dict[str, Any]] = []
    fallback_dispatches: dict[str, list[dict[str, Any]]] = {}
    for node in selected:
        item = by_node[node]
        derived = item["decision"]["derived"]
        purpose = derived["purpose"]
        kind = (
            "work"
            if purpose == "implementation"
            else "review" if purpose == "acceptance" else "analysis"
        )
        spec: dict[str, Any] = {
            "acceptance": derived["acceptance"],
            "baseline": snapshot["state_id"],
            "contract": item["contract"],
            "decision": item["decision"],
            "fork_turns": item.get("fork_turns", "none"),
            "generation": item["generation"],
            "graph_sha256": graph_sha256,
            "judgment": derived["judgment"],
            "kind": kind,
            "mode": "fresh" if kind == "review" else workspace_mode,
            "node": node,
            "purpose": purpose,
            "scopes": item["scopes"],
        }
        for name in ("current_state", "epoch", "evidence"):
            if name in item:
                spec[name] = item[name]
        compiled: list[dict[str, Any]] = []
        for variant in item["route_plans"]:
            try:
                compiled.append(compile_dispatch({**spec, "route_plan": variant}))
            except CapsuleError as error:
                raise GraphCompilerError(
                    f"graph node {node} could not compile: {error}"
                ) from error
        dispatches.append(compiled[0])
        fallback_dispatches[node] = compiled[1:]
    write_artifact(
        root,
        baseline_path,
        manifest=manifest,
        snapshot=snapshot,
    )
    return {
        "baseline": snapshot["state_id"],
        "baseline_path": str(baseline_path),
        "dispatches": dispatches,
        "fallback_dispatches": fallback_dispatches,
        "graph_sha256": graph_sha256,
        "manifest": manifest,
        "protocol": PROTOCOL,
        "route_plan": plan,
    }


def verify_prepared_graph(prepared: object, *, repo: Path) -> dict[str, Any]:
    """Verify the current workspace against one exact prepared graph baseline."""

    required = {
        "baseline",
        "baseline_path",
        "dispatches",
        "fallback_dispatches",
        "graph_sha256",
        "manifest",
        "protocol",
        "route_plan",
    }
    if not isinstance(prepared, Mapping) or set(prepared) != required:
        raise GraphCompilerError("prepared graph is malformed")
    if prepared.get("protocol") != PROTOCOL:
        raise GraphCompilerError("prepared graph protocol is invalid")
    manifest = prepared.get("manifest")
    if not isinstance(manifest, Mapping) or _digest(manifest) != prepared.get("graph_sha256"):
        raise GraphCompilerError("prepared graph identity does not match its manifest")
    baseline_path = Path(str(prepared.get("baseline_path")))
    try:
        artifact = load_artifact(baseline_path)
        baseline = artifact["snapshot"]
    except ValueError as error:
        raise GraphCompilerError(f"prepared graph baseline is invalid: {error}") from error
    if (
        baseline["state_id"] != prepared.get("baseline")
        or manifest.get("baseline") != prepared.get("baseline")
        or artifact["manifest"] != manifest
        or artifact["graph_sha256"] != prepared.get("graph_sha256")
    ):
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
